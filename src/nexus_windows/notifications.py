"""Notifications de Windows lues sans rien ouvrir : la base du centre de notifications, en lecture seule.

Le centre de notifications (Win+N) ne s'ouvre qu'au premier plan et se referme dès qu'on rend la main à
l'utilisateur. La même chose se lit dans la base de Windows (%LOCALAPPDATA%\\Microsoft\\Windows\\Notifications\\
wpndatabase.db, SQLite) : application, heure d'arrivée, textes du toast. Ouverte en lecture seule (mode=ro), jamais
modifiée ; codes de sécurité masqués, comme sur le téléphone (nexus-android).
"""

from __future__ import annotations

import datetime as dt
import html
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DATABASE = Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db"))
_TEXT = re.compile(r"<text\b[^>]*>(.*?)</text>", re.DOTALL)
# Un code se masque quand le texte parle de code, de vérification ou de connexion : suites de 4 à 8 chiffres
# (espacées ou non, « 123 456 », « G-123456 »). Les nombres d'un texte ordinaire (années, montants) restent lisibles.
_CODE_CONTEXT = re.compile(
    r"code|otp|v[ée]rif|passcode|mot de passe|password|s[ée]curit|security|2fa|connexion|sign.?in|login|authent",
    re.IGNORECASE,
)
_CODE = re.compile(r"(?<![\w.,])(?:[A-Z]-)?\d(?:[ -]?\d){3,7}(?![\w.,]|\d)")
# Code alphanumérique (« K7F2QX », « ab12-cd34 ») : 5 à 12 signes, au moins un chiffre et une lettre (relecture de Codex).
_ALNUM_CODE = re.compile(
    r"(?<![\w.-])(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[A-Za-z0-9](?:[A-Za-z0-9-]{3,10})[A-Za-z0-9](?![\w-])"
)
MAX_ROWS = 1000  # toasts lus au plus (les plus récents), filtrés ensuite par application
FILETIME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.UTC)


class NotificationsError(RuntimeError):
    pass


@dataclass
class Notification:
    app: str
    arrived: dt.datetime
    texts: list[str]


def mask_codes(text: str, context: str = "") -> str:
    """Codes de sécurité masqués (« ••• ») quand le texte, ou la notification entière (context : titre « Code de
    connexion », code dans le corps), parle de code ou de connexion."""
    if not _CODE_CONTEXT.search(context or text):
        return text
    return _ALNUM_CODE.sub("•••", _CODE.sub("•••", text))


def app_name(primary_id: str) -> str:
    """Nom lisible d'une application à partir de son identifiant (AUMID ou chemin) : « Claude », « Antigravity »."""
    name = primary_id.split("!")[0]
    if name.lower().startswith("electron.app."):
        return name[len("electron.app.") :]
    if "\\" in name:
        return Path(name).stem
    name = name.split("_")[0]  # nom de famille du paquet sans l'éditeur : « AnthropicPBC.Claude »
    return name.split(".")[-1] if "." in name else name


def _texts(payload: bytes | str | None) -> list[str]:
    raw = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload or ""
    out = []
    for match in _TEXT.findall(raw):
        # Entités numériques comprises (« &#55;&#51;… ») : un code ainsi écrit doit être vu, donc masqué.
        text = html.unescape(re.sub(r"<[^>]+>", "", match))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out.append(text)
    return out


def read(limit: int = 20, app: str | None = None, database: Path = DATABASE) -> list[Notification]:
    """Notifications (toasts) les plus récentes d'abord ; app : partie du nom de l'application."""
    if not database.exists():
        raise NotificationsError("Windows notification database not found on this PC.")
    uri = "file:" + database.as_posix() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error as error:
        raise NotificationsError(f"Notification database unreadable: {error}") from error
    try:
        rows = connection.execute(
            "SELECT h.PrimaryId, n.ArrivalTime, n.Payload FROM Notification n "
            "JOIN NotificationHandler h ON h.RecordId = n.HandlerId "
            # Lecture bornée avant de charger les contenus (relecture de Codex) : la base garde des centaines de toasts.
            f"WHERE n.Type = 'toast' ORDER BY n.ArrivalTime DESC LIMIT {MAX_ROWS}"
        ).fetchall()
    except sqlite3.Error as error:
        raise NotificationsError(f"Notification database unreadable: {error}") from error
    finally:
        connection.close()
    out: list[Notification] = []
    wanted = (app or "").lower()
    for primary_id, arrival, payload in rows:
        name = app_name(str(primary_id))
        if wanted and wanted not in name.lower() and wanted not in str(primary_id).lower():
            continue
        texts = _texts(payload)
        if not texts:
            continue
        arrived = (FILETIME_EPOCH + dt.timedelta(microseconds=int(arrival or 0) // 10)).astimezone()
        whole = " ".join(texts)
        out.append(Notification(name, arrived, [mask_codes(t, whole) for t in texts]))
        if len(out) >= limit:
            break
    return out
