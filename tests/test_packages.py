"""Applications Store suspendues par Windows (relevé du 18/10 sur la Calculatrice lancée sans activation : contenu
publié puis tombé à 0) : reprise par IPackageDebugSettings.Resume, sans premier plan, seulement si suspendue."""

from __future__ import annotations

import pytest

from nexus_windows import packages, server

CALC = "Microsoft.WindowsCalculator_11.2607.0.0_x64__8wekyb3d8bbwe"


class FakeSettings:
    def __init__(self, states: dict[str, int]) -> None:
        self.states = states
        self.resumed: list[str] = []

    def GetPackageExecutionState(self, name: str) -> int:
        return self.states[name]

    def Resume(self, name: str) -> None:
        self.resumed.append(name)
        self.states[name] = 1


@pytest.fixture
def calc(monkeypatch):
    settings = FakeSettings({CALC: packages.SUSPENDED})
    monkeypatch.setattr(packages, "app_pids", lambda hwnd: {77})
    monkeypatch.setattr(packages, "package_full_name", lambda pid: CALC if pid == 77 else "")
    monkeypatch.setattr(packages, "_settings", lambda: settings)
    return settings


def test_suspended_package_is_resumed(calc):
    assert packages.resume_suspended(1) == [CALC]
    assert calc.resumed == [CALC]


@pytest.mark.parametrize("state", [0, 1, 4])  # inconnu, en cours, terminé : rien à reprendre
def test_running_package_is_left_alone(calc, state):
    calc.states[CALC] = state
    assert packages.resume_suspended(1) == []
    assert calc.resumed == []


def test_suspending_package_is_resumed(calc):
    calc.states[CALC] = packages.SUSPENDING
    assert packages.resume_suspended(1) == [CALC]


def test_desktop_program_has_no_package(monkeypatch):
    monkeypatch.setattr(packages, "app_pids", lambda hwnd: {5})
    monkeypatch.setattr(packages, "package_full_name", lambda pid: "")
    monkeypatch.setattr(packages, "_settings", lambda: pytest.fail("aucun paquet : jamais d'appel COM"))
    assert packages.resume_suspended(1) == []


def test_launched_pid_is_also_checked(calc, monkeypatch):
    monkeypatch.setattr(packages, "app_pids", lambda hwnd: set())  # cadre encore vide
    assert packages.resume_suspended(1, {77}) == [CALC]


def test_real_process_without_package():
    import os

    assert packages.package_full_name(os.getpid()) == ""  # Python : programme de bureau ordinaire
    assert packages.package_full_name(0) == ""


def test_server_resumes_store_app_before_look(calc, monkeypatch):
    monkeypatch.setattr(server.snapshot, "framework_of", lambda hwnd: "UWP")
    waited: list[float] = []
    monkeypatch.setattr(server.launcher, "wait_content", lambda auto, hwnd, limit_s=3.0: waited.append(limit_s))
    notes: list[str] = []
    server._resume_store_app(None, 1, notes)  # type: ignore[arg-type]
    assert calc.resumed == [CALC] and waited and "reprise sans premier plan" in notes[0]


def test_server_ignores_desktop_frameworks(monkeypatch):
    monkeypatch.setattr(server.snapshot, "framework_of", lambda hwnd: "Win32")
    monkeypatch.setattr(packages, "resume_suspended", lambda *a: pytest.fail("Win32 : jamais de reprise"))
    notes: list[str] = []
    server._resume_store_app(None, 1, notes)  # type: ignore[arg-type]
    assert notes == []
