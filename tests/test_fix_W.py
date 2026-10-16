"""Banc réel du 16/10 (NexusHarness/shared/bench-2026-10-16.md), liste déroulante « Formule » ouverte :

1. WinForms : `expand` refusé (pas de motif ExpandCollapse) → CB_SHOWDROPDOWN relu par CB_GETDROPPEDSTATE ;
2. liste ouverte : choisir puis fermer par CB_SHOWDROPDOWN FALSE ANNULE le choix (Windows rétablit la sélection
   d'avant l'ouverture, mesuré) : l'application recevait une option vide sous un « ~ » → fermer d'abord, puis
   CB_SETCURSEL, CBN_SELCHANGE, CBN_SELENDOK envoyés, relecture (✗ si vide ou contraire) ;
3. liste ouverte, l'arbre UIA de la fenêtre enchaînait sur toutes les fenêtres du bureau (11-20 s puis échec, « aucun
   élément Formule » en 22 s) → la liste déroulante ouverte reste une feuille, jamais une fenêtre d'un autre processus ;
4. WPF : la liste restait ouverte après le choix → refermée (et option publiée deux fois : une seule cible).
Sans fenêtre réelle ; preuve réelle : essai sur les formulaires de NexusAGY/tests/desktop_fixtures.py (CHANGELOG).
"""

from __future__ import annotations

import ctypes

import pytest
from test_contract import HWND, FakeAuto, executor, make_view, node
from test_fix_A import FoldingAuto, clock, link  # noqa: F401  (clock : fixture)

from nexus_windows import actions, methods, snapshot, uia
from nexus_windows import win32 as w

COMBO, DROP, CHROME = 5102, 5103, 5104
CB_ERR = 2**64 - 1  # CB_ERR (-1) relu non signé par SendMessageTimeout
WINFORMS_COMBO = "WindowsForms10.COMBOBOX.app.0.3df5b40_r8_ad1"


class Combo:
    """Liste déroulante DropDownList simulée comme Windows (mesuré sur WinForms le 16/10) : CB_SHOWDROPDOWN FALSE est
    une annulation (sélection d'avant l'ouverture rétablie) ; CBN_SELENDOK valide le choix courant."""

    def __init__(self, items=("Alpha", "Bravo", "Charlie"), dropped=0, opens=True, app_resets=False) -> None:
        self.items = list(items)
        self.sel = self.committed = -1
        self.dropped = dropped
        self.opens = opens  # False : la liste ne s'ouvre jamais (application qui l'ignore)
        self.app_resets = app_resets  # True : l'application remet la sélection à vide à CBN_SELCHANGE
        self.log: list[str] = []

    def send(self, hwnd, msg, wparam=0, lparam=0, timeout_ms=1000):
        items = self.items
        if msg == methods.CB_GETCOUNT:
            return len(items)
        if msg == methods.CB_GETLBTEXTLEN:
            return len(items[wparam]) if 0 <= wparam < len(items) else CB_ERR
        if msg == methods.CB_GETLBTEXT:
            if not 0 <= wparam < len(items):
                return CB_ERR
            text = ctypes.create_unicode_buffer(items[wparam])
            ctypes.memmove(lparam, text, ctypes.sizeof(text))
            return len(items[wparam])
        if msg == methods.CB_SETCURSEL:
            self.log.append(f"SETCURSEL {wparam}")
            self.sel = wparam
            return wparam
        if msg == methods.CB_GETCURSEL:
            return CB_ERR if self.sel < 0 else self.sel
        if msg == methods.CB_GETDROPPEDSTATE:
            return self.dropped
        if msg == methods.CB_SHOWDROPDOWN:
            self.log.append(f"SHOWDROPDOWN {wparam}")
            if self.dropped and not wparam:
                self.sel = self.committed  # CBN_SELENDCANCEL puis CBN_CLOSEUP
                self.log.append("SELENDCANCEL")
            if wparam and not self.dropped and self.opens:
                self.committed = self.sel
                self.dropped = 1
            elif not wparam:
                self.dropped = 0
            return 1
        return 0

    def notify(self, control, code):
        name = {methods.CBN_SELCHANGE: "SELCHANGE", methods.CBN_SELENDOK: "SELENDOK"}.get(code, str(code))
        self.log.append(name)
        if code == methods.CBN_SELCHANGE and self.app_resets:
            self.sel = -1
        if code == methods.CBN_SELENDOK:
            self.committed = self.sel


@pytest.fixture
def combo(monkeypatch):
    fake = Combo()
    monkeypatch.setattr(methods.w, "send_message", fake.send)
    monkeypatch.setattr(
        methods.w, "class_name", lambda h: WINFORMS_COMBO if h == COMBO else "ComboLBox" if h == DROP else ""
    )
    monkeypatch.setattr(methods.w, "style", lambda h: 0x3)  # CBS_DROPDOWNLIST
    monkeypatch.setattr(methods.w, "IsWindowEnabled", lambda h: True)
    monkeypatch.setattr(methods, "_send_notify", fake.notify)
    monkeypatch.setattr(methods, "_notify_parent", fake.notify)
    monkeypatch.setattr(methods.time, "sleep", lambda s: None)
    return fake


def winforms_combo_node(**props) -> uia.Node:
    base = {"hwnd": COMBO, "framework": "WinForm", "has_expand": False, "expand": 3, "value": ""}
    return node("ComboBox", "Formule", **{**base, **props})


def winforms_view(*nodes: uia.Node, windows=None) -> snapshot.View:
    view = make_view(*nodes)
    view.framework = view.info.framework = "WinForms"
    view.windows = windows or [HWND]
    return view


# --- 1. expand d'une liste déroulante WinForms : CB_SHOWDROPDOWN relu ------------------------------------------------
def test_expand_winforms_combo_by_message_and_read_back(combo):
    auto = FakeAuto()
    ex = executor(winforms_view(winforms_combo_node()), auto)
    outcome = ex.expand("Formule", True)
    assert outcome.ok and outcome.verified and outcome.method == "CB_SHOWDROPDOWN"
    assert combo.dropped == 1 and combo.log == ["SHOWDROPDOWN 1"] and auto.calls == []  # ni UIA ni souris
    again = ex.expand("Formule", True)
    assert again.ok and "déjà déplié" in again.line and combo.log == ["SHOWDROPDOWN 1"]  # rien renvoyé
    closed = ex.expand("Formule", False)
    assert closed.ok and closed.verified and combo.dropped == 0


def test_expand_that_does_not_open_is_a_failure_never_a_tilde(combo):
    combo.opens = False
    outcome = executor(winforms_view(winforms_combo_node()), FakeAuto()).expand("Formule", True)
    assert not outcome.ok and outcome.verified is False and "ÉCHEC À LA RELECTURE" in outcome.line


def test_simple_combo_has_nothing_to_expand(combo, monkeypatch):
    monkeypatch.setattr(methods.w, "style", lambda h: methods.CBS_SIMPLE)
    with pytest.raises(actions.ActionError, match="CBS_SIMPLE"):
        executor(winforms_view(winforms_combo_node()), FakeAuto()).expand("Formule", True)


def test_click_on_winforms_combo_toggles_its_list(combo):
    ex = executor(winforms_view(winforms_combo_node()), FakeAuto())
    assert ex.click_element(ex.view.elements[0]).method == "CB_SHOWDROPDOWN" and combo.dropped == 1
    assert ex.click_element(ex.view.elements[0]).method == "CB_SHOWDROPDOWN" and combo.dropped == 0


# --- 2. Choix dans la liste ouverte : fermer d'abord (annulation), puis choisir et valider, relu -----------------------
def test_open_list_choice_survives_the_close_and_reaches_the_application(combo):
    combo.dropped = 1
    assert methods.win32_combo_select(COMBO, "Bravo") == "relu ✓"
    assert combo.sel == 1 and combo.committed == 1 and combo.dropped == 0
    # Ordre : fermeture (annulation de Windows) AVANT le choix, puis notifications d'un vrai choix.
    assert combo.log == ["SHOWDROPDOWN 0", "SELENDCANCEL", "SETCURSEL 1", "SELCHANGE", "SELENDOK"]


def test_the_old_order_loses_the_choice():
    """Ce que faisait 0.7.0 (CB_SETCURSEL liste ouverte puis CB_SHOWDROPDOWN FALSE) : sélection perdue."""
    fake = Combo(dropped=1)
    fake.send(COMBO, methods.CB_SETCURSEL, 1)
    fake.notify(COMBO, methods.CBN_SELCHANGE)
    fake.send(COMBO, methods.CB_SHOWDROPDOWN, 0)
    assert fake.send(COMBO, methods.CB_GETCURSEL) == CB_ERR


def test_application_value_read_back_empty_is_a_failure(combo):
    combo.dropped, combo.app_resets = 1, True
    with pytest.raises(methods.ReadBackFailed, match="vide"):
        methods.win32_combo_select(COMBO, "Bravo")


def test_click_on_item_of_open_combolbox_selects_owner_and_reads_back(combo, monkeypatch):
    combo.dropped = 1
    items = [
        node("ListItem", n, framework="", legacy_action="Double clic", rect=(0, 20 * i, 200, 20))
        for i, n in enumerate(combo.items)
    ]
    link(node("List", "", hwnd=DROP, framework="Win32", rect=(0, 0, 200, 60)), *items)
    monkeypatch.setattr(methods.w, "enum_children", lambda top: [COMBO] if top == HWND else [])
    monkeypatch.setattr(methods, "combo_list_of", lambda c: DROP if c == COMBO else 0)
    auto = FakeAuto()
    view = winforms_view(*items, windows=[HWND, DROP])
    for e in view.elements:
        e.window = DROP
    ex = executor(view, auto)
    ex._windows = {HWND, DROP}
    outcome = ex.click_element(ex._shown_id(view.elements[1]))
    assert outcome.ok and outcome.verified and outcome.method == "CB_SETCURSEL"
    assert combo.sel == 1 and combo.dropped == 0 and auto.calls == []  # ni Invoke MSAA (vrai clic) ni souris


def test_click_on_open_item_that_the_application_drops_is_a_cross(combo, monkeypatch):
    combo.dropped, combo.app_resets = 1, True
    items = [
        node("ListItem", n, framework="", legacy_action="Double clic", rect=(0, 20 * i, 200, 20))
        for i, n in enumerate(combo.items)
    ]
    link(node("List", "", hwnd=DROP, framework="Win32", rect=(0, 0, 200, 60)), *items)
    monkeypatch.setattr(methods.w, "enum_children", lambda top: [COMBO] if top == HWND else [])
    monkeypatch.setattr(methods, "combo_list_of", lambda c: DROP if c == COMBO else 0)
    view = winforms_view(*items, windows=[HWND, DROP])
    ex = executor(view, FakeAuto())
    ex._windows = {HWND, DROP}
    posted: list = []
    monkeypatch.setattr(actions.methods, "post_click", lambda *a, **k: posted.append(a))
    outcome = ex.click_element(ex._shown_id(view.elements[1]))
    assert not outcome.ok and outcome.verified is False and "ÉCHEC À LA RELECTURE" in outcome.line
    assert posted == []  # aucun clic par messages rejoué par-dessus


# --- 3. Liste ouverte : arbre UIA borné (jamais les fenêtres du bureau) -----------------------------------------------
class ComEl:
    """Élément UIA en cache simulé : propriétés par identifiant, enfants en cache, relecture comptée."""

    reads: list[str] = []

    def __init__(self, name: str, hwnd: int = 0, kids: list[ComEl] | None = None, role: str = "Pane") -> None:
        self.name, self.hwnd, self.kids, self.role = name, hwnd, kids or [], role

    def GetCachedPropertyValue(self, pid):
        values = {
            uia.PROPS["name"]: self.name,
            uia.PROPS["hwnd"]: self.hwnd,
            uia.PROPS["control_type"]: {v: k for k, v in uia.CONTROL_TYPES.items()}[self.role],
        }
        return values.get(pid)

    def GetCachedChildren(self):
        ComEl.reads.append(self.name)
        if not self.kids:
            return None
        kids = self.kids
        return type("Array", (), {"Length": len(kids), "GetElement": lambda _self, i: kids[i]})()

    def BuildUpdatedCache(self, cache):
        return self


def bare_automation(root: ComEl) -> uia.Automation:
    auto = uia.Automation.__new__(uia.Automation)
    auto._cache = "subtree"
    auto._cache_kids = "children"
    auto.uia = type("UIA", (), {"ElementFromHandleBuildCache": lambda _self, hwnd, cache: root})()
    return auto


def desktop_leak() -> ComEl:
    """Fenêtre WinForms, liste ouverte : sous la liste déroulante, la ComboLBox puis… Chrome (autre processus)."""
    chrome = ComEl("Accueil - Google Chrome", CHROME, [ComEl("onglet privé", 0, [], "TabItem")], "Window")
    combo = ComEl("Formule", COMBO, [ComEl("", DROP, [], "List"), chrome], "ComboBox")
    panel = ComEl("panneau", 5105, [combo, ComEl("Valider", 5106, [], "Button")])
    return ComEl("NexusAGY banc WinForms", HWND, [panel, ComEl("Nom", 5107, [], "Edit")], "Window")


def test_tree_with_open_combo_never_descends_into_it(monkeypatch):
    monkeypatch.setattr(uia.w, "dropped_combos", lambda top: {COMBO})
    monkeypatch.setattr(uia.w, "IsChild", lambda parent, child: parent == 5105 and child == COMBO)
    monkeypatch.setattr(uia.w, "root", lambda h: h if h in (HWND, CHROME, DROP) else HWND)
    monkeypatch.setattr(uia.w, "window_pid", lambda h: 99 if h == CHROME else 7)
    ComEl.reads = []
    tree = bare_automation(desktop_leak()).tree(HWND)
    names = [n.name for n in tree.walk()]
    assert "Formule" in names and "Valider" in names and "Nom" in names
    assert "Accueil - Google Chrome" not in names and "onglet privé" not in names
    formule = next(n for n in tree.walk() if n.name == "Formule")
    assert formule.children == [] and "Formule" not in ComEl.reads  # ses enfants ne sont même pas demandés


def test_tree_never_lists_a_top_level_window_of_another_process(monkeypatch):
    monkeypatch.setattr(uia.w, "dropped_combos", lambda top: set())  # navigation qui fuit sans liste ouverte
    monkeypatch.setattr(uia.w, "root", lambda h: h if h in (HWND, CHROME, DROP) else HWND)
    monkeypatch.setattr(uia.w, "window_pid", lambda h: 99 if h == CHROME else 7)
    names = [n.name for n in bare_automation(desktop_leak()).tree(HWND).walk()]
    assert "Accueil - Google Chrome" not in names and "onglet privé" not in names
    assert "" in names  # la ComboLBox, même processus, reste


def test_dropped_combos_reads_only_open_combo_boxes(monkeypatch):
    classes = {1: WINFORMS_COMBO, 2: "ComboBox", 3: "Edit", 4: "ComboBox"}
    monkeypatch.setattr(w, "enum_children", lambda top: [1, 2, 3, 4])
    monkeypatch.setattr(w, "class_name", lambda h: classes[h])
    monkeypatch.setattr(w, "IsWindowVisible", lambda h: h != 4)
    monkeypatch.setattr(w, "send_message", lambda h, msg, *a, **k: 1 if h == 1 and msg == w.CB_GETDROPPEDSTATE else 0)
    assert w.dropped_combos(HWND) == {1}


def test_unreadable_window_gives_a_clear_message():
    view = make_view()
    view.unreadable = True
    with pytest.raises(snapshot.TargetError, match="n'a pas pu être lu"):
        snapshot.pick(view, "Formule")


# --- 4. WPF : liste refermée après le choix, option publiée deux fois = une seule cible --------------------------------
def wpf_open_combo(auto_expand: int = 1):
    combo = node("ComboBox", "Formule", has_expand=True, expand=auto_expand, rect=(354, 402, 250, 27))
    items = [
        node("ListItem", n, has_selection_item=True, rect=(355, 430 + 25 * i, 248, 25))
        for i, n in enumerate(("Alpha", "Bravo"))
    ]
    link(combo, *items)
    return combo, items


@pytest.mark.usefixtures("clock")
def test_select_in_already_open_wpf_combo_closes_it():
    combo, items = wpf_open_combo()
    auto = FoldingAuto()
    outcome = executor(make_view(combo, *items), auto).select("Formule", "Bravo")
    assert outcome.ok and outcome.verified
    assert ("select", items[1].element) in auto.calls and ("collapse", combo.element) in auto.calls
    assert combo.element.live["expand"] == 0


@pytest.mark.usefixtures("clock")
def test_click_on_option_of_open_wpf_combo_closes_it_and_reads_back():
    combo, items = wpf_open_combo()
    auto = FoldingAuto()
    ex = executor(make_view(combo, *items), auto)
    outcome = ex.click_element(ex.view.elements[2])
    assert outcome.ok and outcome.verified and "liste refermée" in outcome.line
    assert auto.calls[-1] == ("collapse", combo.element)


def test_wpf_popup_twin_of_an_open_option_is_the_same_target():
    combo, items = wpf_open_combo()
    popup = 5200
    twin = node("ListItem", "Bravo", has_selection_item=True, rect=(355, 433, 248, 25))  # surgissante qui glisse
    view = make_view(combo, *items)
    view.elements.append(snapshot.Element(len(view.elements) + 1, twin, 0, popup))
    view.windows = [HWND, popup]
    element, _ = snapshot.pick(view, "Bravo")
    assert element.node is items[1]


def test_two_distinct_homonym_options_stay_ambiguous():
    combo = node("ComboBox", "Formule", has_expand=True, expand=1)
    items = [node("ListItem", "Bravo", rect=(0, 25 * i, 100, 25)) for i in range(2)]
    link(combo, *items)
    with pytest.raises(snapshot.AmbiguousTarget):
        snapshot.pick(make_view(combo, *items), "Bravo")


@pytest.mark.usefixtures("clock")
def test_closed_combo_is_not_collapsed_by_select():
    combo, items = wpf_open_combo(auto_expand=0)
    auto = FoldingAuto()
    executor(make_view(combo, *items), auto).select("Formule", "Bravo")
    assert not any(call[0] == "collapse" for call in auto.calls)
