"""Tk en arrière-plan par état simulé (23 octobre) et garde-fous de la relecture de Codex du même jour."""

from __future__ import annotations

import pytest
from test_contract import FakeAuto, executor, make_view, node

from nexus_windows import actions


def _tk_executor(monkeypatch, *nodes):
    view = make_view(*nodes)
    view.framework = "Tk"
    ex = executor(view, FakeAuto())
    monkeypatch.setattr(ex, "_confirm_keys", lambda *a: None)
    monkeypatch.setattr(ex, "_fingerprint", iter(range(1000)).__next__)  # chaque relevé diffère : « visible change »
    return ex


def _record(monkeypatch, events: list):
    monkeypatch.setattr(actions.w, "send_message", lambda h, m, *a, **k: events.append(("send", m)) or 0)
    monkeypatch.setattr(actions.w, "PostMessageW", lambda h, m, *a: events.append(("post", m)) or 1)
    monkeypatch.setattr(actions.w, "foreground", lambda: 99)
    monkeypatch.setattr(actions.w, "window_thread", lambda h: 5 if h == 99 else 6)


def test_tk_shortcut_sets_focus_then_keys_then_kills_focus(monkeypatch):
    ex = _tk_executor(monkeypatch)
    events: list = []
    _record(monkeypatch, events)
    monkeypatch.setattr(ex, "_post_combos", lambda combos, fw, sent, **k: events.append(("keys", fw)))
    outcome = ex.press("ctrl+k")
    assert events == [("send", 0x0007), ("keys", "Tk"), ("post", 0x0008)]
    assert outcome.ok and "simulated focus" in outcome.line


def test_tk_shortcut_with_target_never_uses_simulated_focus(monkeypatch):
    """Destinataire non prouvé (Tk ne publie pas de champ focalisable) : jamais de touches au focus d'un autre."""
    ex = _tk_executor(monkeypatch, node("Edit", "Wanted", has_value=True))
    events: list = []
    _record(monkeypatch, events)
    monkeypatch.setattr(ex, "_post_combos", lambda *a, **k: pytest.fail("touches postées sans destinataire prouvé"))
    with pytest.raises(actions.ActionError):
        ex.press("delete", target="Wanted")
    assert events == []


def test_tk_in_front_gets_no_simulated_focus(monkeypatch):
    ex = _tk_executor(monkeypatch)
    events: list = []
    _record(monkeypatch, events)
    monkeypatch.setattr(actions.w, "window_thread", lambda h: 5)  # la fenêtre Tk est au premier plan
    monkeypatch.setattr(ex, "_post_combos", lambda *a, **k: pytest.fail("touches simulées vers la fenêtre utilisée"))
    with pytest.raises(actions.ActionError):
        ex.press("ctrl+k")
    assert events == []  # ni WM_SETFOCUS ni WM_KILLFOCUS


def test_tk_stop_during_focus_sends_no_key(monkeypatch):
    """STOP posé PENDANT WM_SETFOCUS (le premier contrôle de _try est passé) : aucune touche, focus défait."""
    ex = _tk_executor(monkeypatch)
    events: list = []
    _record(monkeypatch, events)
    stop = {"now": False}

    def send(h, m, *a, **k):
        events.append(("send", m))
        stop["now"] = True  # l'arrêt arrive pendant la préparation
        return 0

    def check():
        if stop["now"]:
            raise actions.worker.Cancelled("stop")

    monkeypatch.setattr(actions.w, "send_message", send)
    monkeypatch.setattr(actions.worker, "check_cancelled", check)
    monkeypatch.setattr(ex, "_post_combos", lambda *a, **k: pytest.fail("touche envoyée après l'arrêt"))
    with pytest.raises(actions.worker.Cancelled):
        ex.press("ctrl+k")
    assert events == [("send", 0x0007), ("post", 0x0008)]


def test_tk_taken_by_user_during_focus_sends_no_key(monkeypatch):
    ex = _tk_executor(monkeypatch)
    events: list = []
    _record(monkeypatch, events)
    front = {"tk": False}

    def send(h, m, *a, **k):
        events.append(("send", m))
        front["tk"] = True  # Julien prend la fenêtre Tk pendant WM_SETFOCUS
        return 0

    monkeypatch.setattr(actions.w, "send_message", send)
    monkeypatch.setattr(actions.w, "window_thread", lambda h: 6 if (h == 99 and front["tk"]) or h != 99 else 5)
    monkeypatch.setattr(ex, "_post_combos", lambda *a, **k: pytest.fail("touche envoyée à la fenêtre de Julien"))
    with pytest.raises(actions.ActionError):
        ex.press("ctrl+k")
    assert ("post", 0x0008) not in events  # son vrai focus n'est pas défait


def test_partly_sent_then_failed_is_unknown_and_never_replayed(monkeypatch):
    """Pression acceptée, relâchement refusé : issue inconnue, jamais de vrai clic par-dessus (foreground=true)."""
    ex = _tk_executor(monkeypatch)
    ex.allow_foreground = True

    def half(*a, **k):
        actions.methods._ACCEPTED[0] += 1
        raise actions.methods.MethodError("up refused")

    monkeypatch.setattr(actions.methods, "post_click", half)
    monkeypatch.setattr(actions.realinput, "click", lambda *a, **k: pytest.fail("vrai clic rejoué"))
    with pytest.raises(actions.Unconfirmed, match="partly sent"):
        ex.click_point(50, 50)


def test_point_click_on_stale_element_aims_at_the_point(monkeypatch):
    """Élément trouvé sous le point : ses gestes visent le point donné, pas son centre (Electron occulté, 23/10)."""
    item = node("ListItem", "clic 5", rect=(40, 40, 30, 30), framework="Chromium")
    view = make_view(item)
    view.framework = "Chromium"
    ex = executor(view, FakeAuto())
    monkeypatch.setattr(ex, "_element_under", lambda x, y, roles=None: view.elements[0])
    monkeypatch.setattr(ex, "_page_if_free", lambda: None)
    aimed = []
    monkeypatch.setattr(
        ex, "_click_chromium", lambda e, page, what: aimed.append(ex._point_of(e)) or actions.Outcome(True, what, "x")
    )
    ex.click_point(45, 46)
    assert aimed == [(45, 46)] and ex._aim_at is None


def test_point_click_drops_element_whose_refresh_fails(monkeypatch):
    item = node("Button", "old", rect=(40, 40, 30, 30), framework="Chromium", gone=True)
    view = make_view(item)
    view.framework = "Chromium"
    ex = executor(view, FakeAuto())
    monkeypatch.setattr(ex, "_element_under", lambda x, y, roles=None: view.elements[0])
    monkeypatch.setattr(ex, "_page_if_free", lambda: None)
    monkeypatch.setattr(ex, "click_element", lambda *a, **k: pytest.fail("ancien élément cliqué"))
    monkeypatch.setattr(ex, "_post_click", lambda x, y, *a: (x, y))
    monkeypatch.setattr(ex, "_fingerprint", iter(range(1000)).__next__)
    assert ex.click_point(45, 46).ok


# --- Pression simulée possédée par un fil auxiliaire (relecture de Codex du 23/10, tour 3) -------------------------
class _NoState:
    def __init__(self, *a, **k) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass


def _press_env(monkeypatch, send):
    from nexus_windows import methods

    monkeypatch.setattr(methods, "fake_modifiers", _NoState)
    monkeypatch.setattr(methods.w, "send_message", send)
    monkeypatch.setattr(methods.w, "IsWindow", lambda h: True)
    monkeypatch.setattr(methods.w, "foreground", lambda: 0)
    monkeypatch.setattr(methods.w, "deepest_child_at", lambda top, x, y: 77)
    monkeypatch.setattr(methods.w, "window_thread", lambda h: 6)  # toutes les fenêtres Tk : un seul fil GUI
    monkeypatch.setattr(methods.w, "responsive", lambda h, timeout_ms=300: True)
    monkeypatch.setattr(methods.w, "message_point", lambda h, x, y: (x, y))
    monkeypatch.setattr(methods, "win_class", lambda h: "tkchild")
    return methods


def test_unanswered_press_still_releases_and_stops_the_double_click(monkeypatch):
    sent = []

    def send(h, m, *a, **k):
        sent.append(m)
        return None if m == 0x0201 else 0  # la pression reste sans réponse

    methods = _press_env(monkeypatch, send)
    with pytest.raises(methods.MethodError, match="press"):
        methods.post_click(1, 5, 5, count=2, held=True)
    assert sent == [0x0200, 0x0201, 0x0202]  # survol, pression, relâchement ; jamais de seconde pression


def test_blocked_press_is_abandoned_then_released_and_blocks_new_presses(monkeypatch):
    import threading

    release = threading.Event()
    sent = []

    def send(h, m, *a, **k):
        sent.append(m)
        if m == 0x0201:
            release.wait(5)
        return 0

    methods = _press_env(monkeypatch, send)
    monkeypatch.setattr(methods, "HELD_SEND_S", 0.2)
    with pytest.raises(methods.MethodError, match="within 1 s"):
        methods.post_click(1, 5, 5, held=True)
    with pytest.raises(methods.MethodError, match="not finished"):
        methods.post_click(1, 5, 5, held=True)  # pression précédente pas soldée : rien n'est envoyé
    monkeypatch.setattr(methods.w, "deepest_child_at", lambda top, x, y: 78)
    with pytest.raises(methods.MethodError, match="not finished"):
        methods.post_click(1, 9, 9, held=True)  # autre fenêtre du même fil : refusée aussi
    release.set()
    for _ in range(100):
        if not methods._PENDING_PRESS:
            break
        threading.Event().wait(0.02)
    assert sent == [0x0200, 0x0201, 0x0202] and not methods._PENDING_PRESS  # relâchement envoyé par l'auxiliaire


def test_stop_during_the_hover_sends_no_press(monkeypatch):
    sent = []
    from nexus_windows import worker

    stop = {"now": False}

    def send(h, m, *a, **k):
        sent.append(m)
        stop["now"] = True  # arrêt d'urgence pendant le survol
        return 0

    methods = _press_env(monkeypatch, send)

    def check():
        if stop["now"]:
            raise worker.Cancelled("stop")

    monkeypatch.setattr(methods.worker, "check_cancelled", check)
    with pytest.raises(worker.Cancelled):
        methods.post_click(1, 5, 5, held=True)
    assert sent == [0x0200]


def test_unanswered_release_keeps_the_application_locked_while_its_window_exists(monkeypatch):
    """Relâchement sans réponse : jamais prouvé soldé ; le fil reste fermé aux pressions simulées tant que la fenêtre
    existe (une réponse à WM_NULL ne prouve pas la livraison du relâchement : relecture de Codex du 23/10)."""
    import threading

    gone = threading.Event()
    sent = []

    def send(h, m, *a, **k):
        sent.append(m)
        return None if m == 0x0202 else 0

    methods = _press_env(monkeypatch, send)
    monkeypatch.setattr(methods.w, "IsWindow", lambda h: not gone.is_set())
    monkeypatch.setattr(methods.time, "sleep", lambda s: gone.wait(0.02))
    with pytest.raises(methods.MethodError, match="release"):
        methods.post_click(1, 5, 5, held=True)
    with pytest.raises(methods.MethodError, match="not finished"):
        methods.post_click(1, 5, 5, held=True)
    gone.set()  # la fenêtre disparaît : le fil est libéré
    for _ in range(100):
        if not methods._PENDING_PRESS:
            break
        threading.Event().wait(0.02)
    assert not methods._PENDING_PRESS and sent == [0x0200, 0x0201, 0x0202]


# --- Défilement sans molette (23/10) -----------------------------------------------------------------------------
def test_scroll_bar_lines_read_back(monkeypatch):
    ex = _tk_executor(monkeypatch)
    states = iter([(0, 199, 4, 0), (0, 199, 4, 3)])
    posted = []
    monkeypatch.setattr(actions.methods, "scroll_bar_beside", lambda top, x, y, vertical: 55)
    monkeypatch.setattr(actions.methods, "scroll_bar_state", lambda bar: next(states))
    monkeypatch.setattr(actions.methods, "post_scroll_lines", lambda *a: posted.append(a))
    assert "read back ✓ position 0 → 3" in ex._scroll_by_bar(10, 10, "down", 1)
    assert posted == [(55, True, True, 3)]


def test_scroll_bar_at_end_sends_nothing_and_unmoved_is_unknown(monkeypatch):
    ex = _tk_executor(monkeypatch)
    monkeypatch.setattr(actions.methods, "scroll_bar_beside", lambda top, x, y, vertical: 55)
    monkeypatch.setattr(actions.methods, "post_scroll_lines", lambda *a: pytest.fail("envoyé en butée"))
    monkeypatch.setattr(actions.methods, "scroll_bar_state", lambda bar: (0, 199, 4, 196))
    with pytest.raises(actions.methods.MethodError, match="already at the end"):
        ex._scroll_by_bar(10, 10, "down", 1)
    monkeypatch.setattr(actions.methods, "post_scroll_lines", lambda *a: None)
    monkeypatch.setattr(actions.methods, "scroll_bar_state", lambda bar: (0, 199, 4, 0))
    from nexus_windows import act_pointer

    monkeypatch.setattr(act_pointer.time, "sleep", lambda s: None)
    with pytest.raises(actions.Unconfirmed, match="position unchanged"):
        ex._scroll_by_bar(10, 10, "down", 1)


def test_no_scroll_bar_beside_the_point_is_a_plain_refusal(monkeypatch):
    ex = _tk_executor(monkeypatch)
    monkeypatch.setattr(actions.methods, "scroll_bar_beside", lambda top, x, y, vertical: 0)
    with pytest.raises(actions.methods.MethodError, match="no native scroll bar"):
        ex._scroll_by_bar(10, 10, "down", 1)


def test_scroll_bar_must_touch_the_widget_under_the_point(monkeypatch):
    """Deux listes sœurs A et B, chacune sa barre : le point dans B prend la barre de B, jamais celle de A, plus proche."""
    from nexus_windows import methods

    rects = {
        1: methods.w.Rect(0, 0, 300, 200),  # liste A
        2: methods.w.Rect(300, 0, 315, 200),  # barre A
        3: methods.w.Rect(330, 0, 630, 200),  # liste B
        4: methods.w.Rect(630, 0, 645, 200),  # barre B
    }
    monkeypatch.setattr(methods.w, "deepest_child_at", lambda top, x, y: 3 if x >= 330 else 1)
    monkeypatch.setattr(methods.w, "enum_children", lambda top: [1, 2, 3, 4])
    monkeypatch.setattr(methods, "win_class", lambda h: "scrollbar" if h in (2, 4) else "tkchild")
    monkeypatch.setattr(methods.w, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(methods.w, "GetParent", lambda h: 9)
    monkeypatch.setattr(methods.w, "handle", lambda v: v)
    monkeypatch.setattr(methods.w, "style", lambda h: methods.SBS_VERT)
    monkeypatch.setattr(methods.w, "window_rect", lambda h: rects[h])
    assert methods.scroll_bar_beside(0, 340, 100, True) == 4
    assert methods.scroll_bar_beside(0, 50, 100, True) == 2

    # Barre de A collée aussi à B (B sans barre propre) : on ne sait pas laquelle elle pilote → refus, pour A comme pour B.
    rects[3] = methods.w.Rect(315, 0, 615, 200)
    monkeypatch.setattr(methods.w, "enum_children", lambda top: [1, 2, 3])
    assert methods.scroll_bar_beside(0, 340, 100, True) == 0
    assert methods.scroll_bar_beside(0, 50, 100, True) == 0
    rects[3] = methods.w.Rect(315, 0, 615, 300)  # voisin plus haut que la barre : refus aussi
    assert methods.scroll_bar_beside(0, 50, 100, True) == 0


def test_scroll_into_view_error_after_the_call_is_unknown(monkeypatch):
    from test_contract import FakeAuto as _Auto

    class Pattern:
        def ScrollIntoView(self):  # noqa: N802  (nom COM)
            raise actions.uia.UIAError("connexion perdue")

    class Auto(_Auto):
        def tree(self, hwnd):
            area = node("Group", "zone", rect=(0, 0, 100, 100))
            for i in range(4):
                line = node("Text", f"l{i}", rect=(0, i * 60, 50, 20))
                line.parent = area
                area.children.append(line)
            return area

        def pattern(self, element, name):
            return Pattern()

    view = make_view()
    view.framework = "Chromium"
    ex = executor(view, Auto())
    monkeypatch.setattr(ex, "_window_under", lambda x, y: 1)
    with pytest.raises(actions.Unconfirmed, match="ScrollIntoView sent"):
        ex._scroll_by_item(10, 10, "down", 1)


def test_stop_after_pattern_acquisition_invokes_nothing(monkeypatch):
    from test_contract import FakeAuto as _Auto

    calls = []

    class Pattern:
        def ScrollIntoView(self):  # noqa: N802  (nom COM)
            calls.append("invoke")

    class Auto(_Auto):
        def tree(self, hwnd):
            area = node("Group", "zone", rect=(0, 0, 100, 100))
            for i in range(4):
                line = node("Text", f"l{i}", rect=(0, i * 60, 50, 20))
                line.parent = area
                area.children.append(line)
            return area

        def pattern(self, element, name):
            stop["now"] = True  # arrêt d'urgence pendant l'acquisition
            return Pattern()

    stop = {"now": False}

    def check():
        if stop["now"]:
            raise actions.worker.Cancelled("stop")

    view = make_view()
    view.framework = "Chromium"
    ex = executor(view, Auto())
    monkeypatch.setattr(ex, "_window_under", lambda x, y: 1)
    monkeypatch.setattr(actions.worker, "check_cancelled", check)
    from nexus_windows import act_pointer

    monkeypatch.setattr(act_pointer.worker, "check_cancelled", check)
    with pytest.raises(actions.worker.Cancelled):
        ex._scroll_by_item(10, 10, "down", 1)
    assert calls == []
