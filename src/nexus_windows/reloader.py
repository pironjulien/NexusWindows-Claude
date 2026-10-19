"""Superviseur de nexus-windows : le serveur reprend son nouveau code tout seul, sans reconnexion du client.

Le client (Claude Code) lance ce superviseur ; c'est lui qui tient la liaison MCP (stdio). Il relaie chaque message
vers un processus serveur enfant (`python -m nexus_windows`, marqué par la variable NEXUS_WINDOWS_SUPERVISOR). Avant
chaque appel d'outil, il compare l'empreinte du code sur disque (codeprint) à celle du serveur en cours : si elle a
changé, il attend la fin des appels en cours, laisse le disque se stabiliser (installation en cours), arrête l'enfant,
en lance un neuf et lui rejoue la poignée de main du client (initialize, puis notifications/initialized). Le client
ne voit rien, sauf une note en tête de la réponse : serveur rechargé, numéros [N] d'avant oubliés.

Même principe que les mandataires de rechargement MCP (reloaderoo…) : un processus neuf plutôt qu'un rechargement des
modules à chaud (importlib.reload), qui laisserait cohabiter ancien et nouveau code (fil COM, raccourci d'arrêt
d'urgence, jeton de premier plan, instantanés). Le processus neuf reprend tous les garde-fous tels qu'ils sont sur
disque ; le jeton de premier plan (verrou de fichier) est rendu par Windows à la mort de l'ancien.

Liste d'outils changée par le nouveau code : le client reçoit notifications/tools/list_changed (capacité annoncée) et
la réponse le signale ; un client qui ne relit pas la liste garde les anciens outils jusqu'à /mcp, sans rien casser.
Ce module ne dépend que de la bibliothèque standard : ses propres changements ne s'appliquent qu'à la reconnexion
suivante (signalé par l'outil logs).
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import IO, Any, Protocol

from . import codeprint, log

ENV = "NEXUS_WINDOWS_SUPERVISOR"  # pid du superviseur, dans l'environnement de l'enfant
ENV_CODE = "NEXUS_WINDOWS_SUPERVISOR_CODE"  # empreinte du code du superviseur à son lancement
# Début d'une réponse « serveur dépassé » (freshness.STALE, préfixé de « Warning: » par server.py pour une lecture).
# Les formes françaises restent reconnues : un enfant lancé sur un code d'avant la traduction peut encore répondre.
STALE_HEADS = (
    "nexus-windows server out of date",
    "Warning: nexus-windows server out of date",
    "serveur nexus-windows dépassé",
    "Attention : serveur nexus-windows dépassé",
)
SETTLE_S = 0.4  # disque relu jusqu'à deux empreintes identiques à SETTLE_S d'écart (installation en cours)
SETTLE_MAX_S = 10.0
START_TIMEOUT_S = 60.0
STOP_TIMEOUT_S = 5.0
BUSY_WAIT_S = 120.0  # au-delà, un appel sans réponse est tenu pour figé : serveur relancé (jamais d'attente sans fin)
RETRIES = 2  # appel refusé « dépassé » par l'enfant (code changé entre les deux vérifications) : rejoué
INTERNAL = "nexus-reloader-"  # identifiants des requêtes du superviseur lui-même
TAIL_LINES = 20

Message = dict[str, Any]


class Sink(Protocol):
    """Sortie vers le client (stdout binaire)."""

    def write(self, data: bytes, /) -> int: ...
    def flush(self) -> None: ...


class StartError(Exception):
    """L'enfant n'a pas démarré (code cassé, installation incomplète)."""


def _encode(message: Message) -> bytes:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _first_text(result: Any) -> str:
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list) and content and isinstance(content[0], dict):
        return str(content[0].get("text", ""))
    return ""


def is_stale_answer(message: Message) -> bool:
    """Réponse d'un enfant qui se sait dépassé : refus avant tout effet, ou lecture avertie (rejouable)."""
    return _first_text(message.get("result")).startswith(STALE_HEADS)


def _tool_error(request_id: Any, text: str) -> Message:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


def _tools(result: Any) -> dict[str, str]:
    tools = result.get("tools", []) if isinstance(result, dict) else []
    return {t["name"]: json.dumps(t, sort_keys=True) for t in tools if isinstance(t, dict) and "name" in t}


class Supervisor:
    def __init__(self, command: list[str], out: Sink, *, fingerprint=codeprint.fingerprint) -> None:
        self.command = command
        self.out = out
        self.fingerprint = fingerprint
        self.out_lock = threading.Lock()
        self.in_lock = threading.Lock()  # stdin de l'enfant : fil de distribution et fil stdin (annulations) y écrivent
        self.cond = threading.Condition()
        self.inbox: queue.Queue[tuple[Message, int] | None] = queue.Queue()
        self.child: subprocess.Popen[bytes] | None = None
        self.child_print = ""
        self.child_version = "?"
        self.generation = 0
        self.force_reload = False
        self.inflight: dict[Any, tuple[Message, int]] = {}
        self.queued: set[Any] = set()  # requêtes du client encore dans la file
        self.cancelled: set[Any] = set()  # annulées avant d'avoir quitté la file : jamais envoyées
        self.waiters: dict[str, queue.Queue[Message]] = {}
        self.notes: dict[Any, str] = {}
        self.init_request: Message | None = None
        self.initialized: Message | None = None
        self.tools_requests: set[Any] = set()
        self.client_tools: dict[str, str] | None = None
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=TAIL_LINES)
        self.counter = 0
        self.reloads = 0

    # --- Sorties ----------------------------------------------------------------------------------------------
    def to_client(self, data: bytes | Message) -> None:
        with self.out_lock:
            self.out.write(data if isinstance(data, bytes) else _encode(data))
            self.out.flush()

    def to_child(self, message: Message) -> None:
        child = self.child
        if child is None or child.stdin is None:
            raise BrokenPipeError("server stopped")
        with self.in_lock:
            child.stdin.write(_encode(message))
            child.stdin.flush()

    # --- Processus enfant -------------------------------------------------------------------------------------
    def alive(self) -> bool:
        return self.child is not None and self.child.poll() is None

    def spawn(self) -> None:
        self.child_print = self.fingerprint()
        self.child_version = codeprint.disk_version()
        env = {**os.environ, ENV: str(os.getpid()), ENV_CODE: codeprint.supervisor_fingerprint()}
        self.generation += 1
        child = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        self.child = child
        generation = self.generation
        threading.Thread(
            target=self._read_child, args=(child, generation), name="nexus-reloader-out", daemon=True
        ).start()
        threading.Thread(target=self._relay_stderr, args=(child,), name="nexus-reloader-err", daemon=True).start()

    def stop_child(self) -> None:
        child, self.child = self.child, None
        self.generation += 1  # ses dernières sorties sont ignorées
        if child is None:
            return
        with contextlib.suppress(OSError):
            if child.stdin:
                child.stdin.close()  # fin de stdio : le serveur s'arrête de lui-même
        try:
            child.wait(STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(STOP_TIMEOUT_S)

    def _relay_stderr(self, child: subprocess.Popen[bytes]) -> None:
        assert child.stderr is not None
        for raw in child.stderr:
            self.stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
            with contextlib.suppress(OSError, ValueError):
                sys.stderr.buffer.write(raw)
                sys.stderr.buffer.flush()

    def _read_child(self, child: subprocess.Popen[bytes], generation: int) -> None:
        assert child.stdout is not None
        for raw in child.stdout:
            if generation != self.generation:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue  # le protocole n'écrit que du JSON sur stdout
            if "method" in message or "id" not in message:
                self.to_client(raw)  # requête ou notification du serveur vers le client
                continue
            self._on_response(message, raw)
        if generation == self.generation:
            self._child_died()

    def _on_response(self, message: Message, raw: bytes) -> None:
        request_id = message["id"]
        if isinstance(request_id, str) and request_id.startswith(INTERNAL):
            waiter = self.waiters.get(request_id)
            if waiter:
                waiter.put(message)
            return
        with self.cond:
            entry = self.inflight.pop(request_id, None)
            self.cond.notify_all()
        if entry and entry[0].get("method") == "tools/call" and entry[1] < RETRIES and is_stale_answer(message):
            self.force_reload = True
            self._requeue(entry[0], entry[1] + 1)  # rien n'a été fait : rejoué sur le nouveau code
            return
        changed = False
        result = message.get("result")
        # Note et suivi retirés dans tous les cas : une réponse en erreur JSON-RPC ne doit rien laisser d'orphelin.
        note = self.notes.pop(request_id, None)
        listed = request_id in self.tools_requests
        self.tools_requests.discard(request_id)
        if isinstance(result, dict):
            if self.init_request and request_id == self.init_request.get("id"):
                result.setdefault("capabilities", {}).setdefault("tools", {})["listChanged"] = True
                changed = True
            if listed:
                self.client_tools = _tools(result)
            if note and isinstance(result.get("content"), list):
                result["content"].insert(0, {"type": "text", "text": note})
                changed = True
        self.to_client(message if changed else raw)

    def _child_died(self) -> None:
        with self.cond:
            lost, self.inflight = self.inflight, {}
            self.cond.notify_all()
        tail = " | ".join(list(self.stderr_tail)[-3:])
        log.event("reloader-child-stopped", {"lost_calls": len(lost), "stderr": tail[:300]})
        for request_id, (request, _) in lost.items():
            text = (
                "nexus-windows stopped during the call: UNKNOWN outcome. Read the state again (look) before redoing it."
            )
            if request.get("method") == "tools/call":
                self.to_client(_tool_error(request_id, text))
            else:
                self.to_client({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": text}})

    # --- Rechargement -----------------------------------------------------------------------------------------
    def _internal(self, method: str, params: Any) -> Message:
        self.counter += 1
        request_id = f"{INTERNAL}{self.counter}"
        waiter: queue.Queue[Message] = queue.Queue()
        self.waiters[request_id] = waiter
        try:
            self.to_child({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + START_TIMEOUT_S
            while True:
                try:
                    reply = waiter.get(timeout=0.2)
                    break
                except queue.Empty:
                    if not self.alive() or time.monotonic() > deadline:
                        raise StartError(f"no answer to {method}") from None
        except OSError as error:
            raise StartError(f"link broken ({error})") from error
        finally:
            self.waiters.pop(request_id, None)
        if "error" in reply:
            raise StartError(f"{method} refused: {reply['error']}")
        return reply.get("result") or {}

    def _settled_print(self) -> str:
        """Empreinte du disque une fois stable (installation, synchronisation ou enregistrement en cours)."""
        current = self.fingerprint()
        deadline = time.monotonic() + SETTLE_MAX_S
        while time.monotonic() < deadline:
            time.sleep(SETTLE_S)
            again = self.fingerprint()
            if again == current and again:
                return again
            current = again
        return current

    def reload(self, reason: str) -> str:
        """Nouveau serveur sur le code du disque, poignée de main du client rejouée. Rend la note pour le modèle."""
        before = self.child_version
        started = time.monotonic()
        self.stop_child()
        self._settled_print()
        self.stderr_tail.clear()
        self.spawn()
        notes = [f"nexus-windows reloaded on its new code ({before} → {self.child_version}), without reconnecting"]
        if self.init_request is not None:
            init = self._internal("initialize", self.init_request.get("params", {}))
            self.child_version = str(init.get("serverInfo", {}).get("version", self.child_version))
            notes[0] = f"nexus-windows reloaded on its new code ({before} → {self.child_version}), without reconnecting"
            if self.initialized is not None:
                self.to_child(self.initialized)
            if self.client_tools is not None:
                tools = _tools(self._internal("tools/list", {}))
                if tools != self.client_tools:
                    known = self.client_tools
                    groups = {
                        "new": sorted(set(tools) - set(known)),
                        "changed": sorted(n for n in tools if n in known and tools[n] != known[n]),
                        "removed": sorted(set(known) - set(tools)),
                    }
                    detail = ", ".join(f"{label}: {', '.join(names)}" for label, names in groups.items() if names)
                    notes.append(
                        f"tools changed ({detail}): client notified (list_changed); if they do not appear, "
                        "they will come at the next reconnection (/mcp), the other tools already work"
                    )
                    self.client_tools = tools
                    self.to_client({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        self.reloads += 1
        log.event(
            "reloader-reload",
            {
                "reason": reason,
                "before": before,
                "after": self.child_version,
                "ms": int((time.monotonic() - started) * 1000),
            },
        )
        return "; ".join(notes) + ". Earlier [N] numbers and pages forgotten: look again before acting."

    def _ensure_current(self, *, check_code: bool) -> str:
        """Note de rechargement (« » si rien à faire). Le code n'est comparé qu'avant un appel d'outil."""
        if self.alive() and not self.force_reload and not (check_code and self.fingerprint() != self.child_print):
            return ""
        with self.cond:  # jamais pendant un appel : on attend la fin de ceux en cours, mais pas sans fin
            idle = self.cond.wait_for(lambda: not self.inflight, BUSY_WAIT_S)
        if self.alive() and not self.force_reload and not (check_code and self.fingerprint() != self.child_print):
            return ""
        if not idle:
            # Appel sans réponse depuis BUSY_WAIT_S : serveur figé. Ses appels reçoivent « issue INCONNUE » et le
            # rechargement repart sur un serveur neuf, plutôt que de geler tous les appels suivants.
            log.event("reloader-frozen", {"in_flight": len(self.inflight)})
            self._child_died()
        reason = "stopped" if not self.alive() else ('"out of date" refusal' if self.force_reload else "code changed")
        self.force_reload = False
        return self.reload(reason)

    # --- Messages du client -----------------------------------------------------------------------------------
    def handle(self, message: Message, retries: int = 0) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            self.init_request = message
        elif method == "notifications/initialized":
            self.initialized = message
        elif method == "tools/list":
            self.tools_requests.add(request_id)
        try:
            note = self._ensure_current(check_code=method == "tools/call") if method != "initialize" else ""
        except (StartError, OSError) as error:
            self.stop_child()
            tail = "\n".join(self.stderr_tail) or "(nothing on stderr)"
            log.event("reloader-failure", {"error": str(error)[:300], "stderr": tail[-600:]})
            text = (
                f"nexus-windows could not restart on its new code ({error}). Nothing was done. Code on disk "
                f"broken or installation in progress: retry the call in a moment; if it persists, "
                f"install.ps1. End of the error output:\n{tail[-1500:]}"
            )
            if request_id is not None:
                error_reply = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": text}}
                self.to_client(_tool_error(request_id, text) if method == "tools/call" else error_reply)
            return
        if note and request_id is not None:
            self.notes[request_id] = note
        if request_id is not None:
            with self.cond:  # passage file → en cours atomique, sous le même verrou que _forget
                self.queued.discard(request_id)
                if request_id in self.cancelled:
                    self.cancelled.discard(request_id)
                    self.notes.pop(request_id, None)
                    return  # annulé par le client avant envoi : ni envoyé ni répondu (spécification MCP)
                self.inflight[request_id] = (message, retries)
        try:
            self.to_child(message)
        except OSError:
            if request_id is not None:
                with self.cond:
                    self.inflight.pop(request_id, None)
                self.notes.pop(request_id, None)
                self.force_reload = True
                if retries < RETRIES:
                    self._requeue(message, retries + 1)
                else:
                    self.to_client(_tool_error(request_id, "nexus-windows unreachable: retry the call."))

    def dispatch(self) -> None:
        while (item := self.inbox.get()) is not None:
            try:
                self.handle(*item)
            except Exception as error:  # le superviseur ne tombe jamais : il tient la liaison du client
                log.event("reloader-error", {"error": f"{type(error).__name__}: {error}"[:300]})

    def from_client(self, raw: bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        if "method" not in message or message.get("method") == "notifications/cancelled":
            # Réponse du client à une requête du serveur, ou annulation : tout de suite, même pendant un appel long.
            if message.get("method") == "notifications/cancelled":
                self._forget(message.get("params") or {})
            with contextlib.suppress(OSError):
                self.to_child(message)
            return
        if message.get("id") is not None:
            with self.cond:
                self.queued.add(message["id"])
        self.inbox.put((message, 0))

    def _requeue(self, message: Message, retries: int) -> None:
        with self.cond:
            self.queued.add(message.get("id"))
        self.inbox.put((message, retries))

    def _forget(self, params: Any) -> None:
        """Appel annulé par le client : le serveur n'y répondra jamais (spécification MCP), il ne compte plus comme
        en cours. Sans cela, le rechargement suivant l'attendrait pour toujours et bloquerait tous les appels."""
        request_id = params.get("requestId") if isinstance(params, dict) else None
        if request_id is None:
            return
        with self.cond:
            if self.inflight.pop(request_id, None) is None and request_id in self.queued:
                self.cancelled.add(request_id)  # encore dans la file : handle ne l'enverra pas
            self.cond.notify_all()  # déjà répondue : rien à retenir
        self.notes.pop(request_id, None)
        self.tools_requests.discard(request_id)

    def serve(self, stdin: IO[bytes]) -> None:
        self.spawn()
        dispatcher = threading.Thread(target=self.dispatch, name="nexus-reloader-dispatch", daemon=True)
        dispatcher.start()
        for raw in stdin:
            if raw.strip():
                self.from_client(raw)
        self.inbox.put(None)
        dispatcher.join(STOP_TIMEOUT_S)
        with self.cond:  # client parti : les réponses en route lui sont encore écrites
            self.cond.wait_for(lambda: not self.inflight, STOP_TIMEOUT_S)
        self.stop_child()


def child_command() -> list[str]:
    return [sys.executable, "-m", "nexus_windows", "--child"]


def main() -> None:
    if os.environ.get(ENV) is not None:  # déjà l'enfant d'un superviseur : le serveur lui-même
        from .server import main as serve

        serve()
        return
    Supervisor(child_command(), sys.stdout.buffer).serve(sys.stdin.buffer)
