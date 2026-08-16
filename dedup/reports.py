"""Человекочитаемый отчёт о найденных дубликатах."""

import asyncio
import datetime
import io
from collections import defaultdict

import aiosqlite

from .config import (
    DURATION_POWER,
    ENABLE_FUZZY_MATCHING,
    FUZZY_MATCHING_MODE,
    FUZZY_THRESHOLD,
    KEEP_PRIORITY,
    MAX_DURATION_DIFF_SEC,
    NAME_POWER,
    PENALTY_NUMBERS_MISMATCH,
    SIZE_POWER,
    USE_JACCARD_PENALTY,
    USE_META_FUZZY,
    WEIGHT_DURATION,
    WEIGHT_NAME,
    WEIGHT_SIZE,
)
from .duplicates import _get_potential_duplicate_groups
from .exports import _build_export_path
from .fuzzy import _src_suffix
from .logger import log
from .priority import _order_group_by_keep_priority
from .state import chat_label
from .typedefs import ChatID, EdgeInfo
from .utils import _format_bytes, _format_duration


async def create_duplicates_report(
    chat_id: ChatID, conn: aiosqlite.Connection, ts: str | None = None
) -> None:
    """Создает человекочитаемый отчёт о дубликатах со ссылками.

    Args:
        chat_id: ID чата, по которому строится отчёт.
        conn: Соединение с БД.
        ts: Общий таймстемп прогона (для одинаковых имён файлов отчётов);
            ``None`` — текущий момент.
    """
    log.info(f"Генерация отчета по дубликатам для чата {chat_label(chat_id)}...")

    groups, edge_meta = await _get_potential_duplicate_groups(chat_id, conn)

    if not groups:
        log.info("Дубликатов не найдено. Отчет не нужен.")
        return

    # Раскладываем рёбра по группам один раз: message_id -> индекс группы.
    id_to_group_idx: dict[int, int] = {}
    for gi, group in enumerate(groups):
        for row in group:
            id_to_group_idx[row["message_id"]] = gi

    edges_by_group: defaultdict[int, list[tuple[int, int, EdgeInfo]]] = defaultdict(list)
    for (a, b), info in edge_meta.items():
        gi = id_to_group_idx.get(a)
        if gi is not None and gi == id_to_group_idx.get(b):
            edges_by_group[gi].append((a, b, info))

    if chat_id >= 0:
        log.warning("Возможно личный чат, ссылки могут быть не действительны!")
    clean_chat_id = str(chat_id).removeprefix("-100")

    report_file = _build_export_path(chat_id, "report_duplicates", "txt", ts=ts)

    buf = io.StringIO()

    buf.write(f"ОТЧЕТ О ДУБЛИКАТАХ (Чат: {chat_label(chat_id)})\n")
    buf.write(f"Дата генерации: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    buf.write(f"Найдено групп: {len(groups)}\n\n")
    pretty_priority = ", ".join(f"{n} ~{t:.0%}" if t else n for n, t in KEEP_PRIORITY)
    buf.write(f"Стратегия оригинала: {pretty_priority}\n")
    buf.write(
        "Пометка [KEEP] предварительная: если кандидат не пройдёт верификацию\n"
        "(удалён/изменён в Telegram), оригиналом станет следующий в группе.\n"
    )

    if ENABLE_FUZZY_MATCHING:
        buf.write("\n[НАСТРОЙКИ FUZZY ПОИСКА]\n")
        buf.write(f"  • Режим:          {FUZZY_MATCHING_MODE.upper()}\n")
        buf.write(f"  • Порог сходства: {FUZZY_THRESHOLD}\n")
        buf.write(f"  • Окно времени:   ±{MAX_DURATION_DIFF_SEC} сек\n")
        buf.write(
            f"  • Веса:           Имя={WEIGHT_NAME} | Время={WEIGHT_DURATION} | Размер={WEIGHT_SIZE}\n"
        )
        buf.write(
            f"  • Степени (p):    Имя={NAME_POWER} | Время={DURATION_POWER} | Размер={SIZE_POWER}\n"
        )
        buf.write(f"  • Штраф (числа):  {PENALTY_NUMBERS_MISMATCH}\n")
        buf.write(f"  • Мера Жаккара:   {'ВКЛ' if USE_JACCARD_PENALTY else 'ВЫКЛ'}\n")
        buf.write(f"  • Meta fuzzy:     {'ВКЛ' if USE_META_FUZZY else 'ВЫКЛ'}\n")
        buf.write("  • Связи:          score=итог | текст/длит/размер=вклад(0..1) | штраф\n")
    else:
        buf.write("\n[НАСТРОЙКИ ПОИСКА]\n")
        buf.write("  • Режим:          STRICT (Точное совпадение)\n")

    buf.write("=" * 60 + "\n\n")

    for i, group in enumerate(groups, 1):
        sorted_group = _order_group_by_keep_priority(group)

        buf.write(f"--- ГРУППА #{i} (Файлов: {len(group)}) ---\n")

        for pos, row in enumerate(sorted_group):
            file_name = row["file_name"] or "Без названия"
            performer = (row["performer"] or "").strip()
            title = (row["title"] or "").strip()
            track_meta = " — ".join(x for x in (performer, title) if x) or "не указано"

            size_mb = _format_bytes(row["file_size"] or 0)
            msg_id = row["message_id"]
            msg_uid = row["file_unique_id"]
            dur_str = _format_duration(row["duration"])
            link = f"https://t.me/c/{clean_chat_id}/{msg_id}"

            marker = "[KEEP] " if pos == 0 else ""
            buf.write(f"• {marker}{file_name}\n")
            buf.write(f"  Track: {track_meta}\n")
            buf.write(f"  Info: {size_mb} | Время: {dur_str} | ID: {msg_id} | UID: {msg_uid}\n")
            buf.write(f"  Link: {link}\n")
            buf.write("\n")

        group_edges = edges_by_group.get(i - 1, [])
        if group_edges:
            group_edges.sort(key=lambda e: e[2].score, reverse=True)
            buf.write(f"  Связи ({len(group_edges)}):\n")
            for a, b, info in group_edges:
                if info.reason == "uid":
                    detail = "идентичный файл (UID)"
                elif info.reason == "meta":
                    detail = "точное совпадение метаданных"
                else:
                    detail = (
                        f"fuzzy: score={info.score:.3f} | "
                        f"текст={info.name:.2f}{_src_suffix(info.text_source)} | "
                        f"длит={info.dur:.2f} | размер={info.size:.2f} | "
                        f"штраф=-{info.penalty:.2f}"
                    )
                buf.write(f"    {a} <-> {b}: {detail}\n")
            buf.write("\n")

        buf.write("-" * 30 + "\n\n")

    content = buf.getvalue()
    buf.close()

    def _write() -> None:
        """(СИНХРОННАЯ!) Записывает готовый текст отчёта в файл."""
        with open(report_file, "w", encoding="utf-8") as f:
            f.write(content)

    await asyncio.to_thread(_write)

    log.info(f"Отчет готов! Откройте файл: {report_file}")
