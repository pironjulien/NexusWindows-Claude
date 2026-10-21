"""Lot T de la contre-vérification n° 6 : frappe par tranches confirmée par le début du texte relu (Chromium sans
DevTools, textes longs, éditeurs riches), sans fenêtre réelle.

Le Chromium simulé reprend les mesures réelles (recheck6/lotB_verify/trace.jsonl) : environ 2 ms de traitement par
caractère, valeur UIA publiée 120 ms plus tard (par paquets si publish_s), Entrée insérée par le WM_CHAR que
TranslateMessage poste en FIN de file (après les caractères déjà postés), caractères non traités perdus au rendu du
premier plan (lose) ou traités en retard. Un doublon ou un saut de ligne déplacé se voit donc dans le texte final.
"""

from __future__ import annotations

import math
import re
import time
from collections import deque

import pytest
from test_contract import FakeAuto, as_auto, as_call, make_view, node
from test_fix_b import JULIEN, TARGET, Desk

from nexus_windows import actions, foreground, methods, server, snapshot, stop, uia
from nexus_windows import win32 as w

HWND = 4242
LONG6 = "\n".join(f"{i}" + "abcdefghij" * 12 for i in range(6))  # 731 caractères sur 6 lignes (sonde du constat)
PROSE = "".join(f"Phrase {i:03d} du compte rendu, " for i in range(24))  # 700 caractères sans saut, sans motif


@pytest.fixture(autouse=True)
def no_real_stop(tmp_path, monkeypatch):
    """Jamais le vrai fichier d'arrêt : un arrêt pressé par Julien ne fausse pas ces tests."""
    monkeypatch.setattr(stop, "STOP_FILE", tmp_path / "stop")
    monkeypatch.setattr(stop, "OWNER_FILE", tmp_path / "stop.owner")
    monkeypatch.setattr(stop.log, "event", lambda *a, **k: None)


class VirtualClock:
    """Horloge simulée de ces tests : time.monotonic et time.sleep ne suivent plus le temps réel.

    Les bornes vérifiées ici (tranche ≤ SLICE_S, premier plan rendu à temps) portent sur les décisions de
    type_in_slices face au Chromium simulé. En temps réel, la charge du poste s'y ajoutait : sous charge, un
    time.sleep(0.02) dure jusqu'à 0,11 s, et l'écriture puis la libération du fichier du jeton commun (agentlock)
    jusqu'à 0,27 s (mesuré le 2026-10-16) ; la même frappe donnait alors une tranche de 0,59 s, environ une fois sur
    quatre. Ici le temps n'avance que par les attentes (time.sleep : elles durent exactement ce qui est demandé) et
    d'un pas fixe à chaque lecture de l'horloge (coût du code lui-même, et jamais de boucle sans fin) : les mêmes
    décisions donnent toujours les mêmes durées."""

    TICK_S = 0.00001

    def __init__(self, monkeypatch) -> None:
        self.now = time.monotonic()
        monkeypatch.setattr(time, "monotonic", self.monotonic)
        monkeypatch.setattr(time, "sleep", self.sleep)

    def monotonic(self) -> float:
        self.now += self.TICK_S
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class Chromium:
    """Champ Chromium sans DevTools simulé (textarea, ou éditeur riche : vide, il se lit « \\n »), sur horloge
    simulée (VirtualClock)."""

    def __init__(
        self,
        monkeypatch,
        desk: Desk,
        *,
        value: str = "",
        per_char: float = 0.002,
        lag: float = 0.12,
        publish_s: float = 0.0,
        lose: bool = False,
        rich: bool = False,
        deaf_delete: int = 0,
        freeze_at: int | None = None,
        freeze_s: float = 0.0,
        give_back_s: float = 0.0,
    ) -> None:
        self.time = VirtualClock(monkeypatch)
        self.desk = desk
        self.dom = value
        self.rich = rich
        self.per_char, self.lag, self.publish_s, self.lose = per_char, lag, publish_s, lose
        self.deaf_delete = deaf_delete  # nombre de Suppr ignorées (sélection perdue)
        self.freeze_at, self.freeze_s = freeze_at, freeze_s  # pause du moteur (ramasse-miettes, mise en page)
        self.processed = 0
        self.deaf = False  # champ re-monté : plus rien ne lui arrive
        self.selected = False
        self.queue: deque = deque()
        self.clock = time.monotonic()
        self.published = [(-math.inf, self._uia(value))]
        self.holds: list[list[float]] = []  # [prise, rendu] du premier plan par la cible
        self.log: list[tuple] = []
        self.reads = 0
        self.unreadable_after: int | None = None
        monkeypatch.setattr(methods, "post", self._post)
        monkeypatch.setattr(foreground, "wait_typing_pause", lambda *a, **k: True)
        give_back = desk._give_back

        def end_slice(prev, timeout_s=0.6, exclude=None) -> bool:
            time.sleep(give_back_s)  # SetForegroundWindow attend la cible occupée à traiter ses messages
            self._advance()
            if self.holds and len(self.holds[-1]) == 1:
                self.holds[-1].append(time.monotonic())
            if self.lose:
                self.log.append(("perdus", len(self.queue)))
                self.queue.clear()  # pas encore traités au rendu du premier plan : perdus
            return give_back(prev, timeout_s, exclude)

        monkeypatch.setattr(foreground, "give_back", end_slice)

    # --- Premier plan -------------------------------------------------------------------------------------------
    def activate(self) -> None:
        if self.desk.fg != TARGET:
            self.desk.fg = TARGET
            self.holds.append([time.monotonic()])

    def prepare(self, first: bool) -> int:
        self.activate()
        if first:
            self.selected = False  # ajout : curseur en fin
        return TARGET

    def longest_hold(self) -> float:
        return max(end - start for start, end in self.holds)

    # --- File de messages et traitement -------------------------------------------------------------------------
    def _uia(self, dom: str) -> str:
        """Éditeur riche : un paragraphe vide de fin (<p><br></p>, champ vide ou ligne vide après une Entrée) se lit
        « \\n » ; un paragraphe vide ENTRE deux paragraphes de texte ne se lit pas (mesuré le 2026-10-14 :
        « <p>A</p><p><br></p><p>V</p> » se lit « A\\nV ») : n sauts entre deux textes se lisent n − 1."""
        if not self.rich:
            return dom
        value = re.sub(r"(?<=[^\n])\n(\n+)(?=[^\n])", r"\1", dom)
        return value + "\n" if not dom or dom.endswith("\n") else value

    def _post(self, target, msg, wparam, lparam) -> None:
        if msg == w.WM_CHAR:
            item = ("char", "\n" if wparam == 0x0D else chr(wparam))
        elif msg == w.WM_KEYDOWN:
            item = ("key", wparam)
        else:
            return
        self.log.append(("post", item[1]))
        if self.deaf:
            return
        self._advance()
        if not self.queue:
            self.clock = max(self.clock, time.monotonic())
        self.queue.append(item)

    def _advance(self) -> None:
        now = time.monotonic()
        while self.queue and self.clock + self.per_char <= now:
            if self.freeze_at is not None and self.processed >= self.freeze_at:
                self.clock += self.freeze_s  # le moteur s'arrête un instant : la file s'allonge
                self.freeze_at = None
                continue
            self.clock += self.per_char
            self.processed += 1
            kind, data = self.queue.popleft()
            if kind == "key" and data == 0x0D:
                self.queue.append(("char", "\n"))  # TranslateMessage : le saut de ligne part en fin de file
                continue
            if kind == "key" and data == methods.VK_DELETE:
                if self.deaf_delete:
                    self.deaf_delete -= 1
                elif self.selected:
                    self.dom, self.selected = "", False
            elif kind == "char":
                if self.selected:
                    self.dom, self.selected = "", False
                self.dom += data
            self.published.append((self.clock, self._uia(self.dom)))

    def read(self) -> str:
        """Valeur UIA : celle du DOM UIA_LAG s plus tôt (au début de son paquet si publish_s)."""
        self._advance()
        self.reads += 1
        if self.unreadable_after is not None and self.reads > self.unreadable_after:
            raise uia.UIAError("élément recréé")
        seen = time.monotonic() - self.lag
        if self.publish_s:
            seen = math.floor(seen / self.publish_s) * self.publish_s
        value = self.published[0][1]
        for at, published in self.published:
            if at > seen:
                break
            value = published
        return value


def _reader(sim: Chromium, text: str):
    """(read, prefix) de type_in_slices sur le Chromium simulé."""

    def read() -> str:
        try:
            return methods.norm_text(sim.read())
        except uia.UIAError:
            raise methods.Unreadable("illisible") from None

    return read, lambda value: methods.typed_prefix(value, text)


def _no_duplicate(dom: str, text: str) -> None:
    assert dom == text, f"{len(dom)} caractères au lieu de {len(text)} : {dom[:80]!r}…"


# --- Début relu : typed_prefix et _text_done ---------------------------------------------------------------------
def test_typed_prefix_replace_and_append():
    assert methods.typed_prefix("", "abc") == 0
    assert methods.typed_prefix("ab", "abc") == 2
    assert methods.typed_prefix("abc", "abc") == 3
    assert methods.typed_prefix("abx", "abc") is None  # autre contenu
    assert methods.typed_prefix("abcabc", "abc") is None  # plus long que le texte : doublon
    assert methods.typed_prefix("ab\r\ncd", "ab\ncd") == 5
    assert methods.typed_prefix("\n", "Bonjour") == 0  # éditeur riche vide : son paragraphe vide
    assert methods.typed_prefix("Bonjour Julien\n", "Bonjour Julien\nVoici") == 15  # Entrée relue
    assert methods.typed_prefix("ab\n\n", "ab\ncd") == 3  # paragraphe vide après l'Entrée : un seul saut tapé
    # Ajout : ce qui suit le contenu d'avant ; un éditeur riche vide (« \n ») compte comme vide.
    assert methods.typed_prefix("Bonjour", "Bonjour", "Bonjour", append=True) == 0  # déjà là : rien de tapé
    assert methods.typed_prefix("BonjourBon", "Bonjour", "Bonjour", append=True) == 3
    assert methods.typed_prefix("Bonjour Julien", "Bonjour Julien", "\n", append=True) == 14
    assert methods.typed_prefix("\n", "Bonjour", "\n", append=True) == 0
    assert methods.typed_prefix("Note\nSuite", "Suite", "Note\n\n", append=True) == 5  # paragraphe vide de fin rempli
    assert methods.typed_prefix("xNote", "x", "Note", append=True) is None  # curseur pas en fin : refus


def test_text_done_blank_rich_editor_counts_as_empty():
    """Constat 2 : un éditeur riche vide (<p><br></p>) vaut « \\n » ; son paragraphe est rempli par le texte."""
    ex = actions.Executor(as_auto(FakeAuto()), HWND, lambda rebuild: make_view(), lambda: None)
    two = "Bonjour Julien\nVoici le compte rendu"
    assert ex._text_done(two, "\n", two, replace=False)  # avant : 36 >= 1 + 36 faux
    assert ex._text_done("Bonjour Julien\n", "\n", "Bonjour Julien\n", replace=False)
    assert not ex._text_done("\n", "\n", "Bonjour", replace=False)
    assert ex._text_done("Bonjour\n", "", "Bonjour", replace=True)  # paragraphe vide de fin toléré
    # Règles existantes inchangées.
    assert not ex._text_done("bonjour", "bonjour", "bonjour", replace=False)
    assert ex._text_done("bonjour bonjour", "bonjour ", "bonjour", replace=False)
    assert not ex._text_done("autre", "", "voulu", replace=True)


# --- type_in_slices : Chromium réaliste ----------------------------------------------------------------------------
FREEZE = {"freeze_at": 150, "freeze_s": 0.4}  # pause du moteur pendant une tranche : elle finit sans tout relire
SHORT3 = "\n".join(LONG6.splitlines()[:3])  # 365 caractères sur 3 lignes


@pytest.mark.parametrize(
    ("text", "options"),
    [
        (LONG6, {}),
        (SHORT3, FREEZE),  # traités en retard, après le rendu du premier plan
        (SHORT3, {"publish_s": 0.3}),  # valeur UIA publiée par paquets de 0,3 s (paliers mesurés)
        (SHORT3, {"freeze_at": 100, "freeze_s": 1.5}),  # page gelée 1,5 s (recheck8 « Gel » : doublons avant)
        (PROSE[:300], {"per_char": 0.012}),  # page lente, 12 ms par saisie (recheck8 « Lent »)
    ],
    ids=["731 sur 6 lignes", "365 traités en retard", "365 publiés par paquets", "gel 1,5 s", "page lente"],
)
def test_long_multiline_text_is_complete_without_duplicate(monkeypatch, text, options):
    """Constat 1 : 731 caractères sur 6 lignes, 2 ms par caractère et 120 ms de retard UIA (avant : arrêt à 366/731,
    contenu d'origine remplacé par un début) ; sous charge, des doublons (recheck8). Exigé : complet, aucun doublon,
    chaque tranche ≤ SLICE_S."""
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk, **options)
    typed = methods.type_in_slices(TARGET, {TARGET}, text, sim.prepare, *_reader(sim, text), enter_as_key=True)
    assert typed.complete, typed.detail()
    time.sleep(0.3)
    sim._advance()
    _no_duplicate(sim.dom, text)
    assert typed.confirmed == len(text) and typed.sent == len(text)  # rien n'a été envoyé deux fois
    assert sim.longest_hold() <= methods.SLICE_S, sim.holds  # premier plan de Julien jamais emprunté plus de 0,5 s
    assert typed.longest_ms <= methods.SLICE_S * 1000
    assert desk.gave_back == [JULIEN] * typed.slices


@pytest.mark.parametrize("text", [LONG6, PROSE[:400]], ids=["731 sur 6 lignes", "400 sans saut"])
def test_characters_lost_at_give_back_stop_honestly_without_duplicate(monkeypatch, text):
    """Caractères non traités au rendu du premier plan, perdus : rien n'est jamais renvoyé (un caractère non relu a pu
    n'être que retardé : le renvoyer ferait un doublon). L'arrêt le dit, et le champ contient un début exact."""
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk, lose=True, publish_s=0.3, **FREEZE)
    typed = methods.type_in_slices(TARGET, {TARGET}, text, sim.prepare, *_reader(sim, text), enter_as_key=True)
    time.sleep(0.3)
    sim._advance()
    assert text.startswith(sim.dom), sim.dom[-60:]  # un début exact : ni doublon ni saut déplacé
    if not typed.complete:
        assert "not read back" in typed.stopped and "no character resent" in typed.detail()
        assert typed.sent > typed.confirmed == len(sim.dom)
    assert sim.longest_hold() <= methods.SLICE_S


def test_blank_lines_in_rich_editor_stay_in_place(monkeypatch):
    """recheck8 : courriel à lignes vides dans un éditeur riche (« <p><br></p> » se lit « \\n » de plus) : avant, les
    Entrées étaient repoussées en fin de texte et la frappe s'arrêtait sur un texte faux."""
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk, rich=True)
    text = "Bonjour Julien,\n\nVoici le compte rendu de la réunion.\n\nCordialement"
    typed = methods.type_in_slices(TARGET, {TARGET}, text, sim.prepare, *_reader(sim, text), enter_as_key=True)
    assert typed.complete, typed.detail()
    _no_duplicate(sim.dom, text)


def test_enter_is_never_overtaken_by_following_characters(monkeypatch):
    """Le saut de ligne d'une Entrée postée part en fin de file : sans relecture, « ab⏎cd » deviendrait « abcd⏎ »."""
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk, per_char=0.004)
    text = "ligne 1\nligne 2\n\nligne 4"
    typed = methods.type_in_slices(TARGET, {TARGET}, text, sim.prepare, *_reader(sim, text), enter_as_key=True)
    assert typed.complete
    _no_duplicate(sim.dom, text)


# --- Bornes de la tranche ------------------------------------------------------------------------------------------
def test_slow_prepare_never_stretches_a_slice(monkeypatch):
    """Sonde test_probe_slice_bound_with_slow_prepare : 605 ms avant. Préparation de 0,4 s à chaque tranche : aucune
    tranche ne dépasse 0,5 s, et l'arrêt est honnête (rien ne peut être envoyé)."""
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk)

    def slow(first: bool) -> int:
        target = sim.prepare(first)
        time.sleep(0.4)
        return target

    with pytest.raises(methods.MethodError, match="to receive the keyboard.*nothing was typed"):
        methods.type_in_slices(TARGET, {TARGET}, "y" * 2000, slow, *_reader(sim, "y" * 2000))
    assert len(sim.holds) == methods.STARVED_SLICES and sim.longest_hold() <= methods.SLICE_S and sim.dom == ""


def test_slow_give_back_lengthens_the_reserve(monkeypatch):
    """Banc vivant : sous charge, la restitution a pris jusqu'à ~90 ms. La réserve de fin de tranche s'allonge alors
    pour les tranches suivantes : le premier plan de Julien n'est jamais emprunté plus de SLICE_S."""
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk, give_back_s=0.12, publish_s=0.3)  # relectures tardives : tranches à l'échéance
    typed = methods.type_in_slices(TARGET, {TARGET}, PROSE[:500], sim.prepare, *_reader(sim, PROSE[:500]))
    assert typed.complete and typed.slices > 2
    _no_duplicate(sim.dom, PROSE[:500])
    first, *others = [end - start for start, end in sim.holds]
    assert first <= methods.SLICE_S, sim.holds  # réserve par défaut (0,2 s) : la restitution de 0,12 s y tient
    assert max(others) <= methods.SLICE_S, sim.holds


def test_slow_first_prepare_still_completes_within_bound(monkeypatch):
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk)

    def slow_first(first: bool) -> int:
        target = sim.prepare(first)
        if first:
            time.sleep(0.4)  # focus UIA, activation et sélection lentes
        return target

    typed = methods.type_in_slices(TARGET, {TARGET}, PROSE[:300], slow_first, *_reader(sim, PROSE[:300]))
    assert typed.complete
    _no_duplicate(sim.dom, PROSE[:300])
    assert sim.longest_hold() <= methods.SLICE_S


# --- Arrêts honnêtes -------------------------------------------------------------------------------------------------
def test_unreadable_field_is_never_complete(monkeypatch):
    """Sonde test_probe_final_unread_slice_reported_complete : complete était vrai sans relecture."""
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk)
    sim.unreadable_after = 0
    typed = methods.type_in_slices(TARGET, {TARGET}, "abc", sim.prepare, *_reader(sim, "abc"))
    assert not typed.complete and typed.confirmed == 0 and "can no longer be read back" in typed.stopped


def test_unreadable_field_with_newlines_does_not_stop_silently_at_first_enter(monkeypatch):
    """Sonde test_probe_unreadable_field_stops_at_first_enter (7/20) : l'arrêt est rendu, jamais un succès."""
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk)
    sim.unreadable_after = 3
    text = "ligne1\nligne2\nligne3"
    typed = methods.type_in_slices(TARGET, {TARGET}, text, sim.prepare, *_reader(sim, text), enter_as_key=True)
    assert not typed.complete and "can no longer be read back" in typed.detail()


def test_foreign_content_stops_typing(monkeypatch):
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk)
    original = sim._advance

    def julien_types() -> None:
        original()
        if len(sim.dom) >= 20 and "§" not in sim.dom:
            sim.dom += "§"  # quelqu'un d'autre écrit dans le champ
            sim.published.append((time.monotonic() - 1, sim.dom))

    sim._advance = julien_types
    typed = methods.type_in_slices(TARGET, {TARGET}, PROSE, sim.prepare, *_reader(sim, PROSE))
    assert not typed.complete and "something other than the start of the text" in typed.stopped


def test_field_that_loses_confirmed_text_stops(monkeypatch):
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk, publish_s=0.3)
    original = sim._advance

    def remount() -> None:
        original()
        if len(sim.dom) >= 100 and not sim.deaf:
            sim.dom, sim.queue, sim.deaf = "", deque(), True  # champ re-monté : vidé, il ne reçoit plus rien
            sim.published.append((time.monotonic() - 1, ""))

    sim._advance = remount
    typed = methods.type_in_slices(TARGET, {TARGET}, PROSE, sim.prepare, *_reader(sim, PROSE))
    assert not typed.complete and "lost text already read back" in typed.stopped


# --- Exécuteur : éditeur riche, remplacement, Unconfirmed --------------------------------------------------------------
class EditorAuto(FakeAuto):
    """UI Automation d'un champ Chromium simulé : la valeur relue vient du Chromium simulé."""

    def __init__(self, sim: Chromium) -> None:
        super().__init__()
        self.sim = sim

    def refresh(self, element) -> dict:
        live = super().refresh(element)
        live["value"] = self.sim.read()
        return live

    def set_focus(self, element) -> None:
        self.sim.activate()  # le focus UIA active la fenêtre Chromium

    def select_all_text(self, element) -> None:
        self.sim.selected = True

    def caret_to_end(self, element) -> None:
        self.sim.selected = False


def _chromium_executor(monkeypatch, sim: Chromium, name: str, extra: tuple = ()) -> actions.Executor:
    field = node(
        "Edit", name, framework="Chrome", has_value=True, readonly=False, aria_properties="multiline=true",
        value=sim.read(),
    )  # fmt: skip
    nodes = (field, *extra)
    info = snapshot.WindowInfo(
        HWND, "Labo Web", "msedge.exe", "Chrome_WidgetWin_1", w.Rect(0, 0, 600, 640), False, False, False, "Chromium"
    )
    elements = [snapshot.Element(i + 1, n, 0, HWND) for i, n in enumerate(nodes)]
    view = snapshot.View(HWND, info, [HWND], elements, snapshot.frame_for(info.rect), framework="Chromium")
    ex = actions.Executor(as_auto(EditorAuto(sim)), HWND, lambda rebuild: view, lambda: None)
    ex._pids = {7}
    monkeypatch.setattr(actions.w, "root", lambda h: h)
    monkeypatch.setattr(actions.foreground, "activate", lambda hwnd, timeout_s=0.4: sim.activate() or True)
    monkeypatch.setattr(actions.methods, "keyboard_target", lambda hwnd, windows=None: TARGET)
    return ex


@pytest.mark.parametrize(
    "text", ["Bonjour Julien\nVoici le compte rendu", "y" * 600, SHORT3], ids=["36 sur 2 lignes", "600", "365 sur 3"]
)
def test_rich_editor_append_is_read_back(monkeypatch, text):
    """Constat 2 : éditeur riche (role=textbox, <p><br></p>) : avant, arrêt à 15/36 et 284/600."""
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk, rich=True)
    ex = _chromium_executor(monkeypatch, sim, "Paragraphes")
    outcome = ex.write("Paragraphes", text, replace=False)
    assert outcome.ok and outcome.verified and "read back ✓" in outcome.line, outcome.line
    _no_duplicate(sim.dom, text)
    assert sim.longest_hold() <= methods.SLICE_S


def test_replace_text_that_starts_like_the_original(monkeypatch):
    """Remplacement : le contenu d'origine est effacé et relu vide AVANT tout caractère (sinon « Bonjour » relu ne dit
    pas si rien n'a été traité ou si « Bonjour » a été retapé)."""
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk, value="Bonjour")
    ex = _chromium_executor(monkeypatch, sim, "Notes")
    text = "Bonjour Julien,\n" + PROSE[:300]
    outcome = ex.write("Notes", text, replace=True)
    assert outcome.ok and outcome.verified, outcome.line
    _no_duplicate(sim.dom, text)
    posts = [item for kind, item in sim.log if kind == "post"]
    assert posts[0] == methods.VK_DELETE  # Suppr avant le premier caractère


def test_replace_whose_delete_is_ignored_keeps_original(monkeypatch):
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk, value="CONTENU ORIGINAL DE JULIEN", deaf_delete=99)
    ex = _chromium_executor(monkeypatch, sim, "Notes")
    with pytest.raises(actions.Unconfirmed, match="original content intact"):
        ex.write("Notes", "Nouveau texte", replace=True)
    assert sim.dom == "CONTENU ORIGINAL DE JULIEN"
    assert not [item for kind, item in sim.log if kind == "post" and item != methods.VK_DELETE]  # aucun caractère


def test_partial_typing_stays_unknown_and_stops_the_batch(monkeypatch):
    """Texte partiellement tapé mais devenu illisible → ~ (Unconfirmed), étapes suivantes non exécutées : un
    message tronqué n'est jamais envoyé ni sa frappe rejouée."""
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk)
    send = node("Button", "Envoyer", has_invoke=True)
    auto_calls: list = []
    ex = _chromium_executor(monkeypatch, sim, "Message", (send,))
    ex.auto.invoke = lambda element: auto_calls.append(element)
    monkeypatch.setattr(server.w, "IsWindow", lambda h: True)
    sim.unreadable_after = 6  # l'élément est recréé pendant la frappe
    steps = [server.Step(do="type", target="Message", text=LONG6), server.Step(do="click", target="Envoyer")]
    report, failed = server._run_steps(ex, steps, HWND, as_call())
    assert not failed and report[0].startswith("1. ~") and "partially typed" in report[0], report
    assert "(steps 2 to 2 not run)" in report[-1] and auto_calls == []


def test_unreadable_field_is_refused_before_typing(monkeypatch):
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk)
    ex = _chromium_executor(monkeypatch, sim, "Notes")
    field = ex.resolve_element("Notes", actions.TEXT_ROLES, "champ de saisie")
    sim.unreadable_after = sim.reads  # désormais, plus aucune relecture
    with pytest.raises(actions.ActionError, match="unreadable"):
        ex.write_element(field, "abc", replace=False)
    assert not [kind for kind, _ in sim.log if kind == "post"] and sim.holds == []


def test_clearing_an_empty_field_needs_no_activation(monkeypatch):
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk)
    ex = _chromium_executor(monkeypatch, sim, "Notes")
    outcome = ex.write("Notes", "", replace=True)
    assert outcome.ok and outcome.verified and sim.holds == [] and "already empty" in outcome.line


def test_clearing_stays_within_one_slice(monkeypatch):
    desk = Desk(monkeypatch)
    sim = Chromium(monkeypatch, desk, value="abc", lag=0.6)  # vidage relu après l'échéance de la tranche
    ex = _chromium_executor(monkeypatch, sim, "Notes")
    outcome = ex.write("Notes", "", replace=True)
    assert outcome.ok and outcome.verified and sim.dom == ""
    assert len(sim.holds) == 1 and sim.longest_hold() <= methods.SLICE_S
