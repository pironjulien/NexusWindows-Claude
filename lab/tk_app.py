"""Application de test Tk : boîte à outils sans UI Automation (comme un jeu ou un canevas). Affichée sans activation."""

import argparse
import ctypes
import json
import tkinter as tk


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--x", type=int, default=200)
    parser.add_argument("--y", type=int, default=200)
    args = parser.parse_args()
    root = tk.Tk()
    root.title("Labo Tk")
    root.geometry(f"560x420+{args.x}+{args.y}")
    root.withdraw()
    status = tk.StringVar(value="prêt")
    clicks = {"n": 0}

    def set_status(text: str) -> None:
        status.set(text)
        root.title(f"Labo Tk — {text}")
        print(json.dumps({"event": text}), flush=True)

    tk.Label(root, text="Nom").grid(row=0, column=0, sticky="w", padx=8, pady=4)
    name = tk.Entry(root, width=50)
    name.grid(row=0, column=1, sticky="w")
    name.bind("<KeyRelease>", lambda e: set_status("nom " + name.get()))
    tk.Button(
        root,
        text="Valider",
        width=12,
        command=lambda: (clicks.__setitem__("n", clicks["n"] + 1), set_status(f"clic {clicks['n']}")),
    ).grid(row=1, column=1, sticky="w", pady=4)
    option = tk.IntVar()
    tk.Checkbutton(
        root, text="Option", variable=option, command=lambda: set_status("coche " + ("on" if option.get() else "off"))
    ).grid(row=2, column=1, sticky="w")
    tk.Label(root, text="Statut").grid(row=3, column=0, sticky="w", padx=8)
    tk.Label(root, textvariable=status).grid(row=3, column=1, sticky="w")
    zone = tk.Canvas(root, width=430, height=110, bg="white", highlightthickness=1)
    zone.grid(row=4, column=1, sticky="w", pady=8)
    zone.bind("<Button-1>", lambda e: set_status(f"zone gauche {e.x},{e.y}"))
    zone.bind("<Button-3>", lambda e: set_status(f"zone droit {e.x},{e.y}"))
    zone.bind("<MouseWheel>", lambda e: set_status(f"molette {e.delta}"))
    root.bind_all("<Control-k>", lambda e: set_status("raccourci ctrl+k"))

    def show() -> None:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        ctypes.windll.user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        ctypes.windll.user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)
        print(json.dumps({"ready": True, "top": hwnd}), flush=True)

    root.after(50, show)
    root.mainloop()


if __name__ == "__main__":
    main()
