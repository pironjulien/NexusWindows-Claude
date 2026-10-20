"""OCR Windows natif (Windows.Media.Ocr) en secours d'une fenêtre sans arbre UI Automation utile.

Tk, canevas, jeux, page Chromium recouverte qui ne publie plus son contenu : le texte de l'image devient des éléments
[N] « Text » marqués (OCR), avec leur position, ciblables par numéro ou par nom (le clic se fait alors au point).
Mesuré au labo (20 octobre) : 10 à 21 ms par fenêtre ; textes justes à 16/16 (WPF), 20/23 (Edge), 6/7 (Tk).
Lecture seule et locale : rien ne sort du poste. Paquets winrt absents : l'OCR est simplement indisponible.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from PIL import Image

from . import log

MAX_LINES = 120  # lignes lues au plus (une page de texte entière n'a rien à faire dans une liste d'éléments)
_ENGINE = None
_UNAVAILABLE: str | None = None


@dataclass
class Line:
    text: str
    rect: tuple[int, int, int, int]  # gauche, haut, largeur, hauteur, en pixels de l'image lue


def _engine():
    global _ENGINE, _UNAVAILABLE
    if _ENGINE is not None or _UNAVAILABLE is not None:
        return _ENGINE
    try:
        from winrt.windows.media.ocr import OcrEngine

        _ENGINE = OcrEngine.try_create_from_user_profile_languages()
        if _ENGINE is None:
            _UNAVAILABLE = "no OCR language installed in Windows"
    except Exception as error:  # noqa: BLE001  (paquets absents, hors Windows, WinRT indisponible)
        _UNAVAILABLE = f"{type(error).__name__}: {error}"[:160]
        log.event("ocr-unavailable", {"error": _UNAVAILABLE})
    return _ENGINE


def available() -> bool:
    return _engine() is not None


async def _recognize(engine, image: Image.Image):
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    r, g, b, a = image.convert("RGBA").split()
    writer = DataWriter()
    writer.write_bytes(Image.merge("RGBA", (b, g, r, a)).tobytes())
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, image.width, image.height
    )
    return await engine.recognize_async(bitmap)


def read(image: Image.Image) -> list[Line]:
    """Lignes de texte lues dans `image` (vide si l'OCR est indisponible ou échoue : jamais une erreur d'outil)."""
    engine = _engine()
    if engine is None:
        return []
    side = getattr(type(engine), "max_image_dimension", 0) or 10000  # propriété statique du moteur
    if max(image.width, image.height) > side:  # limite du moteur : image réduite, positions remises à l'échelle
        factor = side / max(image.width, image.height)
        small = image.resize((max(1, int(image.width * factor)), max(1, int(image.height * factor))))
    else:
        factor, small = 1.0, image
    try:
        result = asyncio.run(_recognize(engine, small))
    except Exception as error:  # noqa: BLE001
        log.event("ocr-error", {"error": f"{type(error).__name__}: {error}"[:160]})
        return []
    lines: list[Line] = []
    for line in list(result.lines)[:MAX_LINES]:
        words = list(line.words)
        if not words or not line.text.strip():
            continue
        left = min(wd.bounding_rect.x for wd in words)
        top = min(wd.bounding_rect.y for wd in words)
        right = max(wd.bounding_rect.x + wd.bounding_rect.width for wd in words)
        bottom = max(wd.bounding_rect.y + wd.bounding_rect.height for wd in words)
        box = (left / factor, top / factor, (right - left) / factor, (bottom - top) / factor)
        lines.append(Line(line.text.strip(), tuple(round(v) for v in box)))  # type: ignore[arg-type]
    return lines
