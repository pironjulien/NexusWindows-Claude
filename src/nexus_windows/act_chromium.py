"""Chromium / Electron / CEF : clic UIA relu (Invoke ou action par défaut, technique suivante si aucun effet),
DevTools si l'application a été lancée avec, <select> par recherche au clavier, saisie et zone de rendu."""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from typing import Any

import comtypes

from . import capture, cdp, foreground, keys, methods, realinput, snapshot, uia, worker
from . import win32 as w
from .act_base import (
    DEEP_ACTIVE_JS,
    EXPECTED,
    OWN_STATE_S,
    SELECT_ALL_JS,
    ActionError,
    ExecutorBase,
    NoEffect,
    Outcome,
    _short,
    framework_of,
)


class ChromiumActions(ExecutorBase):
    """Techniques Chromium/Electron/CEF."""

    # --- Clic Chromium/CEF vérifié -------------------------------------------------------------------------------
    def _click_chromium(self, e: snapshot.Element, page: cdp.Page | None, what: str) -> Outcome:
        """Clic sur un élément Chromium/CEF. Mesuré sur Steam (onglets et listes React) : UIA Select est accepté mais
        ne déclenche rien ; UIA Invoke (élément cliquable) et l'action par défaut MSAA (Chromium la traduit en clic DOM,
        sans entrée réelle) fonctionnent, fenêtre recouverte comprise. Chaque technique UIA est donc relue : sans aucun
        effet constaté (état de l'élément, fenêtres de l'application, image), la suivante est essayée ; une même action
        n'est jamais rejouée sous une autre forme (Invoke et action par défaut sont la même action DOM)."""
        p = e.props
        role = e.role
        if role in ("TabItem", "RadioButton") and p.get("selected"):
            # Rien à faire : un clic de plus ne changerait rien et ne pourrait pas être relu (faux « sans effet »).
            return Outcome(True, f"{what} (already selected — read back ✓)", "none", [], True)
        chain: list[tuple[str, Callable[[], Any]]] = []
        if role in ("Edit", "Document"):
            chain.append(("focus without activation", lambda: self._sim_focus(e)))
            chain.append(("UIA focus", lambda: self._quiet_focus(e)))
        elif role == "CheckBox" and p.get("has_toggle"):
            chain.append(("UIA Toggle", self._effect(e, lambda: self.auto.toggle(e.node.element))))
        else:
            expandable = bool(p.get("has_expand")) and role in (
                "ComboBox",
                "MenuItem",
                "SplitButton",
                "TreeItem",
                "Button",
            )
            expand_step = (
                "UIA Expand/Collapse",
                self._effect(
                    e, lambda: (self.auto.collapse if p.get("expand") == 1 else self.auto.expand)(e.node.element)
                ),
            )
            # Liste, menu ou bouton à menu (aria-haspopup) : cliquer, c'est ouvrir ; Expand d'abord, Invoke ensuite si
            # rien n'a bougé. Bouton partagé et nœud d'arbre : le clic exécute ou sélectionne, Invoke d'abord.
            expand_first = expandable and role in ("ComboBox", "MenuItem", "Button")
            if expand_first:
                chain.append(expand_step)
            if p.get("has_invoke"):
                chain.append(("UIA Invoke", self._effect(e, lambda: self.auto.invoke(e.node.element))))
            elif p.get("has_legacy"):
                chain.append(
                    ("UIA default action", self._effect(e, lambda: self.auto.legacy_default_action(e.node.element)))
                )
            if expandable and not expand_first:
                chain.append(expand_step)
        if role not in ("Edit", "Document"):
            if not p.get("has_invoke"):
                child = self._clickable_descendant(e)
                if child is not None:
                    chain.append(
                        (
                            f"UIA Invoke on child {child.control_type}",
                            self._effect(e, lambda: self.auto.invoke(child.element)),
                        )
                    )
            if p.get("has_selection_item"):
                chain.append(("UIA Select", self._effect(e, lambda: self.auto.select(e.node.element))))
        if page is not None:
            chain.append(("DevTools", lambda: self._cdp_click(page, *self._point_of(e))))
        chain.append(("click by messages", self._watched(lambda: self._post_click(*self._point_of(e)))))
        return self._try_or_foreground(chain, what, lambda: realinput.click(*self._point_of(e)))

    @staticmethod
    def _clickable_descendant(e: snapshot.Element) -> uia.Node | None:
        """Premier descendant cliquable (motif Invoke) dessiné dans l'élément : l'icône ou le libellé qui porte le
        gestionnaire de clic d'un onglet ou d'un élément de liste React."""
        outer = e.rect
        for node in e.node.walk():
            if node is e.node or not node.props.get("has_invoke") or node.props.get("offscreen"):
                continue
            rect = node.rect
            if rect is not None and (outer is None or outer.contains(*rect.center)):
                return node
        return None

    def _signature(self, e: snapshot.Element) -> tuple:
        """Ce qu'un clic peut changer, relu sans reconstruire l'arbre : état de l'élément (sélection, coche, dépliage,
        nom, valeur ; disparu), fenêtres visibles de l'application (menus, dialogues, nouvelles fenêtres, titres),
        image réduite de la fenêtre (comparée avec tolérance : un curseur clignotant n'est pas un effet)."""
        try:
            own = self._signature_own(e)
        except uia.UIAError, comtypes.COMError:
            own = ("gone",)
        windows = tuple(
            sorted(
                (h, w.window_text(h))
                for h in w.enum_windows()
                if w.window_pid(h) in self.target_pids and w.IsWindowVisible(h) and not w.is_cloaked(h)
            )
        )
        try:
            image = capture.capture_printwindow(self.hwnd).image.convert("L").resize((96, 96))
        except capture.CaptureError:
            image = None
        return own, windows, image

    def _signature_own(self, e: snapshot.Element) -> tuple:
        live = self._refresh(e.node.element)
        return tuple(live.get(k) for k in ("selected", "toggle", "expand", "name", "value", "enabled"))

    @staticmethod
    def _images_differ(a, b) -> bool:
        if a is None or b is None:
            return a is not b
        from PIL import ImageChops

        diff = ImageChops.difference(a, b)
        diff = diff.point([255 if v > 24 else 0 for v in range(256)] * len(diff.getbands()))  # seuil par canal
        return sum(1 for v in diff.getdata() if v) >= 4

    def _effect(self, e: snapshot.Element, fn: Callable[[], Any], settle_s: float = 1.0) -> Callable[[], str]:
        """Enveloppe une action UIA Chromium : signature avant, action, puis relecture jusqu'à settle_s. Changement :
        décrit (✓ si l'élément est relu sélectionné, coché ou déplié) ; aucun : NoEffect (technique suivante)."""

        def run() -> str:
            before = self._signature(e)
            fn()
            deadline = time.monotonic() + settle_s
            while True:
                time.sleep(0.08)
                worker.check_cancelled()
                after = self._signature(e)
                change = self._describe_change(before, after)
                if change:
                    return self._own_state_change(e, before, change)
                if time.monotonic() >= deadline:
                    raise NoEffect(f"no effect observed within {settle_s:.1f} s")

        return run

    def _own_state_change(self, e: snapshot.Element, before: tuple, change: str, wait_s: float = OWN_STATE_S) -> str:
        """Effet constaté ailleurs (titre, image) avant l'état de l'élément : Chromium publie le titre de la page tout
        de suite, l'arbre d'accessibilité un peu après (mesuré : « effet constaté : fenêtre « liste Charlie » » puis
        sélectionné). Élément sélectionnable, cochable ou dépliable : son état est relu encore un instant pour le
        prouver (✓). Rien n'est rejoué : l'action a déjà eu un effet."""
        p = e.props
        if change.startswith("read back ✓") or not any(
            p.get(k) for k in ("has_selection_item", "has_toggle", "has_expand")
        ):
            return change
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            time.sleep(0.04)
            worker.check_cancelled()
            try:
                own = self._signature_own(e)
            except uia.UIAError, comtypes.COMError:
                break
            proved = self._describe_change(before, (own, before[1], before[2]))
            if proved.startswith("read back ✓"):
                return proved
        return change

    def _describe_change(self, before: tuple, after: tuple) -> str:
        own0, win0, img0 = before
        own1, win1, img1 = after
        if own1 != own0:
            if own1 == ("gone",):
                return "effect observed: the element disappeared (interface changed)"
            selected, toggle, expand = own1[0], own1[1], own1[2]
            if selected and not own0[0]:
                return "read back ✓ selected"
            if toggle is not None and toggle != own0[1]:
                return "read back ✓ " + {0: "unchecked", 1: "checked"}.get(toggle, "check state changed")
            if expand is not None and expand != own0[2]:
                return "read back ✓ " + ("expanded" if expand == 1 else "collapsed")
            return "effect observed: element state changed"
        if win1 != win0:
            new = [title for h, title in win1 if (h, title) not in win0 and title]
            return "effect observed: " + (
                f'window "{snapshot.clip(new[0], 40)}"' if new else "menu or window opened/closed"
            )
        if self._images_differ(img0, img1):
            return "effect observed: visible change"
        return ""

    # --- DevTools -----------------------------------------------------------------------------------------------
    def _page_if_free(self) -> cdp.Page | None:
        """Page DevTools pour un CLIC : une page occupée (boîte JavaScript ouverte, script long) n'empêche pas de
        cliquer par UI Automation — c'est justement ainsi qu'on ferme une boîte « OK ». Saisie et touches, elles,
        laissent remonter PageBusy (refus avant toute activation brève)."""
        try:
            return self._get_page()
        except cdp.PageBusy as error:
            self.notes.append(f"DevTools skipped: {_short(error)}")
            return None

    def _cdp_click(self, page: cdp.Page, x: int, y: int, button: str = "left", count: int = 1) -> str:
        cx, cy = self._css_point(page, x, y)
        page.click(cx, cy, button, count)
        return f"css {cx:.0f},{cy:.0f}"

    def _web_viewport(self) -> w.Rect:
        """Zone de rendu de la page à l'écran (fenêtre Chrome_RenderWidgetHostHWND, à défaut la zone cliente).
        Plusieurs zones (Steam : cadre et magasin) : celle qui recouvre le mieux le document visé."""
        contents = [c for c in w.enum_children(self.hwnd) if w.class_name(c) == "Chrome_RenderWidgetHostHWND"]
        if not contents:
            return w.client_rect_screen(self.hwnd)
        doc = self.document.rect if self.document is not None else None
        if doc is None or len(contents) == 1:
            return w.window_rect(contents[0])

        def overlap(content: int) -> int:
            inter = w.window_rect(content).intersect(doc)
            return inter.width * inter.height if inter is not None else 0

        return w.window_rect(max(contents, key=overlap))

    def _css_point(self, page: cdp.Page, x: int, y: int) -> tuple[float, float]:
        """Point écran → coordonnées CSS de la page (zone de rendu Chromium, devicePixelRatio zoom compris)."""
        rect = self._web_viewport()
        try:
            dpr = float(page.evaluate("window.devicePixelRatio") or 0) or (w.GetDpiForWindow(self.hwnd) or 96) / 96
        except cdp.CDPError, TypeError, ValueError:  # page qui répond autre chose qu'un nombre
            dpr = (w.GetDpiForWindow(self.hwnd) or 96) / 96
        return (x - rect.left) / dpr, (y - rect.top) / dpr

    @staticmethod
    def _cdp_focus_at(page: cdp.Page, cx: float, cy: float, editable: bool = False) -> None:
        """Focus DOM d'un élément sans clic (un clic DevTools activerait la fenêtre). editable : le champ de saisie qui
        contient le point (le point d'un éditeur tombe souvent sur un enfant non focalisable), focus relu ; sinon le
        texte partirait dans le champ qui avait le focus avant.

        Shadow DOM traversé (point, ancêtres et focus relu en profondeur) ; dans un éditeur riche, c'est l'hôte
        d'édition le plus extérieur qui est visé et la sélection doit y être : un enfant [tabindex] focalisé ne
        recevrait pas la frappe (tour 6, Codex). Un champ input/textarea au point passe avant l'éditeur qui le contient :
        sinon tout l'éditeur était remplacé, champ et texte autour compris (W1, Codex). Sinon refus, rien n'est envoyé."""
        wanted = (
            "'input, textarea, [contenteditable]:not([contenteditable=\"false\"])'"
            if editable
            else "'input, textarea, select, button, a[href], [tabindex], [contenteditable]:not([contenteditable=\"false\"])'"
        )
        script = (
            "(() => {"
            " const up = n => n.parentElement || (n.parentNode && n.parentNode.host) || null;"
            " const within = (n, el) => { for (; n; n = n.parentNode || n.host) if (n === el) return true; return false; };"
            f" let hit = document.elementFromPoint({cx}, {cy}); if (!hit) return 'none';"
            f" while (hit.shadowRoot) {{ const d = hit.shadowRoot.elementFromPoint({cx}, {cy});"
            " if (!d || d === hit) break; hit = d; }"
            " const box = n => n.tagName === 'INPUT' || n.tagName === 'TEXTAREA';"
            " let el = null; for (let n = hit; n && !el; n = up(n)) if (box(n)) el = n;"
            " if (el) {} else if (hit.isContentEditable) {"
            " el = hit; for (let p = up(el); p && p.isContentEditable; p = up(p)) el = p; }"
            f" else {{ for (let n = hit; n; n = up(n)) if (n.matches && n.matches({wanted})) {{ el = n; break; }} }}"
            " el = el || hit; el.focus();"
            " let a = document.activeElement; while (a && a.shadowRoot && a.shadowRoot.activeElement)"
            " a = a.shadowRoot.activeElement;"
            " if (!a || (a === document.body && !a.isContentEditable) || !within(a, el)) return 'unfocused';"
            " const field = a.isContentEditable || box(a);"
            + (" if (!field) return 'unfocused';" if editable else "")
            + " if (a.isContentEditable && !box(a)) { const r = el.getRootNode(); const s = (r.getSelection && r.getSelection())"
            " || document.getSelection(); if (!s || !s.anchorNode || !within(s.anchorNode, el)) return 'unselected'; }"
            " return 'ok'; })()"
        )
        state = page.evaluate(script)
        if state == "none":
            raise cdp.CDPError("no element under the point")
        if state != "ok":
            what = "no input field takes the focus" if editable else "the element did not take the focus"
            raise cdp.CDPError(f"{what} under the point: no key was sent")

    def _visible_point(self, e: snapshot.Element, refresh: bool = False) -> tuple[int, int] | None:
        """Centre de la partie de l'élément visible dans la page (None : hors de la zone visible)."""
        if refresh:
            with contextlib.suppress(*EXPECTED):
                e.node.props.update(self._refresh(e.node.element))
        rect = e.rect
        if rect is None or e.props.get("offscreen"):
            return None
        visible = rect.intersect(self._web_viewport())
        return visible.center if visible is not None else None

    def _cdp_focus_field(self, page: cdp.Page, e: snapshot.Element) -> None:
        """Focus DOM du champ par sa partie visible ; hors de la zone visible (ou rien de focalisable au point), il est
        ramené à l'écran (UIA ScrollIntoView) puis le focus est réessayé. Échec : refus avec la cause."""
        cause = "outside the visible area of the page"
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
                raise cdp.CDPError(f"{cause}; ScrollIntoView impossible: {_short(error)}") from None
            self._settle(lambda: self._visible_point(e, refresh=True) is not None)  # position publiée avec retard
        raise cdp.CDPError(f"{cause}, even after ScrollIntoView")

    @staticmethod
    def _cdp_refuse_secret(page: cdp.Page) -> None:
        if page.evaluate("(() => { " + DEEP_ACTIVE_JS + " return !!a && a.type === 'password'; })()"):
            raise ActionError("The focused field is a password field: I do not write in it (security rule).")

    # --- Activation simulée : clavier sans activation réelle (methods.simulated_activation) ----------------------
    def _sim_top(self, e: snapshot.Element | None = None) -> int:
        """Fenêtre de premier niveau qui reçoit les messages d'activation simulée et le clavier : celle de l'élément
        (surgissante ou dialogue de la cible), sinon la fenêtre principale."""
        window = e.window if e is not None and e.window in self.windows else self.hwnd
        return w.root(window) or self.hwnd

    def _sim_activation(
        self, e: snapshot.Element | None = None
    ) -> contextlib.AbstractContextManager[methods.SimulatedActivation]:
        return methods.simulated_activation(self._sim_top(e), self.windows)

    def _take_focus(self, e: snapshot.Element, timeout_s: float = 0.5) -> None:
        """Focus DOM de l'élément sans activer la fenêtre (accSelect), RELU : sans focus prouvé, rien n'est tapé (les
        caractères iraient à l'élément qui avait le focus avant)."""
        try:
            self.auto.take_focus(e.node.element)
        except EXPECTED as error:
            raise methods.MethodError(f"focus without activation impossible ({_short(error)})") from None
        deadline = time.monotonic() + timeout_s
        while True:
            with contextlib.suppress(*EXPECTED):
                if self._refresh(e.node.element).get("focused"):  # publication déclenchée (fenêtre occultée)
                    return
            if time.monotonic() >= deadline:
                raise methods.MethodError("focus without activation not read back on the element: nothing was typed")
            time.sleep(0.02)

    def _sim_focus(self, e: snapshot.Element) -> str:
        started = time.perf_counter()
        with self._sim_activation(e):
            self._take_focus(e)
        return f"read back ✓ focus, without activation (simulated activation) {round((time.perf_counter() - started) * 1000)} ms"

    # --- Texte --------------------------------------------------------------------------------------------------
    def _is_multiline_chromium(self, e: snapshot.Element) -> bool:
        """<textarea> et éditeurs riches sont indiscernables pour UIA (Edit, textbox, multiline) ; UIA Value ne
        prévient pas un éditeur riche → frappes pour tout champ multiligne Chromium."""
        aria = str(e.props.get("aria_properties") or "").lower()
        return framework_of(e, self.view) == "Chromium" and (
            "multiline=true" in aria or e.role in ("Document", "Group", "Custom") or not e.props.get("has_value")
        )

    def _cdp_write(self, page: cdp.Page, e: snapshot.Element, text: str, replace: bool) -> None:
        self._cdp_focus_field(page, e)
        self._cdp_refuse_secret(page)
        if replace:
            page.evaluate(SELECT_ALL_JS)
            if not text:
                page.key("Delete", "Delete", 0x2E)
        else:
            page.evaluate(
                "(() => { "
                + DEEP_ACTIVE_JS
                + " if (a && a.setSelectionRange) a.setSelectionRange(a.value.length, a.value.length); })()"
            )
        if text:
            page.insert_text(text)

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

    def _select_by_typing(self, e: snapshot.Element, option: str) -> Outcome:
        """<select> Chromium : ses options ne sont pas exposées et sa liste ne s'ouvre pas en arrière-plan. Focus puis
        frappe du libellé : la recherche au clavier du <select> choisit l'option et émet `change` comme un humain."""
        page = self._get_page()
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
                activation.check("nothing was typed, the action is cancelled")
                methods.post_chars(methods.keyboard_target(self.hwnd, self.windows), option)
                self._settle(lambda: snapshot.norm(self._read_value(e) or "").lower() == option.strip().lower())
            return f"brief activation {round((time.perf_counter() - started) * 1000)} ms"

        def typing_simulated() -> str:
            """Même frappe, sans activation : focus accSelect relu, activation simulée (premier plan intact)."""
            started = time.perf_counter()
            with self._sim_activation(e) as sim:
                self._take_focus(e)
                sim.check("nothing was typed, the action is cancelled")
                methods.post_chars(self._sim_top(e), option, check=sim.checkpoint)
                self._settle(lambda: snapshot.norm(self._read_value(e) or "").lower() == option.strip().lower())
            return f"without activation (simulated activation) {round((time.perf_counter() - started) * 1000)} ms"

        if page is None:  # DevTools disponible : jamais de frappes (activation brève, que devtools=true évite)
            chain.append(("keystrokes without activation", typing_simulated))
            chain.append(("keystrokes", typing))
        what = f'"{option}" chosen in {e.describe()}'
        outcome = self._try(chain, what)
        if self._value_is(e, option):
            outcome.verified = True
            outcome.line += " — read back ✓"
            return outcome
        outcome.ok = False
        outcome.line += (
            " — READ-BACK FAILED: run again with foreground=true (the user's consent) or launch the application with "
            "devtools=true"
        )
        return outcome

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
            raise cdp.CDPError("option not found (or duplicated) in the <select>")
