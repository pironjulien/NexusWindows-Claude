"""Journal JSONL de chaque appel d'outil : arguments, techniques essayées, durées, focus, erreurs.

Fichiers : ~/.claude/nexus-claude/logs/AAAA-MM-JJ.jsonl (14 jours gardés). Une ligne par appel, lisible avec
l'outil `logs` du serveur ou n'importe quel éditeur. NEXUS_WINDOWS_DEBUG_IMAGES=1 enregistre aussi chaque image
envoyée au modèle (logs/images/), utile pour rejouer un échec.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from . import indicator, journal

ROOT = Path(os.environ.get("NEXUS_WINDOWS_HOME", Path.home() / ".claude" / "nexus-claude"))
LOG_DIR = ROOT / "logs"
IMAGE_DIR = LOG_DIR / "images"
KEEP_DAYS = 14
_LOCK = threading.Lock()
_COUNTER = itertools.count(1)
_SESSION = dt.datetime.now().strftime("%H%M%S")
DEBUG_IMAGES = os.environ.get("NEXUS_WINDOWS_DEBUG_IMAGES") == "1"


def _today_file() -> Path:
    return LOG_DIR / f"{dt.date.today().isoformat()}.jsonl"


def prune() -> None:
    if not LOG_DIR.exists():
        return
    limit = time.time() - KEEP_DAYS * 86400
    for path in list(LOG_DIR.glob("*.jsonl")) + (list(IMAGE_DIR.glob("*.png")) if IMAGE_DIR.exists() else []):
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
        except OSError:
            pass


def write(record: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _LOCK, _today_file().open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def event(name: str, data: dict[str, Any]) -> None:
    """Événement hors appel d'outil (arrêt d'urgence…)."""
    try:
        write(
            {
                "ts": dt.datetime.now().isoformat(timespec="milliseconds"),
                "tool": name,
                "ms": 0,
                "ok": True,
                "args": data,
            }
        )
    except OSError:
        pass


def _short(value: Any, limit: int = 300) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…(+{len(value) - limit})"
    if isinstance(value, list):
        return [_short(v, limit) for v in value[:50]]
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in value.items()}
    return value


class Call:
    """Contexte d'un appel d'outil : `with Call("act", args) as call: call.step(...)`."""

    def __init__(self, tool: str, args: dict[str, Any]) -> None:
        self.id = f"{_SESSION}-{next(_COUNTER):04d}"
        self.tool = tool
        self.args = _short(args)
        self.steps: list[dict[str, Any]] = []
        self.notes: dict[str, Any] = {}
        self.start = time.perf_counter()
        self.error: str | None = None
        self._busy = False
        self.journal: dict[str, Any] | None = None  # app et cadre de la fenêtre : étapes aussi au journal commun

    def step(self, **fields: Any) -> None:
        self.steps.append(_short(fields))
        if self.journal is not None:
            try:
                from . import journal

                journal.etape(self.id, self.journal, fields)
            except Exception:  # noqa: BLE001 - journal commun : jamais bloquant
                pass

    def note(self, **fields: Any) -> None:
        self.notes.update(_short(fields))

    def image(self, png: bytes, label: str = "") -> None:
        if not DEBUG_IMAGES:
            return
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        (IMAGE_DIR / f"{self.id}{'-' + label if label else ''}.png").write_bytes(png)

    def __enter__(self) -> Call:
        self._busy = indicator.begin(self.tool)  # logo « agent au travail » pendant les actions
        return self

    def __exit__(self, exc_type, exc, _tb) -> None:
        if self._busy:
            self._busy = False
            indicator.end()
        if exc is not None and self.error is None:
            # Type et premiers mots seulement, pile sans message : un refus (accord, confirmation) cite le texte saisi
            # et le contenu des champs, jamais écrits sur disque (relecture de Codex du 23/10).
            self.error = f"{exc_type.__name__}: {journal.erreur_propre(exc) or ''}".rstrip(": ")
            self.notes["trace"] = ("".join(traceback.format_tb(_tb, limit=8)) + exc_type.__name__)[-3000:]
        try:
            write(
                {
                    "ts": dt.datetime.now().isoformat(timespec="milliseconds"),
                    "id": self.id,
                    "tool": self.tool,
                    "ms": round((time.perf_counter() - self.start) * 1000),
                    "ok": self.error is None,
                    "args": self.args,
                    "steps": self.steps,
                    **({"notes": self.notes} if self.notes else {}),
                    **({"error": self.error} if self.error else {}),
                }
            )
        except OSError:
            pass


def tail(count: int = 20, errors_only: bool = False) -> list[dict[str, Any]]:
    files = sorted(LOG_DIR.glob("*.jsonl")) if LOG_DIR.exists() else []
    out: list[dict[str, Any]] = []
    for path in reversed(files):
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if errors_only and record.get("ok", True):
                continue
            out.append(record)
            if len(out) >= count:
                return list(reversed(out))
    return list(reversed(out))
