"""Processus d'appoint pour Windows.Graphics.Capture (WGC), lancé à la demande par capture.capture_wgc.

Pourquoi un processus à part : la première capture WGC charge windows-capture (numpy, opencv) et le pilote graphique
(Direct3D) ; mesuré le 19/10/2026 : +770 Mo privés et +25 fils, jamais rendus, dans le serveur de chaque conversation.
Ici, ils vivent le temps d'une série de captures : le processus s'arrête après IDLE_S sans demande, ou dès que son
parent ferme le tube (conversation terminée, serveur rechargé, planté).

Protocole (tubes binaires) : demande = une ligne JSON {"hwnd": int, "timeout": float} ; réponse = une ligne JSON
{"ok": true, "width", "height", "left", "top", "ms"} suivie de width*height*3 octets RGB, ou {"ok": false, "error"}.
"""

from __future__ import annotations

import json
import os
import sys
import threading

IDLE_S = float(os.environ.get("NEXUS_WGC_IDLE_S", "61.8"))


def serve() -> None:
    from .capture import CaptureError, capture_wgc_inline

    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    timer: threading.Timer | None = None

    def arm() -> None:
        nonlocal timer
        if timer:
            timer.cancel()
        timer = threading.Timer(IDLE_S, lambda: os._exit(0))
        timer.daemon = True
        timer.start()

    arm()
    for line in stdin:
        if timer:  # demande reçue : jamais d'arrêt pour inactivité pendant une capture
            timer.cancel()
        if not line.strip():
            arm()
            continue
        try:
            request = json.loads(line)
            shot = capture_wgc_inline(int(request["hwnd"]), float(request.get("timeout", 2.0)))
            image = shot.image.convert("RGB")
            head = {
                "ok": True,
                "width": image.width,
                "height": image.height,
                "left": shot.origin[0],
                "top": shot.origin[1],
                "ms": shot.ms,
            }
            stdout.write(json.dumps(head).encode() + b"\n" + image.tobytes())
        except (CaptureError, ValueError, KeyError, OSError) as error:
            stdout.write(json.dumps({"ok": False, "error": str(error)}).encode() + b"\n")
        stdout.flush()
        arm()


if __name__ == "__main__":
    serve()
