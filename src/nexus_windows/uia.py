"""UI Automation (COM) : arbre d'une fenêtre en un seul aller-retour, motifs d'action en arrière-plan.

Réglages clés :
- AutoSetFocus = False : par défaut UIA donne le focus à l'élément avant chaque motif (Invoke, Toggle…), ce qui
  active la fenêtre cible et la met au premier plan. Désactivé ici.
- ConnectionTimeout / TransactionTimeout courts : une application figée ne bloque pas le serveur 20 s.
- Un seul BuildUpdatedCache(TreeScope_Subtree) par instantané : propriétés et enfants arrivent en un appel.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

import comtypes
import comtypes.client

from . import win32 as w

_mod = comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen import UIAutomationClient as U  # noqa: E402

CONTROL_TYPES: dict[int, str] = {
    getattr(U, name): name[4:-13] for name in dir(U) if name.startswith("UIA_") and name.endswith("ControlTypeId")
}

TreeScope_Element, TreeScope_Children, TreeScope_Subtree = 1, 2, 7
AutomationElementMode_Full = 1

PROPS = {
    "runtime_id": U.UIA_RuntimeIdPropertyId,
    "rect": U.UIA_BoundingRectanglePropertyId,
    "pid": U.UIA_ProcessIdPropertyId,
    "control_type": U.UIA_ControlTypePropertyId,
    "name": U.UIA_NamePropertyId,
    "automation_id": U.UIA_AutomationIdPropertyId,
    "class_name": U.UIA_ClassNamePropertyId,
    "framework": U.UIA_FrameworkIdPropertyId,
    "hwnd": U.UIA_NativeWindowHandlePropertyId,
    "enabled": U.UIA_IsEnabledPropertyId,
    "offscreen": U.UIA_IsOffscreenPropertyId,
    "focused": U.UIA_HasKeyboardFocusPropertyId,
    "focusable": U.UIA_IsKeyboardFocusablePropertyId,
    "password": U.UIA_IsPasswordPropertyId,
    "help": U.UIA_HelpTextPropertyId,
    "accelerator": U.UIA_AcceleratorKeyPropertyId,
    "access_key": U.UIA_AccessKeyPropertyId,
    "value": U.UIA_ValueValuePropertyId,
    "readonly": U.UIA_ValueIsReadOnlyPropertyId,
    "range_value": U.UIA_RangeValueValuePropertyId,
    "toggle": U.UIA_ToggleToggleStatePropertyId,
    "expand": U.UIA_ExpandCollapseExpandCollapseStatePropertyId,
    "selected": U.UIA_SelectionItemIsSelectedPropertyId,
    "legacy_action": U.UIA_LegacyIAccessibleDefaultActionPropertyId,
    "legacy_value": U.UIA_LegacyIAccessibleValuePropertyId,
    "legacy_role": U.UIA_LegacyIAccessibleRolePropertyId,
    "legacy_state": U.UIA_LegacyIAccessibleStatePropertyId,
    "aria_role": U.UIA_AriaRolePropertyId,
    "aria_properties": U.UIA_AriaPropertiesPropertyId,
    "has_invoke": U.UIA_IsInvokePatternAvailablePropertyId,
    "has_value": U.UIA_IsValuePatternAvailablePropertyId,
    "has_toggle": U.UIA_IsTogglePatternAvailablePropertyId,
    "has_selection_item": U.UIA_IsSelectionItemPatternAvailablePropertyId,
    "has_expand": U.UIA_IsExpandCollapsePatternAvailablePropertyId,
    "has_scroll": U.UIA_IsScrollPatternAvailablePropertyId,
    "has_scroll_item": U.UIA_IsScrollItemPatternAvailablePropertyId,
    "has_range": U.UIA_IsRangeValuePatternAvailablePropertyId,
    "has_text": U.UIA_IsTextPatternAvailablePropertyId,
    "has_legacy": U.UIA_IsLegacyIAccessiblePatternAvailablePropertyId,
    "has_window": U.UIA_IsWindowPatternAvailablePropertyId,
}

PATTERNS = {
    "invoke": (U.UIA_InvokePatternId, U.IUIAutomationInvokePattern),
    "value": (U.UIA_ValuePatternId, U.IUIAutomationValuePattern),
    "toggle": (U.UIA_TogglePatternId, U.IUIAutomationTogglePattern),
    "selection_item": (U.UIA_SelectionItemPatternId, U.IUIAutomationSelectionItemPattern),
    "expand": (U.UIA_ExpandCollapsePatternId, U.IUIAutomationExpandCollapsePattern),
    "scroll": (U.UIA_ScrollPatternId, U.IUIAutomationScrollPattern),
    "scroll_item": (U.UIA_ScrollItemPatternId, U.IUIAutomationScrollItemPattern),
    "range": (U.UIA_RangeValuePatternId, U.IUIAutomationRangeValuePattern),
    "text": (U.UIA_TextPatternId, U.IUIAutomationTextPattern),
    "legacy": (U.UIA_LegacyIAccessiblePatternId, U.IUIAutomationLegacyIAccessiblePattern),
    "window": (U.UIA_WindowPatternId, U.IUIAutomationWindowPattern),
    "item_container": (U.UIA_ItemContainerPatternId, U.IUIAutomationItemContainerPattern),
    "virtualized_item": (U.UIA_VirtualizedItemPatternId, U.IUIAutomationVirtualizedItemPattern),
}


class UIAError(RuntimeError):
    pass


@dataclass
class Node:
    element: Any
    depth: int
    props: dict[str, Any]
    children: list[Node] = field(default_factory=list)
    parent: Node | None = None
    truncated: bool = False  # racine d'un arbre coupé à max_nodes : des éléments manquent

    @property
    def control_type(self) -> str:
        return CONTROL_TYPES.get(self.props.get("control_type") or 0, "Unknown")

    @property
    def name(self) -> str:
        return (self.props.get("name") or "").strip()

    @property
    def rect(self) -> w.Rect | None:
        r = self.props.get("rect")
        if not r or len(r) != 4:
            return None
        left, top, width, height = (int(round(v)) for v in r)
        if width <= 0 or height <= 0:
            return None
        return w.Rect(left, top, left + width, top + height)

    @property
    def hwnd(self) -> int:
        return int(self.props.get("hwnd") or 0)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


def _variant(value):
    """Valeurs sentinelles de UIA (« non supporté », « mixte » : pointeurs COM) → None."""
    if value is None or hasattr(value, "QueryInterface"):
        return None
    return value


SELFLAG_TAKEFOCUS = 1  # IAccessible::accSelect : donner le focus à l'élément


class Automation:
    """Client UIA dédié à un thread COM (créé et utilisé sur le même thread)."""

    def __init__(self, auto_set_focus: bool = False, connection_ms: int = 2000, transaction_ms: int = 4000) -> None:
        self.uia = comtypes.client.CreateObject(U.CUIAutomation8, interface=U.IUIAutomation6)
        self.uia.AutoSetFocus = bool(auto_set_focus)
        self.uia.ConnectionTimeout = connection_ms
        self.uia.TransactionTimeout = transaction_ms
        self._cache = self._make_cache(TreeScope_Subtree)
        self._cache_one = self._make_cache(TreeScope_Element)

    def _make_cache(self, scope: int):
        cr = self.uia.CreateCacheRequest()
        for pid in PROPS.values():
            cr.AddProperty(pid)
        cr.TreeScope = scope
        cr.TreeFilter = self.uia.ControlViewCondition
        cr.AutomationElementMode = AutomationElementMode_Full
        return cr

    # --- Lecture ----------------------------------------------------------------------------------------------
    def _props(self, element) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, pid in PROPS.items():
            try:
                value = _variant(element.GetCachedPropertyValue(pid))
            except comtypes.COMError, ValueError:
                continue
            if value is not None:
                out[key] = value
        return out

    def element_from_hwnd(self, hwnd: int):
        try:
            return self.uia.ElementFromHandle(hwnd)
        except comtypes.COMError as error:
            raise UIAError(f"UI Automation cannot see window {hwnd}: {error}") from error

    def tree(self, hwnd: int, max_nodes: int = 4000) -> Node:
        """Arbre « vue contrôle » complet de la fenêtre, en un seul appel inter-processus. Au-delà de max_nodes,
        l'arbre est coupé et sa racine le dit (truncated) : un élément absent n'y est alors pas prouvé absent.

        Liste déroulante Win32/WinForms ouverte dans la fenêtre (w.dropped_combos) : UIA enchaînerait depuis elle sur
        toutes les fenêtres du bureau. L'arbre est alors lu niveau par niveau le long de ses seuls ancêtres (le reste
        en un appel par branche) et la liste déroulante y reste une feuille ; sa liste ouverte (ComboLBox) est lue avec
        sa propre fenêtre, comme toute surgissante. Jamais une fenêtre de premier plan d'un autre processus dans
        l'arbre (fenêtres de l'utilisateur)."""
        pruned = w.dropped_combos(hwnd)
        try:
            root_el = self.uia.ElementFromHandleBuildCache(hwnd, self._cache_children if pruned else self._cache)
        except comtypes.COMError as error:
            raise UIAError(f"UI Automation tree unreadable for {hwnd}: {error}") from error
        state = {"count": 0, "cut": False, "root": hwnd, "pid": 0}
        if pruned:
            root = self._build_pruned(root_el, 0, None, pruned, state, max_nodes)
        else:
            root = self._build(root_el, 0, None, state, max_nodes)
        root.truncated = state["cut"]
        return root

    @property
    def _cache_children(self):
        if getattr(self, "_cache_kids", None) is None:
            self._cache_kids = self._make_cache(TreeScope_Element | TreeScope_Children)
        return self._cache_kids

    @staticmethod
    def _foreign(props: dict[str, Any], state: dict) -> bool:
        """Fenêtre de premier niveau d'un autre processus atteinte par la navigation UIA (jamais à lire)."""
        h = int(props.get("hwnd") or 0)
        if not h or h == state["root"] or w.root(h) != h:
            return False
        if not state["pid"]:
            state["pid"] = w.window_pid(state["root"])
        return w.window_pid(h) != state["pid"]

    @staticmethod
    def _cached_children(element) -> list:
        try:
            kids = element.GetCachedChildren()
        except comtypes.COMError, ValueError:
            return []
        return [kids.GetElement(i) for i in range(kids.Length)] if kids else []  # NULL : aucun enfant

    def _build(
        self, element, depth: int, parent: Node | None, state: dict, max_nodes: int, props: dict | None = None
    ) -> Node:
        state["count"] += 1
        node = Node(element, depth, self._props(element) if props is None else props, parent=parent)
        for kid in self._cached_children(element):
            if state["count"] >= max_nodes:
                state["cut"] = True
                break
            kid_props = self._props(kid)
            if self._foreign(kid_props, state):
                continue
            node.children.append(self._build(kid, depth + 1, node, state, max_nodes, kid_props))
        return node

    def _build_pruned(
        self, element, depth: int, parent: Node | None, pruned: set[int], state: dict, max_nodes: int
    ) -> Node:
        state["count"] += 1
        node = Node(element, depth, self._props(element), parent=parent)
        for kid in self._cached_children(element):
            if state["count"] >= max_nodes:
                state["cut"] = True
                break
            props = self._props(kid)
            h = int(props.get("hwnd") or 0)
            if self._foreign(props, state):
                continue
            if h in pruned:  # liste déroulante ouverte : feuille (ses propriétés seules)
                state["count"] += 1
                node.children.append(Node(kid, depth + 1, props, parent=node))
                continue
            ancestor = bool(h) and any(w.IsChild(h, combo) for combo in pruned)
            try:
                fresh = kid.BuildUpdatedCache(self._cache_children if ancestor else self._cache)
            except comtypes.COMError:
                continue  # branche disparue entre deux lectures
            if ancestor:
                node.children.append(self._build_pruned(fresh, depth + 1, node, pruned, state, max_nodes))
            else:
                node.children.append(self._build(fresh, depth + 1, node, state, max_nodes))
        return node

    def refresh(self, element) -> dict[str, Any]:
        """Propriétés actuelles d'un élément (un aller-retour)."""
        try:
            fresh = element.BuildUpdatedCache(self._cache_one)
        except comtypes.COMError as error:
            raise UIAError(f"Element gone: {error}") from error
        return self._props(fresh)

    def element_at(self, x: int, y: int):
        try:
            return self.uia.ElementFromPointBuildCache(U.tagPOINT(x, y), self._cache_one)
        except comtypes.COMError as error:
            raise UIAError(f"No element at ({x}, {y}): {error}") from error

    def focused(self):
        try:
            return self.uia.GetFocusedElementBuildCache(self._cache_one)
        except comtypes.COMError:
            return None

    def props_of(self, element) -> dict[str, Any]:
        return self._props(element)

    # --- Motifs -----------------------------------------------------------------------------------------------
    def pattern(self, element, name: str):
        pattern_id, interface = PATTERNS[name]
        try:
            unknown = element.GetCurrentPattern(pattern_id)
        except comtypes.COMError as error:
            raise UIAError(f"Pattern {name} unavailable: {error}") from error
        if not unknown:
            raise UIAError(f"Pattern {name} not supported by the element.")
        return unknown.QueryInterface(interface)

    def invoke(self, element) -> None:
        self.pattern(element, "invoke").Invoke()

    def toggle(self, element) -> None:
        self.pattern(element, "toggle").Toggle()

    def select(self, element) -> None:
        self.pattern(element, "selection_item").Select()

    def expand(self, element) -> None:
        self.pattern(element, "expand").Expand()

    def collapse(self, element) -> None:
        self.pattern(element, "expand").Collapse()

    def set_value(self, element, text: str) -> None:
        self.pattern(element, "value").SetValue(text)

    def legacy_default_action(self, element) -> None:
        self.pattern(element, "legacy").DoDefaultAction()

    def legacy_set_value(self, element, text: str) -> None:
        self.pattern(element, "legacy").SetValue(text)

    def scroll_into_view(self, element) -> None:
        self.pattern(element, "scroll_item").ScrollIntoView()

    def focus_within(self, element) -> bool:
        """Le focus clavier est-il sur cet élément ou un de ses descendants (champ intérieur d'une liste déroulante,
        conteneur d'un éditeur) ? Faux si l'élément a disparu ou si le focus est ailleurs (relectures du 20/10 : sans
        ce contrôle, Ctrl+A puis Suppr partaient dans un autre champ)."""
        try:
            current = self.uia.GetFocusedElement()
            walker = self.uia.ControlViewWalker
            for _ in range(32):
                if not current:
                    return False
                if self.uia.CompareElements(current, element):
                    return True
                current = walker.GetParentElement(current)
        except comtypes.COMError:
            return False
        return False

    def realize_named(self, container, name: str) -> bool:
        """Élément de ce nom exact dans une liste virtualisée (ItemContainer) : créé s'il ne l'était pas encore
        (VirtualizedItem.Realize) puis amené à l'écran. Faux si la liste ne le contient pas."""
        try:
            found = self.pattern(container, "item_container").FindItemByProperty(None, U.UIA_NamePropertyId, name)
        except UIAError, comtypes.COMError:  # liste ordinaire sans ItemContainer : la suivante est consultée (tour 3)
            return False
        if not found:
            return False
        with contextlib.suppress(UIAError, comtypes.COMError):
            self.pattern(found, "virtualized_item").Realize()
        with contextlib.suppress(UIAError, comtypes.COMError):
            self.scroll_into_view(found)
        return True

    def scroll(self, element, horizontal: int, vertical: int) -> None:
        """horizontal/vertical : ScrollAmount (0 grand recul, 1 petit recul, 2 rien, 3 grande avance, 4 petite avance)."""
        self.pattern(element, "scroll").Scroll(horizontal, vertical)

    def scroll_percent(self, element) -> tuple[float, float] | None:
        try:
            p = self.pattern(element, "scroll")
            return p.CurrentHorizontalScrollPercent, p.CurrentVerticalScrollPercent
        except UIAError, comtypes.COMError:
            return None

    def set_focus(self, element) -> None:
        element.SetFocus()

    def take_focus(self, element) -> None:
        """Focus par l'action MSAA accSelect(SELFLAG_TAKEFOCUS). Chromium : focus DOM sans activer la fenêtre (UIA
        SetFocus, lui, l'active ; mesuré, bench/chromium_input_probe.py)."""
        self.pattern(element, "legacy").Select(SELFLAG_TAKEFOCUS)

    def show_context_menu(self, element) -> None:
        try:
            element.QueryInterface(U.IUIAutomationElement3).ShowContextMenu()
        except comtypes.COMError as error:
            raise UIAError(f"UIA context menu unavailable: {error}") from error

    def select_all_text(self, element) -> None:
        """Sélectionne tout le texte de l'élément (TextPattern) : la frappe suivante le remplace."""
        self.pattern(element, "text").DocumentRange.Select()

    def caret_to_end(self, element) -> None:
        rng = self.pattern(element, "text").DocumentRange
        rng.MoveEndpointByRange(0, rng, 1)  # début ← fin : plage vide en fin de texte
        rng.Select()

    def text(self, element, max_chars: int = 20000) -> str | None:
        """Texte du document (TextPattern), borné."""
        try:
            pattern = self.pattern(element, "text")
            return pattern.DocumentRange.GetText(max_chars)
        except UIAError, comtypes.COMError:
            return None


def runtime_id(props: dict[str, Any]) -> tuple[int, ...] | None:
    rid = props.get("runtime_id")
    if not rid:
        return None
    try:
        return tuple(int(v) for v in rid)
    except TypeError:
        return None


def com_init() -> None:
    """À appeler une fois par thread qui utilise UIA (appartement multithread recommandé pour un client UIA).

    Le thread principal a déjà été initialisé par comtypes à l'import (STA) : on le garde tel quel.
    """
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:  # RPC_E_CHANGED_MODE : appartement déjà choisi pour ce thread
        pass
