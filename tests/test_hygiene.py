"""Hygiène du dépôt : aucun caractère de contrôle parasite (ESC, BEL…) dans les sources et la documentation.

Origine : des chaînes PowerShell entre guillemets doubles avaient transformé des séquences d'échappement en
caractères de contrôle dans CHANGELOG.md et SKILL.md (texte chargé tel quel dans le contexte du modèle).
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {".py", ".md", ".ps1", ".json", ".html", ".js", ".toml", ".txt"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache"}
ALLOWED = {"\t", "\n", "\r"}


def _files():
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUFFIXES and not SKIP_DIRS.intersection(path.parts):
            yield path


def test_no_control_characters():
    bad = []
    for path in _files():
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            hits = {c for c in line if c not in ALLOWED and unicodedata.category(c) == "Cc"}
            if hits:
                bad.append(f"{path.relative_to(ROOT)}:{number} {sorted(hex(ord(c)) for c in hits)}")
    assert not bad, "\n".join(bad[:20])


def test_no_replacement_characters():
    replacement = chr(0xFFFD)  # écrit par son code : le caractère lui-même ferait échouer ce test sur ce fichier
    bad = [
        str(p.relative_to(ROOT)) for p in _files() if replacement in p.read_text(encoding="utf-8-sig", errors="replace")
    ]
    assert not bad, f"fichiers non UTF-8 ou corrompus : {bad}"
