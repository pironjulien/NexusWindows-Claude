"""Contre-vérification recheck4, lot A : contrat de cible, de coordonnées et de statut (sans fenêtre réelle).

Un test (au moins) par constat corrigé : pont MSAA et ComboLBox (1), ordre des surgissantes (2), fenêtre visée par
les messages (3), cadre unique des coordonnées (4), DevTools sans frappes (12), contenu web recouvert (16), lecture
incomplète (17), replis des listes et menus (20), défilement sans cible (22), code mort retiré (26) et instantané
construit une seule fois au premier nom d'un act.
"""

from __future__ import annotations

import ctypes
import io
import re
from typing import cast

import pytest
from PIL import Image
from test_contract import HWND, ROLE_IDS, FakeAuto, FakeEl, as_auto, as_call, executor, make_view, node, under

from nexus_windows import (
    act_base,
    act_chromium,
    act_pointer,
    act_uia,
    act_wait,
    act_win32,
    actions,
    cdp,
    methods,
    server,
    snapshot,
    uia,
    worker,
)
from nexus_windows import win32 as w
from nexus_windows.session import Session

DROP, COMBO, DIALOG, TOP, POPUP = 5001, 5002, 5003, 5004, 5005


def link(parent: uia.Node, *children: uia.Node) -> uia.Node:
    for child in children:
        child.parent = parent
        parent.children.append(child)
    return parent


def win32_view(*elements: snapshot.Element, windows: list[int] | None = None) -> snapshot.View:
    view = make_view()
    view.elements = list(elements)
    view.windows = windows or [HWND]
    view.framework = view.info.framework = "Win32"
    return view


def chains(ex: actions.Executor) -> list[list[str]]:
    """Chaînes de techniques que l'exécuteur ESSAIERAIT (rien n'est exécuté)."""
    seen: list[list[str]] = []

    def record(chain, what, real):
        seen.append([name for name, _ in chain])
        return actions.Outcome(True, what, "(non exécuté)")

    ex._try_or_foreground = record
    return seen


def changing_screen(ex: actions.Executor) -> None:
    """L'empreinte visuelle change à chaque lecture : une action par messages « a un effet visible »."""
    counter = iter(range(1_000_000))
    ex._fingerprint = lambda: (next(counter),)


class FoldingAuto(FakeAuto):
    def expand(self, element: FakeEl) -> None:
        self.calls.append(("expand", element))
        element.live["expand"] = 1

    def collapse(self, element: FakeEl) -> None:
        self.calls.append(("collapse", element))
        element.live["expand"] = 0


class Clock:
    """Horloge simulée pour les attentes d'actions (1,5 s de recherche d'option sans attendre vraiment)."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def perf_counter(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# --- 1. Pont MSAA : jamais UIA Invoke (vrai double-clic) ------------------------------------------------------------
def msaa_item(name: str, **props) -> uia.Node:
    base = {"framework": "", "legacy_action": "Double clic", "has_invoke": True, "has_selection_item": True}
    return node("ListItem", name, **{**base, **props})


def test_msaa_list_item_without_parent_is_never_invoked(monkeypatch):
    item = msaa_item("Bleu", rect=(10, 10, 80, 16))
    auto = FakeAuto()
    ex = executor(win32_view(snapshot.Element(1, item, 0, HWND)), auto)
    ex._windows = {HWND}
    changing_screen(ex)
    posted: list[tuple] = []
    monkeypatch.setattr(
        actions.methods, "post_click", lambda top, x, y, button="left", count=1, held=False: posted.append((top, count))
    )
    outcome = ex.click_element(ex._shown_id(snapshot.Element(1, item, 0, HWND)))
    assert outcome.ok and outcome.method == "click by messages" and posted == [(HWND, 1)]
    outcome = ex.click_element(ex._shown_id(snapshot.Element(1, item, 0, HWND)), count=2)
    assert outcome.method == "double-click by messages" and posted[-1] == (HWND, 2)
    assert auto.calls == []  # ni invoke ni legacy : aucune vraie souris


def test_msaa_item_in_open_dropdown_selects_owner_combo_by_rank(monkeypatch):
    items = [msaa_item(n, rect=(0, 20 * i, 200, 20)) for i, n in enumerate(("Bleu", "Vert", "Bleu"))]
    dropped = link(node("List", "", hwnd=DROP, framework="Win32", rect=(0, 0, 200, 60)), *items)
    assert dropped.children
    classes = {DROP: "ComboLBox", COMBO: "ComboBox", HWND: "Main"}
    monkeypatch.setattr(methods.w, "class_name", lambda h: classes.get(h, ""))
    monkeypatch.setattr(methods.w, "enum_children", lambda top: [COMBO] if top == HWND else [])
    monkeypatch.setattr(methods, "combo_list_of", lambda combo: DROP if combo == COMBO else 0)
    chosen: list[tuple] = []
    monkeypatch.setattr(methods, "win32_combo_select_index", lambda c, i, n: chosen.append((c, i, n)) or "read back ✓")
    auto = FakeAuto()
    view = win32_view(*[snapshot.Element(i + 1, n, 0, DROP) for i, n in enumerate(items)], windows=[HWND, DROP])
    ex = executor(view, auto)
    ex._windows = {HWND, DROP}
    outcome = ex.click_element(ex._shown_id(view.elements[2]))  # le second « Bleu » : homonyme, distingué par son rang
    assert outcome.ok and outcome.verified and outcome.method == "CB_SETCURSEL"
    assert chosen == [(COMBO, 2, "Bleu")] and auto.calls == []


def test_winforms_button_and_toolstrip_keep_uia_invoke(monkeypatch):
    button_hwnd = 6001
    monkeypatch.setattr(methods.w, "class_name", lambda h: "WindowsForms10.BUTTON.app.0.1" if h == button_hwnd else "")
    monkeypatch.setattr(actions.w, "style", lambda h: methods.BS_PUSHBUTTON)
    button = node("Button", "OK", hwnd=button_hwnd, framework="WinForm", has_invoke=True, legacy_action="Appuyer")
    tool = node("Button", "Enregistrer", framework="WinForm", has_invoke=True, legacy_action="Appuyer")
    view = win32_view(snapshot.Element(1, button, 0, HWND), snapshot.Element(2, tool, 0, HWND))
    view.framework = "WinForms"
    ex = executor(view, FakeAuto())
    ex._windows = {HWND}
    seen = chains(ex)
    ex.click_element(view.elements[0])
    ex.click_element(view.elements[1])
    assert seen[0][:2] == ["BN_CLICKED", "UIA Invoke"] and seen[1][0] == "UIA Invoke"


def test_point_on_msaa_item_is_found_with_its_ancestors(monkeypatch):
    bleu = msaa_item("Bleu", rect=(0, 40, 200, 20), runtime_id=(42, 3))
    dropped = link(node("List", "", hwnd=DROP, rect=(0, 0, 200, 60), runtime_id=(42, 1)), bleu)
    view = win32_view(snapshot.Element(1, bleu, 0, DROP), windows=[HWND, DROP])
    auto = FakeAuto()
    # ElementFromPoint : le même élément, sans parent, rectangle logique (application non adaptée au DPI)
    auto.at_point = FakeEl({**bleu.props, "rect": (0, 0, 10, 10)})
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: DROP)
    ex = executor(view, auto)
    ex._windows = {HWND, DROP}
    ex.acted = False
    element = under(ex, 100, 50)
    assert element.node is bleu and element.node.parent is dropped and element.window == DROP


def test_point_on_text_climbs_to_the_wanted_role(monkeypatch):
    label = node("Text", "Valider", rect=(12, 12, 40, 10), runtime_id=(7, 2))
    button = link(node("Button", "Valider", has_invoke=True, rect=(10, 10, 60, 20), runtime_id=(7, 1)), label)
    auto = FakeAuto()
    auto.at_point = FakeEl(dict(label.props))
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: HWND)
    ex = executor(make_view(button), auto)
    ex._windows = {HWND}
    ex.acted = False
    assert under(ex, 20, 15).node is button


@pytest.fixture
def fake_combo(monkeypatch):
    """Liste déroulante Win32 simulée (messages CB_*), ouverte, avec deux homonymes. Comme Windows (mesuré) : la
    fermer par CB_SHOWDROPDOWN FALSE est une annulation, la sélection d'avant l'ouverture (« committed ») revient."""
    state = {"items": ["Bleu", "Vert", "Bleu"], "sel": -1, "committed": -1, "dropped": 1, "notified": []}

    def send(hwnd, msg, wparam=0, lparam=0, timeout_ms=1000):
        items = state["items"]
        if msg == methods.CB_GETCOUNT:
            return len(items)
        if msg == methods.CB_GETLBTEXTLEN:
            return len(items[wparam])
        if msg == methods.CB_GETLBTEXT:
            text = ctypes.create_unicode_buffer(items[wparam])
            ctypes.memmove(lparam, text, ctypes.sizeof(text))
            return len(items[wparam])
        if msg == methods.CB_SETCURSEL:
            state["sel"] = wparam
            return wparam
        if msg == methods.CB_GETCURSEL:
            return state["sel"]
        if msg == methods.CB_GETDROPPEDSTATE:
            return state["dropped"]
        if msg == methods.CB_SHOWDROPDOWN:
            if state["dropped"] and not wparam:
                state["sel"] = state["committed"]  # CBN_SELENDCANCEL : sélection d'avant l'ouverture rétablie
            if wparam and not state["dropped"]:
                state["committed"] = state["sel"]
            state["dropped"] = wparam
            return 1
        return 0

    def notify(control, code):
        state["notified"].append(code)
        if code == methods.CBN_SELENDOK:
            state["committed"] = state["sel"]

    monkeypatch.setattr(methods.w, "send_message", send)
    monkeypatch.setattr(methods.w, "class_name", lambda h: "ComboBox")
    monkeypatch.setattr(methods, "_notify_parent", notify)
    monkeypatch.setattr(methods, "_send_notify", notify)
    return state


def test_combo_select_by_rank_checks_label_and_closes_the_list(fake_combo):
    assert methods.win32_combo_select_index(COMBO, 2, "Bleu") == "read back ✓"
    assert fake_combo["sel"] == 2 and fake_combo["dropped"] == 0
    assert fake_combo["notified"] == [methods.CBN_SELCHANGE, methods.CBN_SELENDOK]
    fake_combo.update(sel=-1, dropped=1, notified=[])
    with pytest.raises(methods.MethodError, match="call look again"):
        methods.win32_combo_select_index(COMBO, 1, "Bleu")  # le rang ne désigne plus l'élément vu
    assert fake_combo["sel"] == -1 and fake_combo["dropped"] == 1 and fake_combo["notified"] == []


@pytest.mark.parametrize(
    ("style", "selected_after"),
    [(0, {2}), (methods.LBS_EXTENDEDSEL, {2}), (methods.LBS_MULTIPLESEL, {0, 2})],
)
def test_listbox_select_by_rank_like_a_click(monkeypatch, style, selected_after):
    state = {"items": ["Alpha", "Bravo", "Alpha"], "cur": -1, "sel": {0}, "notified": 0}

    def send(hwnd, msg, wparam=0, lparam=0, timeout_ms=1000):
        items = state["items"]
        if msg == methods.LB_GETCOUNT:
            return len(items)
        if msg == methods.LB_GETTEXTLEN:
            return len(items[wparam])
        if msg == methods.LB_GETTEXT:
            text = ctypes.create_unicode_buffer(items[wparam])
            ctypes.memmove(lparam, text, ctypes.sizeof(text))
            return len(items[wparam])
        if msg == methods.LB_SETCURSEL:
            state["cur"], state["sel"] = wparam, {wparam}
            return wparam
        if msg == methods.LB_GETCURSEL:
            return state["cur"]
        if msg == methods.LB_SETSEL:
            targets = set(range(len(items))) if lparam == -1 else {lparam}
            state["sel"] = state["sel"] | targets if wparam else state["sel"] - targets
            return 0
        if msg == methods.LB_GETSEL:
            return int(wparam in state["sel"])
        return 0

    monkeypatch.setattr(methods.w, "send_message", send)
    monkeypatch.setattr(methods.w, "class_name", lambda h: "ListBox")
    monkeypatch.setattr(methods.w, "style", lambda h: style)
    monkeypatch.setattr(methods, "_notify_parent", lambda control, code: state.update(notified=state["notified"] + 1))
    assert methods.win32_listbox_select_index(6, 2, "Alpha") == "read back ✓"
    assert state["sel"] == selected_after and state["notified"] == 1


# --- 2. Surgissantes du haut vers le bas -----------------------------------------------------------------------------
def overlapping(top_role: str, top_name: str):
    main = link(node("Window", "Appli", rect=(0, 0, 400, 300)), node("Pane", "Fond", rect=(0, 0, 400, 300)))
    button = node("Button", "Supprimer", has_invoke=True, rect=(100, 100, 120, 30))
    dialog = link(node("Window", "Options", rect=(50, 50, 300, 200)), button)
    upper = node(top_role, top_name, has_invoke=True, rect=(100, 100, 150, 20))
    popup = link(node("Window", "Confirmer", rect=(90, 80, 170, 80)), upper)
    elements = [
        snapshot.Element(1, main.children[0], 0, HWND),
        snapshot.Element(2, button, 0, DIALOG),
        snapshot.Element(3, upper, 0, TOP),
    ]
    view = make_view()
    view.elements, view.windows = elements, [HWND, DIALOG, TOP]  # ordre de build_view : du bas vers le haut
    assert dialog.children and popup.children
    return view, button, upper


@pytest.mark.parametrize(("role", "name"), [("ListItem", "Bleu"), ("Button", "Oui")])
def test_point_shared_by_two_popups_hits_the_upper_one(monkeypatch, role, name):
    view, button, upper = overlapping(role, name)
    auto = FakeAuto()
    ex = executor(view, auto)
    ex._windows, ex.acted = {HWND, DIALOG, TOP}, False
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: 999)  # cible recouverte par une fenêtre de Julien
    assert under(ex, 110, 110).node is upper
    outcome = ex.click_point(110, 110)
    assert outcome.ok and auto.calls == [("invoke", upper.element)]  # rien sur le dialogue du dessous


def test_real_stacking_order_wins_over_snapshot_order(monkeypatch):
    view, button, upper = overlapping("Button", "Oui")
    monkeypatch.setattr(actions.w, "enum_windows", lambda: [DIALOG, TOP, HWND])  # le dialogue est passé devant
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: 999)
    ex = executor(view, FakeAuto())
    ex._windows, ex.acted = {HWND, DIALOG, TOP}, False
    assert under(ex, 110, 110).node is button


def test_upper_popup_without_wanted_role_blocks_what_is_below(monkeypatch):
    view, button, upper = overlapping("Button", "Oui")
    upper.props.update(control_type=ROLE_IDS["Text"], has_invoke=False)  # simple texte au point
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: 999)
    ex = executor(view, FakeAuto())
    ex._windows, ex.acted = {HWND, DIALOG, TOP}, False
    assert ex._element_under(110, 110) is None  # jamais le bouton du dialogue, dessous


# --- 3. Messages à la fenêtre réellement sous le point -----------------------------------------------------------
@pytest.fixture
def popup_over_main(monkeypatch):
    rects = {POPUP: w.Rect(100, 100, 300, 200), HWND: w.Rect(0, 0, 400, 300)}
    monkeypatch.setattr(actions.w, "enum_windows", lambda: [POPUP, HWND])
    monkeypatch.setattr(actions.w, "IsWindowVisible", lambda h: h in rects)
    monkeypatch.setattr(actions.w, "is_cloaked", lambda h: False)
    monkeypatch.setattr(actions.w, "frame_rect", lambda h: rects.get(h, w.Rect(0, 0, 0, 0)))
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: 999)
    monkeypatch.setattr(methods.w, "deepest_child_at", lambda top, x, y: top)
    monkeypatch.setattr(methods.w, "screen_to_client", lambda h, x, y: (x, y))
    targets: list[int] = []
    monkeypatch.setattr(methods, "post", lambda target, msg, wparam, lparam: targets.append(target))
    view = win32_view(windows=[HWND, POPUP])
    ex = executor(view, FakeAuto())
    ex._windows, ex.acted = {HWND, POPUP}, False
    changing_screen(ex)
    return ex, targets


def test_message_click_goes_to_the_popup_under_the_point(popup_over_main):
    ex, targets = popup_over_main
    outcome = ex.click_point(150, 150)
    assert outcome.ok and outcome.method == "click by messages"
    assert targets and set(targets) == {POPUP}  # la fenêtre principale, dessous, ne reçoit rien


def test_message_wheel_goes_to_the_popup_under_the_point(popup_over_main):
    ex, targets = popup_over_main
    ex.get_frame = lambda: snapshot.frame_for(w.Rect(0, 0, 400, 300), anchor=(0, 0), anchor_size=(400, 300))
    outcome = ex.scroll("150,150", "down", 1)
    assert outcome.method == "wheel by messages" and set(targets) == {POPUP}


# --- 4. Un seul cadre de coordonnées --------------------------------------------------------------------------------
# render sans UI Automation : session.view est simulé par la fixture.
NO_AUTO = as_auto(None)


@pytest.fixture
def shifted(monkeypatch):
    """Image montrée sans surgissante, puis un menu s'ouvre AU-DESSUS de la fenêtre (l'origine du cadre recule)."""
    session = Session()
    monkeypatch.setattr(server, "SESSION", session)
    rect = w.Rect(100, 400, 900, 700)
    info = snapshot.WindowInfo(HWND, "Appli", "a.exe", "c", rect, False, False, False, "WPF")
    supprimer = node("Button", "Supprimer", has_invoke=True, rect=(250, 420, 60, 20))
    exporter = node("MenuItem", "Exporter", has_invoke=True, rect=(150, 350, 250, 20))
    anchor = ((rect.left, rect.top), (rect.width, rect.height))
    before = snapshot.View(
        HWND, info, [HWND], [snapshot.Element(1, supprimer, 0, HWND)], snapshot.frame_for(rect, "normal", *anchor)
    )
    after = snapshot.View(
        HWND, info, [HWND, POPUP],
        [snapshot.Element(1, supprimer, 0, HWND), snapshot.Element(2, exporter, 0, POPUP)],
        snapshot.frame_for(w.Rect(100, 330, 900, 700), "normal", *anchor),
    )  # fmt: skip
    monkeypatch.setattr(session, "view", lambda auto, hwnd, rebuild=False, detail=None: after)
    monkeypatch.setattr(snapshot, "capture_view", lambda view: (Image.new("RGB", view.frame.size), []))
    monkeypatch.setattr(actions.w, "frame_rect", lambda h: rect)
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: 999)
    return session, before, after, supprimer


def test_list_only_uses_the_frame_of_the_last_image(shifted):
    session, before, after, supprimer = shifted
    session.mark_image(before)
    text = server.render(NO_AUTO, HWND, "elements")[0]
    assert "last image (800×300" in text
    listed = dict(re.findall(r'"(\w+)".*?(@-?\d+,-?\d+|\(outside the image\))', text))
    assert listed == {"Supprimer": "@180,30", "Exporter": "(outside the image)"}
    ex = executor(after, FakeAuto())
    ex.get_frame = lambda: session.frames.get(HWND)
    ex._windows, ex.acted = {HWND, POPUP}, False
    kind, point = ex.resolve(listed["Supprimer"][1:])
    assert kind == "point" and under(ex, *point).node is supprimer  # l'élément listé, pas un autre
    assert session.frames[HWND] is before.frame


def test_zoom_never_replaces_the_reference_frame(shifted):
    session, before, after, _ = shifted
    session.mark_image(before)
    out = server.render(NO_AUTO, HWND, "both", region=[0, 0, 100, 50])
    assert len(out) == 2 and session.frames[HWND] is before.frame and "zoom" in out[0]
    server.render(NO_AUTO, HWND, "both")  # image complète : elle devient la référence
    assert session.frames[HWND] is after.frame


def test_square_zoom_stays_under_the_pixel_budget(shifted):
    session, before, _, _ = shifted
    session.mark_image(before)
    out = server.render(NO_AUTO, HWND, "both", region=[0, 0, 300, 300])
    width, height = Image.open(io.BytesIO(out[1].data)).size
    assert width * height <= snapshot.MAX_PIXELS["normal"] and max(width, height) <= 2000  # ×4 : 1200×1200


def test_unchanged_image_is_not_resent_and_keeps_the_frame(shifted):
    session, _, after, _ = shifted
    first = server.render(NO_AUTO, HWND, "both")
    assert len(first) == 2 and session.frames[HWND] is after.frame
    second = server.render(NO_AUTO, HWND, "both")
    assert len(second) == 1 and "Image unchanged" in second[0]
    assert len(server.render(NO_AUTO, HWND, "image")) == 2  # show=image : toujours renvoyée


def test_list_only_without_any_image_memorizes_its_frame(shifted):
    session, _, after, _ = shifted
    server.render(NO_AUTO, HWND, "elements")
    assert session.frames[HWND] is after.frame


def test_list_only_after_a_resize_omits_coordinates(shifted):
    session, before, after, _ = shifted
    session.frames[HWND] = snapshot.frame_for(w.Rect(100, 400, 600, 700), "normal", (100, 400), (500, 300))
    text = server.render(NO_AUTO, HWND, "elements")[0]
    assert "omitted" in text and "changed size" in text and "@" not in text.split("data to read")[1]
    out = server.render(
        NO_AUTO, HWND, "elements", region=[0, 0, 10, 10]
    )  # zoom dans une image périmée : image complète
    assert len(out) == 2 and session.frames[HWND] is after.frame


# --- 12. DevTools : jamais de frappes ----------------------------------------------------------------------------------
class FakePage:
    """Page DevTools simulée : zone visible 400×300 px CSS ; elementFromPoint ne trouve rien hors de cette zone."""

    def __init__(self, field: FakeEl) -> None:
        self.field = field
        self.inserted: list[str] = []

    def evaluate(self, script: str):
        if "devicePixelRatio" in script:
            return 1
        point = re.search(r"elementFromPoint\((-?[\d.]+), (-?[\d.]+)\)", script)
        if point:
            x, y = float(point.group(1)), float(point.group(2))
            inside = 0 <= x < 400 and 0 <= y < 300
            return ("ok" if inside else "none") if "isContentEditable" in script else inside
        return False  # pas de mot de passe ; sélection : sans valeur de retour

    def insert_text(self, text: str) -> None:
        self.inserted.append(text)
        self.field.live["value"] = (self.field.live.get("value") or "") + text


def as_page(page: FakePage) -> cdp.Page:
    """Fausse page DevTools présentée au typage comme la vraie (evaluate et insert_text seulement)."""
    return cast(cdp.Page, page)


class ScrollingAuto(FakeAuto):
    def __init__(self, moves: bool) -> None:
        super().__init__()
        self.moves = moves

    def scroll_into_view(self, element: FakeEl) -> None:
        self.calls.append(("scroll_into_view", element))
        if self.moves:
            element.live["rect"] = (10, 50, 300, 100)


@pytest.mark.parametrize("moves", [True, False])
def test_devtools_write_scrolls_into_view_and_never_types(monkeypatch, moves):
    editor = node(
        "Edit", "Éditeur", framework="Chrome", aria_properties="multiline=true", rect=(10, -200, 300, 100)
    )  # au-dessus de la zone visible
    view = make_view(editor)
    view.framework = view.info.framework = "Chromium"
    page = FakePage(editor.element)
    auto = ScrollingAuto(moves)
    ex = executor(view, auto)
    ex.get_page = lambda: as_page(page)
    ex._web_viewport = lambda: w.Rect(0, 0, 400, 300)
    monkeypatch.setattr(actions.foreground, "brief_activation", lambda *a, **k: pytest.fail("activation brève"))
    if moves:
        outcome = ex.write("Éditeur", "Bonjour", replace=False)
        assert outcome.ok and outcome.verified and outcome.method == "DevTools insertText"
        assert page.inserted == ["Bonjour"]
    else:
        with pytest.raises(actions.ActionError, match="ScrollIntoView") as caught:
            ex.write("Éditeur", "Bonjour", replace=False)
        assert "keystrokes" not in str(caught.value) and page.inserted == []
    assert ("scroll_into_view", editor.element) in auto.calls


def test_devtools_select_never_falls_back_to_typing(monkeypatch):
    combo = node("ComboBox", "Couleur", framework="Chrome", has_value=True, value="Rouge", rect=(10, 10, 100, 20))
    view = make_view(combo)
    view.framework = view.info.framework = "Chromium"
    page = FakePage(combo.element)
    page.evaluate = lambda script: 1 if "devicePixelRatio" in script else False  # <select> introuvable au point
    ex = executor(view, FakeAuto())
    ex.get_page = lambda: as_page(page)
    ex._web_viewport = lambda: w.Rect(0, 0, 400, 300)
    monkeypatch.setattr(actions.foreground, "brief_activation", lambda *a, **k: pytest.fail("activation brève"))
    with pytest.raises(actions.ActionError) as caught:
        ex.select("Couleur", "Bleu")
    assert "DevTools ✗" in str(caught.value) and "keystrokes" not in str(caught.value)


# --- 16. Contenu web d'une fenêtre Chromium recouverte ---------------------------------------------------------------
def chrome(role: str, name: str, **props) -> uia.Node:
    return node(role, name, framework="Chrome", **props)


def web_window(*extra_in_document: uia.Node):
    button = chrome("Button", "Valider", has_invoke=True, rect=(100, 200, 80, 30))
    document = link(
        chrome("Document", "Labo Web — prêt", automation_id="RootWebArea", rect=(0, 80, 800, 520)),
        button,
        *extra_in_document,
    )
    void = chrome("Pane", "", rect=(0, 80, 741, 730))  # views::View vide, frère du Document, dessiné « après »
    pane = link(chrome("Pane", "Labo Web — prêt", rect=(0, 0, 800, 600)), document, void)
    link(chrome("Window", "Labo Web", rect=(0, 0, 800, 600)), pane)
    view = make_view(button)
    view.framework = view.info.framework = "Chromium"
    return view, button


def test_covered_chromium_window_reaches_web_content(monkeypatch):
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: 999)
    view, button = web_window()
    ex = executor(view, FakeAuto())
    ex._windows, ex.acted = {HWND}, False
    assert under(ex, 140, 215).node is button
    assert under(ex, 140, 215, roles=actions.CHECK_ROLES).node is button


def test_web_veil_inside_the_page_still_blocks(monkeypatch):
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: 999)
    veil = chrome("Group", "", rect=(0, 80, 800, 520))  # voile modal de la page, après le bouton dans le DOM
    view, _ = web_window(veil)
    ex = executor(view, FakeAuto())
    ex._windows, ex.acted = {HWND}, False
    assert ex._element_under(140, 215) is None


# --- 17. Lecture incomplète : ni présent ni absent ---------------------------------------------------------------------
def incomplete_view(**flags) -> snapshot.View:
    view = make_view(node("Button", "Valider", has_invoke=True))
    view.node_count = 40
    for key, value in flags.items():
        setattr(view, key, value)
    return view


@pytest.mark.parametrize("flag", ["truncated", "web_missing"])
def test_incomplete_view_is_never_proof_of_absence(flag):
    ex = executor(incomplete_view(**{flag: True}), FakeAuto())
    ex.hwnd = 0  # pas de titre de fenêtre
    assert ex.present("Valider") is True
    assert ex.present("Chargement") is None and ex.unreadable
    with pytest.raises(actions.ActionError, match="incomplete"):
        ex.wait("Chargement", gone=True, timeout_s=0.1)  # jamais « ✓ disparu »


def test_expect_after_an_incomplete_view_is_not_proof(monkeypatch):
    monkeypatch.setattr(server.w, "IsWindow", lambda h: True)
    done = make_view(node("Button", "Lancer", has_invoke=True), node("Text", "Terminé"))
    views = [incomplete_view(truncated=True), done]  # avant l'action : arbre tronqué ; après : complet
    state = {"view": views[0]}

    def get_view(rebuild: bool) -> snapshot.View:
        if rebuild and views:
            state["view"] = views.pop(0)
        return state["view"]

    ex = actions.Executor(as_auto(FakeAuto()), 0, get_view, lambda: None)
    ex._pids = {7}
    steps = [server.Step(do="click", target="Lancer", expect="Terminé")]
    report, failed = server._run_steps(ex, steps, 0, as_call())
    assert not failed and "not proof" in report[0] and report[0].split()[1] == "~"


def test_tree_reports_truncation_only_when_nodes_are_cut():
    class ComEl:
        def __init__(self, kids: list[ComEl]) -> None:
            self.kids = kids

        def GetCachedPropertyValue(self, pid):
            return None

        def GetCachedChildren(self):
            if not self.kids:
                return None
            kids = self.kids
            return type("Array", (), {"Length": len(kids), "GetElement": lambda _self, i: kids[i]})()

    root = ComEl([ComEl([]), ComEl([])])
    auto = uia.Automation.__new__(uia.Automation)
    # Faux COM : Automation créé sans __init__, sans demande de cache ni vrai UI Automation.
    fake_uia = type("UIA", (), {"ElementFromHandleBuildCache": lambda _self, hwnd, cache: root})()
    vars(auto).update(_cache=None, uia=fake_uia)
    assert auto.tree(1, max_nodes=3).truncated is False  # exactement 3 nœuds : rien n'est coupé
    tree = auto.tree(1, max_nodes=2)
    assert tree.truncated is True and len(tree.children) == 1


def test_build_view_flags_truncation_and_missing_web_content(monkeypatch):
    info = snapshot.WindowInfo(
        HWND, "T", "t.exe", "Chrome_WidgetWin_1", w.Rect(0, 0, 400, 300), False, False, False, "Chromium"
    )
    monkeypatch.setattr(snapshot, "window_info", lambda hwnd: info)
    monkeypatch.setattr(snapshot, "popups_of", lambda hwnd: [])
    monkeypatch.setattr(snapshot.w, "frame_rect", lambda hwnd: info.rect)
    monkeypatch.setitem(snapshot._NO_WEB_CONTENT, HWND, float("inf"))  # constat récent : pas de nouvelle attente

    class TreeAuto:
        def tree(self, hwnd, max_nodes):
            root = link(node("Window", "T", rect=(0, 0, 400, 300)), node("Button", "Retour", has_invoke=True))
            root.truncated = True
            return root

    view = snapshot.build_view(as_auto(TreeAuto()), HWND)
    assert view.truncated and view.web_missing and view.roots[HWND].name == "T"
    assert "truncated" in server._header(view, [], view.frame, True)


# --- 20. Liste ou menu ouverts : repliés sur tout chemin d'échec -------------------------------------------------------
@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    for module in (act_base, act_chromium, act_pointer, act_uia, act_wait, act_win32):  # exécuteur découpé par famille
        monkeypatch.setattr(module, "time", fake)
    return fake


def test_select_missing_option_collapses_the_list(clock):
    combo = node("ComboBox", "Couleur", has_expand=True, expand=0)
    auto = FoldingAuto()
    with pytest.raises(actions.ActionError, match="not found"):
        executor(make_view(combo), auto).select("Couleur", "Inexistante")
    assert auto.calls == [("expand", combo.element), ("collapse", combo.element)]


def test_select_value_fallback_collapses_the_list(clock):
    combo = node("ComboBox", "Ville", has_expand=True, expand=0, has_value=True, value="")
    auto = FoldingAuto()
    outcome = executor(make_view(combo), auto).select("Ville", "Lyon")
    assert outcome.ok and outcome.verified
    assert ("collapse", combo.element) in auto.calls and combo.element.live["expand"] == 0


def test_emergency_stop_during_select_does_nothing_more(clock, monkeypatch):
    combo = node("ComboBox", "Couleur", has_expand=True, expand=0)
    auto = FoldingAuto()
    ex = executor(make_view(combo), auto)

    def stop_now() -> None:
        if auto.calls:
            raise worker.Stopped("Arrêt d'urgence")

    monkeypatch.setattr(actions.worker, "check_cancelled", stop_now)
    with pytest.raises(worker.Stopped):
        ex.select("Couleur", "Bleu")
    assert auto.calls == [("expand", combo.element)]  # plus aucune action après l'arrêt, même pas le repli


def test_menu_with_missing_level_collapses_the_opened_one(clock):
    fichier = node("MenuItem", "Fichier", has_expand=True, expand=0)
    auto = FoldingAuto()
    with pytest.raises(actions.ActionError, match="not found"):
        executor(make_view(fichier), auto).menu("Fichier > Inexistant")
    assert auto.calls == [("expand", fichier.element), ("collapse", fichier.element)]


# --- 22. Défilement sans cible : le conteneur sous le point de la molette ----------------------------------------------
class PositionAuto(FakeAuto):
    def scroll(self, element: FakeEl, h: int, v: int) -> None:
        self.calls.append(("scroll", element))
        element.live["position"] = element.live.get("position", 0.0) + 10.0

    def scroll_percent(self, element: FakeEl):
        return 0.0, element.live.get("position", 0.0)


def test_scroll_without_target_uses_the_container_under_the_wheel_point(monkeypatch):
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: 999)
    side = node("List", "Dossiers", has_scroll=True, rect=(0, 0, 100, 300))  # panneau latéral, premier de l'arbre
    content = node("Document", "Contenu", has_scroll=True, rect=(100, 0, 300, 300))  # sous le centre (200, 150)
    auto = PositionAuto()
    ex = executor(make_view(side, content), auto)
    ex._windows, ex.acted = {HWND}, False
    outcome = ex.scroll(None, "down", 1)
    assert outcome.ok and outcome.verified and 'Document "Contenu"' in outcome.line
    assert auto.calls == [("scroll", content.element)]


def test_scroll_without_container_at_center_takes_the_largest(monkeypatch):
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: 999)
    small = node("List", "Dossiers", has_scroll=True, rect=(0, 0, 100, 300))
    large = node("List", "Messages", has_scroll=True, rect=(250, 0, 150, 300))
    auto = PositionAuto()
    ex = executor(make_view(small, large), auto)
    ex._windows, ex.acted = {HWND}, False
    outcome = ex.scroll(None, "down", 1)
    assert 'List "Messages"' in outcome.line and auto.calls == [("scroll", large.element)]


class AtEndAuto(FakeAuto):
    def scroll(self, element: FakeEl, h: int, v: int) -> None:
        self.calls.append(("scroll", element))

    def scroll_percent(self, element: FakeEl):
        return 0.0, 100.0  # déjà tout en bas : rien ne bouge


def test_scroll_at_a_point_on_a_container_already_at_the_end_is_not_a_success(monkeypatch):
    """Banc du 20/10 (Edge, Electron) : molette sur un canevas, page déjà en bas → « ✓ » alors que le canevas n'avait
    rien reçu. Point visé par le modèle : issue inconnue, dite ; sans cible, « déjà en bas » reste vrai."""
    monkeypatch.setattr(actions.w, "window_at", lambda x, y: 999)
    page = node("Document", "Page", has_scroll=True, rect=(0, 0, 400, 300))
    ex = executor(make_view(page), AtEndAuto())
    ex._windows, ex.acted = {HWND}, False
    monkeypatch.setattr(ex, "resolve", lambda spec, prefer=None: ("point", (50, 50)))
    # Banc commun du 21/10 : la technique suivante (molette par message au point) est essayée ; sans elle, échec dit,
    # jamais un « ✓ ».
    with pytest.raises(actions.ActionError, match="already at the end"):
        ex.scroll("50,50", "down", 1)
    whole = ex.scroll(None, "down", 1)
    assert whole.verified and "already at the end" in whole.line


# --- 26. Code mort retiré ; recherche par nom portée sur snapshot.pick -----------------------------------------------
def test_dead_code_is_gone():
    assert not hasattr(snapshot, "find_elements")
    assert not hasattr(uia, "TOGGLE_STATES") and not hasattr(uia, "EXPAND_STATES")
    assert not hasattr(uia.Node, "has")
    for name in ("set_auto_set_focus", "set_range", "get_value"):
        assert not hasattr(uia.Automation, name)


def test_pick_prefers_control_over_label():
    view = make_view(node("Text", "Liste"), node("List", "Liste"), node("Edit", "Nom", has_value=True))
    assert snapshot.pick(view, "Liste")[0].role == "List"  # l'étiquette passe après le contrôle
    assert snapshot.pick(view, "Liste", {"List", "ComboBox"})[0].role == "List"
    assert snapshot.pick(view, "nom")[0].role == "Edit"


def test_pick_role_prefix_and_partial():
    view = make_view(
        node("Button", "Enregistrer sous", has_invoke=True), node("Button", "Enregistrer", has_invoke=True)
    )
    assert snapshot.pick(view, "bouton:Enregistrer")[0].name == "Enregistrer"
    element, note = snapshot.pick(view, "sous")
    assert element.name == "Enregistrer sous" and "partial" in note
    with pytest.raises(snapshot.TargetError):
        snapshot.pick(view, "lien:Enregistrer")


# --- Cible par nom : une seule construction de l'instantané au premier nom d'un act ------------------------------------
@pytest.mark.parametrize(("fresh", "rebuilt"), [(True, False), (False, True)])
def test_first_name_rebuilds_only_an_older_snapshot(fresh, rebuilt):
    view = make_view(node("Button", "Valider", has_invoke=True))
    asked: list[bool] = []

    def get_view(rebuild: bool) -> snapshot.View:
        asked.append(rebuild)
        return view

    ex = actions.Executor(as_auto(FakeAuto()), HWND, get_view, lambda: None, fresh=fresh)
    ex._pids = {7}
    ex.resolve("Valider")
    assert (True in asked) is rebuilt


# --- Bouton à menu Chromium (aria-haspopup) : cliquer, c'est ouvrir ---------------------------------------------------
@pytest.mark.parametrize(
    ("role", "first", "second"),
    [
        ("Button", "UIA Expand/Collapse", "UIA Invoke"),
        ("MenuItem", "UIA Expand/Collapse", "UIA Invoke"),
        ("ComboBox", "UIA Expand/Collapse", "UIA Invoke"),
        ("SplitButton", "UIA Invoke", "UIA Expand/Collapse"),  # la partie principale exécute sa commande
        ("TreeItem", "UIA Invoke", "UIA Expand/Collapse"),  # le clic sélectionne
    ],
)
def test_chromium_expandable_click_order_keeps_both_techniques(role, first, second):
    element = chrome(role, "Plus", has_invoke=True, has_expand=True, expand=0, rect=(100, 200, 80, 30))
    view = make_view(element)
    view.framework = view.info.framework = "Chromium"
    ex = executor(view, FoldingAuto())
    seen = chains(ex)
    ex.click_element(view.elements[0])
    assert seen[0][:2] == [first, second]


def test_expand_accepts_a_button_with_expand_collapse(clock):
    button = node("Button", "Options", has_expand=True, expand=0)
    auto = FoldingAuto()
    outcome = executor(make_view(button), auto).expand("Options", True)
    assert outcome.ok and outcome.verified and auto.calls == [("expand", button.element)]


def test_expand_refuses_a_plain_button_without_invoking_it(clock):
    button = node("Button", "Supprimer", has_invoke=True)
    auto = FakeAuto()
    with pytest.raises(actions.ActionError, match="not expandable"):
        executor(make_view(button), auto).expand("Supprimer", True)
    assert auto.calls == []


# --- Bureau interactif : un fil lancé hors du bureau « Default » n'y verrait aucune fenêtre ---------------------------
def test_default_desktop_opened_once_then_reused(monkeypatch):
    opened: list[str] = []
    attached: list[int] = []
    monkeypatch.setattr(w, "_hdesk_default", None)
    monkeypatch.setattr(w, "OpenDesktopW", lambda name, *a: opened.append(name) or 4242)
    monkeypatch.setattr(w, "SetThreadDesktop", lambda h: attached.append(h) or 1)
    assert w.ensure_default_desktop() and w.ensure_default_desktop()
    assert opened == ["Default"] and attached == [4242, 4242]


def test_default_desktop_refused_is_harmless(monkeypatch):
    monkeypatch.setattr(w, "_hdesk_default", None)
    monkeypatch.setattr(w, "OpenDesktopW", lambda *a: None)  # accès refusé : rien à rattacher
    monkeypatch.setattr(w, "SetThreadDesktop", lambda h: pytest.fail("rattachement sans bureau"))
    assert w.ensure_default_desktop() is False


def test_posted_click_uses_the_logical_client_point_of_a_dpi_virtualized_window(monkeypatch):
    """Banc commun du 22/10 : à 125 %, une fenêtre non adaptée au DPI lit le message posté en pixels logiques ; le point
    physique (ScreenToClient d'un processus par moniteur) visait une ligne plus bas dans une ListBox WinForms."""
    monkeypatch.setattr(methods.w, "screen_to_client", lambda h, x, y: (125, 50))
    monkeypatch.setattr(methods.w, "window_dpi_scale", lambda h: 1.25)
    assert methods.w.message_point(1, 0, 0) == (100, 40)
    monkeypatch.setattr(methods.w, "window_dpi_scale", lambda h: 1.0)
    assert methods.w.message_point(1, 0, 0) == (125, 50)


# --- Texte d'abord (règle commune, point 2 ; banc passe 2 du 24/10) ---------------------------------------------------
def _rich(after: snapshot.View) -> snapshot.View:
    elements = [
        snapshot.Element(i, node("Button", f"Bouton {i}", has_invoke=True, rect=(120 + 40 * i, 420, 30, 20)), 0, HWND)
        for i in range(1, 9)
    ]
    return snapshot.View(HWND, after.info, [HWND], elements, after.frame)


def test_auto_is_text_only_on_a_rich_tree(shifted, monkeypatch):
    session, _, after, _ = shifted
    monkeypatch.setattr(session, "view", lambda auto, hwnd, rebuild=False, detail=None: _rich(after))
    out = server.render(NO_AUTO, HWND, "auto")
    assert len(out) == 1 and '"Bouton 8"' in out[0]  # la liste seule, sans image


def test_auto_attaches_the_image_after_an_unverified_action(shifted, monkeypatch):
    session, _, after, _ = shifted
    monkeypatch.setattr(session, "view", lambda auto, hwnd, rebuild=False, detail=None: _rich(after))
    out = server.render(NO_AUTO, HWND, "auto", unverified=True)
    assert len(out) == 2 and '"Bouton 8"' in out[0]


def test_auto_attaches_the_image_when_the_tree_is_too_thin(shifted):
    out = server.render(NO_AUTO, HWND, "auto")  # deux éléments seulement (canevas, jeu, page muette)
    assert len(out) == 2


def test_unverified_steps():
    assert server.unverified(True, ["✓"]) and server.unverified(False, ["✓", "~"]) and server.unverified(False, ["✗"])
    assert not server.unverified(False, ["✓", "="])


@pytest.mark.parametrize("flag", ["stale", "truncated"])
def test_auto_attaches_the_image_when_the_tree_may_be_stale_or_cut(shifted, monkeypatch, flag):
    session, _, after, _ = shifted
    view = _rich(after)
    setattr(view, flag, True)  # en-tête : « rely on the image » / « incomplete list »
    monkeypatch.setattr(session, "view", lambda auto, hwnd, rebuild=False, detail=None: view)
    assert len(server.render(NO_AUTO, HWND, "auto")) == 2


def test_tree_retries_transient_provider_failure(monkeypatch):
    """Banc du 25/10 : Electron/Edge répond parfois EVENT_E_ALL_SUBSCRIBERS_FAILED le temps de reconstruire son arbre
    d'accessibilité ; l'arbre est relu au lieu de déclarer la fenêtre illisible. Toute autre erreur reste immédiate."""
    import comtypes

    monkeypatch.setattr(uia.time, "sleep", lambda s: None)
    root = type("ComEl", (), {"GetCachedPropertyValue": lambda _s, pid: None, "GetCachedChildren": lambda _s: None})()
    calls = []

    def build(_self, hwnd, cache):
        calls.append(hwnd)
        if len(calls) < uia.TREE_ATTEMPTS:
            raise comtypes.COMError(-2147220991, "aucun abonné", (None, None, None, 0, None))
        return root

    auto = uia.Automation.__new__(uia.Automation)
    vars(auto).update(_cache=None, uia=type("UIA", (), {"ElementFromHandleBuildCache": build})())
    assert auto.tree(1).truncated is False and len(calls) == uia.TREE_ATTEMPTS

    def denied(_self, hwnd, cache):
        calls.append(hwnd)
        raise comtypes.COMError(-2147024891, "accès refusé", (None, None, None, 0, None))

    calls.clear()
    vars(auto).update(uia=type("UIA", (), {"ElementFromHandleBuildCache": denied})())
    with pytest.raises(uia.UIAError):
        auto.tree(1)
    assert len(calls) == 1
