"""Windows lui-même : adresses (ms-settings:…), applications Store cachées, surfaces et panneaux du shell,
notifications lues sans rien ouvrir, application figée sans blocage de la capture."""

from __future__ import annotations

import sqlite3

import pytest

from nexus_windows import capture, keys, notifications, realinput, snapshot
from nexus_windows import launch as launcher
from nexus_windows import win32 as w


def test_uri_scheme():
    assert launcher.uri_scheme("ms-settings:bluetooth") == "ms-settings"
    assert launcher.uri_scheme("mailto:alice@example.com") == "mailto"
    assert launcher.uri_scheme("HTTPS://example.com") == "https"
    assert launcher.uri_scheme(r"C:\Windows\notepad.exe") == ""  # lecteur : un chemin, pas une adresse
    assert launcher.uri_scheme("notepad") == ""
    assert launcher.uri_scheme("cmd /c start x:y") == ""


@pytest.mark.parametrize("uri", ["file:///C:/x.exe", "ms-msdt:/id", "search-ms:query=x", "ms-appinstaller:?source=x"])
def test_dangerous_addresses_refused_before_opening(monkeypatch, uri):
    monkeypatch.setattr(launcher, "shell_open", lambda target: pytest.fail("opened"))
    with pytest.raises(launcher.LaunchError, match="refused"):
        launcher.open_uri(uri, launcher.uri_scheme(uri), 1.0, 0, set())


def test_mask_codes():
    assert notifications.mask_codes("Votre code de vérification : 482 913") == "Votre code de vérification : •••"
    assert notifications.mask_codes("Sign-in code G-551204 for Google") == "Sign-in code ••• for Google"
    assert notifications.mask_codes("Réunion le 20/10/2026 à 10:30, salle 2048") == (
        "Réunion le 20/10/2026 à 10:30, salle 2048"
    )  # aucun mot de code : les nombres restent lisibles
    # Relecture de Codex : codes alphanumériques
    assert notifications.mask_codes("Your sign-in code is K7F2QX") == "Your sign-in code is •••"
    assert notifications.mask_codes("Verification code: ab12-cd34.") == "Verification code: •••."
    assert notifications.mask_codes("Code de vérification envoyé", "") == "Code de vérification envoyé"


def test_numeric_entities_decoded_then_masked(tmp_path):
    path = tmp_path / "wpndatabase.db"
    hidden = "".join(f"&#{ord(c)};" for c in "739201")  # code écrit en entités XML numériques
    xml = f"<toast><visual><binding><text>Code</text><text>Saisir {hidden}</text></binding></visual></toast>"
    _database(path, [("electron.app.App", "toast", xml, 134_369_640_000_000_000)])
    assert notifications.read(database=path)[0].texts == ["Code", "Saisir •••"]


def test_app_name():
    assert notifications.app_name("electron.app.Antigravity") == "Antigravity"
    assert notifications.app_name("AnthropicPBC.Claude_fnn82j28hfe8t!Claude") == "Claude"
    assert notifications.app_name("OpenAI.Codex_2p2nqsd0c76g0!App") == "Codex"
    assert notifications.app_name(r"C:\Program Files\App\tool.exe") == "tool"
    assert notifications.app_name("Microsoft.WindowsStore_8wekyb3d8bbwe!App") == "WindowsStore"


def _database(path, rows):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE NotificationHandler (RecordId INTEGER, PrimaryId TEXT)")
    db.execute("CREATE TABLE Notification (HandlerId INTEGER, Type TEXT, Payload BLOB, ArrivalTime INTEGER)")
    for record, (app, kind, xml, arrival) in enumerate(rows, 1):
        db.execute("INSERT INTO NotificationHandler VALUES (?, ?)", (record, app))
        db.execute("INSERT INTO Notification VALUES (?, ?, ?, ?)", (record, kind, xml.encode("utf-8"), arrival))
    db.commit()
    db.close()


def test_read_notifications(tmp_path):
    path = tmp_path / "wpndatabase.db"
    toast = '<toast><visual><binding><text>{}</text><text id="2">{}</text></binding></visual></toast>'
    noon = 134_369_640_000_000_000  # 20/10/2026, en FILETIME
    _database(
        path,
        [
            ("electron.app.Antigravity", "toast", toast.format("Antigravity", "Fini &amp; vérifié"), noon),
            ("OpenAI.Codex_2p2nqsd0c76g0!App", "toast", toast.format("Code de connexion", "Saisir 739201"), noon + 10),
            ("Microsoft.Windows.Tile", "tile", toast.format("Tuile", "ignorée"), noon + 20),
        ],
    )
    found = notifications.read(database=path)
    assert [n.app for n in found] == ["Codex", "Antigravity"]  # plus récente d'abord ; tuiles ignorées
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


def test_hidden_store_window(monkeypatch):
    windows = {
        1: ("Windows.UI.Core.CoreWindow", "Paramètres", True, "SystemSettings.exe"),  # lancée cachée : listée
        2: ("Windows.UI.Core.CoreWindow", "Rechercher", True, "SearchHost.exe"),  # panneau du shell : jamais
        3: ("Windows.UI.Core.CoreWindow", "Calculatrice", False, "CalculatorApp.exe"),  # affichée : dans son cadre
        4: ("Windows.UI.Core.CoreWindow", "DesktopWindowXamlSource", True, "App.exe"),
        5: ("Chrome_WidgetWin_1", "Chrome", True, "chrome.exe"),
    }
    monkeypatch.setattr(w, "class_name", lambda h: windows[h][0])
    monkeypatch.setattr(w, "window_text", lambda h: windows[h][1])
    monkeypatch.setattr(w, "is_cloaked", lambda h: windows[h][2])
    monkeypatch.setattr(w, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(w, "window_pid", lambda h: h)
    monkeypatch.setattr(w, "process_name", lambda pid: windows[pid][3])
    assert [h for h in windows if snapshot.is_hidden_store_window(h)] == [1]


def test_flyouts_known():
    assert snapshot.FLYOUTS["quick settings"][0] == "win+a"
    assert snapshot.FLYOUTS["notifications"][0] == "win+n"
    assert snapshot.FLYOUTS["démarrer"][1] == "startmenuexperiencehost.exe"


def test_shell_shortcut_only_windows_key(monkeypatch):
    monkeypatch.setattr(realinput, "_send", lambda *inputs: pytest.fail("sent"))
    for combo in ("ctrl+a", "alt+f4", "win+ctrl+d", "enter"):
        with pytest.raises(realinput.ForegroundError, match="Windows-key"):
            realinput.shell_shortcut(keys.parse(combo))


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
