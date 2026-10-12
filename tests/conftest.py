"""Isolation commune des tests."""

from __future__ import annotations

import pytest

from nexus_windows import stop


@pytest.fixture(autouse=True)
def stop_latch_isolated(monkeypatch):
    """Le verrou en mémoire de l'arrêt d'urgence (stop._latched_until) vit autant que le processus : un test qui
    appuie sur l'arrêt ne doit pas figer les suivants. Remis à zéro avant chaque test, restauré après (avec la version
    du fichier d'arrêt déjà lue)."""
    monkeypatch.setattr(stop, "_latched_until", 0.0)
    monkeypatch.setattr(stop, "_seen_mtime", None)
