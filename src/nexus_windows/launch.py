"""Lancement d'applications en arrière-plan : exécutable, commande, ou nom du menu Démarrer (applis Store comprises).

La nouvelle fenêtre ne doit pas prendre la main : démarrage sans activation quand Windows le permet, puis premier
plan rendu à Julien et fenêtre glissée juste sous la sienne si l'application s'est activée d'elle-même.
Identité : la fenêtre lancée est celle d'un processus de l'arbre lancé ; titre et nom d'exécutable ne servent qu'aux
lancements sans processus connu (raccourci du menu Démarrer) ou au lanceur qui se termine (calc → CalculatorApp),
jamais à un navigateur.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import comtypes
import comtypes.client

from . import foreground, log, policy, snapshot, uia
from . import win32 as w

_START_APPS: list[dict] | None = None
_START_APPS_AT = 0.0
_START_APPS_ERROR = ""
_START_APPS_LOCK = threading.Lock()  # le préchargement du démarrage et un launch ne lancent qu'un PowerShell
START_APPS_TTL_S = 600.0
START_APPS_TIMEOUT_S = 20.0


class LaunchError(RuntimeError):
    pass


class AccountError(LaunchError):
    """Un compte (celui de Julien ?) est apparu dans un navigateur DevTools : il a été fermé."""


@dataclass
class Launched:
    hwnd: int
    pid: int
    port: int
    via: str
    notes: list[str]
    profile: Path | None = None  # profil jetable d'un navigateur DevTools
    user_front: int = 0  # fenêtre de Julien au premier plan AVANT le lancement : référence de la garde du serveur
    returned: bool = True  # faux : l'application tient encore le premier plan pris à Julien (Windows a refusé)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_apps(refresh: bool = False) -> list[dict]:
    """Applications du menu Démarrer (nom, AppID), mises en cache 10 minutes. Jamais d'exception (la liste est aussi
    préchargée au démarrage du serveur) : en cas d'échec, la liste précédente ou vide, et la cause gardée pour
    find_start_app."""
    global _START_APPS, _START_APPS_AT, _START_APPS_ERROR
    with _START_APPS_LOCK:
        if _START_APPS is not None and not refresh and time.monotonic() - _START_APPS_AT <= START_APPS_TTL_S:
            return _START_APPS
        ps = shutil.which("powershell.exe") or "powershell.exe"
        try:
            out = subprocess.run(
                [
                    ps,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=START_APPS_TIMEOUT_S,
                creationflags=0x08000000,
            )
            data = json.loads(out.stdout or "[]")
        except subprocess.TimeoutExpired:
            _START_APPS_ERROR = f"Get-StartApps n'a pas répondu en {START_APPS_TIMEOUT_S:.0f} s"
        except (OSError, ValueError) as error:  # PowerShell introuvable, sortie illisible
            _START_APPS_ERROR = f"Get-StartApps a échoué : {error}"
        else:
            rows = data if isinstance(data, list) else [data]
            _START_APPS = [
                r
                for r in rows
                if isinstance(r, dict) and isinstance(r.get("Name"), str) and isinstance(r.get("AppID"), str)
            ]
            _START_APPS_AT = time.monotonic()
            _START_APPS_ERROR = ""
            return _START_APPS
        log.event("menu-demarrer", {"erreur": _START_APPS_ERROR})
        return _START_APPS or []


def find_start_app(name: str) -> dict | None:
    low = name.strip().lower()
    apps = start_apps()
    if not apps and _START_APPS_ERROR:
        raise LaunchError(f"Menu Démarrer illisible ({_START_APPS_ERROR}) : passer le chemin de l'exécutable.")
    for app in apps:
        if app["Name"].lower() == low:
            return app
    matches = [a for a in apps if low in a["Name"].lower()]
    return min(matches, key=lambda a: len(a["Name"])) if matches else None


def _is_start_app(name: str) -> bool:
    """« chemin arguments » ou nom du menu Démarrer ? Menu illisible : on tente la ligne de commande."""
    try:
        return find_start_app(name) is not None
    except LaunchError:
        return False


class IApplicationActivationManager(comtypes.IUnknown):
    _iid_ = comtypes.GUID("{2e941141-7f97-4756-ba1d-9decde894a3d}")
    _methods_ = [
        comtypes.COMMETHOD(
            [],
            comtypes.HRESULT,
            "ActivateApplication",
            (["in"], ctypes.c_wchar_p, "appUserModelId"),
            (["in"], ctypes.c_wchar_p, "arguments"),
            (["in"], ctypes.c_ulong, "options"),
            (["out"], ctypes.POINTER(ctypes.c_ulong), "processId"),
        ),
    ]


CLSID_APPLICATION_ACTIVATION_MANAGER = comtypes.GUID("{45BA127D-10A8-46EA-8AB7-56EA9078943C}")
AO_NOERRORUI = 0x2


def _activate_packaged(app_id: str, args: str) -> int:
    """IApplicationActivationManager::ActivateApplication → pid de l'application Store."""
    manager = comtypes.client.CreateObject(
        CLSID_APPLICATION_ACTIVATION_MANAGER, interface=IApplicationActivationManager
    )
    return int(manager.ActivateApplication(app_id, args or None, AO_NOERRORUI))


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_CreateToolhelp32Snapshot = w._fn(w.kernel32, "CreateToolhelp32Snapshot", w.w.HANDLE, w.w.DWORD, w.w.DWORD)
_Process32FirstW = w._fn(w.kernel32, "Process32FirstW", w.w.BOOL, w.w.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_Process32NextW = w._fn(w.kernel32, "Process32NextW", w.w.BOOL, w.w.HANDLE, ctypes.POINTER(PROCESSENTRY32W))


def descendants(pid: int) -> set[int]:
    """pid et tous ses descendants (instantané Toolhelp : quelques millisecondes)."""
    snap = _CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    children: dict[int, list[int]] = {}
    if snap and snap != ctypes.c_void_p(-1).value:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            ok = _Process32NextW(snap, ctypes.byref(entry))
        w.CloseHandle(snap)
    out, todo = {pid}, [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in out:
                out.add(child)
                todo.append(child)
    return out


UWP_CORE_CLASS = "Windows.UI.Core.CoreWindow"


def _uwp_frame_of(hwnd: int, pids: set[int]) -> bool:
    """Cadre UWP (processus ApplicationFrameHost) d'une application de l'arbre lancé : une CoreWindow de l'arbre porte
    le même titre. Elle n'est pas toujours enfant du cadre : relevé sur la Calculatrice, CoreWindow de premier niveau
    masquée (cloaked) et cadre sans enfant de l'application."""
    if not pids or w.process_name(w.window_pid(hwnd)).lower() not in foreground.HOST_PROCESSES:
        return False
    title = w.window_text(hwnd)
    return bool(title) and any(
        w.window_pid(other) in pids and w.class_name(other) == UWP_CORE_CLASS and w.window_text(other) == title
        for other in w.enum_windows()
    )


def _related(hwnd: int, pids: set[int], hint: str, stem: str, strict: bool) -> bool:
    """La fenêtre vient-elle de l'application lancée ? Par processus : la fenêtre ou ses enfants (CoreWindow d'un
    cadre UWP) appartiennent à l'arbre lancé, ou c'est le cadre UWP d'une CoreWindow de l'arbre. En mode strict, rien
    d'autre ; sinon aussi par titre ou par nom d'exécutable (calc → CalculatorApp, notepad → Notepad)."""
    owners = {w.window_pid(hwnd)} | {w.window_pid(c) for c in w.enum_children(hwnd)}
    if owners & pids or _uwp_frame_of(hwnd, pids):
        return True
    if strict:
        return False
    if hint and hint in w.window_text(hwnd).lower():
        return True
    names = {w.process_name(p).lower() for p in owners}
    return bool(stem) and any(name.startswith(stem[:4]) for name in names)


def _new_window(
    before: set[int],
    pid: int,
    title_hint: str,
    stem: str,
    deadline: float,
    user_front: int,
    fallback: bool = True,
    running: Callable[[], bool] | None = None,
) -> tuple[int, bool]:
    """Attend la fenêtre de l'application lancée — jamais une fenêtre sans lien — et rend le premier plan à Julien dès
    qu'une nouvelle fenêtre de l'application le prend pendant l'attente. Renvoie (hwnd ou 0, premier plan repris).

    pid connu : seules les fenêtres de l'arbre lancé comptent (mode strict), aussi pour rendre le premier plan. Le
    repli titre/nom (`fallback`) n'est permis que sans pid, ou quand le lanceur s'est terminé sans laisser de
    descendant (`running` faux) : l'application vit alors hors de l'arbre (activation COM d'une appli Store)."""
    hint = title_hint.lower()
    pids: set[int] = {pid} if pid else set()
    refreshed = 0.0
    took_back = False
    while True:
        now = time.monotonic()
        if pid and now - refreshed > 0.3:
            pids = descendants(pid)
            refreshed = now
        gone = bool(pid) and running is not None and not running() and pids == {pid}
        strict = bool(pid) and not (fallback and gone)
        fg = w.foreground()
        if (
            user_front
            and fg
            and fg != user_front
            and fg not in before
            and w.root(fg) not in before
            and _related(w.root(fg), pids, hint, stem, strict)  # jamais une nouvelle fenêtre ouverte par Julien
            and not foreground.user_is_switching()
        ):
            took_back = foreground.give_back(user_front, exclude={fg}) or took_back
        for hwnd in w.enum_windows():
            if hwnd in before or not snapshot.is_app_window(hwnd):
                continue
            if _related(hwnd, pids, hint, stem, strict):
                return hwnd, took_back
        if now >= deadline or (gone and not fallback):
            return 0, took_back  # lanceur parti sans fenêtre ni descendant, et repli interdit : rien ne viendra
        time.sleep(0.04)


# Fenêtre recouverte : sans ces options, Chromium cesse de dessiner et de publier son arbre d'accessibilité.
OCCLUSION_FEATURES = ("CalculateNativeWinOcclusion",)
# Edge : tout profil neuf se connecte implicitement au compte Windows de Julien et synchronise ses identifiants
# (mesuré : 985 identifiants copiés dans un profil « jetable ») ; ni --guest ni --inprivate ne l'empêchent.
NO_ACCOUNT_FEATURES = ("msImplicitSignin",)
PROFILES = log.ROOT / "devtools-profiles"
PROFILE_MARKER = ".nexus-profile"  # écrit dès la création : seuls ces dossiers sont supprimés
PROFILE_NAME = re.compile(r"\d{8}-\d{6}-.+")
# Âge minimal avant suppression : un autre serveur peut être en train de démarrer son navigateur sur ce profil.
PROFILE_MIN_AGE_S = 600.0


def merge_disable_features(args: str, features: tuple[str, ...]) -> tuple[str, str]:
    """Chromium ne retient que le DERNIER disable-features (quel que soit son préfixe) : les listes sont fusionnées en
    un seul commutateur. Arguments lus comme Chromium les lit. Renvoie (arguments sans disable-features, commutateur
    fusionné)."""
    found: list[str] = []
    rest: list[str] = []
    for token in policy.split_args(args):
        if policy.switch_key(token) == "disable-features":
            found += [name.strip() for name in token.partition("=")[2].split(",") if name.strip()]
        else:
            rest.append(token)
    merged = list(dict.fromkeys(found + list(features)))
    return subprocess.list2cmdline(rest), f"--disable-features={','.join(merged)}"


def _profile_in_use(profile: Path) -> bool:
    """Chromium garde `lockfile` ouvert en exclusif tant que le navigateur tourne sur ce profil."""
    lock = profile / "lockfile"
    if not lock.exists():
        return False
    try:
        lock.unlink()
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def _prunable(profile: Path, now: float) -> bool:
    """Profil jetable de l'outil (nom daté et marqueur), ancien de plus de PROFILE_MIN_AGE_S, navigateur fermé.
    Jamais une jonction ni un lien : la suppression sortirait du dossier des profils."""
    if (
        not PROFILE_NAME.fullmatch(profile.name)
        or profile.is_symlink()
        or profile.is_junction()
        or not profile.is_dir()
    ):
        return False
    try:
        age = now - (profile / PROFILE_MARKER).stat().st_mtime
    except OSError:
        return False  # sans marqueur : pas un profil créé par l'outil
    return age > PROFILE_MIN_AGE_S and not _profile_in_use(profile)


def prune_profiles() -> int:
    """Supprime les profils DevTools jetables dont le navigateur est fermé (créés par l'outil, jamais réutilisés)."""
    try:
        entries = list(PROFILES.iterdir()) if PROFILES.is_dir() else []
    except OSError:
        return 0
    removed = 0
    now = time.time()
    for profile in entries:
        if _prunable(profile, now):
            shutil.rmtree(profile, ignore_errors=True)
            removed += not profile.exists()
    return removed


def fresh_profile() -> Path:
    PROFILES.mkdir(parents=True, exist_ok=True)
    prune_profiles()
    profile = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=PROFILES))
    marker = {"pid": os.getpid(), "created": time.time()}
    (profile / PROFILE_MARKER).write_text(json.dumps(marker), encoding="utf-8")
    return profile


def kill_tree(pid: int) -> None:
    """Ferme un processus lancé par l'outil et ses descendants (navigateur DevTools à profil neuf)."""
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)


# --- Compte dans un navigateur DevTools ----------------------------------------------------------------------
SIGNIN_URLS = ("edge://sync", "edge://signin", "chrome://sync", "chrome://signin")
# Local State, profile.info_cache.<profil> : compte Google (user_name, gaia_id) ou Microsoft (edge_account_*).
ACCOUNT_KEYS = ("user_name", "gaia_id")
ACCOUNT_KEY_PREFIXES = ("edge_account_",)
# Preferences de chaque profil : google.services.* garde le dernier compte connecté.
SERVICE_ACCOUNT_KEYS = ("last_gaia_id", "last_username", "last_signed_in_username", "last_account_id")
# Contrôle répété après le lancement : la connexion implicite d'Edge survient au démarrage du profil.
ACCOUNT_WATCH_S = 15.0
ACCOUNT_POLL_S = 0.5


def _json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def profile_account(profile: Path) -> str | None:
    """Compte inscrit dans un profil Chromium : Local State (info_cache) puis Preferences de chaque profil
    (account_info, google.services). Renvoie OÙ il a été vu (jamais sa valeur), ou None."""
    cache = _dict(_dict(_json_file(profile / "Local State").get("profile")).get("info_cache"))
    names = ["Default"]
    for name, entry in cache.items():
        keys = sorted(
            k for k, v in _dict(entry).items() if v and (k in ACCOUNT_KEYS or k.startswith(ACCOUNT_KEY_PREFIXES))
        )
        if keys:
            return f"Local State, profil « {name} » : {', '.join(keys)}"
        if isinstance(name, str) and name not in names and not set(name) & set("/\\:") and name not in (".", ".."):
            names.append(name)
    for name in names:
        prefs = _json_file(profile / name / "Preferences")
        if prefs.get("account_info"):
            return f"Preferences du profil « {name} » : account_info"
        services = _dict(_dict(prefs.get("google")).get("services"))
        keys = sorted(k for k in SERVICE_ACCOUNT_KEYS if services.get(k))
        if keys:
            return f"Preferences du profil « {name} » : google.services.{', '.join(keys)}"
    return None


def account_found(port: int, profile: Path | None) -> str | None:
    """Raison de fermer un navigateur DevTools : page de connexion ou de synchronisation ouverte, ou compte inscrit
    dans son profil jetable. None si aucun compte."""
    from . import cdp

    if port:
        try:
            urls = [str(t.get("url", "")) for t in cdp.targets(port)]
        except cdp.CDPError:
            urls = []
        page = next((u for u in urls if u.startswith(SIGNIN_URLS)), None)
        if page:
            return f"page {page.split('?')[0]} ouverte"
    return profile_account(profile) if profile is not None else None


ACCOUNT_EXIT_WAIT_S = 5.0  # sortie du navigateur fermé pour cause de compte, avant de supprimer son profil


def remove_profile(profile: Path, timeout_s: float = ACCOUNT_EXIT_WAIT_S) -> bool:
    """Supprime sur-le-champ un profil jetable de l'outil (il porte un compte : jamais laissé jusqu'à la purge).
    Seulement un dossier daté et marqué, directement sous PROFILES, jamais une jonction ni un lien. Réessaie le temps
    que les derniers processus du navigateur lâchent leurs fichiers. Vrai si le dossier n'existe plus."""
    try:
        inside = profile.parent.resolve() == PROFILES.resolve()
    except OSError:
        return False
    if (
        not inside
        or not PROFILE_NAME.fullmatch(profile.name)
        or profile.is_symlink()
        or profile.is_junction()
        or not (profile / PROFILE_MARKER).is_file()
    ):
        return False
    deadline = time.monotonic() + timeout_s
    while True:
        shutil.rmtree(profile, ignore_errors=True)
        if not profile.exists():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def close_account_browser(proc: subprocess.Popen | None, pid: int, profile: Path | None) -> bool:
    """Navigateur DevTools où un compte est apparu : arbre fermé, sortie attendue, puis son profil jetable (qui porte
    le compte) supprimé sur-le-champ. Le processus lancé est visé par son Popen, dont le handle empêche Windows de
    réattribuer son pid tant qu'il vit. Vrai si le profil a été supprimé."""
    if proc is not None:
        if proc.poll() is None:
            kill_tree(proc.pid)
        try:
            proc.wait(ACCOUNT_EXIT_WAIT_S)
        except subprocess.TimeoutExpired:
            pass
    else:
        kill_tree(pid)
    return remove_profile(profile) if profile is not None else False


def _profile_fate(profile: Path | None, removed: bool) -> str:
    if profile is None:
        return ""
    return " ; profil jetable supprimé" if removed else " ; profil jetable NON supprimé (fichiers verrouillés : purge)"


def guard_account(
    proc: subprocess.Popen, port: int, profile: Path, on_kill: Callable[[Path], None] | None = None
) -> threading.Thread:
    """Contrôle de compte répété pendant ACCOUNT_WATCH_S après le lancement (connexion implicite tardive). Un compte
    apparu ferme aussitôt le navigateur, supprime son profil et prévient le serveur (on_kill(profil) : session
    nettoyée). Le processus est suivi par son handle (Popen) : un pid réattribué à un autre programme n'est jamais
    visé."""

    def run() -> None:
        deadline = time.monotonic() + ACCOUNT_WATCH_S
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(ACCOUNT_POLL_S)
            reason = account_found(port, profile)
            if reason and proc.poll() is None:
                removed = close_account_browser(proc, proc.pid, profile)
                if on_kill is not None:
                    on_kill(profile)
                log.event(
                    "devtools-compte", {"pid": proc.pid, "port": port, "raison": reason, "profil_supprime": removed}
                )
                return

    thread = threading.Thread(target=run, name="nexus-account-guard", daemon=True)
    thread.start()
    return thread


_iphlpapi = ctypes.WinDLL("iphlpapi")
_GetExtendedTcpTable = w._fn(
    _iphlpapi, "GetExtendedTcpTable", ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int,
    ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong,
)  # fmt: skip
TCP_TABLE_OWNER_PID_LISTENER, AF_INET = 3, 2


def port_owner(port: int) -> tuple[int, str] | None:
    """Processus qui écoute sur le port TCP local, et l'adresse d'écoute (pour vérifier 127.0.0.1)."""
    size = ctypes.c_ulong(0)
    _GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0)
    buffer = ctypes.create_string_buffer(size.value + 4096)
    size = ctypes.c_ulong(len(buffer))
    if _GetExtendedTcpTable(buffer, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0) != 0:
        return None
    count = ctypes.c_ulong.from_buffer(buffer).value
    rows = (ctypes.c_ulong * (count * 6)).from_buffer(buffer, 4)
    for i in range(count):
        _state, local_addr, local_port, _ra, _rp, pid = rows[i * 6 : i * 6 + 6]
        if socket.ntohs(local_port & 0xFFFF) == port:
            return pid, socket.inet_ntoa(local_addr.to_bytes(4, "little"))
    return None


# --- Contenu prêt (applications Store, UWP, WinUI) ------------------------------------------------------------
CONTENT_LIMIT_S = 3.0  # au-delà, on rend la main avec une note plutôt que de bloquer le lancement
CONTENT_GAP_S = 0.15  # deux instantanés espacés d'au moins ce délai, au même nombre d'éléments : interface stable


def _content_count(view: snapshot.View) -> int:
    """Éléments du contenu de l'application. Cadre UWP (ApplicationFrameHost) : ses propres éléments (titre, boutons
    de fenêtre) ne comptent pas, le contenu vient du processus de l'application."""
    host = w.window_pid(view.hwnd)
    if w.process_name(host).lower() not in foreground.HOST_PROCESSES:
        return len(view.elements)
    return sum(1 for e in view.elements if int(e.props.get("pid") or host) != host)


def uwp_unshown(hwnd: int) -> bool:
    """Cadre UWP resté vide : la CoreWindow de même titre est restée au premier niveau, masquée, hors du cadre.
    Mesuré sur la Calculatrice lancée en arrière-plan : sans activation, Windows ne l'affiche jamais et la suspend
    (son arbre UIA passe de 46 nœuds à 1 en 2 s) ; attendre n'y change rien."""
    host = w.window_pid(hwnd)
    if w.process_name(host).lower() not in foreground.HOST_PROCESSES:
        return False
    if any(w.window_pid(child) != host for child in w.enum_children(hwnd)):
        return False  # contenu de l'application rattaché au cadre
    title = w.window_text(hwnd)
    return bool(title) and any(
        w.class_name(other) == UWP_CORE_CLASS and w.window_text(other) == title and w.is_cloaked(other)
        for other in w.enum_windows()
    )


def _holds_front(hwnd: int) -> bool:
    """La fenêtre lancée (ou une de ses fenêtres) tient-elle le premier plan ? Par fenêtre seulement."""
    fg = w.foreground()
    return bool(hwnd and fg) and foreground.belongs_to(fg, foreground.target_windows(hwnd), set())


def wait_content(auto: uia.Automation, hwnd: int, limit_s: float = CONTENT_LIMIT_S) -> bool:
    """Attend que l'arbre UI Automation de la fenêtre lancée ait un contenu stable (une appli Store publie d'abord
    son cadre seul). False si rien de stable dans le délai."""
    deadline = time.monotonic() + limit_s
    previous = -1
    while True:
        try:
            count = _content_count(snapshot.build_view(auto, hwnd))
        except uia.UIAError, comtypes.COMError, OSError:
            count = -1  # fenêtre en cours de construction (ou fermée) : pas encore lisible
        if count > 0 and count == previous:
            return True
        previous = count
        if time.monotonic() + CONTENT_GAP_S >= deadline:
            return False
        time.sleep(CONTENT_GAP_S)


# --- Lancement -----------------------------------------------------------------------------------------------
_KNOWN_FOLDER_ID = re.compile(r"(\{[0-9A-Fa-f-]{36}\})\\(.+)")


def _app_id_path(app_id: str) -> str | None:
    """Fichier désigné par un AppID du menu Démarrer : chemin complet, ou « {dossier connu}\\chemin » (Git GUI,
    Windows PowerShell…). None pour un AppID d'application Store ou de raccourci."""
    if Path(app_id).is_absolute():
        return app_id if Path(app_id).exists() else None
    match = _KNOWN_FOLDER_ID.fullmatch(app_id)
    if match:
        base = w.known_folder(match.group(1))
        if base and (Path(base) / match.group(2)).exists():
            return str(Path(base) / match.group(2))
    return None


def launch(
    app: str,
    args: str = "",
    devtools: bool = False,
    wait_s: float = 15.0,
    auto: uia.Automation | None = None,
    on_account_kill: Callable[[Path], None] | None = None,
) -> Launched:
    """on_account_kill(profil) : appelé quand la garde de compte ferme le navigateur DevTools lancé (le serveur y
    oublie ce processus)."""
    notes: list[str] = []
    user_front = w.foreground()
    before = set(w.enum_windows())
    port = free_port() if devtools else 0
    extra = f" --remote-debugging-port={port} --remote-allow-origins=http://127.0.0.1:{port}" if devtools else ""
    pid = 0
    via = ""
    title_hint = ""
    proc: subprocess.Popen | None = None
    profile: Path | None = None
    browser = False
    candidate = app.strip().strip('"')
    exe = None
    if Path(os.path.expandvars(candidate)).exists():
        exe = os.path.expandvars(candidate)
    elif shutil.which(candidate):
        exe = shutil.which(candidate)
    if exe is None and " " in app and not _is_start_app(app):
        parts = shlex.split(app, posix=False)
        head = parts[0].strip('"')
        if Path(head).exists() or shutil.which(head):
            exe = shutil.which(head) or head
            args = " ".join(parts[1:]) + (" " + args if args else "")
    if exe is not None:
        policy.check_launch_exe(exe, args, devtools=devtools)
        browser = policy.is_browser_exe(exe)
        if devtools and browser:
            # Jamais le profil réel de Julien derrière un port DevTools : profil neuf, sans compte ni synchronisation.
            profile = fresh_profile()
            extra += f' --user-data-dir="{profile}" --no-first-run --no-default-browser-check --disable-sync'
            notes.append("navigateur DevTools : profil neuf, sans compte ni synchronisation (supprimé ensuite)")
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = w.SW_SHOWNOACTIVATE
        if policy.is_chromium_exe(exe):
            features = OCCLUSION_FEATURES + (NO_ACCOUNT_FEATURES if devtools and browser else ())
            args, disable = merge_disable_features(args, features)
            extra += f" {disable} --force-renderer-accessibility"
            notes.append("Chromium/Electron : rendu et accessibilité maintenus même fenêtre recouverte")
        command = subprocess.list2cmdline([exe]) + (" " + args if args else "") + extra
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=startupinfo,
                shell=False,
            )
        except OSError as error:
            raise LaunchError(f"Lancement impossible de « {exe} » : {error}") from error
        pid, via = proc.pid, "exécutable"
    else:
        entry = find_start_app(app)
        if entry is None:
            raise LaunchError(f"« {app} » : ni exécutable, ni application du menu Démarrer.")
        app_id = entry["AppID"]
        title_hint = entry["Name"]
        policy.check_launch_app_id(app_id, title_hint)
        target = _app_id_path(app_id)
        if target and target.lower().endswith(".exe"):
            # Exécutable : politique et identité par processus.
            return launch(target, args, devtools, wait_s, auto, on_account_kill)
        if target:
            policy.check_launch_exe(target, "")  # document ouvert par son association : jamais un script
        policy.check_launch_app_id(app_id, title_hint, args, devtools)
        notes.append(f"menu Démarrer : « {title_hint} »")
        if "!" in app_id:
            try:
                pid = _activate_packaged(app_id, args.strip())
                via = "application Store"
            except (comtypes.COMError, OSError) as error:
                raise LaunchError(f"Activation de « {title_hint} » refusée : {error}") from error
        else:
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{app_id}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            via = "raccourci du menu Démarrer"
            if args.strip():
                notes.append("arguments ignorés : un raccourci du menu Démarrer se lance tel quel")
    stem = Path(exe).stem.lower() if exe else ""
    running = (lambda: proc.poll() is None) if proc is not None else None
    hwnd, took_back = _new_window(
        before, pid, title_hint, stem, time.monotonic() + max(1.0, wait_s), user_front,
        fallback=not (devtools or browser), running=running,
    )  # fmt: skip
    pids = descendants(pid) if pid else set()
    if devtools and (not hwnd or w.window_pid(hwnd) not in pids):
        kill_tree(pid)
        raise LaunchError(
            f"DevTools : aucune fenêtre du processus lancé en {max(1.0, wait_s):.0f} s (application déjà ouverte à "
            "instance unique ? la fermer puis relancer avec devtools=true) : processus lancé fermé, aucune autre "
            "fenêtre prise à sa place."
        )
    fg = w.foreground()
    if hwnd and fg and fg != user_front:
        windows = foreground.target_windows(hwnd)
        if foreground.belongs_to(fg, windows, foreground.target_pids(hwnd) | pids, before):
            if user_front and w.IsWindow(user_front):
                took_back = foreground.give_back(user_front, exclude=windows) or took_back
            elif not user_front:
                # Aucune fenêtre de Julien à qui rendre le premier plan : la nouvelle reste active, mais au fond.
                w.SetWindowPos(hwnd, w.HWND_BOTTOM, 0, 0, 0, 0, w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE)
                notes.append(
                    "aucune fenêtre n'était au premier plan : l'application s'est activée, placée au fond de "
                    "l'empilement"
                )
    if hwnd and w.foreground() != hwnd and user_front and w.IsWindow(user_front):
        w.SetWindowPos(hwnd, user_front, 0, 0, 0, 0, w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE)
    if not hwnd:
        notes.append(
            "aucune fenêtre de cette application détectée (déjà ouverte à instance unique ? windows pour la trouver)"
        )
    if devtools:
        reason = account_found(port, profile)
        if reason:
            removed = close_account_browser(proc, pid, profile)
            raise AccountError(
                f"Compte détecté dans le navigateur DevTools ({reason}) : fermé aussitôt (jamais le compte de Julien "
                f"derrière un port DevTools){_profile_fate(profile, removed)}."
            )
        if profile is not None and proc is not None:
            guard_account(proc, port, profile, on_account_kill)
    if auto is not None and hwnd and (exe is None or snapshot.framework_of(hwnd) in ("UWP", "WinUI")):
        # Jamais d'activation pour « montrer » une application Store : mesuré, elle ne l'affichait dans aucun des 17
        # lancements et coûtait environ 1 s de premier plan à Julien. Si Windows la laisse vide, la note le dit.
        # Pendant l'attente (jusqu'à 3 s), la garde rend aussitôt le premier plan si l'application le prend.
        with foreground.Watch(hwnd, front=user_front, tail_s=0) as watch:
            ready = wait_content(auto, hwnd)
        if watch.stolen:
            took_back = took_back or not watch.failed
        if not ready:
            if not uwp_unshown(hwnd):
                notes.append("contenu pas encore prêt (interface encore vide) : look avant d'agir")
            elif _holds_front(hwnd):
                notes.append("application Store jamais affichée par Windows, bien qu'elle tienne le premier plan")
            else:
                notes.append(
                    "application Store jamais affichée par Windows (lancée sans activation, contenu suspendu : "
                    "attendre n'y change rien) : l'afficher exige le premier plan, window(action=\"front\", "
                    "foreground=true) avec l'accord de Julien"
                )
    # Premier plan relu en fin de lancement, jamais supposé : la note ne dit « rendu » que s'il l'est vraiment.
    returned = not (user_front and w.IsWindow(user_front) and _holds_front(hwnd))
    if not returned:
        notes.append(
            "l'application a pris le premier plan et Windows a refusé de le rendre : premier plan PAS rendu à Julien "
            "(la garde du serveur réessaie)"
        )
    elif took_back:
        notes.append("l'application s'est mise devant : premier plan rendu aussitôt à Julien")
    app_pid = foreground.app_pid(hwnd) if hwnd else pid
    if pid and app_pid != pid and w.process_name(app_pid).lower() in foreground.HOST_PROCESSES:
        app_pid = pid  # cadre UWP sans CoreWindow enfant : l'application est le processus lancé, pas l'hôte
    return Launched(hwnd, app_pid, port, via, notes, profile, user_front, returned)
