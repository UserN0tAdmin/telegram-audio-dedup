"""Тесты проверки дискового пространства (dedup.disk)."""

from pathlib import Path

from dedup import disk
from dedup.context import get_settings
from dedup.disk import _check_static_disk_space, _scan_project_files_sync, check_disk_space


def patch_free_bytes(monkeypatch, free_bytes: int):
    monkeypatch.setattr(disk.shutil, "disk_usage", lambda path: (0, 0, free_bytes))


def test_static_check_enough(monkeypatch, tmp_path):
    patch_free_bytes(monkeypatch, 200 * 1024**2)
    assert _check_static_disk_space(tmp_path, 100) is True


def test_static_check_not_enough(monkeypatch, tmp_path):
    patch_free_bytes(monkeypatch, 50 * 1024**2)
    assert _check_static_disk_space(tmp_path, 100) is False


async def test_dispatcher_uses_static_when_configured(configure_settings, monkeypatch, tmp_path):
    configure_settings(safety={"min_free_space_mb": 100})
    patch_free_bytes(monkeypatch, 150 * 1024**2)
    assert await check_disk_space() is True
    patch_free_bytes(monkeypatch, 10 * 1024**2)
    assert await check_disk_space() is False


async def test_dynamic_check_empty_project_is_ok(configure_settings, tmp_path):
    # Нет БД и нет каталога бэкапов — проверка тривиально проходит
    configure_settings(safety={"min_free_space_mb": 0})
    assert await check_disk_space() is True


async def test_dynamic_check_computes_from_db_size(configure_settings, monkeypatch, tmp_path):
    configure_settings(safety={"min_free_space_mb": 0})
    db_file = Path(get_settings().paths.db_file)
    db_file.write_bytes(b"x" * (2 * 1024 * 1024))  # БД 2 МиБ
    (db_file.parent / f"{db_file.name}-wal").write_bytes(b"w" * 1024)

    # Требуется ~2 МиБ * 1.5 + 16 МиБ буфера; даём ровно меньше — отказ
    patch_free_bytes(monkeypatch, 18 * 1024 * 1024)
    assert await check_disk_space() is False
    # Даём с запасом — успех
    patch_free_bytes(monkeypatch, 100 * 1024 * 1024)
    assert await check_disk_space() is True


def test_scan_project_files_counts_db_wal_and_backups(configure_settings, tmp_path):
    db_file = Path(get_settings().paths.db_file)
    db_file.write_bytes(b"d" * 1000)
    (db_file.parent / f"{db_file.name}-wal").write_bytes(b"w" * 200)
    (db_file.parent / f"{db_file.name}-shm").write_bytes(b"s" * 50)

    backup_dir = Path(get_settings().paths.backup_dir)
    (backup_dir / "sub").mkdir(parents=True)
    (backup_dir / "sub" / "old.bak").write_bytes(b"b" * 500)

    db_size, backups_size = _scan_project_files_sync()
    assert db_size == 1000 + 200 + 50
    assert backups_size == 500
