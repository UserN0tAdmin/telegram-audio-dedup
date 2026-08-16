"""Тесты state.py: метки чатов, резолв идентификаторов, кэши сессии."""

import argparse

import pytest
from fakes import make_chat
from pyrogram.enums import ChatType

from dedup import context, state


def test_remember_chat_channel():
    state.remember_chat(make_chat(-1001, title="Канал", username="chan"))
    assert state.CHAT_LABELS[-1001] == ("Канал", "chan")


def test_remember_chat_user_combines_names():
    user_chat = make_chat(
        42, chat_type=ChatType.PRIVATE, title=None, first_name="Иван", last_name="Петров"
    )
    state.remember_chat(user_chat)
    assert state.CHAT_LABELS[42] == ("Иван Петров", None)


def test_remember_chat_user_single_name():
    user_chat = make_chat(43, chat_type=ChatType.PRIVATE, title=None, first_name="Оля")
    state.remember_chat(user_chat)
    assert state.CHAT_LABELS[43] == ("Оля", None)


def test_chat_label_default_parts_is_id():
    # Дефолт фабрики: chat_label_parts = ("id",)
    assert state.chat_label(-1002003) == "-1002003"
    state.remember_chat(make_chat(-1002003, title="Неважно"))
    assert state.chat_label(-1002003) == "-1002003"


def test_chat_label_full_parts(configure_settings):
    configure_settings(logging={"chat_label_parts": ("title", "username", "id")})
    state.remember_chat(make_chat(-1001, title="Канал", username="chan"))
    assert state.chat_label(-1001) == "Канал [@chan | -1001]"


def test_chat_label_title_only_unknown_chat_falls_back_to_id(configure_settings):
    configure_settings(logging={"chat_label_parts": ("title",)})
    # Метки нет, юзернейм из несуществующей сессии не достаётся
    assert state.chat_label(-5555555) == "-5555555"


def test_chat_label_empty_parts_falls_back_to_id(configure_settings):
    configure_settings(logging={"chat_label_parts": ()})
    state.remember_chat(make_chat(-1001, title="Канал", username="chan"))
    assert state.chat_label(-1001) == "-1001"


def test_chat_label_username_part_skipped_when_unknown(configure_settings):
    configure_settings(logging={"chat_label_parts": ("username", "id")})
    state.remember_chat(make_chat(-1001, title="Канал"))  # username нет
    # username пуст -> единственная часть id
    assert state.chat_label(-1001) == "-1001"


def test_chat_id_or_username_numeric_passthrough():
    assert state.chat_id_or_username("12345") == 12345
    assert state.chat_id_or_username("-1001234567890") == -1001234567890


def test_chat_id_or_username_fails_without_session():
    with pytest.raises(argparse.ArgumentTypeError, match="не удалось найти"):
        state.chat_id_or_username("@no_such_user")


def test_session_lookup_returns_none_for_missing_session_file():
    # session_name фабрики указывает на несуществующий файл в tmp
    assert state._username_from_session(1) is None
    assert state._id_from_session("@anybody") is None


def test_get_settings_raises_when_not_set(monkeypatch):
    monkeypatch.setattr(context, "_settings", None)
    with pytest.raises(RuntimeError, match="Конфигурация не загружена"):
        context.get_settings()
