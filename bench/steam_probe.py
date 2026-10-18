"""Vérification de bout en bout sur le vrai Steam de Julien, en LECTURE SEULE, par le vrai serveur MCP (stdio).

Scénario : Steam ouvert (avec -cef-disable-occlusion -cef-force-accessibility, et -cef-enable-debugging si DevTools),
fenêtres laissées recouvertes : Paramètres (menu Steam > Paramètres si besoin) → onglet Stockage → lecture des
lecteurs. Rien n'est modifié : seuls des onglets sont choisis et des fenêtres lues. Chaque étape est jugée sur ce
que Steam affiche ensuite (texte relu), pas sur le compte rendu de l'outil.

Usage : python bench/steam_probe.py [--devtools] [--foreground]
  --devtools    enregistre le port DevTools d'un Steam déjà lancé avec -cef-enable-debugging (sans relance)
  --foreground  rejoue les clics avec foreground=true (fenêtre gardée devant pendant le lot) : accord de Julien
Résultats : bench/results/AAAA-MM-JJ_HHMM-steam.json
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from pathlib import Path

from mcp import Client, StdioServerParameters

from nexus_windows import win32 as w

ROOT = Path(__file__).resolve().parent
TABS = ("Téléchargements", "Stockage")


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def settings_hwnd() -> int:
    return next((h for h in w.enum_windows() if w.window_text(h) == "Paramètres Steam" and w.IsWindowVisible(h)), 0)


async def main(devtools: bool, foreground: bool) -> list[dict]:
    rows: list[dict] = []

    def row(label: str, ok: bool, report: str, ms: float) -> None:
        covered = "premier plan" not in report.split("\n", 1)[0]
        rows.append({"etape": label, "ok": ok, "recouverte": covered, "ms": round(ms), "rapport": report[:1500]})
        print(f"{'OK ' if ok else 'ÉCHEC'} {label} ({round(ms)} ms)\n   " + report[:600].replace("\n", "\n   "))

    params = StdioServerParameters(command=sys.executable, args=["-m", "nexus_windows"])
    async with Client(params, read_timeout_seconds=300) as client:
        if devtools:
            t0 = time.perf_counter()
            result = text_of(await client.call_tool("launch", {"app": "Steam", "devtools": True, "then": "none"}))
            row("launch Steam devtools (déjà ouvert)", "DevTools" in result, result, (time.perf_counter() - t0) * 1000)
        if not settings_hwnd():
            t0 = time.perf_counter()
            menu = text_of(
                await client.call_tool("look", {"window": "Steam", "show": "elements", "query": "Paramètres"})
            )
            if "MenuItem « Paramètres »" in menu:  # menu déjà ouvert (un clic sur « Steam » le refermerait)
                steps = [{"do": "click", "target": "menuitem:Paramètres"}]
            else:
                steps = [{"do": "menu", "path": "Steam > Paramètres"}]
            result = text_of(
                await client.call_tool(
                    "act", {"window": "Steam", "then": "none", "foreground": foreground, "steps": steps}
                )
            )
            time.sleep(1.5)
            row("menu Steam > Paramètres", bool(settings_hwnd()), result, (time.perf_counter() - t0) * 1000)
        hwnd = settings_hwnd()
        if not hwnd:
            return rows
        for tab in TABS:
            t0 = time.perf_counter()
            result = text_of(
                await client.call_tool(
                    "act",
                    {"window": hwnd, "then": "elements", "foreground": foreground,
                     "steps": [{"do": "click", "target": f"onglet:{tab}"}]},
                )
            )  # fmt: skip
            ok = "✓" in result.split("\n1. ", 1)[-1].split("\n", 1)[0]
            row(f"onglet {tab}", ok, result, (time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        result = text_of(await client.call_tool("look", {"window": hwnd, "show": "elements", "max_elements": 300}))
        drives = [line.strip() for line in result.splitlines() if ":\\" in line or "(C:)" in line or " GO" in line]
        row(
            "lecture du lecteur affiché (recouverte)",
            bool(drives),
            "\n".join(drives) or result,
            (time.perf_counter() - t0) * 1000,
        )
        # Liste des lecteurs : la liste déroulante des disques ouverte (lecture), lue, puis repliée (collapse : sans
        # effet si elle s'est déjà refermée ; pas Échap, qui ferme la fenêtre Paramètres de Steam).
        t0 = time.perf_counter()
        opened = text_of(
            await client.call_tool(
                "act",
                {"window": hwnd, "then": "elements", "foreground": foreground,
                 "steps": [{"do": "click", "target": "combobox:Disque local", "expect": "Ajouter un disque"}]},
            )
        )  # fmt: skip
        items = [line.strip() for line in opened.splitlines() if "ListItem" in line or "Option" in line]
        row(
            "liste des lecteurs ouverte",
            "Ajouter un disque" in opened,
            "\n".join(opened.split("\n")[3:4] + items),
            (time.perf_counter() - t0) * 1000,
        )
        t0 = time.perf_counter()
        closed = text_of(
            await client.call_tool(
                "act",
                {"window": hwnd, "then": "none", "foreground": foreground,
                 "steps": [{"do": "collapse", "target": "combobox:Disque local"},
                           {"do": "wait", "text": "Ajouter un disque", "gone": True}]},
            )
        )  # fmt: skip
        row("liste des lecteurs refermée", "✗" not in closed, closed, (time.perf_counter() - t0) * 1000)
    return rows


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):  # console ordinaire (pas une sortie remplacée)
        sys.stdout.reconfigure(encoding="utf-8")
    out = asyncio.run(main("--devtools" in sys.argv, "--foreground" in sys.argv))
    path = ROOT / "results" / time.strftime("%Y-%m-%d_%H%M-steam.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{sum(r['ok'] for r in out)}/{len(out)} étapes réussies → {path.name}")
