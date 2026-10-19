"""Accord Claude–Antigravity « garde-fous » du 18/10/2026 : politique commune (shared/garde-fous.json) et confirmation
étanche. Non-régression : un geste à confirmer est refusé AVANT toute entrée, quelle que soit sa forme (clic par nom,
par [N], en coordonnées, Entrée, Espace, Ctrl+Entrée, Alt+lettre, saut de ligne tapé) ; confirm=true le laisse passer ;
une routine ne garde jamais confirm=true ; le blocage dur s'applique aussi à la lecture."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nexus_windows import actions, foreground, policy, routines, server, uia
from nexus_windows import win32 as w

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contract import HWND, FakeAuto, as_call, executor, make_view, node  # noqa: E402


def _group(*children: uia.Node) -> uia.Node:
    """Fenêtre > groupe (composeur, formulaire) > enfants : le voisinage d'un champ."""
    window = node("Window", "Fenêtre")
    group = node("Group", "")
    group.parent = window
    window.children.append(group)
    for child in children:
        child.parent = group
        group.children.append(child)
    return window


def _composer(send_label: str = "Envoyer", **send_props):
    field = node("Edit", "Message", has_value=True, value="", focused=True)
    send = node("Button", send_label, has_invoke=True, **send_props)
    _group(field, send)
    return field, send


# --- Clic ---------------------------------------------------------------------------------------------------------
def test_click_on_send_refused_then_allowed_with_confirm():
    send = node("Button", "Envoyer", has_invoke=True)
    auto = FakeAuto()
    ex = executor(make_view(send), auto)
    with pytest.raises(actions.ConfirmationRequired, match="confirm=true"):
        ex.click("Envoyer")
    assert auto.calls == []  # rien n'a été fait
    ex.confirm = True
    ex.click("Envoyer")
    assert [c[0] for c in auto.calls] == ["invoke"]


def test_double_click_refused_right_click_free():
    publish = node("Button", "Publier", has_invoke=True)
    ex = executor(make_view(publish), FakeAuto())
    with pytest.raises(actions.ConfirmationRequired):
        ex.click("Publier", count=2)
    ex._confirm_click(ex.resolve_element("Publier", set(), "x"), button="right")  # menu contextuel : rien à confirmer


def test_text_inside_a_button_counts_for_the_button():
    button = node("Button", "", has_invoke=True)
    label = node("Text", "Supprimer définitivement")
    label.parent = button
    button.children.append(label)
    ex = executor(make_view(label), FakeAuto())
    with pytest.raises(actions.ConfirmationRequired, match="supprimer definitivement"):
        ex._confirm_click(ex.resolve_element("Supprimer définitivement", set(), "x"))


@pytest.mark.parametrize(
    ("role", "name", "props"),
    [
        ("ListItem", "Éléments envoyés", {}),  # dossier : ne déclenche rien
        ("TabItem", "Publier", {}),
        ("Button", "Boîte d'envoi", {"has_invoke": True}),  # nom, pas verbe d'action
        ("MenuItem", "Envoyer vers", {"has_invoke": True, "has_expand": True, "expand": 0}),  # ouvre un sous-menu
        ("Button", "Supprimer", {"has_invoke": True}),  # supprimer simple : réversible (corbeille)
        ("Button", "Déployer en production", {"has_invoke": True}),  # autorisé durablement par Julien
        ("Button", "Réinitialiser les filtres", {"has_invoke": True}),
    ],
)
def test_free_clicks(role, name, props):
    target = node(role, name, **props)
    ex = executor(make_view(target), FakeAuto())
    ex._confirm_click(ex.resolve_element(name, set(), "x"))  # ne lève pas


@pytest.mark.parametrize(
    "label",
    [
        "Payer 49,99 €",
        "Passer la commande",
        "Effectuer le virement",
        "Send",
        "Poster",
        "Vider la corbeille",
        "Désinstaller",
        "Formater",
        "Réinitialiser ce PC",
        "Résilier mon abonnement",
        "Supprimer mon compte",
        "Change to public",
        "Rendre le dépôt public",
    ],
)
def test_confirm_words(label):
    target = node("Button", label, has_invoke=True)
    ex = executor(make_view(target), FakeAuto())
    with pytest.raises(actions.ConfirmationRequired):
        ex._confirm_click(ex.resolve_element(label, set(), "x"))


def test_generic_ok_in_a_payment_window(monkeypatch):
    ok = node("Button", "OK", has_invoke=True)
    ex = executor(make_view(ok), FakeAuto())
    element = ex.resolve_element("OK", set(), "x")
    ex._confirm_click(element)  # fenêtre ordinaire : OK libre
    monkeypatch.setattr(ex, "_title", lambda: "Confirmer le paiement — Banque")
    with pytest.raises(actions.ConfirmationRequired, match="payment"):
        ex._confirm_click(element)


def test_click_by_coordinates_cannot_bypass(monkeypatch):
    send = node("Button", "Envoyer", has_invoke=True, rect=(100, 100, 80, 30))
    icon = node("Image", "", rect=(110, 105, 16, 16))
    icon.parent = send
    send.children.append(icon)
    ex = executor(make_view(send), FakeAuto())
    with pytest.raises(actions.ConfirmationRequired, match="coordinates"):
        ex._confirm_point(115, 110)  # sur l'icône du bouton
    ex._confirm_point(115, 110, button="right")
    ex.confirm = True
    ex._confirm_point(115, 110)


def test_click_point_refuses_before_any_input(monkeypatch):
    send = node("Button", "Envoyer", has_invoke=True, rect=(100, 100, 80, 30))
    auto = FakeAuto()
    ex = executor(make_view(send), auto)
    posted: list = []
    monkeypatch.setattr(ex, "_post_click", lambda *a, **k: posted.append(a) or 0)
    with pytest.raises(actions.ConfirmationRequired):
        ex.click_point(120, 110)
    assert posted == [] and auto.calls == []


# --- Touches ------------------------------------------------------------------------------------------------------
def test_enter_in_a_composer_refused_before_any_key(monkeypatch):
    field, send = _composer()
    sent: list = []
    monkeypatch.setattr(actions.realinput, "press", lambda combo: sent.append(combo))
    ex = executor(make_view(field, send), FakeAuto())
    with pytest.raises(actions.ConfirmationRequired, match="next to the field"):
        ex.press("enter", "Message")
    assert sent == []


def test_newline_typed_in_a_composer_refused():
    field, send = _composer()
    auto = FakeAuto()
    ex = executor(make_view(field, send), auto)
    with pytest.raises(actions.ConfirmationRequired, match="line break"):
        ex.write("Message", "Bonjour\n", replace=True)
    assert auto.calls == []
    ex._confirm_text(ex.resolve_element("Message", set(), "x"), "Bonjour sans saut")  # texte seul : libre


@pytest.mark.parametrize(("keys", "refused"), [("enter", True), ("ctrl+enter", True), ("shift+enter", False),
                                               ("alt+enter", False), ("tab", False), ("ctrl+a delete", False)])  # fmt: skip
def test_key_kinds_in_a_composer(keys, refused):
    field, send = _composer()
    ex = executor(make_view(field, send), FakeAuto())
    element = ex.resolve_element("Message", set(), "x")
    combos = actions.keys.parse_sequence(keys)
    if refused:
        with pytest.raises(actions.ConfirmationRequired):
            ex._confirm_keys(combos, keys, element)
    else:
        ex._confirm_keys(combos, keys, element)


def test_enter_triggers_the_default_button():
    field = node("Edit", "Nom", has_value=True, value="")
    other = node("Edit", "Commentaire", has_value=True, value="")
    _group(field, other)
    default = node("Button", "Supprimer définitivement", has_invoke=True, legacy_state=0x100)
    window = field.parent.parent if field.parent else None
    assert window is not None
    default.parent = window
    window.children.append(default)
    ex = executor(make_view(field, default), FakeAuto())
    with pytest.raises(actions.ConfirmationRequired, match="default button"):
        ex._confirm_keys(actions.keys.parse_sequence("enter"), "enter", ex.resolve_element("Nom", set(), "x"))


def test_space_on_focused_button_and_alt_shortcut():
    send = node("Button", "Envoyer", has_invoke=True, focused=True, access_key="Alt+E")
    ex = executor(make_view(send), FakeAuto())
    with pytest.raises(actions.ConfirmationRequired):
        ex._confirm_keys(actions.keys.parse_sequence("space"), "space", None)
    with pytest.raises(actions.ConfirmationRequired, match="shortcut"):
        ex._confirm_keys(actions.keys.parse_sequence("alt+e"), "alt+e", None)
    ex._confirm_keys(actions.keys.parse_sequence("alt+f"), "alt+f", None)  # autre raccourci : libre


# --- Étapes, routines, lecture ------------------------------------------------------------------------------------
def test_step_confirm_flag_reaches_the_executor():
    send = node("Button", "Envoyer", has_invoke=True)
    auto = FakeAuto()
    ex = executor(make_view(send), auto)
    report, failed = server._run_steps(ex, [server.Step(do="click", target="Envoyer")], HWND, as_call())
    assert failed and "confirm=true" in report[0] and auto.calls == []
    report, failed = server._run_steps(ex, [server.Step(do="click", target="Envoyer", confirm=True)], HWND, as_call())
    assert not failed and [c[0] for c in auto.calls] == ["invoke"]


def test_routine_never_keeps_confirm(monkeypatch):
    saved: list = []
    monkeypatch.setattr(routines, "remember", lambda app, name, steps, fp, title: saved.append(steps))
    steps = [server.Step(do="click", target="Envoyer", confirm=True)]
    server._remember("app", {}, "t", "envoi", steps, ["✓"], False)
    assert saved and "confirm" not in saved[0][0]


def test_hard_block_also_refuses_reading(monkeypatch):
    monkeypatch.setattr(foreground, "app_pid", lambda hwnd: 1)
    monkeypatch.setattr(w, "process_name", lambda pid: "KeePassXC.exe")
    monkeypatch.setattr(w, "class_name", lambda hwnd: "Qt5QWindowIcon")
    monkeypatch.setattr(w, "window_text", lambda hwnd: "Coffre")
    with pytest.raises(actions.ActionError, match="password manager"):
        server._check_readable_window(HWND)
    monkeypatch.setattr(w, "process_name", lambda pid: "Antigravity.exe")
    server._check_readable_window(HWND)  # l'autre agent : lisible et pilotable


def test_policy_is_the_shared_one():
    assert policy.garde_fous.POLICY_FILE.name == "garde-fous.json"
    assert policy.garde_fous.POLICY_FILE.parent == policy.SHARED_DIR
    assert policy.SHARED_DIR.name == "shared"
