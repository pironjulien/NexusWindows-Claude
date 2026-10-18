"""Politique de sécurité : la politique COMMUNE à Claude et Antigravity (NexusHarness/shared/garde-fous.json).

Accord Claude–Antigravity du 18/10/2026 (journal de la trinité, « Garde-fous ») : un garde-fou n'existe que s'il
empêche une auto-élévation ou une fuite de secrets, ou s'il demande l'accord avant un geste sans retour qui touche des
tiers, de l'argent ou détruit. Julien travaille en « ignorer les permissions » : le shell de Claude fait déjà tout,
donc consoles, interpréteurs, Explorateur, barre des tâches, outils d'administration et autres agents sont libres.

- BLOCAGE DUR (garde-fous.json, catégories de « claude ») : bureau sécurisé (UAC, verrouillage, ouverture de session),
  fenêtres d'identifiants Windows, gestionnaires de mots de passe. L'application Claude et la fenêtre d'Antigravity
  sont permises (relecture du 18/10 : aucune demande d'autorisation n'y passe en « ignorer les permissions »).
- CONFIRMATION ÉTANCHE : confirm.py (mêmes motifs, même fichier).
- LANCEMENT : seulement ce qui exposerait une session de Julien (profil de navigateur choisi par le modèle, port
  DevTools ou extension chargée sur un navigateur ; devtools=true ouvre un profil neuf).
Politique absente ou illisible : toute action refusée (échec fermé). `look` reste toujours permis, sauf blocage dur.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

from . import win32 as w

AGENT = "claude"
# Source du paquet (installé en mode éditable) : NexusHarness/NexusClaude/nexus-windows/src/nexus_windows/policy.py,
# la politique vivante est NexusHarness/shared. Copie publique et paquet PyPI : pas de dossier commun voisin, la même
# politique est embarquée dans le paquet (nexus_windows/shared, copiée par l'export public).
_LIVE_SHARED = Path(os.environ.get("NEXUS_SHARED_DIR") or Path(__file__).resolve().parents[4] / "shared")
_BUNDLED_SHARED = Path(__file__).resolve().parent / "shared"
SHARED_DIR = _LIVE_SHARED if (_LIVE_SHARED / "garde_fous.py").is_file() else _BUNDLED_SHARED
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


def hard_block(process: str, cls: str) -> str:
    """Raison du blocage dur de cette fenêtre (ni lue ni pilotée), « » si libre. Politique illisible : PolicyError."""
    try:
        return garde_fous.hard_block_reason(AGENT, process, cls)
    except garde_fous.GardeFousError as error:
        raise PolicyError(f"{error} : rien n'a été fait (échec fermé)") from error


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
    (msedge_proxy, pwahelper, chrome_proxy, dossier de version…), qui ouvrent les mêmes profils, ceux de Julien.
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
        return "--single-argument fait du reste de la ligne un seul argument (commutateurs de l'outil neutralisés)"
    for token in split_args(args):
        if tail_matters and token == "--":
            return (
                "« -- » met fin aux commutateurs : ceux que l'outil ajoute (profil neuf, sans compte) seraient ignorés"
            )
        key = switch_key(token)
        if key in CHROMIUM_DENY_SWITCHES:
            return f"le commutateur {key} ouvre un canal de contrôle sur les sessions du navigateur (devtools=true)"
        if key in PROFILE_SWITCHES and (browser or devtools):
            return (
                f"le commutateur {key} choisirait un profil du navigateur (celui de Julien ?) : jamais fourni par le "
                "modèle ; devtools=true ouvre un profil neuf"
            )
    return None


def _refuse(reason: str) -> PolicyError:
    return PolicyError(f"Lancement refusé : {reason}.")


def check_launch_exe(path: str, args: str, devtools: bool = False) -> None:
    """Refuse seulement ce qui ouvrirait une session de Julien à un canal de contrôle : DevTools hors de l'exécutable
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
                f"devtools=true seulement sur l'exécutable principal du navigateur (msedge.exe, chrome.exe…), jamais "
                f"sur {name}, qui ouvrirait le profil de Julien"
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
            f"Lancement refusé : devtools=true pas pour « {name} » (application du menu Démarrer ou empaquetée) : "
            "passer le chemin complet de l'exécutable Chromium ou Electron."
        )
    reason = switch_refusal(args)
    if reason:
        raise _refuse(reason)
