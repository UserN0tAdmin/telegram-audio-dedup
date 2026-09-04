"""Тесты синхронизации истории чата в БД (dedup.sync) с FakeClient."""

import pytest
from fakes import FakeClient, make_message
from helpers import CHAT_ID
from pyrogram.enums import MessagesFilter

from dedup.sync import _flush_audio_batch, sync_messages


def audio_tuples(count):
    return [
        (CHAT_ID, i, f"U{i}", f"song{i}.mp3", 1000 * i, 100 + i, "Perf", "Title")
        for i in range(1, count + 1)
    ]


async def test_flush_audio_batch_inserts_and_ignores_duplicates(fresh_db):
    batch = audio_tuples(3)
    assert await _flush_audio_batch(fresh_db, batch) == 3
    # Повторная вставка того же батча полностью игнорируется (PK)
    assert await _flush_audio_batch(fresh_db, batch) == 0
    async with fresh_db.execute("SELECT COUNT(*) FROM audios") as c:
        assert (await c.fetchone())[0] == 3


async def test_flush_audio_batch_upsert_refreshes_stale_row(fresh_db):
    await fresh_db.execute(
        "INSERT INTO audios VALUES (?, 1, 'OLD', 'old.mp3', 100, 10, 'Old', 'Name')",
        (CHAT_ID,),
    )
    await fresh_db.commit()
    fresh = [(CHAT_ID, 1, "NEW", "new.mp3", 200, 20, "New", "Title")]
    assert await _flush_audio_batch(fresh_db, fresh, upsert=True) == 1
    async with fresh_db.execute(
        "SELECT file_unique_id, file_name, file_size, duration, performer, title"
        " FROM audios WHERE chat_id = ? AND message_id = 1",
        (CHAT_ID,),
    ) as c:
        assert tuple(await c.fetchone()) == ("NEW", "new.mp3", 200, 20, "New", "Title")


def build_history():
    return [
        make_message(1, file_name="song1.mp3", uid="S1", file_size=1000, duration=101),
        make_message(2, file_name="song2.mp3", uid="S2", file_size=2000, duration=102),
        make_message(3, file_name="song3.mp3", uid="S3", file_size=3000, duration=103),
        make_message(4, file_name="song4.mp3", uid="S4", file_size=4000, duration=104),
        make_message(5, kind="none"),  # не аудио — пропускается
        make_message(6, empty=True),  # пустое — пропускается, но id сканируется
    ]


async def test_sync_messages_inserts_rows_and_updates_cursor(fresh_db, configure_settings):
    configure_settings(performance={"sync_batch_size": 2})  # несколько коммитов
    client = FakeClient()
    client.history = build_history()

    await sync_messages(client, CHAT_ID, fresh_db)

    async with fresh_db.execute("SELECT COUNT(*) FROM audios") as c:
        assert (await c.fetchone())[0] == 4
    async with fresh_db.execute(
        "SELECT is_fully_synced, newest_scanned_id FROM chat_sync_state WHERE chat_id = ?",
        (CHAT_ID,),
    ) as c:
        synced, newest = await c.fetchone()
    assert synced == 1
    assert newest == 6  # максимум среди всех просмотренных, включая пропущенные


async def test_sync_messages_incremental_uses_min_id(fresh_db):
    await fresh_db.execute("INSERT INTO chat_sync_state VALUES (?, 1, 3)", (CHAT_ID,))
    await fresh_db.commit()

    client = FakeClient()
    client.history = build_history()
    await sync_messages(client, CHAT_ID, fresh_db)

    # Оба прохода (AUDIO + DOCUMENT) получили min_id от сохранённого курсора
    search_calls = client.calls["search_messages"]
    assert len(search_calls) == 2
    for _, kwargs in search_calls:
        assert kwargs["min_id"] == 3
    assert {kwargs["filter"] for _, kwargs in search_calls} == {
        MessagesFilter.AUDIO,
        MessagesFilter.DOCUMENT,
    }


async def test_sync_messages_rolls_back_and_reraises_on_error(fresh_db):
    client = FakeClient()
    client.history = build_history()

    async def broken_search(chat_id, filter=None, **kwargs):
        yield make_message(1, file_name="partial.mp3", uid="P1")
        raise RuntimeError("сеть отвалилась")

    client.search_messages = broken_search
    with pytest.raises(RuntimeError, match="сеть отвалилась"):
        await sync_messages(client, CHAT_ID, fresh_db)

    async with fresh_db.execute("SELECT COUNT(*) FROM audios") as c:
        assert (await c.fetchone())[0] == 0  # ничего не зафиксировано
    async with fresh_db.execute("SELECT COUNT(*) FROM chat_sync_state") as c:
        assert (await c.fetchone())[0] == 0  # курсор не обновлён


async def test_sync_messages_is_idempotent(fresh_db):
    client = FakeClient()
    client.history = build_history()
    await sync_messages(client, CHAT_ID, fresh_db)
    await sync_messages(client, CHAT_ID, fresh_db)

    async with fresh_db.execute("SELECT COUNT(*) FROM audios") as c:
        assert (await c.fetchone())[0] == 4  # дублей не появилось


async def test_sync_messages_force_ignores_cursor_and_keeps_rows(fresh_db):
    await fresh_db.execute("INSERT INTO chat_sync_state VALUES (?, 1, 3)", (CHAT_ID,))
    # Строка из прошлого прогона: PK (chat_id, message_id) совпадает с message 1
    await fresh_db.execute(
        "INSERT INTO audios VALUES (?, 1, 'S1', 'song1.mp3', 1000, 101, 'Perf', 'Title')",
        (CHAT_ID,),
    )
    await fresh_db.commit()

    client = FakeClient()
    client.history = build_history()
    await sync_messages(client, CHAT_ID, fresh_db, force=True)

    # Перескан шёл без min_id, несмотря на сохранённый курсор
    search_calls = client.calls["search_messages"]
    assert len(search_calls) == 2
    for _, kwargs in search_calls:
        assert "min_id" not in kwargs

    # Существующая строка не задвоилась (INSERT OR IGNORE), курсор пересчитан
    async with fresh_db.execute("SELECT COUNT(*) FROM audios") as c:
        assert (await c.fetchone())[0] == 4
    async with fresh_db.execute(
        "SELECT is_fully_synced, newest_scanned_id FROM chat_sync_state WHERE chat_id = ?",
        (CHAT_ID,),
    ) as c:
        synced, newest = await c.fetchone()
    assert synced == 1
    assert newest == 6


async def test_sync_messages_force_refreshes_stale_metadata(fresh_db):
    await fresh_db.execute("INSERT INTO chat_sync_state VALUES (?, 1, 3)", (CHAT_ID,))
    # Stale-строка: тот же PK (chat_id, 1), но устаревшая мета
    await fresh_db.execute(
        "INSERT INTO audios VALUES (?, 1, 'OLD', 'old.mp3', 111, 11, 'Old', 'Name')",
        (CHAT_ID,),
    )
    await fresh_db.commit()

    client = FakeClient()
    client.history = build_history()
    await sync_messages(client, CHAT_ID, fresh_db, force=True)

    async with fresh_db.execute(
        "SELECT file_unique_id, file_name, file_size, duration FROM audios"
        " WHERE chat_id = ? AND message_id = 1",
        (CHAT_ID,),
    ) as c:
        assert tuple(await c.fetchone()) == ("S1", "song1.mp3", 1000, 101)
    async with fresh_db.execute("SELECT COUNT(*) FROM audios") as c:
        assert (await c.fetchone())[0] == 4  # обновилась, не задвоилась


async def test_sync_messages_force_on_virgin_database_is_full_scan(fresh_db):
    client = FakeClient()
    client.history = build_history()
    await sync_messages(client, CHAT_ID, fresh_db, force=True)

    async with fresh_db.execute("SELECT COUNT(*) FROM audios") as c:
        assert (await c.fetchone())[0] == 4
    for _, kwargs in client.calls["search_messages"]:
        assert "min_id" not in kwargs
