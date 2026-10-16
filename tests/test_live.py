"""Tests de bout en bout sur les applications du labo, via le vrai serveur MCP (stdio). Lents : pytest -m live.

Chaque vérification est jugée par l'application de test (événements, journal de page, affichage), pas par le compte
rendu de l'outil. Les seuls échecs admis sont les refus explicites documentés dans bench/RESULTS.md.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "live"))

import drive  # noqa: E402

# Refus assumés (raccourcis Ctrl impossibles en arrière-plan en WPF) : le test exige qu'ils restent des refus
# explicites, jamais de faux succès. Chromium sans DevTools les reçoit désormais par activation simulée (0.7.0).
EXPECTED_REFUSALS = {("wpf", "key ctrl+k")}


@pytest.mark.live
@pytest.mark.parametrize("kind", ["win32", "winforms", "wpf", "electron", "edge", "occult", "tk", "calc"])
def test_scenario(kind):
    rows = asyncio.run(drive.drive(kind))
    assert rows, "aucune vérification exécutée"
    for label, ok, report in rows:
        if (kind, label) in EXPECTED_REFUSALS and not ok:
            assert "✗" in report and "foreground=true" in report, report  # refus honnête, pas un succès menteur
    failures = [(label, report) for label, ok, report in rows if not ok and (kind, label) not in EXPECTED_REFUSALS]
    # État de Windows où AUCUNE activation n'est possible (Recherche masquée au premier plan) : les frappes Chromium
    # doivent alors être refusées avec la cause, jamais réussies en mentant ; le reste du scénario doit passer.
    blocked = [f for f in failures if "premier plan est tenu par" in f[1]]
    others = [f for f in failures if f not in blocked and not (blocked and f[0] == "lot de 6 étapes")]
    assert not others, others
    if blocked:
        pytest.xfail(f"activation refusée par Windows (état du poste) pour {[label for label, _ in blocked]}")


@pytest.mark.live
@pytest.mark.parametrize("kind", ["electron", "edge"])
def test_devtools_remedy(kind):
    """Lancée avec devtools=true, une application Chromium accepte frappes, raccourcis et <select> sans activation."""
    rows = asyncio.run(drive.drive_devtools(kind))
    failures = [(label, report) for label, ok, report in rows if not ok]
    assert not failures, failures
