"""Attente avant le rendu final d'un act : jusqu'à une interface posée, jamais plus que settle_ms (bench/speed.py)."""

from __future__ import annotations

import time

from nexus_windows import server


def test_settle_stops_as_soon_as_the_window_is_still(monkeypatch):
    prints = iter([("a",), ("b",), ("b",), ("c",)])
    monkeypatch.setattr(server, "_settle_print", lambda hwnd: next(prints))
    started = time.monotonic()
    waited = server.settle(1, 0.3)
    assert waited < 0.25 and time.monotonic() - started < 0.25  # « b » deux fois : posée, sans attendre 300 ms


def test_settle_never_exceeds_settle_ms(monkeypatch):
    counter = iter(range(10_000))
    monkeypatch.setattr(server, "_settle_print", lambda hwnd: (next(counter),))  # animation sans fin
    waited = server.settle(1, 0.2)
    assert 0.19 <= waited < 0.3


def test_settle_zero_means_no_wait(monkeypatch):
    monkeypatch.setattr(server, "_settle_print", lambda hwnd: (_ for _ in ()).throw(AssertionError("capture")))
    assert server.settle(1, 0.0) < 0.05
