"""Тесты применения изменений (dedup.apply): фильтры защиты, dry-run, боевой режим."""

import re

from fakes import FakeClient, make_message
from helpers import CHAT_ID

from dedup import state
from dedup.apply import (
    _apply_db_updates,
    _delete_db_records,
    _filter_ignored_ids,
    _get_regex_protected_ids,
    handle_database_changes,
)


async def remaining_ids(conn):
    async with conn.execute(
        "SELECT message_id FROM audios WHERE chat_id = ? ORDER BY message_id", (CHAT_ID,)
    ) as c:
        return [r[0] for r in await c.fetchall()]


async def test_filter_ignored_ids_respects_ignore_list(seeded_db):
    state.IGNORE_MESSAGES[CHAT_ID] = {1, 3}
    result = await _filter_ignored_ids(seeded_db, CHAT_ID, [1, 2, 3, 4])
    assert result == [2, 4]


async def test_filter_ignored_ids_respects_chat_regex(seeded_db):
    # Паттерн без якорей защитил бы и 6 (имя содержит «Fuzzy Band»)
    state.IGNORE_REGEX[CHAT_ID] = [re.compile(r"^Fuzzy Band$")]
    result = await _filter_ignored_ids(seeded_db, CHAT_ID, [5, 6, 7])
    assert result == [6, 7]  # 5 защищён performer'ом


async def test_regex_matches_any_text_field(seeded_db):
    # Один паттерн без якорей бьёт и по имени файла, и по мете
    state.IGNORE_REGEX[CHAT_ID] = [re.compile(r"Fuzzy Band")]
    result = await _filter_ignored_ids(seeded_db, CHAT_ID, [5, 6, 7])
    assert result == [7]


async def test_filter_ignored_ids_respects_global_regex(seeded_db):
    state.GLOBAL_IGNORE_REGEX.append(re.compile(r"Meta Dup"))
    result = await _filter_ignored_ids(seeded_db, CHAT_ID, [3, 4, 7])
    assert result == [7]


async def test_regex_protection_checks_name_performer_title(seeded_db):
    patterns = [re.compile(r"zero duration"), re.compile(r"^Artist$"), re.compile(r"Meta Only")]
    protected = await _get_regex_protected_ids(seeded_db, CHAT_ID, [1, 7, 8, 10], patterns)
    # 1 — performer «Artist», 8 — title «Meta Only», 10 — имя «zero duration.mp3»
    assert protected == {1, 8, 10}


async def test_delete_db_records(seeded_db):
    await _delete_db_records(seeded_db, CHAT_ID, [1, 2])
    assert 1 not in await remaining_ids(seeded_db)
    assert 2 not in await remaining_ids(seeded_db)
    assert 3 in await remaining_ids(seeded_db)


async def test_apply_db_updates(seeded_db):
    updated = make_message(
        1,
        chat_id=CHAT_ID,
        file_name="renamed.mp3",
        file_size=55_000,
        duration=222,
        performer="New Perf",
        title="New Title",
        uid="AAAA-new",
    )
    await _apply_db_updates(seeded_db, CHAT_ID, [updated])
    await seeded_db.commit()

    async with seeded_db.execute("SELECT * FROM audios WHERE message_id = 1") as c:
        row = await c.fetchone()
    assert row["file_unique_id"] == "AAAA-new"
    assert row["file_name"] == "renamed.mp3"
    assert row["duration"] == 222


async def test_handle_database_changes_dry_run_touches_nothing(seeded_db):
    client = FakeClient()
    updated = make_message(3, chat_id=CHAT_ID, uid="BBBB-changed")

    await handle_database_changes(
        client,
        CHAT_ID,
        seeded_db,
        tg_ids=[1, 2],
        db_delete_ids=[7],
        db_update_records=[updated],
    )

    assert client.calls == {}  # ни одного вызова API
    assert await remaining_ids(seeded_db) == list(range(1, 12))  # БД нетронута


async def test_handle_database_changes_deletes_and_archives(seeded_db, configure_settings):
    configure_settings(core={"dry_run": False}, archive={"archive_before_delete": True})
    client = FakeClient()

    await handle_database_changes(
        client,
        CHAT_ID,
        seeded_db,
        tg_ids=[1, 2],
        db_delete_ids=[7],
        db_update_records=[],
        archive_target_id=-1007777000000,
    )

    # Архивация заголовком + батчем forward, затем удаление
    assert "send_message" in client.calls
    assert "forward_messages" in client.calls
    args, _ = client.calls["delete_messages"][0]
    assert args[0] == CHAT_ID
    assert list(args[1]) == [1, 2]
    remaining = await remaining_ids(seeded_db)
    assert 1 not in remaining and 2 not in remaining and 7 not in remaining


async def test_archive_header_disabled_skips_separator(seeded_db, configure_settings):
    configure_settings(
        core={"dry_run": False},
        archive={"archive_before_delete": True, "archive_send_header": False},
    )
    client = FakeClient()

    await handle_database_changes(
        client,
        CHAT_ID,
        seeded_db,
        tg_ids=[1, 2],
        db_delete_ids=[],
        db_update_records=[],
        archive_target_id=-1007777000000,
    )

    assert "send_message" not in client.calls  # заголовок не шлётся
    assert "forward_messages" in client.calls  # архивация и удаление работают
    assert "delete_messages" in client.calls
    remaining = await remaining_ids(seeded_db)
    assert 1 not in remaining and 2 not in remaining


async def test_archive_failure_aborts_delete(seeded_db, configure_settings):
    configure_settings(
        core={"dry_run": False},
        archive={"archive_before_delete": True, "abort_delete_on_archive_failure": True},
    )
    client = FakeClient()

    async def failing_forward(*args, **kwargs):
        raise RuntimeError("архив недоступен")

    client.forward_messages = failing_forward

    await handle_database_changes(
        client,
        CHAT_ID,
        seeded_db,
        tg_ids=[1, 2],
        db_delete_ids=[],
        db_update_records=[],
        archive_target_id=-1007777000000,
    )

    assert "delete_messages" not in client.calls  # удаление заблокировано
    assert 1 in await remaining_ids(seeded_db)  # и БД не тронута


async def test_ignore_list_protects_from_real_delete(seeded_db, configure_settings):
    configure_settings(core={"dry_run": False})
    state.IGNORE_MESSAGES[CHAT_ID] = {2}
    client = FakeClient()

    await handle_database_changes(
        client, CHAT_ID, seeded_db, tg_ids=[1, 2], db_delete_ids=[], db_update_records=[]
    )

    args, _ = client.calls["delete_messages"][0]
    assert list(args[1]) == [1]  # 2 защищён ignore-списком
    assert 2 in await remaining_ids(seeded_db)
