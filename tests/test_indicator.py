"""Indicateur « agent au travail » : quels outils l'allument, quand il s'éteint, rendu du logo (sans fenêtre)."""

from __future__ import annotations

import time

import pytest

from nexus_windows import indicator, log


@pytest.fixture
def fresh(monkeypatch):
    """Indicateur neuf dont le fil de dessin n'est jamais lancé (rien à l'écran)."""
    state = indicator.Indicator()
    monkeypatch.setattr(indicator, "INDICATOR", state)
    monkeypatch.setattr(indicator, "enabled", lambda: True)
    monkeypatch.setattr(state, "_run", lambda: None)
    return state


def test_only_acting_tools_light_it(fresh):
    for tool in ("windows", "look", "notifications", "logs"):
        assert indicator.begin(tool) is False
    assert not fresh.busy()
    for tool in ("act", "launch", "window"):
        assert indicator.begin(tool) is True
        indicator.end()
    assert fresh.busy()  # encore visible juste après la dernière action


def test_lingers_then_goes_out(fresh, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(indicator.time, "monotonic", lambda: clock[0])
    indicator.begin("act")
    clock[0] += 30  # action longue : toujours affiché
    assert fresh.busy()
    indicator.end()
    clock[0] += indicator.LINGER_S - 0.1
    assert fresh.busy()
    clock[0] += 0.2
    assert not fresh.busy()


def test_overlapping_calls_keep_it_on(fresh, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(indicator.time, "monotonic", lambda: clock[0])
    indicator.begin("act")
    indicator.begin("launch")
    indicator.end()
    clock[0] += 10
    assert fresh.busy()  # un appel toujours en cours
    indicator.end()
    clock[0] += 10
    assert not fresh.busy()


def test_log_call_drives_it(fresh, monkeypatch):
    with log.Call("look", {}):
        assert not fresh.busy()
    with log.Call("act", {}):
        assert fresh._active == 1
    assert fresh._active == 0 and fresh.busy()
    with pytest.raises(RuntimeError), log.Call("act", {}):
        raise RuntimeError("échec")
    assert fresh._active == 0  # une action en échec éteint aussi


def test_disabled_draws_nothing(monkeypatch):
    state = indicator.Indicator()
    monkeypatch.setattr(indicator, "INDICATOR", state)
    indicator.begin("act")  # NEXUS_WINDOWS_INDICATOR=0 (conftest)
    assert state._thread is None
    indicator.end()


def test_animation_curves():
    assert indicator.angle_at(0) == pytest.approx(0, abs=0.5)
    assert indicator.angle_at(0.5) == pytest.approx(180, abs=1)
    assert indicator.angle_at(0.999) == pytest.approx(360, abs=1)


def test_frames_render_logo_without_halo():
    started = time.perf_counter()
    frames = indicator.render_frames(72, count=8)
    assert time.perf_counter() - started < 10
    side = round(72 * indicator.CANVAS)
    assert len(frames) == 8 and all(len(f) == side * side * 4 for f in frames)
    first, quarter = frames[0], frames[2]  # face, puis tranche (logo vu de profil à 90°)

    def alpha(frame: bytes, x: int, y: int) -> int:
        return frame[(y * side + x) * 4 + 3]

    def opaque_columns(frame: bytes) -> int:
        row = side // 2
        return sum(alpha(frame, x, row) > 200 for x in range(side))

    assert alpha(first, side // 2, side // 2) == 255  # logo plein au centre
    assert alpha(first, 0, 0) == 0  # coins transparents : seul le logo se voit
    assert 60 <= opaque_columns(first) <= 80  # diamètre ~72 px
    assert opaque_columns(quarter) < opaque_columns(first)  # il tourne sur lui-même
    # Plus de halo bleu-violet (Julien, 22/10) : au-dessus du logo, au plus l'ombre noire, jamais de couleur.
    at = ((side // 2 - 40) * side + side // 2) * 4
    b, g, r, a = first[at : at + 4]
    assert b == g == r == 0


def test_logo_asset_shipped():
    assert indicator.LOGO.is_file()


def test_imports_off_windows():
    """Démarrage hors Windows (conteneur Linux des annuaires) : ni WINFUNCTYPE ni WinDLL, le module se charge quand même."""
    import subprocess
    import sys

    code = (
        "import ctypes, os; del ctypes.WINFUNCTYPE; del ctypes.WinDLL; os.name = 'posix'; "
        "from nexus_windows import indicator; assert not indicator.enabled()"
    )
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
