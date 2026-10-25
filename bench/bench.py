"""Banc de mesure : quelle technique marche, sans voler le focus, pour chaque famille d'application.

Pour chaque application du labo et chaque tâche (cliquer, cocher, saisir, choisir, menu, zone brute, molette,
raccourci), essaie chaque technique et note : réussite vérifiée (événement émis par l'application ou valeur relue),
premier plan changé ou non, curseur déplacé ou non, durée. Résultats : bench/results/<date>.json et .md.

Usage : python bench/bench.py [win32 winforms wpf tk electron edge]
"""

from __future__ import annotations

import datetime as dt
import json
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lab"))

import apps  # noqa: E402
from redact import redact_home  # noqa: E402

from nexus_windows import cdp, foreground, methods, uia  # noqa: E402
from nexus_windows import win32 as w  # noqa: E402

uia.com_init()
AUTO = uia.Automation(auto_set_focus=False)
AUTO_ASF = uia.Automation(auto_set_focus=True)
TEXT = "Bonjour 123"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- Recherche d'éléments -----------------------------------------------------------------------------------
def tree_of(app: apps.LabApp, min_nodes: int = 25, timeout_s: float = 6.0) -> uia.Node:
    deadline = time.monotonic() + timeout_s
    while True:
        tree = AUTO.tree(app.hwnd)
        if len(list(tree.walk())) >= min_nodes or time.monotonic() > deadline:
            return tree
        time.sleep(0.4)  # Chromium construit son arbre d'accessibilité à la première demande


def find(tree: uia.Node, types: tuple[str, ...], name: str, exact: bool = True) -> uia.Node | None:
    low = name.lower()
    for node in tree.walk():
        if node.control_type in types and (node.name.lower() == low if exact else low in node.name.lower()):
            if node.rect is not None:
                return node
    return None


def zone_point(tree: uia.Node) -> tuple[int, int] | None:
    node = find(tree, ("Image", "Group", "Pane", "Custom", "Text"), "Zone")
    if node and node.rect and node.rect.height > 40:
        return node.rect.center
    statut = find(tree, ("Text",), "Statut")
    if statut and statut.rect:
        return statut.rect.left + 160, statut.rect.bottom + 60
    return None


# --- Vérification -------------------------------------------------------------------------------------------
def wait_event(app: apps.LabApp, predicate, timeout_s: float = 1.5, baseline_title: str | None = None) -> str | None:
    deadline = time.monotonic() + timeout_s
    last_title = baseline_title  # Edge : seul un titre qui change compte (pas le statut de l'essai précédent)
    while time.monotonic() < deadline:
        for event in app.last_events(0.05):
            if predicate(event):
                return event
        if app.kind == "edge":
            title = w.window_text(app.hwnd)
            if title != last_title and title.startswith("Labo Web — "):
                last_title = title
                event = title.removeprefix("Labo Web — ")
                if predicate(event):
                    return event
        time.sleep(0.03)
    return None


def read_value(node: uia.Node) -> str | None:
    try:
        props = AUTO.refresh(node.element)
    except uia.UIAError:
        return None
    value = props.get("value")
    if value is None and node.hwnd:
        value = w.get_text_message(node.hwnd)
    return value


# --- Techniques ---------------------------------------------------------------------------------------------
def cdp_center(page: cdp.Page, selector: str) -> tuple[float, float]:
    rect = page.evaluate(
        f"(() => {{ const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect();"
        " return [r.left + r.width / 2, r.top + r.height / 2]; })()"
    )
    return rect[0], rect[1]


SELECTORS = {
    "button": "#valider",
    "check": "#option",
    "name": "#nom",
    "list": "#o2",
    "zone": "#zone",
    "editor": "#editeur",
}


def run_method(
    app: apps.LabApp, tree: uia.Node, task: str, method: str, page: cdp.Page | None, turn: int = 0
) -> Callable[..., object]:
    """Exécute la technique ; renvoie un prédicat d'événement ou une fonction de relecture.

    `turn` alterne l'option visée (Bravo/Charlie, Vert/Bleu) pour qu'un essai change toujours l'état.
    """
    node = None
    item_name = ("Bravo", "Charlie")[turn % 2]
    color = ("Vert", "Bleu")[turn % 2]
    if task == "button":
        node = find(tree, ("Button",), "Valider")
        expect = lambda e: e.startswith("clic")  # noqa: E731
    elif task == "check":
        node = find(tree, ("CheckBox",), "Option")
        expect = lambda e: e.startswith("coche")  # noqa: E731
    elif task == "text":
        node = find(tree, ("Edit",), "Nom")
        expect = None
    elif task == "combo":
        node = find(tree, ("ComboBox",), "Couleur")
        expect = lambda e: e == f"couleur {color}"  # noqa: E731
    elif task == "list":
        node = find(tree, ("ListItem",), item_name)
        expect = lambda e: e == f"liste {item_name}"  # noqa: E731
    elif task == "menu":
        node = find(tree, ("MenuItem",), "Fichier")
        expect = lambda e: e == "menu action"  # noqa: E731
    elif task == "zone":
        expect = lambda e: e.startswith("zone gauche")  # noqa: E731
    elif task == "wheel":
        expect = lambda e: e.startswith("molette")  # noqa: E731
    elif task == "shortcut":
        expect = lambda e: e == "raccourci ctrl+k"  # noqa: E731
    else:
        raise ValueError(task)

    needs_node = task in ("button", "check", "text", "combo", "list", "menu")
    if needs_node and node is None:
        raise methods.MethodError("élément introuvable dans l'arbre UIA")

    def need() -> uia.Node:
        """Élément de la tâche, que les techniques qui agissent sur un élément exigent."""
        if node is None:
            raise methods.MethodError("élément introuvable dans l'arbre UIA")
        return node

    def need_center() -> tuple[int, int]:
        """Centre de l'élément de la tâche, qui doit publier son rectangle."""
        rect = need().rect
        if rect is None:
            raise methods.MethodError("élément sans rectangle")
        return rect.center

    auto = AUTO_ASF if method.endswith("_asf") else AUTO
    base = method.removesuffix("_asf")

    if base == "uia_invoke":
        auto.invoke(need().element)
    elif base == "uia_toggle":
        auto.toggle(need().element)
    elif base == "uia_legacy":
        auto.legacy_default_action(need().element)
    elif base == "uia_select":
        auto.select(need().element)
    elif base == "uia_value":
        if task == "combo":
            auto.set_value(need().element, color)
        else:
            auto.set_value(need().element, TEXT)
    elif base == "legacy_value":
        auto.legacy_set_value(need().element, TEXT)
    elif base == "uia_combo_item":
        item = find(need(), ("ListItem",), color) or find(tree, ("ListItem",), color)
        if item is None:
            auto.expand(need().element)
            time.sleep(0.3)
            item = find(AUTO.tree(app.hwnd), ("ListItem",), color)
        if item is None:
            raise methods.MethodError(f"option « {color} » introuvable")
        auto.select(item.element)
        try:
            auto.collapse(need().element)
        except uia.UIAError:
            pass
    elif base in ("uia_menu", "uia_menu_invoke", "uia_menu_legacy"):
        {"uia_menu": auto.expand, "uia_menu_invoke": auto.invoke, "uia_menu_legacy": auto.legacy_default_action}[base](
            need().element
        )
        time.sleep(0.3)
        item = None
        for hwnd in [app.hwnd] + [
            h
            for h in w.enum_windows()
            if w.window_pid(h) == w.window_pid(app.hwnd) and h != app.hwnd and w.IsWindowVisible(h)
        ]:
            item = find(AUTO.tree(hwnd), ("MenuItem",), "Action menu")
            if item:
                break
        if item is None:
            raise methods.MethodError("« Action menu » introuvable après dépliage")
        auto.invoke(item.element)
    elif base == "msg_click":
        if task == "zone":
            point = zone_point(tree)
        else:
            point = need_center()
        if point is None:
            raise methods.MethodError("pas de point")
        methods.post_click(app.hwnd, *point)
    elif base == "win32_bn":
        if not need().hwnd:
            raise methods.MethodError("élément sans HWND")
        methods.win32_button_click(need().hwnd)
    elif base == "win32_edit":
        if not need().hwnd:
            raise methods.MethodError("élément sans HWND")
        methods.win32_edit_replace(need().hwnd, TEXT)
    elif base == "win32_combo":
        if not need().hwnd:
            raise methods.MethodError("élément sans HWND")
        methods.win32_combo_select(need().hwnd, color)
    elif base == "win32_list":
        parent = need().parent
        hwnd = parent.hwnd if parent else 0
        if not hwnd:
            raise methods.MethodError("liste sans HWND")
        methods.win32_listbox_select(hwnd, item_name)
    elif base == "win32_menu":
        methods.win32_menu_command(app.hwnd, "Fichier > Action menu")
    elif base == "msg_chars":
        target = need().hwnd or methods.post_click(app.hwnd, *need_center())
        methods.post_chars(target, TEXT)
    elif base == "msg_wheel":
        point = zone_point(tree)
        if point is None:
            raise methods.MethodError("pas de point")
        methods.post_wheel(app.hwnd, *point, -120)
    elif base == "msg_combo":
        methods.post_combo(methods.keyboard_target(app.hwnd), [0x4B], [0x11])
    elif base == "msg_keys_plain":
        target = methods.keyboard_target(app.hwnd)
        w.PostMessageW(target, w.WM_KEYDOWN, 0x11, 1)
        methods.post_key(target, 0x4B)
        w.PostMessageW(target, w.WM_KEYUP, 0x11, 0xC0000001)
    elif base.startswith("cdp_"):
        if page is None:
            raise methods.MethodError("pas de DevTools")
        if base == "cdp_click":
            selector = {
                "button": "#valider",
                "check": "#option",
                "list": "#o2" if turn % 2 == 0 else "#o3",
                "zone": "#zone",
            }[task]
            page.click(*cdp_center(page, selector))
        elif base == "cdp_text":
            page.evaluate("document.querySelector('#nom').focus()")
            page.evaluate("document.querySelector('#nom').select()")
            page.insert_text(TEXT)
        elif base == "cdp_wheel":
            page.wheel(*cdp_center(page, "#zone"), 120)
        elif base == "cdp_key":
            page.key("k", "KeyK", 0x4B, modifiers=2)
    else:
        raise ValueError(base)

    if task == "text":

        def verify() -> str | None:
            deadline = time.monotonic() + 1.5
            value = None
            while time.monotonic() < deadline:
                value = read_value(need())
                if value is not None and value.endswith(TEXT):
                    return value
                time.sleep(0.05)
            return None

        return verify
    assert expect is not None  # seule la tâche « text » n'a pas de prédicat d'événement
    return expect


TASK_METHODS = {
    "button": ["uia_invoke", "uia_invoke_asf", "uia_legacy", "msg_click", "win32_bn", "cdp_click"],
    "check": ["uia_toggle", "uia_toggle_asf", "uia_legacy", "msg_click", "win32_bn", "cdp_click"],
    "text": ["uia_value", "uia_value_asf", "win32_edit", "legacy_value", "msg_chars", "cdp_text"],
    "combo": ["uia_combo_item", "uia_value", "win32_combo"],
    "list": ["uia_select", "uia_select_asf", "uia_legacy", "msg_click", "win32_list", "cdp_click"],
    "menu": ["uia_menu", "uia_menu_invoke", "uia_menu_legacy", "win32_menu"],
    "zone": ["msg_click", "cdp_click"],
    "wheel": ["msg_wheel", "cdp_wheel"],
    "shortcut": ["msg_combo", "msg_keys_plain", "cdp_key"],
}


class ForegroundLost(RuntimeError):
    pass


def restore_foreground(hwnd: int) -> None:
    if hwnd and not foreground.give_back(hwnd):
        raise ForegroundLost(f"Impossible de rendre le premier plan à « {w.window_text(hwnd)[:40]} ».")


def bench_kind(kind: str) -> list[dict]:
    rows: list[dict] = []
    port = free_port() if kind in ("electron", "edge") else 0
    fg_start = w.foreground()
    app = apps.launch(kind, x=150, y=120, cdp_port=port)
    time.sleep(1.0)
    restore_foreground(fg_start)
    page = None
    if port:
        for _ in range(20):
            try:
                page = cdp.Page(port, match="Labo")
                break
            except cdp.CDPError:
                time.sleep(0.25)
    try:
        for task, method_list in TASK_METHODS.items():
            for turn, method in enumerate(method_list):
                if method.startswith("cdp_") and page is None:
                    continue
                tree = tree_of(app)
                app.drain()
                fg_now = w.foreground()
                if fg_now and w.window_pid(fg_now) == w.window_pid(app.hwnd):
                    print(f"  (premier plan repris en différé par {kind})", flush=True)
                    try:
                        restore_foreground(fg_start)
                    except ForegroundLost as error:
                        print(f"  !! {error}", flush=True)
                elif fg_now != fg_start and fg_now:
                    fg_start = fg_now  # Julien a changé de fenêtre lui-même
                fg0, cur0, tick0 = w.foreground(), w.cursor_pos(), w.last_input_tick()
                title0 = w.window_text(app.hwnd)
                t0 = time.perf_counter()
                row: dict[str, object] = {"kind": kind, "task": task, "method": method}
                try:
                    check = run_method(app, tree, task, method, page, turn)
                    row["call_ms"] = round((time.perf_counter() - t0) * 1000)
                    if task == "text":
                        got = check()
                    else:
                        got = wait_event(app, check, baseline_title=title0)
                    row["ok"] = got is not None
                    row["detail"] = got or "aucun effet observé"
                except (methods.MethodError, uia.UIAError, cdp.CDPError, Exception) as error:  # noqa: BLE001
                    row["call_ms"] = round((time.perf_counter() - t0) * 1000)
                    row["ok"] = False
                    row["detail"] = f"{type(error).__name__}: {error}"[:160]
                fg1 = w.foreground()
                watch_end = time.monotonic() + 0.8  # certaines applications s'activent après coup
                while fg1 == fg0 and time.monotonic() < watch_end:
                    time.sleep(0.02)
                    fg1 = w.foreground()
                cur1, tick1 = w.cursor_pos(), w.last_input_tick()
                row["user_active"] = tick1 != tick0
                row["focus_stolen"] = fg1 != fg0 and not row["user_active"]
                row["cursor_moved"] = cur1 != cur0 and not row["user_active"]
                if fg1 != fg0 and fg0 and not row["user_active"]:
                    t_restore = time.perf_counter()
                    try:
                        restore_foreground(fg0)
                    except ForegroundLost as error:
                        print(f"  !! {error}", flush=True)
                        fg_start = w.foreground()
                    row["restore_ms"] = round((time.perf_counter() - t_restore) * 1000)
                stolen = f"FOCUS VOLÉ (rendu {row.get('restore_ms')} ms) " if row["focus_stolen"] else ""
                print(
                    f"  {kind:9} {task:9} {method:16} {'OK ' if row['ok'] else 'NON'} "
                    f"{stolen}{row['call_ms']:5d} ms  {row['detail']}",
                    flush=True,
                )
                rows.append(row)
                time.sleep(0.15)
                # Referme un éventuel menu ou liste resté ouvert.
                for hwnd in w.enum_windows():
                    if (
                        w.window_pid(hwnd) == w.window_pid(app.hwnd)
                        and w.class_name(hwnd) in ("#32768", "ComboLBox")
                        and w.IsWindowVisible(hwnd)
                    ):
                        w.PostMessageW(app.hwnd, 0x001F, 0, 0)  # WM_CANCELMODE
    finally:
        if page:
            page.close()
        app.close()
    return rows


def main() -> None:
    kinds = sys.argv[1:] or list(apps.KINDS)
    all_rows: list[dict] = []
    for kind in kinds:
        print(f"== {kind}", flush=True)
        try:
            all_rows += bench_kind(kind)
        except Exception as error:  # noqa: BLE001
            print(f"  {kind} : ÉCHEC DU BANC {error}", flush=True)
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    (out / f"{stamp}.json").write_text(
        redact_home(json.dumps(all_rows, ensure_ascii=False, indent=1)), encoding="utf-8"
    )
    print(f"Résultats : {out / (stamp + '.json')}")


if __name__ == "__main__":
    main()
