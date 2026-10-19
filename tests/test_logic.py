"""Tests unitaires de la logique pure (sans fenêtre réelle)."""

from __future__ import annotations

import json

import pytest

from nexus_windows import keys, log, methods, snapshot, uia
from nexus_windows import win32 as w


# --- Touches ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("combo", "mods", "key"),
    [
        ("ctrl+s", (0x11,), ord("S")),
        ("Ctrl+Shift+N", (0x11, 0x10), ord("N")),
        ("alt+f4", (0x12,), 0x73),
        ("enter", (), 0x0D),
        ("entrée", (), 0x0D),
        ("échap", (), 0x1B),
        ("win+e", (0x5B,), ord("E")),
        ("ctrl+end", (0x11,), 0x23),
        ("2", (), ord("2")),
    ],
)
def test_parse_combos(combo, mods, key):
    parsed = keys.parse(combo)
    assert parsed.modifiers == mods
    assert parsed.key == key


def test_parse_sequence_and_errors():
    assert [c.key for c in keys.parse_sequence("ctrl+a delete")] == [ord("A"), 0x2E]
    with pytest.raises(keys.KeyParseError):
        keys.parse("hyper+x")
    with pytest.raises(keys.KeyParseError):
        keys.parse("touche-inconnue")


def test_cdp_modifier_bits():
    assert keys.parse("ctrl+shift+k").cdp_modifiers == 2 | 8
    assert keys.parse("alt+x").cdp_modifiers == 1


# --- Coordonnées ----------------------------------------------------------------------------------------------
def test_frame_roundtrip_scaled():
    frame = snapshot.frame_for(w.Rect(100, 50, 3300, 1850), "normal")  # 3200×1800 : limité par 1,15 Mpx
    width, height = frame.image_size
    assert width <= 1568 and height <= 1568 and width * height <= 1_150_000
    assert abs(width / height - 3200 / 1800) < 0.01
    x, y = frame.to_image(1700, 950)
    sx, sy = frame.to_screen(x, y)
    assert abs(sx - 1700) <= 3 and abs(sy - 950) <= 3
    assert frame.contains_image_point(0, 0) and not frame.contains_image_point(width, 10)


def test_frame_limits_per_detail():
    normal = snapshot.frame_for(w.Rect(0, 0, 1920, 1080), "normal")
    high = snapshot.frame_for(w.Rect(0, 0, 3840, 2160), "high")
    assert normal.image_size[0] * normal.image_size[1] <= 1_150_000
    assert max(high.image_size) <= 2000 and high.image_size[0] * high.image_size[1] <= 3_750_000
    wide = snapshot.frame_for(w.Rect(0, 0, 1568, 300), "normal")  # sous les deux limites : taille réelle
    assert wide.scale == 1.0


def test_frame_follows_moved_window():
    frame = snapshot.frame_for(w.Rect(100, 100, 900, 700), "normal", anchor=(100, 100))
    assert frame.to_screen(10, 20) == (110, 120)
    moved = w.Rect(300, 50, 1100, 650)  # fenêtre déplacée depuis l'image : le point suit la fenêtre
    assert frame.to_screen(10, 20, moved) == (310, 70)
    assert frame.to_image(310, 70, moved) == (10, 20)


def test_frame_small_window_not_upscaled():
    frame = snapshot.frame_for(w.Rect(0, 0, 640, 480))
    assert frame.scale == 1.0 and frame.image_size == (640, 480)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("120,45", (120.0, 45.0)), ("(10; 20)", (10.0, 20.0)), ([5, 6], (5.0, 6.0)), ("#12", None), ("Valider", None)],
)
def test_parse_point(spec, expected):
    assert snapshot.parse_point(spec) == expected


# --- Recherche d'éléments -------------------------------------------------------------------------------------
def _node(role: str, name: str, **props) -> uia.Node:
    ids = {v: k for k, v in uia.CONTROL_TYPES.items()}
    base = {"control_type": ids[role], "name": name, "rect": (0, 0, 10, 10)}
    base.update(props)
    return uia.Node(None, 0, base)


def _view(*nodes: uia.Node) -> snapshot.View:
    info = snapshot.WindowInfo(1, "t", "p.exe", "c", w.Rect(0, 0, 100, 100), False, False, False, "Win32")
    elements = [snapshot.Element(i + 1, n, 0, 1) for i, n in enumerate(nodes)]
    return snapshot.View(1, info, [1], elements, snapshot.frame_for(info.rect))


def test_lines_and_flags():
    view = _view(
        _node("CheckBox", "Option", has_toggle=True, toggle=1),
        _node("Button", "Menu", toggle=2),  # pas de motif Toggle : pas d'état affiché
        _node("Edit", "Mot de passe", has_value=True, value="secret", password=True),
        _node("Edit", "Lecture", has_value=True, value="abc", readonly=True),
    )
    out, hidden = snapshot.lines(view)
    assert hidden == 0
    assert "[checked]" in out[0]
    assert "mixed" not in out[1]
    assert "••••" in out[2] and "secret" not in out[2]
    assert "[read-only]" in out[3]


def test_lines_query_and_limit():
    view = _view(*[_node("Button", f"B{i}", has_invoke=True) for i in range(30)])
    out, hidden = snapshot.lines(view, max_elements=10)
    assert len(out) == 10 and hidden == 20
    out, _ = snapshot.lines(view, query="B2")
    assert all("B2" in line for line in out)


# --- Méthodes Win32 -------------------------------------------------------------------------------------------
def test_match_options():
    assert methods.match_option(["Rouge", "Vert", "Bleu"], "vert") == (1, False)
    assert methods.match_option(["Rouge clair", "Bleu"], "rouge") == (0, True)
    with pytest.raises(methods.MethodError):
        methods.match_option(["A"], "Z")


def test_key_lparam_bits():
    down = methods._key_lparam(0x41, up=False)
    up = methods._key_lparam(0x41, up=True)
    assert down & 1 and not down >> 31
    assert up >> 31 & 1 and up >> 30 & 1


# --- Journal --------------------------------------------------------------------------------------------------
def test_log_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(log, "LOG_DIR", tmp_path)
    with log.Call("act", {"text": "x" * 1000}) as call:
        call.step(n=1, do="click", ok=True)
    with pytest.raises(RuntimeError), log.Call("look", {}):
        raise RuntimeError("boum")
    records = log.tail(10)
    assert [r["tool"] for r in records] == ["act", "look"]
    assert records[0]["ok"] and not records[1]["ok"] and "boum" in records[1]["error"]
    assert len(records[0]["args"]["text"]) < 400  # texte long tronqué
    assert log.tail(10, errors_only=True)[0]["tool"] == "look"
    line = next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(line)["steps"][0]["do"] == "click"
