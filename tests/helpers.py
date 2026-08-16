"""Общие построители тестовых данных и независимые оракулы для сверки."""

import hashlib
import sqlite3
from collections import defaultdict
from pathlib import Path

# Синтетический чат seeded_db
CHAT_ID = -1001234567890
# Второй чат для кросс-чатовых проверок
OTHER_CHAT_ID = -1009999999999

_AUDIO_COLUMNS = (
    "chat_id, message_id, file_unique_id, file_name, file_size, duration, performer, title"
)


def make_row(
    message_id: int,
    file_name: str | None,
    file_size: int,
    duration: int,
    *,
    uid: str | None = None,
    performer: str | None = None,
    title: str | None = None,
    chat_id: int = CHAT_ID,
) -> dict:
    """Dict-строка, совместимая с DBRow: прод-код читает поля через ``row["..."]``."""
    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "file_unique_id": uid if uid is not None else f"uid-{message_id}",
        "file_name": file_name,
        "file_size": file_size,
        "duration": duration,
        "performer": performer,
        "title": title,
    }


# Синтетический датасет: покрывает все способы группировки и крайние случаи
# живых данных (NULL performer, <unknown>, duration=0, title==filename).
SEED_ROWS: list[dict] = [
    # Дубль по file_unique_id: имена отличаются суффиксом копии
    make_row(
        1,
        "Artist - Track One.mp3",
        10_000_000,
        200,
        uid="AAAA",
        performer="Artist",
        title="Track One",
    ),
    make_row(
        2,
        "Artist - Track One (1).mp3",
        10_050_000,
        200,
        uid="AAAA",
        performer="Artist",
        title="Track One",
    ),
    # Дубль по мета-кортежу (uid разные, всё остальное идентично)
    make_row(
        3, "Meta Dup Song.flac", 5_000_000, 180, uid="BBBB", performer="Meta Dup", title="Song"
    ),
    make_row(
        4, "Meta Dup Song.flac", 5_000_000, 180, uid="CCCC", performer="Meta Dup", title="Song"
    ),
    # Fuzzy-пара: мусорное имя против меты второго файла (источник «имя-мета»)
    make_row(
        5, "tmpab12cd.mp3", 8_000_000, 210, uid="DDDD", performer="Fuzzy Band", title="Fuzzy Song"
    ),
    make_row(6, "Fuzzy Band - Fuzzy Song.mp3", 8_100_000, 211, uid="EEEE"),
    # Одиночка без дубликатов
    make_row(7, "Completely Different Track.mp3", 3_000_000, 95, uid="FFFF"),
    # Крайние случаи из живой БД
    make_row(8, None, 2_000_000, 60, uid="GGGG", performer="Only Meta", title="Meta Only"),
    make_row(9, "unknown_track.mp3", 1_500_000, 45, uid="HHHH", performer="<unknown>"),
    make_row(10, "zero duration.mp3", 900_000, 0, uid="IIII"),
    make_row(
        11, "Same Name.mp3", 7_000_000, 190, uid="JJJJ", performer="X Y", title="Same Name.mp3"
    ),
    # Строка другого чата: группировщики работают по одному чату — не должна
    # попасть в группы CHAT_ID
    make_row(12, "Other Chat Song.mp3", 4_000_000, 100, uid="KKKK", chat_id=OTHER_CHAT_ID),
]


def row_tuples(rows: list[dict]) -> list[tuple]:
    """Dict-строки в кортежи для INSERT (порядок = порядок колонок audios)."""
    return [tuple(r.values()) for r in rows]


async def seed_database(conn, rows: list[dict]) -> None:
    """Вставляет dict-строки в таблицу audios и коммитит."""
    placeholders = ", ".join("?" * 8)
    await conn.executemany(
        f"INSERT INTO audios ({_AUDIO_COLUMNS}) VALUES ({placeholders})",
        row_tuples(rows),
    )
    await conn.commit()


def groups_as_partition(groups) -> set[frozenset[int]]:
    """Группы (list[list[DBRow]]) в множество frozenset из message_id."""
    return {frozenset(r["message_id"] for r in group) for group in groups}


def fetch_chat_rows(db_path: Path, chat_id: int) -> list[sqlite3.Row]:
    """Строки чата из БД в режиме только чтение (независимый канал чтения)."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM audios WHERE chat_id = ?", (chat_id,)).fetchall()


def chat_row_counts(db_path: Path) -> dict[int, int]:
    """{chat_id: число строк audios} по всей БД."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT chat_id, COUNT(*) AS cnt FROM audios GROUP BY chat_id ORDER BY cnt"
        ).fetchall()
    return dict(rows)


def sql_count(db_path: Path, sql: str, params: tuple = ()) -> int:
    """Скалярный SQL-счётчик для оракулов экспортов."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        return conn.execute(sql, params).fetchone()[0]


def fuzzy_signature(fuzzy_settings) -> str:
    """Хэш параметров fuzzy-матчера: привязка golden-чисел к настройкам."""
    payload = "|".join(
        repr(v)
        for v in (
            fuzzy_settings.matching_mode,
            fuzzy_settings.threshold,
            fuzzy_settings.max_duration_diff_sec,
            fuzzy_settings.name_power,
            fuzzy_settings.duration_power,
            fuzzy_settings.size_power,
            fuzzy_settings.weight_name,
            fuzzy_settings.weight_duration,
            fuzzy_settings.weight_size,
            fuzzy_settings.penalty_numbers_mismatch,
            fuzzy_settings.use_jaccard_penalty,
            fuzzy_settings.use_meta_fuzzy,
        )
    )
    return hashlib.md5(payload.encode()).hexdigest()[:12]


def exact_grouping_oracle(rows: list[dict]) -> set[frozenset[int]]:
    """Независимая реализация точной группировки (union-find) для сверки.

    Алгоритм отличается от прод-кода (BFS по графу): связи строятся
    отдельно по file_unique_id и по мета-кортежу, затем объединяются
    union-find. Совпадение разбиений — сильная проверка корректности.
    """
    parent: dict[int, int] = {r["message_id"]: r["message_id"] for r in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_uid: defaultdict[str, list[int]] = defaultdict(list)
    by_meta: defaultdict[tuple, list[int]] = defaultdict(list)
    for r in rows:
        by_uid[r["file_unique_id"]].append(r["message_id"])
        by_meta[(r["file_name"], r["performer"], r["title"], r["file_size"], r["duration"])].append(
            r["message_id"]
        )

    for bucket in (*by_uid.values(), *by_meta.values()):
        for other in bucket[1:]:
            union(bucket[0], other)

    components: defaultdict[int, set[int]] = defaultdict(set)
    for r in rows:
        components[find(r["message_id"])].add(r["message_id"])
    return {frozenset(ids) for ids in components.values() if len(ids) > 1}
