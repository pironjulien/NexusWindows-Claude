"""Windows lui-même : adresses (ms-settings:…), applications Store cachées, surfaces et panneaux du shell,
notifications lues sans rien ouvrir, application figée sans blocage de la capture. Comprend les régressions des
relectures de Codex et d'un agent neuf sur 0.14.1."""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading

import pytest
from PIL import Image

from nexus_windows import capture, keys, notifications, policy, realinput, snapshot, worker
from nexus_windows import launch as launcher
from nexus_windows import win32 as w


def test_uri_scheme():
    assert launcher.uri_scheme("ms-settings:bluetooth") == "ms-settings"
    assert launcher.uri_scheme("mailto:alice@example.com?subject=Hello World") == "mailto"  # espaces permis
    assert launcher.uri_scheme("HTTPS://example.com") == "https"
    assert launcher.uri_scheme(r"C:\Windows\notepad.exe") == ""  # lecteur : un chemin, pas une adresse
    assert launcher.uri_scheme("notepad") == ""
    assert launcher.uri_scheme("cmd /c start x:y") == ""


@pytest.mark.parametrize("uri", ["steam://run/730", "ms-settings:about", "https://example.com", "tel:+33600000000"])
def test_launch_is_free_otherwise(monkeypatch, uri):
    """Tours 3 et 4 : lancer est libre (politique commune du 18/10) ; tel: ouvre un composeur, l'appel reste un clic
    gardé dans act. Chaque liste « à confirmer » se contournait (npx, explorer.exe tel:…)."""
    opened: list[str] = []
    monkeypatch.setattr(launcher, "open_uri", lambda uri, scheme, *a: opened.append(uri) or "ok")
    assert launcher.launch(uri) == "ok" and opened == [uri]


def test_scripts_and_installers_open_freely(monkeypatch, tmp_path):
    doc = tmp_path / "run.bat"
    doc.write_text("echo ok\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "open_path", lambda *a, **k: "opened")
    assert launcher.launch(str(doc)) == "opened"


def test_mask_codes():
    assert notifications.mask_codes("Votre code de vérification : 482 913") == "Votre code de vérification : •••"
    assert notifications.mask_codes("Sign-in code G-551204 for Google") == "Sign-in code ••• for Google"
    assert notifications.mask_codes("Réunion le 20/10/2026 à 10:30, salle 2048") == (
        "Réunion le 20/10/2026 à 10:30, salle 2048"
    )  # aucun mot de code : les nombres restent lisibles
    assert notifications.mask_codes("Your sign-in code is K7F2QX") == "Your sign-in code is •••"
    assert notifications.mask_codes("Verification code: ab12-cd34.") == "Verification code: •••."
    assert notifications.mask_codes("Code de vérification envoyé", "") == "Code de vérification envoyé"


@pytest.mark.parametrize(
    "text",
    [
        "Your verification code: 123456.",  # relecture de Codex : ponctuation finale
        "Votre code : 123456, ne le partagez pas.",
        "Your login code is 482 913.",
        "Votre PIN : 4821",  # relecture d'un agent neuf
        "Use 482913 to confirm your account",
        "Apple ID: 482913 is your number",
        "Code de connexion 12345678901",  # 11 chiffres
    ],
)
def test_codes_masked(text):
    masked = notifications.mask_codes(text)
    assert not any(ch.isdigit() for ch in masked.replace("Apple", "")), masked


def test_numeric_entities_decoded_then_masked(tmp_path):
    path = tmp_path / "wpndatabase.db"
    hidden = "".join(f"&#{ord(c)};" for c in "739201")  # code écrit en entités XML numériques
    xml = f"<toast><visual><binding><text>Code</text><text>Saisir {hidden}.</text></binding></visual></toast>"
    _database(path, [("electron.app.App", "toast", xml, NOON, 0)])
    assert notifications.read(database=path)[0].texts == ["Code", "Saisir •••."]


def test_expired_flood_does_not_hide_active(tmp_path):
    """Tour 2 (Codex) : 1 000 toasts récents expirés cachaient une notification plus ancienne encore active."""
    path = tmp_path / "wpndatabase.db"
    toast = "<toast><visual><binding><text>{}</text></binding></visual></toast>"
    past = notifications._filetime(dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
    rows = [("electron.app.Spam", "toast", toast.format(f"vieux {i}"), NOON + 100 + i, past) for i in range(1000)]
    rows.append(("electron.app.Active", "toast", toast.format("toujours là"), NOON, 0))
    _database(path, rows)
    assert [n.app for n in notifications.read(database=path)] == ["Active"]


def test_bank_balance_not_masked():
    """Tour 2 : « compte » seul ne suffit plus (un solde était masqué)."""
    assert notifications.mask_codes("Solde du compte : 1 234,56 €") == "Solde du compte : 1 234,56 €"
    assert notifications.mask_codes("Tu código es 482913") == "Tu código es •••"


def test_app_name():
    assert notifications.app_name("electron.app.Antigravity") == "Antigravity"
    assert notifications.app_name("AnthropicPBC.Claude_fnn82j28hfe8t!Claude") == "Claude"
    assert notifications.app_name("OpenAI.Codex_2p2nqsd0c76g0!App") == "Codex"
    assert notifications.app_name(r"C:\Program Files\App\tool.exe") == "tool"
    assert notifications.app_name("Microsoft.WindowsStore_8wekyb3d8bbwe!App") == "WindowsStore"


NOON = 134_369_640_000_000_000  # 20/10/2026, en FILETIME


def _database(path, rows):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE NotificationHandler (RecordId INTEGER, PrimaryId TEXT)")
    db.execute(
        "CREATE TABLE Notification (HandlerId INTEGER, Type TEXT, Payload BLOB, ArrivalTime INTEGER, ExpiryTime INTEGER)"
    )
    for record, (app, kind, xml, arrival, expiry) in enumerate(rows, 1):
        db.execute("INSERT INTO NotificationHandler VALUES (?, ?)", (record, app))
        db.execute(
            "INSERT INTO Notification VALUES (?, ?, ?, ?, ?)", (record, kind, xml.encode("utf-8"), arrival, expiry)
        )
    db.commit()
    db.close()


def test_read_notifications(tmp_path):
    path = tmp_path / "wpndatabase.db"
    toast = '<toast><visual><binding><text>{}</text><text id="2">{}</text></binding></visual></toast>'
    future = notifications._filetime(dt.datetime.now(dt.UTC) + dt.timedelta(days=1))
    past = notifications._filetime(dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
    _database(
        path,
        [
            ("electron.app.Antigravity", "toast", toast.format("Antigravity", "Fini &amp; vérifié"), NOON, future),
            (
                "OpenAI.Codex_2p2nqsd0c76g0!App",
                "toast",
                toast.format("Code de connexion", "Saisir 739201"),
                NOON + 10,
                0,
            ),
            ("Microsoft.Windows.Tile", "tile", toast.format("Tuile", "ignorée"), NOON + 20, 0),
            ("electron.app.Old", "toast", toast.format("Vieille", "expirée"), NOON + 30, past),
        ],
    )
    found = notifications.read(database=path)
    assert [n.app for n in found] == ["Codex", "Antigravity"]  # plus récente d'abord ; tuiles et expirées ignorées
    assert found[0].texts == ["Code de connexion", "Saisir •••"]
    assert found[1].texts == ["Antigravity", "Fini & vérifié"]
    assert found[0].arrived.year == 2026
    assert [n.app for n in notifications.read(app="antigravity", database=path)] == ["Antigravity"]
    assert len(notifications.read(limit=1, database=path)) == 1
    with pytest.raises(notifications.NotificationsError):
        notifications.read(database=tmp_path / "absent.db")


def test_surfaces(monkeypatch):
    monkeypatch.setattr(w, "FindWindowW", lambda cls, title: {"Shell_TrayWnd": 11, "Progman": 22}.get(cls, 0))
    assert snapshot.surface_window("taskbar") == 11
    assert snapshot.surface_window("Barre des tâches") == 11
    assert snapshot.surface_window("desktop") == 22
    assert snapshot.surface_window("Calculatrice") == 0


def _fake_windows(monkeypatch, windows):
    """windows : hwnd → (classe, titre, masquée, processus, réduite)."""
    monkeypatch.setattr(w, "class_name", lambda h: windows[h][0])
    monkeypatch.setattr(w, "window_text", lambda h: windows[h][1])
    monkeypatch.setattr(w, "is_cloaked", lambda h: windows[h][2])
    monkeypatch.setattr(w, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(w, "IsIconic", lambda h: windows[h][4])
    monkeypatch.setattr(w, "window_pid", lambda h: h)
    monkeypatch.setattr(w, "process_name", lambda pid: windows[pid][3])
    monkeypatch.setattr(w, "enum_windows", lambda: list(windows))
    monkeypatch.setattr(w, "enum_children", lambda h: [])


def test_hidden_store_window(monkeypatch):
    _fake_windows(
        monkeypatch,
        {
            1: ("Windows.UI.Core.CoreWindow", "Paramètres", True, "SystemSettings.exe", False),  # cachée : listée
            2: ("Windows.UI.Core.CoreWindow", "Rechercher", True, "SearchHost.exe", False),  # panneau : jamais
            3: ("Windows.UI.Core.CoreWindow", "Calculatrice", False, "CalculatorApp.exe", False),
            4: ("Windows.UI.Core.CoreWindow", "DesktopWindowXamlSource", True, "App.exe", False),
            5: ("Chrome_WidgetWin_1", "Chrome", True, "chrome.exe", False),
        },
    )
    assert [h for h in (1, 2, 3, 4, 5) if snapshot.is_hidden_store_window(h)] == [1]


def test_minimized_store_app_not_hidden(monkeypatch):
    """Relecture d'un agent neuf, reproduit le 20/10 sur la Calculatrice : réduite, Windows détache et masque sa
    CoreWindow ; elle reste une appli réduite, jamais « jamais affichée », et son cadre reste listé."""
    _fake_windows(
        monkeypatch,
        {
            10: ("Windows.UI.Core.CoreWindow", "Calculatrice", True, "CalculatorApp.exe", False),
            11: ("ApplicationFrameWindow", "Calculatrice", False, "ApplicationFrameHost.exe", True),
        },
    )
    monkeypatch.setattr(w, "ex_style", lambda h: 0)
    monkeypatch.setattr(w, "owner", lambda h: 0)
    monkeypatch.setattr(w, "frame_rect", lambda h: w.Rect(0, 0, 100, 100))
    monkeypatch.setattr(w, "IsZoomed", lambda h: False)
    monkeypatch.setattr(w, "foreground", lambda: 0)
    listed = snapshot.app_windows()
    assert [(i.hwnd, i.hidden, i.minimized) for i in listed] == [(11, False, True)]


def test_flyouts_known():
    assert snapshot.FLYOUTS["quick settings"][0] == "win+a"
    assert snapshot.FLYOUTS["notifications"][0] == "win+n"
    assert snapshot.FLYOUTS["démarrer"][1] == "startmenuexperiencehost.exe"
    assert "searchhost.exe" in snapshot.PANEL_PROCESSES


def test_shell_shortcut_only_panel_keys(monkeypatch):
    monkeypatch.setattr(realinput, "_send", lambda *inputs: pytest.fail("sent"))
    for combo in ("ctrl+a", "alt+f4", "win+ctrl+d", "enter", "win+l", "win+r"):  # Win+L verrouille, Win+R exécute
        with pytest.raises(realinput.ForegroundError, match="panels"):
            realinput.shell_shortcut(keys.parse(combo))


def test_real_input_stops_when_call_abandoned(monkeypatch):
    """Relecture de Codex : un appel abandonné (délai dépassé) continuait sa saisie réelle. Plus aucun lot envoyé."""
    sent: list[int] = []
    monkeypatch.setattr(realinput, "_send", lambda *inputs: sent.append(len(inputs)))
    monkeypatch.setattr(realinput, "_ALLOWED", {7})
    monkeypatch.setattr(realinput, "_SINCE", None)
    monkeypatch.setattr(realinput, "_TOKEN", None)
    monkeypatch.setattr(w, "foreground", lambda: 7)
    monkeypatch.setattr(w, "root", lambda h: h)
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(worker._LOCAL, "cancel", cancel, raising=False)
    with pytest.raises(worker.Cancelled):
        realinput.type_text("abc")
    assert sent == []


def test_frozen_window_not_printed(monkeypatch):
    """Application figée : PrintWindow bloquerait sans fin (mesuré > 8 s) ; refusé d'emblée, la capture WGC prend le
    relais dans capture_view."""
    monkeypatch.setattr(w, "IsIconic", lambda h: False)
    monkeypatch.setattr(w, "window_rect", lambda h: w.Rect(0, 0, 100, 100))
    monkeypatch.setattr(w, "frame_rect", lambda h: w.Rect(0, 0, 100, 100))
    monkeypatch.setattr(w, "responsive", lambda h, timeout_ms=300: False)
    monkeypatch.setattr(w, "PrintWindow", lambda *a: pytest.fail("PrintWindow on a frozen window"))
    with pytest.raises(capture.CaptureError, match="not responding"):
        capture.capture_printwindow(42)


def test_flyout_needs_foreground():
    from nexus_windows import server

    with pytest.raises(server.WindowError, match="foreground=true"):
        server._open_flyout("quick settings", snapshot.FLYOUTS["quick settings"], False)


def test_close_flyout_respects_user_takeover(monkeypatch):
    """Relecture de Codex : panneau 20 ouvert depuis la fenêtre 10, l'utilisateur est passé à sa fenêtre 30 ; la
    fin du lot ne lui reprend pas la main."""
    from nexus_windows import foreground, server

    monkeypatch.setattr(w, "foreground", lambda: 30)
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(w, "window_pid", lambda h: h)
    monkeypatch.setattr(w, "process_name", lambda pid: "notepad.exe")
    monkeypatch.setattr(foreground, "give_back", lambda *a, **k: pytest.fail("foreground taken back"))
    monkeypatch.setattr(realinput, "escape_to", lambda *a: pytest.fail("escape sent"))
    server._close_flyout(10, {20})
    # Tour 2 : l'utilisateur a ouvert lui-même un AUTRE panneau (réglages rapides) : on n'y touche pas non plus.
    monkeypatch.setattr(w, "process_name", lambda pid: "shellhost.exe")
    server._close_flyout(10, {20})


def test_panel_already_open_is_left_to_the_user(monkeypatch):
    """Tour 2 : un panneau que l'utilisateur avait ouvert n'est jamais refermé par le lot."""
    from nexus_windows import server

    monkeypatch.setattr(snapshot, "flyout_window", lambda process, cls: 20)
    monkeypatch.setattr(server, "_panel_front", lambda processes=None: 20)
    hwnd, panel, user_front, opened = server._open_flyout("start", snapshot.FLYOUTS["start"], True)
    assert (hwnd, opened) == (20, False)


def test_screen_refused_when_protected_window_moves(monkeypatch):
    """Tour 2 : zones relevées avant et après la capture ; si elles changent à chaque essai, refus."""
    from nexus_windows import actions, server

    moves = iter(range(100))
    monkeypatch.setattr(server, "_protected_rects", lambda: [w.Rect(next(moves), 0, 50, 50)])
    white = Image.new("RGB", (200, 100), (255, 255, 255))
    monkeypatch.setattr(capture, "capture_screen", lambda: capture.Shot(white, (0, 0), "screen", 1.0))
    with pytest.raises(actions.ActionError, match="refused"):
        server._screen_overview("normal", None)


def test_screen_overview_blacks_out_protected_windows(monkeypatch):
    """Relectures de Codex et d'un agent neuf : un gestionnaire de mots de passe à l'écran passait dans l'image et la
    liste de look(window="screen"). Sa zone est noircie, son titre jamais rendu."""
    from nexus_windows import server

    white = Image.new("RGB", (200, 100), (255, 255, 255))
    monkeypatch.setattr(capture, "capture_screen", lambda: capture.Shot(white, (0, 0), "screen", 1.0))
    monkeypatch.setattr(w, "all_top_windows", lambda: [5])
    monkeypatch.setattr(w, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(w, "is_cloaked", lambda h: False)
    monkeypatch.setattr(w, "IsIconic", lambda h: False)
    monkeypatch.setattr(w, "class_name", lambda h: "Vault")
    monkeypatch.setattr(w, "frame_rect", lambda h: w.Rect(10, 10, 60, 60))
    monkeypatch.setattr(server.foreground, "app_pid", lambda h: h)
    monkeypatch.setattr(w, "process_name", lambda pid: "Bitwarden.exe")
    monkeypatch.setattr(policy, "hard_block", lambda process, cls: "password manager")
    info = snapshot.WindowInfo(
        5, "Bitwarden — coffre", "Bitwarden.exe", "Vault", w.Rect(10, 10, 60, 60), False, False, False, "Chromium"
    )
    monkeypatch.setattr(snapshot, "app_windows", lambda: [info])
    text, image = server._screen_overview("normal", None)
    assert "Bitwarden — coffre" not in text and "protected window" in text
    shot = Image.open(__import__("io").BytesIO(image.data))
    assert shot.getpixel((30, 30)) == (0, 0, 0) and shot.getpixel((150, 80)) == (255, 255, 255)


def test_dialog_of_user_window_not_taken_back(monkeypatch):
    """Tour 3 (Codex) : boîte de dialogue 20 possédée par la fenêtre 10 de l'utilisateur : elle reste devant."""
    from nexus_windows import foreground

    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    monkeypatch.setattr(w, "foreground", lambda: 20)
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(w, "owner", lambda h: {20: 10}.get(h, 0))
    monkeypatch.setattr(foreground, "user_input_since", lambda *a: False)
    monkeypatch.setattr(foreground, "system_foreground", lambda: "")
    monkeypatch.setattr(foreground, "user_is_switching", lambda: False)
    monkeypatch.setattr(foreground, "system_foreground", lambda: "")
    monkeypatch.setattr(foreground, "give_back", lambda *a, **k: pytest.fail("dialog taken back"))
    import time as clock

    assert launcher._give_back_while(10, clock.monotonic() + 0.2) is False


def test_notification_toast_is_not_the_panel(monkeypatch):
    """Tour 3 (agent neuf) : la notification éphémère (même processus, même classe) n'est pas le centre."""
    monkeypatch.setattr(w, "all_top_windows", lambda: [1, 2])
    monkeypatch.setattr(w, "class_name", lambda h: snapshot.UWP_CORE_CLASS)
    monkeypatch.setattr(w, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(w, "is_cloaked", lambda h: False)
    monkeypatch.setattr(w, "window_pid", lambda h: h)
    monkeypatch.setattr(w, "process_name", lambda pid: "ShellExperienceHost.exe")
    monkeypatch.setattr(w, "frame_rect", lambda h: w.Rect(0, 0, 400, 110 if h == 1 else 1380))
    assert snapshot.flyout_window("shellexperiencehost.exe", snapshot.UWP_CORE_CLASS) == 2


def test_panel_opened_by_user_during_pause_is_not_toggled(monkeypatch):
    """Tour 3 (Codex) : Démarrer ouvert par l'utilisateur pendant l'attente de sa pause : le raccourci ne part pas."""
    from nexus_windows import foreground, server

    seen = iter([0, 20])
    monkeypatch.setattr(snapshot, "flyout_window", lambda process, cls: next(seen))
    monkeypatch.setattr(foreground, "wait_typing_pause", lambda: True)
    monkeypatch.setattr(server, "_panel_front", lambda processes=None: 20)
    monkeypatch.setattr(realinput, "shell_shortcut", lambda combo: pytest.fail("shortcut sent"))
    hwnd, _, _, opened = server._open_flyout("start", snapshot.FLYOUTS["start"], True)
    assert (hwnd, opened) == (20, False)


def test_realize_skips_ordinary_lists():
    """Tour 3 (Codex) : une liste sans ItemContainer ne coupe plus la recherche."""
    from nexus_windows import uia

    auto = uia.Automation.__new__(uia.Automation)
    auto.pattern = lambda element, name: (_ for _ in ()).throw(uia.UIAError("Pattern item_container not supported"))
    assert auto.realize_named(object(), "Activation") is False


def test_access_codes_masked():
    assert "482913" not in notifications.mask_codes(
        "Use 482913 to access your Microsoft account. Do not share this number."
    )


def test_open_uri_reports_kept_foreground(monkeypatch):
    """Tour 4 (Codex) : sans fenêtre identifiée, « rendu » n'est plus supposé : l'état réel est relu."""
    from nexus_windows import foreground

    monkeypatch.setattr(launcher, "shell_open", lambda target: 0)
    monkeypatch.setattr(launcher, "_give_back_while", lambda *a, **k: False)
    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    monkeypatch.setattr(w, "foreground", lambda: 20)
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(w, "owner", lambda h: 0)
    # UserChoice lit maintenant les touches et la souris : ce scénario sans choix de l'utilisateur
    # doit rester indépendant des gestes réels sur le bureau pendant la suite.
    monkeypatch.setattr(foreground, "user_is_switching", lambda: False)
    monkeypatch.setattr(w, "mouse_button_down", lambda: False)
    monkeypatch.setattr(w, "class_name", lambda h: "TestWindow")
    monkeypatch.setattr(foreground, "system_foreground", lambda: "")
    result = launcher.open_uri("https://example.com", "https", 1.0, 10, set())
    assert result.returned is False and any("kept the foreground" in n for n in result.notes)


def test_real_write_stops_when_target_lost_focus(monkeypatch):
    """Tours 3 et 4 : après le clic, le champ visé (ou son champ intérieur) doit avoir le focus ; sinon, ni Ctrl+A ni
    Suppr ni texte (un autre champ aurait été vidé)."""
    from nexus_windows import actions

    sent: list[str] = []
    monkeypatch.setattr(actions.realinput, "click", lambda *a, **k: sent.append("click"))
    monkeypatch.setattr(actions.realinput, "press", lambda combo: sent.append("key"))
    monkeypatch.setattr(actions.realinput, "type_text", lambda text: sent.append("text"))

    class Auto:
        def focus_within(self, element):
            return False

    ex = actions.Executor.__new__(actions.Executor)
    ex.auto = Auto()  # type: ignore[assignment]
    target = snapshot.Element(1, __import__("nexus_windows").uia.Node(object(), 0, {"name": "Nom"}), 0, 1)
    monkeypatch.setattr(ex, "_point_of", lambda e: (5, 5))
    monkeypatch.setattr(actions.methods, "keyboard_target", lambda top, windows=None: 99)  # autre champ
    ex.hwnd, ex._windows = 1, {1}
    with pytest.raises(actions.ActionError, match="did not take the focus"):
        ex._real_write(target, "nouveau", replace=True)
    assert sent == ["click"]


def test_switch_to_gives_up_when_user_picks_another_window(monkeypatch):
    """Tour 5 (Codex) : la restitution vers 10 ne reprend jamais la fenêtre 30 que l'utilisateur vient de choisir."""
    from nexus_windows import foreground

    fronts = iter([20, 20, 30, 30, 30, 30, 30, 30])
    monkeypatch.setattr(w, "window_pid", lambda h: h)  # 30 : un autre programme
    monkeypatch.setattr(w, "foreground", lambda: next(fronts, 30))
    monkeypatch.setattr(foreground, "_attempt", lambda hwnd, expected: None)
    monkeypatch.setattr(foreground, "_SwitchToThisWindow", lambda *a: pytest.fail("forced switch over the user"))
    assert foreground._switch_to(10, 1.0) is False


def test_reused_instance_does_not_claim_foreground_returned(monkeypatch):
    """Tour 5 (Codex) : instance déjà ouverte réutilisée (aucune fenêtre identifiée) qui garde le premier plan : le
    compte rendu le dit, il ne suppose pas « rendu »."""
    from nexus_windows import foreground

    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    monkeypatch.setattr(w, "foreground", lambda: 20)
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(w, "owner", lambda h: 0)
    monkeypatch.setattr(foreground, "system_foreground", lambda: "")
    monkeypatch.setattr(foreground, "user_is_switching", lambda: False)
    monkeypatch.setattr(w, "mouse_button_down", lambda: False)
    monkeypatch.setattr(w, "class_name", lambda h: "TestWindow")
    assert launcher._front_returned(10, 0) is False
    monkeypatch.setattr(w, "foreground", lambda: 10)
    assert launcher._front_returned(10, 0) is True


# --- Tour 6 (Codex et agent neuf) ---------------------------------------------------------------------------------
def _focus_executor(monkeypatch, local_target: int):
    from nexus_windows import actions

    class Auto:
        def focus_within(self, element):
            return False  # le focus réel du premier plan est ailleurs (surgissante d'un autre fil)

    ex = actions.Executor.__new__(actions.Executor)
    ex.auto = Auto()  # type: ignore[assignment]
    ex.hwnd, ex._windows = 1, {1}
    monkeypatch.setattr(actions.methods, "keyboard_target", lambda top, windows=None: local_target)
    node = __import__("nexus_windows").uia.Node(object(), 0, {"name": "Champ", "hwnd": 7})
    return ex, snapshot.Element(1, node, 0, 1)


def test_real_input_trusts_only_the_real_focus(monkeypatch):
    """P1 : le focus local du fil (messages adressés) ne prouve pas où ira le VRAI clavier."""
    ex, field = _focus_executor(monkeypatch, local_target=7)
    assert ex._focus_reaches(field) is True  # messages adressés : le focus local suffit
    assert ex._focus_reaches(field, real=True) is False  # vrai clavier : refus


def test_give_back_never_falls_back_over_the_users_choice(monkeypatch):
    from nexus_windows import foreground

    fronts = iter([20, 20])  # départ (lu une fois), cadre Store ?
    monkeypatch.setattr(w, "foreground", lambda: next(fronts, 30))  # puis l'utilisateur prend 30
    monkeypatch.setattr(w, "window_pid", lambda h: h)
    monkeypatch.setattr(w, "process_name", lambda pid: "cible.exe")
    monkeypatch.setattr(foreground, "_restorable", lambda h: True)
    monkeypatch.setattr(foreground, "_switch_to", lambda hwnd, t, ours=None, expected=None: False)
    monkeypatch.setattr(foreground, "_hand_over", lambda *a: pytest.fail("hand-over over the user's choice"))
    monkeypatch.setattr(foreground, "_ask_holder", lambda *a: pytest.fail("ask-holder over the user's choice"))
    monkeypatch.setattr(foreground, "_checkpoint", lambda: None)
    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    monkeypatch.setattr(w, "IsIconic", lambda h: False)
    assert foreground.give_back(10, exclude={20}) is False
    fronts = iter([20])  # départ : la cible tient le premier plan, puis l'utilisateur prend 30
    assert foreground.activate(21) is False


def test_attempt_rechecks_before_set_foreground(monkeypatch):
    from nexus_windows import foreground

    fronts = iter([20, 20, 30])  # l'utilisateur change de fenêtre pendant BringWindowToTop
    monkeypatch.setattr(w, "foreground", lambda: next(fronts, 30))
    monkeypatch.setattr(w, "window_thread", lambda h: 0)
    monkeypatch.setattr(w, "BringWindowToTop", lambda h: True)
    monkeypatch.setattr(w, "SetForegroundWindow", lambda h: pytest.fail("set over the user's new window"))
    foreground._attempt(10, 20)


@pytest.mark.parametrize("blip", [0, 21])  # premier plan nul un instant ; autre fenêtre de la CIBLE (démarrage)
def test_switch_to_keeps_trying_through_target_windows_or_empty_front(monkeypatch, blip):
    from nexus_windows import foreground

    fronts = iter([20, blip, blip, 10])
    expected: list[int] = []
    monkeypatch.setattr(w, "foreground", lambda: next(fronts, 10))
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(w, "owner", lambda h: 0)
    monkeypatch.setattr(foreground, "_attempt", lambda hwnd, exp: expected.append(exp))
    monkeypatch.setattr(foreground, "_SwitchToThisWindow", lambda *a: None)
    assert foreground._switch_to(10, 1.0, {20, 21}) is True
    assert expected and expected[-1] == (
        blip or 20
    )  # la tentative vise la fenêtre de la cible qui tient le premier plan


def test_switch_to_gives_up_for_another_window_of_the_same_program(monkeypatch):
    """Tour 7 : Explorateur A → Explorateur B (même explorer.exe) choisi par l'utilisateur : jamais repris."""
    from nexus_windows import foreground

    fronts = iter([20, 31])
    monkeypatch.setattr(w, "foreground", lambda: next(fronts, 31))
    monkeypatch.setattr(w, "window_pid", lambda h: 5)  # tout le même processus
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(w, "owner", lambda h: 0)
    monkeypatch.setattr(foreground, "_attempt", lambda *a: pytest.fail("attempt over the user's choice"))
    assert foreground._switch_to(10, 1.0) is False


def _desk(monkeypatch, state):
    """Bureau simulé pour UserChoice : premier plan, bouton tenu, fenêtre sous le pointeur, Alt."""
    from nexus_windows import foreground

    monkeypatch.setattr(w, "foreground", lambda: state["fg"])
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(w, "class_name", lambda h: "Shell_TrayWnd" if h == 99 else "App")
    monkeypatch.setattr(w, "mouse_button_down", lambda: state["button"])
    monkeypatch.setattr(w, "cursor_pos", lambda: (0, 0))
    monkeypatch.setattr(w, "window_at", lambda x, y: state["over"])
    monkeypatch.setattr(foreground, "user_is_switching", lambda: state["button"] or state["alt"])
    return foreground


@pytest.mark.parametrize("over", [30, 99])  # clic sur la fenêtre elle-même, ou sur son bouton de la barre des tâches
def test_user_choice_follows_a_click(monkeypatch, over):
    state = {"fg": 10, "button": False, "alt": False, "over": 10}
    fg = _desk(monkeypatch, state)
    choice = fg.UserChoice(10)
    state.update(button=True, over=over)
    choice.observe()
    state.update(button=False, fg=30)
    assert choice.chose(30) and choice.front == 30


def test_user_choice_follows_alt_tab(monkeypatch):
    state = {"fg": 10, "button": False, "alt": True, "over": 10}
    fg = _desk(monkeypatch, state)
    choice = fg.UserChoice(10)
    choice.observe()
    state.update(alt=False, fg=30)
    assert choice.chose(30) and choice.front == 30


def test_user_choice_sees_a_steal(monkeypatch):
    """Aucun geste, ou un clic dans SA fenêtre pendant que la cible s'active : c'est un vol."""
    state = {"fg": 10, "button": True, "alt": False, "over": 10}
    fg = _desk(monkeypatch, state)
    choice = fg.UserChoice(10)
    choice.observe()
    state.update(button=False, fg=30)
    assert not choice.chose(30) and choice.front == 10


def test_address_watch_never_takes_back_the_window_the_user_clicked(monkeypatch):
    """Tour 7, P1 : launch("https://…") dans un Chrome déjà ouvert ; l'utilisateur clique Chrome : il le garde."""
    import time as _t

    state = {"fg": 10, "button": False, "alt": False, "over": 10}
    fg = _desk(monkeypatch, state)
    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    monkeypatch.setattr(w, "owner", lambda h: 0)
    monkeypatch.setattr(fg, "system_foreground", lambda: "")
    monkeypatch.setattr(fg, "give_back", lambda *a, **k: pytest.fail("took back the user's choice"))
    launcher._CHOICE = None
    launcher._choice(10)
    state.update(button=True, over=30)
    launcher._choice(10).observe()
    state.update(button=False, fg=30)
    assert launcher._give_back_while(10, _t.monotonic() + 0.2) is False
    assert launcher._users(10) == 30 and launcher._front_returned(10, 0)


def test_address_watch_gives_back_a_steal(monkeypatch):
    import time as _t

    state = {"fg": 30, "button": False, "alt": False, "over": 10}
    fg = _desk(monkeypatch, state)
    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    monkeypatch.setattr(w, "owner", lambda h: 0)
    monkeypatch.setattr(fg, "system_foreground", lambda: "")
    calls: list[int] = []
    monkeypatch.setattr(fg, "give_back", lambda prev, **k: calls.append(prev) or state.update(fg=prev) or True)
    launcher._CHOICE = None
    assert launcher._give_back_while(10, _t.monotonic() + 0.2) is True and calls == [10]


def test_password_in_shadow_dom_is_refused(monkeypatch):
    """Tour 7, P1 : le contrôle du mot de passe lit l'élément actif EN PROFONDEUR (shadow DOM), pas son hôte."""
    from nexus_windows import actions
    from nexus_windows.act_chromium import ChromiumActions

    class Page:
        def evaluate(self, script):
            return "shadowRoot.activeElement" in script and "password" in script

    with pytest.raises(actions.ActionError, match="password"):
        ChromiumActions._cdp_refuse_secret(Page())  # type: ignore[arg-type]
    from nexus_windows.act_base import SELECT_ALL_JS

    assert "shadowRoot.activeElement" in SELECT_ALL_JS


@pytest.mark.parametrize(("front", "moved"), [(50, False), (10, True)])
def test_settings_not_pushed_under_when_user_chose_them(monkeypatch, front, moved):
    """P2 : Paramètres au premier plan parce que l'utilisateur y est allé : pas glissés sous son ancienne fenêtre."""
    from nexus_windows import foreground

    moves: list[int] = []
    monkeypatch.setattr(w, "foreground", lambda: front)
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    monkeypatch.setattr(w, "is_cloaked", lambda h: False)
    monkeypatch.setattr(w, "SetWindowPos", lambda h, *a: moves.append(h))
    monkeypatch.setattr(launcher, "_settings_window", lambda: 50)
    monkeypatch.setattr(launcher, "_is_settings", lambda h: False)
    monkeypatch.setattr(launcher, "SETTINGS_SETTLE_S", 0.0)
    monkeypatch.setattr(foreground, "user_is_switching", lambda: False)
    import time as _t

    launcher._settings_behind(10, _t.monotonic() + 1)
    assert moves == ([50] if moved else [])


@pytest.mark.parametrize("state", ["unfocused", "unselected", "none"])
def test_devtools_focus_refused_unless_proven(state):
    """P1 : éditeur riche (enfant [tabindex], sélection ailleurs) ou shadow DOM non atteint → rien n'est envoyé."""
    from nexus_windows import cdp
    from nexus_windows.act_chromium import ChromiumActions

    sent: list[str] = []

    class Page:
        def evaluate(self, script):
            sent.append(script)
            return state

    for editable in (False, True):
        with pytest.raises(cdp.CDPError):
            ChromiumActions._cdp_focus_at(Page(), 5, 5, editable=editable)  # type: ignore[arg-type]
    assert all("shadowRoot" in s and "isContentEditable" in s and "getSelection" in s for s in sent)
