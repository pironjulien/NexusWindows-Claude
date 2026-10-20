"""Vraie souris et vrai clavier (SendInput) : uniquement pour les actions que l'utilisateur a autorisées (foreground=True).

La fenêtre cible passe devant, l'action est jouée, puis le curseur revient à sa place et le premier plan à la
fenêtre de l'utilisateur. Refus net si Windows ne met pas la cible devant (sinon les entrées iraient ailleurs).
"""

from __future__ import annotations

import ctypes
import time
from contextlib import contextmanager

from . import foreground, keys, stop, worker
from . import win32 as w


class ForegroundError(RuntimeError):
    pass


_LAST_MOVE: tuple[int, int] | None = None  # dernière position où NOUS avons mis le curseur
_ALLOWED: set[int] = set()  # fenêtres de la cible pendant un emprunt autorisé
_TOKEN = None  # suspension de la garde, renouvelée lot par lot
_SINCE: tuple[float, int] | None = None  # début de l'emprunt (time.monotonic, GetTickCount) : entrées de Julien après


def _guard(x: int | None = None, y: int | None = None) -> None:
    """Avant chaque lot d'entrées réelles : l'utilisateur ne doit pas avoir touché clavier, boutons ou molette depuis le début
    de l'emprunt, la cible doit être devant, et le point viser une de ses fenêtres. Sinon (L'utilisateur a repris la main,
    une autre fenêtre s'est intercalée) on s'arrête au lieu d'écrire chez lui."""
    if not _ALLOWED:
        raise ForegroundError("Real input outside an allowed borrow: refused.")
    if stop.remaining_s() > 0:
        raise ForegroundError(stop.message())
    if _SINCE is not None and foreground.user_input_since(*_SINCE):
        raise foreground.UserInterrupted(
            "The user used the keyboard or mouse during the real mouse/keyboard input: stopped, nothing else sent; "
            "what came before may have reached the target: check the target (look)"
        )
    if w.root(w.foreground()) not in _ALLOWED:
        raise ForegroundError(
            "The target is no longer in the foreground (the user took over?): stopped, nothing else sent."
        )
    if x is not None and y is not None and w.window_at(x, y) not in _ALLOWED:
        raise ForegroundError(f"The point ({x}, {y}) does not hit the target window (covered?): stopped.")
    if _TOKEN is not None:
        foreground.renew(_TOKEN)


def _send(*inputs: w.INPUT) -> None:
    foreground.mark_own_input()
    array = (w.INPUT * len(inputs))(*inputs)
    if w.SendInput(len(inputs), array, ctypes.sizeof(w.INPUT)) != len(inputs):
        raise ForegroundError("Windows refused the simulated input (locked screen, UAC or secure desktop).")


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


def _at(flags: int, x: int, y: int, data: int = 0) -> w.INPUT:
    """Événement souris AU point (x, y) de l'écran : déplacement absolu sur le bureau virtuel joint au bouton ou à la
    molette, pour que l'événement tombe là où on vise même si l'utilisateur bouge la souris au même moment."""
    ax, ay = _absolute(x, y)
    return _mouse(flags | w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE | w.MOUSEEVENTF_VIRTUALDESK, ax, ay, data)


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
    """window(action="front"), autorisé par l'utilisateur : comme un emprunt, pause de l'utilisateur attendue (sinon BusyUser) et
    arrêt d'urgence contrôlé juste avant ; la cible passe devant sous suspension de la garde, et les surveillances en
    cours (traîne d'un appel précédent comprise) adoptent ce premier plan voulu au lieu de le rendre. Jeton commun aux
    agents tenu le temps de la mise au premier plan."""
    with foreground.agent_token("window put in front"):
        if not foreground.wait_typing_pause():
            raise foreground.BusyUser("The user is using the mouse or keyboard: bringing to the foreground postponed.")
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
        f'foreground held by "{w.window_text(fg) or w.class_name(fg)}" ({w.process_name(w.window_pid(fg))})'
        if fg
        else "no window in the foreground"
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
        self.started_tick = w.GetTickCount()
        self.user_took_over = False

    def checkpoint(self) -> None:
        """Entre deux étapes : l'utilisateur a-t-il repris la main (touche, clic, molette ; Raw Input, sinon GetLastInputInfo
        hors de nos entrées) ? Alors le lot s'arrête et le premier plan lui reste. Sinon, la cible est remise devant si
        une autre fenêtre s'est intercalée."""
        if foreground.user_input_since(self.started, self.started_tick):
            self.user_took_over = True
            raise foreground.UserInterrupted(
                "The user used the keyboard or mouse during the foreground batch: batch stopped, foreground "
                "left to the user"
            )
        if w.root(w.foreground()) not in self.windows:
            worker.check_cancelled()
            with foreground.suspended():
                if bring_to_front(self.hwnd):
                    foreground.adopt_foreground()


_HOLD: Hold | None = None


def current_hold() -> Hold | None:
    return _HOLD


def _target_still_front(windows: set[int], pids: set[int]) -> bool:
    """Le premier plan est-il encore à la cible ? Sinon l'utilisateur (ou une autre de ses fenêtres) l'a pris entre-temps : on
    ne lui impose pas l'ancienne fenêtre (comme foreground.Activation.give_back)."""
    return foreground.belongs_to(w.foreground(), windows, pids, set())


@contextmanager
def held(hwnd: int, windows: set[int] | None = None):
    """foreground=true accordé : la cible passe devant (après une pause de l'utilisateur) et y reste pendant tout le lot ;
    les surveillances du premier plan l'adoptent au lieu de le « rendre ». À la sortie (fin, erreur, arrêt
    d'urgence), premier plan rendu à la fenêtre de l'utilisateur (sauf s'il a repris la main lui-même) et curseur remis si
    nous l'avons déplacé et qu'il n'a pas bougé depuis. Jeton commun aux agents tenu pendant tout le lot."""
    with foreground.agent_token("foreground batch"), _held(hwnd, windows) as hold:
        yield hold


@contextmanager
def _held(hwnd: int, windows: set[int] | None):
    global _HOLD, _LAST_MOVE
    if not foreground.wait_typing_pause():
        raise foreground.BusyUser(
            "The user is using the mouse or keyboard: foreground batch postponed, nothing was done."
        )
    worker.check_cancelled()
    previous = w.foreground()
    allowed = set(windows or foreground.target_windows(hwnd))
    pids = foreground.target_pids(hwnd, list(allowed))
    hold = Hold(hwnd, allowed, previous, w.cursor_pos())
    _LAST_MOVE = None
    with foreground.suspended():
        # Déjà devant par une de ses fenêtres (panneau de Windows ouvert par son raccourci : la recherche de Démarrer
        # tient le premier plan pour lui) : rien à activer.
        if w.root(w.foreground()) not in allowed and not bring_to_front(hwnd):
            raise ForegroundError(f"Windows refuses to bring the window to the front ({_holder()}): nothing was done.")
        foreground.adopt_foreground()
    _HOLD = hold
    try:
        yield hold
    finally:
        _HOLD = None
        time.sleep(0.05)
        if _LAST_MOVE is not None and w.cursor_pos() == _LAST_MOVE:
            w.SetCursorPos(*hold.cursor)
        if not hold.user_took_over and previous and previous not in allowed and _target_still_front(allowed, pids):
            with foreground.suspended():
                foreground.give_back(previous, exclude=allowed)
                foreground.adopt_foreground()


@contextmanager
def borrowed(hwnd: int, windows: set[int] | None = None):
    """Met la cible devant le temps du bloc (après une pause de l'utilisateur), puis rend curseur et premier plan.

    Chaque lot d'entrées vérifie d'abord que la cible est toujours devant (et le point sur elle) ; la garde est
    suspendue lot par lot. Le curseur n'est remis en place que si nous l'avons déplacé et que l'utilisateur ne l'a pas
    bougé depuis. Jeton commun aux agents tenu pendant tout l'emprunt.
    """
    with foreground.agent_token("real mouse or keyboard"), _borrowed(hwnd, windows):
        yield


@contextmanager
def _borrowed(hwnd: int, windows: set[int] | None):
    global _LAST_MOVE, _ALLOWED, _TOKEN, _SINCE
    if not foreground.wait_typing_pause():
        raise foreground.BusyUser("The user is using the mouse or keyboard: borrow postponed, nothing was sent.")
    worker.check_cancelled()  # arrêt d'urgence demandé pendant l'attente : rien n'est mis devant
    previous = w.foreground()
    cursor = w.cursor_pos()
    _LAST_MOVE = None
    allowed = set(windows or foreground.target_windows(hwnd))
    pids = foreground.target_pids(hwnd, list(allowed))
    outer = _SINCE  # emprunt imbriqué : on garde le début du plus ancien
    with foreground.suspended() as token:
        _TOKEN = token
        _SINCE = outer or (time.monotonic(), w.GetTickCount())
        try:
            if not bring_to_front(hwnd):
                raise ForegroundError(
                    f"Windows refuses to bring the window to the front ({_holder()}): nothing was sent."
                )
            _ALLOWED = set(allowed)
            yield
        finally:
            _ALLOWED = set()
            _TOKEN = None
            _SINCE = outer
            time.sleep(0.05)
            if _LAST_MOVE is not None and w.cursor_pos() == _LAST_MOVE:
                w.SetCursorPos(*cursor)
            if previous and previous not in allowed and _target_still_front(allowed, pids):
                foreground.give_back(previous, exclude=allowed)


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
    """Déplacement, appuis et relâchements en UN SEUL SendInput, chacun avec les coordonnées absolues du point :
    Windows ne glisse aucune autre entrée au milieu, le clic ne peut pas tomber sous le curseur de l'utilisateur."""
    global _LAST_MOVE
    _guard(x, y)
    down, up = _BTN[button]
    _send(_at(0, x, y), *[_at(flag, x, y) for _ in range(count) for flag in (down, up)])
    _LAST_MOVE = w.cursor_pos()


def drag(x1: int, y1: int, x2: int, y2: int, steps: int = 12) -> None:
    global _LAST_MOVE
    _guard(x1, y1)
    _send(_at(0, x1, y1), _at(w.MOUSEEVENTF_LEFTDOWN, x1, y1))  # appui au point de départ, quoi que fasse Julien
    _LAST_MOVE = w.cursor_pos()
    try:
        for i in range(1, steps + 1):  # la destination peut être une autre fenêtre (glisser vers une autre appli)
            time.sleep(0.015)
            move(x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps, check_point=False)
    finally:
        _send(_mouse(w.MOUSEEVENTF_LEFTUP))


def wheel(x: int, y: int, delta: int, horizontal: bool = False) -> None:
    """Molette au point, déplacement absolu joint dans le même SendInput (comme click)."""
    global _LAST_MOVE
    _guard(x, y)
    _send(_at(0, x, y), _at(w.MOUSEEVENTF_HWHEEL if horizontal else w.MOUSEEVENTF_WHEEL, x, y, delta))
    _LAST_MOVE = w.cursor_pos()


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


def shell_shortcut(combo: keys.Combo) -> None:
    """Raccourci Windows (touche Windows seule ou avec une lettre) : il va au shell, jamais dans la fenêtre de
    l'utilisateur ; sert à ouvrir un panneau de Windows dans un lot foreground=true accordé. Arrêt d'urgence respecté."""
    if 0x5B not in (*combo.modifiers, combo.key) or (combo.modifiers and set(combo.modifiers) != {0x5B}):
        raise ForegroundError("Only Windows-key shortcuts (win, win+letter) go to the shell.")
    if stop.remaining_s() > 0:
        raise ForegroundError(stop.message())
    downs = [_key(vk) for vk in combo.modifiers] + [_key(combo.key)]
    ups = [_key(combo.key, up=True)] + [_key(vk, up=True) for vk in reversed(combo.modifiers)]
    _send(*downs, *ups)


def escape_to(hwnd: int) -> None:
    """Échap au clavier réel, seulement si `hwnd` (un panneau de Windows ouvert par nous) tient le premier plan :
    il se referme, rien n'arrive dans une fenêtre de l'utilisateur."""
    if stop.remaining_s() > 0:
        raise ForegroundError(stop.message())
    if w.root(w.foreground()) != hwnd:
        return
    _send(_key(0x1B), _key(0x1B, up=True))


def press(combo: keys.Combo) -> None:
    _guard()
    downs = [_key(vk) for vk in combo.modifiers] + [_key(combo.key)]
    ups = [_key(combo.key, up=True)] + [_key(vk, up=True) for vk in reversed(combo.modifiers)]
    _send(*downs, *ups)
