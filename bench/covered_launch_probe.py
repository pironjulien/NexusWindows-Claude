"""Sonde : une application Chromium/Electron FERMÉE, lancée par launch derrière une fenêtre agrandie au premier plan,
est-elle dessinée et lisible (image non unie, contenu web dans l'arbre) ?

Constat du 2026-10-24 (Logi Options+, nom du menu Démarrer « Logi Options+ », AppID « Logi.OptionsPlus.App ») : lancée
par son raccourci, sans les commutateurs anti-occlusion, la fenêtre recouverte n'est jamais peinte (image violette
unie, 0 élément, même 60 s plus tard). Usage : covered_launch_probe.py ["Logi Options+"] [attente_s]
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lab"))
import apps  # noqa: E402

from nexus_windows import capture, foreground, launch, snapshot, uia  # noqa: E402
from nexus_windows import win32 as w  # noqa: E402

uia.com_init()
auto = uia.Automation()
name = sys.argv[1] if len(sys.argv) > 1 else "Logi Options+"
wait_s = float(sys.argv[2]) if len(sys.argv) > 2 else 10
# « front » : la fenêtre agrandie déjà au premier plan sert de couverture (Electron le 24/10 : Claude) ; sinon une
# fenêtre Win32 du labo, agrandie et activée.
real_front = len(sys.argv) > 3 and sys.argv[3] == "front"
apps.require_julien_away()
user_fg = w.foreground()
cover = None if real_front else apps.launch("win32")
launched = None
try:
    if cover is not None:
        w.ShowWindow(cover.hwnd, w.SW_MAXIMIZE)
        foreground.activate(cover.hwnd)
        time.sleep(0.5)
    print("premier plan :", w.window_text(w.foreground()), flush=True)
    launched = launch.launch(name, auto=auto, wait_s=20)
    print("via :", launched.via, "| hwnd :", launched.hwnd, "| notes :", launched.notes, flush=True)
    assert launched.hwnd, "aucune fenêtre"
    print("premier plan après launch :", w.window_text(w.foreground()), flush=True)
    time.sleep(wait_s)
    for method, fn in (("printwindow", capture.capture_printwindow), ("wgc", capture.capture_wgc)):
        shot = fn(launched.hwnd)
        print(method, f"unie à {capture.blank_ratio(shot.image):.0%}", flush=True)
        shot.image.save(apps.STATE / "smoke" / f"covered-launch-{method}.png")
    view = snapshot.build_view(auto, launched.hwnd)
    print("cadre :", w.frame_rect(launched.hwnd), "| recouvrante :", w.frame_rect(cover.hwnd if cover else user_fg))
    print("éléments :", len(view.elements), "| contenu web absent :", view.web_missing, "| notes :", view.notes)
finally:
    if launched is not None and launched.pid:
        launch.kill_tree(launched.pid)
    if cover is not None:
        cover.close()
    foreground.give_back(user_fg)
