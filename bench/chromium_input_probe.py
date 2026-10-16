"""Sonde : écrire dans une application Chromium/Electron déjà ouverte SANS activation (ni brève, ni réelle).

Electron du labo lancé par le vrai `launch` du serveur (sans vol du premier plan), avec un port DevTools servant
UNIQUEMENT d'observateur : journal des événements de la page (keydown, beforeinput, input, focus ; isTrusted),
valeur des champs, élément actif. Aucune technique essayée ne passe par DevTools.

Pour chaque champ (input, textarea, éditeur riche contenteditable) et chaque technique : texte relu dans la page,
événements `input` de confiance émis, premier plan de Julien intact (relevé avant/après, garde foreground.Watch).

Usage : python bench/chromium_input_probe.py   (résultats : bench/results/<date>-chromium-input.json)
Sans danger pour Julien : seule la fenêtre de test créée ici reçoit des messages ; elle est fermée à la fin.
"""

from __future__ import annotations

import ctypes
import datetime
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lab"))
import apps  # noqa: E402

from nexus_windows import cdp, foreground, launch, methods, policy, uia  # noqa: E402
from nexus_windows import win32 as w  # noqa: E402

WM_SETFOCUS, WM_KILLFOCUS, WM_ACTIVATE, WM_NCACTIVATE, WM_ACTIVATEAPP = 0x0007, 0x0008, 0x0006, 0x0086, 0x001C
WM_IME_CHAR, WM_UNICHAR = 0x0286, 0x0109
WA_INACTIVE, WA_ACTIVE = 0, 1

OBSERVER = r"""
(() => {
  if (window.__probe) return true;
  window.__probe = [];
  const log = (e) => window.__probe.push([e.type, e.isTrusted, (e.target && (e.target.id || e.target.nodeName)) || '',
                                          e.data || e.key || '']);
  for (const t of ['keydown', 'keypress', 'beforeinput', 'input', 'focus', 'blur', 'focusin'])
    document.addEventListener(t, log, true);
  window.addEventListener('focus', log, true); window.addEventListener('blur', log, true);
  return true;
})()
"""


def js(page: cdp.Page, expr: str):
    return page.evaluate(expr)


def field_value(page: cdp.Page, field: str) -> str:
    return js(page, f"(() => {{ const e = document.getElementById('{field}'); return e.value ?? e.innerText; }})()")


def reset(page: cdp.Page, field: str) -> None:
    """Remet le champ à vide et l'élément actif à rien (observateur : pas d'événement de confiance)."""
    js(
        page,
        f"(() => {{ const e = document.getElementById('{field}'); if ('value' in e) e.value = ''; else e.innerHTML = '';"
        " if (document.activeElement) document.activeElement.blur(); window.__probe = []; })()",
    )


def dom_focus(page: cdp.Page, field: str) -> None:
    """Pré-condition « champ focalisé dans le DOM » (pour isoler la question du clavier de celle du focus)."""
    js(page, f"(() => {{ const e = document.getElementById('{field}'); e.focus(); window.__probe = []; }})()")


def state(page: cdp.Page) -> dict:
    return js(
        page,
        "(() => ({ active: document.activeElement && (document.activeElement.id || document.activeElement.nodeName),"
        " hasFocus: document.hasFocus(), visibility: document.visibilityState, events: window.__probe.slice(0, 40) }))()",
    )


def render_widget(hwnd: int) -> int:
    return next((c for c in w.enum_children(hwnd) if w.class_name(c) == "Chrome_RenderWidgetHostHWND"), 0)


def chars(target: int, text: str, msg: int = w.WM_CHAR) -> None:
    for ch in text:
        methods.post(target, msg, ord(ch), 1)


def keydown_char(target: int, text: str) -> None:
    """WM_KEYDOWN + WM_CHAR + WM_KEYUP avec lParam complet (scan code), comme TranslateMessage les produirait."""
    for ch in text:
        vk_scan = ctypes.windll.user32.VkKeyScanW(ord(ch))
        vk = vk_scan & 0xFF
        methods.post(target, w.WM_KEYDOWN, vk, methods._key_lparam(vk, False))
        methods.post(target, w.WM_CHAR, ord(ch), methods._key_lparam(vk, False))
        methods.post(target, w.WM_KEYUP, vk, methods._key_lparam(vk, True))


FAKE = {"nc", "act", "focus"}  # messages d'activation simulée envoyés (variantes de la sonde « first »)


def fake_activate(hwnd: int, on: bool) -> None:
    """Messages d'activation SANS activation réelle : la fenêtre croit être active (ou ne plus l'être)."""
    if on:
        if "app" in FAKE:
            w.send_message(hwnd, WM_ACTIVATEAPP, 1, 0)
        if "nc" in FAKE:
            w.send_message(hwnd, WM_NCACTIVATE, 1, 0)
        if "act" in FAKE:
            w.send_message(hwnd, WM_ACTIVATE, WA_ACTIVE, 0)
        if "focus" in FAKE:
            w.send_message(hwnd, WM_SETFOCUS, 0, 0)
    else:
        if "focus" in FAKE:
            w.send_message(hwnd, WM_KILLFOCUS, 0, 0)
        if "act" in FAKE:
            w.send_message(hwnd, WM_ACTIVATE, WA_INACTIVE, 0)
        if "nc" in FAKE:
            w.send_message(hwnd, WM_NCACTIVATE, 0, 0)
        if "app" in FAKE:
            w.send_message(hwnd, WM_ACTIVATEAPP, 0, 0)


def fake_then(hwnd: int, fn) -> None:
    fake_activate(hwnd, True)
    try:
        time.sleep(0.03)
        fn()
        time.sleep(0.15)
    finally:
        fake_activate(hwnd, False)


def matrix_messages(technique, auto, hwnd, rwh, field, node) -> None:
    """Canaux par messages et par UIA, fenêtre inactive, champ déjà focalisé dans le DOM."""
    technique("WM_CHAR fenêtre principale", field, lambda f: chars(hwnd, "abc"))
    technique("WM_CHAR Chrome_RenderWidgetHostHWND", field, lambda f: chars(rwh, "abc"))
    technique("KEYDOWN+CHAR lParam complet", field, lambda f: keydown_char(hwnd, "abc"))
    technique("WM_IME_CHAR", field, lambda f: chars(hwnd, "abc", WM_IME_CHAR))
    technique("WM_UNICHAR", field, lambda f: chars(hwnd, "abc", WM_UNICHAR))

    def with_keyboard_state(f):
        with methods.fake_modifiers(hwnd, []):
            chars(hwnd, "abc")

    technique("SetKeyboardState attaché + WM_CHAR", field, with_keyboard_state)
    if node is not None:
        technique("UIA ValuePattern.SetValue", field, lambda f: auto.set_value(node.element, "abc"))
        technique("UIA Legacy put_accValue", field, lambda f: auto.legacy_set_value(node.element, "abc"))


def matrix_fake(technique, auto, hwnd, field, node, page) -> None:
    """Activation simulée par messages (WM_NCACTIVATE, WM_ACTIVATE, WM_SETFOCUS), puis les moyens de focus."""
    for attempt in range(3):
        technique(
            f"activation simulée + WM_CHAR #{attempt + 1}", field, lambda f: fake_then(hwnd, lambda: chars(hwnd, "abc"))
        )
    technique("activation simulée + KEYDOWN+CHAR", field, lambda f: fake_then(hwnd, lambda: keydown_char(hwnd, "abc")))
    if node is None:
        return
    focus_ways = {
        "UIA SetFocus": lambda: auto.set_focus(node.element),
        "TextPattern Select": lambda: auto.caret_to_end(node.element),
        "Legacy accSelect(TAKEFOCUS)": lambda: auto.pattern(node.element, "legacy").Select(1),
        "Legacy action par défaut": lambda: auto.legacy_default_action(node.element),
        "clic par messages": lambda: methods.post_click(hwnd, *node.rect.center),
    }
    for name, focus in focus_ways.items():

        def inside(f, focus=focus):
            def run():
                focus()
                time.sleep(0.08)
                chars(hwnd, "abc")

            fake_then(hwnd, run)

        technique(f"activation simulée PUIS {name} + WM_CHAR", field, inside, pre_focus=False)


def matrix_keys(technique, auto, hwnd, page, nodes) -> None:
    """Touches : Entrée dans les champs multilignes, raccourci Ctrl+K (état clavier simulé), activation simulée."""
    VK_RETURN, VK_CONTROL, VK_K = 0x0D, 0x11, 0x4B

    def enter_lines(f):
        def run():
            chars(hwnd, "ab")
            methods.post_key(hwnd, VK_RETURN)
            time.sleep(0.05)
            chars(hwnd, "abc")

        fake_then(hwnd, run)

    for field in ("notes", "editeur"):
        technique("activation simulée + Entrée (touche)", field, enter_lines)

    def ctrl_k(f):
        fake_then(hwnd, lambda: methods.post_combo(hwnd, [VK_K], [VK_CONTROL]))

    js(page, "document.title = 'Labo Web — x'")
    technique("activation simulée + Ctrl+K (état simulé)", "nom", ctrl_k)
    print("   titre après Ctrl+K :", w.window_text(hwnd))


def matrix_first(technique, auto, hwnd, notes, variant) -> None:
    """Toute première activation simulée d'une fenêtre neuve (jamais activée) : plain, wait (pause après les messages
    d'activation), warm (activation simulée puis désactivation une première fois, à vide)."""

    def run(f):
        if variant == "warm":
            fake_activate(hwnd, True)
            time.sleep(0.05)
            fake_activate(hwnd, False)
            time.sleep(0.05)
        fake_activate(hwnd, True)
        try:
            time.sleep(0.3 if variant == "wait" else 0.03)
            if variant != "nofocus":
                auto.pattern(notes.element, "legacy").Select(1)
                time.sleep(0.05)
            chars(hwnd, "abc")
            time.sleep(0.15)
        finally:
            fake_activate(hwnd, False)

    prefocus = variant == "nofocus"  # « nofocus » : champ focalisé dans le DOM par l'observateur, pas d'accSelect
    technique(
        f"première activation simulée ({variant}, {'+'.join(sorted(FAKE)) or 'aucun message'})",
        "notes",
        run,
        pre_focus=prefocus,
    )
    technique("deuxième activation simulée", "notes", run, pre_focus=prefocus)


def matrix_replace(technique, auto, hwnd, page, nodes) -> None:
    """Remplacement et ajout : focus accSelect, TextPattern (tout sélectionner / curseur en fin), puis WM_CHAR."""
    label = {"nom": "Nom", "notes": "Notes", "editeur": "Éditeur"}
    for field in ("nom", "notes", "editeur"):
        node = nodes[label[field]]
        for how in ("remplacer", "ajouter"):

            def run(f, node=node, how=how):
                js(
                    page,
                    f"(() => {{ const e = document.getElementById('{f}'); if ('value' in e) e.value = 'XYZ';"
                    " else e.textContent = 'XYZ'; window.__probe = []; })()",
                )
                time.sleep(0.1)

                def inside():
                    auto.pattern(node.element, "legacy").Select(1)
                    time.sleep(0.05)
                    (auto.select_all_text if how == "remplacer" else auto.caret_to_end)(node.element)
                    time.sleep(0.05)
                    chars(hwnd, "abc")

                fake_then(hwnd, inside)

            technique(f"{how} : accSelect + TextPattern + WM_CHAR", field, run, pre_focus=False)


class ShellEvents:
    """Notifications du shell (RegisterShellHookWindow) : clignotement du bouton de la barre des tâches (HSHELL_FLASH,
    ce que Windows fait quand il REFUSE une mise au premier plan) et activations, pour une fenêtre donnée."""

    HSHELL_WINDOWACTIVATED, HSHELL_RUDEAPPACTIVATED, HSHELL_FLASH = 4, 0x8004, 0x8006

    def __init__(self, target: int) -> None:
        import threading

        self.target = target
        self.events: list[str] = []
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _run(self) -> None:
        from ctypes import wintypes as wt

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.RegisterWindowMessageW.restype = wt.UINT
        shellhook = user32.RegisterWindowMessageW("SHELLHOOK")
        names = {
            self.HSHELL_WINDOWACTIVATED: "activée",
            self.HSHELL_RUDEAPPACTIVATED: "activée",
            self.HSHELL_FLASH: "CLIGNOTE",
        }

        def proc(hwnd, msg, wparam, lparam):
            if msg == shellhook and wparam in names and (lparam or 0) == self.target:
                self.events.append(names[wparam])
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(proc)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wt.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR),
            ]

        wc = WNDCLASSW(lpfnWndProc=self._proc, lpszClassName="NexusProbeShellHook")
        user32.RegisterClassW(ctypes.byref(wc))
        user32.CreateWindowExW.restype = wt.HWND
        user32.CreateWindowExW.argtypes = [
            wt.DWORD,
            wt.LPCWSTR,
            wt.LPCWSTR,
            wt.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wt.HWND,
            wt.HMENU,
            wt.HINSTANCE,
            wt.LPVOID,
        ]
        hwnd = user32.CreateWindowExW(0, "NexusProbeShellHook", "", 0, 0, 0, 0, 0, None, None, None, None)
        user32.RegisterShellHookWindow(hwnd)
        self._ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def matrix_flash(technique, auto, hwnd, notes) -> None:
    """Cinq activations simulées de suite sur une fenêtre neuve : vol du premier plan, clignotement de la barre."""
    shell = ShellEvents(hwnd)

    def run(f):
        shell.events.clear()
        fake_then(hwnd, lambda: (auto.pattern(notes.element, "legacy").Select(1), time.sleep(0.05), chars(hwnd, "abc")))
        time.sleep(0.3)
        print("   shell :", shell.events or "rien")

    for attempt in range(5):
        technique(f"activation simulée #{attempt + 1} (barre des tâches observée)", "notes", run, pre_focus=False)


def matrix_isolate(technique, auto, hwnd, page, notes) -> None:
    """Après la toute première activation (droit de premier plan du lancement consommé, voir « first ») : quel
    ingrédient fait passer les caractères ? Chaque variante trois fois, champ focalisé dans le DOM sauf mention."""
    technique("consommation du droit de lancement", "notes", lambda f: fake_then(hwnd, lambda: None), pre_focus=False)
    focus = lambda: auto.pattern(notes.element, "legacy").Select(1)  # noqa: E731

    def only(messages: set[str], with_focus: bool):
        def run(f):
            saved = set(FAKE)
            FAKE.clear()
            FAKE.update(messages)
            try:
                fake_then(hwnd, lambda: ((focus(), time.sleep(0.05)) if with_focus else None, chars(hwnd, "abc")))
            finally:
                FAKE.clear()
                FAKE.update(saved)

        return run

    variants = [
        ("rien (WM_CHAR seul)", set(), False),
        ("accSelect seul", set(), True),
        ("WM_NCACTIVATE seul", {"nc"}, False),
        ("WM_ACTIVATE seul", {"act"}, False),
        ("WM_SETFOCUS seul", {"focus"}, False),
        ("nc+act+focus", {"nc", "act", "focus"}, False),
        ("nc+act+focus + accSelect", {"nc", "act", "focus"}, True),
    ]
    for name, messages, with_focus in variants:
        for attempt in range(3):
            technique(f"{name} #{attempt + 1}", "notes", only(messages, with_focus), pre_focus=not with_focus)
    print("   après coup :", state(page)["hasFocus"], "(document.hasFocus, fenêtre jamais activée pour de vrai)")


def save(rows: list[dict], mode: str) -> None:
    out = ROOT / "bench" / "results" / f"{datetime.datetime.now():%Y-%m-%d_%H%M%S}-chromium-input-{mode}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("→", out.name)


def main() -> None:
    uia.com_init()
    auto = uia.Automation()
    tmp = Path(tempfile.mkdtemp(prefix="nexus-probe-"))
    # Politique de sonde : l'Electron générique du labo autorisé, ici seulement (jamais le policy.json du poste).
    policy.POLICY_FILE = tmp / "policy.json"
    policy.POLICY_FILE.write_text(json.dumps({"allow_launch": [str(apps.ELECTRON)]}), encoding="utf-8")
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    for arg in sys.argv:
        if arg.startswith("--fake="):  # sous-ensemble des messages d'activation simulée : nc,act,focus,app
            FAKE.clear()
            FAKE.update(arg.split("=", 1)[1].split(","))
    edge = "--edge" in sys.argv
    if edge:
        exe = str(apps.edge_path())
        args = f"--window-size=600,640 --app={(apps.LAB / 'web_app.html').as_uri()}"
    else:
        exe = str(apps.ELECTRON)
        args = f'"{apps.LAB / "electron_main.js"}" --lab-x=180 --lab-y=140'
    user_fg = w.foreground()
    launched = launch.launch(exe, args, devtools=True, auto=auto, wait_s=45.0)
    hwnd = launched.hwnd
    rows: list[dict] = []
    try:
        page = cdp.Page(launched.port)
        js(page, OBSERVER)
        rwh = render_widget(hwnd)
        print(f"fenêtre {hwnd} rendu {rwh} premier plan de Julien {user_fg} (actuel {w.foreground()})")
        tree = auto.tree(hwnd)
        nodes = {n.name: n for n in tree.walk() if n.control_type in ("Edit", "Document", "Group") and n.name}

        def technique(name, field, run, pre_focus=True):
            reset(page, field)
            if pre_focus:
                dom_focus(page, field)
            fg0 = w.foreground()
            started = time.perf_counter()
            error = ""
            with foreground.Watch(hwnd) as guard:
                try:
                    run(field)
                except Exception as exc:  # noqa: BLE001 — sonde : toute erreur est un résultat
                    error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.4)
            took = round((time.perf_counter() - started) * 1000)
            st = state(page)
            value = field_value(page, field)
            trusted_input = any(e[0] == "input" and e[1] for e in st["events"])
            row = {
                "technique": name,
                "champ": field,
                "valeur": value,
                "ok": "abc" in (value or ""),
                "input_de_confiance": trusted_input,
                "keydown": any(e[0] == "keydown" for e in st["events"]),
                "actif": st["active"],
                "hasFocus": st["hasFocus"],
                "premier_plan_intact": w.foreground() == fg0 and not guard.stolen,
                "vols_rendus": guard.stolen,
                "ms": took,
                "erreur": error,
            }
            rows.append(row)
            flag = "OK " if row["ok"] and row["premier_plan_intact"] else "NON"
            print(
                f"{flag} {name:38} {field:8} valeur={value!r:12} input={trusted_input!s:5} keydown={row['keydown']!s:5}"
                f" actif={st['active']} focus={st['hasFocus']} pp={'intact' if row['premier_plan_intact'] else 'VOLÉ'}"
                f" {took} ms {error[:80]}"
            )

        label = {"nom": "Nom", "notes": "Notes", "editeur": "Éditeur"}
        if mode == "first":  # première activation simulée d'une fenêtre neuve (variante en 2e argument)
            matrix_first(technique, auto, hwnd, nodes["Notes"], sys.argv[2] if len(sys.argv) > 2 else "plain")
            return
        if mode == "isolate":
            matrix_isolate(technique, auto, hwnd, page, nodes["Notes"])
            return
        if mode == "flash":
            matrix_flash(technique, auto, hwnd, nodes["Notes"])
            return
        if mode == "replace":
            matrix_replace(technique, auto, hwnd, page, nodes)
            return
        for field in ("nom", "notes", "editeur"):
            node = nodes.get(label[field])
            if mode in ("all", "messages"):
                matrix_messages(technique, auto, hwnd, rwh, field, node)
            if mode in ("all", "fake"):
                matrix_fake(technique, auto, hwnd, field, node, page)
        if mode in ("all", "fake"):
            matrix_keys(technique, auto, hwnd, page, nodes)
    finally:
        save(rows, mode + ("-edge" if edge else ""))
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
        launch.kill_tree(launched.pid)
        foreground.give_back(user_fg) if w.foreground() != user_fg and w.IsWindow(user_fg) else None


if __name__ == "__main__":
    main()
