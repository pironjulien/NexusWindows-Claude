"""Politique de sécurité : la politique COMMUNE à Claude et Antigravity (NexusHarness/shared/garde-fous.json).

Accord Claude–Antigravity du 18/10/2026 (journal de la trinité, « Garde-fous ») : un garde-fou n'existe que s'il
empêche une auto-élévation ou une fuite de secrets, ou s'il demande l'accord avant un geste sans retour qui touche des
tiers, de l'argent ou détruit. L'utilisateur travaille en « ignorer les permissions » : le shell de Claude fait déjà tout,
donc consoles, interpréteurs, Explorateur, barre des tâches, outils d'administration et autres agents sont libres.

- BLOCAGE DUR (garde-fous.json, catégories de « claude ») : bureau sécurisé (UAC, verrouillage, ouverture de session),
  fenêtres d'identifiants Windows, gestionnaires de mots de passe. L'application Claude et la fenêtre d'Antigravity
  sont permises (relecture du 18/10 : aucune demande d'autorisation n'y passe en « ignorer les permissions »).
- CONFIRMATION ÉTANCHE : confirm.py (mêmes motifs, même fichier).
- LANCEMENT : seulement ce qui exposerait une session de l'utilisateur (profil de navigateur choisi par le modèle, port
  DevTools ou extension chargée sur un navigateur ; devtools=true ouvre un profil neuf).
Politique absente ou illisible : toute action refusée (échec fermé). `look` reste toujours permis, sauf blocage dur.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

from . import win32 as w
from .codeprint import SHARED_DIR  # politique vivante (NexusHarness/shared) ou embarquée hors du dépôt

AGENT = "claude"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
import garde_fous  # noqa: E402  (module commun, hors du paquet)

EXPLORER_CLASSES = {"CabinetWClass", "ExploreWClass"}  # fenêtres de l'Explorateur de fichiers (repère, pas un refus)

# Commutateurs Chromium qui ouvrent un canal de contrôle sur le navigateur, donc sur les sessions qu'il porte.
# Noms sans préfixe : Chromium accepte « -- », « - » et « / ».
CHROMIUM_DENY_SWITCHES = {
    "load-extension", "remote-debugging-port", "remote-debugging-pipe", "remote-debugging-address",
    "remote-allow-origins", "disable-web-security",
    "inspect", "inspect-brk", "inspect-port",  # Electron : inspecteur Node du processus principal
    # Steam : port DevTools sur la session de Julien (seulement launch(app="Steam", devtools=true), avec son accord).
    "cef-enable-debugging", "devtools-port",
}  # fmt: skip
# Profils d'un navigateur : jamais choisis par le modèle (ce serait celui de Julien) ; devtools=true en crée un neuf.
PROFILE_SWITCHES = {"user-data-dir", "profile-directory"}
# Préfixes de commutateur reconnus par Chromium sous Windows (CommandLine::IsSwitch), du plus long au plus court.
SWITCH_PREFIXES = ("--", "-", "/")

# Navigateurs Chromium : exécutable principal, et DLL du navigateur (dossier de version, à côté des annexes).
BROWSER_EXES = {"msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe", "opera.exe", "chromium.exe"}
BROWSER_DLLS = ("msedge.dll", "chrome.dll")


class PolicyError(RuntimeError):
    pass


# Raisons de blocage dur de garde-fous.json (politique commune, en français) : rendues en anglais au modèle ; une
# raison inconnue (nouvelle catégorie) reste telle quelle.
HARD_BLOCK_REASONS = {
    "invite UAC, écran de verrouillage ou ouverture de session (bureau sécurisé)": (
        "UAC prompt, lock screen or sign-in screen (secure desktop)"
    ),
    "fenêtre d'identifiants Windows": "Windows credential window",
    "gestionnaire de mots de passe": "password manager",
}


def english_reason(reason: str) -> str:
    """Raison de garde_fous.hard_block_reason en anglais (suffixe « (processus) » gardé)."""
    for french, english in HARD_BLOCK_REASONS.items():
        if reason.startswith(french):
            return english + reason[len(french) :]
    return reason


def hard_block(process: str, cls: str) -> str:
    """Raison du blocage dur de cette fenêtre (ni lue ni pilotée), « » si libre. Politique illisible : PolicyError."""
    try:
        return english_reason(garde_fous.hard_block_reason(AGENT, process, cls))
    except garde_fous.GardeFousError as error:
        raise PolicyError(f"safety policy unreadable ({error}): nothing was done (fail closed)") from error


def act_refusal(process: str, cls: str, title: str = "") -> str | None:
    """Raison du refus d'agir dans cette fenêtre, ou None si permis."""
    return hard_block(process, cls) or None


def pe_subsystem(path: str) -> int | None:
    """Sous-système PE de l'exécutable : 2 graphique, 3 console ; None si illisible."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(4096)
    except OSError:
        return None
    if len(header) < 0x40 or header[:2] != b"MZ":
        return None
    offset = struct.unpack_from("<I", header, 0x3C)[0]
    if offset + 0x5E > len(header) or header[offset : offset + 4] != b"PE\0\0":
        return None
    return struct.unpack_from("<H", header, offset + 24 + 68)[0]


# --- Lancement ------------------------------------------------------------------------------------------------
def _has_any(folder: Path, names) -> bool:
    try:
        return any((folder / name).is_file() for name in names)
    except OSError:
        return False


def is_browser_exe(exe: str) -> bool:
    """Exécutable d'un dossier d'installation de navigateur Chromium : le navigateur lui-même, mais aussi ses annexes
    (msedge_proxy, pwahelper, chrome_proxy, dossier de version…), qui ouvrent les mêmes profils, ceux de l'utilisateur.
    Repère : l'exécutable principal ou la DLL du navigateur dans son dossier ou l'un des deux parents."""
    path = Path(os.path.expandvars(exe))
    if path.name.lower() in BROWSER_EXES:
        return True
    if not path.is_absolute():
        return False
    folder = path.parent
    return any(_has_any(base, (*BROWSER_EXES, *BROWSER_DLLS)) for base in (folder, folder.parent, folder.parent.parent))


def is_chromium_exe(exe: str) -> bool:
    """Chromium : navigateurs (et leurs annexes) ou applications Electron, reconnaissables à leurs ressources."""
    path = Path(os.path.expandvars(exe))
    if path.name.lower() == "electron.exe" or is_browser_exe(str(path)):
        return True
    folder = path.parent
    return (folder / "resources" / "app.asar").exists() or (folder / "chrome_100_percent.pak").exists()


def split_args(args: str) -> list[str]:
    """Arguments découpés comme Chromium les découpe sous Windows (CommandLineToArgvW), espaces de bord retirés."""
    return [token.strip() for token in w.command_line_args(args)] if args.strip() else []


def switch_key(token: str) -> str | None:
    """Nom du commutateur que Chromium lit dans ce jeton (CommandLine::IsSwitch) : un seul préfixe « -- », « - » ou
    « / » retiré, clé avant « = », en minuscules (Windows). None si ce n'est pas un commutateur."""
    for prefix in SWITCH_PREFIXES:
        if token.startswith(prefix):
            if len(token) == len(prefix):
                return None
            return token[len(prefix) :].split("=", 1)[0].lower()
    return None


def switch_refusal(args: str, chromium: bool = True, browser: bool = True, devtools: bool = False) -> str | None:
    """Raison de refuser ces arguments, lus comme Chromium les lit ; None si permis."""
    tail_matters = chromium or devtools  # l'outil ajoute alors ses propres commutateurs APRÈS ceux du modèle
    if tail_matters and "single-argument" in args.lower():
        return "--single-argument turns the rest of the line into one argument (the tool's switches neutralized)"
    for token in split_args(args):
        if tail_matters and token == "--":
            return '"--" ends the switches: those the tool adds (fresh profile, no account) would be ignored'
        key = switch_key(token)
        if key in CHROMIUM_DENY_SWITCHES:
            return f"the {key} switch opens a control channel on the browser's sessions (devtools=true)"
        if key in PROFILE_SWITCHES and (browser or devtools):
            return (
                f"the {key} switch would pick a browser profile (the user's?): never given by the model; "
                "devtools=true opens a fresh profile"
            )
    return None


def _refuse(reason: str) -> PolicyError:
    return PolicyError(f"Launch refused: {reason}.")


def check_launch_exe(path: str, args: str, devtools: bool = False) -> None:
    """Refuse seulement ce qui ouvrirait une session de l'utilisateur à un canal de contrôle : DevTools hors de l'exécutable
    principal d'un navigateur (son profil neuf), profil choisi par le modèle, commutateurs de débogage. Un alias
    d'exécution WindowsApps est jugé sur sa cible résolue aussi."""
    target = Path(os.path.expandvars(path))
    resolved = w.appexec_target(str(target))
    candidates = [target] + ([Path(os.path.expandvars(resolved))] if resolved else [])
    for candidate in candidates:
        name = candidate.name.lower()
        browser = is_browser_exe(str(candidate))
        if devtools and browser and name not in BROWSER_EXES:
            raise _refuse(
                f"devtools=true only on the browser's main executable (msedge.exe, chrome.exe…), never "
                f"on {name}, which would open the user's profile"
            )
        reason = switch_refusal(
            args, chromium=browser or is_chromium_exe(str(candidate)), browser=browser, devtools=devtools
        )
        if reason:
            raise _refuse(reason)


def check_launch_app_id(app_id: str, name: str, args: str = "", devtools: bool = False) -> None:
    """Application du menu Démarrer ou empaquetée : ses arguments sont lus comme des commutateurs Chromium (nature
    inconnue : règles les plus strictes) ; jamais de port DevTools (son processus et son profil ne sont pas
    vérifiables)."""
    if devtools:
        raise PolicyError(
            f'Launch refused: devtools=true not for "{name}" (Start menu or packaged app): '
            "give the full path of the Chromium or Electron executable."
        )
    reason = switch_refusal(args)
    if reason:
        raise _refuse(reason)
