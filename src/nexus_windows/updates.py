"""Mises à jour faites par le serveur lui-même, hors de tout conteneur (contrat commun des produits Nexus, 26/10) : au
démarrage, en arrière-plan et au plus une fois par jour, dernière version sur PyPI ; état écrit dans
~/.nexus/status/windows-claude.json pour l'appli Nexus.

Le serveur lancé par une IA (`uvx --python 3.14 nexus-windows-claude`, sans version : il démarre aussi hors ligne)
prend la nouvelle version au prochain démarrage de l'IA : ici, `uvx nexus-windows-claude@<version> --version` la
prépare dans le cache de uv. Installé autrement (plugin épinglé du catalogue, extension .mcpb, clone de développement) :
c'est son installeur qui met à jour, l'état le dit. NEXUS_UPDATE_CHECK=off : aucune requête."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from . import __version__

PRODUCT = "windows-claude"
PACKAGE = "nexus-windows-claude"
PYPI = f"https://pypi.org/pypi/{PACKAGE}/json"
DAY = 24 * 3600.0
TIMEOUT = 15.0


def disabled() -> bool:
    return os.environ.get("NEXUS_UPDATE_CHECK", "on").strip().lower() in {"0", "off", "false", "no"}


def status_file() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".nexus" / "status" / f"{PRODUCT}.json"


def memo_file() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".nexus" / PRODUCT / "update.json"


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def write_status(state: str, latest: str | None, message: str) -> None:
    path = status_file()
    data = {
        "product": PRODUCT,
        "version": __version__,
        "latest": latest or __version__,
        "state": state,
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message": message,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)  # l'appli ne lit jamais un fichier à moitié écrit
    except OSError:
        pass  # l'état est une information : jamais une raison d'empêcher le serveur de démarrer


def _memo() -> dict:
    try:
        data = json.loads(memo_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except OSError, ValueError:
        return {}


def latest_on_pypi() -> str:
    with urllib.request.urlopen(PYPI, timeout=TIMEOUT) as response:
        return str(json.loads(response.read(4 * 1024 * 1024))["info"]["version"])


def via_uvx() -> bool:
    """Serveur lancé par uvx (environnement dans le cache de uv) : la mise à jour passe par uvx."""
    prefix = Path(sys.prefix).as_posix().lower()
    return "/archive-v0/" in prefix or "/uv/cache/" in prefix or "/.cache/uv/" in prefix


def prepare(version: str) -> str | None:
    """Pose `version` dans le cache de uv pour le prochain lancement par l'IA ; rend un message d'échec, ou None."""
    uvx = shutil.which("uvx")
    if not uvx:
        return "uvx not found on the PATH"
    try:
        done = subprocess.run(
            [uvx, "--python", "3.14", f"{PACKAGE}@{version}", "--version"],
            capture_output=True, text=True, timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )  # fmt: skip
    except OSError, subprocess.TimeoutExpired:
        return "uvx could not run"
    if done.returncode or version not in done.stdout:
        return f"uvx failed ({(done.stderr or done.stdout).strip()[-200:]})"
    return None


def check(now: float | None = None) -> None:
    """Un passage complet ; chaque échec garde la version en cours et l'écrit dans l'état."""
    now = time.time() if now is None else now
    if disabled():
        write_status("idle", None, f"{PACKAGE} {__version__}; update checks turned off (NEXUS_UPDATE_CHECK=off)")
        return
    memo = _memo()
    if now - float(memo.get("checked", 0)) < DAY and memo.get("latest"):
        latest = str(memo["latest"])
    else:
        write_status("checking", memo.get("latest"), "checking for updates")
        try:
            latest = latest_on_pypi()
        except (OSError, ValueError, KeyError) as error:
            write_status("failed", memo.get("latest"), f"update check failed, {__version__} kept ({error})")
            return
        try:
            memo_file().parent.mkdir(parents=True, exist_ok=True)
            memo_file().write_text(json.dumps({"checked": now, "latest": latest}), encoding="utf-8")
        except OSError:
            pass
    if _version(latest) <= _version(__version__):  # en cours plus récente que PyPI (pas encore servie) : à jour
        write_status("idle", __version__, f"{PACKAGE} {__version__} is up to date")
        return
    if not via_uvx():
        write_status("idle", latest, f"{PACKAGE} {latest} available; update it where it was installed from")
        return
    write_status("updating", latest, f"preparing {PACKAGE} {latest}")
    failure = prepare(latest)
    if failure:
        write_status("failed", latest, f"{PACKAGE} {latest} not prepared, {__version__} kept: {failure}")
    else:
        write_status("done", latest, f"{PACKAGE} {latest} ready; used from the next start of Claude")


def start() -> None:
    """Au démarrage du serveur : en arrière-plan, jamais bloquant, jamais fatal."""

    def run() -> None:
        try:
            check()
        except Exception as error:  # noqa: BLE001 - jamais fatal
            write_status("failed", None, f"update check failed ({error})")

    threading.Thread(target=run, name="nexus-updates", daemon=True).start()
