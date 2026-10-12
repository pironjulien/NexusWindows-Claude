"""Exécution des étapes d'action avec la technique la plus sûre pour chaque famille d'interface.

Ordre des techniques tiré du banc (bench/RESULTS.md) :
- Win32 natif : messages directs (BN_CLICKED, CB/LB_SETCURSEL + notification relue, EM_REPLACESEL, WM_COMMAND).
  UIA Invoke y vole le focus et UIA Select n'y prévient pas l'application ; sur un élément servi par le pont MSAA
  (élément de liste), Invoke fait même un VRAI double-clic : jamais.
- WinForms : UIA (Invoke, Toggle, Value), listes et listes déroulantes par messages.
- WPF / UWP / WinUI : UI Automation seulement (WPF accepte aussi WM_CHAR).
- Chromium / Electron : UI Automation ; champs multilignes et <select> par frappes dans une activation brève ;
  DevTools si l'application a été lancée avec (alors jamais de frappes).
- Tk et canevas : aucun canal d'arrière-plan prouvé → vraie souris/clavier avec accord.
Les clics et la molette par messages vont à la fenêtre de la cible réellement sous le point (surgissante comprise).

Contrat de statut (rapporté au modèle) :
- verified=True  « ✓ » : l'effet a été relu (valeur, coche, sélection, texte apparu) ;
- verified=None  « ~ » : action transmise par un canal fiable, effet non relu (à vérifier sur l'image) ;
- ok=False       « ✗ » : échec, refus, relecture contraire, ou aucun effet visible après une action par messages.

Contrat de cible :
- [N] se résout dans la numérotation MONTRÉE au modèle, puis l'élément est relu et son identité contrôlée
  (rôle, nom, identifiant d'automatisation) : s'il a changé, refus « refaire look » ;
- un nom se résout dans un instantané frais (reconstruit au premier nom de chaque appel et après chaque action),
  sans homonyme ni correspondance partielle ambiguë ;
- un point se résout en élément de la CIBLE sous ce point (jamais une autre fenêtre, même du même programme ; la
  plus haute de ses fenêtres qui couvre le point décide) ; pour coche, choix, dépliage, focus et saisie, l'intention
  est appliquée à cet élément, ou refusée.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import comtypes

from . import capture, cdp, foreground, keys, methods, policy, realinput, snapshot, uia, worker
from . import win32 as w

UIA_ONLY = {"WPF", "XAML", "UWP", "WinUI", "DirectUI"}
NO_KEYBOARD_MESSAGES = {"XAML", "UWP", "WinUI", "DirectUI", "Tk"}  # WM_CHAR/WM_KEYDOWN postés ignorés (mesuré)
NO_MOUSE_MESSAGES = UIA_ONLY | {"Tk"}  # clics et molette postés ignorés (mesuré)
BROWSERS_WITHOUT_MESSAGE_WHEEL = {"msedge.exe", "chrome.exe", "brave.exe"}  # Edge ✗ au banc ; Electron ✓
# Contrôles communs Win32 dont les éléments sont servis par le proxy MSAA natif (oleacc) : leur action par défaut
# simule un vrai clic ou double-clic (SendInput).
NATIVE_PROXY_CLASSES = (
    "listbox", "combolbox", "combobox", "systabcontrol32", "syslistview32", "systreeview32", "sysheader32",
    "toolbarwindow32", "rebarwindow32", "msctls_statusbar32", "#32768",
)  # fmt: skip
# Zones du shell où un double-clic, un clic droit, la touche « applications » ou Maj+F10 EXÉCUTENT un fichier : la liste
# d'une boîte de fichiers (DirectUIHWND/SHELLDLL_DefView d'une #32770) et les menus contextuels shell (#32768). Le clic
# simple de sélection, le nom de fichier et les boutons Ouvrir/Enregistrer restent permis.
SHELL_VIEW_CLASSES = ("SHELLDLL_DefView", "DirectUIHWND")
FILE_DIALOG_CLASS = "#32770"
SHELL_MENU_CLASS = "#32768"
VK_APPS = 0x5D
VK_F10 = 0x79
VK_SHIFT = 0x10
ITEM_ROLES = {"ListItem", "TreeItem", "DataItem", "TabItem", "RadioButton"}
TEXT_ROLES = {"Edit", "Document", "ComboBox"}
CHOICE_ROLES = {
    "ComboBox",
    "List",
    "Tree",
    "Tab",
    "DataGrid",
    "Table",
    "ListItem",
    "TreeItem",
    "TabItem",
    "RadioButton",
}
CHECK_ROLES = {"CheckBox", "RadioButton", "ListItem", "TreeItem", "MenuItem", "Button"}
EXPAND_ROLES = {"ComboBox", "TreeItem", "MenuItem", "SplitButton", "Group", "DataItem"}
SCROLL_ROLES = {"List", "Tree", "Document", "Pane", "DataGrid", "Table", "Group"}
LEAF_ROLES = {
    "Button", "CheckBox", "RadioButton", "Hyperlink", "MenuItem", "TabItem", "ListItem", "TreeItem", "DataItem",
    "SplitButton", "ComboBox", "HeaderItem",
}  # fmt: skip
EXPECTED = (
    methods.MethodError,
    uia.UIAError,
    comtypes.COMError,
    cdp.CDPError,
    capture.CaptureError,
    OSError,
    ValueError,
    foreground.BusyUser,
)
SELECT_ALL_JS = "(() => { const a = document.activeElement; if (a && a.select) a.select(); else document.execCommand('selectAll'); })()"


class ActionError(RuntimeError):
    pass


class NeedsForeground(ActionError):
    pass


class Unconfirmed(ActionError):
    """Action transmise dont l'effet n'a pas été relu : ne rien rejouer par-dessus (doublon possible), même avec
    foreground=true."""


class NoVisibleEffect(methods.MethodError):
    """Action par messages sans retour possible et rien n'a changé à l'écran : l'application l'a ignorée."""


@dataclass
class Outcome:
    ok: bool
    line: str
    method: str = ""
    attempts: list[str] = field(default_factory=list)
    verified: bool | None = None  # True : effet relu ; None : transmis sans relecture

    @property
    def symbol(self) -> str:
        if not self.ok:
            return "✗"
        return "✓" if self.verified else "~"


def _short(error: BaseException) -> str:
    text = str(error) or type(error).__name__
    return text.splitlines()[0][:160]


def framework_of(e: snapshot.Element, view: snapshot.View) -> str:
    raw = str(e.props.get("framework") or "").lower()
    return snapshot.FRAMEWORK_BY_UIA.get(raw) or view.framework or "Win32"


def _unique(elements: list[snapshot.Element]) -> list[snapshot.Element]:
    """Un même élément UIA peut apparaître dans deux arbres (WPF : sous-menu dans le menu ET dans sa surgissante) :
    dédoublonnage par identifiant d'exécution (c'est le même élément, n'importe lequel convient)."""
    unique: dict[tuple, snapshot.Element] = {}
    for e in elements:
        unique.setdefault(uia.runtime_id(e.props) or (id(e.node),), e)
    return list(unique.values())


def _same_identity(expected: tuple, live: dict) -> bool:
    """L'élément relu est-il celui que le modèle a vu ? (rôle, nom, identifiant d'automatisation à l'affichage)"""
    return snapshot.identity(live) == expected


# --- Empilement des fenêtres de la cible et recouvrement ----------------------------------------------------------
PATTERN_FLAGS = ("has_invoke", "has_toggle", "has_value", "has_selection_item", "has_expand", "has_range")


def _top_down(windows: set[int], bottom_up: list[int]) -> list[int]:
    """Fenêtres du haut vers le bas de l'empilement réel (EnumWindows) ; celles qu'il ne voit plus suivent, dans
    l'ordre de l'instantané (bottom_up : view.windows, du bas vers le haut)."""
    order = [h for h in w.enum_windows() if h in windows]
    order += [h for h in reversed(bottom_up) if h in windows and h not in order]
    return order + sorted(windows - set(order))


def _roots_top_down(view: snapshot.View) -> list[tuple[int, list[uia.Node]]]:
    """Fenêtres de la vue du haut vers le bas (surgissantes dans l'ordre z réel, puis la fenêtre principale), chacune
    avec les racines de ses arbres. Vue sans racines mémorisées : celles des éléments listés ; aucune racine : arbre
    illisible."""
    roots: dict[int, list[uia.Node]] = {win: [root] for win, root in view.roots.items()}
    if not roots:
        seen: set[int] = set()
        for e in view.elements:
            root = e.node
            while root.parent is not None:
                root = root.parent
            if id(root) not in seen:
                seen.add(id(root))
                roots.setdefault(e.window, []).append(root)
    popups = (set(view.windows[1:]) | set(roots)) - {view.hwnd}
    order = _top_down(popups, view.windows[1:]) + [view.hwnd]
    return [(win, roots.get(win, [])) for win in order]


def _covers(window: int, x: int, y: int) -> bool:
    """La fenêtre est-elle affichée sur ce point écran ? (géométrie, indépendante de ce qui la recouvre)"""
    return bool(w.IsWindowVisible(window)) and not w.is_cloaked(window) and w.frame_rect(window).contains(x, y)


def _contains(node: uia.Node, x: int, y: int) -> bool:
    rect = node.rect
    return rect is not None and not node.props.get("offscreen") and rect.contains(x, y)


def _web_document(node: uia.Node) -> bool:
    """Racine d'une page web Chromium/Electron (le DOM y porte l'ordre z de ce qui est dessiné)."""
    p = node.props
    return (
        node.control_type == "Document"
        and str(p.get("framework") or "").lower() == "chrome"
        and str(p.get("automation_id") or "").lower() == "rootwebarea"
    )


def _chrome_void(node: uia.Node) -> bool:
    """Vue Chromium vide (views::View de mise en page) : sans nom, sans enfant ni motif, elle ne dessine rien."""
    p = node.props
    return (
        node.control_type in ("Pane", "Group", "Custom")
        and str(p.get("framework") or "").lower() == "chrome"
        and not node.name
        and not node.children
        and not any(p.get(k) for k in PATTERN_FLAGS)
    )


def _hit_in(root: uia.Node, x: int, y: int) -> uia.Node | None:
    """Nœud le plus haut sous le point dans un arbre : le dernier (ordre de l'arbre) qui contient le point, enfants et
    frères suivants étant dessinés par-dessus. Point dans une page web : son seul sous-arbre (un voile web y figure) ;
    ailleurs, les vues Chromium vides sont transparentes. Un voile WPF/Win32 sans nom reste opaque."""
    document = next((n for n in root.walk() if _web_document(n) and _contains(n, x, y)), None)
    hit = None
    for node in (document or root).walk():
        if _contains(node, x, y) and (document is not None or not _chrome_void(node)):
            hit = node
    return hit


def _locate(view: snapshot.View, rid: tuple | None) -> tuple[uia.Node, int] | None:
    """Nœud de l'instantané (nœuds non listés compris) qui porte cet identifiant d'exécution, et sa fenêtre."""
    if not rid:
        return None
    for window, roots in _roots_top_down(view):
        for root in roots:
            for node in root.walk():
                if uia.runtime_id(node.props) == rid:
                    return node, window
    return None


class Executor:
    def __init__(
        self,
        auto: uia.Automation,
        hwnd: int,
        get_view: Callable[[bool], snapshot.View],
        get_page: Callable[[], cdp.Page | None],
        allow_foreground: bool = False,
        get_shown: Callable[[], snapshot.View | None] | None = None,
        get_frame: Callable[[], snapshot.Frame | None] | None = None,
        fresh: bool = False,
    ) -> None:
        self.auto = auto
        self.hwnd = hwnd
        self.get_view = get_view
        self.get_page = get_page
        self.get_shown = get_shown or (lambda: None)
        self.get_frame = get_frame or (lambda: None)
        self.allow_foreground = allow_foreground
        # Vrai au départ : le premier nom de chaque appel se cherche dans un instantané reconstruit (l'interface a pu
        # changer depuis le dernier appel, y compris par l'appel précédent rendu avec then="none"). fresh : instantané
        # construit à l'instant par cet appel, rien n'a été fait depuis : pas de seconde construction.
        self.acted = not fresh
        self.notes: list[str] = []  # remarques à joindre au compte rendu de l'étape (correspondance partielle…)
        self.unreadable = ""  # pourquoi le dernier present() n'a pu conclure (arbre vide, tronqué, page non publiée)
        self._pids: set[int] | None = None
        self._windows: set[int] | None = None

    @property
    def view(self) -> snapshot.View:
        return self.get_view(False)

    @property
    def target_pids(self) -> set[int]:
        """Processus réellement propriétaires de la cible (UWP : l'application, pas ApplicationFrameHost)."""
        if self._pids is None:
            self._pids = foreground.target_pids(self.hwnd, list(self.windows))
        return self._pids

    @property
    def windows(self) -> set[int]:
        """Fenêtres de la cible : principale, surgissantes, dialogues possédés. Jamais une autre fenêtre du même
        programme (Julien peut avoir son propre Edge, son propre document)."""
        if self._windows is None:
            self._windows = foreground.target_windows(self.hwnd, self.view.windows)
        return self._windows

    def _rebuild(self) -> snapshot.View:
        self.acted = False
        self._pids = None
        self._windows = None
        return self.get_view(True)

    @staticmethod
    def _shown_id(e: snapshot.Element) -> snapshot.Element:
        """Les comptes rendus ne citent jamais de [N] : la liste renvoyée avec eux est renumérotée, un numéro y
        désignerait autre chose. L'élément est décrit par son rôle et son nom."""
        return snapshot.Element(0, e.node, e.depth, e.window)

    def _frame(self) -> snapshot.Frame:
        """Cadre de la dernière image MONTRÉE de cette fenêtre : les points "x,y" s'y lisent. La fenêtre a pu bouger
        (suivi), mais pas changer de taille (les pixels de l'image ne correspondraient plus)."""
        frame = self.get_frame()
        if frame is None:
            raise ActionError("Aucune image de cette fenêtre montrée : look (avec image) avant de viser un point x,y.")
        if frame.resized(w.frame_rect(self.hwnd)):
            raise ActionError(
                "La fenêtre a changé de taille depuis la dernière image : refaire look avant de viser x,y."
            )
        return frame

    def _screen_to_image(self, x: int, y: int) -> tuple[int, int]:
        frame = self.get_frame() or self.view.frame
        return frame.to_image(x, y, w.frame_rect(self.hwnd))

    # --- Outils internes ----------------------------------------------------------------------------------------
    def _try(self, chain: list[tuple[str, Callable[[], Any]]], what: str) -> Outcome:
        attempts: list[str] = []
        for name, fn in chain:
            worker.check_cancelled()
            try:
                detail = fn()
            except NeedsForeground:
                raise
            except NoVisibleEffect as error:
                attempts.append(f"{name} ✗ {_short(error)}")
                # L'action par messages a pu prendre effet plus tard : ne rien rejouer par-dessus (doublon possible).
                raise Unconfirmed(
                    f"{what} : aucun effet visible en 0,6 s ({' ; '.join(attempts)}). L'effet a pu arriver plus tard : "
                    "vérifier sur l'image avant toute nouvelle tentative."
                ) from None
            except EXPECTED as error:
                attempts.append(f"{name} ✗ {_short(error)}")
                continue
            attempts.append(f"{name} ✓")
            text = detail if isinstance(detail, str) else ""
            verified = True if text.startswith("relu ✓") else None
            extra = f" — {text}" if text else ""
            return Outcome(True, f"{what} ({name}{extra})", name, attempts, verified)
        raise ActionError(f"{what} : aucune technique n'a marché ({' ; '.join(attempts) or 'aucune applicable'}).")

    def fresh(self, e: snapshot.Element, expected: tuple | None = None) -> snapshot.Element:
        """Relit l'élément en direct (état, focus, position). expected : identité montrée au modèle ; refus si
        l'élément à cette place n'est plus celui-là (liste insérée, ligne décalée…)."""
        try:
            live = self.auto.refresh(e.node.element)
        except uia.UIAError:
            live = None
        if live is not None:
            if expected is not None and not _same_identity(expected, live):
                raise ActionError(
                    f"{e.describe()} a changé (désormais « {snapshot.clip(snapshot.norm(live.get('name')), 40)} ») : "
                    "refaire look."
                )
            e.node.props.update(live)
            return self._shown_id(e)
        rid = uia.runtime_id(e.props)
        if rid:
            for other in self._rebuild().elements:
                if uia.runtime_id(other.props) == rid and (expected is None or _same_identity(expected, other.props)):
                    return self._shown_id(other)
        raise ActionError(f"{e.describe()} a disparu ou l'interface a changé : refaire look.")

    def resolve(self, spec: Any, prefer: set[str] | None = None) -> tuple[str, Any]:
        """Cible → ("element", Element relu en direct) ou ("point", (x_écran, y_écran))."""
        if spec is None or spec == "":
            raise ActionError("Cible manquante.")
        point = snapshot.parse_point(spec)
        if point is not None:
            frame = self._frame()
            if not frame.contains_image_point(*point):
                width, height = frame.image_size
                raise ActionError(f"Point {point} hors de l'image de la fenêtre ({width}×{height}).")
            return "point", frame.to_screen(*point, w.frame_rect(self.hwnd))
        ident = None
        if isinstance(spec, int):
            ident = spec
        elif isinstance(spec, str) and snapshot._ID.match(spec):
            ident = int(snapshot._ID.match(spec).group(1))
            bare = spec.strip()
            if bare.isdigit():
                view = self._rebuild() if self.acted else self.view
                if any(e.name == bare for e in view.elements):
                    raise ActionError(
                        f"« {bare} » : élément [{bare}] ou élément nommé « {bare} » ? Écrire #{bare} ou nom:{bare}."
                    )
        if ident is not None:
            shown = self.get_shown()
            if shown is None or not shown.displayed:
                raise ActionError(f"[{ident}] : aucune liste montrée pour cette fenêtre : faire look d'abord.")
            element = shown.by_id.get(ident)
            if element is None or ident not in shown.displayed:
                raise ActionError(f"[{ident}] n'a pas été montré dans le dernier look : refaire look (ou query=…).")
            return "element", self.fresh(element, expected=shown.displayed[ident])
        # Par nom : instantané frais (reconstruit au premier nom de l'appel et après chaque action).
        view = self._rebuild() if self.acted else self.view
        try:
            element, note = snapshot.pick(view, str(spec), prefer)
        except snapshot.AmbiguousTarget as error:
            raise ActionError(str(error)) from None
        except snapshot.TargetError:
            try:
                element, note = snapshot.pick(self._rebuild(), str(spec), prefer)
            except snapshot.TargetError as error:
                raise ActionError(str(error)) from None
        if note:
            self.notes.append(note)
        return "element", self.fresh(element)

    def resolve_element(self, spec: Any, prefer: set[str], what: str) -> snapshot.Element:
        """Comme resolve, mais un point devient l'élément de la cible sous ce point (rôles `prefer`), ou un refus :
        coche, choix, dépliage et saisie ne se réduisent jamais à un simple clic au hasard."""
        kind, target = self.resolve(spec, prefer)
        if kind == "element":
            return target
        element = self._element_under(*target, roles=prefer)
        if element is None:
            raise ActionError(f"Aucun élément « {what} » de la fenêtre sous ce point : viser par [N] ou par nom.")
        return element

    def _element_hwnd_class(self, e: snapshot.Element) -> tuple[int, str]:
        hwnd = e.node.hwnd
        return hwnd, (methods.win_class(hwnd) if hwnd else "")

    def _ancestor_hwnd(self, e: snapshot.Element, classes: tuple[str, ...]) -> int:
        node = e.node
        for _ in range(6):
            if node is None:
                return 0
            if node.hwnd and methods._is_class(node.hwnd, classes):
                return node.hwnd
            node = node.parent
        return 0

    @staticmethod
    def _sibling_index(e: snapshot.Element, role: str) -> int | None:
        """Rang de l'élément parmi ses frères du même rôle (ordre de l'arbre UIA = ordre du contrôle)."""
        parent = e.node.parent
        if parent is None:
            return None
        rid = uia.runtime_id(e.props)
        same = [c for c in parent.children if c.control_type == role]
        for index, child in enumerate(same):
            if child is e.node or (rid and uia.runtime_id(child.props) == rid):
                return index
        return None

    def _point_of(self, e: snapshot.Element) -> tuple[int, int]:
        rect = e.rect
        if rect is None or e.props.get("offscreen"):
            try:
                self.auto.scroll_into_view(e.node.element)
                e.node.props.update(self.auto.refresh(e.node.element))
                rect = e.rect
            except EXPECTED:
                pass
        if rect is None:
            raise ActionError(f"{e.describe()} n'a pas de position à l'écran.")
        return rect.center

    @staticmethod
    def _msaa_item(e: snapshot.Element, fw: str) -> bool:
        """Élément Win32/WinForms servi par le pont MSAA (sans fenêtre propre, avec une action MSAA par défaut) : UIA
        Invoke y passe par cette action, qui fait un VRAI clic ou double-clic (SendInput). Jamais sans foreground=true.
        Les boutons WinForms (fenêtre propre) et les ToolStrip (framework « WinForm ») n'en sont pas."""
        p = e.props
        if not (
            fw in ("Win32", "WinForms")
            and not e.node.hwnd
            and str(p.get("framework") or "").lower() in ("", "win32")
            and bool(p.get("legacy_action"))
        ):
            return False
        # Seuls les proxys natifs (oleacc) des contrôles communs simulent un clic ; un objet accessible fourni par
        # l'application (MenuStrip et ToolStrip WinForms : PerformClick géré) agit sans entrée réelle.
        node = e.node.parent
        for _ in range(12):
            if node is None:
                return True  # contrôle porteur inconnu : prudence
            if node.hwnd:
                return methods._is_class(node.hwnd, NATIVE_PROXY_CLASSES)
            node = node.parent
        return True

    def _window_under(self, x: int, y: int) -> int:
        """Fenêtre de la cible sous un point écran, par la géométrie : surgissantes et dialogues du haut vers le bas,
        puis la fenêtre principale. Pas window_at seul : un dialogue de la cible recouvert par une fenêtre de Julien
        doit rester visé, et jamais la fenêtre principale SOUS une surgissante de la cible."""
        for window in _top_down(self.windows - {self.hwnd}, self.view.windows[1:]):
            if _covers(window, x, y):
                return window
        return self.hwnd

    def _post_click(self, x: int, y: int, button: str = "left", count: int = 1) -> int:
        """Clic par messages à la fenêtre de la cible réellement sous le point (puis à son enfant le plus profond)."""
        return methods.post_click(self._window_under(x, y), x, y, button, count)

    def _foreground_or_fail(self, what: str, fn: Callable[[], None]) -> Outcome:
        if not self.allow_foreground:
            raise NeedsForeground(
                f"{what} : impossible en arrière-plan pour {self.view.framework}. Avec l'accord de Julien, relancer "
                "avec foreground=true (vraie souris/clavier quelques instants)."
            )
        worker.check_cancelled()
        with realinput.borrowed(self.hwnd, self.windows):
            fn()
        return Outcome(True, f"{what} (vraie souris/clavier, premier plan rendu)", "foreground")

    def _try_or_foreground(self, chain, what: str, real: Callable[[], None]) -> Outcome:
        try:
            if chain:
                return self._try(chain, what)
        except ActionError as error:
            if not self.allow_foreground or isinstance(error, Unconfirmed):
                raise
        return self._foreground_or_fail(what, real)

    def _quiet_focus(self, e: snapshot.Element) -> str:
        """Focus UIA dans une activation brève : il active souvent la fenêtre (Win32, WPF, Chromium). On attend une
        pause de Julien, puis on lui rend le premier plan aussitôt ; le compte rendu le signale."""
        started = time.perf_counter()
        with foreground.brief_activation(self.hwnd, self.windows):
            foreground.mark_own_input()
            self.auto.set_focus(e.node.element)
        took = round((time.perf_counter() - started) * 1000)
        return f"focus UIA, activation brève {took} ms"

    # --- Empreinte visuelle (actions par messages, sans retour) -------------------------------------------------
    def _fingerprint(self) -> tuple:
        windows = tuple(sorted(h for h in snapshot.popups_of(self.hwnd)))
        try:
            image = capture.capture_printwindow(self.hwnd).image.convert("L").resize((96, 96))
            digest = hashlib.blake2b(image.tobytes(), digest_size=16).digest()
        except capture.CaptureError:
            digest = b""
        return digest, windows, w.window_text(self.hwnd)

    def _watched(
        self, fn: Callable[[], Any], prepare: Callable[[], Any] | None = None, settle_s: float = 0.6
    ) -> Callable[[], str]:
        """Enveloppe une action par messages : préparation (focus, activation) d'abord, puis empreinte de référence,
        puis l'action ; si rien ne change (image, surgissantes, titre) en settle_s, c'est NoVisibleEffect."""

        def run() -> str:
            if prepare is not None:
                prepare()
                time.sleep(0.05)
            before = self._fingerprint()
            fn()
            deadline = time.monotonic() + settle_s
            while True:
                time.sleep(0.06)
                worker.check_cancelled()  # arrêt d'urgence, délai dépassé, entrée de Julien pendant une activation
                if self._fingerprint() != before:
                    return "changement visible"
                if time.monotonic() >= deadline:
                    raise NoVisibleEffect("aucun effet visible : l'application ignore probablement ces messages")

        return run

    # --- Clic ---------------------------------------------------------------------------------------------------
    def click(self, spec: Any, button: str = "left", count: int = 1) -> Outcome:
        kind, target = self.resolve(spec)
        if kind == "point":
            return self.click_point(*target, button=button, count=count)
        return self.click_element(target, button=button, count=count)

    def click_element(self, e: snapshot.Element, button: str = "left", count: int = 1) -> Outcome:
        view = self.view
        fw = framework_of(e, view)
        role = e.role
        p = e.props
        what = f"clic sur {e.describe()}"
        if p.get("enabled") is False:
            raise ActionError(f"{e.describe()} est désactivé.")
        self._refuse_shell_view(e, dangerous=button == "right" or count >= 2)
        chain: list[tuple[str, Callable[[], Any]]] = []
        hwnd, cls = self._element_hwnd_class(e)
        winforms = cls.startswith("windowsforms")
        msaa = self._msaa_item(e, fw)
        page = self._page_if_free()

        if button == "right":
            chain.append(("menu contextuel UIA", lambda: self.auto.show_context_menu(e.node.element)))
            if page is not None:
                chain.append(("DevTools", lambda: self._cdp_click(page, *self._point_of(e), "right")))
            if fw not in NO_MOUSE_MESSAGES:
                chain.append(
                    ("clic droit par messages", self._watched(lambda: self._post_click(*self._point_of(e), "right")))
                )
            return self._try_or_foreground(chain, what, lambda: realinput.click(*self._point_of(e), "right"))
        if count >= 2:
            if p.get("has_invoke") and role in ITEM_ROLES and not msaa:
                chain.append(("UIA Invoke (ouvrir)", lambda: self.auto.invoke(e.node.element)))
            if page is not None:
                chain.append(("DevTools", lambda: self._cdp_click(page, *self._point_of(e), "left", 2)))
            if fw not in NO_MOUSE_MESSAGES:
                chain.append(
                    (
                        "double-clic par messages",
                        self._watched(lambda: self._post_click(*self._point_of(e), "left", 2)),
                    )
                )
            return self._try_or_foreground(
                chain, what.replace("clic", "double-clic"), lambda: realinput.click(*self._point_of(e), "left", 2)
            )

        if hwnd and methods._is_class(hwnd, methods.BUTTON_CLASSES):
            kind = w.style(hwnd) & methods.BS_TYPEMASK
            if not winforms or kind in (methods.BS_PUSHBUTTON, methods.BS_DEFPUSHBUTTON):
                chain.append(("BN_CLICKED", lambda: methods.win32_button_click(hwnd)))
        if role == "ListItem" and fw in ("Win32", "WinForms"):
            chain += self._win32_item_chain(e)
        if role == "TabItem" and fw in ("Win32", "WinForms"):
            tabs = self._ancestor_hwnd(e, methods.TAB_CLASSES)
            index = self._sibling_index(e, "TabItem")
            if tabs and index is not None:
                chain.append(("TCM_SETCURFOCUS", lambda: methods.win32_tab_select(tabs, index)))
        if role == "MenuItem" and fw == "Win32" and not p.get("has_expand"):
            chain.append(("WM_COMMAND menu", lambda: methods.win32_menu_command(self.hwnd, e.name)))
        if role == "CheckBox" and p.get("has_toggle"):
            chain.append(("UIA Toggle", lambda: self.auto.toggle(e.node.element)))
        if role in ITEM_ROLES and p.get("has_selection_item") and fw not in ("Win32", "WinForms"):
            chain.append(("UIA Select", lambda: self.auto.select(e.node.element)))
        if p.get("has_expand") and role in ("ComboBox", "MenuItem", "SplitButton", "TreeItem") and not winforms:
            expanded = p.get("expand") == 1
            chain.append(
                ("UIA Expand/Collapse", lambda: (self.auto.collapse if expanded else self.auto.expand)(e.node.element))
            )
        if (
            p.get("has_invoke")
            and not msaa
            and not (hwnd and methods._is_class(hwnd, methods.BUTTON_CLASSES) and fw == "Win32")
        ):
            chain.append(("UIA Invoke", lambda: self.auto.invoke(e.node.element)))
        if p.get("has_toggle") and role != "CheckBox":
            chain.append(("UIA Toggle", lambda: self.auto.toggle(e.node.element)))
        if (
            p.get("has_selection_item")
            and not any(n == "UIA Select" for n, _ in chain)
            and fw not in ("Win32", "WinForms")
        ):
            chain.append(("UIA Select", lambda: self.auto.select(e.node.element)))
        if role in ("Edit", "Document"):
            chain.append(("focus UIA", lambda: self._quiet_focus(e)))
        if p.get("has_legacy") and fw not in ("Win32", "WinForms"):
            # Win32/WinForms : l'action par défaut MSAA d'oleacc fait souvent un VRAI clic (SendInput) sur les onglets,
            # en-têtes et barres d'outils : jamais sans foreground=true.
            chain.append(("UIA Legacy", lambda: self.auto.legacy_default_action(e.node.element)))
        if page is not None:
            chain.append(("DevTools", lambda: self._cdp_click(page, *self._point_of(e))))
        if fw not in NO_MOUSE_MESSAGES:
            chain.append(("clic par messages", self._watched(lambda: self._post_click(*self._point_of(e)))))
        return self._try_or_foreground(chain, what, lambda: realinput.click(*self._point_of(e)))

    def _win32_item_chain(self, e: snapshot.Element) -> list[tuple[str, Callable[[], Any]]]:
        """Élément d'une liste ou d'une liste déroulante Win32/WinForms : par le contrôle qui le porte, à son RANG
        (homonymes distingués), contrôlé par le libellé. Liste déroulante ouverte : l'élément est dans la ComboLBox,
        fenêtre de premier niveau à part, et la liste déroulante propriétaire se retrouve par GetComboBoxInfo."""
        index = self._sibling_index(e, "ListItem")
        drop = self._ancestor_hwnd(e, methods.COMBO_LIST_CLASSES)
        combo = self._ancestor_hwnd(e, methods.COMBO_CLASSES)
        if drop and not combo:
            combo = methods.combo_owning(drop, [self.hwnd, *sorted(self.windows - {self.hwnd, drop})])
        listbox = 0 if drop or combo else self._ancestor_hwnd(e, methods.LISTBOX_CLASSES)
        if combo:
            if index is not None:
                return [("CB_SETCURSEL", lambda: methods.win32_combo_select_index(combo, index, e.name))]
            return [("CB_SETCURSEL", lambda: methods.win32_combo_select(combo, e.name))]  # par libellé, sans homonyme
        if listbox:
            if index is not None:
                return [("LB_SETCURSEL", lambda: methods.win32_listbox_select_index(listbox, index, e.name))]
            return [("LB_SETCURSEL", lambda: methods.win32_listbox_select(listbox, e.name))]
        return []

    def _element_under(self, x: int, y: int, roles: set[str] | None = None) -> snapshot.Element | None:
        """Élément actionnable de la CIBLE sous un point écran, toujours rendu avec ses ancêtres (nœud de l'instantané).

        UIA ElementFromPoint n'est interrogé que si la fenêtre réellement visible à ce point est une fenêtre de la cible
        (sinon il renverrait l'élément de la fenêtre du dessus, éventuellement une autre fenêtre du même programme) ; son
        résultat est retrouvé dans l'instantané par identifiant d'exécution (ancêtres : rôle voulu, liste porteuse), puis
        remonté jusqu'au rôle voulu (un texte dans un bouton). Sinon : test de recouvrement sur l'instantané."""
        wanted = roles or LEAF_ROLES
        view = self._rebuild() if self.acted else self.view
        if w.window_at(x, y) in self.windows:
            try:
                element = self.auto.element_at(x, y)
                props = self.auto.props_of(element)
            except uia.UIAError:
                element, props = None, {}
            if element is not None and int(props.get("pid") or 0) in self.target_pids:
                located = _locate(view, uia.runtime_id(props))
                if located is not None:
                    node, window = located
                    while node is not None and node.control_type not in wanted:
                        node = node.parent
                    if node is not None and node.rect is not None and node.rect.contains(x, y):
                        return snapshot.Element(0, node, 0, window)
        # Recouverte (ou point sans élément exploitable) : test de recouvrement sur l'arbre complet de l'instantané
        # (nœuds non listés compris : un voile modal sans nom couvre ce qui est dessous), surgissantes d'abord.
        hit = self._hit(view, x, y)
        if hit is None:
            return None
        node, window = hit
        while node is not None and node.control_type not in wanted:
            node = node.parent
        if node is None or node.rect is None or not node.rect.contains(x, y):
            return None  # le point tombe sur un voile, un fond ou une zone sans élément de ce rôle
        return snapshot.Element(0, node, 0, window)

    @staticmethod
    def _hit(view: snapshot.View, x: int, y: int) -> tuple[uia.Node, int] | None:
        """Nœud le plus haut sous le point, et sa fenêtre. Fenêtres du haut vers le bas : la PREMIÈRE qui couvre le
        point décide, sans jamais descendre sous une surgissante visible (même sans élément du rôle voulu, même à
        l'arbre illisible). Dans un arbre : voir _hit_in."""
        for window, roots in _roots_top_down(view):
            for root in roots:
                hit = _hit_in(root, x, y)
                if hit is not None:
                    return hit, window
            if window != view.hwnd and _covers(window, x, y):
                return None
        return None

    def click_point(self, x: int, y: int, button: str = "left", count: int = 1) -> Outcome:
        """Clic à un point : l'élément de la cible sous le point s'il est actionnable, sinon DevTools ou messages."""
        view = self.view
        self._refuse_shell_view(point=(x, y), dangerous=button == "right" or count >= 2)
        image_xy = self._screen_to_image(x, y)
        if button == "left":
            element = self._element_under(x, y)
            window_area = max(1, view.info.rect.width * view.info.rect.height)
            if (
                element is not None
                and element.rect is not None
                and element.rect.width * element.rect.height < window_area / 4
            ):
                outcome = self.click_element(element, button, count)
                outcome.line += f" au point {image_xy}"
                return outcome
        chain: list[tuple[str, Callable[[], Any]]] = []
        page = self._page_if_free()
        if page is not None:
            chain.append(("DevTools", lambda: self._cdp_click(page, x, y, button, count)))
        if view.framework not in NO_MOUSE_MESSAGES:
            chain.append(("clic par messages", self._watched(lambda: self._post_click(x, y, button, count))))
        label = {"left": "clic", "right": "clic droit", "middle": "clic milieu"}[button]
        return self._try_or_foreground(
            chain, f"{label} au point {image_xy}", lambda: realinput.click(x, y, button, count)
        )

    # --- DevTools -----------------------------------------------------------------------------------------------
    def _page_if_free(self) -> cdp.Page | None:
        """Page DevTools pour un CLIC : une page occupée (boîte JavaScript ouverte, script long) n'empêche pas de
        cliquer par UI Automation — c'est justement ainsi qu'on ferme une boîte « OK ». Saisie et touches, elles,
        laissent remonter PageBusy (refus avant toute activation brève)."""
        try:
            return self.get_page()
        except cdp.PageBusy as error:
            self.notes.append(f"DevTools ignoré : {_short(error)}")
            return None

    def _cdp_click(self, page: cdp.Page, x: int, y: int, button: str = "left", count: int = 1) -> str:
        cx, cy = self._css_point(page, x, y)
        page.click(cx, cy, button, count)
        return f"css {cx:.0f},{cy:.0f}"

    def _web_viewport(self) -> w.Rect:
        """Zone de rendu de la page à l'écran (fenêtre Chrome_RenderWidgetHostHWND, à défaut la zone cliente)."""
        content = next((c for c in w.enum_children(self.hwnd) if w.class_name(c) == "Chrome_RenderWidgetHostHWND"), 0)
        return w.window_rect(content) if content else w.client_rect_screen(self.hwnd)

    def _css_point(self, page: cdp.Page, x: int, y: int) -> tuple[float, float]:
        """Point écran → coordonnées CSS de la page (zone de rendu Chromium, devicePixelRatio zoom compris)."""
        rect = self._web_viewport()
        try:
            dpr = float(page.evaluate("window.devicePixelRatio") or 0) or (w.GetDpiForWindow(self.hwnd) or 96) / 96
        except cdp.CDPError:
            dpr = (w.GetDpiForWindow(self.hwnd) or 96) / 96
        return (x - rect.left) / dpr, (y - rect.top) / dpr

    @staticmethod
    def _cdp_focus_at(page: cdp.Page, cx: float, cy: float, editable: bool = False) -> None:
        """Focus DOM d'un élément sans clic (un clic DevTools activerait la fenêtre). editable : le champ de saisie qui
        contient le point (le point d'un éditeur tombe souvent sur un enfant non focalisable), focus relu ; sinon le
        texte partirait dans le champ qui avait le focus avant."""
        if not editable:
            script = f"(() => {{ const el = document.elementFromPoint({cx}, {cy}); if (!el) return false; el.focus(); return true; }})()"
            if not page.evaluate(script):
                raise cdp.CDPError("aucun élément sous le point")
            return
        script = (
            f"(() => {{ const hit = document.elementFromPoint({cx}, {cy}); if (!hit) return 'none';"
            " const el = hit.closest('input, textarea, [contenteditable]:not([contenteditable=\"false\"])') || hit;"
            " el.focus(); const a = document.activeElement;"
            " const field = !!a && (a.isContentEditable || a.tagName === 'INPUT' || a.tagName === 'TEXTAREA');"
            " return field && (a === el || el.contains(a)) ? 'ok' : 'unfocused'; })()"
        )
        state = page.evaluate(script)
        if state == "none":
            raise cdp.CDPError("aucun élément sous le point")
        if state != "ok":
            raise cdp.CDPError("aucun champ de saisie ne prend le focus sous le point")

    def _visible_point(self, e: snapshot.Element, refresh: bool = False) -> tuple[int, int] | None:
        """Centre de la partie de l'élément visible dans la page (None : hors de la zone visible)."""
        if refresh:
            with contextlib.suppress(*EXPECTED):
                e.node.props.update(self.auto.refresh(e.node.element))
        rect = e.rect
        if rect is None or e.props.get("offscreen"):
            return None
        visible = rect.intersect(self._web_viewport())
        return visible.center if visible is not None else None

    def _cdp_focus_field(self, page: cdp.Page, e: snapshot.Element) -> None:
        """Focus DOM du champ par sa partie visible ; hors de la zone visible (ou rien de focalisable au point), il est
        ramené à l'écran (UIA ScrollIntoView) puis le focus est réessayé. Échec : refus avec la cause."""
        cause = "hors de la zone visible de la page"
        for attempt in range(2):
            point = self._visible_point(e, refresh=attempt > 0)
            if point is not None:
                try:
                    self._cdp_focus_at(page, *self._css_point(page, *point), editable=True)
                    return
                except cdp.CDPError as error:
                    cause = _short(error)
            if attempt:
                break
            try:
                self.auto.scroll_into_view(e.node.element)
            except EXPECTED as error:
                raise cdp.CDPError(f"{cause} ; ScrollIntoView impossible : {_short(error)}") from None
            self._settle(lambda: self._visible_point(e, refresh=True) is not None)  # position publiée avec retard
        raise cdp.CDPError(f"{cause}, même après ScrollIntoView")

    @staticmethod
    def _cdp_refuse_secret(page: cdp.Page) -> None:
        if page.evaluate("(() => { const a = document.activeElement; return !!a && a.type === 'password'; })()"):
            raise ActionError("Le champ qui a le focus est un mot de passe : je n'y écris pas (règle de sécurité).")

    # --- Texte --------------------------------------------------------------------------------------------------
    def _is_multiline_chromium(self, e: snapshot.Element) -> bool:
        """<textarea> et éditeurs riches sont indiscernables pour UIA (Edit, textbox, multiline) ; UIA Value ne
        prévient pas un éditeur riche → frappes pour tout champ multiligne Chromium."""
        aria = str(e.props.get("aria_properties") or "").lower()
        return framework_of(e, self.view) == "Chromium" and (
            "multiline=true" in aria or e.role in ("Document", "Group", "Custom") or not e.props.get("has_value")
        )

    def _ensure_active(self) -> None:
        if w.root(w.foreground()) in self.windows:
            return
        if not foreground.activate(self.hwnd) or w.root(w.foreground()) not in self.windows:
            holder = foreground.system_foreground()
            if holder:
                raise methods.MethodError(
                    f"Windows refuse d'activer la fenêtre : le premier plan est tenu par {holder} (aucune fenêtre n'a "
                    "le focus). Remède : application lancée par launch avec devtools=true, ou foreground=true"
                )
            raise methods.MethodError("la fenêtre n'a pas pu être activée pour recevoir les frappes")

    def _keystrokes(self, e: snapshot.Element, text: str, replace: bool) -> str:
        """Focus UIA puis caractères WM_CHAR vers la fenêtre de la cible qui a le clavier, par tranches d'activation
        brève (le focus UIA active la fenêtre ; Chromium ne lit le clavier que fenêtre active) : pause de Julien
        attendue, contrôle explicite de l'activation avant de poster, premier plan rendu à chaque échéance.

        Chaque tranche est confirmée par la valeur RELUE du champ (début du texte qu'il contient) : la suite repart de
        là, sans doublon. Champ illisible : rien n'est tapé (MethodError). Texte qui n'a pas pu être tapé en entier et
        relu : Unconfirmed (l'étape échoue, rien n'est rejoué par-dessus, l'action s'arrête là)."""
        chromium = self.view.framework == "Chromium"
        started = time.perf_counter()
        before = self._read_value(e)
        if before is None:
            raise methods.MethodError(
                "valeur du champ illisible : la frappe ne pourrait pas être relue, rien n'a été tapé"
            )

        def prepare(first: bool) -> int:
            """Met la cible en état de recevoir le clavier ; renvoie la fenêtre qui le reçoit. Aux tranches suivantes,
            seulement la réactiver : elle garde focus et curseur (un nouveau focus pourrait tout sélectionner)."""
            foreground.mark_own_input()
            if first:
                self.auto.set_focus(e.node.element)
            if chromium:
                self._ensure_active()
            if first:
                if replace:
                    try:
                        self.auto.select_all_text(e.node.element)
                    except EXPECTED:
                        if chromium:
                            raise methods.MethodError("sélection du texte existant impossible") from None
                        methods.post_combo(methods.keyboard_target(self.hwnd, self.windows), [0x41], [0x11])
                else:
                    with contextlib.suppress(*EXPECTED):
                        self.auto.caret_to_end(e.node.element)
                time.sleep(0.03)
            return methods.keyboard_target(self.hwnd, self.windows)

        original = methods.norm_text(before)
        if not text:  # vider : sélection puis Suppr, dans une seule activation brève d'au plus methods.SLICE_S
            if not original:
                return "champ déjà vide, aucune activation"
            with foreground.brief_activation(self.hwnd, self.windows) as activation:
                begun = time.monotonic()
                target = prepare(True)
                activation.check("rien n'a été effacé, l'action est annulée")
                methods.post_key(target, methods.VK_DELETE)
                # Relu jusqu'à l'échéance de la tranche seulement : au-delà, la relecture finale (_verify_text) suit,
                # premier plan rendu.
                remaining = begun + methods.SLICE_S - methods.GIVE_BACK_S - time.monotonic()
                self._settle(lambda: self._text_done(self._read_value(e), before, text, replace), max_s=remaining)
            return f"Suppr, activation brève {round((time.perf_counter() - started) * 1000)} ms"

        # Remplacement d'un contenu : effacé (Suppr) et relu vide avant tout caractère, sans quoi un texte qui commence
        # comme lui serait indiscernable de « rien n'a encore été traité ».
        cleared = [not (replace and original)]

        def read() -> str:
            try:
                value = self._read_value(e)
            except EXPECTED as error:
                raise methods.Unreadable(_short(error)) from None
            if value is None:
                raise methods.Unreadable("valeur du champ illisible")
            return methods.norm_text(value)

        def prefix(value: str) -> int | None:
            """Début du texte relu dans le champ ; -1 : contenu d'origine encore là ; None : autre chose."""
            if not cleared[0]:
                if value.strip() and value == original:
                    return -1
                cleared[0] = True
                if not value.strip():
                    return 0
            return methods.typed_prefix(value, text, before, append=not replace)

        # Tranches d'activation d'au plus 0,5 s, premier plan rendu et pause de Julien attendue entre chacune,
        # interruption à sa première frappe ou à son premier clic ; jamais un caractère renvoyé.
        typed = methods.type_in_slices(
            self.hwnd, self.windows, text, prepare, read, prefix, enter_as_key=chromium, clear=not cleared[0]
        )
        if not typed.complete:
            raise Unconfirmed(
                f"texte partiellement tapé : {typed.detail()}. Vérifier la cible (look) avant de compléter"
            )
        return typed.detail()

    @staticmethod
    def _norm_text(value: str | None) -> str:
        return (value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")

    def _text_done(self, value: str | None, before: str, text: str, replace: bool) -> bool:
        """Remplacer : valeur égale au texte (un champ vidé peut garder un saut de ligne). Ajouter : le texte est
        présent ET la valeur a grandi d'au moins sa longueur (le texte déjà là ne suffit pas) ; un contenu d'avant
        entièrement blanc compte comme vide (un éditeur riche vide, « <p><br></p> », se lit « \\n » et son paragraphe
        vide est rempli par le texte tapé). Dans les deux cas, le champ qui contient exactement le texte à la suite
        du contenu d'avant (paragraphe vide de fin d'un éditeur riche toléré) est aussi un succès."""
        got, want, old = self._norm_text(value), self._norm_text(text), self._norm_text(before)
        if not replace and not old.strip():
            old = ""
        if replace:
            if got == want or (not want and got.strip() == ""):
                return True
        elif want in got and len(got) >= len(old) + len(want):
            return True
        return bool(want) and methods.typed_prefix(value, text, before, append=not replace) == len(want)

    @staticmethod
    def _settle(done: Callable[[], bool], max_s: float = 0.4) -> None:
        """Laisse l'application lire les messages postés : jusqu'à ce que done() soit vrai, au plus max_s (jamais
        au-delà, même pour la première pause : une échéance d'activation brève est respectée)."""
        deadline = time.monotonic() + max_s
        time.sleep(min(0.03, max(0.0, max_s)))
        while time.monotonic() < deadline:
            worker.check_cancelled()
            try:
                if done():
                    return
            except EXPECTED:
                pass
            time.sleep(0.03)

    def _read_value(self, e: snapshot.Element) -> str | None:
        hwnd, cls = self._element_hwnd_class(e)
        if hwnd and methods._is_class(hwnd, methods.EDIT_CLASSES):
            value = w.get_text_message(hwnd)
            if value is not None:
                return value
        try:
            props = self.auto.refresh(e.node.element)
        except uia.UIAError:
            return None
        e.node.props.update(props)
        return props.get("value")

    def _verify_text(
        self, e: snapshot.Element, before: str, text: str, replace: bool, timeout_s: float = 1.2
    ) -> bool | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            value = self._read_value(e)
            if value is None:
                return None
            if self._text_done(value, before, text, replace):
                return True
            time.sleep(0.05)
        return False

    @staticmethod
    def _refuse_secret(props: dict, label: str) -> None:
        if props.get("password"):
            raise ActionError(f"{label} est un champ de mot de passe : je n'y écris pas (règle de sécurité).")

    @staticmethod
    def _refuse_command_surface(e: snapshot.Element) -> None:
        """Barre d'adresse de l'Explorateur ou d'une boîte de fichiers : ce qu'on y tape s'exécute (« cmd » + Entrée).
        Reconnue par les classes des ancêtres UIA, ou à défaut des fenêtres parentes Win32."""
        inside = False
        node: uia.Node | None = e.node
        for _ in range(16):
            if node is None or inside:
                break
            inside = str(node.props.get("class_name") or "") in policy.COMMAND_SURFACE_CLASSES
            node = node.parent
        hwnd = e.node.hwnd
        for _ in range(16):
            if not hwnd or inside:
                break
            inside = w.class_name(hwnd) in policy.COMMAND_SURFACE_CLASSES
            hwnd = w.handle(w.GetParent(hwnd))
        if inside:
            raise ActionError(
                f"{e.describe()} est une barre d'adresse : ce qui y est tapé peut lancer une commande (règle de "
                "sécurité). Choisir le dossier ou le fichier dans la liste, ou passer par le shell."
            )

    def _window_classes(self, e: snapshot.Element | None, point: tuple[int, int] | None) -> tuple[set[str], str]:
        """Classes des fenêtres ancêtres d'un élément (ou de la fenêtre sous un point) et processus de la fenêtre de
        premier niveau. Les classes viennent des nœuds UIA de l'instantané (class_name), puis des fenêtres Win32
        parentes — ainsi la boîte de fichiers et le menu shell sont reconnus même quand l'élément n'a pas de hwnd propre."""
        classes: set[str] = set()
        top = 0
        if e is not None:
            node: uia.Node | None = e.node
            for _ in range(24):
                if node is None:
                    break
                name = str(node.props.get("class_name") or "")
                if name:
                    classes.add(name)
                node = node.parent
            hwnd = e.node.hwnd
        else:
            top = self._window_under(*point)
            hwnd = w.deepest_child_at(top, *point)
        while hwnd:
            classes.add(w.class_name(hwnd))
            top = hwnd
            hwnd = w.handle(w.GetParent(hwnd))
            if len(classes) > 48:
                break
        root = w.root(top) if top else 0
        process = w.process_name(w.window_pid(root)).lower() if root else ""
        if not process and e is not None:
            process = self.view.info.process.lower()
        return classes, process

    def _file_dialog_open(self) -> bool:
        """Une boîte de dialogue de fichiers (#32770 contenant une liste shell) est-elle ouverte dans la cible ? Un menu
        contextuel #32768 posé au-dessus exécuterait un fichier, même si le menu appartient au processus de l'application."""
        for root in self.view.roots.values():
            classes = {str(node.props.get("class_name") or "") for node in root.walk()}
            if FILE_DIALOG_CLASS in classes and any(c in classes for c in SHELL_VIEW_CLASSES):
                return True
        for hwnd in self.windows:
            if w.class_name(hwnd) == FILE_DIALOG_CLASS and any(
                w.class_name(child) in SHELL_VIEW_CLASSES for child in w.enum_children(hwnd)
            ):
                return True
        return False

    def _shell_surface(
        self, e: snapshot.Element | None = None, point: tuple[int, int] | None = None
    ) -> tuple[bool, bool]:
        """(liste d'une boîte de fichiers, menu contextuel shell) pour cet élément ou ce point."""
        classes, process = self._window_classes(e, point)
        in_file_list = FILE_DIALOG_CLASS in classes and any(c in classes for c in SHELL_VIEW_CLASSES)
        in_shell_menu = SHELL_MENU_CLASS in classes and (process == "explorer.exe" or self._file_dialog_open())
        return in_file_list, in_shell_menu

    def _refuse_shell_view(
        self, e: snapshot.Element | None = None, *, point: tuple[int, int] | None = None, dangerous: bool = False
    ) -> None:
        """Refuse les gestes qui, dans une boîte de fichiers ou un menu contextuel du shell, exécuteraient un fichier.
        - menu contextuel shell (#32768 de l'Explorateur ou au-dessus d'une boîte de fichiers) : TOUT geste refusé,
          car une entrée s'y déclenche au simple clic ;
        - liste d'une boîte de fichiers (sous SHELLDLL_DefView ou DirectUIHWND d'une #32770) : seuls les gestes
          « dangereux » (clic droit, double-clic, touche « applications », Maj+F10) sont refusés ; le clic simple de
          sélection, le nom de fichier et les boutons Ouvrir/Enregistrer restent permis."""
        in_file_list, in_shell_menu = self._shell_surface(e, point)
        if in_shell_menu:
            raise ActionError(
                "Menu contextuel du shell : une entrée y exécute ou supprime un fichier au simple clic (règle de "
                "sécurité). Fermer le menu (Échap) ; pour agir sur un fichier, passer par le shell de Claude Code."
            )
        if in_file_list and dangerous:
            raise ActionError(
                "Boîte de fichiers : clic droit, double-clic, touche « applications » et Maj+F10 y exécuteraient un "
                "fichier (règle de sécurité). Clic simple pour sélectionner, puis le bouton Ouvrir ou Enregistrer ; "
                "pour lancer un fichier, passer par le shell de Claude Code."
            )

    def write(self, spec: Any, text: str, replace: bool) -> Outcome:
        if spec is None or spec == "":
            return self._write_focused(text, replace)
        return self.write_element(self.resolve_element(spec, TEXT_ROLES, "champ de saisie"), text, replace)

    def write_element(self, e: snapshot.Element, text: str, replace: bool) -> Outcome:
        verb = "texte remplacé" if replace else "texte saisi"
        p = e.props
        self._refuse_secret(p, e.describe())
        self._refuse_command_surface(e)
        if p.get("readonly"):
            raise ActionError(f"{e.describe()} est en lecture seule : rien n'a été écrit.")
        if p.get("enabled") is False:
            raise ActionError(f"{e.describe()} est désactivé.")
        fw = framework_of(e, self.view)
        hwnd, cls = self._element_hwnd_class(e)
        before = self._read_value(e) or ""
        chain: list[tuple[str, Callable[[], Any]]] = []
        page = self.get_page()
        if hwnd and methods._is_class(hwnd, methods.EDIT_CLASSES):
            chain.append(("EM_REPLACESEL", lambda: methods.win32_edit_replace(hwnd, text, append=not replace)))
        if page is not None and fw == "Chromium":
            chain.append(("DevTools insertText", lambda: self._cdp_write(page, e, text, replace)))
        multiline_chromium = fw == "Chromium" and self._is_multiline_chromium(e)
        # DevTools disponible : jamais de frappes (elles demandent une activation brève, que devtools=true évite).
        keystrokes = page is None
        if multiline_chromium and keystrokes:
            chain.append(("frappes", lambda: self._keystrokes(e, text, replace)))
        if p.get("has_value") and not multiline_chromium:

            def set_value() -> None:
                current = "" if replace else (self._read_value(e) or "")
                self.auto.set_value(e.node.element, current + text)

            chain.append(("UIA Value", set_value))
        if p.get("has_legacy") and fw != "Chromium":
            chain.append(
                (
                    "UIA Legacy Value",
                    lambda: self.auto.legacy_set_value(
                        e.node.element, ("" if replace else (self._read_value(e) or "")) + text
                    ),
                )
            )
        if fw not in NO_KEYBOARD_MESSAGES and not multiline_chromium and keystrokes:
            chain.append(("frappes", lambda: self._keystrokes(e, text, replace)))
        outcome = self._try_or_foreground(
            chain, f"{verb} dans {e.describe()}", lambda: self._real_write(e, text, replace)
        )
        verified = self._verify_text(e, before, text, replace, timeout_s=1.5 if fw == "Chromium" else 1.2)
        if verified is False:
            outcome.ok = False
            outcome.line += f" — ÉCHEC À LA RELECTURE : valeur actuelle de {len(self._read_value(e) or '')} caractères"
        elif verified:
            outcome.verified = True
            outcome.line += " — relu ✓"
        return outcome

    def _cdp_write(self, page: cdp.Page, e: snapshot.Element, text: str, replace: bool) -> None:
        self._cdp_focus_field(page, e)
        self._cdp_refuse_secret(page)
        if replace:
            page.evaluate(SELECT_ALL_JS)
            if not text:
                page.key("Delete", "Delete", 0x2E)
        else:
            page.evaluate(
                "(() => { const a = document.activeElement;"
                " if (a && a.setSelectionRange) a.setSelectionRange(a.value.length, a.value.length); })()"
            )
        if text:
            page.insert_text(text)

    def _real_write(self, e: snapshot.Element, text: str, replace: bool) -> None:
        realinput.click(*self._point_of(e))
        time.sleep(0.05)
        if replace:
            realinput.press(keys.parse("ctrl+a"))
            realinput.press(keys.parse("delete"))
        else:
            realinput.press(keys.parse("ctrl+end"))
        realinput.type_text(text)

    def _focused_target_element(self) -> snapshot.Element | None:
        """Élément qui a le focus DANS LA CIBLE (jamais le focus système quand c'est une fenêtre de Julien)."""
        if w.root(w.foreground()) in self.windows:
            focused = self.auto.focused()
            if focused is not None:
                props = self.auto.props_of(focused)
                if int(props.get("pid") or 0) in self.target_pids:
                    return snapshot.Element(0, uia.Node(focused, 0, props), 0, self.hwnd)
        view = self._rebuild()
        for e in view.elements:
            if e.props.get("focused") and e.role in TEXT_ROLES:
                return self._shown_id(e)
        hwnd = methods.keyboard_target(self.hwnd, self.windows)
        if hwnd != self.hwnd:
            for e in view.elements:
                if e.node.hwnd == hwnd:
                    return self._shown_id(e)
        return None

    def _write_focused(self, text: str, replace: bool) -> Outcome:
        """Saisie sans cible : seulement dans le champ qui a le focus DANS la cible, relu ; sinon refus."""
        page = self.get_page()
        verb = "texte remplacé" if replace else "texte saisi"
        if page is not None:

            def via_cdp() -> str:
                self._cdp_refuse_secret(page)
                if replace:
                    page.evaluate(SELECT_ALL_JS)
                page.insert_text(text)
                return ""

            return self._try([("DevTools insertText", via_cdp)], f"{verb} dans l'élément qui a le focus")
        element = self._focused_target_element()
        if element is None:
            raise ActionError(
                f"{verb} : aucun champ de la fenêtre n'a le focus (ou il est inconnu) : préciser target (nom ou [N])."
            )
        return self.write_element(element, text, replace)

    # --- Touches ------------------------------------------------------------------------------------------------
    def press(self, spec: str, target: Any = None) -> Outcome:
        combos = keys.parse_sequence(spec)
        if not combos:
            raise ActionError("Aucune touche.")
        printable = any(c.char and c.char.isprintable() and not c.modifiers for c in combos)
        element = None
        if target not in (None, ""):
            element = self.resolve_element(target, TEXT_ROLES | LEAF_ROLES, "élément")
        page = self.get_page()
        fw = self.view.framework
        if page is not None:
            if element is not None:
                self._cdp_focus_at(page, *self._css_point(page, *self._point_of(element)))
            if printable:
                self._cdp_refuse_secret(page)
            for combo in combos:
                key, code = self._cdp_names(combo)
                if combo.key == 0x0D and not combo.modifiers:
                    char = "\r"  # sans caractère, Entrée ne valide ni ne crée de ligne dans la page
                elif combo.char and not combo.modifiers and combo.char.isprintable():
                    char = combo.char
                else:
                    char = None
                page.key(key, code, combo.key, combo.cdp_modifiers, text=char)
            return Outcome(True, f"touches « {spec} » (DevTools)", "DevTools")
        focused = element or self._focused_target_element()
        if focused is not None:
            self._refuse_command_surface(focused)  # même une touche seule : Entrée y exécuterait ce qui est tapé
            context_menu_key = any(c.key == VK_APPS for c in combos) or any(
                c.key == VK_F10 and VK_SHIFT in c.modifiers for c in combos
            )  # touche « applications » ou Maj+F10 : ouvrent le menu contextuel (fichier exécutable au clic)
            self._refuse_shell_view(focused, dangerous=context_menu_key)
            if printable:
                self._refuse_secret(focused.props, focused.describe())
        shortcut = any(set(c.modifiers) - {0x10} for c in combos)
        real = lambda: [realinput.press(c) for c in combos]  # noqa: E731
        # Raccourcis (Ctrl/Alt/Win) : WPF/UWP les ignorent par messages, Chromium lit l'état réel du clavier.
        if (shortcut and (fw in UIA_ONLY or fw in ("Chromium", "Tk"))) or fw in NO_KEYBOARD_MESSAGES:
            hint = " (ou relancer l'application par launch avec devtools=true)" if fw == "Chromium" else ""
            try:
                return self._foreground_or_fail(f"touches « {spec} »", real)
            except NeedsForeground as error:
                raise NeedsForeground(f"{error}{hint}") from None
        sent: list[str] = []

        def prepare() -> None:
            if element is not None and (fw == "Chromium" or not self.auto.refresh(element.node.element).get("focused")):
                foreground.mark_own_input()
                self.auto.set_focus(element.node.element)
            if fw == "Chromium":
                if element is None:
                    self._focus_web_content()
                self._ensure_active()

        def run() -> str:
            if fw == "Chromium" or element is not None:
                with foreground.brief_activation(self.hwnd, self.windows) as activation:

                    def checked() -> None:
                        prepare()
                        activation.check("rien n'a été envoyé, l'action est annulée")

                    detail = self._watched(lambda: self._post_combos(combos, fw, sent), checked)()
            else:
                detail = self._watched(lambda: self._post_combos(combos, fw, sent))()
            return f"{', '.join(sorted(set(sent)))} ; {detail}"

        return self._try_or_foreground([("messages", run)], f"touches « {spec} »", real)

    def _post_combos(self, combos: list[keys.Combo], fw: str, sent: list[str]) -> None:
        target_hwnd = methods.keyboard_target(self.hwnd, self.windows)
        for combo in combos:
            if combo.modifiers and fw != "Chromium":
                methods.post_combo(target_hwnd, [combo.key], list(combo.modifiers))
                sent.append("état clavier simulé")
            elif combo.modifiers:  # Maj seule en Chromium (les raccourcis Ctrl/Alt ont été refusés plus haut)
                for vk in combo.modifiers:
                    methods.post(target_hwnd, w.WM_KEYDOWN, vk, methods._key_lparam(vk, False))
                methods.post_key(target_hwnd, combo.key)
                for vk in reversed(combo.modifiers):
                    methods.post(target_hwnd, w.WM_KEYUP, vk, methods._key_lparam(vk, True))
                sent.append("messages")
            else:
                methods.post_key(target_hwnd, combo.key)
                sent.append("messages")

    def _focus_web_content(self) -> None:
        """Chromium sans DevTools : le clavier ne va à la page que si un de ses éléments a le focus."""
        if w.root(w.foreground()) in self.windows:
            focused = self.auto.focused()
            if focused is not None:
                props = self.auto.props_of(focused)
                if int(props.get("pid") or 0) in self.target_pids and props.get("control_type") != 50032:
                    return  # un élément de la page (pas la fenêtre elle-même) a déjà le focus
        document = next((e for e in self.view.elements if e.role == "Document" and e.window == self.hwnd), None)
        if document is not None:
            foreground.mark_own_input()
            with contextlib.suppress(*EXPECTED):
                self.auto.set_focus(document.node.element)

    @staticmethod
    def _cdp_names(combo: keys.Combo) -> tuple[str, str]:
        if combo.key in keys.CDP_NAMES:
            return keys.CDP_NAMES[combo.key]
        if 0x41 <= combo.key <= 0x5A:
            letter = chr(combo.key)
            return (letter if 0x10 in combo.modifiers else letter.lower()), f"Key{letter}"
        if 0x30 <= combo.key <= 0x39:
            return chr(combo.key), f"Digit{chr(combo.key)}"
        if 0x70 <= combo.key <= 0x87:
            return f"F{combo.key - 0x6F}", f"F{combo.key - 0x6F}"
        return combo.char or "", ""

    # --- Choix, cases, dépliage ---------------------------------------------------------------------------------
    def select(self, spec: Any, option: str | None) -> Outcome:
        e = self.resolve_element(spec, CHOICE_ROLES, "liste ou option")
        if not option:
            return self.click_element(e)
        fw = framework_of(e, self.view)
        hwnd, cls = self._element_hwnd_class(e)
        what = f"« {option} » choisi dans {e.describe()}"
        if hwnd and methods._is_class(hwnd, methods.COMBO_CLASSES):
            return self._try([("CB_SETCURSEL", lambda: methods.win32_combo_select(hwnd, option))], what)
        if hwnd and methods._is_class(hwnd, methods.LISTBOX_CLASSES):
            return self._try([("LB_SETCURSEL", lambda: methods.win32_listbox_select(hwnd, option))], what)
        item = self._find_item(e, option)
        if item is None and fw == "Chromium" and e.role == "ComboBox":
            return self._select_by_typing(e, option)
        opened = False
        keep_open = False
        try:
            if item is None and e.props.get("has_expand"):
                windows_before = set(self.view.windows)
                self.auto.expand(e.node.element)
                opened = True
                deadline = time.monotonic() + 1.5
                while item is None and time.monotonic() < deadline:
                    worker.check_cancelled()
                    time.sleep(0.15)
                    self._rebuild()
                    item = self._find_item(e, option, popups_after=windows_before)
            if item is None:
                if e.props.get("has_value") and not e.props.get("readonly"):
                    outcome = self._try([("UIA Value", lambda: self.auto.set_value(e.node.element, option))], what)
                    if self._value_is(e, option):
                        outcome.verified = True
                        outcome.line += " — relu ✓"
                    else:
                        outcome.ok = False
                        outcome.line += " — ÉCHEC À LA RELECTURE"
                    return outcome
                raise ActionError(f"Option « {option} » introuvable dans {e.describe()}.")
            outcome = self.click_element(item)
            keep_open = e.role != "ComboBox"  # arbre ou liste dépliés pour atteindre l'option : laissés tels quels
        except worker.Cancelled:
            opened = False  # arrêt d'urgence ou appel abandonné : plus aucune action, même pas le repli
            raise
        finally:
            # Une liste déroulante ouverte reste une surgissante au-dessus des fenêtres de Julien (un clic de sa part y
            # choisirait une option) : repliée sur tout chemin, échec compris.
            if opened and not keep_open:
                self._collapse_if_open(e)
        if self._selection_confirmed(e, item):
            outcome.verified = True
            outcome.line += " — relu ✓"
        else:
            outcome.ok = False
            outcome.line += f" — ÉCHEC À LA RELECTURE : {e.describe()} n'affiche pas « {snapshot.clip(item.name, 40)} »"
        return outcome

    def _collapse_if_open(self, e: snapshot.Element) -> bool:
        """Replie l'élément s'il est encore déplié (relu en direct) ; une erreur de repli n'efface pas celle de l'action."""
        with contextlib.suppress(*EXPECTED):
            if self.auto.refresh(e.node.element).get("expand") == 1:
                self.auto.collapse(e.node.element)
                return True
        return False

    def _selection_confirmed(self, container: snapshot.Element, item: snapshot.Element, timeout_s: float = 0.6) -> bool:
        """Relecture côté CONTENEUR (valeur de la liste déroulante), ou sélection de l'élément pour une liste."""
        chromium = framework_of(container, self.view) == "Chromium"
        deadline = time.monotonic() + (timeout_s * 2 if chromium else timeout_s)
        want = snapshot.norm(item.name).lower()
        combo_with_value = container.role == "ComboBox" and container.props.get("has_value")
        while True:
            try:
                value = self.auto.refresh(container.node.element).get("value") if combo_with_value else None
                selected = self.auto.refresh(item.node.element).get("selected")
            except EXPECTED:
                value, selected = None, None
            if combo_with_value and value and snapshot.norm(str(value)).lower() == want:
                return True
            if not combo_with_value and selected:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _select_by_typing(self, e: snapshot.Element, option: str) -> Outcome:
        """<select> Chromium : ses options ne sont pas exposées et sa liste ne s'ouvre pas en arrière-plan. Focus puis
        frappe du libellé : la recherche au clavier du <select> choisit l'option et émet `change` comme un humain."""
        page = self.get_page()
        chain: list[tuple[str, Callable[[], Any]]] = []
        if page is not None:
            chain.append(("DevTools", lambda: self._cdp_select(page, e, option)))

        def typing() -> str:
            started = time.perf_counter()
            with foreground.brief_activation(self.hwnd, self.windows) as activation:
                foreground.mark_own_input()
                self.auto.set_focus(e.node.element)
                self._ensure_active()
                time.sleep(0.03)
                activation.check("rien n'a été tapé, l'action est annulée")
                methods.post_chars(methods.keyboard_target(self.hwnd, self.windows), option)
                self._settle(lambda: snapshot.norm(self._read_value(e) or "").lower() == option.strip().lower())
            return f"activation brève {round((time.perf_counter() - started) * 1000)} ms"

        if page is None:  # DevTools disponible : jamais de frappes (activation brève, que devtools=true évite)
            chain.append(("frappes", typing))
        what = f"« {option} » choisi dans {e.describe()}"
        outcome = self._try(chain, what)
        if self._value_is(e, option):
            outcome.verified = True
            outcome.line += " — relu ✓"
            return outcome
        outcome.ok = False
        outcome.line += (
            " — ÉCHEC À LA RELECTURE : relancer avec foreground=true (accord de Julien) ou lancer l'application avec "
            "devtools=true"
        )
        return outcome

    def _value_is(self, e: snapshot.Element, expected: str, timeout_s: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            value = self._read_value(e)
            if value is not None and snapshot.norm(value).lower() == expected.strip().lower():
                return True
            time.sleep(0.05)
        return False

    def _cdp_select(self, page: cdp.Page, e: snapshot.Element, option: str) -> None:
        x, y = self._point_of(e)
        cx, cy = self._css_point(page, x, y)
        wanted = json.dumps(option.strip().lower())
        script = (
            f"(() => {{ const el = document.elementFromPoint({cx}, {cy});"
            " if (!el || el.tagName !== 'SELECT') return false;"
            f" const hits = [...el.options].filter(o => o.text.trim().toLowerCase() === {wanted});"
            " if (hits.length !== 1) return false;"
            " el.value = hits[0].value; el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true})); return true; })()"
        )
        if not page.evaluate(script):
            raise cdp.CDPError("option introuvable (ou en double) dans le <select>")

    def _find_item(
        self, container: snapshot.Element, option: str, popups_after: set[int] | None = None
    ) -> snapshot.Element | None:
        """Option d'une liste, par niveaux : d'abord le sous-arbre du conteneur (options repliées ou hors écran
        comprises), puis — liste dépliée — les seules fenêtres surgissantes APPARUES depuis le dépliage (jamais une
        autre liste de la fenêtre). WPF expose la même option dans les deux, sous deux identifiants distincts : chaque
        niveau est donc examiné seul, une correspondance exacte l'emportant sur une partielle. Doublons exacts ou
        correspondance partielle ambiguë dans un niveau : refus."""
        view = self.view
        rid = uia.runtime_id(container.props)
        root = next((e.node for e in view.elements if rid and uia.runtime_id(e.props) == rid), container.node)
        roles = ITEM_ROLES | {"MenuItem"}
        tiers = [
            [
                snapshot.Element(0, node, 0, self.hwnd)
                for node in root.walk()
                if node is not root and node.control_type in roles and node.name
            ]
        ]
        if popups_after is not None:
            tiers.append([e for e in view.elements if e.window not in popups_after and e.role in roles])
        found: list[tuple[snapshot.Element, bool, str]] = []
        for tier in tiers:
            unique: dict[tuple, snapshot.Element] = {}
            for e in tier:
                unique.setdefault(uia.runtime_id(e.props) or (id(e.node),), e)
            candidates = list(unique.values())
            names = [snapshot.norm(e.name) for e in candidates]
            if not candidates:
                continue
            try:
                index, partial = methods.match_option(names, option)
            except methods.AmbiguousOption as error:
                raise ActionError(str(error)) from None
            except methods.MethodError:
                continue
            found.append((candidates[index], partial, names[index]))
            if not partial:
                break
        if not found:
            return None
        element, partial, name = next((f for f in found if not f[1]), found[0])
        if partial:
            self.notes.append(f"correspondance partielle : « {name} »")
        return element

    def set_checked(self, spec: Any, state: bool | None) -> Outcome:
        e = self.resolve_element(spec, CHECK_ROLES, "case à cocher")  # resolve relit déjà l'état en direct
        if e.role == "RadioButton":
            if state is False:
                raise ActionError(f"{e.describe()} : un bouton radio ne se décoche pas, choisir une autre option.")
            if e.props.get("selected") or e.props.get("toggle") == 1:
                return Outcome(True, f"{e.describe()} déjà choisi (relu)", "aucune", verified=True)
            outcome = self.click_element(e)
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                with contextlib.suppress(*EXPECTED):
                    live = self.auto.refresh(e.node.element)
                    if live.get("selected") or live.get("toggle") == 1:
                        outcome.verified = True
                        outcome.line += " — choisi ✓"
                        return outcome
                time.sleep(0.05)
            outcome.ok = False
            outcome.line += " — ÉCHEC À LA RELECTURE"
            return outcome
        if not e.props.get("has_toggle"):
            raise ActionError(f"{e.describe()} n'est pas une case à cocher (pas de motif Toggle) : utiliser click.")
        before = e.props.get("toggle")
        wanted = (1 if before != 1 else 0) if state is None else (1 if state else 0)
        if before == wanted:
            return Outcome(
                True, f"{e.describe()} déjà {'coché' if wanted else 'décoché'} (relu)", "aucune", verified=True
            )
        timeout = 1.2 if framework_of(e, self.view) == "Chromium" else 0.6
        outcome = None
        after = before
        for _ in range(3):  # une case à trois états passe par « mixte » : au plus trois bascules
            outcome = self.click_element(e)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with contextlib.suppress(*EXPECTED):
                    after = self.auto.refresh(e.node.element).get("toggle")
                if after != before:
                    break
                time.sleep(0.05)
            if after == wanted or after == before:
                break
            before = after
        assert outcome is not None
        if after == wanted:
            outcome.verified = True
            outcome.line += f" — {'coché' if wanted else 'décoché'} ✓"
        else:
            outcome.ok = False
            state_name = {0: "décoché", 1: "coché", 2: "mixte"}.get(after, "inconnu")
            outcome.line += f" — ÉCHEC À LA RELECTURE : état {state_name}"
        return outcome

    def expand(self, spec: Any, open_: bool) -> Outcome:
        e = self.resolve_element(spec, EXPAND_ROLES, "élément dépliable")
        word = "déplié" if open_ else "replié"
        if not e.props.get("has_expand"):
            # Jamais de repli sur Invoke : ce serait exécuter la commande de l'élément (bouton, menu feuille).
            raise ActionError(f"{e.describe()} n'est pas dépliable (pas de motif ExpandCollapse) : utiliser click.")
        state = e.props.get("expand")  # relu en direct par resolve
        if state == 3:
            raise ActionError(f"{e.describe()} n'a rien à déplier (élément feuille).")
        if state in (0, 1, 2) and (state != 0) == open_:
            return Outcome(True, f"{e.describe()} déjà {word} (relu)", "aucune", verified=True)
        outcome = self._try(
            [("UIA ExpandCollapse", lambda: (self.auto.expand if open_ else self.auto.collapse)(e.node.element))],
            f"{e.describe()} {word}",
        )
        deadline = time.monotonic() + 0.6
        state = None
        while time.monotonic() < deadline:
            with contextlib.suppress(*EXPECTED):
                state = self.auto.refresh(e.node.element).get("expand")
            if state in (0, 1, 2) and (state != 0) == open_:
                outcome.verified = True
                outcome.line += " — relu ✓"
                return outcome
            time.sleep(0.05)
        if state is None:
            outcome.line += " — état non relu (vérifier sur l'image)"
        else:
            outcome.ok = False
            outcome.line += (
                f" — ÉCHEC À LA RELECTURE : {'feuille' if state == 3 else 'replié' if state == 0 else 'déplié'}"
            )
        return outcome

    def focus(self, spec: Any) -> Outcome:
        e = self.resolve_element(spec, TEXT_ROLES | LEAF_ROLES, "élément")
        return self._try([("focus UIA", lambda: self._quiet_focus(e))], f"focus sur {e.describe()}")

    # --- Défilement ---------------------------------------------------------------------------------------------
    def _scroll_container_at(self, x: int, y: int) -> uia.Node | None:
        """Conteneur défilable de la CIBLE au point : jamais celui d'une fenêtre de Julien qui la recouvre, ni celui
        d'une fenêtre de la cible SOUS une de ses surgissantes."""
        view = self._rebuild() if self.acted else self.view
        node: uia.Node | None = None
        if w.window_at(x, y) in self.windows:
            try:
                element = self.auto.element_at(x, y)
                props = self.auto.props_of(element)
            except uia.UIAError:
                props, element = {}, None
            if element is not None and int(props.get("pid") or 0) in self.target_pids:
                if props.get("has_scroll"):
                    return uia.Node(element, 0, props)
                located = _locate(view, uia.runtime_id(props))
                node = located[0] if located else None
        if node is None:
            hit = self._hit(view, x, y)
            node = hit[0] if hit else None
        while node is not None:
            if node.props.get("has_scroll"):
                return node
            node = node.parent
        return None

    def _main_scroll_container(self, view: snapshot.View) -> uia.Node | None:
        """À défaut de conteneur sous le centre : le plus grand conteneur défilable visible de la fenêtre principale."""
        best, area = None, 0
        for window, roots in _roots_top_down(view):
            if window != self.hwnd:
                continue
            for root in roots:
                for node in root.walk():
                    rect = node.rect
                    if node.props.get("has_scroll") and rect is not None and not node.props.get("offscreen"):
                        if rect.width * rect.height > area:
                            best, area = node, rect.width * rect.height
        return best

    def scroll(self, spec: Any, direction: str, amount: int) -> Outcome:
        direction = (direction or "down").lower()
        aliases = {"bas": "down", "haut": "up", "gauche": "left", "droite": "right"}
        direction = aliases.get(direction, direction)
        if direction not in ("up", "down", "left", "right", "into_view"):
            raise ActionError("direction : up, down, left, right ou into_view.")
        amount = max(1, min(int(amount or 3), 50))
        view = self._rebuild() if self.acted else self.view
        element = None
        point = None
        if spec not in (None, ""):
            kind, target = self.resolve(spec, SCROLL_ROLES)
            if kind == "point":
                point = target
            else:
                element = target
        if direction == "into_view":
            if element is None:
                raise ActionError("into_view demande un élément.")
            return self._try(
                [("UIA ScrollIntoView", lambda: self.auto.scroll_into_view(element.node.element))],
                f"{element.describe()} amené à l'écran",
            )
        container = None
        if element is not None:
            probe: uia.Node | None = element.node
            while probe is not None and container is None:
                if probe.props.get("has_scroll"):
                    container = probe
                probe = probe.parent
        elif point is not None:
            container = self._scroll_container_at(*point)
        else:
            # Sans cible : le conteneur sous le centre de la fenêtre, là où visent aussi DevTools et la molette (pas le
            # premier de l'arbre, souvent un panneau latéral) ; à défaut le plus grand, et la molette vise son centre.
            point = view.info.rect.center
            container = self._scroll_container_at(*point)
            if container is None:
                container = self._main_scroll_container(view)
                if container is not None and container.rect is not None:
                    point = container.rect.center
        where = f" de {snapshot.Element(0, container, 0, self.hwnd).describe()}" if container is not None else ""
        chain: list[tuple[str, Callable[[], Any]]] = []
        vertical = direction in ("up", "down")
        if container is not None:
            step = 4  # ScrollAmount_SmallIncrement
            back = 1  # ScrollAmount_SmallDecrement

            def scroll_uia() -> str:
                before = self.auto.scroll_percent(container.element)
                axis = 1 if vertical else 0
                if before is not None and before[axis] < 0:
                    raise methods.MethodError("ce conteneur ne défile pas dans cette direction")
                for _ in range(amount):
                    worker.check_cancelled()
                    h = back if direction == "left" else step if direction == "right" else 2
                    v = back if direction == "up" else step if direction == "down" else 2
                    self.auto.scroll(container.element, h, v)
                # Chromium publie la nouvelle position avec retard : jusqu'à 1 s avant de conclure.
                deadline = time.monotonic() + 1.0
                after = before
                while time.monotonic() < deadline:
                    after = self.auto.scroll_percent(container.element)
                    if after != before:
                        break
                    time.sleep(0.05)
                if after is None or after[axis] < 0:
                    return "position non relue"
                if after != before:
                    return f"relu ✓ position {after[axis]:.0f} %"
                at_end = after[axis] >= 99.9 if direction in ("down", "right") else after[axis] <= 0.1
                if at_end:
                    return f"relu ✓ déjà en butée ({after[axis]:.0f} %)"
                # Défilement transmis sans effet relu : aucune autre technique par-dessus (double défilement possible).
                raise Unconfirmed(
                    f"défilement {direction} : UIA Scroll transmis, position inchangée ({after[axis]:.0f} %) en 1 s : "
                    "vérifier sur l'image avant de recommencer."
                )

        if point is None:
            point = element.rect.center if element is not None and element.rect else view.info.rect.center
        delta = 120 * amount * (1 if direction in ("up", "left") else -1)
        horizontal = not vertical
        page = self.get_page()
        if page is not None:  # DevTools d'abord : molette de confiance, effet immédiat

            def scroll_cdp() -> None:
                cx, cy = self._css_point(page, *point)
                page.wheel(cx, cy, -delta if not horizontal else 0, -delta if horizontal else 0)

            chain.append(("DevTools molette", scroll_cdp))
        if container is not None:
            chain.append(("UIA Scroll", scroll_uia))
        process = w.process_name(w.window_pid(self.hwnd)).lower()
        if view.framework not in NO_MOUSE_MESSAGES and process not in BROWSERS_WITHOUT_MESSAGE_WHEEL:
            chain.append(
                (
                    "molette par messages",
                    self._watched(lambda: methods.post_wheel(self._window_under(*point), *point, delta, horizontal)),
                )
            )
        return self._try_or_foreground(
            chain, f"défilement {direction} ×{amount}{where}", lambda: realinput.wheel(*point, delta, horizontal)
        )

    # --- Menus --------------------------------------------------------------------------------------------------
    def menu(self, path: str) -> Outcome:
        parts = [p.strip() for p in path.replace("/", ">").split(">") if p.strip()]
        if not parts:
            raise ActionError("Chemin de menu vide.")
        if methods.win32_menu_items(self.hwnd):
            try:
                return self._try(
                    [("WM_COMMAND menu", lambda: methods.win32_menu_command(self.hwnd, path))], f"menu « {path} »"
                )
            except ActionError:
                pass
        outcome = None
        opened: list[snapshot.Element] = []  # niveaux dépliés, à replier si le chemin échoue plus loin
        clicked = False
        try:
            for index, name in enumerate(parts):
                last = index == len(parts) - 1
                roles = {"MenuItem", "Button", "SplitButton"} if index == 0 else {"MenuItem"}
                item = None
                deadline = time.monotonic() + 1.5
                while item is None:
                    worker.check_cancelled()
                    view = self._rebuild() if index > 0 or self.acted else self.view
                    low = name.lower()
                    exact = _unique(
                        [e for e in view.elements if e.role in roles and snapshot.norm(e.name).lower() == low]
                    )
                    if not exact:
                        exact = _unique(
                            [e for e in view.elements if e.role in roles and low in snapshot.norm(e.name).lower()]
                        )
                    if len(exact) > 1:
                        raise ActionError(f"Menu « {name} » ambigu ({', '.join(e.describe() for e in exact[:5])}).")
                    item = self._shown_id(exact[0]) if exact else None
                    if item is None:
                        if time.monotonic() > deadline:
                            raise ActionError(f"Menu « {name} » introuvable (chemin « {path} »).")
                        time.sleep(0.15)
                self._refuse_shell_view(item, dangerous=True)  # jamais un menu contextuel du shell (exécute un fichier)
                if last:
                    clicked = True
                    outcome = self.click_element(item)
                else:
                    fw = framework_of(item, self.view)
                    chain = []
                    if item.props.get("has_expand"):
                        chain.append(("UIA Expand", lambda item=item: self.auto.expand(item.node.element)))
                    if item.props.get("has_invoke") and not self._msaa_item(item, fw):
                        chain.append(("UIA Invoke", lambda item=item: self.auto.invoke(item.node.element)))
                    if item.props.get("has_legacy") and fw not in ("Win32", "WinForms"):
                        chain.append(
                            ("UIA Legacy", lambda item=item: self.auto.legacy_default_action(item.node.element))
                        )
                    self._try(chain, f"menu « {name} » ouvert")
                    opened.append(item)
                    time.sleep(0.2)
        except worker.Cancelled:
            raise  # arrêt d'urgence ou appel abandonné : plus aucune action, même pas le repli
        except BaseException:
            self._close_menus(opened, clicked)
            raise
        assert outcome is not None
        outcome.line = f"menu « {path} » : {outcome.line}"
        return outcome

    def _close_menus(self, opened: list[snapshot.Element], clicked: bool) -> None:
        """Menus dépliés par un chemin qui a échoué : repliés du plus profond au premier (un menu ouvert reste une
        surgissante au-dessus des fenêtres de Julien). Sans motif ExpandCollapse, l'action par défaut le referme, mais
        seulement si le dernier clic n'a pas été tenté : il a pu fermer le menu, qu'elle rouvrirait."""
        for item in reversed(opened):
            if item.props.get("has_expand"):
                self._collapse_if_open(item)
                continue
            native = framework_of(item, self.view) in ("Win32", "WinForms")  # action MSAA par défaut : un vrai clic
            if not clicked and item.props.get("has_legacy") and not native:
                with contextlib.suppress(*EXPECTED):
                    self.auto.legacy_default_action(item.node.element)

    # --- Attente ------------------------------------------------------------------------------------------------
    @staticmethod
    def _unreadable(view: snapshot.View) -> str:
        """Pourquoi un texte absent de cette vue n'y est pas prouvé absent (vide : la vue est complète)."""
        if view.node_count <= 1 or not view.elements:
            return "arbre UI Automation vide"
        if view.truncated:
            return f"arbre UI Automation tronqué ({view.node_count} nœuds lus)"
        if view.web_missing:
            return "contenu web non publié par Chromium"
        return ""

    def present(self, text: str) -> bool | None:
        """Le texte figure-t-il dans la fenêtre (noms, valeurs, titre), en mots entiers (« 4 » ne trouve pas « 42 ») ?
        None : lecture incomplète (arbre vide ou tronqué, contenu web non publié) — ni présent ni absent ; la cause
        est dans self.unreadable."""
        view = self._rebuild()
        pattern = re.compile(rf"(?<!\w){re.escape(snapshot.norm(text))}(?!\w)", re.IGNORECASE)
        sources = [w.window_text(self.hwnd)]
        for e in view.elements:
            sources += [e.name, str(e.props.get("value") or "")]
        self.unreadable = ""
        if any(pattern.search(snapshot.norm(s)) for s in sources if s):
            return True
        self.unreadable = self._unreadable(view)
        return None if self.unreadable else False

    def wait(self, text: str | None, gone: bool, timeout_s: float) -> Outcome:
        timeout_s = max(0.1, min(float(timeout_s or 5), 30))
        if not text:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                worker.check_cancelled()
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            return Outcome(True, f"attente {timeout_s:.1f} s", "sleep")
        start = time.monotonic()
        while True:
            worker.check_cancelled()
            found = self.present(text)
            # Disparu ne se conclut que sur une lecture complète (False) : jamais sur None (arbre tronqué, page non
            # publiée), où le texte peut être là sans être lu.
            if found is not None and found != gone:
                elapsed = time.monotonic() - start
                return Outcome(
                    True, f"« {text} » {'disparu' if gone else 'présent'} après {elapsed:.1f} s", "wait", verified=True
                )
            if time.monotonic() - start > timeout_s:
                if found is None:
                    raise ActionError(
                        f"« {text} » : lecture de la fenêtre incomplète ({self.unreadable}) : vérifier sur l'image."
                    )
                raise ActionError(f"« {text} » toujours {'présent' if gone else 'absent'} après {timeout_s:.0f} s.")
            time.sleep(0.25)

    # --- Glisser et survol : DevTools si disponible, sinon vraie souris -------------------------------------------
    def _point(self, spec: Any) -> tuple[int, int]:
        kind, target = self.resolve(spec)
        return target if kind == "point" else self._point_of(target)

    def drag(self, start: Any, end: Any) -> Outcome:
        x1, y1 = self._point(start)
        x2, y2 = self._point(end)
        page = self.get_page()
        if page is not None:
            a, b = self._css_point(page, x1, y1), self._css_point(page, x2, y2)
            page.drag(*a, *b)
            return Outcome(True, "glisser-déposer (DevTools)", "DevTools")
        return self._foreground_or_fail("glisser-déposer", lambda: realinput.drag(x1, y1, x2, y2))

    def hover(self, spec: Any) -> Outcome:
        x, y = self._point(spec)
        page = self.get_page()
        if page is not None:
            page.hover(*self._css_point(page, x, y))
            return Outcome(True, "survol (DevTools)", "DevTools")
        return self._foreground_or_fail("survol", lambda: realinput.move(x, y))
