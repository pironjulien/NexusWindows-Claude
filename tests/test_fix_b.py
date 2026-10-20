"""Contrats du lot B de la contre-vérification n° 4 (garde du premier plan, activation brève, arrêt d'urgence,
serveur dépassé, raccourcis) : chaque constat a son test, sans fenêtre réelle (Win32 simulé)."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from nexus_windows import foreground, freshness, keys, methods, physical, realinput, server, stop, worker

TARGET, JULIEN, OTHER = 100, 200, 300
VK = foreground


@pytest.fixture(autouse=True)
def no_real_stop(tmp_path, monkeypatch):
    """Jamais le vrai fichier d'arrêt ni le vrai battement : un arrêt pressé par Julien ne fausse pas ces tests."""
    monkeypatch.setattr(stop, "STOP_FILE", tmp_path / "stop")
    monkeypatch.setattr(stop, "OWNER_FILE", tmp_path / "stop.owner")
    monkeypatch.setattr(stop, "_latched_until", 0.0)
    monkeypatch.setattr(stop.log, "event", lambda *a, **k: None)


class Desk:
    """Bureau simulé : premier plan, touches enfoncées, bouton, classes ; restitutions enregistrées."""

    def __init__(self, monkeypatch, fg: int = JULIEN) -> None:
        self.fg = fg
        self.keys: set[int] = set()
        self.button = False
        self.over = JULIEN
        self.classes: dict[int, str] = {}
        self.cloaked: set[int] = set()
        self.gave_back: list[int] = []
        self.idle_ms = 60_000
        m = monkeypatch
        m.setattr(foreground.w, "foreground", lambda: self.fg)
        m.setattr(foreground.w, "enum_windows", lambda: [JULIEN, TARGET, OTHER])
        m.setattr(foreground.w, "GetAsyncKeyState", self._key_state)
        m.setattr(foreground.w, "mouse_button_down", lambda: self.button)
        m.setattr(foreground.w, "cursor_pos", lambda: (0, 0))
        m.setattr(foreground.w, "window_at", lambda x, y: self.over)
        m.setattr(foreground.w, "class_name", lambda h: self.classes.get(h, "Fenetre"))
        m.setattr(foreground.w, "is_cloaked", lambda h: h in self.cloaked)
        m.setattr(foreground.w, "IsWindowVisible", lambda h: True)
        m.setattr(foreground, "target_windows", lambda h, extra=None: {TARGET})
        m.setattr(foreground, "target_pids", lambda h, extra=None: {7})
        m.setattr(foreground, "belongs_to", lambda fg, windows, pids, known=None: fg in windows)
        m.setattr(foreground, "user_idle_ms", lambda: self.idle_ms)
        m.setattr(foreground, "give_back", self._give_back)
        m.setattr(foreground.physical, "available", lambda: True)  # jamais les vraies entrées de Julien
        m.setattr(foreground.physical, "since", lambda moment: False)

    def _key_state(self, vk: int) -> int:
        """Comme Windows : la touche générique (VK_MENU, VK_CONTROL, VK_SHIFT) est enfoncée avec sa gauche ou sa
        droite — AltGr enfonce donc VK_MENU."""
        sides = {0x10: {0xA0, 0xA1}, 0x11: {0xA2, 0xA3}, 0x12: {0xA4, 0xA5}}.get(vk, set())
        return 0x8000 if vk in self.keys or sides & self.keys else 0

    def _give_back(self, prev, timeout_s=0.6, exclude=None) -> bool:
        self.gave_back.append(prev)
        self.fg = prev
        return True


# --- keys.parse : touche manquante ------------------------------------------------------------------------------
@pytest.mark.parametrize("combo", ["ctrl+", "ctrl+shift+", "+a", "a+", "++", "ctrl++shift", "ctrl + "])
def test_keys_parse_refuses_missing_key(combo):
    with pytest.raises(keys.KeyParseError):
        keys.parse(combo)


def test_keys_parse_plus_key():
    plus = keys.parse("+")
    assert plus.modifiers == () and plus.char == "+"
    assert keys.parse("ctrl++") == keys.Combo((0x11,), plus.key, "+")
    assert keys.parse("ctrl+shift++").modifiers == (0x11, 0x10)
    assert keys.parse("ctrl + s") == keys.Combo((0x11,), ord("S"), "s")


# --- AltGr n'est pas un Alt+Tab ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("pressed", "switching"),
    [
        ({VK.VK_LCONTROL, VK.VK_RMENU}, False),  # AltGr (AZERTY : @ # { } [ ] | € ~)
        ({VK.VK_LCONTROL}, False),
        (set(), False),
        ({VK.VK_LMENU}, True),
        ({VK.VK_RMENU}, True),  # Alt droit sans Ctrl gauche : un vrai Alt
        ({VK.VK_LWIN}, True),
        ({VK.VK_RWIN}, True),
    ],
)
def test_user_is_switching_ignores_altgr(monkeypatch, pressed, switching):
    desk = Desk(monkeypatch)
    desk.keys = pressed
    assert foreground.user_is_switching() is switching


def test_steal_during_altgr_is_given_back(monkeypatch):
    """Test A : vol pendant AltGr tenu → rendu (avant : adopté en silence au relâchement)."""
    desk = Desk(monkeypatch)
    watch = foreground.Watch(TARGET)
    desk.keys = {VK.VK_LCONTROL, VK.VK_RMENU}
    desk.fg = TARGET
    watch._check()
    assert desk.gave_back == [JULIEN] and watch.stolen == 1


def test_stale_alt_flag_does_not_adopt_a_later_steal(monkeypatch):
    """Test B : drapeau Alt levé, Julien passe ailleurs, puis vol sans aucune touche → rendu."""
    desk = Desk(monkeypatch)
    watch = foreground.Watch(TARGET)
    desk.keys, desk.fg = {VK.VK_LMENU}, TARGET  # la cible s'active pendant un Alt de Julien
    watch._check()
    assert watch._alt_pending
    desk.fg = OTHER  # Julien finit son Alt+Tab ailleurs : drapeau effacé
    watch._check()
    assert not watch._alt_pending and watch.front == OTHER
    desk.keys, desk.fg = set(), TARGET  # vol, aucune touche
    watch._check()
    assert desk.gave_back == [OTHER]


def test_alt_flag_cleared_after_give_back(monkeypatch):
    desk = Desk(monkeypatch)
    watch = foreground.Watch(TARGET)
    watch._alt_pending = True
    desk.button, desk.over, desk.fg = True, JULIEN, TARGET  # bouton tenu dans SA fenêtre : vol
    watch._check()
    assert desk.gave_back == [JULIEN] and not watch._alt_pending


# --- Alt+Tab et barre des tâches de Julien vers la cible : jamais annulés -----------------------------------------
@pytest.mark.parametrize("switcher", ["MultitaskingViewFrame", "XamlExplorerHostIslandWindow", "TaskSwitcherWnd"])
def test_alt_tab_switcher_first_then_target(monkeypatch, switcher):
    desk = Desk(monkeypatch)
    desk.classes[OTHER] = switcher
    watch = foreground.Watch(TARGET)
    desk.keys, desk.fg = {VK.VK_LMENU}, OTHER  # sélecteur au premier plan pendant le geste
    watch._check()
    desk.keys, desk.fg = set(), TARGET  # relâché : Windows active la fenêtre choisie
    watch._check()
    assert desk.gave_back == [] and watch.front == TARGET


def test_alt_tab_target_first_then_release(monkeypatch):
    desk = Desk(monkeypatch)
    watch = foreground.Watch(TARGET)
    desk.keys, desk.fg = {VK.VK_LMENU}, TARGET  # cible active pendant qu'Alt est tenu
    watch._check()
    desk.keys = set()
    watch._check()
    assert desk.gave_back == [] and watch.front == TARGET


def test_taskbar_click_then_target(monkeypatch):
    desk = Desk(monkeypatch)
    desk.classes[OTHER] = "Shell_TrayWnd"
    watch = foreground.Watch(TARGET)
    desk.button, desk.over, desk.fg = True, OTHER, OTHER  # bouton enfoncé sur la barre des tâches
    watch._check()
    desk.button, desk.fg = False, TARGET  # relâché : la cible s'active
    watch._check()
    assert desk.gave_back == [] and watch.front == TARGET


def test_switcher_then_julien_window_then_steal_is_given_back(monkeypatch):
    desk = Desk(monkeypatch)
    desk.classes[OTHER] = "MultitaskingViewFrame"
    watch = foreground.Watch(TARGET)
    desk.fg = OTHER
    watch._check()
    desk.fg = JULIEN  # Julien choisit SA fenêtre
    watch._check()
    desk.fg = TARGET  # la cible vole dans la seconde : pas un choix de Julien
    watch._check()
    assert desk.gave_back == [JULIEN]


def test_old_switcher_does_not_excuse_a_steal(monkeypatch):
    desk = Desk(monkeypatch)
    desk.classes[OTHER] = "MultitaskingViewFrame"
    watch = foreground.Watch(TARGET)
    desk.fg = OTHER
    watch._check()
    desk.fg = 0  # plus de premier plan pendant plus d'une seconde
    watch._check()
    assert watch._switcher_seen is not None
    watch._switcher_seen -= 2 * foreground.SWITCH_WINDOW_S
    desk.fg = TARGET
    watch._check()
    assert watch.stolen == 1 and len(desk.gave_back) == 1


@pytest.mark.parametrize("kind", ["switcher", "cloaked", "invisible"])
def test_give_back_never_targets_shell_or_hidden_window(monkeypatch, kind):
    calls: list[int] = []
    monkeypatch.setattr(foreground.w, "foreground", lambda: TARGET)
    monkeypatch.setattr(foreground.w, "IsWindow", lambda h: True)
    monkeypatch.setattr(foreground.w, "IsIconic", lambda h: False)
    monkeypatch.setattr(foreground.w, "IsWindowVisible", lambda h: kind != "invisible")
    monkeypatch.setattr(foreground.w, "is_cloaked", lambda h: kind == "cloaked")
    monkeypatch.setattr(foreground.w, "class_name", lambda h: "Shell_TrayWnd" if kind == "switcher" else "Fenetre")
    monkeypatch.setattr(foreground, "_next_user_window", lambda exclude: JULIEN)
    monkeypatch.setattr(foreground, "_switch_to", lambda hwnd, timeout_s, ours=None: calls.append(hwnd) or True)
    assert foreground.give_back(OTHER, exclude={TARGET})
    assert calls == [JULIEN]


# --- Premier plan nul : la garde démarre quand même --------------------------------------------------------------
def test_watch_starts_when_foreground_is_null(monkeypatch):
    desk = Desk(monkeypatch, fg=0)
    with foreground.Watch(TARGET) as watch:
        assert watch._thread is not None
        desk.fg = JULIEN  # Julien revient sur sa fenêtre…
        time.sleep(0.08)
        desk.fg = TARGET  # …puis la cible la lui vole
        deadline = time.monotonic() + 2
        while not desk.gave_back and time.monotonic() < deadline:
            time.sleep(0.01)
    watch._thread.join(2)
    assert desk.gave_back == [JULIEN] and watch.stolen == 1


def test_brief_activation_refuses_null_or_system_foreground(monkeypatch):
    desk = Desk(monkeypatch, fg=0)
    with pytest.raises(foreground.BusyUser, match="held by no window"):
        with foreground.brief_activation(TARGET, {TARGET}):
            pytest.fail("activation sans fenêtre à qui rendre le premier plan")
    desk.fg = OTHER
    desk.cloaked = {OTHER}
    monkeypatch.setattr(foreground.w, "window_text", lambda h: "Recherche")
    with pytest.raises(foreground.BusyUser, match="foreground is held by hidden system window"):
        with foreground.brief_activation(TARGET, {TARGET}):
            pytest.fail("activation avec une fenêtre système masquée au premier plan")


# --- Pause de Julien : bouton tenu, durée minimale -----------------------------------------------------------------
def test_held_mouse_button_is_not_a_pause(monkeypatch):
    desk = Desk(monkeypatch)
    desk.button = True
    assert not foreground.wait_typing_pause(max_wait_s=0.05)
    real = foreground.wait_typing_pause
    monkeypatch.setattr(foreground, "wait_typing_pause", lambda: real(max_wait_s=0.05))
    with pytest.raises(foreground.BusyUser, match="holding a button"):
        with foreground.brief_activation(TARGET, {TARGET}):
            pytest.fail("activation pendant un glisser de Julien")


def test_pause_lasts_at_least_one_second(monkeypatch):
    desk = Desk(monkeypatch)
    assert foreground.QUIET_S >= 1.0
    assert inspect.signature(foreground.wait_typing_pause).parameters["quiet_s"].default == foreground.QUIET_S
    desk.idle_ms = 900
    assert not foreground.wait_typing_pause(max_wait_s=0.05)
    desk.idle_ms = 1000
    assert foreground.wait_typing_pause(max_wait_s=0.05)


# --- Interruption pendant les frappes et l'attente ------------------------------------------------------------------
def _interrupt_after(monkeypatch, posts: list, count: int) -> None:
    monkeypatch.setattr(foreground.physical, "available", lambda: True)
    monkeypatch.setattr(foreground.physical, "since", lambda moment: len(posts) >= count)


def test_physical_input_stops_post_chars_at_once(monkeypatch):
    desk = Desk(monkeypatch)
    posts: list[tuple] = []
    monkeypatch.setattr(methods, "post", lambda *a: posts.append(a))
    _interrupt_after(monkeypatch, posts, 3)
    with pytest.raises(foreground.UserInterrupted, match="foreground given back"):
        with foreground.brief_activation(TARGET, {TARGET}):
            desk.fg = TARGET  # la cible s'active
            methods.post_chars(TARGET, "abcdefghij")
    assert len(posts) == 3  # rien après la première entrée de Julien
    assert desk.gave_back == [JULIEN]  # rendu sur-le-champ, une seule fois


def test_checkpoints_interrupt_waits_during_activation(monkeypatch):
    """_settle et tout point de contrôle du thread bureau (worker.check_cancelled) arrêtent l'activation."""
    desk = Desk(monkeypatch)
    posts: list = []
    _interrupt_after(monkeypatch, posts, 0)
    worker.check_cancelled()  # hors activation : rien
    reached: list[str] = []
    with pytest.raises(foreground.UserInterrupted, match="typing stopped and foreground given back"):
        with foreground.brief_activation(TARGET, {TARGET}):
            desk.fg = TARGET
            worker.check_cancelled()  # comme chaque tour de _settle
            reached.append("suite de l'attente")
    assert reached == [] and desk.gave_back == [JULIEN]
    worker.check_cancelled()  # activation terminée : plus rien en cours sur ce fil


def test_interruption_fallback_without_raw_input(monkeypatch):
    ticks = {"now": 1000, "last": 500}
    monkeypatch.setattr(foreground.physical, "available", lambda: False)
    monkeypatch.setattr(foreground.w, "GetTickCount", lambda: ticks["now"])
    monkeypatch.setattr(foreground.w, "last_input_tick", lambda: ticks["last"])
    monkeypatch.setattr(foreground, "_OWN_INPUT", (0, 0))
    activation = foreground.Activation(time.monotonic())  # début au tick 1000
    ticks["now"] = 1500
    assert not activation.interrupted()  # entrée antérieure au début
    ticks["last"] = 1000
    assert not activation.interrupted()
    ticks["last"] = 1200
    assert activation.interrupted()  # entrée pendant l'activation
    monkeypatch.setattr(foreground, "_OWN_INPUT", (1150, 1300))
    assert not activation.interrupted()  # notre propre entrée datée


# --- Frappe par tranches -------------------------------------------------------------------------------------------
class App:
    """Application simulée : lit les caractères postés à PER_CHAR s chacun, à partir du début de chaque tranche."""

    PER_CHAR = 0.0005

    def __init__(self, monkeypatch, desk: Desk) -> None:
        self.desk = desk
        self.typed = ""
        self.log: list[tuple] = []
        self.batch_start = 0.0
        self.batch = 0
        self.slices: list[list[float]] = []
        self.pauses = 0
        monkeypatch.setattr(methods, "post", self._post)
        real = foreground.wait_typing_pause

        def pause() -> bool:
            self.pauses += 1
            return real(max_wait_s=0.05)

        monkeypatch.setattr(foreground, "wait_typing_pause", pause)
        give_back = desk._give_back

        def end_slice(prev, timeout_s=0.6, exclude=None) -> bool:
            self.slices[-1].append(time.monotonic())
            return give_back(prev, timeout_s, exclude)

        monkeypatch.setattr(foreground, "give_back", end_slice)

    def _post(self, target, msg, wparam, lparam) -> None:
        if msg == methods.w.WM_CHAR:
            self.typed += chr(wparam)
        elif msg == methods.w.WM_KEYDOWN and wparam == 0x0D:
            self.typed += "\n"
        else:
            return
        self.log.append(("post", self.typed[-1]))
        if not self.batch:
            self.batch_start = time.monotonic()
        self.batch += 1

    def prepare(self, first: bool) -> int:
        self.slices.append([time.monotonic()])
        self.desk.fg = TARGET
        return TARGET

    def progress(self) -> int:
        """Début du texte relu (lot T : progress() remplace consumed(préfixe)) : le lot en cours, une fois lu."""
        done = time.monotonic() >= self.batch_start + self.PER_CHAR * self.batch
        k = len(self.typed) if done else len(self.typed) - self.batch
        self.log.append(("read", k))
        if done:
            self.batch = 0
        return k


def as_reader(progress):
    """(read, prefix) de type_in_slices à partir d'un progress() simulé (début relu) : la valeur relue est ce compte."""
    return (lambda: f"#{progress()}"), (lambda value: int(value[1:]))


def test_long_text_is_typed_in_short_slices(monkeypatch):
    desk = Desk(monkeypatch)
    app = App(monkeypatch, desk)
    text = "".join(chr(ord("a") + i % 26) for i in range(400))
    typed = methods.type_in_slices(TARGET, {TARGET}, text, app.prepare, *as_reader(app.progress), slice_s=0.1)
    assert typed.complete and app.typed == text
    assert typed.slices > 1 and len(app.slices) == typed.slices
    assert app.pauses == typed.slices  # pause de Julien attendue avant chaque tranche
    assert desk.gave_back == [JULIEN] * typed.slices  # premier plan rendu après chaque tranche
    assert all(end - start <= 0.1 + 0.05 for start, end in app.slices)  # activation bornée (marge d'ordonnanceur)
    assert "brief activations" in typed.detail() and "devtools=true" in typed.detail()


def test_short_text_is_one_slice(monkeypatch):
    desk = Desk(monkeypatch)
    app = App(monkeypatch, desk)
    typed = methods.type_in_slices(TARGET, {TARGET}, "Julien Piron", app.prepare, *as_reader(app.progress))
    assert typed.complete and typed.slices == 1 and app.typed == "Julien Piron"
    assert typed.detail().startswith("WM_CHAR keystrokes, brief activation")


def test_enter_is_synchronised_by_read_back(monkeypatch):
    desk = Desk(monkeypatch)
    app = App(monkeypatch, desk)
    typed = methods.type_in_slices(
        TARGET, {TARGET}, "ab\r\ncd", app.prepare, *as_reader(app.progress), enter_as_key=True
    )
    assert typed.complete and app.typed == "ab\ncd"
    read_newline = app.log.index(("read", 3))
    assert read_newline < app.log.index(("post", "c"))  # la suite n'est postée qu'une fois l'Entrée relue


def test_unread_slice_stops_without_exception(monkeypatch):
    """Lot T : une tranche non relue ne suffit plus à arrêter (premier plan rendu, reprise au début relu) ; l'arrêt
    vient quand le début relu n'avance pas pendant deux tranches."""
    desk = Desk(monkeypatch)
    app = App(monkeypatch, desk)
    typed = methods.type_in_slices(TARGET, {TARGET}, "x" * 400, app.prepare, *as_reader(lambda: 0), slice_s=0.1)
    # Rien n'est jamais renvoyé : ce qui n'est pas relu est attendu hors activation, puis l'arrêt le dit.
    assert not typed.complete and typed.confirmed == 0 and typed.slices == 1 and typed.sent > 0
    assert "not read back" in typed.detail() and "rest not sent" in typed.detail()
    assert "no character resent" in typed.detail()


def test_busy_julien_between_slices_stops_but_first_slice_raises(monkeypatch):
    desk = Desk(monkeypatch)
    app = App(monkeypatch, desk)
    desk.idle_ms = 0
    with pytest.raises(foreground.BusyUser):  # rien tapé : une autre technique peut être essayée
        methods.type_in_slices(TARGET, {TARGET}, "abc", app.prepare, *as_reader(app.progress))
    assert app.typed == ""
    desk.idle_ms = 60_000

    def prepare(first: bool) -> int:
        if not first:
            desk.idle_ms = 0  # Julien reprend la main entre deux tranches
        return app.prepare(first)

    typed = methods.type_in_slices(TARGET, {TARGET}, "y" * 400, prepare, *as_reader(app.progress), slice_s=0.1)
    assert not typed.complete and typed.confirmed > 0 and "rest not sent" in typed.stopped


def test_physical_input_during_slices_raises_with_progress(monkeypatch):
    desk = Desk(monkeypatch)
    app = App(monkeypatch, desk)
    monkeypatch.setattr(foreground.physical, "available", lambda: True)
    monkeypatch.setattr(foreground.physical, "since", lambda moment: len(app.typed) >= 5)
    with pytest.raises(foreground.UserInterrupted, match="0/50 characters read back.*5 more sent"):
        methods.type_in_slices(TARGET, {TARGET}, "z" * 50, app.prepare, *as_reader(app.progress))
    assert app.typed == "z" * 5 and desk.gave_back == [JULIEN]


# --- window(action="front") : arrêt, pause, garde suspendue --------------------------------------------------------
def _front_env(monkeypatch, desk: Desk) -> list[int]:
    fronted: list[int] = []

    def bring(hwnd: int) -> bool:
        fronted.append(hwnd)
        desk.fg = hwnd
        return True

    monkeypatch.setattr(realinput, "bring_to_front", bring)
    return fronted


def test_front_is_not_undone_by_a_running_watch(monkeypatch):
    desk = Desk(monkeypatch)
    _front_env(monkeypatch, desk)
    with foreground.Watch(TARGET) as watch:  # garde d'un appel précédent, encore en cours (traîne comprise)
        assert realinput.put_in_front(TARGET)
        time.sleep(0.15)  # la garde tourne après la fin de la suspension
    assert watch._thread is not None
    watch._thread.join(2)
    assert desk.gave_back == [] and watch.front == TARGET and watch.stolen == 0


def test_front_without_adoption_would_be_undone(monkeypatch):
    """Témoin : la même mise au premier plan hors put_in_front est défaite par la garde."""
    desk = Desk(monkeypatch)
    with foreground.Watch(TARGET) as watch:
        desk.fg = TARGET
        deadline = time.monotonic() + 2
        while not desk.gave_back and time.monotonic() < deadline:
            time.sleep(0.01)
    assert watch._thread is not None
    watch._thread.join(2)
    assert desk.gave_back == [JULIEN]


def test_front_respects_stop_and_typing(monkeypatch):
    desk = Desk(monkeypatch)
    fronted = _front_env(monkeypatch, desk)
    stop.pressed()
    with pytest.raises(worker.Stopped):
        realinput.put_in_front(TARGET)
    # Fin de l'arrêt simulée par l'échéance : fichier vieilli ET verrou en mémoire échu (la suppression du fichier,
    # elle, ne lève rien : test_fix_G2).
    old = time.time() - stop.HOLD_S - 1
    os.utime(stop.STOP_FILE, (old, old))
    monkeypatch.setattr(stop, "_latched_until", 0.0)
    desk.idle_ms = 0
    real = foreground.wait_typing_pause
    monkeypatch.setattr(foreground, "wait_typing_pause", lambda: real(max_wait_s=0.05))
    with pytest.raises(foreground.BusyUser):
        realinput.put_in_front(TARGET)
    assert fronted == [] and desk.fg == JULIEN


def test_stop_checked_right_before_any_activation(monkeypatch):
    desk = Desk(monkeypatch)
    switched: list[int] = []
    monkeypatch.setattr(foreground, "_switch_to", lambda hwnd, timeout_s, ours=None: switched.append(hwnd) or True)
    monkeypatch.setattr(foreground.w, "IsWindow", lambda h: True)
    monkeypatch.setattr(foreground.w, "IsIconic", lambda h: False)
    stop.pressed()
    with pytest.raises(worker.Stopped):
        foreground.activate(TARGET)
    with pytest.raises(worker.Stopped):
        with realinput.borrowed(TARGET, {TARGET}):
            pytest.fail("emprunt pendant un arrêt d'urgence")
    with pytest.raises(worker.Stopped):  # demandé pendant l'attente de la pause : rien n'est activé
        with foreground.brief_activation(TARGET, {TARGET}):
            pytest.fail("activation brève pendant un arrêt d'urgence")
    assert switched == [] and desk.fg == JULIEN


# --- Arrêt d'urgence : raccourci non obtenu, battement, écriture impossible ------------------------------------------
class FakeUser32:
    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def RegisterHotKey(self, *args) -> bool:
        self.calls += 1
        return self.calls > self.fail_times

    def SetTimer(self, *args) -> int:
        return 1

    def GetMessageW(self, *args) -> int:
        return 0  # boucle de messages terminée aussitôt


def test_hotkey_failure_is_logged_once_then_relay(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(stop.log, "event", lambda name, data: events.append(data))
    monkeypatch.setattr(stop, "_user32", FakeUser32(fail_times=3))
    monkeypatch.setattr(stop, "HEARTBEAT_S", 0.001)
    monkeypatch.setattr(stop, "_tried", threading.Event())
    monkeypatch.setattr(stop, "_owner", threading.Event())
    monkeypatch.setattr(stop, "_failure", 0)
    stop._loop()
    assert [e["hotkey"] for e in events] == [False, True]  # échec journalisé UNE fois, puis le relais
    assert stop.owns_hotkey() and stop.hotkey_note() == "" and stop.status() == "held by this server"
    beat = json.loads(stop.OWNER_FILE.read_text(encoding="utf-8"))
    assert beat["pid"] > 0 and time.time() - beat["ts"] < 5


def test_hotkey_note_without_live_owner(monkeypatch):
    monkeypatch.setattr(stop, "_tried", threading.Event())
    monkeypatch.setattr(stop, "_owner", threading.Event())
    monkeypatch.setattr(stop, "_failure", 1409)
    assert stop.hotkey_note() == ""  # pas encore essayé : rien à affirmer
    stop._tried.set()
    assert "NOT GUARANTEED" in stop.hotkey_note() and "1409" in stop.hotkey_note()
    stop.OWNER_FILE.write_text(json.dumps({"pid": 4242, "version": "x", "ts": time.time()}), encoding="utf-8")
    assert stop.hotkey_note() == "" and "4242" in stop.status()  # un autre serveur vivant le détient
    stale = time.time() - stop.OWNER_STALE_S - 1
    stop.OWNER_FILE.write_text(json.dumps({"pid": 4242, "version": "x", "ts": stale}), encoding="utf-8")
    assert "NOT GUARANTEED" in stop.hotkey_note()


def test_stop_press_survives_write_error(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(stop.log, "event", lambda name, data: events.append(data))

    def broken() -> None:
        raise OSError("disque plein")

    monkeypatch.setattr(stop, "request", broken)
    stop.pressed()  # ne lève pas : le fil du raccourci survit
    assert stop.remaining_s() > stop.HOLD_S - 5  # arrêt tenu par ce serveur
    with pytest.raises(worker.Stopped):
        worker.check_cancelled()
    assert events[-1]["shared"] is False


# --- Serveur dépassé ------------------------------------------------------------------------------------------------
def test_fingerprint_sees_any_source_change(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    before = freshness.fingerprint(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")  # même version, code modifié
    assert freshness.fingerprint(tmp_path) != before


def test_stale_reason_rechecks_disk_at_most_every_30_s(monkeypatch):
    reads: list[int] = []
    monkeypatch.setattr(freshness, "LOADED", "ancien")
    monkeypatch.setattr(freshness, "fingerprint", lambda root=None: reads.append(1) or "nouveau")
    monkeypatch.setattr(freshness, "_checked", None)
    monkeypatch.setattr(freshness, "_reason", "")
    assert freshness.CHECK_EVERY_S <= 30
    reason = freshness.stale_reason()
    assert reason.startswith(freshness.STALE) and freshness.stale_reason() == reason
    assert len(reads) == 1
    monkeypatch.setattr(freshness, "_checked", time.monotonic() - freshness.CHECK_EVERY_S)
    monkeypatch.setattr(freshness, "fingerprint", lambda root=None: "ancien")  # code remis à l'identique
    assert freshness.stale_reason() == ""


def test_supervisor_status_in_logs(monkeypatch):
    monkeypatch.setattr(freshness, "SUPERVISED", False)
    assert freshness.supervisor_status().startswith("absent")
    monkeypatch.setattr(freshness, "SUPERVISED", True)
    monkeypatch.setenv("NEXUS_WINDOWS_SUPERVISOR", "42")
    monkeypatch.setenv("NEXUS_WINDOWS_SUPERVISOR_CODE", freshness.supervisor_fingerprint())
    assert freshness.supervisor_status().startswith(
        "active (supervisor pid 42): the server reloads its new code by itself"
    )
    monkeypatch.setenv("NEXUS_WINDOWS_SUPERVISOR_CODE", "ancien")
    assert "next reconnection" in freshness.supervisor_status()


def test_loaded_fingerprint_matches_disk():
    assert freshness.LOADED and freshness.fingerprint() == freshness.LOADED


def test_stale_server_refuses_actions_and_warns_reads(monkeypatch):
    stale = f"{freshness.STALE} (test)"
    monkeypatch.setattr(freshness, "stale_reason", lambda: stale)
    monkeypatch.setattr(stop, "hotkey_note", lambda: "")
    ran: list[str] = []
    wk = worker.Worker()
    monkeypatch.setattr(server, "WORKER", wk)
    try:
        with pytest.raises(ToolError, match="nexus-windows server out of date: reconnect \\(/mcp\\)"):
            asyncio.run(server._run(lambda auto: ran.append("act") or "fait"))
        assert ran == []  # refusé avant tout effet
        text = asyncio.run(server._run(lambda auto: "liste", reads_only=True))
        assert text.startswith(f"Warning: {stale}.") and text.endswith("liste")
        look = asyncio.run(server._run(lambda auto: ["en-tête\néléments", "image"], reads_only=True))
        assert look[0].startswith("Warning: nexus-windows server out of date") and look[1] == "image"
    finally:
        wk.abandon()


def test_hotkey_note_heads_look(monkeypatch):
    monkeypatch.setattr(freshness, "stale_reason", lambda: "")
    monkeypatch.setattr(stop, "hotkey_note", lambda: "Arrêt d'urgence NON GARANTI (test)")
    wk = worker.Worker()
    monkeypatch.setattr(server, "WORKER", wk)
    try:
        look = asyncio.run(server._run(lambda auto: ["Fenêtre 1 « X »", "image"], reads_only=True))
        acted = asyncio.run(server._run(lambda auto: "fait"))
    finally:
        wk.abandon()
    assert look[0].splitlines()[0] == "Arrêt d'urgence NON GARANTI (test)" and acted == "fait"


def test_logs_show_server_state(tmp_path, monkeypatch):
    monkeypatch.setattr(server.log, "LOG_DIR", tmp_path)
    monkeypatch.setattr(freshness, "stale_reason", lambda: "")
    out = asyncio.run(server.logs(5))
    lines = out.splitlines()
    assert "up to date" in lines[0] and lines[1].startswith("Automatic reload: ")
    assert lines[2].startswith("Emergency stop") and lines[3].startswith("User input")
    assert lines[-1] == "Log empty."
    monkeypatch.setattr(physical, "_ok", True)
    monkeypatch.setattr(physical, "counts", {"physical": 3, "injected": 9})
    assert "3 physical inputs" in physical.summary() and "9 injected" in physical.summary()


# --- Banc de test : Julien absent exigé, premier plan rendu -------------------------------------------------------
def _lab():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lab"))
    try:
        import apps
    finally:
        sys.path.pop(0)
    return apps


@pytest.mark.parametrize(
    ("raw_input", "listening", "physical_idle", "system_idle", "away"),
    [
        (True, 200.0, 30.0, 30.0, False),  # entrée physique de Julien il y a 30 s
        (True, 200.0, float("inf"), 2.0, True),  # rien de physique en 200 s ; entrée injectée d'un autre outil
        (True, 5.0, float("inf"), 2.0, True),  # écoute trop courte : on écoute jusqu'à 120 s, rien de physique
        (True, 5.0, 1.0, 2.0, False),  # entrée physique vue pendant l'écoute : refus aussitôt
        (False, 0.0, float("inf"), 130.0, True),
        (False, 0.0, float("inf"), 60.0, False),
    ],
)
def test_lab_requires_julien_away(monkeypatch, raw_input, listening, physical_idle, system_idle, away):
    apps = _lab()
    elapsed = [0.0]  # horloge simulée : l'attente de l'écoute avance sans dormir
    monkeypatch.setattr(apps.time, "sleep", lambda s: elapsed.__setitem__(0, elapsed[0] + s))
    monkeypatch.setattr(physical, "start", lambda timeout_s=2.0: raw_input)
    monkeypatch.setattr(physical, "available", lambda: raw_input)
    monkeypatch.setattr(physical, "listening_s", lambda: listening + elapsed[0] if raw_input else 0.0)
    monkeypatch.setattr(physical, "idle_s", lambda: physical_idle)
    monkeypatch.setattr(foreground.w, "GetTickCount", lambda: 1_000_000)
    monkeypatch.setattr(foreground.w, "last_input_tick", lambda: 1_000_000 - int(system_idle * 1000))
    if away:
        apps.require_julien_away()
    else:
        with pytest.raises(apps.JulienActive):
            apps.require_julien_away()


def test_lab_guard_gives_foreground_back(monkeypatch):
    from nexus_windows import launch

    apps = _lab()
    desk = Desk(monkeypatch)
    monkeypatch.setattr(launch, "descendants", lambda pid: {pid, 51})
    monkeypatch.setattr(foreground.w, "root", lambda h: h)
    monkeypatch.setattr(foreground.w, "window_pid", lambda h: 51 if h == TARGET else 99)
    guard = apps._ForegroundGuard(50, JULIEN)
    desk.fg = TARGET  # la fenêtre du labo (processus enfant) prend le premier plan
    deadline = time.monotonic() + 2
    while not desk.gave_back and time.monotonic() < deadline:
        time.sleep(0.01)
    desk.fg = OTHER  # Julien passe à une autre de ses fenêtres : le labo ne la lui reprend pas
    guard.finish(0.1)
    assert desk.gave_back == [JULIEN] and guard.taken == 1 and desk.fg == OTHER


def test_lab_browser_followed_through_its_profile_after_relaunch(monkeypatch, tmp_path):
    """Constat du 16/10 : Edge se relance (le processus lancé quitte avec 0, une copie au même profil garde la
    fenêtre). La fenêtre du labo est cherchée chez le détenteur du profil, et close() ferme aussi cette copie."""
    from nexus_windows import launch

    apps = _lab()

    class Dead:
        pid = 40

        def poll(self):
            return 0

    holder = {"pid": 70}
    killed = []
    monkeypatch.setattr(launch, "descendants", lambda pid: {pid, pid + 1})
    monkeypatch.setattr(apps, "profile_browser_pid", lambda profile: holder["pid"])
    monkeypatch.setattr(apps.os, "kill", lambda pid, sig: killed.append(pid) or holder.update(pid=0))
    app = apps.LabApp("edge", Dead(), profile=tmp_path / "edge-profile")  # type: ignore[arg-type]
    assert app.pids() == {40, 41, 70, 71}
    app.close()
    assert killed == [70] and holder["pid"] == 0
    assert apps.LabApp("win32", Dead()).pids() == {40, 41}  # type: ignore[arg-type]


# --- Code mort retiré ---------------------------------------------------------------------------------------------
def test_dead_code_removed():
    assert not hasattr(foreground.Watch, "wait_tail")
    assert not hasattr(methods, "_match")
    assert list(inspect.signature(methods.post_key).parameters) == ["target", "vk"]
    source = inspect.getsource(server)
    assert "stop.status()" in source and "physical.summary()" in source  # owns_hotkey et counts branchés
