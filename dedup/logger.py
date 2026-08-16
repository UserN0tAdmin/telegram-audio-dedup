"""Логирование приложения: консоль, ротация файла, логгер Pyrogram.

``log`` создаётся без обработчиков; подключение консоли и файла выполняет
:func:`setup_logger` из точки входа — после загрузки конфигурации.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .settings import Settings

log = logging.getLogger("AudioDeleter")


def setup_logger(settings: Settings) -> logging.Logger:
    """Настраивает обработчики логгера приложения и Pyrogram.

    Args:
        settings: Полная конфигурация: секция ``[logging]`` плюс путь
            ``[paths].log_file``.

    Returns:
        Настроенный логгер приложения (доступен и как ``log``).
    """
    log.setLevel(logging.DEBUG)

    # Убираем обработчики, чтобы избежать дублирования вывода
    log.handlers.clear()

    # --- Обработчик для вывода в консоль ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(settings.logging.log_level_console)
    console_formatter = logging.Formatter("%(levelname)s [%(module)s.%(funcName)s]: %(message)s")
    console_handler.setFormatter(console_formatter)
    log.addHandler(console_handler)

    # --- Обработчик для записи в файл с ротацией ---
    log_path = Path(settings.paths.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_path,
        mode="a",
        maxBytes=settings.logging.log_max_bytes,
        backupCount=settings.logging.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(settings.logging.log_level_file)
    file_formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s [%(module)s.%(funcName)s] - %(message)s"
    )
    file_handler.setFormatter(file_formatter)
    log.addHandler(file_handler)

    # Логгер библиотеки Pyrogram пишет в те же обработчики
    pyro_logger = logging.getLogger("pyrogram")
    pyro_logger.setLevel(settings.logging.log_level_pyrogram)

    if pyro_logger.hasHandlers():
        pyro_logger.handlers.clear()

    pyro_logger.addHandler(console_handler)
    pyro_logger.addHandler(file_handler)

    # Отключаем всплытие (propagate), чтобы не дублировалось в root логгер,
    # если он где-то настроен.
    pyro_logger.propagate = False

    return log
