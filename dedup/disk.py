"""Проверка свободного дискового пространства (статическая и динамическая стратегии)."""

import asyncio
import os
import shutil
from pathlib import Path

from .config import (
    BACKUP_DIR,
    BACKUP_ON_STARTUP,
    DB_FILE,
    DYNAMIC_SPACE_COEFFICIENT,
    DYNAMIC_SPACE_SAFETY_BUFFER_MB,
    MIN_FREE_SPACE_MB,
)
from .logger import log
from .utils import _format_bytes, _get_existing_parent, _get_size_safely

# Блок, отвечающий исключительно за проверку наличия достаточного свободного места на диске. Включает обе стратегии проверки (статическую и динамическую).


async def check_disk_space() -> bool:
    """Функция-диспетчер для проверки свободного места на диске.

    Выбирает стратегию по конфигу: ``MIN_FREE_SPACE_MB > 0`` — статическая
    проверка (фиксированный лимит), иначе динамическая (расчёт от размера БД).

    Returns:
        ``True``, если места достаточно для продолжения работы.
    """
    if MIN_FREE_SPACE_MB > 0:
        log.info("Выполняется СТАТИЧЕСКАЯ проверка свободного места...")
        return await asyncio.to_thread(
            _check_static_disk_space, Path(BACKUP_DIR), MIN_FREE_SPACE_MB
        )

    log.info("Выполняется ДИНАМИЧЕСКАЯ проверка свободного места...")
    return await _check_dynamic_disk_space()


def _check_static_disk_space(path_to_check: Path, required_mb: float) -> bool:
    """Проверяет наличие достаточного статического количества свободного места.

    Args:
        path_to_check: Путь, на разделе которого проверяется свободное место.
        required_mb: Требуемое количество свободного места в МБ.

    Returns:
        ``True``, если свободного места не меньше требуемого.
    """
    try:
        target_path = _get_existing_parent(path_to_check)
        if not target_path.exists():
            log.critical(
                f"Не удалось найти существующий путь для '{path_to_check}'. Проверьте, смонтирован ли диск."
            )
            return False

        _, _, free_bytes = shutil.disk_usage(target_path)
        log.info(
            f"Доступно на разделе '{target_path}': {_format_bytes(free_bytes)}. Требуется: {required_mb:.2f} МБ."
        )
        if free_bytes < (required_mb * 1024**2):
            log.critical("Недостаточно свободного места!")
            return False
        return True
    except Exception as e:
        log.critical(f"Не удалось проверить свободное место на диске. Ошибка: {e}")
        return False


async def _check_dynamic_disk_space() -> bool:
    """Выполняет динамический расчёт необходимого свободного места.

    Оценивает потребность по размеру БД и бэкапов с учётом коэффициента
    роста и буфера безопасности из конфига.

    Returns:
        ``True``, если расчётная потребность удовлетворена.
    """
    try:
        db_size, backups_size = await asyncio.to_thread(_scan_project_files_sync)

        if db_size == 0 and not Path(BACKUP_DIR).exists():
            log.info("Проект еще не содержит данных. Проверка свободного места не требуется.")
            return True

        safety_buffer_bytes = DYNAMIC_SPACE_SAFETY_BUFFER_MB * 1024 * 1024

        if BACKUP_ON_STARTUP:
            required_bytes = int(db_size * DYNAMIC_SPACE_COEFFICIENT + safety_buffer_bytes)
            log_reason = "Для создания бэкапа и роста БД"
        else:
            # Используем 20% от "запаса прочности" (coeff-1.0) для оценки роста.
            # Пример: coeff=1.5 (запас 50%) -> (1.5-1)*0.2+1 = 1.1 (запас 10%).
            growth_coefficient = (DYNAMIC_SPACE_COEFFICIENT - 1.0) * 0.2 + 1.0
            required_bytes = int(db_size * growth_coefficient + safety_buffer_bytes / 2)
            log_reason = "Для роста БД во время работы"

        backup_path = Path(BACKUP_DIR)
        target_path = _get_existing_parent(backup_path)
        _, _, free_bytes = await asyncio.to_thread(shutil.disk_usage, target_path)

        log.info(f"Текущий размер файлов проекта: {_format_bytes(db_size + backups_size)}.")
        log.info(f"  - Размер БД (с .wal/.shm): {_format_bytes(db_size)}.")
        log.info(f"  - Размер существующих бэкапов: {_format_bytes(backups_size)}.")
        log.info(f"{log_reason} требуется ~{_format_bytes(required_bytes)} свободного места.")
        log.info(f"Доступно на разделе '{target_path}': {_format_bytes(free_bytes)}.")

        if free_bytes < required_bytes:
            log.critical("Недостаточно свободного места!")
            return False

        return True
    except Exception as e:
        log.critical(
            f"Не удалось выполнить динамическую проверку места. Ошибка: {e}",
            exc_info=True,
        )
        return False


def _scan_project_files_sync() -> tuple[int, int]:
    """(СИНХРОННАЯ!) Безопасно сканирует файлы проекта, возвращая их размеры.

    Returns:
        Кортеж ``(размер_БД_с_wal_shm, размер_бэкапов)`` в байтах.
    """
    db_path = Path(DB_FILE)

    db_main_size = _get_size_safely(db_path)
    db_wal_size = _get_size_safely(db_path.with_name(f"{db_path.name}-wal"))
    db_shm_size = _get_size_safely(db_path.with_name(f"{db_path.name}-shm"))
    total_db_size = db_main_size + db_wal_size + db_shm_size

    total_backup_size = 0
    backup_path = Path(BACKUP_DIR)
    if backup_path.is_dir():
        for dirpath, _, filenames in os.walk(backup_path, followlinks=False):
            for filename in filenames:
                filepath = Path(dirpath) / filename
                total_backup_size += _get_size_safely(filepath)

    return total_db_size, total_backup_size
