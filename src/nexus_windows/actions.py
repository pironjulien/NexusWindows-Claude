"""Exécution des étapes d'action avec la technique la plus sûre pour chaque famille d'interface.

Ordre des techniques tiré du banc (bench/RESULTS.md) :
- Win32 natif : messages directs (BN_CLICKED, CB/LB_SETCURSEL + notification relue, EM_REPLACESEL, WM_COMMAND).
  UIA Invoke y vole le focus et UIA Select n'y prévient pas l'application ; sur un élément servi par le pont MSAA
  (élément de liste), Invoke fait même un VRAI double-clic : jamais.
- WinForms : UIA (Invoke, Toggle, Value), listes et listes déroulantes par messages.
- WPF / UWP / WinUI : UI Automation seulement (WPF accepte aussi WM_CHAR).
- Chromium / Electron : UI Automation ; champs multilignes et <select> par frappes dans une activation brève ;
  DevTools si l'application a été lancée avec (alors jamais de frappes).
- Tk et canevas : aucun canal d'arrière-plan prouvé → vraie souris/clavier avec accord.
Les clics et la molette par messages vont à la fenêtre de la cible réellement sous le point (surgissante comprise).

Contrat de statut (rapporté au modèle) :
- verified=True  « ✓ » : l'effet a été relu (valeur, coche, sélection, texte apparu) ;
- already=True   « = » : état voulu par la postcondition de l'étape (check) déjà vrai : aucune entrée envoyée ;
- verified=None  « ~ » : action transmise par un canal fiable, effet non relu (issue inconnue : vérifier sur l'image) ;
- ok=False       « ✗ » : échec, refus, relecture contraire, postcondition non atteinte, ou aucun effet visible après
  une action par messages.

Contrat de cible :
- [N] se résout dans la numérotation MONTRÉE au modèle, puis l'élément est relu et son identité contrôlée
  (rôle, nom, identifiant d'automatisation) : s'il a changé, refus « refaire look » ;
- un nom se résout dans un instantané frais (reconstruit au premier nom de chaque appel et après chaque action),
  sans homonyme ni correspondance partielle ambiguë ;
- un point se résout en élément de la CIBLE sous ce point (jamais une autre fenêtre, même du même programme ; la
  plus haute de ses fenêtres qui couvre le point décide) ; pour coche, choix, dépliage, focus et saisie, l'intention
  est appliquée à cet élément, ou refusée.
"""

from __future__ import annotations

# Façade : l'exécuteur est découpé par famille d'interface (même comportement, mêmes noms) :
# - act_base     cible (résolution, relecture, identité), chaînes de techniques, garde-fous (secret, shell)
# - act_win32    messages Win32 (listes, onglets, menus natifs, frappes WM_CHAR par tranches, combinaisons)
# - act_uia      actions par UI Automation (WinForms, WPF, UWP/WinUI) et répartition par famille
# - act_chromium Chromium/Electron/CEF (clic relu, DevTools, saisie sans activation, <select>)
# - act_pointer  points, clics par messages, défilement, glisser, survol
# - act_wait     présence d'un texte, attente
from . import capture, cdp, foreground, keys, methods, policy, realinput, snapshot, uia, worker  # noqa: F401  (réexportés)
from . import win32 as w  # noqa: F401
from .act_base import (  # noqa: F401
    BROWSERS_WITHOUT_MESSAGE_WHEEL,
    CHECK_ROLES,
    CHOICE_ROLES,
    EXPAND_ROLES,
    EXPECTED,
    ITEM_ROLES,
    LEAF_ROLES,
    NO_KEYBOARD_MESSAGES,
    NO_MOUSE_MESSAGES,
    SCROLL_ROLES,
    TEXT_ROLES,
    UIA_ONLY,
    ActionError,
    ExecutorBase,
    NeedsForeground,
    NoEffect,
    NoVisibleEffect,
    Outcome,
    Unconfirmed,
    framework_of,
)
from .act_chromium import ChromiumActions
from .act_confirm import ConfirmActions, ConfirmationRequired  # noqa: F401  (réexporté)
from .act_pointer import PointerActions
from .act_uia import UiaActions
from .act_wait import WaitActions
from .act_win32 import Win32Actions


class Executor(UiaActions, ChromiumActions, Win32Actions, PointerActions, WaitActions, ConfirmActions):
    """Exécuteur des étapes d'une fenêtre cible : réunion des familles (voir les modules act_*)."""
