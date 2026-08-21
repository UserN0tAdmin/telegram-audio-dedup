"""Проверка тестовой инфраструктуры: импорты, настройки, изоляция путей."""

from pathlib import Path

from fakes import FakeClient, make_chat

from dedup.cleaning import clean_filename
from dedup.context import get_settings


def test_package_imports_and_settings_are_active():
    assert get_settings().performance.verify_concurrency > 0
    # Все рабочие пути указывают во временный каталог теста
    paths = get_settings().paths
    for value in (paths.db_file, paths.exports_dir, paths.backup_dir, paths.downloads_dir):
        assert Path(value).is_absolute() and "/tmp/" in str(Path(value)), value


def test_pure_function_works_without_io():
    assert clean_filename("Artist - Track.mp3") == "artist track"


def test_fake_client_records_calls():
    client = FakeClient()
    client.chats[-1001] = make_chat(-1001)

    import asyncio

    async def scenario():
        return await client.get_chat(-1001)

    chat = asyncio.run(scenario())
    assert chat.id == -1001
    assert client.calls["get_chat"] == [((-1001,), {})]
