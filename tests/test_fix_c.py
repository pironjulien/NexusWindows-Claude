"""Lot C de la contre-vérification 0.4.0 : politique de lancement, identité de la fenêtre lancée, compte d'un
navigateur DevTools, robustesse DevTools, cache des processus, fuite Windows.Graphics.Capture, contenu prêt après
un lancement Store, profils jetables, entrées invalides. Sans fenêtre réelle, sauf le test marqué live."""

from __future__ import annotations

import ctypes
import gc
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import cast

import pytest

from nexus_windows import actions, capture, cdp, launch, policy, server, session, snapshot, uia
from nexus_windows import win32 as w

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contract import HWND, FakeAuto, as_auto, make_view, node  # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def as_popen(proc: object) -> subprocess.Popen:
    """Faux processus (pid, poll, wait) présenté au typage comme un subprocess.Popen."""
    return cast(subprocess.Popen, proc)


@pytest.fixture
def electron_app(tmp_path) -> str:
    """Application Electron factice : ressources Chromium à côté de l'exécutable, aucun navigateur."""
    folder = tmp_path / "MonAppli"
    folder.mkdir()
    (folder / "chrome_100_percent.pak").write_bytes(b"")
    (folder / "MonAppli.exe").write_bytes(b"")
    return str(folder / "MonAppli.exe")


@pytest.fixture
def edge_install(tmp_path) -> Path:
    """Dossier d'installation d'Edge factice : principal, annexes, dossier de version avec la DLL du navigateur."""
    app = tmp_path / "Edge" / "Application"
    version = app / "154.0.4258.62"
    version.mkdir(parents=True)
    for name in ("msedge.exe", "msedge_proxy.exe", "pwahelper.exe"):
        (app / name).write_bytes(b"")
    for name in ("msedge.dll", "msedge.exe", "identity_helper.exe", "pwahelper.exe"):
        (version / name).write_bytes(b"")
    return app


# --- must_fix 6 : commutateurs Chromium lus comme Chromium les lit --------------------------------------------
@pytest.mark.parametrize("prefix", ["--", "-", "/"])
@pytest.mark.parametrize(
    "switch",
    ["remote-debugging-port=9333", "REMOTE-DEBUGGING-PORT=1", "load-extension=C:\\x", "Disable-Web-Security"],
)
@pytest.mark.parametrize("exe", ["edge", "chrome", "electron"])
def test_chromium_switch_prefixes_and_case_refused(prefix, switch, exe, electron_app):
    path = {"edge": EDGE, "chrome": CHROME, "electron": electron_app}[exe]
    with pytest.raises(policy.PolicyError, match="switch opens a control channel"):
        policy.check_launch_exe(path, f"--app=https://exemple.test {prefix}{switch}")


@pytest.mark.parametrize("prefix", ["--", "-", "/"])
@pytest.mark.parametrize("key", ["user-data-dir=C:\\x", "Profile-Directory=Default"])
@pytest.mark.parametrize("devtools", [False, True])
def test_browser_profile_switches_refused_with_or_without_devtools(prefix, key, devtools):
    for exe in (EDGE, CHROME):
        with pytest.raises(policy.PolicyError, match="profile"):
            policy.check_launch_exe(exe, f'"{prefix}{key}"', devtools=devtools)


@pytest.mark.parametrize("args", ["--app=x -- --flag", "--single-argument https://x", '"x --single-argument y"'])
def test_switch_terminator_and_single_argument_refused(args, electron_app):
    for exe in (EDGE, electron_app):
        with pytest.raises(policy.PolicyError):
            policy.check_launch_exe(exe, args)


def test_ordinary_chromium_arguments_accepted(electron_app):
    policy.check_launch_exe(EDGE, "--window-size=600,640 --app=file:///C:/x/web_app.html", devtools=True)
    policy.check_launch_exe(electron_app, '"C:\\a b\\main.js" --lab-x=150 /flag', devtools=True)
    policy.check_launch_exe(electron_app, "--user-data-dir=C:\\donnees")  # pas un navigateur : ses propres données


def test_switch_key_follows_chromium():
    assert [policy.switch_key(t) for t in ("--A=b", "-a", "/a=b=c", "---a", "--", "-", "/", "a", "")] == [
        "a", "a", "a", "-a", None, None, None, None, None,
    ]  # fmt: skip
    assert policy.split_args('--a="x y" "--b=c" C:\\x\\ ""') == ["--a=x y", "--b=c", "C:\\x\\", ""]


def test_disable_features_merged_whatever_the_prefix():
    rest, switch = launch.merge_disable_features(
        '--app=x -DISABLE-FEATURES=A /disable-features=B "--disable-features=C" "C:\\a b\\x.js"', ("Z", "A")
    )
    assert switch == "--disable-features=A,B,C,Z"
    assert policy.split_args(rest) == ["--app=x", "C:\\a b\\x.js"]


# --- must_fix 7 : tout exécutable d'un dossier de navigateur est le navigateur ---------------------------------
def test_browser_install_annexes_are_the_browser(edge_install, electron_app):
    version = edge_install / "154.0.4258.62"
    for exe in (edge_install / "msedge_proxy.exe", edge_install / "pwahelper.exe", version / "identity_helper.exe"):
        assert policy.is_browser_exe(str(exe)) and policy.is_chromium_exe(str(exe))
        with pytest.raises(policy.PolicyError, match="main executable"):
            policy.check_launch_exe(str(exe), "", devtools=True)
        with pytest.raises(policy.PolicyError, match="profile"):
            policy.check_launch_exe(str(exe), "--profile-directory=Default --app-id=abc")
    policy.check_launch_exe(str(edge_install / "msedge.exe"), "", devtools=True)  # le principal : profil neuf
    policy.check_launch_exe(str(version / "msedge.exe"), "", devtools=True)
    assert not policy.is_browser_exe(electron_app) and policy.is_chromium_exe(electron_app)


def _fresh_local_state(**account) -> dict:
    """Local State d'un profil Edge neuf (relevé sur deux profils réels sans compte), compte éventuel ajouté."""
    entry = {
        "edge_account_cid": "", "edge_account_environment": 0, "edge_account_first_name": "",
        "edge_account_type": 0, "edge_account_tenant_id": "", "edge_non_signin_profile_type": 1,
        "edge_wam_aad_for_app_account_type": 0, "gaia_id": "", "user_name": "", "is_using_default_name": True,
        "name": "Profil 1",
    }  # fmt: skip
    entry.update(account)
    return {"profile": {"info_cache": {"Default": entry}}}


def _profile(tmp_path, local_state: dict, preferences: dict | None = None) -> Path:
    profile = tmp_path / "profil"
    (profile / "Default").mkdir(parents=True)
    (profile / "Local State").write_text(json.dumps(local_state), encoding="utf-8")
    prefs = {"account_info": None, "signin": {"allowed": True}, "account_tracker_service_last_update": "1"}
    prefs.update(preferences or {})
    (profile / "Default" / "Preferences").write_text(json.dumps(prefs), encoding="utf-8")
    return profile


def test_fresh_edge_profile_has_no_account(tmp_path):
    assert launch.profile_account(_profile(tmp_path, _fresh_local_state())) is None
    assert launch.account_found(0, _profile(tmp_path / "b", _fresh_local_state())) is None


@pytest.mark.parametrize(
    ("account", "preferences", "where"),
    [
        ({"user_name": "julien@example.com"}, None, "user_name"),
        ({"gaia_id": "1234"}, None, "gaia_id"),
        ({"edge_account_type": 1, "edge_account_cid": "abcd"}, None, "edge_account_"),
        ({}, {"account_info": [{"email": "julien@example.com", "gaia": "1"}]}, "account_info"),
        ({}, {"google": {"services": {"last_username": "julien@example.com"}}}, "last_username"),
    ],
)
def test_account_in_profile_is_detected_without_echoing_it(tmp_path, account, preferences, where):
    reason = launch.profile_account(_profile(tmp_path, _fresh_local_state(**account), preferences))
    assert reason and where in reason and "julien@" not in reason and "abcd" not in reason


def test_account_check_before_act_kills_the_devtools_browser(tmp_path, monkeypatch):
    killed = []
    sess = session.Session()
    sess.devtools[999] = 0
    sess.profiles[999] = _profile(tmp_path, _fresh_local_state())
    # Identité relevée à l'enregistrement (register_devtools) : même heure de création au moment du contrôle.
    sess.identities[999] = 1234
    monkeypatch.setattr(session, "_open", lambda pid: 77)
    monkeypatch.setattr(session, "_close", lambda handle: None)
    monkeypatch.setattr(session, "_started_at", lambda handle: 1234)
    monkeypatch.setattr(session, "_wait_exit", lambda handle, timeout_ms=0: None)
    monkeypatch.setattr(w, "window_pid", lambda h: 999)
    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    monkeypatch.setattr(launch, "kill_tree", killed.append)
    monkeypatch.setattr(session.log, "event", lambda *a, **k: None)
    sess.check_account(HWND)  # profil neuf : rien
    assert killed == []
    sess.profiles[999] = _profile(tmp_path / "b", _fresh_local_state(edge_account_type=1))  # connexion implicite
    with pytest.raises(launch.AccountError, match="closed at once"):
        sess.check_account(HWND)
    assert killed == [999] and 999 not in sess.profiles and 999 not in sess.devtools
    sess.check_account(HWND)  # plus rien d'enregistré pour ce processus


def test_account_guard_repeats_the_check_after_launch(tmp_path, monkeypatch):
    profile = _profile(tmp_path, _fresh_local_state())
    killed = []

    class Proc:
        pid = 4321

        def poll(self):
            return 0 if killed else None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(launch, "ACCOUNT_POLL_S", 0.02)
    monkeypatch.setattr(launch, "kill_tree", killed.append)
    monkeypatch.setattr(launch.log, "event", lambda *a, **k: None)
    thread = launch.guard_account(as_popen(Proc()), 0, profile)
    time.sleep(0.1)
    assert killed == []  # profil neuf
    (profile / "Local State").write_text(json.dumps(_fresh_local_state(user_name="x@y")), encoding="utf-8")
    thread.join(2)
    assert killed == [4321] and not thread.is_alive()


def test_account_guard_never_kills_an_exited_process(tmp_path, monkeypatch):
    profile = _profile(tmp_path, _fresh_local_state(user_name="x@y"))
    killed = []

    class Exited:
        pid = 4321

        def poll(self):
            return 0  # navigateur déjà fermé : son pid a pu être réattribué

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(launch, "ACCOUNT_POLL_S", 0.01)
    monkeypatch.setattr(launch, "kill_tree", killed.append)
    launch.guard_account(as_popen(Exited()), 0, profile).join(2)
    assert killed == []


# --- must_fix 8 : hôtes d'exécution système, AppID, applications empaquetées -----------------------------------
@pytest.mark.parametrize(
    ("exe", "args"),
    [
        (r"C:\Windows\System32\SyncAppvPublishingServer.exe", "n; calc"),
        (r"C:\Windows\System32\cmstp.exe", "/au x.inf"),
        (r"C:\Windows\System32\InfDefaultInstall.exe", "x.inf"),
        (r"C:\Windows\hh.exe", "x.chm"),
        (r"C:\Windows\System32\mshta.exe", "x"),
        (r"C:\Tcl\bin\wish86t.exe", ""),
        (r"C:\Program Files\Git\git-bash.exe", "-c echo"),
        (r"C:\Program Files\Git\git-cmd.exe", ""),
        (r"C:\Windows\System32\control.exe", "x.cpl"),
        (r"C:\Python\pythonw3.14.exe", ""),
    ],
)
def test_system_execution_hosts_allowed(exe, args):
    """Accord du 18/10 : hôtes d'exécution permis, le shell de Claude fait déjà tout (non-régression)."""
    policy.check_launch_exe(exe, args)


def test_control_panel_alone_allowed():
    policy.check_launch_exe(r"C:\Windows\System32\control.exe", "")


@pytest.mark.parametrize(
    ("app_id", "name"),
    [
        ("MicrosoftCorporationII.QuickAssist_8wekyb3d8bbwe!App", "Assistance rapide"),
        ("Microsoft.WinDbg_8wekyb3d8bbwe!Microsoft.WinDbg", "WinDbg"),
        ("Microsoft.PowerAutomateDesktop_8wekyb3d8bbwe!PAD.Console", "Power Automate"),
        ("AnthropicPBC.Claude_fnn82j28hfe8t!Claude", "Claude"),
        ("Microsoft.AutoGenerated.{0FDD9C6B-656B-0F1F-AB08-B1CDBB7067B3}", "Git Bash"),
    ],
)
def test_app_ids_allowed(app_id, name):
    policy.check_launch_app_id(app_id, name)


def test_packaged_app_switches_and_devtools_refused():
    calc = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
    policy.check_launch_app_id(calc, "Calculatrice")
    with pytest.raises(policy.PolicyError, match="devtools"):
        policy.check_launch_app_id(calc, "Calculatrice", devtools=True)
    for args in ("/remote-debugging-port=9222", "-user-data-dir=C:\\x", "x -- y"):
        with pytest.raises(policy.PolicyError):
            policy.check_launch_app_id(calc, "Calculatrice", args)


def test_packaged_launch_never_receives_a_devtools_port(monkeypatch):
    activated = []
    monkeypatch.setattr(launch, "find_start_app", lambda app: {"Name": "Calculatrice", "AppID": "X.Calc_8w!App"})
    monkeypatch.setattr(launch, "_activate_packaged", lambda app_id, args: activated.append(args) or 1)
    with pytest.raises(policy.PolicyError, match="devtools"):
        launch.launch("Calculatrice", devtools=True)
    assert activated == []


# --- must_fix 9 : la fenêtre lancée est celle de l'arbre lancé -------------------------------------------------
@pytest.fixture
def desktop(monkeypatch):
    """Bureau simulé : fenêtres (hwnd → pid, titre), processus (pid → nom), descendants de l'arbre lancé."""
    state = {
        "windows": {},
        "names": {1111: "msedge.exe", 4242: "msedge.exe", 50: "CalculatorApp.exe"},
        "tree": {1111},
        "fg": 0,
        "gave_back": 0,
        "classes": {},
        "children": {},
    }
    monkeypatch.setattr(w, "window_pid", lambda h: state["windows"][h][0])
    monkeypatch.setattr(w, "class_name", lambda h: state["classes"].get(h, "Fenetre"))
    monkeypatch.setattr(w, "window_text", lambda h: state["windows"][h][1])
    monkeypatch.setattr(w, "enum_children", lambda h: state["children"].get(h, []))
    monkeypatch.setattr(w, "is_cloaked", lambda h: state["classes"].get(h) == launch.UWP_CORE_CLASS)
    monkeypatch.setattr(w, "enum_windows", lambda: list(state["windows"]))
    monkeypatch.setattr(w, "process_name", lambda p: state["names"].get(p, f"pid {p}"))
    monkeypatch.setattr(w, "foreground", lambda: state["fg"])
    monkeypatch.setattr(w, "root", lambda h: h)
    # Une CoreWindow de premier niveau est masquée (cloaked) : jamais une fenêtre d'application.
    monkeypatch.setattr(launch.snapshot, "is_app_window", lambda h: state["classes"].get(h) != launch.UWP_CORE_CLASS)
    monkeypatch.setattr(launch, "descendants", lambda pid: set(state["tree"]) | {pid})
    monkeypatch.setattr(launch.foreground, "user_is_switching", lambda: False)
    monkeypatch.setattr(
        launch.foreground,
        "give_back",
        lambda prev, exclude=None: state.update(gave_back=state["gave_back"] + 1) or True,
    )
    return state


def test_related_strict_never_takes_a_foreign_window(desktop):
    desktop["windows"][900] = (4242, "Boîte de réception - Microsoft Edge")  # Edge de Julien, hors de l'arbre
    assert not launch._related(900, {1111, 1112}, "", "msedge", strict=True)
    assert launch._related(900, {1111, 1112}, "", "msedge", strict=False)  # repli : réservé aux cas sans pid


def test_new_window_ignores_julien_edge_while_the_browser_starts(desktop):
    desktop["windows"][900] = (4242, "Nouvel onglet - Microsoft Edge")
    desktop["fg"] = 900  # Julien vient d'ouvrir une fenêtre dans SON Edge pendant l'attente
    hwnd, took_back = launch._new_window(
        set(), 1111, "", "msedge", time.monotonic() + 0.3, user_front=77, fallback=False, running=lambda: True
    )
    assert hwnd == 0 and desktop["gave_back"] == 0  # ni prise pour la fenêtre lancée, ni premier plan repris
    desktop["windows"][901] = (1111, "Labo Web")
    assert launch._new_window(set(), 1111, "", "msedge", time.monotonic() + 0.3, 77, False, lambda: True)[0] == 901


def test_new_window_stops_early_when_the_launcher_left_nothing(desktop):
    desktop["windows"][900] = (4242, "Microsoft Edge")
    desktop["tree"] = set()
    t0 = time.monotonic()
    hwnd, _ = launch._new_window(set(), 1111, "", "msedge", t0 + 5, 0, fallback=False, running=lambda: False)
    assert hwnd == 0 and time.monotonic() - t0 < 1  # navigateur passé à une instance déjà ouverte : pas d'attente


def test_launcher_stub_falls_back_to_the_store_app(desktop):
    desktop["windows"][800] = (50, "Calculatrice")  # CalculatorApp, activée hors de l'arbre de calc.exe
    desktop["tree"] = set()
    alive = {"v": True}
    assert launch._new_window(set(), 2222, "", "calc", time.monotonic() + 0.2, 0, True, lambda: alive["v"])[0] == 0
    alive["v"] = False  # le lanceur calc.exe s'est terminé sans descendant
    assert launch._new_window(set(), 2222, "", "calc", time.monotonic() + 0.2, 0, True, lambda: alive["v"])[0] == 800


def test_packaged_uwp_frame_found_when_its_corewindow_is_not_a_child(desktop):
    """Relevé réel (Calculatrice lancée par ActivateApplication) : cadre ApplicationFrameWindow de l'hôte, sans enfant
    de l'application ; CoreWindow de CalculatorApp au premier niveau, masquée, du même titre."""
    desktop["names"].update({18588: "ApplicationFrameHost.exe", 3704: "CalculatorApp.exe"})
    desktop["windows"].update(
        {
            500: (18588, "Photos"),
            501: (18588, "Paramètres"),
            12782696: (18588, "Calculatrice"),
            37948162: (3704, "Calculatrice"),
        }
    )
    desktop["classes"].update(
        {500: "ApplicationFrameWindow", 501: "ApplicationFrameWindow", 12782696: "ApplicationFrameWindow",
         37948162: launch.UWP_CORE_CLASS}
    )  # fmt: skip
    desktop["tree"] = set()
    hwnd, _ = launch._new_window({500}, 3704, "Calculatrice", "", time.monotonic() + 0.3, 0, fallback=True)
    assert hwnd == 12782696
    # Un autre cadre du même hôte (fenêtre Store ouverte par Julien pendant l'attente) n'est jamais pris.
    assert not launch._related(501, {3704}, "", "", strict=True)


def test_unshown_store_app_is_reported_precisely(desktop):
    """Lancée sans activation, une appli Store garde sa CoreWindow masquée hors du cadre : la note le dit, au lieu de
    promettre qu'un look suffira."""
    desktop["names"].update({18588: "ApplicationFrameHost.exe", 3704: "CalculatorApp.exe"})
    desktop["windows"].update({10: (18588, "Calculatrice"), 11: (3704, "Calculatrice"), 12: (18588, "")})
    desktop["classes"].update(
        {10: "ApplicationFrameWindow", 11: launch.UWP_CORE_CLASS, 12: "ApplicationFrameTitleBarWindow"}
    )
    desktop["children"][10] = [12]  # cadre sans enfant de l'application
    assert launch.uwp_unshown(10)
    desktop["children"][10] = [12, 11]  # CoreWindow rattachée au cadre : affichée
    assert not launch.uwp_unshown(10)
    assert not launch.uwp_unshown(11)  # pas un cadre UWP


def test_devtools_launch_without_its_window_kills_and_refuses(electron_app, monkeypatch):
    killed = []

    class Proc:
        pid = 5555

        def poll(self):
            return None

    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(launch, "_new_window", lambda *a, **k: (0, False))
    monkeypatch.setattr(launch, "kill_tree", killed.append)
    monkeypatch.setattr(launch, "descendants", lambda pid: {pid})
    monkeypatch.setattr(w, "foreground", lambda: 0)
    monkeypatch.setattr(w, "enum_windows", lambda: [])
    with pytest.raises(launch.LaunchError, match="no window of the launched process"):
        launch.launch(electron_app, devtools=True, wait_s=1)
    assert killed == [5555]


# --- must_fix 14 : nom de processus jamais servi d'un cache --------------------------------------------------------
def test_process_name_reread_for_a_reused_pid_and_failures_not_cached(monkeypatch):
    state = {"open": False, "name": r"C:\x\premier.exe"}

    def query(handle, flags, buf, size):
        buf.value = state["name"]
        return True

    monkeypatch.setattr(w, "OpenProcess", lambda access, inherit, pid: 77 if state["open"] else 0)
    monkeypatch.setattr(w, "QueryFullProcessImageNameW", query)
    monkeypatch.setattr(w, "CloseHandle", lambda h: True)
    assert w.process_name(4000) == "pid 4000"  # OpenProcess refusé : échec
    state["open"] = True
    assert w.process_name(4000) == "premier.exe"  # l'échec n'a pas été retenu
    state["name"] = r"C:\x\gestionnaire-mdp.exe"  # même pid réattribué à un autre programme
    assert w.process_name(4000) == "gestionnaire-mdp.exe"


# --- must_fix 18 : DevTools mort ou occupé, jamais d'erreur interne --------------------------------------------------
TITLE = "Labo Web"


class FakeDevTools:
    """Point DevTools local : liste HTTP, WebSocket qui répond (ok), se tait (hang) ou ferme l'onglet."""

    def __init__(self) -> None:
        from websockets.sync.server import serve

        self.mode = "ok"
        self.list_body = None
        self.conns: list = []
        self.ws = serve(self._handler, "127.0.0.1", 0)
        self.ws_port = self.ws.socket.getsockname()[1]
        threading.Thread(target=self.ws.serve_forever, daemon=True).start()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/json/version"):
                    body = json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{outer.ws_port}/devtools/browser/x"})
                else:
                    body = outer.list_body or json.dumps(
                        [
                            {
                                "type": "page",
                                "title": TITLE,
                                "webSocketDebuggerUrl": f"ws://127.0.0.1:{outer.ws_port}/devtools/page/1",
                            }
                        ]
                    )
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args) -> None:
                pass

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def _handler(self, conn) -> None:
        self.conns.append(conn)
        for raw in conn:
            message = json.loads(raw)
            if self.mode == "hang":
                continue  # moteur de rendu bloqué (alert ouverte, script long)
            result = {"result": {"type": "object", "value": ["visible", TITLE]}}
            conn.send(json.dumps({"id": message["id"], "result": result}))

    def kill_tab(self) -> None:
        deadline = time.monotonic() + 2
        while not self.conns and time.monotonic() < deadline:
            time.sleep(0.01)
        for conn in self.conns:
            conn.close()
        time.sleep(0.2)

    def stop(self) -> None:
        self.ws.shutdown()
        self.http.shutdown()


@pytest.fixture
def devtools(monkeypatch):
    fake = FakeDevTools()
    sess = session.Session()
    monkeypatch.setattr(server, "SESSION", sess)
    monkeypatch.setattr(w, "window_pid", lambda h: 999)
    monkeypatch.setattr(w, "window_text", lambda h: TITLE + " - Microsoft Edge")
    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    sess.devtools[999] = fake.port
    auto = FakeAuto()
    view = make_view(node("Button", "OK", has_invoke=True, framework="Chrome"))

    def click() -> str:
        ex = actions.Executor(as_auto(auto), HWND, lambda rebuild: view, lambda: sess.page(HWND))
        ex._pids = {7}
        ex._windows = {HWND}
        ex._effect = lambda e, fn, settle_s=1.0: fn  # effet du clic hors sujet ici (voir test_fix_U)
        report, _ = server._run_steps(ex, [server.Step(do="click", target="OK")], HWND, server.log.Call("act", {}))
        return report[0]

    yield fake, sess, auto, click
    sess.forget_all()
    fake.stop()


def test_page_transport_errors_are_typed(devtools):
    fake, *_ = devtools
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)  # connect() tenu en gestionnaire de contexte
        page = cdp.Page(fake.port, match=TITLE)
        assert page.evaluate("1") == ["visible", TITLE]
    fake.mode = "hang"
    t0 = time.perf_counter()
    with pytest.raises(cdp.PageBusy, match="did not answer"):
        page.evaluate("1", timeout_s=0.2)
    assert time.perf_counter() - t0 < 1.5
    fake.kill_tab()
    with pytest.raises(cdp.PageGone):
        page.evaluate("1")
    page.close()  # déjà fermée : sans erreur


def test_closed_tab_reconnects_and_never_internal_error(devtools):
    fake, sess, auto, click = devtools
    first = sess.page(HWND)
    assert first is not None
    fake.kill_tab()
    page = sess.page(HWND)  # sonde : PageGone → connexion jetée et refaite
    assert page is not None and page is not first and fake.port not in sess.dead_ports
    fake.kill_tab()
    line = click()
    assert "internal error" not in line.lower() and "UIA Invoke" in line and auto.calls


def test_busy_page_is_reported_fast_and_port_stays_alive(devtools):
    fake, sess, auto, click = devtools
    assert sess.page(HWND) is not None
    fake.mode = "hang"
    t0 = time.perf_counter()
    with pytest.raises(cdp.PageBusy):
        sess.page(HWND)
    assert time.perf_counter() - t0 < 2 * session.PROBE_S + 0.5
    assert fake.port not in sess.dead_ports and HWND in sess.pages  # ni port mort, ni nouvelle connexion
    t0 = time.perf_counter()
    line = click()
    assert "internal error" not in line.lower() and time.perf_counter() - t0 < 3
    # Sans résolution paresseuse de la page dans click_element (actions.py) : refus explicite ; avec : UIA Invoke.
    assert "UIA Invoke" in line or ("✗" in line and "did not answer" in line)
    fake.mode = "ok"
    assert "UIA Invoke" in click() and fake.port not in sess.dead_ports


def test_invalid_devtools_list_is_a_cdp_error(devtools):
    fake, sess, *_ = devtools
    fake.list_body = "<html>pas du JSON</html>"
    with pytest.raises(cdp.CDPError, match="unreachable"):
        cdp.targets(fake.port)
    fake.list_body = json.dumps({"pas": "une liste"})
    with pytest.raises(cdp.CDPError):
        cdp.targets(fake.port)
    # Page sans titre, entrée qui n'est pas un objet, point WebSocket qui répond en HTTP simple (poignée refusée).
    url = f"ws://127.0.0.1:{fake.port}/pas-un-websocket"
    fake.list_body = json.dumps([{"type": "page", "title": None, "webSocketDebuggerUrl": url}, 3])
    assert cdp.pick_page(cdp.targets(fake.port), "x")["title"] is None
    assert sess.page(HWND) is None  # connexion impossible : pas de DevTools pour cette action, pas d'exception
    assert fake.port in sess.dead_ports


def test_devtools_dead_code_removed():
    assert not hasattr(cdp.Page, "screenshot_png") and not hasattr(cdp.Page, "viewport")
    assert not hasattr(capture, "fit")  # capture_screen revenue en 0.14.0 : look(window="screen")


# --- must_fix 19 : fin de capture WGC sans fuite --------------------------------------------------------------------
class FakeControl:
    def __init__(self, finished: bool) -> None:
        self.finished = finished
        self.calls: list[str] = []

    def is_finished(self) -> bool:
        return self.finished

    def wait(self) -> None:
        self.calls.append("wait")

    def stop(self) -> None:
        self.calls.append("stop")


def test_wgc_ending_joins_after_a_frame_and_stops_otherwise(monkeypatch):
    done = threading.Event()
    done.set()
    control = FakeControl(finished=True)
    capture._end_wgc(control, done)
    assert control.calls == ["wait"]  # jamais un second stop() natif après celui du rappel (fuite de handles)
    control = FakeControl(finished=False)
    capture._end_wgc(control, threading.Event())
    assert control.calls == ["stop"]  # délai dépassé : la capture tourne encore
    monkeypatch.setattr(capture, "WGC_JOIN_S", 0.05)
    control = FakeControl(finished=False)
    t0 = time.monotonic()
    capture._end_wgc(control, done)  # fil bloqué : attente bornée, puis arrêt
    assert control.calls == ["stop"] and time.monotonic() - t0 < 1


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
        (name, ctypes.c_size_t)
        for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
            "PrivateUsage",
        )
    ]  # fmt: skip


def _handles_and_private_mb() -> tuple[int, float]:
    gc.collect()
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    process = kernel32.GetCurrentProcess()
    count = ctypes.c_ulong()
    kernel32.GetProcessHandleCount(ctypes.c_void_p(process), ctypes.byref(count))
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    ctypes.WinDLL("psapi").GetProcessMemoryInfo(ctypes.c_void_p(process), ctypes.byref(pmc), pmc.cb)
    return count.value, pmc.PrivateUsage / 2**20


@pytest.mark.live
# Depuis le 19/10/2026, WGC passe par wgc_helper : la fuite intermittente de windows-capture 2.0.1 (~1 handle noyau
# par capture, recheck6/7) reste dans ce processus d'appoint, arrêté après une minute sans usage, plus dans le serveur.
def test_wgc_120_captures_do_not_leak():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lab"))
    import apps

    app = apps.launch("win32", x=140, y=140)
    try:
        for _ in range(5):
            capture.capture_wgc(app.hwnd)  # échauffement : imports, premières allocations
        handles0, private0 = _handles_and_private_mb()
        for _ in range(120):
            assert capture.capture_wgc(app.hwnd).image.size[0] > 0
        handles1, private1 = _handles_and_private_mb()
    finally:
        app.close()
    assert abs(handles1 - handles0) <= 10, (handles0, handles1)
    assert abs(private1 - private0) <= 20, (round(private0, 1), round(private1, 1))


# --- must_fix 23 : lancement Store, contenu UIA stable avant de rendre la main ---------------------------------------
def _uwp_view(pids: list[int]) -> snapshot.View:
    return make_view(*[node("Button", f"B{i}", pid=pid) for i, pid in enumerate(pids)])


def test_content_count_ignores_the_uwp_frame(monkeypatch):
    monkeypatch.setattr(w, "window_pid", lambda h: 10)
    monkeypatch.setattr(w, "process_name", lambda p: "ApplicationFrameHost.exe" if p == 10 else "CalculatorApp.exe")
    assert launch._content_count(_uwp_view([10, 10, 10, 10])) == 0  # cadre seul (titre, boutons de fenêtre)
    assert launch._content_count(_uwp_view([10, 10, 20, 20, 20])) == 3
    monkeypatch.setattr(w, "process_name", lambda p: "WinUIApp.exe")
    assert launch._content_count(_uwp_view([10, 10])) == 2


def test_wait_content_until_stable(monkeypatch):
    monkeypatch.setattr(w, "window_pid", lambda h: 10)
    monkeypatch.setattr(w, "process_name", lambda p: "ApplicationFrameHost.exe" if p == 10 else "CalculatorApp.exe")
    monkeypatch.setattr(launch, "CONTENT_GAP_S", 0.01)
    frames = [[10] * 4, [10] * 4, [10] * 4 + [20] * 30, [10] * 4 + [20] * 80, [10] * 4 + [20] * 80]
    built = []

    def build(auto, hwnd):
        built.append(1)
        return _uwp_view(frames[min(len(built), len(frames)) - 1])

    monkeypatch.setattr(launch.snapshot, "build_view", build)
    assert launch.wait_content(as_auto(FakeAuto()), HWND, limit_s=2)
    assert len(built) == 5  # pas avant que le contenu (80 éléments) se soit stabilisé
    built.clear()
    frames[:] = [[10] * 4]
    t0 = time.monotonic()
    assert not launch.wait_content(as_auto(FakeAuto()), HWND, limit_s=0.2)  # cadre seul, stable : jamais « prêt »
    assert time.monotonic() - t0 < 1

    def broken(auto, hwnd):
        raise uia.UIAError("fenêtre en construction")

    monkeypatch.setattr(launch.snapshot, "build_view", broken)
    assert not launch.wait_content(as_auto(FakeAuto()), HWND, limit_s=0.1)


# --- Mineurs : profils jetables, entrées invalides --------------------------------------------------------------------
def test_profiles_pruned_only_when_marked_old_and_real_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "PROFILES", tmp_path)
    fresh = launch.fresh_profile()
    assert (fresh / launch.PROFILE_MARKER).is_file()  # marqueur écrit dès la création
    old = time.time() - launch.PROFILE_MIN_AGE_S - 60

    def make(name: str, marked: bool = True, age: float = old) -> Path:
        path = tmp_path / name
        path.mkdir()
        if marked:
            (path / launch.PROFILE_MARKER).write_text("{}", encoding="utf-8")
            os.utime(path / launch.PROFILE_MARKER, (age, age))
        return path

    gone = make("20260101-000000-ancien")
    unmarked = make("20260101-000000-sans", marked=False)
    foreign = make("mes-donnees")
    recent = make("20260101-000000-recent", age=time.time() - 60)
    target = make("cible-hors-du-dossier")
    (target / "precieux.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "20260101-000000-jonction"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False)
    assert launch.prune_profiles() == 1
    assert not gone.exists() and unmarked.exists() and foreign.exists() and recent.exists() and fresh.exists()
    assert (target / "precieux.txt").exists()
    if made.returncode == 0:
        assert link.is_junction() and not launch._prunable(link, time.time() + 10**6)


def test_start_menu_timeout_is_a_launch_error(monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired("powershell", kwargs.get("timeout", 0.0))

    monkeypatch.setattr(launch, "_START_APPS", None)
    monkeypatch.setattr(launch, "_START_APPS_ERROR", "")
    monkeypatch.setattr(launch.subprocess, "run", slow)
    monkeypatch.setattr(launch.log, "event", lambda *a, **k: None)
    assert launch.start_apps() == []  # le préchargement du serveur ne lève jamais
    with pytest.raises(launch.LaunchError, match="Start menu unreadable"):
        launch.find_start_app("Calculatrice")


def test_start_menu_rows_are_validated(monkeypatch):
    class Out:
        stdout = json.dumps([{"Name": "Calculatrice", "AppID": "X!App"}, {"Name": None, "AppID": "Y"}, "z"])

    monkeypatch.setattr(launch, "_START_APPS", None)
    monkeypatch.setattr(launch, "_START_APPS_ERROR", "")
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: Out())
    assert launch.find_start_app("calc") == {"Name": "Calculatrice", "AppID": "X!App"}


@pytest.mark.live
def test_wgc_runs_in_a_helper_that_frees_its_memory(monkeypatch):
    """19/10/2026 : la capture WGC chargeait le pilote graphique et opencv DANS le serveur (+770 Mo privés, +25 fils,
    jamais rendus). Elle passe par wgc_helper : le serveur reste léger, l'appoint s'arrête seul après inactivité."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lab"))
    import apps

    def private_mb(pid: int) -> float:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        ctypes.WinDLL("psapi").GetProcessMemoryInfo(ctypes.c_void_p(handle), ctypes.byref(pmc), pmc.cb)
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return pmc.PrivateUsage / 2**20

    monkeypatch.setenv("NEXUS_WGC_IDLE_S", "2")
    if capture._HELPER is not None:  # appoint d'un test précédent, lancé avec l'attente par défaut
        capture._HELPER.kill()
        capture._HELPER = None
    app = apps.launch("win32", x=140, y=140)
    try:
        _, private0 = _handles_and_private_mb()
        for _ in range(3):
            assert capture.capture_wgc(app.hwnd).image.size[0] > 0
        _, private1 = _handles_and_private_mb()
        helper = capture._HELPER
        assert helper is not None and helper.poll() is None
        # Le poids est bien chez l'appoint (lanceur du venv + vrai Python, son enfant).
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={helper.pid}').ProcessId",
            ],
            capture_output=True,
            text=True,
        ).stdout.split()
        assert private_mb(helper.pid) + sum(private_mb(int(p)) for p in out) > 100
    finally:
        app.close()
    assert private1 - private0 < 30, (round(private0, 1), round(private1, 1))
    helper.wait(10)  # arrêt seul après NEXUS_WGC_IDLE_S sans demande
