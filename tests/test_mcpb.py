"""Extension Claude Desktop (mcpb/) : même version partout, serveur PyPI épinglé à cette version, mêmes outils que le
serveur, Python 3.14 seulement (uv prendrait sinon la plus récente : 3.15 relevé le 25/10)."""

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
    assert manifest["version"] == project["version"] == __version__
    assert project["dependencies"] == [f"nexus-windows-claude=={__version__}"]
    assert f'name = "nexus-windows-claude"\nversion = "{__version__}"' in (MCPB / "uv.lock").read_text(encoding="utf-8")
    assert project["requires-python"] == manifest["compatibility"]["runtimes"]["python"] == ">=3.14,<3.15"


def test_manifest_tools_are_the_server_tools():
    manifest = json.loads((MCPB / "manifest.json").read_text(encoding="utf-8"))
    declared = {tool["name"] for tool in manifest["tools"]}
    assert declared == {tool.name for tool in anyio.run(server.mcp.list_tools)}
