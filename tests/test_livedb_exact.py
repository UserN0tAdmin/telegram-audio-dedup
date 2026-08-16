"""Интеграция с живой БД: точная группировка против независимого SQL-оракула."""

import pytest
from helpers import chat_row_counts, exact_grouping_oracle, fetch_chat_rows, groups_as_partition

from dedup.duplicates import _group_audios_by_duplicates, get_potential_duplicate_groups

pytestmark = pytest.mark.livedb


def test_all_chats_grouping_equals_oracle(live_db_copy):
    counts = chat_row_counts(live_db_copy)
    assert counts, "в живой БД нет чатов с аудио"

    total_groups = 0
    for chat_id, row_count in counts.items():
        rows = fetch_chat_rows(live_db_copy, chat_id)
        assert len(rows) == row_count

        groups, edge_meta = _group_audios_by_duplicates(rows)
        partition = groups_as_partition(groups)
        assert partition == exact_grouping_oracle(rows), f"расхождение в чате {chat_id}"

        # Инварианты: группы >= 2, дизъюнктны, рёбра только между участниками
        flat = [mid for group in partition for mid in group]
        assert len(flat) == len(set(flat))
        grouped = set(flat)
        for (a, b), info in edge_meta.items():
            assert a in grouped and b in grouped
            assert info.reason in ("uid", "meta")
            assert info.score == 1.0
        total_groups += len(groups)

    # Дубликаты считаются внутри одного чата (кросс-чатовые не группируются),
    # поэтому масштаб скромнее глобальной статистики по БД
    assert total_groups >= 30


async def test_dispatcher_exact_mode_reads_from_database(live_db_copy, live_settings):
    """Полный путь: SELECT из БД -> to_thread -> группировщик (fuzzy выключен)."""
    import aiosqlite

    chat_id = min(chat_row_counts(live_db_copy), key=lambda c: chat_row_counts(live_db_copy)[c])
    expected = exact_grouping_oracle(fetch_chat_rows(live_db_copy, chat_id))

    async with aiosqlite.connect(live_db_copy) as conn:
        conn.row_factory = aiosqlite.Row
        groups, _ = await get_potential_duplicate_groups(chat_id, conn)

    assert groups_as_partition(groups) == expected
