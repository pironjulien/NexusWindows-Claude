"""Sonde : saisie dans un éditeur riche (contenteditable) Chromium/Electron sans DevTools, en arrière-plan."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lab"))
import apps  # noqa: E402
import comtypes  # noqa: E402

from nexus_windows import foreground, methods, uia  # noqa: E402
from nexus_windows import win32 as w  # noqa: E402

uia.com_init()
auto = uia.Automation()


def value_of(element) -> str | None:
    """Valeur UIA actuelle (ValuePattern), None si l'élément n'en a pas."""
    try:
        return auto.pattern(element, "value").CurrentValue
    except uia.UIAError, comtypes.COMError:
        return None


app = apps.launch("electron", x=160, y=130)
user_fg = w.foreground()
time.sleep(1.0)
foreground.give_back(user_fg)
try:
    tree = None
    for _ in range(15):
        tree = auto.tree(app.hwnd)
        if len(list(tree.walk())) > 25:
            break
        time.sleep(0.4)
    editor = next(
        (n for n in tree.walk() if n.name == "Éditeur" and n.control_type in ("Edit", "Document", "Group")), None
    )
    print(
        "éditeur :",
        editor and (editor.control_type, {k: v for k, v in editor.props.items() if k.startswith("has_") and v}),
    )
    for label, action in (
        ("uia_value", lambda: auto.set_value(editor.element, "Salut UIA")),
        ("legacy_value", lambda: auto.legacy_set_value(editor.element, "Salut legacy")),
    ):
        app.drain()
        with foreground.Watch(app.hwnd) as guard:
            try:
                action()
                result = app.last_events(1.0)
            except Exception as error:  # noqa: BLE001
                result = f"{type(error).__name__}: {error}"
        print(f"{label:14} -> {result} valeur={value_of(editor.element)!r} {guard.note()}")
    # Focus DOM par un Invoke/Select UIA puis WM_CHAR au widget de rendu Chromium.
    app.drain()
    with foreground.Watch(app.hwnd) as guard:
        try:
            auto.set_focus(editor.element)
        except Exception as error:  # noqa: BLE001
            print("focus legacy :", error)
        time.sleep(0.2)
        target = methods.keyboard_target(app.hwnd)
        print("cible clavier :", target, w.class_name(target))
        methods.post_chars(target, " + messages")
        result = app.last_events(1.0)
    print(f"{'focus+chars':14} -> {result} valeur={value_of(editor.element)!r} {guard.note()}")
finally:
    app.close()
