"""Клиент Telegram, резолв чатов, списки игнорирования, атрибуты аудио."""

import asyncio
from argparse import Namespace
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlparse

from mtproxy_bridge import is_mtproto_link, needs_padded_transport, start_local_bridge

# Используется kurigram
from pyrogram import Client, types
from pyrogram.connection.transport.tcp import TCPAbridged, TCPIntermediatePadded
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import (
    PeerIdInvalid,
    UsernameInvalid,
    UsernameNotOccupied,
    UserNotParticipant,
)

from .context import get_settings
from .errors import IgnoreListResolutionError
from .logger import log
from .state import GLOBAL_IGNORE_REGEX, IGNORE_MESSAGES, IGNORE_REGEX, chat_label, remember_chat
from .typedefs import AudioMeta, ChatID, MessageID

# Самый большой блок, содержащий всю "умную" часть скрипта: получение сообщений из Telegram, их анализ, поиск дубликатов и выполнение действий.


async def create_telegram_client() -> Client | None:
    """Создает, настраивает и возвращает экземпляр клиента Kurigram.

    Returns:
        Настроенный клиент или ``None`` при критической ошибке конфигурации
        (например, кривой прокси).
    """
    p = get_settings().pyrogram
    client_kwargs: dict[str, Any] = {
        "api_id": p.api_id,
        "api_hash": p.api_hash,
        "no_updates": True,
        "max_concurrent_transmissions": 10,
        "sleep_threshold": p.sleep_threshold,
        # "protocol_factory": TCPPadded, # todo в конфиг вынести
    }

    if p.proxy_url:
        if is_mtproto_link(p.proxy_url):
            try:
                local_port = await start_local_bridge(p.proxy_url)
                transport = (
                    TCPIntermediatePadded if needs_padded_transport(p.proxy_url) else TCPAbridged
                )
                client_kwargs["proxy"] = {
                    "scheme": "socks5",
                    "hostname": "127.0.0.1",
                    "port": local_port,
                }
                client_kwargs["protocol_factory"] = transport

                log.info(
                    f"MTProto-прокси из конфига поднят как локальный мост: "
                    f"127.0.0.1:{local_port} -> {p.proxy_url.split('server=')[-1].split('&')[0]}"
                )
            except Exception as e:
                log.critical(f"Не удалось поднять локальный мост для MTProto-прокси. Ошибка: {e}")
                return None
        else:
            try:
                parsed_proxy = urlparse(p.proxy_url)
                proxy_dict = {
                    "scheme": parsed_proxy.scheme,
                    "hostname": parsed_proxy.hostname,
                    "port": parsed_proxy.port,
                    "username": parsed_proxy.username,
                    "password": parsed_proxy.password,
                }
                client_kwargs["proxy"] = proxy_dict

                log.info(
                    f"Используется прокси: {proxy_dict['scheme']}://{proxy_dict['hostname']}:{proxy_dict['port']}"
                )
            except Exception as e:
                log.critical(
                    f"Не удалось распарсить URL прокси. Проверьте правильность ссылки в конфиге. Ошибка: {e}"
                )
                return None

    return Client(p.session_name, **client_kwargs)


async def resolve_chat_identifiers(
    app: Client,
    identifiers: Sequence[str],
    banner: str | None = "Преобразую идентификаторы чатов в числовые ID...",
) -> list[ChatID]:
    """Преобразует список идентификаторов чатов в уникальные числовые ID.

    Числовые ID проходят как есть, юзернеймы резолвятся конкурентными
    запросами к API; порядок входного списка сохраняется.

    Args:
        app: Клиент Telegram.
        identifiers: Список строк — числа, ``@usernames`` или ссылки.
        banner: Заголовок для лога; ``None`` — не печатать.

    Returns:
        Список уникальных числовых ID в порядке первого появления.
    """
    if banner:
        log.info(f"\n{'=' * 20}\n{banner}")

    # 1. Дедуп строк — чтобы не слать лишние запросы к API
    unique_identifiers: list[str] = []
    seen: set[str] = set()
    for ident in identifiers:
        clean = ident.strip()
        if not clean:
            continue
        if clean not in seen:
            seen.add(clean)
            unique_identifiers.append(clean)
        else:
            log.debug(f"Пропущен дубликат во входящем списке: '{clean}'")

    semaphore = asyncio.Semaphore(get_settings().performance.verify_concurrency)

    # 2. Резолв одного идентификатора: число -> само, имя -> API (или None)
    async def resolve_one(ident: str) -> ChatID | None:
        """Резолвит один идентификатор: число — само, имя — через API."""
        try:
            chat_id = int(ident)
            log.debug(f"Идентификатор '{ident}' распознан как числовой ID.")
            return chat_id
        except ValueError:
            log.debug(f"'{ident}' не является числом. Отправляю запрос к API.")
        async with semaphore:
            try:
                chat = await app.get_chat(ident)
            except (UsernameNotOccupied, PeerIdInvalid):
                log.error(f"Не удалось найти чат с именем '{ident}'. Он будет пропущен...")
                return None
            except UsernameInvalid:
                log.error(f"Имя пользователя '{ident}' невалидно. Оно будет пропущено...")
                return None
            except Exception as e:
                log.error(
                    f"Произошла непредвиденная ошибка при обработке '{ident}': {e}. Он будет пропущен..."
                )
                return None
        if not chat.id:
            log.error(f"Для имени пользователя '{ident}' не получен id. Будет пропущен...")
            return None
        remember_chat(chat)
        log.info(f"Имя пользователя '{ident}' успешно преобразовано в ID: {chat.id}")
        return chat.id

    # gather сохраняет порядок задач -> порядок входа сохраняется сам собой
    results = await asyncio.gather(*(resolve_one(i) for i in unique_identifiers))

    # 3. Дедуп ID — юзернейм и число могут указывать на один чат
    seen_ids: set[ChatID] = set()
    final_unique_ids: list[ChatID] = []
    duplicates_found: list[ChatID] = []
    for chat_id in results:
        if chat_id is None:
            continue
        if chat_id in seen_ids:
            duplicates_found.append(chat_id)
        else:
            seen_ids.add(chat_id)
            final_unique_ids.append(chat_id)

    if duplicates_found:
        duplicate_counts = Counter(duplicates_found)
        log.warning(
            f"В итоговом списке ID обнаружены дубликаты (возможно, юзернейм указывает на тот же ID): "
            f"{dict(duplicate_counts)}. Каждый чат будет обработан только один раз."
        )

    return final_unique_ids


async def resolve_and_validate_archive_target(app: Client, me_id: int) -> ChatID | None:
    """Резолвит ARCHIVE_TARGET в числовой ID и проверяет возможность записи.

    ``'me'``/``'self'`` → Избранное. Жёстко проверяются только каналы (нужны
    права на постинг); для групп полагаемся на runtime fail-safe в
    ``_archive_chunk``.

    Args:
        app: Клиент Telegram.
        me_id: ID текущего аккаунта.

    Returns:
        ID архивного чата или ``None``, если цель недоступна/нет прав.
    """
    cfg = get_settings().archive
    log.info(f"\n{'=' * 20}\nПроверяю архивную цель '{cfg.archive_target}'...")
    resolved = await resolve_chat_identifiers(app, [cfg.archive_target], banner=None)
    if not resolved:
        log.error(f"Не удалось разрешить archive_target='{cfg.archive_target}'.")
        return None
    target_id = resolved[0]

    try:
        chat = await app.get_chat(target_id)
        remember_chat(chat)
    except Exception as e:
        log.error(f"Архивный чат '{cfg.archive_target}' ({target_id}) недоступен: {e}")
        return None

    if target_id == me_id:
        log.info("Архивная цель: Избранное (Saved Messages).")
        return target_id

    if chat.type == ChatType.CHANNEL:
        try:
            member = await app.get_chat_member(target_id, me_id)
        except UserNotParticipant:
            log.error(f"Вы не участник канала {target_id} — публикация в архив невозможна.")
            return None
        privs = getattr(member, "privileges", None)
        can_post = bool(privs and privs.can_post_messages)
        if (
            member.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
            or not can_post
        ):
            log.error(f"Нет прав на публикацию в канал {target_id}.")
            return None

    log.info(
        f"Архивная цель валидна: {target_id} (режим: {cfg.archive_mode}, hide_sender={cfg.archive_hide_sender})."
    )
    return target_id


async def populate_ignore_list(app: Client) -> None:
    """Обрабатывает RAW_IGNORE_LIST и RAW_IGNORE_REGEX.

    Разрешает юзернеймы в ID и заполняет ``IGNORE_MESSAGES`` /
    ``IGNORE_REGEX`` / ``GLOBAL_IGNORE_REGEX``.

    Args:
        app: Клиент Telegram для резолва юзернеймов.

    Raises:
        IgnoreListResolutionError: Если какой-либо идентификатор из списков
            исключений не удалось проверить через API.
    """
    ignore_cfg = get_settings().ignore
    if not ignore_cfg.raw_ignore_list and not ignore_cfg.raw_ignore_regex:
        return

    log.info(f"\n{'=' * 20}\nОбработка списков исключений (ignore_list / ignore_regex)...")

    # (key, payload, applier) — applier знает, куда положить данные после резолвинга
    usernames_to_resolve: list[tuple[str, Callable[[int], None]]] = []

    def _dispatch(key: str, apply: Callable[[int], None]) -> None:
        try:
            apply(int(key))
        except ValueError:
            usernames_to_resolve.append((key, apply))

    for key, msg_ids in ignore_cfg.raw_ignore_list.items():
        _dispatch(key, lambda cid, ids=msg_ids: IGNORE_MESSAGES[cid].update(ids))

    for key, patterns in ignore_cfg.raw_ignore_regex.items():
        if key == "*":
            GLOBAL_IGNORE_REGEX.extend(patterns)
            continue
        _dispatch(key, lambda cid, pats=patterns: IGNORE_REGEX[cid].extend(pats))

    if not usernames_to_resolve:
        log.info("Все идентификаторы в списках исключений корректны.")
        return

    log.info(f"Проверяю {len(usernames_to_resolve)} имен/ссылок...")
    semaphore = asyncio.Semaphore(get_settings().performance.verify_concurrency)

    async def resolve_task(identifier: str):
        """Резолвит один юзернейм ignore-списка в числовой ID."""
        async with semaphore:
            chat = await app.get_chat(identifier)
            remember_chat(chat)
            return chat.id

    tasks = [resolve_task(k) for k, _ in usernames_to_resolve]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [
        (name, r)
        for (name, _), r in zip(usernames_to_resolve, results, strict=True)
        if isinstance(r, Exception)
    ]
    if errors:
        log.critical(
            "ОШИБКА КОНФИГУРАЦИИ: Не удалось проверить идентификаторы в списках исключений:"
        )
        for username, exc in errors:
            log.critical(f" ID/Name '{username}' выдало: {exc}")
        raise IgnoreListResolutionError(f"Ошибок проверки имен: {len(errors)}")

    for (_, apply), chat_id in zip(usernames_to_resolve, results, strict=True):
        apply(chat_id)

    log.info(f"Успешно обработано {len(results)} юзернеймов.")


async def can_process_chat(app: Client, chat_id: ChatID, me_id: int, args: Namespace) -> bool:
    """Проверяет права доступа к чату.

    Args:
        app: Клиент Telegram.
        chat_id: ID проверяемого чата.
        me_id: ID текущего аккаунта.
        args: Аргументы CLI (определяют режим read-only).

    Returns:
        ``True``, если чат доступен для обработки в текущем режиме.
    """
    try:
        log.info(f"\n{'=' * 20}\nПроверка прав для чата {chat_label(chat_id)}...")

        try:
            chat = await app.get_chat(chat_id)
            remember_chat(chat)
        except PeerIdInvalid:
            log.error(f"Чат {chat_label(chat_id)} не найден или недоступен.")
            return False

        # 1. Определяем режим работы
        core = get_settings().core
        is_read_only = args.command in ("report", "download", "sync") or core.dry_run or core.report_only

        # 2. Личный чат — всегда разрешено
        if chat.type == ChatType.PRIVATE:
            log.info(f"Чат {chat_label(chat_id)} — личный диалог. Разрешено.")
            return True

        # 3. Проверяем членство
        try:
            member = await app.get_chat_member(chat_id, me_id)
        except UserNotParticipant:
            if is_read_only and chat.username:
                log.warning("Не участник, но чат публичный. Разрешено (read-only).")
                return True
            log.error(f"Не участник чата {chat_label(chat_id)}. Пропущен.")
            return False

        # 4. Read-only режим — права на удаление не нужны
        if is_read_only:
            log.info("Режим 'только чтение'. Разрешено.")
            return True

        # 5. Боевой режим — нужны права на удаление
        has_delete_rights = member.status == ChatMemberStatus.OWNER or (
            member.privileges and member.privileges.can_delete_messages
        )

        if has_delete_rights:
            log.info(f"Права на удаление в чате {chat_label(chat_id)} подтверждены.")
            return True

        log.error(f"Нет прав на удаление в чате {chat_label(chat_id)}. Пропущен.")
        return False

    except Exception as e:
        log.error(f"Ошибка проверки прав в чате {chat_label(chat_id)}: {e}")
        return False


def get_audio_attributes(message: types.Message | None) -> AudioMeta | None:
    """Проверяет, является ли сообщение аудио или аудио-документом.

    Args:
        message: Сообщение Telegram (может быть ``None``/empty/service).

    Returns:
        ``AudioMeta`` с атрибутами или ``None``, если это не аудиофайл.
    """
    if not message or message.empty or message.service:
        return None

    if message.audio:
        return AudioMeta(
            file_unique_id=message.audio.file_unique_id,
            file_name=message.audio.file_name,
            file_size=message.audio.file_size,
            duration=message.audio.duration or 0,
            performer=message.audio.performer,
            title=message.audio.title,
        )

    if (
        message.document
        and message.document.mime_type
        and message.document.mime_type.startswith("audio/")
    ):
        return AudioMeta(
            file_unique_id=message.document.file_unique_id,
            file_name=message.document.file_name,
            file_size=message.document.file_size,
            duration=0,
            performer=None,
            title=None,
        )

    return None


async def fetch_audio_meta_chunk(
    app: Client, chat_id: ChatID, message_ids: list[MessageID]
) -> list[AudioMeta | None]:
    """Загружает сообщения из Telegram и извлекает аудио-атрибуты.

    Args:
        app: Клиент Telegram.
        chat_id: ID чата.
        message_ids: Список message_id для загрузки.

    Returns:
        Список ``AudioMeta`` (или ``None`` для не-аудио/пустых сообщений),
        выровненный по ``message_ids``.
    """
    messages = await app.get_messages(chat_id, message_ids)
    return [get_audio_attributes(m) for m in messages]
