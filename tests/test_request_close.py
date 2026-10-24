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


def test_stop_during_pattern_lookup_sends_nothing(monkeypatch):
    posted = _setup(monkeypatch, visible=True)
    pattern = Pattern(fail=False)

    def stopped() -> None:
        raise act_uia.worker.Stopped("Emergency stop")

    monkeypatch.setattr(act_uia.worker, "check_cancelled", stopped)
    try:
        act_uia.request_close(Auto(pattern), 5)  # type: ignore[arg-type]
    except act_uia.worker.Stopped:
        pass
    else:
        raise AssertionError("the stop must interrupt the close request")
    assert pattern.calls == 0 and posted == []


class _Target:
    """Juste ce que _close_target lit : hwnd et auto (relecture de Codex du 24/10)."""

    def __init__(self, pattern: Pattern) -> None:
        self.hwnd = 5
        self.auto = Auto(pattern)


def test_alt_f4_never_closes_an_explorer_window_with_several_tabs(monkeypatch):
    posted = _setup(monkeypatch, visible=True)
    pattern = Pattern(fail=False)
    monkeypatch.setattr(act_uia.w, "class_name", lambda h: "CabinetWClass")
    monkeypatch.setattr(act_uia.launcher, "explorer_tabs", lambda h: ["Documents", "Téléchargements"])
    monkeypatch.setattr(act_uia.w, "IsWindowEnabled", lambda h: True)
    try:
        act_uia.UiaActions._close_target(_Target(pattern), 'keys "alt+f4"')  # type: ignore[arg-type]
    except act_uia.ActionError as error:
        assert "2 tabs" in str(error) and "nothing was sent" in str(error)
    else:
        raise AssertionError("an Explorer window with several tabs must not be closed")
    assert pattern.calls == 0 and posted == []


def test_alt_f4_refused_on_a_window_disabled_by_a_modal_dialog(monkeypatch):
    posted = _setup(monkeypatch, visible=True)
    pattern = Pattern(fail=False)
    monkeypatch.setattr(act_uia.w, "class_name", lambda h: "#32770")
    monkeypatch.setattr(act_uia.w, "IsWindowEnabled", lambda h: False)
    try:
        act_uia.UiaActions._close_target(_Target(pattern), 'keys "alt+f4"')  # type: ignore[arg-type]
    except act_uia.ActionError as error:
        assert "disabled by an open dialog" in str(error)
    else:
        raise AssertionError("a disabled window must not be closed")
    assert pattern.calls == 0 and posted == []
