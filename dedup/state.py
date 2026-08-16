"""Глобальное состояние прогона: метки чатов, списки игнорирования, резолв идентификаторов."""

import argparse
import functools
import re
import sqlite3
from collections import defaultdict
from typing import Final
from urllib.parse import urlparse

from pyrogram import types

from .config import CHAT_LABEL_PARTS, SESSION_NAME
from .logger import log
from .typedefs import ChatID

IGNORE_MESSAGES: Final[defaultdict[int, set[int]]] = defaultdict(set)
IGNORE_REGEX: Final[defaultdict[int, list[re.Pattern[str]]]] = defaultdict(list)
GLOBAL_IGNORE_REGEX: Final[list[re.Pattern[str]]] = []
CHAT_LABELS: dict[int, tuple[str, str | None]] = {}


def remember_chat(chat: types.Chat) -> None:
    """Запоминает отображаемое имя чата.

    Args:
        chat: Объект чата Telegram.
    """
    name = chat.title or " ".join(
        p
        for p in (
            getattr(chat, "first_name", ""),
            getattr(chat, "last_name", ""),
        )
        if p
    )
    name = " ".join((name or "").split())
    CHAT_LABELS[chat.id] = (name, chat.username)


@functools.cache
def _username_from_session(chat_id: int) -> str | None:
    """Юзернейм из файла сессии (только чтение).

    Args:
        chat_id: Числовой ID чата.

    Returns:
        Юзернейм или ``None``, если не нашли / не смогли прочитать.
    """
    try:
        with sqlite3.connect(f"file:{SESSION_NAME}.session?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT username FROM usernames WHERE id = ? LIMIT 1", (chat_id,)
            ).fetchone()
            return row[0] if row and row[0] else None
    except sqlite3.Error:
        return None


@functools.cache
def _id_from_session(identifier: str) -> int | None:
    """Обратный резолв: @username / t.me-ссылка -> id из файла сессии.

    Args:
        identifier: Строка вида ``@username`` или ссылка ``t.me/...``.

    Returns:
        Числовой ID чата или ``None``, если не найден / не распознан.
    """
    raw = identifier.strip()

    if not raw:
        return None

    if raw.startswith("@"):
        uname = raw[1:]
    else:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = parsed.netloc.lower()

        if host in {"t.me", "telegram.me"}:
            parts = [p for p in parsed.path.split("/") if p]
            if not parts:
                return None
            if parts[0] == "s" and len(parts) > 1:
                parts = parts[1:]
            uname = parts[0]
        else:
            uname = raw

    uname = uname.strip().strip("/").lower()

    if not uname or uname.startswith("+") or uname in {"c", "joinchat"}:
        return None

    try:
        with sqlite3.connect(f"file:{SESSION_NAME}.session?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT id FROM usernames WHERE username = ? COLLATE NOCASE LIMIT 1",
                (uname,),
            ).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def chat_label(chat_id: int) -> str:
    """Человеческое имя чата для логов согласно ``chat_label_parts``.

    Args:
        chat_id: Числовой ID чата.

    Returns:
        Строка вида ``"Название [@user | -100...]"`` (состав зависит от конфига).
    """
    name, username = CHAT_LABELS.get(chat_id, ("", None))

    if username is None:
        username = _username_from_session(chat_id)

    values = {
        "title": name,
        "username": f"@{username}" if username else "",
        "id": str(chat_id),
    }

    parts = [values[key] for key in CHAT_LABEL_PARTS if values.get(key)]

    if not parts:
        return str(chat_id)

    if len(parts) == 1:
        return parts[0]

    has_title = "title" in CHAT_LABEL_PARTS and bool(name)
    if has_title:
        tail = [
            values[key]
            for key in CHAT_LABEL_PARTS
            if key != "title" and key in values and values[key]
        ]
        return f"{name} [{' | '.join(tail)}]" if tail else name

    return f"[{' | '.join(parts)}]"


def chat_id_or_username(identifier: str) -> ChatID:
    """Тип-функция для argparse: числовой ID как есть, юзернейм — через файл сессии.

    Args:
        identifier: Число, ``@username`` или t.me-ссылка.

    Returns:
        Числовой ID чата.

    Raises:
        argparse.ArgumentTypeError: Если идентификатор не число и не найден
            в файле сессии.
    """
    try:
        return int(identifier)
    except ValueError:
        pass
    if chat_id := _id_from_session(identifier):
        log.info(f"'{identifier}' найден в сессии: {chat_id}")
        return chat_id
    raise argparse.ArgumentTypeError(
        f"не удалось найти '{identifier}' в файле сессии. "
        f"Укажите числовой ID или запустите онлайн-режим (report/download), "
        f"чтобы чат попал в кэш сессии."
    )
