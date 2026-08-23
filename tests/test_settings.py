"""Тесты load_config: полный разбор, накопление ошибок, env/CLI-приоритет, keep_priority."""

import configparser

import pytest

from dedup import settings as settings_module
from dedup.errors import ConfigError
from dedup.settings import KEEP_CRITERIA_VALID, load_config

VALID_CONFIG = """\
[core]
chat_list = -100111, @user1
dry_run = false
report_only = false
revoke_private_chats = true
keep_priority = largest ~ 3%, best_meta

[pyrogram]
session_name = test_session
proxy_url =
sleep_threshold = 120

[archive]
archive_before_delete = true
archive_target = me
archive_mode = copy
archive_hide_sender = false
archive_send_header = false
abort_delete_on_archive_failure = true

[fuzzy_matching]
enable = true
matching_mode = set
threshold = 0.85
max_duration_diff_sec = 5
name_power = 1.5
duration_power = 2.0
size_power = 1.0
weight_name = 0.6
weight_duration = 0.3
weight_size = 0.1
penalty_numbers_mismatch = 0.05
use_jaccard_penalty = true
use_meta_fuzzy = false

[paths]
backup_dir = bak
db_file = db.sqlite
downloads_dir = dl
exports_dir = exp
log_file = log/x.log

[system_safety]
lock_timeout = -1
min_free_space_mb = 100
dynamic_space_coefficient = 2.0
dynamic_space_safety_buffer_mb = 32

[performance]
sync_batch_size = 100
batch_delete_size = 50
verify_chunk_size = 10
verify_concurrency = 2
db_cache_size = -64000

[backup]
backup_on_startup = false
backup_only_if_changed = false
rotate_before_backup = true
max_backups = 3
archive_old_backups = false
lzma_preset = 5
max_archives = 2

[logging]
log_level_console = WARNING
log_level_file = INFO
log_level_pyrogram = ERROR
log_max_bytes = 1048576
log_backup_count = 2
chat_label_parts = title, username, id
"""


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Отключает реальный .env и переменные окружения API-ключей."""
    monkeypatch.setattr(settings_module, "load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)
    return tmp_path / "config.cfg"


def write_config(path, text=VALID_CONFIG):
    path.write_text(text, encoding="utf-8")
    return path


def test_full_valid_config(isolated_env):
    settings = load_config(write_config(isolated_env))

    assert settings.core.chat_list == ("-100111", "@user1")
    assert settings.core.dry_run is False
    # oldest добавлен автоматически как гарантированный тай-брейк
    assert settings.core.keep_priority == (("largest", 0.03), ("best_meta", 0.0), ("oldest", 0.0))

    assert settings.pyrogram.session_name == "test_session"
    assert settings.pyrogram.sleep_threshold == 120

    assert settings.archive.archive_before_delete is True
    assert settings.archive.archive_mode == "copy"
    assert settings.archive.archive_send_header is False

    assert settings.fuzzy.enable is True
    assert settings.fuzzy.threshold == pytest.approx(0.85)
    assert settings.fuzzy.use_jaccard_penalty is True
    assert settings.fuzzy.use_meta_fuzzy is False

    assert settings.paths.db_file == "db.sqlite"
    assert settings.safety.lock_timeout is None  # -1 -> бесконечное ожидание
    assert settings.safety.min_free_space_mb == pytest.approx(100)
    assert settings.performance.verify_chunk_size == 10
    assert settings.backup.max_backups == 3
    assert settings.backup.lzma_preset == 5
    assert settings.logging.chat_label_parts == ("title", "username", "id")

    assert settings.lock_file == __import__("pathlib").Path("test_session.lock")


def test_missing_file_raises_config_error(isolated_env):
    with pytest.raises(ConfigError, match="не найден"):
        load_config(isolated_env)


def test_errors_accumulate_into_single_exception(isolated_env):
    bad = VALID_CONFIG.replace("weight_name = 0.6", "weight_name = 0.5")  # сумма весов 0.9
    bad = bad.replace(
        "keep_priority = largest ~ 3%, best_meta",
        "keep_priority = biggest, largest, largest ~ , oldest ~ 5%",
    )
    bad += "[ignore_list]\nbadchat = 1, notanumber\n\n[ignore_regex]\nother = [\n"
    path = write_config(isolated_env, bad)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    message = str(excinfo.value)

    # Все проблемы собраны в одно исключение
    assert "Сумма весов" in message
    assert "неизвестный критерий 'biggest'" in message
    assert "дважды" in message
    assert "допуск неприменим" in message
    assert "недопустимое значение: 'notanumber'" in message
    assert "некорректный regex" in message
    assert message.count("\n - ") >= 6


def test_env_overrides_api_credentials(isolated_env, monkeypatch):
    monkeypatch.setenv("TG_API_ID", "999")
    monkeypatch.setenv("TG_API_HASH", "env-hash")
    settings = load_config(write_config(isolated_env))
    assert settings.pyrogram.api_id == 999
    assert settings.pyrogram.api_hash == "env-hash"
    assert settings.startup_warnings == ()


def test_cli_overrides_beat_env_for_api_credentials(isolated_env, monkeypatch):
    monkeypatch.setenv("TG_API_ID", "999")
    monkeypatch.setenv("TG_API_HASH", "env-hash")
    settings = load_config(
        write_config(isolated_env),
        cli_overrides={("pyrogram", "api_id"): "42", ("pyrogram", "api_hash"): "cli-hash"},
    )
    assert settings.pyrogram.api_id == 42
    assert settings.pyrogram.api_hash == "cli-hash"


def test_api_credentials_missing_gives_warning(isolated_env):
    settings = load_config(write_config(isolated_env))
    assert settings.pyrogram.api_id == 0
    assert settings.pyrogram.api_hash == ""
    assert len(settings.startup_warnings) == 1
    assert "API_ID" in settings.startup_warnings[0]


def test_archive_target_required_when_enabled(isolated_env):
    bad = VALID_CONFIG.replace("archive_target = me", "archive_target =")
    with pytest.raises(ConfigError, match="archive_target не задан"):
        load_config(write_config(isolated_env, bad))


def test_legacy_keep_newest_duplicate(isolated_env):
    legacy = VALID_CONFIG.replace(
        "keep_priority = largest ~ 3%, best_meta", "keep_newest_duplicate = true"
    )
    settings = load_config(write_config(isolated_env, legacy))
    assert settings.core.keep_priority == (("newest", 0.0),)


def test_fallbacks_when_config_is_minimal(isolated_env):
    path = isolated_env
    path.write_text("[core]\nchat_list =\n", encoding="utf-8")
    settings = load_config(path)
    assert settings.core.chat_list == ()
    assert settings.core.keep_priority == (("oldest", 0.0),)  # пустой keep_priority + легаси-выкл
    assert settings.fuzzy.threshold == pytest.approx(0.90)
    assert settings.performance.sync_batch_size == 7000


# --- CLI-перекрытия (cli_overrides в load_config) ---


def test_cli_overrides_beat_file(isolated_env):
    settings = load_config(
        write_config(isolated_env),  # dry_run = false в файле
        cli_overrides={("core", "dry_run"): "true"},
    )
    assert settings.core.dry_run is True


def test_cli_overrides_create_missing_section(isolated_env):
    isolated_env.write_text("[core]\nchat_list =\n", encoding="utf-8")
    settings = load_config(isolated_env, cli_overrides={("archive", "archive_mode"): "copy"})
    assert settings.archive.archive_mode == "copy"


def test_cli_overrides_parsed_like_ini_values(isolated_env):
    settings = load_config(
        write_config(isolated_env),
        cli_overrides={
            ("core", "chat_list"): "@new_chat",
            ("core", "keep_priority"): "smallest ~ 2%, newest",
            ("logging", "log_level_console"): "DEBUG",
        },
    )
    assert settings.core.chat_list == ("@new_chat",)
    assert settings.core.keep_priority == (("smallest", 0.02), ("newest", 0.0))
    assert settings.logging.log_level_console == "DEBUG"


def test_cli_overrides_clamped_and_validated_as_from_file(isolated_env):
    path = write_config(isolated_env)
    settings = load_config(path, cli_overrides={("performance", "batch_delete_size"): "500"})
    assert settings.performance.batch_delete_size == 100  # кламп 1..100

    with pytest.raises(ConfigError, match="Сумма весов"):  # как из файла
        load_config(path, cli_overrides={("fuzzy_matching", "weight_name"): "0.9"})


def test_cli_overrides_ignore_sections_accept_any_key(isolated_env):
    settings = load_config(
        write_config(isolated_env),
        cli_overrides={("ignore_list", "-1001234567890"): "4973, 4660"},
    )
    assert settings.ignore.raw_ignore_list == {"-1001234567890": {4973, 4660}}


def test_cli_overrides_recorded_with_old_value(isolated_env):
    settings = load_config(
        write_config(isolated_env),
        cli_overrides={("core", "dry_run"): "true", ("core", "report_only"): "true"},
    )
    assert settings.applied_overrides == (
        ("core", "dry_run", "false", "true"),
        ("core", "report_only", "false", "true"),
    )


def test_no_cli_overrides_leaves_settings_untouched(isolated_env):
    settings = load_config(write_config(isolated_env))
    assert settings.applied_overrides == ()


def test_cli_overrides_unknown_section_rejected(isolated_env):
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            write_config(isolated_env),
            cli_overrides={("coor", "dry_run"): "true"},
        )
    assert "неизвестная секция 'coor'" in str(excinfo.value)
    assert "core" in str(excinfo.value)  # список допустимых прилагается


def test_cli_overrides_unknown_option_rejected(isolated_env):
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            write_config(isolated_env),
            cli_overrides={("core", "dry_runn"): "true"},
        )
    assert "неизвестная опция 'core.dry_runn'" in str(excinfo.value)
    assert "dry_run" in str(excinfo.value)  # список допустимых прилагается


def test_cli_override_option_names_case_insensitive(isolated_env):
    settings = load_config(
        write_config(isolated_env),
        cli_overrides={("core", "DRY_RUN"): "true"},
    )
    assert settings.core.dry_run is True


# --- Юнит-тесты приватного _parse_keep_priority ---


def parse_keep_priority(items, keep_newest=None):
    parser = configparser.ConfigParser(interpolation=None)
    parser.add_section("core")
    parser.set("core", "keep_priority", items)
    if keep_newest is not None:
        parser.set("core", "keep_newest_duplicate", str(keep_newest).lower())
    errors: list[str] = []
    result = settings_module._parse_keep_priority(parser, errors)
    return result, errors


def test_keep_priority_parses_tolerance():
    result, errors = parse_keep_priority("largest ~ 3%, smallest ~ 150%")
    assert errors == ["допуск у 'smallest' должен быть в диапазоне 0–100%"]
    assert result == (("largest", 0.03), ("oldest", 0.0))  # тай-брейк дописан


def test_keep_priority_bare_tilde_rejected():
    result, errors = parse_keep_priority("smallest ~")
    assert result == (("oldest", 0.0),)  # сам критерий отброшен
    assert any("не указан допуск" in e for e in errors)


def test_keep_priority_rejects_unknown_duplicate_and_tolerance_on_oldest():
    result, errors = parse_keep_priority("biggest, largest, largest, oldest ~ 5%")
    assert ("largest", 0.0) in result
    assert len(errors) == 3  # unknown, duplicate, tolerance on oldest


def test_keep_priority_oldest_and_newest_are_exclusive():
    result, errors = parse_keep_priority("oldest, newest")
    assert result == (("oldest", 0.0), ("newest", 0.0))
    assert any("взаимоисключающи" in e for e in errors)


def test_keep_priority_appends_oldest_tiebreak():
    result, errors = parse_keep_priority("largest")
    assert errors == []
    assert result[-1] == ("oldest", 0.0)


def test_keep_criteria_registry_consistency():
    # Реестр экстракторов priority.py обязан соответствовать валидатору конфига
    from dedup.priority import _KEEP_CRITERIA

    assert set(_KEEP_CRITERIA) == set(KEEP_CRITERIA_VALID)
