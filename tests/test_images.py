"""Règle image commune des produits Nexus (shared/images.md, 24/10) : WebP, 1568 px / 1,15 Mpx, plafonds, pas de double."""

from __future__ import annotations

import io
import random

from PIL import Image

from nexus_windows import snapshot


def _ui(width: int, height: int) -> Image.Image:
    image = Image.new("RGB", (width, height), (243, 243, 243))
    for y in range(0, height, 40):
        image.paste((30, 90, 200), (20, y + 8, min(width, 420), y + 30))
    return image


def test_ui_capture_is_lossless_webp_with_the_same_size():
    image = _ui(1394, 824)
    data = snapshot.image_bytes(image)
    back = Image.open(io.BytesIO(data))
    assert back.format == "WEBP" and back.size == image.size
    assert back.convert("RGB").tobytes() == image.tobytes()  # sans perte
    assert len(data) <= snapshot.LOSSLESS_BUDGET


def test_noisy_capture_falls_back_to_lossy_and_stays_under_the_hard_limit():
    random.seed(3)
    image = Image.frombytes("RGB", (1568, 733), random.randbytes(1568 * 733 * 3))
    data = snapshot.image_bytes(image)
    back = Image.open(io.BytesIO(data))
    assert back.format == "WEBP" and back.size == image.size  # mêmes dimensions : coordonnées inchangées
    assert len(data) <= snapshot.IMAGE_LIMIT


def test_high_detail_follows_the_common_limit():
    frame = snapshot.frame_for(snapshot.w.Rect(0, 0, 3840, 2160), detail="high")
    width, height = frame.image_size
    assert max(width, height) <= 1568 and width * height <= 1_150_000


def test_image_print_tells_an_unchanged_screen_from_a_changed_one():
    a = _ui(800, 600)
    b = a.copy()
    b.paste((200, 30, 30), (300, 300, 340, 330))
    assert snapshot.image_print(a) == snapshot.image_print(a.copy())
    assert snapshot.image_print(a) != snapshot.image_print(b)


def test_print_tells_red_from_green_of_the_same_grey():
    red = Image.new("RGB", (800, 600), (255, 0, 0))
    green = Image.new("RGB", (800, 600), (0, 130, 0))
    assert red.convert("L").tobytes() == green.convert("L").tobytes()  # même gris : l'empreinte grise les confondait
    assert snapshot.image_print(red) != snapshot.image_print(green)
    assert snapshot.image_print(red) != snapshot.image_print(Image.new("RGB", (400, 300), (255, 0, 0)))


def test_evicted_or_forgotten_windows_leave_no_image_print():
    from nexus_windows.session import Session

    session = Session()
    session.sent_images[(1, None)] = b"a"
    session.sent_images[(1, (0.0, 0.0, 9.0, 9.0))] = b"b"
    session.sent_images[(2, None)] = b"c"
    session.forget_view(1)
    assert list(session.sent_images) == [(2, None)]
    session.forget_all()  # même sans vue gardée pour la fenêtre 2
    assert not session.sent_images


def test_old_debug_webp_images_are_pruned(tmp_path, monkeypatch):
    import os
    import time

    from nexus_windows import log

    images = tmp_path / "images"
    images.mkdir()
    monkeypatch.setattr(log, "LOG_DIR", tmp_path)
    monkeypatch.setattr(log, "IMAGE_DIR", images)
    old = time.time() - (log.KEEP_DAYS + 1) * 86400
    for name in ("old.webp", "old.png", "new.webp"):
        (images / name).write_bytes(b"x")
    for name in ("old.webp", "old.png"):
        os.utime(images / name, (old, old))
    log.prune()
    assert sorted(p.name for p in images.iterdir()) == ["new.webp"]


def test_debug_image_failure_never_blocks_the_image(tmp_path, monkeypatch):
    from nexus_windows import log

    blocked = tmp_path / "file"
    blocked.write_text("x")  # un FICHIER à la place du dossier : écriture impossible
    monkeypatch.setattr(log, "DEBUG_IMAGES", True)
    monkeypatch.setattr(log, "IMAGE_DIR", blocked / "images")
    call = log.Call.__new__(log.Call)
    call.id = "t"
    call.image(b"RIFF", "look")  # aucune exception
