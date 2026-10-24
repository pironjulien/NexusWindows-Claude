"""Application Chromium/Electron recouverte jamais dessinée (Logi Options+, 2026-10-24) : launch par le nom du menu
Démarrer passe par l'exécutable (commutateurs anti-occlusion) ; look dit l'image unie et le remède selon la ligne de
commande réelle du processus, jamais « une application lancée par launch n'a pas ce problème » sans le vérifier."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest
from PIL import Image

from nexus_windows import capture, launch, snapshot

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contract import as_auto, make_view, node  # noqa: E402

LOGI = r"C:\Program Files\LogiOptionsPlus\logioptionsplus.exe"


def _chromium_view(web_missing: bool = False) -> snapshot.View:
    view = make_view(node("Button", "OK"))
    view.info = dataclasses.replace(view.info, framework="Chromium")
    view.web_missing = web_missing
    return view


def _blank_captures(monkeypatch, size) -> None:
    violet = Image.new("RGB", size, (88, 40, 160))  # écran de démarrage uni
    monkeypatch.setattr(capture, "capture_printwindow", lambda h: capture.Shot(violet, (0, 0), "printwindow", 1.0))
    monkeypatch.setattr(capture, "capture_wgc", lambda h: capture.Shot(violet, (0, 0), "wgc", 1.0))
    monkeypatch.setattr(snapshot.w, "foreground", lambda: 999)  # la fenêtre de Julien devant
    monkeypatch.setattr(snapshot.w, "root", lambda h: h)
    monkeypatch.setattr(snapshot.steam, "is_steam_window", lambda h: False)
    monkeypatch.setattr(snapshot.w, "window_pid", lambda h: 4242)
    monkeypatch.setattr(snapshot, "covered_by_window", lambda h: True)


def test_blank_covered_chromium_says_not_drawn_and_how_to_fix(monkeypatch):
    view = _chromium_view()
    _blank_captures(monkeypatch, view.frame.size)
    monkeypatch.setattr(snapshot.cdp, "process_command_line", lambda pid: f'"{LOGI}"')  # sans commutateur
    _image, notes = snapshot.capture_view(view)
    last = notes[-1]
    assert notes[:2] == ["capture printwindow blank", "capture wgc blank"]
    assert "probably not drawn" in last and "not started by launch" in last and "launch it again" in last


def test_blank_covered_chromium_started_with_switches_says_loading(monkeypatch):
    view = _chromium_view()
    _blank_captures(monkeypatch, view.frame.size)
    line = f'"{LOGI}" --disable-features=Foo,CalculateNativeWinOcclusion --force-renderer-accessibility'
    monkeypatch.setattr(snapshot.cdp, "process_command_line", lambda pid: line)
    _image, notes = snapshot.capture_view(view)
    assert "real content or it is still loading" in notes[-1] and "probably" in notes[-1]


def test_blank_note_not_repeated_when_web_note_given(monkeypatch):
    view = _chromium_view(web_missing=True)
    _blank_captures(monkeypatch, view.frame.size)
    monkeypatch.setattr(snapshot.cdp, "process_command_line", lambda pid: pytest.fail("remède déjà donné plus haut"))
    _image, notes = snapshot.capture_view(view)
    assert notes[-1].endswith("see the note above.")


def test_unreadable_command_line_never_said_without_switches(monkeypatch):
    view = _chromium_view()
    _blank_captures(monkeypatch, view.frame.size)
    monkeypatch.setattr(snapshot.cdp, "process_command_line", lambda pid: None)
    _image, notes = snapshot.capture_view(view)
    assert "unreadable" in notes[-1] and "not started by launch" not in notes[-1]


def test_plain_page_not_covered_never_said_not_drawn(monkeypatch):
    """Page blanche d'une Electron côte à côte, non recouverte : image unie, mais rien d'affirmé sur le rendu."""
    view = _chromium_view()
    _blank_captures(monkeypatch, view.frame.size)
    monkeypatch.setattr(snapshot, "covered_by_window", lambda h: False)
    _image, notes = snapshot.capture_view(view)
    assert not any("not drawn" in n for n in notes)


def test_covered_by_window_needs_one_opaque_window_above(monkeypatch):
    rects = {
        1: snapshot.w.Rect(0, 0, 2560, 1380),  # calque transparent plein écran au-dessus
        2: snapshot.w.Rect(0, 0, 1200, 1380),  # moitié gauche seulement
        3: snapshot.w.Rect(0, 0, 2560, 1380),  # Claude agrandi
        9: snapshot.w.Rect(330, 180, 2230, 1260),  # la cible
    }
    order = [1, 2, 3, 9]
    monkeypatch.setattr(snapshot.w, "frame_rect", lambda h: rects[h])
    monkeypatch.setattr(snapshot.w, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(snapshot.w, "is_cloaked", lambda h: False)
    monkeypatch.setattr(snapshot.w, "IsIconic", lambda h: False)
    monkeypatch.setattr(snapshot.w, "ex_style", lambda h: 0x80020 if h == 1 else 0)
    monkeypatch.setattr(snapshot.w, "enum_windows", lambda: order)
    assert snapshot.covered_by_window(9)
    order[:] = [1, 2, 9, 3]  # Claude derrière la cible
    assert not snapshot.covered_by_window(9)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (f'"{LOGI}"', False),
        (f'"{LOGI}" --disable-features=CalculateNativeWinOcclusion', True),
        (f'"{LOGI}" --disable-features=CalculateNativeWinOcclusion --disable-features=Other', False),  # dernier retenu
        (f'"{LOGI}" --disable-features=A, CalculateNativeWinOcclusion', False),  # espace : jeton suivant
        (f'"{LOGI}" --disable-features=calculatenativewinocclusion', False),  # nom de fonctionnalité exact
        (f'"{LOGI}" -- --disable-features=CalculateNativeWinOcclusion', False),  # après le terminateur : argument
        (f'"{LOGI}" --single-argument --disable-features=CalculateNativeWinOcclusion', False),
        (f'"{LOGI}" --disable-features=CalculateNativeWinOcclusion -- --disable-features=Other', True),
    ],
)
def test_occlusion_switch_read_as_chromium_reads_it(monkeypatch, line, expected):
    monkeypatch.setattr(snapshot.cdp, "process_command_line", lambda pid: line)
    assert snapshot.occlusion_disabled(1) is expected


def _start_menu(monkeypatch, shortcut: tuple[str, str] | None) -> list[str]:
    launched: list[str] = []
    monkeypatch.setattr(
        launch, "find_start_app", lambda name: {"Name": "Logi Options+", "AppID": "Logi.OptionsPlus.App"}
    )
    monkeypatch.setattr(launch, "_app_id_path", lambda app_id: None)
    monkeypatch.setattr(launch, "_shortcut_target", lambda app_id: shortcut)
    monkeypatch.setattr(launch.policy, "check_launch_app_id", lambda *a, **k: None)
    monkeypatch.setattr(launch.policy, "is_chromium_exe", lambda exe: exe == LOGI)
    real = launch.launch

    def fake(app, args="", devtools=False, wait_s=15.0, auto=None, on_account_kill=None, restart=False):
        if app == LOGI:
            launched.append(app)
            return launch.Launched(1, 2, 0, "executable", ["Chromium/Electron: rendering and accessibility kept"])
        return real(app, args, devtools, wait_s, auto, on_account_kill, restart)

    monkeypatch.setattr(launch, "launch", fake)
    return launched


def test_start_menu_chromium_without_arguments_launched_by_its_executable(monkeypatch):
    launched = _start_menu(monkeypatch, (LOGI, ""))
    result = launch.launch("Logi Options+", auto=as_auto(object()))
    assert launched == [LOGI] and result.via == "executable"
    assert result.notes[0] == 'Start menu: "Logi Options+" (logioptionsplus.exe)'


def test_start_menu_chromium_with_own_arguments_keeps_shortcut_and_says_so(monkeypatch):
    launched = _start_menu(monkeypatch, (LOGI, "--remote-debugging-port=9227"))
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(launch, "_new_window", lambda *a, **k: (0, False))
    monkeypatch.setattr(launch, "_front_returned", lambda user_front, hwnd: True)
    result = launch.launch("Logi Options+")
    assert launched == [] and result.via == "Start menu shortcut"
    assert any("switches that keep it drawn when covered are NOT added" in n for n in result.notes)
