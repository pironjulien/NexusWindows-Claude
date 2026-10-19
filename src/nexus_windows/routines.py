"""Mémoire des routines par application : des lots d'étapes vérifiés, rejoués par leur nom.

Repris du modèle de NexusCodex et adapté au lot d'étapes : une routine n'est enregistrée que si TOUTES ses étapes ont
été vérifiées (✓) ou trouvées déjà faites (=) — jamais une issue inconnue (~) ni un échec — et que ses cibles sont
sémantiques (noms visibles). Ni numéro [N] (la numérotation change à chaque look) ni point x,y n'est appris : au
rejeu, chaque cible est retrouvée par son nom dans l'état réel et chaque étape est revérifiée.

L'application est identifiée par son exécutable (nom, chemin, taille, date) : une mise à jour est signalée au rejeu,
qui reste possible puisque chaque étape est revérifiée ; un rejeu raté marque la routine « à revoir ».

Texte variable : `{{nom}}`, valeur donnée au rejeu par params={"nom": "…"}, seulement dans les champs de texte
(`text`, `check.value`, `check.contains`) : jamais dans une cible, une touche ou un chemin de menu (une cible « {{b}} »
remplie en « 120,340 » deviendrait un clic par point). Un texte tapé (`type`, `set`) n'est fait QUE de modèles `{{x}}` :
tout texte fixe y est refusé à l'enregistrement. Au rejeu, un paramètre manquant ou inconnu est refusé, et les étapes
remplies repassent la règle des cibles sémantiques.
Fichiers : ~/.claude/nexus-claude/routines/<exécutable>.json (local au poste, hors OneDrive).

Miroir entre les PC de l'utilisateur : NexusClaude/routines/<exécutable>.json dans le dépôt (synchronisé par OneDrive, suivi
par Git). Il ne garde que la définition des routines (étapes, empreinte, titre, date d'enregistrement), jamais leurs
statistiques de rejeu, et JAMAIS de texte saisi en clair : une routine dont un texte (`text`, `check.value`,
`check.contains`) n'est pas fait que de modèles `{{x}}` reste locale au poste. Fusion par date : pour chaque routine, la
version enregistrée (ou oubliée : pierre tombale datée) le plus récemment l'emporte, dans les deux sens. Le serveur
synchronise au démarrage et réécrit le miroir de l'application à chaque routine enregistrée ou oubliée ;
scripts/sync-routines.ps1 fait de même hors serveur (étape de Sync-NexusPoste).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import foreground, log, snapshot
from . import win32 as w

ROOT = log.ROOT / "routines"
_REPO = Path(__file__).resolve().parents[3]  # NexusClaude (sources lancées depuis le dépôt, installation éditable)


def _default_mirror() -> Path | None:
    """Miroir du dépôt : NEXUS_ROUTINES_MIRROR s'il est posé (vide : aucun) ; sinon NexusClaude/routines, sauf pour un
    serveur de test isolé (NEXUS_WINDOWS_HOME), qui ne doit jamais toucher aux routines de l'utilisateur."""
    explicit = os.environ.get("NEXUS_ROUTINES_MIRROR")
    if explicit is not None:
        return Path(explicit) if explicit.strip() else None
    if os.environ.get("NEXUS_WINDOWS_HOME") or not (_REPO / "nexus-windows").is_dir():
        return None
    return _REPO / "routines"


MIRROR: Path | None = _default_mirror()
SCHEMA = 1
MIRRORED_KEYS = ("steps", "fingerprint", "window", "saved")  # définition seule : statistiques de rejeu locales
NAME = re.compile(r"^[\w][\w .-]{0,59}$")
PARAM = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_LOCK = threading.Lock()


class RoutineError(ValueError):
    pass


def app_of(hwnd: int) -> tuple[str, dict[str, Any]]:
    """(clé de l'application, empreinte de son exécutable) pour la fenêtre."""
    pid = foreground.app_pid(hwnd)
    name = (w.process_name(pid) or "inconnu").lower()
    path = w.process_path(pid)
    fingerprint: dict[str, Any] = {"path": path}
    try:
        info = os.stat(path)
        fingerprint |= {"size": info.st_size, "mtime": int(info.st_mtime)}
    except OSError:
        pass
    return name, fingerprint


def _file(app: str) -> Path:
    return ROOT / f"{re.sub(r'[^\w.-]', '_', app)}.json"


def _load(app: str) -> dict[str, Any]:
    try:
        data = json.loads(_file(app).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {"schema": SCHEMA, "app": app, "routines": {}}
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("routines"), dict):
        return {"schema": SCHEMA, "app": app, "routines": {}}
    return data


def _write(target: Path, data: dict[str, Any]) -> bool:
    """Écriture atomique (un lecteur voit l'ancienne ou la nouvelle version, jamais un morceau), seulement si le
    contenu change (le miroir est synchronisé par OneDrive et suivi par Git : aucune réécriture à l'identique)."""
    text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    try:
        if target.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return True


def _save(app: str, data: dict[str, Any]) -> None:
    _write(_file(app), data)


def names(app: str) -> list[str]:
    return sorted(_load(app)["routines"])


def summary(app: str) -> str:
    """Ligne d'en-tête de look : routines connues pour cette application."""
    routines = _load(app)["routines"]
    if not routines:
        return ""
    parts = []
    for name in sorted(routines):
        entry = routines[name]
        params = sorted(set(PARAM.findall(json.dumps(entry.get("steps", []), ensure_ascii=False))))
        flag = " (à revoir)" if entry.get("last_ok") is False else ""
        parts.append(f"{name}{'(' + ', '.join(params) + ')' if params else ''}{flag}")
    return f"Routines enregistrées pour {app} : {', '.join(parts)} — act(routine=…, params=…)."


TEXT_PARAM_FIELDS = ("value", "contains")  # champs de check où un {{x}} est permis (avec `text` de l'étape)
TYPING = ("type", "set")  # étapes dont `text` est tapé


def _template_only(value: str) -> bool:
    """Valeur faite UNIQUEMENT d'un ou plusieurs modèles {{x}} (et d'espaces) : aucun texte fixe."""
    return bool(PARAM.search(value)) and not PARAM.sub("", value).strip()


def _params_in(value: Any) -> bool:
    return bool(PARAM.search(json.dumps(value, ensure_ascii=False)))


def misplaced(steps: list[dict[str, Any]]) -> str:
    """Premier {{x}} hors des champs de texte (`text`, `check.value`, `check.contains`), ou « » : rempli au rejeu, il
    changerait une cible, une touche ou un menu (« {{b}} » → « 120,340 » : clic par point)."""
    allowed = "permis seulement dans text, check.value, check.contains"
    for index, step in enumerate(steps, 1):
        for key, value in step.items():
            if key == "text":
                continue
            if key == "check" and isinstance(value, dict):
                for sub, inner in value.items():
                    if sub not in TEXT_PARAM_FIELDS and _params_in(inner):
                        return f"étape {index} : {{{{x}}}} dans check.{sub} ({allowed})"
            elif _params_in(value):
                return f"étape {index} : {{{{x}}}} dans {key} ({allowed})"
    return ""


def fixed_text(steps: list[dict[str, Any]]) -> str:
    """Premier texte tapé (type, set) qui contient du texte fixe, ou « » : il serait gardé en clair (et le miroir
    OneDrive/Git en recevrait une copie). Seuls des modèles {{x}} remplis au rejeu sont permis."""
    for index, step in enumerate(steps, 1):
        text = step.get("text")
        if step.get("do") in TYPING and isinstance(text, str) and text.strip() and not _template_only(text):
            return (
                f"étape {index} : texte tapé fixe (seuls des modèles {{{{x}}}} sont permis, valeur donnée au rejeu par "
                "params)"
            )
    return ""


def unreusable(steps: list[dict[str, Any]]) -> str:
    """Pourquoi ce lot ne peut pas devenir une routine (cible non sémantique), ou « »."""
    for index, step in enumerate(steps, 1):
        for key in ("target", "to"):
            value = step.get(key)
            if value is None:
                continue
            if isinstance(value, (int, list)) or snapshot.parse_point(value) is not None:
                return f"étape {index} : cible {value!r} par numéro ou par point (seuls les noms visibles se rejouent)"
            if isinstance(value, str) and snapshot._ID.match(value):
                return f"étape {index} : cible {value!r} par numéro [N] (seuls les noms visibles se rejouent)"
        check = step.get("check") or {}
        target = check.get("target")
        if isinstance(target, int) or (isinstance(target, str) and snapshot._ID.match(target)):
            return f"étape {index} : check par numéro [N] (seuls les noms visibles se rejouent)"
    return ""


def remember(app: str, name: str, steps: list[dict[str, Any]], fingerprint: dict[str, Any], title: str) -> None:
    if not NAME.match(name):
        raise RoutineError(f"Nom de routine « {name} » : lettres, chiffres, espaces, « . », « - », 60 caractères.")
    reason = unreusable(steps) or misplaced(steps) or fixed_text(steps)
    if reason:
        raise RoutineError(reason)
    with _LOCK:
        data = _load(app)
        data["routines"][name] = {
            "steps": steps,
            "fingerprint": fingerprint,
            "window": title,
            "saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "runs": 0,
            "last_ok": True,
        }
        data.setdefault("forgotten", {}).pop(name, None)
        _save(app, data)
    _export_quietly(app)


def forget(app: str, name: str) -> bool:
    with _LOCK:
        data = _load(app)
        if data["routines"].pop(name, None) is None:
            return False
        # Pierre tombale datée : sans elle, le miroir d'un autre PC ferait revenir la routine à la synchronisation.
        data.setdefault("forgotten", {})[name] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save(app, data)
    _export_quietly(app)
    return True


def get(app: str, name: str) -> dict[str, Any]:
    entry = _load(app)["routines"].get(name)
    if entry is None:
        known = names(app)
        raise RoutineError(
            f"Aucune routine « {name} » pour {app}"
            + (f" (connues : {', '.join(known)})." if known else " (aucune enregistrée).")
        )
    return entry


def _fill(value: Any, params: dict[str, str], missing: set[str]) -> Any:
    if isinstance(value, str):

        def sub(match: re.Match) -> str:
            key = match.group(1)
            if key not in params:
                missing.add(key)
                return match.group(0)
            return str(params[key])

        return PARAM.sub(sub, value)
    if isinstance(value, dict):
        return {k: _fill(v, params, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, params, missing) for v in value]
    return value


def fill(steps: list[dict[str, Any]], params: dict[str, str] | None) -> list[dict[str, Any]]:
    """Étapes, modèles {{x}} remplacés par params ; un paramètre manquant (il serait tapé tel quel) ou inconnu (faute
    de frappe : la valeur voulue n'irait nulle part) est refusé, rien n'est exécuté."""
    params = params or {}
    expected = set(PARAM.findall(json.dumps(steps, ensure_ascii=False)))
    unknown = sorted(set(params) - expected)
    if unknown:
        wanted = f"attendus : {', '.join(sorted(expected))}" if expected else "aucun paramètre attendu"
        raise RoutineError(f"Paramètres inconnus : {', '.join(unknown)} ({wanted}).")
    missing: set[str] = set()
    filled = _fill(steps, params, missing)
    if missing:
        raise RoutineError(f"Paramètres manquants : {', '.join(sorted(missing))} (params={{…}}).")
    return filled


def expand(entry: dict[str, Any], params: dict[str, str] | None) -> list[dict[str, Any]]:
    """Étapes de la routine, paramètres remplacés (fill) ; la routine (enregistrée avant ces règles, reprise du miroir
    ou modifiée à la main) est refusée si un {{x}} sort des champs de texte, ou si une étape remplie vise un numéro ou
    un point : rien n'est exécuté."""
    steps = entry.get("steps", [])
    reason = misplaced(steps)
    if reason:
        raise RoutineError(f"Routine refusée : {reason}.")
    filled = fill(steps, params)
    reason = unreusable(filled)
    if reason:
        raise RoutineError(f"Routine refusée après remplissage des paramètres : {reason}.")
    return filled


def changed(entry: dict[str, Any], fingerprint: dict[str, Any]) -> bool:
    saved = entry.get("fingerprint") or {}
    return any(saved.get(k) != fingerprint.get(k) for k in ("size", "mtime") if k in saved or k in fingerprint)


def record_run(app: str, name: str, ok: bool) -> None:
    with _LOCK:
        data = _load(app)
        entry = data["routines"].get(name)
        if entry is None:
            return
        entry["runs"] = int(entry.get("runs", 0)) + 1
        entry["last_ok"] = ok
        entry["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save(app, data)


# --- Miroir entre les PC (NexusClaude/routines) ------------------------------------------------------------------
def plain_text(entry: dict[str, Any]) -> str:
    """Premier texte saisi en clair d'une routine (étape et champ), « » s'il n'y en a pas : un texte saisi ne passe
    dans le miroir que sous forme de modèle `{{x}}` (rempli au rejeu)."""
    for index, step in enumerate(entry.get("steps") or [], 1):
        if not isinstance(step, dict):
            continue
        check = step.get("check")
        if not isinstance(check, dict):
            check = {}
        fields = [
            ("text", step.get("text")),
            ("check.value", check.get("value")),
            ("check.contains", check.get("contains")),
        ]
        for field, value in fields:
            if isinstance(value, str) and value.strip() and not _template_only(value):
                return f"étape {index}, {field}"  # texte fixe, même mêlé à un {{x}} (« IBAN FR76… {{nom}} »)
    return ""


def _definition(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: entry[k] for k in MIRRORED_KEYS if k in entry}


def _merge(local: dict[str, Any], mirror: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Fusion par date d'une application : (fichier local, fichier miroir, changements au poste). Pour chaque nom, le
    plus récent de l'enregistrement et de l'oubli, de part et d'autre, l'emporte ; à égalité, le poste."""
    changes: list[str] = []
    routines_local = dict(local.get("routines") or {})
    forgotten_local = dict(local.get("forgotten") or {})
    routines_mirror = dict(mirror.get("routines") or {})
    forgotten_mirror = dict(mirror.get("forgotten") or {})
    for name in sorted(set(routines_local) | set(forgotten_local) | set(routines_mirror) | set(forgotten_mirror)):
        candidates = []  # (date, priorité à égalité, origine) : le poste l'emporte à égalité
        if name in routines_local:
            candidates.append((str(routines_local[name].get("saved") or ""), 3, "local"))
        if name in forgotten_local:
            candidates.append((str(forgotten_local[name]), 2, "local-oubli"))
        if name in routines_mirror:
            candidates.append((str(routines_mirror[name].get("saved") or ""), 1, "miroir"))
        if name in forgotten_mirror:
            candidates.append((str(forgotten_mirror[name]), 0, "miroir-oubli"))
        stamp, _rank, origin = max(candidates)
        if origin == "local":
            entry = routines_local[name]
            forgotten_local.pop(name, None)
            if not plain_text(entry):  # texte saisi en clair : la routine reste au poste, le miroir garde le sien
                routines_mirror[name] = _definition(entry)
                forgotten_mirror.pop(name, None)
        elif origin == "miroir":
            entry = routines_mirror[name]
            kept = routines_local.get(name) or {}
            same = str(kept.get("saved") or "") == stamp
            routines_local[name] = {
                **_definition(entry),
                "runs": kept.get("runs", 0) if same else 0,
                "last_ok": kept.get("last_ok", True) if same else True,
            }
            forgotten_local.pop(name, None)
            if not same:
                changes.append(f"{name} : version du {stamp} reprise du miroir")
        elif origin == "local-oubli":
            routines_local.pop(name, None)
            routines_mirror.pop(name, None)
            forgotten_mirror[name] = stamp
        else:  # miroir-oubli
            if routines_local.pop(name, None) is not None:
                changes.append(f"{name} : oubliée sur un autre poste le {stamp}")
            forgotten_local[name] = stamp
    app = local.get("app") or mirror.get("app")
    new_local = {"schema": SCHEMA, "app": app, "routines": routines_local, "forgotten": forgotten_local}
    new_mirror = {"schema": SCHEMA, "app": app, "routines": routines_mirror, "forgotten": forgotten_mirror}
    return new_local, new_mirror, changes


def _load_path(path: Path, app: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {"schema": SCHEMA, "app": app, "routines": {}}
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("routines"), dict):
        return {"schema": SCHEMA, "app": app, "routines": {}}
    return data


def sync_app(app: str) -> list[str]:
    """Synchronise une application dans les deux sens ; renvoie les changements faits au poste."""
    if MIRROR is None:
        return []
    mirror_file = MIRROR / _file(app).name
    with _LOCK:
        local, mirror, changes = _merge(_load(app), _load_path(mirror_file, app))
        if local["routines"] or local["forgotten"] or _file(app).exists():
            _save(app, local)
        if mirror["routines"] or mirror["forgotten"] or mirror_file.exists():
            _write(mirror_file, mirror)
    return changes


def sync() -> list[str]:
    """Toutes les applications connues d'un côté ou de l'autre (démarrage du serveur, scripts/sync-routines.ps1)."""
    if MIRROR is None:
        return []
    apps: set[str] = set()
    for folder in (ROOT, MIRROR):
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            app = _load_path(path, path.stem).get("app")
            apps.add(app if isinstance(app, str) and app else path.stem)
    changes: list[str] = []
    for app in sorted(apps):
        changes += [f"{app} : {change}" for change in sync_app(app)]
    return changes


def _export_quietly(app: str) -> None:
    """Après un enregistrement ou un oubli : miroir de l'application à jour. Un miroir en écriture impossible
    (OneDrive occupé) ne fait jamais échouer l'action : la synchronisation suivante rattrapera."""
    try:
        sync_app(app)
    except OSError as error:
        log.event("routines-miroir", {"app": app, "erreur": str(error)[:200]})


def main(argv: list[str] | None = None) -> int:
    """python -m nexus_windows.routines sync : synchronisation hors serveur (scripts/sync-routines.ps1)."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    if args[:1] != ["sync"]:
        print("usage : python -m nexus_windows.routines sync")
        return 2
    if MIRROR is None:
        print("miroir des routines désactivé (NEXUS_ROUTINES_MIRROR vide, ou serveur de test isolé)")
        return 0
    changes = sync()
    print(f"routines synchronisées avec {MIRROR}")
    for change in changes or ["rien de nouveau pour ce poste"]:
        print(f"  {change}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
