"""
database.py — работает с Supabase (PostgreSQL) через REST API.
"""
from __future__ import annotations
import time
from typing import Optional, Any
from supabase import create_client, Client
import config

_client: Optional[Client] = None


def _db() -> Client:
    global _client
    if _client is None:
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    return _client


# ── In-process cache ──────────────────────────────────────────────────────────
# Бот работает в одном процессе — простой dict кэш безопасен и убирает 80%
# HTTP-запросов к Supabase. Это критично для отзывчивости кнопок.

_MISS = object()

# pending_actions: write-through (set/clear обновляют кэш одновременно с БД)
_cache_pending: dict[int, tuple[float, Optional[dict]]] = {}
_CACHE_TTL_PENDING = 600.0  # 10 мин — инвалидируем только через write-через

# users: TTL-кэш 5 сек + инвалидация при апдейтах
_cache_user: dict[int, tuple[float, Optional[dict]]] = {}
_CACHE_TTL_USER = 5.0


def _cache_get(cache: dict, key: Any, ttl: float):
    entry = cache.get(key)
    if entry is None:
        return _MISS
    ts, val = entry
    if time.time() - ts > ttl:
        return _MISS
    return val


def _cache_set(cache: dict, key: Any, val):
    cache[key] = (time.time(), val)


def _user_cache_invalidate(user_id: Optional[int] = None):
    """Сбрасывает кэш пользователя. Без аргумента — сбрасывает весь user-кэш."""
    if user_id is None:
        _cache_user.clear()
    else:
        _cache_user.pop(user_id, None)


# ── Users ─────────────────────────────────────────────────────────────────────

def get_user(user_id: int) -> Optional[dict]:
    cached = _cache_get(_cache_user, user_id, _CACHE_TTL_USER)
    if cached is not _MISS:
        return cached
    r = _db().table("users").select("*").eq("user_id", user_id).execute()
    result = r.data[0] if r.data else None
    _cache_set(_cache_user, user_id, result)
    return result


def _escape_like(s: str) -> str:
    """Экранируем спецсимволы LIKE/ILIKE (\\ % _) чтобы пользовательский ввод
    не интерпретировался как wildcard. Default ESCAPE-символ Postgres — '\\'."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_user_by_username(username: str) -> Optional[dict]:
    r = _db().table("users").select("*").ilike("username", _escape_like(username)).execute()
    return r.data[0] if r.data else None


def username_exists(username: str) -> bool:
    r = (_db().table("users").select("user_id")
         .ilike("username", _escape_like(username)).execute())
    return bool(r.data)


def create_user(user_id: int, username: str, is_admin: bool = False):
    _db().table("users").insert({
        "user_id":  user_id,
        "username": username,
        "is_admin": is_admin,
    }).execute()
    _user_cache_invalidate(user_id)


def get_leaderboard() -> list[dict]:
    r = (_db().table("users")
         .select(
             "user_id,username,rating,games_played,wins,coins,"
             "is_calibrated,calibration_games_count,"
             "combo_prefix1,combo_prefix2,combo_suffix1,combo_suffix2"
         )
         .order("rating", desc=True)
         .limit(25)
         .execute())
    return r.data


def get_all_users() -> list[dict]:
    r = _db().table("users").select("*").order("username").execute()
    return r.data


def set_admin(user_id: int, is_admin: bool):
    _db().table("users").update({"is_admin": is_admin}).eq("user_id", user_id).execute()
    _user_cache_invalidate(user_id)


def update_rating(user_id: int, new_rating: int):
    _db().table("users").update({"rating": new_rating}).eq("user_id", user_id).execute()
    _user_cache_invalidate(user_id)


def delete_user(user_id: int):
    _db().table("users").delete().eq("user_id", user_id).execute()
    _user_cache_invalidate(user_id)


def reset_all_ratings():
    _db().table("users").update({
        "rating": 0, "games_played": 0, "wins": 0,
    }).neq("user_id", 0).execute()
    _user_cache_invalidate()


def reset_ratings_only():
    """Обнуляет только рейтинг (игры и победы не трогает)."""
    _db().table("users").update({"rating": 0}).neq("user_id", 0).execute()
    _user_cache_invalidate()


def reset_games_only():
    """Обнуляет только games_played и wins (рейтинг не трогает)."""
    _db().table("users").update({"games_played": 0, "wins": 0}).neq("user_id", 0).execute()
    _user_cache_invalidate()


def reset_combos():
    """Удаляет все записи combo_logs."""
    _db().table("combo_logs").delete().neq("id", 0).execute()


def undo_last_combo(user_id: int) -> bool:
    """Удаляет последнюю запись combo_logs игрока. Возвращает True если удалено."""
    r = (_db().table("combo_logs")
         .select("id")
         .eq("user_id", user_id)
         .order("id", desc=True)
         .limit(1)
         .execute())
    if not r.data:
        return False
    _db().table("combo_logs").delete().eq("id", r.data[0]["id"]).execute()
    return True


# ── Games ─────────────────────────────────────────────────────────────────────

def record_game(total_players: int, recorded_by: int, results: list[tuple]):
    """results: [(user_id, place, score, points_change, color, went_first), ...]

    Replaces the broken RPC (which used RETURNING id instead of RETURNING game_id)
    with direct table inserts + user-stat updates.
    """
    # 1. Insert game row and retrieve its game_id
    game_row = _db().table("games").insert({
        "total_players": total_players,
        "recorded_by":   recorded_by,
    }).execute()
    game_id = game_row.data[0]["game_id"]

    # 2. Insert one result row per player
    _db().table("game_results").insert([
        {
            "game_id":       game_id,
            "user_id":       uid,
            "place":         place,
            "score":         score,
            "points_change": pts,
            "color":         color,
            "went_first":    went_first,
        }
        for uid, place, score, pts, color, went_first in results
    ]).execute()

    # 3. Update each player's aggregate stats
    for uid, place, _score, pts, _color, _went_first in results:
        user = get_user(uid)
        if not user:
            continue
        is_calibrated = user.get("is_calibrated", True)
        new_games = user.get("games_played", 0) + 1
        new_wins  = user.get("wins", 0) + (1 if place == 1 else 0)
        update_data: dict = {
            "games_played": new_games,
            "wins":         new_wins,
        }
        # Некалиброванные игроки рейтинг не меняют — он выставится в complete_calibration
        if is_calibrated:
            update_data["rating"] = max(0, user.get("rating", 0) + pts)
        _db().table("users").update(update_data).eq("user_id", uid).execute()
        _user_cache_invalidate(uid)


def get_player_stats(user_id: int) -> dict:
    """Возвращает победы по цвету и кол-во первых ходов."""
    # Победы по цвету (place == 1, color не null)
    r = (_db().table("game_results")
         .select("color")
         .eq("user_id", user_id)
         .eq("place", 1)
         .not_.is_("color", "null")
         .execute())
    color_wins: dict[str, int] = {}
    for row in r.data:
        c = row.get("color") or "?"
        color_wins[c] = color_wins.get(c, 0) + 1

    # Количество раз, когда ходил первым
    r2 = (_db().table("game_results")
          .select("user_id")
          .eq("user_id", user_id)
          .eq("went_first", True)
          .execute())
    first_count = len(r2.data)

    return {"color_wins": color_wins, "first_count": first_count}


# ── Combo logs ────────────────────────────────────────────────────────────────

def log_combo(user_id: int, combo_key: str):
    _db().table("combo_logs").insert({
        "user_id":   user_id,
        "combo_key": combo_key,
    }).execute()


def get_combo_stats(user_id: int) -> list[dict]:
    """Возвращает список {combo_key, count} отсортированный по убыванию."""
    r = _db().table("combo_logs").select("combo_key").eq("user_id", user_id).execute()
    counts: dict[str, int] = {}
    for row in r.data:
        k = row["combo_key"]
        counts[k] = counts.get(k, 0) + 1
    return [
        {"combo_key": k, "count": v}
        for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)
    ]


def get_combo_count(user_id: int) -> int:
    """Общее количество записанных комбо игрока."""
    r = (_db().table("combo_logs")
         .select("id", count="exact")
         .eq("user_id", user_id)
         .execute())
    return r.count if r.count is not None else len(r.data)


def get_first_combo(user_id: int) -> Optional[dict]:
    """Первое когда-либо записанное комбо игрока."""
    r = (_db().table("combo_logs")
         .select("combo_key,created_at")
         .eq("user_id", user_id)
         .order("id", asc=True)
         .limit(1)
         .execute())
    return r.data[0] if r.data else None


def update_combo_customization(user_id: int, **kwargs):
    """Обновляет поля кастомизации профиля (combo_prefix1 и т.д.)."""
    allowed = {
        "combo_prefix1", "combo_prefix2",
        "combo_suffix1", "combo_suffix2",
        "combo_title",       "combo_banner",
        "combo_about",       "combo_divider",
        "combo_color_frame", "combo_bank_fx",
        "combo_win_message",
    }
    data = {k: v for k, v in kwargs.items() if k in allowed}
    if data:
        _db().table("users").update(data).eq("user_id", user_id).execute()
        _user_cache_invalidate(user_id)


def get_all_combo_counts() -> dict:
    """Возвращает {user_id: combo_count} для всех игроков."""
    r = _db().table("combo_logs").select("user_id").execute()
    counts: dict = {}
    for row in r.data:
        uid = row["user_id"]
        counts[uid] = counts.get(uid, 0) + 1
    return counts


# ── Extended player stats ─────────────────────────────────────────────────────

def get_avg_stats(user_id: int) -> dict:
    """Среднее место и средний счёт за игру."""
    r = _db().table("game_results").select("place,score").eq("user_id", user_id).execute()
    if not r.data:
        return {"avg_place": None, "avg_score": None, "count": 0}
    places = [row["place"] for row in r.data]
    scores = [row["score"] for row in r.data]
    return {
        "avg_place": round(sum(places) / len(places), 1),
        "avg_score": round(sum(scores) / len(scores)),
        "count":     len(places),
    }


def get_player_streak(user_id: int) -> dict:
    """Текущая серия побед/поражений (по убыванию game_id)."""
    r = (_db().table("game_results")
         .select("place")
         .eq("user_id", user_id)
         .order("game_id", desc=True)
         .limit(50)
         .execute())
    if not r.data:
        return {"type": "none", "count": 0}
    first_place  = r.data[0]["place"]
    streak_type  = "win" if first_place == 1 else "loss"
    count        = 0
    for row in r.data:
        is_win = row["place"] == 1
        if (streak_type == "win" and is_win) or (streak_type == "loss" and not is_win):
            count += 1
        else:
            break
    return {"type": streak_type, "count": count}


def get_game_history(user_id: int, limit: int = 10) -> list[dict]:
    """Последние N игр игрока с метаданными партии."""
    r = (_db().table("game_results")
         .select("place, score, color, went_first, game_id, points_change, games(total_players, played_at)")
         .eq("user_id", user_id)
         .order("game_id", desc=True)
         .limit(limit)
         .execute())
    return r.data


def get_game_co_players(game_ids: list[int], exclude_uid: int) -> dict[int, list[dict]]:
    """Возвращает {game_id: [{'place', 'color', 'username'}]} — соперники для набора игр."""
    if not game_ids:
        return {}
    r = (_db().table("game_results")
         .select("game_id, place, color, user_id")
         .in_("game_id", game_ids)
         .neq("user_id", exclude_uid)
         .order("place", desc=False)
         .execute())
    # Батч-запрос имён
    uid_set = {row["user_id"] for row in r.data}
    unames: dict[int, str] = {}
    if uid_set:
        ur = _db().table("users").select("user_id, username").in_("user_id", list(uid_set)).execute()
        unames = {u["user_id"]: u["username"] for u in ur.data}
    result: dict[int, list] = {}
    for row in r.data:
        gid = row["game_id"]
        result.setdefault(gid, []).append({
            "place":    row["place"],
            "color":    row.get("color") or "",
            "username": unames.get(row["user_id"], "?"),
        })
    return result


def get_specific_combo_count(user_id: int, combo_keys: list[str]) -> int:
    """Количество записей комбо с конкретными ключами для игрока."""
    if not combo_keys:
        return 0
    r = (_db().table("combo_logs")
         .select("id", count="exact")
         .eq("user_id", user_id)
         .in_("combo_key", combo_keys)
         .execute())
    return r.count if r.count is not None else len(r.data)


def get_head_to_head(uid1: int, uid2: int) -> dict:
    """Статистика личных встреч двух игроков."""
    r1 = _db().table("game_results").select("game_id,place").eq("user_id", uid1).execute()
    r2 = _db().table("game_results").select("game_id,place").eq("user_id", uid2).execute()
    map1   = {row["game_id"]: row["place"] for row in r1.data}
    map2   = {row["game_id"]: row["place"] for row in r2.data}
    shared = set(map1) & set(map2)
    uid1_ahead = sum(1 for g in shared if map1[g] < map2[g])
    uid2_ahead = sum(1 for g in shared if map2[g] < map1[g])
    return {
        "total":     len(shared),
        "uid1_wins": uid1_ahead,
        "uid2_wins": uid2_ahead,
    }


def get_global_combo_stats(limit: int = 10) -> list[dict]:
    """Топ комбинаций среди всех игроков."""
    r = _db().table("combo_logs").select("combo_key").execute()
    counts: dict[str, int] = {}
    for row in r.data:
        k = row["combo_key"]
        counts[k] = counts.get(k, 0) + 1
    return [
        {"combo_key": k, "count": v}
        for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    ]


# ── Solo mode ─────────────────────────────────────────────────────────────────

def get_solo_leaderboard(limit: int = 25) -> list[dict]:
    r = (_db().table("users")
         .select("username,solo_rating,solo_games_played,solo_wins")
         .gt("solo_games_played", 0)
         .order("solo_rating", desc=True)
         .limit(limit)
         .execute())
    return r.data


def update_solo_stats(user_id: int, *, win: bool, rated: bool, rating_delta: int):
    """Атомарно обновляет solo_rating, solo_games_played, solo_wins."""
    user = get_user(user_id)
    if not user:
        return
    new_rating = max(0, user.get("solo_rating", 0) + (rating_delta if rated else 0))
    new_games  = user.get("solo_games_played", 0) + 1
    new_wins   = user.get("solo_wins", 0) + (1 if win else 0)
    _db().table("users").update({
        "solo_rating":       new_rating,
        "solo_games_played": new_games,
        "solo_wins":         new_wins,
    }).eq("user_id", user_id).execute()
    _user_cache_invalidate(user_id)


def record_solo_game(user_id: int, *, score: int, bot_score: int,
                     target: int, win: bool, rated: bool):
    _db().table("solo_history").insert({
        "user_id":   user_id,
        "result":    "win" if win else "loss",
        "score":     score,
        "bot_score": bot_score,
        "target":    target,
        "rated":     rated,
    }).execute()


def get_solo_history(user_id: int, limit: int = 10) -> list[dict]:
    r = (_db().table("solo_history")
         .select("*")
         .eq("user_id", user_id)
         .order("id", desc=True)
         .limit(limit)
         .execute())
    return r.data


# ── Pending actions (состояния диалога) ───────────────────────────────────────

def get_pending_action(user_id: int) -> Optional[dict]:
    cached = _cache_get(_cache_pending, user_id, _CACHE_TTL_PENDING)
    if cached is not _MISS:
        return cached
    r = _db().table("pending_actions").select("*").eq("user_id", user_id).execute()
    result = r.data[0] if r.data else None
    _cache_set(_cache_pending, user_id, result)
    return result


def set_pending_action(user_id: int, action: str, data: dict | None = None):
    payload = {
        "user_id": user_id,
        "action":  action,
        "data":    data or {},
    }
    # Cache-first: следующий get_pending_action видит новое состояние моментально,
    # даже если HTTP-запрос ещё не завершился (или выполняется в to_thread).
    _cache_set(_cache_pending, user_id, payload)
    _db().table("pending_actions").upsert(payload).execute()


def clear_pending_action(user_id: int):
    _cache_set(_cache_pending, user_id, None)
    _db().table("pending_actions").delete().eq("user_id", user_id).execute()


# ── Game drafts (резервная копия финала игры) ─────────────────────────────────

def save_game_draft(admin_id: int, results: list, target: int):
    """Сохраняет итоги игры в резервную таблицу (upsert по admin_id)."""
    _db().table("game_drafts").upsert({
        "admin_id": admin_id,
        "results":  results,
        "target":   target,
    }).execute()


def get_game_draft(admin_id: int) -> Optional[dict]:
    r = _db().table("game_drafts").select("*").eq("admin_id", admin_id).execute()
    return r.data[0] if r.data else None


def delete_game_draft(admin_id: int):
    _db().table("game_drafts").delete().eq("admin_id", admin_id).execute()


# ── Achievements ──────────────────────────────────────────────────────────────

def get_user_achievements(user_id: int) -> list[str]:
    """Возвращает список achievement_id заработанных игроком."""
    r = _db().table("user_achievements").select("achievement_id").eq("user_id", user_id).execute()
    return [row["achievement_id"] for row in r.data]


def grant_achievement(user_id: int, achievement_id: str) -> bool:
    """Выдаёт ачивку если ещё не была выдана. Возвращает True если только что выдана.

    Ошибки разделяем:
      - UNIQUE VIOLATION (Postgres код 23505) — ачивка уже есть, тихо False
      - Любая другая ошибка — логируем как ERROR и возвращаем False
        (чтобы вызывающий код не падал, но проблема была видна в логах)
    """
    try:
        _db().table("user_achievements").insert({
            "user_id": user_id,
            "achievement_id": achievement_id,
        }).execute()
        return True
    except Exception as e:
        err_text = str(e)
        # Признаки уникальной ошибки PostgREST/Postgres
        if (
            "23505" in err_text
            or "duplicate key" in err_text.lower()
            or "unique constraint" in err_text.lower()
        ):
            return False
        # Реальная ошибка — логируем громко, чтобы не молчать
        import logging
        logging.getLogger(__name__).error(
            "grant_achievement: неожиданная ошибка БД (uid=%s ach=%s): %s",
            user_id, achievement_id, e,
        )
        return False


def revoke_achievement(user_id: int, achievement_id: str):
    """Удаляет ачивку у игрока (для дебага)."""
    _db().table("user_achievements").delete()\
        .eq("user_id", user_id).eq("achievement_id", achievement_id).execute()


def clear_user_combo_logs(user_id: int):
    """Удаляет ВСЕ комбо-логи игрока (только для дебага!)."""
    _db().table("combo_logs").delete().eq("user_id", user_id).execute()


def get_user_combo_log_keys(user_id: int) -> list[str]:
    """Возвращает все combo_key игрока в хронологическом порядке (для снэпшота)."""
    r = (_db().table("combo_logs")
         .select("combo_key")
         .eq("user_id", user_id)
         .order("id", desc=False)
         .execute())
    return [row["combo_key"] for row in r.data]


def restore_achievements(user_id: int, achievement_ids: list[str]):
    """Полностью заменяет набор ачивок пользователя на список из снэпшота."""
    _db().table("user_achievements").delete().eq("user_id", user_id).execute()
    if achievement_ids:
        _db().table("user_achievements").insert([
            {"user_id": user_id, "achievement_id": a}
            for a in achievement_ids
        ]).execute()


def restore_combo_logs(user_id: int, combo_keys: list[str]):
    """Полностью заменяет combo_logs пользователя на данные из снэпшота."""
    _db().table("combo_logs").delete().eq("user_id", user_id).execute()
    if not combo_keys:
        return
    # Вставляем батчами по 100 чтобы не упереться в лимит запроса
    for i in range(0, len(combo_keys), 100):
        chunk = combo_keys[i:i + 100]
        _db().table("combo_logs").insert([
            {"user_id": user_id, "combo_key": k}
            for k in chunk
        ]).execute()


# ── Extended stats ─────────────────────────────────────────────────────────────

def get_best_worst_score(user_id: int) -> dict:
    """Лучший и худший счёт за одну игру."""
    r = _db().table("game_results").select("score").eq("user_id", user_id).execute()
    if not r.data:
        return {"best": None, "worst": None}
    scores = [row["score"] for row in r.data]
    return {"best": max(scores), "worst": min(scores)}


def get_wins_by_player_count(user_id: int) -> dict:
    """Возвращает {total_players: wins} для всех сыгранных форматов."""
    r = (_db().table("game_results")
         .select("place, games(total_players)")
         .eq("user_id", user_id)
         .execute())
    wins: dict[int, int] = {}
    games: dict[int, int] = {}
    for row in r.data:
        n = (row.get("games") or {}).get("total_players", 0)
        if not n:
            continue
        games[n] = games.get(n, 0) + 1
        if row["place"] == 1:
            wins[n] = wins.get(n, 0) + 1
    return {"wins": wins, "games": games}


def get_total_rating_delta(user_id: int) -> int:
    """Сумма всех изменений рейтинга за все игры."""
    r = _db().table("game_results").select("points_change").eq("user_id", user_id).execute()
    return sum(row["points_change"] for row in r.data if row.get("points_change") is not None)


def get_color_play_counts(user_id: int) -> dict[str, int]:
    """Сколько раз играл каждым цветом."""
    r = (_db().table("game_results")
         .select("color")
         .eq("user_id", user_id)
         .not_.is_("color", "null")
         .execute())
    counts: dict[str, int] = {}
    for row in r.data:
        c = row.get("color") or ""
        if c:
            counts[c] = counts.get(c, 0) + 1
    return counts


# ── Calibration ───────────────────────────────────────────────────────────────

def update_calibration(user_id: int, perf_value: float):
    """Добавляет одно значение производительности (0.0–1.0) к счётчикам калибровки."""
    user = get_user(user_id)
    if not user:
        return
    new_sum   = user.get("calibration_perf_sum", 0) + perf_value
    new_count = user.get("calibration_games_count", 0) + 1
    _db().table("users").update({
        "calibration_perf_sum":   new_sum,
        "calibration_games_count": new_count,
    }).eq("user_id", user_id).execute()
    _user_cache_invalidate(user_id)


def complete_calibration(user_id: int, new_rating: int):
    """Завершает калибровку: выставляет финальный рейтинг и помечает is_calibrated=True."""
    _db().table("users").update({
        "is_calibrated": True,
        "rating":        new_rating,
    }).eq("user_id", user_id).execute()
    _user_cache_invalidate(user_id)


def mark_calibration_notified(user_id: int):
    """Отмечает что игрок уже увидел уведомление о калибровке."""
    _db().table("users").update({"calibration_notified": True}).eq("user_id", user_id).execute()
    _user_cache_invalidate(user_id)


# ── Магазин и инвентарь ───────────────────────────────────────────────────────

def get_coins(user_id: int) -> int:
    """Текущий баланс монет."""
    user = get_user(user_id)
    return int(user.get("coins", 0)) if user else 0


def add_coins(user_id: int, amount: int) -> int:
    """Прибавляет монеты. Возвращает новый баланс.
    Автоматически обновляет max_balance если баланс вырос до нового максимума."""
    user = get_user(user_id)
    if not user:
        return 0
    new_balance = max(0, int(user.get("coins", 0)) + int(amount))
    update: dict = {"coins": new_balance}
    if new_balance > int(user.get("max_balance", 0)):
        update["max_balance"] = new_balance
    _db().table("users").update(update).eq("user_id", user_id).execute()
    _user_cache_invalidate(user_id)
    return new_balance


def spend_coins(user_id: int, amount: int) -> tuple[bool, int]:
    """Списывает монеты. Возвращает (успех, новый_баланс).
    Если денег не хватает — возвращает (False, текущий_баланс)."""
    user = get_user(user_id)
    if not user:
        return False, 0
    cur = int(user.get("coins", 0))
    if cur < amount:
        return False, cur
    new_balance = cur - int(amount)
    _db().table("users").update({"coins": new_balance}).eq("user_id", user_id).execute()
    _user_cache_invalidate(user_id)
    return True, new_balance


def list_shop_items(slot_type: str | None = None) -> list[dict]:
    """Возвращает доступные вещи в магазине, опционально по типу слота.
    Скрывает предметы с stock=0 (тираж закончился)."""
    q = _db().table("shop_items").select("*").eq("available", True)
    if slot_type:
        q = q.eq("slot_type", slot_type)
    r = q.order("price", desc=False).execute()
    # Фильтруем предметы с нулевым остатком тиража
    return [it for it in (r.data or []) if it.get("stock") != 0]


def get_shop_item(item_id: int) -> Optional[dict]:
    """Один товар по item_id."""
    r = _db().table("shop_items").select("*").eq("item_id", item_id).execute()
    return r.data[0] if r.data else None


def get_user_items(user_id: int, slot_type: str | None = None) -> list[dict]:
    """Возвращает купленные пользователем вещи (с метаданными из shop_items)."""
    r = (_db().table("user_items")
         .select("item_id, purchased_at, shop_items(*)")
         .eq("user_id", user_id)
         .execute())
    items = []
    for row in r.data or []:
        item = row.get("shop_items") or {}
        if slot_type and item.get("slot_type") != slot_type:
            continue
        item["purchased_at"] = row.get("purchased_at")
        items.append(item)
    return items


def user_owns_item(user_id: int, item_id: int) -> bool:
    """Проверка владения товаром."""
    r = (_db().table("user_items").select("id")
         .eq("user_id", user_id).eq("item_id", item_id).execute())
    return bool(r.data)


def list_all_shop_items(slot_type: str | None = None) -> list[dict]:
    """Все товары в магазине (включая available=false) — для админ-UI."""
    q = _db().table("shop_items").select("*")
    if slot_type:
        q = q.eq("slot_type", slot_type)
    r = q.order("slot_type").order("price").execute()
    return r.data or []


def update_shop_item(item_id: int, **fields) -> bool:
    """Обновляет одно или несколько полей товара. Возвращает True если успешно.
    Разрешённые поля: name, content, description, price, rarity, available."""
    allowed = {"name", "content", "description", "price", "rarity", "available"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return False
    try:
        _db().table("shop_items").update(data).eq("item_id", item_id).execute()
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(
            "update_shop_item failed (item_id=%s, fields=%s): %s", item_id, data, e,
        )
        return False


def delete_shop_item(item_id: int) -> bool:
    """Удаляет товар. CASCADE на user_items сработает автоматически."""
    try:
        _db().table("shop_items").delete().eq("item_id", item_id).execute()
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("delete_shop_item failed (item_id=%s): %s", item_id, e)
        return False


def insert_shop_item(slot_type: str, content: str, name: str,
                     price: int, rarity: str = "common",
                     description: str | None = None) -> Optional[int]:
    """Добавляет новый товар. Возвращает item_id или None при ошибке."""
    try:
        r = _db().table("shop_items").insert({
            "slot_type": slot_type, "content": content, "name": name,
            "price": price, "rarity": rarity,
            "description": description, "available": True,
        }).execute()
        return r.data[0]["item_id"] if r.data else None
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("insert_shop_item failed: %s", e)
        return None


def count_item_owners(item_id: int) -> int:
    """Сколько игроков купило этот товар — для проверки перед удалением."""
    r = (_db().table("user_items").select("id", count="exact")
         .eq("item_id", item_id).execute())
    return r.count if r.count is not None else len(r.data or [])


def buy_item(user_id: int, item_id: int) -> tuple[bool, str, int]:
    """Покупка товара. Возвращает (успех, сообщение, новый_баланс_монет).

    Атомарность: проверяем владение/баланс/тираж, списываем монеты,
    добавляем в инвентарь, уменьшаем stock. Если что-то упало — откатываем.
    """
    item = get_shop_item(item_id)
    if not item or not item.get("available", True):
        return False, "Товар не найден или недоступен.", get_coins(user_id)
    if user_owns_item(user_id, item_id):
        return False, "У тебя уже есть этот предмет.", get_coins(user_id)
    # Проверяем тираж
    stock = item.get("stock")
    if stock is not None and stock <= 0:
        return False, "Тираж этого предмета закончился!", get_coins(user_id)
    price = int(item["price"])
    ok, new_balance = spend_coins(user_id, price)
    if not ok:
        return False, f"Недостаточно монет. Нужно {price}, у тебя {new_balance}.", new_balance
    try:
        _db().table("user_items").insert({
            "user_id": user_id,
            "item_id": item_id,
        }).execute()
    except Exception as e:
        add_coins(user_id, price)
        return False, f"Ошибка покупки: {e}", get_coins(user_id)
    # Уменьшаем stock если ограничен
    if stock is not None:
        try:
            _db().table("shop_items").update(
                {"stock": max(0, stock - 1)}
            ).eq("item_id", item_id).execute()
        except Exception:
            pass  # не критично — товар уже куплен
    return True, f"✅ Куплено: {item['name']} за {price} 💰", new_balance


def grant_item(user_id: int, item_id: int) -> bool:
    """Добавляет предмет в инвентарь бесплатно (призы, дебаг и т.д.)."""
    try:
        _db().table("user_items").insert({
            "user_id": user_id,
            "item_id": item_id,
        }).execute()
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(
            "grant_item failed (uid=%s item_id=%s): %s", user_id, item_id, e
        )
        return False


def remove_user_item(user_id: int, item_id: int) -> bool:
    """Удаляет предмет из инвентаря игрока (для дебага).
    Также автоматически снимает предмет с экипированных слотов.
    """
    item = get_shop_item(item_id)
    if item:
        slot = item.get("slot_type", "")
        content = item.get("content", "")
        user = get_user(user_id)
        if user and content:
            # Снимаем предмет со всех слотов где он надето
            clear = {}
            slot_fields = {
                "prefix":      ["combo_prefix1", "combo_prefix2"],
                "suffix":      ["combo_suffix1", "combo_suffix2"],
                "title":       ["combo_title"],
                "banner":      ["combo_banner"],
                "divider":     ["combo_divider"],
                "color_frame": ["combo_color_frame"],
                "bank_fx":     ["combo_bank_fx"],
                "win_message": ["combo_win_message"],
            }
            for field in slot_fields.get(slot, []):
                if user.get(field) == content:
                    clear[field] = None
            if clear:
                update_combo_customization(user_id, **clear)
    try:
        _db().table("user_items").delete()\
            .eq("user_id", user_id).eq("item_id", item_id).execute()
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(
            "remove_user_item failed (uid=%s item_id=%s): %s", user_id, item_id, e
        )
        return False


# ── Global Roulette ───────────────────────────────────────────────────────────

def get_global_roulette_state() -> dict:
    """Возвращает текущий котёл и номер раунда. Создаёт запись если нет."""
    r = _db().table("global_roulette_state").select("*").eq("id", 1).execute()
    if r.data:
        return r.data[0]
    # Создаём начальную запись
    _db().table("global_roulette_state").insert(
        {"id": 1, "pot": 0, "round": 0}
    ).execute()
    return {"id": 1, "pot": 0, "round": 0}


def add_global_roulette_bet(user_id: int, amount: int) -> tuple[bool, int]:
    """Добавляет ставку игрока в текущий котёл. Списывает монеты.
    Возвращает (успех, новый_баланс)."""
    ok, new_balance = spend_coins(user_id, amount)
    if not ok:
        return False, new_balance
    state = get_global_roulette_state()
    new_pot = int(state.get("pot", 0)) + amount
    _db().table("global_roulette_state").update(
        {"pot": new_pot}
    ).eq("id", 1).execute()
    _db().table("global_roulette_bets").insert(
        {"user_id": user_id, "amount": amount, "round": state.get("round", 0)}
    ).execute()
    return True, new_balance


def get_global_roulette_bets(round_num: int) -> list[dict]:
    """Все ставки текущего раунда."""
    r = (_db().table("global_roulette_bets")
         .select("user_id, amount")
         .eq("round", round_num)
         .execute())
    return r.data or []


def close_global_roulette_round(new_pot: int) -> int:
    """Завершает раунд: обновляет pot, round и last_spin_at.
    Возвращает новый номер раунда."""
    import datetime
    state     = get_global_roulette_state()
    new_round = int(state.get("round", 0)) + 1
    _db().table("global_roulette_state").update({
        "pot":          new_pot,
        "round":        new_round,
        "last_spin_at": datetime.datetime.utcnow().isoformat(),
    }).eq("id", 1).execute()
    return new_round


# ── Переводы монет ────────────────────────────────────────────────────────────

def transfer_coins(from_uid: int, to_uid: int, amount: int) -> tuple[bool, str]:
    """Переводит монеты от одного игрока другому.
    Возвращает (успех, сообщение_об_ошибке)."""
    if from_uid == to_uid:
        return False, "Нельзя переводить самому себе."
    if amount <= 0:
        return False, "Сумма должна быть положительной."
    ok, _ = spend_coins(from_uid, amount)
    if not ok:
        return False, "Недостаточно монет."
    add_coins(to_uid, amount)
    return True, ""


def get_recent_opponents(user_id: int, limit: int = 8) -> list[dict]:
    """Возвращает последних уникальных соперников по истории игр."""
    # Берём последние 30 game_id игрока
    r = (_db().table("game_results")
         .select("game_id")
         .eq("user_id", user_id)
         .order("game_id", desc=True)
         .limit(30)
         .execute())
    if not r.data:
        return []
    game_ids = [row["game_id"] for row in r.data]

    # Все участники этих игр, кроме самого игрока
    r2 = (_db().table("game_results")
          .select("user_id")
          .in_("game_id", game_ids)
          .neq("user_id", user_id)
          .execute())
    if not r2.data:
        return []

    # Дедупликация с сохранением порядка (свежие первые)
    seen: set[int] = set()
    unique_uids: list[int] = []
    for row in r2.data:
        uid = row["user_id"]
        if uid not in seen:
            seen.add(uid)
            unique_uids.append(uid)
            if len(unique_uids) >= limit:
                break

    if not unique_uids:
        return []

    r3 = (_db().table("users")
          .select("user_id,username,rating,coins")
          .in_("user_id", unique_uids)
          .execute())
    user_map = {u["user_id"]: u for u in (r3.data or [])}
    return [user_map[uid] for uid in unique_uids if uid in user_map]


# ── VIP Зал казино ────────────────────────────────────────────────────────────

def get_active_vip_lobbies() -> list[dict]:
    """Активные лобби (статус waiting), не старше 12 часов."""
    r = (_db().table("vip_lobbies")
         .select("id,host_uid,entry_fee,pot,status,created_at")
         .eq("status", "waiting")
         .order("created_at", desc=True)
         .limit(8)
         .execute())
    return r.data or []


def get_vip_lobby(lobby_id: int) -> Optional[dict]:
    r = _db().table("vip_lobbies").select("*").eq("id", lobby_id).execute()
    return r.data[0] if r.data else None


def get_vip_lobby_players(lobby_id: int) -> list[dict]:
    """Все игроки лобби с данными руки."""
    r = (_db().table("vip_lobby_players")
         .select("user_id,hand,hand_rank,hand_name")
         .eq("lobby_id", lobby_id)
         .execute())
    return r.data or []


def create_vip_lobby(host_uid: int, entry_fee: int) -> int:
    """Создаёт лобби, списывает взнос с хоста. Возвращает lobby_id (0 при ошибке)."""
    ok, _ = spend_coins(host_uid, entry_fee)
    if not ok:
        return 0
    r = (_db().table("vip_lobbies").insert({
        "host_uid":  host_uid,
        "entry_fee": entry_fee,
        "pot":       entry_fee,
        "status":    "waiting",
    }).execute())
    if not r.data:
        return 0
    lobby_id = r.data[0]["id"]
    _db().table("vip_lobby_players").insert({
        "lobby_id": lobby_id,
        "user_id":  host_uid,
    }).execute()
    return lobby_id


def join_vip_lobby(lobby_id: int, user_id: int) -> tuple[bool, str]:
    """Присоединяется к лобби, списывает взнос. Возвращает (успех, ошибка)."""
    lobby = get_vip_lobby(lobby_id)
    if not lobby:
        return False, "Лобби не найдено."
    if lobby["status"] != "waiting":
        return False, "Игра уже идёт или завершена."
    players = get_vip_lobby_players(lobby_id)
    if len(players) >= 6:
        return False, "Лобби заполнено (макс. 6)."
    if any(p["user_id"] == user_id for p in players):
        return False, "Ты уже в этом лобби."
    ok, _ = spend_coins(user_id, lobby["entry_fee"])
    if not ok:
        return False, "Недостаточно монет."
    _db().table("vip_lobby_players").insert({
        "lobby_id": lobby_id,
        "user_id":  user_id,
    }).execute()
    _db().table("vip_lobbies").update(
        {"pot": lobby["pot"] + lobby["entry_fee"]}
    ).eq("id", lobby_id).execute()
    return True, ""


def leave_vip_lobby(lobby_id: int, user_id: int) -> tuple[bool, int]:
    """Выходит из лобби и возвращает взнос. Если хост — закрывает лобби и рефандит всех.
    Возвращает (был_хостом, сумма_возврата_вызывающему)."""
    lobby = get_vip_lobby(lobby_id)
    if not lobby or lobby["status"] != "waiting":
        return False, 0
    fee = lobby["entry_fee"]
    is_host = (lobby["host_uid"] == user_id)

    if is_host:
        # Рефанд всем участникам, закрываем лобби
        players = get_vip_lobby_players(lobby_id)
        for p in players:
            add_coins(p["user_id"], fee)
        _db().table("vip_lobbies").update({"status": "finished"}).eq("id", lobby_id).execute()
        return True, fee
    else:
        _db().table("vip_lobby_players").delete(
        ).eq("lobby_id", lobby_id).eq("user_id", user_id).execute()
        _db().table("vip_lobbies").update(
            {"pot": lobby["pot"] - fee}
        ).eq("id", lobby_id).execute()
        add_coins(user_id, fee)
        return False, fee


def start_vip_game(lobby_id: int, hands_data: list[dict]) -> bool:
    """Запускает игру: сохраняет руки, переводит в статус playing.
    hands_data: [{user_id, hand, hand_rank, hand_name}, ...]"""
    _db().table("vip_lobbies").update({"status": "playing"}).eq("id", lobby_id).execute()
    for hd in hands_data:
        (_db().table("vip_lobby_players")
         .update({
             "hand":      hd["hand"],
             "hand_rank": hd["hand_rank"],
             "hand_name": hd["hand_name"],
         })
         .eq("lobby_id", lobby_id)
         .eq("user_id",  hd["user_id"])
         .execute())
    return True


def finish_vip_game(lobby_id: int, winner_uid: int, prize: int) -> bool:
    """Завершает игру: начисляет приз победителю, статус finished."""
    add_coins(winner_uid, prize)
    _db().table("vip_lobbies").update({
        "status":     "finished",
        "winner_uid": winner_uid,
    }).eq("id", lobby_id).execute()
    return True


# ── Зал Богатства ─────────────────────────────────────────────────────────────

_HALL_TIERS = ("bronze", "silver", "gold", "diamond")


def update_max_casino_win(user_id: int, amount: int) -> None:
    """Обновляет рекорд максимального выигрыша в казино, если amount больше текущего."""
    user = get_user(user_id)
    if not user:
        return
    if amount > int(user.get("max_casino_win", 0)):
        _db().table("users").update(
            {"max_casino_win": amount}
        ).eq("user_id", user_id).execute()
        _user_cache_invalidate(user_id)


def get_hall_plaques(limit: int = 30) -> list[dict]:
    """Все таблички, отсортированные: сначала diamond → bronze, внутри тира — по дате."""
    r = (_db().table("hall_plaques")
         .select("id,user_id,tier,message,purchased_at")
         .order("purchased_at", desc=False)
         .limit(limit)
         .execute())
    rows = r.data or []
    order = {t: i for i, t in enumerate(("diamond", "gold", "silver", "bronze"))}
    return sorted(rows, key=lambda x: (order.get(x["tier"], 9), x["purchased_at"]))


def get_user_plaques(user_id: int) -> list[dict]:
    """Таблички конкретного игрока."""
    r = (_db().table("hall_plaques")
         .select("*")
         .eq("user_id", user_id)
         .execute())
    return r.data or []


def buy_hall_plaque(user_id: int, tier: str, message: str,
                    price: int) -> tuple[bool, str]:
    """Покупает табличку. Возвращает (успех, описание ошибки)."""
    if tier not in _HALL_TIERS:
        return False, "Неизвестный тир."
    # Проверяем, нет ли уже такой таблички
    existing = (_db().table("hall_plaques")
                .select("id")
                .eq("user_id", user_id)
                .eq("tier", tier)
                .execute())
    if existing.data:
        return False, "У тебя уже есть табличка этого тира."
    ok, _ = spend_coins(user_id, price)
    if not ok:
        return False, f"Недостаточно монет. Нужно {price}."
    _db().table("hall_plaques").insert({
        "user_id": user_id,
        "tier":    tier,
        "message": message[:60],
    }).execute()
    return True, ""


def list_hall_shop_items(slot_type: str | None = None) -> list[dict]:
    """Предметы только из Зала Богатства (hall_only=true)."""
    q = (_db().table("shop_items")
         .select("*")
         .eq("available", True)
         .eq("hall_only", True))
    if slot_type:
        q = q.eq("slot_type", slot_type)
    r = q.order("price", desc=False).execute()
    return [it for it in (r.data or []) if it.get("stock") != 0]


# ── Casino Shares ─────────────────────────────────────────────────────────────

SHARE_PRICE_BUY      = 5_000_000
SHARE_PRICE_SELL     = 4_500_000
TOTAL_SHARES         = 10_000
MAX_SHARES_PER_PLAYER = 100


def get_casino_economy() -> dict:
    """Возвращает единственную строку casino_economy."""
    r = _db().table("casino_economy").select("*").eq("id", 1).execute()
    if r.data:
        return r.data[0]
    # Страховка — строка должна быть создана миграцией
    _db().table("casino_economy").insert(
        {"id": 1, "shares_sold": 0, "dividend_pool": 0}
    ).execute()
    return {"id": 1, "shares_sold": 0, "dividend_pool": 0}


def get_casino_shares(user_id: int) -> dict:
    """Портфель игрока. Если записи нет — возвращает пустой шаблон."""
    r = _db().table("casino_shares").select("*").eq("user_id", user_id).execute()
    if r.data:
        return r.data[0]
    return {"user_id": user_id, "shares": 0, "dividends_claimed": 0, "last_claim_at": None}


def add_to_dividend_pool(amount: int) -> None:
    """Атомарно прибавляет amount к dividend_pool."""
    if amount <= 0:
        return
    eco = get_casino_economy()
    new_pool = int(eco.get("dividend_pool", 0)) + int(amount)
    _db().table("casino_economy").update({"dividend_pool": new_pool}).eq("id", 1).execute()


def buy_casino_shares(user_id: int, count: int) -> tuple[bool, str]:
    """Покупает count акций. Возвращает (ok, error_msg)."""
    import datetime as _dt
    if count <= 0:
        return False, "Количество должно быть > 0."
    eco       = get_casino_economy()
    portfolio = get_casino_shares(user_id)
    current   = int(portfolio.get("shares", 0))
    sold      = int(eco.get("shares_sold", 0))
    available = TOTAL_SHARES - sold

    if available <= 0:
        return False, "Все акции уже раскуплены."
    if count > available:
        return False, f"Доступно только {available} акций."
    if current + count > MAX_SHARES_PER_PLAYER:
        can_buy = MAX_SHARES_PER_PLAYER - current
        return False, (
            f"Нельзя держать более {MAX_SHARES_PER_PLAYER} акций.\n"
            f"У тебя: {current}. Можно купить ещё: {can_buy}."
        )

    total_cost = SHARE_PRICE_BUY * count
    ok, _ = spend_coins(user_id, total_cost)
    if not ok:
        return False, f"Недостаточно монет. Нужно {total_cost:,}."

    # Обновляем портфель
    _db().table("casino_shares").upsert({
        "user_id": user_id,
        "shares":  current + count,
        "dividends_claimed": int(portfolio.get("dividends_claimed", 0)),
    }).execute()

    # Обновляем экономику
    _db().table("casino_economy").update({
        "shares_sold": sold + count,
    }).eq("id", 1).execute()

    return True, ""


def sell_casino_shares(user_id: int, count: int) -> tuple[bool, str]:
    """Продаёт count акций казино. Возвращает (ok, error_msg)."""
    if count <= 0:
        return False, "Количество должно быть > 0."
    portfolio = get_casino_shares(user_id)
    current   = int(portfolio.get("shares", 0))
    if current < count:
        return False, f"Недостаточно акций. У тебя: {current}."

    payout = SHARE_PRICE_SELL * count
    add_coins(user_id, payout)

    eco = get_casino_economy()
    _db().table("casino_shares").update({
        "shares": current - count,
    }).eq("user_id", user_id).execute()

    _db().table("casino_economy").update({
        "shares_sold": max(0, int(eco.get("shares_sold", 0)) - count),
    }).eq("id", 1).execute()

    return True, ""


def claim_dividends(user_id: int) -> tuple[bool, int, str]:
    """
    Запрашивает дивиденды. 60-минутный кулдаун.
    Возвращает (ok, amount_credited, error_msg).
    """
    import datetime as _dt
    portfolio = get_casino_shares(user_id)
    shares    = int(portfolio.get("shares", 0))
    if shares == 0:
        return False, 0, "У тебя нет акций казино."

    # Проверяем кулдаун
    last_claim = portfolio.get("last_claim_at")
    if last_claim:
        try:
            lc = _dt.datetime.fromisoformat(last_claim)
            if lc.tzinfo is None:
                lc = lc.replace(tzinfo=_dt.timezone.utc)
            now     = _dt.datetime.now(_dt.timezone.utc)
            elapsed = (now - lc).total_seconds()
            if elapsed < 3600:
                remaining = int(3600 - elapsed)
                m, s = divmod(remaining, 60)
                return False, 0, f"Следующая выплата через {m}м {s}с."
        except Exception:
            pass

    eco     = get_casino_economy()
    pool    = int(eco.get("dividend_pool", 0))
    claimed = int(portfolio.get("dividends_claimed", 0))
    owed    = int(pool * shares / TOTAL_SHARES) - claimed

    if owed <= 0:
        return False, 0, (
            "Дивиденды ещё не накоплены.\n"
            "Пул пополняется с каждым проигрышем в казино."
        )

    add_coins(user_id, owed)
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    _db().table("casino_shares").update({
        "dividends_claimed": claimed + owed,
        "last_claim_at":     now_iso,
    }).eq("user_id", user_id).execute()

    return True, owed, ""


# ── Кросс-бот обменник ────────────────────────────────────────────────────────

CROSS_RATE = 3      # 1 Кубик = 3 FUT-монеты
CROSS_FEE  = 0.10   # 10% комиссия сжигается (дефляция)


def _cross_calc(amount_in: int, direction: str) -> tuple[int, int]:
    """Возвращает (amount_out, commission) для конвертации."""
    if direction == "cube_to_fut":
        gross      = amount_in * CROSS_RATE
        commission = max(1, round(gross * CROSS_FEE))
        return gross - commission, commission
    else:  # fut_to_cube
        gross      = amount_in / CROSS_RATE
        commission = max(0, round(gross * CROSS_FEE))
        return max(1, int(gross) - int(commission)), int(commission)


def create_cross_transfer(user_id: int, direction: str, amount_in: int) -> tuple[bool, int, str]:
    """
    Инициирует конвертацию монет между ботами.
    'cube_to_fut': списывает кубики здесь, создаёт pending-запись для FUT-бота.
    'fut_to_cube': создаёт запись (FUT-бот уже списал монеты у себя).
    Возвращает (ok, transfer_id, error).
    """
    if amount_in <= 0:
        return False, 0, "Сумма должна быть положительной."
    if direction not in ("cube_to_fut", "fut_to_cube"):
        return False, 0, "Неверное направление."

    amount_out, commission = _cross_calc(amount_in, direction)

    if direction == "cube_to_fut":
        ok, _ = spend_coins(user_id, amount_in)
        if not ok:
            return False, 0, "Недостаточно монет."

    r = _db().table("cross_transfers").insert({
        "user_id":    user_id,
        "direction":  direction,
        "amount_in":  amount_in,
        "commission": commission,
        "amount_out": amount_out,
        "status":     "pending",
    }).execute()
    if not r.data:
        if direction == "cube_to_fut":
            add_coins(user_id, amount_in)  # возвращаем монеты при ошибке БД
        return False, 0, "Ошибка базы данных."
    return True, r.data[0]["id"], ""


def get_pending_cross_transfers(user_id: int, direction: str | None = None) -> list[dict]:
    """Все pending переводы пользователя (опционально фильтрует по направлению)."""
    q = (
        _db().table("cross_transfers")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", "pending")
    )
    if direction:
        q = q.eq("direction", direction)
    return q.order("created_at").execute().data or []


def claim_cross_transfers(user_id: int, direction: str) -> tuple[bool, int, str]:
    """
    Забирает все pending переводы данного направления.
    'fut_to_cube': добавляет кубики пользователю здесь.
    'cube_to_fut': только маркирует claimed (FUT-бот добавит монеты у себя).
    Возвращает (ok, total_amount_out, error).
    """
    import datetime as _dt
    transfers = get_pending_cross_transfers(user_id, direction)
    if not transfers:
        return False, 0, "Нет ожидающих переводов."

    total_out = sum(int(t["amount_out"]) for t in transfers)
    ids       = [t["id"] for t in transfers]
    now_iso   = _dt.datetime.now(_dt.timezone.utc).isoformat()

    _db().table("cross_transfers").update({
        "status":     "claimed",
        "claimed_at": now_iso,
    }).in_("id", ids).execute()

    if direction == "fut_to_cube":
        add_coins(user_id, total_out)

    return True, total_out, ""


def cancel_cross_transfer(transfer_id: int, user_id: int) -> tuple[bool, str]:
    """Отменяет pending перевод. Для cube_to_fut возвращает кубики."""
    r = (
        _db().table("cross_transfers")
        .select("*")
        .eq("id", transfer_id)
        .eq("user_id", user_id)
        .eq("status", "pending")
        .execute()
    )
    if not r.data:
        return False, "Перевод не найден."
    t = r.data[0]
    _db().table("cross_transfers").update({"status": "expired"}).eq("id", transfer_id).execute()
    if t["direction"] == "cube_to_fut":
        add_coins(user_id, int(t["amount_in"]))  # возвращаем кубики
    return True, ""