"""Passe 2 du 28/10 : touches de l'Explorateur prouvées par leur seul effet attribuable (focus sur un NOUVEL élément,
sélection étendue), numéros [N] d'une surgissante gardés quand on vise la boîte par son titre, et défilement sans
conteneur lu sous le point refusé comme cible manquante, pas comme « impossible en arrière-plan »."""

from __future__ import annotations

import dataclasses

import pytest
from test_contract import HWND, make_view, node
from test_seen_change import Shown, _run, rid, view

from nexus_windows import actions, server, session, snapshot


def test_f2_focusing_a_new_rename_field_is_proof(monkeypatch):
    before = view(node("ListItem", "note.txt", selected=True, focused=True, **rid(1)))
    after = view(node("ListItem", "note.txt", selected=True, **rid(1)), node("Edit", "Nom", focused=True, **rid(2)))
    report, failed, fields = _run(monkeypatch, Shown(before, after, None), do="key", keys="f2")
    assert not failed and report[0].startswith("1. ✓") and 'keyboard focus on new Edit "Nom"' in report[0], report
    assert fields[0]["preuve"] == "auto"
    assert "steps 2 to 2 not run" not in report[-1], report


def test_ctrl_a_selecting_more_items_is_proof_only_untargeted():
    before = view(node("ListItem", "a", selected=True, focused=True, **rid(1)), node("ListItem", "b", **rid(2)))
    after = view(
        node("ListItem", "a", selected=True, focused=True, **rid(1)), node("ListItem", "b", selected=True, **rid(2))
    )
    probe = server.Probe(server._fingerprint(before), "Test", frozenset({(42, 1), (42, 2)}), 1)
    assert server._key_transition(probe, after, "Ctrl+A", targeted=False) == "2 items selected (before: 1)"
    # cible désignée : son focus préparatoire peut la sélectionner ; autre raccourci : la sélection n'est pas son effet
    assert server._key_transition(probe, after, "ctrl+a", targeted=True) == ""
    assert server._key_transition(probe, after, "ctrl+c", targeted=False) == ""
    assert server._key_transition(probe, after, "ctrl+a ctrl+c", targeted=False) == ""


def test_focus_of_the_app_thread_in_a_new_window_is_proof(monkeypatch):
    # fenêtre en arrière-plan : aucun focus UIA ; le fil de l'Explorateur, lui, focalise le nouveau champ de renommage
    monkeypatch.setattr(server.w, "class_name", lambda h: "Edit")
    view_ = view(node("ListItem", "note.txt", **rid(1)))
    probe = server.Probe(
        server._fingerprint(view_), "Test", frozenset({(42, 1)}), 0, focus=11, hwnds=frozenset({10, 11})
    )
    assert server._key_transition(probe, view_, "f2", False, focus=55) == 'keyboard focus on new window "Edit"'
    assert server._key_transition(probe, view_, "f2", False, focus=10) == ""  # fenêtre qui existait déjà
    assert server._key_transition(probe, view_, "f2", False, focus=11) == ""  # focus inchangé
    assert server._key_transition(probe, view_, "f2 enter", False, focus=55) == ""  # séquence
    assert server._key_transition(probe, view_, "ctrl+c", True, focus=55) == ""  # focus préparatoire de la cible


def test_a_sequence_is_never_proved_by_one_of_its_gestures():
    before = view(node("ListItem", "note.txt", focused=True, **rid(1)))
    after = view(node("ListItem", "note.txt", **rid(1)), node("Edit", "Nom", focused=True, **rid(2)))
    probe = server.Probe(server._fingerprint(before), "Test", frozenset({(42, 1)}), 0)
    assert server._key_transition(probe, after, "f2", targeted=False).startswith("keyboard focus on new Edit")
    assert server._key_transition(probe, after, "f2 enter", targeted=False) == ""


def test_focus_moving_to_an_existing_element_proves_nothing(monkeypatch):
    # Tab, ou focus posé par l'outil sur la cible : l'élément existait déjà, rien n'est attribuable à la touche
    before = view(node("Edit", "A", focused=True, **rid(1)), node("Edit", "B", **rid(2)))
    after = view(node("Edit", "A", **rid(1)), node("Edit", "B", focused=True, **rid(2)))
    report, _, _ = _run(monkeypatch, Shown(before, after, None), do="key", keys="f2")
    assert report[0].startswith("1. ~"), report


def test_ctrl_c_enabling_paste_proves_nothing(monkeypatch):
    before = view(node("Button", "Coller", enabled=False, **rid(1)), node("ListItem", "a", selected=True, **rid(2)))
    after = view(node("Button", "Coller", enabled=True, **rid(1)), node("ListItem", "a", selected=True, **rid(2)))
    report, _, _ = _run(monkeypatch, Shown(before, after, None), do="key", keys="f2")
    assert report[0].startswith("1. ~"), report


def test_a_new_element_without_focus_proves_nothing(monkeypatch):
    before = view(node("Text", "Horloge", value="10:00", **rid(1)))
    after = view(node("Text", "Horloge", value="10:01", **rid(3)))
    report, _, _ = _run(monkeypatch, Shown(before, after, None), do="key", keys="f2")
    assert report[0].startswith("1. ~"), report


DIALOG = 99


def _shown_view(hwnd: int, windows: list[int], taken: float) -> snapshot.View:
    base = make_view(node("Edit", "Nom du fichier :"))
    return dataclasses.replace(base, hwnd=hwnd, windows=windows, taken=taken)


def test_a_dialog_keeps_the_numbers_shown_with_its_parent(monkeypatch):
    monkeypatch.setattr(session.w, "IsWindow", lambda h: True)
    s = session.Session()
    parent = _shown_view(HWND, [HWND, DIALOG], 1.0)
    s.shown[HWND] = parent
    assert s.shown_for(DIALOG) is parent
    own = _shown_view(DIALOG, [DIALOG], 2.0)
    s.shown[DIALOG] = own
    assert s.shown_for(DIALOG) is own  # sa propre liste d'abord
    assert s.shown_for(1234) is None  # fenêtre sans lien : aucun numéro


def test_the_most_recent_list_wins_between_dialog_and_parent(monkeypatch):
    # relecture de Codex : une ancienne liste propre de la boîte ne prime pas la liste du parent montrée après
    monkeypatch.setattr(session.w, "IsWindow", lambda h: True)
    s = session.Session()
    old_dialog = _shown_view(DIALOG, [DIALOG], 1.0)
    parent = _shown_view(HWND, [HWND, DIALOG], 2.0)
    s.mark_shown(old_dialog, {1})
    s.mark_shown(parent, {1})
    assert s.shown_for(DIALOG) is parent
    s.mark_shown(old_dialog, {1})  # la boîte regardée de nouveau : sa liste reprend la main
    assert s.shown_for(DIALOG) is old_dialog


def test_a_closed_parent_lends_no_numbers(monkeypatch):
    monkeypatch.setattr(session.w, "IsWindow", lambda h: h != HWND)
    s = session.Session()
    s.shown[HWND] = _shown_view(HWND, [HWND, DIALOG], 1.0)
    assert s.shown_for(DIALOG) is None


def test_a_point_without_scroll_container_is_a_missing_target(monkeypatch):
    from test_contract import FakeAuto, as_auto

    uwp = dataclasses.replace(make_view(), framework="UWP")
    ex = actions.Executor(as_auto(FakeAuto()), HWND, lambda rebuild: uwp, lambda title=None: None, fresh=True)
    monkeypatch.setattr(ex, "_scroll_container_at", lambda x, y: None)
    monkeypatch.setattr(ex, "_main_scroll_container", lambda view: None)
    monkeypatch.setattr(ex, "_get_page", lambda: None)
    with pytest.raises(actions.ActionError) as error:
        ex.scroll(None, "down", 3)
    assert not isinstance(error.value, actions.NeedsForeground)
    assert "no scrollable container read under this point" in str(error.value)
