"""Client Chrome DevTools Protocol minimal (Chromium, Edge, Electron lancés avec --remote-debugging-port).

Entrées injectées dans le moteur de rendu : aucun effet sur la vraie souris, le vrai clavier ni le premier plan,
même fenêtre recouverte. Écoute limitée à 127.0.0.1.

Erreurs : toute défaillance est une CDPError, jamais une exception de transport brute. PageGone : connexion perdue
(onglet fermé, navigateur arrêté), la page est à jeter. PageBusy : la page ne répond pas dans le délai (boîte
JavaScript ouverte, script long), le port reste valable.
"""

from __future__ import annotations

import contextlib
import ctypes
import html
import itertools
import json
import re
import secrets
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect


class CDPError(RuntimeError):
    pass


class PageGone(CDPError):
    """Connexion à la page perdue : onglet fermé, navigateur arrêté. Une reconnexion peut retrouver la page."""


class PageBusy(CDPError):
    """La page ne répond pas dans le délai : boîte JavaScript ouverte ou script long. Le port n'est pas mort."""


# Fermeture d'une connexion : jamais d'attente longue sur une page muette ou déjà partie.
CLOSE_TIMEOUT_S = 1.0
# Point DevTools local : jamais par le proxy du système (il ne joindrait pas 127.0.0.1, ou verrait le trafic).
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_json(port: int, path: str, timeout_s: float) -> Any:
    with _LOCAL.open(f"http://127.0.0.1:{port}{path}", timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def targets(port: int, timeout_s: float = 0.5) -> list[dict[str, Any]]:
    try:
        data = _get_json(port, "/json/list", timeout_s)
    except (OSError, ValueError) as error:  # ValueError : réponse illisible (JSON, encodage)
        raise CDPError(f"DevTools unreachable on port {port}: {error}") from error
    if not isinstance(data, list):
        raise CDPError(f"DevTools: unexpected answer on port {port}.")
    return [t for t in data if isinstance(t, dict)]


def alive(port: int, timeout_s: float = 0.3) -> bool:
    """Le port répond-il vraiment en DevTools (et pas seulement « ouvert ») ?"""
    try:
        data = _get_json(port, "/json/version", timeout_s)
    except OSError, ValueError:
        return False
    return isinstance(data, dict) and "webSocketDebuggerUrl" in data


BROWSER_SUFFIXES = (" - Microsoft Edge", " – Microsoft Edge", " - Microsoft​ Edge", " - Google Chrome",
                    " - Brave", " - Chromium")  # fmt: skip


def page_title_from_window(title: str) -> str:
    """Titre de fenêtre d'un navigateur → titre de l'onglet actif (sans « - Microsoft Edge » ni profil)."""
    for suffix in BROWSER_SUFFIXES:
        if suffix in title:
            title = title[: title.index(suffix)]
    # Edge ajoute « et N pages de plus » / « and N more pages » quand plusieurs onglets sont ouverts.
    for marker in (" et ", " and "):
        head, _, tail = title.rpartition(marker)
        if head and ("page" in tail.lower()):
            title = head
    return title.strip()


class AmbiguousPage(CDPError):
    """Plusieurs onglets possibles : ce n'est pas un port mort, seulement une page qu'on ne peut pas désigner."""


def titles_agree(window_title: str, page_title: str) -> bool:
    """Le titre de la fenêtre correspond-il à celui de la page ? (le titre de fenêtre suit document.title avec un
    léger retard, et le navigateur le tronque ou l'habille)"""
    wanted = page_title_from_window(window_title).strip().lower()
    title = (page_title or "").strip().lower()
    return not wanted or wanted == title or (bool(title) and (wanted in title or title in wanted))


def _title(target: dict[str, Any]) -> str:
    return str(target.get("title") or "").strip().lower()


def pick_page(pages: list[dict[str, Any]], window_title: str | None) -> dict[str, Any]:
    if len(pages) == 1 or window_title is None:
        if not pages:
            raise CDPError("No DevTools page available.")
        if len(pages) > 1:
            raise AmbiguousPage("Several DevTools pages: the active tab cannot be identified.")
        return pages[0]
    wanted = page_title_from_window(window_title).strip().lower()
    exact = [t for t in pages if _title(t) == wanted]
    if len(exact) == 1:
        return exact[0]
    agree = [t for t in pages if titles_agree(window_title, _title(t))]
    if len(agree) == 1:
        return agree[0]
    raise AmbiguousPage("Active tab not found unambiguously among the DevTools pages.")


class Page:
    """Connexion à une page (onglet ou fenêtre Electron)."""

    def __init__(
        self, port: int, match: str | None = None, timeout_s: float = 5.0, target: dict[str, Any] | None = None
    ) -> None:
        # target : page déjà désignée (et prouvée : proven_page) ; sinon choisie par le titre de la fenêtre
        for attempt in range(0 if target is not None else 2):
            pages = [t for t in targets(port) if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            try:
                self.info = pick_page(pages, match)
                break
            except AmbiguousPage:
                if attempt:
                    raise
                time.sleep(0.15)  # titre de fenêtre en retard sur document.title : une seconde lecture
        if target is not None:
            self.info = target
        self.timeout_s = timeout_s
        self._ids = itertools.count(1)
        # Connexion longue (gardée d'un appel à l'autre) : son gestionnaire de contexte est tenu par une pile, fermée
        # par close().
        self._stack = contextlib.ExitStack()
        try:
            self.ws = self._stack.enter_context(
                connect(
                    str(self.info["webSocketDebuggerUrl"]),
                    max_size=64 * 1024 * 1024,
                    open_timeout=timeout_s,
                    close_timeout=CLOSE_TIMEOUT_S,
                    proxy=None,
                )
            )
        except (WebSocketException, OSError) as error:  # TimeoutError compris (poignée de main trop lente)
            raise CDPError(f"DevTools connection failed on port {port}: {error}") from error

    def close(self) -> None:
        try:
            self._stack.close()
        except WebSocketException, OSError:
            pass  # déjà fermée ou partie : rien à libérer de plus

    def __enter__(self) -> Page:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def call(self, method: str, *, timeout_s: float | None = None, **params: Any) -> dict[str, Any]:
        """Appel DevTools. Délai dépassé → PageBusy (connexion gardée : la réponse tardive sera ignorée) ; transport
        rompu → PageGone (connexion fermée)."""
        ident = next(self._ids)
        limit = self.timeout_s if timeout_s is None else timeout_s
        deadline = time.monotonic() + limit
        try:
            self.ws.send(json.dumps({"id": ident, "method": method, "params": params}))
            while True:
                message = json.loads(self.ws.recv(timeout=max(0.0, deadline - time.monotonic())))
                if isinstance(message, dict) and message.get("id") == ident:
                    break
        except TimeoutError as error:  # avant OSError, dont il hérite
            raise PageBusy(
                f"{method}: the page did not answer within {limit:.1f} s (JavaScript dialog open or long script: target "
                "its OK/Cancel button, or retry)"
            ) from error
        except (WebSocketException, OSError, ValueError) as error:  # ValueError : message illisible
            self.close()
            raise PageGone(f"{method}: DevTools connection lost ({type(error).__name__}: {error})") from error
        if "error" in message:
            error = message["error"]
            raise CDPError(f"{method}: {error.get('message') if isinstance(error, dict) else error}")
        result = message.get("result", {})
        return result if isinstance(result, dict) else {}

    def evaluate(self, expression: str, timeout_s: float | None = None) -> Any:
        result = self.call(
            "Runtime.evaluate", timeout_s=timeout_s, expression=expression, returnByValue=True, awaitPromise=True
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise CDPError(f"JavaScript: {details.get('text') if isinstance(details, dict) else details}")
        value = result.get("result", {})
        return value.get("value") if isinstance(value, dict) else None

    # Coordonnées CSS de la page (pas des pixels écran) ----------------------------------------------------------
    def click(self, x: float, y: float, button: str = "left", count: int = 1) -> None:
        self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        for i in range(1, count + 1):
            self.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button=button, clickCount=i)
            self.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button=button, clickCount=i)

    def hover(self, x: float, y: float) -> None:
        self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)

    def drag(self, x1: float, y1: float, x2: float, y2: float, steps: int = 12) -> None:
        self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x1, y=y1)
        self.call("Input.dispatchMouseEvent", type="mousePressed", x=x1, y=y1, button="left", clickCount=1)
        for i in range(1, steps + 1):
            x, y = x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps
            self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="left", buttons=1)
        self.call("Input.dispatchMouseEvent", type="mouseReleased", x=x2, y=y2, button="left", clickCount=1)

    def wheel(self, x: float, y: float, delta_y: float, delta_x: float = 0) -> None:
        self.call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=delta_x, deltaY=delta_y)

    def insert_text(self, text: str) -> None:
        self.call("Input.insertText", text=text)

    def key(self, key: str, code: str, vk: int, modifiers: int = 0, text: str | None = None) -> None:
        """modifiers : 1 Alt, 2 Ctrl, 4 Meta, 8 Maj."""
        params: dict[str, Any] = {"key": key, "code": code, "windowsVirtualKeyCode": vk, "modifiers": modifiers}
        self.call("Input.dispatchKeyEvent", type="rawKeyDown", **params)
        if text:
            self.call("Input.dispatchKeyEvent", type="char", text=text, **params)
        self.call("Input.dispatchKeyEvent", type="keyUp", **params)


# --- Page prouvée d'une fenêtre que l'outil n'a pas lancée (recette B de Codex, banc commun du 21/10) -----------------
# Molette de confiance par DevTools dans une fenêtre Chromium/Electron/Edge lancée par quelqu'un d'autre avec un port de
# débogage. Avant toute entrée, trois preuves (relectures de Codex du 21/10) : le port figure dans la ligne de commande
# du processus de la fenêtre ; l'adresse contactée (127.0.0.1, à défaut 0.0.0.0) est écoutée par CE processus exact et
# lui seul (jamais un parent, un enfant ni un autre programme lancé avec le même port) ; la page est liée à CETTE
# fenêtre : titre décodé égal au suffixe de navigateur près, puis titre aléatoire posé sur la page et vu dans le titre de
# la fenêtre en moins de PROOF_S (titre d'origine remis seulement s'il y est encore). Sinon : Unproven, rien n'est
# envoyé.


class Unproven(CDPError):
    """La page DevTools n'est pas prouvée être celle de la fenêtre : rien n'est envoyé."""


class MarkerLeft(RuntimeError):
    """Le marqueur de la preuve a pu rester dans le titre de la page (nettoyage sans réponse) : jamais une CDPError,
    pour que l'outil n'enchaîne AUCUNE autre technique par-dessus (relecture de Codex, 21/10)."""


PROOF_S = 0.5
_PORT_FLAG = re.compile(r"--remote-debugging-port(?:=|\s+)(\d+)", re.IGNORECASE)
_BROWSER_SUFFIX = re.compile(r"^ [-–] (?:[^\r\n]+? [-–] )?(?:Microsoft\u200b? Edge|Google Chrome|Chromium|Brave)$")


def port_from_command_line(text: str | None) -> int | None:
    """Port de débogage déclaré dans une ligne de commande ; 0 (« choisi par Chromium ») ou absent : aucun."""
    found = _PORT_FLAG.search(text or "")
    port = int(found.group(1)) if found else 0
    return port if 0 < port < 65536 else None


def process_command_line(pid: int) -> str | None:
    """Ligne de commande d'un processus (ProcessCommandLineInformation, Windows 8.1+), sans WMI ; None si illisible."""
    kernel32, ntdll = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("ntdll")
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    ntdll.NtQueryInformationProcess.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)
    ]  # fmt: skip
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = ctypes.c_ulong(0)
        ntdll.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if ntdll.NtQueryInformationProcess(handle, 60, buffer, size.value, ctypes.byref(size)) < 0:
            return None
        length = ctypes.c_ushort.from_buffer(buffer).value  # UNICODE_STRING : Length (octets), MaximumLength, Buffer
        pointer = ctypes.c_void_p.from_buffer(buffer, 8).value
        return ctypes.wstring_at(pointer, length // 2) if pointer else None
    finally:
        kernel32.CloseHandle(handle)


def owner_proven(pid: int, port: int, rows: list[tuple[int, str, int]]) -> bool:
    """Le port, à l'adresse que l'outil contacte (127.0.0.1, à défaut 0.0.0.0), est-il écouté par le processus EXACT de
    la fenêtre et par lui seul ? rows : (port, adresse, pid) des écoutes TCP IPv4 (launch.listeners)."""
    for address in ("127.0.0.1", "0.0.0.0"):
        owners = {owner for number, addr, owner in rows if number == port and addr == address}
        if owners:
            return owners == {pid}
    return False


def title_matches(window_title: str, page_title: str) -> bool:
    """Titre de page (décodé) égal à celui de la fenêtre, au suffixe de navigateur près (« - [profil - ]Microsoft Edge »,
    « - Google Chrome »…) ; Electron : égalité exacte. Jamais un simple préfixe."""
    if not page_title:
        return False
    if window_title == page_title:
        return True
    return window_title.startswith(page_title) and bool(_BROWSER_SUFFIX.match(window_title[len(page_title) :]))


def proven_page(pages: list[dict[str, Any]], window_title: str, port: int) -> dict[str, Any]:
    """L'unique page au titre décodé égal à celui de la fenêtre (/json/list échappe les titres en HTML : « A &amp; B ») ;
    son WebSocket doit rester sur 127.0.0.1 et le même port (localhost réécrit). Sinon Unproven, jamais de repli
    « une seule page »."""
    candidates = [t for t in pages if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    matching = [t for t in candidates if title_matches(window_title, html.unescape(str(t.get("title") or "")))]
    if len(matching) != 1:
        raise Unproven(
            f"No single DevTools page proven to be this window ({len(candidates)} pages, {len(matching)} matching)"
        )
    url = urllib.parse.urlsplit(str(matching[0]["webSocketDebuggerUrl"]))
    if url.scheme != "ws" or url.hostname not in ("127.0.0.1", "localhost") or url.port != port:
        raise Unproven(f"DevTools page served elsewhere than 127.0.0.1:{port}")
    return {**matching[0], "webSocketDebuggerUrl": urllib.parse.urlunsplit(url._replace(netloc=f"127.0.0.1:{port}"))}


def prove_window(
    page: Page,
    window_title: Callable[[], str],
    timeout_s: float = PROOF_S,
    sleep: Callable[[float], None] = time.sleep,
    reconnect: Callable[[], Page] | None = None,
) -> None:
    """Preuve active page ↔ fenêtre : titre d'origine lu AVANT toute écriture ; titre aléatoire posé seulement si le
    titre est encore celui lu (sinon refus : jamais écraser un titre changé entre la lecture et la pose) ; vu dans le
    titre de CETTE fenêtre en moins de timeout_s ; remis seulement si le titre aléatoire y est encore (une page qui a
    changé le sien entre-temps le garde), sur une NOUVELLE connexion au même onglet si la première a cassé ; puis le
    titre natif doit avoir quitté le titre aléatoire dans la fenêtre avant toute molette (une relecture par l'image ne
    le verra pas). Jamais vu, ou nettoyage non prouvé : Unproven, et rien n'est envoyé (relecture de Codex, 21/10)."""
    original = page.evaluate("document.title")
    if not isinstance(original, str):
        raise Unproven("Page title unreadable")
    nonce = "nexus-" + secrets.token_hex(12)
    seen = posed = False
    primary: BaseException | None = None
    # Habillage EXACT du titre natif autour du marqueur (préfixe, suffixe), relevé quand la fenêtre le montre : le contrôle
    # final exige titre final + CE même habillage (relecture de Codex, 21/10 : « Rapport - Personnel - Google Chrome »
    # d'un onglet B passait pour « Rapport » de A, la règle du suffixe acceptant un profil facultatif).
    wrap: tuple[str, str] | None = None

    def look() -> bool:
        nonlocal wrap
        shown = window_title()
        if not title_matches(shown, nonce):
            return False
        at = shown.find(nonce)
        wrap = (shown[:at], shown[at + len(nonce) :])
        return True

    try:
        # pose conditionnelle ; une réponse perdue après la pose est quand même nettoyée et vérifiée ci-dessous
        posed = (
            page.evaluate(
                f"(() => {{ if (document.title !== {json.dumps(original)}) return false;"
                f" document.title = {json.dumps(nonce)}; return true; }})()"
            )
            is True
        )
        deadline = time.monotonic() + timeout_s
        while posed and not seen:
            seen = look()
            if seen or time.monotonic() >= deadline:
                break
            sleep(0.02)
    except BaseException as error:  # pose peut-être appliquée (connexion cassée…) : traitée comme posée
        primary = error
    maybe_posed = posed or primary is not None
    # 1. Nettoyage DOM (conditionnel, nouvelle connexion si besoin) : sans réponse, le titre a pu rester marqué.
    if maybe_posed and not _restore_title(page, nonce, original, reconnect):
        raise MarkerLeft(
            "The page title could not be restored after the proof (connection lost): it may still show a proof "
            "marker; nothing was sent"
        ) from primary
    # 2. Retour natif confirmé (relecture de Codex, 21/10) : si la fenêtre a montré le marqueur (ou le montre encore),
    # elle doit l'avoir quitté ET afficher le titre FINAL de la page visée (relu, jamais réécrit) : un autre onglet
    # activé pendant le nettoyage, ou une remise non vue, est la même erreur terminale que le nettoyage impossible.
    if maybe_posed and (seen or look()) and wrap is not None:
        prefix, suffix = wrap
        final = _read_title(page, reconnect)
        deadline = time.monotonic() + timeout_s
        while True:
            shown = window_title()
            if final is not None and nonce not in shown and shown == f"{prefix}{final}{suffix}":
                break
            if time.monotonic() >= deadline:
                raise MarkerLeft(
                    "The window does not show the page's restored title after the proof (marker still shown, or "
                    "another tab or page in front): nothing was sent"
                ) from primary
            sleep(0.02)
    if primary is not None:
        raise primary
    if not posed:
        raise Unproven("The page changed its title while it was being read: nothing was sent")
    if not seen:
        raise Unproven("This DevTools page is not proven to be the window (its title never showed up in the window)")


def _read_title(page: Page, reconnect: Callable[[], Page] | None) -> str | None:
    """document.title actuel de la page (nouvelle connexion au même onglet si la première a cassé) ; None : illisible."""
    try:
        title = page.evaluate("document.title")
    except CDPError:
        if reconnect is None:
            return None
        try:
            with reconnect() as again:
                title = again.evaluate("document.title")
        except CDPError:
            return None
    return title if isinstance(title, str) else None


def _restore_title(page: Page, nonce: str, original: str, reconnect: Callable[[], Page] | None) -> bool:
    """Titre d'origine remis si le marqueur y est encore ; connexion cassée : même nettoyage sur une nouvelle connexion
    au même onglet. Vrai si le nettoyage a répondu."""
    script = f"(() => {{ if (document.title === {json.dumps(nonce)}) document.title = {json.dumps(original)}; }})()"
    try:
        page.evaluate(script)
        return True
    except CDPError:
        if reconnect is None:
            return False
    try:
        with reconnect() as again:
            again.evaluate(script)
        return True
    except CDPError:
        return False
