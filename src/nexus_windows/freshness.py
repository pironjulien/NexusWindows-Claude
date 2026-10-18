"""Serveur dépassé : le code exécuté n'est plus celui du disque.

Chaque conversation de Claude garde son serveur nexus-windows jusqu'à sa fin. Après une mise à jour (install.ps1,
synchronisation OneDrive, modification), un serveur déjà lancé exécuterait encore l'ancien code — garde-fous et arrêt
d'urgence compris — sans que rien ne le signale. L'empreinte des sources du paquet est prise au chargement, puis
comparée à celle du disque au plus toutes les CHECK_EVERY_S secondes. Le numéro de version seul ne suffit pas : une
modification sans changement de numéro passerait inaperçue.
"""

from __future__ import annotations

import os
import threading
import time

from . import __version__
from .codeprint import PACKAGE, SHARED_MODULE, disk_version, fingerprint, supervisor_fingerprint

__all__ = ["PACKAGE", "SHARED_MODULE", "disk_version", "fingerprint", "stale_reason"]
CHECK_EVERY_S = 30.0
STALE = "serveur nexus-windows dépassé : reconnecter (/mcp) ou nouvelle conversation"
# Lancé par le superviseur (reloader) : il relance le serveur sur le nouveau code avant l'appel suivant ; ce refus ne
# sert plus qu'au cas rare d'un code changé entre sa vérification et celle du serveur (l'appel est alors rejoué).
SUPERVISED = os.environ.get("NEXUS_WINDOWS_SUPERVISOR") is not None


def supervisor_status() -> str:
    """Ligne de l'outil logs : superviseur présent ou non, et à jour (il ne se recharge pas lui-même)."""
    if not SUPERVISED:
        return "absent (serveur lancé sans superviseur : un changement de code demandera /mcp)"
    pid = os.environ.get("NEXUS_WINDOWS_SUPERVISOR", "?")
    loaded = os.environ.get("NEXUS_WINDOWS_SUPERVISOR_CODE", "")
    current = supervisor_fingerprint()
    if loaded and current and loaded != current:
        return f"actif (superviseur pid {pid}, son propre code a changé : appliqué à la prochaine reconnexion, sans urgence)"
    return f"actif (superviseur pid {pid}) : le serveur reprend tout seul son nouveau code avant l'appel suivant"


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
