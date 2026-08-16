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

from .context import get_settings
from .fuzzy import clean_filename, clean_meta, process_for_fuzzy
from .logger import log
from .state import chat_label
from .typedefs import DBRow
from .utils import format_bytes, format_duration

# Минимальная схожесть (0..100) и максимум выводимых результатов.
SCORE_CUTOFF: Final[float] = 70.0
_RESULT_LIMIT: Final[int] = 20


def rank_rows(
    query: str, rows: list[DBRow], score_cutoff: float = SCORE_CUTOFF, wratio: bool = False
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

    Returns:
        Не более ``_RESULT_LIMIT`` пар ``(score, row)`` со схожестью
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
    return ranked[:_RESULT_LIMIT]


def _format_result(score: int, row: DBRow) -> str:
    """Форматирует одну строку результата для вывода в консоль."""
    title = row["file_name"] or " ".join(
        filter(None, (row["performer"], "-", row["title"]))
    ).strip(" -") or "<без имени>"
    duration = format_duration(row["duration"])
    size = format_bytes(row["file_size"]) if row["file_size"] else ""
    public_chat_id = str(row["chat_id"]).removeprefix("-100")
    link = f"https://t.me/c/{public_chat_id}/{row['message_id']}"
    parts = (f"[{score}%] {title}", duration, size, chat_label(row["chat_id"]), link)
    return " | ".join(p for p in parts if p)


async def run_search(
    query: str, score_cutoff: float = SCORE_CUTOFF, wratio: bool = False
) -> None:
    """Выполняет нечёткий поиск по всем чатам в БД и печатает результаты.

    Работает офлайн: требуется только файл БД, Telegram-клиент не нужен.

    Args:
        query: Поисковый запрос пользователя.
        score_cutoff: Минимальная схожесть (0..100) для попадания в выдачу.
        wratio: Использовать WRatio вместо ``token_set_ratio``.
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

        results = await asyncio.to_thread(rank_rows, query, rows, score_cutoff, wratio)
        log.info(f"Просмотрено {len(rows)} аудио; совпадений: {len(results)}.")
        for score, row in results:
            log.info(_format_result(score, row))

    except aiosqlite.Error as e:
        log.critical(f"Произошла ошибка SQLite при поиске: {e}")
