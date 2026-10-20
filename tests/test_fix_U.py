"""Constats du 2026-10-14 sur Steam (fenêtres SDL de steamwebhelper.exe, interface CEF) :
1. fenêtre recouverte : rien de dessiné ni publié → Steam relancé avec ses commutateurs -cef-disable-occlusion
   -cef-force-accessibility ; cadre SDL qui loge CEF reconnu comme Chromium ;
2. UIA Select accepté sans effet sur les onglets React → Invoke / action par défaut d'abord, effet relu, technique
   suivante si rien ne change ;
3. foreground=true ne basculait jamais vers la vraie souris → repli quand les messages sont ignorés ;
4. premier plan rendu après chaque étape → fenêtre gardée devant tout le lot (rendu final compris) ;
plus le canal DevTools de Steam (-cef-enable-debugging), la page DevTools choisie par document, les menus CEF lus
dans leurs surgissantes et les doublons d'un même contrôle.
"""

from __future__ import annotations

import pytest
from test_contract import HWND, FakeAuto, accorded, as_auto, make_view, node

from nexus_windows import actions, foreground, launch, physical, policy, realinput, session, snapshot, steam, uia
from nexus_windows import win32 as w


def chrome(role: str, name: str, **props) -> uia.Node:
    return node(role, name, framework="Chrome", **props)


def chromium_executor(view, auto, allow_foreground: bool = False) -> actions.Executor:
    ex = actions.Executor(as_auto(auto), HWND, lambda rebuild: view, lambda *a: None, allow_foreground=allow_foreground)
    ex._pids = {7}
    ex._windows = {HWND}
    return ex


class ReactAuto(FakeAuto):
    """Onglet React de Steam : Select accepté sans rien faire ; action par défaut (clic DOM) ou Invoke sur l'icône
    enfant sélectionnent l'onglet."""

    def __init__(self, tab, works: set[str]) -> None:
        super().__init__()
        self.tab = tab
        self.works = works

    def select(self, element) -> None:
        self.calls.append(("select", element))  # accepté, sans effet

    def legacy_default_action(self, element) -> None:
        self.calls.append(("legacy", element))
        if "legacy" in self.works:
            self.tab.element.live["selected"] = True

    def invoke(self, element) -> None:
        self.calls.append(("invoke", element))
        if "invoke" in self.works:
            self.tab.element.live["selected"] = True


def react_tab(selected: bool = False):
    tab = chrome("TabItem", "Stockage", has_selection_item=True, has_legacy=True, selected=selected)
    icon = chrome("Image", "", has_invoke=True)
    icon.parent = tab
    tab.children = [icon]
    return tab, icon


# --- 1. Cadre SDL qui loge CEF ----------------------------------------------------------------------------------
def test_sdl_frame_hosting_cef_is_chromium(monkeypatch):
    classes = {1: "SDL_app", 2: "CefBrowserWindow", 3: "Chrome_RenderWidgetHostHWND", 9: "SDL_app"}
    monkeypatch.setattr(w, "class_name", lambda h: classes.get(h, ""))
    monkeypatch.setattr(w, "enum_children", lambda h: [2, 3] if h == 1 else [])
    assert snapshot.framework_of(1) == "Chromium"
    assert snapshot.framework_of(9) == "SDL"  # jeu SDL sans navigateur embarqué


def test_native_window_with_webview_keeps_its_family(monkeypatch):
    monkeypatch.setattr(w, "class_name", lambda h: {1: "HwndWrapper[x]", 3: "Chrome_RenderWidgetHostHWND"}.get(h, ""))
    monkeypatch.setattr(w, "enum_children", lambda h: [3])
    assert snapshot.framework_of(1) == "WPF"


# --- 2. Clic Chromium vérifié ------------------------------------------------------------------------------------
def test_react_tab_uses_default_action_not_select_and_reads_selection():
    tab, _icon = react_tab()
    auto = ReactAuto(tab, {"legacy"})
    outcome = chromium_executor(make_view(tab), auto).click("Stockage")
    assert outcome.ok and outcome.verified and "read back ✓ selected" in outcome.line
    assert [c[0] for c in auto.calls] == ["legacy"]  # jamais Select d'abord


def test_no_effect_moves_to_the_clickable_child():
    tab, icon = react_tab()
    auto = ReactAuto(tab, {"invoke"})
    outcome = chromium_executor(make_view(tab), auto).click("Stockage")
    assert outcome.ok and outcome.verified and "child" in outcome.line
    assert [c[0] for c in auto.calls] == ["legacy", "invoke"] and auto.calls[1][1] is icon.element
    assert "no effect observed" in " ".join(outcome.attempts)


def test_invoke_and_default_action_are_never_both_played():
    """Invoke et action par défaut sont la même action DOM en Chromium : sans effet relu, jamais rejouée."""
    button = chrome("Button", "Envoyer", has_invoke=True, has_legacy=True)
    auto = FakeAuto()
    ex = chromium_executor(make_view(button), auto)
    ex._watched = _ignored(still=True)
    accorded(ex, lambda: ex.click("Envoyer"))  # « Envoyer » : geste sortant, accord lié à l'action exacte
    with pytest.raises(actions.Unconfirmed):
        ex.click("Envoyer")
    assert [c[0] for c in auto.calls] == ["invoke"]


def test_already_selected_tab_is_not_clicked_again():
    tab, _icon = react_tab(selected=True)
    auto = ReactAuto(tab, set())
    outcome = chromium_executor(make_view(tab), auto).click("Stockage")
    assert outcome.ok and outcome.verified and auto.calls == []


def test_change_description():
    ex = chromium_executor(make_view(chrome("Button", "x")), FakeAuto())
    base = ((None, None, 0, "x", None, True), ((1, "Steam"),), None)
    assert ex._describe_change(base, base) == ""
    opened = (base[0], ((1, "Steam"), (2, "Paramètres Steam")), None)
    assert "Paramètres Steam" in ex._describe_change(base, opened)
    expanded = ((None, None, 1, "x", None, True), base[1], None)
    assert ex._describe_change(base, expanded) == "read back ✓ expanded"
    assert "disappeared" in ex._describe_change(base, (("gone",), base[1], None))


# --- 3. foreground=true : repli vers la vraie souris quand les messages sont ignorés -----------------------------
def _ignored(still: bool):
    def watched(fn, prepare=None, settle_s=0.6):
        def run():
            raise actions.NoVisibleEffect("aucun effet visible", lambda: still)

        return run

    return watched


def test_ignored_messages_fall_back_to_real_mouse_when_allowed(monkeypatch):
    target = chrome("Text", "Steam")
    auto = FakeAuto()
    ex = chromium_executor(make_view(target), auto, allow_foreground=True)
    ex._watched = _ignored(still=True)
    real = []
    monkeypatch.setattr(realinput, "click", lambda x, y, button="left", count=1: real.append((x, y)))
    monkeypatch.setattr(realinput, "borrowed", lambda hwnd, windows=None: _null())
    outcome = ex.click("Steam")
    assert outcome.ok and outcome.method == "foreground" and real


def test_late_effect_is_not_replayed_with_real_mouse(monkeypatch):
    target = chrome("Text", "Steam")
    ex = chromium_executor(make_view(target), FakeAuto(), allow_foreground=True)
    ex._watched = _ignored(still=False)
    monkeypatch.setattr(realinput, "click", lambda *a, **k: pytest.fail("vraie souris après un effet tardif"))
    outcome = ex.click("Steam")
    assert outcome.ok and "delayed" in outcome.line


def test_ignored_messages_without_permission_stay_unconfirmed():
    target = chrome("Text", "Steam")
    ex = chromium_executor(make_view(target), FakeAuto())
    ex._watched = _ignored(still=True)
    with pytest.raises(actions.Unconfirmed):
        ex.click("Steam")


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --- 4. Lot gardé au premier plan --------------------------------------------------------------------------------
@pytest.fixture
def desk(monkeypatch):
    state = {"fg": 100, "given": [], "adopted": 0, "since": False}
    monkeypatch.setattr(foreground, "wait_typing_pause", lambda *a, **k: True)
    monkeypatch.setattr(realinput.worker, "check_cancelled", lambda: None)
    monkeypatch.setattr(w, "foreground", lambda: state["fg"])
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(w, "cursor_pos", lambda: (5, 5))

    def front(hwnd):
        state["fg"] = hwnd
        return True

    def give_back(prev, timeout_s=0.6, exclude=None):
        state["given"].append(prev)
        state["fg"] = prev
        return True

    monkeypatch.setattr(realinput, "bring_to_front", front)
    monkeypatch.setattr(foreground, "give_back", give_back)
    monkeypatch.setattr(foreground, "adopt_foreground", lambda: state.__setitem__("adopted", state["adopted"] + 1))
    monkeypatch.setattr(physical, "available", lambda: True)
    monkeypatch.setattr(physical, "since", lambda t: state["since"])
    return state


def test_hold_keeps_target_in_front_for_the_whole_batch(desk):
    with realinput.held(HWND, {HWND}) as hold:
        assert desk["fg"] == HWND and realinput.current_hold() is hold
        desk["fg"] = 555  # une autre fenêtre s'intercale entre deux étapes
        hold.checkpoint()
        assert desk["fg"] == HWND  # remise devant
    assert desk["given"] == [100] and desk["fg"] == 100 and realinput.current_hold() is None
    assert desk["adopted"] >= 2  # surveillances : adoptent la cible, puis la fenêtre de Julien


def test_hold_stops_when_julien_takes_over_and_leaves_him_the_foreground(desk):
    with pytest.raises(foreground.UserInterrupted):
        with realinput.held(HWND, {HWND}) as hold:
            desk["since"] = True
            hold.checkpoint()
    assert desk["given"] == []  # premier plan laissé à Julien, rien ne lui est imposé


def test_hold_gives_back_on_error(desk):
    with pytest.raises(RuntimeError):
        with realinput.held(HWND, {HWND}):
            raise RuntimeError("étape en échec")
    assert desk["given"] == [100]


# --- Steam : politique, lancement, DevTools --------------------------------------------------------------------
@pytest.mark.parametrize("args", ["-cef-enable-debugging", "--devtools-port=9", "/cef-enable-debugging"])
def test_model_can_never_open_steam_devtools_itself(args):
    assert "opens a control channel" in (policy.switch_refusal(args, chromium=False, browser=False) or "")


def test_view_switches_are_allowed():
    assert (
        policy.switch_refusal("-cef-disable-occlusion -cef-force-accessibility", chromium=False, browser=False) is None
    )


def test_steam_command_appends_tool_switches_after_model_args():
    cmd = steam.command(r"C:\S\steam.exe", "-silent", devtools=True)
    assert cmd.endswith("-silent -cef-disable-occlusion -cef-force-accessibility -cef-enable-debugging")
    assert "-cef-enable-debugging" not in steam.command(r"C:\S\steam.exe", "", devtools=False)


TABLE = [(10, 1, "steam.exe"), (11, 10, "steamwebhelper.exe"), (12, 11, "steamwebhelper.exe")]


def test_games_running_blocks_restart(monkeypatch):
    assert steam.games_running(TABLE) == []
    table = TABLE + [(13, 10, "Medieval2.exe")]
    assert steam.games_running(table) == ["Medieval2.exe"]
    monkeypatch.setattr(steam, "_table", lambda: table)
    with pytest.raises(steam.SteamError, match="Medieval2.exe"):
        steam.shutdown(r"C:\S\steam.exe")


def test_running_steam_without_devtools_needs_restart(monkeypatch):
    monkeypatch.setattr(steam, "steam_pids", lambda table=None: [10])
    monkeypatch.setattr(steam, "debugging_port", lambda: None)
    monkeypatch.setattr(policy, "check_launch_exe", lambda *a, **k: None)
    with pytest.raises(launch.LaunchError, match="restart=true"):
        launch.launch_steam(r"C:\S\steam.exe", "", True, False, 5, 0, set())


def test_restart_is_refused_before_closing_steam_when_port_is_taken(monkeypatch):
    monkeypatch.setattr(steam, "steam_pids", lambda table=None: [10])
    monkeypatch.setattr(steam, "port_taken_by_other", lambda: "node.exe")
    monkeypatch.setattr(policy, "check_launch_exe", lambda *a, **k: None)
    monkeypatch.setattr(steam, "shutdown", lambda exe: pytest.fail("Steam fermé alors que le port est pris"))
    with pytest.raises(launch.LaunchError, match="node.exe"):
        launch.launch_steam(r"C:\S\steam.exe", "", True, True, 5, 0, set())


def test_restart_only_for_steam():
    with pytest.raises(launch.LaunchError, match="Steam only"):
        launch.launch(r"C:\Windows\notepad.exe", restart=True)


def test_steam_session_is_never_closed_by_account_guard(monkeypatch):
    sess = session.Session()
    sess.devtools[51] = 8080
    sess.own_sessions.add(51)
    monkeypatch.setattr(w, "window_pid", lambda h: 51)
    monkeypatch.setattr(launch, "kill_tree", lambda pid: pytest.fail("session Steam de Julien fermée"))
    monkeypatch.setattr(launch, "account_found", lambda *a: "page de connexion")
    sess.check_account(HWND)  # sans erreur ni fermeture


def test_is_steam_exe(tmp_path):
    exe = tmp_path / "steam.exe"
    exe.write_bytes(b"")
    assert not steam.is_steam_exe(str(exe))  # un steam.exe quelconque n'est pas le client Valve
    (tmp_path / "steamclient64.dll").write_bytes(b"")
    assert steam.is_steam_exe(str(exe)) and not steam.is_steam_exe(str(tmp_path / "game.exe"))


# --- Cibles : doublons d'un même contrôle, liste déroulante sans nom -----------------------------------------------
def test_nested_same_name_tab_is_one_target():
    outer = chrome("TabItem", "Stockage", has_selection_item=True)
    inner = chrome("TabItem", "Stockage", has_selection_item=True)
    inner.parent = outer
    outer.children = [inner]
    picked, _ = snapshot.pick(make_view(outer, inner), "onglet:Stockage")
    assert picked.node is outer


def test_unnamed_combobox_is_found_by_its_value():
    combo = chrome("ComboBox", "", value="Disque local (C:) 357 Go libres", has_expand=True)
    picked, note = snapshot.pick(make_view(combo), "Disque local")
    assert picked.node is combo and "partial match" in note


# --- Page DevTools par document (Steam : cadre et magasin dans une même fenêtre) --------------------------------
def test_viewport_follows_the_targeted_document(monkeypatch):
    rects = {31: w.Rect(0, 0, 1000, 60), 32: w.Rect(0, 60, 1000, 800)}
    monkeypatch.setattr(w, "enum_children", lambda h: [31, 32])
    monkeypatch.setattr(w, "class_name", lambda h: "Chrome_RenderWidgetHostHWND")
    monkeypatch.setattr(w, "window_rect", lambda h: rects[h])
    ex = chromium_executor(make_view(chrome("Button", "x")), FakeAuto())
    store = chrome("Document", "Bienvenue sur Steam", rect=(0, 70, 1000, 790))
    ex.document = store
    assert ex._web_viewport() == rects[32]
    ex.document = None
    assert ex._web_viewport() == rects[31]


def test_page_by_document_title_is_requested(monkeypatch):
    asked = []
    ex = actions.Executor(as_auto(FakeAuto()), HWND, lambda r: make_view(), lambda title=None: asked.append(title))
    ex.document = chrome("Document", "Bienvenue sur Steam")
    ex._get_page()
    ex.document = None
    ex._get_page()
    assert asked == ["Bienvenue sur Steam", None]


# --- Mise au premier plan refusée par un cadre d'application Store ------------------------------------------------
def test_activation_asks_store_frame_holder_to_hand_over(monkeypatch):
    state = {"fg": 77, "posted": []}
    monkeypatch.setattr(foreground, "_checkpoint", lambda: None)
    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    monkeypatch.setattr(w, "IsIconic", lambda h: False)
    monkeypatch.setattr(w, "foreground", lambda: state["fg"])
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(foreground, "_switch_to", lambda hwnd, timeout_s, ours=None: False)  # attache de file refusée
    monkeypatch.setattr(foreground, "system_foreground", lambda: "")

    def post(hwnd, msg, wparam, lparam):
        state["posted"].append((hwnd, msg, wparam, lparam))
        state["fg"] = lparam  # DefWindowProc : SetForegroundWindow(lParam)

    monkeypatch.setattr(w, "PostMessageW", post)
    assert foreground.activate(HWND)
    assert state["posted"] == [(77, foreground.WM_SYSCOMMAND, foreground.SC_HOTKEY, HWND)]


def test_no_hand_over_request_to_a_hidden_system_window(monkeypatch):
    monkeypatch.setattr(foreground, "_checkpoint", lambda: None)
    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    monkeypatch.setattr(w, "IsIconic", lambda h: False)
    monkeypatch.setattr(w, "foreground", lambda: 77)
    monkeypatch.setattr(foreground, "_switch_to", lambda hwnd, timeout_s, ours=None: False)
    monkeypatch.setattr(foreground, "system_foreground", lambda: "fenêtre système masquée « Recherche »")
    monkeypatch.setattr(w, "PostMessageW", lambda *a: pytest.fail("message à une fenêtre système"))
    assert not foreground.activate(HWND)
