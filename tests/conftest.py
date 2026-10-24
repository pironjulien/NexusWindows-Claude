"""Isolation commune des tests."""

from __future__ import annotations

import pytest

from nexus_windows import agentlock, foreground, launch, log, routines, stop
from nexus_windows import win32 as w


@pytest.fixture(autouse=True)
def log_isolated(monkeypatch, tmp_path):
    """Journal de chaque test dans un dossier à lui, jamais le vrai ~/.claude/nexus-claude/logs (que l'outil logs et
    Julien relisent), ni le dossier d'état du poste (profils DevTools jetables)."""
    home = tmp_path / "nexus-claude"
    monkeypatch.setattr(log, "ROOT", home)
    monkeypatch.setattr(log, "LOG_DIR", home / "logs")
    monkeypatch.setattr(log, "IMAGE_DIR", home / "logs" / "images")
    monkeypatch.setattr(launch, "PROFILES", home / "devtools-profiles")
    monkeypatch.setenv("NEXUS_JOURNAL_HOME", str(tmp_path / "journal-commun"))  # jamais le vrai journal commun


@pytest.fixture(autouse=True)
def routines_isolated(monkeypatch, tmp_path):
    """Routines de chaque test dans un dossier à lui, jamais celles du poste."""
    monkeypatch.setattr(routines, "ROOT", tmp_path / "routines")


@pytest.fixture(autouse=True)
def routines_mirror_isolated(monkeypatch, tmp_path):
    """Miroir des routines (NexusClaude/routines, synchronisé par OneDrive) jamais écrit par un test : dossier à lui."""
    monkeypatch.setattr(routines, "MIRROR", tmp_path / "routines-miroir")


@pytest.fixture(autouse=True)
def agent_token_isolated(monkeypatch, tmp_path):
    """Jeton commun aux agents : fichier propre au test, jamais celui du poste (un autre agent de Julien peut le
    tenir pendant les tests, et les tests ne doivent jamais le lui prendre)."""
    monkeypatch.setattr(agentlock, "PATH", tmp_path / "foreground.lock")


@pytest.fixture(autouse=True)
def stop_latch_isolated(monkeypatch):
    """Le verrou en mémoire de l'arrêt d'urgence (stop._latched_until) vit autant que le processus : un test qui
    appuie sur l'arrêt ne doit pas figer les suivants. Remis à zéro avant chaque test, restauré après (avec la version
    du fichier d'arrêt déjà lue)."""
    monkeypatch.setattr(stop, "_latched_until", 0.0)
    monkeypatch.setattr(stop, "_seen_mtime", None)
    monkeypatch.setattr(stop, "_seen_shared", None)


@pytest.fixture(autouse=True)
def stop_files_isolated(monkeypatch, tmp_path):
    """Fichiers d'arrêt et de battement propres au test : un test qui « appuie » ne doit JAMAIS écrire le vrai
    ~/.nexus/stop, qui figerait NexusAGY et NexusCodex deux minutes."""
    monkeypatch.setattr(stop, "STOP_FILE", tmp_path / "claude" / "stop")
    monkeypatch.setattr(stop, "OWNER_FILE", tmp_path / "claude" / "stop.owner")
    monkeypatch.setattr(stop, "SHARED_STOP_FILE", tmp_path / "nexus" / "stop")
    monkeypatch.setattr(stop, "SHARED_OWNER_FILE", tmp_path / "nexus" / "stop.owner")


@pytest.fixture(autouse=True)
def foreground_watches_finished(monkeypatch):
    """Une garde du premier plan (foreground.Watch) finit sa traîne dans un fil après le bloc : sans cela, elle
    continuerait sur les simulations du test suivant (même cible, même fenêtre de Julien) et y appellerait give_back.
    Après chaque test, toutes les gardes encore en cours sont arrêtées et attendues, simulations encore en place."""
    yield
    with foreground._LOCK:
        live = list(foreground._LIVE)
    for watch in live:
        watch._stop.set()
    for watch in live:
        if watch._thread is not None:
            watch._thread.join(timeout=5)
            assert not watch._thread.is_alive(), "garde du premier plan toujours active après le test"


@pytest.fixture(autouse=True)
def indicator_off(monkeypatch):
    """Aucun logo « agent au travail » pendant les tests : rien ne s'affiche à l'écran de Julien (test_indicator
    l'éprouve lui-même, sans fenêtre)."""
    monkeypatch.setenv("NEXUS_WINDOWS_INDICATOR", "0")


@pytest.fixture(autouse=True)
def launch_choice_isolated(monkeypatch):
    """Suivi du choix de l'utilisateur pendant un lancement (launch._CHOICE) : jamais hérité d'un autre test."""
    monkeypatch.setattr(launch, "_CHOICE", None)


@pytest.fixture(autouse=True)
def wheel_lines_isolated(monkeypatch):
    """Molette : une ligne par cran, jamais le réglage « Lignes à faire défiler » du poste (les tests comptent les pas
    envoyés) ; un test qui en dépend le fixe lui-même."""
    monkeypatch.setattr(w, "wheel_scroll_lines", lambda: 1)
    monkeypatch.setattr(w, "wheel_scroll_chars", lambda: 1)
