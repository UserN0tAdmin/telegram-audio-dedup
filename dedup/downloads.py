"""Параллельное скачивание аудиофайлов чата."""

import asyncio
import itertools
import os
import shutil
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from pyrogram import Client

from .config import DOWNLOADS_DIR
from .db import create_connection
from .logger import log
from .state import chat_label
from .typedefs import ChatID
from .utils import _format_bytes, _get_size_safely, _sanitize_filename


def _download_matches_existing(final_path: Path, expected_size: int, expected_mtime: float) -> bool:
    """Проверяет, лежит ли на диске уже скачанная версия файла.

    Args:
        final_path: Путь к локальному файлу.
        expected_size: Ожидаемый размер из БД (допуск 100 байт).
        expected_mtime: Ожидаемая дата изменения; ``0`` — не проверять
            (2 сек погрешности для ФС типа FAT32/exFAT).

    Returns:
        ``True``, если размер и дата совпадают с ожидаемыми.
    """
    existing_size = _get_size_safely(final_path)
    try:
        existing_mtime = final_path.stat().st_mtime
    except OSError:
        existing_mtime = 0

    size_matches = existing_size > 0 and abs(existing_size - expected_size) < 100
    mtime_matches = expected_mtime == 0 or abs(existing_mtime - expected_mtime) <= 2.0
    return size_matches and mtime_matches


def _clear_tty_line(is_tty: bool) -> None:
    """Стирает текущую строку терминала (только в TTY)."""
    if is_tty:
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()


def _render_download_status(is_tty: bool, active: dict[str, int], concurrency: int) -> None:
    """Перерисовывает строку статуса активных загрузок (только в TTY)."""
    if not is_tty:
        return

    parts = []
    for name in sorted(active.keys()):
        percent = active[name]
        short_name = (name[:20] + "..") if len(name) > 23 else name
        parts.append(f"[{short_name}: {percent}%]")

    status_line = "  ".join(parts)

    sys.stdout.write(f"\r\033[KЗагрузка ({len(active)}/{concurrency} парал.): {status_line}")
    sys.stdout.flush()


def _make_download_progress_callback(
    is_tty: bool, active: dict[str, int], concurrency: int, filename: str
) -> Callable[[int, int], None]:
    """Создаёт callback прогресса pyrogram для одного файла (не чаще 10 Гц)."""
    last_update_time = 0.0

    def progress(current: int, total: int) -> None:
        nonlocal last_update_time
        if total == 0:
            return

        percent = int(current * 100 / total)
        now = time.time()

        if is_tty and (percent >= 100 or (now - last_update_time > 0.1)):
            active[filename] = percent
            _render_download_status(is_tty, active, concurrency)
            last_update_time = now

    return progress


async def _download_worker(
    app: Client,
    download_dir: Path,
    queue: asyncio.Queue,
    is_tty: bool,
    active: dict[str, int],
    stats: dict[str, int],
    concurrency: int,
) -> None:
    """Воркер очереди скачивания: подготавливает имя и качает файл.

    Args:
        app: Клиент Telegram.
        download_dir: Каталог для сохранения файлов чата.
        queue: Очередь задач ``(message, file_name, expected_size)``.
        is_tty: Выводить ли живой прогресс в терминал.
        active: Активные загрузки: имя файла -> процент (общий словарь).
        stats: Счётчики результата (общий словарь success/skipped/error).
        concurrency: Число параллельных воркеров (для статус-строки).
    """
    while True:
        try:
            task_item = await queue.get()
        except asyncio.CancelledError:
            return

        message, file_name, expected_size = task_item
        safe_name = "unknown"
        final_path = None

        try:
            # --- 1. Подготовка имени файла ---
            base_name = file_name if file_name else f"audio_{message.id}"
            safe_name = _sanitize_filename(base_name)

            # Если расширения нет, пытаемся угадать по mime-type
            if not Path(safe_name).suffix:
                mime = None
                if message.audio:
                    mime = message.audio.mime_type
                elif message.document:
                    mime = message.document.mime_type
                guessed_ext = app.guess_extension(mime) if mime else None
                safe_name += guessed_ext if guessed_ext else ".mp3"

            final_path = download_dir / safe_name

            if safe_name in active:
                safe_name = f"{message.id}_{safe_name}"
                final_path = download_dir / safe_name

            expected_mtime = message.date.timestamp() if message.date else 0

            should_download = True
            if final_path.exists():
                if _download_matches_existing(final_path, expected_size, expected_mtime):
                    should_download = False
                else:
                    safe_name = f"{message.id}_{safe_name}"
                    final_path = download_dir / safe_name
                    if final_path.exists() and _download_matches_existing(
                        final_path, expected_size, expected_mtime
                    ):
                        should_download = False

            # --- 2. Выполнение действия ---
            if not should_download:
                if not is_tty:
                    log.debug(f"Файл существует, пропуск: {safe_name}")
                stats["skipped"] += 1
            else:
                active[safe_name] = 0
                if not is_tty:
                    log.info(f"Начало загрузки: {safe_name} ({_format_bytes(expected_size)})")
                else:
                    _render_download_status(is_tty, active, concurrency)

                progress_callback = _make_download_progress_callback(
                    is_tty, active, concurrency, safe_name
                )

                await app.download_media(
                    message,
                    file_name=str(final_path),
                    progress=progress_callback,
                )

                # --- Установка оригинальной даты изменения файла ---
                if message.date:
                    try:
                        mtime = message.date.timestamp()
                        os.utime(final_path, (mtime, mtime))
                    except Exception as e:
                        log.debug(f"Не удалось обновить дату для файла {safe_name}: {e}")

                if not is_tty:
                    log.info(f"Успешно скачано: {safe_name}")

                stats["success"] += 1

        except Exception as e:
            if final_path and final_path.exists():
                with suppress(OSError):
                    os.remove(final_path)

            _clear_tty_line(is_tty)
            log.error(f"Ошибка загрузки {safe_name}: {e}")
            stats["error"] += 1

        finally:
            active.pop(safe_name, None)
            _render_download_status(is_tty, active, concurrency)
            queue.task_done()


async def download_chat_audio(app: Client, chat_id: ChatID) -> None:
    """Скачивает все аудиофайлы из указанного чата в локальную папку downloads.

    Уже скачанные файлы пропускаются по размеру и дате изменения;
    скачивание идёт пулом воркеров с прогрессом в TTY.

    Args:
        app: Клиент Telegram.
        chat_id: ID чата, чьи аудио скачивать.
    """
    log.info(f"Запуск режима СКАЧИВАНИЯ для чата {chat_label(chat_id)}...")

    download_dir = Path(DOWNLOADS_DIR) / str(chat_id)
    download_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Папка для сохранения: {download_dir.resolve()}")

    is_tty = sys.stdout.isatty()

    async with (
        create_connection() as conn,
        conn.execute(
            "SELECT message_id, file_name, file_size FROM audios WHERE chat_id = ? ORDER BY message_id",
            (chat_id,),
        ) as cursor,
    ):
        records = await cursor.fetchall()

    if not records:
        log.warning(
            f"В базе данных нет записей для чата {chat_label(chat_id)}. Сначала запустите синхронизацию."
        )
        return

    total_files = len(records)

    total_expected_bytes = sum((r["file_size"] or 0) for r in records)

    _, _, free_bytes = await asyncio.to_thread(shutil.disk_usage, download_dir)

    log.info(f"Статистика для скачивания (Чат {chat_label(chat_id)}):")
    log.info(f"  - Файлов в БД: {total_files}")
    log.info(f"  - Общий размер: {_format_bytes(total_expected_bytes)} (без учета уже скачанных)")
    log.info(f"  - Свободно на диске: {_format_bytes(free_bytes)}")

    if free_bytes < total_expected_bytes:
        log.warning("Свободного места на диске меньше, чем суммарный размер файлов!")
        log.warning("Если часть файлов уже была скачана ранее, они будут пропущены.")
        log.warning("Загрузка начнется через 10 секунд... Нажмите Ctrl+C для отмены.")

        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            log.info("Загрузка отменена.")
            raise

    log.info("Инициализация очереди загрузки...")

    # Ограничение одновременных загрузок
    # todo (Проверить на премиуме)
    download_concurrency = 6 if app.me.is_premium else 3
    queue: asyncio.Queue = asyncio.Queue(maxsize=16)
    active: dict[str, int] = {}
    stats = {"success": 0, "skipped": 0, "error": 0}

    # Запуск воркеров
    workers = [
        asyncio.create_task(
            _download_worker(app, download_dir, queue, is_tty, active, stats, download_concurrency)
        )
        for _ in range(download_concurrency)
    ]

    # Producer
    chunk_size = 100
    local_meta = {r["message_id"]: (r["file_name"], r["file_size"]) for r in records}
    all_msg_ids = list(local_meta.keys())
    processed_count = 0

    try:
        for chunk_ids in itertools.batched(all_msg_ids, chunk_size):
            try:
                messages = await app.get_messages(chat_id, list(chunk_ids))
            except Exception as e:
                _clear_tty_line(is_tty)
                log.error(f"Неустранимая ошибка при получении списка сообщений: {e}")
                continue

            if not messages:
                continue

            for msg in messages:
                if not msg or msg.empty:
                    continue
                if not (msg.audio or msg.document):
                    continue

                db_name, db_size = local_meta.get(msg.id, (None, 0))

                current_file_name = None
                if msg.audio and msg.audio.file_name:
                    current_file_name = msg.audio.file_name
                elif msg.document and msg.document.file_name:
                    current_file_name = msg.document.file_name

                if not current_file_name:
                    current_file_name = db_name

                await queue.put((msg, current_file_name, db_size))

            processed_count += len(chunk_ids)
            if processed_count % 500 == 0:
                _clear_tty_line(is_tty)
                log.info(f"--- Обработано метаданных {processed_count}/{total_files} ---")
                _render_download_status(is_tty, active, download_concurrency)

        await queue.join()

    finally:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    _clear_tty_line(is_tty)
    log.info(
        f"\n{'=' * 20}\nСкачивание завершено.\n"
        f"Успешно: {stats['success']}\n"
        f"Пропущено: {stats['skipped']}\n"
        f"Ошибок: {stats['error']}"
    )
