"""Нечёткий поиск (подкоманда ``search``) по всем аудио в БД: запрос против имён и метаданных.

Скоринг всегда ``token_set_ratio`` независимо от ``matching_mode`` конфига —
это настройка строгости дедупликации, а поиску нужен recall. Флаг ``--wratio``
переключает на WRatio (партиал-совпадения, сжатая шкала).
"""

import asyncio
from pathlib import Path
from typing import Final

import aiosqlite
import numpy as np
from rapidfuzz import fuzz, process
from wcwidth import wcswidth

from .context import get_settings
from .fuzzy import clean_filename, clean_meta, process_for_fuzzy
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
) -> list[tuple[int, DBRow]]:
    """Ранжирует строки таблицы ``audios`` по схожести с запросом.

    Каждая строка представлена двумя вариантами текста — очищенным именем
    файла и очищенными метаданными (``performer+title``); итоговая оценка —
    максимум по вариантам. Работает синхронно: вызывать через
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
    q = process_for_fuzzy(query)
    if not q or not rows:
        return []

    variant_texts: list[str] = []
    variant_rows: list[int] = []
    for i, row in enumerate(rows):
        name = process_for_fuzzy(clean_filename(row["file_name"]))
        if name:
            variant_texts.append(name)
            variant_rows.append(i)
        meta = process_for_fuzzy(clean_meta(row["performer"], row["title"]))
        if meta:
            variant_texts.append(meta)
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
        ((int(score), rows[row_i]) for row_i, score in best.items()),
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


def _format_results(results: list[tuple[int, DBRow]]) -> list[str]:
    """Форматирует строки результата для вывода в консоль.

    Выравнивание как в экспорте ``filenames-url``: каждая колонка (имя,
    длительность, размер, чат) добивается пробелами до самой широкой строки
    результата с учётом визуальной ширины символов. Колонка, пустая во всех
    строках (например, нет размера), не выводится.
    """
    cells: list[tuple[int, str, str, str, str, str]] = []
    for score, row in results:
        title = (
            row["file_name"]
            or " ".join(filter(None, (row["performer"], "-", row["title"]))).strip(" -")
            or "<без имени>"
        )
        size = format_bytes(row["file_size"]) if row["file_size"] else ""
        public_chat_id = str(row["chat_id"]).removeprefix("-100")
        cells.append(
            (
                score,
                title,
                format_duration(row["duration"]),
                size,
                chat_label(row["chat_id"]),
                f"https://t.me/c/{public_chat_id}/{row['message_id']}",
            )
        )

    widths = [
        max((_visual_width(c[1]) for c in cells), default=0),  # имя
        max((_visual_width(c[2]) for c in cells), default=0),  # длительность
        max((_visual_width(c[3]) for c in cells), default=0),  # размер
        max((_visual_width(c[4]) for c in cells), default=0),  # чат
    ]
    has_size = any(c[3] for c in cells)

    lines = []
    for score, title, duration, size, chat, link in cells:
        parts = [f"[{score:>3}%] {_pad(title, widths[0])}", _pad(duration, widths[1])]
        if has_size:
            parts.append(_pad(size, widths[2]))
        parts.append(_pad(chat, widths[3]))
        parts.append(link)
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
