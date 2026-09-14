"""Пользовательские исключения проекта."""


class AlreadyRunningError(RuntimeError):
    """Исключение, выбрасываемое при невозможности захватить lock-файл."""



class IgnoreListResolutionError(Exception):
    """Исключение, выбрасываемое, если не удалось разрешить идентификаторы из ignore_list."""



class ConfigError(RuntimeError):
    """Исключение, выбрасываемое при некорректной конфигурации приложения."""

