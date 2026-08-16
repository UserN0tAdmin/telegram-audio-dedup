"""Контекст прогона: доступ к загруженной конфигурации.

``set_settings`` вызывается один раз из точки входа (или тестовой фикстуры);
остальные модули читают конфигурацию через ``get_settings()`` в момент
вызова, а не на импорте.
"""

from .settings import Settings

_settings: Settings | None = None


def set_settings(settings: Settings) -> None:
    """Устанавливает конфигурацию текущего прогона.

    Args:
        settings: Настройки, загруженные :func:`dedup.settings.load_config`.
    """
    global _settings
    _settings = settings


def get_settings() -> Settings:
    """Возвращает конфигурацию текущего прогона.

    Returns:
        Активный :class:`dedup.settings.Settings`.

    Raises:
        RuntimeError: Конфигурация не установлена (точка входа не вызывала
            ``set_settings``).
    """
    if _settings is None:
        raise RuntimeError(
            "Конфигурация не загружена: вызовите set_settings(load_config()) в точке входа."
        )
    return _settings
