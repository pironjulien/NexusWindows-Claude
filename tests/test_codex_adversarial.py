"""Reproductions indépendantes de Codex (relecture de 2d900af, 0.13.1), gardées en non-régression (0.13.2)."""

import json

import pytest
from PIL import Image
from test_changes import _anchored, _node, _rows, _shown
from test_contract import FakeAuto, as_auto, node

from nexus_windows import actions, ocr, server, snapshot
from nexus_windows import win32 as w


def _ocr_executor(monkeypatch, window):
    # Keep this geometry-only reproduction independent of native OCR initialization at test-module import.
    monkeypatch.setattr(ocr, "read", lambda image: [ocr.Line("Valider", (76, 52, 78, 21))])
    rect = w.Rect(100, 200, 500, 500)
    info = snapshot.WindowInfo(1, "Tk", "python.exe", "TkTopLevel", rect, False, False, False, "Tk")
    view = snapshot.View(1, info, [1], [], snapshot.frame_for(rect, "normal", (100, 200), (400, 300)))
    snapshot.add_ocr(view, Image.new("RGB", (400, 300)))
    view.displayed = {e.id: snapshot.identity(e.props) for e in view.elements}
    empty = snapshot.View(1, info, [1], [], view.frame)
    ex = actions.Executor(
        None,  # type: ignore[arg-type]
        1,
        lambda rebuild: empty,
        lambda title=None: None,
        get_shown=lambda: view,
        get_frame=lambda: view.frame,
        fresh=False,
    )
    monkeypatch.setattr(ex, "_hit", lambda v, x, y: None)
    monkeypatch.setattr(actions.w, "frame_rect", lambda hwnd: window)
    return ex, view


@pytest.mark.parametrize("dx,dy", [(60, 40), (-700, -500), (0, 1000), (0, 0)])
def test_ocr_uses_read_view_anchor_even_with_older_image(monkeypatch, dx, dy):
    ex, view = _ocr_executor(monkeypatch, w.Rect(100 + dx, 200 + dy, 500 + dx, 500 + dy))
    ex.get_frame = lambda: snapshot.frame_for(w.Rect(-300, -400, 100, -100), "normal", (-300, -400), (400, 300))
    for spec in ("[1]", "Valider"):
        assert ex.resolve(spec) == ("point", (215 + dx, 262 + dy))


@pytest.mark.parametrize("dw,dh", [(1, 0), (0, -1), (100, 150)])
def test_ocr_refuses_any_resize(monkeypatch, dw, dh):
    ex, _ = _ocr_executor(monkeypatch, w.Rect(100, 200, 500 + dw, 500 + dh))
    for spec in ("[1]", "Valider"):
        with pytest.raises(actions.ActionError, match="changed size"):
            ex.resolve(spec)


@pytest.mark.parametrize("dx,dy,scroll", [(60, 40, 0), (-700, -500, 0), (70, 80, -30)])
def test_window_translation_and_scroll_are_independent(dx, dy, scroll):
    before = _shown(_anchored(_node("Button", "Fixed", 999), *_rows(0, 100)))
    after = _anchored(_node("Button", "Fixed", 999, rect=(dx, dy, 10, 10)), *_rows(dx, 100 + dy + scroll), at=(dx, dy))
    changes = snapshot.diff_view(before, after)
    assert not changes.changed
    assert changes.shift == ((0, scroll) if scroll else None)
    assert changes.shifted == (list(range(2, 22)) if scroll else [])


def test_nonconsecutive_shifted_ids_are_named_exactly():
    before = _shown(_anchored(*(_node("Button", f"B{i}", i, rect=(20, 40 + 20 * i, 10, 10)) for i in range(6))))
    after = _anchored(
        *(_node("Button", f"B{i}", i, rect=(20, 40 + 20 * i + (40 if i % 2 == 0 else 0), 10, 10)) for i in range(6))
    )
    report, known = server._changes(after, before, before.frame)
    assert "in the image: [1], [3], [5] " in report[0]
    assert known == set(range(1, 7))


@pytest.mark.parametrize("fresh_count", [1, 3, 6])
def test_refreshing_numeric_targets_does_not_corrupt_window_move_baseline(fresh_count):
    """Real resolve -> fresh mutates shown props; window anchors still refer to the old look."""
    old_nodes = [node("Button", f"B{i}", rect=(20, 40 + i * 30, 80, 20), runtime_id=(42, i)) for i in range(6)]
    before = _shown(_anchored(*old_nodes))
    after = _anchored(
        *(node("Button", f"B{i}", rect=(80, 80 + i * 30, 80, 20), runtime_id=(42, i)) for i in range(6)), at=(60, 40)
    )
    ex = actions.Executor(
        as_auto(FakeAuto()), 1, lambda rebuild: before, lambda title=None: None, get_shown=lambda: before, fresh=False
    )
    for i, old in enumerate(old_nodes):
        old.element.live["rect"] = (80, 80 + i * 30, 80, 20)
    original = [e.rect.center if e.rect else None for e in before.elements]
    for i in range(fresh_count):
        assert ex.resolve(f"[{i + 1}]")[0] == "element"
    report, _ = server._changes(after, before, before.frame)
    print(
        json.dumps(
            {
                "fresh_count": fresh_count,
                "initial_centers": original,
                "baseline_after_resolve": [e.rect.center if e.rect else None for e in before.elements],
                "report": report,
            }
        )
    )
    assert report == ["No change since the last list (6 elements, same [N])."]


def test_refreshed_targets_then_window_move_and_real_scroll_reports_the_scroll_only():
    """Cibles revalidées entre le look et changes, fenêtre déplacée de (+60, +40) ET liste défilée de 30 px."""
    old_nodes = [node("Button", f"B{i}", rect=(20, 40 + i * 30, 80, 20), runtime_id=(42, i)) for i in range(6)]
    before = _shown(_anchored(*old_nodes))
    ex = actions.Executor(
        as_auto(FakeAuto()), 1, lambda rebuild: before, lambda title=None: None, get_shown=lambda: before
    )
    for i, old in enumerate(old_nodes):
        old.element.live["rect"] = (80, 80 + i * 30 - 30, 80, 20)
    for i in range(6):
        ex.resolve(f"[{i + 1}]")
    after = _anchored(
        *(node("Button", f"B{i}", rect=(80, 80 + i * 30 - 30, 80, 20), runtime_id=(42, i)) for i in range(6)),
        at=(60, 40),
    )
    report, _ = server._changes(after, before, before.frame)
    dy = round(-30 * before.frame.scale)
    assert report[0].startswith("No change") and f"scrolled by (+0, {dy:+d}) px in the image: [1-6]" in report[0]
