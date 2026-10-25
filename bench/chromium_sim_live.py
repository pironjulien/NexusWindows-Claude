"""Banc vivant de l'activation simulée : Electron (ou Edge) du labo SANS DevTools, piloté par le vrai serveur MCP.

Lancé par l'outil `launch` du serveur (aucun vol du premier plan, utilisable pendant que Julien travaille : la fenêtre
de test est créée ici et fermée à la fin, aucune fenêtre de Julien n'est touchée). Chaque étape du scénario web de
tests/live/drive.py (saisies courtes et longues, éditeurs riches, pages figées ou lentes, <select>, Ctrl+K) est jugée
par le journal de la page (événement émis, empreinte du contenu exact), la relecture de l'outil, la technique
annoncée (« sans activation ») et les prises du premier plan par la cible, relevées toutes les millisecondes.

Usage : python bench/chromium_sim_live.py [electron|edge|occult]   (résultats : bench/results/<date>-chromium-sim-<kind>.json)
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "live"))
sys.path.insert(0, str(ROOT / "lab"))

import apps  # noqa: E402
import drive  # noqa: E402  (tests/live ajouté à sys.path juste au-dessus)
from mcp import Client  # noqa: E402
from redact import redact_home  # noqa: E402

from nexus_windows import win32 as w  # noqa: E402

SCENARIO = [s for s in drive.SCENARIO if s[0] in drive.WEB_ONLY | set(drive.WEB_EXPECT) | {
    "select Couleur", "key ctrl+k", "set Notes 2 lignes", "click Charlie"}]  # fmt: skip
SCENARIO.insert(0, ("focus Notes", [{"do": "focus", "target": "Notes"}], None))


def _spawn_plain_electron() -> apps.LabApp:
    """Electron du labo SANS les commutateurs de launch (comme une application déjà ouverte par Julien) : affiché sans
    activation (showInactive), garde du labo qui rend le premier plan s'il était pris. Pas d'attente de l'absence de
    Julien : aucune activation n'est demandée ici."""
    user_fg = w.foreground()
    app, ready = apps._start("electron", 150, 110, 0)
    guard = apps._ForegroundGuard(app.proc.pid, user_fg)
    try:
        apps._wait_window(app, ready, 30.0)
    finally:
        guard.finish(apps.GUARD_TAIL_S if app.hwnd else 0.0)
    return app


async def main(kind: str) -> None:
    rows: list[dict] = []
    fg0 = w.foreground()
    plain: apps.LabApp | None = None
    async with Client(drive.server_params(), read_timeout_seconds=180) as client:
        if kind == "occult":
            # Application déjà ouverte, sans commutateur, puis occultée (hors écran) : moteur de rendu « hidden ».
            plain = _spawn_plain_electron()
            hwnd = plain.hwnd
            await client.call_tool("look", {"window": hwnd, "show": "elements"})
            w.SetWindowPos(hwnd, 1, -4000, -4000, 0, 0, 0x1 | 0x10)  # SWP_NOSIZE | SWP_NOACTIVATE, en bas
            time.sleep(1.0)
            print(f"Fenêtre {hwnd} : Electron sans commutateur, hors écran")
        else:
            if kind == "edge":
                exe = str(apps.edge_path())
                args = f"--window-size=600,640 --app={(apps.LAB / 'web_app.html').as_uri()}"
            else:
                exe = str(apps.ELECTRON)
                args = f'"{apps.LAB / "electron_main.js"}" --lab-x=150 --lab-y=110'
            result = await client.call_tool("launch", {"app": exe, "args": args, "then": "elements"})
            text = drive.text_of(result)
            match = re.search(r"Window (\d+)", text)  # comptes rendus en anglais depuis 0.10.0
            print(text.splitlines()[0][:160])
            if not match:
                raise SystemExit(text[:400])
            hwnd = int(match.group(1))
        app = plain or apps.LabApp(kind, proc=None, hwnd=hwnd)  # type: ignore[arg-type]
        events = drive.Events(app)
        try:
            await drive.wait_stable(client, ("Nom", "Notes"))
            for label, steps, expect in SCENARIO:
                expect = drive.WEB_EXPECT.get(label, expect)
                events.reset()
                started = time.perf_counter()
                with drive.ForegroundHolds(hwnd) as holds:
                    result = await client.call_tool("act", {"window": hwnd, "steps": steps, "then": "none"})
                    seen = events.collect(3.0, (lambda s, x=expect: any(x(e) for e in s)) if expect else None)
                ms = round((time.perf_counter() - started) * 1000)
                report = drive.text_of(result).splitlines()[0] if drive.text_of(result) else "(vide)"
                ok = drive.step_ok(report) and not result.is_error
                if expect is not None:
                    ok = ok and any(expect(e) for e in seen)
                if label in drive.READ_BACK:
                    ok = ok and "read back ✓" in report
                without = "without activation" in report or "simulated activation" in report
                rows.append(
                    {
                        "étape": label,
                        "ok": ok,
                        "sans_activation": without,
                        "prises_premier_plan": len(holds.holds),
                        "plus_longue_ms": holds.longest_ms,
                        "ms": ms,
                        "compte_rendu": report[:300],
                        "événements": seen[-3:],
                    }
                )
                flag = "OK " if ok else "NON"
                print(
                    f"{flag} {label:34} {ms:5} ms  {'sans activation' if without else 'AUTRE TECHNIQUE':16} "
                    f"{holds.summary():40} {report[:110]}"
                )
        finally:
            if plain is not None:
                plain.close()
            else:
                await client.call_tool("window", {"window": hwnd, "action": "close"})
    intact = w.foreground() == fg0
    print(f"premier plan de Julien intact à la fin : {intact}")
    out = ROOT / "bench" / "results" / f"{datetime.datetime.now():%Y-%m-%d_%H%M}-chromium-sim-{kind}.json"
    text = json.dumps({"lignes": rows, "premier_plan_intact": intact}, ensure_ascii=False, indent=1)
    out.write_text(redact_home(text), "utf-8")
    passed = sum(r["ok"] for r in rows)
    print(f"{passed}/{len(rows)} réussies, {sum(r['sans_activation'] for r in rows)} sans activation → {out.name}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "electron"))
