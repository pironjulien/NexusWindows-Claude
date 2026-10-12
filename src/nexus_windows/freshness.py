"""Serveur dépassé : le code exécuté n'est plus celui du disque.

Chaque conversation de Claude garde son serveur nexus-windows jusqu'à sa fin. Après une mise à jour (install.ps1,
synchronisation OneDrive, modification), un serveur déjà lancé exécuterait encore l'ancien code — garde-fous et arrêt
d'urgence compris — sans que rien ne le signale. L'empreinte des sources du paquet est prise au chargement, puis
comparée à celle du disque au plus toutes les CHECK_EVERY_S secondes. Le numéro de version seul ne suffit pas : une
modification sans changement de numéro passerait inaperçue.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path

from . import __version__

PACKAGE = Path(__file__).resolve().parent
CHECK_EVERY_S = 30.0
STALE = "serveur nexus-windows dépassé : reconnecter (/mcp) ou nouvelle conversation"


def fingerprint(root: Path = PACKAGE) -> str:
    """Empreinte des sources Python du paquet (noms et contenus) ; « » si illisibles."""
    digest = hashlib.sha256()
    try:
        for path in sorted(root.glob("*.py")):
            digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    except OSError:
        return ""
    return digest.hexdigest()


def disk_version(root: Path = PACKAGE) -> str:
    try:
        match = re.search(r'__version__\s*=\s*"([^"]+)"', (root / "__init__.py").read_text(encoding="utf-8"))
    except OSError:
        return "?"
    return match.group(1) if match else "?"


LOADED = fingerprint()  # au chargement du paquet, en même temps que ses modules
_lock = threading.Lock()
_checked: float | None = None  # time.monotonic() de la dernière lecture du disque
_reason = ""


def stale_reason() -> str:
    """Message « serveur dépassé » si le code sur disque diffère de celui chargé (disque relu au plus toutes les
    CHECK_EVERY_S secondes), « » sinon — ou si une empreinte est illisible : rien à affirmer."""
    global _checked, _reason
    with _lock:
        now = time.monotonic()
        if _checked is not None and now - _checked < CHECK_EVERY_S:
            return _reason
        _checked = now
        current = fingerprint()
        if not LOADED or not current or current == LOADED:
            _reason = ""
        else:
            on_disk = disk_version()
            change = f"{__version__} → {on_disk}" if on_disk != __version__ else f"{__version__} modifiée"
            _reason = f"{STALE} (code chargé au démarrage ≠ code sur disque : {change})"
        return _reason
