"""Тесты Telegram-слоя (dedup.tg) на FakeClient: резолв, ignore-списки, права."""

import itertools
from argparse import Namespace
from types import SimpleNamespace

import pytest
from fakes import ME_ID, FakeClient, make_chat, make_member, make_message
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import PeerIdInvalid, UsernameInvalid, UsernameNotOccupied, UserNotParticipant

from dedup import state
from dedup.errors import IgnoreListResolutionError
from dedup.tg import (
    can_process_chat,
    fetch_audio_meta_chunk,
    populate_ignore_list,
    resolve_chat_identifiers,
)

CHAT = -1001500000001


def read_only_args(command="report"):
    return Namespace(command=command)


def full_run_args():
    return Namespace(command=None)


# --- resolve_chat_identifiers ---


async def test_numeric_identifiers_pass_through_without_api():
    client = FakeClient()
    result = await resolve_chat_identifiers(client, ["123", " 456 ", ""])
    assert result == [123, 456]
    assert client.calls == {}  # числовые ID не требуют обращений к API


async def test_username_resolved_via_api_and_remembered():
    client = FakeClient()
    client.chats["@music"] = make_chat(CHAT, title="Музыка", username="music")
    result = await resolve_chat_identifiers(client, ["@music"])
    assert result == [CHAT]
    assert state.CHAT_LABELS[CHAT] == ("Музыка", "music")


async def test_input_and_result_dedup_preserves_order():
    client = FakeClient()
    client.chats["@same"] = make_chat(5)
    result = await resolve_chat_identifiers(client, ["7", "7", "@same", "5", ""])
    assert result == [7, 5]


@pytest.mark.parametrize("error", [UsernameNotOccupied, PeerIdInvalid, UsernameInvalid])
async def test_known_username_errors_skip_identifier(error):
    client = FakeClient()
    client.chats["@gone"] = error("нет такого")
    assert await resolve_chat_identifiers(client, ["@gone", "100"]) == [100]


async def test_unexpected_error_skips_identifier():
    client = FakeClient()
    client.chats["@boom"] = RuntimeError("что-то сломалось")
    assert await resolve_chat_identifiers(client, ["@boom", "200"]) == [200]


# --- populate_ignore_list ---


async def test_numeric_ignore_lists_fill_globals_without_api(configure_settings):
    import re

    pattern = re.compile(r"never delete this")
    configure_settings(
        ignore={
            "raw_ignore_list": {str(CHAT): {10, 20}},
            "raw_ignore_regex": {str(CHAT + 1): [pattern], "*": [re.compile(r"global")]},
        }
    )
    client = FakeClient()

    await populate_ignore_list(client)

    assert client.calls == {}  # всё числовое — без обращений к API
    assert state.IGNORE_MESSAGES[CHAT] == {10, 20}
    assert state.IGNORE_REGEX[CHAT + 1] == [pattern]
    assert [re.compile(r"global")] == state.GLOBAL_IGNORE_REGEX


async def test_username_in_ignore_list_resolved_via_api(configure_settings):
    configure_settings(ignore={"raw_ignore_list": {"@user": {1}}})
    client = FakeClient()
    client.chats["@user"] = make_chat(4242, title="Личный")

    await populate_ignore_list(client)

    assert state.IGNORE_MESSAGES[4242] == {1}


async def test_failed_ignore_resolution_raises(configure_settings):
    configure_settings(ignore={"raw_ignore_list": {"@bad": {1}}})
    client = FakeClient()
    client.chats["@bad"] = ValueError("недоступен")

    with pytest.raises(IgnoreListResolutionError):
        await populate_ignore_list(client)


async def test_empty_ignore_config_is_noop(configure_settings):
    client = FakeClient()
    await populate_ignore_list(client)
    assert client.calls == {}
    assert not state.IGNORE_MESSAGES and not state.IGNORE_REGEX and not state.GLOBAL_IGNORE_REGEX


# --- can_process_chat ---


async def test_private_chat_always_allowed():
    client = FakeClient()
    client.chats[CHAT] = make_chat(CHAT, chat_type=ChatType.PRIVATE, first_name="Друг")
    assert await can_process_chat(client, CHAT, ME_ID, full_run_args()) is True


async def test_read_only_public_chat_allowed_for_non_participant():
    client = FakeClient()
    client.chats[CHAT] = make_chat(CHAT, username="public_chan")
    client.members[(CHAT, ME_ID)] = UserNotParticipant("не участник")
    assert await can_process_chat(client, CHAT, ME_ID, read_only_args()) is True


async def test_full_run_denied_for_non_participant(configure_settings):
    configure_settings(core={"dry_run": False})  # боевой режим, не read-only
    client = FakeClient()
    client.chats[CHAT] = make_chat(CHAT, username="public_chan")
    client.members[(CHAT, ME_ID)] = UserNotParticipant("не участник")
    assert await can_process_chat(client, CHAT, ME_ID, full_run_args()) is False


async def test_admin_with_delete_rights_allowed(configure_settings):
    configure_settings(core={"dry_run": False})
    client = FakeClient()
    client.chats[CHAT] = make_chat(CHAT)
    client.members[(CHAT, ME_ID)] = make_member(
        ChatMemberStatus.ADMINISTRATOR,
        privileges=SimpleNamespace(can_delete_messages=True),
    )
    assert await can_process_chat(client, CHAT, ME_ID, full_run_args()) is True


async def test_admin_without_delete_rights_denied_in_full_run(configure_settings):
    configure_settings(core={"dry_run": False})
    client = FakeClient()
    client.chats[CHAT] = make_chat(CHAT)
    client.members[(CHAT, ME_ID)] = make_member(
        ChatMemberStatus.ADMINISTRATOR,
        privileges=SimpleNamespace(can_delete_messages=False),
    )
    assert await can_process_chat(client, CHAT, ME_ID, full_run_args()) is False
    assert await can_process_chat(client, CHAT, ME_ID, read_only_args()) is True


async def test_owner_allowed(configure_settings):
    configure_settings(core={"dry_run": False})
    client = FakeClient()
    client.chats[CHAT] = make_chat(CHAT)
    client.members[(CHAT, ME_ID)] = make_member(ChatMemberStatus.OWNER)
    assert await can_process_chat(client, CHAT, ME_ID, full_run_args()) is True


async def test_dry_run_mode_counts_as_read_only(configure_settings):
    configure_settings(core={"dry_run": True})
    client = FakeClient()
    client.chats[CHAT] = make_chat(CHAT)
    client.members[(CHAT, ME_ID)] = make_member(
        ChatMemberStatus.MEMBER, privileges=SimpleNamespace(can_delete_messages=False)
    )
    assert await can_process_chat(client, CHAT, ME_ID, full_run_args()) is True


async def test_unavailable_chat_denied():
    client = FakeClient()
    client.chats[CHAT] = PeerIdInvalid("нет чата")
    assert await can_process_chat(client, CHAT, ME_ID, full_run_args()) is False


# --- get_messages-обёртки ---


async def test_fetch_audio_meta_chunk_maps_messages():
    client = FakeClient()
    client.messages = {
        1: make_message(1, file_name="a.mp3", uid="U1"),
        2: make_message(2, empty=True),
    }
    result = await fetch_audio_meta_chunk(client, CHAT, [1, 2])
    assert result[0] is not None and result[0].file_name == "a.mp3"
    assert result[1] is None  # пустое сообщение -> не аудио


async def test_verify_messages_chunks_requests(configure_settings):
    from helpers import CHAT_ID

    from dedup.duplicates import _verify_messages_from_api

    configure_settings(performance={"verify_chunk_size": 2, "verify_concurrency": 2})
    client = FakeClient()
    client.messages = {i: make_message(i, uid=f"V{i}") for i in range(1, 6)}

    verified = await _verify_messages_from_api(client, CHAT_ID, [5, 4, 3, 2, 1])

    assert len(verified) == 5
    assert all(msg is not None and msg.id == mid for mid, msg in verified.items())
    # 5 id чанками по 2 -> 3 запроса, все id запрошены ровно по разу
    requested = [call[0][1] for call in client.calls["get_messages"]]
    assert len(requested) == 3
    assert sorted(itertools.chain.from_iterable(requested)) == [1, 2, 3, 4, 5]


async def test_verify_messages_maps_chunk_errors_to_exception(configure_settings):
    from helpers import CHAT_ID

    from dedup.duplicates import _verify_messages_from_api

    configure_settings(performance={"verify_chunk_size": 10})
    client = FakeClient()

    async def failing_get_messages(chat_id, message_ids):
        raise RuntimeError("API упал")

    client.get_messages = failing_get_messages

    verified = await _verify_messages_from_api(client, CHAT_ID, [1, 2])
    assert isinstance(verified[1], RuntimeError)
    assert isinstance(verified[2], RuntimeError)
