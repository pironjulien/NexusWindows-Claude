"""Résultat en quatre états (✓ = ~ ✗), postconditions de champ (check) et mémoire des routines."""

from __future__ import annotations

import json

import pytest
from test_contract import HWND, FakeAuto, as_auto, as_call, executor, make_view, node

from nexus_windows import actions, postcond, routines, server


def run(ex, *steps):
    symbols: list[str] = []
    report, failed = server._run_steps(ex, list(steps), HWND, as_call(), symbols)
    return report, failed, symbols


# --- Postconditions de champ ------------------------------------------------------------------------------------
def test_check_verified_after_set():
    field = node("Edit", "Nom", has_value=True, value="")
    ex = executor(make_view(field), FakeAuto())
    report, failed, symbols = run(
        ex, server.Step(do="set", target="Nom", text="Julien", check=server.Check(target="Nom", value="Julien"))
    )
    assert not failed and symbols == ["✓"] and "verified" in report[0]


def test_check_already_true_sends_nothing():
    field = node("Edit", "Nom", has_value=True, value="Julien")
    auto = FakeAuto()
    report, failed, symbols = run(
        executor(make_view(field), auto),
        server.Step(do="set", target="Nom", text="Julien", check=server.Check(target="Nom", value="Julien")),
    )
    assert not failed and symbols == ["="] and auto.calls == [] and "already true" in report[0]


def test_check_not_met_stops_the_batch():
    button = node("Button", "Lancer", has_invoke=True)
    status = node("Text", "En attente")
    ex = executor(make_view(button, status), FakeAuto())
    report, failed, symbols = run(
        ex,
        server.Step(
            do="click", target="Lancer", timeout=0.2, check=server.Check(target="Text:En attente", value="Fini")
        ),
        server.Step(do="click", target="Lancer"),
    )
    assert failed and symbols == ["✗"] and "NOT REACHED" in report[0] and "not run" in report[-1]


def test_check_unreadable_state_is_unknown_not_success():
    button = node("Button", "Lancer", has_invoke=True)
    label = node("Text", "Statut")  # aucun motif Toggle : « coché » n'est pas publié
    ex = executor(make_view(button, label), FakeAuto())
    report, failed, symbols = run(
        ex,
        server.Step(do="click", target="Lancer", timeout=0.2, check=server.Check(target="Text:Statut", checked=True)),
    )
    assert not failed and symbols == ["~"] and "unknown outcome" in report[0]


@pytest.mark.parametrize("single_step", [False, True])
def test_unknown_action_stops_before_the_next_action_without_replay(single_step):
    first = node("Button", "Lancer", has_invoke=True)
    next_button = node("Button", "Continuer", has_invoke=True)
    auto = FakeAuto()
    steps = [server.Step(do="click", target="Lancer")]
    if not single_step:
        steps.append(server.Step(do="click", target="Continuer"))
    report, failed, symbols = run(executor(make_view(first, next_button), auto), *steps)
    assert not failed and symbols == ["~"]  # transmis, pas un échec certain
    assert auto.calls == [("invoke", first.element)]  # ni second bouton, ni répétition du premier
    assert any("unknown outcome" in line for line in report)
    assert not any("✗" in line for line in report)
    if not single_step:
        assert "steps 2 to 2 not run" in report[-1]
    assert "NOT saved" in server._remember("test.exe", FP, "T", "r", steps, symbols, failed)
    assert routines.names("test.exe") == []


def test_unconfirmed_exception_stays_unknown_and_stops_without_replay():
    first = node("Button", "Lancer", has_invoke=True)
    next_button = node("Button", "Continuer", has_invoke=True)

    class UnreadableAfterInvoke(FakeAuto):
        def invoke(self, element):
            super().invoke(element)
            raise actions.NoVisibleEffect("sent once, no readable effect")

    auto = UnreadableAfterInvoke()
    steps = [server.Step(do="click", target="Lancer"), server.Step(do="click", target="Continuer")]
    report, failed, symbols = run(executor(make_view(first, next_button), auto), *steps)
    assert not failed and symbols == ["~"]  # l'exception Unconfirmed du moteur n'est pas un échec certain
    assert auto.calls == [("invoke", first.element)]
    assert "no readable effect" in report[0] and "not run" in report[-1]
    assert "NOT saved" in server._remember("test.exe", FP, "T", "r", steps, symbols, failed)
    assert routines.names("test.exe") == []


@pytest.mark.parametrize("with_expect", [False, True])
def test_unreadable_requested_check_stops_even_after_a_verified_action(with_expect):
    field = node("Edit", "Nom", has_value=True, value="")
    label = node("Text", "Statut")  # la propriété « checked » n'est pas publiée
    auto = FakeAuto()
    check = server.Check(target="Text:Statut", checked=True)
    steps = [
        server.Step(do="set", target="Nom", text="Fini", timeout=0.1, check=check,
                    expect="Fini" if with_expect else None),  # absent avant, présent après : autre preuve insuffisante
        server.Step(do="set", target="Nom", text="Suite"),
    ]  # fmt: skip
    report, failed, symbols = run(executor(make_view(field, label), auto), *steps)
    assert not failed and symbols == ["~"]
    assert auto.calls == [("set_value", "Fini")] and field.element.live["value"] == "Fini"
    assert "postcondition unreadable" in report[0] and "not run" in report[-1]


def test_verified_already_satisfied_and_observer_steps_continue(monkeypatch):
    monkeypatch.setattr(server.w, "IsWindow", lambda hwnd: True)
    first = node("Edit", "Nom", has_value=True, value="Julien")
    second = node("Edit", "Ville", has_value=True, value="")
    auto = FakeAuto()
    report, failed, symbols = run(
        executor(make_view(first, second), auto),
        server.Step(do="set", target="Nom", text="Julien", check=server.Check(target="Nom", value="Julien")),
        server.Step(do="wait", timeout=0.1),  # aucune entrée, seule la durée écoulée est prouvée
        server.Step(do="set", target="Ville", text="Paris"),
        server.Step(do="set", target="Nom", text="Piron"),
    )
    assert not failed and symbols == ["=", "✓", "✓", "✓"]
    assert auto.calls == [("set_value", "Paris"), ("set_value", "Piron")]
    assert "waited" in report[1] and not any("stopped" in line for line in report)


@pytest.mark.parametrize("with_check", [False, True])
@pytest.mark.parametrize("incomplete", [False, True])
def test_expect_distinguishes_an_incomplete_read_from_a_proven_absence(with_check, incomplete):
    field = node("Edit", "Nom", has_value=True, value="")
    label = node("Text", "Statut")
    view = make_view(field, label)
    view.node_count = 3
    view.truncated = incomplete
    auto = FakeAuto()
    check = server.Check(target="Text:Statut", checked=True) if with_check else None
    steps = [
        server.Step(do="set", target="Nom", text="Fini", timeout=0.1, check=check, expect="Pret"),
        server.Step(do="set", target="Nom", text="Suite"),
    ]
    report, failed, symbols = run(executor(view, auto), *steps)
    assert auto.calls == [("set_value", "Fini")] and field.element.live["value"] == "Fini"
    assert "not run" in report[-1]
    if incomplete:
        assert not failed and symbols == ["~"]
        assert "EXPECTED TEXT UNREADABLE" in report[0] and not any("✗" in line for line in report)
    else:
        assert failed and symbols == ["✗"] and "EXPECTED NOT SEEN" in report[0]
    assert "NOT saved" in server._remember("test.exe", FP, "T", "r", steps, symbols, failed)
    assert routines.names("test.exe") == []


@pytest.mark.parametrize("incomplete", [False, True])
def test_wait_step_distinguishes_an_incomplete_read_from_a_proven_absence(incomplete):
    button = node("Button", "Continuer", has_invoke=True)
    view = make_view(button)
    view.node_count = 2
    view.truncated = incomplete
    auto = FakeAuto()
    steps = [server.Step(do="wait", text="Pret", timeout=0.1), server.Step(do="click", target="Continuer")]
    report, failed, symbols = run(executor(view, auto), *steps)
    assert auto.calls == [] and "not run" in report[-1]
    if incomplete:
        assert not failed and symbols == ["~"]
        assert "incomplete window read" in report[0] and not any("✗" in line for line in report)
    else:
        assert failed and symbols == ["✗"] and "absent" in report[0]
    assert "NOT saved" in server._remember("test.exe", FP, "T", "r", steps, symbols, failed)
    assert routines.names("test.exe") == []


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
    ex = actions.Executor(
        as_auto(auto), HWND, lambda rebuild: closed_view if state["closed"] else open_view, lambda: None
    )
    ex._pids = {7}
    report, failed, symbols = run(
        ex,
        server.Step(do="click", target="Fermer", check=server.Check(target="Confirmer la suppression", exists=False)),
    )
    assert not failed and symbols == ["✓"] and "absent" in report[0]


def test_absence_in_an_empty_reading_is_unknown():
    button = node("Button", "Fermer", has_invoke=True)
    view = make_view(button)  # node_count 0 : lecture vide, rien n'y est prouvé absent
    verdict = postcond.evaluate(executor(view, FakeAuto()), {"target": "Confirmer", "exists": False})
    assert verdict.state is None and "read is incomplete" in verdict.detail


def test_compare_fields():
    props = {"value": " Julien  Piron ", "toggle": 1, "expand": 0, "enabled": True, "has_value": True}
    assert postcond._compare(props, {"target": "x", "value": "Julien Piron"}).state is True
    assert postcond._compare(props, {"target": "x", "contains": "piron"}).state is True
    assert postcond._compare(props, {"target": "x", "checked": True, "expanded": False}).state is True
    assert postcond._compare(props, {"target": "x", "checked": False}).state is False
    assert postcond._compare(props, {"target": "x", "focused": True}).state is None  # non publié : inconnu
    assert postcond._compare({"selected": True}, {"target": "x", "checked": True}).state is True  # radio


def test_tally():
    assert server._tally(["✓", "✓", "=", "~"]) == "Summary: 2 verified, 1 already done, 1 unknown outcome."
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
    assert "(needs review)" in routines.summary("test.exe")
    assert routines.forget("test.exe", "saisir nom") and routines.names("test.exe") == []
    with pytest.raises(routines.RoutineError, match="none saved"):
        routines.get("test.exe", "saisir nom")


def test_bad_name_refused():
    with pytest.raises(routines.RoutineError):
        routines.remember("test.exe", "../évasion", [{"do": "click", "target": "OK"}], FP, "")


def test_server_remember_only_fully_proven_batches():
    steps = [server.Step(do="click", target="OK", expect="Fait")]
    refused = server._remember("test.exe", FP, "T", "r", steps, ["~"], False)
    assert "NOT saved" in refused and routines.names("test.exe") == []
    assert "NOT saved" in server._remember("test.exe", FP, "T", "r", steps, ["✗"], True)
    by_number = [server.Step(do="click", target="#2")]
    assert "by number" in server._remember("test.exe", FP, "T", "r", by_number, ["✓"], False)
    assert "saved for test.exe" in server._remember("test.exe", FP, "T", "r", steps, ["✓"], False)
    saved = json.loads((routines.ROOT / "test.exe.json").read_text(encoding="utf-8"))
    assert saved["routines"]["r"]["steps"] == [{"do": "click", "target": "OK", "expect": "Fait"}]
    assert "forgotten" in server._remember("test.exe", FP, "T", "r", [], [], False)


def test_server_routine_steps_and_notes():
    routines.remember("test.exe", "r", [{"do": "set", "target": "Nom", "text": "{{x}}"}], FP, "T")
    notes: list[str] = []
    steps, template = server._routine_steps("test.exe", FP | {"size": 99}, "r", {"x": "abc"}, notes)
    assert template[0].text == "{{x}}"
    assert steps[0].text == "abc" and steps[0].do == "set"
    assert any("updated since the routine" in n for n in notes)


def test_template_steps_run_with_params_and_refuse_missing_values():
    template = [server.Step(do="set", target="Nom", text="{{nom}}", check=server.Check(target="Nom", value="{{nom}}"))]
    filled = server._filled(template, {"nom": "Julien"})
    assert filled[0].check is not None
    assert filled[0].text == "Julien" and filled[0].check.value == "Julien"
    assert template[0].text == "{{nom}}"  # le modèle reste intact pour l'enregistrement
    with pytest.raises(actions.ActionError, match="nom"):
        server._filled(template, None)  # jamais « {{nom}} » tapé tel quel
    plain = [server.Step(do="click", target="OK")]
    assert server._filled(plain, None) is plain
