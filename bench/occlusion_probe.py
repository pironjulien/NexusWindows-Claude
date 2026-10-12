"""Sonde : une fenêtre Chromium recouverte depuis longtemps se redessine-t-elle pour PrintWindow / WGC ?"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lab"))
import apps  # noqa: E402

from nexus_windows import capture, foreground, uia  # noqa: E402
from nexus_windows import win32 as w  # noqa: E402

uia.com_init()
auto = uia.Automation()
kind = sys.argv[1] if len(sys.argv) > 1 else "electron"
wait_s = float(sys.argv[2]) if len(sys.argv) > 2 else 20
user_fg = w.foreground()
app = apps.launch(kind, x=170, y=140)
time.sleep(1.0)
foreground.give_back(user_fg)
out = apps.STATE / "smoke"
try:
    print(f"attente {wait_s:.0f} s fenêtre recouverte…", flush=True)
    time.sleep(wait_s)
    tree = None
    for _ in range(15):
        tree = auto.tree(app.hwnd)
        if len(list(tree.walk())) > 25:
            break
        time.sleep(0.4)
    button = next(n for n in tree.walk() if n.control_type == "Button" and n.name == "Valider")
    for _ in range(3):
        auto.invoke(button.element)
        time.sleep(0.1)
    time.sleep(0.3)
    print("titre fenêtre :", w.window_text(app.hwnd))
    for method, fn in (("printwindow", capture.capture_printwindow), ("wgc", capture.capture_wgc)):
        shot = fn(app.hwnd)
        path = out / f"occlusion-{kind}-{method}.png"
        shot.image.crop((0, 0, shot.size[0], min(shot.size[1], 560))).save(path)
        print(method, f"{shot.ms:.0f} ms", path)
finally:
    app.close()
