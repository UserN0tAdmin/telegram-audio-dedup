"""Пользовательские исключения проекта."""


class AlreadyRunningError(RuntimeError):
    """Исключение, выбрасываемое при невозможности захватить lock-файл."""

    pass


class IgnoreListResolutionError(Exception):
    """Исключение, выбрасываемое, если не удалось разрешить идентификаторы из ignore_list."""

    pass
