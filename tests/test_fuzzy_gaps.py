"""Тесты на пробелы, найденные мутационным тестированием (mutmut).

Закрывают дыры в fuzzy-матчере, которые не чувствуют ни golden-числа,
 ни юнит-тесты:

- точная арифметика штрафа Жаккара за несовпадение числовых токенов;
- фиксированный штраф при выключенном Жаккарде;
- поведение записей с duration=0 / file_size=0 (порог ослабляется,
  score-компонента обнуляется — «нет данных» не должно ни дисквалифицировать
  кандидата, ни накидывать ему баллы);
- SORT-режим с выключенной метой (ветка length-bound оптимистичного
  фильтра).
"""

import pytest
from helpers import groups_as_partition, make_row

from dedup.fuzzy import group_audios_fuzzy_optimized


@pytest.fixture
def fuzzy_runner(configure_settings):
    """Прогон матчера; настройки задаются явно при каждом вызове."""

    def _run(rows, **fuzzy_overrides):
        configure_settings(fuzzy=fuzzy_overrides)
        return group_audios_fuzzy_optimized(rows)

    return _run


# --- Штраф за несовпадение числовых токенов -------------------------------


def test_jaccard_penalty_partial_overlap(fuzzy_runner):
    # {1, 2} против {1, 3}: пересечение 1, объединение 3 → 0.5 * (1 - 1/3)
    rows = [
        make_row(1, "artist song 1 2", 10_000_000, 200),
        make_row(2, "artist song 1 3", 10_010_000, 200),
    ]
    _, edge_meta = fuzzy_runner(
        rows, use_jaccard_penalty=True, penalty_numbers_mismatch=0.5, threshold=0.1
    )
    info = edge_meta[(1, 2)]
    assert info.penalty == pytest.approx(0.5 * (1.0 - 1.0 / 3.0))
    # Инвариант итоговой формулы: score = взвешенная сумма компонентов минус штраф
    assert info.score == pytest.approx(
        info.name * 0.5 + info.dur * 0.3 + info.size * 0.2 - info.penalty
    )


def test_jaccard_penalty_full_for_disjoint_numbers(fuzzy_runner):
    # {5} против {7}: пересечение пусто → максимальный штраф
    rows = [
        make_row(1, "artist song 5", 10_000_000, 200),
        make_row(2, "artist song 7", 10_010_000, 200),
    ]
    _, edge_meta = fuzzy_runner(
        rows, use_jaccard_penalty=True, penalty_numbers_mismatch=0.5, threshold=0.1
    )
    assert edge_meta[(1, 2)].penalty == pytest.approx(0.5)


def test_fixed_penalty_when_jaccard_disabled(fuzzy_runner):
    # Жаккард выключен: штраф фиксированный, пересечение множеств не важно
    rows = [
        make_row(1, "artist song 1 2", 10_000_000, 200),
        make_row(2, "artist song 1 3", 10_010_000, 200),
    ]
    _, edge_meta = fuzzy_runner(
        rows, use_jaccard_penalty=False, penalty_numbers_mismatch=0.5, threshold=0.1
    )
    assert edge_meta[(1, 2)].penalty == pytest.approx(0.5)


def test_no_penalty_for_identical_numbers(fuzzy_runner):
    rows = [
        make_row(1, "artist song 1 2", 10_000_000, 200),
        make_row(2, "artist song 1 2", 10_010_000, 200),
    ]
    _, edge_meta = fuzzy_runner(
        rows, use_jaccard_penalty=True, penalty_numbers_mismatch=0.5, threshold=0.1
    )
    assert edge_meta[(1, 2)].penalty == pytest.approx(0.0)


def test_jaccard_penalty_applies_to_meta_sources_too(fuzzy_runner):
    # Совпадение через «мета -> имя»: числовые множества берутся из меты
    # текущего файла и из имени кандидата; здесь они совпадают -> штраф 0
    rows = [
        make_row(
            1,
            "tmpab12cd.mp3",
            10_000_000,
            200,
            performer="Fuzzy Band 1",
            title="Fuzzy Song 2",
        ),
        make_row(2, "Fuzzy Band 1 - Fuzzy Song 2.mp3", 10_010_000, 200),
    ]
    _, edge_meta = fuzzy_runner(
        rows, use_jaccard_penalty=True, penalty_numbers_mismatch=0.5, threshold=0.1
    )
    info = edge_meta[(1, 2)]
    assert info.penalty == pytest.approx(0.0)


# --- Записи без длительности и размера ------------------------------------


def test_zero_duration_and_size_still_match_identical_names(fuzzy_runner):
    # «Нет данных» дисквалифицирует не кандидата, а только его score-компоненту:
    # порог ослабевает ровно на вес компоненты. Одинаковые имена обязаны
    # сгруппироваться и без duration/file_size.
    rows = [
        make_row(1, "identical name", 0, 0),
        make_row(2, "identical name", 0, 0),
    ]
    groups, edge_meta = fuzzy_runner(rows)
    assert groups_as_partition(groups) == {frozenset({1, 2})}
    info = edge_meta[(1, 2)]
    assert info.reason == "fuzzy"
    assert info.name == pytest.approx(1.0)
    assert info.score == pytest.approx(0.5)  # w_name * 1.0, обе компоненты скомпенсированы


def test_zero_duration_and_size_do_not_rescue_mediocre_names(fuzzy_runner):
    # Ослабленный порог — не лицензия на группировку чего попало: посредственно
    # похожие имена без duration/file_size группироваться не должны
    # (расслабленный порог 0.45 против score 0.327).
    rows = [
        make_row(1, "alpha bravo charlie delta", 0, 0),
        make_row(2, "alpha bravo echo foxtrot", 0, 0),
    ]
    groups, edge_meta = fuzzy_runner(rows)
    assert groups == []
    assert edge_meta == {}


def test_zero_duration_with_valid_neighbor_duration(fuzzy_runner):
    # Текущий файл без длительности, сосед (dur=2) — в том же окне:
    # «нет данных» у одного не должно выкинуть пару из сравнения
    rows = [
        make_row(1, "identical name", 10_000_000, 0),
        make_row(2, "identical name", 10_010_000, 2),
    ]
    groups, _ = fuzzy_runner(rows)
    assert groups_as_partition(groups) == {frozenset({1, 2})}


def test_zero_size_with_valid_duration_still_matches(fuzzy_runner):
    # Только размер неизвестен: порог ослабевает на w_size, длительность считает
    rows = [
        make_row(1, "identical name", 0, 200),
        make_row(2, "identical name", 0, 200),
    ]
    groups, edge_meta = fuzzy_runner(rows)
    assert groups_as_partition(groups) == {frozenset({1, 2})}
    info = edge_meta[(1, 2)]
    assert info.dur == pytest.approx(1.0)
    assert info.size == pytest.approx(0.0)
    assert info.score == pytest.approx(0.5 * 1.0 + 0.3 * 1.0)


# --- SORT-режим с выключенной метой ----------------------------------------


def test_sort_mode_without_meta_matches_identical_names(fuzzy_runner):
    rows = [
        make_row(1, "identical name", 10_000_000, 200),
        make_row(2, "identical name", 10_010_000, 200),
    ]
    groups, edge_meta = fuzzy_runner(rows, matching_mode="sort", use_meta_fuzzy=False)
    assert groups_as_partition(groups) == {frozenset({1, 2})}
    assert edge_meta[(1, 2)].reason == "fuzzy"


def test_sort_mode_without_meta_matches_different_length_names(fuzzy_runner):
    # Разные длины имён: length-bound не должен отрезать настоящего кандидата
    # (верхняя граница ratio по длинам обязана оставаться верхней)
    rows = [
        make_row(1, "the quick brown fox jumps over", 10_000_000, 200),
        make_row(2, "the quick brown fox jumps over dog", 10_010_000, 200),
    ]
    groups, _ = fuzzy_runner(rows, matching_mode="sort", use_meta_fuzzy=False)
    assert groups_as_partition(groups) == {frozenset({1, 2})}
