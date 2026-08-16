"""Фейки объектов Pyrogram: клиент, сообщения, чаты, участники.

Прод-код читает у этих объектов только атрибуты, поэтому достаточно
``SimpleNamespace``; перечисления (ChatType и пр.) используются настоящие,
чтобы сравнения ``==`` работали как в бою.
"""

import datetime
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

from pyrogram.enums import ChatType

# Гость от имени которого работает FakeClient
ME_ID = 777000


def make_chat(
    chat_id: int,
    *,
    title: str | None = None,
    username: str | None = None,
    chat_type: ChatType = ChatType.CHANNEL,
    first_name: str | None = None,
    last_name: str | None = None,
) -> SimpleNamespace:
    """Объект чата, совместимый с state.remember_chat и проверками прав."""
    return SimpleNamespace(
        id=chat_id,
        title=title,
        username=username,
        type=chat_type,
        first_name=first_name,
        last_name=last_name,
    )


def make_member(status, *, privileges=None) -> SimpleNamespace:
    """Объект участника чата для get_chat_member."""
    return SimpleNamespace(status=status, privileges=privileges)


def make_message(
    message_id: int,
    *,
    chat_id: int = -1001234567890,
    file_name: str | None = None,
    file_size: int = 1_000_000,
    duration: int = 100,
    performer: str | None = None,
    title: str | None = None,
    uid: str | None = None,
    mime_type: str = "audio/mpeg",
    empty: bool = False,
    service: bool = False,
    date: datetime.datetime | None = None,
    kind: str = "audio",  # "audio" | "document" | "none"
) -> SimpleNamespace:
    """Сообщение с аудио- или аудио-документом (совместимо с get_audio_attributes)."""
    uid = uid if uid is not None else f"fake-uid-{message_id}"
    audio = None
    document = None
    if kind == "audio":
        audio = SimpleNamespace(
            file_unique_id=uid,
            file_name=file_name,
            file_size=file_size,
            duration=duration,
            performer=performer,
            title=title,
            mime_type=mime_type,
        )
    elif kind == "document":
        document = SimpleNamespace(
            file_unique_id=uid,
            file_name=file_name,
            file_size=file_size,
            mime_type=mime_type,
        )
    return SimpleNamespace(
        id=message_id,
        chat=make_chat(chat_id),
        empty=empty,
        service=service,
        audio=audio,
        document=document,
        date=date or datetime.datetime(2026, 1, 1, 12, 0, 0),
    )


class FakeClient:
    """Программируемый заменитель pyrogram.Client без сети.

    Ответы задаются атрибутами-словарями; все вызовы записываются в
    ``calls`` (имя метода -> список ``(args, kwargs)``).
    """

    def __init__(self):
        self.calls: dict[str, list] = defaultdict(list)
        self.me = SimpleNamespace(id=ME_ID, is_premium=False)
        # get_chat: идентификатор -> чат (или исключение)
        self.chats: dict = {}
        # get_chat_member: (chat_id, user_id) -> участник
        self.members: dict = {}
        # get_messages: message_id -> сообщение (в любом чате)
        self.messages: dict = {}
        # Сообщения, выдаваемые get_chat_history/search_messages
        self.history: list = []
        # Скачанные байты, которые пишет download_media
        self.download_payload = b"fake-audio-bytes"

    def _record(self, name, *args, **kwargs):
        self.calls[name].append((args, kwargs))

    # --- Чаты и участники ---

    async def get_chat(self, chat_id):
        self._record("get_chat", chat_id)
        result = self.chats.get(chat_id)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise KeyError(f"FakeClient: чат не запрограммирован: {chat_id}")
        return result

    async def get_chat_member(self, chat_id, user_id):
        self._record("get_chat_member", chat_id, user_id)
        result = self.members.get((chat_id, user_id))
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise KeyError(f"FakeClient: участник не запрограммирован: {chat_id}/{user_id}")
        return result

    # --- Сообщения ---

    async def get_messages(self, chat_id, message_ids):
        self._record("get_messages", chat_id, message_ids)
        if isinstance(message_ids, int):
            message_ids = [message_ids]
        return [self.messages.get(mid) or make_message(mid, empty=True) for mid in message_ids]

    async def get_chat_history(self, chat_id, limit: int = 0) -> Iterator:
        self._record("get_chat_history", chat_id, limit=limit)
        for message in self.history[:limit]:
            yield message

    async def search_messages(self, chat_id, filter=None, **kwargs) -> Iterator:
        self._record("search_messages", chat_id, filter=filter, **kwargs)
        for message in self.history:
            yield message

    async def search_messages_count(self, chat_id, filter=None) -> int:
        self._record("search_messages_count", chat_id, filter)
        return len(self.history)

    # --- Изменения (по умолчанию «всё успешно») ---

    async def delete_messages(self, chat_id, message_ids, revoke: bool = True) -> int:
        self._record("delete_messages", chat_id, message_ids, revoke=revoke)
        return len(message_ids)

    async def forward_messages(self, chat_id, from_chat_id, message_ids, hide_sender_name=False):
        self._record(
            "forward_messages",
            chat_id,
            from_chat_id,
            message_ids,
            hide_sender_name=hide_sender_name,
        )
        return [None] * len(message_ids)

    async def copy_message(self, chat_id, from_chat_id, message_id):
        self._record("copy_message", chat_id, from_chat_id, message_id)
        return SimpleNamespace(id=1)

    async def send_message(self, chat_id, text):
        self._record("send_message", chat_id, text)
        return SimpleNamespace(id=1)

    # --- Скачивание ---

    async def download_media(self, message, file_name=None, progress=None):
        self._record("download_media", message, file_name)
        if file_name:
            path = Path(file_name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.download_payload)
        return file_name

    @staticmethod
    def guess_extension(mime_type: str | None) -> str | None:
        return {
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "audio/x-flac": ".flac",
            "audio/ogg": ".ogg",
        }.get(mime_type)
