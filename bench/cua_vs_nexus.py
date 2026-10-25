"""Banc côte à côte : nexus-windows contre Cua Driver (trycua/cua, cua-driver-rs, « background delivery »).

Les deux par leur vrai serveur MCP (stdio), sur la même fenêtre du labo, pour les mêmes tâches que RESULTS.md :
bouton, case, champ, éditeur riche, liste déroulante, élément de liste, menu, clic brut, molette, raccourci, capture
d'une fenêtre recouverte. Chaque essai : effet PROUVÉ par l'application (événement émis, ou valeur relue par UIA pour
le champ Nom des applications de bureau), ce que l'outil a déclaré (pour compter les faux succès), premier plan pris
par un autre que Julien (relevé toutes les millisecondes), curseur réel déplacé, durée de l'appel.

Cua est piloté comme son guide le demande à un agent : `run_steps` avec cible par rôle et nom, mode d'arrière-plan
d'abord ; seulement s'il répond « background_unavailable », le même lot est renvoyé avec `foreground_fallback`
(noté à part : c'est un vol de premier plan assumé). Cua Driver s'installe hors OneDrive (CUA ci-dessous) depuis sa
release GitHub officielle, télémétrie coupée (`cua-driver telemetry disable`).

Banc intrusif : refusé si Julien a utilisé clavier ou souris depuis moins de 2 min (comme `pytest -m live`).
Usage : python bench/cua_vs_nexus.py [win32 winforms wpf tk electron edge] [--wait secondes] [--nexus-only]
Résultats : bench/results/<date>-cua-vs-nexus.json
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import datetime
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "live"))
sys.path.insert(0, str(ROOT / "lab"))

import apps  # noqa: E402
import drive  # noqa: E402
import editions  # noqa: E402
import tally_cua  # noqa: E402
from mcp import Client, StdioServerParameters  # noqa: E402
from redact import redact_home  # noqa: E402  (bench/, dossier du script)

from nexus_windows import foreground, uia  # noqa: E402
from nexus_windows import win32 as w  # noqa: E402

CUA = Path(
    os.environ.get(
        "NEXUS_CUA_DRIVER", Path.home() / ".claude" / "nexus-claude" / "lab" / "cua" / "bin" / "cua-driver.exe"
    )
)
# Windows-MCP (CursorTouch, PyPI windows-mcp), installé hors OneDrive par `uv tool install`, télémétrie coupée
# (ANONYMIZED_TELEMETRY=false), outils système exclus (PowerShell, fichiers, registre...). Il n'a pas de mode
# d'arrière-plan : un agent met la fenêtre devant (App switch), puis clique, tape ou fait défiler à l'écran.
WMCP = Path(
    os.environ.get(
        "NEXUS_WINDOWS_MCP",
        Path(os.environ.get("LOCALAPPDATA", "")) / "nexus-lab-windows-mcp" / "bin" / "windows-mcp.exe",
    )
)
WEB = drive.WEB
DESKTOP = ("win32", "winforms", "wpf")
Expect = Callable[[str], bool]


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def cursor() -> tuple[int, int]:
    point = _Point()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


class Foreground:
    """Premier plan relevé toutes les millisecondes pendant un appel : chaque fenêtre qui l'a pris à la place de celle
    de départ (fenêtre du labo, surimpression de l'outil…), avec sa durée cumulée."""

    def __init__(self) -> None:
        self.start = w.foreground()
        self.taken: dict[str, float] = {}
        self.cursor0 = cursor()
        self.cursor_moved = False  # curseur réel déplacé à un instant quelconque de l'appel (même remis ensuite)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bench-foreground", daemon=True)

    def _run(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            now = time.perf_counter()
            fg = w.foreground()
            if not self.cursor_moved and cursor() != self.cursor0:
                self.cursor_moved = True
            if fg and fg != self.start:
                key = f"{w.class_name(fg)} (pid {w.window_pid(fg)})"
                self.taken[key] = self.taken.get(key, 0.0) + (now - last)
            last = now
            time.sleep(0.001)

    def __enter__(self) -> Foreground:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(2)
        if w.foreground() != self.start and self.start:
            self.not_given_back = True
            foreground.give_back(self.start)  # banc : le premier plan de départ revient avant l'essai suivant
        else:
            self.not_given_back = False

    def summary(self) -> str:
        if not self.taken:
            return "intact"
        parts = [f"{k} {round(v * 1000)} ms" for k, v in self.taken.items()]
        return "PRIS : " + ", ".join(parts) + (" (non rendu)" if self.not_given_back else "")


def nexus_params() -> StdioServerParameters:
    """Serveur de l'édition mesurée (editions.EDITION) : celui de ce dépôt pour Claude, le paquet publié sinon."""
    return drive.server_params() if editions.EDITION == "claude" else editions.params()


def cua_params() -> StdioServerParameters:
    return StdioServerParameters(command=str(CUA), args=["mcp"], env=dict(os.environ))


def wmcp_params() -> StdioServerParameters:
    config = WMCP.parent.parent / "config.toml"
    config.touch()
    excluded = "PowerShell,FileSystem,Registry,Process,Scrape,Clipboard,Notification"
    args = ["serve", "--transport", "stdio", "--config", str(config), "--exclude-tools", excluded]
    return StdioServerParameters(command=str(WMCP), args=args, env={**os.environ, "ANONYMIZED_TELEMETRY": "false"})


def text_of(result: Any) -> str:
    return drive.text_of(result)


# --- Géométrie : point de la zone (canevas) en pixels d'image de chaque outil -----------------------------------
def visible_area(app: apps.LabApp) -> w.Rect:
    """Partie de l'écran où la fenêtre affiche réellement son contenu : zone CLIENTE (jamais le décor ni la barre de
    titre) ; web : la zone de rendu de la page (fenêtre Chrome_RenderWidgetHostHWND visible, une seule), à défaut la
    zone cliente (relecture de Codex, 22/10 06:37 : le rectangle extérieur menait un point dans la barre de titre)."""
    if app.kind in WEB:
        views = [c for c in w.enum_children(app.hwnd)
                 if w.class_name(c) == "Chrome_RenderWidgetHostHWND" and w.IsWindowVisible(c)]  # fmt: skip
        if len(views) == 1:
            return w.window_rect(views[0])
    return w.client_rect_screen(app.hwnd)


def visible_center(
    rect: tuple[int, int, int, int], area: tuple[int, int, int, int], min_size: int = 20
) -> tuple[int, int] | None:
    """Centre de la partie de `rect` (gauche, haut, droite, bas) visible dans `area` ; moins de min_size px de côté
    visibles : None (rien à viser, jamais un point hors du contenu)."""
    left, top = max(rect[0], area[0]), max(rect[1], area[1])
    right, bottom = min(rect[2], area[2]), min(rect[3], area[3])
    if right - left < min_size or bottom - top < min_size:
        return None
    return ((left + right) // 2, (top + bottom) // 2)


def _box(r: w.Rect) -> tuple[int, int, int, int]:
    return r.left, r.top, r.right, r.bottom


def zone_screen(app: apps.LabApp) -> tuple[int, int] | None:
    """Centre de la zone (canevas) en pixels d'écran. Page web : amenée dans la vue d'abord (ScrollIntoView par le
    banc, pour les deux outils) ; WPF (Border sans pair UIA) : sous le statut ; Tk : donné par l'application."""
    if "zone_hwnd" in app.extra:
        return w.window_rect(app.extra["zone_hwnd"]).center
    uia.com_init()
    auto = uia.Automation(auto_set_focus=False)
    tree = auto.tree(app.hwnd)
    for node in tree.walk():
        if node.name == "Zone" and node.rect is not None and node.rect.height > 40:
            if app.kind in WEB:
                try:
                    auto.scroll_into_view(node.element)
                    node = settled(auto, app, "Zone")
                except uia.UIAError, StopIteration:
                    pass
            assert node.rect is not None
            # partie VISIBLE de la zone dans le contenu affiché (22/10 : au 2e tour web, son centre tombait sous la
            # fenêtre ; puis, ScrollIntoView raté, dans la barre de titre) ; rien de visible : None
            return visible_center(_box(node.rect), _box(visible_area(app)))
    statut = next((n for n in tree.walk() if n.control_type == "Text" and n.name == "Statut" and n.rect), None)
    if statut is not None and statut.rect is not None:
        return statut.rect.left + 160, statut.rect.bottom + 60
    return None


def to_image(app: apps.LabApp, point: tuple[int, int], size: tuple[int, int]) -> tuple[int, int]:
    """Point écran → pixels d'une image de toute la fenêtre (rectangle GetWindowRect) de taille `size`."""
    rect = w.window_rect(app.hwnd)
    sx, sy = size[0] / rect.width, size[1] / rect.height
    return round((point[0] - rect.left) * sx), round((point[1] - rect.top) * sy)


class Cover:
    """Fenêtre opaque posée sur celle du labo sans être activée (comme une fenêtre de Julien par-dessus) : toutes les
    mesures se font fenêtre recouverte, cas réel d'un agent qui travaille derrière Julien."""

    def __init__(self, hwnd: int) -> None:
        self.target = hwnd
        self.hwnd = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bench-cover", daemon=True)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        user32.CreateWindowExW.argtypes += [ctypes.c_int] * 4 + [ctypes.c_void_p] * 4
        rect = w.window_rect(self.target)
        ex = 0x08000000 | 0x00000080  # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        style = 0x80000000 | 0x00800000  # WS_POPUP | WS_BORDER (rendu visible juste après, sans activation)
        handle = user32.CreateWindowExW(
            ex, "Static", "Couverture du banc", style, rect.left - 20, rect.top - 20, rect.width + 40,
            rect.height + 40, None, None, None, None,
        )  # fmt: skip
        self.hwnd = int(handle or 0)
        if self.hwnd:
            user32.SetWindowPos(ctypes.c_void_p(self.hwnd), None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
        self._ready.set()
        msg = ctypes.create_string_buffer(64)
        while user32.GetMessageW(msg, None, 0, 0) > 0:
            user32.TranslateMessage(msg)
            user32.DispatchMessageW(msg)

    def __enter__(self) -> Cover:
        self._thread.start()
        self._ready.wait(5)
        return self

    def raise_(self) -> None:
        """Remet la couverture devant la fenêtre du labo (qui a pu remonter), toujours sans activation."""
        if self.hwnd:
            flags = 0x0001 | 0x0002 | 0x0010 | 0x0040  # NOSIZE | NOMOVE | NOACTIVATE | SHOWWINDOW
            ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(self.hwnd), None, 0, 0, 0, 0, flags)

    def __exit__(self, *exc: object) -> None:
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(self.hwnd), 0x0010, 0, 0)  # WM_CLOSE
            ctypes.windll.user32.PostThreadMessageW(self._thread.native_id, 0x0012, 0, 0)  # WM_QUIT
        self._thread.join(3)


def covered(app: apps.LabApp) -> bool:
    """La fenêtre du labo est-elle recouverte (au centre) par une autre fenêtre ?"""
    rect = w.window_rect(app.hwnd)
    cx, cy = rect.left + rect.width // 2, rect.top + rect.height // 2
    hit = ctypes.windll.user32.WindowFromPoint(_Point(cx, cy))
    return bool(hit) and w.root(w.handle(hit)) != app.hwnd


# --- Tâches ----------------------------------------------------------------------------------------------------
# (tâche, familles, attendu côté nexus, attendu côté Cua). Valeurs différentes pour les deux outils : chaque essai
# change l'état et ne peut pas être confondu avec le précédent.
TASKS: list[tuple[str, tuple[str, ...]]] = [
    ("bouton", DESKTOP + WEB),
    ("case", DESKTOP + WEB),
    ("champ", DESKTOP + WEB),
    ("éditeur riche", WEB),
    ("liste déroulante", DESKTOP + WEB),
    ("élément de liste", DESKTOP + WEB),
    ("menu", DESKTOP),
    ("clic brut", DESKTOP + WEB + ("tk",)),
    ("molette", DESKTOP + WEB + ("tk",)),
    ("raccourci ctrl+k", DESKTOP + WEB + ("tk",)),
    # Zone qui DÉFILE (liste, région à ascenseur), pas le canevas de « molette » : défiler une liste recouverte, quelle
    # que soit la technique (treizième passage, 23/10). Effet : la position a réellement bougé vers le bas.
    ("défilement", WEB + ("tk",)),
]
VALUES = {
    "nexus": {"nom": "Alice Martin", "couleur": "Bleu", "item": "Charlie", "edit": "Bonjour"},
    "cua": {"nom": "Cua Driver", "couleur": "Vert", "item": "Bravo", "edit": "Salut"},
    "wmcp": {"nom": "Windows MCP", "couleur": "Rouge", "item": "Alpha", "edit": "Hello"},
}


def expectation(task: str, tool: str, kind: str) -> Expect | None:
    v = VALUES[tool]
    return {
        "bouton": lambda e: e.startswith("clic"),
        "case": lambda e: e.startswith("coche"),
        "champ": (lambda e: e == f"nom {v['nom']}") if kind in WEB else None,
        "éditeur riche": lambda e: e.startswith("editeur") and v["edit"] in e,
        "liste déroulante": lambda e: e == f"couleur {v['couleur']}",
        "élément de liste": lambda e: e == f"liste {v['item']}",
        "menu": lambda e: e == "menu action",
        "clic brut": lambda e: e.startswith("zone gauche"),
        "molette": lambda e: e.startswith("molette"),
        "raccourci ctrl+k": lambda e: e == "raccourci ctrl+k",
        "défilement": lambda e: e.startswith("defil liste bas " if kind == "tk" else "defil A bas "),
    }[task]


def settled(auto: uia.Automation, app: apps.LabApp, name: str) -> uia.Node:
    """Élément relu jusqu'à ce que sa position ne bouge plus (deux lectures égales, 2 s au plus) : une page occultée
    publie sa nouvelle géométrie avec retard après ScrollIntoView (23/10 : le point de la zone tombait sur une ligne du
    journal de la page, pour tous les outils)."""
    last = None
    node = None
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        time.sleep(0.25)
        node = next(
            n
            for n in auto.tree(app.hwnd).walk()
            if n.name == name and n.rect is not None and (name != "Défilement" or n.control_type == "Group")
        )
        if last is not None and node.rect == last:
            break
        last = node.rect
    assert node is not None
    return node


def web_scroll_offset(app: apps.LabApp) -> int | None:
    """Position de la première ligne de la zone « Défilement » DANS la zone (pixels), lue par l'accessibilité de la page :
    elle diminue quand la zone défile vers le bas, que la page soit visible ou cachée."""
    uia.com_init()
    auto = uia.Automation(auto_set_focus=False)
    for node in auto.tree(app.hwnd).walk():
        if node.name == "Défilement" and node.control_type == "Group" and node.rect is not None:
            first = next((c for c in node.children if c.name == "A ligne 1" and c.rect is not None), None)
            return None if first is None or first.rect is None else first.rect.top - node.rect.top
    return None


def target_in_front(app: apps.LabApp) -> bool:
    """La fenêtre au premier plan appartient-elle à l'application du labo (laissée devant par un outil précédent) ?"""
    front = w.foreground()
    return bool(front) and w.window_pid(front) in app.pids()


def task_point(task: str, app: apps.LabApp) -> tuple[int, int]:
    """Point écran visé : la zone (canevas) pour « clic brut » et « molette », la zone qui défile pour « défilement »
    (Tk : la liste, à gauche de sa barre native ; page web : la région « Défilement », amenée dans la vue)."""
    point = scroll_zone_screen(app) if task == "défilement" else zone_screen(app)
    if point is None:
        raise RuntimeError("zone introuvable")
    return point


def scroll_zone_screen(app: apps.LabApp) -> tuple[int, int] | None:
    if "barre_hwnd" in app.extra:
        bar = w.window_rect(app.extra["barre_hwnd"])
        return bar.left - 80, (bar.top + bar.bottom) // 2
    uia.com_init()
    auto = uia.Automation(auto_set_focus=False)
    for node in auto.tree(app.hwnd).walk():
        if node.name == "Défilement" and node.control_type == "Group" and node.rect is not None:
            with contextlib.suppress(uia.UIAError, StopIteration):
                auto.scroll_into_view(node.element)
                node = settled(auto, app, "Défilement")
            assert node.rect is not None
            return visible_center(_box(node.rect), _box(visible_area(app)))
    return None


def name_value(app: apps.LabApp) -> str | None:
    """Valeur du champ Nom relue par UIA (applications de bureau : aucun événement émis pour ce champ)."""
    uia.com_init()
    auto = uia.Automation(auto_set_focus=False)
    for node in auto.tree(app.hwnd).walk():
        if node.control_type == "Edit" and node.name == "Nom":
            try:
                value = auto.refresh(node.element).get("value")
            except uia.UIAError:
                return None
            if value is None and node.hwnd:
                value = w.get_text_message(node.hwnd)
            return value
    return None


def nexus_call(task: str, app: apps.LabApp, image_size: tuple[int, int]) -> tuple[str, dict]:
    v = VALUES["nexus"]
    hwnd = app.hwnd
    steps: list[dict[str, Any]]
    if task == "bouton":
        steps = [{"do": "click", "target": "Valider"}]
    elif task == "case":
        steps = [{"do": "toggle", "target": "Option"}]
    elif task == "champ":
        steps = [{"do": "set", "target": "champ:Nom", "text": v["nom"]}]
    elif task == "éditeur riche":
        steps = [{"do": "type", "target": "Éditeur", "text": v["edit"]}]
    elif task == "liste déroulante":
        steps = [{"do": "select", "target": "Couleur", "option": v["couleur"]}]
    elif task == "élément de liste":
        steps = [{"do": "click", "target": v["item"]}]
    elif task == "menu":
        steps = [{"do": "menu", "path": "Fichier > Action menu"}]
    elif task in ("clic brut", "molette", "défilement"):
        x, y = to_image(app, task_point(task, app), image_size)
        steps = [{"do": "click", "target": f"{x},{y}"}] if task == "clic brut" else [
            {"do": "scroll", "target": f"{x},{y}", "direction": "down", "amount": 1}
        ]  # fmt: skip
    else:
        steps = [{"do": "key", "keys": "ctrl+k"}]
    return "act", {"window": hwnd, "steps": steps, "then": "none"}


def cua_calls(task: str, app: apps.LabApp, image_size: tuple[int, int], attempt: str) -> list[tuple[str, dict]]:
    """Appels qu'un agent ferait avec Cua, d'après son guide : run_steps par rôle et nom ; pour un point, une capture
    d'abord (get_window_state : elle fixe le cadre des coordonnées) ; menu par invoke_menu, puis par clics."""
    v = VALUES["cua"]
    pid, wid = w.window_pid(app.hwnd), app.hwnd
    base = {"pid": pid, "window_id": wid}
    calls: list[tuple[str, dict]] = []
    steps: list[dict[str, Any]]
    if task == "menu" and attempt == "arrière-plan":
        return [("invoke_menu", {**base, "path": ["Fichier", "Action menu"]})]
    if task == "menu":
        steps = [
            {"click": {"role": "menuitem", "name": "Fichier"}},
            {"click": {"role": "menuitem", "name": "Action menu"}},
        ]
    elif task == "bouton":
        steps = [{"click": {"role": "button", "name": "Valider"}}]
    elif task == "case":
        steps = [{"click": {"role": "checkbox", "name": "Option"}}]
    elif task == "champ":
        # Page web : « Nom » désigne aussi l'étiquette (Cua refuse l'ambiguïté) ; l'agent ajoute nth comme demandé.
        nth = {"nth": 1} if app.kind in WEB else {}
        steps = [{"set_value": {"role": "textfield", "name": "Nom", "value": v["nom"], **nth}}]
    elif task == "éditeur riche":
        steps = [{"click": {"name": "Éditeur", "nth": 1}}, {"type_text": {**base, "text": v["edit"]}}]
    elif task == "liste déroulante" and attempt == "par clics":
        # Ce que la réponse de Cua conseille quand la valeur posée n'est pas une sélection : ouvrir, puis l'option.
        steps = [{"click": {"role": "combobox", "name": "Couleur"}}, {"click": {"name": v["couleur"]}}]
    elif task == "liste déroulante":
        steps = [{"set_value": {"role": "combobox", "name": "Couleur", "value": v["couleur"]}}]
    elif task == "élément de liste":
        steps = [{"click": {"role": "listitem", "name": v["item"]}}]
    elif task in ("clic brut", "molette", "défilement"):
        x, y = to_image(app, task_point(task, app), image_size)
        calls.append(("get_window_state", {**base, "include_accessibility_tree": False}))
        if task == "clic brut":
            steps = [{"click": {**base, "x": x, "y": y}}]
        else:
            steps = [{"scroll": {**base, "x": x, "y": y, "direction": "down", "amount": 1}}]
    else:
        steps = [{"hotkey": {**base, "keys": ["ctrl", "k"]}}]
    args: dict[str, Any] = {**base, "steps": steps}
    if attempt == "premier plan":
        args["foreground_fallback"] = True
    calls.append(("run_steps", args))
    return calls


def cua_refused_background(text: str) -> bool:
    low = text.lower()
    return "background_unavailable" in low or "foreground_fallback" in low or "background input" in low


UNCERTAIN = ("may change the shown text", "check with get_window_state", "verify t", "unverifiable")


def claim_of(result: Any, text: str) -> str:
    """Ce que Cua a déclaré : « ok », « incertain » (il dit lui-même que l'effet n'est pas prouvé, l'équivalent du
    « ~ » de nexus) ou « échec »."""
    if result.is_error:
        return "échec"
    low = text.lower()
    if any(m in low for m in ("error", "failed", "✗", "refused", "unavailable", "not found", "no element")):
        return "échec"
    return "incertain" if any(m in low for m in UNCERTAIN) else "ok"


def nexus_claim(text: str) -> str:
    first = text.splitlines()[0] if text else ""
    if not drive.step_ok(first):
        return "échec"
    return "ok" if "✓" in first[:8] else "incertain"


async def capture(client: Client, tool: str, app: apps.LabApp) -> dict:
    """Capture de la fenêtre (recouverte par celles de Julien) : durée, taille, image pleine (non noire, non vide)."""
    t0 = time.perf_counter()
    if tool == "nexus" and editions.EDITION != "claude":
        result = await client.call_tool("look", editions.look_args(app.hwnd, image=True))
    elif tool == "nexus":
        result = await client.call_tool("look", {"window": app.hwnd, "show": "image"})
    else:
        args = {"pid": w.window_pid(app.hwnd), "window_id": app.hwnd, "include_accessibility_tree": False}
        result = await client.call_tool("get_window_state", args)
    ms = (time.perf_counter() - t0) * 1000
    image = drive.image_of(result)
    if image is None:
        return {"ok": False, "ms": round(ms), "note": text_of(result)[:200], "size": None}
    gray = image.convert("L")
    hist = gray.histogram()
    dark = sum(hist[:20]) / (image.width * image.height)
    top = max(hist) / (image.width * image.height)
    ok = dark < 0.5 and top < 0.97  # ni noire, ni d'une seule couleur
    path = apps.STATE / "cua-vs-nexus"
    path.mkdir(parents=True, exist_ok=True)
    image.save(path / f"{app.kind}-{tool}.png")
    note = f"{image.width}×{image.height}, sombre {dark:.0%}, couleur dominante {top:.0%}"
    return {"ok": ok, "ms": round(ms), "note": note, "size": (image.width, image.height)}


def element_center(app: apps.LabApp, name: str, types: tuple[str, ...] = (), popups: bool = False) -> list[int]:
    """Centre écran d'un élément par son nom (et son type UIA) : ce que la capture annotée de Windows-MCP donne à
    l'agent. `popups` : cherche aussi dans les autres fenêtres visibles du processus (liste ouverte, menu déroulé)."""
    uia.com_init()
    auto = uia.Automation(auto_set_focus=False)
    pid = w.window_pid(app.hwnd)
    hwnds = [app.hwnd]
    if popups:
        hwnds += [h for h in w.enum_windows() if h != app.hwnd and w.IsWindowVisible(h) and w.window_pid(h) == pid]
    for hwnd in hwnds:
        for node in auto.tree(hwnd).walk():
            rect = node.rect
            if node.name == name and (not types or node.control_type in types) and rect and rect.width and rect.height:
                return [rect.center[0], rect.center[1]]
    raise RuntimeError(f"élément introuvable : {name}")


async def wmcp_run(client: Client, task: str, app: apps.LabApp) -> tuple[Any, list[str]]:
    """Ce qu'un agent fait avec Windows-MCP : mettre la fenêtre devant (App switch), puis agir à l'écran sur la
    position de l'élément (Click, Type, Scroll, Shortcut)."""
    v = VALUES["wmcp"]
    names: list[str] = []

    async def call(name: str, args: dict) -> Any:
        names.append(name)
        return await client.call_tool(name, args)

    await call("App", {"mode": "switch", "name": w.window_text(app.hwnd)})
    await asyncio.sleep(0.3)
    if task == "bouton":
        result = await call("Click", {"loc": element_center(app, "Valider", ("Button",))})
    elif task == "case":
        result = await call("Click", {"loc": element_center(app, "Option", ("CheckBox",))})
    elif task == "champ":
        loc = element_center(app, "Nom", ("Edit",))
        result = await call("Type", {"loc": loc, "text": v["nom"], "clear": True})
    elif task == "éditeur riche":
        result = await call("Type", {"loc": element_center(app, "Éditeur"), "text": v["edit"]})
    elif task == "liste déroulante":
        await call("Click", {"loc": element_center(app, "Couleur", ("ComboBox",))})
        await asyncio.sleep(0.5)
        result = await call("Click", {"loc": element_center(app, v["couleur"], ("ListItem", "MenuItem"), popups=True)})
    elif task == "élément de liste":
        result = await call("Click", {"loc": element_center(app, v["item"], ("ListItem",))})
    elif task == "menu":
        await call("Click", {"loc": element_center(app, "Fichier", ("MenuItem",))})
        await asyncio.sleep(0.5)
        result = await call("Click", {"loc": element_center(app, "Action menu", ("MenuItem",), popups=True)})
    elif task in ("clic brut", "molette", "défilement"):
        point = task_point(task, app)
        if task == "clic brut":
            result = await call("Click", {"loc": list(point)})
        else:
            result = await call("Scroll", {"loc": list(point), "direction": "down", "wheel_times": 1})
    else:
        result = await call("Shortcut", {"shortcut": "ctrl+k"})
    return result, names


async def run_task(
    client: Client, tool: str, task: str, app: apps.LabApp, events: drive.Events, size: tuple[int, int]
) -> dict:
    expect = expectation(task, tool, app.kind)
    out: dict[str, Any] = {"tâche": task}
    by_clicks = tool == "cua" and task in ("menu", "liste déroulante")
    attempts = ["arrière-plan"] + (["par clics"] if by_clicks else []) + ["premier plan"]
    if tool == "wmcp":
        attempts = ["premier plan"]  # aucun mode d'arrière-plan : la fenêtre est mise devant, à l'écran
    for attempt in attempts:
        if tool == "nexus" and attempt != "arrière-plan":
            break  # nexus choisit seul sa technique ; aucun premier plan sans l'accord de Julien
        events.reset()
        offset0 = web_scroll_offset(app) if task == "défilement" and app.kind in WEB else None
        calls: list[tuple[str, dict]] = []
        if tool == "nexus" and editions.EDITION == "claude":
            calls = [nexus_call(task, app, size)]
        elif tool == "cua":
            calls = cua_calls(task, app, size, attempt)
        mouse0 = cursor()
        with Foreground() as fg:
            t0 = time.perf_counter()
            result: Any = None
            if tool == "wmcp":
                result, names = await wmcp_run(client, task, app)
                calls = [(name, {}) for name in names]
            elif tool == "nexus" and editions.EDITION != "claude":
                point = task_point(task, app) if task in ("clic brut", "molette", "défilement") else None
                result, names = await editions.run(client, task, app, VALUES["nexus"], point)
                calls = [(name, {}) for name in names]
            for name, args in (
                calls if tool != "wmcp" and not (tool == "nexus" and editions.EDITION != "claude") else []
            ):
                result = await client.call_tool(name, args)
            ms = (time.perf_counter() - t0) * 1000
        text = text_of(result)
        if expect is not None:
            # Défilement : la page signale la position une fois stable depuis 150 ms, minuterie qu'une page occultée
            # ralentit (Electron recouvert, 23/10) ; même délai pour les trois outils, l'attente s'arrête au signal.
            wait_s = 4.0 if task == "défilement" else 1.5
            seen = events.collect(wait_s, lambda s, e=expect: any(e(x) for x in s))
            effect = any(expect(x) for x in seen)
            if not effect and offset0 is not None:
                # Page cachée (Chromium occulté) : la zone défile mais la page ne déclenche son évènement « scroll »
                # qu'une fois visible (relevé par un observateur DevTools le 23/10 : scrollTop 98, page « hidden »). La
                # position est alors relue par l'accessibilité de la page, même mesure pour les trois outils.
                offset1 = web_scroll_offset(app)
                if offset1 is not None and offset1 < offset0:
                    effect = True
                    seen = [f"position relue (page cachée) : {offset0 - offset1} px"]
        else:
            value = name_value(app)
            seen = [f"valeur relue : {value!r}"]
            effect = value == VALUES[tool]["nom"]
        if tool == "nexus":
            claim = nexus_claim(text) if editions.EDITION == "claude" else editions.claim(text)
        else:
            claim = claim_of(result, text)
        row = {
            "effet": effect,
            "déclaré": claim,
            "faux succès": claim == "ok" and not effect,
            "premier plan": fg.summary(),
            "curseur déplacé": fg.cursor_moved or cursor() != mouse0,
            "ms": round(ms),
            "appels": [name for name, _ in calls],
            "réponse": text[:400],
            "vu": [s[:80] for s in seen[-3:]],
        }
        out[attempt] = row
        flag = "OK " if effect else "NON"
        print(f"   {tool:5} {attempt:12} {flag} {task:18} {ms:6.0f} ms  {fg.summary()[:70]}  {text[:90]!r}")
        if tool in ("nexus", "wmcp") or effect:
            break
        if attempt == "arrière-plan" and by_clicks:
            continue  # invoke_menu en échec, ou valeur posée sans sélection : l'agent essaie les clics par nom
        if not cua_refused_background(text):
            break
    return out


async def reading_sizes(nexus: Client, cua: Client, app: apps.LabApp) -> dict:
    """Taille des réponses (caractères) : liste complète, puis rendu après un clic (nexus then="changes", Cua
    run_steps avec observe : ses lectures « since » par défaut). Moins de texte = moins de jetons pour l'agent."""
    pid, wid = w.window_pid(app.hwnd), app.hwnd
    out: dict[str, Any] = {}
    t0 = time.perf_counter()
    full_n = text_of(await nexus.call_tool("look", {"window": wid, "show": "elements"}))
    out["nexus liste"] = {"car": len(full_n), "ms": round((time.perf_counter() - t0) * 1000)}
    t0 = time.perf_counter()
    args = {"pid": pid, "window_id": wid, "include_screenshot": False}
    full_c = text_of(await cua.call_tool("get_window_state", args))
    out["cua liste"] = {"car": len(full_c), "ms": round((time.perf_counter() - t0) * 1000)}
    t0 = time.perf_counter()
    step = {"do": "click", "target": "Valider"}
    after_n = text_of(await nexus.call_tool("act", {"window": wid, "steps": [step], "then": "changes"}))
    out["nexus après clic (changes)"] = {"car": len(after_n), "ms": round((time.perf_counter() - t0) * 1000)}
    t0 = time.perf_counter()
    batch = {"pid": pid, "window_id": wid, "steps": [{"click": {"role": "button", "name": "Valider"}}], "observe": {}}
    after_c = text_of(await cua.call_tool("run_steps", batch))
    out["cua après clic (observe)"] = {"car": len(after_c), "ms": round((time.perf_counter() - t0) * 1000)}
    out["textes"] = {"nexus": after_n[:600], "cua": after_c[:600]}
    for key, value in out.items():
        if key != "textes":
            print(f"   lecture {key:28} {value['car']:6} car  {value['ms']:5} ms")
    return out


async def bench_app(kind: str, nexus: Client, cua: Client | None, wmcp: Client | None = None) -> dict:
    app = apps.launch(kind, x=150, y=110)
    events = drive.Events(app)
    info = nexus.server_info
    report: dict[str, Any] = {
        "famille": kind,
        "édition": {
            "nom": editions.EDITION,
            "serveur": getattr(info, "name", None),
            "version": getattr(info, "version", None),
            "commande": " ".join([nexus_params().command, *nexus_params().args]),
        },
        "recouverte": None,
        "capture": {},
        "tâches": [],
    }
    try:
        with Cover(app.hwnd) as cover:
            await asyncio.sleep(1.0)
            # arbre publié (Chromium)
            if editions.EDITION == "claude":
                await nexus.call_tool("look", {"window": app.hwnd, "show": "elements"})
            else:
                await nexus.call_tool("look", editions.look_args(app.hwnd))
            cover.raise_()
            report["recouverte"] = covered(app)
            print(f"== {kind} : {w.window_text(app.hwnd)!r}, recouverte={report['recouverte']}")
            tools = [("nexus", nexus)] + ([("cua", cua)] if cua is not None else [])
            if app.kind != "tk" and cua is not None and editions.EDITION == "claude":
                report["lecture"] = await reading_sizes(nexus, cua, app)
            sizes = {}
            for tool, client in tools:
                cap = await capture(client, tool, app)
                report["capture"][tool] = cap
                sizes[tool] = cap["size"] or (1, 1)
                print(f"   {tool:5} capture      {'OK ' if cap['ok'] else 'NON'} {cap['ms']:5} ms  {cap['note']}")
            for task, kinds in TASKS:
                if kind not in kinds:
                    continue
                row: dict[str, Any] = {"tâche": task}
                covers: list[bool] = []
                for tool, client in tools:
                    cover.raise_()
                    # exigé AVANT chaque essai (W16, Codex), et la cible jamais déjà au premier plan : le relevé du premier
                    # plan ne verrait alors aucune prise (relecture de Codex du 24/10, lignes WPF laissées devant)
                    covers.append(covered(app) and not target_in_front(app))
                    try:
                        row[tool] = await run_task(client, tool, task, app, events, sizes[tool])
                    except Exception as exc:  # noqa: BLE001  (banc : une erreur d'un outil n'arrête pas l'autre)
                        row[tool] = {"erreur": f"{type(exc).__name__}: {exc}"[:300]}
                        print(f"   {tool:5} ERREUR {task} : {exc}")
                covers.append(covered(app) and not target_in_front(app))
                if wmcp is not None:
                    # Après le contrôle de couverture : Windows-MCP met la fenêtre devant par construction.
                    cover.raise_()
                    try:
                        row["wmcp"] = await run_task(wmcp, "wmcp", task, app, events, (1, 1))
                    except Exception as exc:  # noqa: BLE001
                        row["wmcp"] = {"erreur": f"{type(exc).__name__}: {exc}"[:300]}
                        print(f"   wmcp  ERREUR {task} : {exc}")
                row["recouverte"] = all(covers)
                # Ligne comptée seulement si la cible était recouverte avant chaque essai et après : sinon à part,
                # jamais mêlée aux comptes « fenêtre recouverte ».
                row["comptée"] = row["recouverte"]
                if not row["comptée"]:
                    print(f"   HORS COMPTE {task} : cible non recouverte pendant l'essai")
                report["tâches"].append(row)
    finally:
        app.close()
    return report


async def main(kinds: list[str], wait_s: float, nexus_only: bool = False) -> None:
    if not nexus_only and not CUA.exists():
        raise SystemExit(f"Cua Driver absent : {CUA} (ou --nexus-only)")
    results = []
    async with contextlib.AsyncExitStack() as stack:
        nexus = await stack.enter_async_context(Client(nexus_params(), read_timeout_seconds=120))
        cua = None if nexus_only else await stack.enter_async_context(Client(cua_params(), read_timeout_seconds=120))
        wmcp = None
        if not nexus_only and WMCP.exists():
            wmcp = await stack.enter_async_context(Client(wmcp_params(), read_timeout_seconds=120))
        deadline = time.monotonic() + wait_s
        for kind in kinds:
            while True:
                try:
                    results.append(await bench_app(kind, nexus, cua, wmcp))
                    break
                except apps.JulienActive as exc:
                    print(f"== {kind} : {exc}", flush=True)
                    if time.monotonic() > deadline:
                        results.append({"famille": kind, "refus": str(exc)})
                        break
                    await asyncio.sleep(30)  # Julien au travail : on attend qu'il s'absente
    suffix = "nexus-seul" if nexus_only else "cua-vs-nexus"
    if editions.EDITION != "claude":
        suffix += f"-{editions.EDITION}"
    out = ROOT / "bench" / "results" / f"{datetime.datetime.now():%Y-%m-%d_%H%M}-{suffix}.json"
    text = json.dumps(results, ensure_ascii=False, indent=1)
    # Réponses brutes des deux serveurs : le profil du poste (URL file:/// du labo) n'a rien à faire dans un relevé.
    text = redact_home(text)
    out.write_text(text, encoding="utf-8")
    print(f"Résultats : {out}")
    print(tally_cua.report(out))  # comptes de RESULTS.md, calculés depuis le relevé


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    args = sys.argv[1:]
    wait = 0.0
    if "--wait" in args:  # --wait <secondes> : attendre l'absence de Julien au lieu de refuser
        i = args.index("--wait")
        wait = float(args[i + 1])
        del args[i : i + 2]
    nexus_only = "--nexus-only" in args  # côté nexus-windows seul (Cua absent du poste)
    args = [a for a in args if a != "--nexus-only"]
    asyncio.run(main(args or list(apps.KINDS), wait, nexus_only))
