"""Empreinte du code de nexus-windows, sans dépendance hors bibliothèque standard.

Partagée par le superviseur (reloader : relance le serveur quand l'empreinte change) et par le serveur lui-même
(freshness : refuse d'agir avec un code dépassé). Couvre les sources Python du paquet et le module de la politique
commune (commun/garde_fous.py) ; ses DONNÉES (garde-fous.json) sont relues à chaque changement, sans redémarrage.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
# Source du paquet (installé en mode éditable) : NexusHarness/NexusWindows/claude/src/nexus_windows, la politique
# vivante est NexusHarness/NexusWindows/commun. Copie publique et paquet PyPI : pas de dossier commun voisin, la même politique est
# embarquée dans le paquet (nexus_windows/shared, copiée par l'export public).
_LIVE_SHARED = Path(os.environ.get("NEXUS_SHARED_DIR") or PACKAGE.parents[2] / "commun")
_BUNDLED_SHARED = PACKAGE / "shared"
SHARED_DIR = _LIVE_SHARED if (_LIVE_SHARED / "garde_fous.py").is_file() else _BUNDLED_SHARED
SHARED_MODULE = SHARED_DIR / "garde_fous.py"


def fingerprint(root: Path = PACKAGE) -> str:
    """Empreinte des sources Python du paquet (noms et contenus) ; « » si illisibles."""
    digest = hashlib.sha256()
    try:
        for path in [*sorted(root.glob("*.py")), SHARED_MODULE]:
            digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    except OSError:
        return ""
    return digest.hexdigest()


def supervisor_fingerprint(root: Path = PACKAGE) -> str:
    """Empreinte du code que le superviseur (reloader) charge une fois pour toutes : il ne se recharge pas lui-même."""
    digest = hashlib.sha256()
    try:
        for name in ("__main__.py", "reloader.py", "codeprint.py", "log.py"):
            digest.update(name.encode("utf-8") + b"\0" + (root / name).read_bytes() + b"\0")
    except OSError:
        return ""
    return digest.hexdigest()


def disk_version(root: Path = PACKAGE) -> str:
    try:
        match = re.search(r'__version__\s*=\s*"([^"]+)"', (root / "__init__.py").read_text(encoding="utf-8"))
    except OSError:
        return "?"
    return match.group(1) if match else "?"
