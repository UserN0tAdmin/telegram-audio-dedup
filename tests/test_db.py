"""Тесты слоя БД: инициализация, коннект, валидация, ремонт (dedup.db)."""

from pathlib import Path

from helpers import CHAT_ID

from dedup.context import get_settings
from dedup.db import create_connection, initialize_database, repair_database, validate_database
from dedup.typedefs import AudioMeta


async def fetch_all(conn, sql, params=()):
    async with conn.execute(sql, params) as cursor:
        return await cursor.fetchall()


async def test_initialize_creates_schema_and_is_idempotent(fresh_db):
    for _ in range(2):  # повторная инициализация не падает (IF NOT EXISTS)
        await initialize_database()

    tables = {
        r[0] for r in await fetch_all(fresh_db, "SELECT name FROM sqlite_master WHERE type='table'")
    }
    indexes = {
        r[0] for r in await fetch_all(fresh_db, "SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {"audios", "chat_sync_state"} <= tables
    assert {"idx_chat_unique", "idx_chat_meta"} <= indexes

    journal = (await fetch_all(fresh_db, "PRAGMA journal_mode"))[0][0]
    assert str(journal).lower() == "wal"


async def test_create_connection_applies_pragmas_and_row_factory(configure_settings):
    configure_settings(performance={"db_cache_size": -12345})
    await initialize_database()
    async with create_connection() as conn:
        assert (await fetch_all(conn, "PRAGMA synchronous"))[0][0] == 1  # NORMAL
        assert (await fetch_all(conn, "PRAGMA temp_store"))[0][0] == 2  # MEMORY
        assert (await fetch_all(conn, "PRAGMA cache_size"))[0][0] == -12345
        assert conn.row_factory is not None
        await conn.execute(
            "INSERT INTO audios (chat_id, message_id, file_unique_id, file_name)"
            " VALUES (?, ?, ?, ?)",
            (CHAT_ID, 1, "U1", "name.mp3"),
        )
        await conn.commit()  # соединение не в autocommit — фиксируем явно
    async with create_connection() as conn:
        rows = await fetch_all(conn, "SELECT file_name FROM audios WHERE message_id = 1")
        assert rows[0]["file_name"] == "name.mp3"


async def test_validate_database_ok_on_fresh(fresh_db):
    assert await validate_database() is True


async def test_validate_database_fails_on_missing_table(fresh_db):
    await fresh_db.execute("DROP TABLE chat_sync_state")
    await fresh_db.commit()
    assert await validate_database() is False


async def test_validate_database_fails_on_broken_rows(fresh_db):
    await fresh_db.execute(
        "INSERT INTO audios VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (CHAT_ID, 1, "U1", "n", -5, 100, None, None),  # отрицательный размер
    )
    await fresh_db.commit()
    assert await validate_database() is False


async def test_validate_database_negative_cursor_is_warning_only(fresh_db, caplog):
    await fresh_db.execute(
        "INSERT INTO chat_sync_state (chat_id, is_fully_synced, newest_scanned_id) VALUES (?, 1, -10)",
        (CHAT_ID,),
    )
    await fresh_db.commit()
    with caplog.at_level("WARNING"):
        assert await validate_database() is True
    assert "некорректных курсоров" in caplog.text


async def test_repair_database_fixes_and_deletes_broken_rows(fresh_db):
    # Две сломанные записи и одна здоровая
    await fresh_db.executemany(
        "INSERT INTO audios VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (CHAT_ID, 1, "U1", "broken.mp3", 100, -5, None, None),  # отрицательная длительность
            (CHAT_ID, 2, "U2", "gone.mp3", 100, -7, None, None),  # будет удалён (API вернул None)
            (CHAT_ID, 3, "U3", "healthy.mp3", 100, 50, "P", "T"),
        ],
    )
    await fresh_db.execute(
        "INSERT INTO chat_sync_state (chat_id, is_fully_synced, newest_scanned_id) VALUES (?, 1, -42)",
        (CHAT_ID,),
    )
    await fresh_db.commit()

    truth = AudioMeta(
        file_unique_id="U1-fixed",
        file_name="fixed.mp3",
        file_size=200,
        duration=42,
        performer="P",
        title="T",
    )

    async def fake_fetch(chat_id, message_ids):
        assert chat_id == CHAT_ID
        return [truth if mid == 1 else None for mid in message_ids]

    await repair_database(fake_fetch)

    rows = await fetch_all(fresh_db, "SELECT * FROM audios ORDER BY message_id")
    assert [r["message_id"] for r in rows] == [1, 3]
    fixed = rows[0]
    assert fixed["file_unique_id"] == "U1-fixed"
    assert fixed["duration"] == 42

    cursor_row = (await fetch_all(fresh_db, "SELECT newest_scanned_id FROM chat_sync_state"))[0]
    assert cursor_row[0] == 0  # некорректный курсор сброшен


async def test_repair_database_clean_db_is_noop(fresh_db):
    async def unexpected_fetch(chat_id, message_ids):
        raise AssertionError("чистая БД не должна обращаться к API")

    await repair_database(unexpected_fetch)
    assert await validate_database() is True


async def test_db_file_created_at_configured_path(fresh_db):
    db_path = Path(get_settings().paths.db_file)
    assert db_path.exists()
