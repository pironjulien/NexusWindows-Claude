"""Win32 natif par messages directs : élément de liste ou d'onglet par son rang, pont MSAA évité (son action
par défaut est un vrai clic), frappes WM_CHAR par tranches d'activation brève relues, combinaisons de touches avec
état clavier simulé."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from typing import Any

from . import foreground, keys, methods, snapshot, uia, worker
from . import win32 as w
from .act_base import (
    EXPECTED,
    NATIVE_PROXY_CLASSES,
    ActionError,
    ExecutorBase,
    Unconfirmed,
    _short,
)


class Win32Actions(ExecutorBase):
    """Techniques Win32 par messages (aussi frappes pour Chromium sans DevTools)."""

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

    @staticmethod
    def _win32_combo_of(e: snapshot.Element) -> int:
        """Liste déroulante Win32/WinForms désignée par l'élément : elle-même (sa fenêtre), ou son bouton d'ouverture
        (élément sans fenêtre juste sous elle). 0 sinon, options de la liste comprises."""
        node = e.node
        if e.role == "ComboBox" and node.hwnd and methods._is_class(node.hwnd, methods.COMBO_CLASSES):
            return node.hwnd
        parent = node.parent
        if (
            e.role == "Button"
            and not node.hwnd
            and parent is not None
            and parent.control_type == "ComboBox"
            and parent.hwnd
            and methods._is_class(parent.hwnd, methods.COMBO_CLASSES)
        ):
            return parent.hwnd
        return 0

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

    def _ensure_active(self) -> None:
        if w.root(w.foreground()) in self.windows:
            return
        if not foreground.activate(self.hwnd) or w.root(w.foreground()) not in self.windows:
            holder = foreground.system_foreground()
            if holder:
                raise methods.MethodError(
                    f"Windows refuses to activate the window: the foreground is held by {holder} (no window has "
                    "the focus). Remedy: app started by launch with devtools=true, or foreground=true"
                )
            raise methods.MethodError("the window could not be activated to receive keystrokes")

    def _keystrokes(self, e: snapshot.Element, text: str, replace: bool, simulated: bool = False) -> str:
        """Focus UIA puis caractères WM_CHAR vers la fenêtre de la cible qui a le clavier, par tranches d'activation
        brève (le focus UIA active la fenêtre ; Chromium ne lit le clavier que fenêtre active) : pause de l'utilisateur
        attendue, contrôle explicite de l'activation avant de poster, premier plan rendu à chaque échéance.

        simulated (Chromium) : aucune activation réelle. Focus DOM par accSelect relu, tranches en activation simulée
        (methods.simulated_activation) : le premier plan de l'utilisateur n'est jamais pris.

        Chaque tranche est confirmée par la valeur RELUE du champ (début du texte qu'il contient) : la suite repart de
        là, sans doublon. Champ illisible : rien n'est tapé (MethodError). Texte qui n'a pas pu être tapé en entier et
        relu : Unconfirmed (l'étape échoue, rien n'est rejoué par-dessus, l'action s'arrête là)."""
        chromium = self.view.framework == "Chromium"
        started = time.perf_counter()
        before = self._read_value(e)
        if before is None:
            raise methods.MethodError("field value unreadable: typing could not be read back, nothing was typed")
        top = self._sim_top(e) if simulated else self.hwnd

        def prepare(first: bool) -> int:
            """Met la cible en état de recevoir le clavier ; renvoie la fenêtre qui le reçoit. Aux tranches suivantes,
            seulement la réactiver : elle garde focus et curseur (un nouveau focus pourrait tout sélectionner)."""
            if simulated:
                if first:
                    self._take_focus(e)
                    try:
                        (self.auto.select_all_text if replace else self.auto.caret_to_end)(e.node.element)
                    except EXPECTED:
                        raise methods.MethodError("could not select the existing text") from None
                    time.sleep(0.03)
                return top
            foreground.mark_own_input()
            if first:
                self.auto.set_focus(e.node.element)
                # Focus relu sur le champ (ou son champ intérieur) AVANT toute sélection ou Suppr : sinon Ctrl+A puis
                # Suppr partaient dans le champ qui avait gardé le focus (relecture de Codex, tour 4).
                if not chromium and not self._focus_reaches(e, wait_s=0.2):
                    raise methods.MethodError(f"{e.describe()} did not take the keyboard focus: nothing was sent")
            if chromium:
                self._ensure_active()
            elif not first and not self._focus_reaches(e, wait_s=0.2):
                # Tranches suivantes : focus relu AVANT chaque envoi, sans refocaliser ni resélectionner (l'application a
                # pu déplacer son focus en perdant l'activation) : la saisie s'arrête là, rien n'est tapé ailleurs
                # (tour 5, Codex : la suite du texte partait dans un autre champ).
                raise Unconfirmed(f"{e.describe()} lost the keyboard focus between two slices: typing stopped there")
            if first:
                if replace:
                    try:
                        self.auto.select_all_text(e.node.element)
                    except EXPECTED:
                        if chromium:
                            raise methods.MethodError("could not select the existing text") from None
                        methods.post_combo(methods.keyboard_target(self.hwnd, self.windows), [0x41], [0x11])
                else:
                    with contextlib.suppress(*EXPECTED):
                        self.auto.caret_to_end(e.node.element)
                time.sleep(0.03)
            return methods.keyboard_target(self.hwnd, self.windows)

        original = methods.norm_text(before)
        if not text:  # vider : sélection puis Suppr, dans une seule activation brève d'au plus methods.SLICE_S
            if not original:
                return "field already empty, no activation"
            activate = self._sim_activation(e) if simulated else foreground.brief_activation(self.hwnd, self.windows)
            with activate as activation:
                begun = time.monotonic()
                target = prepare(True)
                activation.check("nothing was erased, the action is cancelled")
                methods.post_key(target, methods.VK_DELETE)
                # Relu jusqu'à l'échéance de la tranche seulement : au-delà, la relecture finale (_verify_text) suit,
                # premier plan rendu.
                remaining = begun + methods.SLICE_S - methods.GIVE_BACK_S - time.monotonic()
                self._settle(lambda: self._text_done(self._read_value(e), before, text, replace), max_s=remaining)
            how = "no activation (simulated activation)" if simulated else "brief activation"
            return f"Delete, {how} {round((time.perf_counter() - started) * 1000)} ms"

        # Remplacement d'un contenu : effacé (Suppr) et relu vide avant tout caractère, sans quoi un texte qui commence
        # comme lui serait indiscernable de « rien n'a encore été traité ».
        cleared = [not (replace and original)]

        def read() -> str:
            try:
                value = self._read_value(e)
            except EXPECTED as error:
                raise methods.Unreadable(_short(error)) from None
            if value is None:
                raise methods.Unreadable("field value unreadable")
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
        if simulated:
            # Une seule activation simulée pour toute la frappe : entre deux tranches, une désactivation postée encore
            # en file passerait derrière la réactivation suivante (envoyée, prioritaire) et ferait jeter la suite. Les
            # tranches ne servent plus qu'à borner ce qui est envoyé sans être encore relu.
            with self._sim_activation(e) as sim:
                typed = methods.type_in_slices(
                    top, self.windows, text, prepare, read, prefix, enter_as_key=True, clear=not cleared[0],
                    slice_s=methods.SIM_SLICE_S, activation=lambda: contextlib.nullcontext(sim),
                )  # fmt: skip
        else:
            typed = methods.type_in_slices(
                self.hwnd, self.windows, text, prepare, read, prefix, enter_as_key=chromium, clear=not cleared[0]
            )
        if not typed.complete:
            raise Unconfirmed(f"text partially typed: {typed.detail()}. Check the target (look) before completing it")
        return typed.detail()

    def _own_edit_hwnd(self, element: snapshot.Element) -> int:
        """Fenêtre Edit Win32 propre au champ désigné (dans la cible), sinon 0. Ses touches lui sont postées directement,
        sans activation : passe 2 du 23/10, l'activation brève avant Entrée dans le champ de renommage de l'Explorateur
        faisait valider le nom ET ouvrir le dossier ; postée au seul champ, Entrée valide sans rien ouvrir (mesuré)."""
        own, cls = self._element_hwnd_class(element)
        if own and cls.lower() in methods.EDIT_CLASSES and (w.root(own) in self.windows or w.IsChild(self.hwnd, own)):
            return own
        return 0

    def _post_combos(
        self,
        combos: list[keys.Combo],
        fw: str,
        sent: list[str],
        simulated: bool = False,
        element: snapshot.Element | None = None,
        own: int = 0,
        still_ours: Callable[[], None] | None = None,
    ) -> None:
        """simulated : Chromium en activation simulée (clavier à sa fenêtre de premier plan, raccourcis avec état
        clavier simulé : mesuré, Ctrl+K reçu par la page). element : la cible désignée ; ses touches vont à SA fenêtre
        de premier niveau, celle que l'activation simulée vise (dialogue, surgissante : relecture de Codex, W14)."""
        # Explorateur en activation simulée : touches à la vue qui a le focus du fil (sa DirectUIHWND), pas au cadre
        target_hwnd = (
            self._sim_top(element)
            if simulated and fw != "Explorer"
            else methods.keyboard_target(self.hwnd, self.windows)
        )
        if own:
            # Champ Win32 désigné qui a SA fenêtre, décidé UNE fois par l'appelant (route sans activation) : ses touches
            # vont à lui ; disparu entre-temps, rien n'est envoyé, jamais au focus d'un autre contrôle (relecture de Codex).
            # Relu juste avant l'envoi : vivant, toujours un champ Edit, toujours DANS la cible (reparenté : rien).
            still = (
                w.IsWindow(own)
                and methods.win_class(own) in methods.EDIT_CLASSES
                and (w.root(own) in self.windows or w.IsChild(self.hwnd, own))
            )
            if not still:
                raise ActionError(
                    "The targeted field disappeared or left the window before the keys: nothing was sent."
                )
            target_hwnd = own
        before = methods.accepted_posts()
        for index, combo in enumerate(combos):
            if index:
                worker.check_cancelled()  # arrêt d'urgence entre deux touches : la suivante ne part pas
            if still_ours is not None:
                still_ours()  # garde de l'appelant (fenêtre Tk reprise par l'utilisateur…) avant CHAQUE touche
            try:
                _post_one(target_hwnd, combo, fw, sent, simulated)
            except methods.MethodError as error:
                # Un message déjà accepté, même au milieu de la première touche (WM_KEYDOWN parti, WM_KEYUP refusé) :
                # issue inconnue (relecture de Codex).
                if index or methods.accepted_posts() != before:
                    # Touches déjà parties : issue inconnue, jamais rejouée au vrai clavier par-dessus (relecture de Codex :
                    # « delete ctrl+a » donnait Suppr postée puis Suppr réelle).
                    raise Unconfirmed(f"keys partly sent ({index}/{len(combos)}), then: {_short(error)}") from None
                raise


def _post_one(target_hwnd: int, combo: keys.Combo, fw: str, sent: list[str], simulated: bool) -> None:
    """Une touche ou un raccourci, par messages."""
    if combo.modifiers and (fw != "Chromium" or simulated):
        # Ctrl/Alt tenus 0,5 s partout : à 0,15 s, Ctrl+K posté à une fenêtre Win32 recouverte arrivait parfois après la
        # restauration de l'état (banc du 23/10 : « ~ sans effet » ; sonde à 0,5 s : reçu, bench/miss_probes.py).
        hold = methods.SIM_MODIFIERS_HOLD_S
        methods.post_combo(target_hwnd, [combo.key], list(combo.modifiers), hold_s=hold)
        sent.append("simulated keyboard state")
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
