"""Vérification finale (recheck8) : arrêt d'urgence robuste à un fichier illisible, programme renommé refusé au
lancement, premier plan gardé pendant l'attente du contenu d'une application Store."""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest
from test_contract import as_auto
from test_fix_G2 import JULIEN, TARGET, _store_launch

from nexus_windows import launch, policy, stop, worker

SYSTEM32 = Path(r"C:\Windows\System32")


@pytest.fixture
def stop_file(tmp_path, monkeypatch):
    monkeypatch.setattr(stop, "STOP_FILE", tmp_path / "stop")
    monkeypatch.setattr(stop.log, "event", lambda *a, **k: None)
    return tmp_path / "stop"


@pytest.mark.parametrize(
    "content", ["arrêt".encode("utf-16"), b"\xff\xfe\x00\xd8\x80", b"\x80\x81\x82"], ids=["utf-16", "bom", "octets"]
)
def test_unreadable_stop_file_neither_breaks_tools_nor_lifts_the_stop(stop_file, content):
    """Un fichier d'arrêt écrit par « > » de Windows PowerShell 5.1 (UTF-16) ou corrompu : avant, UnicodeDecodeError
    dans chaque outil et fil de surveillance tué. Désormais lu en octets : l'arrêt est posé, et le reste après rm."""
    stop_file.write_bytes(content)
    assert stop.remaining_s() > 0
    stop_file.unlink()
    with pytest.raises(worker.Stopped):
        worker.check_cancelled()


def test_watch_thread_survives_any_error(stop_file, monkeypatch):
    calls = {"n": 0}

    def flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("panne imprévue")

    monkeypatch.setattr(stop, "_read_file", flaky)
    monkeypatch.setattr(stop, "WATCH_S", 0.01)
    done = threading.Event()
    watcher = threading.Thread(target=stop._watch, args=(done,), daemon=True)
    watcher.start()
    deadline = time.monotonic() + 2
    while calls["n"] < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    alive = watcher.is_alive()
    done.set()
    watcher.join(2)
    assert calls["n"] >= 3 and alive  # l'erreur est journalisée, le fil continue


@pytest.mark.parametrize(("source", "suffix"), [("findstr.exe", ".bin"), ("notepad.exe", ".dat")])
def test_renamed_program_is_allowed(tmp_path, source, suffix):
    """Accord du 18/10 : un programme renommé n'est plus refusé (le shell de Claude fait déjà tout)."""
    original = SYSTEM32 / source
    if not original.exists():
        pytest.skip(f"{original} absent")
    copy = tmp_path / f"renomme{suffix}"
    shutil.copyfile(original, copy)
    policy.check_launch_exe(str(copy), "")


def test_steal_during_wait_content_is_given_back_at_once(monkeypatch):
    """recheck8 : un vol du premier plan pendant wait_content (jusqu'à 3 s) n'était rendu qu'à la fin de launch."""
    fg = {"hwnd": JULIEN}
    gave = _store_launch(monkeypatch, fg, give_back_ok=True)
    monkeypatch.setattr(launch, "uwp_unshown", lambda hwnd: False)
    steal: dict = {}

    def wait_content(auto, hwnd, limit_s=3.0):
        fg["hwnd"] = TARGET  # l'application prend le premier plan pendant l'attente
        steal["at"] = time.monotonic()
        deadline = time.monotonic() + 1.0
        while fg["hwnd"] != JULIEN and time.monotonic() < deadline:
            time.sleep(0.005)
        steal["back"] = time.monotonic()
        return True

    monkeypatch.setattr(launch, "wait_content", wait_content)
    result = launch.launch("Calculatrice", auto=as_auto(object()))
    assert gave and gave[-1] == JULIEN and result.returned
    assert steal["back"] - steal["at"] < 0.2  # rendu pendant l'attente, pas à la fin du lancement
    assert any("given back to the user at once" in n for n in result.notes)
