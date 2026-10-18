"""Applications Store (UWP, WinUI empaquetées) suspendues par Windows : reprise sans premier plan.

Relevé le 18/10 sur la Calculatrice lancée sans activation : son contenu (40 éléments) apparaît, puis Windows la
suspend (gestion du cycle de vie des applications, PLM) et son arbre UI Automation tombe à 0 ; regarder ou attendre
n'y change rien. IPackageDebugSettings.Resume, l'interface documentée des débogueurs, la remet « en cours » et son
contenu revient aussitôt, sans premier plan ni réglage persistant (contrairement à EnableDebugging, jamais appelé).
"""

from __future__ import annotations

import ctypes
from ctypes import HRESULT, POINTER, c_int, c_ulong, c_void_p, c_wchar_p
from ctypes import wintypes as wt
from typing import TYPE_CHECKING

import comtypes
import comtypes.client
from comtypes import COMMETHOD, GUID, IUnknown

from . import foreground
from . import win32 as w

UWP_CORE_CLASS = "Windows.UI.Core.CoreWindow"
SUSPENDING, SUSPENDED = 2, 3  # PACKAGE_EXECUTION_STATE
APPMODEL_ERROR_NO_PACKAGE = 15700
ERROR_INSUFFICIENT_BUFFER = 122


class IPackageDebugSettings(IUnknown):
    _iid_ = GUID("{F27C3930-8029-4AD1-94E3-3DBA417810C1}")
    _methods_ = [
        COMMETHOD([], HRESULT, "EnableDebugging", (["in"], c_wchar_p), (["in"], c_wchar_p), (["in"], c_wchar_p)),
        COMMETHOD([], HRESULT, "DisableDebugging", (["in"], c_wchar_p)),
        COMMETHOD([], HRESULT, "Suspend", (["in"], c_wchar_p)),
        COMMETHOD([], HRESULT, "Resume", (["in"], c_wchar_p)),
        COMMETHOD([], HRESULT, "TerminateAllProcesses", (["in"], c_wchar_p)),
        COMMETHOD([], HRESULT, "SetTargetSessionId", (["in"], c_ulong)),
        COMMETHOD(
            [],
            HRESULT,
            "EnumerateBackgroundTasks",
            (["in"], c_wchar_p),
            (["out"], POINTER(c_ulong)),
            (["out"], POINTER(c_void_p)),
            (["out"], POINTER(c_void_p)),
        ),  # fmt: skip
        COMMETHOD([], HRESULT, "ActivateBackgroundTask", (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "StartServicing", (["in"], c_wchar_p)),
        COMMETHOD([], HRESULT, "StopServicing", (["in"], c_wchar_p)),
        COMMETHOD([], HRESULT, "StartSessionRedirection", (["in"], c_wchar_p), (["in"], c_ulong)),
        COMMETHOD([], HRESULT, "StopSessionRedirection", (["in"], c_wchar_p)),
        COMMETHOD([], HRESULT, "GetPackageExecutionState", (["in"], c_wchar_p), (["out"], POINTER(c_int))),
    ]

    if TYPE_CHECKING:  # méthodes créées par comtypes à partir de _methods_ (déclarées pour le contrôle de types)

        def GetPackageExecutionState(self, name: str) -> int: ...
        def Resume(self, name: str) -> None: ...


CLSID_PACKAGE_DEBUG_SETTINGS = GUID("{B1AEC16F-2383-4852-B0E9-8F0B1DC66B4D}")
_GetPackageFullName = ctypes.WinDLL("kernel32", use_last_error=True).GetPackageFullName
_GetPackageFullName.restype = wt.LONG
_GetPackageFullName.argtypes = [wt.HANDLE, POINTER(ctypes.c_uint32), wt.LPWSTR]


def package_full_name(pid: int) -> str:
    """Nom complet du paquet du processus (« Microsoft.WindowsCalculator_11…__8wekyb3d8bbwe »), « » s'il n'en a pas."""
    handle = w.OpenProcess(w.PROCESS_QUERY_LIMITED_INFORMATION, False, pid) if pid else None
    if not handle:
        return ""
    try:
        size = ctypes.c_uint32(0)
        if _GetPackageFullName(handle, ctypes.byref(size), None) != ERROR_INSUFFICIENT_BUFFER:
            return ""  # APPMODEL_ERROR_NO_PACKAGE : programme de bureau ordinaire
        buffer = ctypes.create_unicode_buffer(size.value)
        return buffer.value if _GetPackageFullName(handle, ctypes.byref(size), buffer) == 0 else ""
    finally:
        w.CloseHandle(handle)


def app_pids(hwnd: int) -> set[int]:
    """Processus de l'application d'une fenêtre : elle-même, ou, pour un cadre UWP (ApplicationFrameHost), ses
    fenêtres enfants d'un autre processus et la CoreWindow de même titre restée hors du cadre."""
    host = w.window_pid(hwnd)
    if w.process_name(host).lower() not in foreground.HOST_PROCESSES:
        return {host} if host else set()
    pids = {w.window_pid(child) for child in w.enum_children(hwnd)} - {host, 0}
    title = w.window_text(hwnd)
    if title:
        pids |= {
            w.window_pid(other)
            for other in w.enum_windows()
            if w.class_name(other) == UWP_CORE_CLASS and w.window_text(other) == title
        } - {0}
    return pids


def _settings() -> IPackageDebugSettings:
    return comtypes.client.CreateObject(CLSID_PACKAGE_DEBUG_SETTINGS, interface=IPackageDebugSettings)


def resume_suspended(hwnd: int, extra_pids: set[int] | None = None) -> list[str]:
    """Reprend les paquets suspendus (ou en cours de suspension) de l'application de cette fenêtre ; rend les noms
    des paquets repris (liste vide : rien de suspendu, ou application qui n'est pas empaquetée)."""
    names = {name for pid in app_pids(hwnd) | (extra_pids or set()) if (name := package_full_name(pid))}
    if not names:
        return []
    resumed: list[str] = []
    try:
        settings = _settings()
        for name in sorted(names):
            if settings.GetPackageExecutionState(name) in (SUSPENDING, SUSPENDED):
                settings.Resume(name)
                resumed.append(name)
    except comtypes.COMError, OSError:
        return resumed  # interface indisponible : l'appelant garde sa note honnête
    return resumed
