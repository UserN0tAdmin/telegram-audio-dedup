"""Тесты разбора аргументов CLI (dedup.cli)."""

import pytest

from dedup.cli import collect_cli_overrides, parse_arguments
from dedup.errors import ConfigError
from dedup.search import RESULT_LIMIT, SCORE_CUTOFF


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


def test_search_min_score_defaults_to_score_cutoff(monkeypatch):
    assert parse(["search", "q"], monkeypatch).min_score == int(SCORE_CUTOFF)


def test_search_min_score_accepts_custom_value(monkeypatch):
    args = parse(["search", "q", "--min-score", "50"], monkeypatch)
    assert args.min_score == 50


def test_search_wratio_flag(monkeypatch):
    assert parse(["search", "q"], monkeypatch).wratio is False
    assert parse(["search", "q", "--wratio"], monkeypatch).wratio is True


def test_search_limit_defaults_to_result_limit(monkeypatch):
    assert parse(["search", "q"], monkeypatch).limit == RESULT_LIMIT


def test_search_limit_accepts_custom_value(monkeypatch):
    args = parse(["search", "q", "--limit", "5"], monkeypatch)
    assert args.limit == 5


@pytest.mark.parametrize("bad", ["0", "-1", "abc"])
def test_search_limit_rejects_non_positive(bad, monkeypatch):
    monkeypatch.setattr("sys.argv", ["prog", "search", "q", "--limit", bad])
    with pytest.raises(SystemExit) as excinfo:
        parse_arguments()
    assert excinfo.value.code == 2


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


# --- Флаги перекрытия конфигурации ---


def test_set_flag_repeats(monkeypatch):
    args = parse(
        ["--set", "core.dry_run=false", "--set", "fuzzy_matching.threshold=0.8", "search", "q"],
        monkeypatch,
    )
    assert collect_cli_overrides(args) == {
        ("core", "dry_run"): "false",
        ("fuzzy_matching", "threshold"): "0.8",
    }


def test_set_after_subcommand_merges_with_before(monkeypatch):
    args = parse(
        ["--set", "core.report_only=true", "search", "q", "--set", "core.dry_run=false"],
        monkeypatch,
    )
    assert collect_cli_overrides(args) == {
        ("core", "report_only"): "true",
        ("core", "dry_run"): "false",
    }


def test_set_last_occurrence_wins(monkeypatch):
    args = parse(["--set", "core.dry_run=true", "--set", "core.dry_run=false"], monkeypatch)
    assert collect_cli_overrides(args)[("core", "dry_run")] == "false"


def test_set_value_may_contain_equals(monkeypatch):
    args = parse(["--set", "ignore_regex.*=(?i)x=y"], monkeypatch)
    assert collect_cli_overrides(args)[("ignore_regex", "*")] == "(?i)x=y"


def test_set_empty_value_clears_option(monkeypatch):
    args = parse(["--set", "pyrogram.proxy_url="], monkeypatch)
    assert collect_cli_overrides(args)[("pyrogram", "proxy_url")] == ""


@pytest.mark.parametrize("bad", ["nodots", "a.b.c=1", "=v", "a.=v", ".b=v", "a.b"])
def test_set_rejects_malformed_entries(bad, monkeypatch):
    args = parse(["--set", bad], monkeypatch)
    with pytest.raises(ConfigError):
        collect_cli_overrides(args)


def test_dry_run_tri_state(monkeypatch):
    assert ("core", "dry_run") not in collect_cli_overrides(parse([], monkeypatch))
    assert collect_cli_overrides(parse(["--dry-run"], monkeypatch))[("core", "dry_run")] == "true"
    assert (
        collect_cli_overrides(parse(["--no-dry-run"], monkeypatch))[("core", "dry_run")] == "false"
    )


def test_dry_run_after_subcommand_wins(monkeypatch):
    args = parse(["--dry-run", "search", "q", "--no-dry-run"], monkeypatch)
    assert collect_cli_overrides(args)[("core", "dry_run")] == "false"


def test_set_wins_over_sugar(monkeypatch):
    args = parse(["--dry-run", "--set", "core.dry_run=false"], monkeypatch)
    assert collect_cli_overrides(args)[("core", "dry_run")] == "false"


def test_chat_flag(monkeypatch):
    args = parse(["--chat", "@music,-1001234567890"], monkeypatch)
    assert collect_cli_overrides(args)[("core", "chat_list")] == "@music,-1001234567890"


def test_download_positional_chat_not_confused_with_override(monkeypatch):
    args = parse(["--chat", "@outer", "download", "@inner"], monkeypatch)
    assert args.chat == "@inner"
    assert collect_cli_overrides(args)[("core", "chat_list")] == "@outer"


def test_threshold_flag(monkeypatch):
    args = parse(["--threshold", "0.85"], monkeypatch)
    assert collect_cli_overrides(args)[("fuzzy_matching", "threshold")] == "0.85"


@pytest.mark.parametrize("bad", ["1.5", "-0.1", "abc"])
def test_threshold_rejects_out_of_range(bad, monkeypatch):
    monkeypatch.setattr("sys.argv", ["prog", "--threshold", bad])
    with pytest.raises(SystemExit) as excinfo:
        parse_arguments()
    assert excinfo.value.code == 2
