"""Тесты бэкапов (dedup.backups): создание, хэш-пропуск, ротация, xz-архивы."""

import datetime
import lzma
from pathlib import Path

from dedup import backups as backups_module
from dedup.backups import (
    _archive_backup_file,
    _compress_file_sync,
    _perform_backup_creation,
    _perform_rotation,
    create_database_backup,
)
from dedup.context import get_settings


class FixedDatetime(datetime.datetime):
    """Детерминированные таймстемпы: каждая выдача now() на секунду позже."""

    _tick = 0

    @classmethod
    def now(cls, tz=None):
        cls._tick += 1
        return cls(2026, 1, 1, 12, 0, cls._tick, tzinfo=tz)


def backup_dir() -> Path:
    return Path(get_settings().paths.backup_dir)


def db_path() -> Path:
    return Path(get_settings().paths.db_file)


def bak_files() -> list[Path]:
    return sorted(backup_dir().glob("test_*.sqlite.bak"))


def xz_files() -> list[Path]:
    return sorted(backup_dir().glob("test_*.sqlite.bak.xz"))


async def test_perform_backup_creation(fresh_db):
    backup_dir().mkdir(parents=True, exist_ok=True)
    result = await _perform_backup_creation(db_path(), backup_dir(), "hash-1")
    assert result is not None and result.exists()
    assert (backup_dir() / ".latest_backup.hash").read_text(encoding="utf-8") == "hash-1"


async def test_backup_creation_rejects_corrupted_source(tmp_path):
    corrupted = tmp_path / "corrupt.sqlite"
    corrupted.write_bytes(b"this is not a sqlite file at all")
    result = await _perform_backup_creation(corrupted, tmp_path / "bak", "hash")
    assert result is None


async def test_create_database_backup_flow(fresh_db, configure_settings, monkeypatch):
    # max_backups=5: без ротации легко считать созданные копии
    configure_settings(backup={"max_backups": 5})
    monkeypatch.setattr(backups_module.datetime, "datetime", FixedDatetime)
    FixedDatetime._tick = 0

    await create_database_backup()
    assert len(bak_files()) == 1

    # Не изменилась -> хэш совпадает -> пропуск
    await create_database_backup()
    assert len(bak_files()) == 1

    # Изменили содержимое -> новый бэкап.
    # Вставка уходит в WAL: чекпоинт переносит её в основной файл, хэш меняется.
    await fresh_db.execute("INSERT INTO audios VALUES (-1001, 1, 'U', 'n', 1, 1, NULL, NULL)")
    await fresh_db.commit()
    async with fresh_db.execute("PRAGMA wal_checkpoint(TRUNCATE)") as cursor:
        await cursor.fetchall()
    await create_database_backup()
    assert len(bak_files()) == 2


async def test_rotation_archives_oldest_baks(fresh_db, configure_settings):
    configure_settings(backup={"max_backups": 1, "archive_old_backups": True})
    backup_dir().mkdir(parents=True)
    for name in (
        "test_2020-01-01_00-00-00",
        "test_2021-01-01_00-00-00",
        "test_2022-01-01_00-00-00",
    ):
        (backup_dir() / f"{name}.sqlite.bak").write_bytes(b"bak")

    await _perform_rotation(db_path(), backup_dir())

    # Ротация ПОСЛЕ создания: храним ровно max_backups=1 горячих
    assert len(bak_files()) == 1
    assert bak_files()[0].name == "test_2022-01-01_00-00-00.sqlite.bak"
    # Два старых ушли в архив
    assert len(xz_files()) == 2


async def test_rotation_deletes_when_archiving_disabled(fresh_db, configure_settings):
    configure_settings(backup={"max_backups": 2, "archive_old_backups": False})
    backup_dir().mkdir(parents=True)
    for i in range(4):
        (backup_dir() / f"test_202{i}-01-01_00-00-00.sqlite.bak").write_bytes(b"bak")

    await _perform_rotation(db_path(), backup_dir())
    assert len(bak_files()) == 2
    assert xz_files() == []


async def test_rotation_limits_archives(fresh_db, configure_settings):
    configure_settings(backup={"max_backups": 0, "archive_old_backups": True, "max_archives": 2})
    backup_dir().mkdir(parents=True)
    for i in range(5):
        (backup_dir() / f"test_202{i}-01-01_00-00-00.sqlite.bak.xz").write_bytes(b"x")

    await _perform_rotation(db_path(), backup_dir())
    assert len(xz_files()) == 2  # старые три удалены


async def test_archive_backup_file(tmp_path, configure_settings):
    configure_settings(backup={"lzma_preset": 0})  # быстро
    source = tmp_path / "test_2020-01-01_00-00-00.sqlite.bak"
    payload = b"backup-payload-content"
    source.write_bytes(payload)

    await _archive_backup_file(source)

    archive = tmp_path / "test_2020-01-01_00-00-00.sqlite.bak.xz"
    assert archive.exists()
    assert not source.exists()  # исходник удалён
    with lzma.open(archive, "rb") as f:
        assert f.read() == payload


def test_compress_file_sync_roundtrip(tmp_path):
    source = tmp_path / "s.bin"
    dest = tmp_path / "d.xz"
    payload = b"x" * 100_000
    source.write_bytes(payload)

    _compress_file_sync(source, dest, preset=0)

    with lzma.open(dest, "rb") as f:
        assert f.read() == payload
