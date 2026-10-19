"""Revue croisée du 16/10 : chaque défaut confirmé a son test, sans fenêtre réelle ni vraie entrée (Win32 simulé).

1. jeton commun : imbrication comptée par fil (test_agentlock.test_another_thread_waits_like_another_agent) ;
2. premier plan rendu à l'ancienne fenêtre seulement si la cible le tient encore (emprunt et lot au premier plan) ;
3. élément défilé hors vue dont ScrollIntoView échoue : refus, jamais le centre de l'ancien rectangle ;
4. postcondition sur un nom partiel : issue inconnue, jamais « déjà fait » ;
5. remember après un rejeu : le modèle {{x}}, jamais les valeurs du jour ; paramètre inconnu refusé ;
6. texte tapé d'une routine : seulement des modèles {{x}}, aucun texte fixe ;
7. {{x}} seulement dans les champs de texte ; étapes remplies revérifiées (jamais un clic par point) ;
8. vraie souris : déplacement et boutons en un seul SendInput ; entrées de Julien vues entre deux lots (Raw Input,
   sinon GetLastInputInfo hors de nos entrées) ;
9. garde de compte : pages DevTools à clé (hwnd, titre) oubliées sans erreur interne ;
10. arrêt d'urgence : version du fichier notée vue et arrêt posé d'un seul bloc.
"""

from __future__ import annotations

import ctypes
import json

import pytest
from test_contract import HWND, FakeAuto, executor, make_view, node
from test_fix_b import JULIEN, OTHER, TARGET, Desk
from test_fix_G2 import _sign_in, tracked  # noqa: F401  (tracked : fixture)

from nexus_windows import actions, foreground, launch, physical, postcond, realinput, routines, server, stop, uia
from nexus_windows import win32 as w

FP = {"path": "C:/app/test.exe", "size": 10, "mtime": 1}


# --- 2. Premier plan rendu seulement si la cible le tient encore -------------------------------------------------
@pytest.fixture
def desk(monkeypatch):
    desk = Desk(monkeypatch)

    def front(hwnd):
        desk.fg = hwnd
        return True

    monkeypatch.setattr(realinput, "bring_to_front", front)
    monkeypatch.setattr(realinput.worker, "check_cancelled", lambda: None)
    monkeypatch.setattr(foreground, "adopt_foreground", lambda: None)
    monkeypatch.setattr(realinput.time, "sleep", lambda s: None)
    return desk


def test_borrowed_gives_back_while_the_target_still_holds_the_foreground(desk):
    with realinput._borrowed(TARGET, {TARGET}):
        assert desk.fg == TARGET
    assert desk.gave_back == [JULIEN] and desk.fg == JULIEN


def test_borrowed_never_imposes_the_old_window_once_julien_took_a_third_one(desk):
    with realinput._borrowed(TARGET, {TARGET}):
        desk.fg = OTHER  # Julien est passé dans une autre de ses fenêtres pendant l'emprunt
    assert desk.gave_back == [] and desk.fg == OTHER


def test_held_never_imposes_the_old_window_once_julien_took_a_third_one(desk):
    with realinput._held(TARGET, {TARGET}):
        desk.fg = OTHER
    assert desk.gave_back == [] and desk.fg == OTHER


# --- 8. Vraie souris et vrai clavier ------------------------------------------------------------------------------
@pytest.fixture
def sent(desk, monkeypatch):
    """Entrées « envoyées » relevées par lot (jamais transmises à Windows) ; bureau virtuel 0..1001 × 0..1001."""
    batches: list[list[tuple[int, int, int, int]]] = []

    def send(*inputs):
        batches.append([(i.mi.dwFlags, i.mi.dx, i.mi.dy, i.mi.mouseData) for i in inputs])

    monkeypatch.setattr(realinput, "_send", send)
    metrics = {w.SM_XVIRTUALSCREEN: 0, w.SM_YVIRTUALSCREEN: 0, w.SM_CXVIRTUALSCREEN: 1002, w.SM_CYVIRTUALSCREEN: 1002}
    monkeypatch.setattr(w, "GetSystemMetrics", lambda index: metrics[index])
    monkeypatch.setattr(w, "root", lambda h: h)
    desk.over = TARGET  # le point vise la cible
    return batches


ABS = w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE | w.MOUSEEVENTF_VIRTUALDESK


def test_click_moves_and_presses_in_a_single_sendinput_at_the_point(sent):
    with realinput._borrowed(TARGET, {TARGET}):
        realinput.click(100, 200, count=2)
    assert len(sent) == 1  # rien ne peut s'intercaler entre le déplacement et les boutons
    batch = sent[0]
    buttons = [flags & ~ABS for flags, *_ in batch]
    assert buttons == [0, w.MOUSEEVENTF_LEFTDOWN, w.MOUSEEVENTF_LEFTUP, w.MOUSEEVENTF_LEFTDOWN, w.MOUSEEVENTF_LEFTUP]
    assert all(flags & ABS == ABS for flags, *_ in batch)  # chaque événement porte le point absolu
    assert {(dx, dy) for _, dx, dy, _ in batch} == {realinput._absolute(100, 200)}


def test_wheel_carries_the_point_in_the_same_sendinput(sent):
    with realinput._borrowed(TARGET, {TARGET}):
        realinput.wheel(10, 20, -120)
    assert len(sent) == 1 and [flags & ~ABS for flags, *_ in sent[0]] == [0, w.MOUSEEVENTF_WHEEL]
    assert sent[0][1][1:3] == realinput._absolute(10, 20)


def test_typing_stops_at_julien_physical_input_between_batches(sent, monkeypatch):
    with pytest.raises(foreground.UserInterrupted, match="The user used the keyboard"):
        with realinput._borrowed(TARGET, {TARGET}):
            realinput.type_text("ab")
            monkeypatch.setattr(physical, "since", lambda moment: True)  # Julien tape au milieu
            realinput.type_text("cd")
    assert len(sent) == 2  # « a », « b » ; rien après son entrée


def test_without_raw_input_last_input_info_minus_own_injections_counts(monkeypatch):
    ticks = {"now": 2000, "last": 1000}
    monkeypatch.setattr(physical, "available", lambda: False)
    monkeypatch.setattr(w, "GetTickCount", lambda: ticks["now"])
    monkeypatch.setattr(w, "last_input_tick", lambda: ticks["last"])
    monkeypatch.setattr(w, "foreground", lambda: TARGET)
    monkeypatch.setattr(w, "root", lambda h: h)
    monkeypatch.setattr(foreground, "_OWN_INPUT", (0, 0))
    ticks["now"] = 1000
    hold = realinput.Hold(TARGET, {TARGET}, JULIEN, (0, 0))  # début au tic 1000
    ticks["now"] = 1600
    hold.checkpoint()  # aucune entrée depuis : le lot continue
    ticks["now"], ticks["last"] = 1500, 1500
    foreground.mark_own_input()  # notre propre SendInput au tic 1500
    ticks["now"] = 1700
    hold.checkpoint()  # notre entrée n'est pas Julien
    ticks["last"] = 1690  # entrée hors de nos injections : Julien
    with pytest.raises(foreground.UserInterrupted):
        hold.checkpoint()
    assert hold.user_took_over


# --- 3. Élément défilé hors vue -----------------------------------------------------------------------------------
class NoScroll(FakeAuto):
    def scroll_into_view(self, element) -> None:
        raise uia.UIAError("ScrollIntoView refusé")


def test_offscreen_element_whose_scroll_fails_is_refused_not_clicked_at_its_old_center():
    row = node("ListItem", "Ligne 40", offscreen=True, rect=(10, 900, 190, 20))
    ex = executor(make_view(row), NoScroll())
    with pytest.raises(actions.ActionError, match="outside the visible area"):
        ex._point_of(ex.view.elements[0])


def test_element_brought_into_view_is_aimed_at_its_visible_part(monkeypatch):
    class Scrolls(FakeAuto):
        def scroll_into_view(self, element) -> None:
            element.live.update(offscreen=False, rect=(350, 10, 100, 20))  # à cheval sur le bord droit (x, y, l, h)

    row = node("ListItem", "Ligne 40", offscreen=True, rect=(10, 900, 190, 20))
    ex = executor(make_view(row), Scrolls())
    monkeypatch.setattr(w, "IsWindow", lambda h: h == HWND)
    monkeypatch.setattr(w, "frame_rect", lambda h: w.Rect(0, 0, 400, 300))
    assert ex._point_of(ex.view.elements[0]) == (375, 20)  # centre de la partie visible, pas (400, 20)


# --- 4. Postcondition sur un nom partiel --------------------------------------------------------------------------
def test_partial_name_in_a_check_is_unknown_never_already_done():
    box = node("CheckBox", "Newsletter partenaires", has_toggle=True, toggle=1)
    ex = executor(make_view(box), FakeAuto())
    verdict = postcond.evaluate(ex, {"target": "Newsletter", "checked": True})
    assert verdict.state is None and "partial match" in verdict.detail
    exact = postcond.evaluate(ex, {"target": "Newsletter partenaires", "checked": True})
    assert exact.state is True


# --- 5. remember après un rejeu : le modèle d'origine -----------------------------------------------------------
def test_replayed_routine_is_remembered_as_its_template():
    routines.remember("test.exe", "r", [{"do": "set", "target": "Nom", "text": "{{nom}}"}], FP, "T")
    todo, template = server._planned("test.exe", FP, None, "r", {"nom": "Julien"}, [])
    assert todo[0].text == "Julien" and template[0].text == "{{nom}}"
    assert "saved" in server._remember("test.exe", FP, "T", "copie", template, ["✓"], False)
    saved = json.loads((routines.ROOT / "test.exe.json").read_text(encoding="utf-8"))
    assert saved["routines"]["copie"]["steps"][0]["text"] == "{{nom}}"
    assert "Julien" not in json.dumps(saved, ensure_ascii=False)


def test_unknown_params_are_refused():
    routines.remember("test.exe", "r", [{"do": "set", "target": "Nom", "text": "{{nom}}"}], FP, "T")
    entry = routines.get("test.exe", "r")
    with pytest.raises(routines.RoutineError, match="Unknown parameters: nmo"):
        routines.expand(entry, {"nom": "Julien", "nmo": "faute"})
    with pytest.raises(actions.ActionError, match="Unknown parameters"):
        server._filled([server.Step(do="click", target="OK")], {"x": "1"})


# --- 6. Texte tapé d'une routine : seulement des modèles --------------------------------------------------------
@pytest.mark.parametrize("text", ["Mon IBAN FR76 3000 {{nom}}", "Julien", "{{a}}-{{b}}"])
def test_typed_text_with_fixed_parts_is_refused(text):
    with pytest.raises(routines.RoutineError, match="fixed typed text"):
        routines.remember("test.exe", "r", [{"do": "type", "target": "Note", "text": text}], FP, "T")
    assert routines.names("test.exe") == []


def test_template_only_typed_text_is_accepted_and_mixed_checks_stay_local():
    routines.remember("test.exe", "r", [{"do": "type", "target": "Note", "text": " {{a}} {{b}} "}], FP, "T")
    mixed = {"steps": [{"do": "click", "target": "OK", "check": {"target": "Note", "value": "IBAN {{x}}"}}]}
    assert routines.plain_text(mixed) == "step 1, check.value"  # jamais copié dans le miroir


# --- 7. {{x}} seulement dans les champs de texte ; étapes remplies revérifiées ----------------------------------
@pytest.mark.parametrize(
    "step",
    [
        {"do": "click", "target": "{{b}}"},
        {"do": "drag", "target": "Fichier", "to": "{{b}}"},
        {"do": "key", "keys": "{{k}}"},
        {"do": "click", "target": "OK", "check": {"target": "{{b}}", "checked": True}},
    ],
)
def test_params_outside_text_fields_are_refused_at_remember(step):
    with pytest.raises(routines.RoutineError, match="allowed only in text"):
        routines.remember("test.exe", "r", [step], FP, "T")


def test_stored_routine_filled_into_a_point_is_refused(monkeypatch):
    """Routine d'avant la règle (ou reprise du miroir) : « {{b}} » rempli en « 120,340 » ne devient jamais un clic
    par point."""
    folder = routines.ROOT
    folder.mkdir(parents=True, exist_ok=True)
    entry = {"steps": [{"do": "click", "target": "{{b}}"}], "fingerprint": FP, "saved": "2026-10-16T10:00:00"}
    data = {"schema": 1, "app": "test.exe", "routines": {"r": entry}}
    (folder / "test.exe.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(actions.ActionError, match="Routine refused"):
        server._planned("test.exe", FP, None, "r", {"b": "120,340"}, [])
    monkeypatch.setattr(routines, "misplaced", lambda steps: "")  # même sans la règle des champs : refus après
    with pytest.raises(routines.RoutineError, match="after filling in the parameters.*point"):
        routines.expand(routines.get("test.exe", "r"), {"b": "120,340"})


# --- 9. Garde de compte : clés (hwnd, titre) ----------------------------------------------------------------------
class FakePage:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_account_guard_forgets_pages_keyed_by_hwnd_and_title(tracked, monkeypatch):  # noqa: F811
    sess, profile, state = tracked

    def is_window(hwnd):  # comme ctypes : un tuple n'est pas un HWND
        if not isinstance(hwnd, int):
            raise ctypes.ArgumentError("argument 1: TypeError: wrong type")
        return hwnd != 5555

    monkeypatch.setattr(w, "IsWindow", is_window)
    titled, gone, alive = FakePage(), FakePage(), FakePage()
    sess.pages[(4242, "Magasin")] = titled
    sess.pages[5555] = gone  # fenêtre fermée
    sess.pages[(6666, "Autre")] = alive
    _sign_in(profile)
    with pytest.raises(launch.AccountError):
        sess.check_account(4242)
    assert titled.closed and gone.closed and not alive.closed
    assert list(sess.pages) == [(6666, "Autre")]


# --- 10. Arrêt d'urgence : vu et posé d'un seul bloc ------------------------------------------------------------
def test_a_reader_in_the_middle_of_a_new_stop_version_still_sees_the_stop(monkeypatch):
    """Un autre fil qui lit le fichier pendant qu'un premier pose l'arrêt ne voit jamais « version déjà vue, pas
    d'arrêt » : l'arrêt est posé dans le même bloc que la version notée vue."""
    stop.STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    stop.STOP_FILE.write_text("appui", encoding="utf-8")
    real = stop.time.time
    seen: list[float] = []

    class Clock:
        def __getattr__(self, name):
            return getattr(__import__("time"), name)

        @staticmethod
        def time():
            if not seen:
                seen.append(-1.0)
                seen[0] = stop.remaining_s()  # lecture d'un autre fil, au milieu du traitement du premier
            return real()

    monkeypatch.setattr(stop, "time", Clock())
    assert stop.remaining_s() > 0
    assert seen and seen[0] > 0
