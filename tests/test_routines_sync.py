"""Miroir des routines entre les PC de Julien (NexusWindows/claude/routines) : fusion par date, oublis, jamais de texte en
clair. Le miroir et le dossier local sont ceux de conftest (dossiers du test, jamais ceux du poste)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_windows import routines

STEPS = [{"do": "set", "target": "Nom", "text": "{{nom}}"}, {"do": "click", "target": "Enregistrer"}]


def _mirror() -> Path:
    """Miroir des routines, toujours posé par conftest (dossier du test) sauf dans le test qui le désactive."""
    assert routines.MIRROR is not None
    return routines.MIRROR


def _write(folder, app, data):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{app}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _routine(saved, steps=STEPS, **extra):
    return {"steps": steps, "fingerprint": {"size": 1}, "window": "Fiche", "saved": saved, **extra}


def test_remember_writes_the_mirror_without_run_statistics():
    routines.remember("app.exe", "fiche", STEPS, {"size": 1}, "Fiche")
    routines.record_run("app.exe", "fiche", False)
    mirror = json.loads((_mirror() / "app.exe.json").read_text(encoding="utf-8"))
    assert mirror["routines"]["fiche"]["steps"] == STEPS
    assert "runs" not in mirror["routines"]["fiche"] and "last_ok" not in mirror["routines"]["fiche"]


def test_plain_typed_text_never_reaches_the_mirror():
    with pytest.raises(routines.RoutineError, match="fixed typed text"):  # jamais gardé, même au poste
        routines.remember("app.exe", "secret", [{"do": "set", "target": "Nom", "text": "Alice Martin"}], {}, "Fiche")
    routines.remember(
        "app.exe", "verif", [{"do": "click", "target": "OK", "check": {"target": "Nom", "value": "x"}}], {}, "F"
    )
    assert routines.names("app.exe") == ["verif"]  # gardée au poste
    mirror = (_mirror() / "app.exe.json").read_text(encoding="utf-8") if _mirror().exists() else ""
    assert "Alice Martin" not in mirror and '"secret"' not in mirror and '"verif"' not in mirror
    assert routines.plain_text({"steps": [{"do": "set", "text": "{{prenom}} {{nom}}"}]}) == ""


def test_newer_mirror_version_wins_and_older_is_kept_out():
    _write(
        routines.ROOT,
        "app.exe",
        {"schema": 1, "app": "app.exe", "routines": {"fiche": _routine("2026-10-01T10:00:00", runs=5)}},
    )
    newer = [{"do": "click", "target": "Valider"}]
    _write(
        _mirror(),
        "app.exe",
        {"schema": 1, "app": "app.exe", "routines": {"fiche": _routine("2026-10-02T10:00:00", newer)}},
    )
    changes = routines.sync()
    assert changes and routines.get("app.exe", "fiche")["steps"] == newer
    assert routines.get("app.exe", "fiche")["runs"] == 0  # statistiques d'une autre version : remises à zéro
    # Plus ancienne au miroir : le poste garde la sienne et la publie.
    _write(
        _mirror(),
        "app.exe",
        {"schema": 1, "app": "app.exe", "routines": {"fiche": _routine("2026-09-01T10:00:00")}},
    )
    routines.sync()
    mirror = json.loads((_mirror() / "app.exe.json").read_text(encoding="utf-8"))
    assert mirror["routines"]["fiche"]["steps"] == newer


def test_forgetting_propagates_and_is_not_undone_by_an_older_copy():
    routines.remember("app.exe", "fiche", STEPS, {}, "Fiche")
    assert routines.forget("app.exe", "fiche")
    mirror = json.loads((_mirror() / "app.exe.json").read_text(encoding="utf-8"))
    assert "fiche" not in mirror["routines"] and "fiche" in mirror["forgotten"]
    # Un autre poste avait une copie plus ancienne : elle ne revient pas.
    _write(
        routines.ROOT,
        "app.exe",
        {"schema": 1, "app": "app.exe", "routines": {"fiche": _routine("2026-01-01T00:00:00")}},
    )
    routines.sync()
    assert routines.names("app.exe") == []


def test_sync_imports_routines_of_another_pc_and_never_rewrites_identically():
    _write(
        _mirror(),
        "autre.exe",
        {"schema": 1, "app": "autre.exe", "routines": {"r": _routine("2026-10-03T08:00:00")}},
    )
    assert routines.sync() == ["autre.exe: r: version of 2026-10-03T08:00:00 taken from the mirror"]
    path = _mirror() / "autre.exe.json"
    before = path.stat().st_mtime_ns
    assert routines.sync() == []
    assert path.stat().st_mtime_ns == before and routines.names("autre.exe") == ["r"]


def test_mirror_disabled_does_nothing(monkeypatch):
    monkeypatch.setattr(routines, "MIRROR", None)
    routines.remember("app.exe", "fiche", STEPS, {}, "Fiche")
    assert routines.sync() == [] and routines.main(["sync"]) == 0


def test_test_servers_never_use_julien_mirror(monkeypatch):
    monkeypatch.delenv("NEXUS_ROUTINES_MIRROR", raising=False)
    monkeypatch.setenv("NEXUS_WINDOWS_HOME", "C:/tmp/serveur-de-test")
    assert routines._default_mirror() is None
    monkeypatch.setenv("NEXUS_ROUTINES_MIRROR", "")
    assert routines._default_mirror() is None
