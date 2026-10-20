"""Micro-essais d'arrière-plan demandés par les relectures de 0.12.0 (Codex, Antigravity), 20 octobre.

Chaque essai : fenêtre du labo recouverte par une fenêtre opaque non activée (`cua_vs_nexus.Cover`, comme une fenêtre
de Julien), effet jugé par l'application, premier plan et curseur réel relevés pendant tout l'essai.
- wpf : Ctrl+K avec activation simulée étendue (WM_ACTIVATE + WM_SETFOCUS envoyés, état clavier simulé, touches postées).
- web : molette Chromium par WM_POINTERWHEEL posté, et par WM_VSCROLL à Chrome_RenderWidgetHostHWND.
- tk : clic, molette et Ctrl+K postés à la fenêtre enfant du canevas (pas au cadre).
- pointeur : InjectSyntheticPointerInput (toucher) au centre de la zone recouverte : qui le reçoit ?
Usage : python bench/background_probes.py [wpf web tk pointeur]
"""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lab"))
sys.path.insert(0, str(ROOT / "tests" / "live"))
sys.path.insert(0, str(ROOT / "bench"))

import apps  # noqa: E402
import cua_vs_nexus as bench  # noqa: E402
import drive  # noqa: E402

from nexus_windows import methods, uia  # noqa: E402
from nexus_windows import win32 as w  # noqa: E402

WM_ACTIVATE, WM_SETFOCUS, WM_KILLFOCUS, WM_NCACTIVATE = 0x0006, 0x0007, 0x0008, 0x0086
WM_VSCROLL, WM_POINTERWHEEL, SB_LINEDOWN = 0x0115, 0x024E, 1
VK_CONTROL, VK_K = 0x11, 0x4B
user32 = ctypes.windll.user32
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageTimeoutW.argtypes += [wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]


def send(hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> None:
    result = ctypes.c_size_t()
    user32.SendMessageTimeoutW(hwnd, msg, wparam, lparam, 0x0002, 1000, ctypes.byref(result))  # SMTO_ABORTIFHUNG


def trial(app: apps.LabApp, label: str, act: Callable[[], object], wanted: str) -> dict:
    events = drive.Events(app)
    events.reset()
    with bench.Foreground() as fg:
        try:
            act()
            error = ""
        except Exception as exc:  # noqa: BLE001  (essai : l'erreur est le résultat)
            error = f"{type(exc).__name__}: {exc}"[:120]
        seen = events.collect(1.5, lambda s: any(e.startswith(wanted) for e in s))
    ok = any(e.startswith(wanted) for e in seen)
    row = {"essai": label, "effet": ok, "premier plan": fg.summary(), "curseur": fg.cursor_moved, "erreur": error}
    flag = "OK " if ok else "NON"
    print(
        f"   {app.kind:8} {label:58} {flag} {fg.summary()[:50]} curseur={'BOUGÉ' if fg.cursor_moved else 'intact'} {error}"
    )
    return row


def probe_wpf() -> list[dict]:
    app = apps.launch("wpf", x=150, y=110)
    rows = []
    try:
        with bench.Cover(app.hwnd) as cover:
            time.sleep(0.8)
            cover.raise_()
            top = app.hwnd

            def extended() -> None:
                send(top, WM_NCACTIVATE, 1, 0)
                send(top, WM_ACTIVATE, 1, 0)  # WA_ACTIVE
                send(top, WM_SETFOCUS, 0, 0)
                methods.post_combo(top, [VK_K], [VK_CONTROL], hold_s=0.5)
                time.sleep(0.3)
                w.PostMessageW(top, WM_KILLFOCUS, 0, 0)
                w.PostMessageW(top, WM_ACTIVATE, 0, 0)
                w.PostMessageW(top, WM_NCACTIVATE, 0, 0)

            rows.append(
                trial(
                    app,
                    "Ctrl+K, messages seuls",
                    lambda: methods.post_combo(top, [VK_K], [VK_CONTROL], 0.5),
                    "raccourci",
                )
            )
            rows.append(trial(app, "Ctrl+K, activation simulée + WM_SETFOCUS + état simulé", extended, "raccourci"))
    finally:
        app.close()
    return rows


def zone(app: apps.LabApp) -> tuple[int, int]:
    for _ in range(25):  # Chromium publie son arbre d'accessibilité à la première demande
        point = bench.zone_screen(app)
        if point is not None:
            return point
        time.sleep(0.3)
    raise SystemExit("zone introuvable")


def probe_web(kind: str) -> list[dict]:
    app = apps.launch(kind, x=150, y=110)
    rows = []
    try:
        with bench.Cover(app.hwnd) as cover:
            time.sleep(1.0)
            uia.com_init()
            x, y = zone(app)
            cover.raise_()
            child = w.deepest_child_at(app.hwnd, x, y)
            wheel = (-120 & 0xFFFF) << 16
            lp = w.make_lparam(x, y)
            rows.append(trial(app, "témoin : clic posté au point", lambda: methods.post_click(app.hwnd, x, y), "zone"))
            rows.append(
                trial(
                    app,
                    "WM_POINTERWHEEL posté (rendu)",
                    lambda: methods.post(child, WM_POINTERWHEEL, wheel | 1, lp),
                    "molette",
                )
            )
            rows.append(
                trial(
                    app,
                    "WM_POINTERWHEEL posté (activation simulée)",
                    lambda: _in_sim(app.hwnd, lambda: methods.post(child, WM_POINTERWHEEL, wheel | 1, lp)),
                    "molette",
                )
            )
            rows.append(
                trial(
                    app,
                    "WM_VSCROLL SB_LINEDOWN au rendu",
                    lambda: methods.post(child, WM_VSCROLL, SB_LINEDOWN, 0),
                    "molette",
                )
            )
    finally:
        app.close()
    return rows


def _in_sim(top: int, fn: Callable[[], None]) -> None:
    with methods.simulated_activation(top):
        fn()
        time.sleep(0.2)


def probe_tk() -> list[dict]:
    app = apps.launch("tk", x=150, y=110)
    rows = []
    try:
        with bench.Cover(app.hwnd) as cover:
            time.sleep(1.0)
            cover.raise_()
            canvas = app.extra["zone_hwnd"]
            x, y = w.window_rect(canvas).center
            cx, cy = w.screen_to_client(canvas, x, y)
            lp = w.make_lparam(cx, cy)

            def click() -> None:
                methods.post(canvas, w.WM_MOUSEMOVE, 0, lp)
                methods.post(canvas, 0x0201, 0x0001, lp)  # WM_LBUTTONDOWN, MK_LBUTTON
                methods.post(canvas, 0x0202, 0, lp)

            rows.append(trial(app, "clic posté au canevas (fenêtre enfant)", click, "zone gauche"))
            rows.append(
                trial(
                    app,
                    "molette postée au canevas",
                    lambda: methods.post(canvas, w.WM_MOUSEWHEEL, (-120 & 0xFFFF) << 16, w.make_lparam(x, y)),
                    "molette",
                )
            )
            rows.append(
                trial(
                    app,
                    "Ctrl+K posté au canevas (état simulé)",
                    lambda: methods.post_combo(canvas, [VK_K], [VK_CONTROL], 0.5),
                    "raccourci",
                )
            )
    finally:
        app.close()
    return rows


# --- Pointeur synthétique (toucher) -------------------------------------------------------------------------
class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", ctypes.c_uint32), ("pointerId", ctypes.c_uint32), ("frameId", ctypes.c_uint32),
        ("pointerFlags", ctypes.c_uint32), ("sourceDevice", wintypes.HANDLE), ("hwndTarget", wintypes.HWND),
        ("ptPixelLocation", wintypes.POINT), ("ptHimetricLocation", wintypes.POINT),
        ("ptPixelLocationRaw", wintypes.POINT), ("ptHimetricLocationRaw", wintypes.POINT),
        ("dwTime", wintypes.DWORD), ("historyCount", ctypes.c_uint32), ("InputData", ctypes.c_int32),
        ("dwKeyStates", wintypes.DWORD), ("PerformanceCount", ctypes.c_uint64), ("ButtonChangeType", ctypes.c_int),
    ]  # fmt: skip


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO), ("touchFlags", ctypes.c_uint32), ("touchMask", ctypes.c_uint32),
        ("rcContact", wintypes.RECT), ("rcContactRaw", wintypes.RECT), ("orientation", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
    ]  # fmt: skip


class POINTER_TYPE_INFO(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("touchInfo", POINTER_TOUCH_INFO)]


def inject_tap(x: int, y: int) -> None:
    user32.CreateSyntheticPointerDevice.restype = wintypes.HANDLE
    device = user32.CreateSyntheticPointerDevice(2, 1, 1)  # PT_TOUCH, 1 contact, POINTER_FEEDBACK_DEFAULT
    if not device:
        raise OSError(ctypes.get_last_error() or "CreateSyntheticPointerDevice")
    try:
        for flags in (0x00010000 | 0x00000002 | 0x00000004, 0x00040000):  # DOWN|INRANGE|INCONTACT, puis UP
            info = POINTER_TYPE_INFO()
            info.type = 2
            touch = info.touchInfo
            touch.pointerInfo.pointerType = 2
            touch.pointerInfo.pointerId = 0
            touch.pointerInfo.pointerFlags = flags
            touch.pointerInfo.ptPixelLocation = wintypes.POINT(x, y)
            touch.touchMask = 0x1  # TOUCH_MASK_CONTACTAREA
            touch.rcContact = wintypes.RECT(x - 2, y - 2, x + 2, y + 2)
            if not user32.InjectSyntheticPointerInput(device, ctypes.byref(info), 1):
                raise OSError(f"InjectSyntheticPointerInput : erreur {ctypes.GetLastError()}")
            time.sleep(0.05)
    finally:
        user32.DestroySyntheticPointerDevice(device)


def probe_pointer() -> list[dict]:
    rows = []
    for kind in ("win32", "wpf"):
        app = apps.launch(kind, x=150, y=110)
        try:
            with bench.Cover(app.hwnd) as cover:
                time.sleep(0.8)
                uia.com_init()
                x, y = zone(app)
                cover.raise_()
                hit = w.root(w.handle(user32.WindowFromPoint(wintypes.POINT(x, y))))
                print(
                    f"   {kind:8} sous le point : {w.class_name(hit)} ({'couverture' if hit == cover.hwnd else 'autre'})"
                )
                rows.append(
                    trial(app, "toucher synthétique au centre de la zone recouverte", lambda: inject_tap(x, y), "zone")
                )
        finally:
            app.close()
    return rows


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    wanted = sys.argv[1:] or ["wpf", "web", "tk", "pointeur"]
    if "wpf" in wanted:
        probe_wpf()
    if "web" in wanted:
        for k in ("electron", "edge"):
            probe_web(k)
    if "tk" in wanted:
        probe_tk()
    if "pointeur" in wanted:
        probe_pointer()
