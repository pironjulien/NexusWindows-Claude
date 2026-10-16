"""Arrêt d'urgence commun aux agents (~/.nexus/stop, ~/.nexus/stop.owner) : fichiers isolés par conftest."""

from __future__ import annotations

import json
import os
import time

import pytest

from nexus_windows import stop, worker


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(stop.log, "event", lambda *a, **k: None)


def test_press_tells_the_other_agents():
    stop.pressed()
    data = json.loads(stop.SHARED_STOP_FILE.read_text(encoding="utf-8"))
    assert data["by"] == "NexusClaude" and data["hold_s"] == stop.HOLD_S and data["nonce"]
    assert stop.remaining_s() > stop.HOLD_S - 5


def test_stop_written_by_another_agent_freezes_this_server():
    stop.SHARED_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    stop.SHARED_STOP_FILE.write_text(json.dumps({"by": "NexusAGY", "at": time.time()}), encoding="utf-8")
    with pytest.raises(worker.Stopped):
        worker.check_cancelled()
    stop.SHARED_STOP_FILE.unlink()  # supprimé : l'arrêt tient quand même (verrou en mémoire)
    assert stop.remaining_s() > 100


def test_old_shared_stop_is_ignored():
    stop.SHARED_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    stop.SHARED_STOP_FILE.write_text("{}", encoding="utf-8")
    old = time.time() - stop.HOLD_S - 10
    os.utime(stop.SHARED_STOP_FILE, (old, old))
    assert stop.remaining_s() == 0


def test_hotkey_held_by_another_agent_counts_as_available(monkeypatch):
    monkeypatch.setattr(stop, "_owner", type("E", (), {"is_set": lambda self: False})())
    monkeypatch.setattr(stop, "_tried", type("E", (), {"is_set": lambda self: True})())
    assert stop.hotkey_note()  # personne : non garanti
    stop.SHARED_OWNER_FILE.parent.mkdir(parents=True, exist_ok=True)
    stop.SHARED_OWNER_FILE.write_text(json.dumps({"owner": "NexusAGY", "pid": 9, "ts": time.time()}), encoding="utf-8")
    assert stop.hotkey_note() == "" and "NexusAGY" in stop.status()


def test_heartbeat_written_in_both_places():
    stop._heartbeat()
    for path in (stop.OWNER_FILE, stop.SHARED_OWNER_FILE):
        assert json.loads(path.read_text(encoding="utf-8"))["owner"] == "NexusClaude"
