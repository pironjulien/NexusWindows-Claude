"""Lancement d'applications en arrière-plan : exécutable, commande, ou nom du menu Démarrer (applis Store comprises).

La nouvelle fenêtre ne doit pas prendre la main : démarrage sans activation quand Windows le permet, puis premier
plan rendu à l'utilisateur et fenêtre glissée juste sous la sienne si l'application s'est activée d'elle-même.
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
from typing import TYPE_CHECKING

import comtypes
import comtypes.client

from . import foreground, log, packages, policy, snapshot, steam, uia
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
    """Un compte (celui de l'utilisateur ?) est apparu dans un navigateur DevTools : il a été fermé."""


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
    own_session: bool = False  # port DevTools sur la session réelle de Julien (Steam) : jamais fermé par l'outil


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
            _START_APPS_ERROR = f"Get-StartApps did not answer within {START_APPS_TIMEOUT_S:.0f} s"
        except (OSError, ValueError) as error:  # PowerShell introuvable, sortie illisible
            _START_APPS_ERROR = f"Get-StartApps failed: {error}"
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
        log.event("start-menu", {"error": _START_APPS_ERROR})
        return _START_APPS or []


def find_start_app(name: str) -> dict | None:
    low = name.strip().lower()
    apps = start_apps()
    if not apps and _START_APPS_ERROR:
        raise LaunchError(f"Start menu unreadable ({_START_APPS_ERROR}): pass the executable's path.")
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
    if TYPE_CHECKING:  # méthode créée par comtypes depuis _methods_ (paramètre « out » rendu comme résultat)

        def ActivateApplication(self, appUserModelId: str, arguments: str | None, options: int) -> int: ...


CLSID_APPLICATION_ACTIVATION_MANAGER = comtypes.GUID("{45BA127D-10A8-46EA-8AB7-56EA9078943C}")
AO_NOERRORUI = 0x2


def _activate_packaged(app_id: str, args: str) -> int:
    """IApplicationActivationManager::ActivateApplication → pid de l'application Store."""
    manager = comtypes.client.CreateObject(
        CLSID_APPLICATION_ACTIVATION_MANAGER, interface=IApplicationActivationManager
    )
    return int(manager.ActivateApplication(app_id, args or None, AO_NOERRORUI))


def descendants(pid: int) -> set[int]:
    """pid et tous ses descendants (instantané Toolhelp : quelques millisecondes)."""
    children: dict[int, list[int]] = {}
    for child, parent, _name in w.process_table():
        children.setdefault(parent, []).append(child)
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
    """Attend la fenêtre de l'application lancée — jamais une fenêtre sans lien — et rend le premier plan à l'utilisateur dès
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
        exited = bool(pid) and running is not None and not running()
        # Lanceur parti : arbre relu sur-le-champ avant de conclure. Mesuré (Edge 155, profil neuf) : le lanceur
        # relance le navigateur puis se termine en moins de 0,3 s ; relu à la cadence de 0,3 s seulement, l'arbre
        # datait d'avant cet enfant et le lancement échouait en 0,5 s, navigateur orphelin laissé ouvert.
        if pid and (now - refreshed > 0.3 or exited):
            pids = descendants(pid)
            refreshed = now
        gone = exited and pids == {pid}
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
    """Ferme un processus lancé par l'outil et ses descendants (navigateur DevTools à profil neuf). Le lanceur peut
    être déjà parti (Edge se relance) : taskkill /T sur lui seul ne fermerait pas ses enfants orphelins, chacun est
    donc nommé."""
    if pid:
        command = ["taskkill", "/T", "/F"]
        for member in sorted(descendants(pid)):
            command += ["/PID", str(member)]
        subprocess.run(command, capture_output=True, check=False)


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
            return f'Local State, profile "{name}": {", ".join(keys)}'
        if isinstance(name, str) and name not in names and not set(name) & set("/\\:") and name not in (".", ".."):
            names.append(name)
    for name in names:
        prefs = _json_file(profile / name / "Preferences")
        if prefs.get("account_info"):
            return f'Preferences of profile "{name}": account_info'
        services = _dict(_dict(prefs.get("google")).get("services"))
        keys = sorted(k for k in SERVICE_ACCOUNT_KEYS if services.get(k))
        if keys:
            return f'Preferences of profile "{name}": google.services.{", ".join(keys)}'
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
            return f"page {page.split('?')[0]} open"
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
    return "; disposable profile removed" if removed else "; disposable profile NOT removed (locked files: purge)"


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
                    "devtools-account", {"pid": proc.pid, "port": port, "reason": reason, "profile_removed": removed}
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


def listeners() -> list[tuple[int, str, int]]:
    """Ports TCP (IPv4) en écoute : (port, adresse d'écoute, pid propriétaire)."""
    size = ctypes.c_ulong(0)
    _GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0)
    buffer = ctypes.create_string_buffer(size.value + 4096)
    size = ctypes.c_ulong(len(buffer))
    if _GetExtendedTcpTable(buffer, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0) != 0:
        return []
    count = ctypes.c_ulong.from_buffer(buffer).value
    rows = (ctypes.c_ulong * (count * 6)).from_buffer(buffer, 4)
    out = []
    for i in range(count):
        _state, local_addr, local_port, _ra, _rp, pid = rows[i * 6 : i * 6 + 6]
        out.append((socket.ntohs(local_port & 0xFFFF), socket.inet_ntoa(local_addr.to_bytes(4, "little")), pid))
    return out


def port_owner(port: int) -> tuple[int, str] | None:
    """Processus qui écoute sur le port TCP local, et l'adresse d'écoute (pour vérifier 127.0.0.1)."""
    return next(((pid, address) for number, address, pid in listeners() if number == port), None)


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


# --- Dossier ou document ouvert par son association -------------------------------------------------------------
class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIconOrMonitor", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


SEE_MASK_NOCLOSEPROCESS, SEE_MASK_NOASYNC, SEE_MASK_FLAG_NO_UI = 0x40, 0x100, 0x400
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
_shell32.ShellExecuteExW.restype = ctypes.c_bool
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetProcessId.argtypes = [ctypes.c_void_p]
_kernel32.GetProcessId.restype = ctypes.c_ulong
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def openable(candidate: str) -> Path | None:
    """Chemin existant d'un dossier ou d'un document (pas d'un programme) : ouvert par son association, comme un
    double-clic de l'utilisateur. Un programme, quel que soit son nom, reste un lancement d'exécutable (et sa politique)."""
    if not candidate:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(candidate)))
    try:
        if path.is_dir():
            return path.resolve()
        if not path.is_file() or path.suffix.lower() == ".exe" or policy.pe_subsystem(str(path)) is not None:
            return None
    except OSError:
        return None
    return path.resolve()


def shell_open(path: Path) -> int:
    """ShellExecuteEx « open » sans activation demandée ; pid du processus qui l'ouvre (0 : application déjà ouverte
    qui l'a pris en charge, ou Explorateur)."""
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI
    info.lpVerb = "open"
    info.lpFile = str(path)
    info.nShow = w.SW_SHOWNOACTIVATE
    if not _shell32.ShellExecuteExW(ctypes.byref(info)):
        raise LaunchError(f'Opening "{path}" refused by Windows (error {ctypes.get_last_error()}).')
    if not info.hProcess:
        return 0
    try:
        return int(_kernel32.GetProcessId(info.hProcess))
    finally:
        _kernel32.CloseHandle(info.hProcess)


def explorer_tabs(hwnd: int) -> list[str]:
    """Dossiers affichés par les onglets d'une fenêtre de l'Explorateur (Windows 11 : un onglet par élément de
    Shell.Application, même HWND). Vide si la fenêtre n'est pas une fenêtre de l'Explorateur."""
    tabs: list[str] = []
    try:
        shell = comtypes.client.CreateObject("Shell.Application", dynamic=True)
        windows = shell.Windows()
        for index in range(int(windows.Count)):
            try:
                item = windows.Item(index)
                if item is not None and int(item.HWND) == hwnd:
                    tabs.append(str(item.Document.Folder.Self.Path))
            except comtypes.COMError, AttributeError, OSError, ValueError:
                continue
    except comtypes.COMError, OSError:
        return []
    return tabs


def explorer_windows(path: Path) -> list[int]:
    """Fenêtres de l'Explorateur dont le dossier affiché est exactement `path` (Shell.Application)."""
    wanted = os.path.normcase(str(path))
    found: list[int] = []
    try:
        shell = comtypes.client.CreateObject("Shell.Application", dynamic=True)
        windows = shell.Windows()
        for index in range(int(windows.Count)):
            try:
                item = windows.Item(index)
                if item is not None and os.path.normcase(str(item.Document.Folder.Self.Path)) == wanted:
                    found.append(int(item.HWND))
            except comtypes.COMError, AttributeError, OSError, ValueError:
                continue
    except comtypes.COMError, OSError:
        return []
    return found


def titled_windows(text: str) -> list[int]:
    """Fenêtres d'application dont le titre contient `text` (sans casse)."""
    low = text.lower()
    return [h for h in w.enum_windows() if snapshot.is_app_window(h) and low in w.window_text(h).lower()]


REUSED_AFTER_S = (
    1.5  # une fenêtre déjà ouverte n'est retenue qu'après ce délai sans nouvelle fenêtre (onglet, instance)
)


def _opened_window(path: Path, folder: bool, before: set[int], waited_s: float) -> int:
    """Nouvelle fenêtre qui montre le dossier ou le document ; une fenêtre déjà ouverte seulement après
    REUSED_AFTER_S (l'application a pu l'ouvrir dans un onglet ou une instance existante), et pour un document
    seulement si son titre porte le nom COMPLET du fichier (extension comprise) : jamais une fenêtre sans lien dont le
    titre contient le même mot."""
    if folder:
        candidates = explorer_windows(path)
        fresh = [h for h in candidates if h not in before]
        old = candidates
    else:
        fresh = [h for h in titled_windows(path.stem) if h not in before] if len(path.stem) >= 3 else []
        fresh = fresh or [h for h in titled_windows(path.name) if h not in before]
        old = titled_windows(path.name)
    if fresh:
        return fresh[0]
    return old[0] if old and waited_s >= REUSED_AFTER_S else 0


def open_path(path: Path, wait_s: float, user_front: int, before: set[int]) -> Launched:
    """Ouvre un dossier (Explorateur) ou un document (application associée) sans lui donner la main, et VÉRIFIE ce
    qui s'est ouvert : dossier affiché par l'Explorateur relu (Shell.Application), ou fenêtre dont le titre porte le
    nom du document. Faute de preuve, la note le dit (jamais « ouvert » supposé)."""
    folder = path.is_dir()
    via = "folder" if folder else "document"
    notes: list[str] = []
    pid = shell_open(path)
    start = time.monotonic()
    deadline = start + max(1.0, wait_s)
    took_back = False
    hwnd = 0
    while True:
        fg = w.foreground()
        if (
            user_front
            and fg
            and fg != user_front
            and w.root(fg) not in before
            and not foreground.user_is_switching()
            and (
                w.class_name(w.root(fg)) in policy.EXPLORER_CLASSES
                if folder
                else path.stem.lower() in w.window_text(w.root(fg)).lower()
            )  # jamais une fenêtre que Julien vient d'ouvrir lui-même : seulement celle du dossier ou du document
        ):
            took_back = foreground.give_back(user_front, exclude={fg}) or took_back  # la nouvelle fenêtre s'est activée
        hwnd = _opened_window(path, folder, before, time.monotonic() - start)
        if hwnd or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    verified = bool(hwnd)
    if hwnd:
        # Explorateur : Windows 11 réutilise une fenêtre cachée créée d'avance (mesuré le 15/10 : « déjà là » avec 0
        # onglet visible) ; seuls ses onglets disent si la fenêtre est partagée avec Julien.
        tabs = len(explorer_tabs(hwnd)) if folder else 0
        shared = tabs > 1 if folder else hwnd in before
        where = (
            (f"tab of a window with {tabs} tabs" if shared else "window of its own")
            if folder
            else ("window already open" if shared else "new window")
        )
        shown = f'folder shown "{path}"' if folder else f'title "{snapshot.clip(w.window_text(hwnd), 80)}"'
        notes.append(f"opening verified ({where}, {shown})")
        if shared:
            notes.append(
                "opened in a window the user already had (new tab or document of the same instance): do not close "
                "this window, it also holds the user's own tabs or documents (close only the opened tab, ctrl+w)"
            )
        fg = w.foreground()
        if fg and fg != user_front and user_front and w.IsWindow(user_front):
            windows = foreground.target_windows(hwnd)
            if foreground.belongs_to(fg, windows, foreground.target_pids(hwnd), before):
                took_back = foreground.give_back(user_front, exclude=windows) or took_back
        if w.foreground() != hwnd and user_front and w.IsWindow(user_front) and hwnd not in before:
            w.SetWindowPos(hwnd, user_front, 0, 0, 0, 0, w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE)
    else:
        notes.append(
            f"opening NOT verified: no {'Explorer window on this folder' if folder else 'window bearing the document name'} "
            f"within {max(1.0, wait_s):.0f} s (windows to look for it)"
        )
    returned = not (user_front and w.IsWindow(user_front) and hwnd and _holds_front(hwnd))
    if not returned:
        notes.append("the window took the foreground and Windows refused to give it back (the guard retries)")
    elif took_back:
        notes.append("the window came to the front: foreground given back to the user at once")
    log.event("open", {"via": via, "verified": verified, "pid": pid})
    app_pid = foreground.app_pid(hwnd) if hwnd else pid
    return Launched(hwnd, app_pid, 0, via, notes, None, user_front, returned)


def launch(
    app: str,
    args: str = "",
    devtools: bool = False,
    wait_s: float = 15.0,
    auto: uia.Automation | None = None,
    on_account_kill: Callable[[Path], None] | None = None,
    restart: bool = False,
) -> Launched:
    """on_account_kill(profil) : appelé quand la garde de compte ferme le navigateur DevTools lancé (le serveur y
    oublie ce processus). restart : relancer l'application déjà ouverte (Steam seulement, accord de l'utilisateur)."""
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
    document = openable(candidate)
    if document is not None:
        if args.strip() or devtools or restart:
            raise LaunchError("Opening a folder or a document: no args, no devtools, no restart.")
        return open_path(document, wait_s, user_front, before)
    exe = None
    if Path(os.path.expandvars(candidate)).exists():
        exe = os.path.expandvars(candidate)
    elif shutil.which(candidate):
        exe = shutil.which(candidate)
    if exe is None and candidate.lower() in ("steam", "steam.exe"):
        exe = steam.steam_exe()
    if exe is None and " " in app and not _is_start_app(app):
        parts = shlex.split(app, posix=False)
        head = parts[0].strip('"')
        if Path(head).exists() or shutil.which(head):
            exe = shutil.which(head) or head
            args = " ".join(parts[1:]) + (" " + args if args else "")
    if exe is not None and steam.is_steam_exe(exe):
        return launch_steam(exe, args, devtools, restart, wait_s, user_front, before)
    if restart:
        raise LaunchError("restart=true: Steam only (restart with its switches).")
    if exe is not None:
        policy.check_launch_exe(exe, args, devtools=devtools)
        browser = policy.is_browser_exe(exe)
        if devtools and browser:
            # Jamais le profil réel de Julien derrière un port DevTools : profil neuf, sans compte ni synchronisation.
            profile = fresh_profile()
            extra += f' --user-data-dir="{profile}" --no-first-run --no-default-browser-check --disable-sync'
            notes.append("DevTools browser: fresh profile, no account and no sync (removed afterwards)")
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = w.SW_SHOWNOACTIVATE
        if policy.is_chromium_exe(exe):
            features = OCCLUSION_FEATURES + (NO_ACCOUNT_FEATURES if devtools and browser else ())
            args, disable = merge_disable_features(args, features)
            extra += f" {disable} --force-renderer-accessibility"
            notes.append("Chromium/Electron: rendering and accessibility kept even when the window is covered")
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
            raise LaunchError(f'Cannot launch "{exe}": {error}') from error
        pid, via = proc.pid, "executable"
    else:
        entry = find_start_app(app)
        if entry is None:
            raise LaunchError(f'"{app}": neither an executable nor a Start menu app.')
        app_id = entry["AppID"]
        title_hint = entry["Name"]
        policy.check_launch_app_id(app_id, title_hint)
        target = _app_id_path(app_id)
        if target and target.lower().endswith(".exe"):
            # Exécutable : politique et identité par processus.
            return launch(target, args, devtools, wait_s, auto, on_account_kill, restart)
        if target:
            policy.check_launch_exe(target, "")  # document ouvert par son association : jamais un script
        policy.check_launch_app_id(app_id, title_hint, args, devtools)
        notes.append(f'Start menu: "{title_hint}"')
        if "!" in app_id:
            try:
                pid = _activate_packaged(app_id, args.strip())
                via = "Store app"
            except (comtypes.COMError, OSError) as error:
                raise LaunchError(f'Activation of "{title_hint}" refused: {error}') from error
        else:
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{app_id}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            via = "Start menu shortcut"
            if args.strip():
                notes.append("arguments ignored: a Start menu shortcut is launched as is")
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
            f"DevTools: no window of the launched process within {max(1.0, wait_s):.0f} s (single-instance app "
            "already open? close it, then launch again with devtools=true): launched process closed, no other "
            "window taken in its place."
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
                    "no window was in the foreground: the app activated itself, placed at the bottom of the z-order"
                )
    if hwnd and w.foreground() != hwnd and user_front and w.IsWindow(user_front):
        w.SetWindowPos(hwnd, user_front, 0, 0, 0, 0, w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE)
    if not hwnd:
        notes.append("no window of this app detected (single-instance app already open? windows to find it)")
    if devtools:
        reason = account_found(port, profile)
        if reason:
            removed = close_account_browser(proc, pid, profile)
            raise AccountError(
                f"Account detected in the DevTools browser ({reason}): closed at once (never the user's account behind "
                f"a DevTools port){_profile_fate(profile, removed)}."
            )
        if profile is not None and proc is not None:
            guard_account(proc, port, profile, on_account_kill)
    if auto is not None and hwnd and (exe is None or snapshot.framework_of(hwnd) in ("UWP", "WinUI")):
        # Jamais d'activation pour « montrer » une application Store : mesuré, elle ne l'affichait dans aucun des 17
        # lancements et coûtait environ 1 s de premier plan à Julien. Si Windows la laisse vide, la note le dit.
        # Pendant l'attente (jusqu'à 3 s), la garde rend aussitôt le premier plan si l'application le prend.
        with foreground.Watch(hwnd, front=user_front, tail_s=0) as watch:
            ready = wait_content(auto, hwnd)
            if not ready and packages.resume_suspended(hwnd, {pid} if pid else None):
                # Suspendue par Windows (lancée sans activation) : reprise, sans premier plan, puis contenu relu.
                notes.append("Store app suspended by Windows: resumed without foreground")
                ready = wait_content(auto, hwnd)
        if watch.stolen:
            took_back = took_back or not watch.failed
        if not ready:
            if not uwp_unshown(hwnd):
                notes.append("content not ready yet (interface still empty): look before acting")
            elif _holds_front(hwnd):
                notes.append("Store app never shown by Windows, although it holds the foreground")
            else:
                notes.append(
                    "Store app never shown by Windows (launched without activation, content suspended: waiting "
                    'changes nothing): showing it requires the foreground, window(action="front", '
                    "foreground=true) with the user's consent"
                )
    # Premier plan relu en fin de lancement, jamais supposé : la note ne dit « rendu » que s'il l'est vraiment.
    returned = not (user_front and w.IsWindow(user_front) and _holds_front(hwnd))
    if not returned:
        notes.append(
            "the app took the foreground and Windows refused to give it back: foreground NOT given back to the user "
            "(the server's guard retries)"
        )
    elif took_back:
        notes.append("the app came to the front: foreground given back to the user at once")
    app_pid = foreground.app_pid(hwnd) if hwnd else pid
    if pid and app_pid != pid and w.process_name(app_pid).lower() in foreground.HOST_PROCESSES:
        app_pid = pid  # cadre UWP sans CoreWindow enfant : l'application est le processus lancé, pas l'hôte
    return Launched(hwnd, app_pid, port, via, notes, profile, user_front, returned)


# --- Steam ----------------------------------------------------------------------------------------------------
def _wait_steam(deadline: float, user_front: int, before: set[int]) -> tuple[int, bool]:
    """Attend la fenêtre principale de Steam (steamwebhelper), en rendant le premier plan à l'utilisateur dès qu'une fenêtre
    de Steam le prend (fenêtre de mise à jour de steam.exe comprise)."""
    took_back = False
    while True:
        fg = w.foreground()
        name = w.process_name(w.window_pid(fg)).lower() if fg else ""
        if (
            user_front
            and fg
            and fg != user_front
            and name in (steam.STEAM_EXE, steam.WEBHELPER)
            and not foreground.user_is_switching()
        ):
            took_back = foreground.give_back(user_front, exclude={fg}) or took_back
        hwnd = steam.main_window()
        if hwnd:
            return hwnd, took_back
        if time.monotonic() >= deadline:
            return 0, took_back
        time.sleep(0.1)


def launch_steam(
    exe: str, args: str, devtools: bool, restart: bool, wait_s: float, user_front: int, before: set[int]
) -> Launched:
    """Steam avec -cef-disable-occlusion -cef-force-accessibility (lisible et pilotable recouvert), et
    -cef-enable-debugging si devtools (port 127.0.0.1:8080 sur la session de l'utilisateur : seulement avec son accord).
    Steam déjà ouvert : ses commutateurs ne changent qu'à une relance (restart, accord de l'utilisateur ; refusée si un jeu
    tourne). Tout est contrôlé AVANT de fermer Steam."""
    policy.check_launch_exe(exe, args)  # arguments du modèle : jamais de port DevTools ni de commutateur dangereux
    notes: list[str] = []
    running = bool(steam.steam_pids())
    if running and not restart:
        port = steam.debugging_port() if devtools else None
        if devtools and not port:
            raise LaunchError(
                "Steam is already open without DevTools: its switches apply only at startup. With the user's "
                'consent, launch(app="Steam", devtools=true, restart=true) closes it, then starts it again '
                "(downloads interrupted; refused if a game is running). Nothing was done."
            )
        hwnd = steam.main_window()
        took_back = False
        if not hwnd:
            # Steam dans la zone de notification : sa fenêtre principale s'ouvre par son propre protocole.
            subprocess.Popen(
                [exe, "steam://open/main"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )  # fmt: skip
            hwnd, took_back = _wait_steam(time.monotonic() + max(5.0, wait_s), user_front, before)
        notes.append(
            "Steam was already open (existing window)"
            + (
                f", DevTools already active on 127.0.0.1:{port}"
                if port
                else "; if look finds it empty once covered, restart=true (the user's consent) starts it again with "
                "-cef-disable-occlusion -cef-force-accessibility"
            )
        )
        if took_back:
            notes.append("Steam came to the front: foreground given back to the user at once")
        pid = w.window_pid(hwnd) if hwnd else 0
        if port:
            log.event("steam-devtools", {"action": "registered", "port": port, "pid": pid})
        return Launched(hwnd, pid, port or 0, "Steam", notes, None, user_front, True, own_session=bool(port))
    if devtools:
        other = steam.port_taken_by_other()
        if other and other.lower() != steam.WEBHELPER:
            raise LaunchError(
                f"Port {steam.DEVTOOLS_PORT} already taken by {other}: Steam's DevTools could not open on it "
                "(and that program would be taken for Steam). Nothing was closed."
            )
    if running:
        try:
            steam.shutdown(exe)
        except steam.SteamError as error:
            raise LaunchError(str(error)) from error
        notes.append("Steam closed cleanly, then started again")
        before = set(w.enum_windows())
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = w.SW_SHOWNOACTIVATE
    try:
        proc = subprocess.Popen(
            steam.command(exe, args, devtools), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, startupinfo=startupinfo,
        )  # fmt: skip
    except OSError as error:
        raise LaunchError(f"Cannot launch Steam: {error}") from error
    log.event("steam", {"action": "launch", "devtools": devtools, "restart": running, "pid": proc.pid})
    deadline = time.monotonic() + max(wait_s, steam.START_WAIT_S)
    hwnd, took_back = _wait_steam(deadline, user_front, before)
    notes.append("Steam: -cef-disable-occlusion -cef-force-accessibility (readable and drivable even when covered)")
    port = 0
    if devtools:
        while time.monotonic() < deadline:
            port = steam.debugging_port() or 0
            if port:
                break
            time.sleep(0.5)
        if port:
            notes.append(
                f"Steam DevTools on 127.0.0.1:{port}: the user's Steam session is reachable by any local program "
                'while Steam stays open this way; launch(app="Steam", restart=true) without devtools closes it'
            )
            log.event("steam-devtools", {"action": "opened", "port": port, "pid": proc.pid})
        else:
            notes.append("Steam DevTools: port not opened in time (actions without DevTools)")
    if not hwnd:
        notes.append("Steam main window not shown yet (update, sign-in?): windows, then look")
    elif took_back:
        notes.append("Steam came to the front: foreground given back to the user at once")
    returned = not (user_front and w.IsWindow(user_front) and hwnd and _holds_front(hwnd))
    pid = w.window_pid(hwnd) if hwnd else proc.pid
    return Launched(
        hwnd, pid, port, "Steam (restarted)" if running else "Steam", notes, None, user_front, returned,
        own_session=bool(port),
    )  # fmt: skip
