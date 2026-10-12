"""Client Chrome DevTools Protocol minimal (Chromium, Edge, Electron lancés avec --remote-debugging-port).

Entrées injectées dans le moteur de rendu : aucun effet sur la vraie souris, le vrai clavier ni le premier plan,
même fenêtre recouverte. Écoute limitée à 127.0.0.1.

Erreurs : toute défaillance est une CDPError, jamais une exception de transport brute. PageGone : connexion perdue
(onglet fermé, navigateur arrêté), la page est à jeter. PageBusy : la page ne répond pas dans le délai (boîte
JavaScript ouverte, script long), le port reste valable.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import time
import urllib.request
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
        raise CDPError(f"DevTools injoignable sur le port {port} : {error}") from error
    if not isinstance(data, list):
        raise CDPError(f"DevTools : réponse inattendue sur le port {port}.")
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
            raise CDPError("Aucune page DevTools disponible.")
        if len(pages) > 1:
            raise AmbiguousPage("Plusieurs pages DevTools : onglet actif impossible à désigner.")
        return pages[0]
    wanted = page_title_from_window(window_title).strip().lower()
    exact = [t for t in pages if _title(t) == wanted]
    if len(exact) == 1:
        return exact[0]
    agree = [t for t in pages if titles_agree(window_title, _title(t))]
    if len(agree) == 1:
        return agree[0]
    raise AmbiguousPage("Onglet actif introuvable sans ambiguïté parmi les pages DevTools.")


class Page:
    """Connexion à une page (onglet ou fenêtre Electron)."""

    def __init__(self, port: int, match: str | None = None, timeout_s: float = 5.0) -> None:
        for attempt in range(2):
            pages = [t for t in targets(port) if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            try:
                self.info = pick_page(pages, match)
                break
            except AmbiguousPage:
                if attempt:
                    raise
                time.sleep(0.15)  # titre de fenêtre en retard sur document.title : une seconde lecture
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
            raise CDPError(f"Connexion DevTools impossible sur le port {port} : {error}") from error

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
                f"{method} : la page ne répond pas en {limit:.1f} s (boîte JavaScript ouverte ou script long : viser "
                "son bouton OK/Annuler, ou réessayer)"
            ) from error
        except (WebSocketException, OSError, ValueError) as error:  # ValueError : message illisible
            self.close()
            raise PageGone(f"{method} : connexion DevTools perdue ({type(error).__name__} : {error})") from error
        if "error" in message:
            error = message["error"]
            raise CDPError(f"{method} : {error.get('message') if isinstance(error, dict) else error}")
        result = message.get("result", {})
        return result if isinstance(result, dict) else {}

    def evaluate(self, expression: str, timeout_s: float | None = None) -> Any:
        result = self.call(
            "Runtime.evaluate", timeout_s=timeout_s, expression=expression, returnByValue=True, awaitPromise=True
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise CDPError(f"JavaScript : {details.get('text') if isinstance(details, dict) else details}")
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
