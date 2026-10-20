"""OCR Windows en secours d'une fenêtre sans arbre UI Automation (relecture d'Antigravity, 20 octobre)."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw, ImageFont

from nexus_windows import actions, ocr, snapshot, uia
from nexus_windows import win32 as w


def _view(*names: str) -> snapshot.View:
    info = snapshot.WindowInfo(
        1, "Tk", "python.exe", "TkTopLevel", w.Rect(100, 200, 500, 500), False, False, False, "Tk"
    )
    ids = {v: k for k, v in uia.CONTROL_TYPES.items()}
    nodes = [uia.Node(None, 0, {"control_type": ids["Text"], "name": n, "rect": (110, 210, 40, 20)}) for n in names]
    elements = [snapshot.Element(i + 1, n, 0, 1) for i, n in enumerate(nodes)]
    frame = snapshot.frame_for(info.rect, "normal", (100, 200), (info.rect.width, info.rect.height))
    return snapshot.View(1, info, [1], elements, frame)


def test_text_read_by_ocr_becomes_elements_at_their_screen_position(monkeypatch):
    lines = [ocr.Line("Valider", (76, 52, 78, 21)), ocr.Line("Tk", (10, 5, 20, 20))]
    monkeypatch.setattr(snapshot.ocr, "read", lambda image: lines)
    view = _view("Tk")  # « Tk » déjà publié par UIA : pas doublé
    assert snapshot.wants_ocr(view)
    assert snapshot.add_ocr(view, Image.new("RGB", (400, 300))) == 1
    text = view.elements[-1]
    assert text.name == "Valider" and text.id == 2 and text.props["ocr"]
    assert text.rect == w.Rect(176, 252, 254, 273)  # origine de l'image (100, 200) ajoutée
    assert snapshot.flags(text) == " (OCR)" and not snapshot.wants_ocr(view)
    assert "read by OCR" in view.notes[-1]


def test_enough_uia_elements_means_no_ocr():
    assert not snapshot.wants_ocr(_view(*(f"E{i}" for i in range(snapshot.OCR_BELOW))))


def test_ocr_unavailable_adds_nothing(monkeypatch):
    monkeypatch.setattr(snapshot.ocr, "read", lambda image: [])
    view = _view()
    assert snapshot.add_ocr(view, Image.new("RGB", (10, 10))) == 0 and view.elements == []


def _executor(view: snapshot.View) -> actions.Executor:
    view.displayed = {e.id: snapshot.identity(e.props) for e in view.elements}
    frame = view.frame
    ex = actions.Executor(
        None,  # type: ignore[arg-type]  (aucune lecture UIA : la cible OCR n'a pas d'élément)
        1,
        get_view=lambda rebuild: _view(),
        get_page=lambda title=None: None,
        allow_foreground=False,
        get_shown=lambda: view,
        get_frame=lambda: frame,
        fresh=False,
    )
    return ex


def test_ocr_text_is_targeted_at_its_point_by_number_and_by_name(monkeypatch):
    monkeypatch.setattr(snapshot.ocr, "read", lambda image: [ocr.Line("Valider", (76, 52, 78, 21))])
    view = _view()
    snapshot.add_ocr(view, Image.new("RGB", (400, 300)))
    ex = _executor(view)
    monkeypatch.setattr(ex, "_hit", lambda v, x, y: None)
    monkeypatch.setattr(actions.w, "frame_rect", lambda h: view.info.rect)
    assert ex.resolve("[1]") == ("point", (215, 262))
    assert ex.resolve("Valider") == ("point", (215, 262))
    assert any("read by OCR" in note for note in ex.notes)


@pytest.mark.skipif(not ocr.available(), reason="OCR Windows indisponible sur ce poste")
def test_windows_ocr_reads_a_rendered_word():
    image = Image.new("RGB", (360, 90), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("segoeui.ttf", 36)
    except OSError:
        font = ImageFont.load_default(36)
    draw.text((20, 20), "Enregistrer", fill="black", font=font)
    lines = ocr.read(image)
    assert any(line.text == "Enregistrer" for line in lines)
    left, top, width, height = next(line.rect for line in lines if line.text == "Enregistrer")
    assert 10 <= left <= 30 and 15 <= top <= 40 and width > 120 and height > 15


def test_tk_button_is_never_clicked_by_ignored_messages(monkeypatch):
    """Banc du 20/10 : un bouton Tk publié en « Button » Win32 recevait BN_CLICKED, que Tk ignore (« ~ » sans effet,
    même avec foreground=true). Arrière-plan : refus franc ; foreground=true : vraie souris."""
    from test_contract import FakeAuto, executor, make_view, node

    button = node("Button", "Valider", framework="Win32", has_invoke=True, has_legacy=True)
    view = make_view(button)
    view.framework = "Tk"
    auto = FakeAuto()
    ex = executor(view, auto)
    monkeypatch.setattr(actions.methods, "win32_button_click", lambda hwnd: pytest.fail("BN_CLICKED envoyé à Tk"))
    with pytest.raises(actions.NeedsForeground, match="impossible in the background for Tk"):
        ex.click("Valider")
    assert auto.calls == []  # ni Invoke, ni action par défaut MSAA, ni message
    clicks = []
    monkeypatch.setattr(actions.realinput, "click", lambda *a, **k: clicks.append(a))
    monkeypatch.setattr(actions.realinput, "borrowed", lambda *a, **k: __import__("contextlib").nullcontext())
    ex.allow_foreground = True
    assert ex.click("Valider").ok and len(clicks) == 1


def _ocr_executor(monkeypatch, window: w.Rect):
    monkeypatch.setattr(snapshot.ocr, "read", lambda image: [ocr.Line("Valider", (76, 52, 78, 21))])
    view = _view()
    snapshot.add_ocr(view, Image.new("RGB", (400, 300)))
    ex = _executor(view)
    monkeypatch.setattr(ex, "_hit", lambda v, x, y: None)
    monkeypatch.setattr(actions.w, "frame_rect", lambda h: window)
    return ex, view


def test_ocr_target_follows_a_moved_window(monkeypatch):
    """Relecture de Codex : fenêtre déplacée de (+60, +40) depuis la lecture → (275, 302), pas (215, 262)."""
    ex, _ = _ocr_executor(monkeypatch, w.Rect(160, 240, 560, 540))
    assert ex.resolve("[1]") == ("point", (275, 302))
    assert ex.resolve("Valider") == ("point", (275, 302))


def test_ocr_target_refused_after_a_resize(monkeypatch):
    ex, _ = _ocr_executor(monkeypatch, w.Rect(100, 200, 600, 500))
    with pytest.raises(actions.ActionError, match="changed size since the text was read"):
        ex.resolve("Valider")


def test_ocr_target_uses_the_view_it_was_read_in_not_an_older_image(monkeypatch):
    """Image plus ancienne (autre position) dans le cadre des points "x,y" : la cible OCR suit la vue de sa lecture."""
    ex, view = _ocr_executor(monkeypatch, w.Rect(100, 200, 500, 500))
    old = snapshot.frame_for(view.info.rect, "normal", (0, 0), (400, 300))
    ex.get_frame = lambda: old
    assert ex.resolve("Valider") == ("point", (215, 262))
