"""
scoring.py — движок очков для сольного режима.

Принимает список кубиков (dict с value+color) или просто значений.
Возвращает очки или None если набор недопустим (есть кубик без комбинации).

Поддерживает:
  • Базовые правила (стриты, кратные ×3+, одиночные 1 и 5)
  • 🔴 Красные пятёрки с множителями:
      - Одиночная 5 с красной = 100 (вместо 50)
      - Пара 5-5 с ≥1 красной = 250 + (R-1)×250
      - N×5 (N≥3) с R красными:
            base + 100 + (N-3)×1000 + (R-1)×500
        где base = 500 × 2^(N-3)
  • 🟡 Жёлтый 4-5-6 (с ≥1 жёлтым) = 500
  • 🟡 Жёлтый 1-2-3 (с ≥2 жёлтыми) = 1000  [ультовая комбинация]
  • 🟡 Жёлтый 1-2-3-4-5-6 (жёлтые в обеих половинах) = 3000  [ультовая комбинация]
  • 🔵 Синие 2-2 (обе синие) = 2000  [ультовая комбинация]
"""
from __future__ import annotations
from collections import Counter
from typing import Iterable


def _normalize(items: Iterable) -> list[dict]:
    """Приводит вход к списку dict {value, color}."""
    out = []
    for x in items:
        if isinstance(x, dict):
            out.append({"value": int(x["value"]), "color": x.get("color")})
        else:
            out.append({"value": int(x), "color": None})
    return out


def _red_5_multiple_score(count: int, red_count: int) -> int:
    """Очки за N×5 с R красными (count = N, red_count = R)."""
    base = 500 * (2 ** (count - 3))   # count=3→500, =4→1000, =5→2000, =6→4000
    if red_count == 0:
        return base
    first_red_bonus = 100 + (count - 3) * 1000
    additional      = (red_count - 1) * 500
    return base + first_red_bonus + additional


def _score_fives(dice: list[dict], indices: list[int]) -> int:
    """Очки за выделенные кубики со значением 5 (с учётом красных)."""
    n = len(indices)
    if n == 0:
        return 0
    red_count = sum(1 for i in indices if dice[i].get("color") == "red")

    if n == 1:
        return 100 if red_count else 50

    if n == 2:
        if red_count >= 1:
            return 250 + (red_count - 1) * 250   # 1 red→250, 2 reds→500
        return 100  # 50+50 как два одиночных

    return _red_5_multiple_score(n, red_count)


# ── Yellow helpers ─────────────────────────────────────────────────────────────

def _find_yellow_456(dice: list[dict], already_used: set[int]) -> list[int] | None:
    """
    Ищет 3 кубика 4, 5, 6 с хотя бы одним жёлтым.
    Возвращает индексы или None.
    """
    avail = [(i, d) for i, d in enumerate(dice) if i not in already_used]

    def find_one(value: int, prefer_yellow: bool):
        if prefer_yellow:
            for i, d in avail:
                if d["value"] == value and d.get("color") == "yellow" and i not in chosen:
                    return i
        for i, d in avail:
            if d["value"] == value and i not in chosen:
                return i
        return None

    chosen: set[int] = set()
    have_4 = [i for i, d in avail if d["value"] == 4]
    have_5 = [i for i, d in avail if d["value"] == 5]
    have_6 = [i for i, d in avail if d["value"] == 6]
    if not (have_4 and have_5 and have_6):
        return None
    has_yellow_among = any(
        dice[i].get("color") == "yellow"
        for i in have_4 + have_5 + have_6
    )
    if not has_yellow_among:
        return None

    yellow_pos = next(
        (v for v in (4, 5, 6) if any(dice[i].get("color") == "yellow"
                                      for i in {4: have_4, 5: have_5, 6: have_6}[v])),
        None,
    )
    for value in (4, 5, 6):
        idx = find_one(value, prefer_yellow=(value == yellow_pos))
        if idx is None:
            return None
        chosen.add(idx)
    return list(chosen)


def _find_yellow_123(dice: list[dict], already_used: set[int]) -> list[int] | None:
    """
    Ищет 3 кубика 1, 2, 3 с хотя бы двумя жёлтыми (ультовая комбинация).
    Возвращает индексы или None.
    """
    avail = [(i, d) for i, d in enumerate(dice) if i not in already_used]
    have_1 = [i for i, d in avail if d["value"] == 1]
    have_2 = [i for i, d in avail if d["value"] == 2]
    have_3 = [i for i, d in avail if d["value"] == 3]
    if not (have_1 and have_2 and have_3):
        return None
    # Нужны ≥2 жёлтых среди этих трёх кубиков
    all_cands = have_1 + have_2 + have_3
    yellow_count = sum(1 for i in all_cands if dice[i].get("color") == "yellow")
    if yellow_count < 2:
        return None
    chosen: list[int] = []
    for pool in (have_1, have_2, have_3):
        # prefer yellow
        picked = next((i for i in pool if dice[i].get("color") == "yellow" and i not in chosen), None)
        if picked is None:
            picked = next((i for i in pool if i not in chosen), None)
        if picked is None:
            return None
        chosen.append(picked)
    return chosen


def _is_yellow_full_straight(dice: list[dict]) -> bool:
    """True если 1-2-3-4-5-6 с жёлтыми в обеих половинах (1-3 и 4-6)."""
    low_yellow  = any(d.get("color") == "yellow" and d["value"] in (1, 2, 3) for d in dice)
    high_yellow = any(d.get("color") == "yellow" and d["value"] in (4, 5, 6) for d in dice)
    return low_yellow and high_yellow


def _find_blue_2_pair(dice: list[dict], already_used: set[int]) -> list[int]:
    """Возвращает до 2 индексов синих 2 (ультовая комбинация)."""
    idx = [
        i for i, d in enumerate(dice)
        if i not in already_used and d.get("color") == "blue" and d["value"] == 2
    ]
    return idx[:2]


# ── score_selection ────────────────────────────────────────────────────────────

def score_selection(items: Iterable) -> int | None:
    """
    Очки за выделенные кубики или None если в выделении есть "висячий" кубик.
    """
    dice = _normalize(items)
    if not dice:
        return None

    n = len(dice)
    values = [d["value"] for d in dice]
    sorted_vals = sorted(values)

    # ── Стриты ──────────────────────────────────────────────────────────────────
    if sorted_vals == [1, 2, 3, 4, 5, 6]:
        # 🟡 Жёлтый: в обеих половинах → 3000
        return 3000 if _is_yellow_full_straight(dice) else 1500

    # Малые прямые: работают и когда выбрано больше 5 кубиков
    # (напр. [1,2,3,4,5] + лишняя 5 → 500 + 50 = 550).
    # Точная проверка == [x,y,z,w,v] ломается при 6+ кубиках, поэтому
    # извлекаем 5 нужных индексов, а остаток оцениваем рекурсивно.
    for straight_vals, straight_score in (([1, 2, 3, 4, 5], 500), ([2, 3, 4, 5, 6], 750)):
        if all(v in sorted_vals for v in straight_vals):
            tmp: list[int] = []
            for v in straight_vals:
                idx = next(
                    (i for i, d in enumerate(dice) if d["value"] == v and i not in tmp),
                    None,
                )
                if idx is None:
                    tmp = []
                    break
                tmp.append(idx)
            if len(tmp) == 5:
                remaining = [i for i in range(n) if i not in tmp]
                if not remaining:
                    return straight_score
                rem_score = score_selection([dice[i] for i in remaining])
                if rem_score is not None:
                    return straight_score + rem_score

    used: set[int] = set()
    score = 0

    # ── 🔵 Синяя пара 2-2 = 2000 (ультовая комбинация) ─────────────────────────
    blue_pair = _find_blue_2_pair(dice, used)
    if len(blue_pair) == 2:
        score += 2000
        used.update(blue_pair)

    # ── 🟡 Жёлтая 4-5-6 = 500 ───────────────────────────────────────────────────
    yellow_456 = _find_yellow_456(dice, used)
    if yellow_456 is not None:
        score += 500
        used.update(yellow_456)

    # ── 🟡 Жёлтая 1-2-3 с ≥2 жёлтыми = 1000 ────────────────────────────────────
    if yellow_456 is None:   # не занимаем кубики 4-5-6 и 1-2-3 одновременно
        yellow_123 = _find_yellow_123(dice, used)
        if yellow_123 is not None:
            score += 1000
            used.update(yellow_123)

    counts = Counter(values[i] for i in range(n) if i not in used)
    used_in_multi: set[int] = set()

    # ── Пятёрки: пара / кратные (с учётом красных) ───────────────────────────────
    five_indices = [i for i in range(n) if i not in used and dice[i]["value"] == 5]
    if len(five_indices) >= 2:
        score += _score_fives(dice, five_indices)
        for i in five_indices:
            used.add(i)
        used_in_multi.add(5)

    # ── Кратные ×3+ ──────────────────────────────────────────────────────────────
    for value, count in counts.items():
        if value == 5 or count < 3:
            continue
        base = 1000 if value == 1 else value * 100
        multiplier = 2 ** (count - 3)
        score += base * multiplier
        used_in_multi.add(value)
        for i, d in enumerate(dice):
            if i in used:
                continue
            if d["value"] == value:
                used.add(i)

    # ── Одиночные 1 и 5 ──────────────────────────────────────────────────────────
    for value in (1, 5):
        if value in used_in_multi:
            continue
        for i, d in enumerate(dice):
            if i in used:
                continue
            if d["value"] != value:
                continue
            if value == 1:
                score += 100
            else:
                score += 100 if d.get("color") == "red" else 50
            used.add(i)

    if len(used) != n:
        return None
    return score


# ── best_score_subset ──────────────────────────────────────────────────────────

def best_score_subset(roll: Iterable) -> tuple[int, list[int]]:
    """
    Жадный лучший набор очковых кубиков из броска.
    Возвращает (очки, индексы выбранных в roll).
    """
    dice = _normalize(roll)
    n = len(dice)
    sorted_vals = sorted(d["value"] for d in dice)

    # Полный стрит
    if sorted_vals == [1, 2, 3, 4, 5, 6]:
        pts = 3000 if _is_yellow_full_straight(dice) else 1500
        return pts, list(range(n))

    used: set[int] = set()

    # Малый/большой стрит
    for straight, base_score in (([1, 2, 3, 4, 5], 500), ([2, 3, 4, 5, 6], 750)):
        if all(v in sorted_vals for v in straight):
            tmp = []
            for v in straight:
                for i, d in enumerate(dice):
                    if i in used or i in tmp:
                        continue
                    if d["value"] == v:
                        tmp.append(i)
                        break
            if len(tmp) == 5:
                used.update(tmp)
                sc = base_score
                for i, d in enumerate(dice):
                    if i in used:
                        continue
                    if d["value"] == 1:
                        sc += 100
                        used.add(i)
                    elif d["value"] == 5:
                        sc += 100 if d.get("color") == "red" else 50
                        used.add(i)
                return sc, sorted(used)

    score = 0

    # 🔵 Синяя пара 2-2 = 2000
    blue_pair = _find_blue_2_pair(dice, used)
    if len(blue_pair) == 2:
        score += 2000
        used.update(blue_pair)

    # 🟡 4-5-6 с жёлтой
    y456 = _find_yellow_456(dice, used)
    if y456:
        score += 500
        used.update(y456)

    # 🟡 1-2-3 с ≥2 жёлтыми
    if not y456:
        y123 = _find_yellow_123(dice, used)
        if y123:
            score += 1000
            used.update(y123)

    # Пятёрки оптом
    five_indices = [i for i in range(n) if i not in used and dice[i]["value"] == 5]
    used_in_multi: set[int] = set()
    if len(five_indices) >= 2:
        score += _score_fives(dice, five_indices)
        for i in five_indices:
            used.add(i)
        used_in_multi.add(5)

    # Кратные ×3+
    counts = Counter(d["value"] for i, d in enumerate(dice) if i not in used)
    for value, count in counts.items():
        if value == 5 or count < 3:
            continue
        base = 1000 if value == 1 else value * 100
        multiplier = 2 ** (count - 3)
        score += base * multiplier
        used_in_multi.add(value)
        for i, d in enumerate(dice):
            if i in used:
                continue
            if d["value"] == value:
                used.add(i)

    # Одиночные 1, 5
    for value in (1, 5):
        if value in used_in_multi:
            continue
        for i, d in enumerate(dice):
            if i in used:
                continue
            if d["value"] != value:
                continue
            if value == 1:
                score += 100
            else:
                score += 100 if d.get("color") == "red" else 50
            used.add(i)

    return score, sorted(used)


def has_any_score(roll: Iterable) -> bool:
    score, _ = best_score_subset(roll)
    return score > 0


# ── Самопроверка ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cases = [
        # (dice_or_values, expected)
        ([1], 100),
        ([5], 50),
        ([{"value": 5, "color": "red"}], 100),
        ([{"value": 5, "color": "red"}, 5], 250),           # пара-бонус
        ([{"value": 5, "color": "red"}, {"value": 5, "color": "red"}], 500),  # 2 reds = 500
        ([1, 1, 1], 1000),
        ([6, 6, 6, 6, 6, 6], 4800),
        ([1, 2, 3, 4, 5, 6], 1500),
        ([4, 5, 6], None),
        ([{"value": 4, "color": "yellow"}, 5, 6], 500),
        ([{"value": 4}, {"value": 5, "color": "yellow"}, 6], 500),
        ([{"value": 4, "color": "yellow"}, 5, 6, 1], 600),
        ([{"value": 4, "color": "yellow"}, 5, 6, 5, 5], 600),
        ([1, 1, 1, 5, 5], 1100),
        # Малая прямая + лишние кубики (баг был: возвращал None)
        ([1, 2, 3, 4, 5, 5], 550),       # прямая 500 + одиночная 5 50
        ([1, 2, 3, 4, 5, 1], 600),       # прямая 500 + одиночная 1 100
        ([2, 3, 4, 5, 6, 1], 1500),      # это полная прямая 1-6 = 1500 (не 750+100)
        ([1, 2, 3, 4, 5, 5, 5], 600),   # прямая 500 + пара 5-5 100
        ([1, 2, 3, 4, 5, 4], None),      # прямая + лишняя 4 → 4 не скорится → None
        ([2, 3, 4, 5, 6, 3], None),      # большая прямая + лишняя 3 → None
        # Новые комбо
        ([{"value": 2, "color": "blue"}, {"value": 2, "color": "blue"}], 2000),  # синие 2-2
        ([{"value": 1, "color": "yellow"}, {"value": 2, "color": "yellow"}, 3], 1000),  # жёлтая 1-2-3
        ([{"value": 1, "color": "yellow"}, 2, {"value": 3, "color": "yellow"}], 1000),
        (  # жёлтый стрит = 3000
            [{"value": 1, "color": "yellow"}, 2, 3,
             4, {"value": 5, "color": "yellow"}, 6],
            3000,
        ),
    ]
    for vals, expected in cases:
        got = score_selection(vals)
        ok = "OK" if got == expected else "FAIL"
        print(f"{ok} {vals!r} -> {got} (expected {expected})")