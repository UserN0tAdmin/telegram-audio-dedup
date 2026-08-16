"""Применение изменений: архивация, удаление сообщений, обновление БД."""

import datetime
import itertools
import re

import aiosqlite
from pyrogram import Client, types

from .config import (
    ABORT_DELETE_ON_ARCHIVE_FAILURE,
    ARCHIVE_BEFORE_DELETE,
    ARCHIVE_HIDE_SENDER,
    ARCHIVE_MODE,
    BATCH_DELETE_SIZE,
    DRY_RUN,
    REVOKE_PRIVATE_CHATS,
)
from .logger import log
from .state import GLOBAL_IGNORE_REGEX, IGNORE_MESSAGES, IGNORE_REGEX, chat_label, remember_chat
from .tg import _get_audio_attributes
from .typedefs import ChatID, MessageID


async def _get_regex_protected_ids(
    conn: aiosqlite.Connection,
    chat_id: ChatID,
    tg_ids: list[MessageID],
    patterns: list[re.Pattern[str]],
) -> set[MessageID]:
    """Возвращает ID сообщений, чьи метаданные матчатся regex-защитой.

    Проверяет ``file_name``/``performer``/``title`` из локальной БД.

    Args:
        conn: Соединение с БД.
        chat_id: ID чата.
        tg_ids: Кандидаты на удаление.
        patterns: Компилированные regex-паттерны защиты.

    Returns:
        Множество message_id, защищённых от удаления.
    """
    protected: set[MessageID] = set()
    CHUNK = 4000

    for i in range(0, len(tg_ids), CHUNK):
        chunk = tg_ids[i : i + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = await conn.execute_fetchall(
            f"SELECT message_id, file_name, performer, title "
            f"FROM audios WHERE chat_id = ? AND message_id IN ({placeholders})",
            (chat_id, *chunk),
        )
        for msg_id, *fields in rows:
            hit = next(
                ((p, f) for f in fields if f for p in patterns if p.search(f)),
                None,
            )
            if hit:
                protected.add(msg_id)
                log.info(
                    f"Regex-защита: сообщение {msg_id} ('{hit[1]}') совпало с '{hit[0].pattern}'."
                )
    return protected


async def _filter_ignored_ids(
    conn: aiosqlite.Connection,
    chat_id: ChatID,
    tg_ids: list[MessageID],
) -> list[MessageID]:
    """Отсекает сообщения из ignore-листа и regex-защиты.

    Args:
        conn: Соединение с БД (для чтения метаданных regex-защиты).
        chat_id: ID чата.
        tg_ids: Кандидаты на удаление.

    Returns:
        Отфильтрованный список message_id без побочных эффектов в БД.
    """
    ignore_list = IGNORE_MESSAGES.get(chat_id, set())
    final = [msg_id for msg_id in tg_ids if msg_id not in ignore_list]
    skipped = len(tg_ids) - len(final)
    if skipped:
        log.info(
            f"Пропускаю удаление {skipped} сообщений из чата {chat_label(chat_id)} (в списке игнорирования)."
        )

    patterns = IGNORE_REGEX.get(chat_id, []) + GLOBAL_IGNORE_REGEX
    if patterns and final:
        protected = await _get_regex_protected_ids(conn, chat_id, final, patterns)
        if protected:
            final = [msg_id for msg_id in final if msg_id not in protected]
            log.info(
                f"Пропускаю удаление {len(protected)} сообщений из чата {chat_label(chat_id)} (regex-защита)."
            )

    return final


def _log_planned_changes(
    chat_id: ChatID,
    tg_ids: list[MessageID],
    db_delete_ids: list[MessageID],
    db_update_records: list[types.Message],
    archive_target_id: ChatID | None,
) -> None:
    """Печатает план изменений (режим DRY_RUN) без побочных эффектов.

    Args:
        chat_id: ID чата.
        tg_ids: Планируемые к удалению из Telegram.
        db_delete_ids: Планируемые к удалению из БД.
        db_update_records: Планируемые к обновлению в БД.
        archive_target_id: ID архивного чата, если архивация включена.
    """
    log.info("РЕЖИМ СИМУЛЯЦИИ АКТИВЕН. Никаких реальных изменений не будет.")
    if tg_ids:
        if ARCHIVE_BEFORE_DELETE and archive_target_id is not None:
            log.info(
                f"Планируется {ARCHIVE_MODE} {len(tg_ids)} сообщений из чата {chat_label(chat_id)} "
                f"в архив {archive_target_id} перед удалением."
            )
        log.info(
            f"Планируется к удалению из Telegram в чате {chat_label(chat_id)} ({len(tg_ids)} шт.): {tg_ids}"
        )
    if db_delete_ids:
        log.info(
            f"Планируется к удалению из БД для чата {chat_label(chat_id)} ({len(db_delete_ids)} шт.): {db_delete_ids}"
        )
    if db_update_records:
        update_info = []
        for msg in db_update_records:
            attrs = _get_audio_attributes(msg)
            name = attrs.file_name if attrs else "<not-audio>"
            update_info.append(f"{msg.id} -> '{name}'")
        log.info(
            f"Планируется к обновлению в БД для чата {chat_label(chat_id)} ({len(db_update_records)} шт.): {update_info}"
        )


async def _send_archive_header(
    app: Client, archive_target_id: ChatID, chat_id: ChatID, count: int
) -> None:
    """Шлёт в архив текстовый разделитель перед пересылкой батчей чата.

    Косметика: падение заголовка не должно останавливать архивацию.

    Args:
        app: Клиент Telegram.
        archive_target_id: ID архивного чата.
        chat_id: ID обрабатываемого чата.
        count: Число сообщений в плане удаления.
    """
    try:
        chat = await app.get_chat(chat_id)
        remember_chat(chat)
    except Exception:
        pass

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"Удалено из чата {chat_label(chat_id)}\nФайлов (план): {count}\n{ts}"
    try:
        await app.send_message(archive_target_id, text)
    except Exception as e:
        log.warning(f"Не удалось отправить заголовок архива для чата {chat_label(chat_id)}: {e}")


async def _archive_chunk(
    app: Client, chat_id: ChatID, archive_target_id: ChatID, chunk: list[MessageID]
) -> bool:
    """Архивирует батч (forward или copy) сообщений.

    ``hide_sender_name`` применяется только при forward (copy и так без автора).

    Args:
        app: Клиент Telegram.
        chat_id: ID чата-источника.
        archive_target_id: ID архивного чата.
        chunk: Батч message_id.

    Returns:
        ``True``, только если ВЕСЬ батч успешно заархивирован.
    """
    try:
        if ARCHIVE_MODE == "copy":
            for msg_id in chunk:
                await app.copy_message(
                    chat_id=archive_target_id, from_chat_id=chat_id, message_id=msg_id
                )
            archived = len(chunk)
        else:
            result = await app.forward_messages(
                chat_id=archive_target_id,
                from_chat_id=chat_id,
                message_ids=chunk,
                hide_sender_name=ARCHIVE_HIDE_SENDER,
            )
            archived = len(result) if isinstance(result, list) else 1
    except Exception as e:
        log.error(f"Архивация батча из чата {chat_label(chat_id)} провалена: {e}")
        return False

    if archived != len(chunk):
        log.warning(
            f"Заархивировано лишь {archived}/{len(chunk)} из чата {chat_label(chat_id)}; батч не будет удалён."
        )
        return False
    return True


async def _archive_and_delete_messages(
    app: Client,
    chat_id: ChatID,
    conn: aiosqlite.Connection,
    tg_ids: list[MessageID],
    archive_target_id: ChatID | None,
) -> None:
    """Архивирует (опц.) и пакетно удаляет сообщения из Telegram и БД.

    Инвариант: батч удаляется только после успешной архивации (когда она
    включена и ``ABORT_DELETE_ON_ARCHIVE_FAILURE=True``). Из БД чистятся
    только реально исчезнувшие из TG записи. Не коммитит — коммит
    в оркестраторе.

    Args:
        app: Клиент Telegram.
        chat_id: ID обрабатываемого чата.
        conn: Соединение с БД.
        tg_ids: Сообщения к удалению из Telegram.
        archive_target_id: ID архивного чата или ``None``.
    """
    if not tg_ids:
        return

    archive_enabled = ARCHIVE_BEFORE_DELETE and archive_target_id is not None
    if archive_enabled and archive_target_id == chat_id:
        log.error(f"Архивная цель совпадает с чатом {chat_label(chat_id)} — архивация невозможна.")
        if ABORT_DELETE_ON_ARCHIVE_FAILURE:
            log.error("Удаление в этом чате пропущено во избежание потери данных.")
            return
        archive_enabled = False

    log.info(
        f"Обработка {len(tg_ids)} дубликатов в чате {chat_label(chat_id)} "
        f"(архивация: {'вкл' if archive_enabled else 'выкл'})..."
    )

    if archive_enabled:
        await _send_archive_header(app, archive_target_id, chat_id, len(tg_ids))

    batches = list(itertools.batched(tg_ids, BATCH_DELETE_SIZE))
    total = len(batches)

    for i, chunk_tuple in enumerate(batches):
        batch_num = i + 1
        chunk = list(chunk_tuple)
        log.info(f"Батч {batch_num}/{total} ({len(chunk)} сообщений)...")

        if archive_enabled:
            ok = await _archive_chunk(app, chat_id, archive_target_id, chunk)
            if not ok and ABORT_DELETE_ON_ARCHIVE_FAILURE:
                log.error(f"Батч {batch_num}/{total} не заархивирован — удаление пропущено.")
                continue

        try:
            deleted = await app.delete_messages(chat_id, chunk, revoke=REVOKE_PRIVATE_CHATS)
            deleted_count = deleted if isinstance(deleted, int) else len(chunk)

            if deleted_count != len(chunk):
                log.warning(
                    f"Батч {batch_num}/{total}: Telegram удалил {deleted_count}/{len(chunk)}; "
                    f"уточняю, какие записи реально исчезли..."
                )
                check = await app.get_messages(chat_id, chunk)
                check = check if isinstance(check, list) else [check]
                alive = {m.id for m in check if m and not m.empty}
                gone = [m_id for m_id in chunk if m_id not in alive]
            else:
                gone = chunk

            await conn.executemany(
                "DELETE FROM audios WHERE chat_id = ? AND message_id = ?",
                [(chat_id, mid) for mid in gone],
            )
            log.info(
                f"Батч {batch_num}/{total}: удалено {deleted_count}, очищено записей БД: {len(gone)}."
            )
        except Exception as e:
            log.error(
                f"Не удалось удалить батч {batch_num}/{total} в чате {chat_label(chat_id)}: {e}"
            )


async def _delete_db_records(
    conn: aiosqlite.Connection, chat_id: ChatID, db_delete_ids: list[MessageID]
) -> None:
    """Удаляет устаревшие записи из БД (сообщений уже нет в TG).

    Args:
        conn: Соединение с БД.
        chat_id: ID чата.
        db_delete_ids: message_id записей к удалению.
    """
    if not db_delete_ids:
        return
    log.info(
        f"Чистка {len(db_delete_ids)} устаревших записей из БД для чата {chat_label(chat_id)}..."
    )
    await conn.executemany(
        "DELETE FROM audios WHERE chat_id = ? AND message_id = ?",
        [(chat_id, mid) for mid in db_delete_ids],
    )
    log.info(f"Удалено {len(db_delete_ids)} устаревших записей для чата {chat_label(chat_id)}.")


async def _apply_db_updates(
    conn: aiosqlite.Connection, chat_id: ChatID, db_update_records: list[types.Message]
) -> None:
    """Обновляет изменившиеся записи в БД. Не коммитит.

    Args:
        conn: Соединение с БД.
        chat_id: ID чата (для логов).
        db_update_records: Актуальные объекты сообщений Telegram.
    """
    if not db_update_records:
        return
    log.info(
        f"Обновление {len(db_update_records)} изменённых записей в БД для чата {chat_label(chat_id)}..."
    )
    update_data = []
    for r in db_update_records:
        attrs = _get_audio_attributes(r)
        if attrs:
            update_data.append((*attrs, r.chat.id, r.id))
    await conn.executemany(
        "UPDATE audios SET file_unique_id=?, file_name=?, file_size=?, duration=?, "
        "performer=?, title=? WHERE chat_id=? AND message_id=?",
        update_data,
    )
    log.info(f"Обновлено {len(db_update_records)} записей для чата {chat_label(chat_id)}.")


async def handle_database_changes(
    app: Client,
    chat_id: ChatID,
    conn: aiosqlite.Connection,
    tg_ids: list[MessageID],
    db_delete_ids: list[MessageID],
    db_update_records: list[types.Message],
    archive_target_id: ChatID | None = None,
) -> None:
    """Оркестратор применения изменений единой транзакцией.

    Под-функции не коммитят — коммит делается здесь один раз.

    Args:
        app: Клиент Telegram.
        chat_id: ID обрабатываемого чата.
        conn: Соединение с БД.
        tg_ids: Сообщения к удалению из Telegram.
        db_delete_ids: Записи к удалению из БД.
        db_update_records: Сообщения к обновлению в БД.
        archive_target_id: ID архивного чата или ``None``.
    """
    if not any([tg_ids, db_delete_ids, db_update_records]):
        log.debug(f"Для чата {chat_label(chat_id)} нет запланированных изменений.")
        return

    log.info(f"\n{'=' * 10}\nПриведение в исполнение плана для чата {chat_label(chat_id)}...")

    final_tg_ids = await _filter_ignored_ids(conn, chat_id, tg_ids)

    if DRY_RUN:
        _log_planned_changes(
            chat_id, final_tg_ids, db_delete_ids, db_update_records, archive_target_id
        )
        return

    await _archive_and_delete_messages(app, chat_id, conn, final_tg_ids, archive_target_id)
    await _delete_db_records(conn, chat_id, db_delete_ids)
    await _apply_db_updates(conn, chat_id, db_update_records)

    await conn.commit()
