"""Тесты точной группировки _group_audios_by_duplicates (чистая функция)."""

from helpers import CHAT_ID, SEED_ROWS, exact_grouping_oracle, groups_as_partition, make_row

from dedup.duplicates import _group_audios_by_duplicates


def test_empty_input():
    assert _group_audios_by_duplicates([]) == ([], {})


def test_groups_by_file_unique_id():
    rows = [
        make_row(1, "Name A.mp3", 100, 50, uid="X"),
        make_row(2, "Name B.mp3", 200, 60, uid="X"),
        make_row(3, "Lonely.mp3", 300, 70, uid="Y"),
    ]
    groups, edge_meta = _group_audios_by_duplicates(rows)
    assert groups_as_partition(groups) == {frozenset({1, 2})}
    assert edge_meta[(1, 2)].reason == "uid"
    assert edge_meta[(1, 2)].score == 1.0


def test_groups_by_meta_tuple_when_uid_differs():
    rows = [
        make_row(1, "Song.flac", 500, 80, uid="A", performer="P", title="T"),
        make_row(2, "Song.flac", 500, 80, uid="B", performer="P", title="T"),
        make_row(3, "Song.flac", 999, 80, uid="C", performer="P", title="T"),  # другой размер
    ]
    groups, edge_meta = _group_audios_by_duplicates(rows)
    assert groups_as_partition(groups) == {frozenset({1, 2})}
    assert edge_meta[(1, 2)].reason == "meta"


def test_transitive_merge_of_uid_and_meta_links():
    # A-B связаны по uid, B-C по мета-кортежу -> одна компонента {A,B,C}
    rows = [
        make_row(1, "Same Name.mp3", 100, 50, uid="AB"),
        make_row(2, "Same Name.mp3", 100, 50, uid="AB"),
        make_row(3, "Same Name.mp3", 100, 50, uid="C3"),
    ]
    # row3: uid отличается, но мета совпадает с row1/row2
    groups, _ = _group_audios_by_duplicates(rows)
    assert groups_as_partition(groups) == {frozenset({1, 2, 3})}


def test_singletons_excluded():
    rows = [make_row(1, "one.mp3", 10, 10), make_row(2, "two.mp3", 20, 20)]
    groups, edge_meta = _group_audios_by_duplicates(rows)
    assert groups == []
    assert edge_meta == {}


def test_matches_independent_oracle_on_synthetic_data():
    rows = [r for r in SEED_ROWS if r["chat_id"] == CHAT_ID]
    groups, _ = _group_audios_by_duplicates(rows)
    assert groups_as_partition(groups) == exact_grouping_oracle(rows)


def test_null_meta_fields_only_group_on_exact_none():
    # None-поля входят в мета-кортеж: совпадение только при полном равенстве
    rows = [
        make_row(1, None, 100, 50, uid="A", performer="P", title=None),
        make_row(2, None, 100, 50, uid="B", performer="P", title=None),
        make_row(3, None, 100, 50, uid="C", performer="P", title="T"),
    ]
    groups, _ = _group_audios_by_duplicates(rows)
    assert groups_as_partition(groups) == {frozenset({1, 2})}
