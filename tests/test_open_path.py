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
