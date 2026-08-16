"""Юнит-тесты очистки имён и меты (dedup.fuzzy): чистые функции без I/O."""

import pytest

from dedup.fuzzy import clean_filename, clean_meta, process_for_fuzzy, src_suffix


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Живые паттерны данных music_library.sqlite
        ("Artist - Track.mp3", "artist track"),
        ("Маша и Медведи - Любочка.mp3", "маша и медведи любочка"),
        ("tmpke2ul30j.mp3", "tmpke2ul30j"),  # мусорное имя сайта-качалки
        ("Powerful_T_-_Face_The_Race_72716591.mp3", "powerful t face the race"),  # ID отрезан
        ("Александр_Пушной_6_кадров_cover_version.flac", "александр пушной 6 кадров cover version"),
        # Реклама и сайты
        ("zaycev_net_Song_Name.mp3", "song name"),
        ("Song_Name [muzlome.com].mp3", "song name"),
        ("www.site.com_Song.mp3", "song"),
        # Цифровые префиксы/суффиксы
        ("1234567 - Real Song.mp3", "real song"),
        ("track 72716591.mp3", "track"),
        ("12345_1234567890123.mp3", "12345 1234567890123"),  # pure-id сохраняется
        # Суффиксы копий
        ("Track (1).mp3", "track"),
        ("Track [2].flac", "track"),
        # Без изменений по сути
        ("song name.mp3", "song name"),
    ],
)
def test_clean_filename_real_world_patterns(raw, expected):
    assert clean_filename(raw) == expected


def test_clean_filename_empty_inputs():
    assert clean_filename(None) == ""
    assert clean_filename("") == ""


@pytest.mark.parametrize(
    ("performer", "title", "expected"),
    [
        ("Performer", "Title", "performer title"),
        ("Artist", None, "artist"),
        (None, "Title", "title"),
        (None, None, ""),
        ("<unknown>", "Title", "title"),  # плейсхолдер исполнителя отброшен
        ("[unknown]", "<UNKNOWN>", ""),
        ("Artist", "Song (lyrics).mp3", "artist song (lyrics)"),
    ],
)
def test_clean_meta_placeholders_and_join(performer, title, expected):
    assert clean_meta(performer, title) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("artist - track", "artist track"),  # пунктуация -> пробелы, схлопывание
        ("artist  track", "artist track"),
        ("маша и медведи", "маша и медведи"),  # кириллица сохраняется
        ("", ""),
    ],
)
def test_process_for_fuzzy_normalizes(raw, expected):
    assert process_for_fuzzy(raw) == expected


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
