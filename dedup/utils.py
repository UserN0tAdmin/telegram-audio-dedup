"""Системные утилиты: IPC-блокировка, umask, форматирование, хэширование, работа с путями."""

import asyncio
import hashlib
import os
import stat as statmod
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import fasteners

from .config import SESSION_NAME
from .errors import AlreadyRunningError
from .logger import log

LOCK_FILE = Path(f"{SESSION_NAME}.lock")


@asynccontextmanager
async def async_ipc_lock(path: str | Path, timeout: float | None = 0) -> AsyncGenerator[None, None]:
    """Асинхронный контекстный менеджер для межпроцессной блокировки.

    Предотвращает одновременный запуск нескольких копий скрипта.

    Args:
        path: Путь к lock-файлу (например, ``my_script.lock``).
        timeout: Поведение ожидания захвата:
            ``0`` — не блокироваться (мгновенно вернуть результат);
            ``None`` — ждать бесконечно;
            ``> 0.0`` — ждать указанное время в секундах.

    Raises:
        AlreadyRunningError: Если lock-файл не удалось захватить за отведённое время.
    """
    lock = fasteners.InterProcessLock(path)
    blocking = (timeout is None) or (timeout > 0)
    acquired = await asyncio.to_thread(lock.acquire, blocking=blocking, timeout=timeout)

    if not acquired:
        raise AlreadyRunningError(f"Скрипт уже запущен (lock-файл: {path})")
    log.debug(f"Lock-файл ({path}) успешно захвачен.")
    try:
        yield
    finally:
        await asyncio.to_thread(lock.release)
        log.debug(f"Lock-файл ({path}) освобождён.")


@contextmanager
def secure_umask(mask: int = 0o077) -> Generator[None, None, None]:
    """Контекстный менеджер для временной и безопасной установки umask процесса.

    Гарантирует восстановление исходной маски после выхода из блока.

    Args:
        mask: Временная маска прав (например, ``0o077``).
    """
    original_umask = os.umask(mask)
    log.debug(f"Установлена временная umask={oct(mask)} для повышения безопасности.")
    try:
        yield
    finally:
        os.umask(original_umask)
        log.debug("Восстановлена исходная системная umask.")


def _format_bytes(size_bytes: int | float) -> str:
    """Форматирует байты в человекочитаемый вид (B, KiB, MiB, GiB, TiB).

    Args:
        size_bytes: Размер в байтах (берётся по модулю).

    Returns:
        Строка вида ``"12.34 MiB"``.
    """
    size_bytes = abs(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size_bytes < 1024.0:
            return f"{int(size_bytes)} {unit}" if unit == "B" else f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TiB"


def _format_duration(seconds: int | None) -> str:
    """Форматирует секунды в mm:ss (или h:mm:ss для длинных файлов).

    Args:
        seconds: Длительность в секундах; ``None`` или неположительное
            значение дают ``"00:00"``.

    Returns:
        Строка вида ``"03:25"`` или ``"1:02:03"``.
    """
    if seconds is None or seconds <= 0:
        return "00:00"

    seconds = int(seconds)

    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)

    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _sanitize_filename(filename: str) -> str:
    """Очищает имя файла от запрещённых системных символов.

    Сохраняет читаемость, пробелы и unicode (кириллицу, эмодзи). Защищает
    от зарезервированных имён Windows и обрезает длину до безопасной
    по количеству байт UTF-8.

    Args:
        filename: Исходное имя файла.

    Returns:
        Безопасное имя файла; ``"unnamed_file"``, если после очистки
        ничего не осталось.
    """
    MAX_FILENAME_BYTES = 215
    _RESERVED_NAMES = frozenset(
        {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
    )

    if not filename:
        return "unnamed_file"

    forbidden = '<>:"/\\|?*'
    remove = "".join(chr(c) for c in range(32)) + chr(127)
    table = str.maketrans(forbidden, "_" * len(forbidden), remove)

    cleaned = filename.translate(table).strip(" .")

    if not cleaned:
        return "unnamed_file"

    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    if len(cleaned.encode("utf-8")) > MAX_FILENAME_BYTES:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext.encode("utf-8")) <= 20:
            ext_bytes = f".{ext}".encode()
            stem = stem.encode("utf-8")[: MAX_FILENAME_BYTES - len(ext_bytes)].decode(
                "utf-8", errors="ignore"
            )
            cleaned = f"{stem}.{ext}"
        else:
            cleaned = cleaned.encode("utf-8")[:MAX_FILENAME_BYTES].decode("utf-8", errors="ignore")

    return cleaned or "unnamed_file"


def _calculate_file_hash_sync(file_path: Path) -> str:
    """(СИНХРОННАЯ!) Вычисляет хэш-сумму BLAKE2b файла.

    Args:
        file_path: Путь к файлу.

    Returns:
        Hex-строка хэша.
    """
    with open(file_path, "rb") as f:
        return hashlib.file_digest(f, "blake2b").hexdigest()


def _get_existing_parent(path: Path) -> Path:
    """Итеративно находит первый существующий родительский каталог.

    Args:
        path: Путь, который может не существовать.

    Returns:
        Ближайший существующий родитель (или сам путь, если он существует).
    """
    current = path.resolve()
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _get_size_safely(path: Path) -> int:
    """Возвращает размер обычного файла.

    Args:
        path: Путь к файлу.

    Returns:
        Размер в байтах; ``0`` для симлинков, каталогов и при ошибках.
    """
    try:
        st = path.lstat()
        if statmod.S_ISLNK(st.st_mode):
            log.debug(f"Игнорируется симлинк: {path}")
            return 0
        if statmod.S_ISREG(st.st_mode):
            return st.st_size
    except (FileNotFoundError, OSError) as e:
        if not isinstance(e, FileNotFoundError):
            log.debug(f"Не удалось получить размер файла '{path}': {e}")
    return 0
