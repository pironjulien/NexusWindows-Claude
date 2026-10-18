"""Superviseur (reloader) : le serveur reprend son nouveau code sans reconnexion du client. Serveur enfant simulé
(script JSON-RPC minimal), empreinte du code simulée : aucun vrai serveur ni fichier du paquet touché."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from nexus_windows import reloader

CHILD = r"""
import json, os, sys, time
from pathlib import Path
here = Path(sys.argv[1])
version = (here / "version").read_text()
seen = here / f"seen-{os.getpid()}"
def reply(msg_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n"); sys.stdout.flush()
def text(msg_id, value, error=False):
    reply(msg_id, {"content": [{"type": "text", "text": value}], "isError": error})
for line in sys.stdin:
    msg = json.loads(line)
    with seen.open("a") as f: f.write(msg.get("method", "réponse") + "\n")
    method, msg_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        reply(msg_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                       "serverInfo": {"name": "nexus-windows", "version": version}})
    elif method == "tools/list":
        names = json.loads((here / "tools").read_text())
        reply(msg_id, {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in names]})
    elif method == "tools/call":
        name = msg["params"]["name"]
        if name == "crash":
            os._exit(3)
        if name == "slow":
            time.sleep(1.0)
        if name == "silent":  # appel annulé : le SDK MCP n'y répond jamais
            continue
        if name == "stale-once" and (here / "stale").exists():
            (here / "stale").unlink()
            text(msg_id, "serveur nexus-windows dépassé : reconnecter (/mcp) (test). Rien n'a été fait.", True)
            continue
        text(msg_id, f"pid {os.getpid()} version {version}")
"""


class Out:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.messages: list[dict[str, Any]] = []

    def write(self, data: bytes) -> int:
        with self.lock:
            self.messages.append(json.loads(data))
        return len(data)

    def flush(self) -> None:
        pass

    def wait(self, predicate, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                for message in self.messages:
                    if predicate(message):
                        return message
            time.sleep(0.02)
        raise AssertionError(f"rien reçu ; messages : {self.messages}")

    def reply(self, msg_id, timeout: float = 20.0) -> dict[str, Any]:
        return self.wait(lambda m: m.get("id") == msg_id, timeout)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    (tmp_path / "child.py").write_text(CHILD, encoding="utf-8")
    (tmp_path / "version").write_text("1.0", encoding="utf-8")
    (tmp_path / "tools").write_text(json.dumps(["look", "act"]), encoding="utf-8")
    code = {"print": "v1"}
    monkeypatch.setattr(reloader, "SETTLE_S", 0.01)
    monkeypatch.setattr(reloader.codeprint, "disk_version", lambda root=None: (tmp_path / "version").read_text())
    out = Out()
    sup = reloader.Supervisor(
        [sys.executable, str(tmp_path / "child.py"), str(tmp_path)], out, fingerprint=lambda: code["print"]
    )
    sup.spawn()
    dispatcher = threading.Thread(target=sup.dispatch, daemon=True)
    dispatcher.start()
    counter = iter(range(1, 1000))

    def send(method: str, params: dict[str, Any] | None = None, *, notify: bool = False) -> Any:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            message["id"] = next(counter)
        sup.from_client(json.dumps(message).encode() + b"\n")
        return message.get("id")

    def call(name: str) -> dict[str, Any]:
        return out.reply(send("tools/call", {"name": name, "arguments": {}}))["result"]

    def handshake() -> None:
        out.reply(send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}}))
        send("notifications/initialized", notify=True)
        out.reply(send("tools/list"))

    def new_code(version: str, tools: list[str] | None = None) -> None:
        (tmp_path / "version").write_text(version, encoding="utf-8")
        if tools is not None:
            (tmp_path / "tools").write_text(json.dumps(tools), encoding="utf-8")
        code["print"] = f"v{version}"

    yield type("H", (), {"sup": sup, "out": out, "send": send, "call": call, "handshake": handshake,
                         "new_code": new_code, "dir": tmp_path})  # fmt: skip
    sup.inbox.put(None)
    sup.stop_child()


def _text(result: dict[str, Any]) -> str:
    return "\n".join(c["text"] for c in result["content"])


def _pid(result: dict[str, Any]) -> str:
    return _text(result).split("pid ")[1].split()[0]


def test_relays_and_announces_list_changed(harness):
    init = harness.out.reply(harness.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))
    assert init["result"]["capabilities"]["tools"]["listChanged"] is True  # le superviseur sait prévenir le client
    result = harness.call("look")
    assert "version 1.0" in _text(result) and "rechargé" not in _text(result)


def test_new_code_reloads_before_next_call_without_reconnection(harness):
    harness.handshake()
    first = harness.call("look")
    harness.new_code("2.0")
    second = harness.call("act")
    text = _text(second)
    assert text.startswith("nexus-windows rechargé sur son nouveau code (1.0 → 2.0), sans reconnexion")
    assert "refaire look" in text and "version 2.0" in text
    assert _pid(second) != _pid(first)
    seen = (harness.dir / f"seen-{_pid(second)}").read_text().split()
    assert seen[:3] == ["initialize", "notifications/initialized", "tools/list"]  # poignée de main rejouée
    assert _text(harness.call("act")).startswith("pid")  # note donnée une seule fois
    assert harness.sup.reloads == 1


def test_changed_tool_list_is_signalled_not_fatal(harness):
    harness.handshake()
    harness.new_code("2.0", tools=["look", "act", "clipboard"])
    text = _text(harness.call("look"))
    assert "outils changés (nouveaux : clipboard)" in text and "/mcp" in text and "version 2.0" in text
    harness.out.wait(lambda m: m.get("method") == "notifications/tools/list_changed")


def test_stale_refusal_is_replayed_on_new_code(harness):
    """Code changé entre la vérification du superviseur et celle du serveur : le refus (rien n'a été fait) n'atteint
    pas le client, l'appel est rejoué sur un serveur neuf."""
    harness.handshake()
    before = _pid(harness.call("look"))
    (harness.dir / "stale").write_text("1")
    result = harness.call("stale-once")
    assert not result["isError"] and "dépassé" not in _text(result)
    assert _pid(result) != before


def test_waits_for_running_call_before_reloading(harness):
    harness.handshake()
    slow = harness.send("tools/call", {"name": "slow", "arguments": {}})
    time.sleep(0.2)
    harness.new_code("2.0")
    quick = harness.send("tools/call", {"name": "look", "arguments": {}})
    slow_result = harness.out.reply(slow)["result"]
    assert "version 1.0" in _text(slow_result)  # l'appel en cours finit sur l'ancien serveur
    assert "version 2.0" in _text(harness.out.reply(quick)["result"])


def test_cancelled_call_never_blocks_the_next_reload(harness):
    """Régression 0.9.1 : un appel annulé par le client (jamais répondu) restait « en cours » ; le rechargement suivant
    l'attendait sans fin et plus aucun appel ne répondait."""
    harness.handshake()
    silent = harness.send("tools/call", {"name": "silent", "arguments": {}})
    harness.send("notifications/cancelled", {"requestId": silent, "reason": "timeout"}, notify=True)
    harness.new_code("2.0")
    result = harness.out.reply(harness.send("tools/call", {"name": "look", "arguments": {}}), timeout=5.0)["result"]
    assert "version 2.0" in _text(result)


def test_hung_call_is_abandoned_then_reload_proceeds(harness, monkeypatch):
    monkeypatch.setattr(reloader, "BUSY_WAIT_S", 0.3)
    harness.handshake()
    hung = harness.send("tools/call", {"name": "silent", "arguments": {}})  # jamais répondu, jamais annulé
    harness.new_code("2.0")
    result = harness.out.reply(harness.send("tools/call", {"name": "look", "arguments": {}}), timeout=5.0)["result"]
    assert "version 2.0" in _text(result)  # serveur figé relancé plutôt que tout bloquer
    lost = harness.out.reply(hung)["result"]
    assert lost["isError"] and "issue INCONNUE" in _text(lost)


def test_late_cancel_after_answer_leaves_nothing_behind(harness):
    harness.handshake()
    done = harness.send("tools/call", {"name": "look", "arguments": {}})
    harness.out.reply(done)
    harness.send("notifications/cancelled", {"requestId": done}, notify=True)
    assert not harness.sup.cancelled and not harness.sup.inflight


def test_child_crash_reports_unknown_outcome_then_restarts(harness):
    harness.handshake()
    crashed = harness.call("crash")
    assert crashed["isError"] and "issue INCONNUE" in _text(crashed)
    after = harness.call("look")
    assert not after["isError"] and "version 1.0" in _text(after)


def test_broken_new_code_answers_cleanly_and_recovers(harness, monkeypatch):
    harness.handshake()
    good = list(harness.sup.command)
    harness.sup.command = [sys.executable, "-c", "import sys; sys.stderr.write('SyntaxError: test\\n'); sys.exit(1)"]
    harness.new_code("2.0")
    broken = harness.call("look")
    assert broken["isError"] and "n'a pas pu redémarrer" in _text(broken) and "Rien n'a été fait" in _text(broken)
    harness.sup.command = good
    assert "version 2.0" in _text(harness.call("look"))  # code réparé : l'appel suivant repart


def test_stale_answer_detection():
    def answer(text: str) -> dict[str, Any]:
        return {"id": 1, "result": {"content": [{"type": "text", "text": text}], "isError": True}}

    assert reloader.is_stale_answer(
        answer("serveur nexus-windows dépassé : reconnecter (/mcp) (x). Rien n'a été fait.")
    )
    assert reloader.is_stale_answer(answer("Attention : serveur nexus-windows dépassé (x).\nliste"))
    assert not reloader.is_stale_answer(answer("Fenêtre introuvable : serveur nexus-windows dépassé"))


def test_entry_point_runs_supervisor_unless_child(monkeypatch):
    ran: list[str] = []
    monkeypatch.delenv(reloader.ENV, raising=False)
    monkeypatch.setattr(reloader.Supervisor, "serve", lambda self, stdin: ran.append("superviseur"))
    reloader.main()
    assert ran == ["superviseur"]
    assert reloader.child_command()[1:] == ["-m", "nexus_windows", "--child"]
    main = Path(reloader.__file__).with_name("__main__.py").read_text(encoding="utf-8")
    assert "reloader" in main
