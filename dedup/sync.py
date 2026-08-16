"""Синхронизация истории аудиосообщений чата в базу данных."""

import time
from typing import Any

import aiosqlite
from pyrogram import Client
from pyrogram.enums import MessagesFilter

from .context import get_settings
from .logger import log
from .state import chat_label
from .tg import get_audio_attributes
from .typedefs import ChatID


async def _flush_audio_batch(
    conn: aiosqlite.Connection,
    batch: list[tuple],
) -> int:
    """Вставляет батч аудио и коммитит.

    Args:
        conn: Соединение с БД.
        batch: Список кортежей со значениями колонок ``audios``.

    Returns:
        Число реально вставленных строк (``INSERT OR IGNORE``).
    """
    cur = await conn.executemany(
        "INSERT OR IGNORE INTO audios (chat_id, message_id, file_unique_id, file_name, file_size, duration, performer, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        batch,
    )
    await conn.commit()
    return cur.rowcount


async def _get_media_total(
    app: Client,
    chat_id: ChatID,
    media_filter: MessagesFilter,
    is_incremental: bool,
) -> int | None:
    """Пытается получить общее количество сообщений для прогресса.

    Args:
        app: Клиент Telegram.
        chat_id: ID чата.
        media_filter: Фильтр типа медиа (AUDIO или DOCUMENT).
        is_incremental: Инкрементальный режим — total не нужен.

    Returns:
        Общее число сообщений или ``None``, если получить не удалось/не нужно.
    """
    if is_incremental:
        return None
    try:
        count = await app.search_messages_count(chat_id, filter=media_filter)
        return count if count > 0 else None
    except Exception:
        return None


async def sync_messages(
    app: Client,
    chat_id: ChatID,
    conn: aiosqlite.Connection,
) -> None:
    """Синхронизация: поиск AUDIO + DOCUMENT на серверах Telegram.

    Каждый батч коммитится отдельно; курсор обновляется только в конце;
    ``INSERT OR IGNORE`` даёт идемпотентность.

    Args:
        app: Клиент Telegram.
        chat_id: ID синхронизируемого чата.
        conn: Соединение с БД.

    Raises:
        Exception: Любая ошибка синхронизации — транзакция откатывается
            и ошибка пробрасывается наверх.
    """
    log.info(f"\n{'=' * 40}\nНачинаю синхронизацию чата {chat_label(chat_id)}")

    # --- 1. Читаем состояние ---
    async with conn.execute(
        "SELECT is_fully_synced, newest_scanned_id FROM chat_sync_state WHERE chat_id = ?",
        (chat_id,),
    ) as c:
        row = await c.fetchone()

    is_fully_synced = row[0] if row else 0
    db_newest_id = row[1] if row else 0

    max_id_found = db_newest_id
    try:
        async for m in app.get_chat_history(chat_id, limit=1):
            max_id_found = max(max_id_found, m.id)
    except Exception:
        pass

    total_added = 0

    search_kwargs: dict[str, Any] = {}
    is_incremental = is_fully_synced and db_newest_id > 0

    if is_incremental:
        search_kwargs["min_id"] = db_newest_id
        log.info(f"Инкрементальная синхронизация (ID > {db_newest_id})")

    FILTERS_MAP = {
        MessagesFilter.AUDIO: "АУДИО",
        MessagesFilter.DOCUMENT: "ДОКУМЕНТЫ",
    }

    LOG_INTERVAL = 5.0  # секунд между строками прогресса

    # --- 2. Сканируем и пишем побатчево ---
    try:
        for media_filter, filter_name in FILTERS_MAP.items():
            total_count = await _get_media_total(
                app,
                chat_id,
                media_filter,
                is_incremental,
            )

            batch: list[tuple] = []
            filter_added = 0
            scanned = 0
            last_log_time = time.monotonic()

            async for message in app.search_messages(
                chat_id,
                filter=media_filter,
                **search_kwargs,
            ):
                scanned += 1

                if message.id > max_id_found:
                    max_id_found = message.id

                audio_attrs = get_audio_attributes(message)
                if audio_attrs:
                    batch.append((message.chat.id, message.id, *audio_attrs))

                # --- батч заполнен → коммитим ---
                if len(batch) >= get_settings().performance.sync_batch_size:
                    added = await _flush_audio_batch(conn, batch)
                    filter_added += added
                    total_added += added
                    batch.clear()

                # --- периодический лог ---
                now = time.monotonic()
                if now - last_log_time >= LOG_INTERVAL:
                    progress = f" / {total_count}" if total_count else ""
                    log.info(
                        f"  {filter_name}: "
                        f"просмотрено {scanned}{progress}, "
                        f"добавлено {filter_added}"
                    )
                    last_log_time = now

            # --- остаток ---
            if batch:
                added = await _flush_audio_batch(conn, batch)
                filter_added += added
                total_added += added
                batch.clear()

            log.info(f"  {filter_name}: готово. Просмотрено {scanned}, добавлено {filter_added}")

        # --- 3. Фиксируем курсор ---
        await conn.execute(
            "INSERT OR REPLACE INTO chat_sync_state "
            "(chat_id, is_fully_synced, newest_scanned_id) "
            "VALUES (?, 1, ?)",
            (chat_id, max_id_found),
        )
        await conn.commit()
        log.info(f"Синхронизация завершена. Новых записей: {total_added}")

    except Exception as e:
        await conn.rollback()
        log.error(
            f"Ошибка синхронизации чата {chat_label(chat_id)}: {e}",
            exc_info=True,
        )
        raise
