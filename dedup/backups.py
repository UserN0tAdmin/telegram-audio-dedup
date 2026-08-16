"""Создание, ротация и архивирование резервных копий базы данных."""

import asyncio
import datetime
import lzma
import os
import shutil
from pathlib import Path

import aiosqlite

from .context import get_settings
from .logger import log
from .utils import calculate_file_hash_sync

# Здесь собрана вся логика, связанная с созданием, ротацией, архивированием и удалением резервных копий базы данных.


async def create_database_backup() -> None:
    """Главная управляющая функция для процесса бэкапа."""
    cfg = get_settings()
    source_db_path = Path(cfg.paths.db_file)
    backup_dir = Path(cfg.paths.backup_dir)

    # --- БЛОК 1: Предварительные проверки (Хэш и существование) ---
    if not source_db_path.exists():
        log.debug(f"Файл БД '{source_db_path}' не существует, бэкап не требуется.")
        return

    current_db_hash = ""
    if cfg.backup.backup_only_if_changed:
        log.info("Режим 'бэкап только при изменениях' активен (проверка по хэш-сумме).")
        hash_file_path = backup_dir / ".latest_backup.hash"

        try:
            log.info("Вычисляю хэш-сумму текущей БД (это может занять время для больших файлов)...")
            current_db_hash = await asyncio.to_thread(calculate_file_hash_sync, source_db_path)
            log.debug(f"Текущий хэш БД: {current_db_hash[:40]}...")

            if not hash_file_path.is_file():
                log.info("Файл с хэшем предыдущего бэкапа не найден. Будет создан новый.")
            else:
                stored_hash = await asyncio.to_thread(hash_file_path.read_text, encoding="utf-8")
                log.debug(f"Хэш бэкапа БД: {stored_hash[:40]}...")
                if current_db_hash == stored_hash.strip():
                    log.info(
                        "Хэш-сумма БД не изменилась. Создание новой резервной копии пропущено."
                    )
                    return
                else:
                    log.info(
                        "Обнаружены изменения в БД (хэш-суммы не совпадают). Создание бэкапа необходимо."
                    )
        except Exception as e:
            log.warning(
                f"Не удалось проверить хэш-сумму БД. Ошибка: {e}. Бэкап будет создан для безопасности."
            )
            if not current_db_hash:
                current_db_hash = await asyncio.to_thread(calculate_file_hash_sync, source_db_path)

    if not current_db_hash:
        current_db_hash = await asyncio.to_thread(calculate_file_hash_sync, source_db_path)

    await asyncio.to_thread(os.makedirs, backup_dir, exist_ok=True)

    # --- БЛОК 2: Ротация "ДО" (если включена) ---
    if cfg.backup.rotate_before_backup:
        log.info("Режим экономии места: сначала ротация, потом создание бэкапа.")
        await _perform_rotation(source_db_path, backup_dir)

    # --- БЛОК 3: Создание бэкапа (Единая точка входа) ---
    # Мы вызываем создание здесь один раз, независимо от режима ротации
    new_backup_path = await _perform_backup_creation(source_db_path, backup_dir, current_db_hash)

    # --- БЛОК 4: Обработка результата и Ротация "ПОСЛЕ" ---
    if new_backup_path:
        # 4.1: Специфичная логика для max_backups=0
        # Если пользователь не хочет хранить "горячие" бэкапы, мы должны сразу же заархивировать созданный файл.
        if cfg.backup.max_backups == 0 and cfg.backup.archive_old_backups:
            log.info(
                f"Настройка MAX_BACKUPS=0: Немедленная архивация свежего бэкапа '{new_backup_path.name}'..."
            )
            await _archive_backup_file(new_backup_path)

        # 4.2: Ротация "ПОСЛЕ", если она не была выполнена "ДО"
        if not cfg.backup.rotate_before_backup:
            log.info("Обычный режим: выполнение ротации после успешного создания бэкапа.")
            await _perform_rotation(source_db_path, backup_dir)
    else:
        # Если создание не удалось
        if not cfg.backup.rotate_before_backup:
            log.warning(
                "Создание бэкапа не удалось. Ротация старых копий пропущена для безопасности."
            )


async def _perform_backup_creation(
    source_db_path: Path, backup_dir: Path, db_hash: str
) -> Path | None:
    """Атомарно создает одну новую резервную копию.

    Пишет во временный файл и переименовывает его только после успешного
    завершения; сохраняет хэш-состояние БД для режима
    ``backup_only_if_changed``.

    Args:
        source_db_path: Путь к исходной БД.
        backup_dir: Каталог для бэкапов.
        db_hash: Хэш-сумма текущего состояния БД.

    Returns:
        Путь к созданному бэкапу или ``None`` в случае ошибки.
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    final_backup_path = backup_dir / f"{source_db_path.stem}_{timestamp}.sqlite.bak"
    tmp_backup_path = final_backup_path.with_suffix(final_backup_path.suffix + ".tmp")
    hash_file_path = backup_dir / ".latest_backup.hash"

    try:
        log.info("Создание копии БД...")
        async with aiosqlite.connect(source_db_path) as src_conn:
            async with src_conn.execute("PRAGMA integrity_check;") as cursor:
                result = await cursor.fetchone()
                if not result or result[0].lower() != "ok":
                    log.critical(
                        f"Исходная БД повреждена! integrity_check: "
                        f"'{result[0] if result else 'нет ответа'}'. Бэкап отменен."
                    )
                    return None

            # Пишем во временный файл
            async with aiosqlite.connect(tmp_backup_path) as dest_conn:
                await src_conn.backup(dest_conn)

        # Если все прошло успешно, атомарно переименовываем .tmp в .bak
        await asyncio.to_thread(os.replace, tmp_backup_path, final_backup_path)

        # После успешного создания бэкапа, сохраняем хэш этого состояния
        await asyncio.to_thread(hash_file_path.write_text, db_hash, encoding="utf-8")
        log.debug(f"Сохранен новый хэш БД: {db_hash[:40]}...")

        log.info(f"Резервная копия '{final_backup_path.name}' успешно создана.")
        return final_backup_path

    except aiosqlite.Error as e:
        log.critical(f"Произошла ошибка базы данных при создании бэкапа: {e}")
        return None
    except OSError as e:
        log.critical(f"Произошла файловая ошибка при создании бэкапа: {e}")
        return None
    except Exception as e:
        log.critical(f"Непредвиденная ошибка при создании резервной копии БД: {e}.")
        return None

    finally:
        # В любом случае, если временный файл остался, удаляем его
        if await asyncio.to_thread(tmp_backup_path.exists):
            try:
                await asyncio.to_thread(os.remove, tmp_backup_path)
                log.warning(f"Удален временный файл бэкапа: {tmp_backup_path.name}")
            except OSError:
                pass


async def _perform_rotation(source_db_path: Path, backup_dir: Path) -> None:
    """Выполняет ротацию бэкапов и архивов согласно настройкам.

    Args:
        source_db_path: Путь к исходной БД (для выделения её бэкапов по имени).
        backup_dir: Каталог с бэкапами.
    """
    db_stem = source_db_path.stem
    cfg = get_settings().backup

    # --- Ротация "горячих" бэкапов (.bak) ---
    # [ВАЖНО] Разрешаем вход даже если MAX_BACKUPS == 0, чтобы удалить "зависшие" файлы
    if cfg.max_backups >= 0:
        try:
            # Если лимит 0, то целевое количество файлов = 0.
            # Если лимит > 0, то вычисляем как обычно.
            if cfg.max_backups == 0:
                target_hot_backups = 0
            else:
                # Если ротация ДО создания, мы должны оставить место под 1 новый (MAX - 1).
                # Если ПОСЛЕ, то мы уже создали, значит храним ровно MAX.
                target_hot_backups = (
                    cfg.max_backups - 1 if cfg.rotate_before_backup else cfg.max_backups
                )

            # Защита от отрицательных чисел (на всякий случай)
            if target_hot_backups < 0:
                target_hot_backups = 0

            backups = await asyncio.to_thread(
                lambda: sorted(backup_dir.glob(f"{db_stem}_*.sqlite.bak"))
            )

            num_to_process = len(backups) - target_hot_backups

            if num_to_process > 0:
                log.info(
                    f"Ротация .bak: найдено {len(backups)}, лимит {target_hot_backups}. Обработка {num_to_process} лишних файлов..."
                )

                # Сортировка glob обычно по имени (а там дата), так что самые старые в начале списка.
                # Удаляем/Архивируем самые старые.
                for backup_path in backups[:num_to_process]:
                    if cfg.archive_old_backups:
                        await _archive_backup_file(backup_path)
                    else:
                        log.debug(f"Удаляю старый бэкап: {backup_path.name}")
                        await asyncio.to_thread(os.remove, backup_path)
        except Exception as e:
            log.error(f"Ошибка при ротации бэкапов: {e}")

    # --- Ротация архивов (.xz) ---
    if cfg.max_archives > 0 and cfg.archive_old_backups:
        try:
            archives = await asyncio.to_thread(
                lambda: sorted(backup_dir.glob(f"{db_stem}_*.sqlite.bak.xz"))
            )
            num_to_delete = len(archives) - cfg.max_archives
            if num_to_delete > 0:
                log.info(
                    f"Найдено {len(archives)} архивов (лимит: {cfg.max_archives}). Удаляю {num_to_delete} самых старых..."
                )
                for archive_path in archives[:num_to_delete]:
                    await asyncio.to_thread(os.remove, archive_path)
                    log.debug(f"Удален старый архив: {archive_path.name}")
        except Exception as e:
            log.error(f"Ошибка при ротации архивов: {e}")


async def _archive_backup_file(backup_path: Path) -> None:
    """Атомарно сжимает один файл бэкапа и удаляет исходник.

    Args:
        backup_path: Путь к файлу бэкапа (``*.sqlite.bak``).
    """
    archive_path = backup_path.with_suffix(backup_path.suffix + ".xz")
    tmp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    try:
        log.info(f"Архивирую: {backup_path.name}...")
        await asyncio.to_thread(
            _compress_file_sync, backup_path, tmp_path, get_settings().backup.lzma_preset
        )
        await asyncio.to_thread(os.replace, tmp_path, archive_path)
        await asyncio.to_thread(os.remove, backup_path)
        log.info(f" -> {archive_path.name}")
    except Exception as e:
        log.error(f"Не удалось заархивировать {backup_path.name}: {e}")
        if tmp_path.exists():
            await asyncio.to_thread(os.remove, tmp_path)


def _compress_file_sync(source_path: Path, dest_path: Path, preset: int) -> None:
    """(СИНХРОННАЯ!) Сжимает файл в lzma-архив с заданным пресетом.

    Args:
        source_path: Исходный файл.
        dest_path: Путь итогового архива.
        preset: Уровень сжатия lzma (0-9).
    """
    with (
        open(source_path, "rb") as f_in,
        lzma.open(dest_path, "wb", preset=preset, check=lzma.CHECK_CRC64) as f_out,
    ):
        shutil.copyfileobj(f_in, f_out, length=1024 * 1024)  # type: ignore [arg-type]
