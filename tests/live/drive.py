"""Pilote le serveur MCP réel (stdio) contre les applications du labo, comme Claude Code le ferait.

Chaque action est jugée par l'événement que l'application de test émet (stdout ou journal de page) ; là où
l'application n'en émet pas (champ Nom des applications de bureau, Notes), par la relecture de l'outil (« relu ✓ »
exigé), jamais par un simple compte rendu sans relecture. Trois volets par famille :
- étapes unitaires (un act par action) ;
- lot (un seul act pour tout le scénario : c'est le flux recommandé, et celui où un état périmé mentait) ;
- état changé entre deux appels (« Julien » coche la case lui-même — BM_SETCHECK puis BN_CLICKED pour un bouton
  Win32/WinForms, dont le proxy UIA prendrait le premier plan ; UIA ailleurs —, puis l'outil la décoche par son nom).
Plus : Tk (doit refuser honnêtement), Calculatrice UWP (12 + 30 = 42 après un contenu UIA stable ; touche par
messages refusée), DPI. Banc intrusif : refusé si Julien a utilisé clavier ou souris depuis moins de 2 min.
Textes longs (Chromium sans DevTools : frappe par tranches) : 731 et 700 caractères sur 6 lignes dans Notes, 700 dans
l'Éditeur, 700 sur 6 lignes dans l'éditeur riche « Paragraphes » (<p><br></p> au départ) ; contenu exact prouvé par
l'empreinte que la page calcule sur son DOM, « relu ✓ » exigé, et chaque prise du premier plan par la cible ≤ 0,5 s.
Usage : python tests/live/drive.py [win32 winforms wpf electron edge occult tk calc] ; images : ~/.claude/nexus-claude/lab/drive/
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lab"))

import apps  # noqa: E402
from mcp import Client, StdioServerParameters  # noqa: E402
from PIL import Image  # noqa: E402

from nexus_windows import foreground, methods, snapshot, uia  # noqa: E402
from nexus_windows import win32 as w  # noqa: E402

OUT = apps.STATE / "drive"
OUT.mkdir(parents=True, exist_ok=True)
WEB = ("electron", "edge")


def fingerprint(text: str) -> str:
    """Empreinte FNV-1a 32 bits (UTF-8), la même que celle de web_app.html."""
    value = 0x811C9DC5
    for byte in text.encode("utf-8"):
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return f"{value:08x}"


def _lines(count: int, total: int) -> str:
    """`count` lignes, `total` caractères sauts de ligne compris, mots tous différents, aucune ligne finie par une
    espace (un éditeur riche la garderait en espace insécable)."""
    body = total - (count - 1)
    sizes = [body // count + (1 if i < body % count else 0) for i in range(count)]
    lines, word = [], 0
    for i, size in enumerate(sizes):
        line = f"Ligne {i} :"
        while len(line) < size:
            line += f" mot{word:03d}"
            word += 1
        line = line[:size]
        lines.append(line[:-1] + "." if line.endswith(" ") else line)
    return "\n".join(lines)


# Textes longs : le constat de la contre-vérification n° 6 (731 caractères sur 6 lignes) et des textes sans motif
# répété (un doublon ou un saut de ligne déplacé change l'empreinte).
LONG_731 = "\n".join(f"{i}" + "abcdefghij" * 12 for i in range(6))
LINES_700 = _lines(6, 700)
PROSE_700 = " ".join(f"Phrase {i:03d} du compte rendu." for i in range(25)) + "!"
TWO_LINES = "Bonjour Julien\nVoici le compte rendu"
COURRIEL = "Bonjour Julien,\n\nVoici le compte rendu de la réunion.\n\nCordialement"  # lignes vides (recheck8)
STRESS_300 = " ".join(f"Mot{i:03d}" for i in range(60))[:299] + "."
assert len(LONG_731) == 731 and len(LINES_700) == 700 and LINES_700.count("\n") == 5 and len(PROSE_700) == 700
assert len(STRESS_300) == 300


def _field(name: str, text: str):
    return lambda e: e == f"{name} {len(text)} #{fingerprint(text)}"


def _notes(text: str):
    return lambda e: e == f"notes {len(text)} #{fingerprint(text)}"


def _paragraphs(text: str):
    lines = text.count("\n") + 1
    return lambda e: e == f"paragraphes {lines} lignes {len(text)} car. #{fingerprint(text)}"


def _editor(text: str):
    return lambda e: e.replace("\xa0", " ") == f"editeur {text}"


SCENARIO = [
    ("click Valider", [{"do": "click", "target": "Valider"}], lambda e: e.startswith("clic")),
    ("check Option", [{"do": "check", "target": "Option"}], lambda e: e == "coche on"),
    ("uncheck Option", [{"do": "uncheck", "target": "Option"}], lambda e: e == "coche off"),
    ("set Nom", [{"do": "set", "target": "champ:Nom", "text": "Julien Piron"}], None),
    ("type Nom", [{"do": "type", "target": "champ:Nom", "text": " 42"}], None),
    ("select Couleur", [{"do": "select", "target": "Couleur", "option": "Bleu"}], lambda e: e == "couleur Bleu"),
    ("click Charlie", [{"do": "click", "target": "Charlie"}], lambda e: e == "liste Charlie"),
    ("menu", [{"do": "menu", "path": "Fichier > Action menu"}], lambda e: e == "menu action"),
    ("key ctrl+k", [{"do": "key", "keys": "ctrl+k"}], lambda e: e == "raccourci ctrl+k"),
    ("set Notes 2 lignes", [{"do": "set", "target": "Notes", "text": "ligne1\nligne2"}], None),
    ("type Éditeur", [{"do": "type", "target": "Éditeur", "text": "Bonjour"}], lambda e: e.startswith("editeur") and "Bonjour" in e),
    ("type Éditeur bis", [{"do": "type", "target": "Éditeur", "text": " encore"}], lambda e: "encore" in e),
    ("set Notes 731 sur 6 lignes", [{"do": "set", "target": "Notes", "text": LONG_731}], _notes(LONG_731)),
    ("set Notes 700 sur 6 lignes", [{"do": "set", "target": "Notes", "text": LINES_700}], _notes(LINES_700)),
    ("set Éditeur 700", [{"do": "set", "target": "Éditeur", "text": PROSE_700}], _editor(PROSE_700)),
    ("type Paragraphes 700 sur 6 lignes", [{"do": "type", "target": "Paragraphes", "text": LINES_700}], _paragraphs(LINES_700)),
    ("set Paragraphes 2 lignes", [{"do": "set", "target": "Paragraphes", "text": TWO_LINES}], _paragraphs(TWO_LINES)),
    ("set Paragraphes courriel", [{"do": "set", "target": "Paragraphes", "text": COURRIEL}], _paragraphs(COURRIEL)),
    # Pages chargées (recheck8 : doublons avant 0.5.0) : moteur figé 1,5 s, 12 ms par saisie, arbre lourd.
    ("set Gel 300", [{"do": "set", "target": "Gel", "text": STRESS_300}], _field("gel", STRESS_300)),
    ("set Lent 300", [{"do": "set", "target": "Lent", "text": STRESS_300}], _field("lent", STRESS_300)),
    ("set Lourd 200", [{"do": "set", "target": "Lourd", "text": STRESS_300[:200]}], _field("lourd", STRESS_300[:200])),
    ("click onglet Avancé", [{"do": "click", "target": "Avancé"}], lambda e: e == "onglet Avancé"),
]  # fmt: skip
# Textes longs (frappe par tranches en Chromium sans DevTools) : contenu exact (empreinte du DOM), relu, et chaque
# prise du premier plan par la cible ≤ methods.SLICE_S.
LONG_TEXT = {
    "set Notes 731 sur 6 lignes", "set Notes 700 sur 6 lignes", "set Éditeur 700", "type Paragraphes 700 sur 6 lignes",
    "set Paragraphes 2 lignes", "set Paragraphes courriel", "set Gel 300", "set Lent 300", "set Lourd 200",
}  # fmt: skip
WEB_ONLY = {"type Éditeur", "type Éditeur bis"} | LONG_TEXT
NOT_WEB = {"menu"}
# Saisies : la page web émet « nom … » ; les applications de bureau n'émettent rien pour ce champ.
WEB_EXPECT = {"set Nom": lambda e: e == "nom Julien Piron", "type Nom": lambda e: e == "nom Julien Piron 42"}
READ_BACK = {"set Nom", "type Nom", "set Notes 2 lignes"} | LONG_TEXT  # « relu ✓ » exigé dans le compte rendu
DESKTOP_VALUE = {"set Nom": "Julien Piron", "type Nom": "Julien Piron 42"}  # valeur du champ relue à part


def field_value(hwnd: int, name: str) -> str:
    """Valeur d'un champ lue par une instance UI Automation à part (oracle indépendant de l'outil)."""
    uia.com_init()
    auto = uia.Automation()
    node = next((n for n in auto.tree(hwnd).walk() if n.control_type == "Edit" and n.name == name), None)
    return str(auto.refresh(node.element).get("value") or "") if node is not None else "(champ introuvable)"


WIN32_ONLY = {"click onglet Avancé"}  # contrôle d'onglets Win32 : l'action MSAA par défaut y ferait un VRAI clic
BATCH = [
    {"do": "click", "target": "Valider"},
    {"do": "check", "target": "Option"},
    {"do": "uncheck", "target": "Option"},
    {"do": "set", "target": "champ:Nom", "text": "Lot"},
    {"do": "select", "target": "Couleur", "option": "Vert"},
    {"do": "click", "target": "Bravo"},
]
BATCH_EVENTS = ["clic", "coche on", "coche off", "couleur Vert", "liste Bravo"]


def server_params() -> StdioServerParameters:
    """Serveur MCP réel, isolé : journaux et profils DevTools dans un dossier de test (jamais ceux de Julien). La
    politique est la commune (shared/garde-fous.json)."""
    home = apps.STATE / "server-home"
    home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "NEXUS_WINDOWS_HOME": str(home), "PYTHONIOENCODING": "utf-8"}
    return StdioServerParameters(command=sys.executable, args=["-m", "nexus_windows"], env=env)


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def image_of(result) -> Image.Image | None:
    for c in result.content:
        if getattr(c, "type", "") == "image":
            return Image.open(io.BytesIO(base64.b64decode(c.data))).convert("RGB")
    return None


def step_ok(report_line: str) -> bool:
    match = re.match(r"^\s*\d+\. ([✓~✗])", report_line or "")
    return bool(match) and match.group(1) in "✓~"


class ForegroundHolds:
    """Prises du premier plan par la cible pendant un appel, relevées toutes les millisecondes (fenêtre de la cible
    ou de son processus, fil de rendu Chromium compris)."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd, self.pid = hwnd, w.window_pid(hwnd)
        self.holds: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="drive-foreground-holds", daemon=True)

    def _ours(self, fg: int) -> bool:
        return bool(fg) and (w.root(fg) == self.hwnd or w.window_pid(fg) == self.pid)

    def _run(self) -> None:
        since = None
        while not self._stop.is_set():
            now = time.perf_counter()
            if self._ours(w.foreground()):
                since = now if since is None else since
            elif since is not None:
                self.holds.append(now - since)
                since = None
            time.sleep(0.001)
        if since is not None:
            self.holds.append(time.perf_counter() - since)

    def __enter__(self) -> ForegroundHolds:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(2)

    @property
    def longest_ms(self) -> int:
        return round(max(self.holds, default=0.0) * 1000)

    def summary(self) -> str:
        return f"premier plan pris {len(self.holds)} fois, au plus {self.longest_ms} ms"


def julien_checks(auto: uia.Automation, box: uia.Node) -> None:
    """« Julien » coche la case lui-même. Bouton Win32/WinForms : BM_SETCHECK puis BN_CLICKED au parent, comme son
    clic (le proxy UIA de ces boutons appelle SetForegroundWindow : il prendrait son premier plan). Ailleurs : UIA."""
    # Bouton Win32 pur seulement : une case WinForms garde son propre état (BM_SETCHECK ne le change pas) et son
    # fournisseur UIA ne touche pas au premier plan.
    if box.hwnd and w.class_name(box.hwnd).lower() == "button":
        w.send_message(box.hwnd, w.BM_SETCHECK, 1)
        parent = w.handle(w.GetParent(box.hwnd))
        methods.post(parent, w.WM_COMMAND, (w.BN_CLICKED << 16) | (w.GetDlgCtrlID(box.hwnd) & 0xFFFF), box.hwnd)
    else:
        auto.toggle(box.element)


async def wait_stable(client: Client, names: tuple[str, ...], timeout_s: float = 5.0) -> str:
    """Attend que la liste d'éléments de la fenêtre courante soit stable (deux relevés identiques) et contienne
    `names` : une application UWP publie son arbre UIA par étapes après l'apparition de sa fenêtre."""
    deadline = time.monotonic() + timeout_s
    previous: list[str] | None = None
    while True:
        text = text_of(await client.call_tool("look", {"show": "elements", "max_elements": 600}))
        elements = [line.strip() for line in text.splitlines() if line.strip().startswith("[")]
        if elements == previous and all(any(f"« {n} »" in line for line in elements) for n in names):
            return text
        if time.monotonic() >= deadline:
            return text
        previous = elements
        await asyncio.sleep(0.2)


class Events:
    """Événements de l'application de test : stdout (Win32, WinForms, WPF) ou journal de la page lu par UIA
    (Electron, Edge : le titre de fenêtre fusionne les changements rapides, le journal les garde tous)."""

    def __init__(self, app: apps.LabApp) -> None:
        self.app = app
        self.web = app.kind in WEB
        self.consumed = 0
        if self.web:
            uia.com_init()
            self.auto = uia.Automation()

    def _journal(self) -> list[str]:
        # Fenêtre recouverte et « occluded » : Chromium ne publie les changements qu'à une action d'accessibilité ;
        # l'observateur déclenche la publication comme l'outil (ScrollIntoView du Document racine, sans effet).
        snapshot.flush_web(self.auto, self.app.hwnd)
        tree = self.auto.tree(self.app.hwnd)
        snapshot.remember_web_documents(self.app.hwnd, tree)
        journal = next((n for n in tree.walk() if n.name == "Journal" and n.control_type == "List"), None)
        return [c.name for c in journal.walk() if c.control_type == "ListItem"] if journal else []

    def reset(self) -> None:
        self.app.drain()
        if self.web:
            self.consumed = len(self._journal())

    def collect(self, wait_s: float, until=None) -> list[str]:
        seen: list[str] = []
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.web:
                items = self._journal()
                seen += items[self.consumed :]
                self.consumed = len(items)
                time.sleep(0.05)
            else:
                seen += self.app.last_events(0.05)
            if until and until(seen):
                break
        return seen


OCCLUDED_SCENARIO = [
    ("click Charlie", [{"do": "click", "target": "Charlie"}], lambda e: e == "liste Charlie"),
    ("click Bravo", [{"do": "click", "target": "Bravo"}], lambda e: e == "liste Bravo"),
    ("click Alpha", [{"do": "click", "target": "Alpha"}], lambda e: e == "liste Alpha"),
    ("check Option", [{"do": "check", "target": "Option"}], lambda e: e == "coche on"),
    ("click Valider expect", [{"do": "click", "target": "Valider", "expect": "clic 1"}], lambda e: e == "clic 1"),
    ("set Nom", [{"do": "set", "target": "champ:Nom", "text": "Occulté"}], lambda e: e == "nom Occulté"),
]  # fmt: skip


async def drive_occluded() -> list[tuple[str, bool, str]]:
    """Electron du labo (sans commutateur) occulté : déplacé hors écran sans activation, son moteur de rendu passe
    « hidden » (mesuré : document.visibilityState) et ne publie plus les changements d'accessibilité qu'à l'action
    suivante — l'état qu'une activation brève laisse derrière elle quand la fenêtre est recouverte. Aucune étape
    n'active la fenêtre : chaque effet doit être relu à jour (✓, événement de la page), jamais en retard d'un cran."""
    app = apps.launch("electron", x=150, y=110)  # Julien absent exigé ; premier plan rendu s'il est pris
    rows: list[tuple[str, bool, str]] = []
    events = Events(app)
    try:
        async with Client(server_params(), read_timeout_seconds=120) as client:
            await client.call_tool("look", {"window": app.hwnd, "show": "elements"})  # arbre construit, visible
            w.SetWindowPos(app.hwnd, 1, -4000, -4000, 0, 0, 0x1 | 0x10)  # SWP_NOSIZE | SWP_NOACTIVATE, en bas
            time.sleep(1.0)
            print("== electron occulté (hors écran, jamais activé)")
            fg0 = w.foreground()
            for label, steps, expect in OCCLUDED_SCENARIO:
                events.reset()
                t0 = time.perf_counter()
                result = await client.call_tool("act", {"window": app.hwnd, "steps": steps, "then": "none"})
                ms = (time.perf_counter() - t0) * 1000
                report = text_of(result).splitlines()[0] if text_of(result) else "(vide)"
                ok = bool(re.match(r"^\s*\d+\. ✓", report)) and not result.is_error  # effet relu exigé
                seen = events.collect(1.5, lambda s: any(expect(e) for e in s))
                ok = ok and any(expect(e) for e in seen) and len(seen) == 1  # ni en retard, ni en double
                rows.append((f"occulté {label}", ok, report))
                print(f"   {'OK ' if ok else 'NON'} {label:20} {ms:5.0f} ms  {report[:150]}   ⇒ {seen[-3:]}")
            rows.append(("occulté premier plan intact", w.foreground() == fg0, ""))
    finally:
        app.close()
    return rows


async def drive(kind: str) -> list[tuple[str, bool, str]]:
    if kind == "occult":
        return await drive_occluded()
    if kind == "tk":
        return await drive_tk()
    if kind == "calc":
        return await drive_calc()
    app = apps.launch(kind, x=150, y=110)  # Julien absent exigé ; premier plan rendu s'il est pris
    rows: list[tuple[str, bool, str]] = []
    events = Events(app)
    try:
        async with Client(server_params(), read_timeout_seconds=120) as client:
            look = await client.call_tool("look", {"window": app.hwnd})
            head = text_of(look).splitlines()
            print(f"== {kind} : {head[0][:100]}")
            if kind == "win32":  # application non adaptée au DPI : l'image doit être pleine (pas de bande noire)
                image = image_of(look)
                assert image is not None, "look sans image"
                corner = image.crop((image.width - 60, image.height - 60, image.width - 10, image.height - 10))
                px = corner.tobytes()  # RVB : 3 octets par pixel
                dark = sum(1 for i in range(0, len(px), 3) if px[i] + px[i + 1] + px[i + 2] < 60) / (50 * 50)
                rows.append(("capture DPI", dark < 0.5, f"coin bas-droit sombre à {dark:.0%}"))
                print(f"   {'OK ' if dark < 0.5 else 'NON'} capture DPI      coin bas-droit sombre à {dark:.0%}")
            for label, steps, expect in SCENARIO:
                if (kind in WEB and label in NOT_WEB) or (kind not in WEB and label in WEB_ONLY):
                    continue
                if kind != "win32" and label in WIN32_ONLY:
                    continue
                if label == "set Notes 2 lignes" and kind not in WEB:
                    continue
                events.reset()
                fg0 = w.foreground()
                t0 = time.perf_counter()
                with ForegroundHolds(app.hwnd) as holds:
                    result = await client.call_tool("act", {"window": app.hwnd, "steps": steps, "then": "none"})
                ms = (time.perf_counter() - t0) * 1000
                report = text_of(result).splitlines()[0] if text_of(result) else "(vide)"
                ok = step_ok(report) and not result.is_error
                if expect is None and kind in WEB:
                    expect = WEB_EXPECT.get(label)
                if expect is not None:
                    wanted = expect  # fixé pour le lambda (expect est réaffecté à chaque tour)
                    seen = events.collect(1.5, lambda s: any(wanted(e) for e in s))
                    ok = ok and any(wanted(e) for e in seen)
                    report += f"   ⇒ {[e[:60] for e in seen[-3:]]}"
                if label in READ_BACK:
                    ok = ok and "read back ✓" in report
                if label in DESKTOP_VALUE and kind not in WEB:
                    # Oracle externe (relecture de Codex) : la valeur du champ relue par une lecture indépendante,
                    # pas seulement le « read back ✓ » de l'outil.
                    value = field_value(app.hwnd, "Nom")
                    ok = ok and value == DESKTOP_VALUE[label]
                    report += f"   ⇒ champ « {value} »"
                if label in LONG_TEXT:  # chaque tranche d'activation brève : premier plan de Julien rendu à 0,5 s
                    ok = ok and holds.longest_ms <= methods.SLICE_S * 1000
                    report += f"   [{holds.summary()}]"
                stolen = w.foreground() != fg0
                if stolen and fg0:
                    ok = False  # premier plan de l'utilisateur non rendu à la fin de l'appel : échec, pas un avertissement
                    report += "   [foreground NOT given back]"
                rows.append((label, ok, report))
                shown = report if label in LONG_TEXT else report[:160]
                print(f"   {'OK ' if ok else 'NON'} {label:18} {ms:5.0f} ms{' FOCUS !' if stolen else ''}  {shown}")
                if stolen and fg0:
                    foreground.give_back(fg0)
            # Lot : un seul act, chaque effet prouvé par l'application.
            events.reset()
            t0 = time.perf_counter()
            result = await client.call_tool("act", {"window": app.hwnd, "steps": BATCH, "then": "none"})
            ms = (time.perf_counter() - t0) * 1000
            seen = events.collect(2.0, lambda s: all(any(e.startswith(x) for e in s) for x in BATCH_EVENTS))
            missing = [x for x in BATCH_EVENTS if not any(e.startswith(x) for e in seen)]
            lines = text_of(result).splitlines()
            ok = not missing and all(step_ok(line) for line in lines[: len(BATCH)])
            rows.append(("lot de 6 étapes", ok, f"manquants {missing} ; {' | '.join(lines[:6])[:300]}"))
            print(f"   {'OK ' if ok else 'NON'} lot de 6 étapes   {ms:5.0f} ms  manquants={missing}")
            # État changé entre deux appels : « Julien » coche la case lui-même, l'outil doit la décocher vraiment.
            uia.com_init()
            auto = uia.Automation()
            box = next(n for n in auto.tree(app.hwnd).walk() if n.control_type == "CheckBox" and n.name == "Option")
            if auto.refresh(box.element).get("toggle") != 1:
                julien_checks(auto, box)
            time.sleep(0.3)
            events.reset()
            result = await client.call_tool(
                "act", {"window": app.hwnd, "steps": [{"do": "uncheck", "target": "Option"}], "then": "none"}
            )
            seen = events.collect(1.5, lambda s: "coche off" in s)
            report = text_of(result).splitlines()[0]
            ok = "coche off" in seen and step_ok(report)
            rows.append(("état changé entre appels", ok, report))
            print(f"   {'OK ' if ok else 'NON'} état changé       {report[:150]} ⇒ {seen[-2:]}")
    finally:
        app.close()
    return rows


async def drive_tk() -> list[tuple[str, bool, str]]:
    """Tk n'a ni UI Automation ni canal de messages efficace : l'outil doit refuser, jamais annoncer un succès."""
    app = apps.launch("tk", x=150, y=110)  # Julien absent exigé ; premier plan rendu s'il est pris
    rows = []
    try:
        async with Client(server_params(), read_timeout_seconds=120) as client:
            await client.call_tool("look", {"window": app.hwnd, "show": "none"})
            await client.call_tool("look", {"window": app.hwnd})
            app.drain()
            result = await client.call_tool(
                "act", {"window": app.hwnd, "steps": [{"do": "click", "target": "300,250"}], "then": "none"}
            )
            report = text_of(result).splitlines()[0]
            events = app.last_events(1.0)
            honest = ("✗" in report and "foreground=true" in report) or bool(events)
            rows.append(("tk clic : honnête", honest, f"{report[:160]} ⇒ {events}"))
            print(f"== tk\n   {'OK ' if honest else 'NON'} clic honnête       {report[:160]} ⇒ {events}")
    finally:
        app.close()
    return rows


async def drive_calc() -> list[tuple[str, bool, str]]:
    apps.require_julien_away()
    rows = []
    async with Client(server_params(), read_timeout_seconds=120) as client:
        fg0 = w.foreground()
        result = await client.call_tool("launch", {"app": "Calculatrice", "then": "elements"})
        print("== calc : " + text_of(result).splitlines()[0][:100])
        for note in (line for line in text_of(result).splitlines() if "Store" in line or "contenu" in line):
            print("   lancement : " + note[:160])  # application suspendue, reprise, contenu vide : la cause visible
        try:
            # launch rend la main dès la fenêtre ; l'arbre UIA de la Calculatrice arrive ensuite, par étapes.
            names = ("Un", "Deux", "Plus", "Trois", "Zéro", "Est égal à")
            ready = await wait_stable(client, names)
            print("   " + next((line for line in ready.splitlines() if "elements from" in line), "")[:100])
            steps = [{"do": "click", "target": n} for n in names[:-1]]
            steps.append({"do": "click", "target": "Est égal à", "expect": "42"})
            result = await client.call_tool("act", {"steps": steps, "then": "elements"})
            text = text_of(result)
            # Premier plan de l'utilisateur rendu exigé (relecture de Codex : imprimé, jamais compté).
            ok = "est 42" in text and "✗" not in text.split("\n[")[0] and w.foreground() == fg0
            rows.append(("calc 12+30=42", ok, text.splitlines()[-1][:120]))
            print(f"   {'OK ' if ok else 'NON'} 12+30=42           focus intact={w.foreground() == fg0}")
            result = await client.call_tool("act", {"steps": [{"do": "key", "keys": "7"}], "then": "none"})
            report = text_of(result).splitlines()[0]
            honest = "✗" in report  # UWP ignore les touches postées : refus explicite attendu, pas un faux ✓
            rows.append(("calc touche : honnête", honest, report))
            print(f"   {'OK ' if honest else 'NON'} touche honnête     {report[:150]}")
        finally:
            await client.call_tool("window", {"action": "close"})
    return rows


DEVTOOLS_SCENARIO = [
    ("select Couleur", [{"do": "select", "target": "Couleur", "option": "Bleu"}], lambda e: e == "couleur Bleu"),
    ("key ctrl+k", [{"do": "key", "keys": "ctrl+k"}], lambda e: e == "raccourci ctrl+k"),
    ("set Notes 2 lignes", [{"do": "set", "target": "Notes", "text": "ligne1\nligne2"}], None),
    ("type Éditeur", [{"do": "type", "target": "Éditeur", "text": "Bonjour"}], lambda e: e.startswith("editeur") and "Bonjour" in e),
    ("type Éditeur bis", [{"do": "type", "target": "Éditeur", "text": " encore"}], lambda e: "encore" in e),
]  # fmt: skip


async def drive_devtools(kind: str) -> list[tuple[str, bool, str]]:
    """Remède annoncé quand l'activation est impossible : application lancée par l'outil avec devtools=true. Les
    frappes, raccourcis et <select> passent alors par DevTools, sans jamais activer la fenêtre."""
    apps.require_julien_away()
    if kind == "edge":
        exe = apps.edge_path()
        args = f"--window-size=600,640 --app={(apps.LAB / 'web_app.html').as_uri()}"
    else:
        exe = apps.ELECTRON
        args = f'"{apps.LAB / "electron_main.js"}" --lab-x=150 --lab-y=110'
    rows: list[tuple[str, bool, str]] = []
    async with Client(server_params(), read_timeout_seconds=120) as client:
        fg0 = w.foreground()
        result = await client.call_tool("launch", {"app": str(exe), "args": args, "devtools": True, "then": "elements"})
        text = text_of(result)
        match = re.search(r"Window (\d+)", text)  # comptes rendus en anglais depuis 0.10.0
        print(f"== {kind} DevTools : {text.splitlines()[0][:120]}")
        if not match:
            return [("lancement DevTools", False, text[:300])]
        hwnd = int(match.group(1))
        app = apps.LabApp(kind, proc=None, hwnd=hwnd)  # type: ignore[arg-type]
        events = Events(app)
        try:
            if "DevTools 127.0.0.1" not in text:
                return [("lancement DevTools", False, text[:300])]
            for label, steps, expect in DEVTOOLS_SCENARIO:
                events.reset()
                fg = w.foreground()
                t0 = time.perf_counter()
                result = await client.call_tool("act", {"window": hwnd, "steps": steps, "then": "none"})
                ms = (time.perf_counter() - t0) * 1000
                report = text_of(result).splitlines()[0] if text_of(result) else "(vide)"
                ok = step_ok(report) and not result.is_error and "DevTools" in report
                if expect is not None:
                    seen = events.collect(1.5, lambda s: any(expect(e) for e in s))
                    ok = ok and any(expect(e) for e in seen)
                    report += f"   ⇒ {seen[-3:]}"
                if label == "set Notes 2 lignes":
                    ok = ok and "read back ✓" in report
                stolen = w.foreground() != fg
                ok = ok and not stolen
                rows.append((label, ok, report))
                print(
                    f"   {'OK ' if ok else 'NON'} {label:18} {ms:5.0f} ms{' FOCUS !' if stolen else ''}  {report[:160]}"
                )
            rows.append(("premier plan intact", w.foreground() == fg0, ""))
        finally:
            await client.call_tool("window", {"window": hwnd, "action": "close"})
    return rows


async def drive_windows_shell() -> list[tuple[str, bool, str]]:
    """Windows lui-même (0.14) : page des Paramètres ouverte par adresse puis pilotée (liste virtualisée), barre des
    tâches, écran entier, notifications, réglages rapides au premier plan puis refermés. Chaque effet relu à part :
    premier plan de l'utilisateur rendu, panneau refermé, page atteinte."""
    apps.require_julien_away()
    rows: list[tuple[str, bool, str]] = []
    # Paramètres déjà ouverts par l'utilisateur : jamais fermés par le banc (relecture de Codex, tour 2).
    settings_before = {
        h for h in w.enum_windows() if w.process_name(w.window_pid(h)).lower() in ("systemsettings.exe",)
    } | {h for h in w.enum_windows() if w.window_text(h) in ("Paramètres", "Settings")}
    async with Client(server_params(), read_timeout_seconds=120) as client:
        fg0 = w.foreground()
        result = await client.call_tool("launch", {"app": "ms-settings:about", "then": "elements"})
        text = text_of(result)
        match = re.search(r"Window (\d+)", text)
        settings = int(match.group(1)) if match else 0
        rows.append(("ms-settings ouvert", bool(settings) and "SystemSettings" in text, text.splitlines()[0][:120]))
        rows.append(("ms-settings : premier plan rendu", w.foreground() == fg0, ""))
        try:
            if settings:
                # « Activation » est en bas de la liste Système, hors de l'arbre tant qu'on n'a pas défilé.
                steps = [
                    {"do": "click", "target": "Système"},
                    {"do": "wait", "text": "Son", "timeout": 6},
                    {"do": "click", "target": "Activation"},
                    {"do": "wait", "text": "Windows 11", "timeout": 6},
                ]
                result = await client.call_tool("act", {"window": settings, "steps": steps, "then": "none"})
                report = text_of(result)
                # Oracle : l'attente de la page Activation (étape 4) relue ✓, et le nom trouvé dans la longue liste.
                reached = re.search(r"^4\. ✓", report, re.MULTILINE) is not None
                rows.append(
                    ("liste virtualisée", reached and "long list" in report and "✗" not in report, report[:200])
                )
                rows.append(("Paramètres : premier plan intact", w.foreground() == fg0, ""))
        finally:
            if settings and settings not in settings_before:
                await client.call_tool("window", {"window": settings, "action": "close"})
        result = await client.call_tool("look", {"window": "taskbar", "show": "elements"})
        rows.append(("barre des tâches", "Button" in text_of(result), text_of(result).splitlines()[0][:100]))
        result = await client.call_tool("look", {"window": "screen"})
        rows.append(("écran entier", image_of(result) is not None and "Whole screen" in text_of(result), ""))
        result = await client.call_tool("notifications", {"limit": 3})
        rows.append(("notifications", not result.is_error, text_of(result).splitlines()[0][:100]))
        steps = [{"do": "wait", "text": "Bluetooth", "timeout": 3}]
        result = await client.call_tool(
            "act", {"window": "quick settings", "foreground": True, "steps": steps, "then": "elements"}
        )
        time.sleep(0.4)
        panel_open = bool(snapshot.flyout_window(*snapshot.FLYOUTS["quick settings"][1:]))
        quick = text_of(result)
        read = re.search(r"^1\. ✓", quick, re.MULTILINE) is not None and "Wi-Fi" in quick  # l'étape elle-même ✓
        rows.append(("réglages rapides lus", read, quick.splitlines()[0][:100]))
        rows.append(("réglages rapides refermés, premier plan rendu", not panel_open and w.foreground() == fg0, ""))
    for label, good, report in rows:
        print(f"   {'OK ' if good else 'NON'} {label:45} {report[:120]}")
    return rows


async def main() -> None:
    kinds = sys.argv[1:] or ["win32", "winforms", "wpf", "electron", "edge", "occult", "tk", "calc"]
    total = ok = 0
    for kind in kinds:
        try:
            rows = await drive(kind)
        except Exception as error:  # noqa: BLE001
            print(f"== {kind} : ÉCHEC {type(error).__name__}: {error}")
            continue
        total += len(rows)
        ok += sum(1 for _, good, _ in rows if good)
    print(f"\nBilan : {ok}/{total} vérifications.")


if __name__ == "__main__":
    asyncio.run(main())
