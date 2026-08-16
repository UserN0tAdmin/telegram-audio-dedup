"""Тесты скачивания (dedup.downloads): матчинг существующих файлов и конвейер."""

import asyncio
import datetime
import os
from pathlib import Path

from fakes import FakeClient, make_message
from helpers import CHAT_ID

from dedup.context import get_settings
from dedup.downloads import _download_matches_existing, _download_worker, download_chat_audio


def touch(path: Path, size: int, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_matches_existing_size_within_tolerance(tmp_path):
    target = tmp_path / "a.mp3"
    touch(target, size=10_000, mtime=1000.0)
    assert _download_matches_existing(target, expected_size=10_050, expected_mtime=1000.0) is True
    assert _download_matches_existing(target, expected_size=9_960, expected_mtime=1001.5) is True


def test_mismatch_on_size_or_mtime(tmp_path):
    target = tmp_path / "a.mp3"
    touch(target, size=10_000, mtime=1000.0)
    assert _download_matches_existing(target, expected_size=20_000, expected_mtime=1000.0) is False
    assert _download_matches_existing(target, expected_size=10_000, expected_mtime=1100.0) is False


def test_zero_expected_mtime_skips_time_check(tmp_path):
    target = tmp_path / "old.mp3"
    touch(target, size=1000, mtime=1.0)
    assert _download_matches_existing(target, expected_size=1000, expected_mtime=0) is True


def test_missing_file_never_matches(tmp_path):
    assert _download_matches_existing(tmp_path / "no.mp3", 100, 0) is False


async def test_download_worker_downloads_and_sets_mtime(tmp_path):
    client = FakeClient()
    date = datetime.datetime(2025, 6, 15, 10, 0, 0)
    message = make_message(7, file_name="worker song.mp3", file_size=17, date=date)
    queue = asyncio.Queue()
    queue.put_nowait((message, "worker song.mp3", 17))

    stats = {"success": 0, "skipped": 0, "error": 0}
    task = asyncio.create_task(
        _download_worker(
            client, tmp_path, queue, is_tty=False, active={}, stats=stats, concurrency=1
        )
    )
    await queue.join()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    file = tmp_path / "worker song.mp3"
    assert stats["success"] == 1
    assert file.read_bytes() == client.download_payload
    assert file.stat().st_mtime == date.timestamp()  # дата сообщения сохранена


async def test_download_worker_skips_existing(tmp_path):
    client = FakeClient()
    message = make_message(7, file_name="dup.mp3", file_size=100)
    touch(tmp_path / "dup.mp3", size=100, mtime=message.date.timestamp())

    queue = asyncio.Queue()
    queue.put_nowait((message, "dup.mp3", 100))
    stats = {"success": 0, "skipped": 0, "error": 0}
    task = asyncio.create_task(
        _download_worker(
            client, tmp_path, queue, is_tty=False, active={}, stats=stats, concurrency=1
        )
    )
    await queue.join()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert stats == {"success": 0, "skipped": 1, "error": 0}
    assert "download_media" not in client.calls


async def test_download_worker_names_nameless_files(tmp_path):
    client = FakeClient()
    message = make_message(42, file_name=None, file_size=10)  # имя появится из id
    queue = asyncio.Queue()
    queue.put_nowait((message, None, 10))

    stats = {"success": 0, "skipped": 0, "error": 0}
    task = asyncio.create_task(
        _download_worker(
            client, tmp_path, queue, is_tty=False, active={}, stats=stats, concurrency=1
        )
    )
    await queue.join()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert (tmp_path / "audio_42.mp3").exists()  # фолбэк-имя + расширение по mime


async def test_download_chat_audio_full_pipeline(seeded_db):
    client = FakeClient()
    client.messages = {
        i: make_message(i, file_name=f"chat file {i}.mp3", file_size=1000 * i, uid=f"D{i}")
        for i in range(1, 12)
    }

    await download_chat_audio(client, CHAT_ID)

    download_dir = Path(get_settings().paths.downloads_dir) / str(CHAT_ID)
    files = sorted(download_dir.glob("*.mp3"))
    assert len(files) == 11  # все строки чата скачаны
    # Один запрос чанками по 100 id
    assert len(client.calls["get_messages"]) == 1


async def test_download_chat_audio_empty_chat_logs_warning(fresh_db):
    client = FakeClient()
    await download_chat_audio(client, CHAT_ID)
    assert client.calls == {}  # нет записей в БД — нет и обращений к API
