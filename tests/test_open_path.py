"""launch avec un chemin de dossier ou de document : ouverture par association, vérifiée, politique."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from nexus_windows import launch as launcher


def test_openable(tmp_path):
    doc = tmp_path / "notes.txt"
    doc.write_text("x", encoding="utf-8")
    renamed = tmp_path / "programme.bin"
    shutil.copy(sys.executable, renamed)  # un programme renommé reste un programme
    assert launcher.openable(str(tmp_path)) == tmp_path.resolve()
    assert launcher.openable(str(doc)) == doc.resolve()
    assert launcher.openable(str(renamed)) is None
    assert launcher.openable(sys.executable) is None
    assert launcher.openable(str(tmp_path / "absent.txt")) is None
    assert launcher.openable("") is None


def test_opened_window_prefers_new_and_needs_full_name_for_old(monkeypatch, tmp_path):
    doc = tmp_path / "budget.xlsx"
    titles = {1: "budget.xlsx - Excel", 2: "Budget 2027 – notes", 3: "budget - Excel"}
    monkeypatch.setattr(
        launcher, "titled_windows", lambda text: [h for h, t in titles.items() if text.lower() in t.lower()]
    )
    assert launcher._opened_window(doc, False, before={1, 2, 3}, waited_s=0.1) == 0  # rien de neuf encore
    assert launcher._opened_window(doc, False, before={1, 2, 3}, waited_s=2.0) == 1  # nom complet, après délai
    assert launcher._opened_window(doc, False, before={1, 2}, waited_s=0.1) == 3  # nouvelle fenêtre au nom du fichier
    monkeypatch.setattr(launcher, "explorer_windows", lambda path: [10, 11])
    assert launcher._opened_window(tmp_path, True, before={10}, waited_s=0.1) == 11
    assert launcher._opened_window(tmp_path, True, before={10, 11}, waited_s=0.1) == 0
    assert launcher._opened_window(tmp_path, True, before={10, 11}, waited_s=2.0) == 10


def test_open_path_reports_verified(monkeypatch, tmp_path):
    calls = {"n": 0}

    def found(path, folder, before, waited):
        calls["n"] += 1
        return 777 if calls["n"] >= 2 else 0

    monkeypatch.setattr(launcher, "shell_open", lambda path: 1234)
    monkeypatch.setattr(launcher, "_opened_window", found)
    result = launcher.open_path(tmp_path, 5.0, user_front=0, before=set())
    assert result.hwnd == 777 and result.via == "folder"
    assert any("opening verified" in n for n in result.notes)


def test_open_path_never_claims_unproven_success(monkeypatch, tmp_path):
    doc = tmp_path / "lettre.docx"
    doc.write_text("x", encoding="utf-8")
    monkeypatch.setattr(launcher, "packaged_handler", lambda path: "")  # association classique
    monkeypatch.setattr(launcher, "shell_open", lambda path: 0)
    monkeypatch.setattr(launcher, "_opened_window", lambda *a: 0)
    result = launcher.open_path(doc, 1.0, user_front=0, before=set())
    assert result.hwnd == 0 and any("NOT verified" in n for n in result.notes)


def test_close_refuses_an_explorer_window_with_several_tabs(monkeypatch):
    import asyncio

    from nexus_windows import server, snapshot

    closed: list[tuple] = []
    monkeypatch.setattr(server.SESSION, "resolve_window", lambda window: 55)
    monkeypatch.setattr(snapshot, "window_info", lambda hwnd: type("I", (), {"label": lambda self: "Explorateur"})())
    monkeypatch.setattr(server, "_check_actionable_window", lambda hwnd: None)
    monkeypatch.setattr(server.w, "class_name", lambda hwnd: "CabinetWClass")
    monkeypatch.setattr(launcher, "explorer_tabs", lambda hwnd: ["C:\\Julien\\Documents", "C:\\Temp\\essai"])
    monkeypatch.setattr(server.methods, "post", lambda *a: closed.append(a))
    monkeypatch.setattr(server.freshness, "stale_reason", lambda: "")
    text = asyncio.run(server.window(action="close", window=55))
    assert "NOT closed" in text and "2 tabs" in text and closed == []


def test_launch_routes_paths_to_open(monkeypatch, tmp_path):
    seen: list[Path] = []
    monkeypatch.setattr(launcher, "open_path", lambda path, *a: seen.append(path) or "ouvert")
    assert launcher.launch(str(tmp_path)) == "ouvert" and seen == [tmp_path.resolve()]
    with pytest.raises(launcher.LaunchError, match="no args"):
        launcher.launch(str(tmp_path), args="/x")


# --- Document ouvert par une application Store (26/10 : MP4 → Lecteur multimédia, « pid 0, aucune fenêtre ») ---------
def _store(monkeypatch, activate, shell_pid=0):
    calls: list[str] = []
    monkeypatch.setattr(
        launcher, "packaged_handler", lambda path: "Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic"
    )
    monkeypatch.setattr(launcher, "_activate_for_file", activate)
    monkeypatch.setattr(launcher, "shell_open", lambda path: calls.append("shell") or shell_pid)
    monkeypatch.setattr(launcher, "_opened_window", lambda *a: 0)  # titre du cadre : « Lecteur multimédia »
    monkeypatch.setattr(launcher, "uwp_unshown", lambda hwnd: False)
    return calls


def test_open_path_store_document_found_by_its_app_window(monkeypatch, tmp_path):
    video = tmp_path / "globale.mp4"
    video.write_bytes(b"x")
    calls = _store(monkeypatch, lambda app, path: 4321)
    seen = {"n": 0}

    def app_window(path, pid, before, waited, auto):
        seen["n"] += 1
        assert pid == 4321
        return 900 if seen["n"] >= 2 else 0

    monkeypatch.setattr(launcher, "_app_document_window", app_window)
    monkeypatch.setattr(launcher, "shows_document", lambda auto, hwnd, path: True)
    result = launcher.open_path(video, 5.0, user_front=0, before=set())
    assert result.hwnd == 900 and result.via == "document (Store app)" and calls == []
    assert any("opening verified" in n and "Store app activated" in n for n in result.notes)
    assert "document name read in the window" in result.notes


def test_open_path_store_without_file_contract_falls_back_to_shell(monkeypatch, tmp_path):
    import comtypes

    photo = tmp_path / "photo.png"
    photo.write_bytes(b"x")

    def refused(app, path):
        raise comtypes.COMError(-2144927148, "contrat non pris en charge", (None, None, None, 0, None))

    calls = _store(monkeypatch, refused, shell_pid=55)
    monkeypatch.setattr(launcher, "_opened_window", lambda *a: 66)
    result = launcher.open_path(photo, 5.0, user_front=0, before=set())
    assert calls == ["shell"] and result.via == "document" and result.hwnd == 66


def test_open_path_store_nothing_opened_says_why(monkeypatch, tmp_path):
    video = tmp_path / "globale.mp4"
    video.write_bytes(b"x")
    _store(monkeypatch, lambda app, path: 4321)
    monkeypatch.setattr(launcher, "_app_document_window", lambda *a: 0)
    result = launcher.open_path(video, 1.0, user_front=0, before=set())
    assert result.hwnd == 0
    assert any("NOT verified" in n and "Store app opened for it" in n for n in result.notes)
    # Activation impossible, ShellExecute sans fenêtre : la note dit pourquoi et quoi faire.
    import comtypes

    def refused(app, path):
        raise comtypes.COMError(-1, "refus", (None, None, None, 0, None))

    _store(monkeypatch, refused)
    result = launcher.open_path(video, 1.0, user_front=0, before=set())
    assert any("did not activate in the background" in n and "foreground=true" in n for n in result.notes)


def test_app_document_window_new_or_reused_showing_the_document(monkeypatch, tmp_path):
    video = tmp_path / "globale.mp4"
    monkeypatch.setattr(launcher, "descendants", lambda pid: {pid})
    monkeypatch.setattr(launcher.w, "enum_windows", lambda: [1, 2, 3])
    monkeypatch.setattr(launcher.snapshot, "is_app_window", lambda h: h != 3)
    monkeypatch.setattr(launcher.snapshot, "is_hidden_store_window", lambda h: h == 3)
    mine = {2, 3}  # 1 : fenêtre sans lien (jamais retenue)
    monkeypatch.setattr(launcher, "_related", lambda h, pids, hint, stem, strict: h in mine and strict)
    shown = {"v": False}
    monkeypatch.setattr(launcher, "shows_document", lambda auto, h, path: shown["v"])
    assert launcher._app_document_window(video, 9, before={1, 2}, waited_s=0.1, auto=None) == 3  # nouvelle (cachée)
    assert launcher._app_document_window(video, 9, before={1, 2, 3}, waited_s=2.0, auto=None) == 0  # rien de prouvé
    shown["v"] = True
    assert launcher._app_document_window(video, 9, before={1, 2, 3}, waited_s=0.1, auto=None) == 0  # trop tôt
    assert launcher._app_document_window(video, 9, before={1, 2, 3}, waited_s=2.0, auto=None) == 2  # réutilisée


def test_shows_document_reads_the_title_or_the_content(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_contract import node
    from test_fix_A import link

    video = tmp_path / "globale.mp4"
    titles = {5: "Lecteur multimédia", 6: "globale.mp4 - VLC"}
    monkeypatch.setattr(launcher.w, "window_text", lambda h: titles.get(h, ""))
    tree = link(node("Window", "Lecteur multimédia"), node("Button", "globale, . Lecture en cours."))
    auto = type("A", (), {"tree": lambda self, h: tree})()
    assert launcher.shows_document(None, 6, video)
    assert not launcher.shows_document(None, 5, video)
    assert launcher.shows_document(auto, 5, video)  # type: ignore[arg-type]


def test_reused_window_already_in_front_is_not_reported_as_taken(monkeypatch):
    monkeypatch.setattr(launcher.w, "IsWindow", lambda h: True)
    monkeypatch.setattr(launcher.w, "root", lambda h: h)
    monkeypatch.setattr(launcher, "_users", lambda user_front: user_front)
    monkeypatch.setattr(launcher, "_holds_front", lambda h: True)
    assert launcher._front_returned(70, 70)  # le lecteur était déjà la fenêtre de l'utilisateur
    assert not launcher._front_returned(70, 71)
