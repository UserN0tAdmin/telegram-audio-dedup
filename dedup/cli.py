"""Разбор аргументов командной строки (подкоманды repair/report/download/export/search)."""

import argparse
from argparse import Namespace

from .errors import ConfigError
from .search import SCORE_CUTOFF
from .state import chat_id_or_username


def min_score(value: str) -> int:
    """Type-функция argparse: целое 0..100 — порог схожести для ``search``.

    Args:
        value: Сырая строка из командной строки.

    Returns:
        Порог в процентах схожести.

    Raises:
        argparse.ArgumentTypeError: Если значение не целое или вне 0..100.
    """
    try:
        score = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"порог должен быть целым числом, получено '{value}'"
        ) from None
    if not 0 <= score <= 100:
        raise argparse.ArgumentTypeError(f"порог должен быть в диапазоне 0..100, получено {score}")
    return score


def fuzzy_threshold(value: str) -> float:
    """Type-функция argparse: число 0..1 — порог fuzzy для ``--threshold``.

    Args:
        value: Сырая строка из командной строки.

    Returns:
        Порог схожести.

    Raises:
        argparse.ArgumentTypeError: Если значение не число или вне 0..1.
    """
    try:
        threshold = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"порог должен быть числом, получено '{value}'") from None
    if not 0.0 <= threshold <= 1.0:
        raise argparse.ArgumentTypeError(f"порог должен быть в диапазоне 0..1, получено {value}")
    return threshold


def _override_parents() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    """Пара parent-парсеров с глобальными флагами перекрытия конфигурации.

    Первый — для топ-парсера (обычные дефолты), второй — для субпарсеров
    (``SUPPRESS``-дефолты и суффикс ``_post`` у dest): субпарсер разбирается
    в отдельный namespace и копирует наверх только фактически встреченные
    флаги, поэтому перекрытия работают и до, и после подкоманды, позднее
    указание приоритетнее.

    Returns:
        ``(pre, post)`` — parent-парсеры для топ-парсера и субпарсеров.
    """
    pre = argparse.ArgumentParser(add_help=False)
    post = argparse.ArgumentParser(add_help=False)

    for parser, suffix, suppress in ((pre, "", False), (post, "_post", True)):
        parser.add_argument(
            "--set",
            dest=f"config_set{suffix}",
            action="append",
            default=argparse.SUPPRESS if suppress else [],
            metavar="SECTION.OPTION=VALUE",
            help="Перекрыть опцию config.cfg на этот запуск (повторяемый), "
            "напр. --set fuzzy_matching.threshold=0.85",
        )
        parser.add_argument(
            "--dry-run",
            dest=f"dry_run{suffix}",
            action=argparse.BooleanOptionalAction,
            default=argparse.SUPPRESS if suppress else None,
            help="Перекрыть core.dry_run на этот запуск",
        )
        parser.add_argument(
            "--chat",
            dest=f"chat_override{suffix}",
            default=argparse.SUPPRESS if suppress else None,
            metavar="CHAT_LIST",
            help="Чаты обработки через запятую; перекрывает core.chat_list",
        )
        parser.add_argument(
            "--threshold",
            dest=f"threshold_override{suffix}",
            type=fuzzy_threshold,
            default=argparse.SUPPRESS if suppress else None,
            metavar="F",
            help="Порог нечёткого поиска 0..1; перекрывает fuzzy_matching.threshold",
        )
    return pre, post


def collect_cli_overrides(args: Namespace) -> dict[tuple[str, str], str]:
    """Собирает флаги перекрытия конфигурации в ``{(секция, опция): значение}``.

    Сахарные флаги применяются первыми, затем строки ``--set`` в порядке
    следования (при конфликте ``--set`` приоритетнее сахара, позднее указание
    приоритетнее раннего). Схему ключей (секции/опции) проверяет
    :func:`dedup.settings.load_config`, здесь — только синтаксис.

    Args:
        args: Аргументы, разобранные :func:`parse_arguments`.

    Returns:
        Отображение ``{(секция, опция): значение}`` для ``load_config``.

    Raises:
        ConfigError: Некорректный синтаксис строки ``--set``.
    """

    def late(pre_attr: str, post_attr: str):
        late_value = getattr(args, post_attr, None)
        return late_value if late_value is not None else getattr(args, pre_attr, None)

    overrides: dict[tuple[str, str], str] = {}
    if (dry_run := late("dry_run", "dry_run_post")) is not None:
        overrides[("core", "dry_run")] = "true" if dry_run else "false"
    if (chat := late("chat_override", "chat_override_post")) is not None:
        overrides[("core", "chat_list")] = chat
    if (threshold := late("threshold_override", "threshold_override_post")) is not None:
        overrides[("fuzzy_matching", "threshold")] = str(threshold)

    set_entries = list(getattr(args, "config_set", None) or []) + list(
        getattr(args, "config_set_post", None) or []
    )
    for entry in set_entries:
        raw_key, sep, value = entry.partition("=")
        if not sep:
            raise ConfigError(f"--set: ожидается формат SECTION.OPTION=VALUE, получено '{entry}'")
        section, dot, option = raw_key.partition(".")
        if not (dot and section and option) or "." in option:
            raise ConfigError(
                f"--set: ключ '{raw_key}' должен иметь вид SECTION.OPTION "
                f"(ровно одна точка), получено '{entry}'"
            )
        overrides[(section, option)] = value
    return overrides


def parse_arguments() -> argparse.Namespace:
    """Настраивает и парсит аргументы командной строки.

    Подкоманды: ``repair``, ``report``, ``download``, ``export``, ``search``.
    Вызов без подкоманды — обычный прогон дедупликации. Глобальные флаги
    перекрытия конфигурации (``--set``/``--dry-run``/``--chat``/
    ``--threshold``) принимаются до и после подкоманды.

    Returns:
        Разобранные аргументы (`argparse.Namespace`).
    """
    overrides_pre, overrides_post = _override_parents()
    parser = argparse.ArgumentParser(
        description="Скрипт для поиска и удаления дубликатов аудио в Telegram чатах.",
        parents=[overrides_pre],
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<команда>")

    def add_subcommand(name: str, help_text: str) -> argparse.ArgumentParser:
        return subparsers.add_parser(name, help=help_text, parents=[overrides_post])

    add_subcommand(
        "repair",
        "Запускает утилиту для восстановления и оптимизации базы данных.",
    )

    add_subcommand(
        "report",
        "Создает текстовый файл-отчет с найденными группами дубликатов и ссылками (без удаления).",
    )

    p_download = add_subcommand(
        "download",
        "Скачивает все аудиофайлы из БД для указанного чата в папку downloads.",
    )
    p_download.add_argument(
        "chat",
        type=str,
        metavar="CHAT_IDENTIFIER",
        help="Идентификатор чата (ID, @username или ссылка).",
    )

    p_search = add_subcommand(
        "search",
        "Нечёткий поиск (с пониманием опечаток) по всем аудио в БД; выводит топ-совпадения и завершает работу.",
    )
    p_search.add_argument(
        "query",
        metavar="QUERY",
        help="Поисковый запрос: имя файла или исполнитель/название.",
    )
    p_search.add_argument(
        "--min-score",
        type=min_score,
        default=int(SCORE_CUTOFF),
        metavar="N",
        help=f"Минимальная схожесть в процентах 0..100 (по умолчанию {int(SCORE_CUTOFF)}).",
    )
    p_search.add_argument(
        "--wratio",
        action="store_true",
        help="Скорер WRatio вместо token_set_ratio: ловит короткие обрывки с опечатками, "
        "но шкала сжата (слово найдено = 90) — учтите в --min-score.",
    )

    p_export = add_subcommand(
        "export",
        "Экспорт данных из БД; действие указывается подкомандой.",
    )
    export_subparsers = p_export.add_subparsers(
        dest="export_command", metavar="<действие>", required=True
    )

    p = export_subparsers.add_parser(
        "filenames",
        help="Экспортирует все имена файлов из БД в текстовый файл и завершает работу.",
    )
    p.add_argument(
        "chat",
        type=chat_id_or_username,
        metavar="CHAT_ID|@USERNAME",
        help="Чат, для которого экспортировать имена файлов.",
    )

    p = export_subparsers.add_parser(
        "filenames-url",
        help="Экспортирует имена файлов и ссылки на сообщения из БД в текстовый файл.",
    )
    p.add_argument(
        "chat",
        type=chat_id_or_username,
        metavar="CHAT_ID|@USERNAME",
        help="Чат, для которого экспортировать имена файлов со ссылками.",
    )

    p = export_subparsers.add_parser(
        "cleaned-names",
        help="Экспортирует процесс очистки имён файлов в CSV формат.",
    )
    p.add_argument(
        "chat",
        type=chat_id_or_username,
        metavar="CHAT_ID|@USERNAME",
        nargs="?",
        default=0,
        help="Чат; без аргумента — вся БД.",
    )

    p = export_subparsers.add_parser(
        "cleaned-meta",
        help="Экспортирует процесс очистки метаданных (performer+title) в CSV формат.",
    )
    p.add_argument(
        "chat",
        type=chat_id_or_username,
        metavar="CHAT_ID|@USERNAME",
        nargs="?",
        default=0,
        help="Чат; без аргумента — вся БД.",
    )

    p = export_subparsers.add_parser(
        "xlsx",
        help="Экспорт в Excel.",
    )
    p.add_argument(
        "chat",
        type=chat_id_or_username,
        metavar="CHAT_ID|@USERNAME",
        nargs="?",
        default=0,
        help="ID чата для фильтрации; без аргумента — полный экспорт всей БД.",
    )

    return parser.parse_args()
