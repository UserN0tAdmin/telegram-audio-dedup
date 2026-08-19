"""Тесты нечёткого поиска (dedup.search): ранжирование и офлайн-запуск."""

import logging

from helpers import CHAT_ID, make_row
from wcwidth import wcswidth

from dedup.search import RESULT_LIMIT, SCORE_CUTOFF, _format_results, rank_rows, run_search


def test_rank_rows_exact_match_scores_100():
    rows = [make_row(1, "Artist - Track One.mp3", 10_000_000, 200)]
    results = rank_rows("artist track one", rows)
    assert len(results) == 1
    assert results[0][0] == 100
    assert results[0][1]["message_id"] == 1


def test_rank_rows_survives_typo_and_order():
    # Опечатка в исполнителе + перестановка слов: token_set_ratio прощает
    rows = [make_row(1, "Artist - Track One.mp3", 10_000_000, 200)]
    results = rank_rows("track one artest", rows)
    assert len(results) == 1
    assert results[0][0] >= SCORE_CUTOFF


def test_rank_rows_score_cutoff_parameter():
    # Совпадение с опечатками (~78%): строгий порог отсекает, мягкий пропускает
    rows = [make_row(1, "Sane Sang.mp3", 1_000_000, 100)]
    assert rank_rows("same song", rows, score_cutoff=80) == []
    results = rank_rows("same song", rows, score_cutoff=75)
    assert len(results) == 1
    assert 75 <= results[0][0] < 80


def test_rank_rows_ignores_dedup_matching_mode(configure_settings):
    # matching_mode — настройка строгости дедупликации; поиск всегда token_set
    configure_settings(fuzzy={"matching_mode": "sort"})
    rows = [make_row(1, "Same Song Live.mp3", 1_000_000, 100)]
    results = rank_rows("same song", rows)  # слово-подмножество: token_sort дал бы мало
    assert len(results) == 1
    assert results[0][0] == 100


def test_rank_rows_wratio_finds_short_typo_fragment():
    # Обрывок с опечаткой против длинного имени: token_set не видит (38%),
    # WRatio вытаскивает через партиал-окно (~72%)
    rows = [make_row(1, "Воскресный ангел.ape", 1_000_000, 200)]
    assert rank_rows("ангил", rows) == []
    results = rank_rows("ангил", rows, wratio=True)
    assert len(results) == 1
    assert results[0][0] >= 70


def test_rank_rows_matches_meta_when_no_filename():
    # Имени нет — совпадение строится по тегам performer+title
    rows = [make_row(8, None, 2_000_000, 60, performer="Only Meta", title="Meta Only")]
    results = rank_rows("only meta", rows)
    assert len(results) == 1
    assert results[0][1]["message_id"] == 8


def test_rank_rows_takes_best_of_name_and_meta():
    # Имя не осмысленное (мусор), но мета совпадает — строка всё равно найдена
    rows = [
        make_row(5, "tmpab12cd.mp3", 8_000_000, 210, performer="Fuzzy Band", title="Fuzzy Song"),
        make_row(7, "Completely Different Track.mp3", 3_000_000, 95),
    ]
    results = rank_rows("fuzzy band fuzzy song", rows)
    assert [r[1]["message_id"] for r in results] == [5]


def test_rank_rows_filters_irrelevant():
    rows = [
        make_row(1, "Artist - Track One.mp3", 10_000_000, 200),
        make_row(7, "Completely Different Track.mp3", 3_000_000, 95),
    ]
    assert rank_rows("zzzzz qqqqqq wwwww", rows) == []


def test_rank_rows_limited_to_result_limit():
    # Одинаковые имена в разных чатах: все выше порога, но вывод ограничен
    rows = [
        make_row(i, "Same Song.mp3", 1_000_000, 100, chat_id=CHAT_ID + i)
        for i in range(1, RESULT_LIMIT + 20)
    ]
    results = rank_rows("same song", rows)
    assert len(results) == RESULT_LIMIT


def test_rank_rows_result_limit_parameter():
    # Лимит можно перекрыть: результат обрезается даже при большом числе совпадений
    rows = [make_row(i, "Same Song.mp3", 1_000_000, 100, chat_id=CHAT_ID + i) for i in range(1, 10)]
    assert len(rank_rows("same song", rows, result_limit=3)) == 3
    assert len(rank_rows("same song", rows, result_limit=100)) == 9


def test_rank_rows_exact_match_ranks_first():
    # Точное совпадение (100) выше совпадения с опечатками
    rows = [
        make_row(1, "Sane Sang.mp3", 1_000_000, 100),  # 2 замены — score ~89
        make_row(2, "Same Song.mp3", 1_000_000, 100),
    ]
    results = rank_rows("same song", rows)
    assert [r[1]["message_id"] for r in results] == [2, 1]
    scores = [score for score, _ in results]
    assert scores == sorted(scores, reverse=True)


def test_rank_rows_empty_query_or_rows():
    rows = [make_row(1, "Song.mp3", 1, 1)]
    assert rank_rows("", rows) == []
    assert rank_rows("   ", rows) == []
    assert rank_rows("song", []) == []


async def test_run_search_missing_db_logs_critical(caplog):
    # БД из дефолтных настроек не существует — критичное сообщение, не исключение
    with caplog.at_level(logging.CRITICAL):
        await run_search("anything")
    assert any("не найден" in r.message for r in caplog.records)


async def test_run_search_prints_rows_with_links(seeded_db, caplog):
    # Поиск по всей БД: строки обоих чатов, ссылка без префикса -100
    with caplog.at_level(logging.INFO, logger="AudioDeleter"):
        await run_search("fuzzy song")
    text = "\n".join(r.message for r in caplog.records)
    assert "Просмотрено 12 аудио" in text
    assert "https://t.me/c/1234567890/5" in text  # мета-совпадение строки 5
    assert "https://t.me/c/1234567890/6" in text  # имя "Fuzzy Band - Fuzzy Song"


async def test_run_search_no_results(seeded_db, caplog):
    with caplog.at_level(logging.INFO, logger="AudioDeleter"):
        await run_search("zzzzz qqqqqq")
    text = "\n".join(r.message for r in caplog.records)
    assert "совпадений: 0" in text


def test_format_results_aligns_columns():
    # Колонки добиваются до самой широкой строки: визуальная ширина префикса
    # до ссылки одинакова у всех строк (wcswidth, как в прод-коде)
    rows = [
        make_row(1, "Short.mp3", 1_000_000, 100),
        make_row(2, "A Considerably Longer File Name.flac", 5_000_000_000, 7_200),
    ]
    lines = _format_results([(95, rows[0]), (72, rows[1])])
    prefix_widths = {wcswidth(line[: line.index("https://t.me/")]) for line in lines}
    assert len(prefix_widths) == 1


def test_format_results_pads_wide_chars_like_plain():
    # Кириллица и латиница одной «визуальной» длины дают одинаковое смещение ссылки
    rows = [
        make_row(1, "Песня о свободе.mp3", 1_000_000, 100),
        make_row(2, "Pesnya o svobode.mp3", 1_000_000, 100),
    ]
    lines = _format_results([(90, rows[0]), (80, rows[1])])
    offsets = {line.index("https://t.me/") for line in lines}
    assert len(offsets) == 1


def test_format_results_drops_all_empty_size_column():
    # file_size = 0 у всех строк — колонка размера не выводится вовсе
    rows = [make_row(1, "Song One.mp3", 0, 100), make_row(2, "Song Two.mp3", 0, 100)]
    lines = _format_results([(90, rows[0]), (80, rows[1])])
    assert all("MiB" not in line and "B |" not in line for line in lines)
    assert all("https://t.me/c/" in line for line in lines)


def test_format_results_keeps_link_without_100_prefix():
    rows = [make_row(1, "Song.mp3", 1_000_000, 100)]
    (line,) = _format_results([(88, rows[0])])
    assert "https://t.me/c/1234567890/1" in line  # -100 из chat_id убран
