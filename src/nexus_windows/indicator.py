"""Indicateur « agent au travail » : le logo π de Julien tourne sur lui-même en bas à droite de l'écran de la fenêtre
pilotée, comme un écran de chargement de jeu, sans halo (le logo seul, avec son ombre).

Affiché dès qu'une action démarre (act, launch, window ; jamais les simples lectures), retiré 1,618 s après la
dernière. Fenêtre en couches toujours au premier plan, sans bordure, transparente aux clics, jamais activée, absente
de la barre des tâches (fenêtre d'outil) et des captures d'écran (WDA_EXCLUDEFROMCAPTURE : ni `look` ni une capture
de l'utilisateur ne la voient). Dessinée par un fil dédié du serveur : rien d'autre ne s'affiche, aucun processus en
plus. NEXUS_WINDOWS_INDICATOR=0 la coupe (tests).
"""

from __future__ import annotations

import ctypes
import math
import os
import threading
import time
from ctypes import wintypes as w
from pathlib import Path

LINGER_S = 1.618  # retrait après la dernière action (nombre d'or)
PERIOD_S = 1.618  # un tour du logo
FRAMES = 48  # images par tour (~30 par seconde)
FADE_S = 0.3
SIZE = 72  # diamètre du logo à 100 % (pixels logiques)
MARGIN = 28  # distance aux bords de la zone de travail
CANVAS = 2.0  # côté de la fenêtre / diamètre du logo : place pour l'ombre
TOOLS = {"act", "launch", "window"}  # outils qui agissent ; windows, look, notifications et logs ne font que lire
LOGO = Path(__file__).with_name("assets") / "logo.png"
CLASS_NAME = "NexusWindowsBusyIndicator"


def enabled() -> bool:
    return os.name == "nt" and os.environ.get("NEXUS_WINDOWS_INDICATOR", "1") != "0"


# --- Rendu (Pillow) --------------------------------------------------------------------------------------------------
def _bezier(x1: float, y1: float, x2: float, y2: float, t: float) -> float:
    """cubic-bezier CSS : y pour l'abscisse t (dichotomie sur le paramètre)."""
    lo, hi = 0.0, 1.0
    for _ in range(40):
        u = (lo + hi) / 2
        x = 3 * x1 * u * (1 - u) ** 2 + 3 * x2 * u * u * (1 - u) + u**3
        lo, hi = (u, hi) if x < t else (lo, u)
    u = (lo + hi) / 2
    return 3 * y1 * u * (1 - u) ** 2 + 3 * y2 * u * u * (1 - u) + u**3


def angle_at(phase: float) -> float:
    """Angle du logo (degrés) à cette fraction du tour : cubic-bezier(0.45, 0, 0.55, 1), comme NexusBrowser."""
    return 360.0 * _bezier(0.45, 0.0, 0.55, 1.0, phase % 1.0)


def render_frames(diameter: int, count: int = FRAMES) -> list[bytes]:
    """Images BGRA prémultipliées (côté = diameter × CANVAS), une par fraction de tour."""
    from PIL import Image, ImageDraw, ImageFilter

    side = round(diameter * CANVAS)
    center = side / 2
    # Sans halo (demande de Julien du 22/10 : le fond bleu-violet faisait « moche ») : la pièce et son ombre seules.
    # Pièce : logo rond, liseré blanc 0,18 sur 2 px, ombre portée (0 8px 22px noir 0,55).
    scale = diameter / 72
    supersample = 4
    big = diameter * supersample
    mask = Image.new("L", (big, big))
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    mask = mask.resize((diameter, diameter), Image.Resampling.LANCZOS)
    logo = Image.open(LOGO).convert("RGBA").resize((diameter, diameter), Image.Resampling.LANCZOS)
    logo.putalpha(Image.composite(logo.getchannel("A"), Image.new("L", logo.size), mask))
    ring_w = max(1, round(2 * scale))
    ring_side = diameter + 2 * ring_w
    ring_big = Image.new("L", (ring_side * supersample, ring_side * supersample))
    ImageDraw.Draw(ring_big).ellipse((0, 0, ring_big.width - 1, ring_big.height - 1), fill=round(0.18 * 255))
    ring = ring_big.resize((ring_side, ring_side), Image.Resampling.LANCZOS)
    coin = Image.new("RGBA", ring.size, (255, 255, 255, 0))
    coin.putalpha(ring)
    coin.alpha_composite(logo, (ring_w, ring_w))
    blur = 22 * scale / 2
    shadow_pad = math.ceil(blur * 3)
    shadow_side = diameter + 2 * shadow_pad
    shadow = Image.new("L", (shadow_side, shadow_side))
    shadow.paste(mask.point(_ramp(0.55)), (shadow_pad, shadow_pad))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    shadow_rgba = Image.new("RGBA", shadow.size, (0, 0, 0, 0))
    shadow_rgba.putalpha(shadow)

    frames: list[bytes] = []
    for i in range(count):
        phase = i / count
        canvas = Image.new("RGBA", (side, side))
        cos = math.cos(math.radians(angle_at(phase)))
        width = max(1, round(coin.width * abs(cos)))
        s_w = max(1, round(shadow_rgba.width * abs(cos)))
        if abs(cos) > 0.02:
            s = shadow_rgba.resize((s_w, shadow_rgba.height), Image.Resampling.BILINEAR)
            canvas.alpha_composite(s, (round(center - s_w / 2), round(center - s.height / 2 + 8 * scale)))
            face = coin.resize((width, coin.height), Image.Resampling.LANCZOS)
            if cos < 0:  # revers : le logo vu de dos, en miroir (backface-visibility: visible)
                face = face.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            canvas.alpha_composite(face, (round(center - width / 2), round(center - coin.height / 2)))
        frames.append(_premultiplied_bgra(canvas))
    return frames


def _ramp(factor: float) -> list[int]:
    """Table d'opacité multipliée par factor (Image.point)."""
    return [round(v * factor) for v in range(256)]


def _premultiplied_bgra(image) -> bytes:
    from PIL import Image

    r, g, b, a = image.convert("RGBa").split()
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


# --- Fenêtre Win32 ---------------------------------------------------------------------------------------------------
if os.name == "nt":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        _shcore = ctypes.WinDLL("shcore")
    except OSError:
        _shcore = None

LRESULT = ctypes.c_ssize_t
# Hors Windows (conteneur Linux des annuaires), le module se charge sans rien dessiner : enabled() est faux.
WNDPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR), ("hIconSm", w.HICON),
    ]  # fmt: skip


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD), ("biBitCount", w.WORD),
        ("biCompression", w.DWORD), ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG),
        ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD),
    ]  # fmt: skip


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]  # fmt: skip


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


WS_POPUP = 0x80000000
WS_EX = 0x80000 | 0x20 | 0x8 | 0x80 | 0x08000000  # LAYERED | TRANSPARENT | TOPMOST | TOOLWINDOW | NOACTIVATE
WDA_EXCLUDEFROMCAPTURE = 0x11
SWP = 0x0010 | 0x0200 | 0x0040  # NOACTIVATE | NOOWNERZORDER | SHOWWINDOW
HWND_TOPMOST = -1
SW_HIDE = 0
ULW_ALPHA = 2
PM_REMOVE = 1


def _sig(dll, name: str, restype, *argtypes):
    func = getattr(dll, name)
    func.restype = restype
    func.argtypes = list(argtypes)
    return func


if os.name == "nt":
    _DefWindowProcW = _sig(_user32, "DefWindowProcW", LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    _RegisterClassExW = _sig(_user32, "RegisterClassExW", w.ATOM, ctypes.POINTER(_WNDCLASSEXW))
    _CreateWindowExW = _sig(
        _user32, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID,
    )  # fmt: skip
    _SetWindowDisplayAffinity = _sig(_user32, "SetWindowDisplayAffinity", w.BOOL, w.HWND, w.DWORD)
    _SetWindowPos = _sig(_user32, "SetWindowPos", w.BOOL, w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, w.UINT)  # fmt: skip
    _ShowWindow = _sig(_user32, "ShowWindow", w.BOOL, w.HWND, ctypes.c_int)
    _UpdateLayeredWindow = _sig(
        _user32, "UpdateLayeredWindow", w.BOOL, w.HWND, w.HDC, ctypes.POINTER(w.POINT), ctypes.POINTER(w.SIZE),
        w.HDC, ctypes.POINTER(w.POINT), w.COLORREF, ctypes.POINTER(_BLENDFUNCTION), w.DWORD,
    )  # fmt: skip
    _PeekMessageW = _sig(_user32, "PeekMessageW", w.BOOL, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
    _TranslateMessage = _sig(_user32, "TranslateMessage", w.BOOL, ctypes.POINTER(w.MSG))
    _DispatchMessageW = _sig(_user32, "DispatchMessageW", LRESULT, ctypes.POINTER(w.MSG))
    _GetDC = _sig(_user32, "GetDC", w.HDC, w.HWND)
    _ReleaseDC = _sig(_user32, "ReleaseDC", ctypes.c_int, w.HWND, w.HDC)
    _IsWindow = _sig(_user32, "IsWindow", w.BOOL, w.HWND)
    _GetForegroundWindow = _sig(_user32, "GetForegroundWindow", w.HWND)
    _MonitorFromWindow = _sig(_user32, "MonitorFromWindow", w.HMONITOR, w.HWND, w.DWORD)
    _GetMonitorInfoW = _sig(_user32, "GetMonitorInfoW", w.BOOL, w.HMONITOR, ctypes.POINTER(_MONITORINFO))
    _CreateCompatibleDC = _sig(_gdi32, "CreateCompatibleDC", w.HDC, w.HDC)
    _CreateDIBSection = _sig(_gdi32, "CreateDIBSection", w.HBITMAP, w.HDC, ctypes.POINTER(_BITMAPINFOHEADER),
                             w.UINT, ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD)  # fmt: skip
    _SelectObject = _sig(_gdi32, "SelectObject", w.HGDIOBJ, w.HDC, w.HGDIOBJ)
    _DeleteObject = _sig(_gdi32, "DeleteObject", w.BOOL, w.HGDIOBJ)
    _GetModuleHandleW = _sig(_kernel32, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)


def monitor_of(hwnd: int) -> tuple[tuple[int, int, int, int], float]:
    """Zone de travail (gauche, haut, droite, bas) et échelle (DPI / 96) de l'écran de cette fenêtre (à défaut,
    celui de la fenêtre au premier plan)."""
    if not hwnd or not _IsWindow(hwnd):
        hwnd = _GetForegroundWindow() or 0
    monitor = _MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST (principal sans fenêtre)
    info = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    _GetMonitorInfoW(monitor, ctypes.byref(info))
    scale = 1.0
    if _shcore is not None:
        dpi_x, dpi_y = w.UINT(), w.UINT()
        if _shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0 and dpi_x.value:
            scale = dpi_x.value / 96
    work = info.rcWork
    return (work.left, work.top, work.right, work.bottom), scale


class Indicator:
    """État partagé (compteur d'actions, dernière fin, fenêtre visée) et fil de dessin, lancé au premier besoin."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._active = 0
        self._last_end = 0.0
        self._target = 0
        self._thread: threading.Thread | None = None
        self._frames: dict[int, list[bytes]] = {}
        self.hwnd = 0
        self.shown_at: tuple[int, int, int] | None = None  # (x, y, côté) pendant l'affichage, pour les tests

    # --- API des outils ---------------------------------------------------------------------------------------
    def begin(self) -> None:
        if not enabled():
            return
        with self._lock:
            self._active += 1
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="nexus-busy-indicator", daemon=True)
                self._thread.start()
        self._wake.set()

    def end(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._last_end = time.monotonic()

    def target(self, hwnd: int) -> None:
        self._target = int(hwnd or 0)

    def busy(self) -> bool:
        with self._lock:
            return self._active > 0 or time.monotonic() - self._last_end < LINGER_S

    # --- Fil de dessin ----------------------------------------------------------------------------------------
    def _run(self) -> None:
        try:
            self._loop()
        except Exception as exc:  # un indicateur ne doit jamais gêner une action : il se tait, noté au journal
            self.shown_at = None
            from . import log

            log.event("indicator", {"error": f"{type(exc).__name__}: {exc}"})

    def _create(self) -> None:
        self._proc = WNDPROC(lambda h, m, wp, lp: _DefWindowProcW(h, m, wp, lp))
        instance = _GetModuleHandleW(None)
        cls = _WNDCLASSEXW(cbSize=ctypes.sizeof(_WNDCLASSEXW), lpfnWndProc=self._proc, hInstance=instance,
                           lpszClassName=CLASS_NAME)  # fmt: skip
        _RegisterClassExW(ctypes.byref(cls))  # déjà enregistrée après une relance du fil : sans effet
        self.hwnd = _CreateWindowExW(WS_EX, CLASS_NAME, "Nexus Windows", WS_POPUP, 0, 0, 1, 1, None, None,
                                     instance, None) or 0  # fmt: skip
        if not self.hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW")
        _SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE)

    def frames_for(self, diameter: int) -> list[bytes]:
        if diameter not in self._frames:
            self._frames[diameter] = render_frames(diameter)
        return self._frames[diameter]

    def _loop(self) -> None:
        self._create()
        screen_dc = _GetDC(None)
        mem_dc = _CreateCompatibleDC(screen_dc)
        bitmap, bits, side = None, ctypes.c_void_p(), 0
        msg = w.MSG()
        while True:
            self._wake.wait()
            start = time.monotonic()
            visible_since, hide_from = start, None
            while True:
                while _PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    _TranslateMessage(ctypes.byref(msg))
                    _DispatchMessageW(ctypes.byref(msg))
                now = time.monotonic()
                if self.busy():
                    if hide_from is not None:  # nouvelle action pendant le retrait : réapparition
                        visible_since, hide_from = now - FADE_S * self._fade(now, hide_from, out=True), None
                elif hide_from is None:
                    hide_from = now
                if hide_from is not None and now - hide_from >= FADE_S:
                    break
                (left, top, right, bottom), scale = monitor_of(self._target)
                diameter = max(24, round(SIZE * scale))
                frames = self.frames_for(diameter)
                new_side = round(diameter * CANVAS)
                if new_side != side:
                    if bitmap:
                        _DeleteObject(bitmap)
                    header = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=new_side,
                                               biHeight=-new_side, biPlanes=1, biBitCount=32)  # fmt: skip
                    bitmap = _CreateDIBSection(screen_dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
                    _SelectObject(mem_dc, bitmap)
                    side = new_side
                frame = frames[int((now - start) / PERIOD_S * FRAMES) % FRAMES]
                grow = self._fade(now, visible_since if hide_from is None else hide_from, out=hide_from is not None)
                pixels = frame if grow >= 1 else _scaled(frame, side, 0.6 + 0.4 * _overshoot(grow))
                ctypes.memmove(bits, pixels, len(pixels))
                margin = round(MARGIN * scale)
                x = right - margin - diameter // 2 - side // 2
                y = bottom - margin - diameter // 2 - side // 2
                blend = _BLENDFUNCTION(0, 0, max(0, min(255, round(255 * grow))), 1)  # AC_SRC_OVER, AC_SRC_ALPHA
                _UpdateLayeredWindow(self.hwnd, screen_dc, ctypes.byref(w.POINT(x, y)),
                                     ctypes.byref(w.SIZE(side, side)), mem_dc, ctypes.byref(w.POINT(0, 0)), 0,
                                     ctypes.byref(blend), ULW_ALPHA)  # fmt: skip
                # Toujours devant, même au-dessus des fenêtres « toujours visibles » ouvertes après lui, sans activation.
                _SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP | 0x0001 | 0x0002)  # NOSIZE | NOMOVE
                self.shown_at = (x, y, side)
                time.sleep(max(0.0, PERIOD_S / FRAMES - (time.monotonic() - now)))
            _ShowWindow(self.hwnd, SW_HIDE)
            self.shown_at = None
            with self._lock:
                if not (self._active > 0 or time.monotonic() - self._last_end < LINGER_S):
                    self._wake.clear()

    @staticmethod
    def _fade(now: float, since: float, *, out: bool) -> float:
        k = min(1.0, max(0.0, (now - since) / FADE_S))
        return 1.0 - k if out else k


def _overshoot(k: float) -> float:
    """cubic-bezier(0.34, 1.56, 0.64, 1) : léger rebond à l'apparition, comme NexusBrowser."""
    return _bezier(0.34, 1.56, 0.64, 1.0, k)


def _scaled(frame: bytes, side: int, factor: float) -> bytes:
    from PIL import Image

    image = Image.frombytes("RGBA", (side, side), frame)
    size = max(1, round(side * factor))
    out = Image.new("RGBA", (side, side))
    out.paste(image.resize((size, size), Image.Resampling.BILINEAR), ((side - size) // 2, (side - size) // 2))
    return out.tobytes()


INDICATOR = Indicator()


def begin(tool: str) -> bool:
    """Début d'un appel d'outil : l'indicateur s'affiche si l'outil agit. Vrai si l'appel compte (end à appeler)."""
    if tool not in TOOLS:
        return False
    INDICATOR.begin()
    return True


def end() -> None:
    INDICATOR.end()


def target(hwnd: int) -> None:
    INDICATOR.target(hwnd)
