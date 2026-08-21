"""Юнит-тесты нормализации имён и меты (dedup.cleaning): чистые функции без I/O."""

import pytest

from dedup.cleaning import (
    clean_filename,
    clean_for_search,
    clean_meta,
    clean_meta_for_search,
    process_for_fuzzy,
)


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
    ("raw", "expected"),
    [
        # «Мусор» из clean_filename здесь сохраняется — по нему тоже ищут
        ("zaycev_net_Song.mp3", "zaycev net song mp3"),
        ("Song_Name [muzlome.com].mp3", "song name muzlome com mp3"),
        ("www.site.com_Song.mp3", "www site com song mp3"),
        ("Powerful_T_-_Face_The_Race_72716591.mp3", "powerful t face the race 72716591 mp3"),
        ("Track (1).mp3", "track 1 mp3"),
        ("https://x.y/Song.flac", "https x y song flac"),
        # Пунктуация между буквами не склеивается в один токен
        ("song.mp3", "song mp3"),
        ("Song Name", "song name"),
        ("", ""),
        (None, ""),
    ],
)
def test_clean_for_search_keeps_junk(raw, expected):
    assert clean_for_search(raw) == expected


@pytest.mark.parametrize(
    ("performer", "title", "expected"),
    [
        ("<unknown>", "Title", "title"),  # плейсхолдер отброшен
        ("[unknown]", "<UNKNOWN>", ""),
        ("Artist", "Song.mp3", "artist song mp3"),  # расширение сохранено
        (None, None, ""),
    ],
)
def test_clean_meta_for_search_placeholders_and_junk(performer, title, expected):
    assert clean_meta_for_search(performer, title) == expected
