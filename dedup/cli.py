"""Разбор аргументов командной строки (подкоманды repair/report/download/export/search)."""

import argparse

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


def parse_arguments() -> argparse.Namespace:
    """Настраивает и парсит аргументы командной строки.

    Подкоманды: ``repair``, ``report``, ``download``, ``export``, ``search``.
    Вызов без подкоманды — обычный прогон дедупликации.

    Returns:
        Разобранные аргументы (`argparse.Namespace`).
    """
    parser = argparse.ArgumentParser(
        description="Скрипт для поиска и удаления дубликатов аудио в Telegram чатах."
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<команда>")

    subparsers.add_parser(
        "repair",
        help="Запускает утилиту для восстановления и оптимизации базы данных.",
    )

    subparsers.add_parser(
        "report",
        help="Создает текстовый файл-отчет с найденными группами дубликатов и ссылками (без удаления).",
    )

    p_download = subparsers.add_parser(
        "download",
        help="Скачивает все аудиофайлы из БД для указанного чата в папку downloads.",
    )
    p_download.add_argument(
        "chat",
        type=str,
        metavar="CHAT_IDENTIFIER",
        help="Идентификатор чата (ID, @username или ссылка).",
    )

    p_search = subparsers.add_parser(
        "search",
        help="Нечёткий поиск (с пониманием опечаток) по всем аудио в БД; выводит топ-совпадения и завершает работу.",
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

    p_export = subparsers.add_parser(
        "export",
        help="Экспорт данных из БД; действие указывается подкомандой.",
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
