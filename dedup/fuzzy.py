"""Fuzzy-движок дедупликации: подготовка массивов, матчинг, группы.

Очистка имён и меты — в :mod:`dedup.cleaning`; сюда они приходят уже
нормализованными.
"""

import logging
import re
import time
from collections import defaultdict, deque
from itertools import combinations
from typing import Any

import numpy as np
from rapidfuzz import fuzz, process

from .cleaning import clean_filename, clean_meta, process_for_fuzzy
from .context import get_settings
from .logger import log
from .typedefs import DBRow, DuplicateGroup, EdgeInfo, EdgeMeta, edge_key

# Числа из имён/меты для penalty-механики; компилируется один раз.
_RE_DIGITS = re.compile(r"\d+")

# region --- Подфункции fuzzy-матчера ---


# Источники текстового совпадения: имя-имя, имя-мета, мета-имя, мета-мета
_SRC_NN, _SRC_NM, _SRC_MN, _SRC_MM = 0, 1, 2, 3
_SRC_LABEL = {
    _SRC_NN: "имя-имя",
    _SRC_NM: "имя-мета",
    _SRC_MN: "мета-имя",
    _SRC_MM: "мета-мета",
}


def src_suffix(src: int | None) -> str:
    """Подпись источника для отчёта, напр. ``'(мета-мета)'``.

    Args:
        src: Код источника (0..3) или ``None`` для uid/meta.

    Returns:
        Строка-суффикс в скобках или ``""``.
    """
    return f"({_SRC_LABEL[src]})" if src is not None else ""


def _prepare_arrays(
    sorted_rows: list[DBRow],
) -> tuple[
    np.ndarray,  # ids
    np.ndarray,  # durations
    np.ndarray,  # sizes
    list[str],  # names (очищённые)
    list[str],  # names_processed
    list[str],  # metas_processed
    np.ndarray,  # name_lengths
    list[set[int]],  # numbers_cache (числа из имени)
    list[set[int]],  # meta_numbers_cache (числа из меты)
    list[str | None],  # uids
    dict[int, DBRow],  # id_to_row
]:
    """Строит numpy-массивы и вспомогательные структуры из отсортированных строк БД.

    Все тяжёлые преобразования (очистка имён, RapidFuzz default_process,
    извлечение чисел) выполняются здесь — по одному разу на файл.

    Args:
        sorted_rows: Записи БД, отсортированные по ``duration`` (возрастание).

    Returns:
        Кортеж из одиннадцати объектов — массивы ids/durations/sizes,
        списки имён (сырых и обработанных), мета, длины имён, кэш числовых множеств,
        список UID-ов и словарь message_id → DBRow.
    """
    ids = np.array([r["message_id"] for r in sorted_rows], dtype=np.int64)
    durations = np.array([r["duration"] or 0 for r in sorted_rows], dtype=np.int32)
    sizes = np.array([r["file_size"] or 0 for r in sorted_rows], dtype=np.float64)

    names = [clean_filename(r["file_name"]) for r in sorted_rows]
    names_processed = [process_for_fuzzy(n) for n in names]

    if get_settings().fuzzy.use_meta_fuzzy:
        metas = [clean_meta(r["performer"], r["title"]) for r in sorted_rows]
    else:
        metas = [""] * len(sorted_rows)  # фича выключена -> мета пустая всюду
    metas_processed = [process_for_fuzzy(m) for m in metas]

    name_lengths = np.array([len(n) for n in names_processed], dtype=np.int32)
    numbers_cache = [{int(x) for x in _RE_DIGITS.findall(n)} for n in names]
    meta_numbers_cache = [{int(x) for x in _RE_DIGITS.findall(m)} for m in metas]

    uids = [r["file_unique_id"] for r in sorted_rows]
    id_to_row = {r["message_id"]: r for r in sorted_rows}

    return (
        ids,
        durations,
        sizes,
        names,
        names_processed,
        metas_processed,
        name_lengths,
        numbers_cache,
        meta_numbers_cache,
        uids,
        id_to_row,
    )


def _uid_prepass(
    ids: np.ndarray,
    uids: list[str | None],
    adjacency: defaultdict[int, set[int]],
    edge_meta: EdgeMeta,
) -> int:
    """Связывает файлы с одинаковым ``file_unique_id`` до основного цикла.

    ``file_unique_id`` означает буквально один и тот же файл на серверах
    Telegram — совпадение гарантировано, fuzzy не нужен.

    Args:
        ids: Массив message_id (int64), параллельный ``uids``.
        uids: Список file_unique_id (может содержать ``None``).
        adjacency: Граф смежности — модифицируется на месте.
        edge_meta: Метаданные рёбер — модифицируется на месте; для каждой
            UID-связи пишется EdgeInfo(reason="uid").

    Returns:
        Количество добавленных UID-связей.
    """
    # todo проверять также и имя файла, титле и перформер
    uid_groups: defaultdict[str, list[int]] = defaultdict(list)
    for idx, uid in enumerate(uids):
        if uid:
            uid_groups[uid].append(idx)

    stats_uid_matches = 0
    for indices in uid_groups.values():
        if len(indices) < 2:
            continue
        for a, b in combinations(indices, 2):
            id_a, id_b = int(ids[a]), int(ids[b])
            adjacency[id_a].add(id_b)
            adjacency[id_b].add(id_a)
            edge_meta[edge_key(id_a, id_b)] = EdgeInfo(
                reason="uid", score=1.0, name=None, dur=None, size=None, penalty=0.0
            )
            stats_uid_matches += 1

    return stats_uid_matches


def _compute_window_scores(
    i: int,
    window_end: int,
    durations: np.ndarray,
    sizes: np.ndarray,
    buf_thresholds: np.ndarray,
    buf_scores_dur: np.ndarray,
    buf_scores_size: np.ndarray,
    base_threshold: float,
    w_dur: float,
    w_size: float,
    dur_power: float,
    size_power: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Вычисляет оценки длительности и размера для окна соседей.

    Заполняет три pre-allocated буфера (views, не копии): динамические
    пороги, score по длительности и score по размеру.

    Args:
        i: Индекс текущего файла в отсортированном массиве.
        window_end: Правая граница скользящего окна (exclusive).
        durations: Массив длительностей (int32).
        sizes: Массив размеров файлов (float64).
        buf_thresholds: Pre-allocated буфер порогов (изменяется на месте).
        buf_scores_dur: Pre-allocated буфер score по длительности.
        buf_scores_size: Pre-allocated буфер score по размеру.
        base_threshold: Базовый порог схожести (FUZZY_THRESHOLD).
        w_dur: Вес длительности (WEIGHT_DURATION).
        w_size: Вес размера (WEIGHT_SIZE).
        dur_power: Показатель степени для score длительности (DURATION_POWER).
        size_power: Показатель степени для score размера (SIZE_POWER).

    Returns:
        Три view-а на буферы (dynamic_thresholds, scores_dur, scores_size)
        длиной ``window_end - i - 1``. Изменять вне функции безопасно —
        они ссылаются на те же pre-allocated массивы.
    """
    window_size = window_end - (i + 1)

    dynamic_thresholds = buf_thresholds[:window_size]
    scores_dur = buf_scores_dur[:window_size]
    scores_size = buf_scores_size[:window_size]

    dynamic_thresholds.fill(base_threshold)
    scores_dur.fill(0.0)
    scores_size.fill(0.0)

    current_dur = durations[i]
    neigh_durs = durations[i + 1 : window_end]
    curr_size = sizes[i]
    neigh_sizes = sizes[i + 1 : window_end]

    # Длительность
    valid_dur_mask = (neigh_durs > 0) & (current_dur > 0)
    if current_dur > 0 and np.any(valid_dur_mask):
        vi = np.flatnonzero(valid_dur_mask)
        v = neigh_durs[vi]
        ratio_dur = np.minimum(v / current_dur, current_dur / v)
        scores_dur[vi] = ratio_dur**dur_power

    invalid_dur_mask = ~valid_dur_mask
    if np.any(invalid_dur_mask):
        dynamic_thresholds[invalid_dur_mask] -= base_threshold * w_dur

    # Размер
    valid_size_mask = (neigh_sizes > 0) & (curr_size > 0)
    if curr_size > 0 and np.any(valid_size_mask):
        vi = np.flatnonzero(valid_size_mask)
        v = neigh_sizes[vi]
        ratio_size = np.minimum(v / curr_size, curr_size / v)
        scores_size[vi] = ratio_size**size_power

    invalid_size_mask = ~valid_size_mask
    if np.any(invalid_size_mask):
        dynamic_thresholds[invalid_size_mask] -= base_threshold * w_size

    return dynamic_thresholds, scores_dur, scores_size


def _optimistic_filter(
    i: int,
    window_end: int,
    name_lengths: np.ndarray,
    scores_dur: np.ndarray,
    scores_size: np.ndarray,
    dynamic_thresholds: np.ndarray,
    w_name: float,
    w_dur: float,
    w_size: float,
    name_power: float,
    fuzzy_mode: str,
    use_meta: bool,
) -> np.ndarray:
    """Отсекает заведомо непроходных кандидатов без вызова fuzzy.

    SORT-режим без меты: верхняя граница ratio = 2·min(L1,L2)/(L1+L2) по длинам имён.
    SET-режим ИЛИ включённая мета: граница имени = 1.0 (length-bound по имени отрезал
    бы кандидатов, совпадающих по мете).

    Args:
        i: Индекс текущего файла.
        window_end: Правая граница окна (exclusive).
        name_lengths: Массив длин обработанных имён (int32).
        scores_dur: Score по длительности (view на буфер).
        scores_size: Score по размеру (view на буфер).
        dynamic_thresholds: Динамические пороги (view на буфер).
        w_name: Вес имени.
        w_dur: Вес длительности.
        w_size: Вес размера.
        name_power: Степень текстового score.
        fuzzy_mode: ``"set"`` или ``"sort"``.
        use_meta: Включена ли мета-fuzzy.

    Returns:
        Булева маска длиной ``window_end - i - 1``: ``True`` — кандидат
        проходит оптимистичную проверку.
    """
    if fuzzy_mode != "set" and not use_meta:
        curr_len = float(name_lengths[i])
        neigh_lens = name_lengths[i + 1 : window_end].astype(np.float64)
        sum_lens = neigh_lens + curr_len
        max_name_ratio = np.where(
            sum_lens > 0,
            2.0 * np.minimum(neigh_lens, curr_len) / sum_lens,
            0.0,
        )
        max_name_score = max_name_ratio**name_power
        max_potential = w_name * max_name_score + w_dur * scores_dur + w_size * scores_size
    else:
        max_potential = w_name * 1.0 + w_dur * scores_dur + w_size * scores_size

    return max_potential >= dynamic_thresholds


def _compute_penalty(
    current_numbers: set[int],
    candidate_numbers: set[int],
) -> float:
    """Вычисляет штраф за несовпадение числовых токенов в именах.

    Если оба множества пусты или равны — штраф 0. При включённом
    ``USE_JACCARD_PENALTY`` штраф пропорционален расстоянию Жаккара;
    иначе фиксированный ``PENALTY_NUMBERS_MISMATCH``.

    Args:
        current_numbers: Числа из имени текущего файла.
        candidate_numbers: Числа из имени кандидата.

    Returns:
        Штраф в диапазоне ``[0.0, PENALTY_NUMBERS_MISMATCH]``.
    """
    if current_numbers == candidate_numbers:
        return 0.0
    cfg = get_settings().fuzzy
    if cfg.use_jaccard_penalty and (current_numbers or candidate_numbers):
        union = len(current_numbers | candidate_numbers)
        intersection = len(current_numbers & candidate_numbers)
        return cfg.penalty_numbers_mismatch * (1.0 - intersection / union) if union else 0.0
    return cfg.penalty_numbers_mismatch


def _filter_already_connected(
    abs_indices: np.ndarray,
    valid_indices_relative: np.ndarray,
    ids: np.ndarray,
    adjacency_i: set[int] | None,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Убирает из кандидатов файлы, уже связанные с текущим.

    Пропуск уже связанных (по UID или ранее найденным fuzzy) позволяет
    избежать повторных fuzzy-сравнений, результат которых не изменит граф.

    Args:
        abs_indices: Абсолютные индексы кандидатов.
        valid_indices_relative: Относительные индексы кандидатов (в окне).
        ids: Массив message_id.
        adjacency_i: Множество соседей текущего узла (или ``None``).

    Returns:
        Отфильтрованные ``abs_indices``, ``valid_indices_relative`` и
        количество пропущенных кандидатов.
    """
    if not adjacency_i:
        return abs_indices, valid_indices_relative, 0

    mask = np.ones(abs_indices.size, dtype=bool)
    skipped = 0
    for k, abs_idx in enumerate(abs_indices.tolist()):
        if int(ids[abs_idx]) in adjacency_i:
            mask[k] = False
            skipped += 1

    if skipped:
        abs_indices = abs_indices[mask]
        valid_indices_relative = valid_indices_relative[mask]

    return abs_indices, valid_indices_relative, skipped


def _match_batch(
    i: int,
    abs_indices: np.ndarray,
    valid_indices_relative: np.ndarray,
    ids: np.ndarray,
    names: list[str],
    names_processed: list[str],
    metas_processed: list[str],
    numbers_cache: list[set[int]],
    meta_numbers_cache: list[set[int]],
    dynamic_thresholds: np.ndarray,
    scores_dur: np.ndarray,
    scores_size: np.ndarray,
    fuzz_scorer: Any,
    w_name: float,
    w_dur: float,
    w_size: float,
    name_power: float,
    adjacency: defaultdict[int, set[int]],
    edge_meta: EdgeMeta,
) -> tuple[int, int, list[float]]:
    """Векторизованное сравнение: ``process.cdist`` для массива кандидатов.

    Все источники (имя/мета × имя/мета) считаются одним cdist. Penalty
    вычисляется лениво: Stage 1 отбирает выживших по оптимистичной оценке
    (penalty=0 -> верхняя граница итогового score, безопасно т.к. penalty >= 0),
    Stage 2 считает реальный source-aware penalty и выбирает источник только
    для выживших.

    Args:
        i: Индекс текущего файла.
        abs_indices: Абсолютные индексы кандидатов.
        valid_indices_relative: Относительные индексы кандидатов.
        ids: Массив message_id.
        names: Очищённые имена (для лога).
        names_processed: Обработанные имена (для fuzzy).
        metas_processed: Обработанная мета performer+title (для fuzzy).
        numbers_cache: Кэш числовых множеств из имён.
        meta_numbers_cache: Кэш числовых множеств из меты.
        dynamic_thresholds: Динамические пороги (view на буфер).
        scores_dur: Score по длительности (view).
        scores_size: Score по размеру (view).
        fuzz_scorer: ``fuzz.token_set_ratio`` или ``token_sort_ratio``.
        w_name: Вес имени.
        w_dur: Вес длительности.
        w_size: Вес размера.
        name_power: Степень текстового score.
        adjacency: Граф смежности — модифицируется на месте.
        edge_meta: Метаданные рёбер — модифицируется на месте; для каждого
            совпадения пишется EdgeInfo(reason="fuzzy") с коэффициентами.

    Returns:
        Кортеж ``(comparisons, matches, matched_scores)`` — счётчики для статистики.
    """
    current_name = names_processed[i]
    current_meta = metas_processed[i]
    current_numbers = numbers_cache[i]
    current_meta_numbers = meta_numbers_cache[i]
    id_i = int(ids[i])

    # --- cutoff (без изменений): penalty source-aware -> cutoff только ослабляется ---
    if w_name > 0:
        min_name_powered_scores = (
            dynamic_thresholds[valid_indices_relative]
            - scores_dur[valid_indices_relative] * w_dur
            - scores_size[valid_indices_relative] * w_size
        ) / w_name

        possible_mask = min_name_powered_scores <= 1.0
        if not np.any(possible_mask):
            return 0, 0, []
        if not np.all(possible_mask):
            abs_indices = abs_indices[possible_mask]
            valid_indices_relative = valid_indices_relative[possible_mask]

        min_raw_scores = np.maximum(0.0, min_name_powered_scores[possible_mask]) ** (
            1.0 / name_power
        )
        global_cutoff = float(np.min(min_raw_scores)) * 100.0
    else:
        global_cutoff = 0.0

    comparisons = abs_indices.size
    abs_list = abs_indices.tolist()
    n = comparisons
    candidate_names = [names_processed[idx] for idx in abs_list]
    candidate_metas = [metas_processed[idx] for idx in abs_list]
    has_candidate_meta = any(candidate_metas)
    empty_meta_mask = np.array([not m for m in candidate_metas], dtype=bool)

    rel = valid_indices_relative
    thr = dynamic_thresholds[rel]
    dur_contrib = scores_dur[rel] * w_dur
    size_contrib = scores_size[rel] * w_size

    # --- ЕДИНЫЙ cdist: строки = queries, столбцы = choices ---
    # queries:  [0]=имя тек., [1]=мета тек. (если есть)
    # choices:  [0:n]=имена кандидатов, [n:2n]=меты кандидатов (если есть)
    queries = [current_name]
    if current_meta:
        queries.append(current_meta)
    choices = candidate_names + (candidate_metas if has_candidate_meta else [])

    dist = process.cdist(
        queries,
        choices,
        scorer=fuzz_scorer,
        processor=None,
        dtype=np.float64,
        score_cutoff=global_cutoff,
        workers=1,
    )

    # Собираем матрицу источников (P, n), fuzzy 0..100, срезами из dist.
    score_rows: list[np.ndarray] = [dist[0, 0:n]]  # NN — всегда
    src_codes: list[int] = [_SRC_NN]
    if has_candidate_meta:
        score_rows.append(dist[0, n : 2 * n])  # NM
        src_codes.append(_SRC_NM)
    if current_meta:
        score_rows.append(dist[1, 0:n])  # MN
        src_codes.append(_SRC_MN)
        if has_candidate_meta:
            score_rows.append(dist[1, n : 2 * n])  # MM
            src_codes.append(_SRC_MM)

    stacked = np.vstack(score_rows)  # (P, n)
    src_arr = np.asarray(src_codes, dtype=np.int8)
    meta_src_rows = [p for p, s in enumerate(src_codes) if s in (_SRC_NM, _SRC_MM)]
    mask_phantoms = has_candidate_meta and bool(empty_meta_mask.any()) and meta_src_rows

    # --- Stage 1: оптимистичный отбор (penalty=0 -> верхняя граница) ---
    powered_stacked = (stacked / 100.0) ** name_power
    optimistic = powered_stacked * w_name  # (P, n)
    if mask_phantoms:
        for p in meta_src_rows:
            optimistic[p, empty_meta_mask] = -np.inf

    optimistic_final = optimistic.max(axis=0) + dur_contrib + size_contrib
    survive = optimistic_final >= thr
    if not np.any(survive):
        return comparisons, 0, []

    surv_idx = np.flatnonzero(survive)  # позиции в массиве кандидатов
    surv_scores = stacked[:, surv_idx]  # (P, S)

    # --- Stage 2: реальный penalty только для выживших ---
    p_count = surv_scores.shape[0]
    penalty_stacked = np.zeros((p_count, surv_idx.size), dtype=np.float64)
    for col, s in enumerate(surv_idx.tolist()):
        abs_idx = abs_list[s]
        cand_nums = numbers_cache[abs_idx]
        cand_meta_nums = meta_numbers_cache[abs_idx]
        for p, src in enumerate(src_codes):
            cur_n = current_numbers if src in (_SRC_NN, _SRC_NM) else current_meta_numbers
            cand_n = cand_nums if src in (_SRC_NN, _SRC_MN) else cand_meta_nums
            penalty_stacked[p, col] = _compute_penalty(cur_n, cand_n)

    surv_scores_powered = (surv_scores / 100.0) ** name_power
    adjusted = surv_scores_powered * w_name - penalty_stacked
    if mask_phantoms:
        surv_empty = empty_meta_mask[surv_idx]
        if surv_empty.any():
            for p in meta_src_rows:
                adjusted[p, surv_empty] = -np.inf

    best_idx = np.argmax(adjusted, axis=0)
    cols = np.arange(surv_idx.size)
    fuzzy_scores_raw = surv_scores[best_idx, cols] / 100.0
    fuzzy_scores = fuzzy_scores_raw**name_power
    penalties = penalty_stacked[best_idx, cols]
    src_per_cand = src_arr[best_idx]

    rel_surv = rel[surv_idx]
    final_scores = (
        fuzzy_scores * w_name
        + scores_dur[rel_surv] * w_dur
        + scores_size[rel_surv] * w_size
        - penalties
    )
    match_mask = final_scores >= dynamic_thresholds[rel_surv]
    matched_scores = final_scores[match_mask].tolist()
    matched_positions = np.flatnonzero(match_mask)

    if log.isEnabledFor(logging.DEBUG):
        for k in matched_positions:
            s = int(surv_idx[k])
            abs_idx = int(abs_indices[s])
            rel_idx = int(rel[s])
            log.debug(
                f"[MATCH] Score: {final_scores[k]:.3f} (Penalty: -{penalties[k]:.2f}) | "
                f"Text: {fuzzy_scores[k]:.2f} ({_SRC_LABEL[int(src_per_cand[k])]}), "
                f"Dur: {scores_dur[rel_idx]:.2f}, Size: {scores_size[rel_idx]:.2f} | "
                f"'{names[i]}' <==> '{names[abs_idx]}'"
            )

    for pos in matched_positions.tolist():
        s = int(surv_idx[pos])
        abs_idx = int(abs_indices[s])
        rel_idx = int(rel[s])
        id_j = int(ids[abs_idx])
        adjacency[id_i].add(id_j)
        adjacency[id_j].add(id_i)
        edge_meta[edge_key(id_i, id_j)] = EdgeInfo(
            reason="fuzzy",
            score=float(final_scores[pos]),
            name=float(fuzzy_scores[pos]),
            dur=float(scores_dur[rel_idx]),
            size=float(scores_size[rel_idx]),
            penalty=float(penalties[pos]),
            text_source=int(src_per_cand[pos]),
        )

    return comparisons, int(match_mask.sum()), matched_scores


def _build_groups_bfs(
    ids: np.ndarray,
    adjacency: defaultdict[int, set[int]],
    id_to_row: dict[int, DBRow],
) -> list[DuplicateGroup]:
    """Собирает связные компоненты графа смежности обходом в ширину.

    Сложность O(N + E), где N — количество файлов, E — количество рёбер.

    Args:
        ids: Массив всех message_id (int64).
        adjacency: Граф смежности (только узлы с хотя бы одной связью).
        id_to_row: Словарь message_id → DBRow.

    Returns:
        Список компонент с размером ≥ 2 (одиночные файлы исключены).
    """
    groups = []
    processed = set()

    for item_id in ids.tolist():
        if item_id in processed or item_id not in adjacency:
            continue

        component: list[DBRow] = []
        queue = deque([item_id])
        processed.add(item_id)

        while queue:
            curr = queue.popleft()
            component.append(id_to_row[curr])
            for neighbor in adjacency[curr]:
                if neighbor not in processed:
                    processed.add(neighbor)
                    queue.append(neighbor)

        if len(component) > 1:
            groups.append(component)

    return groups


def _log_stats(
    count: int,
    t_prep: float,
    t_loop: float,
    t_bfs: float,
    t_total: float,
    stats_comparisons: int,
    stats_uid_matches: int,
    stats_matches: int,
    stats_skipped_connected: int,
    num_groups: int,
    match_scores: list[float],
) -> None:
    """Выводит сводную статистику fuzzy-поиска.

    Args:
        count: Общее число файлов.
        t_prep: Время подготовки данных (сек).
        t_loop: Время основного цикла (сек).
        t_bfs: Время сборки групп BFS (сек).
        t_total: Полное время выполнения (сек).
        stats_comparisons: Число пар-кандидатов, дошедших до текстового этапа.
        stats_uid_matches: Число связей, найденных через UID.
        stats_matches: Число связей, найденных через fuzzy.
        stats_skipped_connected: Число пропущенных уже связанных пар.
        num_groups: Число найденных групп дубликатов.
        match_scores: Финальные score всех fuzzy-совпадений за весь прогон.
            Если список непустой, выводятся min/p25/median/p75/max.
            Пустой список допустим (например, совпадений не найдено).
    """
    if t_loop > 0 and stats_comparisons > 0:
        ops = stats_comparisons / t_loop
        ops_str = (
            f"{ops / 1_000_000:.2f}M"
            if ops >= 1_000_000
            else f"{ops / 1_000:.1f}K"
            if ops >= 1_000
            else f"{ops:.0f}"
        )
    else:
        ops_str = "N/A"

    overhead_per_file = (t_loop * 1000 / count) if count > 0 else 0
    avg_candidates = stats_comparisons / count if count > 0 else 0

    log.info(
        f"Fuzzy-поиск: {stats_comparisons:,} пар-кандидатов, "
        f"{stats_uid_matches:,} UID-связей, "
        f"{stats_matches:,} fuzzy-связей, "
        f"{num_groups} групп"
    )
    log.info(
        f"Тайминги: подготовка={t_prep:.3f}s, цикл={t_loop:.3f}s, "
        f"BFS={t_bfs:.3f}s, всего={t_total:.3f}s"
    )
    log.info(
        f"Производительность: {ops_str} pairs/sec | "
        f"Avg кандидатов/файл: {avg_candidates:.1f} | "
        f"Overhead: {overhead_per_file:.3f}ms/файл | "
        f"Пропущено (уже связаны): {stats_skipped_connected}"
    )

    if match_scores:
        arr = np.array(match_scores, dtype=np.float64)
        log.info(
            f"Score совпадений: min={arr.min():.3f}, "
            f"p25={np.percentile(arr, 25):.3f}, "
            f"median={np.median(arr):.3f}, "
            f"p75={np.percentile(arr, 75):.3f}, "
            f"max={arr.max():.3f} "
            f"(порог={get_settings().fuzzy.threshold:.3f})"
        )
    if stats_comparisons > 0:
        duplicate_rate = stats_matches / stats_comparisons * 100
        log.info(f"Доля дубликатов: {duplicate_rate:.1f}%")


# endregion

# region --- Оркестратор fuzzy-поиска ---


def group_audios_fuzzy_optimized(all_audios: list[DBRow]) -> tuple[list[DuplicateGroup], EdgeMeta]:
    """Находит группы дубликатов аудиофайлов через fuzzy matching.

    Использует sliding window по отсортированным длительностям + NumPy
    для векторизации. Сравнение имён — RapidFuzz (token_set/sort_ratio).

    Args:
        all_audios: Список записей из БД с полями message_id, duration,
            file_size, file_name, file_unique_id, performer, title.

    Returns:
        Кортеж (groups, edge_meta):
          - groups: список групп, где каждая группа — list[DBRow] с len >= 2.
            Одиночные файлы (без дубликатов) не включаются.
          - edge_meta: метаданные связей (причина + коэффициенты) для отчёта.
    """
    if not all_audios:
        return [], {}

    count = len(all_audios)
    log.info(f"Запуск векторизованного Fuzzy поиска для {count} файлов...")

    t_total_start = time.perf_counter()

    # 1. Подготовка
    t_prep_start = time.perf_counter()
    sorted_rows = sorted(all_audios, key=lambda r: r["duration"] or 0)
    (
        ids,
        durations,
        sizes,
        names,
        names_processed,
        metas_processed,
        name_lengths,
        numbers_cache,
        meta_numbers_cache,
        uids,
        id_to_row,
    ) = _prepare_arrays(sorted_rows)

    # Локальные алиасы настроек для горячего цикла; name_power и size_power
    # передаются напрямую (алиас совпал бы с именем поля).
    cfg = get_settings().fuzzy
    BASE_THRESHOLD = cfg.threshold
    MAX_DIFF = cfg.max_duration_diff_sec
    W_NAME = cfg.weight_name
    W_DUR = cfg.weight_duration
    W_SIZE = cfg.weight_size
    DUR_POWER = cfg.duration_power

    window_ends = np.searchsorted(durations, durations + MAX_DIFF, side="right")
    max_window_size = max(1, int(np.max(window_ends - np.arange(count) - 1)))
    buf_thresholds = np.empty(max_window_size, dtype=np.float64)
    buf_scores_dur = np.empty(max_window_size, dtype=np.float64)
    buf_scores_size = np.empty(max_window_size, dtype=np.float64)

    if cfg.matching_mode == "set":
        fuzz_scorer = fuzz.token_set_ratio
        log.info("Режим Fuzzy: SET (Агрессивный, ищет пересечения слов)")
    else:
        fuzz_scorer = fuzz.token_sort_ratio
        log.info("Режим Fuzzy: SORT (Строгий, чувствителен к разным словам)")

    adjacency: defaultdict[int, set[int]] = defaultdict(set)
    edge_meta: EdgeMeta = {}

    # 2. UID предпроход
    stats_uid_matches = _uid_prepass(ids, uids, adjacency, edge_meta)
    t_prep_end = time.perf_counter()

    # 3. Основной цикл (Sliding Window)
    t_loop_start = time.perf_counter()
    stats_comparisons = 0
    stats_matches = 0
    stats_skipped_connected = 0
    all_match_scores: list[float] = []

    match_kwargs: dict[str, Any] = {
        "ids": ids,
        "names": names,
        "names_processed": names_processed,
        "metas_processed": metas_processed,
        "numbers_cache": numbers_cache,
        "meta_numbers_cache": meta_numbers_cache,
        "fuzz_scorer": fuzz_scorer,
        "w_name": W_NAME,
        "w_dur": W_DUR,
        "w_size": W_SIZE,
        "name_power": cfg.name_power,
        "adjacency": adjacency,
        "edge_meta": edge_meta,
    }

    for i in range(count):
        window_end = window_ends[i]
        if window_end <= i + 1:
            continue

        dynamic_thresholds, scores_dur, scores_size = _compute_window_scores(
            i,
            window_end,
            durations,
            sizes,
            buf_thresholds,
            buf_scores_dur,
            buf_scores_size,
            BASE_THRESHOLD,
            W_DUR,
            W_SIZE,
            DUR_POWER,
            cfg.size_power,
        )

        candidates_mask = _optimistic_filter(
            i,
            window_end,
            name_lengths,
            scores_dur,
            scores_size,
            dynamic_thresholds,
            W_NAME,
            W_DUR,
            W_SIZE,
            cfg.name_power,
            cfg.matching_mode,
            cfg.use_meta_fuzzy,
        )
        if not np.any(candidates_mask):
            continue

        valid_indices_relative = np.flatnonzero(candidates_mask)
        abs_indices = valid_indices_relative + (i + 1)
        id_i = int(ids[i])

        abs_indices, valid_indices_relative, skipped = _filter_already_connected(
            abs_indices,
            valid_indices_relative,
            ids,
            adjacency.get(id_i),
        )
        stats_skipped_connected += skipped
        if abs_indices.size == 0:
            continue

        shared: dict[str, Any] = {
            "abs_indices": abs_indices,
            "valid_indices_relative": valid_indices_relative,
            "dynamic_thresholds": dynamic_thresholds,
            "scores_dur": scores_dur,
            "scores_size": scores_size,
        }

        c, m, scores = _match_batch(i, **shared, **match_kwargs)
        all_match_scores.extend(scores)

        stats_comparisons += c
        stats_matches += m

    t_loop_end = time.perf_counter()

    # 4. Сборка групп BFS
    t_bfs_start = time.perf_counter()
    groups = _build_groups_bfs(ids, adjacency, id_to_row)
    t_bfs_end = time.perf_counter()

    # 5. Статистика
    _log_stats(
        count=count,
        t_prep=t_prep_end - t_prep_start,
        t_loop=t_loop_end - t_loop_start,
        t_bfs=t_bfs_end - t_bfs_start,
        t_total=t_bfs_end - t_total_start,
        stats_comparisons=stats_comparisons,
        stats_uid_matches=stats_uid_matches,
        stats_matches=stats_matches,
        stats_skipped_connected=stats_skipped_connected,
        num_groups=len(groups),
        match_scores=all_match_scores,
    )

    return groups, edge_meta


# endregion
