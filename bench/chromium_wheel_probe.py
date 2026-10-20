"""Sonde : molette par messages dans Chromium recouvert, avec et sans activation simulée (banc du 20 octobre).

Edge et Electron du labo lancés sans activation ; la zone (canevas, écouteur `wheel`) est amenée dans la vue par
UIA ; trois molettes postées à la fenêtre de rendu sous la zone : sans rien, dans `methods.simulated_activation`,
puis encore sans rien. Effet jugé par le titre de la page (« molette … »), premier plan relevé.
Usage : python bench/chromium_wheel_probe.py [electron edge]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lab"))
sys.path.insert(0, str(ROOT / "tests" / "live"))

import apps  # noqa: E402
import drive  # noqa: E402

from nexus_windows import methods, uia  # noqa: E402
from nexus_windows import win32 as w  # noqa: E402


def zone_center(auto: uia.Automation, hwnd: int) -> tuple[int, int]:
    for _ in range(30):
        zone = next((n for n in auto.tree(hwnd).walk() if n.name == "Zone" and n.rect is not None), None)
        if zone is not None:
            auto.scroll_into_view(zone.element)
            time.sleep(0.4)
            zone = next(n for n in auto.tree(hwnd).walk() if n.name == "Zone" and n.rect is not None)
            assert zone.rect is not None
            return zone.rect.center
        time.sleep(0.3)
    raise SystemExit("zone introuvable")


def probe(kind: str) -> list[tuple[str, bool, str]]:
    app = apps.launch(kind, x=150, y=110)
    uia.com_init()
    auto = uia.Automation(auto_set_focus=False)
    events = drive.Events(app)
    rows = []
    try:
        x, y = zone_center(auto, app.hwnd)
        events.reset()
        methods.post_click(app.hwnd, x, y)  # témoin : le point vise bien la zone
        seen = events.collect(1.5, lambda s: any(e.startswith("zone") for e in s))
        print(f"   {kind:8} témoin clic au point ({x},{y}) : {seen[-2:]}")
        child = w.deepest_child_at(app.hwnd, x, y)
        cx, cy = w.screen_to_client(child, x, y)
        wheel = (-120 & 0xFFFF) << 16

        def move_then_wheel(target: int) -> None:
            methods.post(child, w.WM_MOUSEMOVE, 0, w.make_lparam(cx, cy))
            methods.post(target, w.WM_MOUSEWHEEL, wheel, w.make_lparam(x, y))

        variants = {
            "rendu, seul": lambda: methods.post(child, w.WM_MOUSEWHEEL, wheel, w.make_lparam(x, y)),
            "principale, seul": lambda: methods.post(app.hwnd, w.WM_MOUSEWHEEL, wheel, w.make_lparam(x, y)),
            "survol puis rendu": lambda: move_then_wheel(child),
            "survol puis principale": lambda: move_then_wheel(app.hwnd),
        }
        for name, send in variants.items():
            for simulated in (False, True):
                events.reset()
                fg0 = w.foreground()
                if simulated:
                    with methods.simulated_activation(app.hwnd):
                        send()
                        time.sleep(0.15)
                else:
                    send()
                seen = events.collect(1.2, lambda s: any(e.startswith("molette") for e in s))
                ok = any(e.startswith("molette") for e in seen)
                mode = f"{name}{' + activation simulée' if simulated else ''}"
                rows.append((mode, ok, f"{seen[-2:]} premier plan {'intact' if w.foreground() == fg0 else 'CHANGÉ'}"))
                print(f"   {kind:8} {mode:42} {'OK ' if ok else 'NON'} {rows[-1][2]}")
    finally:
        app.close()
    return rows


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    for kind in sys.argv[1:] or ["electron", "edge"]:
        probe(kind)
