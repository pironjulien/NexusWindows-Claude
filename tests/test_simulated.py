"""Activation simulée (Chromium sans DevTools, sans activation réelle) et lancement d'Edge qui se relance.

Mesures à l'origine (bench/chromium_input_probe.py, bench/chromium_sim_live.py, 15 octobre 2026) : WM_NCACTIVATE et
WM_ACTIVATE envoyés à la seule fenêtre Chromium suffisent pour que WM_CHAR produise des événements de confiance, sans
changement du premier plan ; la désactivation doit être POSTÉE (des messages envoyés passeraient devant les
caractères encore en file) ; la toute première fois par processus peut utiliser son droit de premier plan de
lancement (≈10 ms, rendu) : jeton commun et pause de Julien exigés alors, et seulement alors.
"""

from __future__ import annotations

import contextlib
import dataclasses
import time

import pytest
from test_contract import FakeAuto, executor, make_view, node
from test_fix_c import desktop  # noqa: F401  (fixture du bureau simulé)

from nexus_windows import actions, foreground, launch, methods, uia

TOP = 4242  # fenêtre principale des vues de test_contract


@pytest.fixture(autouse=True)
def no_web_flush(monkeypatch):
    """La publication de l'arbre Chromium (ScrollIntoView du document) n'a pas d'objet sur un UIA simulé."""
    monkeypatch.setattr(actions.snapshot, "flush_web", lambda auto, hwnd: None)


@pytest.fixture
def chromium_window(monkeypatch):
    """Une fenêtre Chromium de premier niveau, inactive (Julien est ailleurs), messages relevés."""
    state = {"fg": 999, "messages": [], "pause": True, "tokens": [], "pid": 55}
    monkeypatch.setattr(methods, "_PRIMED", set())
    monkeypatch.setattr(methods, "SIM_SETTLE_S", 0.0)
    monkeypatch.setattr(methods.w, "IsWindow", lambda h: True)
    monkeypatch.setattr(methods.w, "class_name", lambda h: "Chrome_WidgetWin_1")
    monkeypatch.setattr(methods.w, "foreground", lambda: state["fg"])
    monkeypatch.setattr(methods.w, "root", lambda h: h)
    monkeypatch.setattr(methods.w, "window_pid", lambda h: state["pid"])

    def send(hwnd, msg, wparam=0, lparam=0, timeout_ms=1000):
        state["messages"].append(("envoyé", msg, wparam))
        return 0

    monkeypatch.setattr(methods.w, "send_message", send)
    monkeypatch.setattr(
        methods, "post", lambda hwnd, msg, wparam, lparam: state["messages"].append(("posté", msg, wparam))
    )
    monkeypatch.setattr(methods.foreground, "wait_typing_pause", lambda *a, **k: state["pause"])

    @contextlib.contextmanager
    def token(purpose):
        state["tokens"].append(purpose)
        yield

    monkeypatch.setattr(methods.foreground, "agent_token", token)
    return state


ACTIVATE = [("envoyé", methods.WM_NCACTIVATE, 1), ("envoyé", methods.WM_ACTIVATE, methods.WA_ACTIVE)]
DEACTIVATE = [("posté", methods.WM_ACTIVATE, methods.WA_INACTIVE), ("posté", methods.WM_NCACTIVATE, 0)]


def test_activation_sent_then_deactivation_posted_after_the_keys(chromium_window):
    with methods.simulated_activation(TOP, {TOP}):
        methods.post(TOP, 0x0102, ord("a"), 1)  # un caractère posté dans le bloc
    assert chromium_window["messages"] == [*ACTIVATE, ("posté", 0x0102, ord("a")), *DEACTIVATE]


def test_first_time_per_process_waits_for_julien_and_the_token(chromium_window):
    with methods.simulated_activation(TOP, {TOP}):
        pass
    assert len(chromium_window["tokens"]) == 1
    chromium_window["pause"] = False  # Julien tape : sans importance une fois le processus amorcé
    with methods.simulated_activation(TOP, {TOP}):
        pass
    assert len(chromium_window["tokens"]) == 1  # plus de jeton ni d'attente : rien ne peut plus prendre la main


def test_first_time_while_julien_types_sends_nothing(chromium_window):
    chromium_window["pause"] = False
    with pytest.raises(foreground.BusyUser), methods.simulated_activation(TOP, {TOP}):
        pytest.fail("bloc exécuté")
    assert chromium_window["messages"] == [] and chromium_window["pid"] not in methods._PRIMED


def test_refused_outside_chromium_or_when_already_in_front(chromium_window, monkeypatch):
    chromium_window["fg"] = TOP  # Julien a choisi la fenêtre : la frappe ordinaire s'applique
    with pytest.raises(methods.MethodError, match="foreground"), methods.simulated_activation(TOP, {TOP}):
        pass
    monkeypatch.setattr(methods.w, "class_name", lambda h: "SDL_app")  # Steam : CEF hors d'atteinte des messages
    with pytest.raises(methods.MethodError, match="Chromium"), methods.simulated_activation(TOP, {TOP}):
        pass
    assert chromium_window["messages"] == []


def test_julien_choosing_the_window_keeps_it_active_and_stops_typing(chromium_window, monkeypatch):
    monkeypatch.setattr(methods.physical, "available", lambda: True)
    monkeypatch.setattr(methods.physical, "since", lambda moment: True)
    with methods.simulated_activation(TOP, {TOP}) as sim:
        sim.checkpoint()  # Julien tape dans SA fenêtre : rien à interrompre
        chromium_window["fg"] = TOP  # il clique dans la cible et tape
        with pytest.raises(foreground.UserInterrupted):
            sim.checkpoint()
    assert chromium_window["messages"] == ACTIVATE  # jamais désactivée sous ses doigts


def test_slices_in_simulated_mode_never_activate(monkeypatch):
    field: list[str] = []
    monkeypatch.setattr(methods.foreground, "brief_activation", lambda *a, **k: pytest.fail("activation brève"))
    monkeypatch.setattr(methods, "_post_char", lambda target, ch, enter_as_key: field.append(ch))

    class Sim:
        def check(self, consequence):
            pass

        def checkpoint(self):
            pass

    typed = methods.type_in_slices(
        TOP, {TOP}, "ab\ncd", lambda first: TOP, lambda: "".join(field),
        lambda value: methods.typed_prefix(value, "ab\ncd"), enter_as_key=True, slice_s=methods.SIM_SLICE_S,
        activation=lambda: contextlib.nullcontext(Sim()),
    )  # fmt: skip
    assert typed.complete and "".join(field) == "ab\ncd" and "without activation" in typed.detail()


def _chromium_view(*nodes):
    return dataclasses.replace(make_view(*nodes), framework="Chromium")


def test_multiline_chromium_tries_simulated_typing_first_then_falls_back(monkeypatch):
    notes = node("Edit", "Notes", value="", framework="Chrome", has_legacy=True)
    ex = executor(_chromium_view(notes), FakeAuto())
    calls: list[bool] = []

    def keystrokes(e, text, replace, simulated=False):
        calls.append(simulated)
        if simulated:
            raise methods.MethodError("focus without activation not read back on the element: nothing was typed")
        notes.element.live["value"] = text
        return "WM_CHAR keystrokes, brief activation 120 ms"

    monkeypatch.setattr(ex, "_keystrokes", keystrokes)
    outcome = ex.write("Notes", "ligne 1\nligne 2", replace=True)
    assert calls == [True, False] and outcome.ok and outcome.verified
    assert "keystrokes without activation ✗" in "; ".join(outcome.attempts)


def test_multiline_chromium_written_without_any_activation(monkeypatch):
    notes = node("Edit", "Notes", value="", framework="Chrome", has_legacy=True)
    ex = executor(_chromium_view(notes), FakeAuto())
    calls: list[bool] = []

    def keystrokes(e, text, replace, simulated=False):
        calls.append(simulated)
        notes.element.live["value"] = text
        return "WM_CHAR keystrokes without activation (activation simulated by messages, the user's foreground intact)"

    monkeypatch.setattr(ex, "_keystrokes", keystrokes)
    outcome = ex.write("Notes", "texte", replace=True)
    assert calls == [True] and outcome.ok and outcome.method == "keystrokes without activation"


def test_take_focus_is_read_back(monkeypatch):
    field = node("Edit", "Notes", framework="Chrome")
    auto = FakeAuto()
    monkeypatch.setattr(auto, "take_focus", lambda element: element.live.update(focused=True), raising=False)
    ex = executor(_chromium_view(field), auto)
    ex._take_focus(ex.resolve("Notes")[1])
    monkeypatch.setattr(auto, "take_focus", lambda element: None)  # accSelect accepté sans effet
    field.element.live["focused"] = False
    with pytest.raises(methods.MethodError, match="not read back"):
        ex._take_focus(ex.resolve("Notes")[1], timeout_s=0.05)

    def refused(element):
        raise uia.UIAError("motif Legacy absent")

    monkeypatch.setattr(auto, "take_focus", refused)
    with pytest.raises(methods.MethodError, match="impossible"):
        ex._take_focus(ex.resolve("Notes")[1])


def test_chromium_shortcut_without_devtools_goes_through_simulated_activation(monkeypatch):
    page = node("Document", "Labo", framework="Chrome")
    ex = executor(_chromium_view(page), FakeAuto())
    monkeypatch.setattr(ex, "_focused_target_element", lambda: None)
    monkeypatch.setattr(
        ex, "_sim_activation", lambda e=None: contextlib.nullcontext(type("S", (), {"check": lambda s, c: None})())
    )
    monkeypatch.setattr(ex, "_sim_top", lambda e=None: TOP)
    combos: list[tuple] = []
    monkeypatch.setattr(
        actions.methods, "post_combo", lambda t, vks, mods, hold_s=0.15: combos.append((vks, mods, hold_s))
    )
    prints = iter([("avant",), ("après",)])
    monkeypatch.setattr(ex, "_fingerprint", lambda: next(prints))
    outcome = ex.press("ctrl+k")
    assert outcome.ok and "simulated activation" in outcome.line
    assert combos == [([0x4B], [0x11], methods.SIM_MODIFIERS_HOLD_S)]


def test_chromium_shortcut_refusal_unchanged_when_simulation_is_impossible(monkeypatch):
    page = node("Document", "Labo", framework="Chrome")
    ex = executor(_chromium_view(page), FakeAuto())
    monkeypatch.setattr(ex, "_focused_target_element", lambda: None)

    def impossible(e=None):
        raise methods.MethodError("not a top-level Chromium window: simulated activation not applicable")

    monkeypatch.setattr(ex, "_sim_activation", impossible)
    with pytest.raises(actions.NeedsForeground, match="devtools=true"):
        ex.press("ctrl+k")


# --- Lancement : Edge qui se relance (mesuré, Edge 155) ------------------------------------------------------------
def test_new_window_rereads_the_tree_as_soon_as_the_launcher_left(desktop, monkeypatch):  # noqa: F811
    """Le lanceur relance le navigateur puis se termine en moins de 0,3 s : l'arbre relu à la cadence seule datait
    d'avant l'enfant, le lancement échouait en 0,5 s et laissait le navigateur orphelin ouvert."""
    reads = {"n": 0}

    def tree(pid):
        reads["n"] += 1
        return {pid} if reads["n"] == 1 else {pid, 4242}  # enfant visible à la relecture suivante seulement

    monkeypatch.setattr(launch, "descendants", tree)
    alive = {"v": True}

    def running():
        result = alive["v"]
        alive["v"] = False  # parti juste après le premier relevé
        return result

    desktop["windows"][901] = (4242, "Labo Web")
    hwnd, _ = launch._new_window(set(), 1111, "", "msedge", time.monotonic() + 2, 0, fallback=False, running=running)
    assert hwnd == 901


def test_kill_tree_names_every_descendant(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(launch, "descendants", lambda pid: {pid, 20, 30})
    monkeypatch.setattr(launch.subprocess, "run", lambda command, **k: commands.append(command))
    launch.kill_tree(10)
    assert commands and all(f"/PID {p}" in " ".join(commands[0]) for p in (10, 20, 30))
