"""Application de test Win32 pure (ctypes) : contrôles standard + zone de clic brute + raccourci Ctrl+K.

Chaque événement met à jour le contrôle « Statut » (lisible par WM_GETTEXT / UI Automation) et écrit une ligne JSON
sur stdout. Affichée sans activation, tout en bas de l'empilement : ne gêne pas l'utilisateur.
Usage : win32_app.py [--title T] [--x X] [--y Y]
"""

import argparse
import ctypes
import json
import sys
from ctypes import wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", w.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", w.HICON),
        ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
    ]


def proto(dll, name, res, *args):
    f = getattr(dll, name)
    f.restype, f.argtypes = res, list(args)
    return f


DefWindowProcW = proto(user32, "DefWindowProcW", LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
CreateWindowExW = proto(
    user32,
    "CreateWindowExW",
    w.HWND,
    w.DWORD,
    w.LPCWSTR,
    w.LPCWSTR,
    w.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    w.HWND,
    w.HMENU,
    w.HINSTANCE,
    w.LPVOID,
)
RegisterClassW = proto(user32, "RegisterClassW", w.ATOM, ctypes.POINTER(WNDCLASSW))
SendMessageW = proto(user32, "SendMessageW", LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
SetWindowTextW = proto(user32, "SetWindowTextW", w.BOOL, w.HWND, w.LPCWSTR)
ShowWindow = proto(user32, "ShowWindow", w.BOOL, w.HWND, ctypes.c_int)
SetWindowPos = proto(
    user32, "SetWindowPos", w.BOOL, w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT
)
GetMessageW = proto(user32, "GetMessageW", w.BOOL, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
TranslateMessage = proto(user32, "TranslateMessage", w.BOOL, ctypes.POINTER(w.MSG))
DispatchMessageW = proto(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(w.MSG))
GetKeyState = proto(user32, "GetKeyState", ctypes.c_short, ctypes.c_int)
CreateMenu = proto(user32, "CreateMenu", w.HMENU)
CreatePopupMenu = proto(user32, "CreatePopupMenu", w.HMENU)
AppendMenuW = proto(user32, "AppendMenuW", w.BOOL, w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR)
SetMenu = proto(user32, "SetMenu", w.BOOL, w.HWND, w.HMENU)
GetStockObject = proto(gdi32, "GetStockObject", w.HGDIOBJ, ctypes.c_int)
GetModuleHandleW = proto(kernel32, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)
PostQuitMessage = proto(user32, "PostQuitMessage", None, ctypes.c_int)
IsDialogMessageW = proto(user32, "IsDialogMessageW", w.BOOL, w.HWND, ctypes.POINTER(w.MSG))
comctl32 = ctypes.WinDLL("comctl32")


class INITCOMMONCONTROLSEX(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("dwICC", w.DWORD)]


class TCITEMW(ctypes.Structure):
    _fields_ = [
        ("mask", w.UINT), ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("pszText", w.LPWSTR),
        ("cchTextMax", ctypes.c_int), ("iImage", ctypes.c_int), ("lParam", w.LPARAM),
    ]  # fmt: skip


class NMHDR(ctypes.Structure):
    _fields_ = [("hwndFrom", w.HWND), ("idFrom", ctypes.c_size_t), ("code", ctypes.c_int)]


InitCommonControlsEx = proto(comctl32, "InitCommonControlsEx", w.BOOL, ctypes.POINTER(INITCOMMONCONTROLSEX))

WS_OVERLAPPEDWINDOW, WS_CHILD, WS_VISIBLE, WS_BORDER, WS_TABSTOP, WS_VSCROLL = (
    0x00CF0000,
    0x40000000,
    0x10000000,
    0x00800000,
    0x00010000,
    0x00200000,
)
ES_MULTILINE, ES_AUTOVSCROLL, ES_AUTOHSCROLL, ES_READONLY, ES_WANTRETURN = 0x4, 0x40, 0x80, 0x800, 0x1000
BS_AUTOCHECKBOX, CBS_DROPDOWNLIST, LBS_NOTIFY, SS_NOTIFY = 0x3, 0x3, 0x1, 0x100
WM_DESTROY, WM_COMMAND, WM_KEYDOWN, WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MOUSEWHEEL, WM_CHAR, WM_SETFONT = (
    0x2,
    0x111,
    0x100,
    0x201,
    0x204,
    0x20A,
    0x102,
    0x30,
)
BM_GETCHECK, CB_ADDSTRING, CB_GETCURSEL, LB_ADDSTRING, LB_GETCURSEL = 0xF0, 0x143, 0x147, 0x180, 0x188
BN_CLICKED, CBN_SELCHANGE, LBN_SELCHANGE = 0, 1, 1
WM_NOTIFY, TCM_INSERTITEMW, TCM_GETCURSEL, TCN_SELCHANGE, ICC_TAB_CLASSES = 0x4E, 0x133E, 0x130B, -551, 0x8
MF_STRING, MF_POPUP = 0x0, 0x10

IDS = {
    "name": 1, "notes": 2, "button": 3, "check": 4, "combo": 5, "list": 6, "readonly": 7, "status": 8, "pad": 9,
    "tabs": 10,
}  # fmt: skip
H: dict[str, int] = {}
STATE = {"clicks": 0, "pad": 0}
COLORS, ITEMS, TABS = ["Rouge", "Vert", "Bleu"], ["Alpha", "Bravo", "Charlie"], ["Général", "Avancé"]


def status(text: str) -> None:
    SetWindowTextW(H["status"], text)
    print(json.dumps({"event": text}), flush=True)


@WNDPROC
def main_proc(hwnd, msg, wparam, lparam):
    if msg == WM_COMMAND:
        ident, code = wparam & 0xFFFF, (wparam >> 16) & 0xFFFF
        if ident == IDS["button"] and code == BN_CLICKED:
            STATE["clicks"] += 1
            status(f"clic {STATE['clicks']}")
        elif ident == IDS["check"] and code == BN_CLICKED:
            status("coche " + ("on" if SendMessageW(H["check"], BM_GETCHECK, 0, 0) else "off"))
        elif ident == IDS["combo"] and code == CBN_SELCHANGE:
            status("couleur " + COLORS[SendMessageW(H["combo"], CB_GETCURSEL, 0, 0)])
        elif ident == IDS["list"] and code == LBN_SELCHANGE:
            status("liste " + ITEMS[SendMessageW(H["list"], LB_GETCURSEL, 0, 0)])
        elif ident == 100:
            status("menu action")
        return 0
    if msg == WM_NOTIFY:
        header = NMHDR.from_address(lparam)
        if header.idFrom == IDS["tabs"] and header.code == TCN_SELCHANGE:
            status("onglet " + TABS[SendMessageW(H["tabs"], TCM_GETCURSEL, 0, 0)])
        return 0
    if msg == WM_DESTROY:
        PostQuitMessage(0)
        return 0
    return DefWindowProcW(hwnd, msg, wparam, lparam)


@WNDPROC
def pad_proc(hwnd, msg, wparam, lparam):
    if msg in (WM_LBUTTONDOWN, WM_RBUTTONDOWN):
        x, y = ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value
        STATE["pad"] += 1
        status(f"zone {'gauche' if msg == WM_LBUTTONDOWN else 'droit'} {x},{y}")
        return 0
    if msg == WM_MOUSEWHEEL:
        status(f"molette {ctypes.c_short((wparam >> 16) & 0xFFFF).value}")
        return 0
    if msg == WM_CHAR:
        status(f"zone touche {chr(wparam)!r}")
        return 0
    return DefWindowProcW(hwnd, msg, wparam, lparam)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", default="Labo Win32")
    parser.add_argument("--x", type=int, default=120)
    parser.add_argument("--y", type=int, default=120)
    args = parser.parse_args()
    hinst = GetModuleHandleW(None)
    for cls, proc in (("NexusLabWin32", main_proc), ("NexusLabPad", pad_proc)):
        wc = WNDCLASSW(
            lpfnWndProc=proc, hInstance=hinst, lpszClassName=cls, hbrBackground=6 if cls.endswith("32") else 1
        )
        RegisterClassW(ctypes.byref(wc))

    top = CreateWindowExW(
        0, "NexusLabWin32", args.title, WS_OVERLAPPEDWINDOW, args.x, args.y, 560, 520, None, None, hinst, None
    )
    menu, sub = CreateMenu(), CreatePopupMenu()
    AppendMenuW(sub, MF_STRING, 100, "Action menu")
    AppendMenuW(menu, MF_POPUP, sub, "Fichier")
    SetMenu(top, menu)
    font = GetStockObject(17)  # DEFAULT_GUI_FONT

    def child(key, cls, text, style, x, y, cx, cy):
        hwnd = CreateWindowExW(
            0, cls, text, WS_CHILD | WS_VISIBLE | style, x, y, cx, cy, top, IDS.get(key, 0), hinst, None
        )
        SendMessageW(hwnd, WM_SETFONT, font, 1)
        if key:
            H[key] = hwnd
        return hwnd

    child(None, "STATIC", "Nom", 0, 10, 12, 80, 20)
    child("name", "EDIT", "", WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL, 100, 10, 430, 24)
    child(None, "STATIC", "Notes", 0, 10, 44, 80, 20)
    child(
        "notes",
        "EDIT",
        "",
        WS_BORDER | WS_TABSTOP | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL | ES_WANTRETURN,
        100,
        42,
        430,
        70,
    )
    child("button", "BUTTON", "Valider", WS_TABSTOP, 100, 122, 120, 30)
    child("check", "BUTTON", "Option", WS_TABSTOP | BS_AUTOCHECKBOX, 240, 126, 120, 24)
    child(None, "STATIC", "Couleur", 0, 10, 166, 80, 20)
    child("combo", "COMBOBOX", "", WS_TABSTOP | CBS_DROPDOWNLIST | WS_VSCROLL, 100, 162, 200, 200)
    for c in COLORS:
        buf = ctypes.create_unicode_buffer(c)  # tampon vivant pendant l'appel (copié par le contrôle)
        SendMessageW(H["combo"], CB_ADDSTRING, 0, ctypes.addressof(buf))
    child(None, "STATIC", "Liste", 0, 10, 200, 80, 20)
    child("list", "LISTBOX", "", WS_BORDER | WS_TABSTOP | LBS_NOTIFY, 100, 198, 200, 70)
    for item in ITEMS:
        buf = ctypes.create_unicode_buffer(item)
        SendMessageW(H["list"], LB_ADDSTRING, 0, ctypes.addressof(buf))
    child(None, "STATIC", "Lecture", 0, 10, 282, 80, 20)
    child("readonly", "EDIT", "abc", WS_BORDER | ES_READONLY | ES_AUTOHSCROLL, 100, 280, 200, 24)
    child(None, "STATIC", "Statut", 0, 10, 316, 80, 20)
    child("status", "STATIC", "prêt", 0, 100, 316, 430, 20)
    child("pad", "NexusLabPad", "Zone", WS_BORDER, 100, 344, 430, 110)
    icc = INITCOMMONCONTROLSEX(ctypes.sizeof(INITCOMMONCONTROLSEX), ICC_TAB_CLASSES)
    InitCommonControlsEx(ctypes.byref(icc))
    child("tabs", "SysTabControl32", "", WS_TABSTOP, 320, 162, 210, 106)
    for index, label in enumerate(TABS):
        text = ctypes.create_unicode_buffer(label)
        item = TCITEMW(mask=0x1, pszText=ctypes.cast(text, w.LPWSTR))  # TCIF_TEXT
        SendMessageW(H["tabs"], TCM_INSERTITEMW, index, ctypes.addressof(item))

    ShowWindow(top, 4)  # SW_SHOWNOACTIVATE
    SetWindowPos(top, w.HWND(1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10)  # HWND_BOTTOM, sans activer
    print(json.dumps({"ready": True, "top": top, **{k: v for k, v in H.items()}}), flush=True)

    msg = w.MSG()
    while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_KEYDOWN and msg.wParam == 0x4B and GetKeyState(0x11) < 0:
            status("raccourci ctrl+k")
            continue
        TranslateMessage(ctypes.byref(msg))
        DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    sys.exit(main())
