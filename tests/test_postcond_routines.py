"""Résultat en quatre états (✓ = ~ ✗), postconditions de champ (check) et mémoire des routines."""

from __future__ import annotations

import json

import pytest
from test_contract import HWND, FakeAuto, executor, make_view, node

from nexus_windows import actions, postcond, routines, server


class Call:
    def step(self, **_):
        pass


def run(ex, *steps):
    symbols: list[str] = []
    report, failed = server._run_steps(ex, list(steps), HWND, Call(), symbols)
    return report, failed, symbols


# --- Postconditions de champ ------------------------------------------------------------------------------------
def test_check_verified_after_set():
    field = node("Edit", "Nom", has_value=True, value="")
    ex = executor(make_view(field), FakeAuto())
    report, failed, symbols = run(
        ex, server.Step(do="set", target="Nom", text="Julien", check={"target": "Nom", "value": "Julien"})
    )
    assert not failed and symbols == ["✓"] and "vérifié" in report[0]


def test_check_already_true_sends_nothing():
    field = node("Edit", "Nom", has_value=True, value="Julien")
    auto = FakeAuto()
    report, failed, symbols = run(
        executor(make_view(field), auto),
        server.Step(do="set", target="Nom", text="Julien", check={"target": "Nom", "value": "Julien"}),
    )
    assert not failed and symbols == ["="] and auto.calls == [] and "déjà vrai" in report[0]


def test_check_not_met_stops_the_batch():
    button = node("Button", "Lancer", has_invoke=True)
    status = node("Text", "En attente")
    ex = executor(make_view(button, status), FakeAuto())
    report, failed, symbols = run(
        ex,
        server.Step(do="click", target="Lancer", timeout=0.2, check={"target": "Text:En attente", "value": "Fini"}),
        server.Step(do="click", target="Lancer"),
    )
    assert failed and symbols == ["✗"] and "NON ATTEINT" in report[0] and "non exécutées" in report[-1]


def test_check_unreadable_state_is_unknown_not_success():
    button = node("Button", "Lancer", has_invoke=True)
    label = node("Text", "Statut")  # aucun motif Toggle : « coché » n'est pas publié
    ex = executor(make_view(button, label), FakeAuto())
    report, failed, symbols = run(
        ex, server.Step(do="click", target="Lancer", timeout=0.2, check={"target": "Text:Statut", "checked": True})
    )
    assert not failed and symbols == ["~"] and "issue inconnue" in report[0]


def test_check_exists_false_waits_for_disappearance():
    button = node("Button", "Fermer", has_invoke=True)
    dialog = node("Text", "Confirmer la suppression")
    state = {"closed": False}

    class Closing(FakeAuto):
        def invoke(self, element):
            super().invoke(element)
            state["closed"] = True

    auto = Closing()
    open_view, closed_view = make_view(button, dialog), make_view(button)
    open_view.node_count = closed_view.node_count = 12  # lecture complète : une absence y est probante
    ex = actions.Executor(auto, HWND, lambda rebuild: closed_view if state["closed"] else open_view, lambda: None)
    ex._pids = {7}
    report, failed, symbols = run(
        ex,
        server.Step(do="click", target="Fermer", check={"target": "Confirmer la suppression", "exists": False}),
    )
    assert not failed and symbols == ["✓"] and "absent" in report[0]


def test_absence_in_an_empty_reading_is_unknown():
    button = node("Button", "Fermer", has_invoke=True)
    view = make_view(button)  # node_count 0 : lecture vide, rien n'y est prouvé absent
    verdict = postcond.evaluate(executor(view, FakeAuto()), {"target": "Confirmer", "exists": False})
    assert verdict.state is None and "lecture incomplète" in verdict.detail


def test_compare_fields():
    props = {"value": " Julien  Piron ", "toggle": 1, "expand": 0, "enabled": True, "has_value": True}
    assert postcond._compare(props, {"target": "x", "value": "Julien Piron"}).state is True
    assert postcond._compare(props, {"target": "x", "contains": "piron"}).state is True
    assert postcond._compare(props, {"target": "x", "checked": True, "expanded": False}).state is True
    assert postcond._compare(props, {"target": "x", "checked": False}).state is False
    assert postcond._compare(props, {"target": "x", "focused": True}).state is None  # non publié : inconnu
    assert postcond._compare({"selected": True}, {"target": "x", "checked": True}).state is True  # radio


def test_tally():
    assert server._tally(["✓", "✓", "=", "~"]) == "Bilan : 2 vérifiées, 1 déjà faites, 1 issue inconnue."
    assert server._tally([]) == ""


# --- Routines -----------------------------------------------------------------------------------------------------
FP = {"path": "C:/app/test.exe", "size": 10, "mtime": 1}


@pytest.mark.parametrize("target", [3, "#3", "[3]", "10,20", [10, 20]])
def test_unreusable_targets(target):
    assert routines.unreusable([{"do": "click", "target": target}])


def test_semantic_targets_are_reusable():
    assert (
        routines.unreusable([{"do": "set", "target": "Nom", "text": "{{nom}}"}, {"do": "click", "target": "OK"}]) == ""
    )


def test_remember_get_expand_and_forget():
    steps = [{"do": "set", "target": "Nom", "text": "{{nom}}"}, {"do": "click", "target": "Valider"}]
    routines.remember("test.exe", "saisir nom", steps, FP, "Fenêtre")
    assert routines.names("test.exe") == ["saisir nom"]
    assert "saisir nom(nom)" in routines.summary("test.exe")
    entry = routines.get("test.exe", "saisir nom")
    assert routines.expand(entry, {"nom": "Julien"})[0]["text"] == "Julien"
    with pytest.raises(routines.RoutineError, match="nom"):
        routines.expand(entry, {})
    assert not routines.changed(entry, FP) and routines.changed(entry, FP | {"mtime": 2})
    routines.record_run("test.exe", "saisir nom", False)
    assert "(à revoir)" in routines.summary("test.exe")
    assert routines.forget("test.exe", "saisir nom") and routines.names("test.exe") == []
    with pytest.raises(routines.RoutineError, match="aucune enregistrée"):
        routines.get("test.exe", "saisir nom")


def test_bad_name_refused():
    with pytest.raises(routines.RoutineError):
        routines.remember("test.exe", "../évasion", [{"do": "click", "target": "OK"}], FP, "")


def test_server_remember_only_fully_proven_batches():
    steps = [server.Step(do="click", target="OK", expect="Fait")]
    refused = server._remember("test.exe", FP, "T", "r", steps, ["~"], False)
    assert "NON enregistrée" in refused and routines.names("test.exe") == []
    assert "NON enregistrée" in server._remember("test.exe", FP, "T", "r", steps, ["✗"], True)
    by_number = [server.Step(do="click", target="#2")]
    assert "numéro" in server._remember("test.exe", FP, "T", "r", by_number, ["✓"], False)
    assert "enregistrée pour test.exe" in server._remember("test.exe", FP, "T", "r", steps, ["✓"], False)
    saved = json.loads((routines.ROOT / "test.exe.json").read_text(encoding="utf-8"))
    assert saved["routines"]["r"]["steps"] == [{"do": "click", "target": "OK", "expect": "Fait"}]
    assert "oubliée" in server._remember("test.exe", FP, "T", "r", [], [], False)


def test_server_routine_steps_and_notes():
    routines.remember("test.exe", "r", [{"do": "set", "target": "Nom", "text": "{{x}}"}], FP, "T")
    notes: list[str] = []
    steps = server._routine_steps("test.exe", FP | {"size": 99}, "r", {"x": "abc"}, notes)
    assert steps[0].text == "abc" and steps[0].do == "set"
    assert any("mise à jour" in n for n in notes)


def test_template_steps_run_with_params_and_refuse_missing_values():
    template = [server.Step(do="set", target="Nom", text="{{nom}}", check={"target": "Nom", "value": "{{nom}}"})]
    filled = server._filled(template, {"nom": "Julien"})
    assert filled[0].text == "Julien" and filled[0].check.value == "Julien"
    assert template[0].text == "{{nom}}"  # le modèle reste intact pour l'enregistrement
    with pytest.raises(actions.ActionError, match="nom"):
        server._filled(template, None)  # jamais « {{nom}} » tapé tel quel
    plain = [server.Step(do="click", target="OK")]
    assert server._filled(plain, None) is plain
