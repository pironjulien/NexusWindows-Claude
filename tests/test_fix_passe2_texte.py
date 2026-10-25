"""Défauts relevés pendant le banc « texte d'abord » du 24/10 (commun/benchmarks/windows/passe2/TEXTE-DABORD.md) :

1. menu Win32 (MainMenu WinForms de l'éditeur de test) : « Fichier > Enregistrer sous » ne trouvait pas l'entrée
   « Enregistrer sous... » (points de suspension), puis UIA Expand échouait ; la boîte ouverte restait « ~ » ;
2. clic sur la cellule « Nom » d'un élément de l'Explorateur : UIA SetFocus l'ajoutait à la sélection ;
3. défilement de Paramètres : un petit pas UIA par cran, et XAML avale les pas qui se suivent (0,3 % par cran) ;
4. fermeture de la Calculatrice par son HWND de contenu (CoreWindow) : « still there ».
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

from nexus_windows import methods, session, uia, worker
from nexus_windows import win32 as w
from nexus_windows.act_base import ActionError, Unconfirmed

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contract import HWND, FakeAuto, FakeEl, executor, make_view, node  # noqa: E402


# --- 1. Menu Win32 : points de suspension, effet relu --------------------------------------------------------
def test_menu_path_matches_an_entry_with_an_ellipsis(monkeypatch):
    posted: list[tuple[int, int]] = []
    monkeypatch.setattr(
        methods,
        "win32_menu_items",
        lambda top: [
            ("Fichier > Ouvrir...", 257),
            ("Fichier > Enregistrer", 258),
            ("Fichier > Enregistrer sous…", 259),
        ],
    )
    monkeypatch.setattr(methods, "post", lambda hwnd, msg, wp, lp: posted.append((msg, wp)))
    assert methods.win32_menu_command(HWND, "Fichier > Enregistrer sous") == "wm_command_menu"
    assert methods.win32_menu_command(HWND, "fichier>ouvrir") == "wm_command_menu"
    assert methods.win32_menu_command(HWND, "Fichier > Enregistrer") == "wm_command_menu"
    assert [wp for _, wp in posted] == [259, 257, 258]  # « Enregistrer » exact, jamais « Enregistrer sous »


def test_menu_exact_entry_wins_over_its_ellipsis_twin(monkeypatch):
    posted: list[int] = []
    monkeypatch.setattr(methods, "win32_menu_items", lambda top: [("File > Run", 10), ("File > Run...", 11)])
    monkeypatch.setattr(methods, "post", lambda hwnd, msg, wp, lp: posted.append(wp))
    methods.win32_menu_command(HWND, "File > Run...")
    methods.win32_menu_command(HWND, "File > Run")
    assert posted == [11, 10]


def test_menu_ambiguous_after_normalization_sends_nothing(monkeypatch):
    posted: list[int] = []
    monkeypatch.setattr(methods, "win32_menu_items", lambda top: [("File > Run...", 10), ("File > Run…", 11)])
    monkeypatch.setattr(methods, "post", lambda hwnd, msg, wp, lp: posted.append(wp))
    with pytest.raises(methods.MethodError, match="ambiguous"):
        methods.win32_menu_command(HWND, "File > Run")
    assert posted == []


def _menu_setup(monkeypatch, popups_after: list[int], titles: dict[int, str], tooltips: tuple[int, ...] = ()):
    from nexus_windows import act_uia

    state = {"sent": False}
    monkeypatch.setattr(methods, "win32_menu_items", lambda top: [("Fichier > Enregistrer sous...", 259)])

    def command(top: int, path: str) -> str:
        state["sent"] = True
        return "wm_command_menu"

    monkeypatch.setattr(methods, "win32_menu_command", command)
    monkeypatch.setattr(act_uia.snapshot, "popups_of", lambda hwnd: list(popups_after) if state["sent"] else [])
    monkeypatch.setattr(act_uia.w, "IsWindow", lambda hwnd: True)
    monkeypatch.setattr(act_uia.w, "window_text", lambda hwnd: titles.get(hwnd, "Éditeur"))
    monkeypatch.setattr(act_uia.w, "style", lambda hwnd: 0 if hwnd in tooltips else act_uia.w.WS_CAPTION)
    monkeypatch.setattr(act_uia.w, "IsWindowEnabled", lambda hwnd: True)
    return state


def test_menu_command_is_proven_by_the_dialog_it_opens(monkeypatch):
    _menu_setup(monkeypatch, [777], {777: "Enregistrer sous"})
    ex = executor(make_view(node("Edit", "Texte", has_value=True, value="")), FakeAuto())
    outcome = ex.menu("Fichier > Enregistrer sous")
    assert outcome.ok and outcome.verified
    assert 'window "Enregistrer sous" opened' in outcome.line


def test_unrelated_popup_or_title_change_is_not_proof(monkeypatch):
    _menu_setup(monkeypatch, [888], {888: "Nouvelle notification", HWND: "Autre titre"})
    ex = executor(make_view(node("Edit", "Texte", has_value=True, value="")), FakeAuto())
    outcome = ex.menu("Fichier > Enregistrer sous")
    assert outcome.ok and outcome.verified is None


def test_tooltip_titled_like_the_entry_is_not_a_dialog(monkeypatch):
    _menu_setup(monkeypatch, [999], {999: "Enregistrer sous"}, tooltips=(999,))
    ex = executor(make_view(node("Edit", "Texte", has_value=True, value="")), FakeAuto())
    outcome = ex.menu("Fichier > Enregistrer sous")
    assert outcome.ok and outcome.verified is None


def test_dialog_whose_title_is_only_part_of_the_entry_is_not_proof(monkeypatch):
    _menu_setup(monkeypatch, [777], {777: "Enregistrer"})  # « Enregistrer » ⊂ « Enregistrer sous » : pas la bonne boîte
    ex = executor(make_view(node("Edit", "Texte", has_value=True, value="")), FakeAuto())
    assert ex.menu("Fichier > Enregistrer sous").verified is None


def test_stop_during_preparation_sends_no_menu_command(monkeypatch):
    from nexus_windows import act_uia

    state = _menu_setup(monkeypatch, [], {})
    read = {"popups": False}
    monkeypatch.setattr(act_uia.snapshot, "popups_of", lambda hwnd: read.update(popups=True) or [])

    def check() -> None:
        if read["popups"]:
            raise worker.Stopped("STOP")

    monkeypatch.setattr(act_uia.worker, "check_cancelled", check)
    ex = executor(make_view(node("Edit", "Texte", has_value=True, value="")), FakeAuto())
    with pytest.raises(worker.Stopped):
        ex.menu("Fichier > Enregistrer sous")
    assert not state["sent"]


def test_stop_during_menu_enumeration_posts_nothing(monkeypatch):
    posted: list[int] = []
    stopped = {"now": False}

    def items(top: int) -> list[tuple[str, int]]:
        stopped["now"] = True  # arrêt posé pendant l'énumération du HMENU
        return [("Fichier > Enregistrer sous...", 259)]

    def check() -> None:
        if stopped["now"]:
            raise worker.Stopped("STOP")

    monkeypatch.setattr(methods, "win32_menu_items", items)
    monkeypatch.setattr(methods, "post", lambda hwnd, msg, wp, lp: posted.append(wp))
    monkeypatch.setattr(methods.worker, "check_cancelled", check)
    with pytest.raises(worker.Stopped):
        methods.win32_menu_command(HWND, "Fichier > Enregistrer sous")
    assert posted == []


def test_unconfirmed_menu_command_is_never_replayed_by_uia(monkeypatch):
    def command(top: int, path: str) -> str:
        raise Unconfirmed("peut-être partie")

    monkeypatch.setattr(methods, "win32_menu_items", lambda top: [("Fichier > Quitter", 260)])
    monkeypatch.setattr(methods, "win32_menu_command", command)
    from nexus_windows import act_uia

    monkeypatch.setattr(act_uia.snapshot, "popups_of", lambda hwnd: [])
    item = node("MenuItem", "Fichier", has_expand=True)
    auto = FakeAuto()
    ex = executor(make_view(item), auto)
    with pytest.raises(Unconfirmed):
        ex.menu("Fichier > Quitter")
    assert auto.calls == []  # aucun chemin UIA par-dessus


def test_menu_command_without_visible_effect_stays_sent_not_proven(monkeypatch):
    _menu_setup(monkeypatch, [], {})
    ex = executor(make_view(node("Edit", "Texte", has_value=True, value="")), FakeAuto())
    outcome = ex.menu("Fichier > Enregistrer sous")
    assert outcome.ok and outcome.verified is None  # envoyé, rien à relire : jamais un faux « ✓ »


# --- 2. Cellule d'une ligne : la ligne sélectionnée seule, jamais SetFocus ---------------------------------
class FocusAddsAuto(FakeAuto):
    """Explorateur : SetFocus sur une cellule AJOUTE sa ligne à la sélection (ce que faisait l'ancien chemin)."""

    def set_focus(self, element: FakeEl) -> None:
        self.calls.append(("set_focus", element))


def _row_with_name_cell(name: str) -> tuple[uia.Node, uia.Node]:
    row = node("ListItem", name, has_selection_item=True, selected=False, framework="DirectUI")
    cell = node("Edit", "Nom", has_value=True, value=name, framework="DirectUI")
    cell.parent = row
    row.children.append(cell)
    return row, cell


def test_click_on_a_name_cell_selects_its_row_alone():
    row, cell = _row_with_name_cell("note")
    auto = FocusAddsAuto()
    ex = executor(make_view(row, cell), auto)
    outcome = ex.click("Edit:Nom")
    assert auto.calls == [("select", row.element)]  # SelectionItem.Select remplace la sélection
    assert outcome.ok and outcome.verified and 'ListItem "note"' in outcome.line


def test_editable_cell_of_a_wpf_grid_row_is_not_replaced_by_its_row():
    row = node("DataItem", "Ligne 1", has_selection_item=True, selected=True)
    cell = node("Edit", "Quantité", has_value=True, value="3")
    cell.parent = row
    row.children.append(cell)
    ex = executor(make_view(row, cell), FakeAuto())
    assert ex._selectable_row_of(ex.view.elements[1]) is None


def test_win32_editor_under_a_directui_row_stays_a_field():
    row, _ = _row_with_name_cell("note")
    editor = node("Edit", "Nom", has_value=True, value="note", framework="Win32", hwnd=5150)
    editor.parent = row
    row.children.append(editor)
    ex = executor(make_view(row, editor), FakeAuto())
    assert ex._selectable_row_of(ex.view.elements[1]) is None


def test_disabled_name_cell_is_refused_not_redirected():
    row, cell = _row_with_name_cell("note")
    cell.element.live["enabled"] = False
    cell.props["enabled"] = False
    auto = FakeAuto()
    ex = executor(make_view(row, cell), auto)
    with pytest.raises(ActionError, match="disabled"):
        ex.click("Edit:Nom")
    assert auto.calls == []


def test_redirected_row_keeps_its_own_number():
    row, cell = _row_with_name_cell("note")
    ex = executor(make_view(row, cell), FakeAuto())
    found = ex._selectable_row_of(ex.view.elements[1])
    assert found is not None and found.id == ex.view.elements[0].id


def test_click_on_a_plain_edit_still_focuses_it():
    field = node("Edit", "Recherche", has_value=True, value="")
    auto = FakeAuto()
    ex = executor(make_view(field), auto)
    assert ex._selectable_row_of(ex.view.elements[0]) is None


def test_click_on_an_item_reads_back_its_selection():
    item = node("ListItem", "beta", has_selection_item=True, selected=False)
    auto = FakeAuto()
    outcome = executor(make_view(item), auto).click("ListItem:beta")
    assert outcome.ok and outcome.verified and "read back ✓ selected" in outcome.line


# --- 3. Défilement : un cran = réglage Windows ; XAML : un pas mesuré, le reste posé en un appel -------------
class XamlScrollAuto(FakeAuto):
    """ScrollViewer animé : chaque pas avance de `small` (ou `large` pour une page), en unités du fournisseur."""

    def __init__(self, small: float = 1.6, large: float = 30.0, at: float = 10.0) -> None:
        super().__init__()
        self.small, self.large, self.position = small, large, at
        self.set_to: list[tuple[float, float]] = []

    def scroll(self, element: FakeEl, h: int, v: int) -> None:
        self.calls.append(("scroll", element))
        amount = {4: self.small, 1: -self.small, 3: self.large, 0: -self.large}.get(v if v != 2 else h, 0.0)
        self.position = max(0.0, min(100.0, self.position + amount))

    def scroll_percent(self, element: FakeEl):
        return -1.0, self.position

    def set_scroll_percent(self, element: FakeEl, h: float, v: float) -> None:
        self.set_to.append((h, v))
        self.position = v

    def scroll_view_size(self, element: FakeEl) -> tuple[float, float] | None:
        return 100.0, self.view_size

    view_size = 50.0


def _settings_like(monkeypatch, auto: XamlScrollAuto, framework: str = "UWP", lines: int = 3):
    from nexus_windows import act_pointer

    monkeypatch.setattr(w, "wheel_scroll_lines", lambda: lines)
    monkeypatch.setattr(w, "wheel_scroll_chars", lambda: 3)
    monkeypatch.setattr(act_pointer.time, "sleep", lambda s: None)
    pane = node("Pane", "Contenu", has_scroll=True, rect=(0, 0, 400, 1000))
    view = make_view(pane)
    view.framework = framework
    ex = executor(view, auto)
    ex._windows, ex.acted = {HWND}, False
    return ex, pane


def test_xaml_scroll_measures_one_step_then_sets_the_rest(monkeypatch):
    auto = XamlScrollAuto()
    ex, pane = _settings_like(monkeypatch, auto)
    outcome = ex.scroll("Pane:Contenu", "down", 5)
    # 5 crans × 3 lignes = 15 pas de 1,6 % (mesuré sur le premier) : 10 → 34 %
    assert auto.calls == [("scroll", pane.element)] and auto.set_to == [(-1, pytest.approx(34.0))]
    assert outcome.ok and outcome.verified and "10.0 % → 34.0 %" in outcome.line


def test_xaml_scroll_keeps_provider_units(monkeypatch):
    auto = XamlScrollAuto(small=0.48)  # panneau virtualisé : un pas = une fraction d'élément, pas 16 px
    ex, _ = _settings_like(monkeypatch, auto)
    ex.scroll("Pane:Contenu", "down", 1)
    assert auto.set_to == [(-1, pytest.approx(10.0 + 0.48 * 3))]


def test_xaml_scroll_up_is_clamped_at_the_top(monkeypatch):
    auto = XamlScrollAuto()
    ex, _ = _settings_like(monkeypatch, auto)
    ex.scroll("Pane:Contenu", "up", 50)
    assert auto.set_to == [(-1, 0.0)]


def test_page_per_notch_setting_uses_large_steps(monkeypatch):
    auto = XamlScrollAuto(at=0.0)
    ex, _ = _settings_like(monkeypatch, auto, lines=-1)
    ex.scroll("Pane:Contenu", "down", 2)
    assert auto.set_to == [(-1, 60.0)]  # premier grand pas mesuré (30 %), deux pages


def test_single_step_is_sent_alone(monkeypatch):
    auto = XamlScrollAuto()
    ex, pane = _settings_like(monkeypatch, auto, lines=1)
    ex.scroll("Pane:Contenu", "down", 1)
    assert auto.calls == [("scroll", pane.element)] and auto.set_to == []


def test_first_step_without_movement_sets_nothing(monkeypatch):
    auto = XamlScrollAuto(at=100.0)
    ex, _ = _settings_like(monkeypatch, auto)
    ex.scroll("Pane:Contenu", "down", 5)
    assert auto.set_to == []


def test_win32_scroll_sends_lines_per_notch_small_steps(monkeypatch):
    auto = XamlScrollAuto()
    ex, pane = _settings_like(monkeypatch, auto, framework="Win32")
    ex.scroll("Pane:Contenu", "down", 2)
    assert auto.set_to == [] and auto.calls == [("scroll", pane.element)] * 6  # 2 crans × 3 lignes


def test_wpf_keeps_its_small_steps(monkeypatch):
    """Petits pas, jamais SetScrollPercent ; un seul par cran depuis le banc commun du 25/10 (trois pas = trois
    défilements reçus par l'application)."""
    auto = XamlScrollAuto()
    ex, pane = _settings_like(monkeypatch, auto, framework="WPF")
    ex.scroll("Pane:Contenu", "down", 2)
    assert auto.set_to == [] and auto.calls == [("scroll", pane.element)] * 2


def test_horizontal_notch_uses_the_characters_setting(monkeypatch):
    auto = XamlScrollAuto()
    ex, pane = _settings_like(monkeypatch, auto, framework="Win32", lines=-1)  # page par cran : vertical seulement
    monkeypatch.setattr(auto, "scroll_percent", lambda element: (auto.position, -1.0))
    ex.scroll("Pane:Contenu", "right", 1)
    assert auto.calls == [("scroll", pane.element)] * 3


def test_zero_lines_per_notch_sends_nothing(monkeypatch):
    auto = XamlScrollAuto()
    ex, _ = _settings_like(monkeypatch, auto, lines=0)
    with pytest.raises(ActionError, match="0 lines"):
        ex.scroll("Pane:Contenu", "down", 1)
    assert auto.set_to == [] and auto.calls == []


def test_stop_after_the_measured_step_sets_nothing(monkeypatch):
    from nexus_windows import act_pointer

    auto = XamlScrollAuto()
    ex, _ = _settings_like(monkeypatch, auto)

    def check() -> None:
        if auto.calls:  # premier pas parti, arrêt posé pendant sa relecture
            raise worker.Stopped("STOP")

    monkeypatch.setattr(act_pointer.worker, "check_cancelled", check)
    with pytest.raises(worker.Stopped):
        ex.scroll("Pane:Contenu", "down", 5)
    assert auto.set_to == []


# --- 4. HWND de contenu : sa fenêtre de premier niveau -----------------------------------------------------
def test_child_hwnd_resolves_to_its_top_level_window(monkeypatch):
    monkeypatch.setattr(session.w, "IsWindow", lambda hwnd: True)
    monkeypatch.setattr(session.w, "root", lambda hwnd: 4000 if hwnd == 4001 else hwnd)
    s = session.Session()
    assert s._resolve_window(4001) == 4000 and s.target == 4000
    assert s._resolve_window("4000") == 4000


def test_set_scroll_failure_after_the_first_step_is_never_replayed(monkeypatch):
    auto = XamlScrollAuto()
    ex, pane = _settings_like(monkeypatch, auto)

    def refuse(element, h, v):
        raise uia.UIAError("SetScrollPercent refusé")

    monkeypatch.setattr(auto, "set_scroll_percent", refuse)
    with pytest.raises(Unconfirmed, match="first step applied"):
        ex.scroll("Pane:Contenu", "down", 5)
    assert auto.calls == [("scroll", pane.element)]  # ni petits pas ni molette par-dessus


def test_unmeasurable_first_step_is_unconfirmed(monkeypatch):
    auto = XamlScrollAuto()
    ex, pane = _settings_like(monkeypatch, auto)
    ticks = itertools.count()
    monkeypatch.setattr(auto, "scroll_percent", lambda element: (-1.0, 10.0 + next(ticks)))  # toujours en mouvement
    with pytest.raises(Unconfirmed, match="could not be measured"):
        ex.scroll("Pane:Contenu", "down", 5)
    assert auto.calls == [("scroll", pane.element)] and auto.set_to == []


def test_content_growing_during_the_measured_step_is_not_extrapolated(monkeypatch):
    auto = XamlScrollAuto(at=95.0)
    ex, pane = _settings_like(monkeypatch, auto)
    original = auto.scroll

    def grows(element, h, v):
        original(element, h, v)
        auto.view_size, auto.position = 25.0, 48.3  # l'étendue double : le pourcentage baisse en descendant

    monkeypatch.setattr(auto, "scroll", grows)
    with pytest.raises(Unconfirmed, match="changed size"):
        ex.scroll("Pane:Contenu", "down", 5)
    assert auto.set_to == []


def test_step_measured_the_wrong_way_is_not_extrapolated(monkeypatch):
    auto = XamlScrollAuto(small=-1.6)  # le pourcentage recule alors que le pas descend
    ex, _ = _settings_like(monkeypatch, auto)
    with pytest.raises(Unconfirmed):
        ex.scroll("Pane:Contenu", "down", 5)
    assert auto.set_to == []


def test_stop_while_reading_the_frame_sends_no_step(monkeypatch):
    from nexus_windows import act_pointer

    auto = XamlScrollAuto()
    ex, _ = _settings_like(monkeypatch, auto)
    read = {"frame": False}
    original = auto.scroll_view_size

    def frame(element):
        read["frame"] = True
        return original(element)

    monkeypatch.setattr(auto, "scroll_view_size", frame)

    def check() -> None:
        if read["frame"]:
            raise worker.Stopped("STOP")

    monkeypatch.setattr(act_pointer.worker, "check_cancelled", check)
    with pytest.raises(worker.Stopped):
        ex.scroll("Pane:Contenu", "down", 5)
    assert auto.calls == [] and auto.set_to == []


def test_late_first_step_in_the_middle_is_not_taken_for_the_end(monkeypatch):
    auto = XamlScrollAuto(small=0.0)  # pas publié après la mesure : position inchangée au milieu
    ex, _ = _settings_like(monkeypatch, auto)
    with pytest.raises(Unconfirmed, match="unchanged"):
        ex.scroll("Pane:Contenu", "down", 5)
    assert auto.set_to == []


# --- 2 bis. Clic droit : la cible sélectionnée seule avant le menu (vrai défaut 2 du banc) -------------------
class ContextMenuAuto(FakeAuto):
    def show_context_menu(self, element: FakeEl) -> None:
        self.calls.append(("context_menu", element))


def _explorer_items(monkeypatch):
    from nexus_windows import act_uia

    monkeypatch.setattr(act_uia.snapshot, "popups_of", lambda hwnd: [])
    folder = node("ListItem", "Dossier renommé", has_selection_item=True, selected=True, framework="DirectUI")
    note = node("ListItem", "note", has_selection_item=True, selected=False, framework="DirectUI")
    return folder, note


def test_right_click_selects_an_unselected_item_alone_before_its_menu(monkeypatch):
    folder, note = _explorer_items(monkeypatch)
    auto = ContextMenuAuto()
    ex = executor(make_view(folder, note), auto)
    ex.click("ListItem:note", button="right")
    assert auto.calls == [("select", note.element), ("context_menu", note.element)]


def test_right_click_on_an_already_selected_item_keeps_the_selection(monkeypatch):
    folder, note = _explorer_items(monkeypatch)
    note.element.live["selected"] = True
    auto = ContextMenuAuto()
    ex = executor(make_view(folder, note), auto)
    ex.click("ListItem:note", button="right")
    assert auto.calls == [("context_menu", note.element)]


def test_right_click_without_selection_read_back_opens_no_menu(monkeypatch):
    folder, note = _explorer_items(monkeypatch)

    class NoSelect(ContextMenuAuto):
        def select(self, element: FakeEl) -> None:
            self.calls.append(("select", element))  # envoyé, jamais relu sélectionné

    from nexus_windows import act_uia

    monkeypatch.setattr(act_uia.time, "sleep", lambda s: None)
    auto = NoSelect()
    ex = executor(make_view(folder, note), auto)
    with pytest.raises(ActionError, match="not read back as selected"):
        ex.click("ListItem:note", button="right")
    assert ("context_menu", note.element) not in auto.calls


def test_right_click_on_a_name_cell_targets_its_row(monkeypatch):
    from nexus_windows import act_uia

    monkeypatch.setattr(act_uia.snapshot, "popups_of", lambda hwnd: [])
    row, cell = _row_with_name_cell("note")
    auto = ContextMenuAuto()
    ex = executor(make_view(row, cell), auto)
    ex.click("Edit:Nom", button="right")
    assert auto.calls == [("select", row.element), ("context_menu", row.element)]


def test_right_click_with_unpublished_selection_state_changes_nothing(monkeypatch):
    folder, note = _explorer_items(monkeypatch)
    del note.element.live["selected"]  # propriété omise : ni vrai ni faux
    auto = ContextMenuAuto()
    ex = executor(make_view(folder, note), auto)
    with pytest.raises(ActionError, match="not published"):
        ex.click("ListItem:note", button="right")
    assert auto.calls == []
