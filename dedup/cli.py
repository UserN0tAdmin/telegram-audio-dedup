"""Разбор аргументов командной строки (подкоманды repair/report/download/export)."""

import argparse

from .state import chat_id_or_username


def parse_arguments() -> argparse.Namespace:
    """Настраивает и парсит аргументы командной строки.

    Подкоманды: ``repair``, ``report``, ``download``, ``export``.
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
