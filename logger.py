import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys


def setup_logger() -> logging.Logger:
    from config import (LOG_LEVEL_CONSOLE, LOG_LEVEL_FILE,
                        LOG_MAX_BYTES, LOG_BACKUP_COUNT,
                        LOG_LEVEL_PYROGRAM, LOG_FILE_PATH)

    # Создаем основной логгер
    logger = logging.getLogger('AudioDeleter')
    logger.setLevel(logging.DEBUG)

    # Убираем стандартные обработчики, чтобы избежать дублирования вывода
    if logger.hasHandlers():
        logger.handlers.clear()

    # --- Обработчик для вывода в консоль ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(LOG_LEVEL_CONSOLE)
    console_formatter = logging.Formatter('%(levelname)s [%(funcName)s]: %(message)s')
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # --- Обработчик для записи в файл с ротацией ---
    log_path = Path(LOG_FILE_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_path,
        mode='a',
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding='utf-8'
    )

    file_handler.setLevel(LOG_LEVEL_FILE)
    file_formatter = logging.Formatter('%(asctime)s - %(levelname)s [%(funcName)s] - %(message)s')
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Получаем логгер библиотеки Pyrogram
    pyro_logger = logging.getLogger("pyrogram")
    pyro_logger.setLevel(LOG_LEVEL_PYROGRAM)

    if pyro_logger.hasHandlers():
        pyro_logger.handlers.clear()

    pyro_logger.addHandler(console_handler)
    pyro_logger.addHandler(file_handler)

    # Отключаем всплытие (propagate), чтобы не дублировалось в root логгер,
    # если он где-то настроен.
    pyro_logger.propagate = False

    return logger


# Создаем экземпляр логгера для импорта в другие модули
log = setup_logger()