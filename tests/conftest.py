"""Фикстуры и фабрики тестового набора dedup.

Инварианты безопасности (нарушать нельзя):
- все пути из Settings (БД, экспорты, бэкапы, downloads, lock, лог) указывают
  в ``tmp_path`` — реальные файлы проекта не затрагиваются;
- ``session_name`` указывает на несуществующий файл, поэтому чтение файла
  сессии Pyrogram всегда возвращает ``None`` вместо обращения к ``my_account.session``;
- сетевых вызовов нет: вместо клиента Telegram используется ``fakes.FakeClient``.
"""

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import SEED_ROWS, seed_database

from dedup import state
from dedup.context import set_settings
from dedup.db import create_connection, initialize_database
from dedup.settings import (
    ArchiveSettings,
    BackupSettings,
    CoreSettings,
    FuzzySettings,
    IgnoreSettings,
    LoggingSettings,
    PathsSettings,
    PerformanceSettings,
    PyrogramSettings,
    SafetySettings,
    Settings,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIVE_DB_PATH = PROJECT_ROOT / "music_library.sqlite"


@pytest.fixture
def make_settings(tmp_path):
    """Фабрика полного ``Settings`` с переопределениями секций.

    Секции переопределяются словарём полей (через ``dataclasses.replace``)
    либо целиком готовым объектом::

        make_settings(fuzzy={"enable": True}, core=my_core)
    """

    def _make(**overrides) -> Settings:
        sections = {
            "core": CoreSettings(
                chat_list=(),
                dry_run=True,
                report_only=False,
                revoke_private_chats=True,
                keep_priority=(("largest", 0.03), ("oldest", 0.0)),
            ),
            "ignore": IgnoreSettings(raw_ignore_list={}, raw_ignore_regex={}),
            "pyrogram": PyrogramSettings(
                api_id=12345,
                api_hash="0123456789abcdef0123456789abcdef",
                session_name=str(tmp_path / "no_session"),
                proxy_url="",
                sleep_threshold=300,
            ),
            "archive": ArchiveSettings(
                archive_before_delete=False,
                archive_target="me",
                archive_mode="forward",
                archive_hide_sender=False,
                archive_send_header=True,
                abort_delete_on_archive_failure=True,
            ),
            "fuzzy": FuzzySettings(
                enable=False,
                matching_mode="set",
                threshold=0.90,
                max_duration_diff_sec=3,
                name_power=1.0,
                duration_power=3.0,
                size_power=1.0,
                weight_name=0.50,
                weight_duration=0.30,
                weight_size=0.20,
                penalty_numbers_mismatch=0.08,
                use_jaccard_penalty=False,
                use_meta_fuzzy=True,
            ),
            "paths": PathsSettings(
                backup_dir=str(tmp_path / "backup"),
                db_file=str(tmp_path / "test.sqlite"),
                downloads_dir=str(tmp_path / "downloads"),
                exports_dir=str(tmp_path / "exports"),
                log_file=str(tmp_path / "log" / "test.log"),
            ),
            "safety": SafetySettings(
                lock_timeout=0.0,
                min_free_space_mb=0.0,
                dynamic_space_coefficient=1.5,
                dynamic_space_safety_buffer_mb=16.0,
            ),
            "performance": PerformanceSettings(
                sync_batch_size=7000,
                batch_delete_size=100,
                verify_chunk_size=200,
                verify_concurrency=4,
                db_cache_size=-256000,
            ),
            "backup": BackupSettings(
                backup_on_startup=True,
                backup_only_if_changed=True,
                rotate_before_backup=False,
                max_backups=1,
                archive_old_backups=True,
                lzma_preset=7,
                max_archives=4,
            ),
            "logging": LoggingSettings(
                log_level_console="INFO",
                log_level_file="DEBUG",
                log_level_pyrogram="WARNING",
                log_max_bytes=2_097_152,
                log_backup_count=5,
                chat_label_parts=("id",),
            ),
        }
        for name, value in overrides.items():
            if name not in sections:
                raise ValueError(f"Неизвестная секция настроек: {name}")
            sections[name] = replace(sections[name], **value) if isinstance(value, dict) else value
        return Settings(
            **sections,
            lock_file=tmp_path / "test.lock",
            startup_warnings=(),
        )

    return _make


@pytest.fixture(autouse=True)
def _reset_run_state():
    """Очистка мутабельных глобалов state.py и их кэшей вокруг каждого теста."""
    state.IGNORE_MESSAGES.clear()
    state.IGNORE_REGEX.clear()
    state.GLOBAL_IGNORE_REGEX.clear()
    state.CHAT_LABELS.clear()
    state._username_from_session.cache_clear()
    state._id_from_session.cache_clear()
    yield
    state.IGNORE_MESSAGES.clear()
    state.IGNORE_REGEX.clear()
    state.GLOBAL_IGNORE_REGEX.clear()
    state.CHAT_LABELS.clear()
    state._username_from_session.cache_clear()
    state._id_from_session.cache_clear()


@pytest.fixture(autouse=True)
def _default_settings(make_settings):
    """Каждый тест начинает с валидным Settings, указывающим в tmp_path."""
    set_settings(make_settings())
    yield


@pytest.fixture
def configure_settings(make_settings):
    """Строит Settings с переопределениями и сразу делает его активным."""

    def _configure(**overrides) -> Settings:
        settings = make_settings(**overrides)
        set_settings(settings)
        return settings

    return _configure


@pytest.fixture
async def fresh_db(configure_settings):
    """Инициализированная пустая БД и настроенное соединение с ней."""
    configure_settings()
    await initialize_database()
    async with create_connection() as conn:
        yield conn


@pytest.fixture
async def seeded_db(fresh_db):
    """БД со стандартным синтетическим датасетом SEED_ROWS."""
    await seed_database(fresh_db, SEED_ROWS)
    yield fresh_db


@pytest.fixture(scope="session")
def live_db_copy(tmp_path_factory):
    """Копия живой БД во временном каталоге (marкер livedb; скип, если её нет)."""
    if not LIVE_DB_PATH.exists():
        pytest.skip(f"Живая БД не найдена: {LIVE_DB_PATH}")
    dest = tmp_path_factory.mktemp("livedb") / "music_library.sqlite"
    shutil.copy2(LIVE_DB_PATH, dest)
    return dest


@pytest.fixture
def live_settings(live_db_copy, configure_settings):
    """Settings, указывающий db_file на копию живой БД."""
    return configure_settings(paths={"db_file": str(live_db_copy)})
