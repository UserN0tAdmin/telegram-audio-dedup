"""Тесты разбора аргументов CLI (dedup.cli)."""

import pytest

from dedup.cli import parse_arguments


def parse(argv, monkeypatch):
    monkeypatch.setattr("sys.argv", ["prog", *argv])
    return parse_arguments()


def test_no_subcommand_means_full_run(monkeypatch):
    args = parse([], monkeypatch)
    assert args.command is None


def test_search_takes_query(monkeypatch):
    args = parse(["search", "beattles hey jude"], monkeypatch)
    assert args.command == "search"
    assert args.query == "beattles hey jude"


def test_search_requires_query(monkeypatch):
    monkeypatch.setattr("sys.argv", ["prog", "search"])
    with pytest.raises(SystemExit) as excinfo:
        parse_arguments()
    assert excinfo.value.code == 2


def test_search_min_score_defaults_to_70(monkeypatch):
    assert parse(["search", "q"], monkeypatch).min_score == 70


def test_search_min_score_accepts_custom_value(monkeypatch):
    args = parse(["search", "q", "--min-score", "50"], monkeypatch)
    assert args.min_score == 50


def test_search_wratio_flag(monkeypatch):
    assert parse(["search", "q"], monkeypatch).wratio is False
    assert parse(["search", "q", "--wratio"], monkeypatch).wratio is True


@pytest.mark.parametrize("bad", ["-1", "101", "abc"])
def test_search_min_score_rejects_out_of_range(bad, monkeypatch):
    monkeypatch.setattr("sys.argv", ["prog", "search", "q", "--min-score", bad])
    with pytest.raises(SystemExit) as excinfo:
        parse_arguments()
    assert excinfo.value.code == 2


def test_repair_and_report_subcommands(monkeypatch):
    assert parse(["repair"], monkeypatch).command == "repair"
    assert parse(["report"], monkeypatch).command == "report"


def test_download_takes_raw_string_identifier(monkeypatch):
    args = parse(["download", "@some_user"], monkeypatch)
    assert args.command == "download"
    assert args.chat == "@some_user"
    assert parse(["download", "-1001234567890"], monkeypatch).chat == "-1001234567890"


def test_export_filenames_with_numeric_chat(monkeypatch):
    args = parse(["export", "filenames", "123"], monkeypatch)
    assert args.command == "export"
    assert args.export_command == "filenames"
    assert args.chat == 123


@pytest.mark.parametrize("action", ["cleaned-names", "cleaned-meta", "xlsx"])
def test_export_actions_default_to_full_database(action, monkeypatch):
    args = parse(["export", action], monkeypatch)
    assert args.export_command == action
    assert args.chat == 0


def test_export_requires_action(monkeypatch):
    monkeypatch.setattr("sys.argv", ["prog", "export"])
    with pytest.raises(SystemExit) as excinfo:
        parse_arguments()
    assert excinfo.value.code == 2


def test_export_username_without_session_fails(monkeypatch):
    # Резолв @username требует файла сессии, которого в тестах нет
    monkeypatch.setattr("sys.argv", ["prog", "export", "filenames", "@nobody"])
    with pytest.raises(SystemExit) as excinfo:
        parse_arguments()
    assert excinfo.value.code == 2
