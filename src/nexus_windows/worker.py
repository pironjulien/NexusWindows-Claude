"""Thread COM dédié : toutes les opérations bureau y passent, une à la fois, avec un délai maximal.

UI Automation est créé et utilisé sur ce seul thread (appartement multithread). Si une application figée bloque un
appel au-delà du délai, le thread est abandonné ET son jeton d'annulation est levé : dès qu'il se débloque, il
s'arrête au prochain point de contrôle (avant chaque étape, chaque technique, chaque attente) au lieu de finir
l'action dans le dos de l'utilisateur. Un nouveau thread prend le relais. Ces points de contrôle servent aussi à
l'arrêt d'urgence de l'utilisateur et à l'interruption d'une activation brève dès qu'il touche clavier ou souris.
"""

from __future__ import annotations

import concurrent.futures as cf
import queue
import threading
from collections.abc import Callable
from typing import Any

from . import foreground, stop, uia

_LOCAL = threading.local()


class Cancelled(RuntimeError):
    """L'appel a été abandonné (délai dépassé) : ne plus rien faire."""


class Stopped(Cancelled):
    """Arrêt d'urgence de l'utilisateur : ne plus rien faire, mais rendre compte de ce qui a été fait."""


def check_cancelled() -> None:
    """Point de contrôle : à appeler avant toute action sur le bureau dans un travail du thread COM. Arrête aussi
    tout sur l'arrêt d'urgence de l'utilisateur (stop.py) et, pendant une activation brève, à sa première entrée physique
    (premier plan rendu sur-le-champ, puis foreground.UserInterrupted)."""
    token: threading.Event | None = getattr(_LOCAL, "cancel", None)
    if token is not None and token.is_set():
        raise Cancelled("Opération annulée : l'appel avait dépassé son délai et a été abandonné.")
    if stop.remaining_s() > 0:
        raise Stopped(stop.message())
    foreground.check_activation()


class Worker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: queue.Queue | None = None
        self._thread: threading.Thread | None = None
        self._cancel: threading.Event | None = None
        self.generation = 0

    def _start(self) -> None:
        self._queue = queue.Queue()
        self._cancel = threading.Event()
        self.generation += 1
        self._thread = threading.Thread(
            target=self._run, args=(self._queue, self._cancel), name=f"nexus-com-{self.generation}", daemon=True
        )
        self._thread.start()

    @staticmethod
    def _run(jobs: queue.Queue, cancel: threading.Event) -> None:
        _LOCAL.cancel = cancel
        uia.com_init()
        automation = uia.Automation()
        while True:
            job = jobs.get()
            if job is None:
                return
            fn, future = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                if cancel.is_set():
                    raise Cancelled("Opération annulée : l'appel avait dépassé son délai et a été abandonné.")
                future.set_result(fn(automation))
            except BaseException as error:  # noqa: BLE001 - transmis à l'appelant
                future.set_exception(error)

    def submit(self, fn: Callable[[uia.Automation], Any]) -> cf.Future:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._start()
            future: cf.Future = cf.Future()
            assert self._queue is not None
            self._queue.put((fn, future))
            return future

    def abandon(self) -> None:
        """Le thread courant est bloqué : jeton d'annulation levé, travaux en attente échoués, nouveau thread au
        prochain appel."""
        with self._lock:
            old, cancel = self._queue, self._cancel
            self._thread = None
            self._queue = None
            self._cancel = None
        if cancel is not None:
            cancel.set()
        if old is None:
            return
        while True:
            try:
                job = old.get_nowait()
            except queue.Empty:
                break
            if job is not None and job[1].set_running_or_notify_cancel():
                job[1].set_exception(Cancelled("Annulé : le thread bureau précédent était bloqué."))
        old.put(None)
