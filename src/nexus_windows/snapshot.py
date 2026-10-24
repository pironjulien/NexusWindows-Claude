"""Instantané d'une fenêtre pour le modèle : liste d'éléments numérotés + image, et correspondance des coordonnées.

Coordonnées : toujours en pixels de l'image envoyée pour la fenêtre (liste seule : ceux de la dernière image montrée,
où les points "x,y" se résolvent). Les menus, listes déroulantes et boîtes de dialogue ouverts par l'application font
partie de l'instantané (fenêtres surgissantes du même processus).
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field

import comtypes
from PIL import Image, ImageDraw, ImageFont

from . import capture, cdp, ocr, policy, steam, uia
from . import win32 as w

# Règle image commune des produits Nexus (shared/images.md, 24/10) : côté long ≤ 1568 px et ≤ 1,15 Mpx (limite standard
# de Claude, aucune réduction de sa part ; GPT-5.x et Gemini 3 lisent la même), WebP sans perte, > 200 Ko → qualité 85
# puis 70, jamais > 500 Ko. detail="high" (2000 px auparavant) suit la même limite : pour lire un petit texte, region
# (zoom) plutôt qu'une grande image.
MAX_SIDE = {"normal": 1568, "high": 1568}
MAX_PIXELS = {"normal": 1_150_000, "high": 1_150_000}
LOSSLESS_BUDGET = 200 * 1024
IMAGE_LIMIT = 500 * 1024

ACTION_ROLES = {
    "Button",
    "CheckBox",
    "RadioButton",
    "ComboBox",
    "Edit",
    "Hyperlink",
    "ListItem",
    "MenuItem",
    "TabItem",
    "TreeItem",
    "Slider",
    "Spinner",
    "SplitButton",
    "DataItem",
    "Document",
    "HeaderItem",
    "Calendar",
}
CONTAINER_ROLES = {
    "List",
    "Tree",
    "Menu",
    "MenuBar",
    "Tab",
    "Table",
    "DataGrid",
    "ToolBar",
    "Group",
    "Pane",
    "StatusBar",
    "Header",
    "Window",
    "Custom",
    "AppBar",
    "SemanticZoom",
}
TEXT_ROLES = {"Text", "Image"}
SKIP_SUBTREE = {"TitleBar", "ScrollBar", "ToolTip"}
SKIP_ROLES = {"Thumb", "Separator"}

ROLE_ALIASES = {
    "bouton": {"Button", "SplitButton"},
    "button": {"Button", "SplitButton"},
    "case": {"CheckBox"},
    "checkbox": {"CheckBox"},
    "radio": {"RadioButton"},
    "champ": {"Edit", "Document", "ComboBox"},
    "edit": {"Edit"},
    "texte": {"Text", "Edit", "Document"},
    "text": {"Text"},
    "lien": {"Hyperlink"},
    "link": {"Hyperlink"},
    "menu": {"MenuItem", "Menu"},
    "menuitem": {"MenuItem"},
    "onglet": {"TabItem"},
    "tab": {"TabItem"},
    "liste": {"List", "ListItem", "ComboBox"},
    "list": {"List"},
    "listitem": {"ListItem", "DataItem"},
    "élément": {"ListItem", "TreeItem", "DataItem"},
    "option": {"ListItem", "RadioButton", "MenuItem"},
    "combo": {"ComboBox"},
    "combobox": {"ComboBox"},
    "arbre": {"TreeItem", "Tree"},
    "treeitem": {"TreeItem"},
    "document": {"Document"},
    "image": {"Image"},
    "groupe": {"Group"},
    "group": {"Group"},
    "curseur": {"Slider"},
    "slider": {"Slider"},
    "cellule": {"DataItem"},
}

FRAMEWORK_BY_CLASS = [
    ("Chrome_WidgetWin_", "Chromium"),
    ("ApplicationFrameWindow", "UWP"),
    ("Windows.UI.Core.CoreWindow", "UWP"),
    ("WinUIDesktopWin32WindowClass", "WinUI"),
    ("HwndWrapper", "WPF"),
    ("WindowsForms10", "WinForms"),
    ("Qt5", "Qt"),
    ("Qt6", "Qt"),
    ("QWidget", "Qt"),
    ("SunAwt", "Java"),
    ("TkTopLevel", "Tk"),
    ("SALFRAME", "LibreOffice"),
    ("CabinetWClass", "Explorer"),
    ("UnityWndClass", "Unity"),
    ("UnrealWindow", "Unreal"),
    ("SDL_app", "SDL"),
    ("GLFW30", "GLFW"),
    ("ConsoleWindowClass", "Console"),
    ("CASCADIA_HOSTING_WINDOW_CLASS", "Terminal"),
    ("Notepad", "WinUI"),
    ("MozillaWindowClass", "Firefox"),
    ("OpusApp", "Word"),
    ("XLMAIN", "Excel"),
    ("rctrl_renwnd32", "Outlook"),
    ("PPTFrameClass", "PowerPoint"),
]
FRAMEWORK_BY_UIA = {
    "win32": "Win32",
    "winform": "WinForms",
    "wpf": "WPF",
    "xaml": "XAML",
    "directui": "DirectUI",
    "chrome": "Chromium",
    "qt": "Qt",
}


def norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- Fenêtres -------------------------------------------------------------------------------------------------
@dataclass
class WindowInfo:
    hwnd: int
    title: str
    process: str
    cls: str
    rect: w.Rect
    minimized: bool
    maximized: bool
    foreground: bool
    framework: str
    hidden: bool = False  # application Store jamais affichée par Windows (lancée sans activation), pilotable quand même
    surface: str = ""  # surface de Windows (« taskbar », « desktop »…) plutôt qu'une application

    def label(self) -> str:
        return f'"{clip(self.title, 80)}" ({self.process}, {self.framework})'


CEF_BROWSER_CLASS = "CefBrowserWindow"
# Cadres qui hébergent souvent un navigateur embarqué CEF : Steam (SDL_app autour de CefBrowserWindow), lanceurs…
HOST_FRAMEWORKS = {"Win32", "SDL", "GLFW", "Qt"}


def hosts_chromium(hwnd: int) -> bool:
    """La fenêtre est-elle le cadre d'un navigateur CEF embarqué (Steam, lanceurs de jeux…) ? Seulement CEF : une
    application native qui loge un simple contrôle WebView2 garde sa famille (ses contrôles natifs restent natifs)."""
    return any(w.class_name(child) == CEF_BROWSER_CLASS for child in w.enum_children(hwnd))


def framework_of(hwnd: int, cls: str | None = None) -> str:
    """Famille d'interface par la classe de fenêtre ; un cadre hôte (SDL, Win32…) qui contient une zone de rendu
    Chromium est du Chromium (CEF) : occlusion, accessibilité à la demande et techniques sont celles de Chromium."""
    cls = cls if cls is not None else w.class_name(hwnd)
    name = next((name for prefix, name in FRAMEWORK_BY_CLASS if cls.startswith(prefix)), "Win32")
    if name in HOST_FRAMEWORKS and hosts_chromium(hwnd):
        return "Chromium"
    return name


def window_info(hwnd: int) -> WindowInfo:
    cls = w.class_name(hwnd)
    return WindowInfo(
        hwnd=hwnd,
        title=w.window_text(hwnd),
        process=w.process_name(w.window_pid(hwnd)),
        cls=cls,
        rect=w.frame_rect(hwnd),
        minimized=bool(w.IsIconic(hwnd)),
        maximized=bool(w.IsZoomed(hwnd)),
        foreground=w.foreground() == hwnd,
        framework=framework_of(hwnd, cls),
    )


def is_app_window(hwnd: int) -> bool:
    """Fenêtres qu'Alt+Tab montrerait : visibles, non masquées, avec titre, pas des outils."""
    if not w.IsWindowVisible(hwnd) or w.is_cloaked(hwnd):
        return False
    ex = w.ex_style(hwnd)
    if ex & w.WS_EX_TOOLWINDOW and not ex & w.WS_EX_APPWINDOW:
        return False
    if w.owner(hwnd) and not ex & w.WS_EX_APPWINDOW:
        return False
    if not w.window_text(hwnd):
        return False
    rect = w.frame_rect(hwnd)
    return w.IsIconic(hwnd) or (rect.width > 1 and rect.height > 1)


UWP_CORE_CLASS = "Windows.UI.Core.CoreWindow"
UWP_FRAME_CLASS = "ApplicationFrameWindow"
# Processus du shell dont les CoreWindow masquées sont des panneaux de Windows (Démarrer, notifications, clavier
# tactile…), jamais des applications : ils ne sont listés que comme surfaces, ouverts.
SHELL_PROCESSES = {
    "textinputhost.exe", "searchhost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe", "shellhost.exe",
    "lockapp.exe", "explorer.exe", "tabtip.exe", "widgets.exe", "screenclippinghost.exe",
}  # fmt: skip


def is_hidden_store_window(hwnd: int) -> bool:
    """Application Store lancée sans activation : Windows ne l'affiche jamais (cadre et CoreWindow masqués par le
    shell), mais sa CoreWindow de premier niveau se lit et se pilote en arrière-plan (relevé le 20/10 sur Paramètres :
    contenu complet, navigation de page en page, Julien ne voit rien)."""
    if w.class_name(hwnd) != UWP_CORE_CLASS or not w.IsWindowVisible(hwnd) or not w.is_cloaked(hwnd):
        return False
    title = w.window_text(hwnd)
    if not title or title == "DesktopWindowXamlSource":
        return False
    return w.process_name(w.window_pid(hwnd)).lower() not in SHELL_PROCESSES


def _minimized_frame(hwnd: int) -> bool:
    """Cadre UWP réduit : Windows détache alors la CoreWindow de l'application et la masque (relevé le 20/10 sur la
    Calculatrice, relecture d'un agent neuf), exactement comme pour une application jamais affichée."""
    return w.class_name(hwnd) == UWP_FRAME_CLASS and bool(w.IsIconic(hwnd)) and is_app_window(hwnd)


def _empty_frame_of(hwnd: int, hidden_titles: set[str]) -> bool:
    """Cadre UWP sans contenu dont l'application est la CoreWindow masquée de même titre (listée à sa place)."""
    if w.class_name(hwnd) != UWP_FRAME_CLASS or w.window_text(hwnd) not in hidden_titles or w.IsIconic(hwnd):
        return False
    host = w.window_pid(hwnd)
    return not any(w.window_pid(child) != host for child in w.enum_children(hwnd))


# Surfaces de Windows lui-même, nommées (français et anglais) : barre des tâches et bureau, toujours là.
SURFACES = {
    "taskbar": ("Shell_TrayWnd", "Taskbar"),
    "barre des tâches": ("Shell_TrayWnd", "Taskbar"),
    "desktop": ("Progman", "Desktop"),
    "bureau": ("Progman", "Desktop"),
}


# Panneaux de Windows qui ne vivent qu'au premier plan (refermés dès qu'on rend la main) : raccourci qui les ouvre,
# processus et classe de leur fenêtre (relevés le 20/10, Windows 11 26H2).
FLYOUTS = {
    "quick settings": ("win+a", "shellhost.exe", "ControlCenterWindow"),
    "réglages rapides": ("win+a", "shellhost.exe", "ControlCenterWindow"),
    "notifications": ("win+n", "shellexperiencehost.exe", UWP_CORE_CLASS),
    "notification center": ("win+n", "shellexperiencehost.exe", UWP_CORE_CLASS),
    "centre de notifications": ("win+n", "shellexperiencehost.exe", UWP_CORE_CLASS),
    # Démarrer : contenu (épinglés, recommandations) dans StartMenuExperienceHost ; la recherche, au premier plan,
    # est une autre fenêtre (SearchHost) qui n'en montre que la zone de saisie.
    "start": ("win", "startmenuexperiencehost.exe", UWP_CORE_CLASS),
    "démarrer": ("win", "startmenuexperiencehost.exe", UWP_CORE_CLASS),
}


# Processus des panneaux (et de la recherche qui tient le premier plan pour Démarrer).
PANEL_PROCESSES = {process for _, process, _ in FLYOUTS.values()} | {"searchhost.exe"}


PANEL_MIN_HEIGHT = 400  # panneaux ouverts : toute la hauteur (≥ 1 000 px relevés) ; une notification éphémère : ~100 px


def flyout_window(process: str, cls: str) -> int:
    """Fenêtre ouverte d'un panneau de Windows (affichée, non masquée), bandes du shell comprises ; 0 sinon. Une
    notification éphémère (« Nouvelle notification », même processus et même classe que le centre de notifications :
    relecture tour 3) n'est jamais prise pour le panneau : trop basse."""
    for hwnd in w.all_top_windows():
        if w.class_name(hwnd) != cls or not w.IsWindowVisible(hwnd) or w.is_cloaked(hwnd):
            continue
        if w.process_name(w.window_pid(hwnd)).lower() == process and w.frame_rect(hwnd).height >= PANEL_MIN_HEIGHT:
            return hwnd
    return 0


def panel_processes(process: str) -> set[str]:
    """Processus des fenêtres d'UN panneau : le sien, plus la recherche qui tient le premier plan pour Démarrer."""
    return {process, "searchhost.exe"} if process == "startmenuexperiencehost.exe" else {process}


def surface_window(name: str) -> int:
    """Fenêtre d'une surface nommée de Windows (« taskbar », « desktop »…), 0 si inconnue ou absente."""
    entry = SURFACES.get(name.strip().lower())
    return int(w.FindWindowW(entry[0], None) or 0) if entry else 0


def shell_surfaces() -> list[WindowInfo]:
    """Barre des tâches et bureau, nommés (surface)."""
    out: list[WindowInfo] = []
    for cls, label in dict(SURFACES.values()).items():
        hwnd = int(w.FindWindowW(cls, None) or 0)
        if hwnd:
            info = window_info(hwnd)
            info.surface = label
            out.append(info)
    return out


def app_windows() -> list[WindowInfo]:
    """Fenêtres d'application (ordre Z), applications Store cachées comprises (marquées hidden)."""
    handles = w.enum_windows()
    minimized = {w.window_text(h) for h in handles if _minimized_frame(h)}
    # Application réduite : son cadre est listé [minimized], sa CoreWindow détachée n'est pas une appli « cachée ».
    hidden = {h for h in handles if is_hidden_store_window(h) and w.window_text(h) not in minimized}
    titles = {w.window_text(h) for h in hidden}
    out: list[WindowInfo] = []
    for h in handles:
        if h in hidden:
            info = window_info(h)
            info.hidden = True
            out.append(info)
        elif is_app_window(h) and not (titles and _empty_frame_of(h, titles)):
            out.append(window_info(h))
    return out


SHELL_CLASSES = {
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland", "Shell_InputSwitchTopLevelWindow",
}  # fmt: skip
MENU_CLASSES = {"#32768", "ComboLBox", "DropDown"}


def popups_of(hwnd: int) -> list[int]:
    """Fenêtres surgissantes visibles de la cible au-dessus d'elle (menus, listes, dialogues) : possédées par elle,
    ou créées par SON fil (menus, listes déroulantes, surgissantes WPF/Chromium sans propriétaire). Jamais une
    fenêtre possédée par une autre fenêtre (une fenêtre de l'utilisateur du même programme), ni la barre des tâches ou le
    bureau (même processus que l'Explorateur)."""
    pid = w.window_pid(hwnd)
    thread = w.window_thread(hwnd)
    out: list[int] = []
    for other in w.enum_windows():  # du haut vers le bas de l'empilement
        if other == hwnd:
            break
        if w.window_pid(other) != pid or not w.IsWindowVisible(other) or w.is_cloaked(other):
            continue
        if w.class_name(other) in SHELL_CLASSES:
            continue
        rect = w.frame_rect(other)
        if rect.width < 4 or rect.height < 4:
            continue
        owners: list[int] = []
        cursor = w.owner(other)
        for _ in range(8):
            if not cursor:
                break
            owners.append(cursor)
            cursor = w.owner(cursor)
        if hwnd in owners:
            out.append(other)
            continue
        if owners or w.window_thread(other) != thread:
            continue  # possédée par une autre fenêtre, ou d'un autre fil (autre fenêtre de l'Explorateur…)
        popup_style = bool(w.style(other) & w.WS_POPUP) and not w.window_text(other)
        if popup_style or w.class_name(other) in MENU_CLASSES:
            out.append(other)
    out.reverse()  # du bas vers le haut, pour coller les images dans l'ordre
    return out


def restore_behind(hwnd: int) -> bool:
    """Fenêtre réduite → restaurée sans activation, tout au fond de l'empilement (L'utilisateur ne la voit pas surgir)."""
    if not w.IsIconic(hwnd):
        return False
    placement = w.WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(w.WINDOWPLACEMENT)
    w.GetWindowPlacement(hwnd, ctypes.byref(placement))
    placement.showCmd = w.SW_SHOWNOACTIVATE
    w.SetWindowPlacement(hwnd, ctypes.byref(placement))
    w.SetWindowPos(hwnd, w.HWND_BOTTOM, 0, 0, 0, 0, w.SWP_NOMOVE | w.SWP_NOSIZE | w.SWP_NOACTIVATE)
    deadline = time.monotonic() + 1.0
    while w.IsIconic(hwnd) and time.monotonic() < deadline:
        time.sleep(0.03)
    time.sleep(0.15)  # laisser l'application se redessiner
    return not w.IsIconic(hwnd)


# --- Correspondance image ↔ écran ---------------------------------------------------------------------------
@dataclass
class Frame:
    origin: tuple[int, int]  # position écran (pixels physiques) du pixel (0, 0) de l'image
    size: tuple[int, int]  # taille physique couverte
    scale: float  # pixels image par pixel physique
    anchor: tuple[int, int] | None = None  # coin de la fenêtre principale à la capture
    anchor_size: tuple[int, int] | None = None  # taille de la fenêtre principale à la capture

    def resized(self, current: w.Rect) -> bool:
        """La fenêtre a-t-elle changé de taille depuis l'image ? (les points de l'image n'y correspondent plus)"""
        return self.anchor_size is not None and (current.width, current.height) != self.anchor_size

    @property
    def image_size(self) -> tuple[int, int]:
        return max(1, math.floor(self.size[0] * self.scale)), max(1, math.floor(self.size[1] * self.scale))

    def _shift(self, current: w.Rect | None) -> tuple[int, int]:
        """Déplacement de la fenêtre depuis la capture : un point de l'image reste attaché à la fenêtre."""
        if current is None or self.anchor is None:
            return 0, 0
        return current.left - self.anchor[0], current.top - self.anchor[1]

    def to_image(self, x: int, y: int, current: w.Rect | None = None) -> tuple[int, int]:
        dx, dy = self._shift(current)
        return (
            math.floor((x - dx - self.origin[0]) * self.scale),
            math.floor((y - dy - self.origin[1]) * self.scale),
        )

    def to_screen(self, x: float, y: float, current: w.Rect | None = None) -> tuple[int, int]:
        """Pixel image → centre du pixel écran correspondant (fenêtre déplacée depuis : suivie)."""
        dx, dy = self._shift(current)
        return (
            self.origin[0] + dx + math.floor((x + 0.5) / self.scale),
            self.origin[1] + dy + math.floor((y + 0.5) / self.scale),
        )

    def contains_image_point(self, x: float, y: float) -> bool:
        width, height = self.image_size
        return 0 <= x < width and 0 <= y < height


def frame_for(
    bounds: w.Rect,
    detail: str = "normal",
    anchor: tuple[int, int] | None = None,
    anchor_size: tuple[int, int] | None = None,
) -> Frame:
    side = MAX_SIDE.get(detail, MAX_SIDE["normal"])
    pixels = MAX_PIXELS.get(detail, MAX_PIXELS["normal"])
    width, height = max(bounds.width, 1), max(bounds.height, 1)
    scale = min(1.0, side / max(width, height), math.sqrt(pixels / (width * height)))
    return Frame((bounds.left, bounds.top), (bounds.width, bounds.height), scale, anchor, anchor_size)


# --- Éléments -----------------------------------------------------------------------------------------------
@dataclass
class Element:
    id: int
    node: uia.Node
    depth: int
    window: int

    @property
    def role(self) -> str:
        return self.node.control_type

    @property
    def name(self) -> str:
        return norm(self.node.name)

    @property
    def rect(self) -> w.Rect | None:
        return self.node.rect

    @property
    def props(self) -> dict:
        return self.node.props

    @property
    def actionable(self) -> bool:
        p = self.props
        return self.role in ACTION_ROLES or any(
            p.get(k) for k in ("has_invoke", "has_toggle", "has_selection_item", "has_expand", "has_value", "has_range")
        )

    def describe(self) -> str:
        label = f'{self.role} "{clip(self.name, 50)}"' if self.name else self.role
        return f"[{self.id}] {label}" if self.id else label  # élément temporaire (sous un point) : pas de numéro


@dataclass
class View:
    hwnd: int
    info: WindowInfo
    windows: list[int]
    elements: list[Element]
    frame: Frame
    taken: float = field(default_factory=time.monotonic)
    hidden: int = 0
    node_count: int = 0
    framework: str = ""
    detail: str = "normal"
    notes: list[str] = field(default_factory=list)
    displayed: dict[int, tuple] = field(default_factory=dict)  # [N] réellement montrés → identité à l'affichage
    issued: int = 0  # plus grand [N] jamais donné dans cette fenêtre (rapports de changements successifs compris)
    ocr_done: bool = False  # texte de l'image déjà lu par OCR et ajouté aux éléments
    frozen: dict[int, dict] = field(default_factory=dict)  # [N] montrés → leurs propriétés à l'affichage (copie)
    # Lecture incomplète : un texte absent de l'arbre n'y est pas prouvé absent (ni présent ni absent).
    truncated: bool = False  # arbre coupé à max_nodes
    web_missing: bool = False  # Chromium n'a pas publié le contenu de la page
    unreadable: bool = False  # arbre de la fenêtre principale illisible (seules ses surgissantes ont été lues)
    stale: bool = False  # page Chromium recouverte dont la publication n'a pu être déclenchée : arbre peut-être périmé
    roots: dict[int, uia.Node] = field(default_factory=dict)  # fenêtre → racine de son arbre (nœuds non listés compris)

    @property
    def by_id(self) -> dict[int, Element]:
        return {e.id: e for e in self.elements}


def identity(props: dict) -> tuple:
    """Ce qui fait qu'un élément est « le même » pour le modèle : rôle, nom, identifiant d'automatisation."""
    return props.get("control_type"), norm(props.get("name")), str(props.get("automation_id") or "")


def _visible(node: uia.Node, bounds: w.Rect) -> bool:
    rect = node.rect
    if rect is None or node.props.get("offscreen"):
        return False
    return rect.intersect(bounds) is not None


def _listable(node: uia.Node, parent_name: str) -> bool:
    role = node.control_type
    if role in SKIP_ROLES:
        return False
    name = norm(node.name)
    p = node.props
    if role in ACTION_ROLES or any(
        p.get(k) for k in ("has_invoke", "has_toggle", "has_selection_item", "has_value", "has_range")
    ):
        return True
    if p.get("has_expand") and role not in ("Pane", "Window"):
        return True
    if role in TEXT_ROLES:
        return bool(name) and name != parent_name
    if role in CONTAINER_ROLES:
        return bool(name) and role != "Window" and name != parent_name
    return bool(name) and p.get("focusable", False)


def build_view(auto: uia.Automation, hwnd: int, detail: str = "normal", max_nodes: int = 6000) -> View:
    info = window_info(hwnd)
    windows = [hwnd] + popups_of(hwnd)
    bounds = info.rect
    for other in windows[1:]:
        r = w.frame_rect(other)
        bounds = w.Rect(
            min(bounds.left, r.left), min(bounds.top, r.top), max(bounds.right, r.right), max(bounds.bottom, r.bottom)
        )
    frame = frame_for(bounds, detail, (info.rect.left, info.rect.top), (info.rect.width, info.rect.height))
    elements: list[Element] = []
    node_count = 0
    root_framework = ""
    notes: list[str] = []
    truncated = web_missing = unreadable = stale = False
    roots: dict[int, uia.Node] = {}
    popups = set(windows[1:])
    chromium = info.framework == "Chromium"
    if w.IsWindow(hwnd) and not w.responsive(hwnd):
        notes.append(
            "Application not responding (frozen or suspended): image from the screen composition, list may be empty"
        )
        windows = [hwnd]  # ni lecture des surgissantes ni rafraîchissement web : chaque appel attendrait son délai
        chromium = False
    if chromium:
        flush_web(auto, hwnd)  # documents de la lecture précédente ; oubliés s'ils ont disparu
    for win in windows:
        try:
            tree = auto.tree(win, max_nodes=max_nodes)
            if win == hwnd and chromium:
                known = hwnd in _ROOT_DOCUMENTS
                remember_web_documents(hwnd, tree)
                # Première lecture (ou document remplacé), fenêtre recouverte : publication déclenchée puis relue.
                flushed = None if known else flush_web(auto, hwnd)
                if flushed:
                    tree = auto.tree(win, max_nodes=max_nodes)
                    remember_web_documents(hwnd, tree)
                elif flushed is False:
                    stale = True
                    notes.append(
                        "UI Automation tree may be stale (Chromium window covered: its publication could not be "
                        "triggered): rely on the image"
                    )
            if win == hwnd and chromium and not _has_web_content(tree):
                tree, ready = _wait_web_content(auto, win, tree, max_nodes)
                if not ready:
                    web_missing = True
                    notes.append(
                        "Web content missing from the UI Automation tree (Chromium stops publishing it while its window "
                        "is covered): rely on the image and clicks by coordinates; " + covered_remedy(hwnd)
                    )
        except uia.UIAError as error:
            if win == hwnd:
                unreadable = True
                notes.append(f"UI Automation tree of the window unreadable ({str(error)[:120]}): rely on the image")
            continue
        truncated = truncated or tree.truncated
        roots[win] = tree
        if win == hwnd:
            root_framework = FRAMEWORK_BY_UIA.get(str(tree.props.get("framework", "")).lower(), "")
        win_bounds = w.frame_rect(win)

        def visit(node: uia.Node, depth: int, parent_name: str) -> None:
            nonlocal node_count
            node_count += 1
            role = node.control_type
            if role in SKIP_SUBTREE:
                return
            if node is not tree and node.hwnd and node.hwnd != win and node.hwnd in popups:
                return  # surgissante publiée aussi sous la fenêtre principale (CEF/Steam) : lue avec sa fenêtre
            listed = node is not tree and _visible(node, win_bounds) and _listable(node, parent_name)
            if listed:
                elements.append(Element(len(elements) + 1, node, depth, win))
            next_name = norm(node.name) if listed else parent_name
            for child in node.children:
                visit(child, depth + (1 if listed else 0), next_name)

        visit(tree, 0, norm(tree.name))
    framework = info.framework if info.framework != "Win32" or not root_framework else root_framework
    return View(
        hwnd, info, windows, elements, frame, node_count=node_count, framework=framework, detail=detail, notes=notes,
        truncated=truncated, web_missing=web_missing, roots=roots, unreadable=unreadable, stale=stale,
    )  # fmt: skip


def is_web_document(node: uia.Node) -> bool:
    """Racine d'une page web Chromium/Electron (le DOM y porte l'ordre z de ce qui est dessiné)."""
    p = node.props
    return (
        node.control_type == "Document"
        and str(p.get("framework") or "").lower() == "chrome"
        and str(p.get("automation_id") or "").lower() == "rootwebarea"
    )


def root_web_documents(tree: uia.Node) -> list[uia.Node]:
    """Documents web de premier niveau d'un arbre (cadres internes exclus : leur document n'est pas la zone
    d'affichage, le faire « défiler en vue » ferait défiler la page qui le contient)."""
    out: list[uia.Node] = []

    def visit(node: uia.Node) -> None:
        if is_web_document(node):
            out.append(node)
            return
        for child in node.children:
            visit(child)

    visit(tree)
    return out


# Publication différée de l'arbre d'accessibilité Chromium (mesuré le 2026-10-14, Electron du labo sans commutateur) :
# une fenêtre recouverte qui a eu le premier plan une fois, puis l'a perdu (activation brève d'une frappe), passe
# « occluded » : document.visibilityState « hidden », plus aucune image, et le moteur de rendu ne sérialise plus les
# changements du DOM pour l'accessibilité. Ils restent en attente jusqu'à la prochaine action d'accessibilité reçue
# par le moteur de rendu (Invoke, Select…) : l'arbre UIA est alors en retard d'un changement. Les lectures UIA seules
# (propriétés, TextPattern, test de position) ne débloquent rien. Remède sans premier plan : ScrollIntoView du
# Document racine. Chromium traite toute action en publiant d'abord ce qui attend, et faire défiler la zone
# d'affichage « en vue » ne déplace rien (page défilée à 0, 120 et 700 px : position, focus et sélection intacts).
# Publication relue en ~15 ms. Fait avant toute lecture d'une fenêtre Chromium qui n'a pas le premier plan (au
# premier plan elle est dessinée : rien n'attend).
_ROOT_DOCUMENTS: dict[int, list] = {}  # hwnd → éléments UIA des documents web de premier niveau (dernière lecture)


def remember_web_documents(hwnd: int, tree: uia.Node) -> None:
    documents = [n.element for n in root_web_documents(tree)]
    if documents:
        _ROOT_DOCUMENTS[hwnd] = documents
    else:
        _ROOT_DOCUMENTS.pop(hwnd, None)


def flush_web(auto: uia.Automation, hwnd: int) -> bool | None:
    """Fait publier à Chromium les changements d'accessibilité en attente (fenêtre recouverte). None : rien à faire
    (fenêtre au premier plan, aucun document connu) ; False : impossible (document disparu, motif absent) — l'arbre
    peut alors être en retard."""
    documents = _ROOT_DOCUMENTS.get(hwnd)
    if not documents or w.root(w.foreground()) == hwnd:
        return None
    done = True
    for element in documents:
        try:
            auto.scroll_into_view(element)
        except uia.UIAError, comtypes.COMError:  # document remplacé (navigation), motif absent
            done = False
    if not done:
        _ROOT_DOCUMENTS.pop(hwnd, None)
    return done


# Commutateur ajouté par launch à toute application Chromium/Electron qu'il démarre (launch.OCCLUSION_FEATURES).
OCCLUSION_FEATURE = "CalculateNativeWinOcclusion"


def occlusion_disabled(pid: int) -> bool | None:
    """Le processus a-t-il été démarré avec le suivi d'occlusion de Chromium coupé ? Ligne lue comme Chromium la
    lit : commutateurs jusqu'au terminateur « -- » ou « --single-argument » seulement, dernier --disable-features retenu, nom de fonctionnalité
    exact (sensible à la casse). None : ligne de commande illisible."""
    line = cdp.process_command_line(pid)
    if line is None:
        return None
    last = None
    for token in policy.split_args(line):
        if token == "--" or policy.switch_key(token) == "single-argument":
            break  # la suite n'est faite que d'arguments
        if policy.switch_key(token) == "disable-features":
            last = token.partition("=")[2]
    return last is not None and OCCLUSION_FEATURE in (n.strip() for n in last.split(","))


_WS_EX_SEE_THROUGH = 0x20 | 0x80000  # WS_EX_TRANSPARENT, WS_EX_LAYERED : calques qui ne cachent pas ce qui est dessous


def covered_by_window(hwnd: int) -> bool:
    """Une fenêtre visible, au-dessus dans l'ordre z, couvre-t-elle à elle seule tout le cadre (Claude agrandi le
    24/10) ? Recouvrement par plusieurs fenêtres ou par un calque : non prouvé, False."""
    rect = w.frame_rect(hwnd)
    for other in w.enum_windows():
        if other == hwnd:
            return False
        if not w.IsWindowVisible(other) or w.is_cloaked(other) or w.IsIconic(other):
            continue
        if w.ex_style(other) & _WS_EX_SEE_THROUGH:
            continue
        r = w.frame_rect(other)
        if r.left <= rect.left and r.top <= rect.top and r.right >= rect.right and r.bottom >= rect.bottom:
            return True
    return False


def covered_remedy(hwnd: int) -> str:
    """Remède, dit au modèle, à une fenêtre Chromium recouverte qui ne se dessine ni ne publie son contenu : selon la
    ligne de commande réelle de son processus, jamais supposée (le 2026-10-24, Logi Options+ lancé par son raccourci
    du menu Démarrer n'avait pas les commutateurs, et la note affirmait le contraire)."""
    if steam.is_steam_window(hwnd):
        return (
            'Steam: launch(app="Steam", restart=true), with the user\'s consent, restarts it with '
            "-cef-disable-occlusion -cef-force-accessibility."
        )
    disabled = occlusion_disabled(w.window_pid(hwnd))
    if disabled:
        return (
            "its process has launch's switches (kept drawn when covered): the plain image is its real content or it "
            "is still loading; look again."
        )
    if disabled is None:
        return (
            "its command line is unreadable (switches unknown): look again; if nothing changes, "
            "act(foreground=true) with the user's consent."
        )
    return (
        "this window was not started by launch with the switches that keep it drawn when covered (opened by the user "
        "or by a shortcut with its own arguments): if the user agrees to close it, close it and launch it again with "
        "launch (executable or Start menu name); otherwise act(foreground=true), with the user's consent."
    )


def _has_web_content(tree: uia.Node) -> bool:
    return any(node.control_type == "Document" and node.children for node in tree.walk())


_NO_WEB_CONTENT: dict[int, float] = {}  # hwnd → instant du dernier constat « contenu web absent »


def _wait_web_content(auto: uia.Automation, hwnd: int, tree: uia.Node, max_nodes: int) -> tuple[uia.Node, bool]:
    """Chromium construit son arbre d'accessibilité à la première demande : on lui laisse jusqu'à 1,5 s
    (une seule fois par minute et par fenêtre, pour ne pas ralentir chaque look d'un Chrome recouvert)."""
    if time.monotonic() - _NO_WEB_CONTENT.get(hwnd, -1e9) < 60:
        return tree, False
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        time.sleep(0.25)
        tree = auto.tree(hwnd, max_nodes=max_nodes)
        if _has_web_content(tree):
            _NO_WEB_CONTENT.pop(hwnd, None)
            return tree, True
    _NO_WEB_CONTENT[hwnd] = time.monotonic()
    return tree, False


# --- Rendu texte ----------------------------------------------------------------------------------------------
def flags(e: Element) -> str:
    p = e.props
    if p.get("ocr"):
        return " (OCR)"  # lu dans l'image : ni valeur ni état, position seulement
    out = []
    value = p.get("value")
    if value is None and e.role in ("Slider", "Spinner", "ProgressBar"):
        value = p.get("range_value")
    if value is not None and str(value) != "" and norm(str(value)) != e.name:
        shown = "••••" if p.get("password") else clip(norm(str(value)), 60)
        out.append(f' = "{shown}"')
    toggle = p.get("toggle") if p.get("has_toggle") else None
    if toggle is not None and e.role in ("CheckBox", "RadioButton", "Button", "MenuItem", "ListItem", "TreeItem"):
        out.append({0: " [unchecked]", 1: " [checked]", 2: " [mixed]"}.get(toggle, ""))
    expand = p.get("expand")
    if (
        expand in (0, 1)
        and p.get("has_expand")
        and e.role in ("ComboBox", "MenuItem", "TreeItem", "SplitButton", "Button", "Group", "DataItem", "ListItem")
    ):
        out.append(" [collapsed]" if expand == 0 else " [expanded]")
    if p.get("selected"):
        out.append(" [selected]")
    if p.get("focused"):
        out.append(" [focus]")
    if p.get("enabled") is False:
        out.append(" [disabled]")
    if p.get("readonly") and e.role in ("Edit", "Document", "ComboBox"):
        out.append(" [read-only]")
    return "".join(out)


def listed(view: View, query: str | None = None, max_elements: int = 200) -> tuple[list[Element], int]:
    """Éléments réellement listés (filtre query, au plus max_elements) et nombre d'éléments cachés."""
    picked = view.elements
    if query:
        q = query.lower()
        picked = [
            e
            for e in picked
            if q in e.name.lower() or q in str(e.props.get("value") or "").lower() or q in e.role.lower()
        ]
    hidden = max(0, len(picked) - max_elements)
    if hidden:
        actionable = [e for e in picked if e.actionable]
        others = [e for e in picked if not e.actionable]
        keep = set(id(e) for e in (actionable + others)[:max_elements])
        picked = [e for e in picked if id(e) in keep]
    return picked, hidden


def lines(
    view: View, query: str | None = None, max_elements: int = 200, frame: Frame | None = None, coords: bool = True
) -> tuple[list[str], int]:
    """Lignes « [N] Rôle « nom » = valeur [états] @x,y », filtrées par query, au plus max_elements.

    frame : cadre des coordonnées, celui où les points "x,y" du modèle se résolvent (par défaut celui de la vue) ;
    la fenêtre a pu bouger depuis (suivie). coords=False : aucune coordonnée (pas de cadre valable)."""
    picked, hidden = listed(view, query, max_elements)
    frame = frame or view.frame
    out: list[str] = []
    current_window = view.hwnd
    for e in picked:
        if e.window != current_window:
            current_window = e.window
            title = w.window_text(e.window)
            out.append(f'— popup window "{clip(title, 60)}" —' if title else "— popup menu / list —")
        indent = "  " * min(e.depth, 8) if not query else ""
        out.append(indent + line_of(e, view, frame, coords))
    return out, hidden


def line_of(e: Element, view: View, frame: Frame, coords: bool = True) -> str:
    """Une ligne « [N] Rôle « nom » = valeur [états] @x,y » (sans retrait)."""
    rect = e.rect
    where = ""
    if rect is not None and coords:
        cx, cy = frame.to_image(*rect.center, view.info.rect)
        if not frame.contains_image_point(cx, cy):
            where = " (outside the image)"
        else:
            where = f" @{cx},{cy}"
            if e.role in CONTAINER_ROLES or e.role in ("Document", "List", "Tree", "Table", "DataGrid"):
                where += f" {round(rect.width * frame.scale)}×{round(rect.height * frame.scale)}"
    name = f' "{clip(e.name, 80)}"' if e.name else ""
    return f"[{e.id}] {e.role}{name}{flags(e)}{where}"


# --- Changements depuis la dernière liste montrée -------------------------------------------------------------
DIFF_FULL = 0.6  # part d'éléments différents au-delà de laquelle la liste complète est rendue (comme nexus-android)
MOVED_PX = 10  # déplacement du centre (pixels d'écran) au-delà duquel un élément est dit déplacé


@dataclass
class Changes:
    """Vue actuelle comparée à la dernière liste montrée. Les éléments retrouvés ont repris leur [N] (renumérotés
    dans la vue), les nouveaux ont un numéro neuf, jamais déjà donné dans cette fenêtre."""

    added: list[Element]
    removed: list[Element]  # éléments de la liste précédente (leur ancien [N])
    changed: list[tuple[Element, list[str]]]
    unchanged: int
    ratio: float  # part d'éléments différents (0 : rien n'a changé, 1 : tout)
    known: set[int]  # [N] valables pour le modèle après ce rapport (retrouvés et ajoutés)
    shift: tuple[int, int] | None = None  # translation commune (défilement) dans la fenêtre, en pixels d'écran
    shifted: list[int] = field(default_factory=list)  # [N] déplacés de cette seule translation (non dits changés)


def diff_view(previous: View, current: View) -> Changes:
    """Rapproche `current` de la liste montrée de `previous` (seuls ses [N] montrés comptent) : même identifiant
    d'exécution UIA d'abord, puis même rôle, nom et identifiant d'automatisation dans l'ordre d'apparition. Modifie
    les numéros de `current` en place ; à appeler sur une vue neuve, avant de la montrer."""
    # Éléments tels que le modèle les a vus : propriétés figées à l'affichage (les actions relisent les nœuds en
    # place ; sans copie figée, une cible revalidée changerait la base de comparaison).
    shown = [
        Element(
            e.id, uia.Node(e.node.element, e.node.depth, dict(previous.frozen.get(e.id, e.props))), e.depth, e.window
        )
        for e in previous.elements
        if e.id in previous.displayed
    ]
    match: dict[int, Element] = {}  # id(élément neuf) → élément ancien
    used: set[int] = set()
    by_rid: dict[tuple, Element] = {}
    for old in shown:
        if rid := uia.runtime_id(old.props):
            by_rid.setdefault(rid, old)
    for e in current.elements:
        old = by_rid.get(uia.runtime_id(e.props) or ())
        if old is not None and old.role == e.role and old.id not in used:
            match[id(e)] = old
            used.add(old.id)
    groups: dict[tuple, list[Element]] = {}
    for old in shown:
        if old.id not in used:
            groups.setdefault(identity(old.props), []).append(old)
    for e in current.elements:
        if id(e) not in match and (queue := groups.get(identity(e.props))):
            old = queue.pop(0)
            match[id(e)] = old
            used.add(old.id)
    # Compteur monotone par fenêtre : un [N] retiré n'est jamais redonné à un autre élément.
    following = max(previous.issued, max((e.id for e in previous.elements), default=0))
    # Positions comparées dans le repère de la fenêtre : un déplacement de la fenêtre entière n'est ni un défilement
    # ni un déplacement d'élément (les coordonnées de l'image le neutralisent déjà).
    offset = _window_offset(previous, current)
    shift = _common_shift([(e, match[id(e)]) for e in current.elements if id(e) in match], offset)
    added, changed, unchanged, known = [], [], 0, set()
    shifted: list[int] = []
    for e in current.elements:
        old = match.get(id(e))
        if old is None:
            following += 1
            e.id = following
            added.append(e)
            known.add(e.id)
            continue
        e.id = old.id
        known.add(e.id)
        moved = _moved(e, old, offset)
        if moved and shift is not None and _moved_by(e, old, shift, offset):
            moved = False
            shifted.append(e.id)
        why = [
            label
            for label, differs in (("text", e.name != old.name), ("state", flags(e) != flags(old)), ("moved", moved))
            if differs
        ]
        if why:
            changed.append((e, why))
        else:
            unchanged += 1
    current.issued = following
    removed = [old for old in shown if old.id not in used]
    differing = len(added) + len(removed) + len(changed)
    ratio = differing / max(len(shown), len(current.elements), 1)
    return Changes(added, removed, changed, unchanged, ratio, known, shift if shifted else None, sorted(shifted))


SHIFT_SHARE = 0.5  # part des éléments retrouvés déplacés du même vecteur au-delà de laquelle c'est un défilement


def _window_offset(previous: View, current: View) -> tuple[int, int]:
    """Déplacement de la fenêtre entre les deux vues (son coin à la construction de chacune)."""
    a, b = previous.frame.anchor, current.frame.anchor
    return (b[0] - a[0], b[1] - a[1]) if a is not None and b is not None else (0, 0)


def _delta(e: Element, old: Element, offset: tuple[int, int] = (0, 0)) -> tuple[int, int] | None:
    """Déplacement de l'élément DANS la fenêtre (celui de la fenêtre retiré)."""
    if e.rect is None or old.rect is None:
        return None
    (x1, y1), (x2, y2) = e.rect.center, old.rect.center
    return x1 - x2 - offset[0], y1 - y2 - offset[1]


def _common_shift(pairs: list[tuple[Element, Element]], offset: tuple[int, int] = (0, 0)) -> tuple[int, int] | None:
    """Translation partagée par la plupart des éléments déplacés dans la fenêtre (défilement d'une page ou d'une
    liste) : la même à MOVED_PX près pour au moins SHIFT_SHARE des éléments retrouvés. None : pas de défilement."""
    deltas = [
        d for e, old in pairs if (d := _delta(e, old, offset)) is not None and max(abs(d[0]), abs(d[1])) > MOVED_PX
    ]
    if not deltas or not pairs:
        return None
    # Candidat : le vecteur exact le plus fréquent (un défilement déplace tout du même nombre de pixels), puis tous
    # ceux qui en sont à MOVED_PX près.
    best = Counter(deltas).most_common(1)[0][0]
    count = sum(1 for x, y in deltas if abs(x - best[0]) <= MOVED_PX and abs(y - best[1]) <= MOVED_PX)
    return best if count >= SHIFT_SHARE * len(pairs) else None


def _moved_by(e: Element, old: Element, shift: tuple[int, int], offset: tuple[int, int] = (0, 0)) -> bool:
    d = _delta(e, old, offset)
    return d is not None and abs(d[0] - shift[0]) <= MOVED_PX and abs(d[1] - shift[1]) <= MOVED_PX


def _moved(e: Element, old: Element, offset: tuple[int, int] = (0, 0)) -> bool:
    if e.rect is None or old.rect is None:
        return (e.rect is None) != (old.rect is None)
    d = _delta(e, old, offset)
    return d is not None and max(abs(d[0]), abs(d[1])) > MOVED_PX


def id_ranges(ids: list[int]) -> str:
    """[2, 3, 4, 7, 9, 10] → "[2-4], [7], [9-10]"."""
    runs: list[list[int]] = []
    for n in sorted(set(ids)):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(f"[{a}]" if a == b else f"[{a}-{b}]" for a, b in runs)


# --- OCR en secours ------------------------------------------------------------------------------------------
OCR_BELOW = 6  # moins d'éléments UIA que cela (Tk, canevas, jeu) : le texte de l'image est lu par OCR
_TEXT_TYPE = 50020  # UIA_TextControlTypeId (constante documentée) : hors Windows, CONTROL_TYPES est vide au chargement


def wants_ocr(view: View) -> bool:
    return not view.ocr_done and (view.web_missing or view.unreadable or len(view.elements) < OCR_BELOW)


def add_ocr(view: View, physical: Image.Image) -> int:
    """Ajoute aux éléments de la vue les lignes de texte lues par OCR dans son image physique (`capture_view`), en
    éléments « Text » marqués (OCR) ; un texte déjà publié par UIA n'est pas doublé. Renvoie le nombre ajouté."""
    view.ocr_done = True
    lines = ocr.read(physical)
    if not lines:
        return 0
    known = {e.name.lower() for e in view.elements if e.name}
    ox, oy = view.frame.origin
    following = max(view.issued, max((e.id for e in view.elements), default=0))
    added = 0
    for line in lines:
        if line.text.lower() in known:
            continue
        left, top, width, height = line.rect
        props = {
            "control_type": _TEXT_TYPE,
            "name": line.text,
            "rect": (ox + left, oy + top, width, height),
            "ocr": True,
        }
        following += 1
        view.elements.append(Element(following, uia.Node(None, 0, props), 0, view.hwnd))
        added += 1
    if added:
        view.notes.append(f"{added} texts read by OCR in the image (no UI Automation element): targets by point")
    return added


# --- Image ----------------------------------------------------------------------------------------------------
def capture_view(view: View) -> tuple[Image.Image, list[str]]:
    """Image physique composée (fenêtre + surgissantes), recalée sur view.frame.

    PrintWindow pour TOUTES les fenêtres (une fenêtre non adaptée au DPI est dessinée à sa taille logique puis
    agrandie : capture_printwindow). Windows.Graphics.Capture seulement en dernier recours, quand PrintWindow échoue ou
    rend une image vide : WGC a perdu ~1 handle noyau par capture dans un régime intermittent (recheck6 : +120 à +161
    handles par série de 120), cause native non identifiée — un serveur qui vit longtemps ne doit pas en dépendre."""
    notes: list[str] = []
    width, height = view.frame.size
    canvas = Image.new("RGB", (width, height), (32, 32, 32))
    for win in view.windows:
        shot = None
        painted = False
        for method in (capture.capture_printwindow, capture.capture_wgc):
            try:
                shot = method(win)
            except Exception as error:  # noqa: BLE001
                notes.append(f"capture {method.__name__.removeprefix('capture_')}: {error}")
                continue
            if capture.blank_ratio(shot.image) < 1.0:
                painted = True
                break
            notes.append(f"capture {shot.method} blank")
        chromium = win == view.hwnd and view.info.framework == "Chromium"
        if shot is not None and not painted and chromium and w.root(w.foreground()) != view.hwnd:
            # Image unie (écran de démarrage, fond) ET recouvrement prouvé : cause probable, jamais affirmée (une page
            # d'une seule couleur, dessinée, donne la même image) ; non recouverte, rien n'est dit.
            if covered_by_window(view.hwnd):
                notes.append(
                    "Image of one plain color, window covered: probably not drawn (Chromium stops drawing a covered "
                    "window), or a page of one color"
                    + (": see the note above." if view.web_missing else "; " + covered_remedy(view.hwnd))
                )
        if shot is None:
            continue
        canvas.paste(shot.image, (shot.origin[0] - view.frame.origin[0], shot.origin[1] - view.frame.origin[1]))
    return canvas, notes


_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _font(size: int):
    if size not in _FONT_CACHE:
        try:
            _FONT_CACHE[size] = ImageFont.truetype("segoeuib.ttf", size)
        except OSError:
            _FONT_CACHE[size] = ImageFont.load_default(size=size)
    return _FONT_CACHE[size]


def marked(view: View, marks: str = "auto") -> list[Element]:
    """Éléments dont le numéro est dessiné sur l'image."""
    if marks == "none":
        return []
    return [e for e in view.elements if e.actionable and e.rect is not None and not (marks == "auto" and e.name)]


def render_image(view: View, physical: Image.Image, marks: str = "auto") -> Image.Image:
    image = (
        physical.resize(view.frame.image_size, Image.Resampling.LANCZOS) if view.frame.scale < 1 else physical.copy()
    )
    if marks == "none":
        return image
    draw = ImageDraw.Draw(image, "RGBA")
    font = _font(11)
    for e in marked(view, marks):
        rect = e.rect
        if rect is None:  # marked() n'en rend pas ; relu ici pour le contrôle de types
            continue
        x, y = view.frame.to_image(rect.left, rect.top)
        label = str(e.id)
        box = draw.textbbox((0, 0), label, font=font)
        tw, th = box[2] - box[0] + 4, box[3] - box[1] + 3
        x = min(max(0, x), image.width - tw)
        y = min(max(0, y), image.height - th)
        draw.rectangle((x, y, x + tw, y + th), fill=(200, 30, 120, 215))
        draw.text((x + 2, y + 1 - box[1]), label, font=font, fill=(255, 255, 255, 255))
    return image


def image_bytes(image: Image.Image) -> bytes:
    """Image envoyée au modèle, en WebP (règle commune) : sans perte si ≤ 200 Ko, sinon qualité 85 puis 70 (jamais
    au-delà de 500 Ko : qualité 50, 30, 10 puis 0 en dernier recours). Mêmes dimensions : les coordonnées de l'image ne changent pas."""
    rgb = image.convert("RGB")

    def encode(**options: object) -> bytes:
        buffer = io.BytesIO()
        rgb.save(buffer, format="WEBP", method=4, **options)
        return buffer.getvalue()

    data = encode(lossless=True)
    if len(data) <= LOSSLESS_BUDGET:
        return data
    # Puis qualité décroissante jusqu'à tenir 500 Ko (image très bruitée : à 0, un écran entier pèse < 100 Ko).
    for quality, budget in (
        (85, LOSSLESS_BUDGET),
        (70, IMAGE_LIMIT),
        (50, IMAGE_LIMIT),
        (30, IMAGE_LIMIT),
        (10, IMAGE_LIMIT),
        (0, IMAGE_LIMIT),
    ):
        data = encode(quality=quality)
        if len(data) <= budget:
            return data
    return data


def image_print(image: Image.Image) -> bytes:
    """Empreinte EXACTE d'une image envoyée (dimensions et pixels RGB) : écran inchangé depuis la dernière image = pas
    de double. Pas l'empreinte grise 64×64 de shared/images.md : un voyant rouge devenu vert y garde le même gris
    (relecture de Codex du 24/10) et disparaîtrait de la réponse."""
    rgb = image.convert("RGB")
    return hashlib.sha256(f"{rgb.width}x{rgb.height}:".encode() + rgb.tobytes()).digest()


# --- Résolution d'une cible -----------------------------------------------------------------------------------
class TargetError(ValueError):
    pass


_POINT = re.compile(r"^\s*\(?\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*\)?\s*$")
_ID = re.compile(r"^\s*(?:#|\[)?\s*(\d+)\s*\]?\s*$")


def parse_point(spec) -> tuple[float, float] | None:
    if isinstance(spec, (list, tuple)) and len(spec) == 2 and all(isinstance(v, (int, float)) for v in spec):
        return float(spec[0]), float(spec[1])
    if isinstance(spec, str):
        match = _POINT.match(spec)
        if match and not _ID.match(spec):
            return float(match.group(1)), float(match.group(2))
    return None


class AmbiguousTarget(TargetError):
    pass


def _split_role(spec: str) -> tuple[set[str] | None, str]:
    text = spec.strip()
    match = re.match(r"^([A-Za-zéèÉ]+)\s*[:]\s*(.+)$", text) or re.match(r"^([A-Za-zéèÉ]+)\s+[«\"'](.+?)[»\"']$", text)
    known = {r.lower(): {r} for r in ACTION_ROLES | CONTAINER_ROLES | TEXT_ROLES}
    if match and match.group(1).lower() in ("nom", "name"):
        return None, match.group(2).strip()  # « nom:15 » : un nom, même s'il ressemble à un numéro
    if match and (match.group(1).lower() in ROLE_ALIASES or match.group(1).lower() in known):
        key = match.group(1).lower()
        return ROLE_ALIASES.get(key) or known[key], match.group(2).strip()
    return None, text


def _rank(e: Element, prefer: set[str] | None) -> tuple:
    return (
        bool(prefer) and e.role not in prefer,
        not e.actionable,
        e.role in TEXT_ROLES,
        e.props.get("enabled") is False,
    )


def _matches(view: View, spec: str) -> tuple[list[Element], list[Element]]:
    roles, text = _split_role(spec)
    wanted = norm(text).strip("«»\"' ").lower()
    pool = [e for e in view.elements if roles is None or e.role in roles]
    exact = [e for e in pool if e.name.lower() == wanted]
    if not exact:
        exact = [e for e in pool if str(e.props.get("automation_id") or "").lower() == wanted]
    # Correspondance partielle : jamais le document racine d'une page désigné par son titre, qui change avec la page
    # (banc commun du 21/10 : « champ:Nom » absent un instant de l'arbre Chromium, le document « Labo Web — nom … »
    # était pris à sa place et refusé en lecture seule). Seul son nom exact le désigne.
    title = norm(w.window_text(view.hwnd)).lower() if view.hwnd else ""
    partial = [
        e
        for e in pool
        if wanted
        and wanted in e.name.lower()
        and e not in exact
        and not (e.role == "Document" and title and e.name.lower() in title)
    ]
    if not exact and not partial and wanted:
        # Liste déroulante sans nom (Steam : choix du disque) : désignée par la valeur qu'elle affiche.
        partial = [
            e for e in pool if e.role == "ComboBox" and not e.name and wanted in norm(str(e.props.get("value") or "")).lower()
        ]  # fmt: skip
    return exact, partial


def scrolled_away(view: View) -> list[Element]:
    """Éléments de la fenêtre principale hors de la zone visible parce que leur conteneur a défilé (UIA « offscreen »,
    motif ScrollItem) : absents de la liste, mais visables par leur nom exact une fois amenés à l'écran
    (ScrollIntoView). Mesuré le 2026-10-14 : page Electron défilée de 275 px par la frappe dans un éditeur du bas,
    champs du haut introuvables."""
    root = view.roots.get(view.hwnd)
    if root is None:
        return []
    out: list[Element] = []
    for node in root.walk():
        p = node.props
        if node is root or not p.get("offscreen") or not p.get("has_scroll_item") or node.rect is None:
            continue
        if node.control_type in TEXT_ROLES or not _listable(node, ""):
            continue
        out.append(Element(0, node, 0, view.hwnd))
    return out


def _outermost(elements: list[Element]) -> list[Element]:
    """Un même contrôle publié deux fois, imbriqué sous le même rôle et le même nom (onglet React de Steam : TabItem
    « Stockage » qui contient un TabItem « Stockage ») n'est pas une ambiguïté : le plus extérieur le représente."""
    nodes = {id(e.node) for e in elements}

    def nested(e: Element) -> bool:
        parent = e.node.parent
        while parent is not None:
            if id(parent) in nodes and parent.control_type == e.role and norm(parent.name) == e.name:
                return True
            parent = parent.parent
        return False

    return [e for e in elements if not nested(e)]


def _without_popup_twins(view: View, elements: list[Element]) -> list[Element]:
    """Liste déroulante WPF ouverte : chaque option est publiée deux fois, sous la liste déroulante (fenêtre
    principale) et dans sa surgissante, avec deux identifiants mais même rôle, même nom, même place. C'est la même
    option : la copie de la surgissante est écartée (celle de la liste déroulante permet de la refermer et de relire
    le choix). Places qui se recouvrent exigées (pas identiques : mesuré, la surgissante glisse pendant ~0,2 s à
    l'ouverture) ; deux options homonymes de la liste déroulante elle-même restent ambiguës."""
    if len(elements) < 2:
        return elements

    def in_open_combo(e: Element) -> bool:
        node = e.node.parent
        for _ in range(4):
            if node is None:
                return False
            if node.control_type == "ComboBox":
                return node.props.get("expand") == 1
            node = node.parent
        return False

    anchors = [e for e in elements if e.window == view.hwnd and in_open_combo(e)]
    twins = {
        id(e)
        for e in elements
        if e.window != view.hwnd
        and e.rect is not None
        and any(
            a.role == e.role and a.name == e.name and a.rect is not None and a.rect.intersect(e.rect) is not None
            for a in anchors
        )
    }
    return [e for e in elements if id(e) not in twins]


def pick(view: View, spec: str, prefer: set[str] | None = None) -> tuple[Element, str]:
    """L'élément désigné par son nom (« Valider », « bouton:Valider », « button Valider »), ou une erreur explicite :
    jamais un choix au hasard entre homonymes.

    prefer : rôles attendus par l'action (une liste pour select, un champ pour set…) classés en tête ; les
    étiquettes (Text) passent après les contrôles du même nom. Renvoie (élément, note) ; la note signale une
    correspondance partielle.
    """
    exact, partial = _matches(view, spec)
    group, kind = (exact, "exact") if exact else (partial, "partial")
    if not group:
        if view.unreadable:
            raise TargetError(
                f'No readable element "{spec}": the UI Automation tree of the window could not be read (only its '
                "popups were). Use look to see the image, target by point x,y."
            )
        raise TargetError(f'No element "{spec}" in the window (look to see the list).')
    group = sorted(group, key=lambda e: _rank(e, prefer))
    best = _rank(group[0], prefer)
    outer = _outermost([e for e in group if _rank(e, prefer) == best])
    tied = _without_popup_twins(view, outer)
    if len(tied) < len(outer) and not any(group[0] is e for e in tied):
        group = [tied[0], *group]  # le premier était la copie de surgissante d'une option : l'option elle-même
    if len(tied) > 1:
        listed = ", ".join(e.describe() for e in tied[:8])
        raise AmbiguousTarget(
            f'"{spec}" matches several elements: {listed}. Target by [N] or narrow it down (role:name).'
        )
    note = f"partial match: {group[0].describe()}" if kind == "partial" else ""
    return group[0], note
