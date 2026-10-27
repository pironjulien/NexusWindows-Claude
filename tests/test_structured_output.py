"""Sorties structurées : windows, notifications et logs publient un outputSchema et joignent structuredContent, sans
changer le texte que lit le modèle (premier bloc)."""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

import anyio
from mcp.types import CallToolResult, TextContent

from nexus_windows import log, notifications, server, snapshot
from nexus_windows import win32 as w

STRUCTURED = {"windows": "windows", "notifications": "notifications", "logs": "records"}
UNSTRUCTURED = {"look", "act", "launch", "window"}


def _info(**changes: Any) -> snapshot.WindowInfo:
    base = snapshot.WindowInfo(
        hwnd=4242, title="Sans titre - Bloc-notes", process="notepad.exe", cls="Notepad", rect=w.Rect(10, 20, 810, 620),
        minimized=False, maximized=False, foreground=False, framework="Win32",
    )  # fmt: skip
    return dataclasses.replace(base, **changes)


def _call(name: str, args: dict[str, Any]) -> CallToolResult:
    async def call():
        return await server.mcp.call_tool(name, args)

    result = anyio.run(call)
    assert isinstance(result, CallToolResult) and not result.is_error
    return result


def test_output_schemas_published():
    async def listing():
        return {tool.name: tool for tool in await server.mcp.list_tools()}

    tools = anyio.run(listing)
    for name, key in STRUCTURED.items():
        schema = tools[name].output_schema
        assert schema and schema["type"] == "object" and key in schema["properties"], name
        assert key in schema.get("required", []), name
    for name in UNSTRUCTURED:
        assert tools[name].output_schema is None, name


def test_windows_text_unchanged_and_data_joined(monkeypatch):
    infos = [_info(), _info(hwnd=99, title="Ouvrir", owner=4242, foreground=True)]
    monkeypatch.setattr(snapshot, "app_windows", lambda: infos)
    monkeypatch.setattr(snapshot, "shell_surfaces", lambda: [])
    monkeypatch.setattr(server.w, "window_text", lambda hwnd: "Sans titre - Bloc-notes")

    result = _call("windows", {"filter": "notepad"})
    text = result.content[-1]
    assert isinstance(text, TextContent)
    assert text.text.endswith("\n".join(server.window_row(info) for info in infos))
    data = result.structured_content
    assert data is not None
    assert [row["hwnd"] for row in data["windows"]] == [4242, 99]
    assert data["windows"][0]["width"] == 800 and data["windows"][0]["height"] == 600
    assert data["windows"][1]["owner"] == 4242 and data["windows"][1]["foreground"] is True
    server.WindowsOutput.model_validate(data)


def test_notifications_data(monkeypatch):
    moment = dt.datetime(2026, 10, 27, 9, 30, tzinfo=dt.UTC)
    found = [notifications.Notification("Courrier", moment, ["Alice Martin", "Réunion à 10 h"])]
    monkeypatch.setattr(notifications, "read", lambda limit, app: found)

    result = _call("notifications", {})
    text = result.content[-1]
    assert isinstance(text, TextContent) and "27/10 09:30 Courrier: Alice Martin — Réunion à 10 h" in text.text
    assert result.structured_content == {
        "notifications": [{"app": "Courrier", "arrived": "2026-10-27T09:30:00+00:00", "texts": found[0].texts}]
    }


def test_logs_data(monkeypatch):
    records = [
        {"ts": "2026-10-27T09:30:00.123", "tool": "act", "ms": 412, "ok": False, "error": "refused",
         "args": {"window": "Bloc-notes", "foreground": False}, "steps": [{"n": 1, "do": "click", "method": "uia"}]},
        {"ts": "2026-10-27T09:31:00.000", "tool": "look", "ms": 80, "ok": True},
    ]  # fmt: skip
    monkeypatch.setattr(log, "tail", lambda count, errors_only: records)

    result = _call("logs", {})
    text = result.content[0]
    assert isinstance(text, TextContent) and "09:30:00.123 act 412 ms ERROR refused" in text.text
    data = result.structured_content
    assert data is not None and data["version"] == server.__version__
    first, second = data["records"]
    assert first["error"] == "refused" and first["args"] == {"window": "Bloc-notes"} and first["steps"][0]["n"] == 1
    assert second["ok"] is True and second["error"] is None


def test_notes_keep_structured_content():
    base = server._structured("row", server.WindowsOutput(windows=[]))
    noted = server._with_notes(base, ["Warning: server out of date."])
    assert isinstance(noted, CallToolResult)
    first = noted.content[0]
    assert isinstance(first, TextContent) and first.text == "Warning: server out of date.\nrow"
    assert noted.structured_content == {"windows": []}
