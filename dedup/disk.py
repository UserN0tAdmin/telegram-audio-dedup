"""Проверка свободного дискового пространства (статическая и динамическая стратегии)."""

import asyncio
import os
import shutil
from pathlib import Path

from .context import get_settings
from .logger import log
from .utils import format_bytes, get_existing_parent, get_size_safely

# Блок, отвечающий исключительно за проверку наличия достаточного свободного места на диске. Включает обе стратегии проверки (статическую и динамическую).


async def check_disk_space() -> bool:
    """Функция-диспетчер для проверки свободного места на диске.

    Выбирает стратегию по конфигу: ``MIN_FREE_SPACE_MB > 0`` — статическая
    проверка (фиксированный лимит), иначе динамическая (расчёт от размера БД).

    Returns:
        ``True``, если места достаточно для продолжения работы.
    """
    cfg = get_settings()
    if cfg.safety.min_free_space_mb > 0:
        log.info("Выполняется СТАТИЧЕСКАЯ проверка свободного места...")
        return await asyncio.to_thread(
            _check_static_disk_space, Path(cfg.paths.backup_dir), cfg.safety.min_free_space_mb
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
        target_path = get_existing_parent(path_to_check)
        if not target_path.exists():
            log.critical(
                f"Не удалось найти существующий путь для '{path_to_check}'. Проверьте, смонтирован ли диск."
            )
            return False

        _, _, free_bytes = shutil.disk_usage(target_path)
        log.info(
            f"Доступно на разделе '{target_path}': {format_bytes(free_bytes)}. Требуется: {required_mb:.2f} МБ."
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
        cfg = get_settings()
        db_size, backups_size = await asyncio.to_thread(_scan_project_files_sync)

        if db_size == 0 and not Path(cfg.paths.backup_dir).exists():
            log.info("Проект еще не содержит данных. Проверка свободного места не требуется.")
            return True

        safety_buffer_bytes = cfg.safety.dynamic_space_safety_buffer_mb * 1024 * 1024

        if cfg.backup.backup_on_startup:
            required_bytes = int(
                db_size * cfg.safety.dynamic_space_coefficient + safety_buffer_bytes
            )
            log_reason = "Для создания бэкапа и роста БД"
        else:
            # Используем 20% от "запаса прочности" (coeff-1.0) для оценки роста.
            # Пример: coeff=1.5 (запас 50%) -> (1.5-1)*0.2+1 = 1.1 (запас 10%).
            growth_coefficient = (cfg.safety.dynamic_space_coefficient - 1.0) * 0.2 + 1.0
            required_bytes = int(db_size * growth_coefficient + safety_buffer_bytes / 2)
            log_reason = "Для роста БД во время работы"

        backup_path = Path(cfg.paths.backup_dir)
        target_path = get_existing_parent(backup_path)
        _, _, free_bytes = await asyncio.to_thread(shutil.disk_usage, target_path)

        log.info(f"Текущий размер файлов проекта: {format_bytes(db_size + backups_size)}.")
        log.info(f"  - Размер БД (с .wal/.shm): {format_bytes(db_size)}.")
        log.info(f"  - Размер существующих бэкапов: {format_bytes(backups_size)}.")
        log.info(f"{log_reason} требуется ~{format_bytes(required_bytes)} свободного места.")
        log.info(f"Доступно на разделе '{target_path}': {format_bytes(free_bytes)}.")

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
    cfg = get_settings()
    db_path = Path(cfg.paths.db_file)

    db_main_size = get_size_safely(db_path)
    db_wal_size = get_size_safely(db_path.with_name(f"{db_path.name}-wal"))
    db_shm_size = get_size_safely(db_path.with_name(f"{db_path.name}-shm"))
    total_db_size = db_main_size + db_wal_size + db_shm_size

    total_backup_size = 0
    backup_path = Path(cfg.paths.backup_dir)
    if backup_path.is_dir():
        for dirpath, _, filenames in os.walk(backup_path, followlinks=False):
            for filename in filenames:
                filepath = Path(dirpath) / filename
                total_backup_size += get_size_safely(filepath)

    return total_db_size, total_backup_size
