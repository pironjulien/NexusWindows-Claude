"""État du serveur entre deux appels : fenêtre courante, instantanés, connexions DevTools.

Deux instantanés par fenêtre :
- `views` : le dernier construit (cache de travail, reconstruit après une action) ;
- `shown` : le dernier dont la numérotation [N] a été montrée au modèle. Un [N] se résout TOUJOURS là, puis
  l'élément est revalidé par son identifiant UIA : jamais de renumérotation silencieuse.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import cdp, log, snapshot, uia
from . import win32 as w

# Sonde d'une page DevTools : une page saine répond en quelques millisecondes ; au-delà, elle est occupée.
PROBE_S = 0.5
# Port DevTools injoignable : pas de nouvelle tentative avant ce délai (ne pas repayer l'attente à chaque action).
DEAD_PORT_S = 30.0
EXIT_WAIT_MS = 5000  # sortie du navigateur fermé pour cause de compte, avant de supprimer son profil


class WindowError(ValueError):
    pass


# --- Identité d'un processus : (pid, heure de création) ---------------------------------------------------------
# Un pid seul ne désigne pas un processus : Windows le réattribue dès que le dernier handle sur le processus terminé
# est fermé. L'heure de création, relevée à l'enregistrement, prouve que c'est le même ; le handle tenu ouvert pendant
# le contrôle puis la fermeture empêche toute réattribution entre les deux.
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x102
_GetProcessTimes = w._fn(w.kernel32, "GetProcessTimes", w.w.BOOL, w.w.HANDLE, *[ctypes.POINTER(w.w.FILETIME)] * 4)
_WaitForSingleObject = w._fn(w.kernel32, "WaitForSingleObject", w.w.DWORD, w.w.HANDLE, w.w.DWORD)


def _open(pid: int) -> int:
    """Handle (lecture limitée + attente) sur le processus, 0 s'il n'existe plus ou est inaccessible."""
    return w.handle(w.OpenProcess(w.PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)) if pid else 0


def _close(handle: int) -> None:
    if handle:
        w.CloseHandle(handle)


def _started_at(handle: int) -> int:
    """Heure de création (FILETIME) d'un processus VIVANT ; 0 s'il est terminé ou illisible."""
    if not handle or _WaitForSingleObject(handle, 0) != WAIT_TIMEOUT:
        return 0
    created, exited, kernel, user = (w.w.FILETIME() for _ in range(4))
    if not _GetProcessTimes(
        handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
    ):
        return 0
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


def _wait_exit(handle: int, timeout_ms: int = EXIT_WAIT_MS) -> None:
    if handle:
        _WaitForSingleObject(handle, timeout_ms)


def process_identity(pid: int) -> int:
    """Heure de création du processus vivant `pid` (0 : terminé, inaccessible ou absent)."""
    handle = _open(pid)
    try:
        return _started_at(handle)
    finally:
        _close(handle)


@dataclass
class Session:
    target: int | None = None
    views: dict[int, snapshot.View] = field(default_factory=dict)
    shown: dict[int, snapshot.View] = field(default_factory=dict)
    frames: dict[int, snapshot.Frame] = field(default_factory=dict)  # cadre de la dernière image montrée
    devtools: dict[int, int] = field(default_factory=dict)  # pid → port DevTools vérifié
    pages: dict[int | tuple[int, str], cdp.Page] = field(default_factory=dict)  # hwnd (ou hwnd, titre) → page
    dead_ports: dict[int, float] = field(default_factory=dict)  # port → instant du dernier échec
    profiles: dict[int, Path] = field(default_factory=dict)  # pid → profil jetable d'un navigateur DevTools
    identities: dict[int, int] = field(default_factory=dict)  # pid → heure de création relevée à l'enregistrement
    # pid → port DevTools ouvert sur la session réelle de Julien (Steam), avec son accord : jamais fermé par l'outil.
    own_sessions: set[int] = field(default_factory=set)

    # --- Fenêtres -----------------------------------------------------------------------------------------------
    def resolve_window(self, spec: int | str | None) -> int:
        if spec is None or spec == "":
            if self.target and w.IsWindow(self.target):
                return self.target
            raise WindowError("Aucune fenêtre courante : appeler windows, ou passer window (titre, processus ou hwnd).")
        if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().isdigit()):
            hwnd = int(spec)
            if not w.IsWindow(hwnd):
                raise WindowError(f"La fenêtre {hwnd} n'existe plus : appeler windows.")
            self.target = hwnd
            return hwnd
        text = str(spec).strip().lower()
        candidates = snapshot.app_windows()
        exact = [c for c in candidates if c.title.lower() == text]
        by_process = [c for c in candidates if c.process.lower() in (text, text + ".exe")]
        partial = [c for c in candidates if text in c.title.lower()]
        for group in (exact, by_process, partial):
            if group:
                self.target = group[0].hwnd  # le plus haut dans l'empilement
                return self.target
        names = ", ".join(f"« {snapshot.clip(c.title, 40)} »" for c in candidates[:12])
        raise WindowError(f"Aucune fenêtre ne correspond à « {spec} ». Fenêtres : {names}.")

    # --- Instantanés --------------------------------------------------------------------------------------------
    def view(self, auto: uia.Automation, hwnd: int, rebuild: bool = False, detail: str | None = None) -> snapshot.View:
        current = self.views.get(hwnd)
        detail = detail or (current.detail if current else "normal")
        if current is None or rebuild or current.detail != detail:
            current = snapshot.build_view(auto, hwnd, detail=detail)
            self.views[hwnd] = current
            if len(self.views) > 24:
                oldest = min(self.views, key=lambda h: self.views[h].taken)
                self.views.pop(oldest, None)
                self.shown.pop(oldest, None)
                self.frames.pop(oldest, None)
        return current

    def mark_shown(self, view: snapshot.View, ids: set[int]) -> None:
        """Les numéros `ids` de cette vue viennent d'être montrés au modèle : ce sont désormais SES [N] (avec
        l'identité de chaque élément à cet instant). Rien de montré : la numérotation précédente reste la sienne."""
        if not ids:
            return
        by_id = view.by_id
        view.displayed = {i: snapshot.identity(by_id[i].props) for i in ids if i in by_id}
        self.shown[view.hwnd] = view

    def mark_image(self, view: snapshot.View) -> None:
        """Une image de cette vue vient d'être montrée : ses coordonnées sont désormais celles des points "x,y"."""
        self.frames[view.hwnd] = view.frame

    def forget(self, hwnd: int) -> None:
        self.views.pop(hwnd, None)
        self.shown.pop(hwnd, None)
        self.frames.pop(hwnd, None)
        for key in [k for k in self.pages if k == hwnd or (isinstance(k, tuple) and k[0] == hwnd)]:
            self.pages.pop(key).close()

    def forget_all(self) -> None:
        for hwnd in list(self.views) + [k[0] if isinstance(k, tuple) else k for k in self.pages]:
            self.forget(hwnd)

    # --- DevTools -----------------------------------------------------------------------------------------------
    def register_devtools(
        self, pid: int, port: int, profile: Path | None = None, timeout_s: float = 6.0, own_session: bool = False
    ) -> bool:
        """N'enregistre le port que s'il répond en DevTools, n'écoute que sur 127.0.0.1 et appartient bien au
        processus de l'application lancée (pas à un autre programme qui aurait pris le port entre-temps).
        Le profil jetable du navigateur est retenu même sans port vérifié : son compte est contrôlé avant chaque act."""
        from .launch import descendants, port_owner

        identity = process_identity(pid)
        if not identity:
            return False  # déjà terminé (fermé pour cause de compte ?) : rien à suivre, ce pid peut être réattribué
        self.identities[pid] = identity
        if profile is not None:
            self.profiles[pid] = profile
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if cdp.alive(port):
                owner = port_owner(port)
                if owner is None or owner[1] != "127.0.0.1" or owner[0] not in descendants(pid):
                    return False
                self.devtools[pid] = port
                if own_session:
                    self.own_sessions.add(pid)
                return True
            time.sleep(0.2)
        return False

    def drop_process(self, pid: int) -> None:
        """Oublie un processus DevTools (fermé, ou pid réattribué) : port, profil et identité. Jamais rien de fermé."""
        self.devtools.pop(pid, None)
        self.profiles.pop(pid, None)
        self.identities.pop(pid, None)
        self.own_sessions.discard(pid)

    def forget_profile(self, profile: Path) -> None:
        """Rappel de la garde de compte (launch.guard_account, autre fil) : le navigateur de ce profil a été fermé."""
        for pid, known in list(self.profiles.items()):
            if known == profile:
                self.drop_process(pid)

    def check_account(self, hwnd: int) -> None:
        """Avant chaque act : jamais un compte de Julien derrière un navigateur DevTools. Un compte apparu après le
        lancement (connexion implicite, synchronisation) ferme aussitôt ce navigateur, lancé par l'outil, et supprime
        son profil. Le processus est identifié par (pid, heure de création) sous un handle tenu ouvert : un pid
        réattribué à un programme de Julien n'est jamais fermé, son entrée est seulement oubliée."""
        from . import launch

        pid = w.window_pid(hwnd)
        port, profile = self.devtools.get(pid), self.profiles.get(pid)
        if (not port and profile is None) or (pid in self.own_sessions and profile is None):
            return  # session de Julien ouverte avec son accord (Steam) : jamais fermée par l'outil
        handle = _open(pid)
        try:
            if not handle or _started_at(handle) != self.identities.get(pid):
                self.drop_process(pid)  # navigateur terminé : ce pid est (ou sera) celui d'un autre programme
                log.event("devtools-oublie", {"pid": pid, "raison": "processus terminé ou pid réattribué"})
                return
            reason = launch.account_found(port or 0, profile)
            if not reason:
                return
            launch.kill_tree(pid)  # handle ouvert : le pid ne peut pas être réattribué entre contrôle et fermeture
            _wait_exit(handle)
        finally:
            _close(handle)
        removed = launch.remove_profile(profile) if profile is not None else False
        log.event("devtools-compte", {"pid": pid, "port": port, "raison": reason, "profil_supprime": removed})
        self.drop_process(pid)
        for other in {k[0] if isinstance(k, tuple) else k for k in self.pages}:  # clé (hwnd, titre) : son hwnd
            if other == hwnd or not w.IsWindow(other):
                self.forget(other)
        raise launch.AccountError(
            f"Compte détecté dans le navigateur DevTools ({reason}) : fermé aussitôt, rien n'a été fait (jamais le "
            f"compte de Julien derrière un port DevTools){launch._profile_fate(profile, removed)}."
        )

    def page(self, hwnd: int, title: str | None = None) -> cdp.Page | None:
        """Page DevTools de la fenêtre, sondée en PROBE_S. None : pas de DevTools pour cette fenêtre (port absent ou
        mort, onglet non désignable). Lève PageBusy si la page ne répond pas (boîte JavaScript, script long) : le
        port n'est pas déclaré mort et aucune nouvelle connexion n'est ouverte, elle bloquerait de même.
        title : titre du document visé quand la fenêtre en porte plusieurs (Steam : cadre et magasin dans la même
        fenêtre) ; sinon le titre de la fenêtre désigne la page."""
        pid = w.window_pid(hwnd)
        port = self.devtools.get(pid)
        if not port:
            return None
        if time.monotonic() - self.dead_ports.get(port, -1e9) < DEAD_PORT_S:
            return None  # échec récent : ne pas repayer le délai à chaque action
        key: int | tuple[int, str] = hwnd if title is None else (hwnd, title)
        wanted = w.window_text(hwnd) if title is None else title
        page = self.pages.get(key)
        if page is not None:
            try:
                if self._shows(page, wanted):
                    return page
            except cdp.PageGone:
                pass  # onglet fermé ou connexion perdue : reconnexion ci-dessous
            self.page_failed(key)
        try:
            page = cdp.Page(port, match=wanted)
        except cdp.AmbiguousPage:
            return None  # pas un port mort : simplement pas de page désignable pour cette action
        except cdp.CDPError:
            self.dead_ports[port] = time.monotonic()
            return None
        self.pages[key] = page
        try:
            page.evaluate("1", timeout_s=PROBE_S)
        except cdp.PageGone:
            self.page_failed(key)  # partie entre la liste et la connexion : pas de DevTools pour cette action
            return None
        return page

    @staticmethod
    def _shows(page: cdp.Page, wanted: str) -> bool:
        """L'onglet actif a pu changer depuis la connexion : la page doit être visible (onglet de premier plan de sa
        fenêtre) et son titre s'accorder avec celui de la fenêtre (qui le suit avec un léger retard). PageBusy et
        PageGone remontent à l'appelant."""
        for attempt in range(2):
            try:
                visibility, title = page.evaluate("[document.visibilityState, document.title]", timeout_s=PROBE_S)
            except cdp.PageBusy, cdp.PageGone:
                raise
            except cdp.CDPError, TypeError, ValueError:  # exception JavaScript, réponse d'une autre forme
                return False
            if visibility == "visible" and cdp.titles_agree(wanted, str(title or "")):
                return True
            if attempt == 0:
                time.sleep(0.15)
        return False

    def page_failed(self, hwnd: int | tuple[int, str]) -> None:
        """Erreur pendant un appel DevTools : on jette la connexion (elle sera refaite, ou le port déclaré mort)."""
        page = self.pages.pop(hwnd, None)
        if page:
            page.close()
