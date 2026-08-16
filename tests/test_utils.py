"""Юнит-тесты dedup.utils: форматтеры, sanitize_filename, хэши, пути, umask, IPC-lock."""

import asyncio
import hashlib
import os
import subprocess
import sys
import time

import pytest

from dedup.errors import AlreadyRunningError
from dedup.typedefs import edge_key
from dedup.utils import (
    async_ipc_lock,
    calculate_file_hash_sync,
    format_bytes,
    format_duration,
    get_existing_parent,
    get_size_safely,
    sanitize_filename,
    secure_umask,
)

# Дочерний процесс: захватывает lock, пишет готовность, ждёт разрешения на выход.
_CHILD_HOLDER = """
import sys, time
from pathlib import Path
from fasteners import InterProcessLock

lock = InterProcessLock(sys.argv[1])
ok = lock.acquire(blocking=False)
Path(sys.argv[2]).write_text("ok" if ok else "fail")
deadline = time.monotonic() + 30
while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:
    time.sleep(0.02)
lock.release()
"""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.00 KiB"),
        (1536, "1.50 KiB"),
        (1024**2, "1.00 MiB"),
        (3 * 1024**3, "3.00 GiB"),
        (2 * 1024**4, "2.00 TiB"),
        (-2048, "2.00 KiB"),  # модуль
        (1023.9, "1023 B"),  # целые байты не получают дробную часть
    ],
)
def test_format_bytes(value, expected):
    assert format_bytes(value) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "00:00"),
        (0, "00:00"),
        (-5, "00:00"),
        (5, "00:05"),
        (65, "01:05"),
        (205, "03:25"),
        (3599, "59:59"),
        (3723, "1:02:03"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_sanitize_filename_empty_becomes_fallback():
    assert sanitize_filename("") == "unnamed_file"


def test_sanitize_filename_replaces_forbidden_chars():
    result = sanitize_filename('a<b>:c"d|e?f*g')
    assert result == "a_b__c_d_e_f_g"


def test_sanitize_filename_strips_control_chars_and_dots():
    assert sanitize_filename("na\x01me") == "name"
    assert sanitize_filename("name..") == "name"
    assert sanitize_filename("  spaced  ") == "spaced"


@pytest.mark.parametrize(
    "reserved",
    ["CON", "PRN", "AUX", "NUL", "COM1", "LPT9", "nul.txt", "con.mp3"],
)
def test_sanitize_filename_windows_reserved(reserved):
    result = sanitize_filename(reserved)
    assert result != reserved
    assert result.startswith("_")


def test_sanitize_filename_keeps_unicode():
    assert sanitize_filename("песня.mp3") == "песня.mp3"


def test_sanitize_filename_truncates_to_215_bytes_with_extension():
    long_name = "я" * 200 + ".mp3"  # 400 байт стема + расширение
    result = sanitize_filename(long_name)
    assert len(result.encode("utf-8")) <= 215
    assert result.endswith(".mp3")


def test_sanitize_filename_truncates_without_extension():
    result = sanitize_filename("я" * 200)
    assert len(result.encode("utf-8")) <= 215


def test_edge_key_orders_pair():
    assert edge_key(1, 2) == (1, 2)
    assert edge_key(2, 1) == (1, 2)
    assert edge_key(5, 5) == (5, 5)


def test_calculate_file_hash_sync_matches_hashlib(tmp_path):
    payload = b"deterministic-bytes"
    target = tmp_path / "f.bin"
    target.write_bytes(payload)
    with open(target, "rb") as f:
        expected = hashlib.file_digest(f, "blake2b").hexdigest()
    assert calculate_file_hash_sync(target) == expected


def test_calculate_file_hash_sync_is_deterministic(tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"same")
    b.write_bytes(b"same")
    assert calculate_file_hash_sync(a) == calculate_file_hash_sync(b)


def test_get_existing_parent_returns_nearest_existing(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    (tmp_path / "a").mkdir()
    assert get_existing_parent(deep) == (tmp_path / "a").resolve()


def test_get_existing_parent_returns_existing_path_itself(tmp_path):
    target = tmp_path / "exists"
    target.mkdir()
    assert get_existing_parent(target) == target.resolve()


def test_get_size_safely_variants(tmp_path):
    regular = tmp_path / "f.bin"
    regular.write_bytes(b"12345")
    assert get_size_safely(regular) == 5
    assert get_size_safely(tmp_path / "missing") == 0
    assert get_size_safely(tmp_path) == 0  # каталог
    link = tmp_path / "link"
    link.symlink_to(regular)
    assert get_size_safely(link) == 0  # симлинки игнорируются


def test_secure_umask_restores_original():
    original = os.umask(0o022)
    try:
        with secure_umask(0o077):
            assert os.umask(0o077) == 0o077  # чтение текущей маски
        assert os.umask(0o077) == 0o022  # восстановлено после выхода
    finally:
        os.umask(original)


async def test_ipc_lock_acquire_and_reacquire(tmp_path):
    lock_file = tmp_path / "run.lock"
    async with async_ipc_lock(lock_file, timeout=0):
        pass
    # После освобождения повторный захват в том же процессе успешен
    async with async_ipc_lock(lock_file, timeout=0):
        pass


def test_ipc_lock_conflicts_with_other_process(tmp_path):
    lock_file = tmp_path / "run.lock"
    ready = tmp_path / "ready"
    release = tmp_path / "release"

    holder = subprocess.Popen(
        [sys.executable, "-c", _CHILD_HOLDER, str(lock_file), str(ready), str(release)]
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.read_text() == "ok", "дочерний процесс не смог захватить lock"

        async def try_acquire():
            async with async_ipc_lock(lock_file, timeout=0):
                pass

        with pytest.raises(AlreadyRunningError):
            asyncio.run(try_acquire())
    finally:
        release.touch()
        assert holder.wait(timeout=10) == 0
