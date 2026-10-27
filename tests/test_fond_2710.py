"""Molette Tk hors écran (27 octobre) : fenêtre entièrement recouverte déplacée le temps du geste, puis remise.

Garde-fous de la relecture de Codex du même jour : couverture exacte (une bande d'un pixel suffit à refuser), retour
relu, même enfant avant et après le déplacement, fenêtre ramenée au premier plan pendant le déplacement."""

from __future__ import annotations

import pytest

from nexus_windows import methods
from nexus_windows import win32 as w

TARGET = w.Rect(100, 100, 300, 200)


def _tk_window(
    monkeypatch,
    covers: dict[int, w.Rect] | None = None,
    child_after: int = 43,
    refuse_back: int = 0,
    brought_forward: bool = False,
) -> list:
    """Fenêtre 42 (TARGET), recouverte par `covers` (fenêtres au-dessus d'elle, du bas vers le haut)."""
    calls: list = []
    covers = {1: w.Rect(0, 0, 1000, 1000)} if covers is None else covers
    above = list(covers)
    place = {"left": TARGET.left, "top": TARGET.top}
    refused = {"n": refuse_back}

    def window_rect(h):
        return w.Rect(place["left"], place["top"], place["left"] + TARGET.width, place["top"] + TARGET.height)

    def set_pos(h, a, x, y, cx, cy, f):
        calls.append(("move", x, y))
        if x == TARGET.left and refused["n"]:
            refused["n"] -= 1
            return 0
        place.update(left=x, top=y)
        return 1

    def prev(h, cmd):
        order = [42, *above]
        i = order.index(h)
        return order[i + 1] if i + 1 < len(order) else 0

    monkeypatch.setattr(w, "IsZoomed", lambda h: 0)
    monkeypatch.setattr(w, "IsIconic", lambda h: 0)
    monkeypatch.setattr(w, "IsWindowVisible", lambda h: 1)
    monkeypatch.setattr(w, "is_cloaked", lambda h: False)
    monkeypatch.setattr(w, "ex_style", lambda h: 0)
    monkeypatch.setattr(w, "has_window_region", lambda h: False)
    monkeypatch.setattr(w, "GetDpiForWindow", lambda h: 96)
    monkeypatch.setattr(w, "monitor_dpi", lambda h, default=96: 96)
    monkeypatch.setattr(w, "GetWindow", prev)
    monkeypatch.setattr(w, "frame_rect", lambda h: window_rect(h) if h == 42 else covers[h])
    monkeypatch.setattr(w, "window_rect", window_rect)
    monkeypatch.setattr(w, "GetSystemMetrics", lambda i: {w.SM_XVIRTUALSCREEN: 0, w.SM_CXVIRTUALSCREEN: 1000}[i])
    monkeypatch.setattr(w, "SetWindowPos", set_pos)
    monkeypatch.setattr(w, "deepest_child_at", lambda top, x, y: 43 if place["left"] == TARGET.left else child_after)
    monkeypatch.setattr(w, "WindowFromPoint", lambda p: child_after)
    monkeypatch.setattr(w, "handle", lambda v: v)
    monkeypatch.setattr(w, "window_thread", lambda h: 6 if h == 42 else 5)
    monkeypatch.setattr(w, "foreground", lambda: 42 if brought_forward and place["left"] != TARGET.left else 99)
    monkeypatch.setattr(methods, "post", lambda h, m, wp, lp: calls.append(("post", h, m, lp)))
    monkeypatch.setattr(methods.time, "sleep", lambda s: None)
    return calls


def test_tk_wheel_off_screen_moves_posts_at_the_shifted_point_and_comes_back(monkeypatch):
    calls = _tk_window(monkeypatch)
    assert methods.post_wheel_offscreen(42, 150, 160, -120) == 43
    dx = 1000 + 50 - 100
    assert calls == [
        ("move", 100 + dx, 100),
        ("post", 43, w.WM_MOUSEWHEEL, w.make_lparam(150 + dx, 160)),
        ("move", 100, 100),
    ]


def test_a_one_pixel_visible_strip_is_enough_to_refuse(monkeypatch):
    """Deux fenêtres couvrent tout sauf la colonne x = 110 : la fenêtre est visible, jamais déplacée."""
    calls = _tk_window(monkeypatch, covers={1: w.Rect(0, 0, 110, 1000), 2: w.Rect(111, 0, 1000, 1000)})
    with pytest.raises(methods.MethodError, match="partly visible"):
        methods.post_wheel_offscreen(42, 150, 160, -120)
    assert calls == []


def test_two_windows_that_cover_everything_together_count_as_covered(monkeypatch):
    _tk_window(monkeypatch, covers={1: w.Rect(0, 0, 200, 1000), 2: w.Rect(200, 0, 1000, 1000)})
    assert methods.fully_covered(42)


def test_a_layered_window_on_top_never_counts_as_cover(monkeypatch):
    _tk_window(monkeypatch)
    monkeypatch.setattr(w, "ex_style", lambda h: methods.WS_EX_LAYERED if h == 1 else 0)
    assert not methods.fully_covered(42)


def test_another_control_under_the_point_off_screen_sends_nothing_and_comes_back(monkeypatch):
    calls = _tk_window(monkeypatch, child_after=44)
    with pytest.raises(methods.MethodError, match="same control"):
        methods.post_wheel_offscreen(42, 150, 160, -120)
    assert [c[0] for c in calls] == ["move", "move"] and calls[-1] == ("move", 100, 100)


def test_window_brought_forward_during_the_move_sends_nothing(monkeypatch):
    calls = _tk_window(monkeypatch, brought_forward=True)
    with pytest.raises(methods.MethodError, match="brought this window forward"):
        methods.post_wheel_offscreen(42, 150, 160, -120)
    assert not [c for c in calls if c[0] == "post"] and calls[-1] == ("move", 100, 100)


def test_a_refused_return_is_tried_again_once(monkeypatch):
    calls = _tk_window(monkeypatch, refuse_back=1)
    assert methods.post_wheel_offscreen(42, 150, 160, -120) == 43
    assert [c for c in calls if c[0] == "move"][-2:] == [("move", 100, 100), ("move", 100, 100)]


def test_a_window_that_cannot_be_put_back_is_reported_with_the_wheel_unknown(monkeypatch):
    _tk_window(monkeypatch, refuse_back=2)
    with pytest.raises(methods.WindowNotRestored, match=r"could not be put back.*effect is unknown"):
        methods.post_wheel_offscreen(42, 150, 160, -120)


def test_tk_wheel_never_moves_a_maximized_window(monkeypatch):
    calls = _tk_window(monkeypatch)
    monkeypatch.setattr(w, "IsZoomed", lambda h: 1)
    with pytest.raises(methods.MethodError, match="maximized"):
        methods.post_wheel_offscreen(42, 150, 160, -120)
    assert calls == []


def test_a_shaped_window_on_top_never_counts_as_cover(monkeypatch):
    """Fenêtre en anneau (SetWindowRgn) dont le cadre englobe la cible : son trou la laisse voir."""
    _tk_window(monkeypatch)
    monkeypatch.setattr(w, "has_window_region", lambda h: h == 1)
    assert not methods.fully_covered(42)


def test_rounded_corners_of_the_cover_never_hide_the_target(monkeypatch):
    """Cible logée dans le coin d'une fenêtre qui la couvre : les coins arrondis DWM la laissent voir."""
    _tk_window(monkeypatch, covers={1: w.Rect(95, 95, 1000, 1000)})
    assert not methods.fully_covered(42)


def test_a_window_not_put_back_is_a_failed_step_not_an_unknown_one():
    from nexus_windows import actions, server

    assert methods.WindowNotRestored in server.STEP_ERRORS
    assert not issubclass(methods.WindowNotRestored, (methods.MethodError, actions.Unconfirmed))


def test_corner_radius_follows_the_scale_of_the_cover(monkeypatch):
    """Couvrant à 250 % (coins de 20 px) dont le bord gauche passe 12 px avant la cible carrée : son coin la laisse voir."""
    _tk_window(monkeypatch, covers={1: w.Rect(88, 100, 1000, 1000)})
    monkeypatch.setattr(w, "monitor_dpi", lambda h, default=96: 240)
    assert not methods.fully_covered(42)


def test_an_unaware_cover_on_a_scaled_screen_uses_the_screen_scale(monkeypatch):
    """Application non adaptée au DPI : GetDpiForWindow rend 96, son écran est à 250 % : le coin est celui de l'écran."""
    _tk_window(monkeypatch, covers={1: w.Rect(88, 100, 1000, 1000)})
    monkeypatch.setattr(w, "GetDpiForWindow", lambda h: 96)
    monkeypatch.setattr(w, "monitor_dpi", lambda h, default=96: 240)
    assert not methods.fully_covered(42)


def test_a_cover_whose_screen_scale_is_unreadable_never_counts(monkeypatch):
    _tk_window(monkeypatch)
    monkeypatch.setattr(w, "monitor_dpi", lambda h, default=96: default)
    assert not methods.fully_covered(42)


def test_a_cover_without_redirection_bitmap_never_counts(monkeypatch):
    """WS_EX_NOREDIRECTIONBITMAP : client possiblement transparent, la cible peut se voir à travers."""
    _tk_window(monkeypatch)
    monkeypatch.setattr(w, "ex_style", lambda h: 0x00200000 if h == 1 else 0)
    assert not methods.fully_covered(42)
