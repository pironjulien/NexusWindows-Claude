"""Reproductions de Codex (relecture de 768aebb) : une cible résolue par [N] ou par nom garde son rang parmi ses
frères (liste Win32 à libellés en double, onglets). Gardées en non-régression (0.13.3)."""

import pytest
from test_contract import HWND, FakeAuto, executor, node
from test_fix_A import COMBO, DROP, fake_combo, link, msaa_item, win32_view  # noqa: F401  (fake_combo : fixture)

from nexus_windows import methods, snapshot


@pytest.mark.parametrize("has_runtime_id", [False, True])
def test_duplicate_dropdown_option_keeps_its_rank_after_resolve(monkeypatch, fake_combo, has_runtime_id):  # noqa: F811
    items = [
        msaa_item(name, **({"runtime_id": (42, index)} if has_runtime_id else {}))
        for index, name in enumerate(("Bleu", "Vert", "Bleu"))
    ]
    link(node("List", "", hwnd=DROP, framework="Win32"), *items)
    classes = {DROP: "ComboLBox", COMBO: "ComboBox", HWND: "Main"}
    monkeypatch.setattr(methods.w, "class_name", lambda hwnd: classes.get(hwnd, ""))
    monkeypatch.setattr(methods.w, "enum_children", lambda hwnd: [COMBO] if hwnd == HWND else [])
    monkeypatch.setattr(methods, "combo_list_of", lambda hwnd: DROP if hwnd == COMBO else 0)
    view = win32_view(
        *(snapshot.Element(index + 1, item, 0, DROP) for index, item in enumerate(items)), windows=[HWND, DROP]
    )
    ex = executor(view, FakeAuto(), shown=view)
    ex._windows = {HWND, DROP}
    kind, target = ex.resolve("[3]")
    assert kind == "element"
    chain = ex._win32_item_chain(target)
    assert chain[0][0] == "CB_SETCURSEL"
    assert chain[0][1]() == "read back ✓"
    assert fake_combo["sel"] == 2


@pytest.mark.parametrize("spec", ["[2]", "Deux"])
@pytest.mark.parametrize("has_runtime_id", [False, True])
def test_tab_keeps_its_sibling_rank_after_resolve(spec, has_runtime_id):
    items = [
        node("TabItem", name, framework="Win32", **({"runtime_id": (42, index)} if has_runtime_id else {}))
        for index, name in enumerate(("Un", "Deux"))
    ]
    link(node("Tab", "", hwnd=COMBO, framework="Win32"), *items)
    view = win32_view(*(snapshot.Element(index + 1, item, 0, HWND) for index, item in enumerate(items)))
    ex = executor(view, FakeAuto(), shown=view)
    kind, target = ex.resolve(spec)
    assert kind == "element"
    assert ex._sibling_index(target, "TabItem") == 1
