"""Bindings Win32 minimales (ctypes), en pixels physiques.

Le processus se déclare « Per-Monitor DPI aware v2 » à l'import : toutes les coordonnées manipulées ici sont des
pixels physiques de l'écran, quel que soit le facteur d'échelle de chaque moniteur.
"""

from __future__ import annotations

import ctypes
import os
import uuid
from ctypes import wintypes as w
from dataclasses import dataclass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
shcore = ctypes.WinDLL("shcore")

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t


def _fn(dll, name: str, restype, *argtypes):
    func = getattr(dll, name)
    func.restype = restype
    func.argtypes = list(argtypes)
    return func


# --- DPI : à faire avant toute autre API fenêtre --------------------------------------------------------------
_DPI_PER_MONITOR_V2 = ctypes.c_void_p(-4)
try:
    _fn(user32, "SetProcessDpiAwarenessContext", w.BOOL, ctypes.c_void_p)(_DPI_PER_MONITOR_V2)
except AttributeError, OSError:  # pragma: no cover - Windows antérieur à 10 1703
    pass


class RECT(ctypes.Structure):
    _fields_ = [("left", w.LONG), ("top", w.LONG), ("right", w.LONG), ("bottom", w.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", w.LONG), ("y", w.LONG)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.DWORD),
        ("flags", w.DWORD),
        ("hwndActive", w.HWND),
        ("hwndFocus", w.HWND),
        ("hwndCapture", w.HWND),
        ("hwndMenuOwner", w.HWND),
        ("hwndMoveSize", w.HWND),
        ("hwndCaret", w.HWND),
        ("rcCaret", RECT),
    ]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", w.UINT),
        ("flags", w.UINT),
        ("showCmd", w.UINT),
        ("ptMinPosition", POINT),
        ("ptMaxPosition", POINT),
        ("rcNormalPosition", RECT),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD),
        ("biWidth", w.LONG),
        ("biHeight", w.LONG),
        ("biPlanes", w.WORD),
        ("biBitCount", w.WORD),
        ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD),
        ("biXPelsPerMeter", w.LONG),
        ("biYPelsPerMeter", w.LONG),
        ("biClrUsed", w.DWORD),
        ("biClrImportant", w.DWORD),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", w.LONG),
        ("dy", w.LONG),
        ("mouseData", w.DWORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", w.WORD),
        ("wScan", w.WORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("_pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]


WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

OpenDesktopW = _fn(user32, "OpenDesktopW", w.HANDLE, w.LPCWSTR, w.DWORD, w.BOOL, w.DWORD)
SetThreadDesktop = _fn(user32, "SetThreadDesktop", w.BOOL, w.HANDLE)
DESKTOP_ALL_ACCESS = 0x01FF
_hdesk_default: int | None = None  # bureau interactif « Default », ouvert une fois

EnumWindows = _fn(user32, "EnumWindows", w.BOOL, WNDENUMPROC, w.LPARAM)
EnumChildWindows = _fn(user32, "EnumChildWindows", w.BOOL, w.HWND, WNDENUMPROC, w.LPARAM)
IsWindow = _fn(user32, "IsWindow", w.BOOL, w.HWND)
FindWindowW = _fn(user32, "FindWindowW", w.HWND, w.LPCWSTR, w.LPCWSTR)
FindWindowExW = _fn(user32, "FindWindowExW", w.HWND, w.HWND, w.HWND, w.LPCWSTR, w.LPCWSTR)
IsWindowVisible = _fn(user32, "IsWindowVisible", w.BOOL, w.HWND)
IsWindowEnabled = _fn(user32, "IsWindowEnabled", w.BOOL, w.HWND)
IsIconic = _fn(user32, "IsIconic", w.BOOL, w.HWND)
IsZoomed = _fn(user32, "IsZoomed", w.BOOL, w.HWND)
IsHungAppWindow = _fn(user32, "IsHungAppWindow", w.BOOL, w.HWND)
IsChild = _fn(user32, "IsChild", w.BOOL, w.HWND, w.HWND)
GetWindowTextLengthW = _fn(user32, "GetWindowTextLengthW", ctypes.c_int, w.HWND)
GetWindowTextW = _fn(user32, "GetWindowTextW", ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
GetClassNameW = _fn(user32, "GetClassNameW", ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
GetWindowThreadProcessId = _fn(user32, "GetWindowThreadProcessId", w.DWORD, w.HWND, ctypes.POINTER(w.DWORD))
GetWindowRect = _fn(user32, "GetWindowRect", w.BOOL, w.HWND, ctypes.POINTER(RECT))
GetClientRect = _fn(user32, "GetClientRect", w.BOOL, w.HWND, ctypes.POINTER(RECT))
ClientToScreen = _fn(user32, "ClientToScreen", w.BOOL, w.HWND, ctypes.POINTER(POINT))
ScreenToClient = _fn(user32, "ScreenToClient", w.BOOL, w.HWND, ctypes.POINTER(POINT))
GetForegroundWindow = _fn(user32, "GetForegroundWindow", w.HWND)
SetForegroundWindow = _fn(user32, "SetForegroundWindow", w.BOOL, w.HWND)
BringWindowToTop = _fn(user32, "BringWindowToTop", w.BOOL, w.HWND)
GetAncestor = _fn(user32, "GetAncestor", w.HWND, w.HWND, w.UINT)
GetWindow = _fn(user32, "GetWindow", w.HWND, w.HWND, w.UINT)
GetParent = _fn(user32, "GetParent", w.HWND, w.HWND)
GetDlgCtrlID = _fn(user32, "GetDlgCtrlID", ctypes.c_int, w.HWND)
GetWindowLongPtrW = _fn(user32, "GetWindowLongPtrW", ctypes.c_ssize_t, w.HWND, ctypes.c_int)
ChildWindowFromPointEx = _fn(user32, "ChildWindowFromPointEx", w.HWND, w.HWND, POINT, w.UINT)
WindowFromPoint = _fn(user32, "WindowFromPoint", w.HWND, POINT)
GetGUIThreadInfo = _fn(user32, "GetGUIThreadInfo", w.BOOL, w.DWORD, ctypes.POINTER(GUITHREADINFO))
PostMessageW = _fn(user32, "PostMessageW", w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
SendMessageTimeoutW = _fn(
    user32,
    "SendMessageTimeoutW",
    LRESULT,
    w.HWND,
    w.UINT,
    w.WPARAM,
    w.LPARAM,
    w.UINT,
    w.UINT,
    ctypes.POINTER(ULONG_PTR),
)
SetWindowPos = _fn(
    user32, "SetWindowPos", w.BOOL, w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT
)
ShowWindow = _fn(user32, "ShowWindow", w.BOOL, w.HWND, ctypes.c_int)
ShowWindowAsync = _fn(user32, "ShowWindowAsync", w.BOOL, w.HWND, ctypes.c_int)
GetWindowPlacement = _fn(user32, "GetWindowPlacement", w.BOOL, w.HWND, ctypes.POINTER(WINDOWPLACEMENT))
SetWindowPlacement = _fn(user32, "SetWindowPlacement", w.BOOL, w.HWND, ctypes.POINTER(WINDOWPLACEMENT))
GetCursorPos = _fn(user32, "GetCursorPos", w.BOOL, ctypes.POINTER(POINT))
SetCursorPos = _fn(user32, "SetCursorPos", w.BOOL, ctypes.c_int, ctypes.c_int)
SendInput = _fn(user32, "SendInput", w.UINT, w.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
GetLastInputInfo = _fn(user32, "GetLastInputInfo", w.BOOL, ctypes.POINTER(LASTINPUTINFO))
MapVirtualKeyW = _fn(user32, "MapVirtualKeyW", w.UINT, w.UINT, w.UINT)
VkKeyScanW = _fn(user32, "VkKeyScanW", ctypes.c_short, w.WCHAR)
GetKeyboardLayout = _fn(user32, "GetKeyboardLayout", w.HKL, w.DWORD)
VkKeyScanExW = _fn(user32, "VkKeyScanExW", ctypes.c_short, w.WCHAR, w.HKL)
AttachThreadInput = _fn(user32, "AttachThreadInput", w.BOOL, w.DWORD, w.DWORD, w.BOOL)
GetKeyboardState = _fn(user32, "GetKeyboardState", w.BOOL, ctypes.POINTER(ctypes.c_ubyte * 256))
SetKeyboardState = _fn(user32, "SetKeyboardState", w.BOOL, ctypes.POINTER(ctypes.c_ubyte * 256))
GetSystemMetrics = _fn(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
GetDpiForWindow = _fn(user32, "GetDpiForWindow", w.UINT, w.HWND)
GetWindowDisplayAffinity = _fn(user32, "GetWindowDisplayAffinity", w.BOOL, w.HWND, ctypes.POINTER(w.DWORD))
PrintWindow = _fn(user32, "PrintWindow", w.BOOL, w.HWND, w.HDC, w.UINT)
GetDC = _fn(user32, "GetDC", w.HDC, w.HWND)
ReleaseDC = _fn(user32, "ReleaseDC", ctypes.c_int, w.HWND, w.HDC)
PostThreadMessageW = _fn(user32, "PostThreadMessageW", w.BOOL, w.DWORD, w.UINT, w.WPARAM, w.LPARAM)

CreateCompatibleDC = _fn(gdi32, "CreateCompatibleDC", w.HDC, w.HDC)
DeleteDC = _fn(gdi32, "DeleteDC", w.BOOL, w.HDC)
CreateDIBSection = _fn(
    gdi32,
    "CreateDIBSection",
    w.HBITMAP,
    w.HDC,
    ctypes.c_void_p,
    w.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    w.HANDLE,
    w.DWORD,
)
SelectObject = _fn(gdi32, "SelectObject", w.HGDIOBJ, w.HDC, w.HGDIOBJ)
DeleteObject = _fn(gdi32, "DeleteObject", w.BOOL, w.HGDIOBJ)
GdiFlush = _fn(gdi32, "GdiFlush", w.BOOL)
BitBlt = _fn(gdi32, "BitBlt", w.BOOL, w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HDC,
             ctypes.c_int, ctypes.c_int, w.DWORD)  # fmt: skip
SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000

GetWindowDpiAwarenessContext = _fn(user32, "GetWindowDpiAwarenessContext", ctypes.c_void_p, w.HWND)
GetAwarenessFromDpiAwarenessContext = _fn(user32, "GetAwarenessFromDpiAwarenessContext", ctypes.c_int, ctypes.c_void_p)
GetDpiFromDpiAwarenessContext = _fn(user32, "GetDpiFromDpiAwarenessContext", w.UINT, ctypes.c_void_p)
MonitorFromWindow = _fn(user32, "MonitorFromWindow", w.HMONITOR, w.HWND, w.DWORD)
GetDpiForMonitor = _fn(
    shcore, "GetDpiForMonitor", ctypes.c_long, w.HMONITOR, ctypes.c_int, ctypes.POINTER(w.UINT), ctypes.POINTER(w.UINT)
)
GetAsyncKeyState = _fn(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
GetNextDlgGroupItem = _fn(user32, "GetNextDlgGroupItem", w.HWND, w.HWND, w.HWND, w.BOOL)

OpenProcess = _fn(kernel32, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
CloseHandle = _fn(kernel32, "CloseHandle", w.BOOL, w.HANDLE)
QueryFullProcessImageNameW = _fn(
    kernel32, "QueryFullProcessImageNameW", w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)
)
GetCurrentThreadId = _fn(kernel32, "GetCurrentThreadId", w.DWORD)
GetTickCount = _fn(kernel32, "GetTickCount", w.DWORD)

DwmGetWindowAttribute = _fn(dwmapi, "DwmGetWindowAttribute", ctypes.c_long, w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD)

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32")
CommandLineToArgvW = _fn(
    shell32, "CommandLineToArgvW", ctypes.POINTER(w.LPWSTR), w.LPCWSTR, ctypes.POINTER(ctypes.c_int)
)
LocalFree = _fn(kernel32, "LocalFree", w.HLOCAL, w.HLOCAL)
SHGetKnownFolderPath = _fn(
    shell32, "SHGetKnownFolderPath", ctypes.c_long, ctypes.c_void_p, w.DWORD, w.HANDLE, ctypes.POINTER(ctypes.c_void_p)
)
CoTaskMemFree = _fn(ole32, "CoTaskMemFree", None, ctypes.c_void_p)

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
OpenProcessToken = _fn(advapi32, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
GetTokenInformation = _fn(
    advapi32, "GetTokenInformation", w.BOOL, w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)
)
GetSidSubAuthorityCount = _fn(advapi32, "GetSidSubAuthorityCount", ctypes.POINTER(ctypes.c_ubyte), ctypes.c_void_p)
GetSidSubAuthority = _fn(advapi32, "GetSidSubAuthority", ctypes.POINTER(w.DWORD), ctypes.c_void_p, w.DWORD)
GetCurrentProcess = _fn(kernel32, "GetCurrentProcess", w.HANDLE)

# --- Constantes ----------------------------------------------------------------------------------------------
GA_PARENT, GA_ROOT, GA_ROOTOWNER = 1, 2, 3
GW_HWNDNEXT, GW_HWNDPREV, GW_OWNER, GW_ENABLEDPOPUP = 2, 3, 4, 6
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_VISIBLE, WS_DISABLED, WS_CHILD, WS_POPUP = 0x10000000, 0x08000000, 0x40000000, 0x80000000
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x80, 0x40000, 0x8, 0x08000000
CWP_SKIPINVISIBLE, CWP_SKIPDISABLED, CWP_SKIPTRANSPARENT = 0x1, 0x2, 0x4
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
PW_RENDERFULLCONTENT = 0x2
SW_HIDE, SW_SHOWNORMAL, SW_SHOWMINIMIZED, SW_MAXIMIZE, SW_SHOWNOACTIVATE = 0, 1, 2, 3, 4
SW_MINIMIZE, SW_SHOWMINNOACTIVE, SW_SHOWNA, SW_RESTORE = 6, 7, 8, 9
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_ASYNCWINDOWPOS = 0x1, 0x2, 0x4, 0x10, 0x4000
HWND_TOP, HWND_BOTTOM = 0, 1
SMTO_NORMAL, SMTO_BLOCK, SMTO_ABORTIFHUNG = 0x0, 0x1, 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

WM_NULL, WM_SETTEXT, WM_GETTEXT, WM_GETTEXTLENGTH, WM_CLOSE = 0x0000, 0x000C, 0x000D, 0x000E, 0x0010
WM_KEYDOWN, WM_KEYUP, WM_CHAR, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0102, 0x0104, 0x0105
WM_COMMAND, WM_SYSCOMMAND = 0x0111, 0x0112
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0200, 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0204, 0x0205, 0x0207, 0x0208
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
WM_SETCURSOR, WM_MOUSEACTIVATE = 0x0020, 0x0021
MK_LBUTTON, MK_RBUTTON, MK_SHIFT, MK_CONTROL, MK_MBUTTON = 0x1, 0x2, 0x4, 0x8, 0x10
BM_GETCHECK, BM_SETCHECK, BM_CLICK = 0x00F0, 0x00F1, 0x00F5
BN_CLICKED = 0
EM_GETSEL, EM_SETSEL, EM_REPLACESEL, WM_CLEAR = 0x00B0, 0x00B1, 0x00C2, 0x0303
ES_READONLY = 0x0800
WHEEL_DELTA = 120

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x4, 0x8
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x1, 0x2, 0x4
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x8, 0x10, 0x20, 0x40
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x800, 0x1000, 0x8000, 0x4000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
WDA_NONE = 0


# --- Helpers ------------------------------------------------------------------------------------------------
def handle(value) -> int:
    """Normalise un HWND ctypes (None, int ou c_void_p) en entier."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return int(getattr(value, "value", 0) or 0)


def window_text(hwnd: int) -> str:
    length = GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = w.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_thread(hwnd: int) -> int:
    return GetWindowThreadProcessId(hwnd, None)


def process_path(pid: int) -> str:
    """Chemin complet de l'exécutable (vide si inaccessible, ex. processus protégé).

    Relu à chaque appel, jamais mis en cache : un pid libéré est réattribué à un autre programme, et la politique
    d'action (policy.act_refusal) se fonde sur ce nom. Un échec n'est donc jamais retenu non plus (quelques µs)."""
    h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = w.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        return buf.value if QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        CloseHandle(h)


def process_name(pid: int) -> str:
    return os.path.basename(process_path(pid)) or f"pid {pid}"


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong), ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]  # fmt: skip


_CreateToolhelp32Snapshot = _fn(kernel32, "CreateToolhelp32Snapshot", w.HANDLE, w.DWORD, w.DWORD)
_Process32FirstW = _fn(kernel32, "Process32FirstW", w.BOOL, w.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_Process32NextW = _fn(kernel32, "Process32NextW", w.BOOL, w.HANDLE, ctypes.POINTER(PROCESSENTRY32W))


def process_table() -> list[tuple[int, int, str]]:
    """Processus du système : (pid, pid parent, nom de l'exécutable). Instantané Toolhelp : quelques millisecondes."""
    snap = _CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    rows: list[tuple[int, int, str]] = []
    if not snap or snap == ctypes.c_void_p(-1).value:
        return rows
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            rows.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = _Process32NextW(snap, ctypes.byref(entry))
    finally:
        CloseHandle(snap)
    return rows


def command_line_args(text: str) -> list[str]:
    """Arguments d'une ligne de commande découpés par CommandLineToArgvW, comme Chromium sous Windows (guillemets,
    barres obliques inverses). Un nom de programme fictif est placé devant : son découpage obéit à d'autres règles."""
    count = ctypes.c_int(0)
    argv = CommandLineToArgvW("x " + text, ctypes.byref(count))
    if not argv:
        raise OSError(ctypes.get_last_error(), "CommandLineToArgvW failed")
    try:
        return [argv[i] for i in range(1, count.value)]
    finally:
        LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def known_folder(guid: str) -> str | None:
    """Chemin d'un dossier connu de Windows (FOLDERID_*) d'après son GUID « {…} » ; None si inconnu."""
    try:
        raw = uuid.UUID(guid).bytes_le
    except ValueError:
        return None
    path = ctypes.c_void_p()
    try:
        if SHGetKnownFolderPath(ctypes.create_string_buffer(raw, 16), 0, None, ctypes.byref(path)) != 0:
            return None
        return ctypes.wstring_at(path.value) if path.value else None
    finally:
        CoTaskMemFree(path)  # à libérer même en cas d'échec (documentation de SHGetKnownFolderPath)


def style(hwnd: int) -> int:
    return GetWindowLongPtrW(hwnd, GWL_STYLE) & 0xFFFFFFFF


def ex_style(hwnd: int) -> int:
    return GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF


def is_cloaked(hwnd: int) -> bool:
    value = w.DWORD()
    if DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)) != 0:
        return False
    return bool(value.value)


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def intersect(self, other: Rect) -> Rect | None:
        left, top = max(self.left, other.left), max(self.top, other.top)
        right, bottom = min(self.right, other.right), min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Rect(left, top, right, bottom)

    @property
    def empty(self) -> bool:
        return self.width <= 0 or self.height <= 0


def window_rect(hwnd: int) -> Rect:
    r = RECT()
    GetWindowRect(hwnd, ctypes.byref(r))
    return Rect(r.left, r.top, r.right, r.bottom)


def frame_rect(hwnd: int) -> Rect:
    """Bords visibles de la fenêtre (sans l'ombre invisible de Windows 10/11)."""
    r = RECT()
    if DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) == 0:
        rect = Rect(r.left, r.top, r.right, r.bottom)
        if not rect.empty:
            return rect
    return window_rect(hwnd)


def client_rect_screen(hwnd: int) -> Rect:
    r = RECT()
    GetClientRect(hwnd, ctypes.byref(r))
    p = POINT(0, 0)
    ClientToScreen(hwnd, ctypes.byref(p))
    return Rect(p.x, p.y, p.x + r.right, p.y + r.bottom)


def foreground() -> int:
    ensure_default_desktop()
    return handle(GetForegroundWindow())


def cursor_pos() -> tuple[int, int]:
    p = POINT()
    GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def last_input_tick() -> int:
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO))
    return info.dwTime if GetLastInputInfo(ctypes.byref(info)) else 0


def root(hwnd: int) -> int:
    return handle(GetAncestor(hwnd, GA_ROOT)) or hwnd


def owner(hwnd: int) -> int:
    return handle(GetWindow(hwnd, GW_OWNER))


def focus_info(hwnd: int) -> GUITHREADINFO | None:
    gui = GUITHREADINFO()
    gui.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not GetGUIThreadInfo(window_thread(hwnd), ctypes.byref(gui)):
        return None
    return gui


def ensure_default_desktop() -> bool:
    """Rattache le thread courant au bureau interactif 'Default' sur WinSta0."""
    global _hdesk_default
    try:
        if not _hdesk_default:
            _hdesk_default = handle(OpenDesktopW("Default", 0, False, DESKTOP_ALL_ACCESS))
        if _hdesk_default:
            return bool(SetThreadDesktop(_hdesk_default))
    except Exception:
        pass
    return False


def enum_windows() -> list[int]:
    ensure_default_desktop()
    out: list[int] = []

    @WNDENUMPROC
    def _cb(hwnd, _lparam):
        out.append(handle(hwnd))
        return True

    EnumWindows(_cb, 0)
    return out


def all_top_windows() -> list[int]:
    """Toutes les fenêtres de premier niveau, panneaux du shell compris : EnumWindows saute les bandes immersives
    (menu Démarrer de StartMenuExperienceHost, relevé le 20/10 : 413 fenêtres par FindWindowEx contre 355)."""
    ensure_default_desktop()
    out: list[int] = []
    hwnd = 0
    while hwnd := handle(FindWindowExW(None, hwnd or None, None, None)):
        out.append(hwnd)
    return out


def enum_children(hwnd: int) -> list[int]:
    out: list[int] = []

    @WNDENUMPROC
    def _cb(child, _lparam):
        out.append(handle(child))
        return True

    EnumChildWindows(hwnd, _cb, 0)
    return out


CB_GETDROPPEDSTATE = 0x157


def dropped_combos(top: int) -> set[int]:
    """Listes déroulantes Win32/WinForms de la fenêtre dont la liste est OUVERTE (CB_GETDROPPEDSTATE).

    Mesuré (WinForms, 2026-10-16) : liste ouverte, la ComboLBox est une fenêtre de premier niveau et UI Automation,
    depuis la liste déroulante, enchaîne sur ses « frères » : toutes les fenêtres du bureau (Chrome, Claude…). L'arbre
    de la fenêtre coûtait alors 11 à 20 s puis échouait (« aucun élément »). uia.Automation.tree n'y descend pas."""
    out: set[int] = set()
    for child in enum_children(top):
        cls = class_name(child).lower()
        if cls.startswith("windowsforms"):
            parts = cls.split(".")
            cls = parts[1] if len(parts) > 1 else cls
        if cls == "combobox" and IsWindowVisible(child) and send_message(child, CB_GETDROPPEDSTATE, timeout_ms=200):
            out.add(child)
    return out


def send_message(hwnd: int, msg: int, wparam: int = 0, lparam: int = 0, timeout_ms: int = 1000) -> int | None:
    """SendMessage borné dans le temps : None si la fenêtre ne répond pas (ou a disparu)."""
    result = ULONG_PTR(0)
    ok = SendMessageTimeoutW(
        hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG | SMTO_NORMAL, timeout_ms, ctypes.byref(result)
    )
    if not ok:
        return None
    return result.value


def responsive(hwnd: int, timeout_ms: int = 300) -> bool:
    """La fenêtre traite-t-elle ses messages ? (WM_NULL borné.) Avant tout appel Windows sans délai qui attend
    l'application (PrintWindow) : sur une application figée il bloquerait sans fin (mesuré : > 8 s, figée 2 min)."""
    return send_message(hwnd, WM_NULL, 0, 0, timeout_ms) is not None


def get_text_message(hwnd: int, timeout_ms: int = 1000) -> str | None:
    """Texte d'un contrôle via WM_GETTEXT (marshallé entre processus par Windows)."""
    length = send_message(hwnd, WM_GETTEXTLENGTH, 0, 0, timeout_ms)
    if length is None:
        return None
    buf = ctypes.create_unicode_buffer(length + 2)
    if send_message(hwnd, WM_GETTEXT, length + 2, ctypes.addressof(buf), timeout_ms) is None:
        return None
    return buf.value


def make_lparam(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    p = POINT(x, y)
    ScreenToClient(hwnd, ctypes.byref(p))
    return p.x, p.y


def window_at(x: int, y: int) -> int:
    """Fenêtre de premier niveau réellement visible au point écran (celle qui recevrait un vrai clic)."""
    return root(handle(WindowFromPoint(POINT(x, y))))


def dpi_awareness(hwnd: int) -> int:
    """0 : non adaptée au DPI (Windows l'agrandit), 1 : DPI système, 2 : par moniteur ; -1 si inconnu."""
    try:
        return GetAwarenessFromDpiAwarenessContext(GetWindowDpiAwarenessContext(hwnd))
    except OSError:
        return -1


def monitor_dpi(hwnd: int) -> int:
    """DPI effectif du moniteur qui affiche la fenêtre (MONITOR_DEFAULTTONEAREST, MDT_EFFECTIVE_DPI)."""
    dpi_x, dpi_y = w.UINT(), w.UINT()
    if GetDpiForMonitor(MonitorFromWindow(hwnd, 2), 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
        return dpi_x.value or 96
    return 96


def window_dpi_scale(hwnd: int) -> float:
    """Facteur entre ce que la fenêtre dessine (PrintWindow) et ce que le DWM affiche : 1.0 si adaptée au DPI."""
    awareness = dpi_awareness(hwnd)
    shown = monitor_dpi(hwnd)
    if awareness == 0:
        return shown / 96
    if awareness == 1:
        context = GetWindowDpiAwarenessContext(hwnd)
        system = GetDpiFromDpiAwarenessContext(context) or 96
        return shown / system
    return 1.0


def mouse_button_down() -> bool:
    """Un bouton de la vraie souris est enfoncé (état physique asynchrone)."""
    return any(GetAsyncKeyState(vk) & 0x8000 for vk in (0x01, 0x02, 0x04))


SECURITY_MANDATORY_HIGH_RID = 0x3000


def integrity_level(pid: int) -> int | None:
    """Niveau d'intégrité du processus (0x2000 moyen, 0x3000 élevé…), None si illisible."""
    process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid) if pid else GetCurrentProcess()
    if not process:
        return None
    token = w.HANDLE()
    try:
        if not OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return None
        size = w.DWORD()
        GetTokenInformation(token, 25, None, 0, ctypes.byref(size))  # TokenIntegrityLevel
        buffer = ctypes.create_string_buffer(size.value or 64)
        if not GetTokenInformation(token, 25, buffer, size, ctypes.byref(size)):
            return None
        sid = ctypes.c_void_p.from_buffer(buffer).value
        count = GetSidSubAuthorityCount(sid).contents.value
        return GetSidSubAuthority(sid, count - 1).contents.value
    finally:
        if token:
            CloseHandle(token)
        if pid:
            CloseHandle(process)


def deepest_child_at(hwnd: int, x: int, y: int) -> int:
    """Fenêtre enfant la plus profonde sous le point écran (x, y), visible et active."""
    current = hwnd
    for _ in range(32):
        cx, cy = screen_to_client(current, x, y)
        child = handle(
            ChildWindowFromPointEx(current, POINT(cx, cy), CWP_SKIPINVISIBLE | CWP_SKIPDISABLED | CWP_SKIPTRANSPARENT)
        )
        if not child or child == current:
            break
        current = child
    return current


# --- Alias d'exécution WindowsApps (reparse point AppExecLink) -------------------------------------------------
# Les exécutables de %LOCALAPPDATA%\Microsoft\WindowsApps ne sont pas des .exe : ce sont des points d'analyse
# (reparse points) de tag IO_REPARSE_TAG_APPEXECLINK. Les ouvrir en lecture échoue (pe_subsystem rend None), ce qui
# permettait à un débogueur ou à un exécuteur de commandes de passer la politique de lancement. On lit donc la cible
# réelle du lien pour lui appliquer les mêmes règles qu'à l'alias.
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FSCTL_GET_REPARSE_POINT = 0x000900A8
IO_REPARSE_TAG_APPEXECLINK = 0x8000001B

CreateFileW = _fn(
    kernel32, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE
)
DeviceIoControl = _fn(
    kernel32, "DeviceIoControl", w.BOOL, w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD,
    ctypes.POINTER(w.DWORD), ctypes.c_void_p,
)  # fmt: skip


def appexec_target(path: str) -> str | None:
    """Cible réelle d'un alias d'exécution WindowsApps (reparse point AppExecLink, tag 0x8000001B) : chemin complet de
    l'exécutable pointé. None si le fichier n'est PAS un tel alias (fichier ordinaire, absent ou illisible autrement) ;
    chaîne vide si c'en est un mais que sa cible n'a pas pu être lue (échec fermé du côté appelant)."""
    raw_handle = CreateFileW(
        path, 0, 7, None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None
    )
    hv = handle(raw_handle)
    if hv in (0, INVALID_HANDLE_VALUE):
        return None
    try:
        buffer = ctypes.create_string_buffer(16 * 1024)
        returned = w.DWORD()
        if not DeviceIoControl(
            raw_handle, FSCTL_GET_REPARSE_POINT, None, 0, buffer, len(buffer), ctypes.byref(returned), None
        ):
            return None  # pas un point d'analyse (ERROR_NOT_A_REPARSE_POINT) ou lecture refusée
        data = buffer.raw[: returned.value]
        # En-tête REPARSE_DATA_BUFFER (8 octets) + version du lien (4) ; viennent ensuite des chaînes UTF-16 à
        # terminaison nulle : [PackageFamilyName, AppUserModelId, ExecutablePath, ApplicationType].
        if len(data) < 12 or int.from_bytes(data[:4], "little") != IO_REPARSE_TAG_APPEXECLINK:
            return None
        parts = data[12:].decode("utf-16-le", "ignore").split("\x00")
        return parts[2] if len(parts) > 2 and parts[2] else ""
    finally:
        CloseHandle(raw_handle)
