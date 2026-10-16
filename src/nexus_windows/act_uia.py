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
    VK_APPS,
    VK_F10,
    VK_SHIFT,
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

        if fw == "Chromium":
            return self._click_chromium(e, page, what)
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
        simulated = ("frappes sans activation", lambda: self._keystrokes(e, text, replace, simulated=True))
        if multiline_chromium and keystrokes:
            chain.append(simulated)
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
            if fw == "Chromium":
                chain.append(simulated)
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
        page = self._get_page()
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
        what = f"touches « {spec} »"
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
                        sim.check("rien n'a été envoyé, l'action est annulée")

                    detail = self._watched(
                        lambda: self._post_combos(combos, fw, sim_sent, simulated=True), prepare_simulated
                    )()
                return f"activation simulée, {', '.join(sorted(set(sim_sent)))} ; {detail}"

            simulated.append(("messages sans activation", run_simulated))
            if shortcut:
                try:
                    return self._try(simulated, what)
                except Unconfirmed:
                    raise
                except ActionError:
                    pass  # l'application ne s'y prête pas : refus (ou vrai clavier autorisé) ci-dessous
        # Raccourcis (Ctrl/Alt/Win) : WPF/UWP les ignorent par messages, Chromium lit l'état réel du clavier.
        if (shortcut and (fw in UIA_ONLY or fw in ("Chromium", "Tk"))) or fw in NO_KEYBOARD_MESSAGES:
            hint = " (ou relancer l'application par launch avec devtools=true)" if fw == "Chromium" else ""
            try:
                return self._foreground_or_fail(what, real)
            except NeedsForeground as error:
                raise NeedsForeground(f"{error}{hint}") from None
        sent: list[str] = []

        def prepare() -> None:
            if element is not None and (fw == "Chromium" or not self._refresh(element.node.element).get("focused")):
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

        return self._try_or_foreground([*simulated, ("messages", run)], what, real)

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
                    live = self._refresh(e.node.element)
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
                state = self._refresh(e.node.element).get("expand")
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
        chain: list[tuple[str, Callable[[], Any]]] = []
        if framework_of(e, self.view) == "Chromium":
            chain.append(("focus sans activation", lambda: self._sim_focus(e)))
        chain.append(("focus UIA", lambda: self._quiet_focus(e)))
        return self._try(chain, f"focus sur {e.describe()}")

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
