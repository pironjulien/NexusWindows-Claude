"""Steam (client Valve) : interface CEF/Chromium dans des fenêtres SDL de steamwebhelper.exe.

Mesuré le 2026-10-14 : sans commutateur, une fenêtre Steam recouverte n'est ni dessinée ni publiée (« 0 éléments sur
5 nœuds UIA », captures noires). Steam documente ses propres commutateurs (aide de steamclient64.dll) :
- `-cef-disable-occlusion` « disables Chromium window occlusion testing » : la fenêtre recouverte reste dessinée ;
- `-cef-force-accessibility` : arbre d'accessibilité publié d'emblée (--force-renderer-accessibility) ;
- `-cef-enable-debugging` « enables Chromium remote debugging, defaulting to localhost:8080 » : port DevTools sur
  127.0.0.1 (Steam ajoute --remote-allow-origins=*), qui ouvre la session Steam de Julien à tout programme local tant
  que Steam reste ouvert ainsi. Seulement par launch(app="Steam", devtools=true), avec l'accord de Julien ; jamais
  fourni par le modèle dans args (policy.CHROMIUM_DENY_SWITCHES).
Les commutateurs ne valent qu'au démarrage : Steam déjà ouvert doit être relancé (restart=true, accord de Julien :
Steam se ferme, téléchargements interrompus ; refusé si un jeu tourne).
"""

from __future__ import annotations

import os
import subprocess
import time
import winreg
from pathlib import Path

from . import log
from . import win32 as w

STEAM_EXE = "steam.exe"
WEBHELPER = "steamwebhelper.exe"
VIEW_SWITCHES = ("-cef-disable-occlusion", "-cef-force-accessibility")
DEVTOOLS_SWITCH = "-cef-enable-debugging"
DEVTOOLS_PORT = 8080  # fixé par Steam (« defaulting to localhost:8080 »)
# Processus de Steam lui-même : tout autre descendant de steam.exe est un jeu (ou son lanceur) qu'une relance tuerait.
STEAM_PROCESSES = {
    "steam.exe", "steamwebhelper.exe", "steamservice.exe", "steamerrorreporter.exe", "steamerrorreporter64.exe",
    "gldriverquery.exe", "gldriverquery64.exe", "vulkandriverquery.exe", "vulkandriverquery64.exe", "conhost.exe",
}  # fmt: skip
SHUTDOWN_WAIT_S = 60.0
START_WAIT_S = 90.0  # premier démarrage : mise à jour du client possible (mesuré : ~60 s)


class SteamError(RuntimeError):
    pass


def steam_exe() -> str | None:
    """Chemin de steam.exe (registre de Valve, à défaut l'emplacement par défaut)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _ = winreg.QueryValueEx(key, "SteamExe")
        path = os.path.normpath(str(value))
        if os.path.isfile(path):
            return path
    except OSError:
        pass
    default = os.path.expandvars(r"%ProgramFiles(x86)%\Steam\steam.exe")
    return default if os.path.isfile(default) else None


def is_steam_exe(path: str) -> bool:
    """steam.exe du client Valve (à côté de steamclient64.dll ou steamui.dll), quel que soit le chemin donné."""
    target = Path(os.path.expandvars(path))
    if target.name.lower() != STEAM_EXE:
        return False
    if not target.is_absolute():
        return True
    return any((target.parent / name).is_file() for name in ("steamclient64.dll", "steamui.dll"))


def is_steam_window(hwnd: int) -> bool:
    return w.process_name(w.window_pid(hwnd)).lower() == WEBHELPER


def _table() -> list[tuple[int, int, str]]:
    return w.process_table()


def steam_pids(table: list[tuple[int, int, str]] | None = None) -> list[int]:
    return [pid for pid, _parent, name in (table if table is not None else _table()) if name.lower() == STEAM_EXE]


def _descendants(roots: list[int], table: list[tuple[int, int, str]]) -> list[tuple[int, str]]:
    children: dict[int, list[tuple[int, str]]] = {}
    for pid, parent, name in table:
        children.setdefault(parent, []).append((pid, name))
    out: list[tuple[int, str]] = []
    todo = list(roots)
    seen = set(roots)
    while todo:
        for pid, name in children.get(todo.pop(), []):
            if pid not in seen:
                seen.add(pid)
                out.append((pid, name))
                todo.append(pid)
    return out


def games_running(table: list[tuple[int, int, str]] | None = None) -> list[str]:
    """Programmes lancés par Steam qui ne sont pas Steam lui-même (jeux, lanceurs) : une relance les fermerait."""
    table = table if table is not None else _table()
    names = {name for _pid, name in _descendants(steam_pids(table), table) if name.lower() not in STEAM_PROCESSES}
    return sorted(names)


def webhelper_pids(table: list[tuple[int, int, str]] | None = None) -> set[int]:
    table = table if table is not None else _table()
    return {pid for pid, name in _descendants(steam_pids(table), table) if name.lower() == WEBHELPER}


def debugging_port() -> int | None:
    """Port DevTools d'un Steam déjà ouvert avec -cef-enable-debugging : écouté par son steamwebhelper, sur
    127.0.0.1 seulement, et qui répond vraiment en DevTools. None sinon."""
    from . import cdp
    from .launch import listeners

    helpers = webhelper_pids()
    for port, address, pid in listeners():
        if pid in helpers and address == "127.0.0.1" and cdp.alive(port):
            return port
    return None


def main_window(table: list[tuple[int, int, str]] | None = None) -> int:
    """Fenêtre principale visible de Steam (steamwebhelper descendant de steam.exe), la plus en avant ; 0 sinon."""
    from .snapshot import is_app_window

    helpers = webhelper_pids(table)
    windows = [h for h in w.enum_windows() if w.window_pid(h) in helpers and is_app_window(h)]
    titled = [h for h in windows if w.window_text(h).strip().lower() == "steam"]
    return (titled or windows or [0])[0]


def shutdown(exe: str, wait_s: float = SHUTDOWN_WAIT_S) -> None:
    """Fermeture propre (steam.exe -shutdown), attendue jusqu'à ce que steam.exe et steamwebhelper aient disparu.
    Refus si un jeu lancé par Steam tourne (il serait fermé)."""
    table = _table()
    games = games_running(table)
    if games:
        raise SteamError(
            f"Relance de Steam refusée : {', '.join(games)} tourne sous Steam (jeu ou lanceur, il serait fermé)."
        )
    if not steam_pids(table):
        return
    log.event("steam", {"action": "fermeture", "raison": "relance avec commutateurs"})
    subprocess.Popen([exe, "-shutdown"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        table = _table()
        if not any(name.lower() in (STEAM_EXE, WEBHELPER) for _pid, _parent, name in table):
            time.sleep(1.0)  # verrous de Steam (fichiers, port) relâchés
            return
        time.sleep(0.5)
    raise SteamError(f"Steam ne s'est pas fermé en {wait_s:.0f} s (fenêtre de confirmation ?) : rien n'a été relancé.")


def command(exe: str, args: str, devtools: bool) -> str:
    """Ligne de commande : arguments du modèle (déjà contrôlés par la politique), puis ceux de l'outil."""
    policy_args = list(VIEW_SWITCHES) + ([DEVTOOLS_SWITCH] if devtools else [])
    return subprocess.list2cmdline([exe]) + (" " + args if args.strip() else "") + " " + " ".join(policy_args)


def port_taken_by_other() -> str | None:
    """Le port 8080 est-il déjà pris par un autre programme ? (Steam ne pourrait pas l'ouvrir ; un autre programme
    serait pris pour le DevTools de Steam.) Nom du programme, ou None."""
    from .launch import port_owner

    owner = port_owner(DEVTOOLS_PORT)
    if owner is None:
        return None
    return w.process_name(owner[0])
