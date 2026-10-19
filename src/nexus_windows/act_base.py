"""Socle de l'exécuteur : résolution des cibles ([N], nom, point) relues en direct, chaînes de techniques avec
leur contrat de statut (✓ = ~ ✗), relectures de valeurs, garde-fous communs (mot de passe, barre d'adresse, shell).

Le contrat complet (statuts, cibles, ordre des techniques) est décrit dans actions.py, façade de l'exécuteur.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import comtypes

from . import capture, cdp, foreground, methods, realinput, snapshot, uia, worker
from . import win32 as w

if TYPE_CHECKING:
    from . import keys

UIA_ONLY = {"WPF", "XAML", "UWP", "WinUI", "DirectUI"}
NO_KEYBOARD_MESSAGES = {"XAML", "UWP", "WinUI", "DirectUI", "Tk"}  # WM_CHAR/WM_KEYDOWN postés ignorés (mesuré)
NO_MOUSE_MESSAGES = UIA_ONLY | {"Tk"}  # clics et molette postés ignorés (mesuré)
# Effet d'un clic Chromium constaté au titre ou à l'image avant l'état de l'élément : celui-ci relu encore au plus
# OWN_STATE_S pour le prouver (publication de l'arbre d'accessibilité quelques dizaines de ms après le titre).
OWN_STATE_S = 0.3
BROWSERS_WITHOUT_MESSAGE_WHEEL = {"msedge.exe", "chrome.exe", "brave.exe"}  # Edge ✗ au banc ; Electron ✓
# Contrôles communs Win32 dont les éléments sont servis par le proxy MSAA natif (oleacc) : leur action par défaut
# simule un vrai clic ou double-clic (SendInput).
NATIVE_PROXY_CLASSES = (
    "listbox", "combolbox", "combobox", "systabcontrol32", "syslistview32", "systreeview32", "sysheader32",
    "toolbarwindow32", "rebarwindow32", "msctls_statusbar32", "#32768",
)  # fmt: skip
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
EXPAND_ROLES = {"ComboBox", "TreeItem", "MenuItem", "SplitButton", "Group", "DataItem", "Button"}
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
    """Action par messages sans retour possible et rien n'a changé à l'écran : l'application l'a ignorée.
    still_unchanged() : nouvelle relecture un peu plus tard (vrai si toujours rien : ignorée, pas retardée)."""

    def __init__(self, message: str, still_unchanged: Callable[[], bool] | None = None) -> None:
        super().__init__(message)
        self.still_unchanged = still_unchanged or (lambda: False)


class NoEffect(methods.MethodError):
    """Action UI Automation acceptée par Chromium/CEF mais sans aucun effet constaté (UIA Select d'un onglet React,
    par exemple) : la technique suivante est essayée."""


@dataclass
class Outcome:
    ok: bool
    line: str
    method: str = ""
    attempts: list[str] = field(default_factory=list)
    verified: bool | None = None  # True : effet relu ; None : transmis sans relecture
    already: bool = False  # état voulu (check) déjà vrai avant l'action : rien n'a été envoyé

    @property
    def symbol(self) -> str:
        if not self.ok:
            return "✗"
        if self.already:
            return "="
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
    document = next((n for n in root.walk() if snapshot.is_web_document(n) and _contains(n, x, y)), None)
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


class ExecutorBase:
    def __init__(
        self,
        auto: uia.Automation,
        hwnd: int,
        get_view: Callable[[bool], snapshot.View],
        get_page: Callable[..., cdp.Page | None],
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
        # Document web visé quand la fenêtre en porte plusieurs (Steam : cadre « Steam » et magasin) : sa page
        # DevTools et sa zone de rendu, pas celles du titre de la fenêtre.
        self.document: uia.Node | None = None
        # confirm=true de l'étape en cours (ordre explicite ou oui de Julien) : lève la confirmation de act_confirm.
        self.confirm = False

    if TYPE_CHECKING:
        # Méthodes que les familles (act_*) s'empruntent entre elles, réunies dans actions.Executor : déclarées ici
        # pour le contrôle de types seulement (aucune à l'exécution : l'ordre de résolution reste celui d'Executor).
        # act_confirm
        def _confirm_click(self, e: snapshot.Element, button: str = "left") -> None: ...
        def _confirm_point(self, x: int, y: int, button: str = "left") -> None: ...
        def _confirm_keys(self, combos: list[keys.Combo], spec: str, element: snapshot.Element | None) -> None: ...
        def _confirm_text(self, element: snapshot.Element | None, text: str) -> None: ...
        def _focused_target_element(self) -> snapshot.Element | None: ...
        # act_pointer
        def _point_of(self, e: snapshot.Element) -> tuple[int, int]: ...
        def _window_under(self, x: int, y: int) -> int: ...
        def _post_click(self, x: int, y: int, button: str = "left", count: int = 1) -> int: ...
        def _element_under(self, x: int, y: int, roles: set[str] | None = None) -> snapshot.Element | None: ...
        @staticmethod
        def _hit(view: snapshot.View, x: int, y: int) -> tuple[uia.Node, int] | None: ...
        def _real_write(self, e: snapshot.Element, text: str, replace: bool) -> None: ...

        # act_uia
        def click_element(
            self, e: snapshot.Element, button: str = "left", count: int = 1, close_list: bool = True
        ) -> Outcome: ...
        def _value_is(self, e: snapshot.Element, expected: str, timeout_s: float = 1.0) -> bool: ...

        # act_chromium
        def _click_chromium(self, e: snapshot.Element, page: cdp.Page | None, what: str) -> Outcome: ...
        def _page_if_free(self) -> cdp.Page | None: ...
        def _cdp_click(self, page: cdp.Page, x: int, y: int, button: str = "left", count: int = 1) -> str: ...
        def _css_point(self, page: cdp.Page, x: int, y: int) -> tuple[float, float]: ...
        @staticmethod
        def _cdp_focus_at(page: cdp.Page, cx: float, cy: float, editable: bool = False) -> None: ...
        @staticmethod
        def _cdp_refuse_secret(page: cdp.Page) -> None: ...
        @staticmethod
        def _cdp_names(combo: keys.Combo) -> tuple[str, str]: ...
        def _cdp_write(self, page: cdp.Page, e: snapshot.Element, text: str, replace: bool) -> None: ...
        def _is_multiline_chromium(self, e: snapshot.Element) -> bool: ...
        def _focus_web_content(self) -> None: ...
        def _select_by_typing(self, e: snapshot.Element, option: str) -> Outcome: ...
        def _sim_top(self, e: snapshot.Element | None = None) -> int: ...
        def _sim_activation(
            self, e: snapshot.Element | None = None
        ) -> AbstractContextManager[methods.SimulatedActivation]: ...
        def _take_focus(self, e: snapshot.Element, timeout_s: float = 0.5) -> None: ...
        def _sim_focus(self, e: snapshot.Element) -> str: ...

        # act_win32
        def _element_hwnd_class(self, e: snapshot.Element) -> tuple[int, str]: ...
        def _ancestor_hwnd(self, e: snapshot.Element, classes: tuple[str, ...]) -> int: ...
        @staticmethod
        def _sibling_index(e: snapshot.Element, role: str) -> int | None: ...
        @staticmethod
        def _msaa_item(e: snapshot.Element, fw: str) -> bool: ...
        @staticmethod
        def _win32_combo_of(e: snapshot.Element) -> int: ...
        def _win32_item_chain(self, e: snapshot.Element) -> list[tuple[str, Callable[[], Any]]]: ...
        def _ensure_active(self) -> None: ...
        def _keystrokes(self, e: snapshot.Element, text: str, replace: bool, simulated: bool = False) -> str: ...
        def _post_combos(self, combos: list[keys.Combo], fw: str, sent: list[str], simulated: bool = False) -> None: ...

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
        programme (L'utilisateur peut avoir son propre Edge, son propre document)."""
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
            except methods.ReadBackFailed as error:
                # Transmis puis relu contraire : échec franc, rien d'autre n'est rejoué par-dessus.
                attempts.append(f"{name} ✗ {_short(error)}")
                return Outcome(
                    False, f"{what} ({name} — ÉCHEC À LA RELECTURE : {_short(error)})", name, attempts, False
                )
            except NoVisibleEffect as error:
                attempts.append(f"{name} ✗ {_short(error)}")
                if self.allow_foreground:
                    # Fenêtre gardée devant (dessinée, publiée) : relue une seconde fois. Toujours rien → messages
                    # ignorés, pas retardés : la vraie souris/le vrai clavier autorisés prennent le relais.
                    if error.still_unchanged():
                        continue
                    return Outcome(True, f"{what} ({name} — changement visible, tardif)", name, attempts, None)
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
            live = self._refresh(e.node.element)
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
            screen = frame.to_screen(*point, w.frame_rect(self.hwnd))
            hit = self._hit(self.view, *screen)
            self._aim(hit[0] if hit else None)
            return "point", screen
        ident = None
        if isinstance(spec, int):
            ident = spec
        elif isinstance(spec, str) and (match := snapshot._ID.match(spec)):
            ident = int(match.group(1))
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
            target = self.fresh(element, expected=shown.displayed[ident])
            self._aim(element.node)
            return "element", target
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
                element, note = self._scroll_to_named(str(spec), prefer, error)
        if note:
            self.notes.append(note)
        target = self.fresh(element)
        self._aim(element.node)
        return "element", target

    def _scroll_to_named(
        self, spec: str, prefer: set[str] | None, missing: snapshot.TargetError
    ) -> tuple[snapshot.Element, str]:
        """Nom absent de la partie visible : élément de ce nom exact hors de vue parce que la page ou la liste a défilé,
        amené à l'écran (UIA ScrollIntoView, comme l'utilisateur ferait défiler) puis relu dans un instantané frais."""
        hidden = snapshot.scrolled_away(self.view)
        try:
            element, partial = snapshot.pick(dataclasses.replace(self.view, elements=hidden), spec, prefer)
        except snapshot.AmbiguousTarget as error:
            raise ActionError(f"{error} (hors de vue)") from None
        except snapshot.TargetError:
            raise ActionError(str(missing)) from None
        if partial:  # seulement un nom exact : jamais faire défiler la page vers un homonyme approximatif
            raise ActionError(str(missing))
        rid = uia.runtime_id(element.props)
        try:
            self.auto.scroll_into_view(element.node.element)
        except EXPECTED as error:
            raise ActionError(
                f"{element.describe()} est hors de vue et n'a pas pu y être amené ({_short(error)})."
            ) from None
        for _ in range(10):
            time.sleep(0.05)
            for other in self._rebuild().elements:
                if rid and uia.runtime_id(other.props) == rid:
                    return other, f"{self._shown_id(other).describe()} hors de vue : amené à l'écran (ScrollIntoView)"
        raise ActionError(f"{element.describe()} amené à l'écran mais toujours hors de vue : refaire look.")

    def _web_documents(self) -> list[uia.Node]:
        """Documents web de premier niveau de la fenêtre principale (cadres internes exclus)."""
        root = self.view.roots.get(self.hwnd)
        return snapshot.root_web_documents(root) if root is not None else []

    def _refresh(self, element) -> dict[str, Any]:
        """Propriétés actuelles d'un élément. Fenêtre Chromium recouverte : publication de l'arbre déclenchée
        d'abord (snapshot.flush_web), sans quoi l'effet de la dernière action n'y paraîtrait qu'à la suivante."""
        if self.view.framework == "Chromium":
            snapshot.flush_web(self.auto, self.hwnd)
        return self.auto.refresh(element)

    def _aim(self, node: uia.Node | None) -> None:
        """Retient le document web de la cible si la fenêtre en porte plusieurs (sinon le titre de fenêtre suffit)."""
        self.document = None
        documents = self._web_documents()
        if node is None or len(documents) < 2:
            return
        while node is not None and not snapshot.is_web_document(node):
            node = node.parent
        if node is not None:
            rid = uia.runtime_id(node.props)
            if any(node is d or (rid and uia.runtime_id(d.props) == rid) for d in documents):
                self.document = node

    def _get_page(self) -> cdp.Page | None:
        if self.document is None:
            return self.get_page()
        return self.get_page(snapshot.norm(self.document.name))

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

    def _foreground_or_fail(self, what: str, fn: Callable[[], None]) -> Outcome:
        if not self.allow_foreground:
            raise NeedsForeground(
                f"{what} : impossible en arrière-plan pour {self.view.framework}. Avec l'accord de l'utilisateur, relancer "
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
        pause de l'utilisateur, puis on lui rend le premier plan aussitôt ; le compte rendu le signale."""
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

                    def still_unchanged() -> bool:
                        time.sleep(0.4)
                        return self._fingerprint() == before

                    raise NoVisibleEffect(
                        "aucun effet visible : l'application ignore probablement ces messages", still_unchanged
                    )

        return run

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
            props = self._refresh(e.node.element)
        except uia.UIAError:
            return None
        e.node.props.update(props)
        return props.get("value")

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
