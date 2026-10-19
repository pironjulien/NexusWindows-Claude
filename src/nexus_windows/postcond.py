"""Postconditions de champ (`check` d'une étape) : l'état voulu d'UN élément, relu avant et après l'action.

Repris du modèle de NexusCodex : une action n'est « vérifiée » que si l'état voulu est relu après elle ; s'il était
déjà vrai avant, l'action n'est pas faite (« déjà fait », aucune entrée) ; une lecture impossible n'est jamais un
succès ni un échec (« inconnu »). Quatre issues pour le modèle : ✓ vérifié, = déjà fait, ~ inconnu, ✗ échec ou non
atteint.

Champs (tous ceux donnés doivent être vrais) : value (valeur exacte, espaces normalisés), contains (sous-chaîne,
sans casse), checked, selected, expanded, focused, enabled, exists. Un état que l'élément ne publie pas reste
inconnu (None), jamais faux.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from typing import Any

from . import snapshot, uia, worker

FIELDS = ("value", "contains", "checked", "selected", "expanded", "focused", "enabled")
TOGGLE_ON, TOGGLE_OFF = 1, 0
EXPANDED, PARTIAL, LEAF = 1, 2, 3


@dataclass
class Verdict:
    state: bool | None  # True : vrai ; False : faux ; None : illisible
    detail: str


def describe(check: dict[str, Any]) -> str:
    target = check.get("target")
    parts = []
    for key in FIELDS:
        if check.get(key) is not None:
            value = check[key]
            parts.append(f"{key}={value!r}" if isinstance(value, str) else f"{key}={'yes' if value else 'no'}")
    if check.get("exists") is not None:
        parts.append("present" if check["exists"] else "absent")
    return f'"{target}" {", ".join(parts) or "present"}'


def _value(props: dict) -> str | None:
    for key in ("value", "legacy_value"):
        if props.get(key) is not None:
            return snapshot.norm(str(props[key]))
    if not props.get("has_value") and props.get("name") is not None:
        return snapshot.norm(str(props["name"]))  # texte statique : son contenu est son nom
    return None


def _checked(props: dict) -> bool | None:
    toggle = props.get("toggle")
    if toggle in (TOGGLE_ON, TOGGLE_OFF):
        return toggle == TOGGLE_ON
    if toggle is None and props.get("selected") is not None:
        return bool(props["selected"])  # bouton radio : coché = choisi
    return None


def _expanded(props: dict) -> bool | None:
    state = props.get("expand")
    if state in (EXPANDED, PARTIAL):
        return True
    if state == 0:
        return False
    return None  # feuille (LEAF) ou motif absent


def _compare(props: dict, check: dict[str, Any]) -> Verdict:
    states: list[bool | None] = []
    seen: list[str] = []
    value = _value(props)
    if check.get("value") is not None:
        states.append(None if value is None else value == snapshot.norm(check["value"]))
        seen.append(f"value {snapshot.clip(value or '?', 60)!r}")
    if check.get("contains") is not None:
        wanted = snapshot.norm(check["contains"]).lower()
        states.append(None if value is None else wanted in value.lower())
        if check.get("value") is None:
            seen.append(f"value {snapshot.clip(value or '?', 60)!r}")
    readers = {
        "checked": _checked,
        "selected": lambda p: None if p.get("selected") is None else bool(p["selected"]),
        "expanded": _expanded,
        "focused": lambda p: None if p.get("focused") is None else bool(p["focused"]),
        "enabled": lambda p: None if p.get("enabled") is None else bool(p["enabled"]),
    }
    for key, read in readers.items():
        if check.get(key) is None:
            continue
        actual = read(props)
        states.append(None if actual is None else actual == bool(check[key]))
        seen.append(f"{key} {'?' if actual is None else ('yes' if actual else 'no')}")
    if check.get("exists") is False:
        states.append(False)  # l'élément est là alors qu'il devait avoir disparu
        seen.append("present")
    if not states:
        states.append(True)  # seulement « présent »
        seen.append("present")
    if any(s is False for s in states):
        return Verdict(False, ", ".join(seen))
    if any(s is None for s in states):
        return Verdict(None, ", ".join(seen) + " (state not exposed by the element)")
    return Verdict(True, ", ".join(seen))


def _find(executor, target: Any) -> tuple[Any | None, str, str]:
    """Élément visé par la postcondition, sans rien faire défiler : (élément ou None si absent, cause si illisible,
    note si le nom ne correspond qu'en partie)."""
    from .actions import ActionError  # import local : actions importe ce module

    if isinstance(target, int) or (isinstance(target, str) and snapshot._ID.match(target)):
        try:
            return executor.resolve(target)[1], "", ""
        except ActionError as error:
            return None, str(error), ""
    view = executor._rebuild()
    try:
        element, note = snapshot.pick(view, str(target))
        return element, "", note
    except snapshot.AmbiguousTarget as error:
        raise ActionError(f"check: {error}") from None
    except snapshot.TargetError:
        pass
    hidden = snapshot.scrolled_away(view)  # présent mais hors de vue (page défilée) : il existe quand même
    try:
        element, note = snapshot.pick(dataclasses.replace(view, elements=hidden), str(target))
        return element, "", note
    except snapshot.TargetError:
        return None, executor._unreadable(view), ""


def evaluate(executor, check: dict[str, Any]) -> Verdict:
    """État actuel de la postcondition (relu en direct). Un nom qui ne correspond qu'en partie (« Newsletter » pour
    « Newsletter partenaires ») ne prouve rien : issue inconnue, jamais vraie ni fausse (une étape « déjà faite » à
    tort serait sautée en silence) ; vrai ou faux exigent le nom exact ou un [N]."""
    element, unreadable, partial = _find(executor, check["target"])
    if element is None:
        if check.get("exists") is False:
            if unreadable:
                return Verdict(None, f"absent from the read, but the read is incomplete ({unreadable})")
            return Verdict(True, "absent")
        if unreadable:
            return Verdict(None, f"not found, incomplete read ({unreadable})")
        return Verdict(False, "not found")
    if partial:
        return Verdict(None, f"{partial}; exact name or [N] required for a postcondition")
    try:
        props = executor._refresh(element.node.element)
    except uia.UIAError as error:
        return Verdict(None, f"could not read back ({error})")
    merged = {**element.props, **(props or {})}
    return _compare(merged, check)


def wait(executor, check: dict[str, Any], timeout_s: float) -> Verdict:
    """Relit jusqu'à ce que la postcondition soit vraie ou jusqu'au délai ; rend le dernier verdict. Ne rejoue
    jamais l'action : seulement des lectures."""
    deadline = time.monotonic() + max(0.1, min(float(timeout_s), 30.0))
    while True:
        worker.check_cancelled()
        verdict = evaluate(executor, check)
        if verdict.state is True or time.monotonic() >= deadline:
            return verdict
        time.sleep(0.1)
