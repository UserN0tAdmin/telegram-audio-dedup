"""Нормализация текста для fuzzy-сравнения и поиска.

Два пайплайна: тяжёлая дедуп-очистка (``clean_filename``/``clean_meta``),
вырезающая расширения, сайты и рекламу — совпадения «мусор» не мешают
находить, — и лёгкая поисковая (``clean_for_search``/``clean_meta_for_search``),
сохраняющая всё это: по сайтам-качалкам, доменам и расширениям тоже ищут.
"""

import re

from rapidfuzz.utils import default_process

from .logger import log

# todo Вынести regex в конфиг
# region --- Регулярки ---
# Компилируются один раз на уровне модуля.

_DOMAINS = r"(?:net|com|ru|me|fm|tv|org|biz|info|cc|xyz|ua|by|kz|top|click|su|pm)"
_MEDIA_EXT = (
    r"mp3|m4a|m4b|flac|wav|ogg|ogx|wma|aac|alac|aiff|ape|opus|wv|webm|mp4|avi|wmv|mkv|flv|mov"
)
_TRASH_SITES = r"(?:muzlome|myzuka|zaycev(?:_?net)?|zvuk|muzofon|hitmo|pesni|lightaudio(?:_ru)?|ruapporangespace|mp3pulse(?:_ru)?|jamix(?:_cc)?|ipleer(?:_com)?|skysound(?:_cc)?|vk4(?:_ru)?)"

_RE_EXT = re.compile(rf"(?:\.(?:{_MEDIA_EXT}))+$", re.IGNORECASE)
_RE_BRACKETS_AD = re.compile(rf"[\[\(][^\]\)]*\b[\w-]+\.{_DOMAINS}\b[^\]\)]*[\]\)]", re.IGNORECASE)
_RE_WWW = re.compile(r"(?:www[._]|https?://|ftp://)", re.IGNORECASE)
_RE_URLS = re.compile(rf"\b[a-z0-9]+\.{_DOMAINS}\b", re.IGNORECASE)
_RE_PURE_ID = re.compile(r"^\d{1,5}[\s_\-]\d{10,}$")
_RE_TRASH_PREFIX = re.compile(rf"^{_TRASH_SITES}[_.\-\s]+", re.IGNORECASE)
_RE_NUM_PREFIX = re.compile(r"^\d{5,7}[\s_\-]+", re.IGNORECASE)
_RE_TRASH_SUFFIX = re.compile(rf"[_.\-\s]+{_TRASH_SITES}[_.\-\s]*$", re.IGNORECASE)
_RE_COPY_SUFFIX = re.compile(r"[\s_\-]*[\(\[]\d[\)\]]$")
_RE_TRASH_IDS = re.compile(r"(?<=[a-zа-яё\d])[\s_\-]+\d{7,}(?:[\s_\-]+\d{1,4})?$", re.IGNORECASE)
_RE_META_PLACEHOLDER = re.compile(
    r"^\s*(?:<\s*unknown\s*>|\[\s*unknown\s*\])\s*$",
    re.IGNORECASE,
)
# endregion


def clean_filename(fname: str | None) -> str:
    """Нормализует имя файла для fuzzy-сравнения дубликатов.

    Удаляет медиа-расширения, рекламные вставки в скобках, сайтовые
    префиксы/суффиксы/домены, суффиксы копий («(1)», «[2]») и длинные
    цифровые ID в конце. Унифицирует разделители (``_``, ``-``) в пробелы.

    Args:
        fname: Исходное имя файла или ``None``.

    Returns:
        Очищенная строка в нижнем регистре. Если после очистки ничего не
        осталось — возвращает имя без расширения (или оригинал как fallback).
    """
    if not fname:
        return ""
    original = fname
    s = fname
    s = _RE_EXT.sub("", s)
    s = s.lower()

    s = _RE_COPY_SUFFIX.sub(" ", s)
    if _RE_PURE_ID.match(s.strip()):
        return s.replace("_", " ").replace("-", " ").strip()
    s = _RE_NUM_PREFIX.sub(" ", s)
    s = _RE_BRACKETS_AD.sub(" ", s)
    s = _RE_WWW.sub(" ", s)
    s = _RE_TRASH_PREFIX.sub(" ", s)
    s = _RE_TRASH_SUFFIX.sub(" ", s)
    s = s.strip()
    s = s.replace("_", " ").replace("-", " ")
    s = _RE_URLS.sub(" ", s)
    s = " ".join(s.split())
    s = _RE_TRASH_IDS.sub("", s)

    cleaned = s.strip()
    if not cleaned:
        log.debug(f"'{original}' очищен в слюни")
        return _RE_EXT.sub("", original).strip() or original
    return cleaned


def clean_for_search(fname: str | None) -> str:
    """Лёгкая нормализация для поиска — «мусор» сохраняется.

    В отличие от :func:`clean_filename`, ничего не вырезает: сайты-качалки,
    домены, URL, расширения и цифровые ID остаются в тексте — по ним тоже
    ищут. Убирает только регистр и разбивает не-буквоцифровые разделители
    (``-_.[]()/``…) пробелами, чтобы ``default_process`` не склеивал
    ``song.mp3`` в один токен ``songmp3``.

    Args:
        fname: Исходный текст (имя файла или запрос) или ``None``.

    Returns:
        Строка в нижнем регистре с пробельными разделителями; пустая —
        если на входе ничего нет.
    """
    if not fname:
        return ""
    return " ".join(re.sub(r"[\W_]+", " ", fname.lower()).split())


def process_for_fuzzy(cleaned_name: str) -> str:
    """``default_process`` + схлопывание пробелов — финальная форма для fuzzy.

    Args:
        cleaned_name: Строка после ``clean_filename``/``clean_meta``
            (или ``clean_for_search``/``clean_meta_for_search``).

    Returns:
        Нормализованная строка для передачи в RapidFuzz.
    """
    return " ".join(default_process(cleaned_name).split())


def clean_meta(performer: str | None, title: str | None) -> str:
    """``performer+title``, очищенные тем же пайплайном, что и имя файла.

    Единая нормализация важнее точечной: имя и мета должны сравниваться
    в одной форме. Плейсхолдеры вида ``'<unknown>'`` отбрасываются как
    отсутствующие значения.

    Args:
        performer: Исполнитель из тегов (может быть ``None``).
        title: Название из тегов (может быть ``None``).

    Returns:
        Очищенная строка; пустая, если не осталось ни performer, ни title.
    """
    parts = [p for p in (performer, title) if p and not _RE_META_PLACEHOLDER.match(p)]
    if not parts:
        return ""
    return clean_filename(" ".join(parts))


def clean_meta_for_search(performer: str | None, title: str | None) -> str:
    """``performer+title`` под поиск: плейсхолдеры отброшены, «мусор» сохранён.

    Args:
        performer: Исполнитель из тегов (может быть ``None``).
        title: Название из тегов (может быть ``None``).

    Returns:
        Строка :func:`clean_for_search` от уцелевших полей; пустая, если
        и performer, и title отсутствуют или являются плейсхолдерами.
    """
    parts = [p for p in (performer, title) if p and not _RE_META_PLACEHOLDER.match(p)]
    if not parts:
        return ""
    return clean_for_search(" ".join(parts))
