"""Confirmation étanche (politique commune NexusHarness/NexusWindows/commun/garde-fous.json, accord Claude–Antigravity du 18/10).

Un geste dont l'effet part chez un tiers, coûte de l'argent ou détruit (payer, envoyer, publier, supprimer
définitivement, formater, désinstaller, réinitialiser, résilier, passer un dépôt en public) exige confirm=true sur
l'étape, posé seulement sur l'ordre explicite de l'utilisateur ou après son oui. Le contrôle porte sur CE QUE LE GESTE
DÉCLENCHE, pas sur sa forme, pour ne pas être contourné :
- clic (simple ou double) sur un élément, par [N], par nom ou en coordonnées : le déclencheur sous le point (bouton,
  lien, élément de menu, ou l'élément lui-même s'il est générique), un texte dans un bouton comptant pour le bouton ;
- touches : Entrée (élément visé ou qui a le focus, bouton par défaut, bouton d'action voisin du champ), Ctrl+Entrée
  (tout bouton d'action de la fenêtre), Espace (élément au focus), Alt+lettre (raccourci d'un bouton ou d'un menu) ;
- saisie d'un texte qui contient un saut de ligne : comme Entrée dans ce champ ;
- pages DevTools : mêmes questions posées au DOM (élément actif, boutons d'envoi de son formulaire, élément au point).
Un élément de liste, d'arbre, un onglet, un champ ou une case ne déclenche rien (rôles non actifs) ; un élément de menu
qui ouvre un sous-menu non plus (« Envoyer vers »). Politique illisible : refus (échec fermé).

Accord de la trinité du 19/10/2026 (« ameliorations-outils », priorité 1) : un geste SORTANT (garde_fous.outward_word :
envoyer, appeler, payer, publier, transférer…) exige en plus accord=<jeton> lié à l'action EXACTE
(garde_fous.Accords, un objet par serveur) : geste, déclencheur(s), processus et titre de la fenêtre, et le CONTENU
(valeurs des champs éditables de la fenêtre, texte tapé par l'étape). Le premier essai rend le résumé et un jeton
neuf ; tout changement entre l'accord et le geste l'annule. Tous les chemins ci-dessus passent par ce même contrôle,
une seule fois par geste (déclencheurs UIA et DOM réunis). Les destructions locales (désinstaller, formater…)
gardent confirm=true seul. Un texte lu ne peut jamais élargir une autorisation.
"""

from __future__ import annotations

import json

from . import cdp, keys, policy, snapshot, uia
from . import win32 as w
from .act_base import DEEP_ACTIVE_JS, ActionError, ExecutorBase, _locate, _roots_top_down

garde_fous = policy.garde_fous
STATE_SYSTEM_DEFAULT = 0x100
EXPAND_LEAF = 3  # ExpandCollapseState_LeafNode
NEIGHBOR_LEVELS = 3  # ancêtres du champ remontés pour trouver son bouton d'envoi (composeur, formulaire)
TRIGGER_DEPTH = 8  # ancêtres remontés depuis un texte ou une icône jusqu'à son bouton
VK_ENTER, VK_SPACE = 0x0D, 0x20
VK_CTRL, VK_ALT, VK_SHIFT, VK_WIN = 0x11, 0x12, 0x10, 0x5B


class ConfirmationRequired(ActionError):
    """Geste irréversible ou chez un tiers demandé sans confirm=true : rien n'a été fait."""


# Libellés du champ « quoi » des contextes de garde-fous.json (politique commune, en français) : rendus en anglais
# dans les messages au modèle ; un libellé inconnu reste tel quel.
CONTEXT_WHAT = {
    "paiement ou virement": "payment or transfer",
    "formatage": "formatting",
    "réinitialisation de l'appareil": "device reset",
}


def english_word(word: str) -> str:
    """Mot à confirmer rendu par garde_fous.confirm_word, partie fixe en anglais (« (fenêtre de X) »)."""
    head, sep, rest = word.partition(" (fenêtre de ")
    if not sep:
        return word
    what = rest[:-1] if rest.endswith(")") else rest
    return f"{head} ({CONTEXT_WHAT.get(what, what)} window)"


def refusal(what: str, label: object, word: str) -> str:
    """Refus sans confirm=true (mêmes conditions que garde_fous.refusal, texte anglais et neutre)."""
    return (
        f'{what} on "{str(label).strip()[:60]}": irreversible or affects a third party ("{english_word(word)}"), '
        "nothing was done. The user's explicit order for THIS action → send the step again with confirm=true; the "
        "agent's own initiative → ask the user first."
    )


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
        out.append((label_of(field), "targeted element"))
    nodes = _all_nodes(view)
    if kind.startswith("alt+"):
        letter = garde_fous.plain(kind)
        for node in nodes:
            if _live_trigger(node) and garde_fous.plain(node.props.get("access_key")) == letter:
                out.append((label_of(node), f"shortcut {node.props.get('access_key')}"))
        return out
    for node in nodes:
        if node.props.get("focused") and _live_trigger(node):
            out.append((label_of(node), "focused element"))
    if kind == "press":
        return out
    if kind == "send":
        return out + [(label_of(node), "action button of the window") for node in nodes if _live_trigger(node)]
    for node in nodes:
        if node.control_type in ("Button", "SplitButton") and _live_trigger(node):
            if int(node.props.get("legacy_state") or 0) & STATE_SYSTEM_DEFAULT:
                out.append((label_of(node), "default button of the window"))
    if field is not None:
        out += [(label_of(node), "action button next to the field") for node in _neighbors(field)]
    return out


# DOM : ce que déclencherait une touche (élément actif, boutons d'envoi de son formulaire, voisins) ou un clic au point.
_DOM_TRIGGER = (
    "button,a[href],[role=button],[role=link],[role=menuitem],input[type=submit],input[type=button],input[type=image]"
)
_DOM_LABEL = "const lab = e => ((e.getAttribute('aria-label') || e.innerText || e.value || e.title || '') + '').trim().slice(0, 80);"
_DOM_KEY_JS = (
    "((kind) => { " + _DOM_LABEL + " const T = " + json.dumps(_DOM_TRIGGER) + ";"
    " const out = []; " + DEEP_ACTIVE_JS + " if (a && a.matches && a.matches(T)) out.push(lab(a));"
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


# DOM : valeurs des champs éditables de la page (jamais les mots de passe), ce que l'accord lie comme contenu.
_DOM_FIELDS_JS = (
    "(() => { const out = [];"
    " document.querySelectorAll('input:not([type=password]):not([type=hidden]):not([type=checkbox]):not([type=radio])"
    ':not([type=submit]):not([type=button]),textarea,[contenteditable=""],[contenteditable=true]\').forEach(e => {'
    " const v = ((e.isContentEditable ? e.innerText : e.value) || '').trim(); if (!v) return;"
    " const n = (e.getAttribute('aria-label') || e.name || e.id || e.placeholder || '').trim().slice(0, 60);"
    " out.push(n + ': ' + v); }); return out; })()"
)
CONTENT_CHARS = 20000  # texte d'un document lu au plus par champ (TextPattern)


class ConfirmActions(ExecutorBase):
    """Contrôles de confirmation appelés par les actions (clic, touches, saisie) avant toute entrée envoyée."""

    def _title(self) -> str:
        return w.window_text(self.hwnd)

    def _field_contents(self) -> list[str]:
        """« nom: valeur » des champs éditables de la fenêtre (Edit, Document ; DOM s'il y a une page), triés : le
        contenu que l'accord lie (destinataires, objet, corps). Jamais un mot de passe."""
        view = self._rebuild() if self.acted else self.view
        fields: list[str] = []
        for node in _all_nodes(view):
            if node.control_type not in ("Edit", "Document") or node.props.get("password"):
                continue
            value = node.props.get("value")
            if not str(value or "").strip() and node.props.get("has_text"):
                value = self.auto.text(node.element, max_chars=CONTENT_CHARS)
            if str(value or "").strip():
                fields.append(f"{node.name}: {str(value).strip()}")
        page = self._get_page()
        if page is not None:
            try:
                found = page.evaluate(_DOM_FIELDS_JS) or []
                fields += [f"page {item}" for item in found]
            except cdp.CDPError, TypeError, ValueError:
                fields.append("page fields unreadable")
        return sorted(fields)

    def _check_accord(self, what: str, outward: list[tuple[str, str, str]], title: str) -> None:
        """Geste sortant : permis seulement avec confirm=true ET l'accord lié à cette action exacte."""
        labels = sorted({" ".join(label.split()) for label, _source, _word in outward})
        content = self._field_contents()
        if self.step_text:
            content.append(f"typed: {self.step_text}")
        act = garde_fous.action(
            "windows",
            self.gesture or what,
            " | ".join(labels),
            content="\n".join(content),
            context=f"{self.view.info.process} — {title}",
        )
        key = garde_fous.fingerprint(act)
        if key in self.approved:
            return
        try:
            registry = policy.accords()
        except policy.PolicyError as error:
            raise ActionError(str(error)) from error
        answer = registry.check(act, self.confirm, self.accord)
        if not answer:
            self.approved.add(key)
            return
        label, source, word = outward[0]
        detail = f"{what} ({source})" if source else what
        raise ConfirmationRequired(
            f'{detail} on "{label.strip()[:60]}": outward action ("{english_word(word)}").\n{answer}'
        )

    def _refuse_if(self, what: str, labels: list[tuple[str, str]]) -> None:
        """Tout ce que le geste déclencherait, réuni : destruction locale → confirm=true ; geste sortant → confirm=true
        et accord lié à l'action exacte. Un seul contrôle par geste (un seul jeton)."""
        try:
            title = self._title()
            outward: list[tuple[str, str, str]] = []
            for label, source in labels:
                word = garde_fous.outward_word(label, title)
                if word:
                    outward.append((label, source, word))
                elif not self.confirm and (word := garde_fous.confirm_word(label, title)):
                    detail = f"{what} ({source})" if source else what
                    raise ConfirmationRequired(refusal(detail, label, word))
            if outward:
                self._check_accord(what, outward, title)
        except garde_fous.GardeFousError as error:
            raise ActionError(f"safety policy unreadable ({error}): nothing was done (fail closed)") from error

    def _confirm_click(self, e: snapshot.Element, button: str = "left") -> None:
        """Clic gauche (simple ou double) sur un élément : ce qu'il déclenche est contrôlé (confirm, accord)."""
        if button != "left":
            return
        trigger = trigger_of(e.node)
        if trigger is not None:
            self._refuse_if("click", [(label_of(trigger), "")])

    def _confirm_point(self, x: int, y: int, button: str = "left") -> None:
        """Clic en coordonnées : même contrôle que le clic sur l'élément sous le point (arbre UIA et DOM réunis)."""
        if button != "left":
            return
        view = self._rebuild() if self.acted else self.view
        labels: list[tuple[str, str]] = []
        hit = self._hit(view, x, y)
        if hit is not None:
            trigger = trigger_of(hit[0])
            if trigger is not None:
                labels.append((label_of(trigger), ""))
        page = self._get_page()
        if page is not None:
            try:
                cx, cy = self._css_point(page, x, y)
                found = page.evaluate(f"{_DOM_POINT_JS}({cx}, {cy})") or []
            except cdp.CDPError, TypeError, ValueError:
                found = []
            labels += [(str(label), "page") for label in found]
        self._refuse_if("click by coordinates", labels)

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
        if not kinds:
            return
        field = self._field_node(element if element is not None else self._focused_target_element())
        view = self._rebuild() if self.acted else self.view
        page = self._get_page()
        labels: list[tuple[str, str]] = []
        for kind in kinds:
            labels += key_triggers(view, field, kind)
            if page is not None:
                try:
                    found = page.evaluate(f"{_DOM_KEY_JS}({json.dumps(kind)})") or []
                except cdp.CDPError, TypeError, ValueError:
                    found = []
                labels += [(str(label), "page") for label in found]
        self._refuse_if(f"{what}: would trigger", labels)

    def _confirm_keys(self, combos: list[keys.Combo], spec: str, element: snapshot.Element | None) -> None:
        self._confirm_kinds([combo_kind(c) for c in combos], element, f'key "{spec}"')

    def _confirm_text(self, element: snapshot.Element | None, text: str) -> None:
        """Un saut de ligne tapé dans un champ vaut Entrée (envoi d'une messagerie, validation d'un formulaire)."""
        if "\n" in text or "\r" in text:
            self._confirm_kinds(["submit"], element, "line break in the text")

    def _confirm_menu(self, path: str) -> None:
        """Menu par WM_COMMAND (sans clic d'élément contrôlé) : son entrée finale est contrôlée comme un clic."""
        leaf = [p.strip() for p in path.replace("/", ">").split(">") if p.strip()][-1:]
        if leaf:
            self._refuse_if("menu", [(leaf[0], "")])
