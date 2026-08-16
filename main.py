"""Поиск и удаление дубликатов аудиофайлов в Telegram-чатах.

Точка входа приложения: загрузка конфигурации, инициализация окружения и
оркестрация полного прогона дедупликации либо подкоманд CLI (``repair``,
``report``, ``download``, ``export``, ``search``). Прикладная логика вынесена
в модули проекта; здесь только порядок её вызова.
"""

import asyncio
import datetime
import sys
from argparse import Namespace
from collections.abc import Callable
from functools import partial
from typing import Any, Final

from pyrogram import Client

from dedup.backups import create_database_backup
from dedup.cli import collect_cli_overrides, parse_arguments
from dedup.context import get_settings, set_settings
from dedup.db import create_connection, initialize_database, repair_database, validate_database
from dedup.disk import check_disk_space
from dedup.downloads import download_chat_audio
from dedup.duplicates import find_and_process_duplicates
from dedup.errors import AlreadyRunningError, ConfigError, IgnoreListResolutionError
from dedup.exports import (
    export_cleaned_meta_to_csv,
    export_cleaned_names_to_csv,
    export_database_to_xlsx,
    export_filenames_to_txt,
    export_filenames_with_url_to_txt,
)
from dedup.logger import log, setup_logger
from dedup.reports import create_duplicates_report
from dedup.search import run_search
from dedup.settings import load_config
from dedup.state import chat_label
from dedup.sync import sync_messages
from dedup.tg import (
    can_process_chat,
    create_telegram_client,
    fetch_audio_meta_chunk,
    populate_ignore_list,
    resolve_and_validate_archive_target,
    resolve_chat_identifiers,
)
from dedup.typedefs import ChatID
from dedup.utils import async_ipc_lock, secure_umask

# Главная управляющая логика: process_single_chat и main выступают "дирижёрами" —
# вызывают функции из других модулей в правильном порядке.


async def process_single_chat(
    app: Client,
    chat_id: ChatID,
    me_id: int,
    args: Namespace,
    run_ts: str | None = None,
    archive_target_id: ChatID | None = None,
) -> None:
    """Полный цикл обработки одного чата.

    Синхронизация, отчёты и удаление дубликатов; ошибки одного чата
    не прерывают обработку остальных.

    Args:
        app: Клиент Telegram.
        chat_id: ID обрабатываемого чата.
        me_id: ID текущего аккаунта.
        args: Аргументы CLI.
        run_ts: Общий таймстемп прогона для имён файлов отчётов.
        archive_target_id: ID архивного чата, если архивация включена.
    """
    if not await can_process_chat(app, chat_id, me_id, args):
        return

    try:
        # NOTE: Подключение пересоздаётся на каждый чат намеренно —
        # изоляция по чатам важнее экономии ~2мс на PRAGMA.
        async with create_connection() as conn:
            await sync_messages(app, chat_id, conn)

            if args.command == "report" or get_settings().core.report_only:
                await create_duplicates_report(chat_id, conn, ts=run_ts)
                return

            async with conn.execute(
                "SELECT is_fully_synced FROM chat_sync_state WHERE chat_id = ?", (chat_id,)
            ) as cursor:
                sync_state = await cursor.fetchone()

            is_fully_synced = sync_state and sync_state[0]

            if is_fully_synced:
                await find_and_process_duplicates(app, chat_id, conn, archive_target_id)
            else:
                log.info(
                    f"Чат {chat_label(chat_id)} еще не полностью синхронизирован. Пропуск этапа очистки дубликатов до завершения синхронизации."
                )

    except Exception as e:
        log.critical(
            f"Произошла невосстановимая ошибка при обработке чата {chat_label(chat_id)}: {e}",
            exc_info=True,
        )


# todo рефакторинг?
# todo поддержка музыки из профиля (отдельная таблица)
async def main(args: Namespace) -> None:
    """Главная управляющая функция.

    Args:
        args: Аргументы CLI, разобранные в ``_bootstrap`` (загруженная
            конфигурация уже учитывает перекрытия из них).
    """
    settings = get_settings()

    # Экспорты: подкоманда -> (функция, финальное сообщение)
    export_actions: Final[dict[str, tuple[Callable[[ChatID], Any], str]]] = {
        "filenames": (export_filenames_to_txt, "Задача экспорта имен файлов завершена."),
        "filenames-url": (
            export_filenames_with_url_to_txt,
            "Задача экспорта имен файлов со ссылками завершена.",
        ),
        "cleaned-names": (
            export_cleaned_names_to_csv,
            "Задача экспорта очищенных имен завершена.",
        ),
        "cleaned-meta": (
            export_cleaned_meta_to_csv,
            "Задача экспорта очищенных метаданных завершена.",
        ),
        "xlsx": (export_database_to_xlsx, "Задача экспорта Excel завершена."),
    }

    if args.command == "export":
        export_func, done_message = export_actions[args.export_command]
        await export_func(args.chat)
        log.info(f"{done_message} Выход.")
        return

    if args.command == "search":
        await run_search(args.query, args.min_score, args.wratio)
        return

    async with async_ipc_lock(settings.lock_file, timeout=settings.safety.lock_timeout):
        if not await check_disk_space():
            log.critical("Работа скрипта прервана из-за недостатка свободного места.")
            return

        if args.command != "repair" and settings.backup.backup_on_startup:
            await create_database_backup()

        app = await create_telegram_client()
        if app is None:
            return

        if args.command == "repair":
            log.info("=" * 15 + "ЗАПУСК В РЕЖИМЕ РЕМОНТА БД" + "=" * 15)
            try:
                async with app:
                    await repair_database(partial(fetch_audio_meta_chunk, app))
            except Exception as e:
                log.critical(f"Произошла критическая ошибка в режиме ремонта: {e}", exc_info=True)
            return

        log.debug(f"\n{'=' * 20}")
        if settings.core.dry_run:
            log.warning("Скрипт запущен в режиме симуляции (dry_run = True).")
        pretty = ", ".join(f"{n} ~{t:.0%}" if t else n for n, t in settings.core.keep_priority)
        log.info(f"Стратегия выбора оригинала: {pretty}")

        await initialize_database()

        if not await validate_database():
            log.critical("Скрипт остановлен из-за критических ошибок валидации БД.")
            return

        async with app:
            me = app.me

            if args.command == "download":
                resolved_ids = await resolve_chat_identifiers(app, [args.chat])
                if not resolved_ids:
                    log.error(f"Не удалось найти чат по идентификатору: {args.chat}")
                    return

                target_chat_id = resolved_ids[0]
                if len(resolved_ids) > 1:
                    log.info("Список обрезан до первого элемента")

                if not await can_process_chat(app, target_chat_id, me.id, args):
                    log.error("Нет доступа к чату или чат не найден.")
                    return

                await download_chat_audio(app, target_chat_id)
                log.info("Работа завершена. Выход.")
                return

            resolved_chat_list = await resolve_chat_identifiers(app, settings.core.chat_list)

            try:
                await populate_ignore_list(app)
            except IgnoreListResolutionError:
                return

            archive_target_id: ChatID | None = None
            if settings.archive.archive_before_delete and not (
                args.command == "report" or settings.core.report_only
            ):
                archive_target_id = await resolve_and_validate_archive_target(app, me.id)
                if archive_target_id is None and not settings.core.dry_run:
                    log.critical(
                        "Архивация включена, но целевой чат недоступен. "
                        "Останавливаюсь, чтобы не удалять без резервной копии."
                    )
                    return
                if archive_target_id in resolved_chat_list:
                    log.warning(
                        f"Архивный чат {archive_target_id} есть в chat_list — "
                        f"на следующем прогоне его содержимое может быть задедуплено."
                    )

            # Единый таймстемп прогона: файлы отчётов всех чатов получат общий
            # префикс, что позволяет собрать их как снапшот одного запуска.
            run_ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

            for chat_id in resolved_chat_list:
                await process_single_chat(
                    app,
                    chat_id,
                    me.id,
                    args,
                    run_ts=run_ts,
                    archive_target_id=archive_target_id,
                )

    log.info("Работа скрипта завершена.")


def _install_event_loop() -> None:
    """Устанавливает быстрый цикл событий под текущую платформу."""
    if sys.platform in ("win32", "cygwin"):
        try:
            import winloop

            winloop.install()
            log.debug(f"winloop установлен как основной цикл событий (Platform: {sys.platform}).")
        except ImportError:
            log.warning(
                "winloop не найден. Рекомендуется 'pip install winloop' для ускорения на Windows."
            )
            log.debug("Используется стандартный цикл событий asyncio.")
    else:
        # Linux, macOS, BSD, и др.
        try:
            import uvloop

            uvloop.install()
            log.debug(f"uvloop установлен как основной цикл событий (Platform: {sys.platform}).")
        except ImportError:
            log.warning("uvloop не найден. Рекомендуется 'pip install uvloop' для ускорения.")
            log.debug("Используется стандартный цикл событий asyncio.")


def _bootstrap() -> None:
    """Синхронная точка входа: аргументы, конфигурация, логирование, запуск."""
    args = parse_arguments()
    try:
        settings = load_config(cli_overrides=collect_cli_overrides(args))
    except ConfigError as e:
        print(f"ОШИБКА КОНФИГУРАЦИИ: {e}", file=sys.stderr)
        sys.exit(2)

    with secure_umask(0o077):
        set_settings(settings)
        setup_logger(settings)
        for warning in settings.startup_warnings:
            log.warning(warning)
        for section, option, old_value, new_value in settings.applied_overrides:
            log.info(
                f"CLI-перекрытие: {section}.{option} = {new_value} (в конфиге: {old_value or '—'})"
            )

        log.info(f"\n\n{('=+' * 60 + '\n') * 2}")
        _install_event_loop()

        try:
            asyncio.run(main(args))
        except AlreadyRunningError as e:
            log.warning(str(e))
            sys.exit(1)
        except Exception as e:
            log.critical(f"Критическая ошибка при выполнении скрипта: {e}", exc_info=True)


if __name__ == "__main__":
    _bootstrap()
