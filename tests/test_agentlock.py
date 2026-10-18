"""Jeton commun aux agents (agentlock) : exclusion entre handles, libération garantie, diagnostic, garde."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest
from test_fix_b import JULIEN, TARGET, Desk

from nexus_windows import agentlock, foreground, realinput


@contextmanager
def other_agent(owner: str = "NexusAGY", purpose: str = "computer_act"):
    """Un autre agent tient le jeton : son propre handle, son propre verrou (comme un autre processus)."""
    handle = agentlock._open()
    assert agentlock._try_lock(handle)
    agentlock._write(handle, json.dumps({"owner": owner, "pid": 4242, "since": time.time() - 2, "purpose": purpose}))
    try:
        yield
    finally:
        agentlock._release(handle)


def test_hold_writes_holder_and_frees_on_exit():
    with agentlock.hold("activation brève"):
        info = json.loads(agentlock.PATH.read_text(encoding="utf-8"))
        assert info["owner"] == "NexusClaude" and info["purpose"] == "activation brève"
        assert agentlock.other_holder() is None  # tenu par nous : personne d'autre
    assert agentlock.PATH.read_text(encoding="utf-8") == ""
    handle = agentlock._open()
    try:
        assert agentlock._try_lock(handle)  # libre pour les autres
    finally:
        agentlock._release(handle)


def test_other_holder_is_seen_and_described():
    assert agentlock.other_holder() is None
    with other_agent():
        holder = agentlock.other_holder()
        assert holder is not None and holder["owner"] == "NexusAGY"
        assert agentlock.describe(holder).startswith("NexusAGY tient la souris et le premier plan depuis ")
        assert "(computer_act)" in agentlock.describe(holder)
    assert agentlock.other_holder() is None


def test_busy_after_wait_and_nothing_left_open():
    checks = []
    with other_agent():
        with pytest.raises(agentlock.Busy, match="NexusAGY tient la souris"):
            with agentlock.hold("test", wait_s=0.2, checkpoint=lambda: checks.append(1)):
                pytest.fail("bloc exécuté sans le jeton")
    assert checks  # attente contrôlée (arrêt d'urgence, appel abandonné)
    with agentlock.hold("après"):  # aucun handle orphelin ne bloque la suite
        pass


def test_wait_ends_as_soon_as_the_other_agent_releases():
    handle = agentlock._open()
    assert agentlock._try_lock(handle)
    released = []

    def release_later():
        if len(released) == 2:
            agentlock._release(handle)
        released.append(1)

    with agentlock.hold("test", wait_s=5, checkpoint=release_later):
        assert agentlock.PATH.read_text(encoding="utf-8")


def test_checkpoint_error_stops_the_wait():
    class Cancelled(Exception):
        pass

    def cancel():
        raise Cancelled

    with other_agent(), pytest.raises(Cancelled):
        with agentlock.hold("test", checkpoint=cancel):
            pytest.fail("bloc exécuté sans le jeton")
    with agentlock.hold("après"):
        pass


def test_reentrant_inside_the_process():
    with agentlock.hold("lot"):
        with agentlock.hold("activation brève"):
            assert agentlock._depth() == 2
        assert agentlock._depth() == 1 and json.loads(agentlock.PATH.read_text(encoding="utf-8"))["purpose"] == "lot"
    assert agentlock._depth() == 0


def test_another_thread_waits_like_another_agent():
    """Appel abandonné (délai dépassé) dont le fil tient encore le jeton, puis appel suivant dans un autre fil : le
    nouveau attend le verrou de fichier au lieu de s'imbriquer ; quand l'ancien le rend, le nouveau tient le vrai
    verrou jusqu'au bout, et personne ne finit avec une profondeur négative."""
    inside, leave = threading.Event(), threading.Event()

    def abandoned():
        with agentlock.hold("appel abandonné"):
            inside.set()
            leave.wait(5)

    thread = threading.Thread(target=abandoned)
    thread.start()
    try:
        assert inside.wait(5)
        with pytest.raises(agentlock.Busy, match="NexusClaude tient la souris"):
            with agentlock.hold("appel suivant", wait_s=0.2):
                pytest.fail("deux fils dans le jeton en même temps")
        with agentlock.hold("appel suivant", wait_s=5, checkpoint=leave.set):
            thread.join(5)
            assert not thread.is_alive() and agentlock._depth() == 1
            handle = agentlock._open()
            try:
                assert not agentlock._try_lock(handle)  # vrai verrou toujours tenu après la sortie de l'ancien fil
            finally:
                agentlock._CloseHandle(handle)
            assert agentlock.other_holder() is None
        assert agentlock._depth() == 0 and agentlock._owners == 0
        with agentlock.hold("après"):  # le verrou réel est encore pris par la suite
            handle = agentlock._open()
            try:
                assert not agentlock._try_lock(handle)
            finally:
                agentlock._CloseHandle(handle)
        with other_agent():
            assert agentlock.other_holder() is not None  # un autre agent reste visible
    finally:
        leave.set()
        thread.join(5)


def test_released_when_the_holder_process_dies(tmp_path):
    """Garantie du système : un agent qui meurt jeton en main ne le garde pas (aucun jeton périmé). Windows libère les
    verrous d'un processus terminé de façon asynchrone (« depends upon available system resources », doc LockFileEx) :
    peu après sa fin, pas à l'instant où wait() rend la main (mesuré : parfois encore tenu juste après)."""
    code = (
        "import sys, time; from pathlib import Path; from nexus_windows import agentlock;"
        f"agentlock.PATH = Path({str(agentlock.PATH)!r}); h = agentlock._open();"
        "assert agentlock._try_lock(h); print('held', flush=True); time.sleep(30)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    try:
        assert proc.stdout.readline().strip() == "held"
        assert agentlock.other_holder() is not None
        proc.kill()
        proc.wait(timeout=10)
        deadline = time.monotonic() + agentlock.WAIT_S  # ce qu'attendrait un autre agent avant de reporter
        while agentlock.other_holder() is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert agentlock.other_holder() is None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_brief_activation_waits_for_the_token(monkeypatch):
    monkeypatch.setattr(agentlock, "WAIT_S", 0.1)
    monkeypatch.setattr(foreground, "_checkpoint", lambda: None)
    reached = []
    monkeypatch.setattr(foreground, "wait_typing_pause", lambda *a, **k: reached.append(1) or False)
    with other_agent():
        with pytest.raises(foreground.BusyUser, match="NexusAGY tient la souris"):
            with foreground.brief_activation(TARGET):
                pytest.fail("activation sans le jeton")
    assert reached == []  # rien n'est tenté tant qu'un autre agent tient souris et premier plan


def test_borrowed_waits_for_the_token(monkeypatch):
    monkeypatch.setattr(agentlock, "WAIT_S", 0.1)
    monkeypatch.setattr(foreground, "_checkpoint", lambda: None)
    monkeypatch.setattr(foreground, "wait_typing_pause", lambda *a, **k: pytest.fail("emprunt tenté"))
    with other_agent(), pytest.raises(foreground.BusyUser):
        with realinput.borrowed(TARGET, {TARGET}):
            pytest.fail("vraie souris sans le jeton")


def test_watch_follows_a_foreground_given_by_another_agent(monkeypatch):
    desk = Desk(monkeypatch)
    watch = foreground.Watch(TARGET)
    desk.fg = TARGET
    with other_agent():
        watch._check()
    assert desk.gave_back == [] and watch.stolen == 0 and watch.front == TARGET


def test_watch_still_gives_back_a_steal_without_other_agent(monkeypatch):
    desk = Desk(monkeypatch)
    watch = foreground.Watch(TARGET)
    desk.fg = TARGET
    watch._check()
    assert desk.gave_back == [JULIEN] and watch.stolen == 1
