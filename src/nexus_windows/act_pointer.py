"""Points, souris et défilement : élément de la CIBLE sous un point (jamais une autre fenêtre), clic par messages
à la fenêtre réellement sous le point, molette ou UIA Scroll relu, glisser et survol (DevTools ou vraie souris)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from . import keys, methods, realinput, snapshot, uia, worker
from . import win32 as w
from .act_base import (
    BROWSERS_WITHOUT_MESSAGE_WHEEL,
    EXPECTED,
    LEAF_ROLES,
    NO_MOUSE_MESSAGES,
    SCROLL_ROLES,
    ActionError,
    ExecutorBase,
    Outcome,
    Unconfirmed,
    _covers,
    _hit_in,
    _locate,
    _roots_top_down,
    _short,
    _top_down,
)


class PointerActions(ExecutorBase):
    """Clics par point, défilement, glisser, survol."""

    # --- Clic ---------------------------------------------------------------------------------------------------
    def click(self, spec: Any, button: str = "left", count: int = 1) -> Outcome:
        kind, target = self.resolve(spec)
        if kind == "point":
            return self.click_point(*target, button=button, count=count)
        return self.click_element(target, button=button, count=count)

    def _point_of(self, e: snapshot.Element) -> tuple[int, int]:
        """Point visé pour un élément : centre de sa partie visible dans sa fenêtre. Hors champ (défilé hors vue, ou
        sans position), il est d'abord ramené à l'écran (UIA ScrollIntoView) puis RELU ; toujours hors champ : refus,
        rien n'est envoyé (le centre de l'ancien rectangle viserait l'élément qui occupe désormais ce point)."""
        if e.rect is None or e.props.get("offscreen"):
            cause = ""
            try:
                self.auto.scroll_into_view(e.node.element)
            except EXPECTED as error:
                cause = f" ; ScrollIntoView impossible : {_short(error)}"
            try:
                e.node.props.update(self._refresh(e.node.element))
            except EXPECTED as error:
                cause += f" ; relecture impossible : {_short(error)}"
            if e.rect is None or e.props.get("offscreen"):
                raise ActionError(
                    f"{e.describe()} est hors de la zone visible, même après ScrollIntoView{cause} : rien n'a été "
                    "envoyé. Faire défiler (scroll) jusqu'à lui, puis relire (look)."
                )
        rect = e.rect
        if rect is None:
            raise ActionError(f"{e.describe()} n'a pas de position à l'écran.")
        window = e.window if e.window in self.windows else self.hwnd
        if not w.IsWindow(window):  # fenêtre disparue : rien pour découper, l'envoi échouera de lui-même
            return rect.center
        visible = rect.intersect(w.frame_rect(window))
        if visible is None:
            raise ActionError(
                f"{e.describe()} est en dehors de sa fenêtre (défilé hors vue ?) : rien n'a été envoyé. Faire défiler "
                "(scroll) jusqu'à lui, puis relire (look)."
            )
        return visible.center

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
        self._confirm_point(x, y, button)
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

    def _real_write(self, e: snapshot.Element, text: str, replace: bool) -> None:
        realinput.click(*self._point_of(e))
        time.sleep(0.05)
        if replace:
            realinput.press(keys.parse("ctrl+a"))
            realinput.press(keys.parse("delete"))
        else:
            realinput.press(keys.parse("ctrl+end"))
        realinput.type_text(text)

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
        point: tuple[int, int] | None = None
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
        scroll_uia: Callable[[], str] | None = None
        if container is not None:
            step = 4  # ScrollAmount_SmallIncrement
            back = 1  # ScrollAmount_SmallDecrement

            def _scroll_uia() -> str:
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

            scroll_uia = _scroll_uia

        # Point final (techniques ci-dessous) : celui donné, sinon le centre de l'élément ou de la fenêtre.
        aim: tuple[int, int] = point or (
            element.rect.center if element is not None and element.rect else view.info.rect.center
        )
        delta = 120 * amount * (1 if direction in ("up", "left") else -1)
        horizontal = not vertical
        page = self._get_page()
        if page is not None:  # DevTools d'abord : molette de confiance, effet immédiat

            def scroll_cdp() -> None:
                cx, cy = self._css_point(page, *aim)
                page.wheel(cx, cy, -delta if not horizontal else 0, -delta if horizontal else 0)

            chain.append(("DevTools molette", scroll_cdp))
        if scroll_uia is not None:
            chain.append(("UIA Scroll", scroll_uia))
        process = w.process_name(w.window_pid(self.hwnd)).lower()
        if view.framework not in NO_MOUSE_MESSAGES and process not in BROWSERS_WITHOUT_MESSAGE_WHEEL:
            chain.append(
                (
                    "molette par messages",
                    self._watched(lambda: methods.post_wheel(self._window_under(*aim), *aim, delta, horizontal)),
                )
            )
        return self._try_or_foreground(
            chain, f"défilement {direction} ×{amount}{where}", lambda: realinput.wheel(*aim, delta, horizontal)
        )

    # --- Glisser et survol : DevTools si disponible, sinon vraie souris -------------------------------------------
    def _point(self, spec: Any) -> tuple[int, int]:
        kind, target = self.resolve(spec)
        return target if kind == "point" else self._point_of(target)

    def drag(self, start: Any, end: Any) -> Outcome:
        x1, y1 = self._point(start)
        x2, y2 = self._point(end)
        page = self._get_page()
        if page is not None:
            a, b = self._css_point(page, x1, y1), self._css_point(page, x2, y2)
            page.drag(*a, *b)
            return Outcome(True, "glisser-déposer (DevTools)", "DevTools")
        return self._foreground_or_fail("glisser-déposer", lambda: realinput.drag(x1, y1, x2, y2))

    def hover(self, spec: Any) -> Outcome:
        x, y = self._point(spec)
        page = self._get_page()
        if page is not None:
            page.hover(*self._css_point(page, x, y))
            return Outcome(True, "survol (DevTools)", "DevTools")
        return self._foreground_or_fail("survol", lambda: realinput.move(x, y))
