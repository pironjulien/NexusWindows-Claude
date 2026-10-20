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


@pytest.mark.parametrize(
    "uri",
    [
        "file:///C:/x.exe", "ms-msdt:/id", "search-ms:query=x", "ms-appinstaller:?source=x",
        # Relecture d'un agent neuf : passaient la liste noire
        "steam://run/730", "ms-word:ofe|u|https://example.com/x.docx", "shell:startup", "tel:+33600000000",
        "callto:alice",
    ],
)  # fmt: skip
def test_other_addresses_need_confirmation(monkeypatch, uri):
    """Garde-fou par confirmation, pas par blocage : sans confirm=true, rien n'est ouvert."""
    monkeypatch.setattr(launcher, "shell_open", lambda target: pytest.fail("opened"))
    with pytest.raises(launcher.LaunchError, match="Needs confirm=true"):
        launcher.launch(uri)


def test_confirmed_address_is_opened(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(launcher, "open_uri", lambda uri, scheme, *a: opened.append(uri) or "ok")
    assert launcher.launch("tel:+33600000000", confirm=True) == "ok" and opened == ["tel:+33600000000"]


@pytest.mark.parametrize(
    "name", ["run.bat", "page.hta", "setup.msi", "go.url", "app.lnk", "x.ps1", "t.reg", "s.py", "s.pyw", "u.msu"]
)
def test_runnable_documents_need_confirmation(monkeypatch, tmp_path, name):
    doc = tmp_path / name
    doc.write_text("[InternetShortcut]\nURL=ms-msdt:/id\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "open_path", lambda *a, **k: pytest.fail("opened"))
    with pytest.raises(launcher.LaunchError, match="Needs confirm=true"):
        launcher.launch(str(doc))
    monkeypatch.setattr(launcher, "open_path", lambda *a, **k: "opened")
    assert launcher.launch(str(doc), confirm=True) == "opened"  # sur l'ordre explicite de l'utilisateur


def test_runner_by_association(monkeypatch):
    """Tour 2 : le programme associé décide aussi (.xyz ouvert par pyw.exe s'exécute)."""
    monkeypatch.setattr(launcher, "associated_program", lambda suffix: r"C:\WINDOWS\pyw.exe")
    assert "runs them" in launcher.document_reason(__import__("pathlib").Path("x.xyz"))
    monkeypatch.setattr(launcher, "associated_program", lambda suffix: r"C:\Program Files\App\viewer.exe")
    assert launcher.document_reason(__import__("pathlib").Path("x.pdf")) == ""


def test_runners_need_confirmation():
    assert launcher.runner_reason(r"C:\Windows\System32\cmd.exe", "/c x.bat")
    assert launcher.runner_reason("powershell.exe")
    assert launcher.runner_reason(
        "explorer.exe", "ms-msdt:/id"
    )  # explorer avec argument : il exécute ce qu'on lui passe
    assert not launcher.runner_reason("explorer.exe")
    assert not launcher.runner_reason("notepad.exe", "notes.txt")


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
    monkeypatch.setattr(server, "_panel_front", lambda: 20)
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
