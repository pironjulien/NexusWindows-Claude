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
    NO_WHEEL_MESSAGES,
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

REAL_TYPING_CHUNK = 32  # caractères tapés au vrai clavier entre deux relectures du focus


class ProofMarkerLeft(Unconfirmed):
    """La preuve DevTools a pu laisser son marqueur dans le titre de la page, ou sa remise n'a pas été vue dans la
    fenêtre : aucune autre technique, ni la vraie souris, par-dessus."""


def _lines_of(area: uia.Node) -> list[uia.Node]:
    """Lignes (feuilles placées) de la zone, à toute profondeur, sauf celles d'une zone intérieure qui les rogne : une
    ligne n'est à `area` que si chaque conteneur entre elles la contient (sinon elle défile dans ce conteneur, pas dans
    `area` : banc du 23/10, la molette sur un canevas faisait défiler la liste voisine par le document)."""
    out: list[uia.Node] = []

    def inside(rect: w.Rect, box: w.Rect) -> bool:
        return rect.top >= box.top - 1 and rect.bottom <= box.bottom + 1

    def visit(node: uia.Node, boxes: list[w.Rect]) -> None:
        for child in node.children:
            rect = child.rect
            if rect is None:
                continue
            if not child.children:
                if all(inside(rect, box) for box in boxes):
                    out.append(child)
            else:
                visit(child, [*boxes, rect])

    visit(area, [])
    return out


class PointerActions(ExecutorBase):
    """Clics par point, défilement, glisser, survol."""

    # Clic par point confié à l'élément qui le couvre : (élément, point donné). Ses gestes au point (messages, DevTools,
    # vraie souris) visent CE point, pas le centre de l'élément (banc du 23/10, Electron recouvert : l'arbre
    # d'accessibilité d'une page occultée n'est plus à jour, le centre d'une ligne « clic 5 » manquait le canevas).
    _aim_at: tuple[snapshot.Element, tuple[int, int]] | None = None

    # --- Clic ---------------------------------------------------------------------------------------------------
    def click(self, spec: Any, button: str = "left", count: int = 1) -> Outcome:
        kind, target = self.resolve(spec)
        if kind == "point":
            return self.click_point(*target, button=button, count=count)
        return self.click_element(target, button=button, count=count)

    def _point_of(self, e: snapshot.Element) -> tuple[int, int]:
        """Point visé pour un élément : centre de sa partie visible dans sa fenêtre. Hors champ (défilé hors vue, ou
        sans position), il est d'abord ramené à l'écran (UIA ScrollIntoView) puis RELU ; toujours hors champ : refus,
        rien n'est envoyé (le centre de l'ancien rectangle viserait l'élément qui occupe désormais ce point). Clic par
        point confié à cet élément : le point donné (`_aim_at`)."""
        if self._aim_at is not None and self._aim_at[0] is e:
            return self._aim_at[1]
        if e.rect is None or e.props.get("offscreen"):
            cause = ""
            try:
                self.auto.scroll_into_view(e.node.element)
            except EXPECTED as error:
                cause = f"; ScrollIntoView failed: {_short(error)}"
            try:
                e.node.props.update(self._refresh(e.node.element))
            except EXPECTED as error:
                cause += f"; read-back failed: {_short(error)}"
            if e.rect is None or e.props.get("offscreen"):
                raise ActionError(
                    f"{e.describe()} is outside the visible area, even after ScrollIntoView{cause}: nothing was "
                    "sent. Scroll to it, then read again (look)."
                )
        rect = e.rect
        if rect is None:
            raise ActionError(f"{e.describe()} has no position on screen.")
        window = e.window if e.window in self.windows else self.hwnd
        if not w.IsWindow(window):  # fenêtre disparue : rien pour découper, l'envoi échouera de lui-même
            return rect.center
        visible = rect.intersect(w.frame_rect(window))
        if visible is None:
            raise ActionError(
                f"{e.describe()} is outside its window (scrolled out of view?): nothing was sent. Scroll "
                "to it, then read again (look)."
            )
        return visible.center

    def _window_under(self, x: int, y: int) -> int:
        """Fenêtre de la cible sous un point écran, par la géométrie : surgissantes et dialogues du haut vers le bas,
        puis la fenêtre principale. Pas window_at seul : un dialogue de la cible recouvert par une fenêtre de l'utilisateur
        doit rester visé, et jamais la fenêtre principale SOUS une surgissante de la cible."""
        for window in _top_down(self.windows - {self.hwnd}, self.view.windows[1:]):
            if _covers(window, x, y):
                return window
        return self.hwnd

    def _post_click(self, x: int, y: int, button: str = "left", count: int = 1) -> int:
        """Clic par messages à la fenêtre de la cible réellement sous le point (puis à son enfant le plus profond)."""
        return methods.post_click(self._window_under(x, y), x, y, button, count, held=self.view.framework == "Tk")

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
            if element is not None:
                # Point donné par le modèle : il fait foi. Élément trouvé dans un instantané périmé (page défilée depuis,
                # fenêtre recouverte : banc du 23/10, Electron, une ligne « clic 5 » à la place du canevas) : relu, il
                # n'est retenu que s'il couvre ENCORE le point ; sinon le clic part au point lui-même.
                try:
                    element.node.props.update(self._refresh(element.node.element))
                except EXPECTED:
                    element = None  # relecture impossible : son ancien rectangle n'est jamais visé (relecture de Codex)
                if element is not None and (element.rect is None or not element.rect.contains(x, y)):
                    element = None
            window_area = max(1, view.info.rect.width * view.info.rect.height)
            if (
                element is not None
                and element.rect is not None
                and element.rect.width * element.rect.height < window_area / 4
            ):
                self._aim_at = (element, (x, y))
                try:
                    outcome = self.click_element(element, button, count)
                finally:
                    self._aim_at = None
                outcome.line += f" at point {image_xy}"
                return outcome
        chain: list[tuple[str, Callable[[], Any]]] = []
        page = self._page_if_free()
        if page is not None:
            chain.append(("DevTools", lambda: self._cdp_click(page, x, y, button, count)))
        if view.framework not in NO_MOUSE_MESSAGES:
            chain.append(("click by messages", self._watched(lambda: self._post_click(x, y, button, count))))
        label = {"left": "click", "right": "right click", "middle": "middle click"}[button]
        return self._try_or_foreground(
            chain, f"{label} at point {image_xy}", lambda: realinput.click(x, y, button, count)
        )

    def _real_write(self, e: snapshot.Element, text: str, replace: bool) -> None:
        realinput.click(*self._point_of(e))
        time.sleep(0.05)
        # Le clic a-t-il donné le focus à CE champ (ou à son champ intérieur) ? Élément déplacé, remplacé ou disparu,
        # surgissante : Ctrl+A puis Suppr videraient un autre champ (relectures, tours 3 et 4) → rien n'est tapé.
        if e.node is not None and not self._focus_reaches(e, wait_s=0.3, real=True):
            raise ActionError(f"{e.describe()} did not take the focus after the click: nothing was typed.")

        def still_focused(step: str) -> None:
            # Relu avant CHAQUE envoi, pas seulement après le clic : Ctrl+A peut déplacer le focus (surgissante,
            # autocomplétion) et Suppr ou le texte partiraient ailleurs (relecture de Codex, W6).
            if e.node is not None and not self._focus_reaches(e, real=True):
                raise ActionError(f"{e.describe()} lost the focus before {step}: the typing stopped there.")

        if replace:
            realinput.press(keys.parse("ctrl+a"))
            still_focused("Delete")
            realinput.press(keys.parse("delete"))
        else:
            realinput.press(keys.parse("ctrl+end"))
        for start in range(0, len(text), REAL_TYPING_CHUNK):
            still_focused("typing")
            realinput.type_text(text[start : start + REAL_TYPING_CHUNK])

    # --- Défilement ---------------------------------------------------------------------------------------------
    def _proven_wheel(self, aim: tuple[int, int], delta: int, horizontal: bool) -> str:
        """Molette DevTools dans une fenêtre Chromium que l'outil n'a pas lancée (banc commun du 21/10, recette B de
        Codex) : port déclaré par le processus de la fenêtre et écouté par lui seul, page désignée par son titre puis
        prouvée par un titre aléatoire vu dans CETTE fenêtre, rapport de pixels lisible. Toute preuve manquante : rien
        n'est envoyé (MethodError). Un seul envoi ; sans accusé : issue inconnue, jamais rejouée ni suivie d'une autre
        technique."""
        from . import cdp, launch

        pid = w.window_pid(self.hwnd)
        port = cdp.port_from_command_line(cdp.process_command_line(pid))
        if port is None:
            raise methods.MethodError("no DevTools port declared by the window's process")
        if not cdp.owner_proven(pid, port, launch.listeners()):
            raise methods.MethodError(f"DevTools port {port} is not listened to by the window's own process alone")
        try:
            target = cdp.proven_page(cdp.targets(port), w.window_text(self.hwnd), port)
            page = cdp.Page(port, target=target, timeout_s=2.0)
        except cdp.CDPError as error:
            raise methods.MethodError(str(error)) from None
        try:
            try:
                cdp.prove_window(
                    page,
                    lambda: w.window_text(self.hwnd),
                    reconnect=lambda: cdp.Page(port, target=target, timeout_s=2.0),
                )
            except cdp.MarkerLeft as error:
                # titre peut-être resté marqué, ou remise non vue : terminal aux DEUX niveaux, ni technique suivante ni
                # vraie souris (Unconfirmed : _try et _try_or_foreground le laissent remonter ; relecture de Codex)
                raise ProofMarkerLeft(str(error)) from error
            metrics = page.evaluate("[window.devicePixelRatio, window.innerWidth, window.innerHeight]")
            if not isinstance(metrics, list) or len(metrics) != 3:
                raise methods.MethodError("page metrics unreadable: nothing was sent")
            ratio, inner_w, inner_h = metrics
            if isinstance(ratio, bool) or not isinstance(ratio, int | float) or not 0.1 < float(ratio) < 16:
                raise methods.MethodError("devicePixelRatio unreadable: nothing was sent")  # jamais 1 supposé
            rect, measured = self._proven_viewport(aim, float(ratio), inner_w, inner_h)
            cx, cy = (aim[0] - rect.left) / float(ratio), (aim[1] - rect.top) / float(ratio)
            worker.check_cancelled()
            try:
                page.wheel(cx, cy, -delta if not horizontal else 0, -delta if horizontal else 0)
            except cdp.CDPError as error:
                raise Unconfirmed(
                    f"DevTools wheel sent without acknowledgement ({error}): outcome unknown, check the image before any "
                    "new attempt."
                ) from None
        finally:
            page.close()
        return f"proven page, port {port}; {measured}"

    def _proven_viewport(self, aim: tuple[int, int], ratio: float, inner_w: Any, inner_h: Any) -> tuple[w.Rect, str]:
        """Zone de rendu de la page PROUVÉE (règle commune aux trois NexusWindows, relecture de Codex du 21/10) :
        exactement UNE zone Chrome_RenderWidgetHostHWND VISIBLE dans toute la fenêtre, le point dedans, taille physique
        = round(innerWidth × DPR) × round(innerHeight × DPR) à ceil(DPR) px près. Plusieurs zones visibles (vues
        multiples, Steam…) : refus, hors périmètre. Rend la zone et le relevé (innerWidth/innerHeight, DPR, rectangle,
        écart) pour le compte rendu ; sinon rien n'est envoyé."""
        import math

        if not all(isinstance(v, int | float) and not isinstance(v, bool) and v > 0 for v in (inner_w, inner_h)):
            raise methods.MethodError("page size unreadable: nothing was sent")
        visible = [
            c
            for c in w.enum_children(self.hwnd)
            if w.class_name(c) == "Chrome_RenderWidgetHostHWND" and w.IsWindowVisible(c)
        ]
        if len(visible) != 1:
            raise methods.MethodError(
                f"{len(visible)} visible render areas in the window (exactly one required): nothing was sent"
            )
        r = w.window_rect(visible[0])
        want_w, want_h = round(inner_w * ratio), round(inner_h * ratio)
        gap = max(abs(r.width - want_w), abs(r.height - want_h))
        measured = (
            f"page {inner_w}×{inner_h} CSS, DPR {ratio:g}, area ({r.left},{r.top},{r.right},{r.bottom}) "
            f"{r.width}×{r.height} px, gap {gap} px"
        )
        x, y = aim
        if not (r.left <= x < r.right and r.top <= y < r.bottom):
            raise methods.MethodError(f"the point is outside the page's render area ({measured}): nothing was sent")
        if gap > math.ceil(ratio):
            raise methods.MethodError(f"render area does not match the proven page ({measured}): nothing was sent")
        return r, measured

    def _scroll_container_at(self, x: int, y: int) -> uia.Node | None:
        """Conteneur défilable de la CIBLE au point : jamais celui d'une fenêtre de l'utilisateur qui la recouvre, ni celui
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

    LINES_PER_NOTCH = 3  # un cran de molette Windows : 3 lignes (réglage par défaut)

    def _scroll_by_item(self, x: int, y: int, direction: str, amount: int) -> str:
        """Défiler sans molette, comme un lecteur d'écran : dans la zone sous le point dont des lignes dépassent dans ce
        sens, la ligne cachée à 3 lignes par cran est amenée dans la vue (UIA ScrollItem.ScrollIntoView), puis sa position
        est RELUE. Aucune zone de ce genre : MethodError, rien n'est envoyé. Envoyé sans mouvement relu en 1 s : issue
        inconnue (~), jamais « ✓ » (page occultée qui ne publie plus son arbre)."""
        if direction not in ("up", "down"):
            raise methods.MethodError("only up and down")
        # Arbre COMPLET et frais de la fenêtre : l'instantané de l'outil écarte les lignes hors vue, celles qu'on cherche.
        node: uia.Node | None = self.auto.tree(self._window_under(x, y))
        while node is not None:
            # Enfants qui couvrent le point : celui qui a lui-même des enfants d'abord (un volet vide de Chromium couvre
            # toute la page à côté du document).
            covering = [c for c in node.children if c.rect is not None and c.rect.contains(x, y)]
            inner = next((c for c in covering if c.children), covering[0] if covering else None)
            if inner is None:
                break
            node = inner
        hidden: list[uia.Node] = []
        while node is not None:
            rect = node.rect
            # Lignes = feuilles de la zone, à toute profondeur : un unique enveloppant de contenu entre la zone et ses
            # lignes est courant (relecture de Codex du 23/10). Chromium publie des rectangles non rognés : une ligne
            # qui dépasse de la zone est cachée par elle.
            lines = _lines_of(node)
            if rect is not None and len(lines) >= 2:
                if direction == "down":
                    hidden = sorted((n for n in lines if n.rect.bottom > rect.bottom + 1), key=lambda n: n.rect.top)
                else:
                    hidden = sorted((n for n in lines if n.rect.top < rect.top - 1), key=lambda n: -n.rect.bottom)
                if hidden:
                    break
            node = node.parent
        if not hidden:
            raise methods.MethodError("no area under the point has hidden lines in this direction")
        target = hidden[min(amount * self.LINES_PER_NOTCH, len(hidden)) - 1]
        area = node
        assert target.rect is not None and area is not None and area.rect is not None
        # Position de la ligne DANS sa zone : ScrollIntoView fait aussi défiler la page autour, ce qui déplace la ligne à
        # l'écran sans que la zone ait défilé (Electron, 23/10) ; seul l'écart ligne-zone prouve le défilement de la zone.
        before = target.rect.top - area.rect.top
        worker.check_cancelled()
        scroll_item = self.auto.pattern(target.element, "scroll_item")  # absent : refus, rien n'est envoyé
        worker.check_cancelled()  # arrêt d'urgence pendant l'acquisition : rien n'est invoqué
        try:
            scroll_item.ScrollIntoView()
        except EXPECTED as error:
            # Erreur APRÈS l'appel : il a pu agir (retour perdu) ; issue inconnue, aucune autre technique par-dessus.
            raise Unconfirmed(
                f"scroll {direction}: ScrollIntoView sent, then {type(error).__name__}: check the image before trying "
                "again."
            ) from None
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            time.sleep(0.05)
            try:
                line = uia.Node(target.element, 0, self._refresh(target.element)).rect
                box = uia.Node(area.element, 0, self._refresh(area.element)).rect
            except EXPECTED:
                continue
            if line is not None and box is not None and line.top - box.top != before:
                return (
                    f"read back ✓ {snapshot.clip(target.name, 30)!r} moved {before - (line.top - box.top):+d} px in "
                    "its area"
                )
        raise Unconfirmed(
            f"scroll {direction}: line brought into view (UIA ScrollItem), its area did not scroll within 1 s: check "
            "the image before trying again."
        )

    def _scroll_by_bar(self, x: int, y: int, direction: str, amount: int) -> str:
        """Défiler par la barre de défilement native en face du point (Tk) : lignes envoyées comme un clic sur sa
        flèche, position RELUE. Aucune barre en face : MethodError ; déjà en butée : MethodError, rien n'est envoyé ;
        envoyé sans mouvement relu : issue inconnue (~)."""
        vertical = direction in ("up", "down")
        forward = direction in ("down", "right")
        bar = methods.scroll_bar_beside(self.hwnd, x, y, vertical)
        if not bar:
            raise methods.MethodError("no native scroll bar drives what is under the point")
        state = methods.scroll_bar_state(bar)
        if state is None:
            raise methods.MethodError("the scroll bar position cannot be read")
        low, high, page, pos = state
        end = max(low, high - max(int(page), 1) + 1)
        if (forward and pos >= end) or (not forward and pos <= low):
            raise methods.MethodError(f"the scroll bar is already at the {'end' if forward else 'start'}")
        worker.check_cancelled()
        methods.post_scroll_lines(bar, vertical, forward, amount * self.LINES_PER_NOTCH)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            time.sleep(0.05)
            after = methods.scroll_bar_state(bar)
            if after is not None and after[3] != pos:
                return f"read back ✓ position {pos} → {after[3]}"
        raise Unconfirmed(
            f"scroll {direction}: scroll bar lines sent, position unchanged after 1 s: check the image before trying "
            "again."
        )

    def _covered_blind(self, view: snapshot.View, x: int, y: int) -> bool:
        """Le point est sous une fenêtre étrangère à la cible, aucune autre fenêtre de la cible (surgissante, palette)
        ne le contient, et l'arbre de la cible n'y trouve aucun élément : la cible n'y montre rien de lisible."""
        if w.window_at(x, y) in self.windows:
            return False
        for other in self.windows:
            if other != self.hwnd and w.IsWindowVisible(other):
                r = w.window_rect(other)
                if r.left <= x < r.right and r.top <= y < r.bottom:
                    return False
        return self._hit(view, x, y) is None

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
            raise ActionError("direction: up, down, left, right or into_view.")
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
                raise ActionError("into_view requires an element.")
            return self._try(
                [("UIA ScrollIntoView", lambda: self.auto.scroll_into_view(element.node.element))],
                f"{element.describe()} brought into view",
            )
        container = None
        aimed = point is not None  # point "x,y" donné par le modèle (pas le centre de la fenêtre choisi ci-dessous)
        if element is not None:
            probe: uia.Node | None = element.node
            while probe is not None and container is None:
                if probe.props.get("has_scroll"):
                    container = probe
                probe = probe.parent
        elif point is not None:
            container = self._scroll_container_at(*point)
            if container is None and self._covered_blind(view, *point):
                # Point recouvert par une fenêtre ÉTRANGÈRE à la cible et rien de la cible lu sous lui (Paramètres masqués
                # derrière la fenêtre de Julien, passe 2 du 23/10) : le grand conteneur défilable s'il contient le point.
                # Jamais sous une surface de la cible (palette, voile, surgissante) : relecture de Codex.
                main = self._main_scroll_container(view)
                rect = main.rect if main is not None else None
                if rect is not None and rect.left <= point[0] < rect.right and rect.top <= point[1] < rect.bottom:
                    container = main
        else:
            # Sans cible : le conteneur sous le centre de la fenêtre, là où visent aussi DevTools et la molette (pas le
            # premier de l'arbre, souvent un panneau latéral) ; à défaut le plus grand, et la molette vise son centre.
            point = view.info.rect.center
            container = self._scroll_container_at(*point)
            if container is None:
                container = self._main_scroll_container(view)
                if container is not None and container.rect is not None:
                    point = container.rect.center
        where = f" of {snapshot.Element(0, container, 0, self.hwnd).describe()}" if container is not None else ""
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
                    raise methods.MethodError("this container does not scroll in this direction")
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
                    return "position not read back"
                if after != before:
                    return f"read back ✓ position {before[axis] if before else 0:.1f} % → {after[axis]:.1f} %"
                at_end = after[axis] >= 99.9 if direction in ("down", "right") else after[axis] <= 0.1
                if at_end and aimed:
                    # Point visé par le modèle (canevas, carte…) : rien n'a bougé et l'élément sous le point n'a reçu
                    # aucune molette (banc du 20/10 : un « ✓ » ici était un faux succès). Aucun effet de bord (position
                    # inchangée, en butée) : la technique suivante (molette par message au point) peut s'appliquer,
                    # sans risque de double défilement (banc commun du 21/10 : canevas Electron).
                    raise methods.MethodError(
                        f"container already at the end ({after[axis]:.0f} %): the wheel must reach what is under "
                        "the point"
                    )
                if at_end:
                    return f"read back ✓ already at the end ({after[axis]:.0f} %)"
                # Défilement transmis sans effet relu : aucune autre technique par-dessus (double défilement possible).
                raise Unconfirmed(
                    f"scroll {direction}: UIA Scroll sent, position unchanged ({after[axis]:.0f} %) after 1 s: "
                    "check the image before trying again."
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

            chain.append(("DevTools wheel", scroll_cdp))
        elif view.framework == "Chromium":
            # Fenêtre Chromium lancée par un autre avec un port de débogage : page PROUVÉE d'abord (recette B de Codex) ;
            # non prouvée : rien n'est envoyé, le défilement sémantique suit.
            chain.append(("DevTools wheel (proven page)", lambda: self._proven_wheel(aim, delta, horizontal)))
        if view.framework == "Chromium" and page is None:
            # Molette d'une page recouverte jetée par Chromium (RerouteMouseWheel), zones qui défilent sans ScrollPattern :
            # la ligne cachée suivante de la zone sous le point est amenée dans la vue, puis relue (mesuré le 23/10).
            chain.append(("UIA ScrollItem", lambda: self._scroll_by_item(*aim, direction, amount)))
        if scroll_uia is not None:
            chain.append(("UIA Scroll", scroll_uia))
        if view.framework == "Tk":
            # Molette Tk renvoyée à la fenêtre sous le point (recouverte : perdue) ; une barre de défilement native
            # (tk.Scrollbar) défile par ses messages de ligne, position relue (mesuré le 23/10).
            chain.append(("scroll bar lines", lambda: self._scroll_by_bar(*aim, direction, amount)))
        process = w.process_name(w.window_pid(self.hwnd)).lower()
        if view.framework not in NO_WHEEL_MESSAGES and process not in BROWSERS_WITHOUT_MESSAGE_WHEEL:
            chain.append(
                (
                    "wheel by messages",
                    self._watched(lambda: methods.post_wheel(self._window_under(*aim), *aim, delta, horizontal)),
                )
            )
        return self._try_or_foreground(
            chain, f"scroll {direction} ×{amount}{where}", lambda: realinput.wheel(*aim, delta, horizontal)
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
            return Outcome(True, "drag and drop (DevTools)", "DevTools")
        return self._foreground_or_fail("drag and drop", lambda: realinput.drag(x1, y1, x2, y2))

    def hover(self, spec: Any) -> Outcome:
        x, y = self._point(spec)
        page = self._get_page()
        if page is not None:
            page.hover(*self._css_point(page, x, y))
            return Outcome(True, "hover (DevTools)", "DevTools")
        return self._foreground_or_fail("hover", lambda: realinput.move(x, y))
