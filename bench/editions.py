"""Éditions NexusWindows mesurées par le banc à trois outils (`cua_vs_nexus.py`), chacune comme son guide le demande à
un agent (relevé des outils publiés le 24/10 : mêmes cinq outils, schémas d'`act` propres à chaque édition).

- claude : nexus-windows-claude (ce dépôt), act par étapes « do », cibles par nom, point « x,y » de l'image ;
- codex : nexus-windows-codex (npx), look (mode text, ou both pour un point : capture native, `screenshotId` et cadre)
  puis act par `kind` et sélecteurs (rôle et nom), avec l'`expect` que son guide demande ;
- antigravity : nexus-windows-antigravity (uvx), look puis act en un lot par libellé, point x/y de la fenêtre.

Édition choisie par NEXUS_BENCH_EDITION ; NEXUS_BENCH_CMD (liste JSON) remplace la commande (copie locale à mesurer)."""

from __future__ import annotations

import json
import os
from typing import Any

import apps
import drive
from mcp import Client, StdioServerParameters

from nexus_windows import win32 as w

EDITION = os.environ.get("NEXUS_BENCH_EDITION", "claude")
COMMANDS = {
    "codex": ["npx.cmd", "-y", "nexus-windows-codex"],
    "antigravity": ["uvx", "--python", "3.14", "nexus-windows-antigravity"],
}
LABELS = {"claude": "nexus-windows-claude", "codex": "nexus-windows-codex", "antigravity": "nexus-windows-antigravity"}


def params() -> StdioServerParameters:
    cmd = json.loads(os.environ["NEXUS_BENCH_CMD"]) if os.environ.get("NEXUS_BENCH_CMD") else COMMANDS[EDITION]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "UV_LINK_MODE": "copy"}
    return StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)


def look_args(hwnd: int, image: bool = False) -> dict[str, Any]:
    if EDITION == "codex":
        return {"window": hwnd, "mode": "both" if image else "text"}
    return {"hwnd": hwnd, "capture_screenshot": image, "max_elements": 200}


def _json_after_first_line(text: str) -> dict[str, Any]:
    start = text.find("{")
    try:
        return json.loads(text[start:]) if start >= 0 else {}
    except ValueError:
        return {}


def claim(text: str) -> str:
    """« ok », « incertain » ou « échec », d'après ce que l'édition annonce (son statut, jamais l'effet)."""
    if EDITION == "antigravity":
        status = _json_after_first_line(text).get("status")
        return "ok" if status == "success" else ("incertain" if status == "unverified" else "échec")
    first = text.strip().splitlines()[0] if text.strip() else ""
    mark = first[:1]
    return "ok" if mark in ("✓", "=") else ("incertain" if mark == "~" else "échec")


def _sel(name: str, role: str | None = None) -> dict[str, str]:
    return {"name": name, **({"role": role} if role else {})}


# Élément de statut de chaque application du labo (identifiant UI Automation lu par le look de Codex le 24/10) : c'est
# l'effet que l'application expose à l'outil, donc la postcondition qu'un agent écrirait (relecture de Codex : sans
# expect, sa réponse est « ~ » par construction). Tk n'a pas d'UI Automation : pas d'expect, « ~ » assumé.
STATUS = {
    "win32": {"automationId": "8"},
    # WinForms et WPF : nom constant « Statut » (le texte affiché n'est pas dans le nom) : pas d'expect possible sur ce
    # champ, « ~ » assumé (relecture de Codex du 24/10).
    "electron": {"role": "text", "ancestor": {"automationId": "statut"}},
    "edge": {"role": "text", "ancestor": {"automationId": "statut"}},
}
STATUS_TOKEN = {
    "bouton": "clic",
    "menu": "menu action",
    "clic brut": "zone gauche",
    "molette": "molette",
    "défilement": "defil A bas",
    "raccourci ctrl+k": "raccourci ctrl+k",
}


def _status_expect(kind: str, task: str) -> dict[str, Any]:
    selector = STATUS.get(kind)
    if selector is None or task not in STATUS_TOKEN:
        return {}
    return {"expect": {"selector": selector, "field": "name", "includes": STATUS_TOKEN[task]}}


async def run(
    client: Client, task: str, app: apps.LabApp, values: dict[str, str], point: tuple[int, int] | None
) -> tuple[Any, list[str]]:
    """Appels qu'un agent ferait avec cette édition pour la tâche : regard puis action, comme son guide le demande."""
    names: list[str] = []

    async def call(name: str, args: dict) -> Any:
        names.append(name)
        return await client.call_tool(name, args)

    hwnd = app.hwnd
    v = values
    if EDITION == "codex":
        if point is not None:
            seen = await call("look", look_args(hwnd, image=True))
            images = _json_after_first_line(drive.text_of(seen)).get("images") or []
            if not images:
                return seen, names
            frame = images[0]["frame"]
            at = {"x": point[0] - frame["left"], "y": point[1] - frame["top"]}
            base = {"screenshotId": images[0]["id"], "point": at}
            step = {"kind": "click", **base} if task == "clic brut" else {"kind": "scroll", **base, "scrollY": 120}
            return await call("act", {"steps": [{**step, **_status_expect(app.kind, task)}]}), names
        await call("look", look_args(hwnd))
        if task == "éditeur riche":
            # Clic de focus relu (expect focused) ; refusé ou non prouvé : rien n'est tapé, sa réponse fait foi.
            focus_step = {
                "kind": "click",
                "selector": _sel("Éditeur"),
                "expect": {"selector": _sel("Éditeur"), "field": "focused", "equals": True},
            }
            clicked = await call("act", {"steps": [focus_step]})
            if claim(drive.text_of(clicked)) != "ok":
                return clicked, names
            await call("look", look_args(hwnd))
            step = {
                "kind": "typeText",
                "focus": _sel("Éditeur"),
                "text": v["edit"],
                "expect": {"selector": _sel("Éditeur"), "field": "value", "includes": v["edit"]},
            }
        else:
            step = {
                "bouton": {"kind": "click", "selector": _sel("Valider", "button")},
                "case": {
                    "kind": "click",
                    "selector": _sel("Option", "checkbox"),
                    "expect": {"selector": _sel("Option", "checkbox"), "field": "checked", "equals": True},
                },
                "champ": {
                    "kind": "setValue",
                    "selector": _sel("Nom", "edit"),
                    "value": v["nom"],
                    "expect": {"selector": _sel("Nom", "edit"), "field": "value", "equals": v["nom"]},
                },
                "liste déroulante": {
                    "kind": "select",
                    "selector": _sel("Couleur", "combobox"),
                    "option": v["couleur"],
                    "expect": {"selector": _sel("Couleur", "combobox"), "field": "value", "equals": v["couleur"]},
                },
                "élément de liste": {
                    "kind": "click",
                    "selector": _sel(v["item"], "listitem"),
                    "expect": {"selector": _sel(v["item"], "listitem"), "field": "selected", "equals": True},
                },
                "menu": {"kind": "menu", "path": "Fichier > Action menu", **_status_expect(app.kind, "menu")},
                "raccourci ctrl+k": {"kind": "key", "key": "ctrl+k", **_status_expect(app.kind, "raccourci ctrl+k")},
            }[task]
            if task == "bouton":
                step = {**step, **_status_expect(app.kind, "bouton")}
        return await call("act", {"steps": [step]}), names
    # antigravity
    await call("look", look_args(hwnd))
    if point is not None:
        rect = w.window_rect(hwnd)
        x, y = point[0] - rect.left, point[1] - rect.top
        step = {"action": "click", "x": x, "y": y} if task == "clic brut" else {
            "action": "scroll", "x": x, "y": y, "delta_y": -1}  # fmt: skip
    else:
        step = {
            "bouton": {"action": "click", "target": "Valider"},
            "case": {"action": "check", "target": "Option"},
            "champ": {"action": "set", "target": "Nom", "text": v["nom"]},
            "éditeur riche": {"action": "type", "target": "Éditeur", "text": v["edit"]},
            "liste déroulante": {"action": "select", "target": "Couleur", "option": v["couleur"]},
            "élément de liste": {"action": "click", "target": v["item"]},
            "menu": {"action": "menu", "path": "Fichier > Action menu"},
            "raccourci ctrl+k": {"action": "key", "key": "ctrl+k"},
        }[task]
    return await call("act", {"hwnd": hwnd, "steps": [step]}), names
