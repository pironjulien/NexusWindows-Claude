"""Anonymisation des relevés du banc avant écriture : profil de l'utilisateur et dossier Documents (OneDrive) retirés.

Commun à tous les scripts de bench/ qui écrivent un relevé dans results/ (les anciens relevés ne sont pas réécrits).
"""

from __future__ import annotations

import re
from pathlib import Path

SEP = r"(?:\\+|/)"  # barre oblique, contre-oblique simple ou échappée une ou plusieurs fois (JSON dans du JSON)


def redact_home(text: str) -> str:
    """Profil de l'utilisateur retiré d'un relevé, sous toutes ses écritures : barres obliques, contre-obliques simples,
    échappées une ou plusieurs fois (réponse JSON d'un outil dans le JSON du relevé : chemins des captures de Codex,
    24/10). Puis « ~/OneDrive/Documents » (disposition des dossiers du poste) devient « <Documents> »."""
    home = Path.home()
    parts = [re.escape(part) for part in home.parts[1:]]  # « Users », nom de session
    drive = re.escape(home.drive)
    pattern = drive + SEP + SEP.join(parts)
    text = re.sub(pattern, "~", text, flags=re.IGNORECASE)
    return re.sub(r"~" + SEP + r"OneDrive[^\\/\"]*" + SEP + r"Documents", "<Documents>", text, flags=re.IGNORECASE)
