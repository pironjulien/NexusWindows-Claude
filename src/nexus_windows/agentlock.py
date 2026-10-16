"""Jeton commun aux agents de Julien (NexusClaude, NexusAGY, NexusCodex) pour la souris et le premier plan.

Protocole : `NexusHarness/shared/foreground-token.md`. Tenir le jeton = verrou exclusif LockFileEx sur l'octet
2^32 de `~/.nexus/foreground.lock` (local au poste, hors OneDrive). Windows le libère à la fermeture du handle,
donc à la mort du processus : jamais de jeton périmé, aucune date à surveiller. Le détenteur écrit en tête du
fichier qui il est (diagnostic seulement : seul le verrou fait foi).

On ne le tient que pendant une prise réelle du premier plan ou de la souris (activation brève, vraie souris et
vrai clavier autorisés, fenêtre mise devant) : les actions en arrière-plan ne le prennent jamais.
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from ctypes import wintypes as w
from pathlib import Path

OWNER = "NexusClaude"
PATH = Path.home() / ".nexus" / "foreground.lock"
WAIT_S = 8.0  # attente d'un autre agent avant de reporter l'action
POLL_S = 0.05

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", w.DWORD),
        ("OffsetHigh", w.DWORD),
        ("hEvent", w.HANDLE),
    ]


def _fn(name: str, restype, *argtypes):
    func = getattr(_kernel32, name)
    func.restype = restype
    func.argtypes = list(argtypes)
    return func


_CreateFileW = _fn("CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE)
_LockFileEx = _fn("LockFileEx", w.BOOL, w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(_OVERLAPPED))
_UnlockFileEx = _fn("UnlockFileEx", w.BOOL, w.HANDLE, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(_OVERLAPPED))
_WriteFile = _fn("WriteFile", w.BOOL, w.HANDLE, w.LPCVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID)
_SetFilePointerEx = _fn("SetFilePointerEx", w.BOOL, w.HANDLE, ctypes.c_longlong, ctypes.c_void_p, w.DWORD)
_SetEndOfFile = _fn("SetEndOfFile", w.BOOL, w.HANDLE)
_CloseHandle = _fn("CloseHandle", w.BOOL, w.HANDLE)

_GENERIC_RW = 0x80000000 | 0x40000000
_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_ALWAYS = 4
_INVALID = w.HANDLE(-1).value
_EXCLUSIVE_NOW = 0x2 | 0x1  # LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY


class Busy(RuntimeError):
    """Un autre agent tient la souris et le premier plan au-delà de l'attente."""


def _region() -> _OVERLAPPED:
    region = _OVERLAPPED()
    region.OffsetHigh = 1  # octet 2^32 : au-delà du texte de diagnostic, qui reste lisible par tous
    return region


def _open() -> int:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = _CreateFileW(str(PATH), _GENERIC_RW, _SHARE_ALL, None, _OPEN_ALWAYS, 0x80, None)
    if not handle or handle == _INVALID:
        raise OSError(ctypes.get_last_error(), f"jeton commun inaccessible : {PATH}")
    return handle


def _try_lock(handle: int) -> bool:
    return bool(_LockFileEx(handle, _EXCLUSIVE_NOW, 0, 1, 0, ctypes.byref(_region())))


def _write(handle: int, text: str) -> None:
    data = text.encode("utf-8")
    _SetFilePointerEx(handle, 0, None, 0)
    written = w.DWORD()
    _WriteFile(handle, data, len(data), ctypes.byref(written), None)
    _SetEndOfFile(handle)


def _release(handle: int) -> None:
    try:
        _write(handle, "")
        _UnlockFileEx(handle, 0, 1, 0, ctypes.byref(_region()))
    finally:
        _CloseHandle(handle)


_STATE = threading.Lock()
_handle: int | None = None
_depth = 0


def _read_holder() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8") or "{}")
    except OSError, ValueError:
        return {}


def other_holder() -> dict | None:
    """Autre agent qui tient le jeton en ce moment ({owner, pid, since, purpose}, champs possiblement vides), ou None
    (libre, ou tenu par nous)."""
    if _depth:
        return None
    try:
        handle = _open()
    except OSError:
        return None
    try:
        if _try_lock(handle):
            _UnlockFileEx(handle, 0, 1, 0, ctypes.byref(_region()))
            return None
    finally:
        _CloseHandle(handle)
    return _read_holder()


def describe(holder: dict) -> str:
    owner = holder.get("owner") or "un autre agent"
    since = holder.get("since")
    held = f" depuis {time.time() - float(since):.1f} s" if isinstance(since, (int, float)) else ""
    purpose = f" ({holder['purpose']})" if holder.get("purpose") else ""
    return f"{owner} tient la souris et le premier plan{held}{purpose}"


@contextmanager
def hold(purpose: str, wait_s: float = WAIT_S, checkpoint: Callable[[], None] | None = None):
    """Bloc où nous tenons le jeton. Réentrant dans le processus. Attend au plus wait_s qu'un autre agent le rende
    (checkpoint appelé à chaque tour : arrêt d'urgence, appel abandonné), sinon Busy."""
    global _handle, _depth
    with _STATE:
        if _depth:
            _depth += 1
            nested = True
        else:
            nested = False
    if nested:
        try:
            yield
        finally:
            with _STATE:
                _depth -= 1
        return
    handle = _open()
    try:
        deadline = time.monotonic() + wait_s
        while not _try_lock(handle):
            if checkpoint is not None:
                checkpoint()
            if time.monotonic() >= deadline:
                raise Busy(f"{describe(_read_holder())} : action reportée, rien n'a été fait")
            time.sleep(POLL_S)
    except BaseException:
        _CloseHandle(handle)
        raise
    _write(handle, json.dumps({"owner": OWNER, "pid": os.getpid(), "since": time.time(), "purpose": purpose}))
    with _STATE:
        _handle, _depth = handle, 1
    try:
        yield
    finally:
        with _STATE:
            _handle, _depth = None, 0
        _release(handle)
