"""Essai réel de l'OCR en secours par le vrai serveur MCP : application Tk du labo (aucun élément UIA).

look : textes lus par OCR en [N] avec @x,y ; act par nom sur un texte OCR : refus honnête en arrière-plan (Tk ignore
les messages), puis foreground=true (labo, Julien absent) : effet prouvé par l'application. Usage : python bench/ocr_live.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lab"))
sys.path.insert(0, str(ROOT / "tests" / "live"))

import apps  # noqa: E402
import drive  # noqa: E402
from mcp import Client  # noqa: E402


async def main() -> None:
    app = apps.launch("tk", x=150, y=110)
    try:
        async with Client(drive.server_params(), read_timeout_seconds=120) as client:
            await asyncio.sleep(1.0)
            t0 = time.perf_counter()
            look = drive.text_of(await client.call_tool("look", {"window": app.hwnd, "show": "elements"}))
            print(f"look ({(time.perf_counter() - t0) * 1000:.0f} ms) :\n{look}\n")
            steps = [{"do": "click", "target": "Valider"}]
            background = await client.call_tool("act", {"window": app.hwnd, "steps": steps, "then": "none"})
            print("arrière-plan :", drive.text_of(background).splitlines()[0][:200])
            app.drain()
            front = await client.call_tool(
                "act", {"window": app.hwnd, "steps": steps, "then": "none", "foreground": True}
            )
            seen = app.last_events(1.5)
            print("premier plan :", drive.text_of(front).splitlines()[0][:200], "⇒", seen)
            print("EFFET PROUVÉ" if any(e.startswith("clic") for e in seen) else "AUCUN EFFET")
    finally:
        app.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    asyncio.run(main())
