"""Нечёткий поиск (подкоманда ``search``) по всем аудио в БД: запрос против имён и метаданных.

Скоринг всегда ``token_set_ratio`` независимо от ``matching_mode`` конфига —
это настройка строгости дедупликации, а поиску нужен recall. Флаг ``--wratio``
переключает на WRatio (партиал-совпадения, сжатая шкала).

Имена и мета индексируются в двух вариантах (см. :mod:`dedup.cleaning`):
дедуп-очистка ``clean_filename`` для обычных запросов и лёгкая нормализация
``clean_for_search``, сохраняющая сайты-качалки, домены, расширения и ID —
по этому «мусору» тоже можно искать.
"""

import asyncio
from pathlib import Path
from typing import Final, NamedTuple

import aiosqlite
import numpy as np
from rapidfuzz import fuzz, process
from wcwidth import wcswidth

from .cleaning import (
    clean_filename,
    clean_for_search,
    clean_meta,
    clean_meta_for_search,
    process_for_fuzzy,
)
from .context import get_settings
from .logger import log
from .state import chat_label
from .typedefs import DBRow
from .utils import format_bytes, format_duration

# Минимальная схожесть (0..100) и максимум выводимых результатов.
SCORE_CUTOFF: Final[float] = 50.0
RESULT_LIMIT: Final[int] = 200


def rank_rows(
    query: str,
    rows: list[DBRow],
    score_cutoff: float = SCORE_CUTOFF,
    wratio: bool = False,
    result_limit: int = RESULT_LIMIT,
) -> list[tuple[float, DBRow]]:
    """Ранжирует строки таблицы ``audios`` по схожести с запросом.

    Каждая строка представлена до четырёх вариантов текста: имя и мета
    (``performer+title``) в двух формах — дедуп-очистка
    (``clean_filename``/``clean_meta``) и лёгкая поисковая нормализация
    (``clean_for_search``/``clean_meta_for_search``), сохраняющая сайты,
    расширения и ID; итоговая оценка — максимум по вариантам. Запрос
    нормализуется той же лёгкой чисткой, поэтому ``zaycev.net`` и
    ``zaycev_net`` эквивалентны. Работает синхронно: вызывать через
    ``asyncio.to_thread``.

    Скорер — всегда ``token_set_ratio`` (настройки дедупликации не влияют);
    ``wratio=True`` переключает на WRatio.

    Args:
        query: Поисковый запрос пользователя.
        rows: Строки таблицы ``audios``.
        score_cutoff: Минимальная схожесть (0..100) для попадания в выдачу.
        wratio: Использовать WRatio вместо ``token_set_ratio``.
        result_limit: Максимум результатов в выдаче.

    Returns:
        Не более ``result_limit`` пар ``(score, row)`` со схожестью
        не ниже ``score_cutoff``, по убыванию score.
    """
    q = process_for_fuzzy(clean_for_search(query))
    if not q or not rows:
        return []

    variant_texts: list[str] = []
    variant_rows: list[int] = []
    for i, row in enumerate(rows):
        # dict.fromkeys убирает дубли вариантов (легко нормализованная
        # мета без «мусора» совпадает с дедуп-очищенной).
        for cleaned in dict.fromkeys(
            (
                clean_for_search(row["file_name"]),
                clean_filename(row["file_name"]),
                clean_meta_for_search(row["performer"], row["title"]),
                clean_meta(row["performer"], row["title"]),
            )
        ):
            processed = process_for_fuzzy(cleaned)
            if processed:
                variant_texts.append(processed)
                variant_rows.append(i)

    if not variant_texts:
        return []

    scorer = fuzz.WRatio if wratio else fuzz.token_set_ratio
    dist = process.cdist(
        [q],
        variant_texts,
        scorer=scorer,
        processor=None,
        dtype=np.float64,
        score_cutoff=score_cutoff,
        workers=1,
    )

    # Максимум по вариантам одной строки (имя / мета); 0 = ниже порога.
    best: dict[int, float] = {}
    for variant_i, score in enumerate(dist[0]):
        row_i = variant_rows[variant_i]
        if score > best.get(row_i, 0.0):
            best[row_i] = float(score)

    ranked = sorted(
        ((score, rows[row_i]) for row_i, score in best.items()),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return ranked[: max(result_limit, 0)]


def _visual_width(text: str) -> int:
    """Визуальная ширина строки в клетках консоли (широкий символ = 2).

    ``wcswidth`` возвращает ``-1`` на управляющих символах — тогда фолбэк
    на длину строки, как в экспорте имён со ссылками.
    """
    width = wcswidth(text)
    return width if width >= 0 else len(text)


def _pad(text: str, width: int) -> str:
    """Добивает строку пробелами до визуальной ширины ``width``."""
    return text + " " * (width - _visual_width(text))


class _ResultRow(NamedTuple):
    """Одна строка результата, уже приведённая к отображаемому виду.

    Все поля, кроме ``score``, уже отформатированы под вывод в консоль
    (см. ``_row_from_db``) и не требуют дальнейшей обработки.

    Attributes:
        score: Схожесть с запросом (0..100), как вернул ``rank_rows``.
        title: Имя файла, либо ``"исполнитель - трек"`` из метаданных,
            либо ``"<без имени>"``, если и то и другое пусто.
        duration: Длительность, отформатированная ``format_duration``.
        size: Размер файла, отформатированный ``format_bytes``; пустая
            строка, если размер неизвестен.
        chat: Человекочитаемая метка чата (``chat_label``).
        link: Прямая ссылка на сообщение в Telegram (``t.me/c/...``).
    """

    score: float
    title: str
    duration: str
    size: str
    chat: str
    link: str


def _row_from_db(score: float, row: DBRow) -> _ResultRow:
    title = (
        row["file_name"]
        or " ".join(filter(None, (row["performer"], "-", row["title"]))).strip(" -")
        or "<без имени>"
    )
    public_chat_id = str(row["chat_id"]).removeprefix("-100")
    return _ResultRow(
        score=score,
        title=title,
        duration=format_duration(row["duration"]),
        size=format_bytes(row["file_size"]) if row["file_size"] else "",
        chat=chat_label(row["chat_id"]),
        link=f"https://t.me/c/{public_chat_id}/{row['message_id']}",
    )


# Колонки между именем и ссылкой, в порядке вывода. Поля из
# _OPTIONAL_MIDDLE_FIELDS скрываются целиком, если пусты у всех строк
# (как сейчас происходит с size). Чтобы добавить колонку: (1) поле в
# _ResultRow, (2) заполнить его в _row_from_db, (3) добавить сюда —
# индексы нигде больше трогать не нужно.
_MIDDLE_FIELDS: Final[tuple[str, ...]] = ("duration", "size", "chat")
_OPTIONAL_MIDDLE_FIELDS: Final[frozenset[str]] = frozenset({"size"})


def _format_results(results: list[tuple[float, DBRow]]) -> list[str]:
    """Форматирует строки результата для вывода в консоль.

    Выравнивание как в экспорте ``filenames-url``: каждая колонка (имя,
    длительность, размер, чат) добивается пробелами до самой широкой строки
    результата с учётом визуальной ширины символов. Колонка, пустая во всех
    строках (например, нет размера), не выводится.
    """
    rows = [_row_from_db(score, row) for score, row in results]

    title_width = max((_visual_width(r.title) for r in rows), default=0)
    widths = {
        field: max((_visual_width(getattr(r, field)) for r in rows), default=0)
        for field in _MIDDLE_FIELDS
    }
    visible_fields = [
        field
        for field in _MIDDLE_FIELDS
        if field not in _OPTIONAL_MIDDLE_FIELDS or any(getattr(r, field) for r in rows)
    ]

    lines = []
    for r in rows:
        parts = [f"[{r.score:>5.1f}%] {_pad(r.title, title_width)}"]
        parts += [_pad(getattr(r, field), widths[field]) for field in visible_fields]
        parts.append(r.link)
        lines.append(" | ".join(parts))
    return lines


async def run_search(
    query: str,
    score_cutoff: float = SCORE_CUTOFF,
    wratio: bool = False,
    result_limit: int = RESULT_LIMIT,
) -> None:
    """Выполняет нечёткий поиск по всем чатам в БД и печатает результаты.

    Работает офлайн: требуется только файл БД, Telegram-клиент не нужен.

    Args:
        query: Поисковый запрос пользователя.
        score_cutoff: Минимальная схожесть (0..100) для попадания в выдачу.
        wratio: Использовать WRatio вместо ``token_set_ratio``.
        result_limit: Максимум результатов в выдаче.
    """
    db_file = get_settings().paths.db_file
    try:
        if not Path(db_file).exists():
            log.critical(f"Файл базы данных '{db_file}' не найден. Нечего искать.")
            return

        async with aiosqlite.connect(db_file) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM audios") as cursor:
                rows = list(await cursor.fetchall())

        if not rows:
            log.warning("База данных не содержит аудиозаписей.")
            return

        results = await asyncio.to_thread(
            rank_rows, query, rows, score_cutoff, wratio, result_limit
        )
        log.info(f"Просмотрено {len(rows)} аудио; совпадений: {len(results)}.")
        for line in _format_results(results):
            log.info(line)

    except aiosqlite.Error as e:
        log.critical(f"Произошла ошибка SQLite при поиске: {e}")
