"""Прямые операции с SQLite: инициализация, подключение, валидация, ремонт."""

import asyncio
import itertools
from collections import defaultdict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import aiosqlite
from pyrogram import Client

from .config import DB_CACHE_SIZE, DB_FILE, VERIFY_CHUNK_SIZE, VERIFY_CONCURRENCY
from .logger import log
from .state import chat_label
from .tg import _get_audio_attributes

# Этот блок отвечает за все прямое взаимодействие с файлом SQLite: инициализация, подключение, валидация, ремонт и простые запросы (получение ID).


# todo Добавить версирование БД user_version
async def initialize_database() -> None:
    """Выполняется ОДИН РАЗ при запуске. Создает новую схему БД.

    Таблицы ``audios`` и ``chat_sync_state`` создаются с ``IF NOT EXISTS``,
    включается WAL-режим и создаются индексы для поиска дубликатов.
    """
    log.debug("Инициализация схемы базы данных...")
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("PRAGMA journal_mode = WAL;") as cursor:
            row = await cursor.fetchone()
        applied_mode = row[0] if row else None
        if not (applied_mode and applied_mode.lower() == "wal"):
            log.warning(
                f"Не удалось включить WAL, journal_mode: {applied_mode}. "
                "Возможно, БД на сетевой ФС."
            )
        # Основная таблица для хранения информации об аудиофайлах
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS audios (
                chat_id INTEGER, message_id INTEGER, file_unique_id TEXT NOT NULL,
                file_name TEXT, file_size INTEGER, duration INTEGER,
                performer TEXT, title TEXT,
                PRIMARY KEY (chat_id, message_id)
            ) STRICT;
        """)
        # Индексы для быстрого поиска дубликатов
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_unique ON audios (chat_id, file_unique_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_meta ON audios (chat_id, file_name, file_size, duration)"
        )

        # Таблица состояния синхронизации
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_sync_state (
                chat_id INTEGER PRIMARY KEY,
                is_fully_synced INTEGER NOT NULL DEFAULT 0,
                newest_scanned_id INTEGER DEFAULT 0
            ) STRICT;
        """)
        await conn.commit()
    log.debug("Схема базы данных готова.")


@asynccontextmanager
async def create_connection() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Контекстный менеджер: новое, полностью настроенное соединение с БД.

    Применяет PRAGMA-настройки из конфига (synchronous, temp_store,
    cache_size) и включает row_factory = aiosqlite.Row.

    Yields:
        Настроенное асинхронное соединение (закрывается при выходе).
    """
    log.debug("Создание нового оптимизированного соединения с БД...")

    async with aiosqlite.connect(DB_FILE) as conn:
        conn.row_factory = aiosqlite.Row

        pragmas = {
            "synchronous": "NORMAL",
            "temp_store": "MEMORY",
            "cache_size": DB_CACHE_SIZE,
            # рассмотреть: "mmap_size", "page_size", "wal_autocheckpoint" и т.д.
        }

        for pragma, value in pragmas.items():
            await conn.execute(f"PRAGMA {pragma} = {value};")

        log.debug(f"Настройки соединения успешно применены: {pragmas}")

        yield conn

    log.debug("Соединение с БД закрыто.")


async def validate_database() -> bool:
    """Проверяет целостность БД.

    Returns:
        ``False`` (блокирует запуск), если:
        1. Файл БД физически повреждён.
        2. Отсутствуют обязательные таблицы.
        3. В таблице audios есть критически повреждённые данные
           (нужна команда ``repair``).
    """
    log.info("Валидация базы данных...")
    warnings = 0

    try:
        async with aiosqlite.connect(DB_FILE) as conn:
            conn.row_factory = aiosqlite.Row

            # --- 1. Физическая целостность (CRITICAL) ---
            async with conn.execute("PRAGMA integrity_check;") as cur:
                result = await cur.fetchone()
                if not result or result[0].lower() != "ok":
                    log.critical(f"Файл БД физически поврежден: {result[0]}")
                    return False

            # --- 2. Наличие таблиц (CRITICAL) ---
            async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
                tables = {row[0] for row in await cur.fetchall()}

            required = {"audios", "chat_sync_state"}
            missing = required - tables
            if missing:
                log.critical(f"Отсутствуют обязательные таблицы: {missing}")
                return False

            # --- 3. Целостность данных в audios (CRITICAL) ---
            # file_name IS NULL сам по себе валиден (у аудио есть performer/title);
            # сломанной считаем запись без всех трёх текстовых полей сразу
            async with conn.execute("""
                SELECT COUNT(*) FROM audios
                WHERE file_unique_id IS NULL OR file_unique_id = ''
                   OR (file_name IS NULL AND performer IS NULL AND title IS NULL)
                   OR file_size IS NULL OR file_size < 0
                   OR duration IS NULL OR duration < 0
            """) as cur:
                broken_count = (await cur.fetchone())[0]
                if broken_count > 0:
                    log.critical(
                        f"ОБНАРУЖЕНО {broken_count} ПОВРЕЖДЁННЫХ ЗАПИСЕЙ в таблице 'audios'.\n"
                        "Запуск поиска дубликатов на таких данных опасен (возможна потеря данных).\n"
                        "Пожалуйста, запустите скрипт с командой: repair"
                    )
                    return False

            # --- 4. Наличие индексов (WARNING) ---
            async with conn.execute("SELECT name FROM sqlite_master WHERE type='index'") as cur:
                indexes = {row[0] for row in await cur.fetchall()}

            for idx in ("idx_chat_unique", "idx_chat_meta"):
                if idx not in indexes:
                    warnings += 1
                    log.warning(f"Отсутствует индекс '{idx}'. Это замедлит работу.")

            # --- 5. Второстепенные проблемы (WARNING) ---
            async with conn.execute("""
                        SELECT COUNT(*) FROM chat_sync_state WHERE newest_scanned_id < 0
                    """) as cur:
                bad_cursors = (await cur.fetchone())[0]

                if bad_cursors > 0:
                    warnings += 1
                    log.warning(
                        f"Найдено {bad_cursors} некорректных курсоров истории (рекомендуется команда repair)."
                    )

    except aiosqlite.Error as e:
        log.critical(f"Ошибка SQLite при валидации: {e}")
        return False
    except Exception as e:
        log.critical(f"Непредвиденная ошибка валидации: {e}")
        return False

    if warnings:
        log.warning(f"Валидация прошла с {warnings} предупреждениями.")
    else:
        log.info("Валидация базы данных успешно пройдена.")

    return True


async def repair_database(app: Client) -> None:
    """Выполняет умное восстановление и очистку базы данных.

    Пытается восстановить повреждённые записи, используя данные из Telegram,
    сбрасывает некорректные курсоры синхронизации, пересоздаёт индексы
    и выполняет VACUUM.

    Args:
        app: Клиент Telegram для сверки записей с сервером.
    """
    log.info("=" * 15 + "ЗАПУСК РЕМОНТА БД" + "=" * 15)

    # --- ЧАСТЬ 1: Работа с данными (Исправление и Очистка) ---
    async with aiosqlite.connect(DB_FILE) as conn:
        conn.row_factory = aiosqlite.Row

        async with conn.execute("PRAGMA integrity_check;") as cur:
            result = await cur.fetchone()
            if not result or result[0].lower() != "ok":
                log.critical("БД физически повреждена. Восстановите из бэкапа вручную.")
                return

        log.info("Этап 1: Поиск и попытка восстановления поврежденных записей...")

        # file_name IS NULL сам по себе валиден; ищем записи без всех трёх текстовых полей
        query = """
            SELECT chat_id, message_id FROM audios
            WHERE file_unique_id IS NULL OR file_unique_id = ''
               OR (file_name IS NULL AND performer IS NULL AND title IS NULL)
               OR file_size IS NULL OR file_size < 0
               OR duration IS NULL OR duration < 0
        """
        async with conn.execute(query) as cursor:
            broken_records = await cursor.fetchall()

        if not broken_records:
            log.info("Поврежденных записей для восстановления не найдено.")
        else:
            log.info(
                f"Найдено {len(broken_records)} потенциально поврежденных записей. Начинаю сверку с Telegram..."
            )

            records_by_chat = defaultdict(list)
            for rec in broken_records:
                records_by_chat[rec["chat_id"]].append(rec["message_id"])

            records_to_update = []
            ids_to_delete = []
            semaphore = asyncio.Semaphore(VERIFY_CONCURRENCY)

            async def fetch_and_process_chunk(chat_id, chunk_ids):
                """Сверяет один чанк message_id с Telegram и раскладывает результат."""
                async with semaphore:
                    try:
                        messages = await app.get_messages(chat_id, chunk_ids)
                        for original_id, msg in zip(chunk_ids, messages, strict=True):
                            attrs = _get_audio_attributes(msg)
                            if attrs:
                                records_to_update.append((*attrs, chat_id, original_id))
                            else:
                                ids_to_delete.append((chat_id, original_id))
                    except Exception as e:
                        log.error(
                            f"Не удалось проверить чанк для чата {chat_label(chat_id)} (ID: {chunk_ids[0]}...): {e}. Чанк будет пропущен."
                        )

            tasks = []
            for chat_id, msg_ids in records_by_chat.items():
                for chunk_tuple in itertools.batched(msg_ids, VERIFY_CHUNK_SIZE):
                    tasks.append(fetch_and_process_chunk(chat_id, list(chunk_tuple)))

            if tasks:
                await asyncio.gather(*tasks)

            if records_to_update:
                await conn.executemany(
                    "UPDATE audios SET file_unique_id=?, file_name=?, file_size=?, duration=?, performer=?, title=? WHERE chat_id=? AND message_id=?",
                    records_to_update,
                )
                log.info(f"  Восстановлено (обновлено) {len(records_to_update)} записей.")

            if ids_to_delete:
                await conn.executemany(
                    "DELETE FROM audios WHERE chat_id = ? AND message_id = ?",
                    ids_to_delete,
                )
                log.info(f"  Удалено {len(ids_to_delete)} невосстановимых записей.")

        log.info("\nЭтап 2: Выполнение стандартной очистки...")
        async with conn.execute(
            "UPDATE chat_sync_state SET newest_scanned_id = 0 WHERE newest_scanned_id < 0"
        ) as cursor:
            if cursor.rowcount > 0:
                log.info(f"  Сброшено {cursor.rowcount} некорректных newest_scanned_id.")

        log.info("  Проверка и создание индексов...")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_unique ON audios (chat_id, file_unique_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_meta ON audios (chat_id, file_name, file_size, duration)"
        )
        log.info("  Индексы проверены.")

        await conn.commit()

    await asyncio.sleep(0.1)

    # --- ЧАСТЬ 2: Оптимизация (VACUUM) на новом соединении ---
    log.info("\nЭтап 3: Оптимизация файла БД (VACUUM)...")
    try:
        async with aiosqlite.connect(DB_FILE, isolation_level=None) as conn:
            log.info("  -> Выполнение wal_checkpoint(TRUNCATE)...")
            async with conn.execute("PRAGMA wal_checkpoint(TRUNCATE);") as cursor:
                await cursor.fetchall()
            log.info("  -> Checkpoint успешно выполнен, курсор закрыт.")

            log.info("  -> Выполнение VACUUM...")
            await conn.execute("VACUUM;")

            log.info("  Оптимизация и сжатие файла БД завершены.")

    except aiosqlite.Error as e:
        log.error(f"  Произошла ошибка на этапе оптимизации БД: {e}")

    log.info("=" * 15 + "РЕМОНТ БД ЗАВЕРШЕН" + "=" * 15)
