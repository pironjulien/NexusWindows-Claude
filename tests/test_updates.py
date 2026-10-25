"""Contrat commun des mises à jour (26/10) : le serveur se met à jour lui-même, au plus une fois par jour, et écrit
son état dans ~/.nexus/status/windows-claude.json ; jamais fatal, jamais une requête quand c'est coupé."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from nexus_windows import __version__, updates


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("NEXUS_UPDATE_CHECK", "on")
    return tmp_path


def status(home) -> dict:
    return json.loads((home / ".nexus" / "status" / "windows-claude.json").read_text(encoding="utf-8"))


def test_turned_off_makes_no_request(home, monkeypatch):
    monkeypatch.setenv("NEXUS_UPDATE_CHECK", "off")
    monkeypatch.setattr(updates, "latest_on_pypi", lambda: pytest.fail("request"))
    updates.check()
    assert status(home)["state"] == "idle" and status(home)["version"] == __version__


def test_newer_version_is_prepared_for_the_next_start(home, monkeypatch):
    monkeypatch.setattr(updates, "latest_on_pypi", lambda: "99.0.0")
    monkeypatch.setattr(updates, "via_uvx", lambda: True)
    prepared = []
    monkeypatch.setattr(updates, "prepare", lambda v: prepared.append(v))
    updates.check(now=1000.0)
    assert prepared == ["99.0.0"]
    assert status(home) | {"checked_at": ""} == status(home) | {
        "product": "windows-claude", "latest": "99.0.0", "state": "done", "checked_at": "",
    }  # fmt: skip
    # Même jour : PyPI n'est pas relu.
    monkeypatch.setattr(updates, "latest_on_pypi", lambda: pytest.fail("second request the same day"))
    updates.check(now=1000.0 + 3600)


def test_failure_keeps_the_running_version(home, monkeypatch):
    monkeypatch.setattr(updates, "latest_on_pypi", lambda: (_ for _ in ()).throw(OSError("offline")))
    updates.check()
    assert status(home)["state"] == "failed" and __version__ in status(home)["message"]


def test_not_launched_by_uvx_says_where_to_update(home, monkeypatch):
    monkeypatch.setattr(updates, "latest_on_pypi", lambda: "99.0.0")
    monkeypatch.setattr(updates, "via_uvx", lambda: False)
    monkeypatch.setattr(updates, "prepare", lambda v: pytest.fail("prepared outside uvx"))
    updates.check()
    assert status(home)["state"] == "idle" and "where it was installed from" in status(home)["message"]


def test_version_flag_prints_the_version_without_starting_a_server():
    out = subprocess.run(
        [sys.executable, "-m", "nexus_windows", "--version"], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0 and out.stdout.strip() == __version__
