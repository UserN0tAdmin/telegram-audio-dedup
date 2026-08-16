"""Поиск, верификация через API и классификация дубликатов."""

import asyncio
import itertools
from collections import defaultdict

import aiosqlite
from pyrogram import Client

from .apply import handle_database_changes
from .context import get_settings
from .fuzzy import group_audios_fuzzy_optimized
from .logger import log
from .priority import order_group_by_keep_priority
from .state import chat_label
from .tg import get_audio_attributes
from .typedefs import (
    ChatID,
    ClassificationResult,
    DBRow,
    DuplicateGroup,
    EdgeInfo,
    EdgeMeta,
    MessageID,
    VerifiedMessagesDict,
    edge_key,
)


async def find_and_process_duplicates(
    app: Client,
    chat_id: ChatID,
    conn: aiosqlite.Connection,
    archive_target_id: ChatID | None = None,
) -> None:
    """Оркестратор анализа дубликатов: формирует списки действий.

    Находит группы потенциальных дубликатов в БД, верифицирует их через
    API, классифицирует и передаёт результат на исполнение.

    Args:
        app: Клиент Telegram.
        chat_id: ID обрабатываемого чата.
        conn: Соединение с БД.
        archive_target_id: ID архивного чата, если включена архивация.
    """
    log.info(f"\n{'=' * 10}\nНачинаю анализ дубликатов в чате {chat_label(chat_id)}...")

    # Шаг 1: Найти группы потенциальных дубликатов в локальной базе данных
    potential_groups, _ = await get_potential_duplicate_groups(chat_id, conn)
    if not potential_groups:
        log.info(f"В чате {chat_label(chat_id)} дубликатов не найдено.")
        return

    log.info(
        f"Найдено {len(potential_groups)} групп потенциальных дубликатов. Начинаю верификацию через API..."
    )

    # Шаг 2: Проверить все сообщения из этих групп, запросив их у Telegram
    ids_to_verify = list({record["message_id"] for group in potential_groups for record in group})
    verified_messages = await _verify_messages_from_api(app, chat_id, ids_to_verify)

    # Шаг 3: Классифицировать дубликаты на основе верифицированных данных
    tg_ids, db_ids, update_records = _classify_verified_duplicates(
        potential_groups, verified_messages
    )

    # Шаг 4: Передать отсортированные списки на исполнение
    await handle_database_changes(
        app,
        chat_id,
        conn,
        sorted(tg_ids),
        sorted(db_ids),
        update_records,
        archive_target_id=archive_target_id,
    )


def _group_audios_by_duplicates(all_audios: list[DBRow]) -> tuple[list[DuplicateGroup], EdgeMeta]:
    """(ЧИСТАЯ ФУНКЦИЯ) Группирует записи точных совпадений.

    Обход графа находит связные компоненты (транзитивные связи) по
    ``file_unique_id`` и полному совпадению метаданных.

    Args:
        all_audios: Записи БД одного чата.

    Returns:
        Кортеж ``(groups, edge_meta)``: группы дубликатов (``len >= 2``) и
        метаданные связей с причиной ``"uid"``/``"meta"`` для отчёта.
    """
    if not all_audios:
        return [], {}

    id_to_record = {rec["message_id"]: rec for rec in all_audios}
    uid_to_ids = defaultdict(list)
    meta_to_ids = defaultdict(list)

    for rec in all_audios:
        msg_id = rec["message_id"]
        uid_to_ids[rec["file_unique_id"]].append(msg_id)
        meta_key = (
            rec["file_name"],
            rec["performer"],
            rec["title"],
            rec["file_size"],
            rec["duration"],
        )
        meta_to_ids[meta_key].append(msg_id)

    potential_duplicate_groups = []
    processed_ids = set()
    edge_meta: EdgeMeta = {}

    for start_msg_id in id_to_record:
        if start_msg_id in processed_ids:
            continue

        # Начинаем обход нового компонента связности
        current_group = {start_msg_id}
        work_set = {start_msg_id}  # Используем set как неупорядоченный буфер
        processed_ids.add(start_msg_id)  # Mark-on-push (сразу помечаем стартовый)

        while work_set:
            curr_id = work_set.pop()  # Порядок извлечения не важен

            rec = id_to_record[curr_id]

            neighbors_uid = uid_to_ids.get(rec["file_unique_id"], [])
            meta_key = (
                rec["file_name"],
                rec["performer"],
                rec["title"],
                rec["file_size"],
                rec["duration"],
            )
            neighbors_meta = meta_to_ids.get(meta_key, [])

            for neighbor_id, reason in itertools.chain(
                ((n, "uid") for n in neighbors_uid),
                ((n, "meta") for n in neighbors_meta),
            ):
                if neighbor_id == curr_id:
                    continue
                key = edge_key(curr_id, neighbor_id)
                # uid имеет приоритет над meta при описании причины
                if reason == "uid" or key not in edge_meta:
                    edge_meta[key] = EdgeInfo(
                        reason=reason,
                        score=1.0,
                        name=None,
                        dur=None,
                        size=None,
                        penalty=0.0,
                    )
                if neighbor_id not in processed_ids:
                    processed_ids.add(neighbor_id)  # Mark-on-push
                    current_group.add(neighbor_id)
                    work_set.add(neighbor_id)

        if len(current_group) > 1:
            group_records = [id_to_record[gid] for gid in current_group]
            potential_duplicate_groups.append(group_records)

    return potential_duplicate_groups, edge_meta


async def get_potential_duplicate_groups(
    chat_id: ChatID, conn: aiosqlite.Connection
) -> tuple[list[DuplicateGroup], EdgeMeta]:
    """Запрашивает из БД все аудио чата и передаёт их группировщику.

    Тяжёлая группировка выполняется в отдельном потоке.

    Args:
        chat_id: ID анализируемого чата.
        conn: Соединение с БД.

    Returns:
        Кортеж ``(groups, edge_meta)``: группы дубликатов и метаданные
        связей (причина + коэффициенты) для отчёта.
    """
    log.debug(f"Анализ чата {chat_label(chat_id)}: Запрос всех аудиозаписей из локальной БД...")
    # Лимит тележки на сообщения 1 млн, т.е. на 1 чат максимум 1 млн записей, что не бьёт по ОЗУ
    # Но фактически маловероятно, что есть чаты, где аудиофайлов больше, чем 300 тыс., что максимум для 512 МБ - 1 ГБ ОЗУ
    # Т.е. здесь сделано верно
    async with conn.execute("SELECT * FROM audios WHERE chat_id = ?", (chat_id,)) as cursor:
        all_audios = await cursor.fetchall()

    if not all_audios:
        log.info(f"В базе данных нет записей для анализа в чате {chat_label(chat_id)}.")
        return [], {}

    if get_settings().fuzzy.enable:
        return await asyncio.to_thread(group_audios_fuzzy_optimized, all_audios)
        # return []
    else:
        return await asyncio.to_thread(_group_audios_by_duplicates, all_audios)


async def _verify_messages_from_api(
    app: Client, chat_id: ChatID, ids_to_verify: list[MessageID]
) -> VerifiedMessagesDict:
    """Надёжно запрашивает у Telegram информацию о сообщениях по их ID.

    Использует семафор для контроля параллельных запросов и пакетирование.

    Args:
        app: Клиент Telegram.
        chat_id: ID чата.
        ids_to_verify: Список message_id для проверки.

    Returns:
        Словарь ``message_id -> Message | None | Exception``.
    """
    verified_messages = {}
    cfg = get_settings().performance
    semaphore = asyncio.Semaphore(cfg.verify_concurrency)

    async def fetch_chunk(chunk_ids):
        """Загружает один чанк сообщений; при ошибке возвращает её."""
        async with semaphore:
            try:
                return await app.get_messages(chat_id, chunk_ids)
            except Exception as e:
                log.error(f"Ошибка при получении пакета сообщений (ID: {chunk_ids[0]}...): {e}.")
                return e

    original_chunks = [
        list(chunk) for chunk in itertools.batched(ids_to_verify, cfg.verify_chunk_size)
    ]
    tasks = [fetch_chunk(chunk) for chunk in original_chunks]
    results_from_gather = await asyncio.gather(*tasks)

    for original_chunk, result_chunk in zip(original_chunks, results_from_gather, strict=True):
        if isinstance(result_chunk, Exception):
            for msg_id in original_chunk:
                verified_messages[msg_id] = result_chunk
        else:
            found_messages = {msg.id: msg for msg in result_chunk if msg}
            for msg_id in original_chunk:
                verified_messages[msg_id] = found_messages.get(msg_id)

    return verified_messages


def _classify_verified_duplicates(
    duplicate_groups: list[DuplicateGroup], verified_messages: VerifiedMessagesDict
) -> ClassificationResult:
    """Анализирует верифицированные сообщения и принимает решение о действиях.

    ВАЖНО: используется стратегия fail-safe — если при проверке любого
    сообщения в группе возникает ошибка API, вся группа пропускается
    для предотвращения случайного удаления данных.

    Args:
        duplicate_groups: Группы потенциальных дубликатов.
        verified_messages: Результат ``_verify_messages_from_api``.

    Returns:
        ClassificationResult со списками действий.
    """
    to_delete_from_tg = set()
    to_delete_from_db = set()
    to_update_in_db = []

    for group in duplicate_groups:
        sorted_group = order_group_by_keep_priority(group)

        found_a_valid_original = False
        group_is_safe_to_process = True

        # --- Шаг 1: Проверка на ошибки API (Safety Check) ---
        group_msg_ids = [r["message_id"] for r in sorted_group]

        for db_record in sorted_group:
            msg_id = db_record["message_id"]
            api_result = verified_messages.get(msg_id)

            if isinstance(api_result, Exception):
                log.warning(
                    f"Не удалось проверить сообщение {msg_id} "
                    f"({type(api_result).__name__}). "
                    f"Группа дубликатов {group_msg_ids} пропущена "
                    f"во избежание потери данных."
                )
                group_is_safe_to_process = False
                break

        if not group_is_safe_to_process:
            continue

        # --- Шаг 2: Основная логика ---
        for db_record in sorted_group:
            msg_id = db_record["message_id"]
            api_result = verified_messages.get(msg_id)

            if api_result is None or api_result.empty:
                log.debug(f"Сообщение {msg_id} не найдено в Telegram. Будет удалено из БД.")
                to_delete_from_db.add(msg_id)
            else:
                api_audio_attrs = get_audio_attributes(api_result)
                if api_audio_attrs:
                    # Проверка на изменение контента
                    if (
                        api_audio_attrs.file_unique_id == db_record["file_unique_id"]
                        and api_audio_attrs.file_name == db_record["file_name"]
                        and api_audio_attrs.file_size == db_record["file_size"]
                        and api_audio_attrs.duration == db_record["duration"]
                        and api_audio_attrs.performer == db_record["performer"]
                        and api_audio_attrs.title == db_record["title"]
                    ):
                        if not found_a_valid_original:
                            found_a_valid_original = True
                            log.debug(
                                f"Сообщение {msg_id} ('{db_record['file_name']}') является валидным оригиналом."
                            )
                        else:
                            to_delete_from_tg.add(msg_id)
                    else:
                        log.info(
                            f"Контент сообщения {msg_id} изменился. Запись в БД будет обновлена."
                        )
                        to_update_in_db.append(api_result)
                else:
                    log.info(f"Сообщение {msg_id} больше не аудио. Запись будет удалена из БД.")
                    to_delete_from_db.add(msg_id)

    return ClassificationResult(to_delete_from_tg, to_delete_from_db, to_update_in_db)
