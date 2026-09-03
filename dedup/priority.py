"""Стратегия выбора оригинала (keep_priority) внутри группы дубликатов."""

from collections.abc import Callable
from typing import Final, NamedTuple

from .cleaning import clean_filename
from .context import get_settings
from .settings import KEEP_CRITERIA_VALID
from .typedefs import DBRow, DuplicateGroup


class KeepCriterion(NamedTuple):
    """Критерий каскада выбора оригинала.

    Attributes:
        extract: Возвращает числовое значение критерия для записи или
            ``None`` (= значение отсутствует; такая запись проигрывает
            записям, у которых оно есть).
        prefer_max: ``True`` — большим значениям приоритет, ``False`` — меньшим.
    """

    extract: Callable[[DBRow], float | None]
    prefer_max: bool


def _extract_positive(field: str) -> Callable[[DBRow], float | None]:
    """Фабрика экстрактора: положительное значение поля или ``None``."""

    def inner(r: DBRow) -> float | None:
        v = r[field]
        return float(v) if v and v > 0 else None

    return inner


_META_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {
        "<unknown>",
        "unknown",
        "unknown artist",
        "[unknown]",
    }
)


def _meta_field_ok(value: str | None) -> bool:
    """``True``, если поле меты заполнено и не является плейсхолдером."""
    v = (value or "").strip()
    return bool(v) and v.lower() not in _META_PLACEHOLDERS


def _extract_has_meta(r: DBRow) -> float:
    """Оценка качества тегов записи (0..2).

    Плейсхолдеры и title, совпадающий с именем файла (мусор от
    сайтов-качалок), не считаются метаданными.

    Args:
        r: Запись БД.

    Returns:
        0..2: по баллу за валидный performer и валидный title.
    """
    score = 0.0
    if _meta_field_ok(r["performer"]):
        score += 1.0
    if _meta_field_ok(r["title"]) and clean_filename(r["title"]) != clean_filename(r["file_name"]):
        score += 1.0
    return score


def _extract_clean_name_len(r: DBRow) -> float | None:
    """Длина очищенного имени файла или ``None`` для пустого имени."""
    cleaned = clean_filename(r["file_name"])
    return float(len(cleaned)) if cleaned else None


_KEEP_CRITERIA: Final[dict[str, KeepCriterion]] = {
    "oldest": KeepCriterion(lambda r: float(r["message_id"]), prefer_max=False),
    "newest": KeepCriterion(lambda r: float(r["message_id"]), prefer_max=True),
    "largest": KeepCriterion(_extract_positive("file_size"), prefer_max=True),
    "smallest": KeepCriterion(_extract_positive("file_size"), prefer_max=False),
    "longest": KeepCriterion(_extract_positive("duration"), prefer_max=True),
    "shortest": KeepCriterion(_extract_positive("duration"), prefer_max=False),
    "best_meta": KeepCriterion(_extract_has_meta, prefer_max=True),
    "longest_clean_name": KeepCriterion(_extract_clean_name_len, prefer_max=True),
}

# Ловим рассинхрон реестра и валидации конфига на импорте, а не в рантайме
assert set(_KEEP_CRITERIA) == KEEP_CRITERIA_VALID, (
    "Реестр критериев priority.py разошёлся с KEEP_CRITERIA_VALID в settings.py"
)


def _cascade_winner(pool: list[DBRow]) -> DBRow:
    """Выбирает лучшего кандидата каскадом критериев KEEP_PRIORITY.

    На каждом уровне: записи без значения отсеиваются (если у кого-то
    значение есть), затем остаются все в пределах допуска от лучшего.
    Уникальный tie-break (oldest/newest) в конце списка гарантирует,
    что каскад завершится ровно одним кандидатом.

    Args:
        pool: Записи-кандидаты одной группы.

    Returns:
        Запись-победитель каскада.
    """
    cands = pool
    for name, tol in get_settings().core.keep_priority:
        if len(cands) == 1:
            break
        crit = _KEEP_CRITERIA[name]
        scored = [(crit.extract(r), r) for r in cands]
        valid = [(v, r) for v, r in scored if v is not None]
        if not valid:
            continue  # критерий неприменим ко всей группе — пропускаем уровень

        best = max(v for v, _ in valid) if crit.prefer_max else min(v for v, _ in valid)
        eps = abs(best) * tol
        cands = [r for v, r in valid if abs(v - best) <= eps]
    return cands[0]


def order_group_by_keep_priority(group: DuplicateGroup) -> list[DBRow]:
    """Полный порядок приоритета: [оригинал, fallback #1, fallback #2, ...].

    Порядок нужен целиком: если лучший кандидат не пройдёт верификацию
    (удалён/изменён), оригиналом станет следующий. Повторный каскад по
    остатку — O(n²·C), но группы дубликатов крошечные.

    Args:
        group: Группа дубликатов.

    Returns:
        Записи группы в порядке убывания приоритета сохранения.
    """
    pool = list(group)
    ordered: list[DBRow] = []
    while pool:
        winner = _cascade_winner(pool)
        ordered.append(winner)
        pool = [r for r in pool if r is not winner]
    return ordered
