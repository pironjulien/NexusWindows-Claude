"""Compte « arrière-plan strict » d'un relevé du banc à trois outils (relecture de Codex du 24/10) : effet prouvé par
l'application, premier plan intact, curseur jamais déplacé, et aucune entrée physique annoncée par l'outil lui-même
(« physical:native » de Codex, `"physical": true` d'Antigravity ; le moniteur ne voit pas une prise de premier plan
quand la cible était déjà devant). Lignes où la cible n'était pas recouverte : hors compte.
Usage : python bench/strict_tally.py results/<relevé>.json"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PHYSICAL = re.compile(r'physical:native|"physical":\s*true|SendInput|foreground taken|premier plan pris', re.I)


def strict(path: Path) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for app in json.loads(path.read_text(encoding="utf-8")):
        for task in app.get("tâches", []):
            counted = task.get("comptée", True)
            for tool in ("nexus", "cua"):
                attempt = task.get(tool, {}).get("arrière-plan")
                if not attempt:
                    continue
                stats = out.setdefault(
                    tool, {"lignes": 0, "comptées": 0, "strict": 0, "strict comptées": 0, "faux succès": 0}
                )
                ok = (
                    attempt.get("effet")
                    and attempt.get("premier plan") == "intact"
                    and not attempt.get("curseur déplacé")
                    and not PHYSICAL.search(attempt.get("réponse") or "")
                )
                stats["lignes"] += 1
                stats["comptées"] += bool(counted)
                stats["strict"] += bool(ok)
                stats["strict comptées"] += bool(ok and counted)
                stats["faux succès"] += bool(attempt.get("faux succès"))
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    for name in sys.argv[1:]:
        print(name, json.dumps(strict(Path(name)), ensure_ascii=False))
