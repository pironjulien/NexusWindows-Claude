"""Politique de sécurité : où nexus-windows peut agir et ce qu'il peut lancer.

Principe : rien ne doit permettre d'exécuter du code ou des commandes en contournant les permissions du shell de
Claude Code (consoles, interpréteurs, scripts), ni d'agir dans les zones de secrets ou dans d'autres agents d'IA.

- REFUS ABSOLU (non surchargeable) : consoles et terminaux, invites UAC et d'identifiants, écran de verrouillage,
  gestionnaires de mots de passe, Claude lui-même, boîte « Exécuter ».
- REFUS PAR DÉFAUT (surchargeable par Julien dans ~/.claude/nexus-claude/policy.json, clé « allow_processes ») :
  autres agents d'IA et IDE à terminal intégré, outils d'administration (registre, consoles MMC, gestionnaire des
  tâches).
- LANCEMENT : ni interpréteur, console ou hôte d'exécution système (surchargeable par chemin, clé « allow_launch ») ;
  arguments lus comme Chromium les lit (préfixes « -- », « - », « / ») ; jamais un profil de navigateur choisi par
  le modèle ; DevTools seulement sur l'exécutable principal d'un navigateur (profil neuf) ou une application Electron.
`look` reste toujours permis : voir n'exécute rien.
"""

from __future__ import annotations

import json
import os
import re
import struct
from pathlib import Path

from . import log
from . import win32 as w

HARD_DENY_PROCESSES = {
    "windowsterminal.exe", "openconsole.exe", "conhost.exe", "mintty.exe", "powershell_ise.exe", "consent.exe",
    "credentialuibroker.exe", "lockapp.exe", "logonui.exe", "claude.exe", "1password.exe", "bitwarden.exe",
    "keepass.exe", "keepassxc.exe", "dashlane.exe", "nordpass.exe", "roboform.exe", "lastpass.exe",
}  # fmt: skip
SOFT_DENY_PROCESSES = {
    "antigravity.exe", "chatgpt.exe", "codex.exe", "code.exe", "code - insiders.exe", "cursor.exe", "windsurf.exe",
    "regedit.exe", "mmc.exe", "taskmgr.exe", "devenv.exe", "idea64.exe", "pycharm64.exe", "rider64.exe",
}  # fmt: skip
SHELL_DENY_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "NotifyIconOverflowWindow"}
SHELL_DENY_PROCESSES = {"searchhost.exe", "searchapp.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe"}
SOFT_DENY_CLASSES = {"CabinetWClass", "ExploreWClass"}  # fenêtres de l'Explorateur de fichiers
# Zones d'une fenêtre qui exécutent ce qu'on y tape (barre d'adresse de l'Explorateur et des boîtes de fichiers).
COMMAND_SURFACE_CLASSES = {"Address Band Root", "Breadcrumb Parent"}
HARD_DENY_CLASSES = {
    "ConsoleWindowClass",
    "CASCADIA_HOSTING_WINDOW_CLASS",
    "PseudoConsoleWindow",
    "Credential Dialog Xaml Host",
}
RUN_DIALOG_TITLES = {"exécuter", "run"}

# Lancement : interpréteurs, hôtes de scripts et hôtes d'exécution système (même graphiques), extensions
# exécutables autres que .exe.
LAUNCH_DENY_EXES = {
    "cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe", "bash.exe", "wsl.exe", "wslhost.exe", "python.exe",
    "pythonw.exe", "py.exe", "pyw.exe", "node.exe", "wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe",
    "regsvr32.exe", "msiexec.exe", "installutil.exe", "regasm.exe", "msbuild.exe", "schtasks.exe", "at.exe",
    "sc.exe", "reg.exe", "certutil.exe", "bitsadmin.exe", "forfiles.exe", "pcalua.exe", "conhost.exe",
    "mintty.exe", "powershell_ise.exe", "windowsterminal.exe", "openconsole.exe",
    "electron.exe",  # Electron générique : exécute le script JavaScript (Node) qu'on lui donne
    # Hôtes d'exécution système : SyncAppvPublishingServer « n; calc » exécute du PowerShell, cmstp et
    # InfDefaultInstall un .inf, hh un .chm scripté, msdt et scriptrunner des scripts, presentationhost une XBAP.
    "syncappvpublishingserver.exe", "cmstp.exe", "infdefaultinstall.exe", "hh.exe", "msdt.exe",
    "scriptrunner.exe", "presentationhost.exe",
    # Shells et interpréteurs graphiques : Git Bash/CMD, Git GUI (ses outils sont des commandes), Java.
    "git-bash.exe", "git-cmd.exe", "git-gui.exe", "javaw.exe",
}  # fmt: skip
# Familles versionnées d'interpréteurs graphiques (nom sans .exe) : pythonw3.14, wish86t, AutoHotkey64, AutoIt3_x64…
# Puis débogueurs et hôtes d'exécution exposés par un alias WindowsApps (même graphiques : la règle « console » ne les
# couvre pas) et leurs cibles résolues : débogueurs (cdb, ntsd, kd, kdnet, WinDbg, DbgX.Shell, dbgsrv), gestionnaire de
# paquets (winget*, WindowsPackageManager*), WSL (wslconfig), autres agents d'IA et leurs hôtes (codex*, ShellMcpServers,
# ActionsMcpHost, extension-host) et Claude lui-même (claude*). Les noms peuvent contenir « - » ou « . » (DbgX.Shell,
# codex-command-runner, ShellMcpServers.Packaging) : la classe comprend donc « \w », « - » et « . ».
LAUNCH_DENY_NAMES = re.compile(
    r"pythonw?[\d.]*|wish[\d.]*t?|tclsh[\d.]*t?|autohotkey\w*|autoit3\w*"
    r"|cdb[\w-]*|ntsd[\w-]*|kd[\w-]*|kdnet|windbg[\w-]*|dbgx\.shell|dbgsrv[\w-]*"
    r"|winget[\w-]*|windowspackagemanager[\w-]*|wslconfig|codex[\w-]*|shellmcpservers[\w.-]*"
    r"|actionsmcphost|extension-host|claude[\w-]*",
    re.IGNORECASE,
)
# Refusés avec un argument seulement : control.exe x.cpl exécute le panneau (une DLL), mmc.exe x.msc peut exécuter du
# code à l'ouverture, regedit.exe x.reg importe dans le registre. Sans argument, ils ne font que s'ouvrir.
LAUNCH_DENY_WITH_ARGS = {"control.exe", "mmc.exe", "regedit.exe"}
# Commutateurs Chromium qui exécutent une commande, chargent du code ou ouvrent un canal de contrôle à côté de
# devtools=true (qui, lui, impose un profil neuf). Noms sans préfixe : Chromium accepte « -- », « - » et « / ».
CHROMIUM_DENY_SWITCHES = {
    "gpu-launcher", "renderer-cmd-prefix", "utility-cmd-prefix", "browser-subprocess-path", "plugin-launcher",
    "ppapi-plugin-launcher", "load-extension", "remote-debugging-port", "remote-debugging-pipe",
    "remote-debugging-address", "remote-allow-origins", "no-sandbox", "disable-web-security", "js-flags",
    "inspect", "inspect-brk", "inspect-port",  # Electron : inspecteur Node du processus principal
}  # fmt: skip
# Profils d'un navigateur : jamais choisis par le modèle (ce serait celui de Julien) ; devtools=true en crée un neuf.
PROFILE_SWITCHES = {"user-data-dir", "profile-directory"}
# Préfixes de commutateur reconnus par Chromium sous Windows (CommandLine::IsSwitch), du plus long au plus court.
SWITCH_PREFIXES = ("--", "-", "/")
LAUNCH_DENY_SUFFIXES = {
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".lnk", ".url", ".py",
    ".pyw", ".msc", ".reg", ".scr", ".cpl", ".msi", ".msp", ".appref-ms", ".jar", ".com", ".pif", ".chm",
}  # fmt: skip
# Applications du menu Démarrer (AppID ou nom, minuscules sans espaces) : consoles, interpréteurs, assistance à
# distance, débogueur, automatisation, Claude lui-même.
LAUNCH_DENY_APP_IDS = (
    "windowsterminal", "powershell", "cmd.exe", "commandprompt", "invitedecommandes", "python", "wsl", "ubuntu",
    "gitbash", "gitcmd", "gitgui", "quickassist", "assistancerapide", "dbgx", "windbg", "powerautomate", "claude",
)  # fmt: skip

# Navigateurs Chromium : exécutable principal, et DLL du navigateur (dossier de version, à côté des annexes).
BROWSER_EXES = {"msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe", "opera.exe", "chromium.exe"}
BROWSER_DLLS = ("msedge.dll", "chrome.dll")

POLICY_FILE = log.ROOT / "policy.json"


class PolicyError(RuntimeError):
    pass


def _policy() -> dict:
    try:
        data = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _allowed_overrides() -> set[str]:
    return {str(name).lower() for name in _policy().get("allow_processes", [])}


def _allowed_launch() -> set[str]:
    """Exécutables que Julien autorise malgré la règle « interpréteur » (chemins complets, clé allow_launch)."""
    return {os.path.normcase(os.path.abspath(os.path.expandvars(str(p)))) for p in _policy().get("allow_launch", [])}


def act_refusal(process: str, cls: str, title: str) -> str | None:
    """Raison du refus d'agir dans cette fenêtre, ou None si permis."""
    process = process.lower()
    if cls in HARD_DENY_CLASSES or process in HARD_DENY_PROCESSES:
        return "console, invite de sécurité, gestionnaire de mots de passe ou Claude"
    if cls == "#32770" and process == "explorer.exe" and title.strip().lower() in RUN_DIALOG_TITLES:
        return "boîte « Exécuter » (elle lancerait une commande)"
    if cls in SHELL_DENY_CLASSES or process in SHELL_DENY_PROCESSES:
        return "barre des tâches, bureau, menu Démarrer ou Recherche (ils lancent programmes et commandes)"
    if cls in SOFT_DENY_CLASSES and "explorer.exe" not in _allowed_overrides():
        return (
            "Explorateur de fichiers : un double-clic y exécute n'importe quel fichier et sa barre d'adresse des "
            f"commandes (fichiers : passer par le shell ; Julien peut l'autoriser dans {POLICY_FILE}, "
            "allow_processes : explorer.exe)"
        )
    if process in SOFT_DENY_PROCESSES and process not in _allowed_overrides():
        return (
            "autre agent d'IA, IDE à terminal intégré ou outil d'administration (Julien peut l'autoriser dans "
            f"{POLICY_FILE}, clé allow_processes)"
        )
    return None


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
            return f"le commutateur {key} exécute du code ou ouvre un canal de contrôle (DevTools : devtools=true)"
        if key in PROFILE_SWITCHES and (browser or devtools):
            return (
                f"le commutateur {key} choisirait un profil du navigateur (celui de Julien ?) : jamais fourni par le "
                "modèle ; devtools=true ouvre un profil neuf"
            )
    return None


def _refuse(reason: str) -> PolicyError:
    return PolicyError(
        f"Lancement refusé : {reason}. Pour exécuter une commande, utiliser l'outil shell de Claude Code "
        "(soumis aux permissions de Julien)."
    )


def _unreadable_exe(path: str) -> bool:
    """Un .exe présent sur le disque mais dont le contenu ne peut pas être lu (fichier verrouillé ou protégé) : son
    sous-système reste inconnu, donc échec fermé. Un fichier absent (la politique n'a pas à juger de l'existence) ou
    lisible mais non-PE (Windows ne l'exécuterait pas) n'est pas concerné."""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as handle:
            handle.read(1)
        return False
    except OSError:
        return True


def _launch_reason(target: Path, args: str, devtools: bool, allowed: bool, alias: bool) -> str | None:
    """Raison de refuser le lancement de CET exécutable (un alias d'exécution OU sa cible résolue), ou None si permis.
    `alias` : le chemin est un alias d'exécution WindowsApps (reparse point) — son sous-système PE n'est pas lisible et
    n'a pas de sens ; seule la cible résolue est jugée là-dessus."""
    name = target.name.lower()
    browser = is_browser_exe(str(target))
    if target.suffix.lower() in LAUNCH_DENY_SUFFIXES:
        return f"un fichier {target.suffix} s'exécute comme un script"
    if (name in LAUNCH_DENY_EXES or LAUNCH_DENY_NAMES.fullmatch(target.stem)) and not allowed:
        return f"{name} est un interpréteur, une console, un débogueur ou un hôte d'exécution de scripts"
    if name in LAUNCH_DENY_WITH_ARGS and args.strip() and not allowed:
        return f"{name} avec un argument exécute le fichier ou le panneau désigné"
    if name == "explorer.exe" and args.strip() and not Path(args.strip().strip('"')).is_dir():
        return "explorer.exe avec un argument autre qu'un dossier peut lancer n'importe quel fichier"
    if not alias and not allowed:
        # Tout fichier, quel que soit son suffixe : une copie de programme renommée .bin ou .dat se lance quand même.
        subsystem = pe_subsystem(str(target))
        if subsystem == 3:
            return f"{name} est un programme console"
        if subsystem is not None and target.suffix.lower() != ".exe":
            return f"{name} est un programme sous un autre nom que .exe (copie renommée ?)"
        if subsystem is None and target.suffix.lower() == ".exe" and _unreadable_exe(str(target)):
            return f"{name} : sous-système illisible (fichier protégé ou verrouillé) ; refusé par sécurité"
    if devtools and browser and name not in BROWSER_EXES:
        return (
            f"devtools=true seulement sur l'exécutable principal du navigateur (msedge.exe, chrome.exe…), jamais sur "
            f"{name}, qui ouvrirait le profil de Julien"
        )
    return switch_refusal(args, chromium=browser or is_chromium_exe(str(target)), browser=browser, devtools=devtools)


def check_launch_exe(path: str, args: str, devtools: bool = False) -> None:
    """Refuse ce qui exécuterait du code ou une commande hors des permissions du shell de Claude Code, ou ouvrirait
    un profil de navigateur de Julien. Un alias d'exécution WindowsApps (reparse point AppExecLink) est résolu et
    TOUTES les règles s'appliquent à l'alias ET à sa cible : lui seul rend le sous-système PE illisible et laissait
    donc passer débogueurs, gestionnaires de paquets et exécuteurs de commandes."""
    target = Path(os.path.expandvars(path))
    allowed_names = _allowed_launch()
    resolved = w.appexec_target(str(target))
    if resolved is None:
        paths: list[tuple[Path, bool]] = [(target, False)]  # fichier ordinaire
    elif resolved == "":
        # Alias d'exécution dont la cible est illisible : échec fermé, sauf autorisation explicite de l'alias.
        if os.path.normcase(os.path.abspath(str(target))) not in allowed_names:
            raise _refuse(f"{target.name.lower()} est un alias d'exécution WindowsApps dont la cible est illisible")
        return
    else:
        paths = [(target, True), (Path(os.path.expandvars(resolved)), False)]
    allowed = any(os.path.normcase(os.path.abspath(str(p))) in allowed_names for p, _ in paths)
    for candidate, is_alias in paths:
        reason = _launch_reason(candidate, args, devtools, allowed, is_alias)
        if reason:
            raise _refuse(reason)


def check_launch_app_id(app_id: str, name: str, args: str = "", devtools: bool = False) -> None:
    """Application du menu Démarrer ou empaquetée : ni console, ni interpréteur, ni agent ou outil de contrôle à
    distance ; ses arguments sont lus comme des commutateurs Chromium (nature inconnue : règles les plus strictes) ;
    jamais de port DevTools (son processus et son profil ne sont pas vérifiables)."""
    low = (app_id + " " + name).lower().replace(" ", "")
    if any(marker in low for marker in LAUNCH_DENY_APP_IDS):
        raise PolicyError(
            f"Lancement refusé : « {name} » est une console, un interpréteur, un outil d'exécution ou de contrôle à "
            "distance, ou Claude. Utiliser l'outil shell de Claude Code."
        )
    if devtools:
        raise PolicyError(
            f"Lancement refusé : devtools=true pas pour « {name} » (application du menu Démarrer ou empaquetée) : "
            "passer le chemin complet de l'exécutable Chromium ou Electron."
        )
    reason = switch_refusal(args)
    if reason:
        raise _refuse(reason)
