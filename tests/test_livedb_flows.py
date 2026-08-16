"""Интеграция с живой БД: экспорты, отчёт, валидация — на копии без сети."""

from pathlib import Path

import pytest
from helpers import chat_row_counts, fetch_chat_rows, sql_count

from dedup.context import get_settings
from dedup.db import validate_database
from dedup.exports import export_cleaned_names_to_csv, export_filenames_to_txt
from dedup.reports import create_duplicates_report

pytestmark = pytest.mark.livedb


@pytest.fixture
def small_chat(live_db_copy) -> int:
    return min(chat_row_counts(live_db_copy), key=lambda c: chat_row_counts(live_db_copy)[c])


def exports_root() -> Path:
    return Path(get_settings().paths.exports_dir)


async def test_validate_live_copy(live_settings):
    assert await validate_database() is True


async def test_filenames_export_matches_sql(live_db_copy, live_settings, small_chat):
    await export_filenames_to_txt(small_chat)
    file = next((exports_root() / str(small_chat)).glob("*_filenames.txt"))

    expected = sql_count(
        live_db_copy,
        "SELECT COUNT(*) FROM audios WHERE chat_id = ? AND file_name IS NOT NULL",
        (small_chat,),
    )
    assert len(file.read_text(encoding="utf-8").splitlines()) == expected


async def test_cleaned_names_export_matches_sql(live_db_copy, live_settings, small_chat):
    import csv

    await export_cleaned_names_to_csv(small_chat)
    file = next((exports_root() / str(small_chat)).glob("*_cleaned_names.csv"))

    expected = sql_count(
        live_db_copy,
        "SELECT COUNT(*) FROM audios WHERE chat_id = ? AND file_name IS NOT NULL",
        (small_chat,),
    )
    with open(file, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f, delimiter=";"))
    assert len(rows) - 1 == expected  # минус заголовок


async def test_duplicates_report_on_live_copy(live_db_copy, live_settings, small_chat):
    import aiosqlite
    from helpers import exact_grouping_oracle

    async with aiosqlite.connect(live_db_copy) as conn:
        conn.row_factory = aiosqlite.Row
        await create_duplicates_report(small_chat, conn, ts="2026-01-01_00-00-00")

    file = exports_root() / str(small_chat) / "2026-01-01_00-00-00_report_duplicates.txt"
    assert file.exists()
    content = file.read_text(encoding="utf-8")

    expected_groups = exact_grouping_oracle(fetch_chat_rows(live_db_copy, small_chat))
    assert f"Найдено групп: {len(expected_groups)}" in content
    assert content.count("• [KEEP] ") == len(expected_groups)
