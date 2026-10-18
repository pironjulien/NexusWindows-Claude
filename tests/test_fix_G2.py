"""Lot G de la contre-vérification finale (recheck6) : arrêt d'urgence tenu en mémoire, application Store qui garde le
premier plan, identité des processus DevTools, capture sans Windows.Graphics.Capture. Faux objets, sauf le test
marqué live (labo réel)."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image

from nexus_windows import capture, foreground, launch, server, session, snapshot, stop, worker
from nexus_windows import win32 as w

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contract import as_auto, make_view, node  # noqa: E402
from test_fix_b import JULIEN, OTHER, TARGET, Desk  # noqa: E402
from test_fix_c import _fresh_local_state, _profile, as_popen  # noqa: E402


@pytest.fixture
def stop_file(tmp_path, monkeypatch):
    """Jamais le vrai fichier d'arrêt ni le vrai journal."""
    monkeypatch.setattr(stop, "STOP_FILE", tmp_path / "stop")
    monkeypatch.setattr(stop, "OWNER_FILE", tmp_path / "stop.owner")
    monkeypatch.setattr(stop.log, "event", lambda *a, **k: None)
    return stop.STOP_FILE


# --- Bloquant 5 : arrêt d'urgence tenu en mémoire ------------------------------------------------------------------
def test_press_then_file_deleted_is_still_stopped(stop_file):
    stop.pressed()
    stop_file.unlink()  # le shell de Claude fait « rm » : rien n'est levé
    assert stop.remaining_s() > stop.HOLD_S - 5
    with pytest.raises(worker.Stopped, match="Arrêt d'urgence"):
        worker.check_cancelled()


def test_press_survives_wall_clock_jumps(stop_file, monkeypatch):
    stop.pressed()
    real = time.time
    for jump in (stop.HOLD_S + 3600, -3600):  # horloge resynchronisée en avant, puis en arrière
        clock = types.SimpleNamespace(time=lambda j=jump: real() + j, monotonic=time.monotonic, strftime=time.strftime)
        monkeypatch.setattr(stop, "time", clock)
        remaining = stop.remaining_s()
        assert stop.HOLD_S - 5 < remaining <= stop.HOLD_S + 0.5  # ni levé, ni prolongé au-delà d'HOLD_S
        with pytest.raises(worker.Stopped):
            worker.check_cancelled()


def test_clock_set_back_does_not_extend_the_stop(stop_file, monkeypatch):
    stop.pressed()
    real = time.time
    clock = types.SimpleNamespace(time=lambda: real() - 3600, monotonic=time.monotonic, strftime=time.strftime)
    monkeypatch.setattr(stop, "time", clock)  # fichier désormais daté d'une heure « dans le futur »
    assert stop.remaining_s() <= stop.HOLD_S + 0.5
    now = time.monotonic()
    monkeypatch.setattr(stop, "_now", lambda: now + stop.HOLD_S + 1)
    assert stop.remaining_s() == 0  # levé à l'échéance, pas une heure plus tard
    stop.request()  # nouvel appui (ici ou ailleurs) : nouvelle version du fichier, verrou reposé
    assert stop.remaining_s() > 0


def test_file_from_another_server_seen_by_watch_then_deleted_is_still_stopped(stop_file, monkeypatch):
    monkeypatch.setattr(stop, "WATCH_S", 0.02)
    done = threading.Event()
    watcher = threading.Thread(target=stop._watch, args=(done,), daemon=True)
    watcher.start()
    try:
        stop.request()  # appui reçu par un AUTRE serveur : seul le fichier le dit
        deadline = time.monotonic() + 2
        while stop._latched_until <= 0 and time.monotonic() < deadline:
            time.sleep(0.01)  # tick du fil de surveillance, sans aucun appel d'outil
        assert stop._latched_until > 0
        stop_file.unlink()
        with pytest.raises(worker.Stopped):
            worker.check_cancelled()
    finally:
        done.set()
        watcher.join(2)
    assert not watcher.is_alive()


def test_latch_expires_only_with_time(stop_file, monkeypatch):
    stop.pressed()
    now = time.monotonic()
    aged = time.time() - (stop.HOLD_S - 1)
    os.utime(stop_file, (aged, aged))  # les deux horloges ont avancé de HOLD_S - 1
    os.utime(stop.SHARED_STOP_FILE, (aged, aged))  # même appui : le fichier commun aux agents a le même âge
    monkeypatch.setattr(stop, "_now", lambda: now + stop.HOLD_S - 1)
    assert 0 < stop.remaining_s() <= 1.5  # fichiers toujours présents : jamais au-delà de l'échéance
    stop_file.unlink()
    stop.SHARED_STOP_FILE.unlink()
    monkeypatch.setattr(stop, "_now", lambda: now + stop.HOLD_S + 1)
    assert stop.remaining_s() == 0
    worker.check_cancelled()  # échu : plus d'arrêt


def test_start_launches_the_watch_thread(monkeypatch):
    started: list = []

    class FakeThread:
        def __init__(self, target, args=(), name="", daemon=False):
            self.target, self.name = target, name

        def start(self):
            started.append((self.target, self.name))

    monkeypatch.setattr(stop, "_started", threading.Event())
    monkeypatch.setattr(stop, "_tried", threading.Event())
    monkeypatch.setattr(stop.threading, "Thread", FakeThread)
    stop.start(timeout_s=0)
    assert (stop._watch, "nexus-stop-watch") in started and (stop._loop, "nexus-stop-hotkey") in started


def test_docstring_no_longer_claims_julien_lifts_by_deleting():
    doc = stop.__doc__ or ""
    assert "en supprimant le fichier" not in doc and "_latched_until" in doc and "rm" in doc


def _fake_run(result=None, error: Exception | None = None):
    async def run(fn, timeout_s=60.0, *, reads_only=False):
        if error is not None:
            raise error
        return result

    return run


def test_act_carries_the_hotkey_note(monkeypatch):
    note = "Arrêt d'urgence Ctrl+Alt+Maj+Échap NON GARANTI (test)"
    monkeypatch.setattr(stop, "hotkey_note", lambda: note)
    steps = [server.Step(do="click", target="OK")]
    monkeypatch.setattr(server, "_run", _fake_run(["1. ✓ clic\nFenêtre 1", "image"]))
    out = asyncio.run(server.act(steps))
    assert out[0].splitlines()[0] == note and out[1] == "image"
    monkeypatch.setattr(server, "_run", _fake_run(error=ToolError("Arrêt d'urgence demandé par Julien")))
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.act(steps))
    assert str(caught.value).splitlines()[0] == note  # le refus aussi
    monkeypatch.setattr(stop, "hotkey_note", lambda: "")
    monkeypatch.setattr(server, "_run", _fake_run(["1. ✓ clic", "image"]))
    assert asyncio.run(server.act(steps)) == ["1. ✓ clic", "image"]  # raccourci détenu : rien d'ajouté


# --- Bloquant 6 : application Store qui garde le premier plan ------------------------------------------------------
class Frame:
    """Bureau simulé : la cible (cadre ApplicationFrameHost) tient le premier plan ; Windows refuse notre attache
    (_switch_to échoue toujours) ; seul le fil du cadre peut le rendre, s'il reçoit SC_HOTKEY."""

    def __init__(self, monkeypatch, host: str = "ApplicationFrameHost.exe", obeys: bool = True) -> None:
        self.fg = TARGET
        self.posted: list[tuple] = []
        self.switches = 0
        m = monkeypatch
        m.setattr(foreground.w, "foreground", lambda: self.fg)
        m.setattr(foreground.w, "IsWindow", lambda h: True)
        m.setattr(foreground.w, "IsIconic", lambda h: False)
        m.setattr(foreground.w, "IsWindowVisible", lambda h: True)
        m.setattr(foreground.w, "is_cloaked", lambda h: False)
        m.setattr(foreground.w, "class_name", lambda h: "ApplicationFrameWindow" if h == TARGET else "Fenetre")
        m.setattr(foreground.w, "root", lambda h: h)
        m.setattr(foreground.w, "owner", lambda h: 0)
        m.setattr(foreground.w, "window_pid", lambda h: 10 if h == TARGET else 20)
        m.setattr(foreground.w, "process_name", lambda pid: host if pid == 10 else "julien.exe")
        m.setattr(foreground.w, "PostMessageW", self._post)
        m.setattr(foreground, "_switch_to", self._switch)
        m.setattr(foreground, "HAND_OVER_S", 0.05)
        self.obeys = obeys

    def _switch(self, hwnd, timeout_s):
        self.switches += 1
        return False  # AttachThreadInput refusé (ERROR_ACCESS_DENIED, mesuré) : SetForegroundWindow refusé

    def _post(self, hwnd, msg, wparam, lparam):
        self.posted.append((hwnd, msg, wparam, lparam))
        if self.obeys and hwnd == self.fg and msg == foreground.WM_SYSCOMMAND and wparam == foreground.SC_HOTKEY:
            self.fg = lparam  # DefWindowProc du fil au premier plan : SetForegroundWindow(lParam)
        return True


def test_give_back_from_a_store_frame_asks_the_frame_itself(monkeypatch):
    desk = Frame(monkeypatch)
    assert foreground.give_back(JULIEN, exclude={TARGET})
    assert desk.fg == JULIEN
    assert desk.posted == [(TARGET, foreground.WM_SYSCOMMAND, foreground.SC_HOTKEY, JULIEN)]
    assert desk.switches == 0  # cadre Store : pas d'attente inutile de 0,6 s


def test_give_back_hands_over_after_a_refused_switch_for_any_target(monkeypatch):
    desk = Frame(monkeypatch, host="cible.exe")
    assert foreground.give_back(JULIEN, exclude={TARGET})
    assert desk.switches == 1 and desk.fg == JULIEN


def test_hand_over_never_posts_to_a_window_outside_the_target(monkeypatch):
    desk = Frame(monkeypatch, host="cible.exe")
    desk.fg = OTHER  # une autre fenêtre (de Julien) tient le premier plan
    assert not foreground.give_back(JULIEN, exclude={TARGET})
    assert desk.posted == []


def test_hand_over_failure_is_reported(monkeypatch):
    desk = Frame(monkeypatch, obeys=False)  # application figée : le message n'est jamais traité
    assert not foreground.give_back(JULIEN, exclude={TARGET})
    assert desk.fg == TARGET


def test_watch_with_julien_front_starts_even_if_target_holds_the_foreground(monkeypatch):
    desk = Desk(monkeypatch, fg=TARGET)  # vol déjà fait, non rendu par launch
    monkeypatch.setattr(foreground.w, "IsWindow", lambda h: True)
    with foreground.Watch(TARGET, front=JULIEN) as watch:
        assert watch._thread is not None and watch.front == JULIEN
        deadline = time.monotonic() + 2
        while not desk.gave_back and time.monotonic() < deadline:
            time.sleep(0.01)
    watch._thread.join(2)
    assert desk.gave_back == [JULIEN] and watch.stolen == 1 and desk.fg == JULIEN


def test_watch_without_front_keeps_julien_choice(monkeypatch):
    Desk(monkeypatch, fg=TARGET)  # Julien a lui-même mis la cible devant
    with foreground.Watch(TARGET) as watch:
        assert watch._thread is None  # comportement inchangé : rien à rendre


def test_watch_front_closed_falls_back_to_next_window(monkeypatch):
    Desk(monkeypatch, fg=TARGET)
    monkeypatch.setattr(foreground.w, "IsWindow", lambda h: h != JULIEN)  # fenêtre de Julien fermée entre-temps
    with foreground.Watch(TARGET, front=JULIEN) as watch:
        assert watch.front == 0 and watch._thread is not None


def test_brief_activation_refuses_when_target_already_took_julien_front(monkeypatch):
    desk = Desk(monkeypatch, fg=TARGET)
    with pytest.raises(foreground.BusyUser, match="tient déjà le premier plan pris à Julien"):
        with foreground.brief_activation(TARGET, {TARGET}, user_front=JULIEN):
            pytest.fail("activation brève sans personne à qui rendre le premier plan")
    with foreground.brief_activation(TARGET, {TARGET}) as activation:  # act : cible choisie par Julien, permise
        assert activation.front == TARGET
    desk.fg = JULIEN
    with foreground.brief_activation(TARGET, {TARGET}, user_front=JULIEN) as activation:
        assert activation.front == JULIEN


def _store_launch(monkeypatch, fg: dict, give_back_ok: bool) -> list[int]:
    """launch.launch d'une application Store, bureau simulé : la Calculatrice prend le premier plan de Julien."""
    gave: list[int] = []
    monkeypatch.setattr(launch, "find_start_app", lambda name: {"Name": "Calculatrice", "AppID": "Calc_8wekyb!App"})
    monkeypatch.setattr(launch, "_app_id_path", lambda app_id: None)
    monkeypatch.setattr(launch.policy, "check_launch_app_id", lambda *a, **k: None)
    monkeypatch.setattr(launch, "_activate_packaged", lambda app_id, args: fg.update(hwnd=TARGET) or 4242)
    monkeypatch.setattr(launch, "_new_window", lambda *a, **k: (TARGET, False))
    monkeypatch.setattr(launch, "descendants", lambda pid: {pid})
    monkeypatch.setattr(launch.w, "foreground", lambda: fg["hwnd"])
    monkeypatch.setattr(launch.w, "enum_windows", lambda: [JULIEN])
    monkeypatch.setattr(launch.w, "IsWindow", lambda h: True)
    monkeypatch.setattr(launch.w, "SetWindowPos", lambda *a: True)
    monkeypatch.setattr(launch.w, "process_name", lambda pid: "CalculatorApp.exe")
    monkeypatch.setattr(launch.foreground, "target_windows", lambda hwnd, extra=None: {hwnd})
    monkeypatch.setattr(launch.foreground, "target_pids", lambda hwnd, extra=None: {4242})
    monkeypatch.setattr(launch.foreground, "app_pid", lambda hwnd: 4242)
    monkeypatch.setattr(launch.foreground, "user_is_switching", lambda: False)  # jamais le vrai clavier ni la souris

    def give_back(prev, timeout_s=0.6, exclude=None):
        gave.append(prev)
        if give_back_ok:
            fg["hwnd"] = prev
        return give_back_ok

    monkeypatch.setattr(launch.foreground, "give_back", give_back)
    monkeypatch.setattr(launch, "wait_content", lambda auto, hwnd, limit_s=3.0: True)
    return gave


def test_store_launch_never_activates_and_says_it_stays_empty(monkeypatch):
    fg = {"hwnd": JULIEN}
    _store_launch(monkeypatch, fg, give_back_ok=True)
    monkeypatch.setattr(launch, "uwp_unshown", lambda hwnd: True)
    monkeypatch.setattr(launch, "wait_content", lambda auto, hwnd, limit_s=3.0: False)
    monkeypatch.setattr(launch.foreground, "brief_activation", lambda *a, **k: pytest.fail("aucune activation"))
    monkeypatch.setattr(launch.foreground, "activate", lambda *a, **k: pytest.fail("aucune activation"))
    result = launch.launch("Calculatrice", auto=as_auto(object()))
    assert result.hwnd == TARGET and result.user_front == JULIEN
    assert any("jamais affichée" in n for n in result.notes)


def test_store_launch_reports_foreground_not_given_back(monkeypatch):
    fg = {"hwnd": JULIEN}
    gave = _store_launch(monkeypatch, fg, give_back_ok=False)
    monkeypatch.setattr(launch, "uwp_unshown", lambda hwnd: False)
    # Calendrier de la garde fixé : un seul contrôle pendant wait_content (pas de nouveau contrôle avant la fin du
    # bloc), wait_content rendant la main dès que la garde a tenté de rendre le premier plan.
    monkeypatch.setattr(foreground.Watch, "POLL_S", 30.0)

    def wait_content(auto, hwnd, limit_s=3.0):
        deadline = time.monotonic() + 5
        while len(gave) < 2:
            assert time.monotonic() < deadline, "la garde n'a pas tenté de rendre le premier plan pendant l'attente"
            time.sleep(0.001)
        return True

    monkeypatch.setattr(launch, "wait_content", wait_content)
    result = launch.launch("Calculatrice", auto=as_auto(object()))
    # Exactement trois tentatives, toutes avant le retour de launch : celle de launch après l'attente de la fenêtre,
    # celle de la garde pendant wait_content, puis son dernier contrôle à la fin du bloc.
    assert gave == [JULIEN, JULIEN, JULIEN] and not result.returned and result.user_front == JULIEN
    assert any("PAS rendu à Julien" in n for n in result.notes)
    assert not any("rendu aussitôt" in n for n in result.notes)


def test_store_launch_given_back_says_so(monkeypatch):
    fg = {"hwnd": JULIEN}
    _store_launch(monkeypatch, fg, give_back_ok=True)
    monkeypatch.setattr(launch, "uwp_unshown", lambda hwnd: False)
    result = launch.launch("Calculatrice", auto=as_auto(object()))
    assert result.returned and any("rendu aussitôt à Julien" in n for n in result.notes)


def test_server_launch_guards_with_julien_front(monkeypatch, tmp_path):
    seen: dict = {}
    monkeypatch.setattr(server.log, "LOG_DIR", tmp_path)
    result = launch.Launched(TARGET, 4242, 0, "application Store", ["n"], user_front=JULIEN, returned=False)
    monkeypatch.setattr(server.launcher, "launch", lambda *a, **k: seen.update(kwargs=k) or result)

    class FakeWatch:
        def __init__(self, target, windows=None, tail_s=None, front=0):
            seen.update(target=target, tail_s=tail_s, front=front)
            self.stolen = 1

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def note(self):
            return "L'application a pris le premier plan (une fois) : rendu aussitôt à Julien."

    monkeypatch.setattr(server, "Watch", FakeWatch)
    monkeypatch.setattr(server, "render", lambda auto, hwnd, then, extra=None, call=None: ["\n".join(extra or ())])
    monkeypatch.setattr(server, "SESSION", session.Session())
    monkeypatch.setattr(server, "_run", lambda fn, timeout_s=0: _sync(fn))
    out = asyncio.run(server.launch("Calculatrice"))
    assert seen["front"] == JULIEN and seen["target"] == TARGET and seen["tail_s"] == 1.6
    assert seen["kwargs"]["on_account_kill"] == server.SESSION.forget_profile
    assert "rendu aussitôt à Julien" in out[0]


async def _sync(fn):
    return fn(None)


# --- Bloquant 7 : identité des processus DevTools ------------------------------------------------------------------
@pytest.fixture
def tracked(tmp_path, monkeypatch):
    """Session avec un navigateur DevTools enregistré (pid 999, heure de création 1111), profil jetable réel sous un
    dossier PROFILES de test ; fermetures et attentes enregistrées."""
    monkeypatch.setattr(launch, "PROFILES", tmp_path / "profiles")
    profile = launch.fresh_profile()
    (profile / "Local State").write_text(json.dumps(_fresh_local_state()), encoding="utf-8")
    (profile / "Default").mkdir()
    (profile / "Default" / "Preferences").write_text(json.dumps({"account_info": None}), encoding="utf-8")
    state = {"created": 1111, "open": True, "killed": [], "waited": [], "closed": []}
    monkeypatch.setattr(session, "_open", lambda pid: 77 if state["open"] else 0)
    monkeypatch.setattr(session, "_close", lambda handle: state["closed"].append(handle))
    monkeypatch.setattr(session, "_started_at", lambda handle: state["created"] if handle else 0)
    monkeypatch.setattr(session, "_wait_exit", lambda handle, timeout_ms=0: state["waited"].append(handle))
    monkeypatch.setattr(launch, "kill_tree", state["killed"].append)
    monkeypatch.setattr(session.log, "event", lambda *a, **k: None)
    monkeypatch.setattr(launch.log, "event", lambda *a, **k: None)
    monkeypatch.setattr(w, "window_pid", lambda h: 999)
    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    sess = session.Session()
    sess.devtools[999] = 9222
    sess.profiles[999] = profile
    sess.identities[999] = 1111
    monkeypatch.setattr(launch, "account_found", lambda port, prof: launch.profile_account(prof))
    return sess, profile, state


def _sign_in(profile: Path) -> None:
    (profile / "Local State").write_text(json.dumps(_fresh_local_state(user_name="x@y")), encoding="utf-8")


def test_reassigned_pid_is_never_killed(tracked):
    sess, profile, state = tracked
    _sign_in(profile)
    state["created"] = 2222  # même pid, autre processus (le Bloc-notes de Julien)
    sess.check_account(4242)  # ne lève pas
    assert state["killed"] == [] and 999 not in sess.devtools and 999 not in sess.profiles
    assert 999 not in sess.identities and state["closed"] == [77]  # handle refermé


def test_exited_browser_is_forgotten_without_kill(tracked):
    sess, profile, state = tracked
    _sign_in(profile)
    state["open"] = False  # processus terminé
    sess.check_account(4242)
    assert state["killed"] == [] and not sess.devtools and not sess.profiles


def test_unidentified_entry_is_never_killed(tracked):
    sess, profile, state = tracked
    _sign_in(profile)
    sess.identities.clear()  # entrée posée sans identité : rien ne prouve que c'est le navigateur lancé
    sess.check_account(4242)
    assert state["killed"] == []


def test_account_at_act_kills_waits_and_removes_profile_at_once(tracked):
    sess, profile, state = tracked
    _sign_in(profile)
    with pytest.raises(launch.AccountError, match="profil jetable supprimé"):
        sess.check_account(4242)
    assert state["killed"] == [999] and state["waited"] == [77]  # sortie attendue sous le même handle
    assert not profile.exists()  # sur-le-champ, pas à la purge (10 min)
    assert not sess.devtools and not sess.profiles and not sess.identities


def test_register_refuses_an_exited_process(tracked, monkeypatch):
    sess, profile, state = tracked
    state["open"] = False
    fresh = session.Session()
    assert not fresh.register_devtools(555, 9333, profile=profile, timeout_s=0.1)
    assert not fresh.profiles and not fresh.identities and not fresh.devtools


def test_guard_kill_cleans_session_and_removes_profile(tracked, monkeypatch):
    sess, profile, state = tracked
    calls: list[str] = []

    class Proc:
        pid = 999

        def poll(self):
            return 0 if state["killed"] else None

        def wait(self, timeout=None):
            calls.append("wait")
            return 0

    monkeypatch.setattr(launch, "ACCOUNT_POLL_S", 0.02)
    thread = launch.guard_account(as_popen(Proc()), 9222, profile, on_kill=sess.forget_profile)
    time.sleep(0.08)
    assert state["killed"] == [] and profile.exists()  # profil neuf
    _sign_in(profile)
    thread.join(3)
    assert state["killed"] == [999] and calls == ["wait"]
    assert not profile.exists() and not sess.devtools and not sess.profiles and not sess.identities


def test_remove_profile_only_inside_profiles_and_marked(tracked, tmp_path):
    _sess, profile, _state = tracked
    outside = _profile(tmp_path / "ailleurs", _fresh_local_state())
    assert not launch.remove_profile(outside, timeout_s=0) and outside.exists()
    unmarked = launch.PROFILES / "20260101-000000-sans"
    unmarked.mkdir()
    assert not launch.remove_profile(unmarked, timeout_s=0) and unmarked.exists()
    assert launch.remove_profile(profile, timeout_s=0) and not profile.exists()


def test_launch_account_at_start_closes_by_popen_and_removes_profile(tracked, monkeypatch, tmp_path):
    _sess, _profile_unused, state = tracked
    folder = tmp_path / "Edge"
    folder.mkdir()
    exe = folder / "msedge.exe"
    exe.write_bytes(b"")
    calls: list[str] = []

    class Proc:
        pid = 5555

        def poll(self):
            return 0 if state["killed"] else None

        def wait(self, timeout=None):
            calls.append("wait")
            return 0

    monkeypatch.setattr(launch.policy, "check_launch_exe", lambda *a, **k: None)
    monkeypatch.setattr(launch.policy, "is_browser_exe", lambda exe: True)
    monkeypatch.setattr(launch.policy, "is_chromium_exe", lambda exe: True)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(launch, "_new_window", lambda *a, **k: (TARGET, False))
    monkeypatch.setattr(launch, "descendants", lambda pid: {pid})
    monkeypatch.setattr(launch.w, "window_pid", lambda h: 5555)
    monkeypatch.setattr(launch.w, "foreground", lambda: JULIEN)
    monkeypatch.setattr(launch.w, "enum_windows", lambda: [JULIEN])
    monkeypatch.setattr(launch.w, "SetWindowPos", lambda *a: True)
    made: list[Path] = []
    real_fresh = launch.fresh_profile

    def fresh() -> Path:
        made.append(real_fresh())
        _sign_in(made[-1])  # connexion implicite dès le démarrage
        return made[-1]

    monkeypatch.setattr(launch, "fresh_profile", fresh)
    with pytest.raises(launch.AccountError, match="profil jetable supprimé"):
        launch.launch(str(exe), devtools=True, wait_s=1)
    assert state["killed"] == [5555] and calls == ["wait"] and not made[0].exists()


# --- Bloquant 8 : capture sans Windows.Graphics.Capture --------------------------------------------------------------
def _view_for_capture():
    return make_view(node("Button", "OK"))  # une seule fenêtre, cadre 400×300 à l'origine


def _shot(image: Image.Image, method: str) -> capture.Shot:
    return capture.Shot(image, (0, 0), method, 1.0)


def _patterned(size) -> Image.Image:
    """Image utilisable (moitié noire, moitié blanche) : blank_ratio < 1."""
    image = Image.new("RGB", size, (255, 255, 255))
    image.paste((0, 0, 0), (0, 0, size[0] // 2, size[1]))
    return image


@pytest.mark.parametrize("dpi_scale", [1.0, 1.25])
def test_capture_view_never_uses_wgc_when_printwindow_works(monkeypatch, dpi_scale):
    view = _view_for_capture()
    size = view.frame.size
    monkeypatch.setattr(snapshot.w, "window_dpi_scale", lambda h: dpi_scale)  # non adaptée au DPI comprise
    monkeypatch.setattr(capture, "capture_printwindow", lambda h: _shot(_patterned(size), "printwindow"))
    monkeypatch.setattr(capture, "capture_wgc", lambda h: pytest.fail("WGC appelé alors que PrintWindow suffit"))
    image, notes = snapshot.capture_view(view)
    assert image.size == size and notes == []


def test_capture_view_uses_wgc_only_as_last_resort(monkeypatch):
    view = _view_for_capture()
    size = view.frame.size
    used: list[str] = []
    monkeypatch.setattr(
        capture, "capture_printwindow", lambda h: used.append("pw") or _shot(Image.new("RGB", size), "printwindow")
    )
    monkeypatch.setattr(capture, "capture_wgc", lambda h: used.append("wgc") or _shot(_patterned(size), "wgc"))
    _image, notes = snapshot.capture_view(view)
    assert used == ["pw", "wgc"] and notes == ["capture printwindow vide"]


# --- Vivant : chemin réel du serveur, 120 look avec image sans fuite --------------------------------------------------
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _server_resources(pid: int) -> tuple[int, int, int]:
    """Handles noyau, objets GDI et objets USER du processus serveur."""
    kernel32, user32 = ctypes.WinDLL("kernel32"), ctypes.WinDLL("user32")
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    assert handle, f"processus serveur {pid} illisible"
    try:
        count = ctypes.c_ulong()
        kernel32.GetProcessHandleCount(ctypes.c_void_p(handle), ctypes.byref(count))
        gdi = user32.GetGuiResources(ctypes.c_void_p(handle), 0)
        usr = user32.GetGuiResources(ctypes.c_void_p(handle), 1)
        return count.value, gdi, usr
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


WARMUP_S = 45.0


async def _look_series(hwnd: int, series: int, count: int) -> tuple[list[tuple], int]:
    """Serveur MCP réel (stdio) : `series` séries de `count` look avec image ; ressources du serveur relevées avant
    la première série puis après chacune (1,5 s plus tard : traînes des gardes finies)."""
    live = str(Path(__file__).resolve().parent / "live")
    if live not in sys.path:  # sans dépendre de test_live.py chargé avant
        sys.path.insert(0, live)
    import drive

    async with drive.Client(drive.server_params(), read_timeout_seconds=120) as client:
        status = drive.text_of(await client.call_tool("logs", {"count": 1}))
        found = re.search(r"pid (\d+)", status)
        assert found is not None
        pid = int(found.group(1))
        # Échauffement : look en continu pendant WARMUP_S. Mesuré le 18/10 : une allocation unique de ~15 handles
        # (et un fil) arrive vers 20-30 s de vie active du serveur, quel que soit le nombre de look ; sans cet
        # échauffement, elle tombait dans la série 1 ou dans la série 2 selon le rythme, et passait pour une fuite.
        warm_until = time.monotonic() + WARMUP_S
        while time.monotonic() < warm_until:
            await client.call_tool("look", {"window": hwnd})
        await asyncio.sleep(3)
        marks = [_server_resources(pid)]
        for _ in range(series):
            for _ in range(count):
                result = await client.call_tool("look", {"window": hwnd})
                assert not result.is_error and drive.image_of(result) is not None
            await asyncio.sleep(1.5)
            marks.append(_server_resources(pid))
    return marks, pid


@pytest.mark.live
def test_server_120_looks_with_image_do_not_leak():
    """Chemin réel du serveur : 2 séries de 120 look avec image dans le même processus serveur. La première installe
    les pools (mesuré : +13 handles, fils, événements, sémaphores, puis 0 aux séries suivantes) : bornée. La seconde ne
    doit rien perdre : une fuite par capture (WGC, recheck6 : +1 par capture) y donnerait ~+120."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lab"))  # apps vit dans lab/, pas tests/live
    import apps

    app = apps.launch("win32", x=140, y=140)
    try:
        marks, pid = asyncio.run(_look_series(app.hwnd, 2, 120))
    finally:
        app.close()
    print(f"serveur pid {pid} : handles/GDI/USER {' → '.join(map(str, marks))}")
    start, warm, end = marks
    assert warm[0] - start[0] <= 30, marks  # échauffement borné
    assert abs(end[0] - warm[0]) <= 10, marks  # aucune perte par look
    assert abs(end[1] - start[1]) <= 10 and abs(end[2] - start[2]) <= 10, marks
