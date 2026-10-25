"""Trois défauts relevés le 25/10 en réel dans Chrome (fiche du Chrome Web Store, session NexusBrowser) :
1. la boîte « Ouvrir » possédée par Chrome n'était pas listée par windows (visée par son hwnd) ;
2. un clic simulé sur un bouton de dépôt n'ouvrait pas le sélecteur (Chromium exige une vraie activation), mais
   l'outil annonçait un « changement visible » ;
3. un saut de ligne tapé dans un champ multiligne (« Description* ») était pris pour une Entrée vers le bouton d'aide
   voisin « Pourquoi ne puis-je pas envoyer cet élément ? », et le résumé d'accord ne nommait pas le champ."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nexus_windows import act_chromium, actions, server, snapshot
from nexus_windows import win32 as w

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contract import FakeAuto, executor, make_view, node  # noqa: E402
from test_garde_fous import _group  # noqa: E402


# --- 1. Fenêtres possédées ----------------------------------------------------------------------------------------
def _fake_desktop(monkeypatch, windows: dict[int, tuple[str, str, int, int]]) -> None:
    """windows : hwnd → (classe, titre, propriétaire, style étendu), dans l'ordre Z (du haut vers le bas)."""
    monkeypatch.setattr(w, "enum_windows", lambda: list(windows))
    monkeypatch.setattr(w, "class_name", lambda h: windows[h][0])
    monkeypatch.setattr(w, "window_text", lambda h: windows[h][1])
    monkeypatch.setattr(w, "owner", lambda h: windows[h][2] if h in windows else 0)
    monkeypatch.setattr(w, "ex_style", lambda h: windows[h][3])
    monkeypatch.setattr(w, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(w, "is_cloaked", lambda h: False)
    monkeypatch.setattr(w, "IsIconic", lambda h: False)
    monkeypatch.setattr(w, "IsZoomed", lambda h: False)
    monkeypatch.setattr(w, "foreground", lambda: 0)
    monkeypatch.setattr(w, "frame_rect", lambda h: w.Rect(0, 0, 600, 400))
    monkeypatch.setattr(w, "window_pid", lambda h: 7)
    monkeypatch.setattr(w, "process_name", lambda pid: "chrome.exe")
    monkeypatch.setattr(w, "enum_children", lambda h: [])
    monkeypatch.setattr(snapshot, "framework_of", lambda h, cls: "Chromium")


CHROME = {
    20: ("#32770", "Ouvrir", 10, 0),  # boîte de dialogue possédée par Chrome : listée, rattachée
    21: ("Chrome_WidgetWin_1", "", 10, 0),  # bulle sans titre : jamais
    22: ("tooltips_class32", "Astuce", 10, w.WS_EX_TOOLWINDOW),  # fenêtre outil : jamais
    10: ("Chrome_WidgetWin_1", "Chrome Web Store - Google Chrome", 0, 0),
}


def test_owned_dialog_listed_with_its_owner(monkeypatch):
    _fake_desktop(monkeypatch, CHROME)
    listed = [(i.hwnd, i.owner) for i in snapshot.app_windows()]
    assert listed == [(20, 10), (10, 0)]


def test_windows_tool_shows_the_dialog_of_its_owner(monkeypatch):
    _fake_desktop(monkeypatch, CHROME)
    rows = [server.window_row(info) for info in snapshot.app_windows()]
    assert rows[0].startswith('20 "Ouvrir" — chrome.exe') and '[dialog of 10 "Chrome Web Store' in rows[0]
    assert "dialog of" not in rows[1]


def test_dialog_targeted_by_title_main_window_by_process(monkeypatch):
    from nexus_windows.session import Session

    _fake_desktop(monkeypatch, CHROME)
    session = Session()
    assert session._resolve_window("Ouvrir") == 20
    assert session._resolve_window("chrome") == 10  # le processus désigne sa fenêtre principale, pas sa boîte


# --- 2. Sélecteur de fichiers Chromium ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("role", "name", "expected"),
    [
        ("Button", "Choisir un fichier", True),
        ("Button", "Choose Files", True),
        ("Button", "Parcourir", True),
        ("Group", "Déposez votre fichier ZIP ici", True),  # zone de dépôt
        ("Group", "Glisser un fichier ici", True),
        ("Button", "Upload", True),
        ("Button", "Dropdown", False),
        ("TabItem", "Upload", False),  # onglet : jamais un sélecteur
        ("Button", "Enregistrer", False),
    ],
)
def test_file_picker_trigger(role, name, expected):
    e = snapshot.Element(1, node(role, name), 0, 1)
    assert act_chromium.opens_file_picker(e) is expected


def _picker_executor(monkeypatch, dialogs_after: set[int], allow_foreground: bool = False):
    ex = executor(make_view(node("Button", "Choisir un fichier", has_invoke=True)), FakeAuto())
    ex.allow_foreground = allow_foreground
    state = {"clicked": 0}

    def dialogs() -> set[int]:
        return dialogs_after if state["clicked"] else set()

    monkeypatch.setattr(ex, "_dialogs", dialogs)
    monkeypatch.setattr(act_chromium, "FILE_DIALOG_S", 0.05)
    monkeypatch.setattr(w, "window_text", lambda h: "Ouvrir")

    def click() -> str:
        state["clicked"] += 1
        return "effect observed: visible change"  # survol ou focus : ce que l'outil prenait pour un effet

    def late() -> str:
        state["clicked"] += 1
        raise act_chromium.NoEffect("no effect observed within 1.0 s")  # la boîte n'arrive qu'après la relecture

    return ex, [("UIA Invoke", click), ("click by messages", late)], state


def test_file_picker_without_dialog_is_a_failure_suggesting_foreground(monkeypatch):
    ex, chain, state = _picker_executor(monkeypatch, set())
    with pytest.raises(actions.ActionError, match="no file dialog opened.*foreground=true"):
        ex._click_file_picker(chain, 'click on Button "Choisir un fichier"', lambda: pytest.fail("real mouse"))
    assert state["clicked"] == 2  # chaque technique essayée, aucune annoncée comme un succès


def test_file_picker_with_dialog_is_read_back_and_stops_there(monkeypatch):
    ex, chain, state = _picker_executor(monkeypatch, {20})
    outcome = ex._click_file_picker(chain, "click", lambda: pytest.fail("real mouse"))
    assert outcome.symbol == "✓" and 'file dialog "Ouvrir" opened' in outcome.line
    assert state["clicked"] == 1  # boîte ouverte : aucune autre technique par-dessus


def test_file_picker_dialog_arriving_late_is_still_read_back(monkeypatch):
    ex, chain, state = _picker_executor(monkeypatch, {20})
    outcome = ex._click_file_picker(chain[1:], "click", lambda: pytest.fail("real mouse"))
    assert outcome.symbol == "✓" and "click by messages" in outcome.line


def test_file_picker_real_click_with_consent(monkeypatch):
    ex, _, state = _picker_executor(monkeypatch, {20}, allow_foreground=True)
    monkeypatch.setattr(actions.realinput, "borrowed", lambda *a: __import__("contextlib").nullcontext())
    outcome = ex._click_file_picker([], "click", lambda: state.update(clicked=1))
    assert outcome.symbol == "✓" and "real mouse" in outcome.line


# --- 3. Saut de ligne dans un champ multiligne --------------------------------------------------------------------
def _form(*extra):
    field = node("Edit", "Description*", has_value=True, value="", focused=True, aria_properties="multiline=true")
    _group(field, *extra)
    return field


def test_line_break_next_to_a_help_question_is_free():
    help_button = node("Button", "Pourquoi ne puis-je pas envoyer cet élément ?", has_invoke=True)
    field = _form(help_button)
    ex = executor(make_view(field, help_button), FakeAuto())
    ex._confirm_text(ex.resolve_element("Description*", set(), "x"), "Ligne 1\nLigne 2")  # rien à confirmer


def test_line_break_next_to_a_send_button_names_the_field():
    send = node("Button", "Envoyer", has_invoke=True)
    field = _form(send)
    ex = executor(make_view(field, send), FakeAuto())
    with pytest.raises(actions.ConfirmationRequired, match=r'line break typed in "Description\*"'):
        ex._confirm_text(ex.resolve_element("Description*", set(), "x"), "Ligne 1\nLigne 2")


def test_link_next_to_the_field_is_not_what_enter_triggers():
    link = node("Hyperlink", "Envoyer", has_invoke=True)
    field = _form(link)
    ex = executor(make_view(field, link), FakeAuto())
    ex._confirm_text(ex.resolve_element("Description*", set(), "x"), "a\nb")


# --- Banc commun du 25/10 : molette Electron en butée -------------------------------------------------------------
def test_scroll_item_never_scrolls_the_outer_area_when_the_inner_one_is_at_its_end(monkeypatch):
    """Zone A sous le point, en bout de course (lignes cachées au-dessus seulement), dans une page qui a encore des
    lignes cachées en dessous : la page défilait à la place de A, annoncée « ✓ ». Désormais refus, rien n'est envoyé."""
    from test_contract import FakeAuto as _Auto

    sent = []

    class Pattern:
        def ScrollIntoView(self):  # noqa: N802  (nom COM)
            sent.append("invoke")

    class Auto(_Auto):
        def tree(self, hwnd):
            page = node("Document", "page", rect=(0, 0, 400, 300))
            area = node("Group", "A", rect=(0, 0, 200, 100))
            area.parent = page
            page.children.append(area)
            for i in range(3):  # lignes de A : deux au-dessus de la zone (défilées), une visible
                line = node("Text", f"A{i}", rect=(0, -100 + i * 60, 150, 20))
                line.parent = area
                area.children.append(line)
            for i in range(3):  # lignes de la page, dont deux sous le bas de la page
                line = node("Text", f"P{i}", rect=(220, 200 + i * 80, 150, 20))
                line.parent = page
                page.children.append(line)
            return page

        def pattern(self, element, name):
            return Pattern()

    view = make_view()
    view.framework = "Chromium"
    ex = executor(view, Auto())
    monkeypatch.setattr(ex, "_window_under", lambda x, y: 1)
    with pytest.raises(act_chromium.methods.MethodError, match="already at its end"):
        ex._scroll_by_item(50, 50, "down", 1)
    assert sent == []
