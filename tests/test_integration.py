"""Branchements entre les lots de corrections (frappe par tranches, page DevTools occupée, application Store)."""

from __future__ import annotations

import contextlib

import pytest
from test_contract import FakeAuto, executor, make_view, node

from nexus_windows import actions, cdp, launch, methods


def test_click_still_works_when_devtools_page_is_busy():
    """Une boîte JavaScript ouverte rend la page DevTools muette : le clic UIA (sur son bouton OK) doit passer."""
    button = node("Button", "OK", has_invoke=True)
    auto = FakeAuto()
    ex = executor(make_view(button), auto)

    def busy():
        raise cdp.PageBusy("la page ne répond pas (boîte JavaScript ouverte ?)")

    ex.get_page = busy
    outcome = ex.click("OK")
    assert outcome.ok and ("invoke", button.element) in auto.calls
    assert any("DevTools ignoré" in n for n in ex.notes)


def test_keystrokes_go_through_slices(monkeypatch):
    """Lot T : progress() (début du texte relu) remplace consumed(préfixe) ; champ vide au départ : rien à effacer."""
    field = node("Edit", "Notes", value="")
    ex = executor(make_view(field), FakeAuto())
    seen = {}

    def fake_slices(
        hwnd, windows, text, prepare, read, prefix, enter_as_key=False, slice_s=methods.SLICE_S, clear=False
    ):
        seen.update(text=text, enter=enter_as_key, read=read, prefix=prefix, clear=clear)
        return methods.Typed(len(text), len(text), 3, 1200)

    monkeypatch.setattr(actions.methods, "type_in_slices", fake_slices)
    detail = ex._keystrokes(ex.resolve("Notes")[1], "ligne1\nligne2", replace=True)
    assert seen["text"] == "ligne1\nligne2" and "3 activations brèves" in detail
    assert seen["prefix"](seen["read"]()) == 0 and seen["clear"] is False  # champ vide : rien de tapé ni à effacer
    field.element.live["value"] = "ligne1\n"
    assert seen["prefix"](seen["read"]()) == len("ligne1\n")


def test_clearing_uses_one_activation_and_delete(monkeypatch):
    field = node("Edit", "Notes", value="abc")
    auto = FakeAuto()
    auto.set_focus = lambda element: None
    auto.select_all_text = lambda element: None
    ex = executor(make_view(field), auto)
    keys: list[int] = []

    @contextlib.contextmanager
    def fake_activation(hwnd, windows=None):
        class Token:
            def check(self, consequence):
                pass

        field.element.live["value"] = ""
        yield Token()

    monkeypatch.setattr(actions.foreground, "brief_activation", fake_activation)
    monkeypatch.setattr(actions.methods, "keyboard_target", lambda hwnd, windows=None: 1)
    monkeypatch.setattr(actions.methods, "post_key", lambda target, vk: keys.append(vk))
    monkeypatch.setattr(actions.methods, "type_in_slices", lambda *a, **k: pytest.fail("pas de tranches pour vider"))
    detail = ex._keystrokes(ex.resolve("Notes")[1], "", replace=True)
    assert keys == [0x2E] and detail.startswith("Suppr")


def test_launch_never_activates_to_show_a_store_app():
    """Mesuré : l'activation brève n'affichait l'application Store dans aucun des 17 lancements et coûtait environ 1 s
    de premier plan à Julien ; launch ne doit jamais activer quoi que ce soit (la garde ne fait que RENDRE)."""
    import inspect

    assert "brief_activation" not in inspect.getsource(launch.launch)
    assert not hasattr(launch, "show_store_app")


@pytest.mark.parametrize(
    ("container_class", "msaa"),
    [
        ("WindowsForms10.Window.8.app.0.141b42a_r6_ad1", False),  # MenuStrip WinForms : PerformClick géré
        ("WindowsForms10.LISTBOX.app.0.141b42a_r6_ad1", True),  # liste native sous WinForms : proxy oleacc
        ("SysTabControl32", True),
        ("ListBox", True),
    ],
)
def test_msaa_rule_only_for_native_proxies(monkeypatch, container_class, msaa):
    container = node("Pane", "porteur", hwnd=77)
    item = node("MenuItem", "Fichier", has_invoke=True, legacy_action="Ouvrir", framework="")
    item.parent = container
    monkeypatch.setattr(actions.methods, "win_class", lambda hwnd: container_class.lower())
    element = make_view(item).elements[0]
    assert actions.Executor._msaa_item(element, "WinForms") is msaa
