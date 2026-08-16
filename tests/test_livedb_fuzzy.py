"""Интеграция с живой БД: fuzzy-группировка и golden-числа стабильности.

Golden-файл ``tests/golden/fuzzy_group_counts.json`` фиксирует число групп и
рёбер для каждого небольшого чата при текущих настройках фабрики тестов.
При изменении логики матчера или дефолтов настройки числа разойдутся.

Перегенерация после осознанного изменения: ``REGEN_GOLDEN=1 pytest tests/test_livedb_fuzzy.py``.
"""

import json
import os
from pathlib import Path

import pytest
from helpers import chat_row_counts, fetch_chat_rows, fuzzy_signature

from dedup.context import get_settings
from dedup.fuzzy import group_audios_fuzzy_optimized

pytestmark = pytest.mark.livedb

GOLDEN_FILE = Path(__file__).parent / "golden" / "fuzzy_group_counts.json"
# Замер на живой БД: самый большой чат (4501 строка) группируется <1с,
# поэтому гоняем все чаты
MAX_ROWS = 5000


def small_chats(live_db_copy) -> dict[int, int]:
    return {c: n for c, n in chat_row_counts(live_db_copy).items() if n <= MAX_ROWS}


def run_fuzzy(live_db_copy, chat_id: int):
    rows = fetch_chat_rows(live_db_copy, chat_id)
    groups, edge_meta = group_audios_fuzzy_optimized(rows)
    return {"rows": len(rows), "groups": len(groups), "edges": len(edge_meta)}


def load_golden() -> dict:
    if not GOLDEN_FILE.exists():
        pytest.fail(f"golden-файл отсутствует: {GOLDEN_FILE}. Запустите с REGEN_GOLDEN=1")
    return json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))


def save_golden(data: dict) -> None:
    GOLDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_fuzzy_group_counts_match_golden(live_db_copy, live_settings):
    chats = small_chats(live_db_copy)
    assert len(chats) >= 5, "ожидалось несколько небольших чатов в живой БД"

    if os.environ.get("REGEN_GOLDEN"):
        save_golden(
            {
                "settings": fuzzy_signature(get_settings().fuzzy),
                "chats": {str(c): run_fuzzy(live_db_copy, c) for c in chats},
            }
        )
        pytest.skip("golden-файл перегенерирован; запустите тест без REGEN_GOLDEN")

    golden = load_golden()
    assert golden["settings"] == fuzzy_signature(get_settings().fuzzy), (
        "настройки fuzzy в фабрике тестов изменились — перегенерируйте golden (REGEN_GOLDEN=1)"
    )

    for chat_id in chats:
        actual = run_fuzzy(live_db_copy, chat_id)
        expected = golden["chats"].get(str(chat_id))
        assert expected is not None, f"чат {chat_id} отсутствует в golden-файле"
        assert actual == expected, f"расхождение fuzzy-статистики в чате {chat_id}"


def test_fuzzy_superset_of_exact_on_live_data(live_db_copy, live_settings):
    """Fuzzy не должен терять точные дубликаты (uid/мета входят в граф всегда)."""
    from helpers import exact_grouping_oracle, groups_as_partition

    for chat_id, _ in list(small_chats(live_db_copy).items())[:3]:
        rows = fetch_chat_rows(live_db_copy, chat_id)
        exact = exact_grouping_oracle(rows)
        fuzzy_groups, _ = group_audios_fuzzy_optimized(rows)
        fuzzy_partition = groups_as_partition(fuzzy_groups)
        # Fuzzy — надмножество: точные группы обязаны найтись и в нём
        assert exact <= fuzzy_partition, f"чат {chat_id}: fuzzy потерял точные группы"
