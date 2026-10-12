"""Contrôle rapide du labo : chaque application se lance sans voler le focus, se capture et expose son arbre UIA."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apps  # noqa: E402

from nexus_windows import capture, uia  # noqa: E402
from nexus_windows import win32 as w  # noqa: E402

OUT = apps.STATE / "smoke"
OUT.mkdir(parents=True, exist_ok=True)
uia.com_init()
auto = uia.Automation()
kinds = sys.argv[1:] or list(apps.KINDS)
for kind in kinds:
    fg_before = w.foreground()
    t0 = time.perf_counter()
    try:
        app = apps.launch(kind)
    except Exception as error:  # noqa: BLE001
        print(f"{kind:9} LANCEMENT ÉCHOUÉ : {error}")
        continue
    launch_ms = (time.perf_counter() - t0) * 1000
    time.sleep(1.0)
    fg_after = w.foreground()
    line = [
        f"{kind:9} hwnd={app.hwnd} classe={w.class_name(app.hwnd)!r} lancement={launch_ms:.0f}ms",
        f"focus={'VOLÉ par ' + w.window_text(fg_after)[:30] if fg_after != fg_before else 'intact'}",
    ]
    for method in ("printwindow", "wgc"):
        try:
            shot = capture.capture_printwindow(app.hwnd) if method == "printwindow" else capture.capture_wgc(app.hwnd)
            shot.image.save(OUT / f"{kind}-{method}.png")
            line.append(
                f"{method}={shot.ms:.0f}ms {shot.size[0]}x{shot.size[1]} vide={capture.blank_ratio(shot.image):.2f}"
            )
        except Exception as error:  # noqa: BLE001
            line.append(f"{method}=ÉCHEC {error}")
    try:
        t1 = time.perf_counter()
        tree = auto.tree(app.hwnd)
        nodes = list(tree.walk())
        line.append(f"uia={(time.perf_counter() - t1) * 1000:.0f}ms {len(nodes)} nœuds")
        named = [f"{n.control_type}:{n.name}" for n in nodes if n.name][:12]
        line.append("ex: " + ", ".join(named))
    except Exception as error:  # noqa: BLE001
        line.append(f"uia=ÉCHEC {error}")
    print(" | ".join(line), flush=True)
    app.close()
