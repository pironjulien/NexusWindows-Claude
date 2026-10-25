"""Sonde des 10 ratés du banc à trois outils (23 octobre) : molette, clic brut et Ctrl+K, fenêtre recouverte.

Hypothèse commune, tirée du code des boîtes à outils : Tk (TkWinGetModifierState) et WPF (Win32MouseDevice,
Win32KeyboardDevice) lisent l'état des boutons de souris et de Ctrl par GetKeyState au traitement du message ; un
message posté sans cet état est jugé incohérent et jeté. L'état est posé pour le SEUL fil de la cible
(`methods.fake_modifiers`, file attachée brièvement, jamais celle de Julien) puis restauré. Molette : Chromium
(RerouteMouseWheel) et Tk renvoient la molette à la fenêtre sous le point (WindowFromPoint) : recouverte, c'est la
fenêtre du dessus, et le message est jeté ; essai témoin seulement. Electron : un Chromium occulté masque sa vue
(fenêtre aura cachée) et ne reçoit plus la souris ; essai frais puis après une activation simulée.
Chaque essai : effet jugé par l'application, premier plan et curseur réel relevés pendant tout l'essai.
Défilement sans molette (relecture) : WM_VSCROLL à la barre native d'une liste Tk, UIA ScrollPattern d'une région web.
Usage : python bench/miss_probes.py [tk wpf win32 electron defil-electron defil-edge] (attend que Julien soit absent depuis 2 min)
"""

from __future__ import annotations

import ctypes
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lab"))
sys.path.insert(0, str(ROOT / "tests" / "live"))
sys.path.insert(0, str(ROOT / "bench"))

import apps  # noqa: E402
import background_probes as probes  # noqa: E402
import cua_vs_nexus as bench  # noqa: E402
from redact import redact_home  # noqa: E402

from nexus_windows import methods, uia  # noqa: E402
from nexus_windows import win32 as w  # noqa: E402

VK_LBUTTON, VK_CONTROL, VK_K = 0x01, 0x11, 0x4B
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_SETFOCUS, WM_KILLFOCUS = 0x0007, 0x0008
WM_VSCROLL, SB_LINEDOWN = 0x0115, 1
WHEEL_DOWN = (-120 & 0xFFFF) << 16
trial = probes.trial


def idle_s() -> float:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO))
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000


def click_with_state(target: int, lp: int) -> None:
    """Bouton gauche « enfoncé » pour le fil de la cible le temps de WM_LBUTTONDOWN, relâché pour WM_LBUTTONUP."""
    with methods.fake_modifiers(target, [VK_LBUTTON], 0.3):
        methods.post(target, w.WM_MOUSEMOVE, 0x0001, lp)
        methods.post(target, WM_LBUTTONDOWN, 0x0001, lp)
    methods.post(target, WM_LBUTTONUP, 0, lp)
    time.sleep(0.2)


def click_plain(target: int, lp: int) -> None:
    methods.post(target, w.WM_MOUSEMOVE, 0, lp)
    methods.post(target, WM_LBUTTONDOWN, 0x0001, lp)
    methods.post(target, WM_LBUTTONUP, 0, lp)


def ctrl_k_focused(focus_to: int, keys_to: int) -> None:
    """Ctrl+K après WM_SETFOCUS envoyé à la seule fenêtre (jamais WM_ACTIVATE), focus rendu par WM_KILLFOCUS posté."""
    probes.send(focus_to, WM_SETFOCUS, 0, 0)
    try:
        methods.post_combo(keys_to, [VK_K], [VK_CONTROL], 0.5)
    finally:
        w.PostMessageW(focus_to, WM_KILLFOCUS, 0, 0)


def probe_tk() -> list[dict]:
    app = apps.launch("tk", x=150, y=110)
    rows = []
    try:
        with bench.Cover(app.hwnd) as cover:
            time.sleep(1.0)
            cover.raise_()
            canvas = app.extra["zone_hwnd"]
            x, y = w.window_rect(canvas).center
            lp = w.make_lparam(*w.screen_to_client(canvas, x, y))
            rows.append(trial(app, "clic posté (témoin)", lambda: click_plain(canvas, lp), "zone gauche"))
            rows.append(
                trial(
                    app, "clic posté, bouton simulé (GetKeyState)", lambda: click_with_state(canvas, lp), "zone gauche"
                )
            )
            rows.append(
                trial(
                    app,
                    "molette postée (témoin)",
                    lambda: methods.post(canvas, w.WM_MOUSEWHEEL, WHEEL_DOWN, w.make_lparam(x, y)),
                    "molette",
                )
            )

            def click_sent(
                count: int,
            ) -> None:  # envoyé (synchrone) : état lu pendant l'envoi, relâché avant le suivant
                probes.send(canvas, w.WM_MOUSEMOVE, 0, lp)
                for i in range(count):
                    with methods.fake_modifiers(canvas, [VK_LBUTTON], 0.0):
                        probes.send(canvas, 0x0203 if i else WM_LBUTTONDOWN, 0x0001, lp)
                    probes.send(canvas, WM_LBUTTONUP, 0, lp)

            rows.append(trial(app, "clic ENVOYÉ, bouton simulé", lambda: click_sent(1), "zone gauche"))
            rows.append(trial(app, "double clic ENVOYÉ, bouton simulé", lambda: click_sent(2), "zone double"))
            bar = app.extra["barre_hwnd"]
            print(f"   barre de la liste : {w.class_name(bar)}")
            rows.append(
                trial(
                    app,
                    "WM_VSCROLL SB_LINEDOWN posté à la barre native",
                    lambda: methods.post(bar, WM_VSCROLL, SB_LINEDOWN, bar),
                    "defil liste bas",
                )
            )
            rows.append(
                trial(
                    app,
                    "WM_VSCROLL SB_LINEDOWN posté au parent de la barre",
                    lambda: methods.post(w.GetParent(bar), WM_VSCROLL, SB_LINEDOWN, bar),
                    "defil liste bas",
                )
            )
            rows.append(
                trial(
                    app,
                    "Ctrl+K posté, Ctrl simulé (témoin)",
                    lambda: methods.post_combo(canvas, [VK_K], [VK_CONTROL], 0.5),
                    "raccourci",
                )
            )
            rows.append(
                trial(
                    app,
                    "Ctrl+K, WM_SETFOCUS au cadre + Ctrl simulé",
                    lambda: ctrl_k_focused(app.hwnd, canvas),
                    "raccourci",
                )
            )
            inner = w.handle(ctypes.windll.user32.GetWindow(app.hwnd, 5))  # GW_CHILD : fenêtre Tk du toplevel
            print(f"   cadre {w.class_name(app.hwnd)}, intérieur {w.class_name(inner)}")
            rows.append(
                trial(
                    app,
                    "Ctrl+K, WM_SETFOCUS + touches au cadre",
                    lambda: ctrl_k_focused(app.hwnd, app.hwnd),
                    "raccourci",
                )
            )
            rows.append(
                trial(
                    app,
                    "Ctrl+K, WM_SETFOCUS + touches à l'intérieur",
                    lambda: ctrl_k_focused(inner, inner),
                    "raccourci",
                )
            )
            rows.append(
                trial(
                    app,
                    "Ctrl+K, WM_SETFOCUS au canevas + Ctrl simulé",
                    lambda: ctrl_k_focused(canvas, canvas),
                    "raccourci",
                )
            )
    finally:
        app.close()
    return rows


def probe_wpf() -> list[dict]:
    app = apps.launch("wpf", x=150, y=110)
    rows = []
    try:
        with bench.Cover(app.hwnd) as cover:
            time.sleep(1.0)
            uia.com_init()
            point = bench.zone_screen(app)
            assert point is not None
            x, y = point
            cover.raise_()
            top = app.hwnd
            lp = w.make_lparam(*w.screen_to_client(top, x, y))
            rows.append(trial(app, "clic posté (témoin)", lambda: click_plain(top, lp), "zone gauche"))
            rows.append(
                trial(app, "clic posté, bouton simulé (GetKeyState)", lambda: click_with_state(top, lp), "zone gauche")
            )

            def wheel() -> None:
                methods.post(top, w.WM_MOUSEMOVE, 0, lp)
                methods.post(top, w.WM_MOUSEWHEEL, WHEEL_DOWN, w.make_lparam(x, y))

            rows.append(trial(app, "survol puis molette postés", wheel, "molette"))
            rows.append(
                trial(app, "Ctrl+K, WM_SETFOCUS seul + Ctrl simulé", lambda: ctrl_k_focused(top, top), "raccourci")
            )
    finally:
        app.close()
    return rows


def probe_win32() -> list[dict]:
    app = apps.launch("win32", x=150, y=110)
    rows = []
    try:
        with bench.Cover(app.hwnd) as cover:
            time.sleep(1.0)
            cover.raise_()
            top = app.hwnd
            target = methods.keyboard_target(top)
            print(f"   cible clavier : {w.class_name(target)} ({'cadre' if target == top else 'enfant'})")
            rows.append(
                trial(
                    app,
                    "Ctrl+K au cadre, Ctrl simulé 0,5 s",
                    lambda: methods.post_combo(top, [VK_K], [VK_CONTROL], 0.5),
                    "raccourci",
                )
            )
            rows.append(
                trial(
                    app,
                    "Ctrl+K au focus, Ctrl simulé 0,5 s",
                    lambda: methods.post_combo(target, [VK_K], [VK_CONTROL], 0.5),
                    "raccourci",
                )
            )

            def k_only() -> None:  # sans WM_KEYDOWN de Ctrl posté : seul l'état simulé dit Ctrl
                with methods.fake_modifiers(top, [VK_CONTROL], 0.5):
                    methods.post(top, w.WM_KEYDOWN, VK_K, 0x00250001)
                    methods.post(top, w.WM_KEYUP, VK_K, 0xC0250001)

            rows.append(trial(app, "K seul posté, Ctrl simulé", k_only, "raccourci"))
    finally:
        app.close()
    return rows


def probe_electron() -> list[dict]:
    app = apps.launch("electron", x=150, y=110)
    rows = []
    try:
        with bench.Cover(app.hwnd) as cover:
            time.sleep(1.5)
            uia.com_init()
            x, y = probes.zone(app)
            cover.raise_()
            time.sleep(1.0)
            rows.append(
                trial(app, "clic posté, jamais activé (frais)", lambda: methods.post_click(app.hwnd, x, y), "zone")
            )

            def in_sim() -> None:
                with methods.simulated_activation(app.hwnd):
                    methods.post_click(app.hwnd, x, y)
                    time.sleep(0.3)

            rows.append(trial(app, "clic posté dans une activation simulée", in_sim, "zone"))
            time.sleep(1.0)  # désactivation traitée : occlusion recalculée
            rows.append(
                trial(app, "clic posté après l'activation simulée", lambda: methods.post_click(app.hwnd, x, y), "zone")
            )
    finally:
        app.close()
    return rows


def probe_web_scroll(kind: str) -> list[dict]:
    """Défilement sans molette : UIA ScrollPattern de la région qui défile (« Défilement »), position relue par la page."""
    app = apps.launch(kind, x=150, y=110)
    rows = []
    try:
        with bench.Cover(app.hwnd) as cover:
            time.sleep(1.5)
            uia.com_init()
            auto = uia.Automation(auto_set_focus=False)
            region = None
            for _ in range(30):
                region = next((n for n in auto.tree(app.hwnd).walk() if n.name == "Défilement"), None)
                if region is not None:
                    break
                time.sleep(0.3)
            assert region is not None and region.rect is not None, "région « Défilement » introuvable"
            region_center = region.rect.center
            cover.raise_()
            print(f"   {kind} : défilement {auto.scroll_percent(region.element)}")
            rows.append(
                trial(
                    app,
                    "UIA ScrollPattern.Scroll petite avance (région)",
                    lambda: auto.scroll(region.element, 2, 4),
                    "defil A bas",
                )
            )
            rows.append(
                trial(
                    app,
                    "UIA SetScrollPercent +10 % (région)",
                    lambda: auto.pattern(region.element, "scroll").SetScrollPercent(-1, 20),
                    "defil A bas",
                )
            )
            line = next((n for n in auto.tree(app.hwnd).walk() if n.name == "A ligne 30"), None)
            if line is not None:
                rows.append(
                    trial(
                        app,
                        "UIA ScrollItem.ScrollIntoView d'une ligne cachée",
                        lambda: auto.scroll_into_view(line.element),
                        "defil A bas",
                    )
                )
            else:
                print(f"   {kind} : ligne « A ligne 30 » absente de l'arbre")
            rows.append(
                trial(
                    app,
                    "molette postée au point de la région (témoin)",
                    lambda: methods.post(app.hwnd, w.WM_MOUSEWHEEL, WHEEL_DOWN, w.make_lparam(*region_center)),
                    "roue A",
                )
            )
    finally:
        app.close()
    return rows


def probe_wpf_keys() -> list[dict]:
    """WPF : la touche est rapportée sans condition (HwndKeyboardInputProvider), mais va à Keyboard.FocusedElement ;
    fenêtre jamais activée : aucun élément focalisé. Essais : focus UIA d'un champ, avec ou sans WM_SETFOCUS d'abord."""
    app = apps.launch("wpf", x=150, y=110)
    rows = []
    try:
        with bench.Cover(app.hwnd) as cover:
            time.sleep(1.0)
            uia.com_init()
            auto = uia.Automation(auto_set_focus=False)
            edit = next(n for n in auto.tree(app.hwnd).walk() if n.control_type == "Edit")
            cover.raise_()
            top = app.hwnd

            def setfocus_then_keys() -> None:
                auto.set_focus(edit.element)
                methods.post_combo(top, [VK_K], [VK_CONTROL], 0.5)

            def wm_then_setfocus() -> None:
                probes.send(top, WM_SETFOCUS, 0, 0)
                auto.set_focus(edit.element)
                methods.post_combo(methods.keyboard_target(top), [VK_K], [VK_CONTROL], 0.5)
                w.PostMessageW(top, WM_KILLFOCUS, 0, 0)

            rows.append(trial(app, "WM_SETFOCUS puis focus UIA du champ, Ctrl simulé", wm_then_setfocus, "raccourci"))
            rows.append(trial(app, "focus UIA du champ puis Ctrl+K, Ctrl simulé", setfocus_then_keys, "raccourci"))
    finally:
        app.close()
    return rows


def list_web_scrollables(kind: str) -> list[dict]:
    app = apps.launch(kind, x=150, y=110)
    found = []
    try:
        time.sleep(2.0)
        uia.com_init()
        auto = uia.Automation(auto_set_focus=False)
        nodes: list[uia.Node] = []
        for _ in range(20):
            nodes = [n for n in auto.tree(app.hwnd).walk() if n.props.get("has_scroll")]
            if len(nodes) > 1:
                break
            time.sleep(0.5)
        for n in nodes:
            found.append({"type": n.control_type, "nom": n.name, "pourcent": auto.scroll_percent(n.element)})
            print(f"   {kind} défilable : {n.control_type} « {n.name[:40]} » {auto.scroll_percent(n.element)}")
    finally:
        app.close()
    return found


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    now = "--maintenant" in sys.argv  # Julien a dit « vas-y » : sans attendre son absence (rien ne touche son clavier)
    wanted = [a for a in sys.argv[1:] if not a.startswith("--")] or [
        "tk",
        "wpf",
        "win32",
        "electron",
        "defil-electron",
        "defil-edge",
    ]
    while not now and idle_s() < 120:
        time.sleep(15)
    print(f"Inactivité {idle_s():.0f} s : début {datetime.now():%H:%M:%S}", flush=True)
    results = {}
    for kind in wanted:
        results[kind] = {
            "tk": probe_tk,
            "wpf": probe_wpf,
            "win32": probe_win32,
            "electron": probe_electron,
            "defil-electron": lambda: probe_web_scroll("electron"),
            "defil-edge": lambda: probe_web_scroll("edge"),
            "wpf-clavier": probe_wpf_keys,
            "liste-defil": lambda: list_web_scrollables("electron"),
        }[kind]()
    out = ROOT / "bench" / "results" / f"{datetime.now():%Y-%m-%d_%H%M}-miss-probes.json"
    out.write_text(redact_home(json.dumps(results, ensure_ascii=False, indent=1)), encoding="utf-8")
    print(f"→ {out.name}")
