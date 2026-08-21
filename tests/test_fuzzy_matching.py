"""Юнит-тесты fuzzy-матчера group_audios_fuzzy_optimized (вход — dict-строки)."""

import pytest
from helpers import CHAT_ID, SEED_ROWS, groups_as_partition, make_row

from dedup.fuzzy import group_audios_fuzzy_optimized, src_suffix


@pytest.fixture
def fuzzy_runner(configure_settings):
    """Прогон матчера с настройками по умолчанию или с оверрайдами секции fuzzy."""

    def _run(rows, **fuzzy_overrides):
        if fuzzy_overrides:
            configure_settings(fuzzy=fuzzy_overrides)
        return group_audios_fuzzy_optimized(rows)

    return _run


def test_empty_input_returns_empty(fuzzy_runner):
    assert fuzzy_runner([]) == ([], {})


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        (0, "(имя-имя)"),
        (1, "(имя-мета)"),
        (2, "(мета-имя)"),
        (3, "(мета-мета)"),
        (None, ""),
    ],
)
def test_src_suffix(src, expected):
    assert src_suffix(src) == expected


def test_uid_duplicates_grouped_even_with_different_names(fuzzy_runner):
    rows = [
        make_row(1, "totally different alpha", 5_000_000, 100, uid="SAME"),
        make_row(2, "another thing entirely", 5_200_000, 105, uid="SAME"),
    ]
    groups, edge_meta = fuzzy_runner(rows)
    assert groups_as_partition(groups) == {frozenset({1, 2})}
    info = edge_meta[(1, 2)]
    assert info.reason == "uid"
    assert info.score == 1.0


def test_similar_names_durations_and_sizes_grouped(fuzzy_runner):
    rows = [
        make_row(1, "Artist - Track.mp3", 10_000_000, 200),
        make_row(2, "artist-track.mp3", 10_050_000, 201),
    ]
    groups, edge_meta = fuzzy_runner(rows)
    assert groups_as_partition(groups) == {frozenset({1, 2})}
    info = edge_meta[(1, 2)]
    assert info.reason == "fuzzy"
    assert info.score >= 0.90  # порог по умолчанию


def test_meta_source_matches_garbage_name_against_meta(fuzzy_runner):
    # Имя первого файла — мусор; совпадение идёт через «имя-мета»/«мета-имя»
    rows = [
        make_row(5, "tmpab12cd.mp3", 8_000_000, 210, performer="Fuzzy Band", title="Fuzzy Song"),
        make_row(6, "Fuzzy Band - Fuzzy Song.mp3", 8_100_000, 211),
    ]
    groups, _ = fuzzy_runner(rows)
    assert groups_as_partition(groups) == {frozenset({5, 6})}


def test_disimilar_tracks_not_grouped(fuzzy_runner):
    rows = [
        make_row(1, "alpha bravo charlie delta", 9_000_000, 200),
        make_row(2, "echo foxtrot golf hotel", 9_100_000, 201),
    ]
    groups, _ = fuzzy_runner(rows)
    assert groups == []


def test_distant_durations_never_compared(fuzzy_runner):
    # Окно ±3 сек: кандидаты вне окна исключаются до текстового сравнения
    rows = [
        make_row(1, "same name here", 9_000_000, 100),
        make_row(2, "same name here", 9_100_000, 400),
    ]
    groups, edge_meta = fuzzy_runner(rows)
    assert groups == []
    assert edge_meta == {}


def test_threshold_above_one_disables_fuzzy_but_keeps_uid(fuzzy_runner):
    uid_rows = [make_row(1, "a", 100, 10, uid="X"), make_row(2, "b", 110, 10, uid="X")]
    groups, _ = fuzzy_runner(uid_rows, threshold=1.5)
    assert groups_as_partition(groups) == {frozenset({1, 2})}  # uid-prepass не зависит от порога

    fuzzy_rows = [make_row(1, "name one", 100, 10), make_row(2, "name one", 110, 10)]
    groups, _ = fuzzy_runner(fuzzy_rows, threshold=1.5)
    assert groups == []


def test_lower_threshold_groups_superset(fuzzy_runner):
    rows = [
        make_row(1, "song version one", 9_000_000, 200),
        make_row(2, "song version two", 9_200_000, 203),  # чуть разные номера
    ]
    _, edge_strict = fuzzy_runner(rows, threshold=0.95)
    groups_loose, edge_loose = fuzzy_runner(rows, threshold=0.60)
    assert len(edge_loose) >= len(edge_strict)
    assert len(groups_loose) >= 1


def test_invariants_on_seed_dataset(fuzzy_runner):
    rows = [r for r in SEED_ROWS if r["chat_id"] == CHAT_ID]
    groups, edge_meta = fuzzy_runner(rows)

    partition = groups_as_partition(groups)
    all_ids = {r["message_id"] for r in rows}

    # Группы дизъюнктны, размер >= 2, участники из входных данных
    flat = [mid for group in partition for mid in group]
    assert len(flat) == len(set(flat))
    assert all(len(group) >= 2 for group in partition)
    assert set(flat) <= all_ids
    # Точные дубликаты обязаны находиться и в fuzzy-режиме
    assert frozenset({1, 2}) in partition  # uid-дубль
    assert frozenset({3, 4}) in partition  # мета-дубль
    assert frozenset({5, 6}) in partition  # fuzzy через мету
    # Все рёбра ссылаются на существующие id
    assert all(a in all_ids and b in all_ids for a, b in edge_meta)


def test_deterministic_between_runs(fuzzy_runner):
    rows = [r for r in SEED_ROWS if r["chat_id"] == CHAT_ID]
    first_groups, first_edges = fuzzy_runner(list(rows))
    second_groups, second_edges = fuzzy_runner(list(rows))
    assert groups_as_partition(first_groups) == groups_as_partition(second_groups)
    assert {k: v.score for k, v in first_edges.items()} == {
        k: v.score for k, v in second_edges.items()
    }


def test_sort_and_set_modes_both_catch_identical_names(fuzzy_runner):
    rows = [
        make_row(1, "identical name", 10_000_000, 200),
        make_row(2, "identical name", 10_010_000, 200),
    ]
    for mode in ("set", "sort"):
        _, edge_meta = fuzzy_runner(rows, matching_mode=mode)
        assert (1, 2) in edge_meta, f"режим {mode} потерял очевидный дубль"
