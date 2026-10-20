"""Relecture de Codex sur 0.15.6 (W1, W3, W4, W6, W7, W12, W14, W18, code de connexion sans le mot « code ») : un test
par faille corrigée, sans fenêtre réelle. W1 a aussi été vérifié dans un vrai Chromium."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from test_contract import FakeAuto, executor, make_view, node

from nexus_windows import (
    act_base,
    act_chromium,
    act_pointer,
    act_win32,
    actions,
    capture,
    keys,
    notifications,
    snapshot,
)
from nexus_windows import win32 as w


# --- W7 : Maj+lettre et raccourcis dans un mot de passe ----------------------------------------------------------
@pytest.mark.parametrize("spec", ["shift+a", "shift+1", "ctrl+v", "ctrl+a delete", "a"])
def test_keys_that_write_are_refused_in_a_password(monkeypatch, spec):
    field = node("Edit", "Mot de passe", has_value=True, password=True, focused=True)
    ex = executor(make_view(field), FakeAuto())
    monkeypatch.setattr(ex, "_confirm_keys", lambda *a: None)
    monkeypatch.setattr(ex, "_try_or_foreground", lambda *a: pytest.fail("a key reached the password field"))
    with pytest.raises(actions.ActionError, match="password field"):
        ex.press(spec, target="Mot de passe")


@pytest.mark.parametrize("spec", ["tab", "shift+tab", "enter", "escape"])
def test_navigation_keys_still_pass_in_a_password(monkeypatch, spec):
    field = node("Edit", "Mot de passe", has_value=True, password=True, focused=True)
    ex = executor(make_view(field), FakeAuto())
    monkeypatch.setattr(ex, "_confirm_keys", lambda *a: None)
    monkeypatch.setattr(ex, "_try_or_foreground", lambda *a: actions.Outcome(True, "keys", "messages"))
    assert ex.press(spec, target="Mot de passe").ok


# --- W1 : un champ dans un éditeur riche passe avant l'éditeur ----------------------------------------------------
def test_input_inside_rich_editor_is_targeted_before_the_editor():
    scripts: list[str] = []
    page = SimpleNamespace(evaluate=lambda js: scripts.append(js) or "ok")
    act_chromium.ChromiumActions._cdp_focus_at(page, 0, 0, editable=True)  # type: ignore[arg-type]
    script = scripts[0]
    assert script.index("if (box(n)) el = n") < script.index("hit.isContentEditable")
    assert "a.isContentEditable && !box(a)" in script  # pas d'exigence de sélection d'éditeur pour le champ


# --- W6 : focus relu avant Suppr et à chaque tranche --------------------------------------------------------------
def _real_writer(monkeypatch, move_focus_on: str):
    state = {"focus": "A"}
    sent: list[str] = []

    def press(combo):
        name = "ctrl+a" if combo.key == 0x41 else "delete" if combo.key == 0x2E else "other"
        sent.append(name)
        if name == move_focus_on:
            state["focus"] = "B"

    monkeypatch.setattr(act_pointer.realinput, "click", lambda *a, **k: None)
    monkeypatch.setattr(act_pointer.realinput, "press", press)
    monkeypatch.setattr(act_pointer.realinput, "type_text", lambda text: sent.append(f"text:{len(text)}"))
    monkeypatch.setattr(act_pointer.time, "sleep", lambda s: None)
    fake = SimpleNamespace(
        _point_of=lambda e: (1, 1), _focus_reaches=lambda e, wait_s=0, real=False: state["focus"] == "A"
    )
    field = SimpleNamespace(node=object(), describe=lambda: '"A"')
    return fake, field, sent


def test_real_write_stops_when_ctrl_a_moves_the_focus(monkeypatch):
    fake, field, sent = _real_writer(monkeypatch, move_focus_on="ctrl+a")
    with pytest.raises(actions.ActionError, match="lost the focus before Delete"):
        act_pointer.PointerActions._real_write(fake, field, "new", True)  # type: ignore[arg-type]
    assert sent == ["ctrl+a"]


def test_real_write_types_in_chunks_with_focus_read_back(monkeypatch):
    fake, field, sent = _real_writer(monkeypatch, move_focus_on="")
    text = "x" * (act_pointer.REAL_TYPING_CHUNK * 2 + 5)
    act_pointer.PointerActions._real_write(fake, field, text, True)  # type: ignore[arg-type]
    chunk = act_pointer.REAL_TYPING_CHUNK
    assert sent == ["ctrl+a", "delete", f"text:{chunk}", f"text:{chunk}", "text:5"]


# --- W14 : touches simulées vers la fenêtre de l'élément (dialogue) ----------------------------------------------
def test_simulated_combos_go_to_the_elements_window(monkeypatch):
    monkeypatch.setattr(w, "root", lambda h: h)
    posted: list[int] = []
    monkeypatch.setattr(act_win32.methods, "post_combo", lambda h, *a, **k: posted.append(h))
    ex = SimpleNamespace(hwnd=100, windows={100, 200})
    ex._sim_top = lambda e=None: act_chromium.ChromiumActions._sim_top(ex, e)  # type: ignore[arg-type]
    dialog = SimpleNamespace(window=200)
    act_win32.Win32Actions._post_combos(ex, keys.parse_sequence("ctrl+w"), "Chromium", [], True, dialog)  # type: ignore[arg-type]
    assert posted == [200]


# --- W4 et W3 : premier plan de l'utilisateur jamais repris --------------------------------------------------------
def test_give_back_leaves_a_window_the_user_already_chose(monkeypatch):
    from nexus_windows import foreground

    monkeypatch.setattr(w, "foreground", lambda: 30)  # l'utilisateur est déjà passé à 30
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(w, "owner", lambda h: 0)
    monkeypatch.setattr(foreground, "_switch_to", lambda *a, **k: pytest.fail("switch over the user's choice"))
    assert foreground.give_back(10, exclude={20}) is False


def test_switch_to_keeps_the_holder_seen_by_the_caller(monkeypatch):
    """W4 : la cible 20 tenait le premier plan à la lecture de l'appelant ; l'utilisateur prend 30 juste après."""
    from nexus_windows import foreground

    monkeypatch.setattr(w, "foreground", lambda: 30)
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(w, "owner", lambda h: 0)
    monkeypatch.setattr(foreground, "_attempt", lambda *a: pytest.fail("SetForegroundWindow over the user's choice"))
    assert foreground._switch_to(10, 1.0, {20}, expected=20) is False


def test_activate_from_empty_foreground_never_asks_the_users_window(monkeypatch):
    """W3 : premier plan vide au départ, puis la fenêtre 30 de l'utilisateur : on ne lui demande pas de céder."""
    from nexus_windows import foreground

    fronts = iter([0])
    monkeypatch.setattr(w, "foreground", lambda: next(fronts, 30))
    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    monkeypatch.setattr(w, "IsIconic", lambda h: False)
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(w, "owner", lambda h: 0)
    monkeypatch.setattr(foreground, "_checkpoint", lambda: None)
    monkeypatch.setattr(foreground, "_switch_to", lambda *a, **k: False)
    monkeypatch.setattr(foreground, "_ask_holder", lambda h: pytest.fail("asked the user's window to give way"))
    assert foreground.activate(20) is False


# --- W12 : liste virtualisée, nom exact exigé ----------------------------------------------------------------------
def test_realized_item_must_match_exactly(monkeypatch):
    lst = SimpleNamespace(control_type=next(iter(act_base.LIST_ROLES)), element=object())
    root = SimpleNamespace(walk=lambda: [lst])
    near = SimpleNamespace(describe=lambda: '"Bluetooth et appareils"')
    monkeypatch.setattr(snapshot, "pick", lambda view, spec, prefer=None: (near, "partial match"))
    monkeypatch.setattr(act_base.time, "sleep", lambda s: None)
    fake = SimpleNamespace(
        view=SimpleNamespace(roots={1: root}),
        hwnd=1,
        auto=SimpleNamespace(realize_named=lambda element, name: True),
        _rebuild=lambda: None,
    )
    with pytest.raises(actions.ActionError, match="Bluetooth"):
        act_base.ExecutorBase._realize_named(fake, "Bluetooth", None, snapshot.TargetError('"Bluetooth" not found'))  # type: ignore[arg-type]


# --- W18 : PrintWindow borné même après un WM_NULL réussi -----------------------------------------------------------
def test_printwindow_that_hangs_is_abandoned_without_freeing_its_bitmap(monkeypatch):
    release = threading.Event()
    freed: list[int] = []
    monkeypatch.setattr(capture, "PRINTWINDOW_TIMEOUT_S", 0.1)
    monkeypatch.setattr(w, "IsIconic", lambda h: False)
    monkeypatch.setattr(w, "window_rect", lambda h: w.Rect(0, 0, 10, 10))
    monkeypatch.setattr(w, "frame_rect", lambda h: w.Rect(0, 0, 10, 10))
    monkeypatch.setattr(w, "responsive", lambda h: True)  # répond au WM_NULL, puis se fige
    monkeypatch.setattr(w, "window_dpi_scale", lambda h: 1.0)
    monkeypatch.setattr(capture, "_dib", lambda width, height: (1, 2, 3, 4))
    monkeypatch.setattr(capture, "_free", lambda *a: freed.append(1))
    monkeypatch.setattr(w, "PrintWindow", lambda *a: release.wait(5) or True)
    start = time.monotonic()
    with pytest.raises(capture.CaptureError, match="stopped responding"):
        capture.capture_printwindow(5)
    assert time.monotonic() - start < 1.0 and freed == []  # bitmap jamais libéré sous le fil qui dessine
    release.set()
    for _ in range(100):
        if freed:
            break
        time.sleep(0.01)
    assert freed == [1]  # libéré par le fil quand l'application répond enfin


# --- Notifications : code de connexion sans le mot « code » ---------------------------------------------------------
def test_sign_in_code_without_the_word_code_is_masked():
    assert "739201" not in notifications.mask_codes("Entrez 739201 pour vous connecter")
    assert "482913" not in notifications.mask_codes("Enter 482913 to continue")
