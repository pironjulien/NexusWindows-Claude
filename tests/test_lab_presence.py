"""Incident du 25/10 : une fenêtre du labo du banc est apparue au premier plan pendant que Julien travaillait (absence
vérifiée seulement au lancement de chaque famille). Une entrée physique pendant le banc ferme aussitôt labo et
couverture, et le banc s'arrête (JulienActive) ; tant qu'il est absent, rien n'est fermé."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lab"))
import apps  # noqa: E402

from nexus_windows import physical  # noqa: E402


def test_return_of_the_user_closes_the_lab_and_stops_the_bench(monkeypatch):
    idle = {"s": 1000.0}
    monkeypatch.setattr(physical, "start", lambda: None)
    monkeypatch.setattr(physical, "available", lambda: True)
    monkeypatch.setattr(physical, "idle_s", lambda: idle["s"])
    closed: list[str] = []

    class Lab:
        def close(self) -> None:
            closed.append("labo")
            apps._LIVE.discard(self)

    forget = apps.on_return(lambda: closed.append("couverture"))
    apps._LIVE.add(Lab())  # type: ignore[arg-type]
    try:
        apps.arm_presence()
        time.sleep(0.3)
        apps.check_presence()  # absent : le banc continue
        assert closed == []
        idle["s"] = 0.0  # entrée physique
        deadline = time.monotonic() + 2.0
        while len(closed) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sorted(closed) == ["couverture", "labo"]
        with pytest.raises(apps.JulienActive):
            apps.check_presence()
    finally:
        forget()
        apps._LIVE.clear()
        apps._ARMED_AT[0] = 0.0
        apps._RETURNED.clear()
