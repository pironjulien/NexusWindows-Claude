"""Protection du premier plan de l'utilisateur pendant qu'on agit en arrière-plan.

Certaines applications s'activent d'elles-mêmes quand on agit sur elles (le proxy UIA des boutons Win32 appelle
SetForegroundWindow avant BM_CLICK ; Chromium s'active sur une prise de focus…). `Watch` surveille le premier plan
pendant tout un appel d'outil (et un court instant après) : si la cible le prend, il est rendu aussitôt à la fenêtre
de l'utilisateur (≈10 ms mesurés). Si l'utilisateur agit lui-même (clic, Alt+Tab, touche Windows, barre des tâches), on le suit
sans lutter — AltGr (Ctrl gauche + Alt droit, courant en AZERTY) n'est pas un Alt+Tab.

Activation brève (Chromium ne lit le clavier que fenêtre active) : jamais sans pause de l'utilisateur ni bouton tenu,
jamais sans fenêtre à qui rendre le premier plan, interrompue à sa première entrée physique (contrôle à chaque
caractère posté et à chaque point de contrôle du thread bureau), et découpée en tranches courtes (methods.py).

Identité de la cible : par FENÊTRES (la fenêtre, ses surgissantes, les fenêtres qu'elle possède, et celles que ses
processus créent pendant l'action), jamais par le seul processus : l'utilisateur peut avoir une autre fenêtre du même
programme (un second Edge, un autre document) qui n'est pas la cible. Les applications UWP sont hébergées par
ApplicationFrameHost, partagé : le processus réel est celui de leur CoreWindow.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from . import agentlock, physical
from . import win32 as w

_SwitchToThisWindow = w._fn(w.user32, "SwitchToThisWindow", None, w.w.HWND, w.w.BOOL)
HOST_PROCESSES = {"applicationframehost.exe"}
VK_LWIN, VK_RWIN, VK_LCONTROL, VK_LMENU, VK_RMENU = 0x5B, 0x5C, 0xA2, 0xA4, 0xA5
TASKBAR_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "TaskListThumbnailWnd", "XamlExplorerHostIslandWindow"}
# Sélecteur de fenêtres (Alt+Tab, Win+Tab) et barre des tâches : Windows les met au premier plan pendant le geste de
# Julien, puis active la fenêtre choisie au relâchement. Jamais une fenêtre à qui « rendre » le premier plan.
SWITCHER_CLASSES = TASKBAR_CLASSES | {"MultitaskingViewFrame", "ForegroundStaging", "TaskSwitcherWnd"}
SWITCH_WINDOW_S = 1.0  # activation de la cible acceptée si le sélecteur ou la barre était devant il y a moins de 1 s
QUIET_S = 1.0  # pause de Julien exigée avant toute activation (clavier, boutons et molette)


class UserInterrupted(RuntimeError):
    """L'utilisateur a touché clavier ou souris pendant une activation brève : ses frappes ont pu arriver dans la cible."""


class BusyUser(RuntimeError):
    """Activation brève impossible sans risque pour l'utilisateur : il tape ou tient un bouton de souris, ou le premier plan
    n'a aucune fenêtre à qui être rendu ensuite (fenêtre système masquée, aucune fenêtre)."""


# --- Suspension datée -----------------------------------------------------------------------------------------
@dataclass
class _Suspension:
    thread: int
    until: float


_SUSPENSION: _Suspension | None = None
_LOCK = threading.Lock()
MAX_SUSPENSION_S = 2.5


def _suspended_now() -> bool:
    current = _SUSPENSION
    return current is not None and time.monotonic() < current.until


@contextmanager
def suspended(max_s: float = MAX_SUSPENSION_S):
    """Bloc où l'on met volontairement une fenêtre devant : surveillance coupée, au plus max_s.

    La suspension porte une échéance : un fil bloqué ou abandonné ne peut pas couper la garde indéfiniment.
    """
    global _SUSPENSION
    token = _Suspension(threading.get_ident(), time.monotonic() + max_s)
    with _LOCK:
        _SUSPENSION = token
    try:
        yield token
    finally:
        with _LOCK:
            if _SUSPENSION is token:
                _SUSPENSION = None


def renew(token: _Suspension, max_s: float = MAX_SUSPENSION_S) -> None:
    """Prolonge un emprunt encore actif (vraie souris/clavier autorisés), lot par lot."""
    token.until = time.monotonic() + max_s


# --- Entrées de l'outil lui-même ------------------------------------------------------------------------------
_MASK = 0xFFFFFFFF
_OWN_INPUT = (
    0,
    0,
)  # (début, fin) en GetTickCount de la dernière fenêtre où nos activations ont pu produire des entrées


def mark_own_input(window_ms: int = 150) -> None:
    """Nos activations et nos SendInput autorisés peuvent produire des entrées synthétiques qui changent
    GetLastInputInfo : on les date pour ne pas les prendre pour l'utilisateur."""
    global _OWN_INPUT
    now = w.GetTickCount()
    _OWN_INPUT = (now, (now + window_ms) & _MASK)


def _between(tick: int, start: int, end: int) -> bool:
    return ((tick - start) & _MASK) <= ((end - start) & _MASK)


def user_idle_ms() -> int:
    """Temps depuis la dernière entrée PHYSIQUE de l'utilisateur (Raw Input : les entrées injectées, les nôtres comme celles
    d'UI Automation, ne comptent pas). Repli sans Raw Input : GetLastInputInfo, nos activations datées écartées."""
    if physical.available():
        return int(min(physical.idle_s(), 86_400.0) * 1000)
    now = w.GetTickCount()
    last = w.last_input_tick()
    start, end = _OWN_INPUT
    if start and _between(last, start, end):
        return (now - start) & _MASK
    return (now - last) & _MASK


def user_input_since(started: float, started_tick: int) -> bool:
    """L'utilisateur a-t-il touché clavier, boutons ou molette depuis `started` (time.monotonic) / `started_tick`
    (GetTickCount relevé au même instant) ? Raw Input si disponible (entrées injectées écartées). Repli sans Raw
    Input : dernière entrée du système postérieure au début, hors de nos propres entrées datées (mark_own_input)."""
    if physical.available():
        return physical.since(started)
    last, now = w.last_input_tick(), w.GetTickCount()
    after = (last - started_tick) & _MASK
    if after == 0 or after > ((now - started_tick) & _MASK):
        return False
    start, end = _OWN_INPUT
    return not (start and _between(last, start, end))


def wait_typing_pause(quiet_s: float = QUIET_S, max_wait_s: float = 4.0) -> bool:
    """Attend que l'utilisateur marque une pause (rien au clavier, aux boutons ni à la molette depuis quiet_s, et aucun
    bouton tenu : un glisser en cours n'est pas une pause, Raw Input ne voyant que les transitions). Faux s'il reste
    actif."""
    deadline = time.monotonic() + max_wait_s
    while True:
        if user_idle_ms() >= quiet_s * 1000 and not w.mouse_button_down():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.03)


def _down(vk: int) -> bool:
    return bool(w.GetAsyncKeyState(vk) & 0x8000)


def user_is_switching() -> bool:
    """L'utilisateur est en train de changer de fenêtre lui-même : bouton de souris enfoncé, Alt ou touche Windows.

    AltGr (Ctrl gauche + Alt droit, courant en AZERTY : @ # { } [ ] | € ~) ne compte pas : l'utilisateur tape, il ne change
    pas de fenêtre. Alt droit seul, sans Ctrl gauche, reste un Alt."""
    if w.mouse_button_down() or _down(VK_LMENU) or _down(VK_LWIN) or _down(VK_RWIN):
        return True
    return _down(VK_RMENU) and not _down(VK_LCONTROL)


# --- Identité de la cible -------------------------------------------------------------------------------------
def target_pids(hwnd: int, extra_windows: list[int] | None = None) -> set[int]:
    """Processus qui possèdent la fenêtre cible : elle, ses fenêtres enfants (CoreWindow UWP) et ses surgissantes.
    L'hôte UWP partagé (ApplicationFrameHost) n'est retenu que s'il n'y a rien d'autre."""
    if not hwnd:
        return set()
    pids = {w.window_pid(hwnd)}
    for child in w.enum_children(hwnd):
        pids.add(w.window_pid(child))
    for other in extra_windows or []:
        pids.add(w.window_pid(other))
    hosts = {p for p in pids if w.process_name(p).lower() in HOST_PROCESSES}
    return pids - hosts if pids - hosts else pids


def app_pid(hwnd: int) -> int:
    """Processus de l'application d'une fenêtre : le sien, sauf pour un cadre UWP (celui de sa CoreWindow)."""
    pid = w.window_pid(hwnd)
    if w.process_name(pid).lower() not in HOST_PROCESSES:
        return pid
    for child in w.enum_children(hwnd):
        child_pid = w.window_pid(child)
        if child_pid != pid:
            return child_pid
    return pid


def owned_by(hwnd: int, targets: set[int]) -> bool:
    cursor = hwnd
    for _ in range(8):
        cursor = w.owner(cursor)
        if not cursor:
            return False
        if cursor in targets:
            return True
    return False


def target_windows(hwnd: int, extra: list[int] | None = None) -> set[int]:
    """La fenêtre cible, ses surgissantes et toutes les fenêtres de premier niveau qu'elle possède (dialogues)."""
    windows = {hwnd} | set(extra or [])
    for other in w.enum_windows():
        if other not in windows and owned_by(other, windows):
            windows.add(other)
    return windows


def belongs_to(fg: int, windows: set[int], pids: set[int], known_before: set[int] | None = None) -> bool:
    """La fenêtre `fg` fait-elle partie de la cible ? Par fenêtre d'abord ; par processus seulement pour une fenêtre
    apparue pendant l'action (absente de `known_before`), jamais pour une fenêtre que l'utilisateur avait déjà."""
    if not fg:
        return False
    if fg in windows or w.root(fg) in windows or owned_by(fg, windows):
        return True
    if known_before is None or fg in known_before:
        return False
    pid = w.window_pid(fg)
    if w.process_name(pid).lower() in HOST_PROCESSES:
        return app_pid(fg) in pids
    return pid in pids


# --- Rendre / prendre le premier plan --------------------------------------------------------------------------
def _attempt(prev: int, expected_fg: int) -> None:
    """Tentative unique ; abandonnée si le premier plan a changé entre-temps (l'utilisateur a pu passer ailleurs).
    On ne s'attache qu'à la file d'entrée de la fenêtre qui a pris le premier plan, jamais à celle de l'utilisateur."""
    fg = w.foreground()
    if fg != expected_fg:
        return
    own = w.GetCurrentThreadId()
    thread = w.window_thread(fg) if fg else 0
    attached = bool(thread and thread != own and w.AttachThreadInput(own, thread, True))
    try:
        if w.foreground() == expected_fg:
            w.BringWindowToTop(prev)
            w.SetForegroundWindow(prev)
    finally:
        if attached:
            w.AttachThreadInput(own, thread, False)


def _next_user_window(exclude: set[int]) -> int:
    """Première fenêtre d'application visible de l'ordre Z qui n'est pas la cible (fenêtre de l'utilisateur disparue)."""
    from .snapshot import is_app_window  # import local : snapshot importe ce module

    for hwnd in w.enum_windows():
        if hwnd in exclude or w.root(hwnd) in exclude:
            continue
        if is_app_window(hwnd) and not w.IsIconic(hwnd):
            return hwnd
    return 0


def _switch_to(hwnd: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    tries = 0
    while time.monotonic() < deadline:
        expected = w.foreground()
        if expected == hwnd:
            return True
        # Thread à part : BringWindowToTop attend le thread de la fenêtre qui perd l'activation (cible occupée).
        worker = threading.Thread(target=_attempt, args=(hwnd, expected), daemon=True)
        worker.start()
        worker.join(0.25)
        tries += 1
        if w.foreground() == hwnd:
            return True
        if tries == 2 and w.foreground() == expected:
            _SwitchToThisWindow(hwnd, True)
        time.sleep(0.03)
    return w.foreground() == hwnd


def _restorable(hwnd: int) -> bool:
    """Fenêtre à qui rendre le premier plan : existante, affichée, non masquée, ni sélecteur ni barre des tâches."""
    return bool(
        hwnd
        and w.IsWindow(hwnd)
        and not w.IsIconic(hwnd)
        and w.IsWindowVisible(hwnd)
        and not w.is_cloaked(hwnd)
        and w.class_name(hwnd) not in SWITCHER_CLASSES
    )


WM_SYSCOMMAND, SC_HOTKEY = 0x0112, 0xF150
HAND_OVER_S = 0.3


def _hand_over(prev: int, exclude: set[int]) -> bool:
    """La cible tient le premier plan et Windows refuse de nous attacher à sa file (ApplicationFrameHost et CoreWindow
    d'une application Store : AttachThreadInput → ERROR_ACCESS_DENIED, mesuré) : notre SetForegroundWindow est alors
    refusé. On demande au fil QUI DÉTIENT le premier plan de le rendre lui-même : WM_SYSCOMMAND SC_HOTKEY, que
    DefWindowProc traite par SetForegroundWindow(lParam) (mesuré : rendu en 2 à 3 ms, cible ni réduite ni déplacée).
    Seulement à une fenêtre de la cible (exclude), jamais à une fenêtre de l'utilisateur."""
    fg = w.foreground()
    if not fg or fg == prev or not (fg in exclude or w.root(fg) in exclude or owned_by(fg, exclude)):
        return False
    w.PostMessageW(fg, WM_SYSCOMMAND, SC_HOTKEY, prev)
    deadline = time.monotonic() + HAND_OVER_S
    while time.monotonic() < deadline:
        if w.foreground() == prev:
            return True
        time.sleep(0.01)
    return w.foreground() == prev


def give_back(prev: int, timeout_s: float = 0.6, exclude: set[int] | None = None) -> bool:
    """Rend le premier plan à la fenêtre de l'utilisateur `prev` (ou, si elle a disparu ou n'est pas une fenêtre à qui le
    rendre — sélecteur, barre des tâches, fenêtre masquée —, à sa fenêtre suivante). Si Windows refuse (cible Store),
    la fenêtre de la cible au premier plan le rend elle-même (_hand_over)."""
    if prev and w.foreground() == prev:
        return True
    if not _restorable(prev):
        prev = _next_user_window((exclude or set()) | {w.foreground()})
        if not prev:
            return False
    mark_own_input()
    exclude = exclude or set()
    if w.process_name(w.window_pid(w.foreground())).lower() in HOST_PROCESSES and _hand_over(prev, exclude):
        return True  # cadre Store : l'attache serait refusée, inutile de faire attendre Julien 0,6 s
    return _switch_to(prev, timeout_s) or _hand_over(prev, exclude)


def system_foreground() -> str:
    """Le premier plan est-il tenu par une fenêtre système masquée ? (Windows 11 : après Échap dans la Recherche ou
    le menu Démarrer, la CoreWindow masquée de SearchHost reste au premier plan tant que l'utilisateur n'a cliqué nulle part.)
    Windows refuse alors toute activation programmatique (mesuré : attache de file, Alt simulé, focus UIA)."""
    fg = w.foreground()
    if not fg:
        return "aucune fenêtre"
    if w.is_cloaked(fg) or not w.IsWindowVisible(fg):
        return f"fenêtre système masquée « {w.window_text(fg) or w.class_name(fg)} »"
    return ""


def _checkpoint() -> None:
    """Juste avant de mettre une fenêtre devant : arrêt d'urgence, appel abandonné, l'utilisateur qui reprend la main."""
    from .worker import check_cancelled  # import local : worker importe ce module

    check_cancelled()


def activate(hwnd: int, timeout_s: float = 0.4) -> bool:
    """Met LA cible devant (activation brève voulue) ; jamais une autre fenêtre à sa place."""
    _checkpoint()
    if not hwnd or not w.IsWindow(hwnd) or w.IsIconic(hwnd):
        return False
    mark_own_input()
    return _switch_to(hwnd, timeout_s) or _ask_holder(hwnd)


def _ask_holder(hwnd: int) -> bool:
    """Le premier plan est tenu par une fenêtre dont Windows refuse d'attacher la file (cadre d'application Store,
    ApplicationFrameHost : ERROR_ACCESS_DENIED, mesuré le 2026-10-14 avec la Calculatrice devant) : on demande au fil
    qui le détient de le céder lui-même à la cible (WM_SYSCOMMAND SC_HOTKEY, traité par DefWindowProc comme
    SetForegroundWindow(lParam) ; mécanisme déjà mesuré pour le rendre à l'utilisateur, voir _hand_over). Rien d'autre n'est
    envoyé à cette fenêtre. Jamais quand une fenêtre système masquée tient le premier plan."""
    fg = w.foreground()
    if not fg or fg == hwnd or system_foreground():
        return False
    w.PostMessageW(fg, WM_SYSCOMMAND, SC_HOTKEY, hwnd)
    deadline = time.monotonic() + HAND_OVER_S
    while time.monotonic() < deadline:
        if w.root(w.foreground()) == hwnd:
            return True
        time.sleep(0.01)
    return w.root(w.foreground()) == hwnd


class Activation:
    """Activation brève en cours : l'utilisateur a-t-il touché clavier ou souris (physiquement) depuis son début ?"""

    def __init__(
        self, started: float, front: int = 0, windows: set[int] | None = None, pids: set[int] | None = None
    ) -> None:
        self.started = started
        self.started_tick = w.GetTickCount()
        self.front = front  # fenêtre de Julien relevée après sa pause, à qui rendre le premier plan
        self.windows = windows or set()
        self.pids = pids or set()

    def interrupted(self) -> bool:
        return user_input_since(self.started, self.started_tick)

    def give_back(self) -> None:
        """Rend le premier plan à l'utilisateur si c'est encore la cible qui l'a (s'il est passé ailleurs entre-temps, on ne
        lui impose pas l'ancienne fenêtre)."""
        fg = w.foreground()
        if self.front and self.front not in self.windows and fg != self.front:
            if belongs_to(fg, self.windows, self.pids, set()):
                give_back(self.front, exclude=self.windows)

    def check(self, consequence: str) -> None:
        """À la première entrée physique de l'utilisateur : premier plan rendu sur-le-champ, puis UserInterrupted."""
        if self.interrupted():
            self.give_back()
            took = round((time.monotonic() - self.started) * 1000)
            raise UserInterrupted(
                f"L'utilisateur a utilisé le clavier ou la souris pendant l'activation brève ({took} ms) : {consequence}."
            )

    def checkpoint(self) -> None:
        """Contrôle en cours de frappe ou d'attente (à chaque caractère posté, à chaque tour d'attente)."""
        self.check(
            "frappe arrêtée et premier plan rendu ; ses touches ont pu arriver dans la cible : vérifier la cible (look)"
        )


_CURRENT = threading.local()  # activation brève en cours sur ce fil (thread bureau)


def check_activation() -> None:
    """Point de contrôle de l'activation brève en cours sur ce fil, s'il y en a une (appelé par
    worker.check_cancelled et à chaque caractère posté)."""
    activation: Activation | None = getattr(_CURRENT, "activation", None)
    if activation is not None:
        activation.checkpoint()


@contextmanager
def agent_token(purpose: str):
    """Jeton commun aux agents de l'utilisateur (agentlock) : on attend qu'un autre agent ait rendu souris et premier plan
    avant de les prendre (sinon BusyUser), et on le tient jusqu'à la fin du bloc."""
    try:
        with agentlock.hold(purpose, checkpoint=_checkpoint):
            yield
    except agentlock.Busy as exc:
        raise BusyUser(str(exc)) from None


@contextmanager
def brief_activation(target: int, windows: set[int] | None = None, user_front: int = 0):
    """Bloc où la cible peut être active un court instant (Chromium ne lit le clavier que fenêtre active).

    Prend d'abord le jeton commun aux agents (agent_token), puis attend une pause de l'utilisateur (sinon BusyUser), refuse si le premier plan n'a aucune fenêtre à qui être rendu
    (aucune, ou fenêtre système masquée : Windows refuserait aussi l'activation), relève sa fenêtre APRÈS la pause,
    suspend la surveillance, puis rend le premier plan à cette fenêtre en sortie. Pendant le bloc, toute entrée
    physique de l'utilisateur l'interrompt au prochain point de contrôle (check_activation).

    user_front : fenêtre de l'utilisateur relevée par l'appelant AVANT que la cible ait pu prendre la main (launch). Si la
    cible tient alors le premier plan, elle le lui a pris : le relevé désignerait la cible elle-même, rien ne serait
    rendu en sortie et « rendu » serait faux : refus (BusyUser). Sans user_front (act), une cible au premier plan est
    celle que l'utilisateur a choisie (comme pour Watch) : l'activation est permise et n'a rien à rendre.
    """
    with agent_token("activation brève"), _brief_activation(target, windows, user_front) as activation:
        yield activation


@contextmanager
def _brief_activation(target: int, windows: set[int] | None, user_front: int):
    if not wait_typing_pause():
        raise BusyUser(
            "L'utilisateur tape, bouge la souris ou tient un bouton en ce moment : frappe reportée pour ne pas capter ses "
            "touches."
        )
    _checkpoint()  # arrêt d'urgence demandé pendant l'attente de la pause
    holder = system_foreground()
    front = w.foreground()
    if holder or not front:
        raise BusyUser(
            f"Windows refuse d'activer la fenêtre : le premier plan est tenu par {holder or 'aucune fenêtre'}, à qui "
            "il ne pourrait pas être rendu ensuite. Remède : application lancée par launch avec devtools=true, ou "
            "foreground=true"
        )
    windows = windows or target_windows(target)
    if user_front and front != user_front and belongs_to(front, windows, set()):  # par fenêtre seulement
        raise BusyUser(
            "l'application tient déjà le premier plan pris à l'utilisateur : aucune activation brève (il n'y aurait personne "
            "à qui le rendre ensuite)"
        )
    activation = Activation(time.monotonic(), front, windows, target_pids(target, list(windows)))
    previous = getattr(_CURRENT, "activation", None)
    _CURRENT.activation = activation
    try:
        with suspended():
            try:
                yield activation
            finally:
                activation.give_back()
    finally:
        _CURRENT.activation = previous
    activation.check("ses frappes ont pu arriver dans la cible : vérifier la cible (look) et son propre texte")


# --- Surveillance -----------------------------------------------------------------------------------------------
_LIVE: set[Watch] = set()  # surveillances en cours (traînes comprises), pour adopt_foreground


def adopt_foreground() -> None:
    """Premier plan donné volontairement (window front autorisé par l'utilisateur) : les surveillances en cours, traînes
    comprises, l'adoptent au lieu de le « rendre ». À appeler sous suspended(), une fois la fenêtre devant."""
    fg = w.foreground()
    with _LOCK:
        live = list(_LIVE)
    for watch in live:
        watch._follow(fg)


class Watch:
    """with Watch(cible): … → toute prise du premier plan par l'application cible est annulée sur-le-champ."""

    POLL_S = 0.008
    TAIL_S = 0.35  # certaines applications s'activent un instant après la fin de l'action

    def __init__(
        self, target: int, windows: list[int] | None = None, tail_s: float | None = None, front: int = 0
    ) -> None:
        """front : fenêtre de l'utilisateur relevée par l'appelant avant que la cible ait pu la prendre (launch). La garde
        la prend pour référence au lieu du premier plan courant : si la cible le tient déjà (vol non rendu), elle
        démarre quand même et le lui rend."""
        self.target = target
        self.windows = target_windows(target, windows) if target else set()
        self.pids = target_pids(target, list(self.windows))
        self.known_before = set(w.enum_windows())
        self.tail_s = self.TAIL_S if tail_s is None else tail_s
        self.stolen = 0
        self.failed = False
        self._alt_pending = False
        self._switcher_seen: float | None = None  # dernier instant où le sélecteur ou la barre était devant
        # Fenêtre de Julien fermée entre-temps : référence nulle, un vol ira à sa fenêtre suivante.
        self._follow((front if w.IsWindow(front) else 0) if front else w.foreground())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _ours(self, hwnd: int) -> bool:
        return belongs_to(hwnd, self.windows, self.pids, self.known_before)

    def _follow(self, fg: int) -> None:
        """Le premier plan de référence devient `fg` (fenêtre où l'utilisateur est passé, ou cible qu'il a choisie)."""
        self.front = fg
        self._alt_pending = False
        self._switcher_seen = time.monotonic() if fg and w.class_name(fg) in SWITCHER_CLASSES else None

    def _check(self) -> None:
        if _suspended_now():
            return
        fg = w.foreground()
        if not fg:
            return
        if fg == self.front:
            if self._switcher_seen is not None:
                self._switcher_seen = time.monotonic()  # sélecteur ou barre toujours devant
            return
        if not self._ours(fg):
            self._follow(fg)  # Julien est passé à une autre fenêtre : on le suit
            return
        if self._switcher_seen is not None and time.monotonic() - self._switcher_seen <= SWITCH_WINDOW_S:
            # Alt+Tab ou barre des tâches : Windows active la fenêtre choisie au relâchement, après le sélecteur.
            self._follow(fg)
            return
        if self._alt_pending and not user_is_switching():
            self._follow(fg)  # Alt+Tab (ou touche Windows) relâché sur la cible : c'est le choix de Julien
            return
        if user_is_switching():
            if w.mouse_button_down():
                over = w.window_at(*w.cursor_pos())
                if self._ours(over) or w.class_name(over) in SWITCHER_CLASSES:
                    self._follow(fg)  # Julien clique sur la cible ou sur son bouton de la barre des tâches
                    return
                # Bouton tenu ailleurs (Julien glisse dans sa propre fenêtre) : c'est bien un vol, on le rend.
            else:
                self._alt_pending = True  # Alt+Tab en cours : on juge au relâchement
                return
        if _suspended_now():
            return  # mise au premier plan voulue commencée pendant ce contrôle : ne pas la défaire
        if agentlock.other_holder() is not None:
            self._follow(fg)  # un autre agent de Julien, jeton en main, a mis cette fenêtre devant : on ne lutte pas
            return
        self.stolen += 1
        self._alt_pending = False
        if not give_back(self.front, exclude=self.windows):
            self.failed = True

    def _run(self) -> None:
        tail_end = None
        try:
            while True:
                self._check()
                if self._stop.is_set():
                    tail_end = tail_end or time.monotonic() + self.tail_s
                    if time.monotonic() >= tail_end:
                        return
                    time.sleep(self.POLL_S)
                else:
                    self._stop.wait(self.POLL_S)  # fin du bloc : réveil immédiat, dernier contrôle sans délai
        finally:
            with _LOCK:
                _LIVE.discard(self)

    def __enter__(self) -> Watch:
        # Premier plan nul au départ (transitoire, écran verrouillé) : la garde démarre quand même ; elle suit Julien
        # dès qu'une de ses fenêtres passe devant, et un vol depuis rien va à sa fenêtre suivante. Référence donnée
        # par l'appelant (front) alors que la cible tient le premier plan : la garde démarre, et son premier contrôle
        # rend le premier plan à Julien. Seule une cible au premier plan choisie par Julien (référence = cible) n'est
        # pas gardée.
        if self.target and not self._ours(self.front):
            self._thread = threading.Thread(target=self._run, name="nexus-foreground-watch", daemon=True)
            with _LOCK:
                _LIVE.add(self)
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        # Sans attendre : le fil de surveillance finit seul sa traîne pendant que la réponse part. Sans traîne
        # (tail_s=0), il ne reste que son dernier contrôle : on l'attend, pour que stolen et failed soient définitifs
        # quand l'appelant les lit (launch), au lieu de dépendre de l'ordonnanceur.
        self._stop.set()
        if self._thread is not None and self.tail_s <= 0:
            self._thread.join()

    def note(self) -> str:
        if not self.stolen:
            return ""
        if self.failed:
            return "L'application a pris le premier plan et Windows a refusé de le rendre à l'utilisateur."
        times = "une fois" if self.stolen == 1 else f"{self.stolen} fois"
        return f"L'application a pris le premier plan ({times}) : rendu aussitôt à l'utilisateur."
