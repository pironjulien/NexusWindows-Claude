"""Fermeture relue (Alt+F4 en arrière-plan, window close) : jamais rejouée, jamais prouvée par une fenêtre déjà
masquée (relecture de Codex du 22/10)."""

from __future__ import annotations

from nexus_windows import act_uia


class Pattern:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.calls = 0

    def Close(self) -> None:  # noqa: N802 - nom du motif UIA
        self.calls += 1
        if self.fail:
            raise OSError("RPC_E_DISCONNECTED")


class Auto:
    def __init__(self, pattern: Pattern | None) -> None:
        self._pattern = pattern

    def element_from_hwnd(self, hwnd: int) -> object:
        return object()

    def pattern(self, element: object, name: str) -> Pattern:
        if self._pattern is None:
            raise LookupError("no Window pattern")
        return self._pattern


def _setup(monkeypatch, visible: bool, alive: bool = True):
    posted: list[int] = []
    monkeypatch.setattr(act_uia.w, "IsWindow", lambda h: alive)
    monkeypatch.setattr(act_uia.w, "IsWindowVisible", lambda h: visible)
    monkeypatch.setattr(act_uia.methods, "post", lambda h, msg, wp, lp: posted.append(msg))
    monkeypatch.setattr(act_uia.time, "sleep", lambda s: None)
    clock = iter(range(1000))
    monkeypatch.setattr(act_uia.time, "monotonic", lambda: next(clock) * 0.5)
    return posted


def test_lost_reply_is_never_replayed(monkeypatch):
    posted = _setup(monkeypatch, visible=True)
    pattern = Pattern(fail=True)
    state, how, detail = act_uia.request_close(Auto(pattern), 5)  # type: ignore[arg-type]
    assert state is None and pattern.calls == 1 and posted == [] and "reply lost" in detail


def test_already_hidden_window_proves_nothing(monkeypatch):
    _setup(monkeypatch, visible=False)
    state, _, detail = act_uia.request_close(Auto(Pattern(fail=False)), 5)  # type: ignore[arg-type]
    assert state is None and "already hidden" in detail


def test_destroyed_window_is_closed(monkeypatch):
    _setup(monkeypatch, visible=True, alive=False)
    state, how, _ = act_uia.request_close(Auto(Pattern(fail=False)), 5)  # type: ignore[arg-type]
    assert state is True and how == "UIA WindowPattern.Close"


def test_no_pattern_falls_back_to_wm_close(monkeypatch):
    posted = _setup(monkeypatch, visible=True)
    state, how, _ = act_uia.request_close(Auto(None), 5)  # type: ignore[arg-type]
    assert how == "WM_CLOSE" and posted == [act_uia.w.WM_CLOSE] and state is False
