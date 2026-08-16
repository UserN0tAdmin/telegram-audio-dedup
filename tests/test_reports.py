"""Тесты отчёта о дубликатах (dedup.reports) на синтетической БД."""

from helpers import CHAT_ID

from dedup.context import get_settings
from dedup.reports import create_duplicates_report


def report_path() -> object:
    from pathlib import Path

    return Path(get_settings().paths.exports_dir)


async def read_report() -> str:
    files = list(report_path().rglob("*_report_duplicates.txt"))
    assert len(files) == 1, "ожидался ровно один файл отчёта"
    return files[0].read_text(encoding="utf-8")


async def test_report_exact_mode(seeded_db):
    await create_duplicates_report(CHAT_ID, seeded_db, ts="2026-01-01_00-00-00")
    content = await read_report()

    assert "Найдено групп: 2" in content  # {1,2} по uid и {3,4} по мета-кортежу
    assert "ГРУППА #1" in content
    assert "ГРУППА #2" in content
    # По одному оригиналу на группу (маркер в шапке не считаем)
    assert content.count("• [KEEP] ") == 2
    # Стратегия фабрики: largest ~3%, затем oldest
    assert "Стратегия оригинала: largest ~3%, oldest" in content
    assert "Режим:          STRICT" in content


async def test_report_fuzzy_mode_lists_settings(seeded_db, configure_settings):
    configure_settings(fuzzy={"enable": True})
    await create_duplicates_report(CHAT_ID, seeded_db, ts="2026-01-01_00-00-00")
    content = await read_report()

    assert "Найдено групп: 3" in content  # + fuzzy-пара {5,6}
    assert "[НАСТРОЙКИ FUZZY ПОИСКА]" in content
    # fuzzy-рёбра описаны коэффициентами
    assert "fuzzy: score=" in content


async def test_report_marks_keep_by_priority(seeded_db, configure_settings):
    # Внутри uid-группы {1,2} файл 2 больше (10 050 000 против 10 000 000)
    # при допуске 3% — оба в полосе, решает oldest: [KEEP] у сообщения 1
    configure_settings()
    await create_duplicates_report(CHAT_ID, seeded_db, ts="2026-01-01_00-00-00")
    content = await read_report()

    keep_block = content.split("ГРУППА #1", 1)[1].split("ГРУППА #2", 1)[0]
    assert "[KEEP] Artist - Track One.mp3" in keep_block
    assert "ID: 1" in keep_block


async def test_report_empty_chat_creates_nothing(fresh_db):
    await create_duplicates_report(-1000000000005, fresh_db, ts="2026-01-01_00-00-00")
    assert list(report_path().rglob("*_report_duplicates.txt")) == []
