"""Latence de look et act par le vrai serveur MCP, sur des fenêtres réelles créées ici (jamais celles de Julien) :
application Win32 du labo et Electron du labo sans commutateur, toutes deux affichées sans activation.

Mesures : look complet (liste + image), look liste seule, act clic avec then="none" et avec l'état rendu (then="both").
Médiane et p90 sur N appels (Valider : effet relu à chaque clic). Usage : python bench/speed.py [N] [--label avant]
Résultats : bench/results/<date>-speed-<label>.json
"""

from __future__ import annotations

import asyncio
import datetime
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "live"))
sys.path.insert(0, str(ROOT / "lab"))

import apps  # noqa: E402
import drive  # noqa: E402  (tests/live ajouté à sys.path juste au-dessus)
from mcp import Client  # noqa: E402

from nexus_windows import win32 as w  # noqa: E402


def spawn(kind: str) -> apps.LabApp:
    """Application du labo affichée sans activation ; garde du labo qui rend le premier plan s'il était pris."""
    user_fg = w.foreground()
    app, ready = apps._start(kind, 160, 120, 0)
    guard = apps._ForegroundGuard(app.proc.pid, user_fg)
    try:
        apps._wait_window(app, ready, 30.0)
    finally:
        guard.finish(apps.GUARD_TAIL_S if app.hwnd else 0.0)
    return app


async def measure(client: Client, hwnd: int, n: int) -> dict[str, dict[str, float]]:
    cases = {
        "look (liste + image)": ("look", {"window": hwnd}),
        "look (liste seule)": ("look", {"window": hwnd, "show": "elements"}),
        "act clic, then=none": ("act", {"window": hwnd, "steps": [{"do": "click", "target": "Valider"}], "then": "none"}),
        "act clic, then=both": ("act", {"window": hwnd, "steps": [{"do": "click", "target": "Valider"}]}),
    }  # fmt: skip
    out: dict[str, dict[str, float]] = {}
    for label, (tool, args) in cases.items():
        await client.call_tool(tool, args)  # échauffement (instantané, caches)
        times = []
        for _ in range(n):
            started = time.perf_counter()
            result = await client.call_tool(tool, args)
            times.append((time.perf_counter() - started) * 1000)
            if result.is_error:
                raise SystemExit(f"{label} : {drive.text_of(result)[:300]}")
        times.sort()
        out[label] = {"médiane_ms": round(statistics.median(times)), "p90_ms": round(times[int(0.9 * (n - 1))])}
        print(f"   {label:24} médiane {out[label]['médiane_ms']:5} ms   p90 {out[label]['p90_ms']:5} ms")
    return out


async def main(n: int, label: str) -> None:
    results: dict[str, dict] = {}
    async with Client(drive.server_params(), read_timeout_seconds=180) as client:
        for kind in ("win32", "electron"):
            app = spawn(kind)
            try:
                for _ in range(50):  # arbre publié (Electron : quelques secondes au premier lancement)
                    look = await client.call_tool("look", {"window": app.hwnd, "show": "elements"})
                    if "« Valider »" in drive.text_of(look):
                        break
                    await asyncio.sleep(0.2)
                print(f"== {kind} ({w.window_text(app.hwnd)})")
                results[kind] = await measure(client, app.hwnd, n)
            finally:
                app.close()
    out = ROOT / "bench" / "results" / f"{datetime.datetime.now():%Y-%m-%d_%H%M}-speed-{label}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("→", out.name)


if __name__ == "__main__":
    count = next((int(a) for a in sys.argv[1:] if a.isdigit()), 10)
    tag = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--label=")), "mesure")
    asyncio.run(main(count, tag))
