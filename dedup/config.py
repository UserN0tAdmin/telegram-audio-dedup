import configparser
import os
import re
import sys
from typing import Final
from dotenv import load_dotenv

# --- ИНИЦИАЛИЗАЦИЯ И ЧТЕНИЕ ФАЙЛОВ ---

load_dotenv(override=False)

config: Final[configparser.ConfigParser] = configparser.ConfigParser(interpolation=None)
if not config.read('config.cfg', encoding='utf-8'):
    print("ОШИБКА: Файл 'config.cfg' не найден. Пожалуйста, создайте его по примеру.")
    sys.exit(1)

# todo override аргументами командной строки
# --- ФУНКЦИИ-ПОМОЩНИКИ ---

def _get_list(section: str, key: str, fallback: str = "") -> list[str]:
    """Получает значение из конфига и преобразует строку 'val1, val2' в список строк['val1', 'val2']."""
    raw_val = config.get(section, key, fallback=fallback)
    return [x.strip() for x in raw_val.split(',') if x.strip()]


def _get_env_or_str(section: str, key: str, env_var: str, fallback: str = "") -> str:
    """Возвращает значение из переменной окружения, если она задана,
        иначе берёт значение из конфигурационного файла (как строку)."""
    if env_val := os.getenv(env_var):
        return env_val
    return config.get(section, key, fallback=fallback)


def _get_env_or_int(section: str, key: str, env_var: str, fallback: int = 0) -> int:
    """Возвращает значение из переменной окружения (преобразовав в int),
        если она задана и корректна, иначе берёт значение из конфига."""
    if env_val := os.getenv(env_var):
        try:
            return int(env_val)
        except ValueError:
            pass
    return config.getint(section, key, fallback=fallback)


def _parse_ignore_list() -> dict[str, set[int]]:
    """Парсит секцию [ignore_list] из конфига в словарь вида:
    {chat_identifier: {msg_id1, msg_id2, ...}}.

    При обнаружении некорректных данных (не чисел) выводит ошибку и завершает работу.
    """
    result = {}
    if config.has_section('ignore_list'):
        for chat_identifier, msg_ids_str in config.items('ignore_list'):
            parts = [x.strip() for x in msg_ids_str.split(',') if x.strip()]
            valid_ids: set[int] = set()

            for part in parts:
                try:
                    valid_ids.add(int(part))
                except ValueError:
                    print(
                        f"ОШИБКА: В секции [ignore_list] для чата '{chat_identifier}'\n"
                        f"Найдено недопустимое значение: '{part}'. Ожидаются только числа.\n"
                        f"Скрипт остановлен во избежание случайного удаления данных."
                    )
                    sys.exit(1)

            if valid_ids:
                result[chat_identifier] = valid_ids
    return result


def _parse_ignore_regex() -> dict[str, list[re.Pattern[str]]]:
    """Парсит секцию [ignore_regex]: {chat_identifier: [compiled_pattern, ...]}.
    Ключ '*' — глобальные паттерны для всех чатов.
    Некорректный regex останавливает скрипт на старте."""
    result: dict[str, list[re.Pattern[str]]] = {}
    if config.has_section('ignore_regex'):
        for chat_identifier, raw_val in config.items('ignore_regex'):
            patterns: list[re.Pattern[str]] = []
            for line in raw_val.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    patterns.append(re.compile(line))
                except re.error as e:
                    print(
                        f"ОШИБКА: Некорректный regex в [ignore_regex] для '{chat_identifier}':\n"
                        f"'{line}' -> {e}\n"
                        f"Скрипт остановлен во избежание случайного удаления данных."
                    )
                    sys.exit(1)
            if patterns:
                result[chat_identifier] = patterns
    return result


_KEEP_CRITERIA_VALID: Final[frozenset[str]] = frozenset({
    "oldest", "newest", "largest", "smallest",
    "longest", "shortest", "best_meta", "longest_clean_name",
})
# Допуск бессмысленен для уникальных (message_id) и бинарных критериев
_KEEP_NO_TOLERANCE: Final[frozenset[str]] = frozenset({"oldest", "newest", "best_meta"})


def _parse_keep_priority() -> list[tuple[str, float]]:
    """Парсит keep_priority в список (критерий, относительный_допуск).

    Формат элемента: 'name' или 'name ~ N%'. Гарантирует наличие
    уникального tie-break'а (oldest/newest) в конце списка.
    """
    raw_items = _get_list('core', 'keep_priority')
    if not raw_items:
        legacy_newest = config.getboolean('core', 'keep_newest_duplicate', fallback=False)
        return [("newest" if legacy_newest else "oldest", 0.0)]

    result: list[tuple[str, float]] = []
    seen: set[str] = set()
    for item in raw_items:
        name, sep, tol_raw = (p.strip() for p in item.partition('~'))
        name = name.lower()

        if name not in _KEEP_CRITERIA_VALID:
            print(f"ОШИБКА: неизвестный критерий '{name}' в keep_priority.\n"
                  f"Допустимо: {', '.join(sorted(_KEEP_CRITERIA_VALID))}")
            sys.exit(1)
        if name in seen:
            print(f"ОШИБКА: критерий '{name}' указан в keep_priority дважды.")
            sys.exit(1)
        if sep and not tol_raw:
            print(f"ОШИБКА: после '~' у '{name}' не указан допуск. Формат: 'largest ~ 3%'.")
            sys.exit(1)

        tol = 0.0
        if tol_raw:
            if name in _KEEP_NO_TOLERANCE:
                print(f"ОШИБКА: допуск неприменим к критерию '{name}'.")
                sys.exit(1)
            try:
                tol = float(tol_raw.removesuffix('%')) / 100.0
            except ValueError:
                print(f"ОШИБКА: некорректный допуск '{tol_raw}' у '{name}'. Формат: 'largest ~ 3%'.")
                sys.exit(1)
            if not (0.0 <= tol <= 1.0):
                print(f"ОШИБКА: допуск у '{name}' должен быть в диапазоне 0–100%.")
                sys.exit(1)

        result.append((name, tol))
        seen.add(name)

    names = {n for n, _ in result}
    if {"oldest", "newest"} <= names:
        print("ОШИБКА: oldest и newest в keep_priority взаимоисключающи.")
        sys.exit(1)
    if not names & {"oldest", "newest"}:
        result.append(("oldest", 0.0))
    return result



# --- КОНСТАНТЫ ПРИЛОЖЕНИЯ ---

# --- Секция [core] ---
CHAT_LIST: Final[list[str]] = _get_list('core', 'chat_list')
DRY_RUN: Final[bool] = config.getboolean('core', 'dry_run', fallback=True)
REPORT_ONLY: Final[bool] = config.getboolean('core', 'report_only', fallback=False)
REVOKE_PRIVATE_CHATS: Final[bool] = config.getboolean('core', 'revoke_private_chats', fallback=True)
KEEP_PRIORITY: Final[list[tuple[str, float]]] = _parse_keep_priority()

# --- Секция[ignore_list] ---
RAW_IGNORE_LIST: Final[dict[str, set[int]]] = _parse_ignore_list()

# --- Секция [ignore_regex] ---
RAW_IGNORE_REGEX: Final[dict[str, list[re.Pattern[str]]]] = _parse_ignore_regex()

# --- Секция [pyrogram] ---
API_ID: Final[int] = _get_env_or_int('pyrogram', 'api_id', 'TG_API_ID', fallback=0)
API_HASH: Final[str] = _get_env_or_str('pyrogram', 'api_hash', 'TG_API_HASH', fallback='')
SESSION_NAME: Final[str] = config.get('pyrogram', 'session_name', fallback='my_account')
PROXY_URL: Final[str] = config.get('pyrogram', 'proxy_url', fallback='')
SLEEP_THRESHOLD: Final[int] = max(1, config.getint('pyrogram', 'sleep_threshold', fallback=300))

if not API_ID or not API_HASH:
    print("ПРЕДУПРЕЖДЕНИЕ: API_ID или API_HASH не заданы ни в config.cfg, ни .env файле!")

# --- Секция [archive] ---
ARCHIVE_BEFORE_DELETE: Final[bool] = config.getboolean('archive', 'archive_before_delete', fallback=False)
ARCHIVE_TARGET: Final[str] = config.get('archive', 'archive_target', fallback='me').strip()
_archive_mode_raw = config.get('archive', 'archive_mode', fallback='forward').lower().strip()
ARCHIVE_MODE: Final[str] = _archive_mode_raw if _archive_mode_raw in ('forward', 'copy') else 'forward'
ARCHIVE_HIDE_SENDER: Final[bool] = config.getboolean('archive', 'archive_hide_sender', fallback=False)
ABORT_DELETE_ON_ARCHIVE_FAILURE: Final[bool] = config.getboolean(
    'archive', 'abort_delete_on_archive_failure', fallback=True
)

if ARCHIVE_BEFORE_DELETE and not ARCHIVE_TARGET:
    print("ОШИБКА: archive_before_delete=true, но archive_target не задан в [archive].")
    sys.exit(1)

# --- Секция [fuzzy_matching] ---
ENABLE_FUZZY_MATCHING: Final[bool] = config.getboolean('fuzzy_matching', 'enable', fallback=False)
_mode_raw = config.get('fuzzy_matching', 'matching_mode', fallback='set').lower().strip()
FUZZY_MATCHING_MODE: Final[str] = _mode_raw if _mode_raw in ('set', 'sort') else 'sort'
FUZZY_THRESHOLD: Final[float] = config.getfloat('fuzzy_matching', 'threshold', fallback=0.90)
MAX_DURATION_DIFF_SEC: Final[int] = config.getint('fuzzy_matching', 'max_duration_diff_sec', fallback=3)

NAME_POWER: Final[float] = config.getfloat('fuzzy_matching', 'name_power', fallback=1.0)
DURATION_POWER: Final[float] = config.getfloat('fuzzy_matching', 'duration_power', fallback=3.0)
SIZE_POWER: Final[float] = config.getfloat('fuzzy_matching', 'size_power', fallback=1.0)

WEIGHT_NAME: Final[float] = config.getfloat('fuzzy_matching', 'weight_name', fallback=0.50)
WEIGHT_DURATION: Final[float] = config.getfloat('fuzzy_matching', 'weight_duration', fallback=0.30)
WEIGHT_SIZE: Final[float] = config.getfloat('fuzzy_matching', 'weight_size', fallback=0.20)
PENALTY_NUMBERS_MISMATCH: Final[float] = config.getfloat('fuzzy_matching', 'penalty_numbers_mismatch', fallback=0.08)
USE_JACCARD_PENALTY: Final[bool] = config.getboolean('fuzzy_matching', 'use_jaccard_penalty', fallback=False)

USE_META_FUZZY: Final[bool] = config.getboolean('fuzzy_matching', 'use_meta_fuzzy', fallback=True)

if abs((WEIGHT_NAME + WEIGHT_DURATION + WEIGHT_SIZE) - 1.0) > 0.01:
    print("ОШИБКА: Сумма весов в [fuzzy_matching] не равна 1.0! Проверьте config.cfg")
    sys.exit(1)
if MAX_DURATION_DIFF_SEC < 0:
    print("ОШИБКА: max_duration_diff_sec не может быть отрицательным")
    sys.exit(1)
if NAME_POWER <= 0:
    print("ОШИБКА: name_power должен быть больше 0")
    sys.exit(1)
if DURATION_POWER < 0:
    print("ОШИБКА: отрицательный duration_power ломает логику поиска в скрипте")
    sys.exit(1)
if SIZE_POWER < 0:
    print("ОШИБКА: отрицательный size_power ломает логику поиска в скрипте")
    sys.exit(1)

# --- Секция [paths] ---
BACKUP_DIR: Final[str] = config.get('paths', 'backup_dir', fallback='backup')
DB_FILE: Final[str] = config.get('paths', 'db_file', fallback='music_library.sqlite')
DOWNLOADS_DIR: Final[str] = config.get('paths', 'downloads_dir', fallback='downloads')
EXPORTS_DIR: Final[str] = config.get('paths', 'exports_dir', fallback='exports')
LOG_FILE_PATH: Final[str] = config.get('paths', 'log_file', fallback='log/script_activity.log')

# --- Секция [system_safety] ---
_lock_timeout_raw: Final[float] = config.getfloat('system_safety', 'lock_timeout', fallback=0)
LOCK_TIMEOUT: Final[float | None] = None if _lock_timeout_raw < 0 else _lock_timeout_raw
MIN_FREE_SPACE_MB: Final[float] = config.getfloat('system_safety', 'min_free_space_mb', fallback=0.0)
DYNAMIC_SPACE_COEFFICIENT: Final[float] = max(1.1, config.getfloat('system_safety', 'dynamic_space_coefficient',
                                                                   fallback=1.5))
DYNAMIC_SPACE_SAFETY_BUFFER_MB: Final[float] = config.getfloat('system_safety', 'dynamic_space_safety_buffer_mb',
                                                               fallback=16.0)

# --- Секция [performance] ---
SYNC_BATCH_SIZE: Final[int] = config.getint('performance', 'sync_batch_size', fallback=7000)
BATCH_DELETE_SIZE: Final[int] = max(1, min(100, config.getint('performance', 'batch_delete_size', fallback=100)))
VERIFY_CHUNK_SIZE: Final[int] = config.getint('performance', 'verify_chunk_size', fallback=200)
VERIFY_CONCURRENCY: Final[int] = config.getint('performance', 'verify_concurrency', fallback=4)
DB_CACHE_SIZE: Final[int] = config.getint('performance', 'db_cache_size', fallback=-256000)

# --- Секция [backup] ---
BACKUP_ON_STARTUP: Final[bool] = config.getboolean('backup', 'backup_on_startup', fallback=True)
BACKUP_ONLY_IF_CHANGED: Final[bool] = config.getboolean('backup', 'backup_only_if_changed', fallback=True)
ROTATE_BEFORE_BACKUP: Final[bool] = config.getboolean('backup', 'rotate_before_backup', fallback=False)
MAX_BACKUPS: Final[int] = config.getint('backup', 'max_backups', fallback=1)
ARCHIVE_OLD_BACKUPS: Final[bool] = config.getboolean('backup', 'archive_old_backups', fallback=True)
LZMA_PRESET: Final[int] = max(0, min(9, config.getint('backup', 'lzma_preset', fallback=7)))
MAX_ARCHIVES: Final[int] = config.getint('backup', 'max_archives', fallback=4)

# --- Секция [logging] ---
LOG_LEVEL_CONSOLE: Final[str] = config.get('logging', 'log_level_console', fallback='INFO')
LOG_LEVEL_FILE: Final[str] = config.get('logging', 'log_level_file', fallback='DEBUG')
LOG_LEVEL_PYROGRAM: Final[str] = config.get('logging', 'log_level_pyrogram', fallback='WARNING')
LOG_MAX_BYTES: Final[int] = config.getint('logging', 'log_max_bytes', fallback=2097152)
LOG_BACKUP_COUNT: Final[int] = config.getint('logging', 'log_backup_count', fallback=5)
CHAT_LABEL_PARTS: Final[list[str]] = _get_list('logging', 'chat_label_parts', fallback='id')