"""Confirmation étanche (politique commune NexusHarness/shared/garde-fous.json, accord Claude–Antigravity du 18/10).

Un geste dont l'effet part chez un tiers, coûte de l'argent ou détruit (payer, envoyer, publier, supprimer
définitivement, formater, désinstaller, réinitialiser, résilier, passer un dépôt en public) exige confirm=true sur
l'étape, posé seulement sur l'ordre explicite de Julien ou après son oui. Le contrôle porte sur CE QUE LE GESTE
DÉCLENCHE, pas sur sa forme, pour ne pas être contourné :
- clic (simple ou double) sur un élément, par [N], par nom ou en coordonnées : le déclencheur sous le point (bouton,
  lien, élément de menu, ou l'élément lui-même s'il est générique), un texte dans un bouton comptant pour le bouton ;
- touches : Entrée (élément visé ou qui a le focus, bouton par défaut, bouton d'action voisin du champ), Ctrl+Entrée
  (tout bouton d'action de la fenêtre), Espace (élément au focus), Alt+lettre (raccourci d'un bouton ou d'un menu) ;
- saisie d'un texte qui contient un saut de ligne : comme Entrée dans ce champ ;
- pages DevTools : mêmes questions posées au DOM (élément actif, boutons d'envoi de son formulaire, élément au point).
Un élément de liste, d'arbre, un onglet, un champ ou une case ne déclenche rien (rôles non actifs) ; un élément de menu
qui ouvre un sous-menu non plus (« Envoyer vers »). Politique illisible : refus (échec fermé).
"""

from __future__ import annotations

import json

from . import cdp, keys, policy, snapshot, uia
from . import win32 as w
from .act_base import ActionError, ExecutorBase, _locate, _roots_top_down

garde_fous = policy.garde_fous
STATE_SYSTEM_DEFAULT = 0x100
EXPAND_LEAF = 3  # ExpandCollapseState_LeafNode
NEIGHBOR_LEVELS = 3  # ancêtres du champ remontés pour trouver son bouton d'envoi (composeur, formulaire)
TRIGGER_DEPTH = 8  # ancêtres remontés depuis un texte ou une icône jusqu'à son bouton
VK_ENTER, VK_SPACE = 0x0D, 0x20
VK_CTRL, VK_ALT, VK_SHIFT, VK_WIN = 0x11, 0x12, 0x10, 0x5B


class ConfirmationRequired(ActionError):
    """Geste irréversible ou chez un tiers demandé sans confirm=true : rien n'a été fait."""


def combo_kind(combo: keys.Combo) -> str:
    """Ce qu'une combinaison peut déclencher : « submit », « send » (Ctrl+Entrée), « press » (Espace), « alt+x », « »."""
    mods = set(combo.modifiers)
    if combo.key == VK_ENTER:
        if mods & {VK_SHIFT, VK_ALT, VK_WIN}:
            return ""
        return "send" if VK_CTRL in mods else "submit"
    if combo.key == VK_SPACE and not mods:
        return "press"
    if mods == {VK_ALT} and combo.char and len(combo.char) == 1 and combo.char.isalnum():
        return f"alt+{combo.char.lower()}"
    return ""


def label_of(node: uia.Node) -> str:
    """Libellé d'un élément : son nom, sinon les textes de ses enfants (bouton à icône et texte séparé)."""
    if node.name:
        return node.name
    texts = [child.name for child in node.walk() if child is not node and child.name]
    return " ".join(texts[:4])


def _submenu(node: uia.Node) -> bool:
    return (
        node.control_type == "MenuItem"
        and bool(node.props.get("has_expand"))
        and node.props.get("expand")
        not in (
            None,
            EXPAND_LEAF,
        )
    )


def trigger_of(node: uia.Node) -> uia.Node | None:
    """Ce qu'un clic sur ce nœud déclenche : le bouton, lien ou élément de menu qui le porte (un texte ou une icône
    dans un bouton compte pour le bouton), sinon le nœud lui-même s'il a un rôle générique ; None pour un élément non
    actif (liste, arbre, onglet, champ, case…) ou un élément de menu qui ouvre un sous-menu."""
    non_action = garde_fous.non_action_roles()
    triggers = garde_fous.trigger_roles()
    current: uia.Node | None = node
    for _ in range(TRIGGER_DEPTH):
        if current is None or current.control_type in ("Window", "Pane"):
            break
        role = current.control_type
        if role in non_action:
            return None
        if role in triggers or current.props.get("has_invoke"):
            return None if _submenu(current) else current
        current = current.parent
    return None if node.control_type in non_action else node


def _live_trigger(node: uia.Node) -> bool:
    return (
        node.control_type in garde_fous.trigger_roles()
        and node.props.get("enabled") is not False
        and not _submenu(node)
    )


def _all_nodes(view: snapshot.View) -> list[uia.Node]:
    return [node for _window, roots in _roots_top_down(view) for root in roots for node in root.walk()]


def _neighbors(field: uia.Node) -> list[uia.Node]:
    """Éléments d'action du groupe du champ : sous-arbre de son ancêtre au plus NEIGHBOR_LEVELS au-dessus, jamais la
    racine (toute la fenêtre)."""
    ancestor = field
    for _ in range(NEIGHBOR_LEVELS):
        if ancestor.parent is None or ancestor.parent.parent is None:
            break
        ancestor = ancestor.parent
    if ancestor is field:
        return []
    return [node for node in ancestor.walk() if node is not field and _live_trigger(node)]


def key_triggers(view: snapshot.View, field: uia.Node | None, kind: str) -> list[tuple[str, str]]:
    """(libellé, d'où) de ce que la touche (`kind` de combo_kind) déclencherait dans la fenêtre. Lecture seule."""
    out: list[tuple[str, str]] = []
    non_action = garde_fous.non_action_roles()
    if kind in ("submit", "send", "press") and field is not None and field.control_type not in non_action:
        out.append((label_of(field), "élément visé"))
    nodes = _all_nodes(view)
    if kind.startswith("alt+"):
        letter = garde_fous.plain(kind)
        for node in nodes:
            if _live_trigger(node) and garde_fous.plain(node.props.get("access_key")) == letter:
                out.append((label_of(node), f"raccourci {node.props.get('access_key')}"))
        return out
    for node in nodes:
        if node.props.get("focused") and _live_trigger(node):
            out.append((label_of(node), "élément qui a le focus"))
    if kind == "press":
        return out
    if kind == "send":
        return out + [(label_of(node), "bouton d'action de la fenêtre") for node in nodes if _live_trigger(node)]
    for node in nodes:
        if node.control_type in ("Button", "SplitButton") and _live_trigger(node):
            if int(node.props.get("legacy_state") or 0) & STATE_SYSTEM_DEFAULT:
                out.append((label_of(node), "bouton par défaut de la fenêtre"))
    if field is not None:
        out += [(label_of(node), "bouton d'action voisin du champ") for node in _neighbors(field)]
    return out


# DOM : ce que déclencherait une touche (élément actif, boutons d'envoi de son formulaire, voisins) ou un clic au point.
_DOM_TRIGGER = (
    "button,a[href],[role=button],[role=link],[role=menuitem],input[type=submit],input[type=button],input[type=image]"
)
_DOM_LABEL = "const lab = e => ((e.getAttribute('aria-label') || e.innerText || e.value || e.title || '') + '').trim().slice(0, 80);"
_DOM_KEY_JS = (
    "((kind) => { " + _DOM_LABEL + " const T = " + json.dumps(_DOM_TRIGGER) + ";"
    " const out = []; const a = document.activeElement;"
    " if (a && a.matches && a.matches(T)) out.push(lab(a));"
    " if (kind === 'press') return out;"
    " if (kind === 'send') { document.querySelectorAll('button,[role=button],input[type=submit]').forEach(b => out.push(lab(b))); return out; }"
    " if (kind.startsWith('alt+')) { document.querySelectorAll('[accesskey]').forEach(b => { if ((b.accessKey || '').toLowerCase() === kind.slice(4)) out.push(lab(b)); }); return out; }"
    " const form = a && (a.form || (a.closest && a.closest('form')));"
    " if (form) form.querySelectorAll('button:not([type=button]):not([type=reset]),input[type=submit],input[type=image]').forEach(b => out.push(lab(b)));"
    " let anc = a; for (let i = 0; i < 3 && anc && anc.parentElement && anc.parentElement !== document.body; i++) anc = anc.parentElement;"
    " if (anc && anc !== a) anc.querySelectorAll('button,[role=button],input[type=submit]').forEach(b => out.push(lab(b)));"
    " return out; })"
)
_DOM_POINT_JS = (
    "((x, y) => { " + _DOM_LABEL + " const e = document.elementFromPoint(x, y); if (!e) return [];"
    " const t = e.closest(" + json.dumps(_DOM_TRIGGER) + "); if (t) return [lab(t)];"
    " const own = lab(e); return own.length <= 40 ? [own] : []; })"
)


class ConfirmActions(ExecutorBase):
    """Contrôles de confirmation appelés par les actions (clic, touches, saisie) avant toute entrée envoyée."""

    def _title(self) -> str:
        return w.window_text(self.hwnd)

    def _refuse_if(self, what: str, labels: list[tuple[str, str]]) -> None:
        if self.confirm:
            return
        try:
            title = self._title()
            for label, source in labels:
                word = garde_fous.confirm_word(label, title)
                if word:
                    detail = f"{what} ({source})" if source else what
                    raise ConfirmationRequired(garde_fous.refusal(detail, label, word))
        except garde_fous.GardeFousError as error:
            raise ActionError(f"{error} : rien n'a été fait (échec fermé)") from error

    def _confirm_click(self, e: snapshot.Element, button: str = "left") -> None:
        """Clic gauche (simple ou double) sur un élément : refus sans confirm si ce qu'il déclenche est à confirmer."""
        if self.confirm or button != "left":
            return
        trigger = trigger_of(e.node)
        if trigger is not None:
            self._refuse_if("clic", [(label_of(trigger), "")])

    def _confirm_point(self, x: int, y: int, button: str = "left") -> None:
        """Clic en coordonnées : même contrôle que le clic sur l'élément sous le point (arbre UIA, puis DOM)."""
        if self.confirm or button != "left":
            return
        view = self._rebuild() if self.acted else self.view
        hit = self._hit(view, x, y)
        if hit is not None:
            trigger = trigger_of(hit[0])
            if trigger is not None:
                self._refuse_if("clic par coordonnées", [(label_of(trigger), "")])
        page = self._get_page()
        if page is not None:
            try:
                cx, cy = self._css_point(page, x, y)
                labels = page.evaluate(f"{_DOM_POINT_JS}({cx}, {cy})") or []
            except cdp.CDPError, TypeError, ValueError:
                labels = []
            self._refuse_if("clic par coordonnées", [(str(label), "page") for label in labels])

    def _field_node(self, element: snapshot.Element | None) -> uia.Node | None:
        """Nœud du champ dans l'instantané (avec ses ancêtres), même s'il vient d'une lecture directe du focus."""
        if element is None:
            return None
        if element.node.parent is not None:
            return element.node
        view = self._rebuild() if self.acted else self.view
        located = _locate(view, uia.runtime_id(element.props))
        return located[0] if located else element.node

    def _confirm_kinds(self, kinds: list[str], element: snapshot.Element | None, what: str) -> None:
        kinds = [kind for kind in dict.fromkeys(kinds) if kind]
        if self.confirm or not kinds:
            return
        field = self._field_node(element if element is not None else self._focused_target_element())
        view = self._rebuild() if self.acted else self.view
        page = self._get_page()
        for kind in kinds:
            labels = key_triggers(view, field, kind)
            if page is not None:
                try:
                    found = page.evaluate(f"{_DOM_KEY_JS}({json.dumps(kind)})") or []
                except cdp.CDPError, TypeError, ValueError:
                    found = []
                labels += [(str(label), "page") for label in found]
            self._refuse_if(f"{what} : déclencherait", labels)

    def _confirm_keys(self, combos: list[keys.Combo], spec: str, element: snapshot.Element | None) -> None:
        self._confirm_kinds([combo_kind(c) for c in combos], element, f"touche « {spec} »")

    def _confirm_text(self, element: snapshot.Element | None, text: str) -> None:
        """Un saut de ligne tapé dans un champ vaut Entrée (envoi d'une messagerie, validation d'un formulaire)."""
        if "\n" in text or "\r" in text:
            self._confirm_kinds(["submit"], element, "saut de ligne dans le texte")
