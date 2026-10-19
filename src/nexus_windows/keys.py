"""Combinaisons de touches : « ctrl+s », « alt+f4 », « shift+tab », « enter », « ctrl+shift+n »."""

from __future__ import annotations

from dataclasses import dataclass

MODIFIERS = {
    "ctrl": 0x11,
    "control": 0x11,
    "ctl": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "maj": 0x10,
    "win": 0x5B,
    "cmd": 0x5B,
}

NAMED = {
    "enter": 0x0D,
    "entrée": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "space": 0x20,
    "espace": 0x20,
    "backspace": 0x08,
    "retour": 0x08,
    "escape": 0x1B,
    "esc": 0x1B,
    "échap": 0x1B,
    "delete": 0x2E,
    "del": 0x2E,
    "suppr": 0x2E,
    "insert": 0x2D,
    "ins": 0x2D,
    "home": 0x24,
    "début": 0x24,
    "end": 0x23,
    "fin": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "up": 0x26,
    "haut": 0x26,
    "down": 0x28,
    "bas": 0x28,
    "left": 0x25,
    "gauche": 0x25,
    "right": 0x27,
    "droite": 0x27,
    "apps": 0x5D,
    "menu": 0x5D,
    "printscreen": 0x2C,
    "pause": 0x13,
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "plus": 0xBB,
    "minus": 0xBD,
    "comma": 0xBC,
    "period": 0xBE,
}
NAMED.update({f"f{i}": 0x6F + i for i in range(1, 25)})

EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5D}

# Noms DevTools (KeyboardEvent.key / code) pour les touches nommées courantes.
CDP_NAMES = {
    0x0D: ("Enter", "Enter"),
    0x09: ("Tab", "Tab"),
    0x20: (" ", "Space"),
    0x08: ("Backspace", "Backspace"),
    0x1B: ("Escape", "Escape"),
    0x2E: ("Delete", "Delete"),
    0x24: ("Home", "Home"),
    0x23: ("End", "End"),
    0x21: ("PageUp", "PageUp"),
    0x22: ("PageDown", "PageDown"),
    0x26: ("ArrowUp", "ArrowUp"),
    0x28: ("ArrowDown", "ArrowDown"),
    0x25: ("ArrowLeft", "ArrowLeft"),
    0x27: ("ArrowRight", "ArrowRight"),
}


class KeyParseError(ValueError):
    pass


@dataclass(frozen=True)
class Combo:
    modifiers: tuple[int, ...]
    key: int
    char: str | None  # caractère produit sans modificateur (pour WM_CHAR), si imprimable

    @property
    def cdp_modifiers(self) -> int:
        bits = 0
        for vk in self.modifiers:
            bits |= {0x12: 1, 0x11: 2, 0x5B: 4, 0x10: 8}[vk]
        return bits


def _vk_for_char(ch: str) -> int:
    from . import win32 as w

    code = w.VkKeyScanW(ch)
    if code == -1:
        raise KeyParseError(f"Key not found on the keyboard layout: {ch!r}")
    return code & 0xFF


def parse(combo: str) -> Combo:
    """« ctrl+shift+s » → Combo. Une lettre ou un chiffre seuls sont acceptés (« a », « 2 »). La touche plus : « + »
    seul ou « ++ » final (« ctrl++ ») ; « ctrl+ » ou « ctrl+shift+ » (touche manquante) sont refusés."""
    text = combo.strip().replace(" ", "")
    if not text:
        raise KeyParseError("Empty key combination.")
    if text == "+":
        parts = ["+"]
    elif text.endswith("++") and len(text) > 2:  # « ctrl++ » : modificateurs puis la touche plus
        parts = text[:-2].split("+") + ["+"]
    else:
        parts = text.split("+")
    if any(not part for part in parts):
        raise KeyParseError(f"Incomplete key combination: {combo.strip()!r} (missing key, e.g. ctrl+s).")
    mods: list[int] = []
    for part in parts[:-1]:
        vk = MODIFIERS.get(part.lower())
        if vk is None:
            raise KeyParseError(f"Unknown modifier: {part!r} (ctrl, alt, shift, win).")
        if vk not in mods:
            mods.append(vk)
    last = parts[-1]
    low = last.lower()
    if low in MODIFIERS and not mods:
        return Combo((), MODIFIERS[low], None)
    if low in NAMED:
        vk = NAMED[low]
        char = {0x20: " ", 0x0D: "\r", 0x09: "\t"}.get(vk)
        return Combo(tuple(mods), vk, char)
    if len(last) == 1:
        if last.isalpha() and last.isascii():
            return Combo(tuple(mods), ord(last.upper()), last.lower())
        if last.isdigit():
            return Combo(tuple(mods), ord(last), last)
        return Combo(tuple(mods), _vk_for_char(last), last)
    raise KeyParseError(f"Unknown key: {last!r}.")


def parse_sequence(keys: str) -> list[Combo]:
    """« ctrl+a delete » → deux combinaisons, appliquées dans l'ordre."""
    return [parse(token) for token in keys.split()]
