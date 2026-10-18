"""Lot S de la contre-vérification finale (recheck6), révisé par l'accord « garde-fous » du 18/10/2026 :

- alias d'exécution WindowsApps : plus refusés par nom (débogueurs, gestionnaires de paquets, consoles…), le shell de
  Claude fait déjà tout ; seuls restent les commutateurs qui exposeraient une session de navigateur ;
- boîtes de fichiers et menus contextuels shell : clic droit, double-clic et entrées du menu shell sont permis
  (non-régression : plus jamais refusés).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nexus_windows import actions, policy, snapshot, uia
from nexus_windows import win32 as w

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contract import HWND, FakeAuto, as_auto, node  # noqa: E402

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


def test_every_alias_launches_without_debugging_switch():
    for alias in ALIASES:
        assert _accepts(str(alias)), alias.name


def test_alias_still_refuses_devtools_switches_of_its_browser(monkeypatch):
    monkeypatch.setattr(w, "appexec_target", lambda p: r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    assert not _accepts(r"C:\x\chrome-alias.exe", "--remote-debugging-port=9222")


def test_benign_windowsapps_aliases_still_launch(monkeypatch):
    """notepad/mspaint/SnippingTool via alias : cible graphique au nom anodin -> acceptés."""
    if not ALIASES:
        pytest.skip("aucun alias WindowsApps sur ce poste")
    for name in ("notepad.exe", "mspaint.exe", "SnippingTool.exe"):
        alias = WINDOWS_APPS / name
        if alias.exists():
            assert _accepts(str(alias)), f"{name} devrait rester lançable"


# =============================================================================================================
# Boîtes de fichiers et menus contextuels shell : libres
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
    ex = actions.Executor(as_auto(auto), HWND, lambda rebuild: view, lambda: None)
    ex.acted = False
    ex._pids = {7}
    ex._windows = {HWND}
    return ex


def test_file_dialog_double_click_allowed():
    dialog, item, _filename, _save = _file_dialog_tree()
    element = snapshot.Element(1, item, 0, HWND)
    ex = _dialog_executor(FakeAuto(), dialog, item)
    ex.click_element(element, button="left", count=2)  # double-clic dans la liste : plus refusé


def test_save_as_filename_and_button_allowed():
    """« Enregistrer sous » : saisie du nom de fichier puis clic sur le bouton — aucun des deux n'est refusé."""
    dialog, _item, filename, save = _file_dialog_tree()
    auto = FakeAuto()
    ex = _dialog_executor(auto, dialog, filename, save)
    typed = ex.write_element(snapshot.Element(1, filename, 0, HWND), "rapport.txt", replace=True)
    assert typed.ok and filename.element.live["value"] == "rapport.txt"
    clicked = ex.click_element(snapshot.Element(2, save, 0, HWND))
    assert clicked.ok


def test_shell_context_menu_entry_allowed(monkeypatch):
    monkeypatch.setattr(actions.methods, "win32_menu_items", lambda hwnd: [])  # pas de barre de menu native
    menu = _win("Window", "#32768")
    entry = _chain(menu, node("MenuItem", "Ouvrir avec", has_invoke=True))
    ex = _dialog_executor(FakeAuto(), menu, entry, process="explorer.exe")
    assert ex.click_element(snapshot.Element(1, entry, 0, HWND), button="left").ok
