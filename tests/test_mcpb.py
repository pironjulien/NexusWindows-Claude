"""Extension Claude Desktop (mcpb/) : même version partout dans l'extension, serveur PyPI épinglé à cette version (la
dernière publiée : elle suit le serveur d'une publication, son verrou ne peut viser qu'une version déjà sur PyPI),
mêmes outils que le serveur, Python 3.14 seulement (uv prendrait sinon la plus récente : 3.15 relevé le 25/10)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import anyio

from nexus_windows import __version__, server

ROOT = Path(__file__).resolve().parents[1]
MCPB = ROOT / "mcpb"


def test_versions_and_pin_follow_the_server():
    manifest = json.loads((MCPB / "manifest.json").read_text(encoding="utf-8"))
    project = tomllib.loads((MCPB / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = manifest["version"]
    assert project["version"] == version and project["dependencies"] == [f"nexus-windows-claude=={version}"]
    assert f'name = "nexus-windows-claude"\nversion = "{version}"' in (MCPB / "uv.lock").read_text(encoding="utf-8")
    assert tuple(map(int, version.split("."))) <= tuple(map(int, __version__.split(".")))
    assert project["requires-python"] == manifest["compatibility"]["runtimes"]["python"] == ">=3.14,<3.15"


def test_manifest_tools_are_the_server_tools():
    manifest = json.loads((MCPB / "manifest.json").read_text(encoding="utf-8"))
    declared = {tool["name"] for tool in manifest["tools"]}
    assert declared == {tool.name for tool in anyio.run(server.mcp.list_tools)}
