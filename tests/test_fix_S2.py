"""Lot S de la contre-vérification finale (recheck6) :

- Bloquant 3 : les alias d'exécution WindowsApps (reparse points AppExecLink) ne doivent plus contourner la politique
  de lancement. La cible du lien est résolue et TOUTES les règles s'appliquent à l'alias ET à sa cible ; échec fermé
  pour un .exe au sous-système illisible ; débogueurs, gestionnaires de paquets, exécuteurs de commandes et Claude
  refusés par leur nom, suffixe .chm refusé.
- Bloquant 4 : dans une boîte de fichiers (#32770) un élément sous SHELLDLL_DefView ou DirectUIHWND refuse clic droit,
  double-clic, touche « applications » et Maj+F10 ; un menu contextuel shell (#32768 de l'Explorateur ou au-dessus
  d'une boîte de fichiers) refuse tout geste ; le nom de fichier, les boutons Ouvrir/Enregistrer et le clic simple de
  sélection restent permis.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nexus_windows import actions, policy, snapshot, uia
from nexus_windows import win32 as w

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contract import HWND, FakeAuto, node  # noqa: E402

# =============================================================================================================
# Bloquant 3 — alias d'exécution WindowsApps (policy.py, win32.appexec_target)
# =============================================================================================================
WINDOWS_APPS = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps"
ALIASES = sorted(WINDOWS_APPS.glob("*.exe")) if WINDOWS_APPS.is_dir() else []


def _accepts(path: str, args: str = "", devtools: bool = False) -> bool:
    try:
        policy.check_launch_exe(path, args, devtools=devtools)
        return True
    except policy.PolicyError:
        return False


@pytest.mark.skipif(not ALIASES, reason="aucun alias WindowsApps sur ce poste")
@pytest.mark.parametrize("alias", ALIASES, ids=lambda p: p.name)
def test_execution_alias_follows_its_target(alias: Path):
    """Chaque alias réellement présent dans WindowsApps : refusé, ou accepté seulement si sa cible résolue l'est aussi.
    Un alias ne doit jamais être accepté quand sa cible, elle, serait refusée (le trou du constat 1)."""
    target = w.appexec_target(str(alias))
    assert target is not None, f"{alias.name} devrait être un alias d'exécution (reparse point AppExecLink)"
    if _accepts(str(alias)):
        # Accepté : alors la cible résolue, jugée seule, doit l'être aussi (sinon l'alias contournerait la règle).
        assert target and _accepts(target), f"{alias.name} accepté alors que sa cible {target!r} ne l'est pas"


@pytest.mark.skipif(not ALIASES, reason="aucun alias WindowsApps sur ce poste")
@pytest.mark.parametrize(
    "name",
    ["cdbX64.exe", "ntsdX64.exe", "kdX64.exe", "kdnet.exe", "WinDbgX.exe", "dbgsrvX64.exe", "winget.exe",
     "wslconfig.exe", "codex-core-command-runner.exe", "claude-desktop-msstore.exe",
     "ShellMcpServers.Packaging.exe", "ActionsMcpHost.exe", "WindowsPackageManagerMCPServer.exe"],
)  # fmt: skip
def test_dangerous_aliases_are_refused_when_present(name: str):
    alias = WINDOWS_APPS / name
    if not alias.exists():
        pytest.skip(f"{name} absent de ce poste")
    assert not _accepts(str(alias), '-c ".shell cmd /c calc"')
    assert not _accepts(str(alias), "", devtools=True)


def test_resolved_graphical_debugger_targets_refused_by_name():
    """Les cibles graphiques (sous-système 2) des débogueurs ne passent pas la règle « console » : elles sont refusées
    par leur nom, qu'on les lance directement ou qu'elles soient atteintes via leur alias."""
    for path in (r"C:\Program Files\WindowsApps\Pkg\ntsd.exe", r"C:\Program Files\WindowsApps\Pkg\DbgX.Shell.exe",
                 r"C:\Program Files\WindowsApps\Pkg\dbgsrv.exe", r"C:\Program Files\WindowsApps\Pkg\Claude.exe",
                 r"C:\Program Files\WindowsApps\Pkg\codex-command-runner.exe",
                 r"C:\Program Files\WindowsApps\Pkg\WinGetMCPServer.exe"):  # fmt: skip
        assert not _accepts(path), f"{path} devrait être refusé par son nom"


def test_chm_suffix_refused():
    assert not _accepts(r"C:\Windows\Help\aide.chm")


def test_alias_rules_apply_to_both_alias_and_target(monkeypatch, tmp_path):
    """La résolution de l'alias applique les règles aux deux : un nom d'alias refusé OU une cible refusée bloque."""
    benign_target = tmp_path / "jeu.exe"
    benign_target.write_bytes(b"MZ" + b"\0" * 4096)  # fichier présent, lisible (non-PE : non exécutable, non concerné)
    console_target = tmp_path / "console.exe"
    console_target.write_bytes(b"MZ" + b"\0" * 4096)

    # 1) nom d'alias dangereux, cible anodine -> refusé par le nom de l'alias
    monkeypatch.setattr(w, "appexec_target", lambda p: str(benign_target))
    assert not _accepts(r"C:\x\cdbX64.exe")
    # 2) alias anodin, cible « console » -> refusé par la cible
    monkeypatch.setattr(w, "appexec_target", lambda p: str(console_target))
    monkeypatch.setattr(policy, "pe_subsystem", lambda path: 3 if path == str(console_target) else 2)
    assert not _accepts(r"C:\x\monappli.exe")
    # 3) alias anodin, cible anodine -> accepté
    monkeypatch.setattr(policy, "pe_subsystem", lambda path: 2)
    monkeypatch.setattr(w, "appexec_target", lambda p: str(benign_target))
    assert _accepts(r"C:\x\monappli.exe")


def test_unresolvable_alias_fails_closed(monkeypatch):
    """Un alias d'exécution dont la cible est illisible est refusé (échec fermé), sauf autorisation explicite."""
    monkeypatch.setattr(w, "appexec_target", lambda p: "")
    monkeypatch.setattr(policy, "POLICY_FILE", Path("nulle_part.json"))
    assert not _accepts(r"C:\x\mystere.exe")
    monkeypatch.setattr(policy, "_allowed_launch", lambda: {os.path.normcase(os.path.abspath(r"C:\x\mystere.exe"))})
    assert _accepts(r"C:\x\mystere.exe")


def test_unreadable_real_exe_fails_closed(monkeypatch, tmp_path):
    """Un .exe réel au sous-système illisible (fichier protégé/verrouillé) est refusé ; un fichier vide lisible, non.
    Le second cas garantit qu'on ne casse pas le lancement d'exécutables ordinaires dont l'en-tête n'est pas lu."""
    locked = tmp_path / "verrou.exe"
    locked.write_bytes(b"")
    monkeypatch.setattr(w, "appexec_target", lambda p: None)
    monkeypatch.setattr(policy, "pe_subsystem", lambda path: None)
    monkeypatch.setattr(policy, "_unreadable_exe", lambda path: True)
    assert not _accepts(str(locked))
    monkeypatch.setattr(policy, "_unreadable_exe", lambda path: False)  # lisible mais non-PE : pas d'échec fermé
    assert _accepts(str(locked))


def test_benign_windowsapps_aliases_still_launch(monkeypatch):
    """notepad/mspaint/SnippingTool via alias : cible graphique au nom anodin -> acceptés."""
    if not ALIASES:
        pytest.skip("aucun alias WindowsApps sur ce poste")
    for name in ("notepad.exe", "mspaint.exe", "SnippingTool.exe"):
        alias = WINDOWS_APPS / name
        if alias.exists():
            assert _accepts(str(alias)), f"{name} devrait rester lançable"


# =============================================================================================================
# Bloquant 4 — boîtes de fichiers et menus contextuels shell (actions.Executor._refuse_shell_view)
# =============================================================================================================
def _chain(parent: uia.Node, child: uia.Node) -> uia.Node:
    child.parent = parent
    parent.children.append(child)
    return child


def _win(role: str, class_name: str, name: str = "") -> uia.Node:
    n = node(role, name)
    n.props["class_name"] = class_name
    return n


def _file_dialog_tree() -> tuple[uia.Node, uia.Node, uia.Node, uia.Node]:
    """Arbre d'une boîte de fichiers : #32770 > (nom de fichier, bouton Enregistrer) et #32770 > SHELLDLL_DefView >
    DirectUIHWND > un fichier de la liste. Rend (racine, élément de liste, champ nom, bouton)."""
    dialog = _win("Window", "#32770", "Enregistrer sous")
    defview = _chain(dialog, _win("Pane", "SHELLDLL_DefView"))
    directui = _chain(defview, _win("Pane", "DirectUIHWND"))
    item = _chain(directui, node("ListItem", "rapport.txt", has_invoke=True, has_selection_item=True))
    filename = _chain(dialog, node("Edit", "Nom du fichier :", has_value=True, value=""))
    save = _chain(dialog, node("Button", "Enregistrer", has_invoke=True))
    return dialog, item, filename, save


def _dialog_executor(auto: FakeAuto, root: uia.Node, *elements: uia.Node, process: str = "explorer.exe"):
    info = snapshot.WindowInfo(HWND, "Boîte", process, root.props["class_name"], w.Rect(0, 0, 400, 300),
                              False, False, False, "Win32")  # fmt: skip
    els = [snapshot.Element(i + 1, n, 0, HWND) for i, n in enumerate(elements)]
    view = snapshot.View(HWND, info, [HWND], els, snapshot.frame_for(info.rect), framework="Win32",
                         roots={HWND: root})  # fmt: skip
    ex = actions.Executor(auto, HWND, lambda rebuild: view, lambda: None)
    ex.acted = False
    ex._pids = {7}
    ex._windows = {HWND}
    return ex


def test_file_dialog_list_refuses_right_and_double_click():
    dialog, item, _filename, _save = _file_dialog_tree()
    element = snapshot.Element(1, item, 0, HWND)
    ex = _dialog_executor(FakeAuto(), dialog, item)
    with pytest.raises(actions.ActionError, match="[Bb]oîte de fichiers"):
        ex.click_element(element, button="right")
    with pytest.raises(actions.ActionError, match="[Bb]oîte de fichiers"):
        ex.click_element(element, button="left", count=2)


def test_file_dialog_single_click_selection_allowed():
    dialog, item, _filename, _save = _file_dialog_tree()
    auto = FakeAuto()
    ex = _dialog_executor(auto, dialog, item)
    outcome = ex.click_element(snapshot.Element(1, item, 0, HWND))  # clic simple = sélection
    assert outcome.ok


def test_save_as_filename_and_button_allowed():
    """« Enregistrer sous » : saisie du nom de fichier puis clic sur le bouton — aucun des deux n'est refusé."""
    dialog, item, filename, save = _file_dialog_tree()
    auto = FakeAuto()
    ex = _dialog_executor(auto, dialog, filename, save)
    typed = ex.write_element(snapshot.Element(1, filename, 0, HWND), "rapport.txt", replace=True)
    assert typed.ok and filename.element.live["value"] == "rapport.txt"
    clicked = ex.click_element(snapshot.Element(2, save, 0, HWND))
    assert clicked.ok


def test_apps_key_and_shift_f10_refused_in_file_list():
    dialog, item, _filename, _save = _file_dialog_tree()
    ex = _dialog_executor(FakeAuto(), dialog, item)
    for combo in ("apps", "shift+f10"):
        with pytest.raises(actions.ActionError, match="[Bb]oîte de fichiers"):
            ex.press(combo, "rapport.txt")


def test_plain_key_allowed_in_file_list():
    """Une touche ordinaire (flèche) sur la liste n'ouvre pas de menu : elle n'est pas refusée par ce garde-fou."""
    dialog, item, _filename, _save = _file_dialog_tree()
    ex = _dialog_executor(FakeAuto(), dialog, item)
    element = ex.resolve_element("rapport.txt", actions.TEXT_ROLES | actions.LEAF_ROLES, "élément")
    ex._refuse_shell_view(element, dangerous=False)  # ne lève pas : clic/flèche de sélection permis


def test_shell_context_menu_of_explorer_refused(monkeypatch):
    monkeypatch.setattr(actions.methods, "win32_menu_items", lambda hwnd: [])  # pas de barre de menu native
    menu = _win("Window", "#32768")
    entry = _chain(menu, node("MenuItem", "Supprimer", has_invoke=True))
    element = snapshot.Element(1, entry, 0, HWND)
    ex = _dialog_executor(FakeAuto(), menu, entry, process="explorer.exe")
    with pytest.raises(actions.ActionError, match="[Mm]enu contextuel"):
        ex.click_element(element, button="left")
    with pytest.raises(actions.ActionError, match="[Mm]enu contextuel"):
        ex.menu("Supprimer")


def test_shell_menu_above_a_file_dialog_refused():
    """Un menu contextuel #32768 appartenant au processus de l'application (pas l'Explorateur), mais posé au-dessus
    d'une boîte de fichiers ouverte, est refusé lui aussi."""
    dialog_root, _item, _f, _s = _file_dialog_tree()
    menu = _win("Window", "#32768")
    entry = _chain(menu, node("MenuItem", "Ouvrir dans un nouvel onglet", has_invoke=True))
    info = snapshot.WindowInfo(HWND, "Menu", "notepad.exe", "#32768", w.Rect(0, 0, 400, 300),
                              False, False, False, "Win32")  # fmt: skip
    view = snapshot.View(HWND, info, [HWND], [snapshot.Element(1, entry, 0, HWND)],
                         snapshot.frame_for(info.rect), framework="Win32", roots={HWND: dialog_root})  # fmt: skip
    ex = actions.Executor(FakeAuto(), HWND, lambda rebuild: view, lambda: None)
    ex.acted = False
    ex._pids = {7}
    ex._windows = {HWND}
    with pytest.raises(actions.ActionError, match="[Mm]enu contextuel"):
        ex.click_element(snapshot.Element(1, entry, 0, HWND), button="left")


def test_ordinary_app_menu_not_treated_as_shell():
    """Un menu déroulant #32768 d'une application ordinaire (sans Explorateur ni boîte de fichiers) reste actionnable."""
    menu = _win("Window", "#32768")
    entry = _chain(menu, node("MenuItem", "Enregistrer", has_invoke=True))
    ex = _dialog_executor(FakeAuto(), menu, entry, process="notepad.exe")
    in_list, shell_menu = ex._shell_surface(snapshot.Element(1, entry, 0, HWND))
    assert not in_list and not shell_menu
    assert ex.click_element(snapshot.Element(1, entry, 0, HWND)).ok
