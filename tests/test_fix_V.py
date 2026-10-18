"""Deux échecs de la suite vivante (préexistants en 0.5.0), mesurés le 2026-10-14 sur l'Electron du labo :
1. fenêtre Chromium recouverte qui a eu le premier plan une fois (activation brève) : « occluded », le moteur de rendu
   ne publie plus les changements d'accessibilité qu'à l'action suivante (journal en retard d'un événement) →
   ScrollIntoView du Document racine avant toute lecture (snapshot.flush_web) ;
2. éditeur riche à paragraphes : un paragraphe vide entre deux paragraphes de texte n'est pas publié
   (« <p>A</p><p><br></p><p>V</p> » se lit « A\\nV ») → courriel à lignes vides arrêté à 17/67 → typed_prefix
   reconnaît cette forme.
"""

from __future__ import annotations

import comtypes
import pytest
from test_contract import HWND, FakeAuto, as_auto, make_view, node

from nexus_windows import actions, methods, snapshot, uia
from nexus_windows import win32 as w

COURRIEL = "Bonjour Julien,\n\nVoici le compte rendu de la réunion.\n\nCordialement"


# --- 2. Forme publiée par un éditeur riche Chromium -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("\n", 0),  # éditeur vide
        ("Bonjour Julien,", 15),
        ("Bonjour Julien,\n", 16),  # première Entrée : paragraphe vide de fin
        ("Bonjour Julien,\n\n", 17),  # seconde Entrée
        ("Bonjour Julien,\nV", 18),  # le paragraphe vide intercalé n'est plus publié (avant : None → arrêt 17/67)
        ("Bonjour Julien,\nVoici le compte rendu de la réunion.\n\n", 55),
        ("Bonjour Julien,\nVoici le compte rendu de la réunion.\nCordialement", 67),
        ("Bonjour Julien,\n\nVoici le compte rendu de la réunion.\n\nCordialement", 67),  # zone de texte : telle quelle
        ("Bonjour Julien,\nVV", None),  # doublon
        ("Bonjour Julien,\nVoici le compte rendu de la réunion.\nCordialementCordialement", None),
        ("Bonjour Julien,\nX", None),  # autre saisie
    ],
)
def test_typed_prefix_reads_chromium_rich_editor_form(value, expected):
    assert methods.typed_prefix(value, COURRIEL) == expected


def test_rich_form_measured_cases():
    """Valeurs relues le 2026-10-14 (Electron du labo, éditeur « Paragraphes »), pour chaque texte tapé."""
    measured = {
        "A\n\nV": "A\nV",  # A, vide, V
        "A\n\n\nV": "A\n\nV",  # A, vide, vide, V
        "A\n\n\n\nV": "A\n\n\nV",
        "A\n\nV\n\n\nW": "A\nV\n\nW",
        "A\n\nV\n": "A\nV\n",  # paragraphe vide de fin : un « \n »
        "\nA": "\nA",  # paragraphe vide de tête : un « \n »
        "\n\nA": "\n\nA",
        "A\n\n": "A\n\n",
    }
    for typed, read in measured.items():
        assert methods.typed_prefix(read, typed) == len(typed), (typed, read)
    # Début de texte : chaque étape d'une frappe de « A\n\n\nV » se relit sans ambiguïté sur ce qui a été tapé.
    assert [methods.typed_prefix(v, "A\n\n\nV") for v in ("A", "A\n", "A\n\n", "A\n\n\n", "A\n\nV")] == [1, 2, 3, 4, 5]


def test_rich_form_on_append_after_existing_text():
    """Ajout derrière un contenu qui finit par du texte : les lignes vides tapées sont intercalées, donc réduites."""
    assert methods.typed_prefix("Note\nSuite", "\n\nSuite", "Note", append=True) == 7
    assert methods.typed_prefix("Note\n\nSuite", "\n\nSuite", "Note", append=True) == 7  # zone de texte
    assert methods.typed_prefix("Note\nAutre", "\n\nSuite", "Note", append=True) is None


def test_rich_form_never_counts_more_than_the_text():
    assert methods._rich_head("A\nV\n", "A\n\nV") == 4  # paragraphe vide de fin en trop : pas une Entrée de plus
    assert methods._rich_head("A\nVX", "A\n\nV") is None
    assert methods._rich_head("A\n\nV", "A\n\nV") is None  # forme littérale : c'est _head qui la lit


def test_text_done_accepts_rich_form():
    ex = actions.Executor(as_auto(FakeAuto()), HWND, lambda rebuild: make_view(), lambda: None)
    read = "Bonjour Julien,\nVoici le compte rendu de la réunion.\nCordialement"
    assert ex._text_done(read, "\n", COURRIEL, replace=True)
    assert ex._text_done(read, "\n", COURRIEL, replace=False)
    assert not ex._text_done("Bonjour Julien,\nVoici", "\n", COURRIEL, replace=True)


# --- 1. Publication d'une fenêtre Chromium recouverte ---------------------------------------------------------------
class FlushAuto(FakeAuto):
    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail

    def scroll_into_view(self, element) -> None:
        self.calls.append(("scroll_into_view", element))
        if self.fail:
            raise comtypes.COMError(-2147220991, "élément disparu", (None, None, None, None, None))

    def refresh(self, element) -> dict:
        self.calls.append(("refresh", element))
        return super().refresh(element)


def web_tree(*children: uia.Node) -> uia.Node:
    """Fenêtre Chromium : Document racine (RootWebArea) qui contient un cadre interne (autre RootWebArea)."""
    root = node("Pane", "Labo Web", framework="Chrome")
    doc = node("Document", "Labo Web", framework="Chrome", automation_id="RootWebArea")
    frame = node("Document", "cadre", framework="Chrome", automation_id="RootWebArea")
    doc.children = [*children, frame]
    root.children = [doc]
    for child in doc.children:
        child.parent = doc
    doc.parent = root
    return root


@pytest.fixture
def covered(monkeypatch):
    """La fenêtre cible n'a pas le premier plan (Julien est dans une autre fenêtre)."""
    state = {"fg": 999}
    monkeypatch.setattr(w, "foreground", lambda: state["fg"])
    monkeypatch.setattr(w, "root", lambda hwnd: hwnd)
    snapshot._ROOT_DOCUMENTS.clear()
    yield state
    snapshot._ROOT_DOCUMENTS.clear()


def test_root_web_documents_skip_inner_frames():
    tree = web_tree(node("Button", "Valider", framework="Chrome"))
    documents = snapshot.root_web_documents(tree)
    assert [d.name for d in documents] == ["Labo Web"]  # le cadre interne ferait défiler la page qui le contient


def test_flush_scrolls_root_document_only_when_covered(covered):
    tree = web_tree()
    auto = FlushAuto()
    assert snapshot.flush_web(as_auto(auto), HWND) is None  # aucun document connu : rien
    snapshot.remember_web_documents(HWND, tree)
    assert snapshot.flush_web(as_auto(auto), HWND) is True
    assert auto.calls == [("scroll_into_view", tree.children[0].element)]
    covered["fg"] = HWND  # au premier plan : dessinée et publiée, rien n'attend
    assert snapshot.flush_web(as_auto(auto), HWND) is None and len(auto.calls) == 1


def test_flush_failure_forgets_documents(covered):
    snapshot.remember_web_documents(HWND, web_tree())
    assert snapshot.flush_web(as_auto(FlushAuto(fail=True)), HWND) is False
    assert HWND not in snapshot._ROOT_DOCUMENTS  # relus au prochain arbre
    snapshot.remember_web_documents(HWND, node("Pane", "Bloc-notes"))
    assert HWND not in snapshot._ROOT_DOCUMENTS


def chromium_view(*nodes: uia.Node) -> snapshot.View:
    view = make_view(*nodes)
    view.framework = "Chromium"
    return view


def test_executor_reads_flush_before_refresh_in_chromium(covered):
    item = node("ListItem", "Charlie", framework="Chrome", has_selection_item=True)
    snapshot.remember_web_documents(HWND, web_tree(item))
    auto = FlushAuto()
    ex = actions.Executor(as_auto(auto), HWND, lambda rebuild: chromium_view(item), lambda *a: None)
    ex._refresh(item.element)
    assert [c[0] for c in auto.calls] == ["scroll_into_view", "refresh"]  # publication déclenchée, puis relue


def test_executor_reads_do_not_flush_outside_chromium(covered):
    box = node("CheckBox", "Option", has_toggle=True, toggle=0)
    snapshot.remember_web_documents(HWND, web_tree())
    auto = FlushAuto()
    ex = actions.Executor(as_auto(auto), HWND, lambda rebuild: make_view(box), lambda *a: None)
    ex._refresh(box.element)
    assert [c[0] for c in auto.calls] == ["refresh"]


# --- Clic Chromium : état de l'élément publié après le titre --------------------------------------------------------
class LateSelection(FakeAuto):
    """L'élément n'est relu sélectionné qu'à la troisième relecture (titre de page publié avant l'arbre)."""

    def __init__(self, item) -> None:
        super().__init__()
        self.item, self.reads = item, 0

    def refresh(self, element) -> dict:
        self.reads += 1
        if self.reads >= 3:
            self.item.element.live["selected"] = True
        return super().refresh(element)


def test_effect_seen_in_title_is_upgraded_to_read_back_selection():
    item = node("ListItem", "Charlie", framework="Chrome", has_selection_item=True, selected=False)
    auto = LateSelection(item)
    ex = actions.Executor(as_auto(auto), HWND, lambda rebuild: make_view(item), lambda *a: None)
    e = make_view(item).elements[0]
    before = (ex._signature_own(e), ((1, "Labo Web — prêt"),), None)
    change = ex._own_state_change(e, before, "effet constaté : fenêtre « Labo Web — liste Charlie »")
    assert change == "relu ✓ sélectionné"


def test_effect_without_state_pattern_is_left_as_is():
    button = node("Button", "Valider", framework="Chrome", has_invoke=True)
    auto = FakeAuto()
    ex = actions.Executor(as_auto(auto), HWND, lambda rebuild: make_view(button), lambda *a: None)
    e = make_view(button).elements[0]
    seen = "effet constaté : fenêtre « Labo Web — clic 1 »"
    assert ex._own_state_change(e, (("x",), (), None), seen) == seen


def test_effect_state_never_proved_stays_honest():
    item = node("ListItem", "Charlie", framework="Chrome", has_selection_item=True, selected=False)
    ex = actions.Executor(as_auto(FakeAuto()), HWND, lambda rebuild: make_view(item), lambda *a: None)
    e = make_view(item).elements[0]
    before = (ex._signature_own(e), (), None)
    seen = "effet constaté : changement visible"
    assert ex._own_state_change(e, before, seen, wait_s=0.1) == seen


# --- Champ hors de vue (page défilée) : visé par son nom exact, amené à l'écran -----------------------------------
class ScrollAuto(FakeAuto):
    def __init__(self) -> None:
        super().__init__()
        self.scrolled: list = []

    def scroll_into_view(self, element) -> None:
        self.scrolled.append(element)
        element.live["offscreen"] = False


def scrolled_page():
    """Page défilée de 275 px (mesuré : Electron du labo) : « Gel » hors de vue, « Valider » visible."""
    gel = node(
        "Edit", "Gel", framework="Chrome", has_scroll_item=True, has_value=True, offscreen=True, runtime_id=(1, 5)
    )
    gel_label = node("Text", "Gel", framework="Chrome", has_scroll_item=True, offscreen=True)
    valider = node("Button", "Valider", framework="Chrome", has_invoke=True, runtime_id=(1, 9))
    root = node("Pane", "Labo Web", framework="Chrome")
    root.children = [gel_label, gel, valider]
    before = make_view(valider)
    before.roots = {HWND: root}
    visible_gel = node("Edit", "Gel", framework="Chrome", has_value=True, runtime_id=(1, 5))
    after = make_view(visible_gel, valider)
    after.roots = {HWND: root}
    return gel, before, after


def test_scrolled_away_lists_only_offscreen_scrollable_controls():
    gel, before, _ = scrolled_page()
    assert [e.node for e in snapshot.scrolled_away(before)] == [gel]  # ni l'étiquette, ni ce qui est visible


def test_name_scrolled_out_of_view_is_brought_into_view():
    gel, before, after = scrolled_page()
    auto = ScrollAuto()
    state = {"view": before}

    def get_view(rebuild: bool) -> snapshot.View:
        if rebuild and auto.scrolled:
            state["view"] = after
        return state["view"]

    ex = actions.Executor(as_auto(auto), HWND, get_view, lambda *a: None)
    ex._pids = {7}
    kind, element = ex.resolve("Gel", {"Edit"})
    assert auto.scrolled == [gel.element] and element.name == "Gel"
    assert any("hors de vue : amené à l'écran" in n and "[" not in n for n in ex.notes)  # jamais de [N] cité


def test_partial_name_never_scrolls_the_page():
    _, before, after = scrolled_page()
    auto = ScrollAuto()
    ex = actions.Executor(as_auto(auto), HWND, lambda rebuild: before, lambda *a: None)
    ex._pids = {7}
    with pytest.raises(actions.ActionError, match="Aucun élément « Ge »"):
        ex.resolve("Ge", {"Edit"})
    assert auto.scrolled == []
