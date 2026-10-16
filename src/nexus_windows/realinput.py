"""Vraie souris et vrai clavier (SendInput) : uniquement pour les actions que Julien a autorisées (foreground=True).

La fenêtre cible passe devant, l'action est jouée, puis le curseur revient à sa place et le premier plan à la
fenêtre de Julien. Refus net si Windows ne met pas la cible devant (sinon les entrées iraient ailleurs).
"""

from __future__ import annotations

import ctypes
import time
from contextlib import contextmanager

from . import foreground, keys, physical, stop, worker
from . import win32 as w


class ForegroundError(RuntimeError):
    pass


_LAST_MOVE: tuple[int, int] | None = None  # dernière position où NOUS avons mis le curseur
_ALLOWED: set[int] = set()  # fenêtres de la cible pendant un emprunt autorisé
_TOKEN = None  # suspension de la garde, renouvelée lot par lot


def _guard(x: int | None = None, y: int | None = None) -> None:
    """Avant chaque lot d'entrées réelles : la cible doit être devant, et le point viser une de ses fenêtres.
    Sinon (Julien a repris la main, une autre fenêtre s'est intercalée) on s'arrête au lieu d'écrire chez lui."""
    if not _ALLOWED:
        raise ForegroundError("Entrée réelle hors d'un emprunt autorisé : refusée.")
    if stop.remaining_s() > 0:
        raise ForegroundError(stop.message())
    if w.root(w.foreground()) not in _ALLOWED:
        raise ForegroundError(
            "La cible n'est plus au premier plan (Julien a repris la main ?) : arrêt, rien d'autre envoyé."
        )
    if x is not None and y is not None and w.window_at(x, y) not in _ALLOWED:
        raise ForegroundError(f"Le point ({x}, {y}) ne vise pas la fenêtre cible (recouverte ?) : arrêt.")
    if _TOKEN is not None:
        foreground.renew(_TOKEN)


def _send(*inputs: w.INPUT) -> None:
    foreground.mark_own_input()
    array = (w.INPUT * len(inputs))(*inputs)
    if w.SendInput(len(inputs), array, ctypes.sizeof(w.INPUT)) != len(inputs):
        raise ForegroundError("Windows a refusé l'entrée simulée (écran verrouillé, UAC ou bureau sécurisé).")


def _key(vk: int, up: bool = False, scan: int = 0, unicode: bool = False) -> w.INPUT:
    flags = (w.KEYEVENTF_KEYUP if up else 0) | (w.KEYEVENTF_UNICODE if unicode else 0)
    if not unicode and vk in keys.EXTENDED:
        flags |= w.KEYEVENTF_EXTENDEDKEY
    item = w.INPUT(type=w.INPUT_KEYBOARD)
    item.ki = w.KEYBDINPUT(0 if unicode else vk, scan, flags, 0, 0)
    return item


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> w.INPUT:
    item = w.INPUT(type=w.INPUT_MOUSE)
    item.mi = w.MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return item


def _absolute(x: int, y: int) -> tuple[int, int]:
    left, top = w.GetSystemMetrics(w.SM_XVIRTUALSCREEN), w.GetSystemMetrics(w.SM_YVIRTUALSCREEN)
    width, height = w.GetSystemMetrics(w.SM_CXVIRTUALSCREEN), w.GetSystemMetrics(w.SM_CYVIRTUALSCREEN)
    return round((x - left) * 65535 / max(1, width - 1)), round((y - top) * 65535 / max(1, height - 1))


def bring_to_front(hwnd: int) -> bool:
    """Met LA cible devant (restaurée si réduite) ; jamais une autre fenêtre à sa place. Arrêt d'urgence contrôlé
    juste avant (foreground.activate)."""
    if not w.IsWindow(hwnd):
        return False
    if w.IsIconic(hwnd):
        worker.check_cancelled()
        w.ShowWindow(hwnd, w.SW_RESTORE)
        time.sleep(0.15)
    return foreground.activate(hwnd, timeout_s=1.0) and w.root(w.foreground()) == hwnd


def put_in_front(hwnd: int) -> bool:
    """window(action="front"), autorisé par Julien : comme un emprunt, pause de Julien attendue (sinon BusyUser) et
    arrêt d'urgence contrôlé juste avant ; la cible passe devant sous suspension de la garde, et les surveillances en
    cours (traîne d'un appel précédent comprise) adoptent ce premier plan voulu au lieu de le rendre. Jeton commun aux
    agents tenu le temps de la mise au premier plan."""
    with foreground.agent_token("fenêtre mise devant"):
        if not foreground.wait_typing_pause():
            raise foreground.BusyUser("Julien utilise la souris ou le clavier : mise au premier plan reportée.")
        worker.check_cancelled()
        with foreground.suspended():
            if not bring_to_front(hwnd):
                return False
            foreground.adopt_foreground()
    return True


def _holder() -> str:
    """Qui tient le premier plan, pour un refus compréhensible."""
    fg = w.foreground()
    return foreground.system_foreground() or (
        f"premier plan tenu par « {w.window_text(fg) or w.class_name(fg)} » ({w.process_name(w.window_pid(fg))})"
        if fg
        else "aucune fenêtre au premier plan"
    )


class Hold:
    """Lot d'étapes avec foreground=true accordé : la cible reste au premier plan du début à la fin du lot (rendu
    final compris), où Chromium/CEF la dessine et publie son arbre ; premier plan et curseur rendus à la fin."""

    def __init__(self, hwnd: int, windows: set[int], previous: int, cursor: tuple[int, int]) -> None:
        self.hwnd = hwnd
        self.windows = windows
        self.previous = previous
        self.cursor = cursor
        self.started = time.monotonic()
        self.user_took_over = False

    def checkpoint(self) -> None:
        """Entre deux étapes : Julien a-t-il repris la main (touche, clic, molette) ? Alors le lot s'arrête et le
        premier plan lui reste. Sinon, la cible est remise devant si une autre fenêtre s'est intercalée."""
        if physical.available() and physical.since(self.started):
            self.user_took_over = True
            raise foreground.UserInterrupted(
                "Julien a utilisé le clavier ou la souris pendant le lot au premier plan : lot arrêté, premier plan "
                "laissé à Julien"
            )
        if w.root(w.foreground()) not in self.windows:
            worker.check_cancelled()
            with foreground.suspended():
                if bring_to_front(self.hwnd):
                    foreground.adopt_foreground()


_HOLD: Hold | None = None


def current_hold() -> Hold | None:
    return _HOLD


@contextmanager
def held(hwnd: int, windows: set[int] | None = None):
    """foreground=true accordé : la cible passe devant (après une pause de Julien) et y reste pendant tout le lot ;
    les surveillances du premier plan l'adoptent au lieu de le « rendre ». À la sortie (fin, erreur, arrêt
    d'urgence), premier plan rendu à la fenêtre de Julien (sauf s'il a repris la main lui-même) et curseur remis si
    nous l'avons déplacé et qu'il n'a pas bougé depuis. Jeton commun aux agents tenu pendant tout le lot."""
    with foreground.agent_token("lot au premier plan"), _held(hwnd, windows) as hold:
        yield hold


@contextmanager
def _held(hwnd: int, windows: set[int] | None):
    global _HOLD, _LAST_MOVE
    if not foreground.wait_typing_pause():
        raise foreground.BusyUser(
            "Julien utilise la souris ou le clavier : lot au premier plan reporté, rien n'a été fait."
        )
    worker.check_cancelled()
    previous = w.foreground()
    allowed = set(windows or foreground.target_windows(hwnd))
    hold = Hold(hwnd, allowed, previous, w.cursor_pos())
    _LAST_MOVE = None
    with foreground.suspended():
        if not bring_to_front(hwnd):
            raise ForegroundError(f"Windows refuse de mettre la fenêtre devant ({_holder()}) : rien n'a été fait.")
        foreground.adopt_foreground()
    _HOLD = hold
    try:
        yield hold
    finally:
        _HOLD = None
        time.sleep(0.05)
        if _LAST_MOVE is not None and w.cursor_pos() == _LAST_MOVE:
            w.SetCursorPos(*hold.cursor)
        if not hold.user_took_over and previous and previous not in allowed:
            with foreground.suspended():
                foreground.give_back(previous, exclude=allowed)
                foreground.adopt_foreground()


@contextmanager
def borrowed(hwnd: int, windows: set[int] | None = None):
    """Met la cible devant le temps du bloc (après une pause de Julien), puis rend curseur et premier plan.

    Chaque lot d'entrées vérifie d'abord que la cible est toujours devant (et le point sur elle) ; la garde est
    suspendue lot par lot. Le curseur n'est remis en place que si nous l'avons déplacé et que Julien ne l'a pas
    bougé depuis. Jeton commun aux agents tenu pendant tout l'emprunt.
    """
    with foreground.agent_token("vraie souris ou vrai clavier"), _borrowed(hwnd, windows):
        yield


@contextmanager
def _borrowed(hwnd: int, windows: set[int] | None):
    global _LAST_MOVE, _ALLOWED, _TOKEN
    if not foreground.wait_typing_pause():
        raise foreground.BusyUser("Julien utilise la souris ou le clavier : emprunt reporté, rien n'a été envoyé.")
    worker.check_cancelled()  # arrêt d'urgence demandé pendant l'attente : rien n'est mis devant
    previous = w.foreground()
    cursor = w.cursor_pos()
    _LAST_MOVE = None
    allowed = windows or foreground.target_windows(hwnd)
    with foreground.suspended() as token:
        _TOKEN = token
        try:
            if not bring_to_front(hwnd):
                raise ForegroundError(
                    f"Windows refuse de mettre la fenêtre devant ({_holder()}) : rien n'a été envoyé."
                )
            _ALLOWED = set(allowed)
            yield
        finally:
            _ALLOWED = set()
            _TOKEN = None
            time.sleep(0.05)
            if _LAST_MOVE is not None and w.cursor_pos() == _LAST_MOVE:
                w.SetCursorPos(*cursor)
            if previous and previous not in allowed:
                foreground.give_back(previous, exclude=set(allowed))


def move(x: int, y: int, check_point: bool = True) -> None:
    global _LAST_MOVE
    _guard(x, y) if check_point else _guard()
    ax, ay = _absolute(x, y)
    _send(_mouse(w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE | w.MOUSEEVENTF_VIRTUALDESK, ax, ay))
    _LAST_MOVE = w.cursor_pos()


_BTN = {
    "left": (w.MOUSEEVENTF_LEFTDOWN, w.MOUSEEVENTF_LEFTUP),
    "right": (w.MOUSEEVENTF_RIGHTDOWN, w.MOUSEEVENTF_RIGHTUP),
    "middle": (w.MOUSEEVENTF_MIDDLEDOWN, w.MOUSEEVENTF_MIDDLEUP),
}


def click(x: int, y: int, button: str = "left", count: int = 1) -> None:
    move(x, y)
    time.sleep(0.03)
    down, up = _BTN[button]
    for _ in range(count):
        _send(_mouse(down), _mouse(up))
        time.sleep(0.04)


def drag(x1: int, y1: int, x2: int, y2: int, steps: int = 12) -> None:
    move(x1, y1)
    time.sleep(0.05)
    _send(_mouse(w.MOUSEEVENTF_LEFTDOWN))
    try:
        for i in range(1, steps + 1):  # la destination peut être une autre fenêtre (glisser vers une autre appli)
            move(x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps, check_point=False)
            time.sleep(0.015)
    finally:
        _send(_mouse(w.MOUSEEVENTF_LEFTUP))


def wheel(x: int, y: int, delta: int, horizontal: bool = False) -> None:
    move(x, y)
    _send(_mouse(w.MOUSEEVENTF_HWHEEL if horizontal else w.MOUSEEVENTF_WHEEL, data=delta))


def type_text(text: str) -> None:
    for ch in text.replace("\r\n", "\n"):
        _guard()
        if ch == "\n":
            _send(_key(0x0D), _key(0x0D, up=True))
            continue
        for unit in [
            int.from_bytes(ch.encode("utf-16-le")[i : i + 2], "little")
            for i in range(0, len(ch.encode("utf-16-le")), 2)
        ]:
            _send(_key(0, scan=unit, unicode=True), _key(0, up=True, scan=unit, unicode=True))
        time.sleep(0.002)


def press(combo: keys.Combo) -> None:
    _guard()
    downs = [_key(vk) for vk in combo.modifiers] + [_key(combo.key)]
    ups = [_key(combo.key, up=True)] + [_key(vk, up=True) for vk in reversed(combo.modifiers)]
    _send(*downs, *ups)
