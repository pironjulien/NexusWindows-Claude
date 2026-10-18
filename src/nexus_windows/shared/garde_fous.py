#!/usr/bin/env python3
"""
Politique unique des garde-fous de bureau, commune à Claude (nexus-windows) et Antigravity (NexusAGY).

Les données vivent dans `garde-fous.json` (même dossier) ; ce module les lit et rend les décisions pures, sans aucun
appel Windows (testable hors ligne). Accord Claude–Antigravity du 18/10/2026 : blocage dur seulement contre
l'auto-élévation et la fuite de secrets ; confirmation étanche seulement pour l'irréversible qui touche des tiers, de
l'argent ou détruit ; tout le reste est libre.

Fichier absent ou illisible : GardeFousError (échec fermé). L'outil qui l'importe refuse alors d'agir plutôt que
d'agir sans politique. Bibliothèque standard seulement.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any

POLICY_FILE = Path(__file__).resolve().with_name("garde-fous.json")
AGENTS = ("claude", "antigravity")

_lock = threading.Lock()
_cache: tuple[tuple[str, int, int], dict[str, Any]] | None = None


class GardeFousError(RuntimeError):
    """Politique commune absente ou invalide : l'outil ne doit pas agir."""


def load(path: Path | None = None) -> dict[str, Any]:
    """Politique lue (relue seulement si le fichier a changé). GardeFousError si absente ou invalide."""
    global _cache
    target = Path(path) if path is not None else POLICY_FILE
    try:
        stat = target.stat()
        key = (str(target), stat.st_mtime_ns, stat.st_size)
        with _lock:
            if _cache is not None and _cache[0] == key:
                return _cache[1]
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GardeFousError(f"politique des garde-fous illisible ({target}) : {error}") from error
    _validate(data, target)
    with _lock:
        _cache = (key, data)
    return data


def _validate(data: Any, target: Path) -> None:
    try:
        assert isinstance(data, dict)
        blocks = data["blocage_dur"]
        for agent in AGENTS:
            for category in data["agents"][agent]:
                assert category in blocks, category
        confirmation = data["confirmation"]
        for pattern in confirmation["motifs"]:
            re.compile(pattern)
        for context in confirmation["contextes"]:
            re.compile(context["titre"])
            re.compile(context["verbes"])
        assert isinstance(confirmation["roles_non_actifs"], list)
        assert isinstance(confirmation["roles_declencheurs"], list)
    except (AssertionError, KeyError, TypeError, re.error) as error:
        raise GardeFousError(f"politique des garde-fous invalide ({target}) : {error!r}") from error


def plain(text: Any) -> str:
    """Texte comparable : sans accents ni casse, apostrophes et ponctuation ramenées à des espaces."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
    return " ".join(re.sub(r"[^0-9a-z]+", " ", bare).split())


# --- Blocage dur -----------------------------------------------------------------------------------------------
def _categories(agent: str) -> list[tuple[str, dict[str, Any]]]:
    data = load()
    if agent not in AGENTS:
        raise GardeFousError(f"agent inconnu de la politique : {agent!r} (attendu : {', '.join(AGENTS)})")
    blocks = data["blocage_dur"]
    return [(name, blocks[name]) for name in data["agents"][agent]]


def _names(value: Any) -> set[str]:
    return {str(item).lower() for item in value or []}


def password_manager_prefixes() -> tuple[str, ...]:
    block = load()["blocage_dur"]["gestionnaires_mots_de_passe"]
    return tuple(str(p).lower() for p in block["prefixes_processus"])


def is_password_manager(process: Any) -> bool:
    return str(process or "").strip().lower().startswith(password_manager_prefixes())


def hard_block_reason(agent: str, process: Any, cls: Any = "") -> str:
    """Raison du blocage dur de cette fenêtre pour cet agent (processus et classe de la fenêtre), « » si libre."""
    proc = str(process or "").strip().lower()
    klass = str(cls or "")
    for _name, block in _categories(agent):
        if proc and proc in _names(block.get("processus")):
            return str(block["raison"])
        if klass and klass in {str(c) for c in block.get("classes") or []}:
            return str(block["raison"])
        prefixes = tuple(str(p).lower() for p in block.get("prefixes_processus") or [])
        if proc and prefixes and proc.startswith(prefixes):
            return f"{block['raison']} ({proc})"
    return ""


def hard_block_processes(agent: str) -> set[str]:
    """Noms exacts de processus bloqués pour cet agent (hors préfixes des gestionnaires de mots de passe)."""
    out: set[str] = set()
    for _name, block in _categories(agent):
        out |= _names(block.get("processus"))
    return out


def hard_block_classes(agent: str) -> set[str]:
    out: set[str] = set()
    for _name, block in _categories(agent):
        out |= {str(c) for c in block.get("classes") or []}
    return out


# --- Confirmation ----------------------------------------------------------------------------------------------
def confirm_patterns() -> tuple[str, ...]:
    return tuple(load()["confirmation"]["motifs"])


def confirm_contexts() -> tuple[tuple[str, str, str], ...]:
    return tuple((c["titre"], c["verbes"], c["quoi"]) for c in load()["confirmation"]["contextes"])


def non_action_roles() -> set[str]:
    return set(load()["confirmation"]["roles_non_actifs"])


def trigger_roles() -> tuple[str, ...]:
    return tuple(load()["confirmation"]["roles_declencheurs"])


def irreversible_word(label: Any, patterns: tuple[str, ...] | None = None) -> str:
    """Mot d'action à confirmer trouvé dans le libellé (accents, casse et apostrophes ignorés), « » sinon."""
    patterns = confirm_patterns() if patterns is None else patterns
    if not patterns:
        return ""
    found = re.search(r"\b(" + "|".join(patterns) + r")\b", plain(label))
    return found.group(0) if found else ""


def confirm_word(
    label: Any,
    title: Any = "",
    patterns: tuple[str, ...] | None = None,
    contexts: tuple[tuple[str, str, str], ...] | None = None,
) -> str:
    """Mot à confirmer pour ce libellé : un motif, ou un verbe générique (valider, OK…) dans une fenêtre dont le TITRE
    parle de paiement ou de formatage ; « » sinon."""
    patterns = confirm_patterns() if patterns is None else patterns
    word = irreversible_word(label, patterns)
    if word or not title or not patterns:
        return word
    contexts = confirm_contexts() if contexts is None else contexts
    text, heading = plain(label), plain(title)
    for context, verbs, what in contexts:
        found = re.search(r"\b(" + verbs + r")\b", text)
        if found and re.search(r"\b(" + context + r")\b", heading):
            return f"{found.group(0)} (fenêtre de {what})"
    return ""


def refusal(what: str, label: Any, word: str) -> str:
    """Message de refus commun : rien n'a été fait, ordre explicite de Julien → confirm=true."""
    return (
        f"{what} sur « {str(label).strip()[:60]} » : irréversible ou chez un tiers (« {word} »), rien n'a été fait. "
        "Ordre explicite de Julien pour CE geste → renvoyer l'étape avec confirm=true ; initiative de l'agent → lui "
        "demander d'abord."
    )
