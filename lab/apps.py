"""Lancement des applications du labo (une par famille d'interface) et lecture de leur statut.

Chaque application affiche un contrôle « Statut » mis à jour à chaque événement (clic, coche, saisie…) et l'écrit
sur stdout en JSON. Toutes s'affichent sans activation et sont posées au bas de l'empilement (send_to_bottom), sous les fenêtres
de Julien.

Banc intrusif malgré tout (mesuré : Edge et Tk prennent le premier plan ≈1,3 s au lancement, SW_SHOWNOACTIVATE
ignoré) : le labo ne se lance que si Julien n'a touché ni clavier ni souris depuis IDLE_REQUIRED_S secondes, et le
premier plan lui est rendu dès qu'un processus du labo le prend, jusqu'à GUARD_TAIL_S secondes après l'apparition
de la fenêtre (comme launch.py).
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

LAB = Path(__file__).resolve().parent
STATE = Path.home() / ".claude" / "nexus-claude" / "lab"
PYTHON = sys.executable


def _system_python() -> str:
    """Python système (Tcl/Tk complet) : le premier python.exe du PATH hors de l'environnement actif. Lancé par
    `uv run`, le PATH commence par l'environnement de test, dont le Python géré par uv n'a pas Tk (banc du 23/10)."""
    venv = str(Path(sys.prefix).resolve()).lower()
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        exe = Path(folder) / "python.exe"
        if folder and exe.is_file() and not str(exe.resolve()).lower().startswith(venv):
            return str(exe)
    return shutil.which("python.exe") or sys.executable


TK_PYTHON = _system_python()
POWERSHELL = shutil.which("powershell.exe") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
ELECTRON = STATE / "electron" / "node_modules" / "electron" / "dist" / "electron.exe"
EDGE_CANDIDATES = [
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
]
CREATE_NO_WINDOW = 0x08000000
IDLE_REQUIRED_S = 120.0  # banc intrusif : seulement si Julien est absent (aucune entrée depuis 2 min)
GUARD_TAIL_S = 2.0  # surveillance du premier plan après l'apparition de la fenêtre du labo

KINDS = ("win32", "winforms", "wpf", "tk", "electron", "edge")


class JulienActive(RuntimeError):
    """Julien a utilisé clavier ou souris récemment : le labo, qui peut prendre le premier plan, n'est pas lancé."""


def julien_idle_s() -> float:
    """Secondes depuis la dernière entrée de Julien. Entrée physique vue par Raw Input pendant son écoute : elle fait
    foi (les entrées injectées par d'autres outils ne comptent pas). Sinon, Julien est inactif depuis au moins la durée
    d'écoute, et au-delà seul GetLastInputInfo le dit (système, entrées injectées comprises : prudent)."""
    from nexus_windows import physical
    from nexus_windows import win32 as w

    physical.start()
    system_s = ((w.GetTickCount() - w.last_input_tick()) & 0xFFFFFFFF) / 1000
    if not physical.available():
        return system_s
    observed, idle = physical.listening_s(), physical.idle_s()
    return idle if idle < observed else max(observed, system_s)


def require_julien_away(idle_required_s: float = IDLE_REQUIRED_S) -> None:
    """Refuse le banc vivant si Julien est actif : ses frappes pourraient partir dans une fenêtre du labo. Raw Input
    sans assez d'historique : on écoute jusqu'à idle_required_s plutôt que de se fier à GetLastInputInfo, que les
    entrées injectées par d'autres programmes remettent à zéro (refus à tort mesuré : « 69 s » sans aucune entrée
    physique). Une entrée physique pendant l'écoute refuse aussitôt."""
    from nexus_windows import physical

    physical.start()
    while physical.available() and physical.listening_s() < idle_required_s:
        if physical.idle_s() < physical.listening_s():
            break  # entrée physique vue pendant l'écoute : décision ci-dessous
        time.sleep(1.0)
    idle = julien_idle_s()
    if idle < idle_required_s:
        raise JulienActive(
            f"Julien a utilisé clavier ou souris il y a {idle:.0f} s (< {idle_required_s:.0f} s) : banc vivant refusé, "
            "il pourrait prendre son premier plan. Relancer quand il est absent."
        )


# --- Présence de Julien pendant tout le banc (incident du 25/10 : une fenêtre du labo au premier plan pendant qu'il
# travaillait ; l'absence n'était vérifiée qu'au lancement de chaque famille) -----------------------------------------
_LIVE: set[LabApp] = set()  # applications du labo ouvertes
_ON_RETURN: list[Callable[[], None]] = []  # fermetures à faire dès que Julien revient (couverture du banc…)
_RETURNED = threading.Event()
_ARMED_AT = [0.0]


class _Presence:
    """Surveille les entrées PHYSIQUES de Julien (Raw Input) tant que le labo est ouvert : à la première, toutes les
    applications du labo et leurs couvertures sont fermées sur-le-champ et le banc s'arrête proprement
    (check_presence lève JulienActive) ; il ne reprend qu'après une nouvelle absence de IDLE_REQUIRED_S."""

    def __init__(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lab-presence", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        from nexus_windows import physical

        while True:
            time.sleep(0.1)
            armed = _ARMED_AT[0]
            if not armed or _RETURNED.is_set() or not physical.available():
                continue
            if physical.idle_s() < time.monotonic() - armed:  # entrée physique depuis l'armement
                _RETURNED.set()
                close_everything()


_PRESENCE: list[_Presence] = []


def arm_presence() -> None:
    """Julien vient d'être vu absent : surveillance armée à partir de maintenant."""
    from nexus_windows import physical

    physical.start()
    if not _PRESENCE:
        _PRESENCE.append(_Presence())
    _RETURNED.clear()
    _ARMED_AT[0] = time.monotonic()


def check_presence() -> None:
    """À appeler avant chaque tâche du banc : Julien revenu → JulienActive (le labo est déjà fermé)."""
    if _RETURNED.is_set():
        raise JulienActive("Julien est revenu : labo fermé aussitôt, banc interrompu jusqu'à sa prochaine absence.")


def on_return(close: Callable[[], None]) -> Callable[[], None]:
    """Fermeture à faire dès que Julien revient ; renvoie de quoi la retirer."""
    _ON_RETURN.append(close)
    return lambda: _ON_RETURN.remove(close) if close in _ON_RETURN else None


def close_everything() -> None:
    for close in list(_ON_RETURN):
        try:
            close()
        except Exception:  # noqa: BLE001 - chaque fermeture est tentée
            pass
    for app in list(_LIVE):
        try:
            app.close()
        except Exception:  # noqa: BLE001
            pass


def send_to_bottom(hwnd: int) -> None:
    """Fenêtre du labo au bas de l'empilement, sans activation : jamais par-dessus une fenêtre de Julien (une fenêtre
    affichée sans activation se pose quand même en HAUT de l'empilement : WinForms, WPF, Electron)."""
    import ctypes

    ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10)


class _ForegroundGuard:
    """Rend le premier plan à Julien dès qu'un processus du labo (lancé ou descendant) le prend, du lancement jusqu'à
    GUARD_TAIL_S après l'apparition de la fenêtre."""

    def __init__(self, pid: int, user_fg: int, pids: Callable[[], set[int]] | None = None) -> None:
        self.user_fg = user_fg
        self.pid = pid
        self.pids = pids  # processus du labo relus en cours de route (navigateur relancé) ; défaut : pid et descendants
        self.until: float | None = None
        self.taken = 0
        self._thread = threading.Thread(target=self._run, name="lab-foreground-guard", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        from nexus_windows import foreground
        from nexus_windows import win32 as w
        from nexus_windows.launch import descendants

        pids, refreshed = {self.pid}, 0.0
        while self.until is None or time.monotonic() < self.until:
            if time.monotonic() - refreshed > 0.3:
                pids = self.pids() if self.pids is not None else descendants(self.pid)
                refreshed = time.monotonic()
            fg = w.foreground()
            if fg and fg != self.user_fg and w.window_pid(w.root(fg)) in pids:
                self.taken += 1
                foreground.give_back(self.user_fg)
            time.sleep(0.01)

    def finish(self, tail_s: float) -> None:
        """Encore tail_s secondes de surveillance (l'application peut s'activer après sa fenêtre), puis fin."""
        self.until = time.monotonic() + tail_s
        self._thread.join(tail_s + 1.0)


@dataclass(eq=False)  # par identité : rangée dans _LIVE
class LabApp:
    kind: str
    proc: subprocess.Popen
    hwnd: int = 0
    events: queue.Queue = field(default_factory=queue.Queue)
    extra: dict = field(default_factory=dict)
    profile: Path | None = None  # navigateur du labo : son dossier de profil, qui le désigne même relancé

    def pids(self) -> set[int]:
        """Processus de l'application : le lancé et ses descendants, plus, pour un navigateur, le processus qui tient
        son profil (Edge peut se relancer : le processus lancé meurt et la fenêtre appartient à sa copie)."""
        from nexus_windows.launch import descendants

        out = descendants(self.proc.pid)
        holder = profile_browser_pid(self.profile) if self.profile else 0
        if holder:
            out |= descendants(holder)
        return out

    def last_events(self, wait_s: float = 0.0) -> list[str]:
        out: list[str] = []
        deadline = time.monotonic() + wait_s
        while True:
            try:
                out.append(self.events.get(timeout=max(0.0, deadline - time.monotonic()) if not out else 0.05))
            except queue.Empty:
                if out or time.monotonic() >= deadline:
                    return out

    def drain(self) -> None:
        while not self.events.empty():
            self.events.get_nowait()

    def close(self) -> None:
        _LIVE.discard(self)
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.profile:
            close_profile_browser(self.profile)  # copie relancée par le navigateur lui-même : jamais laissée ouverte


def profile_browser_pid(profile: Path) -> int:
    """Processus Chromium (Edge) qui tient le profil `profile` : celui de sa fenêtre de messages
    « Chrome_MessageWindow », dont le titre est le dossier du profil (ProcessSingleton de Chromium) ; 0 sans."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowExW.argtypes = (wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowExW.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    hwnd = user32.FindWindowExW(wintypes.HWND(-3), None, "Chrome_MessageWindow", str(profile))  # HWND_MESSAGE
    if not hwnd:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def close_profile_browser(profile: Path, timeout_s: float = 5.0) -> None:
    """Ferme le navigateur du labo qui tient `profile` (profil propre au labo : jamais un navigateur de Julien)."""
    deadline = time.monotonic() + timeout_s
    while (pid := profile_browser_pid(profile)) and time.monotonic() < deadline:
        try:
            os.kill(pid, 15)  # TerminateProcess sous Windows ; ses processus enfants se ferment avec lui
        except OSError:
            pass
        time.sleep(0.2)


def _startupinfo() -> subprocess.STARTUPINFO:
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 4  # SW_SHOWNOACTIVATE
    return si


def _reader(app: LabApp, ready: queue.Queue) -> None:
    assert app.proc.stdout is not None
    for line in app.proc.stdout:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("ready"):
            ready.put(data)
        elif "event" in data:
            app.events.put(data["event"])


def _spawn(kind: str, cmd: list[str]) -> tuple[LabApp, queue.Queue]:
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        startupinfo=_startupinfo(),
        creationflags=CREATE_NO_WINDOW,
    )
    app = LabApp(kind, proc)
    ready: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(app, ready), daemon=True).start()
    return app, ready


def _find_window(pids: Callable[[], set[int]] | None, title_prefix: str, timeout_s: float) -> int:
    """Fenêtre visible de premier niveau dont le titre commence par title_prefix, d'un des processus pids() (relus à
    chaque tour : un navigateur peut se relancer sous un autre processus)."""
    from nexus_windows import win32 as w

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        current = pids() if pids is not None else None
        for hwnd in w.enum_windows():
            if not w.IsWindowVisible(hwnd) or w.owner(hwnd):
                continue
            if w.window_text(hwnd).startswith(title_prefix) and (current is None or w.window_pid(hwnd) in current):
                return hwnd
        time.sleep(0.1)
    return 0


def edge_path() -> Path | None:
    return next((p for p in EDGE_CANDIDATES if p.exists()), None)


def launch(kind: str, x: int = 120, y: int = 120, cdp_port: int = 0, timeout_s: float = 30.0) -> LabApp:
    """Lance une application du labo, seulement si Julien est absent ; premier plan rendu s'il est pris."""
    from nexus_windows import win32 as w

    require_julien_away()
    arm_presence()
    user_fg = w.foreground()  # relevé AVANT le lancement : une fenêtre du labo peut le prendre dès sa création
    app, ready = _start(kind, x, y, cdp_port)
    guard = _ForegroundGuard(app.proc.pid, user_fg, app.pids)
    try:
        _wait_window(app, ready, timeout_s)
    finally:
        guard.finish(GUARD_TAIL_S if app.hwnd else 0.0)
    _LIVE.add(app)
    if app.hwnd:
        send_to_bottom(app.hwnd)
    app.extra["cdp_port"] = cdp_port
    app.extra["foreground_taken"] = guard.taken
    check_presence()  # Julien revenu pendant le lancement : déjà refermé, rien n'est mesuré
    return app


def _start(kind: str, x: int, y: int, cdp_port: int) -> tuple[LabApp, queue.Queue]:
    if kind == "win32":
        app, ready = _spawn(kind, [PYTHON, str(LAB / "win32_app.py"), "--x", str(x), "--y", str(y)])
    elif kind == "tk":
        app, ready = _spawn(kind, [TK_PYTHON, str(LAB / "tk_app.py"), "--x", str(x), "--y", str(y)])
    elif kind in ("winforms", "wpf"):
        script = LAB / f"{kind}_app.ps1"
        cmd = [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-STA",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-X",
            str(x),
            "-Y",
            str(y),
        ]
        app, ready = _spawn(kind, cmd)
    elif kind == "electron":
        cmd = [str(ELECTRON), str(LAB / "electron_main.js"), f"--lab-x={x}", f"--lab-y={y}"]
        if cdp_port:
            cmd.append(f"--remote-debugging-port={cdp_port}")
        app, ready = _spawn(kind, cmd)
    elif kind == "edge":
        exe = edge_path()
        if exe is None:
            raise FileNotFoundError("Microsoft Edge introuvable.")
        profile = STATE / "edge-profile"
        profile.mkdir(parents=True, exist_ok=True)
        # Copie d'un lancement précédent encore ouverte sur ce profil : la nouvelle commande lui serait confiée (le
        # processus lancé quitterait aussitôt) et sa fenêtre resterait sur le bureau de Julien.
        close_profile_browser(profile)
        cmd = [
            str(exe),
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-features=msEdgeSidebarV2,EdgeCollections,CalculateNativeWinOcclusion,msImplicitSignin",
            "--force-renderer-accessibility",
            f"--window-position={x},{y}",
            "--window-size=600,640",
            f"--app={(LAB / 'web_app.html').as_uri()}",
        ]
        if cdp_port:
            cmd.append(f"--remote-debugging-port={cdp_port}")
        app, ready = _spawn(kind, cmd)
        app.profile = profile
    else:
        raise ValueError(f"Type d'application inconnu : {kind}")
    return app, ready


def _wait_window(app: LabApp, ready: queue.Queue, timeout_s: float) -> None:
    if app.kind in ("win32", "tk", "winforms", "wpf"):
        try:
            data = ready.get(timeout=timeout_s)
        except queue.Empty:
            app.close()
            raise TimeoutError(f"{app.kind} : pas de signal « ready » en {timeout_s} s.") from None
        app.hwnd = int(data["top"])
        if "zone_hwnd" in data:  # Tk : aucun élément UI Automation, fenêtre de la zone donnée par l'application
            app.extra["zone_hwnd"] = int(data["zone_hwnd"])
        if "barre_hwnd" in data:  # Tk : barre de défilement native de la liste
            app.extra["barre_hwnd"] = int(data["barre_hwnd"])
        if "wheel_lines" in data:  # WPF : lignes par cran de molette (banc commun : déplacement d'un cran)
            app.extra["wheel_lines"] = int(data["wheel_lines"])
    else:
        title = "Labo Web"
        app.hwnd = _find_window(app.pids, title, timeout_s)  # fenêtre du navigateur, même relancé
        if not app.hwnd:
            app.close()
            raise TimeoutError(f"{app.kind} : fenêtre « {title} » introuvable en {timeout_s} s.")
