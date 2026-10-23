"""Journal commun des trois Nexus Windows (NexusHarness/shared/journal/FORMAT.md) : une ligne par étape d'un lot, sans
texte d'écran (ni texte tapé, ni libellé, ni titre), en plus du journal détaillé de log.py. Jamais d'exception vers
l'outil : un journal ne bloque pas une action."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__

# Cause d'erreur : seulement le début du message, avant le premier guillemet, deux-points, parenthèse ou crochet ;
# tout ce qui suit peut venir de l'écran (noms, options listées, valeurs). Relecture de Codex du 23/10.
_CUT = re.compile(r"[«\"“'’(\[:{]")
# Techniques qui passent par la vraie souris, le vrai clavier ou une activation RÉELLE de la fenêtre
_FOREGROUND = re.compile(
    r"\breal\b|foreground|physical|SendInput|(?<!without )(?<!simulated )activation", re.IGNORECASE
)
_REFUS = {"ConfirmationRequired", "NeedsForeground"}
_DIFFERE = {"BusyUser", "UserInterrupted"}


def _racine() -> Path:
    base = os.environ.get("NEXUS_JOURNAL_HOME") or os.path.join(
        os.environ.get("LOCALAPPDATA", str(Path.home())), "NexusJournal"
    )
    return Path(base) / "windows" / "claude"


def erreur_propre(texte: Any) -> str | None:
    if not texte:
        return None
    debut = _CUT.split(" ".join(str(texte).split()), maxsplit=1)[0].strip(" .,;-")
    return debut[:80] or "error"


def issue(fields: dict[str, Any]) -> str:
    if str(fields.get("error") or "") == "emergency stop":
        return "arrêt"
    if fields.get("already"):
        return "="
    if not fields.get("ok"):
        kind = str(fields.get("error_type") or "")
        if kind in _REFUS:
            return "refus"
        if kind in _DIFFERE:
            return "différé"
        return "✗"
    return "✓" if fields.get("verified") else "~"


def etape(appel: str, contexte: dict[str, Any], fields: dict[str, Any]) -> None:
    """Une étape (fields : ceux de log.Call.step) dans le journal commun ; contexte : app et cadre de la fenêtre."""
    try:
        method = str(fields.get("method") or "") or None
        ligne = {
            "v": 1,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "agent": "claude",
            "outil": __version__,
            "appel": appel,
            "app": contexte.get("app"),
            "cadre": contexte.get("cadre"),
            "action": fields.get("do"),
            "technique": method,
            "issue": issue(fields),
            "ms": fields.get("ms"),
            "premier_plan": bool(method and _FOREGROUND.search(method)),
            "erreur": erreur_propre(fields.get("error")),
        }
        chemin = _racine() / f"{datetime.now(UTC):%Y-%m}.jsonl"
        chemin.parent.mkdir(parents=True, exist_ok=True)
        with chemin.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ligne) + "\n")  # échappé : jamais d'erreur d'encodage (relecture de Codex)
    except Exception:  # noqa: BLE001 - journal : jamais bloquant
        pass
