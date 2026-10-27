"""Action sans preuve demandée (ni expect ni check) : la fenêtre est relue d'elle-même juste après. Seule la transition
ATTENDUE pour un clic sur CETTE instance (suivie par son identifiant d'exécution dans l'arbre complet) prouve l'action
(✓, `preuve: auto` au journal commun) : case cochée, élément déplié ou sélectionné. Tout le reste est rapporté, jamais
pris pour une preuve : horloge, notification, réponse en flux, disparition (homonyme, liste virtualisée, lecture
partielle), surgissante non relue, fenêtre fermée, valeur d'un champ, sélection en chemin d'un clic droit (relectures
de Codex du 22/10 et du 27/10, d'Antigravity du 27/10)."""

from __future__ import annotations

import dataclasses
import json
from typing import cast

from test_contract import HWND, as_call, make_view, node

from nexus_windows import actions, journal, server, snapshot, uia

POPUP = 77


def view(*nodes: uia.Node, hidden: tuple[uia.Node, ...] = (), popup: tuple[uia.Node, ...] | None = None):
    """Vue dont l'arbre complet porte les éléments listés, plus `hidden` (hors champ, non listés) ; `popup` : une
    surgissante lue avec ses éléments."""
    base = make_view(*nodes)
    roots = {HWND: uia.Node(None, 0, {"control_type": 50033}, children=[*nodes, *hidden])}
    if popup is not None:
        roots[POPUP] = uia.Node(None, 0, {"control_type": 50033}, children=list(popup))
    return dataclasses.replace(base, roots=roots)


class Shown:
    """Faux exécutant : la fenêtre se lit `before` avant l'action, `after` ensuite ; `target` : l'élément visé."""

    def __init__(self, before: snapshot.View, after: snapshot.View, target: snapshot.Element | None) -> None:
        self.notes: list[str] = []
        self.acted = False
        self.view = before
        self.before = before
        self.after = after
        self.target = target
        self.aimed: dict | None = None
        self.clicked = False
        self.auto = None  # aucune écoute réelle : `heard` est posé par le test
        self.listen = False
        self.heard: Heard | None = None
        self.invoked = False  # l'application émet « Invoqué » quand l'action est faite
        self.target_pids = {7}

    def disarm(self) -> None:
        self.heard = None

    def _rebuild(self) -> snapshot.View:
        self.view = self.after if self.clicked else self.before
        return self.view


class Heard:
    """Fausse écoute de l'événement « Invoqué »."""

    def __init__(self) -> None:
        self.fired = False

    def wait(self, timeout_s: float) -> bool:
        return self.fired

    def close(self) -> None:
        pass


class Steps:
    def __init__(self) -> None:
        self.fields: list[dict] = []

    def step(self, **fields) -> None:
        self.fields.append(fields)


def _run(monkeypatch, ex: Shown, closes=False, do="click"):
    monkeypatch.setattr(server.w, "IsWindow", lambda h: not (closes and ex.clicked and h == HWND))
    monkeypatch.setattr(server.w, "window_text", lambda h: "Test")
    monkeypatch.setattr(server.time, "sleep", lambda s: None)

    def act(executor, step):
        executor.aimed = dict(executor.target.props) if executor.target else None  # résolution de la cible
        if executor.listen and executor.target is not None:
            executor.heard = Heard()
        executor.clicked = True
        if executor.heard is not None:
            executor.heard.fired = executor.invoked
        return actions.Outcome(True, f"{step.do} [1]", "UIA Invoke")

    monkeypatch.setattr(server, "_dispatch", act)
    steps = [server.Step.model_validate({"do": do, "target": "x"}), server.Step(do="click", target="y")]
    call = Steps()
    report, failed = server._run_steps(cast(actions.Executor, ex), steps, HWND, as_call(call))
    return report, failed, call.fields


def rid(n: int) -> dict:
    return {"runtime_id": [42, n]}


def test_a_change_elsewhere_is_reported_never_proof(monkeypatch):
    before = view(node("Button", "Valider", **rid(1)), node("Text", "Statut", value="prêt"))
    after = view(node("Button", "Valider", **rid(1)), node("Text", "Statut", value="clic 1"))
    report, failed, fields = _run(monkeypatch, Shown(before, after, before.elements[0]))
    assert not failed and report[0].startswith("1. ~"), report
    assert 'window changed since: +Text "clic 1"' in report[0] and "not proof" in report[0], report
    assert "(steps 2 to 2 not run)" in report[-1], report
    assert fields[0]["preuve"] == "absente"


def test_a_vanished_target_is_only_reported(monkeypatch):
    # lecture partielle silencieuse ou ligne virtualisée recyclée : une disparition ne prouve jamais le clic
    before = view(node("MenuItem", "Copier", **rid(1)), node("MenuItem", "Coller", **rid(2)), node("List", "L"))
    after = view(node("List", "L"))
    report, failed, fields = _run(monkeypatch, Shown(before, after, before.elements[0]))
    assert not failed and report[0].startswith("1. ~") and '-MenuItem "Copier"' in report[0], report
    assert fields[0]["preuve"] == "absente"


def test_a_vanished_namesake_proves_nothing(monkeypatch):
    before = view(node("Button", "Supprimer", **rid(1)), node("Button", "Supprimer", **rid(2)))
    after = view(node("Button", "Supprimer", **rid(1)))  # B disparu, A (cliqué) toujours là
    report, _, _ = _run(monkeypatch, Shown(before, after, before.elements[0]))
    assert report[0].startswith("1. ~"), report


def test_a_target_without_runtime_id_proves_nothing(monkeypatch):
    before = view(node("Button", "Réessayer (5)"))
    after = view(node("Button", "Réessayer (4)"))  # compte à rebours, clic ignoré
    report, _, _ = _run(monkeypatch, Shown(before, after, before.elements[0]))
    assert report[0].startswith("1. ~"), report


def test_a_button_scrolled_out_of_the_list_is_not_gone(monkeypatch):
    button = node("Button", "Action", **rid(1))
    before = view(button, node("Text", "Statut"))
    hidden = node("Button", "Action", offscreen=True, **rid(1))
    after = view(node("Text", "Statut"), hidden=(hidden,))
    report, _, _ = _run(monkeypatch, Shown(before, after, before.elements[0]))
    assert report[0].startswith("1. ~"), report


def test_an_unread_popup_proves_nothing(monkeypatch):
    item = node("MenuItem", "Copier", **rid(1))
    before = view(node("List", "L"), popup=(item,))
    before = dataclasses.replace(before, elements=[*before.elements, snapshot.Element(2, item, 0, POPUP)])
    after = view(node("List", "L"))  # la surgissante est encore là (IsWindow) mais son arbre n'a pu être lu
    report, _, _ = _run(monkeypatch, Shown(before, after, before.elements[-1]))
    assert report[0].startswith("1. ~"), report


def test_a_vanished_list_item_proves_nothing(monkeypatch):
    before = view(node("ListItem", "Charlie", **rid(1)), node("List", "L"))
    after = view(node("List", "L"))  # liste virtualisée : l'élément a pu être retiré sans clic
    report, _, _ = _run(monkeypatch, Shown(before, after, before.elements[0]))
    assert report[0].startswith("1. ~"), report


def test_a_recycled_instance_proves_nothing(monkeypatch):
    before = view(node("ListItem", "Alpha", selected=False, **rid(1)))
    after = view(node("ListItem", "Bravo", selected=True, **rid(1)))
    report, _, _ = _run(monkeypatch, Shown(before, after, before.elements[0]))
    assert report[0].startswith("1. ~"), report


def test_a_checked_box_is_proof(monkeypatch):
    before = view(node("CheckBox", "Option", has_toggle=True, toggle=0, **rid(1)))
    after = view(node("CheckBox", "Option", has_toggle=True, toggle=1, **rid(1)))
    report, failed, fields = _run(monkeypatch, Shown(before, after, before.elements[0]))
    assert not failed and report[0].startswith("1. ✓") and 'verified: CheckBox "Option" checked' in report[0], report
    assert fields[0]["preuve"] == "auto" and fields[0]["verified"] is True
    assert "steps 2 to 2 not run" not in report[-1], report


def test_a_selected_tab_is_proof(monkeypatch):
    before = view(node("TabItem", "Réglages", selected=False, **rid(1)))
    after = view(node("TabItem", "Réglages", selected=True, **rid(1)))
    report, _, _ = _run(monkeypatch, Shown(before, after, before.elements[0]))
    assert report[0].startswith("1. ✓") and 'TabItem "Réglages" selected' in report[0], report


def test_an_expanded_tree_item_is_proof(monkeypatch):
    before = view(node("TreeItem", "Dossier", expand=0, **rid(1)))
    after = view(node("TreeItem", "Dossier", expand=1, **rid(1)), node("TreeItem", "Fichier", **rid(2)))
    report, _, _ = _run(monkeypatch, Shown(before, after, before.elements[0]))
    assert report[0].startswith("1. ✓") and 'TreeItem "Dossier" expanded' in report[0], report


def test_a_value_changing_by_itself_proves_nothing(monkeypatch):
    before = view(node("Edit", "Compte", has_value=True, value="1", **rid(1)))
    after = view(node("Edit", "Compte", has_value=True, value="2", **rid(1)))
    report, _, _ = _run(monkeypatch, Shown(before, after, before.elements[0]))
    assert report[0].startswith("1. ~"), report


def test_right_and_double_clicks_prove_nothing_by_themselves(monkeypatch):
    before = view(node("ListItem", "Alpha", selected=False, **rid(1)))
    after = view(node("ListItem", "Alpha", selected=True, **rid(1)), node("MenuItem", "Copier", **rid(2)))
    for do in ("right_click", "double_click"):
        report, _, _ = _run(monkeypatch, Shown(before, after, before.elements[0]), do=do)
        assert report[0].startswith("1. ~"), (do, report)


def test_an_unnamed_target_proves_nothing(monkeypatch):
    before = view(node("Button", "", **rid(1)), node("Text", "Statut", value="prêt"))
    after = view(node("Text", "Statut", value="prêt"))
    report, _, _ = _run(monkeypatch, Shown(before, after, before.elements[0]))
    assert report[0].startswith("1. ~"), report


def test_a_closed_window_is_only_reported(monkeypatch):
    before = view(node("Button", "OK", **rid(1)), node("Text", "Continuer ?"))
    report, _, fields = _run(monkeypatch, Shown(before, before, before.elements[0]), closes=True)
    assert report[0].startswith("1. ~") and "the window closed" in report[0], report
    assert fields[0]["preuve"] == "impossible"  # plus rien à relire


def test_no_change_stays_unknown(monkeypatch):
    before = view(node("Button", "Valider", **rid(1)), node("Text", "Statut", value="7"))
    after = view(node("Button", "Valider", **rid(1)), node("Text", "Statut", value="7"))
    report, _, fields = _run(monkeypatch, Shown(before, after, before.elements[0]))
    assert report[0].startswith("1. ~") and "changed" not in report[0], report
    assert fields[0]["preuve"] == "absente"


def test_incomplete_or_stale_read_proves_nothing(monkeypatch):
    before = view(node("MenuItem", "Copier", **rid(1)), node("Text", "Ligne", value="a"))
    for broken in ({"truncated": True}, {"stale": True}):
        after = dataclasses.replace(view(node("Text", "Ligne", value="a")), **broken)
        report, _, fields = _run(monkeypatch, Shown(before, after, before.elements[0]))
        assert report[0].startswith("1. ~") and "changed" not in report[0], (broken, report)
        assert fields[0]["preuve"] == "impossible", broken


def test_stale_view_before_the_action_proves_nothing(monkeypatch):
    before = dataclasses.replace(view(node("MenuItem", "Copier", **rid(1))), stale=True)
    report, _, fields = _run(monkeypatch, Shown(before, view(), before.elements[0]))
    assert report[0].startswith("1. ~"), report
    assert fields[0]["preuve"] == "impossible"


def test_the_invoked_event_is_proof(monkeypatch):
    before = view(node("Button", "Valider", has_invoke=True, **rid(1)), node("Text", "Statut", value="prêt"))
    ex = Shown(before, before, before.elements[0])
    ex.invoked = True
    report, failed, fields = _run(monkeypatch, ex)
    assert not failed and report[0].startswith("1. ✓") and "invoked: the control reported the click" in report[0]
    assert "give expect to check it" in report[0]  # le clic est reçu, son résultat n'est pas relu
    assert fields[0]["preuve"] == "invoque"
    assert ex.heard is None  # écoute fermée après l'étape


def test_the_invoked_event_proves_even_when_the_dialog_closes(monkeypatch):
    before = view(node("Button", "OK", has_invoke=True, **rid(1)))
    ex = Shown(before, before, before.elements[0])
    ex.invoked = True
    report, _, _ = _run(monkeypatch, ex, closes=True)
    assert report[0].startswith("1. ✓") and "invoked" in report[0], report


def test_the_invoked_event_proves_even_without_a_readable_tree(monkeypatch):
    before = dataclasses.replace(view(node("Button", "Valider", has_invoke=True, **rid(1))), stale=True)
    ex = Shown(before, before, before.elements[0])
    ex.invoked = True
    report, _, fields = _run(monkeypatch, ex)
    assert report[0].startswith("1. ✓"), report
    assert fields[0]["preuve"] == "invoque"


def test_no_listening_for_right_or_double_clicks(monkeypatch):
    before = view(node("Button", "Valider", has_invoke=True, **rid(1)))
    for do in ("right_click", "double_click"):
        ex = Shown(before, before, before.elements[0])
        ex.invoked = True
        report, _, _ = _run(monkeypatch, ex, do=do)
        assert report[0].startswith("1. ~"), (do, report)


def test_journal_line_carries_the_proof_origin(tmp_path):
    contexte = {"app": "test.exe", "cadre": "WPF"}
    journal.etape("1", contexte, {"do": "click", "ok": True, "verified": True, "preuve": "auto"})
    journal.etape("1", contexte, {"do": "click", "ok": True, "preuve": "impossible"})
    journal.etape("1", contexte, {"do": "click", "ok": True})
    journal.etape("1", contexte, {"do": "click", "ok": False, "preuve": "auto"})
    lines = [
        json.loads(x)
        for f in (tmp_path / "journal-commun").rglob("*.jsonl")
        for x in f.read_text("utf-8").split("\n")
        if x
    ]
    assert [(x["issue"], x["preuve"]) for x in lines] == [
        ("✓", "auto"),
        ("~", "impossible"),
        ("~", "absente"),
        ("✗", None),
    ]


def test_the_window_listener_keeps_only_the_target_process():
    class Sender:
        def __init__(self, pid: int) -> None:
            self.CurrentProcessId = pid

    heard = uia._Heard({7})
    heard.HandleAutomationEvent(Sender(9), uia.WINDOW_OPENED)
    assert not heard.fired.is_set()
    heard.HandleAutomationEvent(Sender(7), uia.WINDOW_OPENED)
    assert heard.fired.is_set()


def test_the_executor_listens_only_when_asked_and_on_invokable_elements():
    from test_contract import FakeAuto, executor

    class Listening(FakeAuto):
        def __init__(self) -> None:
            super().__init__()
            self.listened: list[object] = []

        def listen_invoked(self, element):
            self.listened.append(element)
            return Heard()

    auto = Listening()
    button = node("Button", "Valider", has_invoke=True, **rid(1))
    text = node("Text", "Statut", **rid(2))
    ex = executor(view(button, text), auto)
    ex.resolve("Valider")
    assert auto.listened == [] and ex.heard is None  # écoute non demandée
    ex.listen = True
    ex.resolve("Valider")
    assert auto.listened == [button.element] and ex.heard is not None
    ex.resolve("Statut")  # pas de motif Invoke : aucune écoute, la précédente est fermée
    assert ex.heard is None


def test_a_failed_listener_removal_is_kept_and_retried():
    class FlakyUIA:
        def __init__(self) -> None:
            self.registered = 0
            self.fail_next_remove = True

        def AddAutomationEventHandler(self, *args) -> None:  # noqa: N802 - interface COM
            self.registered += 1

        def RemoveAutomationEventHandler(self, *args) -> None:  # noqa: N802 - interface COM
            if self.fail_next_remove:
                self.fail_next_remove = False
                raise OSError("busy")
            self.registered -= 1

    fake = FlakyUIA()
    first = uia.Listener(fake, uia.INVOKED, object(), 1)
    first.close()  # retrait échoué : gardé, jamais perdu
    assert fake.registered == 1 and first in uia._ORPHANS
    second = uia.Listener(fake, uia.INVOKED, object(), 1)  # l'inscription ne paie jamais la dette de nettoyage
    assert fake.registered == 2 and first in uia._ORPHANS
    second.close()  # après l'action : son retrait, puis une ancienne en suspens
    assert fake.registered == 0 and not uia._ORPHANS


def test_a_busy_registry_skips_listening_instead_of_blocking(monkeypatch):
    monkeypatch.setattr(uia, "REGISTRY_WAIT_S", 0.01)
    with uia._REGISTRY:  # un thread de travail abandonné encore bloqué dans Add/Remove
        try:
            uia.Listener(object(), uia.INVOKED, object(), 1)
        except uia.UIAError as error:
            assert "busy" in str(error)
        else:
            raise AssertionError("registration should have been skipped")


def test_an_acknowledged_click_is_never_sent_again():
    from test_contract import FakeAuto, executor

    from nexus_windows import methods

    sent: list[str] = []
    ex = executor(view(node("Button", "Valider", has_invoke=True, **rid(1))), FakeAuto())
    heard = Heard()
    ex.heard = cast(uia.Listener, heard)

    def invoke() -> str:
        sent.append("invoke")
        heard.fired = True  # l'application a reçu le clic, rien ne change à l'écran
        raise methods.MethodError("no effect observed within 1.0 s")

    def posted() -> str:
        sent.append("posted click")
        return ""

    outcome = ex._try([("UIA Invoke", invoke), ("click by messages", posted)], 'click on Button "Valider"')
    assert sent == ["invoke"] and outcome.ok and outcome.symbol == "~", (sent, outcome.line)
    assert "nothing else sent" in outcome.line


def test_a_click_with_expect_still_listens_so_it_is_never_sent_twice(monkeypatch):
    before = view(node("Button", "Valider", has_invoke=True, **rid(1)), node("Text", "Statut", value="prêt"))
    ex = Shown(before, before, before.elements[0])
    ex.invoked = True
    listened: list[bool] = []
    monkeypatch.setattr(server.w, "IsWindow", lambda h: True)
    monkeypatch.setattr(server.w, "window_text", lambda h: "Test")

    def act(executor, step):
        listened.append(executor.listen)  # écoute armée à la résolution, avant tout envoi
        return actions.Outcome(True, "click [1]", "UIA Invoke")

    monkeypatch.setattr(server, "_dispatch", act)
    monkeypatch.setattr(ex, "present", lambda text: False, raising=False)
    monkeypatch.setattr(
        ex,
        "wait",
        lambda text, gone, timeout: actions.Outcome(True, '"fait" present', "wait", verified=True),
        raising=False,
    )
    steps = [server.Step(do="click", target="x", expect="fait")]
    report, failed = server._run_steps(cast(actions.Executor, ex), steps, HWND, as_call(Steps()))
    assert listened == [True], listened
    assert not failed and report[0].startswith("1. ✓") and "expected" in report[0], report
    assert "invoked" not in report[0]  # la preuve est celle demandée, pas la relecture automatique
    assert not ex.listen and ex.heard is None


def test_a_covered_unredrawn_chromium_window_is_unverifiable_not_ignoring(monkeypatch):
    """Cas réel du 27/10 : clic par messages sur Codex Desktop (Chromium) recouvert par Claude, lancé sans les
    commutateurs d'occlusion. Le clic avait marché ; l'image figée ne pouvait pas le montrer."""
    from test_contract import FakeAuto, executor

    from nexus_windows import act_base

    for covered, wanted, unwanted in ((True, "unverifiable: window covered", "ignores"), (False, "ignores", "")):
        chromium = dataclasses.replace(view(node("Button", "Reprendre", **rid(1))), framework="Chromium")
        ex = executor(chromium, FakeAuto())
        monkeypatch.setattr(ex, "_fingerprint", lambda: ("figée",))
        monkeypatch.setattr(snapshot, "covered_by_window", lambda h, covered=covered: covered)
        monkeypatch.setattr(snapshot, "occlusion_disabled", lambda pid: None)
        monkeypatch.setattr(snapshot, "covered_remedy", lambda h: "remedy.")
        monkeypatch.setattr(server.w, "window_pid", lambda h: 7)
        monkeypatch.setattr(act_base.time, "sleep", lambda s: None)
        try:
            ex._try([("click by messages", ex._watched(lambda: None, settle_s=0.0))], "click at 10,10")
        except actions.Unconfirmed as error:
            text = str(error)
        else:
            raise AssertionError("an unproven click by messages must stay unknown")
        assert wanted in text and (not unwanted or unwanted not in text), text
        if covered:
            assert "Do not send it again" in text and "remedy." in text and "check the image" not in text
