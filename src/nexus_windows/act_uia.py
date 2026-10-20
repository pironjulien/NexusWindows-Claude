"""Actions par UI Automation et répartition par famille : clic, saisie, touches, choix, cases, dépliage, focus,
menus. WinForms, WPF, UWP/WinUI passent par UIA ; Win32 et Chromium y sont aiguillés vers leurs techniques propres
(act_win32, act_chromium)."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from typing import Any

from . import foreground, keys, methods, realinput, snapshot, uia, worker
from . import win32 as w
from .act_base import (
    CHECK_ROLES,
    CHOICE_ROLES,
    EXPAND_ROLES,
    EXPECTED,
    ITEM_ROLES,
    LEAF_ROLES,
    NO_KEYBOARD_MESSAGES,
    NO_MOUSE_MESSAGES,
    SELECT_ALL_JS,
    TEXT_ROLES,
    UIA_ONLY,
    ActionError,
    ExecutorBase,
    NeedsForeground,
    Outcome,
    Unconfirmed,
    _unique,
    framework_of,
)


class UiaActions(ExecutorBase):
    """Actions publiques de l'exécuteur, techniques UI Automation."""

    def click_element(
        self, e: snapshot.Element, button: str = "left", count: int = 1, close_list: bool = True
    ) -> Outcome:
        view = self.view
        fw = framework_of(e, view)
        role = e.role
        p = e.props
        what = f"click on {e.describe()}"
        if p.get("enabled") is False:
            raise ActionError(f"{e.describe()} is disabled.")
        self._confirm_click(e, button)
        chain: list[tuple[str, Callable[[], Any]]] = []
        hwnd, cls = self._element_hwnd_class(e)
        winforms = cls.startswith("windowsforms")
        msaa = self._msaa_item(e, fw)
        page = self._page_if_free()

        if button == "right":
            chain.append(("UIA context menu", lambda: self.auto.show_context_menu(e.node.element)))
            if page is not None:
                chain.append(("DevTools", lambda: self._cdp_click(page, *self._point_of(e), "right")))
            if fw not in NO_MOUSE_MESSAGES:
                chain.append(
                    ("right-click by messages", self._watched(lambda: self._post_click(*self._point_of(e), "right")))
                )
            return self._try_or_foreground(chain, what, lambda: realinput.click(*self._point_of(e), "right"))
        if count >= 2:
            if p.get("has_invoke") and role in ITEM_ROLES and not msaa:
                chain.append(("UIA Invoke (open)", lambda: self.auto.invoke(e.node.element)))
            if page is not None:
                chain.append(("DevTools", lambda: self._cdp_click(page, *self._point_of(e), "left", 2)))
            if fw not in NO_MOUSE_MESSAGES:
                chain.append(
                    (
                        "double-click by messages",
                        self._watched(lambda: self._post_click(*self._point_of(e), "left", 2)),
                    )
                )
            return self._try_or_foreground(
                chain, what.replace("click", "double-click", 1), lambda: realinput.click(*self._point_of(e), "left", 2)
            )

        if fw == "Chromium":
            return self._click_chromium(e, page, what)
        if fw == "Tk":  # aucun canal d'arrière-plan (mesuré) : refus franc, ou vraie souris avec foreground=true
            return self._try_or_foreground([], what, lambda: realinput.click(*self._point_of(e)))
        if hwnd and methods._is_class(hwnd, methods.BUTTON_CLASSES):
            kind = w.style(hwnd) & methods.BS_TYPEMASK
            if not winforms or kind in (methods.BS_PUSHBUTTON, methods.BS_DEFPUSHBUTTON):
                chain.append(("BN_CLICKED", lambda: methods.win32_button_click(hwnd)))
        combo = self._win32_combo_of(e) if fw in ("Win32", "WinForms") else 0
        if combo:
            # Liste déroulante Win32/WinForms ou son bouton « Ouvrir » : ouverte ou fermée par CB_SHOWDROPDOWN, relu
            # (un clic par messages y prendrait la capture de la souris).
            opened = bool(methods.combo_dropped(combo))
            chain.append(("CB_SHOWDROPDOWN", lambda: methods.win32_combo_show(combo, not opened)))
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
            chain.append(("UIA focus", lambda: self._quiet_focus(e)))
        if p.get("has_legacy") and fw not in ("Win32", "WinForms"):
            # Win32/WinForms : l'action par défaut MSAA d'oleacc fait souvent un VRAI clic (SendInput) sur les onglets,
            # en-têtes et barres d'outils : jamais sans foreground=true.
            chain.append(("UIA Legacy", lambda: self.auto.legacy_default_action(e.node.element)))
        if page is not None:
            chain.append(("DevTools", lambda: self._cdp_click(page, *self._point_of(e))))
        if fw not in NO_MOUSE_MESSAGES:
            chain.append(("click by messages", self._watched(lambda: self._post_click(*self._point_of(e)))))
        # Option d'une liste déroulante ouverte (WPF, UWP…) : la liste reste ouverte après UIA Select ; refermée, puis
        # le choix relu sur la liste déroulante.
        combo_parent = (
            self._open_combo_of(e) if close_list and role in ITEM_ROLES and fw not in ("Win32", "WinForms") else None
        )
        outcome = self._try_or_foreground(chain, what, lambda: realinput.click(*self._point_of(e)))
        if combo_parent is not None and outcome.ok:
            closed = self._collapse_if_open(combo_parent)
            if self._selection_confirmed(combo_parent, e):
                outcome.verified = True
                outcome.line += " — read back ✓" + (", list closed" if closed else "")
            else:
                outcome.ok = False
                outcome.line += (
                    f' — READ-BACK FAILED: {combo_parent.describe()} does not show "{snapshot.clip(e.name, 40)}"'
                )
        return outcome

    @staticmethod
    def _open_combo_of(e: snapshot.Element) -> snapshot.Element | None:
        """Liste déroulante OUVERTE qui contient l'option (d'après l'instantané), ou None."""
        node = e.node.parent
        for _ in range(4):
            if node is None:
                return None
            if node.control_type == "ComboBox":
                return snapshot.Element(0, node, 0, e.window) if node.props.get("expand") == 1 else None
            node = node.parent
        return None

    def write(self, spec: Any, text: str, replace: bool) -> Outcome:
        if spec is None or spec == "":
            self._confirm_text(None, text)
            return self._write_focused(text, replace)
        element = self.resolve_element(spec, TEXT_ROLES, "text field")
        self._confirm_text(element, text)
        return self.write_element(element, text, replace)

    def write_element(self, e: snapshot.Element, text: str, replace: bool) -> Outcome:
        verb = "text replaced" if replace else "text typed"
        p = e.props
        self._refuse_secret(p, e.describe())
        if p.get("readonly"):
            raise ActionError(f"{e.describe()} is read-only: nothing was written.")
        if p.get("enabled") is False:
            raise ActionError(f"{e.describe()} is disabled.")
        fw = framework_of(e, self.view)
        hwnd, cls = self._element_hwnd_class(e)
        before = self._read_value(e) or ""
        chain: list[tuple[str, Callable[[], Any]]] = []
        page = self._get_page()
        if hwnd and methods._is_class(hwnd, methods.EDIT_CLASSES):
            chain.append(("EM_REPLACESEL", lambda: methods.win32_edit_replace(hwnd, text, append=not replace)))
        if page is not None and fw == "Chromium":
            chain.append(("DevTools insertText", lambda: self._cdp_write(page, e, text, replace)))
        multiline_chromium = fw == "Chromium" and self._is_multiline_chromium(e)
        # DevTools disponible : jamais de frappes (elles demandent une activation brève, que devtools=true évite).
        keystrokes = page is None
        # Chromium sans DevTools : frappes en activation SIMULÉE d'abord (premier plan de Julien jamais pris), puis en
        # activations brèves si l'application ne s'y prête pas (rien n'a alors été tapé).
        simulated = ("keystrokes without activation", lambda: self._keystrokes(e, text, replace, simulated=True))
        if multiline_chromium and keystrokes:
            chain.append(simulated)
            chain.append(("keystrokes", lambda: self._keystrokes(e, text, replace)))
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
            if fw == "Chromium":
                chain.append(simulated)
            chain.append(("keystrokes", lambda: self._keystrokes(e, text, replace)))
        outcome = self._try_or_foreground(
            chain, f"{verb} in {e.describe()}", lambda: self._real_write(e, text, replace)
        )
        verified = self._verify_text(e, before, text, replace, timeout_s=1.5 if fw == "Chromium" else 1.2)
        if verified is False:
            outcome.ok = False
            outcome.line += f" — READ-BACK FAILED: current value is {len(self._read_value(e) or '')} characters long"
        elif verified:
            outcome.verified = True
            outcome.line += " — read back ✓"
        return outcome

    def _focused_target_element(self) -> snapshot.Element | None:
        """Élément qui a le focus DANS LA CIBLE (jamais le focus système quand c'est une fenêtre de l'utilisateur)."""
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
        page = self._get_page()
        verb = "text replaced" if replace else "text typed"
        if page is not None:

            def via_cdp() -> str:
                self._cdp_refuse_secret(page)
                if replace:
                    page.evaluate(SELECT_ALL_JS)
                page.insert_text(text)
                return ""

            return self._try([("DevTools insertText", via_cdp)], f"{verb} in the focused element")
        element = self._focused_target_element()
        if element is None:
            raise ActionError(
                f"{verb}: no field of the window has the focus (or it is unknown): specify target (name or [N])."
            )
        return self.write_element(element, text, replace)

    # --- Touches ------------------------------------------------------------------------------------------------
    def press(self, spec: str, target: Any = None) -> Outcome:
        combos = keys.parse_sequence(spec)
        if not combos:
            raise ActionError("No key.")
        printable = any(c.char and c.char.isprintable() and not c.modifiers for c in combos)
        element = None
        if target not in (None, ""):
            element = self.resolve_element(target, TEXT_ROLES | LEAF_ROLES, "element")
        self._confirm_keys(combos, spec, element)  # avant toute touche : ce qu'elles déclencheraient
        page = self._get_page()
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
            return Outcome(True, f'keys "{spec}" (DevTools)', "DevTools")
        focused = element or self._focused_target_element()
        if focused is not None and printable:
            self._refuse_secret(focused.props, focused.describe())
        shortcut = any(set(c.modifiers) - {0x10} for c in combos)

        def real() -> None:
            # Cible désignée : le vrai clavier va à l'élément qui a le focus, donc la cible le reçoit d'abord et c'est
            # relu ; sinon rien n'est envoyé (relecture de Codex : Ctrl+A visait B et sélectionnait A).
            if element is not None and not self._refresh(element.node.element).get("focused"):
                foreground.mark_own_input()
                self.auto.set_focus(element.node.element)
                if not self._refresh(element.node.element).get("focused"):
                    raise ActionError(f"{element.describe()} did not take the keyboard focus: no key was sent.")
            for combo in combos:
                realinput.press(combo)

        what = f'keys "{spec}"'
        simulated: list[tuple[str, Callable[[], Any]]] = []
        if fw == "Chromium":
            # Activation simulée d'abord : touches et raccourcis (état clavier simulé) lus par Chromium sans que le
            # premier plan de Julien change ; focus de l'élément visé par accSelect, relu.
            sim_sent: list[str] = []

            def run_simulated() -> str:
                with self._sim_activation(element) as sim:

                    def prepare_simulated() -> None:
                        if element is not None:
                            self._take_focus(element)
                        sim.check("nothing was sent, the action is cancelled")

                    detail = self._watched(
                        lambda: self._post_combos(combos, fw, sim_sent, simulated=True), prepare_simulated
                    )()
                return f"simulated activation, {', '.join(sorted(set(sim_sent)))}; {detail}"

            simulated.append(("messages without activation", run_simulated))
            if shortcut:
                try:
                    return self._try(simulated, what)
                except Unconfirmed:
                    raise
                except ActionError:
                    pass  # l'application ne s'y prête pas : refus (ou vrai clavier autorisé) ci-dessous
        # Raccourcis (Ctrl/Alt/Win) : WPF/UWP les ignorent par messages, Chromium lit l'état réel du clavier.
        if (shortcut and (fw in UIA_ONLY or fw in ("Chromium", "Tk"))) or fw in NO_KEYBOARD_MESSAGES:
            hint = " (or relaunch the application with launch and devtools=true)" if fw == "Chromium" else ""
            try:
                return self._foreground_or_fail(what, real)
            except NeedsForeground as error:
                raise NeedsForeground(f"{error}{hint}") from None
        sent: list[str] = []

        def prepare() -> None:
            if element is not None and (fw == "Chromium" or not self._refresh(element.node.element).get("focused")):
                foreground.mark_own_input()
                self.auto.set_focus(element.node.element)
                # Même invariant que le vrai clavier (tour 3, Codex) : les messages vont à la fenêtre qui a le focus ;
                # cible désignée sans focus relu → rien n'est envoyé, jamais Suppr dans un autre champ.
                if fw != "Chromium" and not self._refresh(element.node.element).get("focused"):
                    raise ActionError(f"{element.describe()} did not take the keyboard focus: no key was sent.")
            if fw == "Chromium":
                if element is None:
                    self._focus_web_content()
                self._ensure_active()

        def run() -> str:
            if fw == "Chromium" or element is not None:
                with foreground.brief_activation(self.hwnd, self.windows) as activation:

                    def checked() -> None:
                        prepare()
                        activation.check("nothing was sent, the action is cancelled")

                    detail = self._watched(lambda: self._post_combos(combos, fw, sent), checked)()
            else:
                detail = self._watched(lambda: self._post_combos(combos, fw, sent))()
            return f"{', '.join(sorted(set(sent)))}; {detail}"

        return self._try_or_foreground([*simulated, ("messages", run)], what, real)

    # --- Choix, cases, dépliage ---------------------------------------------------------------------------------
    def select(self, spec: Any, option: str | None) -> Outcome:
        e = self.resolve_element(spec, CHOICE_ROLES, "list or option")
        if not option:
            return self.click_element(e)
        fw = framework_of(e, self.view)
        hwnd, cls = self._element_hwnd_class(e)
        what = f'"{option}" selected in {e.describe()}'
        if hwnd and methods._is_class(hwnd, methods.COMBO_CLASSES):
            return self._try([("CB_SETCURSEL", lambda: methods.win32_combo_select(hwnd, option))], what)
        if hwnd and methods._is_class(hwnd, methods.LISTBOX_CLASSES):
            return self._try([("LB_SETCURSEL", lambda: methods.win32_listbox_select(hwnd, option))], what)
        item = self._find_item(e, option)
        if item is None and fw == "Chromium" and e.role == "ComboBox":
            return self._select_by_typing(e, option)
        opened = False
        keep_open = False
        # Liste déroulante déjà ouverte (expand précédent, ou Julien) : refermée elle aussi une fois l'option choisie
        # (WPF la laisse ouverte après UIA Select : surgissante au-dessus des fenêtres de Julien).
        was_open = e.role == "ComboBox" and e.props.get("expand") == 1
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
                        outcome.line += " — read back ✓"
                    else:
                        outcome.ok = False
                        outcome.line += " — READ-BACK FAILED"
                    return outcome
                raise ActionError(f'Option "{option}" not found in {e.describe()}.')
            outcome = self.click_element(item, close_list=False)  # repli et relecture faits ici
            keep_open = e.role != "ComboBox"  # arbre ou liste dépliés pour atteindre l'option : laissés tels quels
        except worker.Cancelled:
            opened = was_open = False  # arrêt d'urgence ou appel abandonné : plus aucune action, même pas le repli
            raise
        finally:
            # Une liste déroulante ouverte reste une surgissante au-dessus des fenêtres de Julien (un clic de sa part y
            # choisirait une option) : repliée sur tout chemin, échec compris.
            if (opened and not keep_open) or was_open:
                self._collapse_if_open(e)
        if self._selection_confirmed(e, item):
            outcome.verified = True
            outcome.line += " — read back ✓"
        else:
            outcome.ok = False
            outcome.line += f' — READ-BACK FAILED: {e.describe()} does not show "{snapshot.clip(item.name, 40)}"'
        return outcome

    def _collapse_if_open(self, e: snapshot.Element) -> bool:
        """Replie l'élément s'il est encore déplié (relu en direct) ; une erreur de repli n'efface pas celle de l'action."""
        with contextlib.suppress(*EXPECTED):
            if self._refresh(e.node.element).get("expand") == 1:
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
                value = self._refresh(container.node.element).get("value") if combo_with_value else None
                selected = self._refresh(item.node.element).get("selected")
            except EXPECTED:
                value, selected = None, None
            if combo_with_value and value and snapshot.norm(str(value)).lower() == want:
                return True
            if not combo_with_value and selected:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _value_is(self, e: snapshot.Element, expected: str, timeout_s: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            value = self._read_value(e)
            if value is not None and snapshot.norm(value).lower() == expected.strip().lower():
                return True
            time.sleep(0.05)
        return False

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
            self.notes.append(f'partial match: "{name}"')
        return element

    def set_checked(self, spec: Any, state: bool | None) -> Outcome:
        e = self.resolve_element(spec, CHECK_ROLES, "checkbox")  # resolve relit déjà l'état en direct
        if e.role == "RadioButton":
            if state is False:
                raise ActionError(f"{e.describe()}: a radio button cannot be unchecked; select another option.")
            if e.props.get("selected") or e.props.get("toggle") == 1:
                return Outcome(True, f"{e.describe()} already selected (read back)", "none", verified=True)
            outcome = self.click_element(e)
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                with contextlib.suppress(*EXPECTED):
                    live = self._refresh(e.node.element)
                    if live.get("selected") or live.get("toggle") == 1:
                        outcome.verified = True
                        outcome.line += " — selected ✓"
                        return outcome
                time.sleep(0.05)
            outcome.ok = False
            outcome.line += " — READ-BACK FAILED"
            return outcome
        if not e.props.get("has_toggle"):
            raise ActionError(f"{e.describe()} is not a checkbox (no Toggle pattern): use click.")
        before = e.props.get("toggle")
        wanted = (1 if before != 1 else 0) if state is None else (1 if state else 0)
        if before == wanted:
            return Outcome(
                True,
                f"{e.describe()} already {'checked' if wanted else 'unchecked'} (read back)",
                "none",
                verified=True,
            )
        timeout = 1.2 if framework_of(e, self.view) == "Chromium" else 0.6
        outcome = None
        after = before
        for _ in range(3):  # une case à trois états passe par « mixte » : au plus trois bascules
            outcome = self.click_element(e)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with contextlib.suppress(*EXPECTED):
                    after = self._refresh(e.node.element).get("toggle")
                if after != before:
                    break
                time.sleep(0.05)
            if after == wanted or after == before:
                break
            before = after
        assert outcome is not None
        if after == wanted:
            outcome.verified = True
            outcome.line += f" — {'checked' if wanted else 'unchecked'} ✓"
        else:
            outcome.ok = False
            names: dict[int | None, str] = {0: "unchecked", 1: "checked", 2: "mixed"}
            state_name = names.get(after, "unknown")
            outcome.line += f" — READ-BACK FAILED: state {state_name}"
        return outcome

    def expand(self, spec: Any, open_: bool) -> Outcome:
        e = self.resolve_element(spec, EXPAND_ROLES, "expandable element")
        word = "expanded" if open_ else "collapsed"
        combo = self._win32_combo_of(e)
        if combo:
            # Liste déroulante Win32/WinForms (WinForms n'y publie pas ExpandCollapse) : CB_SHOWDROPDOWN, relu.
            if methods.combo_dropped(combo) is open_:
                return Outcome(True, f"{e.describe()} already {word} (read back)", "none", verified=True)
            return self._try(
                [("CB_SHOWDROPDOWN", lambda: methods.win32_combo_show(combo, open_))], f"{e.describe()} {word}"
            )
        if not e.props.get("has_expand"):
            # Jamais de repli sur Invoke : ce serait exécuter la commande de l'élément (bouton, menu feuille).
            raise ActionError(f"{e.describe()} is not expandable (no ExpandCollapse pattern): use click.")
        state = e.props.get("expand")  # relu en direct par resolve
        if state == 3:
            raise ActionError(f"{e.describe()} has nothing to expand (leaf element).")
        if state in (0, 1, 2) and (state != 0) == open_:
            return Outcome(True, f"{e.describe()} already {word} (read back)", "none", verified=True)
        outcome = self._try(
            [("UIA ExpandCollapse", lambda: (self.auto.expand if open_ else self.auto.collapse)(e.node.element))],
            f"{e.describe()} {word}",
        )
        deadline = time.monotonic() + 0.6
        state = None
        while time.monotonic() < deadline:
            with contextlib.suppress(*EXPECTED):
                state = self._refresh(e.node.element).get("expand")
            if state in (0, 1, 2) and (state != 0) == open_:
                outcome.verified = True
                outcome.line += " — read back ✓"
                return outcome
            time.sleep(0.05)
        if state is None:
            outcome.line += " — state not read back (check the image)"
        else:
            outcome.ok = False
            outcome.line += (
                f" — READ-BACK FAILED: {'leaf' if state == 3 else 'collapsed' if state == 0 else 'expanded'}"
            )
        return outcome

    def focus(self, spec: Any) -> Outcome:
        e = self.resolve_element(spec, TEXT_ROLES | LEAF_ROLES, "element")
        chain: list[tuple[str, Callable[[], Any]]] = []
        if framework_of(e, self.view) == "Chromium":
            chain.append(("focus without activation", lambda: self._sim_focus(e)))
        chain.append(("UIA focus", lambda: self._quiet_focus(e)))
        return self._try(chain, f"focus on {e.describe()}")

    # --- Menus --------------------------------------------------------------------------------------------------
    def menu(self, path: str) -> Outcome:
        parts = [p.strip() for p in path.replace("/", ">").split(">") if p.strip()]
        if not parts:
            raise ActionError("Empty menu path.")
        if methods.win32_menu_items(self.hwnd):
            self._confirm_menu(path)  # WM_COMMAND ne passe par aucun clic d'élément : contrôlé ici
            try:
                return self._try(
                    [("WM_COMMAND menu", lambda: methods.win32_menu_command(self.hwnd, path))], f'menu "{path}"'
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
                low = name.lower()
                while item is None:
                    worker.check_cancelled()
                    # Sous-menu : cherché d'abord dans les seules surgissantes (quelques nœuds, quelques ms) ; un menu
                    # CEF (Steam) peut se refermer pendant la relecture complète d'une grande fenêtre.
                    popup = self._popup_item(roles, low) if index > 0 else None
                    if popup is not None:
                        item = popup
                        break
                    view = self._rebuild() if index > 0 or self.acted else self.view
                    exact = _unique(
                        [e for e in view.elements if self._menu_role(e, roles) and snapshot.norm(e.name).lower() == low]
                    )
                    if not exact:
                        exact = _unique(
                            [
                                e for e in view.elements
                                if self._menu_role(e, roles) and low in snapshot.norm(e.name).lower()
                            ]
                        )  # fmt: skip
                    if len(exact) > 1:
                        raise ActionError(f'Menu "{name}" ambiguous ({", ".join(e.describe() for e in exact[:5])}).')
                    item = self._shown_id(exact[0]) if exact else None
                    if item is None:
                        if time.monotonic() > deadline:
                            raise ActionError(f'Menu "{name}" not found (path "{path}").')
                        time.sleep(0.15)
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
                    self._try(chain, f'menu "{name}" opened')
                    opened.append(item)
                    time.sleep(0.2)
        except worker.Cancelled:
            raise  # arrêt d'urgence ou appel abandonné : plus aucune action, même pas le repli
        except BaseException:
            self._close_menus(opened, clicked)
            raise
        assert outcome is not None
        outcome.line = f'menu "{path}": {outcome.line}'
        return outcome

    def _menu_role(self, e: snapshot.Element, roles: set[str]) -> bool:
        """Entrée de menu ; en Chromium/CEF, la barre de menus est souvent du texte cliquable (Steam : « Steam »,
        « Afficher »…), accepté au premier niveau s'il porte Invoke."""
        if e.role in roles:
            return True
        return (
            "Button" in roles
            and e.role in ("Text", "Hyperlink")
            and bool(e.props.get("has_invoke"))
            and framework_of(e, self.view) == "Chromium"
        )

    def _popup_item(self, roles: set[str], low: str) -> snapshot.Element | None:
        """Entrée de nom exact `low` dans les surgissantes de la cible ouvertes à l'instant (sans relire la fenêtre
        principale). Unique, sinon None (la recherche complète décide et signale l'ambiguïté)."""
        found: list[snapshot.Element] = []
        for popup in snapshot.popups_of(self.hwnd):
            try:
                tree = self.auto.tree(popup, max_nodes=400)
            except EXPECTED:
                continue
            for node in tree.walk():
                if node.control_type in roles and snapshot.norm(node.name).lower() == low and node.rect is not None:
                    found.append(snapshot.Element(0, node, 0, popup))
        found = _unique(found)
        return found[0] if len(found) == 1 else None

    def _close_menus(self, opened: list[snapshot.Element], clicked: bool) -> None:
        """Menus dépliés par un chemin qui a échoué : repliés du plus profond au premier (un menu ouvert reste une
        surgissante au-dessus des fenêtres de l'utilisateur). Sans motif ExpandCollapse, l'action par défaut le referme, mais
        seulement si le dernier clic n'a pas été tenté : il a pu fermer le menu, qu'elle rouvrirait."""
        for item in reversed(opened):
            if item.props.get("has_expand"):
                self._collapse_if_open(item)
                continue
            native = framework_of(item, self.view) in ("Win32", "WinForms")  # action MSAA par défaut : un vrai clic
            if not clicked and item.props.get("has_legacy") and not native:
                with contextlib.suppress(*EXPECTED):
                    self.auto.legacy_default_action(item.node.element)
