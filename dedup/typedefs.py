"""Общие типы данных, структуры и псевдонимы, используемые всеми модулями."""

from collections.abc import Callable
from typing import NamedTuple

import aiosqlite
from pyrogram import types

# Примитивы (для читаемости)
type ChatID = int
type MessageID = int
type FileUniqueID = str


# Структура, которую возвращает get_audio_attributes
class AudioMeta(NamedTuple):
    """Атрибуты аудиосообщения (порядок полей = порядок колонок audios в БД).

    Attributes:
        file_unique_id: Уникальный ID файла на серверах Telegram.
        file_name: Имя файла (``None``, если не задано).
        file_size: Размер файла в байтах.
        duration: Длительность в секундах (0, если неизвестна).
        performer: Исполнитель из тегов (``None``, если не задан).
        title: Название из тегов (``None``, если не задано).
    """

    file_unique_id: FileUniqueID
    file_name: str | None
    file_size: int
    duration: int
    performer: str | None
    title: str | None


# Структура для одной строки из БД (обертка над sqlite Row)
type DBRow = aiosqlite.Row

# Группа дубликатов - это список строк из БД
type DuplicateGroup = list[DBRow]

# Алиас для функции форматирования строки CSV при экспорте
type CsvRowFormatter = Callable[[DBRow], list[str] | None]

# Ключ ребра графа дубликатов: упорядоченная пара (min message_id, max message_id)
type EdgeKey = tuple[MessageID, MessageID]


class EdgeInfo(NamedTuple):
    """Причина связи двух файлов и коэффициенты сходства.

    Attributes:
        reason: Причина связи: ``"uid"`` / ``"meta"`` / ``"fuzzy"``.
        score: Итоговый коэффициент сходства (для uid/meta = 1.0).
        name: Вклад текстового fuzzy (имя/мета), 0..1; ``None`` для uid/meta
            (legacy-название поля; фактически это лучший текстовый источник).
        dur: Вклад длительности (0..1); ``None`` для uid/meta.
        size: Вклад размера (0..1); ``None`` для uid/meta.
        penalty: Штраф за несовпадение числовых токенов (0.0, если нет).
        text_source: Код источника fuzzy-совпадения (0..3); ``None`` для uid/meta.
    """

    reason: str
    score: float
    name: float | None
    dur: float | None
    size: float | None
    penalty: float
    text_source: int | None = None


# message_id-пара -> метаданные связи
type EdgeMeta = dict[EdgeKey, EdgeInfo]


def edge_key(a: MessageID, b: MessageID) -> EdgeKey:
    """Канонический (неориентированный) ключ ребра.

    Args:
        a: Первый message_id.
        b: Второй message_id.

    Returns:
        Пара ``(min(a, b), max(a, b))``.
    """
    return (a, b) if a < b else (b, a)


# Словари для верификации (ID сообщения -> Объект сообщения или Ошибка/None)
type VerifiedMessagesDict = dict[MessageID, types.Message | Exception | None]


# Результат классификации дубликатов
class ClassificationResult(NamedTuple):
    """Итог классификации верифицированных групп дубликатов.

    Attributes:
        delete_from_tg: message_id для удаления из Telegram.
        delete_from_db: message_id для удаления только из БД (сообщения нет в ТГ).
        update_in_db: Сообщения для обновления в БД (контент изменился).
    """

    delete_from_tg: set[MessageID]
    delete_from_db: set[MessageID]
    update_in_db: list[types.Message]


# Алиас для функции форматирования строки при экспорте
type RowFormatter = Callable[[DBRow], str | None]
