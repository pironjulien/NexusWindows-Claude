"""Bilan d'un relevé de bench/cua_vs_nexus.py : les comptes de RESULTS.md calculés depuis le JSON, jamais à la main.

Deux bilans : les lignes COMPTÉES (cible recouverte avant chaque essai et après ; champ « comptée », ou « recouverte »
dans les relevés d'avant 0.15.7) et toutes les lignes. Usage : python bench/tally_cua.py results/<relevé>.json
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

BACKGROUND, CLICKS, FRONT = "arrière-plan", "par clics", "premier plan"


def counted(row: dict) -> bool:
    return bool(row.get("comptée", row.get("recouverte")))


def tally(rows: list[dict], tool: str) -> dict[str, Any]:
    out: dict[str, Any] = {"essais": 0, "effet": 0, "par clics": 0, "premier plan": 0, "faux succès": 0}
    out.update({"non prouvé dit": 0, "premier plan pris": 0, "curseur déplacé": 0})
    durations: list[int] = []
    for row in rows:
        runs = row.get(tool) or {}
        background = runs.get(BACKGROUND)
        if not background:
            continue
        out["essais"] += 1
        out["faux succès"] += bool(background.get("faux succès"))
        out["non prouvé dit"] += background.get("déclaré") == "incertain" and not background.get("effet")
        out["premier plan pris"] += background.get("premier plan") != "intact"
        out["curseur déplacé"] += bool(background.get("curseur déplacé"))
        if background.get("effet"):
            out["effet"] += 1
            durations.append(background["ms"])
        elif (runs.get(CLICKS) or {}).get("effet"):
            out["par clics"] += 1
        elif (runs.get(FRONT) or {}).get("effet"):
            out["premier plan"] += 1
    out["médiane ms"] = round(statistics.median(durations)) if durations else None
    return out


def report(path: Path) -> str:
    families = json.loads(path.read_text(encoding="utf-8"))
    rows = [row for family in families for row in family.get("tâches", [])]
    lines = [f"Relevé {path.name} : {len(rows)} tâches, {sum(map(counted, rows))} comptées (cible recouverte)."]
    for label, subset in (("comptées", [r for r in rows if counted(r)]), ("toutes", rows)):
        lines.append(f"\n{label} :")
        for tool in ("nexus", "cua"):
            lines.append(f"  {tool:5} {json.dumps(tally(subset, tool), ensure_ascii=False)}")
    off = [f"{family['famille']}/{row['tâche']}" for family in families for row in family.get("tâches", [])]
    off = [name for name, row in zip(off, rows, strict=True) if not counted(row)]
    lines.append("\nhors compte : " + (", ".join(off) or "aucune"))
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    print(report(Path(sys.argv[1])))
