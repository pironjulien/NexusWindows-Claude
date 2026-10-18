"""Hors Windows : le serveur démarre, répond à initialize et liste ses outils, sans rien piloter.

Les annuaires MCP (Glama) lancent les serveurs dans un conteneur Linux pour lire leurs outils. Les modules du bureau
chargent à l'import des DLL Windows (user32, kernel32…), comtypes et winreg : importé par server.py AVANT eux et
seulement hors Windows, ce module met à leur place des bouchons. Tout usage réel d'un bouchon (appel, lecture d'un
résultat) lève WindowsRequired : chaque outil répond que Windows est requis. Sous Windows, ce module n'est jamais
importé et rien ne change.
"""

from __future__ import annotations

import ctypes
import sys
import types
from typing import Any


class WindowsRequired(RuntimeError):
    """Outil de bureau appelé hors Windows."""


MESSAGE = f"nexus-windows pilote le bureau Windows : indisponible sous {sys.platform}."


class _StubMeta(type):
    """Bouchon : tout attribut, tout appel et toute sous-classe rendent un bouchon (code d'import des modules) ; une
    vraie lecture (booléen, nombre, itération, texte) lève WindowsRequired."""

    def __getattr__(cls, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return cls

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        return cls

    def __getitem__(cls, key: Any) -> Any:
        return cls

    def __mul__(cls, other: Any) -> Any:
        return cls

    def __or__(cls, other: Any) -> Any:
        return cls

    def __bool__(cls) -> bool:
        raise WindowsRequired(MESSAGE)

    def __iter__(cls) -> Any:
        raise WindowsRequired(MESSAGE)

    def __int__(cls) -> int:
        raise WindowsRequired(MESSAGE)


class Stub(metaclass=_StubMeta):
    pass


class _StubModule(types.ModuleType):
    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return Stub


def install() -> None:
    if sys.platform == "win32":
        return
    if not hasattr(ctypes, "WINFUNCTYPE"):  # vrai type ctypes : il sert de champ de structures à l'import
        ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE  # type: ignore[attr-defined]
    for name in ("WinDLL", "OleDLL"):
        if not hasattr(ctypes, name):
            setattr(ctypes, name, Stub)
    for name in ("windll", "oledll"):
        if not hasattr(ctypes, name):
            setattr(ctypes, name, Stub)
    for name in ("comtypes", "comtypes.client", "comtypes.gen", "comtypes.gen.UIAutomationClient", "winreg",
                 "windows_capture"):  # fmt: skip
        module = sys.modules.get(name)
        if module is None or isinstance(module, _StubModule):
            stub = _StubModule(name)
            stub.__path__ = []  # paquet : « from comtypes.gen import … » passe par ici
            sys.modules[name] = stub
    sys.modules["comtypes"].COMError = WindowsRequired  # type: ignore[attr-defined]  (classe d'exception attrapée)


install()
