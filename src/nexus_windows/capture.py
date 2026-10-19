"""Capture d'une fenêtre sans la mettre devant : PrintWindow ou Windows.Graphics.Capture."""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image, ImageStat

from . import win32 as w


class CaptureError(RuntimeError):
    pass


@dataclass
class Shot:
    """Image en pixels physiques ; origin = position écran du pixel (0, 0)."""

    image: Image.Image
    origin: tuple[int, int]
    method: str
    ms: float

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size


def _dib(width: int, height: int):
    """DC mémoire + DIB 32 bits haut-en-bas ; renvoie (hdc, hbitmap, old, bits)."""
    screen = w.GetDC(None)
    try:
        hdc = w.CreateCompatibleDC(screen)
    finally:
        w.ReleaseDC(None, screen)
    header = w.BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height  # haut-en-bas
    header.biPlanes = 1
    header.biBitCount = 32
    bits = ctypes.c_void_p()
    hbitmap = w.CreateDIBSection(hdc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    if not hbitmap:
        w.DeleteDC(hdc)
        raise CaptureError(f"CreateDIBSection {width}×{height} a échoué.")
    old = w.SelectObject(hdc, hbitmap)
    return hdc, hbitmap, old, bits


def _to_image(bits, width: int, height: int) -> Image.Image:
    w.GdiFlush()
    raw = ctypes.string_at(bits, width * height * 4)
    return Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", width * 4, 1)


def _free(hdc, hbitmap, old) -> None:
    w.SelectObject(hdc, old)
    w.DeleteObject(hbitmap)
    w.DeleteDC(hdc)


def capture_printwindow(hwnd: int) -> Shot:
    """Demande à la fenêtre de se dessiner dans notre bitmap (fonctionne même recouverte, pas réduite).

    Une fenêtre non adaptée au DPI se dessine à sa taille logique (80 % à 125 %, reste noir) : on lui donne un
    bitmap à cette taille puis on l'agrandit à la taille affichée, comme le fait le DWM.
    """
    start = time.perf_counter()
    if w.IsIconic(hwnd):
        raise CaptureError("Fenêtre réduite : rien à dessiner.")
    win = w.window_rect(hwnd)
    frame = w.frame_rect(hwnd)
    if win.empty:
        raise CaptureError("Fenêtre de taille nulle.")
    scale = w.window_dpi_scale(hwnd)
    width, height = win.width, win.height
    if scale != 1.0:
        width, height = max(1, round(win.width / scale)), max(1, round(win.height / scale))
    hdc, hbitmap, old, bits = _dib(width, height)
    try:
        if not w.PrintWindow(hwnd, hdc, w.PW_RENDERFULLCONTENT):
            raise CaptureError("PrintWindow a échoué.")
        image = _to_image(bits, width, height)
    finally:
        _free(hdc, hbitmap, old)
    if scale != 1.0:
        image = image.resize((win.width, win.height), Image.Resampling.LANCZOS)
    crop = frame.intersect(win) or win
    image = image.crop((crop.left - win.left, crop.top - win.top, crop.right - win.left, crop.bottom - win.top))
    method = "printwindow" if scale == 1.0 else f"printwindow ×{scale:.2f} (application non adaptée au DPI)"
    return Shot(image, (crop.left, crop.top), method, (time.perf_counter() - start) * 1000)


# Fin du fil de capture une fois l'arrêt demandé par le rappel : quelques millisecondes mesurées ; au-delà, il est
# bloqué et on l'arrête de force plutôt que de figer le worker COM.
WGC_JOIN_S = 1.0


def _end_wgc(control, done: threading.Event) -> None:
    """Rend le fil de capture sans fuite. Après une image (ou la fermeture), le rappel a déjà demandé l'arrêt : on
    JOINT le fil (wait) ; un second stop() natif à ce moment laisse 9 handles par capture (mesuré). Sans image (délai
    dépassé), la capture tourne encore : stop()."""
    if done.is_set():
        deadline = time.monotonic() + WGC_JOIN_S
        while not control.is_finished() and time.monotonic() < deadline:
            time.sleep(0.005)
        if control.is_finished():
            control.wait()
            return
    control.stop()


_HELPER: subprocess.Popen | None = None
_HELPER_LOCK = threading.Lock()
HELPER_START_S = 16.18  # premier appel : démarrage de Python et import de windows-capture (numpy, opencv)


HELPER_READ_S = 5.0  # marge pour une image déjà prise : quelques Mo à copier dans le tube


def _bounded(helper: subprocess.Popen, read: Callable[[], bytes], timeout_s: float) -> bytes:
    """Lecture sur le tube du processus d'appoint ; figé au-delà de timeout_s : arrêté (jamais le worker COM bloqué)."""
    box: list[bytes] = []
    reader = threading.Thread(target=lambda: box.append(read()), daemon=True)
    reader.start()
    reader.join(timeout_s)
    if reader.is_alive():
        helper.kill()
        return b""
    return box[0] if box else b""


def _drop(helper: subprocess.Popen) -> None:
    """Appoint dans un état inconnu (muet, image tronquée) : arrêté ; le suivant repart d'un processus neuf."""
    global _HELPER
    helper.kill()
    if _HELPER is helper:
        _HELPER = None


def _ask(request: bytes, timeout_s: float) -> tuple[subprocess.Popen, bytes]:
    """Envoie la demande au processus d'appoint (lancé s'il ne tourne pas) ; ligne vide : il est tenu pour mort.
    Le délai de démarrage (import de windows-capture) ne s'ajoute que pour un processus qui vient d'être lancé."""
    global _HELPER
    fresh = _HELPER is None or _HELPER.poll() is not None
    if fresh:
        _HELPER = subprocess.Popen(
            [sys.executable, "-m", "nexus_windows.wgc_helper"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        )  # fmt: skip
    helper = _HELPER
    assert helper is not None and helper.stdin is not None and helper.stdout is not None
    stdout = helper.stdout
    try:
        helper.stdin.write(request)
        helper.stdin.flush()
        head = _bounded(helper, stdout.readline, timeout_s + HELPER_READ_S + (HELPER_START_S if fresh else 0))
    except OSError:
        head = b""
    if not head:
        _drop(helper)
    return helper, head


def capture_wgc(hwnd: int, timeout_s: float = 2.0) -> Shot:
    """Windows.Graphics.Capture, faite dans le processus d'appoint wgc_helper (lancé à la demande, arrêté après une
    minute sans usage) : sa mémoire (pilote graphique, numpy, opencv : ~770 Mo) ne reste pas dans ce serveur."""
    if w.IsIconic(hwnd):
        raise CaptureError("Fenêtre réduite : rien à capturer.")
    start = time.perf_counter()
    request = json.dumps({"hwnd": hwnd, "timeout": timeout_s}).encode() + b"\n"
    with _HELPER_LOCK:
        # Processus arrêté entre deux captures (inactivité, plantage) : relancé une fois.
        helper, head = _ask(request, timeout_s)
        if not head:
            helper, head = _ask(request, timeout_s)
        if not head:
            raise CaptureError("Windows.Graphics.Capture : processus d'appoint arrêté sans réponse.")
        try:
            answer = json.loads(head)
        except ValueError:
            _drop(helper)
            raise CaptureError("Windows.Graphics.Capture : réponse illisible du processus d'appoint.") from None
        if not answer.get("ok"):
            raise CaptureError(answer.get("error") or "Windows.Graphics.Capture : échec.")
        size = answer["width"] * answer["height"] * 3
        stdout = helper.stdout
        assert stdout is not None
        raw = _bounded(helper, lambda: stdout.read(size), HELPER_READ_S)
        if len(raw) != size:
            _drop(helper)
            raise CaptureError("Windows.Graphics.Capture : image tronquée.")
    image = Image.frombytes("RGB", (answer["width"], answer["height"]), raw)
    return Shot(image, (answer["left"], answer["top"]), "wgc", (time.perf_counter() - start) * 1000)


def capture_wgc_inline(hwnd: int, timeout_s: float = 2.0) -> Shot:
    """Windows.Graphics.Capture sans bordure jaune ni curseur (composition DWM, fonctionne recouverte), dans CE
    processus : réservé à wgc_helper (voir capture_wgc).

    Dernier recours seulement (snapshot.capture_view : image PrintWindow vide ou en échec) : dans un régime
    intermittent, windows-capture a perdu ~1 handle noyau par capture (recheck6), sans cause native identifiée."""
    from windows_capture import WindowsCapture  # import paresseux : dépendance lourde (numpy, opencv)

    start = time.perf_counter()
    if w.IsIconic(hwnd):
        raise CaptureError("Fenêtre réduite : rien à capturer.")
    result: dict = {}
    done = threading.Event()
    capture = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd)

    @capture.event
    def on_frame_arrived(frame, control):
        if "image" not in result:
            buf = frame.frame_buffer
            result["image"] = Image.frombuffer("RGB", (frame.width, frame.height), buf.tobytes(), "raw", "BGRX", 0, 1)
        control.stop()
        done.set()

    @capture.event
    def on_closed():
        done.set()

    control = capture.start_free_threaded()
    try:
        if not done.wait(timeout_s):
            raise CaptureError("Windows.Graphics.Capture : aucune image reçue.")
    finally:
        try:
            _end_wgc(control, done)
        finally:
            # La capture native retient les méthodes liées de WindowsCapture, qui la retient : cycle invisible du GC
            # (≈ 1,7 Mo par capture mesurés). Le rompre libère l'image et les ressources natives.
            capture.capture = None
    if "image" not in result:
        raise CaptureError("Windows.Graphics.Capture : capture fermée sans image.")
    frame = w.frame_rect(hwnd)
    image = result["image"]
    if image.size != (frame.width, frame.height):  # WGC capture la zone visible DWM : recaler sur le cadre
        image = image.crop((0, 0, min(image.width, frame.width), min(image.height, frame.height)))
    return Shot(image, (frame.left, frame.top), "wgc", (time.perf_counter() - start) * 1000)


def blank_ratio(image: Image.Image) -> float:
    """Part de l'image qui semble vide (noir ou uniforme) : 1.0 = image inutilisable."""
    small = image.convert("L").resize((64, 64))
    stat = ImageStat.Stat(small)
    if stat.stddev[0] < 1.5:
        return 1.0
    hist = small.histogram()
    return max(hist) / (64 * 64)
