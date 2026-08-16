"""Тесты каскада keep_priority (dedup.priority.order_group_by_keep_priority)."""

import pytest
from helpers import make_row

from dedup.priority import order_group_by_keep_priority


def row(mid, *, size=0, dur=0, name=None, performer=None, title=None):
    return make_row(mid, name, size, dur, performer=performer, title=title)


@pytest.fixture
def ordered(configure_settings):
    def _ordered(rows, criteria):
        configure_settings(core={"keep_priority": tuple(criteria)})
        return [r["message_id"] for r in order_group_by_keep_priority(rows)]

    return _ordered


def test_largest(ordered):
    rows = [row(1, size=100), row(2, size=200), row(3, size=150)]
    assert ordered(rows, [("largest", 0.0)]) == [2, 3, 1]


def test_smallest(ordered):
    rows = [row(1, size=100), row(2, size=200), row(3, size=150)]
    assert ordered(rows, [("smallest", 0.0)]) == [1, 3, 2]


def test_longest_and_shortest(ordered):
    rows = [row(1, dur=100), row(2, dur=300), row(3, dur=200)]
    assert ordered(rows, [("longest", 0.0)]) == [2, 3, 1]
    assert ordered(rows, [("shortest", 0.0)]) == [1, 3, 2]


def test_oldest_and_newest(ordered):
    rows = [row(3), row(1), row(2)]
    assert ordered(rows, [("oldest", 0.0)]) == [1, 2, 3]
    assert ordered(rows, [("newest", 0.0)]) == [3, 2, 1]


def test_best_meta_scores_performer_and_title(ordered):
    rows = [
        row(1),  # вообще без меты
        row(2, performer="P"),  # только исполнитель
        row(3, performer="P", title="T"),  # полная мета
    ]
    assert ordered(rows, [("best_meta", 0.0), ("oldest", 0.0)]) == [3, 2, 1]


def test_best_meta_title_equal_to_filename_is_garbage(ordered):
    # title, совпадающий с именем файла, не считается метаданными
    rows = [
        row(1, name="Same Name.mp3", performer="P", title="Same Name.mp3"),
        row(2, name="Other.mp3", performer="P", title="Real Title"),
    ]
    assert ordered(rows, [("best_meta", 0.0), ("oldest", 0.0)]) == [2, 1]


def test_best_meta_placeholders_do_not_count(ordered):
    rows = [
        row(1, performer="<unknown>", title="[unknown]"),
        row(2, performer="Real", title="Song"),
    ]
    assert ordered(rows, [("best_meta", 0.0), ("oldest", 0.0)]) == [2, 1]


def test_longest_clean_name(ordered):
    rows = [
        row(1, name="a.mp3"),
        row(2, name="much longer name here.mp3"),
        row(3),  # без имени -> None, проигрывает
    ]
    assert ordered(rows, [("longest_clean_name", 0.0), ("oldest", 0.0)]) == [2, 1, 3]


def test_cascade_falls_through_to_next_criterion(ordered):
    # Одинаковый размер -> решает best_meta, затем oldest
    rows = [
        row(1, size=100),
        row(2, size=100, performer="P", title="T"),
        row(3, size=100, performer="P"),
    ]
    assert ordered(rows, [("largest", 0.0), ("best_meta", 0.0), ("oldest", 0.0)]) == [2, 3, 1]


def test_tolerance_band_defers_to_tiebreak(ordered):
    # largest ~10%: 105 в допуске от 120 (eps=12), 100 - нет
    rows = [row(1, size=100), row(2, size=105), row(3, size=120)]
    assert ordered(rows, [("largest", 0.10), ("oldest", 0.0)]) == [3, 1, 2]


def test_wide_tolerance_uses_tiebreak(ordered):
    rows = [row(1, size=100), row(2, size=140)]
    # Оба в полосе 50% -> решает oldest
    assert ordered(rows, [("largest", 0.50), ("oldest", 0.0)]) == [1, 2]


def test_valueless_records_lose(ordered):
    rows = [row(1, size=0), row(2, size=10)]
    assert ordered(rows, [("largest", 0.0), ("oldest", 0.0)]) == [2, 1]


def test_level_skipped_when_all_values_missing(ordered):
    rows = [row(2, size=0), row(1, size=0)]
    # largest неприменим ко всем -> уровень пропущен, решает oldest
    assert ordered(rows, [("largest", 0.0), ("oldest", 0.0)]) == [1, 2]


def test_single_element_group_returned_as_is(ordered):
    rows = [row(1, size=5)]
    assert ordered(rows, [("largest", 0.0)]) == [1]


def test_default_priority_from_factory(configure_settings):
    # Дефолт фабрики: largest ~3%, затем oldest
    configure_settings()
    rows = [row(1, size=1000), row(2, size=1010)]  # оба в полосе 3%
    result = [r["message_id"] for r in order_group_by_keep_priority(rows)]
    assert result == [1, 2]  # тай-брейк oldest
