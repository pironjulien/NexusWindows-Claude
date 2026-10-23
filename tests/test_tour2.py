"""Second tour du banc (23/10) et relecture de Codex : fenêtre par titre, touches au champ désigné."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus_windows import act_win32, actions, keys, methods, session, snapshot
from nexus_windows import win32 as w


def _windows(monkeypatch, *items):
    monkeypatch.setattr(snapshot, "surface_window", lambda text: 0)
    monkeypatch.setattr(
        snapshot,
        "app_windows",
        lambda: [SimpleNamespace(hwnd=h, title=t, process=p) for h, t, p in items],
    )
    s = session.Session.__new__(session.Session)
    s.target = 0
    return s


def test_literal_title_wins_over_a_normalized_one(monkeypatch):
    s = _windows(monkeypatch, (12, "my document", "a.exe"), (13, "my  document", "b.exe"))
    assert s._resolve_window("my  document") == 13


def test_non_breaking_space_title_found_with_a_plain_space(monkeypatch):
    s = _windows(monkeypatch, (21, "Propriétés de : note", "explorer.exe"))
    assert s._resolve_window("Propriétés de : note") == 21


def test_process_name_is_matched_as_is(monkeypatch):
    s = _windows(monkeypatch, (31, "Sans rapport", "my  app.exe"))
    assert s._resolve_window("my  app") == 31


def test_a_vanished_field_gets_no_keys(monkeypatch):
    posted: list[int] = []
    monkeypatch.setattr(methods, "post_key", lambda h, vk: posted.append(h))
    monkeypatch.setattr(methods, "keyboard_target", lambda top, windows=None: 222)
    monkeypatch.setattr(w, "IsWindow", lambda h: h != 111)
    ex = SimpleNamespace(hwnd=100, windows={100})
    with pytest.raises(actions.ActionError, match="disappeared"):
        act_win32.Win32Actions._post_combos(ex, keys.parse_sequence("delete"), "Win32", [], own=111)  # type: ignore[arg-type]
    assert posted == []  # jamais au focus d'un autre contrôle


def test_a_partly_sent_sequence_is_never_replayed(monkeypatch):
    posted: list[int] = []
    monkeypatch.setattr(methods, "post_key", lambda h, vk: posted.append(vk))

    def refuse(*args, **kwargs):
        raise methods.MethodError("The target thread is the foreground thread: simulated modifiers refused.")

    monkeypatch.setattr(methods, "post_combo", refuse)
    monkeypatch.setattr(methods, "keyboard_target", lambda top, windows=None: 222)
    ex = SimpleNamespace(hwnd=100, windows={100})
    with pytest.raises(actions.Unconfirmed, match="partly sent"):
        act_win32.Win32Actions._post_combos(ex, keys.parse_sequence("delete ctrl+a"), "Win32", [])  # type: ignore[arg-type]
    assert posted == [0x2E]  # Suppr partie une fois ; Unconfirmed interdit le repli au vrai clavier


def test_a_half_sent_first_key_is_never_replayed(monkeypatch):
    """Relecture de Codex : WM_KEYDOWN accepté puis WM_KEYUP refusé dans la PREMIÈRE touche."""
    answers = iter([True, False])
    monkeypatch.setattr(w, "PostMessageW", lambda *a: next(answers))
    monkeypatch.setattr(methods.ctypes, "get_last_error", lambda: 1400)
    monkeypatch.setattr(methods, "keyboard_target", lambda top, windows=None: 222)
    ex = SimpleNamespace(hwnd=100, windows={100})
    with pytest.raises(actions.Unconfirmed, match="partly sent"):
        act_win32.Win32Actions._post_combos(ex, keys.parse_sequence("delete"), "Win32", [])  # type: ignore[arg-type]


def test_a_reparented_field_gets_no_keys(monkeypatch):
    posted: list[int] = []
    monkeypatch.setattr(methods, "post_key", lambda h, vk: posted.append(h))
    monkeypatch.setattr(methods, "keyboard_target", lambda top, windows=None: 222)
    monkeypatch.setattr(w, "IsWindow", lambda h: True)
    monkeypatch.setattr(methods, "win_class", lambda h: "edit")
    monkeypatch.setattr(w, "root", lambda h: 900)  # sorti de la cible pendant la capture
    monkeypatch.setattr(w, "IsChild", lambda parent, child: False)
    ex = SimpleNamespace(hwnd=100, windows={100})
    with pytest.raises(actions.ActionError, match="left the window"):
        act_win32.Win32Actions._post_combos(ex, keys.parse_sequence("delete"), "Win32", [], own=111)  # type: ignore[arg-type]
    assert posted == []


def test_steps_reach_the_common_journal_without_screen_text(tmp_path):
    import json

    from nexus_windows import journal, log

    with log.Call("act", {"window": "Secret"}) as call:
        call.journal = {"app": "explorer.exe", "cadre": "Win32"}
        call.step(n=1, do="set", target="Mot de passe", ok=True, verified=True, method="EM_REPLACESEL", ms=4)
        call.step(n=2, do="menu", ok=False, error='Menu "Fichier > Ouvrir" not found', ms=9)
        call.step(n=3, do="drag", ok=True, verified=None, method="foreground", ms=200)
    lignes = [json.loads(x) for x in next((tmp_path / "journal-commun" / "windows" / "claude").glob("*.jsonl")).read_text(
        encoding="utf-8").splitlines()]  # fmt: skip
    assert [x["issue"] for x in lignes] == ["✓", "✗", "~"]
    assert lignes[1]["erreur"] == "Menu" and lignes[2]["premier_plan"] is True
    texte = json.dumps(lignes, ensure_ascii=False)
    assert "Mot de passe" not in texte and "Secret" not in texte and "Fichier" not in texte
    assert journal.issue({"ok": False, "error": "emergency stop"}) == "arrêt"


def test_journal_error_keeps_no_screen_names_and_classifies_refusals():
    from nexus_windows import journal

    assert journal.erreur_propre('Option "missing" not found (SCREEN_A, SCREEN_B)') == "Option"
    assert journal.erreur_propre("Can't find 'SECRET'") == "Can"
    assert journal.issue({"ok": False, "error_type": "ConfirmationRequired"}) == "refus"
    assert journal.issue({"ok": False, "error_type": "BusyUser"}) == "différé"
    assert journal.etape("x", {}, {"do": "set", "ok": True, "error": chr(0xD800)}) is None  # jamais d'exception
    assert not journal._FOREGROUND.search("messages without activation")
