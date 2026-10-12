"""Arrêt d'urgence de Julien : Ctrl+Alt+Maj+Échap fige nexus-windows dans TOUTES les conversations.

Chaque conversation de Claude a son propre serveur ; un seul peut détenir le raccourci (RegisterHotKey) : les autres
réessaient régulièrement et prennent le relais quand le détenteur s'arrête. Le détenteur le signale par un battement
daté (~/.claude/nexus-claude/stop.owner, réécrit toutes les HEARTBEAT_S secondes) : un serveur qui n'a pas pu prendre
le raccourci et ne voit aucun détenteur vivant le dit (journal, en-tête de look et d'act) au lieu de laisser croire
l'arrêt disponible.

L'arrêt est annoncé aux autres serveurs par un fichier daté (~/.claude/nexus-claude/stop, chemin fixe, indépendant de
NEXUS_WINDOWS_HOME), mais il est TENU en mémoire : chaque serveur pose un verrou daté en horloge monotone
(_latched_until) à chaque appui et chaque fois qu'il voit le fichier actif, y compris par un fil de surveillance
(WATCH_S) qui le voit même quand le serveur est inactif. Ni la suppression du fichier (le shell de Claude peut faire
« rm »), ni un saut de l'horloge murale ne lèvent l'arrêt d'un serveur en marche : seul le temps (HOLD_S) le fait.
Toute action en cours s'interrompt au prochain point de contrôle et toute nouvelle action est refusée. Un nouvel appui
le réarme (jamais il ne le lève : un double appui par réflexe ne doit pas relancer l'outil). Aucun chemin de code ne
le lève avant l'échéance. Limite : un serveur démarré APRÈS la suppression du fichier ne voit pas l'arrêt.
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path

from . import __version__, log

HOLD_S = 120.0
STOP_FILE = Path.home() / ".claude" / "nexus-claude" / "stop"
OWNER_FILE = STOP_FILE.with_name("stop.owner")
HEARTBEAT_S = 3.0  # battement du détenteur, et intervalle des nouvelles tentatives des autres serveurs
OWNER_STALE_S = 3 * HEARTBEAT_S  # trois battements manqués : plus de détenteur vivant
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
VK_ESCAPE = 0x1B
WM_HOTKEY, WM_TIMER = 0x0312, 0x0113
HOTKEY_LABEL = "Ctrl+Alt+Maj+Échap"
_HOTKEY_ID = 0x4E58  # « NX »

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_user32.GetMessageW.restype = wintypes.BOOL
_user32.SetTimer.argtypes = (wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p)
_user32.SetTimer.restype = ctypes.c_size_t
WATCH_S = 0.2  # le fil de surveillance relit le fichier d'arrêt à ce rythme, serveur inactif compris
_started = threading.Event()
_tried = threading.Event()  # première tentative de RegisterHotKey faite
_owner = threading.Event()  # ce serveur détient le raccourci
_failure = 0  # GetLastError du premier échec de RegisterHotKey (0 : aucun)
_latched_until = 0.0  # _now() : échéance de l'arrêt vu par ce serveur (appui, ou fichier vu actif) ; ne recule jamais
_seen_mtime: tuple | None = None  # version (date, contenu) du fichier d'arrêt déjà prise en compte
_LATCH = threading.Lock()


def _now() -> float:
    """Horloge monotone du verrou : insensible aux réglages de l'horloge murale."""
    return time.monotonic()


def _latch(seconds: float) -> None:
    """Arrêt tenu au moins `seconds` secondes à partir de maintenant ; le verrou ne recule jamais."""
    global _latched_until
    with _LATCH:
        _latched_until = max(_latched_until, _now() + seconds)


def _read_file() -> None:
    """Chaque NOUVELLE version du fichier (un appui, ici ou dans un autre serveur, le réécrit) pose le verrou une
    seule fois, pour ce qu'il lui reste d'après sa date, borné à [0, HOLD_S]. Ensuite seul le verrou compte : ni
    sa suppression ni un saut de l'horloge murale ne lèvent l'arrêt, et une horloge reculée (fichier « du futur »)
    ne le prolonge pas au-delà d'HOLD_S."""
    global _seen_mtime
    try:
        info = STOP_FILE.stat()
        mtime = info.st_mtime
        version = (
            info.st_mtime_ns,
            STOP_FILE.read_bytes(),
        )  # octets bruts : aucun encodage ne peut le rendre illisible
    except OSError:
        return
    with _LATCH:
        if version == _seen_mtime:
            return
        _seen_mtime = version
    left = min(max(HOLD_S - (time.time() - mtime), 0.0), HOLD_S)
    if left > 0:
        _latch(left)


def remaining_s() -> float:
    """Secondes d'arrêt restantes (0 : pas d'arrêt), d'après le verrou en mémoire (nouveau fichier lu d'abord)."""
    _read_file()
    return max(_latched_until - _now(), 0.0)


def request() -> None:
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Contenu unique à chaque appui : deux appuis dans le même tic d'horloge de fichier (~15 ms) restent deux
    # versions distinctes, chacune reposant le verrou.
    STOP_FILE.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {os.getpid()} {os.urandom(6).hex()}", encoding="utf-8")


def pressed() -> None:
    """Raccourci pressé : arrêt, ou arrêt réarmé pour HOLD_S secondes, verrouillé en mémoire AVANT l'écriture du
    fichier (qui ne sert qu'à prévenir les autres serveurs). Une erreur d'écriture ne tue pas l'écoute : l'arrêt est
    alors tenu par ce serveur seul, et l'échec journalisé."""
    _latch(HOLD_S)
    try:
        request()
    except OSError as error:
        log.event("stop", {"active": True, "hold_s": HOLD_S, "shared": False, "error": str(error)})
        return
    log.event("stop", {"active": True, "hold_s": HOLD_S})


def _watch(until: threading.Event) -> None:
    """Relit le fichier d'arrêt toutes les WATCH_S secondes : l'arrêt demandé par un autre serveur est verrouillé ici
    même si ce serveur ne reçoit aucun appel (sinon un « rm » entre deux appels le lèverait)."""
    while not until.wait(WATCH_S):
        try:
            _read_file()  # un fichier illisible compte comme absent : le verrou déjà posé tient
        except Exception as error:  # noqa: BLE001 - ce fil ne doit jamais mourir : il tient l'arrêt d'urgence
            log.event("stop-watch", {"error": f"{type(error).__name__}: {error}"[:300]})


def message() -> str:
    return (
        f"Arrêt d'urgence demandé par Julien ({HOTKEY_LABEL}) : plus aucune action sur le bureau pendant encore "
        f"{remaining_s():.0f} s. Attendre ses instructions dans la conversation ; ne pas contourner."
    )


def _heartbeat() -> None:
    try:
        OWNER_FILE.parent.mkdir(parents=True, exist_ok=True)
        OWNER_FILE.write_text(
            json.dumps({"pid": os.getpid(), "version": __version__, "ts": time.time()}), encoding="utf-8"
        )
    except OSError:
        pass  # battement manqué : les autres serveurs signaleront l'arrêt indisponible, ce qui reste honnête


def owner() -> dict | None:
    """Détenteur vivant du raccourci (battement de moins de OWNER_STALE_S secondes), sinon None."""
    try:
        data = json.loads(OWNER_FILE.read_text(encoding="utf-8"))
        fresh = time.time() - float(data["ts"]) <= OWNER_STALE_S
    except OSError, ValueError, KeyError, TypeError:
        return None
    return data if fresh else None


def _loop() -> None:
    global _failure
    while not _user32.RegisterHotKey(None, _HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, VK_ESCAPE):
        if not _tried.is_set():
            _failure = ctypes.get_last_error()
            log.event("stop", {"hotkey": False, "error": _failure, "retry_s": HEARTBEAT_S, "owner": owner()})
            _tried.set()
        time.sleep(HEARTBEAT_S)  # détenu par un autre serveur nexus-windows (autre conversation) : relais à son arrêt
    _owner.set()
    _tried.set()
    log.event("stop", {"hotkey": True, "after_error": _failure})
    _heartbeat()
    _user32.SetTimer(None, 0, int(HEARTBEAT_S * 1000), None)  # WM_TIMER dans la file de ce fil
    msg = wintypes.MSG()
    while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY and msg.wParam == _HOTKEY_ID:
            pressed()
        elif msg.message == WM_TIMER:
            _heartbeat()


def start(timeout_s: float = 1.0) -> bool:
    """Lance l'écoute du raccourci et la surveillance du fichier d'arrêt (une fois par processus). Vrai si ce serveur
    détient le raccourci ; un échec est journalisé (avec GetLastError) et annoncé par hotkey_note()."""
    if not _started.is_set():
        _started.set()
        threading.Thread(target=_watch, args=(threading.Event(),), name="nexus-stop-watch", daemon=True).start()
        threading.Thread(target=_loop, name="nexus-stop-hotkey", daemon=True).start()
    _tried.wait(timeout_s)
    return owns_hotkey()


def owns_hotkey() -> bool:
    return _owner.is_set()


def hotkey_note() -> str:
    """« » si l'arrêt d'urgence est disponible (ce serveur ou un autre, vivant, détient le raccourci), sinon la note
    à montrer (en-tête de look, windows et act)."""
    if owns_hotkey() or not _tried.is_set() or owner() is not None:
        return ""
    return (
        f"Arrêt d'urgence {HOTKEY_LABEL} NON GARANTI : ce serveur n'a pas obtenu le raccourci (erreur {_failure}) et "
        "aucun serveur nexus-windows à jour ne le détient (un autre programme ou un serveur ancien le tient). "
        "Prévenir Julien."
    )


def status() -> str:
    """État du raccourci pour l'outil logs."""
    if owns_hotkey():
        return "détenu par ce serveur"
    holder = owner()
    if holder is not None:
        return f"détenu par le serveur pid {holder.get('pid')} ({holder.get('version')})"
    if not _tried.is_set():
        return "pas encore essayé"
    return f"NON GARANTI (raccourci non obtenu, erreur {_failure}, aucun détenteur à jour)"
