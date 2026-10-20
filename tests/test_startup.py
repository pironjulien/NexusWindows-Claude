"""Le serveur démarre et liste ses outils (initialize puis tools/list), sous Windows comme sous Linux, où les annuaires
MCP (Glama) le lancent dans un conteneur ; la CI le lance aussi sous Linux. Profil temporaire (HOME, USERPROFILE).
Sous Windows, le serveur est servi sans main() : ni raccourci d'arrêt d'urgence, ni Raw Input, ni fils de démarrage
pendant le test ; ailleurs, le vrai point d'entrée (qui ne les démarre pas hors Windows)."""

from __future__ import annotations

import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from nexus_windows import __version__

TOOLS = {"windows", "look", "act", "launch", "window", "notifications", "logs"}


def test_initialize_and_list_tools(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    if sys.platform == "win32":
        args = ["-c", "from nexus_windows import server; server.mcp.run('stdio')"]
    else:
        args = ["-m", "nexus_windows"]
    params = StdioServerParameters(command=sys.executable, args=args, env=env)

    async def session_check():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "nexus-windows"
            assert init.server_info.version == __version__
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert set(tools) == TOOLS
            for tool in tools.values():
                hints = tool.annotations
                assert hints is not None and hints.title, tool.name
                assert hints.read_only_hint is not None and hints.destructive_hint is not None, tool.name
                assert hints.idempotent_hint is not None, tool.name
            hints = {name: tool.annotations for name, tool in tools.items()}
            assert hints["look"] and hints["look"].read_only_hint
            assert hints["act"] and hints["act"].destructive_hint
            if sys.platform != "win32":
                result = await session.call_tool("windows", {})
                assert result.is_error

    async def check():
        with anyio.fail_after(60):
            await session_check()

    anyio.run(check)
