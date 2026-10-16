"""Attente : présence d'un texte dans la fenêtre (mots entiers), lecture incomplète jamais prise pour une absence."""

from __future__ import annotations

import re
import time

from . import snapshot, worker
from . import win32 as w
from .act_base import (
    ActionError,
    ExecutorBase,
    Outcome,
)


class WaitActions(ExecutorBase):
    """Présence d'un texte et attente."""

    # --- Attente ------------------------------------------------------------------------------------------------
    @staticmethod
    def _unreadable(view: snapshot.View) -> str:
        """Pourquoi un texte absent de cette vue n'y est pas prouvé absent (vide : la vue est complète)."""
        if view.node_count <= 1 or not view.elements:
            return "arbre UI Automation vide"
        if view.truncated:
            return f"arbre UI Automation tronqué ({view.node_count} nœuds lus)"
        if view.web_missing:
            return "contenu web non publié par Chromium"
        return ""

    def present(self, text: str) -> bool | None:
        """Le texte figure-t-il dans la fenêtre (noms, valeurs, titre), en mots entiers (« 4 » ne trouve pas « 42 ») ?
        None : lecture incomplète (arbre vide ou tronqué, contenu web non publié) — ni présent ni absent ; la cause
        est dans self.unreadable."""
        view = self._rebuild()
        pattern = re.compile(rf"(?<!\w){re.escape(snapshot.norm(text))}(?!\w)", re.IGNORECASE)
        sources = [w.window_text(self.hwnd)]
        for e in view.elements:
            sources += [e.name, str(e.props.get("value") or "")]
        self.unreadable = ""
        if any(pattern.search(snapshot.norm(s)) for s in sources if s):
            return True
        self.unreadable = self._unreadable(view)
        return None if self.unreadable else False

    def wait(self, text: str | None, gone: bool, timeout_s: float) -> Outcome:
        timeout_s = max(0.1, min(float(timeout_s or 5), 30))
        if not text:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                worker.check_cancelled()
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            return Outcome(True, f"attente {timeout_s:.1f} s", "sleep")
        start = time.monotonic()
        while True:
            worker.check_cancelled()
            found = self.present(text)
            # Disparu ne se conclut que sur une lecture complète (False) : jamais sur None (arbre tronqué, page non
            # publiée), où le texte peut être là sans être lu.
            if found is not None and found != gone:
                elapsed = time.monotonic() - start
                return Outcome(
                    True, f"« {text} » {'disparu' if gone else 'présent'} après {elapsed:.1f} s", "wait", verified=True
                )
            if time.monotonic() - start > timeout_s:
                if found is None:
                    raise ActionError(
                        f"« {text} » : lecture de la fenêtre incomplète ({self.unreadable}) : vérifier sur l'image."
                    )
                raise ActionError(f"« {text} » toujours {'présent' if gone else 'absent'} après {timeout_s:.0f} s.")
            time.sleep(0.25)
