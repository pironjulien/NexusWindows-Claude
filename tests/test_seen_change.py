"""Clic sans relecture propre (UIA Invoke) : ce qui a changé dans la fenêtre est rapporté, jamais pris pour une preuve
(passe 2 du 22/10, puis relecture de Codex : une horloge change aussi la fenêtre)."""

from __future__ import annotations

import dataclasses
from typing import cast

from test_contract import HWND, as_call, make_view, node

from nexus_windows import actions, server, snapshot


class Shown:
    """Faux exécutant : la fenêtre se lit `before` avant le clic, `after` ensuite."""

    def __init__(self, before: snapshot.View, after: snapshot.View) -> None:
        self.notes: list[str] = []
        self.acted = False
        self.view = before
        self.before = before
        self.after = after
        self.clicked = False

    def _rebuild(self) -> snapshot.View:
        return self.after if self.clicked else self.before


def _run(monkeypatch, ex: Shown):
    monkeypatch.setattr(server.w, "IsWindow", lambda h: True)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)

    def click(executor, step):
        executor.clicked = True
        return actions.Outcome(True, "click [1]", "UIA Invoke")

    monkeypatch.setattr(server, "_dispatch", click)
    steps = [server.Step(do="click", target="7"), server.Step(do="click", target="8")]
    return server._run_steps(cast(actions.Executor, ex), steps, HWND, as_call())


def test_a_change_is_reported_but_never_proof(monkeypatch):
    before = make_view(node("Button", "7"), node("Text", "Affichage", value="0"))
    after = make_view(node("Button", "7"), node("Text", "Affichage", value="7"))
    report, failed = _run(monkeypatch, Shown(before, after))
    assert not failed and report[0].startswith("1. ~"), report
    assert 'window changed since: +Text "7"' in report[0] and "not proof" in report[0], report
    assert "(steps 2 to 2 not run)" in report[-1], report


def test_no_change_no_note(monkeypatch):
    view = make_view(node("Button", "Copier"), node("Text", "Affichage", value="7"))
    report, _ = _run(monkeypatch, Shown(view, make_view(*[e.node for e in view.elements])))
    assert report[0].startswith("1. ~") and "window changed" not in report[0], report


def test_incomplete_read_reports_nothing(monkeypatch):
    before = make_view(node("Button", "Effacer"), node("Text", "Ligne", value="a"))
    after = dataclasses.replace(make_view(node("Button", "Effacer")), truncated=True)
    report, _ = _run(monkeypatch, Shown(before, after))
    assert report[0].startswith("1. ~") and "window changed" not in report[0], report
