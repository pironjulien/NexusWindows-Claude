"""Entrées PHYSIQUES de l'utilisateur (touches, boutons et molette de la souris), distinguées de toute entrée injectée.

GetLastInputInfo compte aussi les entrées injectées : les nôtres (SendInput autorisé), celles d'UI Automation (son
SetFocus injecte une touche VK 0xB9) ou d'un autre programme. Raw Input, lui, donne le périphérique d'origine : une
entrée injectée n'en a pas (hDevice nul). Une fenêtre de messages invisible reçoit ces notifications en arrière-plan
(RIDEV_INPUTSINK). Aucun crochet : rien ne s'insère dans la chaîne d'entrée de l'utilisateur, aucune latence ajoutée, et le
contenu des touches n'est ni lu ni gardé — seulement l'instant de la dernière entrée physique.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

WM_INPUT, RID_INPUT, RIDEV_INPUTSINK = 0x00FF, 0x10000003, 0x00000100
RIM_TYPEMOUSE, RIM_TYPEKEYBOARD = 0, 1
HWND_MESSAGE = wintypes.HWND(-3)
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT), ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]  # fmt: skip


class _RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD), ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]  # fmt: skip


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]  # fmt: skip


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_user32.DefWindowProcW.restype = LRESULT
_user32.RegisterClassW.argtypes = (ctypes.POINTER(_WNDCLASSW),)
_user32.RegisterClassW.restype = wintypes.ATOM
_user32.CreateWindowExW.argtypes = (
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
)  # fmt: skip
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.RegisterRawInputDevices.argtypes = (ctypes.POINTER(_RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT)
_user32.RegisterRawInputDevices.restype = wintypes.BOOL
_user32.GetRawInputData.argtypes = (
    wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID, ctypes.POINTER(wintypes.UINT), wintypes.UINT,
)  # fmt: skip
_user32.GetRawInputData.restype = wintypes.UINT
_user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_user32.GetMessageW.restype = wintypes.BOOL
_user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
_user32.DispatchMessageW.restype = LRESULT
_kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE

_lock = threading.Lock()
_started = threading.Event()
_ready = threading.Event()
_last = 0.0  # time.monotonic() de la dernière entrée physique (0 : aucune vue depuis le démarrage)
_listening_since = 0.0  # time.monotonic() du début de l'écoute
_ok = False
counts = {"physical": 0, "injected": 0}  # diagnostic : nombre d'événements, jamais leur contenu


@WNDPROC
def _proc(hwnd, msg, wparam, lparam):
    global _last
    if msg == WM_INPUT:
        buffer = (ctypes.c_ubyte * 64)()
        size = wintypes.UINT(ctypes.sizeof(buffer))
        got = _user32.GetRawInputData(
            wintypes.HANDLE(lparam), RID_INPUT, buffer, ctypes.byref(size), ctypes.sizeof(_RAWINPUTHEADER)
        )
        if got != 0xFFFFFFFF and got >= ctypes.sizeof(_RAWINPUTHEADER):
            header = _RAWINPUTHEADER.from_buffer(buffer)
            if not header.hDevice:
                counts["injected"] += 1
            elif header.dwType == RIM_TYPEKEYBOARD:
                _last = time.monotonic()
                counts["physical"] += 1
            elif header.dwType == RIM_TYPEMOUSE:
                # Un simple déplacement ne compte pas (il n'envoie rien à une fenêtre) ; boutons et molette, si.
                buttons = int.from_bytes(bytes(buffer[ctypes.sizeof(_RAWINPUTHEADER) + 4 :][:2]), "little")
                if buttons:
                    _last = time.monotonic()
                    counts["physical"] += 1
    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _loop() -> None:
    global _ok, _listening_since
    _listening_since = time.monotonic()
    try:
        hinst = _kernel32.GetModuleHandleW(None)
        wc = _WNDCLASSW(lpfnWndProc=_proc, hInstance=hinst, lpszClassName="NexusClaudePhysicalInput")
        _user32.RegisterClassW(ctypes.byref(wc))
        hwnd = _user32.CreateWindowExW(0, wc.lpszClassName, None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
        devices = (_RAWINPUTDEVICE * 2)(
            _RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, hwnd),  # clavier
            _RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, hwnd),  # souris
        )
        _ok = bool(hwnd) and bool(_user32.RegisterRawInputDevices(devices, 2, ctypes.sizeof(_RAWINPUTDEVICE)))
    finally:
        _ready.set()
    if not _ok:
        return
    msg = wintypes.MSG()
    while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        _user32.DispatchMessageW(ctypes.byref(msg))


def start(timeout_s: float = 2.0) -> bool:
    """Lance l'écoute (une fois par processus). Vrai si Raw Input est disponible."""
    with _lock:
        if not _started.is_set():
            _started.set()
            threading.Thread(target=_loop, name="nexus-physical-input", daemon=True).start()
    _ready.wait(timeout_s)
    return _ok


def available() -> bool:
    return _ok


def idle_s() -> float:
    """Secondes depuis la dernière entrée physique de l'utilisateur (infini si aucune depuis le démarrage)."""
    return float("inf") if not _last else time.monotonic() - _last


def since(moment: float) -> bool:
    """L'utilisateur a-t-il touché clavier ou souris (physiquement) depuis `moment` (time.monotonic) ?"""
    return _last > moment


def listening_s() -> float:
    """Secondes d'écoute (0 si Raw Input indisponible) : idle_s() n'est connu que sur cette durée."""
    return time.monotonic() - _listening_since if _ok else 0.0


def summary() -> str:
    """Diagnostic pour l'outil logs : nombres d'événements vus depuis le démarrage, jamais leur contenu."""
    if not _ok:
        return "Raw Input indisponible (repli : GetLastInputInfo, entrées injectées comprises)"
    idle = idle_s()
    last = "aucune encore" if idle == float("inf") else f"dernière il y a {idle:.0f} s"
    return f"Raw Input actif : {counts['physical']} entrées physiques ({last}), {counts['injected']} injectées écartées"
