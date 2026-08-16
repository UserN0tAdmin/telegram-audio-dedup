"""Загрузка и валидация конфигурации приложения.

Единственная точка чтения ``config.cfg`` и ``.env``: :func:`load_config`
возвращает неизменяемый :class:`Settings`. Побочных эффектов на импорт нет —
модуль можно импортировать в тестах без файла конфигурации.
"""

import configparser
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

from .errors import ConfigError

# Допустимые критерии keep_priority; реестр экстракторов живёт в priority.py
KEEP_CRITERIA_VALID: Final[frozenset[str]] = frozenset(
    {
        "oldest",
        "newest",
        "largest",
        "smallest",
        "longest",
        "shortest",
        "best_meta",
        "longest_clean_name",
    }
)
# Допуск бессмысленен для уникальных (message_id) и бинарных критериев
_KEEP_NO_TOLERANCE: Final[frozenset[str]] = frozenset({"oldest", "newest", "best_meta"})

# Реестр опций для валидации CLI-перекрытий (--set SECTION.OPTION=VALUE):
# секция -> фиксированный набор опций; None — ключи динамические
# ([ignore_list]/[ignore_regex]: ID чатов или '*'). Имена опций, как и в
# самом INI, регистронезависимы; имена секций — чувствительны.
KNOWN_OPTIONS: Final[dict[str, frozenset[str] | None]] = {
    "core": frozenset(
        {
            "chat_list",
            "dry_run",
            "report_only",
            "revoke_private_chats",
            "keep_priority",
            "keep_newest_duplicate",
        }
    ),
    "pyrogram": frozenset({"api_id", "api_hash", "session_name", "proxy_url", "sleep_threshold"}),
    "archive": frozenset(
        {
            "archive_before_delete",
            "archive_target",
            "archive_mode",
            "archive_hide_sender",
            "abort_delete_on_archive_failure",
        }
    ),
    "fuzzy_matching": frozenset(
        {
            "enable",
            "matching_mode",
            "threshold",
            "max_duration_diff_sec",
            "name_power",
            "duration_power",
            "size_power",
            "weight_name",
            "weight_duration",
            "weight_size",
            "penalty_numbers_mismatch",
            "use_jaccard_penalty",
            "use_meta_fuzzy",
        }
    ),
    "paths": frozenset({"backup_dir", "db_file", "downloads_dir", "exports_dir", "log_file"}),
    "system_safety": frozenset(
        {
            "lock_timeout",
            "min_free_space_mb",
            "dynamic_space_coefficient",
            "dynamic_space_safety_buffer_mb",
        }
    ),
    "performance": frozenset(
        {
            "sync_batch_size",
            "batch_delete_size",
            "verify_chunk_size",
            "verify_concurrency",
            "db_cache_size",
        }
    ),
    "backup": frozenset(
        {
            "backup_on_startup",
            "backup_only_if_changed",
            "rotate_before_backup",
            "max_backups",
            "archive_old_backups",
            "lzma_preset",
            "max_archives",
        }
    ),
    "logging": frozenset(
        {
            "log_level_console",
            "log_level_file",
            "log_level_pyrogram",
            "log_max_bytes",
            "log_backup_count",
            "chat_label_parts",
        }
    ),
    "ignore_list": None,
    "ignore_regex": None,
}


# --- Секции конфигурации (отражают секции config.cfg) ---


@dataclass(frozen=True, slots=True)
class CoreSettings:
    """Секция ``[core]``: режим прогона и стратегия выбора оригинала.

    Attributes:
        chat_list: Идентификаторы чатов обработки (ID/@username/ссылки).
        dry_run: Режим симуляции — без реальных изменений в Telegram.
        report_only: Только отчёты, без удаления.
        revoke_private_chats: Отзывать ли сообщения в личных чатах.
        keep_priority: Каскад критериев ``(имя, относительный_допуск)``.
    """

    chat_list: tuple[str, ...]
    dry_run: bool
    report_only: bool
    revoke_private_chats: bool
    keep_priority: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class IgnoreSettings:
    """Секции ``[ignore_list]``/``[ignore_regex]`` в сыром (до резолва) виде.

    Attributes:
        raw_ignore_list: ``{chat_identifier: {msg_id, ...}}``.
        raw_ignore_regex: ``{chat_identifier: [compiled_pattern, ...]}``;
            ключ ``'*'`` — глобальные паттерны.
    """

    raw_ignore_list: dict[str, set[int]]
    raw_ignore_regex: dict[str, list[re.Pattern[str]]]


@dataclass(frozen=True, slots=True)
class PyrogramSettings:
    """Секция ``[pyrogram]``: параметры клиента Telegram.

    Attributes:
        api_id: ID приложения (env ``TG_API_ID`` приоритетнее).
        api_hash: Хэш приложения (env ``TG_API_HASH`` приоритетнее).
        session_name: Имя файла сессии (без расширения).
        proxy_url: Прокси (MTProto-ссылка или обычный URL), ``''`` — нет.
        sleep_threshold: Порог ожидания Pyrogram при флуде (сек).
    """

    api_id: int
    api_hash: str
    session_name: str
    proxy_url: str
    sleep_threshold: int


@dataclass(frozen=True, slots=True)
class ArchiveSettings:
    """Секция ``[archive]``: архивация перед удалением.

    Attributes:
        archive_before_delete: Включена ли архивация.
        archive_target: Куда архивировать (``'me'`` — Избранное).
        archive_mode: ``'forward'`` или ``'copy'``.
        archive_hide_sender: Скрывать автора при forward.
        abort_delete_on_archive_failure: Пропускать удаление, если архивация не удалась.
    """

    archive_before_delete: bool
    archive_target: str
    archive_mode: str
    archive_hide_sender: bool
    abort_delete_on_archive_failure: bool


@dataclass(frozen=True, slots=True)
class FuzzySettings:
    """Секция ``[fuzzy_matching]``: параметры нечёткого сравнения.

    Attributes:
        enable: Включить fuzzy-поиск дубликатов.
        matching_mode: ``'set'`` (множество токенов) или ``'sort'`` (строка).
        threshold: Базовый порог итогового сходства (0..1).
        max_duration_diff_sec: Максимальное расхождение длительности (сек).
        name_power: Степень нормировки текстового вклада.
        duration_power: Степень нормировки вклада длительности.
        size_power: Степень нормировки вклада размера.
        weight_name: Вес текстового сходства.
        weight_duration: Вес сходства длительности.
        weight_size: Вес сходства размера.
        penalty_numbers_mismatch: Штраф за несовпадение числовых токенов.
        use_jaccard_penalty: Дополнительная мера Жаккара для штрафа.
        use_meta_fuzzy: Сравнивать также performer+title.
    """

    enable: bool
    matching_mode: str
    threshold: float
    max_duration_diff_sec: int
    name_power: float
    duration_power: float
    size_power: float
    weight_name: float
    weight_duration: float
    weight_size: float
    penalty_numbers_mismatch: float
    use_jaccard_penalty: bool
    use_meta_fuzzy: bool


@dataclass(frozen=True, slots=True)
class PathsSettings:
    """Секция ``[paths]``: рабочие пути приложения.

    Attributes:
        backup_dir: Каталог бэкапов БД.
        db_file: Файл SQLite-библиотеки.
        downloads_dir: Каталог скачивания аудио.
        exports_dir: Каталог экспортов/отчётов.
        log_file: Файл лога (с подкаталогами).
    """

    backup_dir: str
    db_file: str
    downloads_dir: str
    exports_dir: str
    log_file: str


@dataclass(frozen=True, slots=True)
class SafetySettings:
    """Секция ``[system_safety]``: блокировки и свободное место.

    Attributes:
        lock_timeout: Ожидание lock-файла (сек); ``None`` — бесконечно.
        min_free_space_mb: Статический минимум свободного места (0 — динамический режим).
        dynamic_space_coefficient: Множитель размера БД для динамической оценки.
        dynamic_space_safety_buffer_mb: Абсолютный запас динамической оценки (МБ).
    """

    lock_timeout: float | None
    min_free_space_mb: float
    dynamic_space_coefficient: float
    dynamic_space_safety_buffer_mb: float


@dataclass(frozen=True, slots=True)
class PerformanceSettings:
    """Секция ``[performance]``: размеры батчей и настройки БД.

    Attributes:
        sync_batch_size: Строк на коммит при синхронизации.
        batch_delete_size: Сообщений в батче удаления (1..100).
        verify_chunk_size: Сообщений в запросе верификации.
        verify_concurrency: Параллельные запросы к API.
        db_cache_size: PRAGMA cache_size (байт, отрицательный = КиБ).
    """

    sync_batch_size: int
    batch_delete_size: int
    verify_chunk_size: int
    verify_concurrency: int
    db_cache_size: int


@dataclass(frozen=True, slots=True)
class BackupSettings:
    """Секция ``[backup]``: стратегия бэкапов БД.

    Attributes:
        backup_on_startup: Делать бэкап при запуске.
        backup_only_if_changed: Пропускать, если БД не менялась.
        rotate_before_backup: Ротация до создания нового бэкапа.
        max_backups: Число «горячих» бэкапов (0 — сразу в архив).
        archive_old_backups: Сжимать старые бэкапы в .xz.
        lzma_preset: Уровень сжатия LZMA (0..9).
        max_archives: Лимит файлов в архиве.
    """

    backup_on_startup: bool
    backup_only_if_changed: bool
    rotate_before_backup: bool
    max_backups: int
    archive_old_backups: bool
    lzma_preset: int
    max_archives: int


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    """Секция ``[logging]``: уровни и ротация лога.

    Attributes:
        log_level_console: Уровень для консоли.
        log_level_file: Уровень для файла.
        log_level_pyrogram: Уровень логгера Pyrogram.
        log_max_bytes: Размер файла лога до ротации.
        log_backup_count: Число ротированных файлов.
        chat_label_parts: Состав метки чата: ``title``/``username``/``id``.
    """

    log_level_console: str
    log_level_file: str
    log_level_pyrogram: str
    log_max_bytes: int
    log_backup_count: int
    chat_label_parts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Settings:
    """Полная конфигурация прогона.

    Attributes:
        core: Секция ``[core]``.
        ignore: Секции ``[ignore_list]``/``[ignore_regex]``.
        pyrogram: Секция ``[pyrogram]``.
        archive: Секция ``[archive]``.
        fuzzy: Секция ``[fuzzy_matching]``.
        paths: Секция ``[paths]``.
        safety: Секция ``[system_safety]``.
        performance: Секция ``[performance]``.
        backup: Секция ``[backup]``.
        logging: Секция ``[logging]``.
        lock_file: Путь lock-файла (производное от ``session_name``).
        startup_warnings: Некритические замечания конфигурации.
        applied_overrides: Применённые CLI-перекрытия
            ``(секция, опция, старое_значение, новое_значение)``.
    """

    core: CoreSettings
    ignore: IgnoreSettings
    pyrogram: PyrogramSettings
    archive: ArchiveSettings
    fuzzy: FuzzySettings
    paths: PathsSettings
    safety: SafetySettings
    performance: PerformanceSettings
    backup: BackupSettings
    logging: LoggingSettings
    lock_file: Path
    startup_warnings: tuple[str, ...]
    applied_overrides: tuple[tuple[str, str, str, str], ...] = ()


# --- Помощники чтения (работают по уже прочитанному parser'у) ---


def _get_list(
    config: configparser.ConfigParser, section: str, key: str, fallback: str = ""
) -> list[str]:
    """Значение конфига 'val1, val2' в список без пустых элементов."""
    raw_val = config.get(section, key, fallback=fallback)
    return [x.strip() for x in raw_val.split(",") if x.strip()]


def _get_env_or_str(
    config: configparser.ConfigParser,
    section: str,
    key: str,
    env_var: str,
    fallback: str = "",
    cli_keys: frozenset[tuple[str, str]] = frozenset(),
) -> str:
    """CLI-перекрытие, затем переменная окружения, иначе строка из конфига."""
    if (section, key) in cli_keys:
        return config.get(section, key, fallback=fallback)
    if env_val := os.getenv(env_var):
        return env_val
    return config.get(section, key, fallback=fallback)


def _get_env_or_int(
    config: configparser.ConfigParser,
    section: str,
    key: str,
    env_var: str,
    fallback: int = 0,
    cli_keys: frozenset[tuple[str, str]] = frozenset(),
) -> int:
    """CLI-перекрытие, затем переменная окружения (int), иначе конфиг."""
    if (section, key) in cli_keys:
        return config.getint(section, key, fallback=fallback)
    if env_val := os.getenv(env_var):
        try:
            return int(env_val)
        except ValueError:
            pass
    return config.getint(section, key, fallback=fallback)


def _parse_ignore_list(config: configparser.ConfigParser, errors: list[str]) -> dict[str, set[int]]:
    """Секция ``[ignore_list]`` в ``{chat_identifier: {msg_id, ...}}``.

    Некорректные значения попадают в ``errors`` (не останавливают разбор).
    """
    result: dict[str, set[int]] = {}
    if config.has_section("ignore_list"):
        for chat_identifier, msg_ids_str in config.items("ignore_list"):
            parts = [x.strip() for x in msg_ids_str.split(",") if x.strip()]
            valid_ids: set[int] = set()

            for part in parts:
                try:
                    valid_ids.add(int(part))
                except ValueError:
                    errors.append(
                        f"в секции [ignore_list] для чата '{chat_identifier}' найдено "
                        f"недопустимое значение: '{part}'. Ожидаются только числа."
                    )

            if valid_ids:
                result[chat_identifier] = valid_ids
    return result


def _parse_ignore_regex(
    config: configparser.ConfigParser, errors: list[str]
) -> dict[str, list[re.Pattern[str]]]:
    """Секция ``[ignore_regex]`` в ``{chat_identifier: [pattern, ...]}``.

    Некорректные regex попадают в ``errors`` (не останавливают разбор).
    """
    result: dict[str, list[re.Pattern[str]]] = {}
    if config.has_section("ignore_regex"):
        for chat_identifier, raw_val in config.items("ignore_regex"):
            patterns: list[re.Pattern[str]] = []
            for line in raw_val.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    patterns.append(re.compile(line))
                except re.error as e:
                    errors.append(
                        f"некорректный regex в [ignore_regex] для '{chat_identifier}': '{line}' -> {e}"
                    )
            if patterns:
                result[chat_identifier] = patterns
    return result


def _parse_keep_priority(
    config: configparser.ConfigParser, errors: list[str]
) -> tuple[tuple[str, float], ...]:
    """Секция ``core.keep_priority`` в каскад ``(критерий, допуск)``.

    Формат элемента: ``'name'`` или ``'name ~ N%'``. Гарантирует уникальный
    tie-break (oldest/newest) в конце списка. Проблемы уходит в ``errors``.
    """
    raw_items = _get_list(config, "core", "keep_priority")
    if not raw_items:
        legacy_newest = config.getboolean("core", "keep_newest_duplicate", fallback=False)
        return (("newest" if legacy_newest else "oldest", 0.0),)

    result: list[tuple[str, float]] = []
    seen: set[str] = set()
    for item in raw_items:
        name, sep, tol_raw = (p.strip() for p in item.partition("~"))
        name = name.lower()

        if name not in KEEP_CRITERIA_VALID:
            errors.append(
                f"неизвестный критерий '{name}' в keep_priority. "
                f"Допустимо: {', '.join(sorted(KEEP_CRITERIA_VALID))}"
            )
            continue
        if name in seen:
            errors.append(f"критерий '{name}' указан в keep_priority дважды")
            continue
        if sep and not tol_raw:
            errors.append(f"после '~' у '{name}' не указан допуск. Формат: 'largest ~ 3%'")
            continue

        tol = 0.0
        if tol_raw:
            if name in _KEEP_NO_TOLERANCE:
                errors.append(f"допуск неприменим к критерию '{name}'")
                continue
            try:
                tol = float(tol_raw.removesuffix("%")) / 100.0
            except ValueError:
                errors.append(f"некорректный допуск '{tol_raw}' у '{name}'. Формат: 'largest ~ 3%'")
                continue
            if not (0.0 <= tol <= 1.0):
                errors.append(f"допуск у '{name}' должен быть в диапазоне 0–100%")
                continue

        result.append((name, tol))
        seen.add(name)

    names = {n for n, _ in result}
    if {"oldest", "newest"} <= names:
        errors.append("oldest и newest в keep_priority взаимоисключающи")
    if not names & {"oldest", "newest"}:
        result.append(("oldest", 0.0))
    return tuple(result)


def _apply_cli_overrides(
    config: configparser.ConfigParser,
    cli_overrides: Mapping[tuple[str, str], str] | None,
    errors: list[str],
) -> list[tuple[str, str, str, str]]:
    """Применяет CLI-перекрытия к parser'у до разбора секций.

    Ключи проверяются против :data:`KNOWN_OPTIONS` (проблемы уходят в
    ``errors``); значение пишется как сырая INI-строка, поэтому дальнейший
    разбор, валидация и клампинг едины для файла и CLI.

    Args:
        config: Уже прочитанный parser ``config.cfg``.
        cli_overrides: ``{(секция, опция): значение}`` из командной строки.
        errors: Накопитель проблем конфигурации.

    Returns:
        Список применённых перекрытий ``(секция, опция, старое, новое)``
        для стартового лога.
    """
    applied: list[tuple[str, str, str, str]] = []
    if not cli_overrides:
        return applied

    for (section, option), value in cli_overrides.items():
        option = config.optionxform(option)
        if section not in KNOWN_OPTIONS:
            errors.append(
                f"--set: неизвестная секция '{section}'. "
                f"Допустимые секции: {', '.join(KNOWN_OPTIONS)}"
            )
            continue
        allowed = KNOWN_OPTIONS[section]
        if allowed is not None and option not in allowed:
            errors.append(
                f"--set: неизвестная опция '{section}.{option}'. "
                f"Допустимые опции [{section}]: {', '.join(sorted(allowed))}"
            )
            continue
        if not config.has_section(section):
            config.add_section(section)
        old_value = config.get(section, option, fallback="")
        config.set(section, option, value)
        applied.append((section, option, old_value, value))
    return applied


def load_config(
    path: str | Path | None = None,
    cli_overrides: Mapping[tuple[str, str], str] | None = None,
) -> Settings:
    """Читает ``.env`` и ``config.cfg``, валидирует и собирает :class:`Settings`.

    Args:
        path: Путь к файлу конфигурации; по умолчанию ``config.cfg``
            относительно текущего каталога.
        cli_overrides: Перекрытия из командной строки
            ``{(секция, опция): значение}``; применяются к файлу до разбора,
            приоритет выше ``.env`` и ``config.cfg``.

    Returns:
        Полная неизменяемая конфигурация прогона.

    Raises:
        ConfigError: Файл не найден или содержит некорректные значения
            (все найденные проблемы собираются в одно исключение).
    """
    load_dotenv(override=False)

    config = configparser.ConfigParser(interpolation=None)
    if not config.read(path or "config.cfg", encoding="utf-8"):
        raise ConfigError("Файл 'config.cfg' не найден. Пожалуйста, создайте его по примеру.")

    errors: list[str] = []
    warnings: list[str] = []
    applied_overrides = _apply_cli_overrides(config, cli_overrides, errors)
    cli_keys = frozenset(cli_overrides) if cli_overrides else frozenset()

    # --- [core] + [ignore_list] + [ignore_regex] ---
    core = CoreSettings(
        chat_list=tuple(_get_list(config, "core", "chat_list")),
        dry_run=config.getboolean("core", "dry_run", fallback=True),
        report_only=config.getboolean("core", "report_only", fallback=False),
        revoke_private_chats=config.getboolean("core", "revoke_private_chats", fallback=True),
        keep_priority=_parse_keep_priority(config, errors),
    )
    ignore = IgnoreSettings(
        raw_ignore_list=_parse_ignore_list(config, errors),
        raw_ignore_regex=_parse_ignore_regex(config, errors),
    )

    # --- [pyrogram] ---
    pyrogram = PyrogramSettings(
        api_id=_get_env_or_int(
            config, "pyrogram", "api_id", "TG_API_ID", fallback=0, cli_keys=cli_keys
        ),
        api_hash=_get_env_or_str(
            config, "pyrogram", "api_hash", "TG_API_HASH", fallback="", cli_keys=cli_keys
        ),
        session_name=config.get("pyrogram", "session_name", fallback="my_account"),
        proxy_url=config.get("pyrogram", "proxy_url", fallback=""),
        sleep_threshold=max(1, config.getint("pyrogram", "sleep_threshold", fallback=300)),
    )
    if not pyrogram.api_id or not pyrogram.api_hash:
        warnings.append("API_ID или API_HASH не заданы ни в config.cfg, ни в .env файле!")

    # --- [archive] ---
    archive_mode_raw = config.get("archive", "archive_mode", fallback="forward").lower().strip()
    archive = ArchiveSettings(
        archive_before_delete=config.getboolean("archive", "archive_before_delete", fallback=False),
        archive_target=config.get("archive", "archive_target", fallback="me").strip(),
        archive_mode=archive_mode_raw if archive_mode_raw in ("forward", "copy") else "forward",
        archive_hide_sender=config.getboolean("archive", "archive_hide_sender", fallback=False),
        abort_delete_on_archive_failure=config.getboolean(
            "archive", "abort_delete_on_archive_failure", fallback=True
        ),
    )
    if archive.archive_before_delete and not archive.archive_target:
        errors.append("archive_before_delete=true, но archive_target не задан в [archive].")

    # --- [fuzzy_matching] ---
    mode_raw = config.get("fuzzy_matching", "matching_mode", fallback="set").lower().strip()
    fuzzy = FuzzySettings(
        enable=config.getboolean("fuzzy_matching", "enable", fallback=False),
        matching_mode=mode_raw if mode_raw in ("set", "sort") else "sort",
        threshold=config.getfloat("fuzzy_matching", "threshold", fallback=0.90),
        max_duration_diff_sec=config.getint("fuzzy_matching", "max_duration_diff_sec", fallback=3),
        name_power=config.getfloat("fuzzy_matching", "name_power", fallback=1.0),
        duration_power=config.getfloat("fuzzy_matching", "duration_power", fallback=3.0),
        size_power=config.getfloat("fuzzy_matching", "size_power", fallback=1.0),
        weight_name=config.getfloat("fuzzy_matching", "weight_name", fallback=0.50),
        weight_duration=config.getfloat("fuzzy_matching", "weight_duration", fallback=0.30),
        weight_size=config.getfloat("fuzzy_matching", "weight_size", fallback=0.20),
        penalty_numbers_mismatch=config.getfloat(
            "fuzzy_matching", "penalty_numbers_mismatch", fallback=0.08
        ),
        use_jaccard_penalty=config.getboolean(
            "fuzzy_matching", "use_jaccard_penalty", fallback=False
        ),
        use_meta_fuzzy=config.getboolean("fuzzy_matching", "use_meta_fuzzy", fallback=True),
    )
    if abs((fuzzy.weight_name + fuzzy.weight_duration + fuzzy.weight_size) - 1.0) > 0.01:
        errors.append("Сумма весов в [fuzzy_matching] не равна 1.0! Проверьте config.cfg")
    if fuzzy.max_duration_diff_sec < 0:
        errors.append("max_duration_diff_sec не может быть отрицательным")
    if fuzzy.name_power <= 0:
        errors.append("name_power должен быть больше 0")
    if fuzzy.duration_power < 0:
        errors.append("отрицательный duration_power ломает логику поиска в скрипте")
    if fuzzy.size_power < 0:
        errors.append("отрицательный size_power ломает логику поиска в скрипте")

    # --- [paths] ---
    paths = PathsSettings(
        backup_dir=config.get("paths", "backup_dir", fallback="backup"),
        db_file=config.get("paths", "db_file", fallback="music_library.sqlite"),
        downloads_dir=config.get("paths", "downloads_dir", fallback="downloads"),
        exports_dir=config.get("paths", "exports_dir", fallback="exports"),
        log_file=config.get("paths", "log_file", fallback="log/script_activity.log"),
    )

    # --- [system_safety] ---
    lock_timeout_raw = config.getfloat("system_safety", "lock_timeout", fallback=0)
    safety = SafetySettings(
        lock_timeout=None if lock_timeout_raw < 0 else lock_timeout_raw,
        min_free_space_mb=config.getfloat("system_safety", "min_free_space_mb", fallback=0.0),
        dynamic_space_coefficient=max(
            1.1, config.getfloat("system_safety", "dynamic_space_coefficient", fallback=1.5)
        ),
        dynamic_space_safety_buffer_mb=config.getfloat(
            "system_safety", "dynamic_space_safety_buffer_mb", fallback=16.0
        ),
    )

    # --- [performance] ---
    performance = PerformanceSettings(
        sync_batch_size=config.getint("performance", "sync_batch_size", fallback=7000),
        batch_delete_size=max(
            1, min(100, config.getint("performance", "batch_delete_size", fallback=100))
        ),
        verify_chunk_size=config.getint("performance", "verify_chunk_size", fallback=200),
        verify_concurrency=config.getint("performance", "verify_concurrency", fallback=4),
        db_cache_size=config.getint("performance", "db_cache_size", fallback=-256000),
    )

    # --- [backup] ---
    backup = BackupSettings(
        backup_on_startup=config.getboolean("backup", "backup_on_startup", fallback=True),
        backup_only_if_changed=config.getboolean("backup", "backup_only_if_changed", fallback=True),
        rotate_before_backup=config.getboolean("backup", "rotate_before_backup", fallback=False),
        max_backups=config.getint("backup", "max_backups", fallback=1),
        archive_old_backups=config.getboolean("backup", "archive_old_backups", fallback=True),
        lzma_preset=max(0, min(9, config.getint("backup", "lzma_preset", fallback=7))),
        max_archives=config.getint("backup", "max_archives", fallback=4),
    )

    # --- [logging] ---
    logging_cfg = LoggingSettings(
        log_level_console=config.get("logging", "log_level_console", fallback="INFO"),
        log_level_file=config.get("logging", "log_level_file", fallback="DEBUG"),
        log_level_pyrogram=config.get("logging", "log_level_pyrogram", fallback="WARNING"),
        log_max_bytes=config.getint("logging", "log_max_bytes", fallback=2097152),
        log_backup_count=config.getint("logging", "log_backup_count", fallback=5),
        chat_label_parts=tuple(_get_list(config, "logging", "chat_label_parts", fallback="id")),
    )

    if errors:
        raise ConfigError("Ошибки конфигурации:\n - " + "\n - ".join(errors))

    return Settings(
        core=core,
        ignore=ignore,
        pyrogram=pyrogram,
        archive=archive,
        fuzzy=fuzzy,
        paths=paths,
        safety=safety,
        performance=performance,
        backup=backup,
        logging=logging_cfg,
        lock_file=Path(f"{pyrogram.session_name}.lock"),
        startup_warnings=tuple(warnings),
        applied_overrides=tuple(applied_overrides),
    )
