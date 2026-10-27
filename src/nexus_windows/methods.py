"""Techniques d'entrée en arrière-plan, une par fonction (le choix entre elles est fait ailleurs).

Aucune de ces fonctions ne touche la vraie souris, le vrai clavier ni le premier plan, sauf type_in_slices en mode
d'activation brève (foreground.brief_activation), dernier recours pour les applications qui ne lisent le clavier que
fenêtre active. Chromium sans DevTools passe d'abord par l'activation SIMULÉE (simulated_activation) : messages
d'activation envoyés à sa seule fenêtre, sans que Windows change le premier plan.
"""

from __future__ import annotations

import contextlib
import ctypes
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from . import foreground, physical, worker
from . import win32 as w

BUTTON_CLASSES = ("button",)
EDIT_CLASSES = ("edit", "richedit", "richedit20w", "richedit50w", "richeditd2dpt")
COMBO_CLASSES = ("combobox",)
LISTBOX_CLASSES = ("listbox",)
TAB_CLASSES = ("systabcontrol32",)
TCM_GETITEMCOUNT, TCM_GETCURSEL, TCM_SETCURFOCUS = 0x1304, 0x130B, 0x1330

BS_TYPEMASK = 0xF
BS_OWNERDRAW = 0xB  # boutons WinForms (FlatStyle par défaut) : dessinés par l'application, poussoirs quand même
BS_PUSHBUTTON, BS_DEFPUSHBUTTON, BS_CHECKBOX, BS_AUTOCHECKBOX = 0x0, 0x1, 0x2, 0x3
BS_RADIOBUTTON, BS_3STATE, BS_AUTO3STATE, BS_AUTORADIOBUTTON = 0x4, 0x5, 0x6, 0x9
CB_GETCOUNT, CB_GETCURSEL, CB_SETCURSEL, CB_GETLBTEXT, CB_GETLBTEXTLEN = 0x146, 0x147, 0x14E, 0x148, 0x149
LB_GETCOUNT, LB_SETCURSEL, LB_GETTEXT, LB_GETTEXTLEN, LB_GETCURSEL = 0x18B, 0x186, 0x189, 0x18A, 0x188
CBN_SELCHANGE, LBN_SELCHANGE = 1, 1
CBN_SELENDOK = 9


class MethodError(RuntimeError):
    """La technique ne s'applique pas à cette cible (essayer la suivante)."""


class ReadBackFailed(RuntimeError):
    """Action transmise puis relue CONTRAIRE (sélection perdue, valeur vide, liste toujours fermée) : échec franc,
    jamais « ~ », et aucune autre technique rejouée par-dessus (ce n'est pas une MethodError)."""


def win_class(hwnd: int) -> str:
    return w.class_name(hwnd).lower()


def _is_class(hwnd: int, names: tuple[str, ...]) -> bool:
    cls = win_class(hwnd)
    # WinForms : « WindowsForms10.BUTTON.app.0.… » ; on regarde le segment de classe Win32 sous-jacent.
    if cls.startswith("windowsforms"):
        parts = cls.split(".")
        cls = parts[1] if len(parts) > 1 else cls
    return cls in names


# --- Souris par messages ------------------------------------------------------------------------------------
_BUTTONS = {
    "left": (w.WM_LBUTTONDOWN, w.WM_LBUTTONUP, w.MK_LBUTTON, w.WM_LBUTTONDBLCLK),
    "right": (w.WM_RBUTTONDOWN, w.WM_RBUTTONUP, w.MK_RBUTTON, 0x0206),
    "middle": (w.WM_MBUTTONDOWN, w.WM_MBUTTONUP, w.MK_MBUTTON, 0x0209),
}
_BUTTON_VK = {"left": 0x01, "right": 0x02, "middle": 0x04}  # VK_LBUTTON, VK_RBUTTON, VK_MBUTTON


ERROR_ACCESS_DENIED = 5


_ACCEPTED = [0]  # messages acceptés par PostMessage depuis le lancement (un envoi partiel se voit à son écart)


def accepted_posts() -> int:
    """Nombre de messages postés acceptés : avant/après une séquence, dit si quelque chose est déjà parti."""
    return _ACCEPTED[0]


def post(target: int, msg: int, wparam: int, lparam: int) -> None:
    """PostMessage vérifié : un refus (fenêtre disparue, application élevée bloquée par UIPI) n'est jamais un succès."""
    if not w.PostMessageW(target, msg, wparam, lparam):
        error = ctypes.get_last_error()
        if error == ERROR_ACCESS_DENIED:
            raise MethodError("Windows blocks messages to this application (elevated, UIPI).")
        raise MethodError(f"PostMessage refused (error {error}).")
    _ACCEPTED[0] += 1


def post_click(top: int, x: int, y: int, button: str = "left", count: int = 1, held: bool = False) -> int:
    """Clic par messages à la fenêtre enfant la plus profonde sous (x, y) écran. Renvoie le HWND visé.

    held : bouton « enfoncé » pour le SEUL fil de la cible pendant chaque WM_xBUTTONDOWN, relâché pendant chaque
    WM_xBUTTONUP (état clavier simulé, `fake_modifiers`), messages ENVOYÉS : traités pendant l'envoi, donc lus avec
    le bon état, sans délai deviné, et un double clic donne deux pressions (relecture de Codex du 23/10 : postés, le
    relâchement pouvait être lu bouton encore enfoncé). Tk lit l'état des boutons par GetKeyState au traitement
    (TkWinGetModifierState) et ne fait naître ButtonPress que s'il le voit enfoncé : sans lui, le clic posté est
    ignoré. Mesuré le 23/10 (bench/miss_probes.py) : canevas Tk recouvert, clic et double clic reçus, premier plan et
    curseur intacts."""
    target = w.deepest_child_at(top, x, y)
    # WinForms ne lève DoubleClick que si la fenêtre au point (hit-test) est le contrôle lui-même : point recouvert, double clic posté sans effet (banc commun du 22/10, les trois outils) ; rien n'est envoyé, la technique suivante (vraie souris, premier plan bref) s'en charge si elle est permise.
    if (
        count >= 2
        and win_class(target).startswith("windowsforms")
        and w.handle(w.WindowFromPoint(w.POINT(x, y))) != target
    ):
        raise MethodError("WinForms double click at a covered point has no effect by messages: nothing was sent.")
    cx, cy = w.message_point(target, x, y)
    lp = w.make_lparam(cx, cy)
    down, up, mk, dbl = _BUTTONS[button]
    if held:
        front = w.foreground()
        if front and w.window_thread(front) == w.window_thread(target):
            raise MethodError("The target thread is the foreground thread: simulated button refused.")
        if w.window_thread(target) in _PENDING_PRESS:  # vérifié aussi à la pression ; ici, avant même le survol
            raise MethodError("A previous simulated press in this application is not finished yet: nothing was sent.")
        _send_counted(target, w.WM_MOUSEMOVE, 0, lp)
        for i in range(count):
            worker.check_cancelled()  # arrêt d'urgence (pendant le survol compris) : aucune nouvelle pression
            _held_press(target, _BUTTON_VK[button], dbl if i == 1 and dbl else down, up, mk, lp)
        return target
    post(target, w.WM_MOUSEMOVE, 0, lp)
    for i in range(count):
        post(target, dbl if i == 1 and dbl else down, mk, lp)
        post(target, up, 0, lp)
    return target


def _send_counted(target: int, msg: int, wparam: int, lparam: int) -> None:
    """Message ENVOYÉ, compté dès la tentative comme un message posté accepté : sans réponse en 1 s, il a pu être
    traité en partie (relecture de Codex du 23/10) ; l'issue est alors inconnue, jamais suivie d'un vrai geste.
    Fenêtre disparue : refus prouvé, rien n'est compté."""
    if not w.IsWindow(target):
        raise MethodError("The window disappeared: nothing was sent.")
    _ACCEPTED[0] += 1
    if w.send_message(target, msg, wparam, lparam) is None:
        raise MethodError("The application did not process the message within 1 s.")


HELD_SEND_S = 1.0  # pression envoyée par le fil auxiliaire : attente du fil de travail, au plus
# Fils GUI dont une pression simulée n'est pas encore soldée (relâchement compris) : l'état simulé appartient au FIL,
# commun à toutes ses fenêtres (relecture de Codex du 23/10).
_PENDING_PRESS: set[int] = set()
_PENDING_LOCK = threading.Lock()


def _held_press(target: int, vk: int, down: int, up: int, mk: int, lparam: int) -> None:
    """Une pression et son relâchement, possédés par un fil AUXILIAIRE attaché à la cible : sous AttachThreadInput,
    le délai de SendMessageTimeout n'est pas garanti (files partagées, relecture de Codex du 23/10). L'auxiliaire
    envoie la pression avec `vk` simulé pour le seul fil de la cible, restaure l'état, puis envoie TOUJOURS le
    relâchement, même abandonné en route. Le fil de travail attend HELD_SEND_S au plus (arrêt d'urgence surveillé) ;
    au-delà, ou à l'arrêt, il abandonne : l'auxiliaire qui n'a pas encore envoyé n'envoie plus rien, celui qui est
    bloqué finit son relâchement seul ; tant qu'il n'a pas fini, aucune autre pression simulée vers cette cible."""
    thread = w.window_thread(target)
    with _PENDING_LOCK:
        if thread in _PENDING_PRESS:
            raise MethodError("A previous simulated press in this application is not finished yet: nothing was sent.")
        _PENDING_PRESS.add(thread)
    abandoned = threading.Event()
    done = threading.Event()
    outcome: list[str] = []

    def run() -> None:
        pressed = False
        try:
            if abandoned.is_set():
                return
            with fake_modifiers(target, [vk], 0.0):
                if abandoned.is_set():
                    return
                pressed = True
                _ACCEPTED[0] += 1  # tentative comptée avant l'envoi : sans réponse, issue inconnue
                if w.send_message(target, down, mk, lparam) is None:
                    outcome.append("The application did not process the press within 1 s.")
        except MethodError as error:
            outcome.append(str(error))
        except Exception as error:  # noqa: BLE001  (rendu au fil de travail)
            outcome.append(f"{type(error).__name__}: {error}")
        finally:
            try:
                if pressed:  # relâchement de nettoyage, état déjà restauré : toujours envoyé
                    _ACCEPTED[0] += 1
                    if w.send_message(target, up, 0, lparam) is None:
                        if not outcome:
                            outcome.append("The application did not process the release within 1 s.")
                        done.set()  # le fil de travail rend l'issue inconnue sans attendre la suite
                        # Relâchement peut-être annulé avant livraison, ou encore en traitement : rien ne prouve qu'il
                        # est soldé (une réponse à WM_NULL ne le prouve pas : relecture de Codex du 23/10). Le fil de
                        # l'application reste fermé aux pressions simulées tant que sa fenêtre existe ; seule la vraie
                        # souris, avec l'accord de l'utilisateur, peut encore y cliquer.
                        while w.IsWindow(target):
                            time.sleep(1.0)
            finally:
                with _PENDING_LOCK:
                    _PENDING_PRESS.discard(thread)
                done.set()

    threading.Thread(target=run, name="nexus-held-press", daemon=True).start()
    deadline = time.monotonic() + HELD_SEND_S
    try:
        while not done.wait(0.02):
            worker.check_cancelled()  # arrêt d'urgence : abandon, l'auxiliaire solde seul sa pression
            if time.monotonic() >= deadline:
                raise MethodError("The application did not process the press within 1 s.")
    except BaseException:
        abandoned.set()
        raise
    if outcome:
        raise MethodError(outcome[0])


# --- Barre de défilement native (Tk : tk.Scrollbar est une barre Windows) -----------------------------------------
WM_HSCROLL, WM_VSCROLL = 0x0114, 0x0115
SB_LINEUP, SB_LINEDOWN, SB_ENDSCROLL = 0, 1, 8  # SB_LINELEFT/SB_LINERIGHT : mêmes valeurs
SB_CTL, SIF_RANGE, SIF_PAGE, SIF_POS = 2, 0x1, 0x2, 0x4
SBS_VERT = 0x1


class _SCROLLINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint), ("fMask", ctypes.c_uint), ("nMin", ctypes.c_int), ("nMax", ctypes.c_int),
        ("nPage", ctypes.c_uint), ("nPos", ctypes.c_int), ("nTrackPos", ctypes.c_int),
    ]  # fmt: skip


def scroll_bar_state(bar: int) -> tuple[int, int, int, int] | None:
    """(min, max, page, pos) d'une barre de défilement native (SB_CTL), None si illisible."""
    info = _SCROLLINFO(ctypes.sizeof(_SCROLLINFO), SIF_RANGE | SIF_PAGE | SIF_POS)
    if not ctypes.windll.user32.GetScrollInfo(ctypes.c_void_p(bar), SB_CTL, ctypes.byref(info)):
        return None
    return info.nMin, info.nMax, info.nPage, info.nPos


def scroll_bar_beside(top: int, x: int, y: int, vertical: bool) -> int:
    """Barre de défilement native qui pilote le widget sous (x, y) : une barre visible du bon sens, sœur de ce widget
    (même parent), COLLÉE à lui (bord contre bord à 4 px près, sur toute sa hauteur pour une barre verticale, toute sa
    largeur sinon), comme tk.Scrollbar posée à côté de sa liste. Aucune, ou plusieurs : 0, rien n'est deviné (relecture
    de Codex du 23/10 : la plus proche du point pouvait piloter la liste voisine)."""
    under = w.deepest_child_at(top, x, y)
    if not under or win_class(under) == "scrollbar":
        return 0
    parent = w.handle(w.GetParent(under))
    siblings = [h for h in w.enum_children(top) if w.handle(w.GetParent(h)) == parent and w.IsWindowVisible(h)]

    def touches(bar_rect: w.Rect, box: w.Rect) -> bool:
        if vertical:
            side = abs(bar_rect.left - box.right) <= 4 or abs(box.left - bar_rect.right) <= 4
            return side and bar_rect.top <= box.top + 4 and bar_rect.bottom >= box.bottom - 4
        side = abs(bar_rect.top - box.bottom) <= 4 or abs(box.top - bar_rect.bottom) <= 4
        return side and bar_rect.left <= box.left + 4 and bar_rect.right >= box.right - 4

    def adjacent(bar_rect: w.Rect, box: w.Rect) -> bool:
        """Bord contre bord, avec un recouvrement sur l'autre axe (sans exiger que la barre couvre ce widget)."""
        if vertical:
            side = abs(bar_rect.left - box.right) <= 4 or abs(box.left - bar_rect.right) <= 4
            return side and bar_rect.top < box.bottom and box.top < bar_rect.bottom
        side = abs(bar_rect.top - box.bottom) <= 4 or abs(box.top - bar_rect.bottom) <= 4
        return side and bar_rect.left < box.right and box.left < bar_rect.right

    found: list[int] = []
    for bar in siblings:
        if win_class(bar) != "scrollbar" or bool(w.style(bar) & SBS_VERT) != vertical:
            continue
        r = w.window_rect(bar)
        if not touches(r, w.window_rect(under)):
            continue
        # Barre collée aussi à un AUTRE widget (entre deux listes), quelle que soit sa taille : on ne sait pas
        # laquelle elle pilote, refus (relecture de Codex du 23/10 : un voisin plus grand échappait au contrôle).
        others = [h for h in siblings if h not in (bar, under) and win_class(h) != "scrollbar"]
        if any(adjacent(r, w.window_rect(h)) for h in others):
            continue
        found.append(bar)
    return found[0] if len(found) == 1 else 0


def post_scroll_lines(bar: int, vertical: bool, forward: bool, lines: int) -> None:
    """Lignes de défilement comme un clic sur la flèche de la barre : WM_VSCROLL/WM_HSCROLL au parent de la barre
    (lParam = la barre), puis SB_ENDSCROLL."""
    parent = w.handle(w.GetParent(bar))
    msg = WM_VSCROLL if vertical else WM_HSCROLL
    code = SB_LINEDOWN if forward else SB_LINEUP
    for _ in range(lines):
        post(parent, msg, code, bar)
    post(parent, msg, SB_ENDSCROLL, bar)


def post_wheel(top: int, x: int, y: int, delta: int, horizontal: bool = False) -> int:
    """Molette par message : WM_MOUSEWHEEL porte des coordonnées écran."""
    target = w.deepest_child_at(top, x, y)
    msg = w.WM_MOUSEHWHEEL if horizontal else w.WM_MOUSEWHEEL
    post(target, msg, (delta & 0xFFFF) << 16, w.make_lparam(x, y))
    return target


OFFSCREEN_SETTLE_S, OFFSCREEN_HOLD_S = 0.15, 0.3  # mesurés le 27/10 (sonde du labo Tk recouvert)
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010


# Styles qui ne prouvent pas l'opacité : translucide (LAYERED), traversée (TRANSPARENT), composition sans bitmap
# (NOREDIRECTIONBITMAP : client possiblement entièrement transparent, relecture de Codex du 27/10).
NOT_OPAQUE = 0x00000020 | 0x00080000 | 0x00200000
WS_EX_LAYERED = 0x00080000


def _corner(hwnd: int) -> int:
    """Rayon des coins arrondis DWM de la fenêtre, majoré : 8 px à 100 %, linéaire avec l'échelle de son MONITEUR (12 à
    150 %, 16 à 200 %…), plus 2 px de marge. DPI du moniteur, pas celui de la fenêtre : GetDpiForWindow rend 96 pour
    une application non adaptée au DPI, le DPI système pour une application « system aware », quel que soit l'écran
    (relecture de Codex du 27/10). Illisible : 0, la fenêtre ne compte pas comme couverture."""
    dpi = w.monitor_dpi(hwnd, default=0)
    return -(-8 * max(dpi, w.GetDpiForWindow(hwnd)) // 96) + 2 if dpi else 0


class WindowNotRestored(RuntimeError):
    """La fenêtre n'a pas pu revenir à sa place : échec de l'étape, jamais une issue simplement incertaine (le défilement
    vu par expect ne la rend pas réussie ; relecture de Codex du 27/10)."""


def _minus(rect: w.Rect, cut: w.Rect) -> list[w.Rect]:
    """rect privé de cut : jusqu'à quatre rectangles (bandes haute, basse, gauche, droite)."""
    if cut.left >= rect.right or cut.right <= rect.left or cut.top >= rect.bottom or cut.bottom <= rect.top:
        return [rect]
    out = []
    if cut.top > rect.top:
        out.append(w.Rect(rect.left, rect.top, rect.right, cut.top))
    if cut.bottom < rect.bottom:
        out.append(w.Rect(rect.left, cut.bottom, rect.right, rect.bottom))
    top, bottom = max(rect.top, cut.top), min(rect.bottom, cut.bottom)
    if cut.left > rect.left:
        out.append(w.Rect(rect.left, top, cut.left, bottom))
    if cut.right < rect.right:
        out.append(w.Rect(cut.right, top, rect.right, bottom))
    return out


def fully_covered(top: int) -> bool:
    """Aucun pixel de la fenêtre n'est visible : son cadre, privé du cadre de chaque fenêtre au-dessus d'elle dans
    l'empilement réel, est vide. Prudence : seules les fenêtres visibles, non masquées par DWM, non réduites, sans style
    qui laisse voir à travers (NOT_OPAQUE), sans région SetWindowRgn (forme et trous inconnus) comptent comme
    couverture, coins arrondis DWM exclus à l'échelle de leur moniteur ; une bande visible, même d'un pixel, suffit à
    refuser (relectures de Codex du 27/10 : grille de points qui manquait une bande étroite, fenêtre en anneau)."""
    left = [w.frame_rect(top)]
    above = w.handle(w.GetWindow(top, w.GW_HWNDPREV))
    while above and left:
        if (
            w.IsWindowVisible(above)
            and not w.IsIconic(above)
            and not w.is_cloaked(above)
            and not w.ex_style(above) & NOT_OPAQUE
            and not w.has_window_region(above)
            and (k := _corner(above))  # échelle de son écran illisible : rayon des coins inconnu, jamais comptée
        ):
            c = w.frame_rect(above)
            # la croix du cadre sans ses quatre coins (arrondis par DWM, la fenêtre du dessous s'y voit)
            for cover in (
                w.Rect(c.left + k, c.top, c.right - k, c.bottom),
                w.Rect(c.left, c.top + k, c.right, c.bottom - k),
            ):
                left = [piece for rect in left for piece in _minus(rect, cover)]
        above = w.handle(w.GetWindow(above, w.GW_HWNDPREV))
    return not left


def _same_place(top: int, r: w.Rect) -> bool:
    now = w.window_rect(top)
    return (now.left, now.top, now.right, now.bottom) == (r.left, r.top, r.right, r.bottom)


def post_wheel_offscreen(top: int, x: int, y: int, delta: int, horizontal: bool = False) -> int:
    """Molette Tk d'une fenêtre entièrement recouverte : Tk renvoie la molette à la fenêtre visible AU POINT du message
    (tkWinX.c), si bien que recouverte, elle est perdue. La fenêtre est déplacée au-delà du bureau virtuel (sans
    activation ni changement d'ordre), là où rien ne la recouvre ; la molette y est postée au point correspondant, puis
    la fenêtre revient à sa place, relue au pixel près. Mesuré le 27/10 : molette reçue, premier plan et curseur
    intacts ; témoin recouvert : rien reçu. Refusé, rien n'est envoyé : fenêtre en partie visible, agrandie ou réduite,
    autre enfant au point une fois dehors, fenêtre ramenée au premier plan par l'utilisateur pendant le déplacement. Le délai tenu dehors n'est
    pas un accusé de traitement : l'effet reste relu à l'image. Limite : Tk calcule event.x/event.y depuis la position
    du curseur (GetMessagePos), pas depuis le message ; seuls delta et le widget visé sont sûrs."""
    if w.IsZoomed(top) or w.IsIconic(top):
        raise MethodError("A maximized or minimized window is never moved: nothing was sent.")
    if not fully_covered(top):
        raise MethodError("The window is partly visible: it is never moved off screen; nothing was sent.")
    r = w.window_rect(top)
    expected = w.deepest_child_at(top, x, y)  # enfant visé, relevé AVANT le déplacement (relecture de Codex)
    if not expected:
        raise MethodError("No child window of the target under the point: nothing was sent.")
    right = w.GetSystemMetrics(w.SM_XVIRTUALSCREEN) + w.GetSystemMetrics(w.SM_CXVIRTUALSCREEN)
    dx = right + 50 - r.left
    flags = SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
    thread = w.window_thread(top)
    worker.check_cancelled()  # arrêt d'urgence : la fenêtre n'a pas bougé
    posted = False
    failure: BaseException | None = None
    try:
        if not w.SetWindowPos(top, 0, r.left + dx, r.top, 0, 0, flags):
            raise MethodError("The window could not be moved off screen: nothing was sent.")
        time.sleep(OFFSCREEN_SETTLE_S)
        if (
            w.deepest_child_at(top, x + dx, y) != expected
            or w.handle(w.WindowFromPoint(w.POINT(x + dx, y))) != expected
        ):
            raise MethodError("Off screen, the point does not fall on the same control: nothing was sent.")
        worker.check_cancelled()
        # Dehors, la fenêtre n'est plus sous la souris de l'utilisateur ; seul un Alt+Tab peut l'atteindre : ramenée
        # au premier plan entre-temps, rien n'est envoyé (relecture de Codex du 27/10).
        front = w.foreground()
        if front and w.window_thread(front) == thread:
            raise MethodError("The user brought this window forward meanwhile: nothing was sent.")
        msg = w.WM_MOUSEHWHEEL if horizontal else w.WM_MOUSEWHEEL
        post(expected, msg, (delta & 0xFFFF) << 16, w.make_lparam(x + dx, y))
        posted = True
        time.sleep(OFFSCREEN_HOLD_S)
    except BaseException as error:  # noqa: BLE001  (rendue après le retour de la fenêtre)
        failure = error
    # Retour relu (rectangle entier : un changement de taille ou de DPI se voit aussi), quelle que soit l'issue ;
    # refusé ou corrigé par l'application (WM_WINDOWPOSCHANGING) : un second essai de placement, jamais une seconde molette.
    back = bool(w.SetWindowPos(top, 0, r.left, r.top, 0, 0, flags)) and _same_place(top, r)
    if not back:
        time.sleep(0.05)
        back = bool(w.SetWindowPos(top, 0, r.left, r.top, r.width, r.height, SWP_NOZORDER | SWP_NOACTIVATE))
        back = back and _same_place(top, r)
    if not back:
        sent = "the wheel was sent, its effect is unknown" if posted else "nothing was sent"
        raise WindowNotRestored(
            f"The window could not be put back exactly at ({r.left}, {r.top}): {sent}. Look at it before anything else."
        ) from failure
    if failure is not None:
        raise failure
    return expected


# --- Contrôles Win32 standard : notifications directes au parent ----------------------------------------------
def _notify_parent(control: int, code: int) -> None:
    parent = w.handle(w.GetParent(control))
    if not parent:
        raise MethodError("Control without a parent.")
    ident = w.GetDlgCtrlID(control) & 0xFFFF
    post(parent, w.WM_COMMAND, (code << 16) | ident, control)


def _send_notify(control: int, code: int) -> None:
    """Notification au parent ENVOYÉE (traitée avant le retour, dans l'ordre, et avant toute action suivante : un
    message posté passerait après un message envoyé ensuite, l'Invoke UIA de « Valider » par exemple). Parent qui ne
    répond pas en 2 s : rien d'autre, la relecture décide."""
    parent = w.handle(w.GetParent(control))
    if not parent:
        raise MethodError("Control without a parent.")
    ident = w.GetDlgCtrlID(control) & 0xFFFF
    w.send_message(parent, w.WM_COMMAND, (code << 16) | ident, control, timeout_ms=2000)


def _radio_group(control: int) -> list[int]:
    """Autres boutons radio automatiques du même groupe (entre deux WS_GROUP), comme le ferait un vrai clic."""
    parent = w.handle(w.GetParent(control))
    out: list[int] = []
    current = control
    for _ in range(64):
        current = w.handle(w.GetNextDlgGroupItem(parent, current, False))
        if not current or current == control:
            break
        if _is_class(current, BUTTON_CLASSES) and w.style(current) & BS_TYPEMASK == BS_AUTORADIOBUTTON:
            out.append(current)
    return out


def win32_button_click(control: int) -> str:
    """Bouton Win32/WinForms : notifie le parent comme un vrai clic (BN_CLICKED), coche gérée pour les cases auto."""
    if not _is_class(control, BUTTON_CLASSES):
        raise MethodError("Not a Win32 button.")
    if not w.IsWindowEnabled(control):
        raise MethodError("Button disabled.")
    kind = w.style(control) & BS_TYPEMASK
    if kind in (BS_AUTOCHECKBOX, BS_AUTO3STATE):
        state = w.send_message(control, w.BM_GETCHECK)
        if state is None:
            raise MethodError("Button not responding.")
        nxt = (state + 1) % (3 if kind == BS_AUTO3STATE else 2)
        w.send_message(control, w.BM_SETCHECK, nxt)
    elif kind == BS_AUTORADIOBUTTON:
        for other in _radio_group(control):
            w.send_message(other, w.BM_SETCHECK, 0)
        w.send_message(control, w.BM_SETCHECK, 1)
    _notify_parent(control, w.BN_CLICKED)
    return "bn_clicked"


def win32_tab_select(control: int, index: int) -> str:
    """Onglet Win32/WinForms : TCM_SETCURFOCUS change l'onglet choisi ET notifie le parent (TCN_SELCHANGING/CHANGE),
    comme un clic, sans souris. (UIA Legacy DoDefaultAction y ferait un VRAI clic : curseur de l'utilisateur déplacé.)"""
    if not _is_class(control, TAB_CLASSES):
        raise MethodError("Not a Win32 tab control.")
    count = w.send_message(control, TCM_GETITEMCOUNT)
    if count is None or not 0 <= index < count:
        raise MethodError("Tab not found in the control.")
    w.send_message(control, TCM_SETCURFOCUS, index)
    if w.send_message(control, TCM_GETCURSEL) != index:
        raise MethodError("The tab was not selected (read back).")
    return "read back ✓"


def _item_texts(control: int, count_msg: int, len_msg: int, text_msg: int) -> list[str]:
    count = w.send_message(control, count_msg) or 0
    items = []
    for i in range(min(count, 2000)):
        length = w.send_message(control, len_msg, i)
        if length is None or length > 0x7FFF:
            items.append("")
            continue
        buf = ctypes.create_unicode_buffer(length + 2)
        w.send_message(control, text_msg, i, ctypes.addressof(buf))
        items.append(buf.value)
    return items


class AmbiguousOption(MethodError):
    """Plusieurs options correspondent : on refuse plutôt que de choisir au hasard."""


def match_option(items: list[str], wanted: str) -> tuple[int, bool]:
    """Index de l'option voulue et vrai si la correspondance n'est que partielle (unique, sinon refus)."""
    low = " ".join(wanted.split()).lower()
    exact = [i for i, item in enumerate(items) if " ".join(item.split()).lower() == low]
    if len(exact) == 1:
        return exact[0], False
    if exact:
        raise AmbiguousOption(f'"{wanted}" appears {len(exact)} times in the list: cannot tell which one.')
    partial = [i for i, item in enumerate(items) if low and low in item.lower()]
    if len(partial) == 1:
        return partial[0], True
    if partial:
        names = ", ".join(f'"{items[i]}"' for i in partial[:8])
        raise AmbiguousOption(f'"{wanted}" is ambiguous: {names}. Give the exact label.')
    raise MethodError(f'Option "{wanted}" not found ({", ".join(items[:20])}).')


def _partial_note(items: list[str], index: int, partial: bool) -> str:
    return f'partial match "{items[index]}"' if partial else ""


LBS_MULTIPLESEL, LBS_EXTENDEDSEL = 0x0008, 0x0800


def win32_combo_select(control: int, option: str) -> str:
    """Option d'une liste déroulante Win32/WinForms par son libellé (lu par CB_GETLBTEXT), liste ouverte ou non."""
    if not _is_class(control, COMBO_CLASSES):
        raise MethodError("Not a Win32 combo box.")
    items = _item_texts(control, CB_GETCOUNT, CB_GETLBTEXTLEN, CB_GETLBTEXT)
    index, partial = match_option(items, option)
    detail = _combo_commit(control, index, items[index])
    return " ; ".join(filter(None, [detail, _partial_note(items, index, partial)]))


def win32_listbox_select(control: int, option: str) -> str:
    if not _is_class(control, LISTBOX_CLASSES):
        raise MethodError("Not a Win32 list box.")
    if w.style(control) & (LBS_MULTIPLESEL | LBS_EXTENDEDSEL):
        raise MethodError("Multiple-selection list: LB_SETCURSEL does not apply.")
    items = _item_texts(control, LB_GETCOUNT, LB_GETTEXTLEN, LB_GETTEXT)
    index, partial = match_option(items, option)
    w.send_message(control, LB_SETCURSEL, index)
    if w.send_message(control, LB_GETCURSEL) != index:
        raise MethodError("The list did not take the selection.")
    _notify_parent(control, LBN_SELCHANGE)
    return " ; ".join(filter(None, ["read back ✓", _partial_note(items, index, partial)]))


def win32_edit_replace(control: int, text: str, append: bool = False) -> str:
    """Champ Edit/RichEdit : remplace (ou complète) le texte comme une saisie (EN_CHANGE, annulation possible)."""
    if not _is_class(control, EDIT_CLASSES):
        raise MethodError("Not a Win32 Edit field.")
    if w.style(control) & w.ES_READONLY:
        raise MethodError("Read-only field.")
    if append:
        w.send_message(control, w.EM_SETSEL, 0x7FFFFFFF, 0x7FFFFFFF)
    else:
        w.send_message(control, w.EM_SETSEL, 0, -1)
    buf = ctypes.create_unicode_buffer(text.replace("\r\n", "\n").replace("\n", "\r\n"))
    if w.send_message(control, w.EM_REPLACESEL, 1, ctypes.addressof(buf), timeout_ms=2000) is None:
        raise MethodError("The field is not responding.")
    return "em_replacesel"


# --- Menus Win32 (HMENU) ------------------------------------------------------------------------------------
_GetMenu = w._fn(w.user32, "GetMenu", w.w.HMENU, w.w.HWND)
_GetMenuItemCount = w._fn(w.user32, "GetMenuItemCount", ctypes.c_int, w.w.HMENU)
_GetMenuItemID = w._fn(w.user32, "GetMenuItemID", w.w.UINT, w.w.HMENU, ctypes.c_int)
_GetSubMenu = w._fn(w.user32, "GetSubMenu", w.w.HMENU, w.w.HMENU, ctypes.c_int)
_GetMenuStringW = w._fn(
    w.user32, "GetMenuStringW", ctypes.c_int, w.w.HMENU, w.w.UINT, w.w.LPWSTR, ctypes.c_int, w.w.UINT
)
_GetMenuState = w._fn(w.user32, "GetMenuState", w.w.UINT, w.w.HMENU, w.w.UINT, w.w.UINT)
MF_BYPOSITION, MF_GRAYED, MF_DISABLED = 0x400, 0x1, 0x2


def _menu_label(menu, pos: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _GetMenuStringW(menu, pos, buf, 256, MF_BYPOSITION)
    return buf.value.replace("&", "").split("\t")[0].strip()


def win32_menu_items(top: int) -> list[tuple[str, int]]:
    """Chemins « Fichier > Enregistrer » → identifiant de commande, pour une barre de menus Win32 classique."""
    out: list[tuple[str, int]] = []

    def walk(menu, prefix: str, depth: int) -> None:
        for pos in range(max(0, _GetMenuItemCount(menu))):
            label = _menu_label(menu, pos)
            path = f"{prefix} > {label}" if prefix else label
            sub = _GetSubMenu(menu, pos)
            if sub and depth < 6:
                walk(sub, path, depth + 1)
            else:
                ident = _GetMenuItemID(menu, pos)
                state = _GetMenuState(menu, pos, MF_BYPOSITION)
                if ident not in (0, 0xFFFFFFFF) and label and not state & (MF_GRAYED | MF_DISABLED):
                    out.append((path, ident))

    menu = _GetMenu(top)
    if menu:
        walk(menu, "", 0)
    return out


def _menu_key(label: str) -> str:
    """Entrée de menu comparable : casse, espaces et points de suspension finaux ignorés (« Enregistrer sous... »
    est demandé « Enregistrer sous »)."""
    return " ".join(label.replace("&", "").split()).rstrip(".…").strip().lower()


def win32_menu_command(top: int, path: str) -> str:
    """Commande de menu par son chemin : correspondance exacte d'abord (casse et espaces ignorés), puis sans points de
    suspension, seulement si elle est unique (« File > Run » et « File > Run... » : chacun garde la sienne)."""
    items = win32_menu_items(top)
    if not items:
        raise MethodError("No Win32 menu bar.")
    raw = [p for p in path.replace("/", ">").split(">") if p.strip()]

    def matching(key) -> list[tuple[str, int]]:
        wanted = [key(p) for p in raw]
        found = []
        for label, ident in items:
            parts = [key(p) for p in label.split(">")]
            if parts[-len(wanted) :] == wanted or (len(wanted) == 1 and parts[-1] == wanted[0]):
                found.append((label, ident))
        return found

    for key in (lambda t: " ".join(t.split()).lower(), _menu_key):
        found = matching(key)
        if len(found) > 1:
            raise MethodError(f'Menu command "{path}" ambiguous ({", ".join(label for label, _ in found[:5])}).')
        if found:
            worker.check_cancelled()  # énumération du menu possiblement longue : arrêt relu au dernier moment
            post(top, w.WM_COMMAND, found[0][1] & 0xFFFF, 0)
            return "wm_command_menu"
    raise MethodError(f'Menu command "{path}" not found.')


# --- Clavier par messages -----------------------------------------------------------------------------------
def _key_lparam(vk: int, up: bool, extended: bool = False) -> int:
    scan = w.MapVirtualKeyW(vk, 0) & 0xFF
    lp = 1 | (scan << 16) | ((1 << 24) if extended else 0)
    if up:
        lp |= (1 << 30) | (1 << 31)
    return lp


CHECK_EVERY = 16  # arrêt d'urgence et délai dépassé contrôlés tous les CHECK_EVERY caractères (lecture de fichier)


def _post_char(target: int, ch: str, enter_as_key: bool) -> None:
    if ch == "\n" and enter_as_key:
        post_key(target, 0x0D)
    elif ch == "\n":
        post(target, w.WM_CHAR, 0x0D, 1)
    elif ch == "\t":
        post(target, w.WM_CHAR, 0x09, 1)
    else:
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):
            post(target, w.WM_CHAR, int.from_bytes(data[i : i + 2], "little"), 1)


def post_chars(target: int, text: str, check: Callable[[], None] | None = None) -> int:
    """Texte par WM_CHAR (unités UTF-16), Tab et Entrée en WM_CHAR (Edit, RichEdit, WPF ; texte court sans saut de
    ligne en Chromium, qui n'insère le saut de ligne qu'à partir de la touche : un texte Chromium passe par
    type_in_slices, qui relit chaque Entrée et confirme chaque tranche).

    check : contrôle appelé avant CHAQUE caractère ; par défaut celui de l'activation brève en cours sur ce fil (à la
    première entrée physique de l'utilisateur : premier plan rendu, puis foreground.UserInterrupted).
    """
    checkpoint = check or foreground.check_activation
    sent = 0
    for ch in text.replace("\r\n", "\n"):
        if sent % CHECK_EVERY == 0:
            worker.check_cancelled()  # arrêt d'urgence ou délai dépassé : rien de plus n'est envoyé
        checkpoint()
        _post_char(target, ch, False)
        sent += 1
    return sent


def post_key(target: int, vk: int) -> None:
    """Touche par messages : WM_KEYDOWN puis WM_KEYUP. Pas de WM_CHAR en plus : la boucle de messages de
    l'application le fabrique elle-même (TranslateMessage), l'ajouter doublerait Entrée, Tab ou la lettre."""
    post(target, w.WM_KEYDOWN, vk, _key_lparam(vk, False))
    post(target, w.WM_KEYUP, vk, _key_lparam(vk, True))


class fake_modifiers:
    """Fait croire au thread cible que des touches de modification sont enfoncées (Ctrl, Alt, Maj).

    GetKeyState lit l'état clavier de la file d'entrée du thread : on s'attache brièvement à la file du thread
    cible (jamais à celui de l'utilisateur), on y pose l'état voulu, puis on restaure et on se détache.
    """

    def __init__(self, target: int, vks: list[int], hold_s: float = 0.15) -> None:
        self.hold_s = hold_s  # état tenu après les touches postées, le temps que la cible les traite
        self.target = target
        self.thread = w.window_thread(target)
        self.own = w.GetCurrentThreadId()
        self.vks = vks
        self.saved = (ctypes.c_ubyte * 256)()
        self.attached = False

    def __enter__(self):
        fg_thread = w.window_thread(w.foreground()) if w.foreground() else 0
        if self.thread == fg_thread:
            raise MethodError("The target thread is the foreground thread: simulated modifiers refused.")
        self.attached = bool(w.AttachThreadInput(self.own, self.thread, True))
        if not self.attached:
            raise MethodError("AttachThreadInput refused.")
        w.GetKeyboardState(ctypes.byref(self.saved))
        state = (ctypes.c_ubyte * 256)(*self.saved)
        for vk in self.vks:
            state[vk] = 0x80
        w.SetKeyboardState(ctypes.byref(state))
        return self

    def __exit__(self, *exc) -> None:
        if not self.attached:
            return
        try:
            # Laisser la cible traiter les touches postées pendant que l'état est posé : WaitForInputIdle (2 s au plus)
            # puis au moins hold_s. Ce n'est PAS un accusé de traitement (WaitForInputIdle revient aussitôt si le
            # processus a déjà été inactif une fois : relecture de Codex du 23/10) : une cible très occupée peut lire
            # la touche après la restauration ; l'effet reste relu à l'image, jamais annoncé sans preuve.
            started = time.monotonic()
            with contextlib.suppress(OSError):
                w.wait_input_idle(self.target, 2000)
            remaining = self.hold_s - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
        finally:
            try:
                # Seules les touches simulées reprennent leur valeur d'avant ; les autres, lues à l'instant, restent ce
                # que l'utilisateur tient réellement (relectures de Codex du 23/10). Cible prise entre-temps par
                # l'utilisateur : une touche simulée reprend son état PHYSIQUE (le même Ctrl tenu par lui reste tenu).
                # Lecture impossible : l'état d'avant, entier.
                now = (ctypes.c_ubyte * 256)()
                if not w.GetKeyboardState(ctypes.byref(now)):
                    ctypes.memmove(now, self.saved, 256)
                front = w.foreground()
                taken = bool(front) and w.window_thread(front) == self.thread
                for vk in self.vks:
                    if taken:
                        held = w.GetAsyncKeyState(vk) & 0x8000
                        now[vk] = (0x80 if held else 0) | (self.saved[vk] & 0x01)
                    else:
                        now[vk] = self.saved[vk]
                w.SetKeyboardState(ctypes.byref(now))
            finally:
                w.AttachThreadInput(self.own, self.thread, False)
                self.attached = False


def post_combo(target: int, vks: list[int], modifiers: list[int], hold_s: float = 0.15) -> None:
    """Raccourci par messages : modificateurs simulés le temps du traitement, touches postées. hold_s : durée où
    l'état reste posé après l'envoi (Chromium lit Ctrl et Alt par GetKeyState au traitement de la touche : fenêtre
    occultée ou fil occupé, 0,15 s ne suffit pas toujours, mesuré)."""
    with fake_modifiers(target, modifiers, hold_s):
        for vk in modifiers:
            post(target, w.WM_KEYDOWN, vk, _key_lparam(vk, False))
        for vk in vks:
            post(target, w.WM_KEYDOWN, vk, _key_lparam(vk, False))
            post(target, w.WM_KEYUP, vk, _key_lparam(vk, True))
        for vk in reversed(modifiers):
            post(target, w.WM_KEYUP, vk, _key_lparam(vk, True))


def keyboard_target(top: int, windows: set[int] | None = None) -> int:
    """Fenêtre qui reçoit le clavier dans la cible : focus du thread de la cible s'il est dans une de ses fenêtres
    (fenêtre principale, enfant, dialogue possédé), sinon la fenêtre principale elle-même. Jamais une fenêtre de
    l'utilisateur, même si elle partage le thread."""
    allowed = windows or {top}
    info = w.focus_info(top)
    if info is not None:
        focus = w.handle(info.hwndFocus)
        if focus and (focus in allowed or w.root(focus) in allowed or w.IsChild(top, focus)):
            return focus
    return top


# --- Activation simulée (Chromium sans DevTools, sans activation réelle) -----------------------------------------
# Mesuré le 15 octobre 2026 (bench/chromium_input_probe.py, Electron 44 et Edge 155, fenêtre inactive et recouverte) :
# Chromium jette le clavier d'une fenêtre inactive, quel que soit le canal (WM_CHAR à la fenêtre principale ou à
# Chrome_RenderWidgetHostHWND, KEYDOWN+CHAR à lParam complet, WM_IME_CHAR, WM_UNICHAR, état clavier attaché). Mais il
# tient son état d'activation des MESSAGES qu'il reçoit (WM_NCACTIVATE et WM_ACTIVATE), pas de Windows : ces messages
# envoyés à sa seule fenêtre, sans activation réelle, suffisent pour que WM_CHAR et WM_KEYDOWN produisent des
# événements keydown, beforeinput et input DE CONFIANCE dans <input>, <textarea> et contenteditable, Entrée et
# raccourcis Ctrl (état clavier simulé) compris, premier plan de Julien intact. Focus DOM sans activation : action
# MSAA accSelect(SELFLAG_TAKEFOCUS) (UIA SetFocus, lui, active la fenêtre ; une sélection TextPattern ne focalise pas).
# Une seule réserve, mesurée : une application lancée À L'INSTANT par un processus au premier plan garde le droit de
# prendre le premier plan une fois ; sa toute première activation simulée l'utilise (premier plan pris puis rendu en
# ≈10 ms par la garde). Cette première fois par processus est donc encadrée comme une activation brève (jeton commun,
# pause de Julien attendue) ; ensuite, aucune précaution n'est nécessaire.
WM_ACTIVATE, WM_NCACTIVATE = 0x0006, 0x0086
WM_SETFOCUS, WM_KILLFOCUS = 0x0007, 0x0008  # focus simulé de Tk (act_uia.press)
WA_INACTIVE, WA_ACTIVE = 0, 1
# Fenêtre de premier niveau d'un navigateur ou d'une application Electron (HWNDMessageHandler de Chromium). Steam
# (CEF dans un cadre SDL) n'en est pas : ses messages d'activation n'atteignent pas CEF, activation brève inchangée.
CHROMIUM_TOP_CLASS = "Chrome_WidgetWin_"
# Tranche de frappe en activation simulée : le premier plan de Julien n'est jamais tenu, la tranche borne seulement
# ce qui part sans être encore relu. Même cadence que les activations brèves (≈60 caractères au départ, puis selon le
# débit mesuré) : mesuré sur une page lourde (1 500 éléments reconstruits à chaque caractère), des lots de 200
# arrivaient tous dans la page mais sa valeur UIA ne se publiait plus à temps (« 0/200 relus »).
SIM_SLICE_S = 0.5  # = SLICE_S
SIM_MODIFIERS_HOLD_S = 0.5  # Ctrl/Alt simulés tenus le temps que Chromium traite la touche (occulté : lent)
SIM_SETTLE_S = 0.03  # après la désactivation postée : traitée avant toute nouvelle activation (envoyée, prioritaire)
_PRIMED: set[int] = set()  # processus déjà passés par une activation simulée (droit de premier plan consommé)
FIRST_FOCUS_SETTLE_S = 0.1  # première prise de focus : délai laissé à un éventuel usage du droit de premier plan
FIRST_FOCUS_MAX_S = 0.6  # ... et à sa restitution par la garde


class SimulatedActivation:
    """Activation simulée en cours. Les touches de l'utilisateur vont à SA fenêtre, jamais à la cible : rien à interrompre,
    sauf s'il a lui-même mis la cible au premier plan pendant la frappe (ses touches s'y mêleraient)."""

    def __init__(self, top: int, windows: set[int], first: bool = False) -> None:
        self.top = top
        self.windows = windows
        self.started = time.monotonic()
        self.first = first  # première activation simulée de ce processus : droit de premier plan peut-être pas consommé

    def settle_first(self) -> bool:
        """Après la première prise de focus de ce processus : son droit de premier plan de lancement a pu servir
        (premier plan pris, rendu en ≈10 ms par la garde). Chromium a alors reçu une VRAIE désactivation et jette le
        clavier qui suit (mesuré le 20/10 : « 0/30 relus », à chaque première frappe d'une application lancée depuis
        le programme au premier plan). On attend que le premier plan soit rendu, puis l'activation simulée est
        renvoyée ; rien n'a encore été tapé. Vrai si elle l'a été (le focus est alors à reprendre)."""
        if not self.first:
            return False
        self.first = False
        begun = time.monotonic()
        while True:
            held = w.root(w.foreground()) in self.windows
            elapsed = time.monotonic() - begun
            if (elapsed >= FIRST_FOCUS_SETTLE_S and not held) or elapsed >= FIRST_FOCUS_MAX_S:
                break
            time.sleep(0.005)
        if held:
            return False  # tenu par la cible (l'utilisateur l'a choisie) : la frappe ordinaire s'en charge
        if (
            w.send_message(self.top, WM_NCACTIVATE, 1, 0) is None
            or w.send_message(self.top, WM_ACTIVATE, WA_ACTIVE, 0) is None
        ):
            raise MethodError("the window does not respond to activation messages")
        return True

    def _user_in_target(self) -> bool:
        return w.root(w.foreground()) in self.windows and physical.available() and physical.since(self.started)

    def check(self, consequence: str) -> None:
        if self._user_in_target():
            raise foreground.UserInterrupted(f"The user switched to the window and is typing in it: {consequence}.")

    def checkpoint(self) -> None:
        self.check("typing stopped; the user's keystrokes may have reached the target: check the target (look)")


@contextlib.contextmanager
def simulated_activation(top: int, windows: set[int] | None = None) -> Iterator[SimulatedActivation]:
    """Bloc où la fenêtre Chromium `top` se croit active sans l'être : WM_NCACTIVATE puis WM_ACTIVATE envoyés
    (synchrones), désactivation POSTÉE en sortie, donc traitée après tout ce qui a été posté dans le bloc (des messages
    envoyés passeraient devant les caractères encore en file, qui seraient jetés). Fenêtre réellement au premier plan
    (choisie par l'utilisateur) : MethodError, la frappe ordinaire s'applique. Première fois pour ce processus : jeton commun et
    pause de l'utilisateur (son droit de premier plan de lancement peut être utilisé, voir plus haut)."""
    windows = windows or {top}
    if not w.IsWindow(top) or not w.class_name(top).startswith(CHROMIUM_TOP_CLASS):
        raise MethodError("not a top-level Chromium window: simulated activation not applicable")
    if w.root(w.foreground()) in windows:
        raise MethodError("the window is in the foreground: simulated activation not needed")
    pid = w.window_pid(top)
    first = pid not in _PRIMED
    with contextlib.ExitStack() as stack:
        if first:
            stack.enter_context(foreground.agent_token("simulated activation (first time)"))
            if not foreground.wait_typing_pause():
                raise foreground.BusyUser(
                    "The user is typing, moving the mouse or holding a button right now: first typing without activation "
                    "postponed (the application can still take the foreground once)."
                )
        if w.send_message(top, WM_NCACTIVATE, 1, 0) is None or w.send_message(top, WM_ACTIVATE, WA_ACTIVE, 0) is None:
            raise MethodError("the window does not respond to activation messages")
        _PRIMED.add(pid)
        try:
            yield SimulatedActivation(top, windows, first)
        finally:
            if w.root(w.foreground()) not in windows:  # Julien ne l'a pas choisie entre-temps : état d'origine
                with contextlib.suppress(MethodError):
                    post(top, WM_ACTIVATE, WA_INACTIVE, 0)
                    post(top, WM_NCACTIVATE, 0, 0)
                time.sleep(SIM_SETTLE_S)


# --- Frappe par tranches d'activation brève ------------------------------------------------------------------
# Chromium sans DevTools ne lit le clavier que fenêtre active et publie sa valeur UIA en retard, par paquets (paliers
# de 50 à 420 ms mesurés), d'autant plus tard que sa page est occupée. Règle unique, qui rend un doublon impossible :
# un lot de caractères n'est envoyé que si TOUT ce qui le précède a été relu dans le champ, et rien n'est jamais
# renvoyé (un caractère pas encore relu a pu n'être que retardé).
# Chaque tranche envoie ce qu'elle peut relire avant son échéance et rend le premier plan à l'échéance ; ce qui n'est
# pas encore relu est attendu hors activation ; s'il ne vient pas, la frappe s'arrête et le dit (texte partiel, ✗).
SLICE_S = 0.5  # activation la plus longue d'une tranche, restitution comprise : le premier plan de Julien jamais plus
GIVE_BACK_S = 0.2  # fin de tranche réservée au départ à la restitution (mesuré en charge : jusqu'à 0,29 s)
MIN_POST_S = 0.1  # reste de tranche en deçà duquel rien n'est envoyé (préparation trop longue)
CHAR_COST_S = 0.003  # coût initial d'un caractère : traitement par l'application (mesuré en Chromium : 0,7 à 2 ms)
PHI = 1.618  # marge sur les estimations (constante harmonique)
# Coût le plus haut retenu : le retard fixe de publication UIA, rapporté à un petit lot, gonflerait sinon le coût
# d'un caractère jusqu'à des lots d'un caractère.
MAX_COST_S = CHAR_COST_S * PHI * PHI * PHI
READ_POLL_S = 0.02  # relecture pendant une tranche
WAIT_POLL_S = 0.05  # relecture hors activation
WAIT_IDLE_S = 3.0  # hors activation : attente la plus longue SANS progrès de ce qui a été envoyé
WAIT_MAX_S = 8.0  # hors activation : attente la plus longue au total
STARVED_SLICES = 2  # arrêt après deux tranches sans le temps d'envoyer quoi que ce soit
VK_DELETE = 0x2E


class Unreadable(MethodError):
    """La valeur du champ ne se relit pas : la frappe ne peut être ni confirmée ni poursuivie."""


def norm_text(value: str | None) -> str:
    """Texte comparable d'un champ : sauts de ligne en « \\n », espaces insécables en espaces."""
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")


def _head(rest: str, want: str) -> int | None:
    """Longueur du début de `want` égal à `rest`, ou None. Des sauts de ligne en trop à la fin sont un paragraphe vide
    d'éditeur riche (« <p><br></p> » se lit « \\n ») : comptés seulement s'ils sont dans le texte à cet endroit.
    L'appelant borne ce compte à ce qu'il a envoyé : un paragraphe vide n'est pas une Entrée tapée."""
    if want.startswith(rest):
        return len(rest)
    core = rest.rstrip("\n")
    if not want.startswith(core):
        return None
    tail = want[len(core) :]
    return len(core) + min(len(rest) - len(core), len(tail) - len(tail.lstrip("\n")))


def _rich_head(rest: str, want: str, bounded_left: bool = False) -> int | None:
    """Comme _head, pour la valeur que Chromium publie d'un éditeur riche à paragraphes (mesuré le 2026-10-14) : un
    paragraphe vide entre deux paragraphes de texte n'y paraît pas (« <p>A</p><p><br></p><p>V</p> » se lit « A\\nV »),
    donc n sauts de ligne tapés entre deux textes se relisent n − 1 (n ≥ 2). En tête (ou après un contenu qui finit
    par un saut de ligne) et en fin, les paragraphes vides se lisent un « \\n » chacun. bounded_left : du texte précède
    `want` dans le champ (ajout). La lecture est ambiguë (« A\\nV » est aussi « A », Entrée, « V ») ; l'appelant ne
    tape la suite d'une Entrée qu'une fois la valeur changée par elle, ce qui lève l'ambiguïté."""
    core = rest.rstrip("\n")
    i = j = 0
    while j < len(core):
        if i >= len(want):
            return None
        if want[i] == "\n":
            run = len(want[i:]) - len(want[i:].lstrip("\n"))
            if i + run >= len(want):
                return None  # sauts de ligne de fin du texte : jamais suivis d'un caractère dans le champ
            shown = run - 1 if run >= 2 and (i > 0 or bounded_left) else run
            if core[j : j + shown] != "\n" * shown:
                return None
            i, j = i + run, j + shown
        elif core[j] == want[i]:
            i, j = i + 1, j + 1
        else:
            return None
    tail = want[i:]
    return i + min(len(rest) - len(core), len(tail) - len(tail.lstrip("\n")))


def typed_prefix(value: str | None, text: str, before: str = "", append: bool = False) -> int | None:
    """Combien du début de `text` le champ contient déjà. Remplacement : la valeur entière ; ajout : ce qui suit le
    contenu d'avant, dont les paragraphes vides de la fin ont pu être remplis (un éditeur riche vide se lit « \\n »).
    Valeur lue telle quelle, sinon sous la forme publiée par un éditeur riche Chromium (_rich_head).
    None : la valeur n'est pas un début du texte (autre contenu, doublon, ordre inversé)."""
    got, want = norm_text(value), norm_text(text)
    bases = [""]
    if append:
        old = norm_text(before)
        core = old.rstrip("\n")
        bases = [old[:end] for end in range(len(old), len(core) - 1, -1)]
        if not old.strip() and "" not in bases:
            bases.append("")
    for base in bases:
        if got.startswith(base):
            rest = got[len(base) :]
            head = _head(rest, want)
            if head is None:
                head = _rich_head(rest, want, bounded_left=bool(base) and not base.endswith("\n"))
            if head is not None:
                return head
    return None


@dataclass
class Typed:
    """Compte rendu d'une frappe par tranches (sauts de ligne normalisés en « \\n »). Rien n'y est supposé : confirmed
    est le début du texte RELU dans le champ, sent ce qui a été envoyé (jamais renvoyé)."""

    confirmed: int
    total: int
    slices: int = 0  # activations brèves utilisées
    active_ms: int = 0  # durée cumulée de ces activations, restitution du premier plan comprise
    slice_s: float = SLICE_S
    stopped: str = ""  # cause d'un arrêt avant la fin : le texte n'est alors que partiellement tapé
    longest_ms: int = 0  # tranche la plus longue, de la préparation à la restitution du premier plan
    sent: int = 0
    clear: bool = False  # remplacement d'un contenu d'origine
    erased: bool = False  # contenu d'origine effacé
    simulated: bool = False  # activation simulée : premier plan de Julien jamais pris

    @property
    def complete(self) -> bool:
        """Tout le texte relu dans le champ par la dernière relecture."""
        return not self.stopped and self.confirmed >= self.total

    def state(self) -> str:
        """Ce que contient le champ, d'après les relectures (pour un arrêt ou une interruption)."""
        parts = [f"{self.confirmed}/{self.total} characters read back in the field"]
        if self.sent > self.confirmed:
            parts.append(f"{self.sent - self.confirmed} more sent but not read back (they may still appear)")
        if self.clear:
            parts.append("original content erased" if self.erased else "original content intact")
        parts.append("no character resent")
        return ", ".join(parts)

    def detail(self) -> str:
        if self.stopped:
            return f"typing stopped ({self.state()}): {self.stopped}"
        if self.simulated:
            return (
                "WM_CHAR keystrokes without activation (activation simulated by messages, the user's foreground intact)"
            )
        if self.slices <= 1:
            return f"WM_CHAR keystrokes, brief activation {self.active_ms} ms"
        return (
            f"WM_CHAR keystrokes in {self.slices} brief activations (longest {self.longest_ms} ms, {self.active_ms} "
            "ms in total, foreground given back to the user between each); long text: launch with devtools=true types it "
            "without any activation"
        )


def _seconds(value: float) -> str:
    return f"{value:g} s"


class _Stop(Exception):  # noqa: N818 - signal interne : arrêt de la frappe, avec sa cause
    pass


def type_in_slices(
    hwnd: int,
    windows: set[int],
    text: str,
    prepare: Callable[[bool], int],
    read: Callable[[], str],
    prefix: Callable[[str], int | None],
    enter_as_key: bool = False,
    slice_s: float = SLICE_S,
    clear: bool = False,
    activation: Callable[[], contextlib.AbstractContextManager] | None = None,
) -> Typed:
    """Frappe WM_CHAR d'un texte dans une suite d'activations brèves d'au plus slice_s chacune, restitution comprise.

    read() : valeur du champ (normalisée), Unreadable si elle ne se relit pas. prefix(valeur) : combien du début du
    texte elle contient, -1 tant que le contenu d'origine n'est pas effacé (clear), None si elle n'est pas un début du
    texte. prepare(True) : focus et sélection (remplacement) ou curseur en fin (ajout) ; prepare(False) : seulement
    réactiver la cible ; renvoie la fenêtre qui reçoit le clavier, MethodError si elle ne peut pas.

    Invariant : un lot n'est envoyé que si tout le texte avant lui est relu dans le champ ; rien n'est jamais renvoyé
    (un caractère non relu a pu être seulement retardé). clear : le contenu d'origine sélectionné est effacé
    (Suppr) et relu vide avant tout caractère. En Chromium (enter_as_key), chaque Entrée est relue (valeur changée)
    avant la suite : son saut de ligne arrive en fin de file. Contrôle de l'utilisateur à chaque caractère et à chaque
    relecture (premier plan rendu, UserInterrupted).

    Arrêt (Typed.stopped, texte partiel) : ce qui a été envoyé n'est pas relu à temps hors activation, valeur
    illisible, qui n'est pas un début du texte ou qui recule, tranches trop courtes pour envoyer, l'utilisateur occupé. Lève
    BusyUser ou MethodError si aucune touche n'est partie (une autre technique peut être essayée), UserInterrupted
    (avec l'état du champ) à une entrée physique de l'utilisateur, worker.Stopped / Cancelled.
    """
    return _SliceTyping(hwnd, windows, text, prepare, read, prefix, enter_as_key, slice_s, clear, activation).run()


class _SliceTyping:
    """État d'une frappe par tranches (type_in_slices)."""

    def __init__(self, hwnd, windows, text, prepare, read, prefix, enter_as_key, slice_s, clear, activation) -> None:
        self.hwnd, self.windows, self.prepare, self.read, self.prefix = hwnd, windows, prepare, read, prefix
        self.text = text.replace("\r\n", "\n")
        self.enter_as_key, self.slice_s, self.clear = enter_as_key, slice_s, clear
        self.activation = activation or (lambda: foreground.brief_activation(self.hwnd, self.windows))
        self.out = Typed(0, len(self.text), slice_s=slice_s, clear=clear, simulated=activation is not None)
        self.min_post = min(MIN_POST_S, slice_s / 5)
        self.k = -1 if clear else 0  # début relu dans le champ ; -1 : contenu d'origine pas encore effacé
        self.sent = 0  # text[:sent] envoyé ; jamais diminué : rien n'est renvoyé
        self.before_enter: str | None = None  # valeur relue avant la dernière Entrée pas encore vue
        self.value = ""
        self.cost = CHAR_COST_S
        self.reserve = min(GIVE_BACK_S, slice_s / 2)
        self.read_s = (
            0.0  # durée de la dernière relecture : aucune relecture commencée si elle finirait après l'échéance
        )
        self.active = 0.0
        self.touched = False  # une touche est partie : un empêchement n'est plus une exception, c'est un arrêt rendu

    # --- Relecture --------------------------------------------------------------------------------------------------
    def _look(self) -> int:
        """Relit le champ ; met à jour k (borné à ce qui a été envoyé) ; lève _Stop sur une valeur inexploitable."""
        begin = time.monotonic()
        try:
            value = self.read()
        except Unreadable as error:
            raise _Stop(f"the field value can no longer be read back ({error})") from None
        finally:
            self.read_s = time.monotonic() - begin
        k = self.prefix(value)
        if k is None:
            raise _Stop("the field contains something other than the start of the text (other input?)")
        k = min(k, self.sent)  # un paragraphe vide n'est pas une Entrée tapée
        if self.k >= 0 and k < self.k:
            raise _Stop(f"the field lost text already read back ({k} characters, expected {self.k})")
        if self.before_enter is not None and value != self.before_enter:
            self.before_enter = None  # l'Entrée a fait son effet
        self.k, self.value = max(self.k, k) if k >= 0 else k, value
        return self.k

    def _caught_up(self) -> bool:
        return self.k >= self.sent and self.before_enter is None

    def _wait_in_slice(self, deadline: float, check: Callable[[], None]) -> bool:
        """Pendant une tranche : relit jusqu'à tout relire, ou jusqu'à l'échéance (premier plan à rendre)."""
        while True:
            worker.check_cancelled()  # arrêt d'urgence, appel abandonné
            check()
            if time.monotonic() + self.read_s >= deadline:
                return self._caught_up()
            self._look()
            if self._caught_up():
                return True
            time.sleep(READ_POLL_S)

    def _wait_outside(self) -> None:
        """Premier plan rendu : attend que tout ce qui a été envoyé soit relu, tant que la relecture progresse
        (WAIT_IDLE_S sans progrès, WAIT_MAX_S au plus). Sinon _Stop : rien n'est renvoyé par-dessus."""
        start = progress_at = time.monotonic()
        mark = (self.k, self.before_enter)
        while not self._caught_up():
            worker.check_cancelled()
            self._look()
            now = time.monotonic()
            if (self.k, self.before_enter) != mark:
                mark, progress_at = (self.k, self.before_enter), now
            if self._caught_up():
                return
            if now - progress_at >= WAIT_IDLE_S or now - start >= WAIT_MAX_S:
                if self.k < 0:
                    raise _Stop("Delete did not erase the original content (selection lost?)")
                waited = _seconds(round(now - start, 1))
                raise _Stop(f"what was sent is not read back in the field after {waited}: rest not sent")
            time.sleep(WAIT_POLL_S)

    # --- Tranches ---------------------------------------------------------------------------------------------------
    def run(self) -> Typed:
        starved = 0
        while True:
            try:
                posted = self._slice()
                self._wait_outside()
            except _Stop as stop_cause:
                return self._finish(str(stop_cause))
            except (foreground.BusyUser, MethodError) as error:
                if not self.touched:
                    raise
                return self._finish(f"rest not sent ({error})")
            except foreground.UserInterrupted as error:
                self._sync_out()
                raise foreground.UserInterrupted(f"{str(error).rstrip('.')} ; {self.out.state()}.") from None
            if self.k >= self.out.total:
                return self._finish()
            starved = 0 if posted else starved + 1
            if starved >= STARVED_SLICES:
                limit = _seconds(round(self.slice_s - self.reserve - self.min_post, 3))
                cause = f"the window takes more than {limit} to receive the keyboard: slices too short to send"
                if not self.touched:
                    raise MethodError(f"{cause}; nothing was typed")
                return self._finish(cause)

    def _slice(self) -> bool:
        """Une activation brève : préparation, effacement (remplacement), caractères, relecture jusqu'à l'échéance.
        Renvoie vrai si la tranche a pu envoyer quelque chose."""
        posted = False
        with self.activation() as activation:
            started = time.monotonic()
            self.out.slices += 1
            target = self.prepare(self.out.slices == 1 or self.k < 0)
            activation.check("nothing more was typed" if self.touched else "nothing was typed, the action is cancelled")
            deadline = started + self.slice_s - self.reserve  # préparation comprise ; restitution dans la tranche
            if self.k < 0:  # contenu d'origine sélectionné : effacé et relu vide avant tout caractère
                post_key(target, VK_DELETE)
                self.touched = posted = True
                self._wait_in_slice(deadline, activation.checkpoint)
            elif deadline - time.monotonic() >= self.min_post:
                posted = self._post(target, deadline, activation.checkpoint)
            work_end = time.monotonic()
        ended = time.monotonic()
        # Restitution plus lente que prévu : la réserve des tranches suivantes s'allonge (marge PHI), jusqu'à la moitié.
        self.reserve = min(self.slice_s / 2, max(self.reserve, (ended - min(work_end, deadline)) * PHI))
        self.active += ended - started
        self.out.longest_ms = max(self.out.longest_ms, round((ended - started) * 1000))
        return posted

    def _post(self, target: int, deadline: float, check: Callable[[], None]) -> bool:
        """Un lot de caractères, envoyé seulement si tout ce qui le précède est relu : autant que le débit mesuré
        (envoi, traitement et publication) permet d'en relire avant l'échéance, au moins un. En Chromium, une Entrée
        n'est envoyée qu'une fois relu tout ce qui la précède, et la suite seulement une fois la valeur changée par
        elle : son saut de ligne arrive en fin de file, il serait sinon doublé par les caractères suivants."""
        if not self._caught_up():
            return False
        text, first, begin = self.text, self.sent, time.monotonic()
        end = min(len(text), first + max(1, int((deadline - begin) / PHI / self.cost)))
        while self.sent < end:
            if (self.sent - first) % CHECK_EVERY == 0:
                worker.check_cancelled()
                if time.monotonic() >= deadline:
                    break
            check()  # à chaque caractère : Julien reprend la main → premier plan rendu et arrêt sur-le-champ
            ch = text[self.sent]
            if self.enter_as_key and ch == "\n":
                if not self._wait_in_slice(deadline, check):  # tout ce qui précède l'Entrée, relu d'abord
                    break
                self.before_enter = self.value  # valeur à jour : l'Entrée est vue quand elle change
            _post_char(target, ch, self.enter_as_key)
            self.sent += 1
            self.touched = True
            if self.enter_as_key and ch == "\n" and not self._wait_in_slice(deadline, check):
                break  # Entrée pas encore vue à l'échéance : la suite attend
        if self.sent == first:
            return False
        if self._wait_in_slice(deadline, check):
            measured = (time.monotonic() - begin) / (self.sent - first)
            self.cost = min(max(measured, self.cost / PHI), MAX_COST_S)  # la tranche grandit au plus de PHI à la fois
        else:
            self.cost = min(self.cost * PHI, MAX_COST_S)  # pas relu à temps : tranche suivante plus courte
        return True

    # --- Compte rendu -----------------------------------------------------------------------------------------------
    def _sync_out(self) -> None:
        self.out.confirmed = max(self.k, 0)
        self.out.sent = self.sent
        self.out.erased = self.clear and self.k >= 0
        self.out.active_ms = round(self.active * 1000)

    def _finish(self, cause: str = "") -> Typed:
        if cause and not self.touched:
            raise MethodError(f"{cause}; nothing was typed")
        self._sync_out()
        self.out.stopped = cause
        return self.out


# --- Élément de liste par son rang (homonymes) et liste déroulante ouverte (ComboLBox) ---------------------------
# L'élément d'une liste servi par le pont MSAA n'a pas de fenêtre : UIA Invoke y ferait l'action MSAA par défaut, un
# VRAI double-clic. On agit donc sur le contrôle qui le porte, par son rang, contrôlé par le libellé.
COMBO_LIST_CLASSES = ("combolbox",)  # liste ouverte d'une liste déroulante : fenêtre de premier niveau à part
CB_SHOWDROPDOWN, CB_GETDROPPEDSTATE = 0x14F, 0x157
LB_SETSEL, LB_GETSEL, LB_SETCARETINDEX = 0x185, 0x187, 0x19E


class COMBOBOXINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.w.DWORD), ("rcItem", w.RECT), ("rcButton", w.RECT), ("stateButton", w.w.DWORD),
        ("hwndCombo", w.w.HWND), ("hwndItem", w.w.HWND), ("hwndList", w.w.HWND),
    ]  # fmt: skip


_GetComboBoxInfo = w._fn(w.user32, "GetComboBoxInfo", w.w.BOOL, w.w.HWND, ctypes.POINTER(COMBOBOXINFO))


def combo_list_of(combo: int) -> int:
    """Liste (ComboLBox) d'une liste déroulante Win32/WinForms, d'après GetComboBoxInfo (0 si illisible)."""
    info = COMBOBOXINFO()
    info.cbSize = ctypes.sizeof(COMBOBOXINFO)
    if not _GetComboBoxInfo(combo, ctypes.byref(info)):
        return 0
    return w.handle(info.hwndList)


def combo_owning(drop: int, tops: list[int]) -> int:
    """Liste déroulante propriétaire d'une ComboLBox ouverte, cherchée parmi les contrôles des fenêtres `tops` (la
    fenêtre principale et les dialogues de la cible) : rien d'autre ne relie la ComboLBox à sa liste déroulante."""
    for top in tops:
        for child in w.enum_children(top):
            if _is_class(child, COMBO_CLASSES) and combo_list_of(child) == drop:
                return child
    return 0


def _item_text(control: int, len_msg: int, text_msg: int, index: int) -> str | None:
    length = w.send_message(control, len_msg, index)
    if length is None or length > 0x7FFF:  # LB_ERR / CB_ERR (-1) relu non signé
        return None
    buf = ctypes.create_unicode_buffer(length + 2)
    if w.send_message(control, text_msg, index, ctypes.addressof(buf)) is None:
        return None
    return buf.value


def _checked_rank(control: int, count_msg: int, len_msg: int, text_msg: int, index: int, name: str) -> None:
    """Le rang désigne-t-il bien l'élément vu (même libellé) ? Sinon refus, avant toute modification."""
    count = w.send_message(control, count_msg)
    if count is None or count > 0x7FFF or not 0 <= index < count:
        raise MethodError("Index outside the list: call look again.")
    label = _item_text(control, len_msg, text_msg, index)
    if label is None or " ".join(label.split()) != " ".join(name.split()):
        raise MethodError(f'item no. {index + 1} of the list is not "{name}": call look again.')


def win32_combo_select_index(control: int, index: int, name: str) -> str:
    """Option n° index d'une liste déroulante Win32/WinForms (homonymes distingués par leur rang), contrôlée par son
    libellé avant toute modification, puis choisie comme par l'utilisateur (_combo_commit)."""
    if not _is_class(control, COMBO_CLASSES):
        raise MethodError("Not a Win32 combo box.")
    _checked_rank(control, CB_GETCOUNT, CB_GETLBTEXTLEN, CB_GETLBTEXT, index, name)
    return _combo_commit(control, index, name)


CBS_TYPEMASK, CBS_SIMPLE = 0x3, 0x1


def combo_dropped(control: int) -> bool | None:
    """Liste ouverte ? (CB_GETDROPPEDSTATE ; None si la liste déroulante ne répond pas)."""
    state = w.send_message(control, CB_GETDROPPEDSTATE, timeout_ms=500)
    return None if state is None else bool(state)


def _combo_commit(control: int, index: int, name: str) -> str:
    """Choix d'une option, liste ouverte ou fermée, dans l'ordre qui laisse la valeur à l'application.

    Mesuré (WinForms DropDownList, 2026-10-16) : fermer la liste par CB_SHOWDROPDOWN FALSE est une ANNULATION
    (CBN_SELENDCANCEL) : Windows rétablit la sélection d'avant l'ouverture, et un CB_SETCURSEL posé liste ouverte est
    perdu (l'application lisait une option vide). Donc : liste ouverte refermée d'abord (annulation, rien n'est
    encore choisi), puis CB_SETCURSEL, puis CBN_SELCHANGE et CBN_SELENDOK (choix validé, comme un clic sur l'option)
    ENVOYÉS au parent, puis relecture de l'application : rang (CB_GETCURSEL) et libellé de l'option courante.
    Relecture contraire ou valeur vide : ReadBackFailed (✗), jamais un « ~ »."""
    if combo_dropped(control):
        w.send_message(control, CB_SHOWDROPDOWN, 0, timeout_ms=2000)
    w.send_message(control, CB_SETCURSEL, index)
    if w.send_message(control, CB_GETCURSEL) != index:
        raise MethodError("The combo box did not take the selection.")
    _send_notify(control, CBN_SELCHANGE)
    _send_notify(control, CBN_SELENDOK)
    current = w.send_message(control, CB_GETCURSEL)
    label = _item_text(control, CB_GETLBTEXTLEN, CB_GETLBTEXT, current) if current is not None else None
    if current != index or not label:
        shown = f'"{label}"' if label else "empty"
        raise ReadBackFailed(f'the application kept the value {shown}, not "{name}" (read back)')
    if " ".join(label.split()) != " ".join(name.split()):
        raise ReadBackFailed(f'the application shows "{label}", not "{name}" (read back)')
    if combo_dropped(control):
        raise ReadBackFailed(f'"{name}" chosen but the list stayed open (read back)')
    return "read back ✓"


def win32_combo_show(control: int, open_: bool) -> str:
    """Ouvre ou ferme la liste d'une liste déroulante Win32/WinForms par CB_SHOWDROPDOWN, relu par
    CB_GETDROPPEDSTATE : ni souris, ni clavier, ni premier plan (la liste s'affiche au-dessus, fenêtre inactive).
    Fermer ainsi n'est jamais un choix : c'est l'annulation de Windows (Échap), la sélection d'avant reste."""
    if not _is_class(control, COMBO_CLASSES):
        raise MethodError("Not a Win32 combo box.")
    if w.style(control) & CBS_TYPEMASK == CBS_SIMPLE:
        raise MethodError("list always shown (CBS_SIMPLE): nothing to expand")
    if open_ and not w.IsWindowEnabled(control):
        raise MethodError("combo box disabled")
    w.send_message(control, CB_SHOWDROPDOWN, int(open_), timeout_ms=2000)
    deadline = time.monotonic() + 0.6
    while True:
        state = combo_dropped(control)
        if state is open_:
            return "read back ✓"
        if time.monotonic() >= deadline:
            break
        time.sleep(0.03)
    now = "unreadable" if state is None else "open" if state else "closed"
    raise ReadBackFailed(f"list still {now} (CB_GETDROPPEDSTATE)")


def _is_selected(control: int, index: int) -> bool:
    state = w.send_message(control, LB_GETSEL, index)
    return state is not None and 0 < state <= 0x7FFF


def win32_listbox_select_index(control: int, index: int, name: str) -> str:
    """Élément n° index d'une liste Win32/WinForms (homonymes distingués par leur rang), contrôlé par son libellé,
    comme un clic : LB_SETCURSEL ; liste à sélection multiple : LB_SETSEL (bascule en sélection multiple simple, cet
    élément seul en sélection étendue). Puis LBN_SELCHANGE au parent."""
    if not _is_class(control, LISTBOX_CLASSES):
        raise MethodError("Not a Win32 list box.")
    _checked_rank(control, LB_GETCOUNT, LB_GETTEXTLEN, LB_GETTEXT, index, name)
    style = w.style(control)
    if style & LBS_MULTIPLESEL:
        wanted = not _is_selected(control, index)
        w.send_message(control, LB_SETSEL, int(wanted), index)
        w.send_message(control, LB_SETCARETINDEX, index)
        done = _is_selected(control, index) == wanted
    elif style & LBS_EXTENDEDSEL:
        w.send_message(control, LB_SETSEL, 0, -1)
        w.send_message(control, LB_SETSEL, 1, index)
        w.send_message(control, LB_SETCARETINDEX, index)
        done = _is_selected(control, index)
    else:
        w.send_message(control, LB_SETCURSEL, index)
        if w.send_message(control, LB_GETCURSEL) != index:
            raise MethodError("The list did not take the selection.")
        done = True
    _notify_parent(control, LBN_SELCHANGE)
    return "read back ✓" if done else "selection set, not read back"
