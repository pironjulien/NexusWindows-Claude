"""Le serveur démarre et liste ses outils (initialize puis tools/list), sous Windows comme sous Linux, où les annuaires
MCP (Glama) le lancent dans un conteneur ; la CI le lance aussi sous Linux. Profil temporaire (HOME, USERPROFILE).
Sous Windows, le serveur est servi sans main() : ni raccourci d'arrêt d'urgence, ni Raw Input, ni fils de démarrage
pendant le test ; ailleurs, le vrai point d'entrée (qui ne les démarre pas hors Windows)."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from nexus_windows import __version__, freshness, keys, policy, server, snapshot, stop, worker
from nexus_windows.session import Session

TOOLS = {"windows", "look", "act", "launch", "window", "logs"}
HWND_TEST = 4242  # fenêtre des doubles de test_contract


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


# --- Textes reçus par le client : anglais, neutres --------------------------------------------------------------------
# Marqueurs du français : lettres accentuées, guillemets « », et mots qui n'existent qu'en français (mots entiers).
FRENCH_WORDS = (
    "les", "des", "une", "est", "avec", "sans", "pour", "dans", "fenetre", "cette", "etre", "sont", "aussi", "puis",
    "sinon", "seulement", "jamais", "rien", "aucun", "aucune", "deja", "etape", "pas", "ou", "sur", "du", "et",
)  # fmt: skip
FRENCH = re.compile(r"[àâçéèêëîïôöûùüœÀÂÇÉÈÊ«»]|\b(" + "|".join(FRENCH_WORDS) + r")\b", re.IGNORECASE)
STEERING = re.compile(r"prefer|instead of|rather than|plutôt que|claude in chrome", re.IGNORECASE)


def _french(text: str) -> list[str]:
    return [match.group(0) for match in FRENCH.finditer(text)]


def _descriptions(schema: object) -> list[str]:
    """Toutes les descriptions d'un schéma JSON (paramètres, modèles imbriqués, $defs)."""
    found: list[str] = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "description" and isinstance(value, str):
                found.append(value)
            else:
                found += _descriptions(value)
    elif isinstance(schema, list):
        for item in schema:
            found += _descriptions(item)
    return found


def _client_texts() -> dict[str, str]:
    """Instructions, descriptions des outils et de leurs paramètres, telles que tools/list les publie."""
    texts = {"instructions": server.INSTRUCTIONS}
    for tool in anyio.run(server.mcp.list_tools):
        texts[f"{tool.name}"] = tool.description or ""
        for i, text in enumerate(_descriptions(tool.input_schema)):
            texts[f"{tool.name} schema #{i}"] = text
    return texts


def test_tool_list_has_no_french():
    texts = _client_texts()
    assert {"look", "act", "launch", "windows", "window", "logs"} <= set(texts)
    leaks = {where: _french(text) for where, text in texts.items() if _french(text)}
    assert not leaks


def test_tool_list_does_not_steer_to_other_tools():
    texts = _client_texts()
    steering = {where: STEERING.findall(text) for where, text in texts.items() if STEERING.search(text)}
    assert not steering


def _error_text(call) -> str:
    try:
        result = call()
    except Exception as error:  # noqa: BLE001 - le texte de l'erreur est ce que lit le modèle
        return str(error)
    raise AssertionError(f"erreur attendue, résultat {result!r}")


def test_error_and_refusal_texts_have_no_french(monkeypatch):
    """Refus et erreurs hors ligne (fenêtre inconnue, étape invalide, steps manquant, geste à confirmer, mot de passe,
    cible absente, arrêt d'urgence, serveur dépassé, politique) : aucun marqueur du français ; symboles ✓ ~ ✗ = intacts."""
    import asyncio

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_contract import FakeAuto, as_call, executor, make_view, node

    monkeypatch.setattr(stop.log, "event", lambda *a, **k: None)
    monkeypatch.setattr(snapshot, "app_windows", lambda: [])  # jamais les titres des vraies fenêtres du poste
    texts: dict[str, str] = {}
    wk = worker.Worker()
    monkeypatch.setattr(server, "WORKER", wk)
    monkeypatch.setattr(server, "SESSION", Session())
    try:
        texts["unknown window"] = _error_text(lambda: asyncio.run(server.look(window="zz-no-such-window")))
        texts["no current window"] = _error_text(lambda: asyncio.run(server.act(steps=[server.Step(do="click")])))
        texts["invalid step"] = _error_text(lambda: anyio.run(server.mcp.call_tool, "act", {"steps": [{"do": "fly"}]}))
        texts["windows empty"] = str(asyncio.run(server.windows(filter="zz-no-such-window")))
        texts["logs"] = asyncio.run(server.logs())
        monkeypatch.setattr(freshness, "stale_reason", lambda: f"{freshness.STALE} (test)")
        texts["stale"] = _error_text(lambda: asyncio.run(server._run(lambda auto: "done")))
    finally:
        wk.abandon()
    texts["steps missing"] = _error_text(lambda: server._planned("app", {}, None, None, None, []))
    texts["routine and steps"] = _error_text(
        lambda: server._planned("app", {}, [server.Step(do="click")], "r", None, [])
    )
    texts["unknown routine"] = _error_text(lambda: server._planned("app", {}, None, "none-such", None, []))
    texts["key"] = _error_text(lambda: keys.parse_sequence("ctrl+nokey"))
    texts["hard block"] = policy.hard_block("keepass.exe", "") + " / " + policy.hard_block("consent.exe", "")
    texts["launch"] = _error_text(
        lambda: policy.check_launch_exe(r"C:\Program Files\Google\Chrome\chrome.exe", "--remote-debugging-port=1")
    )

    send = node("Button", "Send", has_invoke=True)
    password = node("Edit", "Password", has_value=True, password=True)
    ex = executor(make_view(send, password), FakeAuto())
    texts["confirm"] = _error_text(lambda: ex.click("Send"))
    texts["password"] = _error_text(lambda: ex.write("Password", "secret", replace=True))
    texts["missing target"] = _error_text(lambda: ex.click("Nothing here"))
    steps = [server.Step(do="click", target="Send"), server.Step(do="click", target="Send")]
    symbols: list[str] = []
    report, failed = server._run_steps(ex, steps, HWND_TEST, as_call(), symbols)
    texts["batch"] = "\n".join([*report, server._tally(symbols)])
    assert failed and symbols == ["✗"] and report[0].startswith("1. ✗ click: ")

    stop.pressed()
    texts["stopped"] = _error_text(lambda: server._run_steps(ex, steps, HWND_TEST, as_call(), []))
    assert "1. ⛔ click interrupted" in texts["stopped"]

    leaks = {where: (_french(text), text) for where, text in texts.items() if _french(text)}
    assert not leaks
    assert "nothing was done" in texts["confirm"] and "confirm=true" in texts["confirm"]
