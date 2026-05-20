"""
bot.py — все хэндлеры и фабрика приложения.
Состояния хранятся в Supabase pending_actions (serverless-совместимо).
"""
import asyncio
import logging
import random
import warnings

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)
import config
import database as db
import scoring

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
warnings.filterwarnings("ignore", message=".*per_message=False.*", category=UserWarning)


# ── Async helpers ─────────────────────────────────────────────────────────────
# supabase-py синхронный — каждый .execute() блокирует весь event loop. Эти
# обёртки выносят БД-работу в thread-pool, чтобы другие хэндлеры могли
# обрабатываться параллельно.

async def adb(func, *args, **kwargs):
    """Запускает blocking БД-функцию в thread-pool без блокировки event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


def fire_and_forget(coro):
    """Запускает корутину в фоне — игрок не ждёт результата.
    Используем для некритичных операций: логирование комбо, рассылка уведомлений."""
    async def _runner():
        try:
            await coro
        except Exception as e:
            logger.warning("Background task failed: %s", e)
    asyncio.create_task(_runner())


async def _safe_send(context, chat_id: int, text: str, **kwargs):
    """Отправляет сообщение, проглатывая ошибки (юзер заблокировал бота и т.п.)."""
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception:
        pass


def log_combo_bg(user_id: int, combo_key: str):
    """Fire-and-forget логирование комбо — игрок не ждёт ответа от Supabase."""
    fire_and_forget(adb(db.log_combo, user_id, combo_key))

MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣"]

# ── Экономика монет ──────────────────────────────────────────────────────────
COIN_BASE_BY_PLACE      = {1: 50, 2: 30, 3: 20, 4: 15, 5: 10, 6: 10}
COIN_RESULT_DIVISOR     = 1000   # 1 монета за каждую 1 000 набранных очков
COIN_PERFECT_BONUS      = 50     # доп. награда если набрал >= цели
SOLO_COIN_MULTIPLIER    = 0.5    # соло даёт половину (анти-фарм)


def _coins_for_game(place: int, score: int, target: int, *, is_solo: bool = False) -> int:
    """Сколько монет получает игрок за одну запись игры."""
    base   = COIN_BASE_BY_PLACE.get(place, 10)
    bonus  = score // COIN_RESULT_DIVISOR
    perfect = COIN_PERFECT_BONUS if target and score >= target else 0
    total  = base + bonus + perfect
    if is_solo:
        total = int(total * SOLO_COIN_MULTIPLIER)
    return total

COLORS = [
    ("🔴", "Красный"),
    ("🟡", "Жёлтый"),
    ("🔵", "Синий"),
    ("🟢", "Зелёный"),
    ("🟣", "Фиолетовый"),
    ("⚪", "Белый"),
]

# Сопоставление эмодзи → имя фракции для движка очков
FACTION_BY_EMOJI = {
    "🔴": "red",
    "🟡": "yellow",
    "🔵": "blue",
    "🟢": "green",
    "🟣": "purple",
    "⚪": "white",
}

# Цветные квадраты для отображения цветных кубиков (заметнее круглых маркеров)
COLOR_SQUARE = {
    "red":    "🟥",
    "yellow": "🟨",
    "blue":   "🟦",
    "green":  "🟩",
    "purple": "🟪",
    "white":  "⬜",
}

# Порог получения 2-го кубика своей фракции
FACTION_2ND_THRESHOLD = 5000

# Рубежи магазина: target → {num_players → [thresholds]}
# Чем больше игроков — тем меньше рубежей (меньше ходов каждый).
# Для числа игроков не в словаре берётся ближайший ≥ num_players ключ.
SHOP_THRESHOLDS: dict[int, dict[int, list[int]]] = {
    20000: {
        3: [2000, 5000, 10000, 12000, 15000],
        4: [2000, 5000, 8000,  15000],
    },
    25000: {
        3: [2000, 5000, 8000,  12000, 15000],
        4: [2000, 5000, 8000,  15000],
    },
}

# ── Special dice catalog ───────────────────────────────────────────────────────

SPECIAL_INFO: dict[str, dict] = {
    "joker_6":  {"name": "🃏 Джокер",      "face": 6,    "emoji": "🃏"},
    "joker_5":  {"name": "🃏 Джокер",      "face": 5,    "emoji": "🃏"},
    "thief":    {"name": "🥷 Вор",          "face": 6,    "emoji": "🥷"},
    "reroll_6": {"name": "🔄 Рерол",        "face": 6,    "emoji": "🔄"},
    "reroll_5": {"name": "🔄 Рерол",        "face": 5,    "emoji": "🔄"},
    "robbery":  {"name": "⚔️ Разбой",       "face": 6,    "emoji": "⚔️"},
    "ruben":    {"name": "💪 Рубен",        "face": None,  "emoji": "💪"},
    "tripling": {"name": "✖️ Утроение",     "face": 6,    "emoji": "✖️"},
    "cross":    {"name": "✝️ Крест",        "face": 6,    "emoji": "✝️"},
    "freeze_6": {"name": "❄️ Заморозка",    "face": 6,    "emoji": "❄️"},
    "freeze_1": {"name": "❄️ Заморозка",    "face": 1,    "emoji": "❄️"},
}

# Рубен: цвет по значению грани (только одиночные бонусы, не ульта)
RUBEN_FACTION: dict[int, str] = {5: "red", 4: "yellow", 2: "blue", 3: "green"}

# Пул магазина: все кубики по 2 экземпляра, Разбой — 1
SHOP_POOL: list[str] = (
    ["joker_6", "joker_5", "thief", "thief",
     "reroll_6", "reroll_5", "robbery",
     "ruben", "ruben", "tripling", "tripling",
     "cross", "cross", "freeze_6", "freeze_1"]
)

# ── Combo catalogue ────────────────────────────────────────────────────────────

DICE_N   = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣"}
SUB_MULT = {"three": 3, "four": 4, "five": 5, "six": 6}

# Человекочитаемые названия комбинаций (для статистики)
COMBOS: dict[str, str] = {
    "straight_full": "1-2-3-4-5-6",
    "straight_low":  "1-2-3-4-5",
    "straight_high": "2-3-4-5-6",
    "special_yellow_456": "🟡 4-5-6",
    "ulta_red":          "🔴 ×2 красных 5️⃣",
    "ulta_yellow_full":  "🟡 Стрит 1-6 (жёлт. в кажд. части)",
    "ulta_yellow_low":   "🟡 1-2-3 (≥2 жёлтых)",
    "ulta_blue":         "🔵 ×2 синих 2️⃣",
    "ulta_green":        "🟢 ×2 зелёных 3️⃣",
    "ulta_white":        "⚪ ×2 белых 1️⃣",
    "ulta_purple":       "🟣 ×2 фиолет. 6️⃣",
    # Спец кубики
    "special_joker":   "🃏 Джокер",
    "special_thief":   "🥷 Вор",
    "special_reroll":  "🔄 Рерол",
    "special_robbery": "⚔️ Разбой",
    "special_ruben":   "💪 Рубен",
    "special_triple":  "✖️ Утроение",
    "special_cross":   "✝️ Крест",
    "special_freeze":  "❄️ Заморозка",
}
for _n in range(1, 7):
    COMBOS[f"three_{_n}"] = "-".join([str(_n)] * 3)
    COMBOS[f"four_{_n}"]  = "-".join([str(_n)] * 4)
    COMBOS[f"five_{_n}"]  = "-".join([str(_n)] * 5)
    COMBOS[f"six_{_n}"]   = "-".join([str(_n)] * 6)


# ── Combo level system ────────────────────────────────────────────────────────

COMBO_LEVELS = [
    {"level": 0,  "name": "Новичок",       "threshold": 0,     "badge": "",    "unlocks": []},
    {"level": 1,  "name": "Наблюдатель",   "threshold": 3,     "badge": "👁",   "unlocks": ["combo_count"]},
    {"level": 2,  "name": "Записной",      "threshold": 8,     "badge": "📋",   "unlocks": []},
    {"level": 3,  "name": "Охотник",       "threshold": 20,    "badge": "🎯",   "unlocks": ["profile_badge"]},
    {"level": 4,  "name": "Следопыт",      "threshold": 45,    "badge": "🔍",   "unlocks": ["prefix1"]},
    {"level": 5,  "name": "Исследователь", "threshold": 90,    "badge": "🧭",   "unlocks": ["top5_combos"]},
    {"level": 6,  "name": "Коллекционер",  "threshold": 160,   "badge": "💼",   "unlocks": ["leaderboard_badge"]},
    {"level": 7,  "name": "Энтузиаст",     "threshold": 260,   "badge": "⭐",   "unlocks": ["fav_combo"]},
    {"level": 8,  "name": "Знаток",        "threshold": 400,   "badge": "🏆",   "unlocks": ["first_combo"]},
    {"level": 9,  "name": "Эксперт",       "threshold": 600,   "badge": "💫",   "unlocks": ["suffix1"]},
    {"level": 10, "name": "Мастер",        "threshold": 900,   "badge": "🎖",   "unlocks": ["title"]},
    {"level": 11, "name": "Чемпион",       "threshold": 1300,  "badge": "🥇",   "unlocks": ["rarity_stats"]},
    {"level": 12, "name": "Гуру",          "threshold": 1800,  "badge": "🔮",   "unlocks": ["prefix2"]},
    {"level": 13, "name": "Легенда",       "threshold": 2500,  "badge": "👑",   "unlocks": ["banner"]},
    {"level": 14, "name": "Мифический",    "threshold": 3500,  "badge": "🌟",   "unlocks": ["avg_per_game"]},
    {"level": 15, "name": "Элита",         "threshold": 4800,  "badge": "💠",   "unlocks": ["suffix2", "about"]},
    {"level": 16, "name": "Бессмертный",   "threshold": 6200,  "badge": "🛡",   "unlocks": ["all_slots"]},
    {"level": 17, "name": "Архивариус",    "threshold": 7800,  "badge": "📜",   "unlocks": ["global_stats"]},
    {"level": 18, "name": "Вечный",        "threshold": 9000,  "badge": "♾️",   "unlocks": ["divider"]},
    {"level": 19, "name": "Бог комбо",     "threshold": 10000, "badge": "🌌",   "unlocks": ["top_leaderboard"]},
]


def _combo_level(count: int) -> dict:
    """Текущий уровень по числу комбо."""
    cur = COMBO_LEVELS[0]
    for lvl in COMBO_LEVELS:
        if count >= lvl["threshold"]:
            cur = lvl
        else:
            break
    return cur


def _combo_next_level(count: int) -> dict | None:
    for lvl in COMBO_LEVELS:
        if lvl["threshold"] > count:
            return lvl
    return None


def _has_unlock(level_n: int, unlock: str) -> bool:
    """Разблокирован ли конкретный элемент на данном уровне (учитывает все предыдущие)."""
    for lvl in COMBO_LEVELS:
        if lvl["level"] <= level_n and unlock in lvl["unlocks"]:
            return True
    return False


def _combo_display_name(user: dict, level_n: int) -> str:
    """Имя с prefix/suffix эмодзи и цветной рамкой.

    Если слот заполнен (через достижение или покупку) — отображается. Уровни
    комбо больше не гейтят показ, только что можно МЕНЯТЬ в combo_customize UI.
    Цветная рамка (color_frame) идёт самой внешней оболочкой.
    """
    p1 = user.get("combo_prefix1") or ""
    p2 = user.get("combo_prefix2") or ""
    s1 = user.get("combo_suffix1") or ""
    s2 = user.get("combo_suffix2") or ""
    cf = user.get("combo_color_frame") or ""
    name = user["username"]
    pre  = (" ".join(filter(None, [p1, p2])) + " ") if (p1 or p2) else ""
    suf  = (" " + " ".join(filter(None, [s1, s2]))) if (s1 or s2) else ""
    inner = f"{pre}{name}{suf}"
    if cf:
        return f"{cf} {inner} {cf}"
    return inner


def _user_display_name(uid: int, user: dict | None = None) -> str:
    """Отображаемое имя игрока с prefix/suffix эмодзи по его уровню комбо.

    Принимает опциональный уже загруженный user-dict чтобы не делать лишний запрос.
    """
    if user is None:
        user = db.get_user(uid)
    if not user:
        return "?"
    cc      = db.get_combo_count(uid)
    level_n = _combo_level(cc)["level"]
    return _combo_display_name(user, level_n)


def _winner_celebration(uid: int, color_emoji: str, score: int) -> str:
    """Праздничная шапка для победителя с баннером, именем, титулом и личной фразой.

    Используется в финале мультиплеера. Показывает баннер + украшенное имя
    + титул + кастомное сообщение победы — всё ради того, чтобы прокачка
    кастомизации имела смысл. Показ привязан к ФАКТИЧЕСКОМУ наличию слота
    (а не к уровню комбо), т.к. слоты теперь можно открыть и через магазин.
    """
    user = db.get_user(uid)
    if not user:
        return f"👑 *Победитель: {color_emoji} ?* — {fmt(score)} очков"

    cc      = db.get_combo_count(uid)
    level_n = _combo_level(cc)["level"]
    dname   = _combo_display_name(user, level_n)
    parts: list[str] = []

    banner = user.get("combo_banner")
    if banner:
        parts.append(f"_{banner}_")

    parts.append("👑 *ПОБЕДИТЕЛЬ* 👑")
    parts.append(f"{color_emoji} *{dname}*  —  *{fmt(score)}* очков")

    title = user.get("combo_title")
    if title:
        parts.append(f"_{title}_")

    # Личное сообщение победы — кастомная фраза
    win_msg = user.get("combo_win_message")
    if win_msg:
        parts.append(f"💬 _«{win_msg}»_")

    divider_char = user.get("combo_divider") or "━"
    divider = divider_char * 16
    return f"{divider}\n" + "\n".join(parts) + f"\n{divider}"


# ── Achievements catalog ───────────────────────────────────────────────────────

# reward_type: "suffix" | "prefix" | "title" | "banner" | "divider"
ACHIEVEMENTS: dict[str, dict] = {
    # ── Игры ──────────────────────────────────────────────────────────────────
    "games_10":  {"name": "Первые шаги",       "emoji": "👣", "desc": "Сыграть 10 игр",
                  "reward_type": "suffix",  "reward_value": "👣", "cat": "games"},
    "games_50":  {"name": "Завсегдатай",       "emoji": "🎮", "desc": "Сыграть 50 игр",
                  "reward_type": "prefix",  "reward_value": "🎮", "cat": "games"},
    "games_100": {"name": "Ветеран",           "emoji": "🎖", "desc": "Сыграть 100 игр",
                  "reward_type": "title",   "reward_value": "Ветеран",   "cat": "games"},
    "games_200": {"name": "Легенда арены",     "emoji": "🏟", "desc": "Сыграть 200 игр",
                  "reward_type": "banner",  "reward_value": "🏟", "cat": "games"},
    # ── Победы ────────────────────────────────────────────────────────────────
    "first_win":  {"name": "Первая победа",     "emoji": "⭐", "desc": "Выиграть первую игру",
                   "reward_type": "suffix",  "reward_value": "⭐", "cat": "wins"},
    "wins_10":    {"name": "Победитель",        "emoji": "🥇", "desc": "Выиграть 10 игр",
                   "reward_type": "title",   "reward_value": "Победитель", "cat": "wins"},
    "wins_50":    {"name": "Мастер побед",      "emoji": "👑", "desc": "Выиграть 50 игр",
                   "reward_type": "prefix",  "reward_value": "👑", "cat": "wins"},
    "wins_100":   {"name": "Непобедимый",       "emoji": "🏆", "desc": "Выиграть 100 игр",
                   "reward_type": "suffix",  "reward_value": "🏆", "cat": "wins"},
    # ── Серии ─────────────────────────────────────────────────────────────────
    "streak_3":   {"name": "В огне",            "emoji": "🔥", "desc": "3 победы подряд",
                   "reward_type": "suffix",  "reward_value": "🔥", "cat": "streaks"},
    "streak_5":   {"name": "Неудержимый",       "emoji": "⚡", "desc": "5 побед подряд",
                   "reward_type": "prefix",  "reward_value": "⚡", "cat": "streaks"},
    "streak_10":  {"name": "Машина побед",      "emoji": "💥", "desc": "10 побед подряд",
                   "reward_type": "title",   "reward_value": "Машина побед", "cat": "streaks"},
    # ── Цвета — Красный ───────────────────────────────────────────────────────
    "red_10":  {"name": "Красная угроза",   "emoji": "🔴", "desc": "10 побед за Красного",
                "reward_type": "suffix",  "reward_value": "🔴", "cat": "colors"},
    "red_50":  {"name": "Алый король",      "emoji": "♦️", "desc": "50 побед за Красного",
                "reward_type": "prefix",  "reward_value": "♦️", "cat": "colors"},
    "red_100": {"name": "Красный дракон",   "emoji": "🐉", "desc": "100 побед за Красного",
                "reward_type": "title",   "reward_value": "Красный дракон", "cat": "colors"},
    # ── Цвета — Жёлтый ────────────────────────────────────────────────────────
    "yellow_10":  {"name": "Золотая рука",  "emoji": "🟡", "desc": "10 побед за Жёлтого",
                   "reward_type": "suffix",  "reward_value": "🟡", "cat": "colors"},
    "yellow_50":  {"name": "Золотой",        "emoji": "☀️", "desc": "50 побед за Жёлтого",
                   "reward_type": "prefix",  "reward_value": "☀️", "cat": "colors"},
    "yellow_100": {"name": "Золотой век",   "emoji": "👑", "desc": "100 побед за Жёлтого",
                   "reward_type": "title",   "reward_value": "Золотой век", "cat": "colors"},
    # ── Цвета — Синий ─────────────────────────────────────────────────────────
    "blue_10":  {"name": "Водяной знак",    "emoji": "🔵", "desc": "10 побед за Синего",
                 "reward_type": "suffix",  "reward_value": "🔵", "cat": "colors"},
    "blue_50":  {"name": "Синий шторм",     "emoji": "🌊", "desc": "50 побед за Синего",
                 "reward_type": "prefix",  "reward_value": "🌊", "cat": "colors"},
    "blue_100": {"name": "Властелин морей", "emoji": "🐋", "desc": "100 побед за Синего",
                 "reward_type": "title",   "reward_value": "Властелин морей", "cat": "colors"},
    # ── Цвета — Зелёный ───────────────────────────────────────────────────────
    "green_10":  {"name": "Лесной охотник", "emoji": "🟢", "desc": "10 побед за Зелёного",
                  "reward_type": "suffix",  "reward_value": "🟢", "cat": "colors"},
    "green_50":  {"name": "Зелёная сила",   "emoji": "🌿", "desc": "50 побед за Зелёного",
                  "reward_type": "prefix",  "reward_value": "🌿", "cat": "colors"},
    "green_100": {"name": "Дух природы",    "emoji": "🌳", "desc": "100 побед за Зелёного",
                  "reward_type": "title",   "reward_value": "Дух природы", "cat": "colors"},
    # ── Цвета — Фиолетовый ────────────────────────────────────────────────────
    "purple_10":  {"name": "Тёмная лошадка","emoji": "🟣", "desc": "10 побед за Фиолетового",
                   "reward_type": "suffix",  "reward_value": "🟣", "cat": "colors"},
    "purple_50":  {"name": "Теневой лорд",  "emoji": "🌑", "desc": "50 побед за Фиолетового",
                   "reward_type": "prefix",  "reward_value": "🌑", "cat": "colors"},
    "purple_100": {"name": "Повелитель теней","emoji":"🕷","desc":"100 побед за Фиолетового",
                   "reward_type": "title",   "reward_value": "Повелитель теней", "cat": "colors"},
    # ── Цвета — Белый ─────────────────────────────────────────────────────────
    "white_10":  {"name": "Зомбак",      "emoji": "💀", "desc": "10 побед за Белого",
                  "reward_type": "suffix",  "reward_value": "💀", "cat": "colors"},
    "white_50":  {"name": "Вампир",          "emoji": "☠️", "desc": "50 побед за Белого",
                  "reward_type": "prefix",  "reward_value": "☠️", "cat": "colors"},
    "white_100": {"name": "Некромант",         "emoji": "👻", "desc": "100 побед за Белого",
                  "reward_type": "title",   "reward_value": "Некромант", "cat": "colors"},
    # ── Комбо ─────────────────────────────────────────────────────────────────
    "combos_50":   {"name": "Комбо-новичок",  "emoji": "🎲", "desc": "Записать 50 комбо",
                    "reward_type": "suffix",  "reward_value": "🎲", "cat": "combos"},
    "combos_100":  {"name": "Комбо-мастер",   "emoji": "🃏", "desc": "Записать 100 комбо",
                    "reward_type": "prefix",  "reward_value": "🃏", "cat": "combos"},
    "combos_500":  {"name": "Комбо-легенда",  "emoji": "🌟", "desc": "Записать 500 комбо",
                    "reward_type": "title",   "reward_value": "Комбо-легенда", "cat": "combos"},
    "combo_lvl10": {"name": "Знаток",         "emoji": "🏆", "desc": "Достичь уровня комбо 10",
                    "reward_type": "divider", "reward_value": "🏆", "cat": "combos"},
    "combo_lvl19": {"name": "Бог комбо",      "emoji": "🌌", "desc": "Достичь уровня комбо 19",
                    "reward_type": "banner",  "reward_value": "🌌", "cat": "combos"},
    # ── Первый ход ────────────────────────────────────────────────────────────
    "first_move_10": {"name": "Смельчак",     "emoji": "🏃", "desc": "Ходить первым 10 раз",
                      "reward_type": "suffix", "reward_value": "🏃", "cat": "special"},
    "first_move_25": {"name": "Первопроходец","emoji": "🌄", "desc": "Ходить первым 25 раз",
                      "reward_type": "prefix", "reward_value": "🌄", "cat": "special"},
    "first_move_50": {"name": "Инициатор",    "emoji": "⚑",  "desc": "Ходить первым 50 раз",
                      "reward_type": "title",  "reward_value": "Инициатор", "cat": "special"},
    # ── Конкретные комбо ──────────────────────────────────────────────────────
    "combo_straight_10": {"name": "Стритер",      "emoji": "📐", "desc": "Записать стрит 1-6 десять раз",
                          "reward_type": "suffix", "reward_value": "📐", "cat": "combos"},
    "combo_six_1":       {"name": "Один на миллион","emoji": "🌌","desc": "Выбросить шесть одинаковых",
                          "reward_type": "banner", "reward_value": "🎲🌌🎲", "cat": "combos"},
    "combo_ulta_10":     {"name": "Ультимейт",    "emoji": "✨", "desc": "Записать 10 ульт",
                          "reward_type": "suffix", "reward_value": "✨", "cat": "combos"},
    "combo_joker_10":    {"name": "Джокер",       "emoji": "🤹", "desc": "Использовать Джокер 10 раз",
                          "reward_type": "suffix", "reward_value": "🤹", "cat": "combos"},
    # ── Особые ────────────────────────────────────────────────────────────────
    "perfect_game": {"name": "Идеальная игра", "emoji": "💎", "desc": "Набрать ровно цель (100%)",
                     "reward_type": "suffix",  "reward_value": "💎", "cat": "special"},
    "calibrated":   {"name": "Откалиброван",   "emoji": "🎯", "desc": "Завершить калибровку",
                     "reward_type": "suffix",  "reward_value": "🎯", "cat": "special"},
    # ── Расширенный гринд — обычные но грозные ────────────────────────────────
    "games_500":      {"name": "Старожил",         "emoji": "🗿", "desc": "Сыграть 500 игр",
                       "reward_type": "title",   "reward_value": "Старожил",       "cat": "games"},
    "wins_250":       {"name": "Триумфатор",       "emoji": "🎖", "desc": "Выиграть 250 игр",
                       "reward_type": "banner",  "reward_value": "🎖 Триумфатор 🎖", "cat": "wins"},
    "combos_1000":    {"name": "Комбо-император",  "emoji": "♛",  "desc": "Записать 1 000 комбо",
                       "reward_type": "title",   "reward_value": "Комбо-император", "cat": "combos"},
    "combos_2500":    {"name": "Архивариус",       "emoji": "📚", "desc": "Записать 2 500 комбо",
                       "reward_type": "banner",  "reward_value": "📚 Архивариус 📚", "cat": "combos"},
    "first_move_100": {"name": "Авангардист",      "emoji": "🚩", "desc": "Ходить первым 100 раз",
                       "reward_type": "title",   "reward_value": "Авангардист",     "cat": "special"},
    # ── Скрытые (не показываются в списке до получения) ───────────────────────
    "night_owl":      {"name": "Сова",             "emoji": "🦉", "desc": "Сыграть с 02:00 до 04:00",
                       "reward_type": "suffix",  "reward_value": "🌙",
                       "cat": "hidden", "hidden": True},
    "early_bird":     {"name": "Ранняя пташка",    "emoji": "🐦", "desc": "Сыграть с 05:00 до 07:00",
                       "reward_type": "suffix",  "reward_value": "🌅",
                       "cat": "hidden", "hidden": True},
    "weekend_warrior":{"name": "Бой выходного",    "emoji": "🛡", "desc": "Победа в субботу/воскресенье",
                       "reward_type": "prefix",  "reward_value": "🛡",
                       "cat": "hidden", "hidden": True},
    "lucky_seven":    {"name": "Семёрка удачи",    "emoji": "🍀", "desc": "Набрать ровно 7 777 очков",
                       "reward_type": "suffix",  "reward_value": "🍀",
                       "cat": "hidden", "hidden": True},
    "underdog":       {"name": "Чёрный конь",      "emoji": "🐴", "desc": "Победить из последнего места",
                       "reward_type": "title",   "reward_value": "Чёрный конь",
                       "cat": "hidden", "hidden": True},
    "no_first_move":  {"name": "Терпеливый",       "emoji": "🧘", "desc": "Победить ни разу не ходив первым (20 игр)",
                       "reward_type": "suffix",  "reward_value": "🧘",
                       "cat": "hidden", "hidden": True},
    "rainbow":        {"name": "Радуга",           "emoji": "🌈", "desc": "Победить хотя бы по 1 разу всеми 6 цветами",
                       "reward_type": "prefix",  "reward_value": "🌈",
                       "cat": "hidden", "hidden": True},
    "double_calib":   {"name": "Образец точности", "emoji": "🎯", "desc": "Завершить калибровку с 80%+",
                       "reward_type": "title",   "reward_value": "Образец точности",
                       "cat": "hidden", "hidden": True},
    # ── Супер-сложные (легендарная награда) ───────────────────────────────────
    "games_1000":     {"name": "Вечный",           "emoji": "♾", "desc": "Сыграть 1 000 игр",
                       "reward_type": "banner",  "reward_value": "♾ Вечный ♾",
                       "cat": "legendary"},
    "wins_500":       {"name": "Архилегенда",      "emoji": "🌟", "desc": "Выиграть 500 игр",
                       "reward_type": "title",   "reward_value": "Архилегенда",
                       "cat": "legendary"},
    "streak_15":      {"name": "Стихия",           "emoji": "🌪", "desc": "15 побед подряд",
                       "reward_type": "prefix",  "reward_value": "🌪",
                       "cat": "legendary"},
    "all_colors_25":  {"name": "Мастер всех цветов","emoji": "🎨", "desc": "По 25+ побед за каждый из 6 цветов",
                       "reward_type": "divider", "reward_value": "✦",
                       "cat": "legendary"},
    "grandmaster":    {"name": "Грандмастер",      "emoji": "👁", "desc": "Lvl комбо 19 + 250 побед + 500 игр",
                       "reward_type": "banner",  "reward_value": "👁 Грандмастер 👁",
                       "cat": "legendary"},
}

# Маппинг цвет-эмодзи → ключи ачивок
_COLOR_ACH: dict[str, tuple[str, str, str]] = {
    "🔴": ("red_10",    "red_50",    "red_100"),
    "🟡": ("yellow_10", "yellow_50", "yellow_100"),
    "🔵": ("blue_10",   "blue_50",   "blue_100"),
    "🟢": ("green_10",  "green_50",  "green_100"),
    "🟣": ("purple_10", "purple_50", "purple_100"),
    "⚪": ("white_10",  "white_50",  "white_100"),
}


async def _check_achievements(
    uid: int, context, *,
    user: dict | None = None,
    game_score: int | None = None,
    game_target: int | None = None,
    game_place: int | None = None,
    game_total_players: int | None = None,
    game_went_first: bool | None = None,
) -> list[str]:
    """Проверяет и выдаёт новые ачивки. Возвращает список только что заработанных.

    Параметры контекста этой игры (если переданы):
      game_score / game_target  → perfect_game, lucky_seven
      game_place / game_total   → underdog (победил с последнего места)
      game_went_first           → no_first_move (победил не ходя первым)
    """
    if user is None:
        user = db.get_user(uid)
    if not user:
        return []

    earned_before = set(db.get_user_achievements(uid))
    newly_earned:  list[str] = []

    def _try(ach_id: str) -> bool:
        if ach_id in earned_before:
            return False
        if db.grant_achievement(uid, ach_id):
            newly_earned.append(ach_id)
            earned_before.add(ach_id)
            return True
        return False

    games  = user.get("games_played", 0)
    wins   = user.get("wins", 0)

    # Игры сыграны
    if games >= 10:   _try("games_10")
    if games >= 50:   _try("games_50")
    if games >= 100:  _try("games_100")
    if games >= 200:  _try("games_200")

    # Победы
    if wins >= 1:   _try("first_win")
    if wins >= 10:  _try("wins_10")
    if wins >= 50:  _try("wins_50")
    if wins >= 100: _try("wins_100")

    # Серия побед
    streak = db.get_player_streak(uid)
    if streak["type"] == "win":
        if streak["count"] >= 3:  _try("streak_3")
        if streak["count"] >= 5:  _try("streak_5")
        if streak["count"] >= 10: _try("streak_10")

    # Победы по цвету + первый ход (один запрос)
    _pstats    = db.get_player_stats(uid)
    color_wins = _pstats["color_wins"]
    first_count = _pstats["first_count"]
    for emoji, (k10, k50, k100) in _COLOR_ACH.items():
        w = color_wins.get(emoji, 0)
        if w >= 10:  _try(k10)
        if w >= 50:  _try(k50)
        if w >= 100: _try(k100)

    # Первый ход
    if first_count >= 10: _try("first_move_10")
    if first_count >= 25: _try("first_move_25")
    if first_count >= 50: _try("first_move_50")

    # Комбо
    combo_count = db.get_combo_count(uid)
    if combo_count >= 50:  _try("combos_50")
    if combo_count >= 100: _try("combos_100")
    if combo_count >= 500: _try("combos_500")
    combo_lvl = _combo_level(combo_count)["level"]
    if combo_lvl >= 10: _try("combo_lvl10")
    if combo_lvl >= 19: _try("combo_lvl19")

    # Конкретные комбо
    _six_keys  = [f"six_{n}" for n in range(1, 7)]
    _ulta_keys = [k for k in COMBOS if k.startswith("ulta_")]
    # six_1 проверяем всегда — достаточно одного такого комбо
    if db.get_specific_combo_count(uid, _six_keys) >= 1: _try("combo_six_1")
    # Остальные — только если у игрока уже ≥10 комбо всего
    if combo_count >= 10:
        if db.get_specific_combo_count(uid, ["straight_full"]) >= 10: _try("combo_straight_10")
        if db.get_specific_combo_count(uid, _ulta_keys)        >= 10: _try("combo_ulta_10")
        if db.get_specific_combo_count(uid, ["special_joker"]) >= 10: _try("combo_joker_10")

    # Идеальная игра (ровно цель)
    if game_score is not None and game_target and game_score == game_target:
        _try("perfect_game")

    # Калибровка
    if user.get("is_calibrated", False):
        _try("calibrated")

    # ── Расширенный гринд ─────────────────────────────────────────────────────
    if games >= 500:           _try("games_500")
    if games >= 1000:          _try("games_1000")
    if wins  >= 250:           _try("wins_250")
    if wins  >= 500:           _try("wins_500")
    if combo_count >= 1000:    _try("combos_1000")
    if combo_count >= 2500:    _try("combos_2500")
    if first_count >= 100:     _try("first_move_100")
    if streak["type"] == "win" and streak["count"] >= 15:
        _try("streak_15")

    # ── Скрытые ачивки ────────────────────────────────────────────────────────
    # Время суток — проверяем по UTC чтобы не зависеть от tz сервера
    from datetime import datetime, timezone, timedelta
    # Считаем по МСК (UTC+3) — у большинства игроков очевидное "ночь"
    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    hour = now_msk.hour
    if 2 <= hour < 4:  _try("night_owl")
    if 5 <= hour < 7:  _try("early_bird")
    # Выходные: победа в субботу/воскресенье
    if now_msk.weekday() in (5, 6) and game_place == 1:
        _try("weekend_warrior")

    # Lucky seven — ровно 7777 очков (любое место)
    if game_score == 7777:
        _try("lucky_seven")

    # Underdog — победил с последнего места
    if game_place == 1 and game_total_players and game_total_players >= 3:
        # Хотя бы 3 игрока, иначе "последнее" = "проигравший"
        pass  # underdog требует tracking места во время игры — пропускаем пока

    # No first move winner — победа без хода первым (если первого хода 0 за 20+ игр)
    if wins >= 20 and first_count == 0 and game_place == 1:
        _try("no_first_move")

    # Rainbow — победы всеми 6 цветами
    if all(color_wins.get(c, 0) >= 1 for c in ("🔴","🟡","🔵","🟢","🟣","⚪")):
        _try("rainbow")

    # Точная калибровка — closed at 80%+ avg perf
    if user.get("is_calibrated", False):
        perf_sum   = user.get("calibration_perf_sum", 0)
        perf_count = user.get("calibration_games_count", 0)
        if perf_count > 0 and (perf_sum / perf_count) >= 0.80:
            _try("double_calib")

    # ── Супер-сложные ─────────────────────────────────────────────────────────
    # Все цвета по 25+
    if all(color_wins.get(c, 0) >= 25 for c in ("🔴","🟡","🔵","🟢","🟣","⚪")):
        _try("all_colors_25")

    # Грандмастер: lvl 19 + 250 побед + 500 игр
    if combo_lvl >= 19 and wins >= 250 and games >= 500:
        _try("grandmaster")

    # Уведомление о новых ачивках
    if newly_earned:
        lines = ["🏅 *Новые достижения!*\n"]
        for ach_id in newly_earned:
            a = ACHIEVEMENTS.get(ach_id, {})
            rtype = a.get("reward_type", "")
            rval  = a.get("reward_value", "")
            reward_str = {
                "prefix":      f"prefix-эмодзи `{rval}`",
                "suffix":      f"suffix-эмодзи `{rval}`",
                "title":       f"титул «{rval}»",
                "banner":      f"баннер `{rval}`",
                "divider":     f"разделитель `{rval}`",
                "color_frame": f"цветная рамка `{rval}`",
                "bank_fx":     f"эффект банка `{rval}`",
                "win_message": f"слова победы «{rval}»",
            }.get(rtype, rval)
            lines.append(f"{a.get('emoji','🏅')} *{a.get('name',ach_id)}*")
            lines.append(f"  _{a.get('desc','')}_")
            lines.append(f"  🎁 Награда: {reward_str}")
        lines.append("\n_Открой Профиль → Кастомизация чтобы надеть._")
        try:
            await context.bot.send_message(chat_id=uid, text="\n".join(lines), parse_mode="Markdown")
        except Exception:
            pass

    return newly_earned


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    if user_id in config.SUPERADMIN_IDS:
        return True
    user = db.get_user(user_id)
    return user is not None and bool(user["is_admin"])


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def place_emoji(place: int, total: int) -> str:
    if place == 1: return "🥇"
    if place == 2: return "🥈"
    if place == 3: return "🥉"
    if place == total and total > 3: return "💀"
    return MEDALS[place - 1] if place <= len(MEDALS) else f"{place}."


def rating_change(place: int, total: int) -> int:
    """Устаревшая формула — оставлена для совместимости с ручной записью без счёта."""
    if place == 1:     return 30
    if place == total: return -15
    if place == 2:     return 15
    return 0


# Диапазоны изменения рейтинга: (min_при_0%, max_при_100%) для каждого места.
# Счёт/цель (0.0–1.0) линейно интерполирует между min и max.
#
# Правила:
#   • 1-е место — всегда ≥ +2 (победитель не теряет рейтинг)
#   • 2-е место в игре на 3+ — всегда ≥ +2 (они не проиграли)
#   • 2-е место в игре на 2 — может быть отрицательным (это последнее место)
#   • Остальные позиции могут уходить в минус в зависимости от счёта
_RATING_RANGES: dict[int, list[tuple[int, int]]] = {
    2: [(+2, 35), (-22, -2)],                          # 2-й = последний → всегда минус
    3: [(+5, 35), (+2, 12), (-22, -8)],                # 1-й и 2-й ≥ +2
    4: [(+8, 35), (+2, 18), (-14, +2), (-22, -10)],    # 3-й может быть чуть в плюсе
    5: [(+8, 35), (+2, 15), (-8,  +5), (-18, -3), (-22, -12)],
    6: [(+8, 35), (+2, 15), (-2, +10), (-10, +2), (-18, -5), (-22, -12)],
}


def rating_change_v2(place: int, total: int, score: int, target: int) -> int:
    """Рейтинговое изменение через диапазон [min, max] для каждого места.

    score / target (0.0–1.0+) линейно маппится на диапазон:
      0%  → min_change   100%+ → max_change
    Таким образом каждое место — даже 2-е из 3-х — может дать как плюс, так и минус
    в зависимости от того, насколько много очков набрал игрок.
    """
    ranges = _RATING_RANGES.get(min(total, 6), _RATING_RANGES[6])
    min_c, max_c = ranges[min(place - 1, len(ranges) - 1)]
    r = min((score / target) if target > 0 else 0.0, 1.0)
    return round(min_c + (max_c - min_c) * r)


def _thief_steal(face: int, num_players: int) -> int:
    """Сумма кражи Вора: base {4:150,5:200,6:250} ±50 за каждого игрока ≠ 3, мин — как при 4+."""
    base = {4: 150, 5: 200, 6: 250}.get(face, 0)
    if base == 0:
        return 0
    bonus = (3 - min(num_players, 4)) * 50   # +50 при 2, 0 при 3, -50 при 4+
    return base + bonus


def _robbery_bonus(num_players: int) -> int:
    """Очки победителя Разбоя за каждого побеждённого."""
    return {2: 500, 3: 400}.get(min(num_players, 3), 300)


def crossed_thresholds(old: int, new: int, target: int, num_players: int = 3) -> list:
    """Возвращает рубежи магазина, пересечённые за этот банк."""
    by_count = SHOP_THRESHOLDS.get(target, {})
    if not by_count:
        return []
    # Выбираем ключ: первый >= num_players, иначе максимальный (меньше рубежей)
    keys = sorted(by_count.keys())
    chosen = keys[-1]
    for k in keys:
        if k >= num_players:
            chosen = k
            break
    return [t for t in by_count[chosen] if old < t <= new]


def _admin_id_from(cb: str) -> int:
    """Извлекает admin_id из конца callback_data: 'gend_123' → 123."""
    return int(cb.rsplit("_", 1)[1])


# ── Keyboards ─────────────────────────────────────────────────────────────────

def main_kb(admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Рейтинговая таблица", callback_data="show_ratings")],
        [InlineKeyboardButton("👤 Мой профиль",          callback_data="show_profile")],
        [InlineKeyboardButton("🎯 Сингл-плеер",          callback_data="solo_menu")],
        [InlineKeyboardButton("🛒 Магазин",              callback_data="shop_menu"),
         InlineKeyboardButton("🎰 Казино",               callback_data="casino_menu")],
        [InlineKeyboardButton("🏛 Зал Богатства",        callback_data="hall_menu")],
    ]
    if admin:
        rows.append([InlineKeyboardButton("⚙️ Админ панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def reply_kb(admin: bool = False) -> ReplyKeyboardMarkup:
    """Постоянная нижняя клавиатура."""
    base = [
        [KeyboardButton("📊 Рейтинг"), KeyboardButton("👤 Профиль")],
    ]
    if admin:
        base.append([KeyboardButton("⚙️ Админ"), KeyboardButton("🎮 Новая игра")])
    return ReplyKeyboardMarkup(base, resize_keyboard=True)


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Записать игру",          callback_data="admin_record_game")],
        [InlineKeyboardButton("🔧 Восстановить финал",     callback_data="admin_recover_game")],
        [InlineKeyboardButton("🔑 Управление админами",    callback_data="admin_manage_admins")],
        [InlineKeyboardButton("✏️ Изменить рейтинг",      callback_data="admin_edit_rating")],
        [InlineKeyboardButton("🗑️ Удалить игрока",        callback_data="admin_delete_player")],
        [InlineKeyboardButton("⚠️ Сброс статистики",      callback_data="admin_reset_menu")],
        [InlineKeyboardButton("◀️ Назад",                 callback_data="main_menu")],
    ])


def reset_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Только рейтинги → 0",         callback_data="reset_ratings_only")],
        [InlineKeyboardButton("🎮 Только игры/победы → 0",      callback_data="reset_games_only")],
        [InlineKeyboardButton("🎲 Только комбо-логи",            callback_data="reset_combos_only")],
        [InlineKeyboardButton("💀 Сбросить всё",                callback_data="admin_reset_confirm")],
        [InlineKeyboardButton("◀️ Назад в админку",             callback_data="admin_panel")],
    ])


def back_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад в админку", callback_data="admin_panel")]])


def back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="main_menu")]])


def player_selection_kb(all_users: list, selected_ids: list) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for u in all_users:
        sel   = u["user_id"] in selected_ids
        label = f"✅ {u['username']}" if sel else u["username"]
        row.append(InlineKeyboardButton(label, callback_data=f"sel_{u['user_id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    n = len(selected_ids)
    if n >= 6:
        buttons.append([InlineKeyboardButton("⚠️ Максимум 6 игроков", callback_data="noop")])
    elif n >= 2:
        buttons.append([InlineKeyboardButton(f"▶️ Начать — {n} игроков", callback_data="start_scoring")])
    elif n == 1:
        buttons.append([InlineKeyboardButton("Выберите ещё минимум 1 игрока", callback_data="noop")])
    else:
        buttons.append([InlineKeyboardButton("Выберите минимум 2 игроков", callback_data="noop")])

    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_admin")])
    return InlineKeyboardMarkup(buttons)


# ── Game display helpers ───────────────────────────────────────────────────────

def game_board_text(players: list, current_idx: int, target: int, turn_score: int) -> str:
    total   = len(players)
    current = players[current_idx]
    color   = current.get("color_emoji", "")
    sorted_players = sorted(enumerate(players), key=lambda x: x[1]["total"], reverse=True)

    lines = [f"🎮 *Игра до {fmt(target)}*\n"]
    lines.append(f"🎯 Ход: {color} *{current.get('display_name', current['username'])}*")
    lines.append(f"➕ Накоплено за ход: *{fmt(turn_score) if turn_score else '—'}*")
    lines.append("")
    lines.append("─────────────────────")
    for rank, (orig_idx, p) in enumerate(sorted_players, 1):
        arrow = " ◀" if orig_idx == current_idx else ""
        pct   = min(int(p["total"] / target * 100), 100)
        col   = p.get("color_emoji", "")
        dname = p.get("display_name", p["username"])
        lines.append(
            f"{place_emoji(rank, total)} {col} *{dname}*{arrow}"
            f"  —  {fmt(p['total'])} / {fmt(target)} ({pct}%)"
        )
    lines.append("─────────────────────")
    return "\n".join(lines)


def board_kb(admin_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏁 Завершить игру (адм.)", callback_data=f"gfinish_{admin_id}"),
    ]])


def combo_cat_kb(admin_id: int) -> InlineKeyboardMarkup:
    """Главная категория выбора комбинаций."""
    aid = admin_id
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📏 Стриты",              callback_data=f"csub_straight_{aid}")],
        [
            InlineKeyboardButton("×3",  callback_data=f"csub_three_{aid}"),
            InlineKeyboardButton("×4",  callback_data=f"csub_four_{aid}"),
            InlineKeyboardButton("×5",  callback_data=f"csub_five_{aid}"),
            InlineKeyboardButton("×6",  callback_data=f"csub_six_{aid}"),
        ],
        [InlineKeyboardButton("⭐ 🟡 4-5-6",            callback_data=f"clog_special_yellow_456_{aid}")],
        [InlineKeyboardButton("🔥 Ульта",               callback_data=f"csub_ulta_{aid}")],
        [InlineKeyboardButton("🎲 Спец кубики",          callback_data=f"csub_specials_{aid}")],
        [InlineKeyboardButton("◀ К ходу",               callback_data=f"cclose_{aid}")],
    ])


def combo_sub_kb(sub: str, admin_id: int) -> InlineKeyboardMarkup:
    """Подкатегория комбинаций."""
    aid  = admin_id
    rows = []

    if sub == "straight":
        rows = [
            [InlineKeyboardButton("1-2-3-4-5-6", callback_data=f"clog_straight_full_{aid}")],
            [
                InlineKeyboardButton("1-2-3-4-5",  callback_data=f"clog_straight_low_{aid}"),
                InlineKeyboardButton("2-3-4-5-6",  callback_data=f"clog_straight_high_{aid}"),
            ],
        ]
    elif sub in SUB_MULT:
        mult = SUB_MULT[sub]
        row  = []
        for n in range(1, 7):
            row.append(InlineKeyboardButton(
                f"{DICE_N[n]}×{mult}", callback_data=f"clog_{sub}_{n}_{aid}",
            ))
            if len(row) == 3:
                rows.append(row); row = []
        if row:
            rows.append(row)
    elif sub == "ulta":
        rows = [
            [
                InlineKeyboardButton("🔴 ×2 пятёрки",   callback_data=f"clog_ulta_red_{aid}"),
                InlineKeyboardButton("🔵 ×2 двойки",    callback_data=f"clog_ulta_blue_{aid}"),
            ],
            [
                InlineKeyboardButton("🟢 ×2 тройки",    callback_data=f"clog_ulta_green_{aid}"),
                InlineKeyboardButton("⚪ ×2 единицы",   callback_data=f"clog_ulta_white_{aid}"),
            ],
            [
                InlineKeyboardButton("🟣 ×2 шестёрки",  callback_data=f"clog_ulta_purple_{aid}"),
            ],
            [
                InlineKeyboardButton("🟡 Стрит 1-6",    callback_data=f"clog_ulta_yellow_full_{aid}"),
                InlineKeyboardButton("🟡 1-2-3",         callback_data=f"clog_ulta_yellow_low_{aid}"),
            ],
        ]

    elif sub == "specials":
        rows = [
            [
                InlineKeyboardButton("🃏 Джокер",    callback_data=f"clog_special_joker_{aid}"),
                InlineKeyboardButton("🥷 Вор",        callback_data=f"clog_special_thief_{aid}"),
            ],
            [
                InlineKeyboardButton("🔄 Рерол",     callback_data=f"clog_special_reroll_{aid}"),
                InlineKeyboardButton("⚔️ Разбой",    callback_data=f"clog_special_robbery_{aid}"),
            ],
            [
                InlineKeyboardButton("💪 Рубен",     callback_data=f"clog_special_ruben_{aid}"),
                InlineKeyboardButton("✖️ Утроение",  callback_data=f"clog_special_triple_{aid}"),
            ],
            [
                InlineKeyboardButton("✝️ Крест",     callback_data=f"clog_special_cross_{aid}"),
                InlineKeyboardButton("❄️ Заморозка", callback_data=f"clog_special_freeze_{aid}"),
            ],
        ]

    rows.append([InlineKeyboardButton("◀ Назад", callback_data=f"ccat_{aid}")])
    return InlineKeyboardMarkup(rows)


def turn_message_text(
    player: dict,
    target: int,
    turn_score: int,
    turn_crosses: int = 0,
    effects: dict | None = None,
    all_players: list | None = None,
) -> str:
    color      = player.get("color_emoji", "")
    color_name = player.get("color_name", "")
    pname_my   = player.get("display_name") or player["username"]
    lines = [f"🎯 *Твой ход, {pname_my}!*  {color} {color_name}"]

    # ── Таблица всех игроков ────────────────────────────────────────────────────
    if all_players and len(all_players) > 1:
        total_p = len(all_players)
        lines.append("─────────────────────")
        for rank, p in enumerate(sorted(all_players, key=lambda x: x["total"], reverse=True), 1):
            arrow = " ◀" if p["user_id"] == player["user_id"] else ""
            pct_p = min(int(p["total"] / target * 100), 100)
            dname_p = p.get("display_name") or p["username"]
            lines.append(
                f"{place_emoji(rank, total_p)} {p.get('color_emoji','')} *{dname_p}*{arrow}"
                f"  {fmt(p['total'])} ({pct_p}%)"
            )
        lines.append("─────────────────────")
    else:
        pct = min(int(player["total"] / target * 100), 100)
        lines.append(f"📊 Счёт: *{fmt(player['total'])}* / {fmt(target)}  ({pct}%)")

    lines.append(f"➕ Накоплено за ход: *{fmt(turn_score) if turn_score else '—'}*")

    # Эффекты с прошлого хода (показываются один раз в начале хода)
    if effects:
        c = effects.get("crosses", 0)
        f = effects.get("frozen", 0)
        if c:
            lines.append(f"✝️ *+{c} кубик{'а' if 2 <= c <= 4 else ''}* с прошлого хода!")
        if f:
            lines.append(f"❄️ *-{f} кубик{'а' if 2 <= f <= 4 else ''}* (заморожен этот ход)")
    # Кресты за текущий ход
    if turn_crosses:
        lines.append(f"✝️ Крестов этим ходом: *{turn_crosses}* (бонус перейдёт дальше)")
    lines += ["", f"🎯 Цель: *{fmt(target)}*", "_Введите число (кратно 50) или нажмите кнопку:_"]
    return "\n".join(lines)


def _white_tokens(player: dict) -> int | None:
    """Возвращает кол-во жетонов если игрок белый, иначе None."""
    if player.get("color_emoji") == "⚪":
        return player.get("tokens", 0)
    return None


def turn_message_kb(
    turn_score: int,
    admin_id: int,
    can_undo: bool = False,
    turn_crosses: int = 0,
    num_players: int = 2,
    white_tokens: int | None = None,
    win_message: str | None = None,
) -> InlineKeyboardMarkup:
    rows = []

    # ── Ряд 1-2: быстрые очки (+50…+1000) ────────────────────────────────────
    quick = [50, 100, 150, 200, 300, 500, 750, 1000]
    for i in range(0, len(quick), 4):
        rows.append([
            InlineKeyboardButton(f"+{fmt(v)}", callback_data=f"gadd_{v}_{admin_id}")
            for v in quick[i:i+4]
        ])

    # ── Ряд 3: +1500 + комбо + таунт + отмена ────────────────────────────────
    misc_row = [InlineKeyboardButton("+1 500", callback_data=f"gadd_1500_{admin_id}")]
    misc_row.append(InlineKeyboardButton("📝 Комбо", callback_data=f"ccat_{admin_id}"))
    if win_message:
        misc_row.append(InlineKeyboardButton("💬 Таунт", callback_data=f"gtaunt_{admin_id}"))
    if can_undo:
        misc_row.append(InlineKeyboardButton("↩️ Отменить", callback_data=f"gundo_{admin_id}"))
    rows.append(misc_row)

    # ── Ряд 4: завершить ход / потерять ход / эффекты ────────────────────────
    end_label = f"✅ +{fmt(turn_score)}" if turn_score else "✅ Завершить ход"
    effects_label = f"⚔️ Эффекты" + (f" ✝️{turn_crosses}" if turn_crosses else "")
    rows.append([
        InlineKeyboardButton(end_label,       callback_data=f"gend_{admin_id}"),
        InlineKeyboardButton("💀 Ничего",     callback_data=f"glose_{admin_id}"),
        InlineKeyboardButton(effects_label,   callback_data=f"gfx_menu_{admin_id}"),
    ])

    # ── Ряд 5: жетоны белого (только для ⚪) ──────────────────────────────────
    if white_tokens is not None:
        tok_row = []
        if white_tokens < 2:
            add_label = f"⚪ +жетон ({white_tokens}/2)"
            tok_row.append(InlineKeyboardButton(add_label, callback_data=f"gtoken_{admin_id}"))
        else:
            tok_row.append(InlineKeyboardButton("⚪ Жетон (2/2 макс)", callback_data="noop"))
        if white_tokens > 0:
            use_label = f"🔘 Использовать жетон ({white_tokens})"
            tok_row.append(InlineKeyboardButton(use_label, callback_data=f"gusetoken_{admin_id}"))
        rows.append(tok_row)

    # ── Ряд 6: служебные ──────────────────────────────────────────────────────
    rows.append([
        InlineKeyboardButton("🏁 Закончить игру", callback_data=f"gfinish_{admin_id}"),
        InlineKeyboardButton("📋 Обновить",       callback_data=f"grefresh_{admin_id}"),
        InlineKeyboardButton("⚙️ Правка",         callback_data=f"gadm_{admin_id}"),
    ])
    return InlineKeyboardMarkup(rows)


# ── /start /cancel ────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user    = db.get_user(user_id)
    if user:
        if user_id in config.SUPERADMIN_IDS and not user["is_admin"]:
            db.set_admin(user_id, True)
        db.clear_pending_action(user_id)
        adm   = is_admin(user_id)
        dname = _user_display_name(user_id, user=user)
        await update.message.reply_text(
            f"👋 С возвращением, *{dname}*!",
            parse_mode="Markdown",
            reply_markup=reply_kb(adm),
        )
        await update.message.reply_text(
            "Выбери действие:", reply_markup=main_kb(adm),
        )
    else:
        db.set_pending_action(user_id, "registering")
        await update.message.reply_text(
            "👋 Добро пожаловать!\n\nВведите ваш никнейм (2–30 символов):"
        )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pending = db.get_pending_action(user_id)
    # Если шла игра — чистим my_turn текущего игрока
    if pending and pending["action"] == "in_game":
        tp = pending["data"].get("turn_player_id")
        if tp and tp != user_id:
            db.clear_pending_action(tp)
    db.clear_pending_action(user_id)
    kb = back_admin_kb() if is_admin(user_id) else back_main_kb()
    await update.message.reply_text("❌ Действие отменено.", reply_markup=kb)


# ── Message dispatcher ────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = update.message.text.strip()
    user    = db.get_user(user_id)
    pending = db.get_pending_action(user_id)

    # ── Reply keyboard shortcuts (работают всегда для зарегистрированных) ──────
    if user is not None and text in ("📊 Рейтинг", "👤 Профиль", "⚙️ Админ", "🎮 Новая игра"):
        if text == "📊 Рейтинг":
            await cmd_ratings(update, context)
        elif text == "👤 Профиль":
            await cmd_profile(update, context)
        elif text == "⚙️ Админ":
            await cmd_admin(update, context)
        elif text == "🎮 Новая игра":
            await cmd_game(update, context)
        return

    if user is None:
        if pending and pending["action"] == "registering":
            await _do_register(update, user_id, text)
        else:
            await update.message.reply_text("Нажмите /start для начала.")
        return

    if not pending:
        return

    action = pending["action"]
    data   = pending.get("data") or {}

    if action == "my_turn":
        await _handle_game_input(update, context, user_id, text, data)
    elif action == "in_game":
        # Возможно, сам админ — текущий игрок
        players     = data.get("players", [])
        current_idx = data.get("current_idx", 0)
        if players and players[current_idx].get("user_id") == user_id:
            await _handle_game_input(update, context, user_id, text, {"game_admin_id": user_id})
    elif action == "selecting_players":
        await update.message.reply_text("Выберите игроков кнопками выше ⬆️")
    elif action == "selecting_mode":
        await update.message.reply_text("Выберите режим игры кнопками выше ⬆️")
    elif action == "compare_waiting":
        await _do_compare(update, user_id, text)
    elif action == "solo_game":
        await update.message.reply_text("🎯 Идёт сольная игра — используй кнопки ⬆️")
    elif action == "edit_rating_name":
        await _handle_edit_name(update, user_id, text)
    elif action == "edit_rating_value":
        await _handle_edit_value(update, user_id, text, data)
    elif action == "delete_player":
        await _handle_delete_name(update, user_id, text)
    elif action == "dbg_shop_edit":
        await _handle_dbg_shop_edit(update, user_id, text, data); return
    elif action == "dbg_shop_add":
        await _handle_dbg_shop_add(update, user_id, text, data); return
    elif action == "transfer_search":
        # Поиск игрока по @username
        username = text.lstrip("@").strip()
        target   = await adb(db.get_user_by_username, username)
        if not target:
            await update.message.reply_text(
                f"❌ Игрок *{username}* не найден. Попробуй ещё раз или отмени:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data="transfer_start"),
                ]]),
            )
            return
        await adb(db.clear_pending_action, user_id)
        coins       = await adb(db.get_coins, user_id)
        target_name = target.get("username", "?")
        target_uid  = target["user_id"]
        if target_uid == user_id:
            await update.message.reply_text("❌ Нельзя переводить самому себе.")
            return
        await update.message.reply_text(
            f"💸 *ПЕРЕВОД → {target_name}*\n\n"
            f"💼 Твой баланс: *{fmt(coins)} 💰*\n\n"
            f"Выбери сумму:",
            parse_mode="Markdown",
            reply_markup=_transfer_amount_kb(target_uid, coins),
        )

    elif action == "transfer_custom":
        # Произвольная сумма перевода
        target_uid = data.get("target_uid")
        if not target_uid:
            await adb(db.clear_pending_action, user_id)
            return
        try:
            amount = int(text.replace(" ", "").replace(",", "").replace(".", ""))
        except ValueError:
            await update.message.reply_text("❌ Введи целое число (например: 50000)")
            return
        if amount <= 0:
            await update.message.reply_text("❌ Сумма должна быть больше нуля.")
            return
        coins = await adb(db.get_coins, user_id)
        if coins < amount:
            await update.message.reply_text(
                f"❌ Недостаточно монет. Баланс: *{fmt(coins)} 💰*",
                parse_mode="Markdown",
            )
            return
        target      = await adb(db.get_user, target_uid)
        target_name = target.get("username", "?") if target else "?"
        await adb(db.clear_pending_action, user_id)
        await update.message.reply_text(
            f"💸 *ПОДТВЕРЖДЕНИЕ ПЕРЕВОДА*\n\n"
            f"Получатель: *{target_name}*\n"
            f"Сумма: *{fmt(amount)} 💰*\n\n"
            f"Твой баланс сейчас: *{fmt(coins)} 💰*\n"
            f"После перевода: *{fmt(coins - amount)} 💰*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"transfer_ok_{target_uid}_{amount}"),
                InlineKeyboardButton("❌ Отмена",      callback_data=f"transfer_to_{target_uid}"),
            ]]),
        )

    elif action == "hall_plaque_msg":
        # Подпись для именной таблички
        tier  = data.get("tier")
        price = data.get("price", 0)
        cfg   = _HALL_PLAQUE_TIERS.get(tier)
        if not cfg:
            await adb(db.clear_pending_action, user_id)
            return
        message = "" if text.strip() == "." else text.strip()[:60]
        await adb(db.clear_pending_action, user_id)
        ok, err = await adb(db.buy_hall_plaque, user_id, tier, message, price)
        if ok:
            coins   = await adb(db.get_coins, user_id)
            preview = f" «{message}»" if message else ""
            await update.message.reply_text(
                f"🏛 *Табличка установлена!*\n\n"
                f"{cfg['emoji']} *{cfg['label']}*{preview}\n\n"
                f"Твоё имя навсегда в Зале Богатства!\n"
                f"💰 Остаток: *{fmt(coins)}* монет",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏛 Зал Богатства", callback_data="hall_menu"),
                    InlineKeyboardButton("🏛 Таблички",      callback_data="hall_plaques"),
                ]]),
            )
        else:
            await update.message.reply_text(f"❌ {err}")

    elif action == "exchange_cube_to_fut":
        # Ввод суммы для конвертации CUB→FUT
        try:
            amount = int(text.replace(" ", "").replace(",", "").replace(".", ""))
        except ValueError:
            await update.message.reply_text("❌ Введи целое число (например: 500)")
            return
        if amount <= 0:
            await update.message.reply_text("❌ Сумма должна быть больше нуля.")
            return
        coins = await adb(db.get_coins, user_id)
        if coins < amount:
            await update.message.reply_text(
                f"❌ Недостаточно монет. Баланс: *{fmt(coins)} CUB*",
                parse_mode="Markdown",
            )
            return
        out, comm = db._cross_calc(amount, "cube_to_fut")
        await adb(db.clear_pending_action, user_id)
        await update.message.reply_text(
            f"💎 *ПОДТВЕРЖДЕНИЕ КОНВЕРТАЦИИ*\n\n"
            f"Потратишь: *{fmt(amount)} CUB*\n"
            f"Получишь: *{fmt(out)} FUT*\n"
            f"Комиссия (сжигается): *{fmt(comm)} FUT*\n\n"
            f"Твой баланс сейчас: *{fmt(coins)} CUB*\n"
            f"После: *{fmt(coins - amount)} CUB*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"exchange_confirm_{amount}"),
                InlineKeyboardButton("❌ Отмена",      callback_data="exchange_menu"),
            ]]),
        )

    elif action == "combo_edit":
        # Только слот "about" принимает текстовый ввод
        slot  = data.get("slot", "")
        if slot != "about":
            return
        limit = 80
        value = text.strip() if text.strip() != "-" else ""
        if value and len(value) > limit:
            await update.message.reply_text(f"❌ Слишком длинно (макс. {limit} символов).")
            return
        db.update_combo_customization(user_id, **{"combo_about": value or None})
        db.clear_pending_action(user_id)
        await update.message.reply_text(
            f"✅ *О себе* обновлено!",
            parse_mode="Markdown",
        )


# ── Registration ──────────────────────────────────────────────────────────────

async def _do_register(update: Update, user_id: int, username: str):
    if not 2 <= len(username) <= 30:
        await update.message.reply_text("Никнейм должен быть от 2 до 30 символов:")
        return
    # Запрещаем wildcard-символы (защита от подмены через ILIKE) и управляющие
    if any(ch in username for ch in ("%", "_", "\\", "*")):
        await update.message.reply_text(
            "Никнейм не должен содержать символы: % _ \\ *"
        )
        return
    if any(ord(ch) < 32 for ch in username):
        await update.message.reply_text("Никнейм содержит недопустимые символы.")
        return
    if db.username_exists(username):
        await update.message.reply_text("Этот никнейм уже занят. Выберите другой:")
        return
    is_super = user_id in config.SUPERADMIN_IDS
    db.create_user(user_id, username, is_admin=is_super)
    db.clear_pending_action(user_id)
    await update.message.reply_text(
        f"✅ Готово! Ваш никнейм: *{username}*",
        parse_mode="Markdown",
        reply_markup=reply_kb(is_super),
    )
    await update.message.reply_text(
        "Выбери действие:", reply_markup=main_kb(is_super),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПИСЬ ИГРЫ — живой трекер (шаги 1–3)
# ══════════════════════════════════════════════════════════════════════════════

async def cb_admin_record_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("⛔ Нет доступа", show_alert=True); return

    all_users = db.get_all_users()
    if len(all_users) < 2:
        await query.edit_message_text("❌ В системе меньше 2 игроков.", reply_markup=back_admin_kb()); return

    db.set_pending_action(user_id, "selecting_players", {"selected": []})
    await query.edit_message_text(
        "🎮 *Запись игры*\n\nВыберите участников (2–6 человек):",
        parse_mode="Markdown", reply_markup=player_selection_kb(all_users, []),
    )


async def cb_select_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer(); return

    target_id = int(query.data.split("_", 1)[1])
    pending   = db.get_pending_action(user_id)
    if not pending or pending["action"] != "selecting_players":
        await query.answer(); return

    selected = list(pending["data"].get("selected", []))
    if target_id in selected:
        selected.remove(target_id)
    else:
        if len(selected) >= 6:
            await query.answer("Максимум 6 игроков!", show_alert=True); return
        selected.append(target_id)

    db.set_pending_action(user_id, "selecting_players", {"selected": selected})
    await query.answer()
    await query.edit_message_text(
        "🎮 *Запись игры*\n\nВыберите участников (2–6 человек):",
        parse_mode="Markdown",
        reply_markup=player_selection_kb(db.get_all_users(), selected),
    )


async def cb_start_scoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    pending      = db.get_pending_action(user_id)
    if not pending or pending["action"] != "selecting_players": return
    selected_ids = list(pending["data"].get("selected", []))
    if len(selected_ids) < 2:
        await query.answer("Нужно минимум 2 игрока!", show_alert=True); return

    all_users = {u["user_id"]: u for u in db.get_all_users()}
    players   = []
    for uid in selected_ids:
        if uid not in all_users:
            continue
        u    = all_users[uid]
        dname = _user_display_name(uid, user=u)
        players.append({"user_id": uid, "username": u["username"],
                         "display_name": dname, "total": 0})
    db.set_pending_action(user_id, "selecting_mode", {"players": players})
    await query.edit_message_text(
        "🎯 *Выберите режим игры:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎯 до 20 000", callback_data="gmode_20000"),
                InlineKeyboardButton("🎯 до 25 000", callback_data="gmode_25000"),
            ],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_admin")],
        ]),
    )


async def cb_select_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор режима (20k/25k) → переходим к ручному выбору цветов."""
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    pending = db.get_pending_action(user_id)
    if not pending or pending["action"] != "selecting_mode": return

    target  = int(query.data.split("_")[1])
    players = pending["data"]["players"]

    db.set_pending_action(user_id, "assigning_colors", {
        "players":      players,
        "target":       target,
        "color_assign": {},   # {str(idx): {"emoji":…,"name":…}}
    })
    await query.edit_message_text(
        _color_assign_text(players, {}),
        parse_mode="Markdown",
        reply_markup=_color_assign_kb(players, {}, user_id),
    )


def _color_assign_text(players: list, color_assign: dict) -> str:
    lines = ["🎨 *Выберите цвета игроков*\n"]
    for i, p in enumerate(players):
        a = color_assign.get(str(i))
        mark = f"{a['emoji']} {a['name']}" if a else "❓ не выбран"
        lines.append(f"{i+1}. *{p['username']}* — {mark}")
    lines.append("\n_Нажмите цвет, чтобы назначить следующему без цвета_")
    return "\n".join(lines)


def _color_assign_kb(players: list, color_assign: dict, admin_id: int) -> InlineKeyboardMarkup:
    used = {v["emoji"] for v in color_assign.values()}
    rows = []
    # Ряд доступных цветов
    color_row = []
    for i, (emoji, _name) in enumerate(COLORS):
        if emoji not in used:
            color_row.append(InlineKeyboardButton(emoji, callback_data=f"gcol_{i}_{admin_id}"))
    if color_row:
        rows.append(color_row)
    # Кнопка случайного назначения оставшихся
    unassigned = [i for i in range(len(players)) if str(i) not in color_assign]
    if unassigned:
        rows.append([InlineKeyboardButton("🎲 Случайные оставшиеся", callback_data=f"gcolrnd_{admin_id}")])
    # Если все назначены → переходим к выбору первого
    if len(color_assign) == len(players):
        rows.append([InlineKeyboardButton("▶️ Кто ходит первым?", callback_data=f"gfirstshow_{admin_id}")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_admin")])
    return InlineKeyboardMarkup(rows)


async def cb_assign_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gcol_<color_idx>_<admin_id> — назначает цвет следующему без цвета игроку."""
    query    = update.callback_query
    user_id  = query.from_user.id
    parts    = query.data.split("_")   # gcol_<cidx>_<admin_id>
    color_idx = int(parts[1])
    admin_id  = int(parts[2])
    if user_id != admin_id:
        await query.answer("Только администратор игры.", show_alert=True); return

    pending = db.get_pending_action(user_id)
    if not pending or pending["action"] != "assigning_colors":
        await query.answer(); return

    players      = pending["data"]["players"]
    target       = pending["data"]["target"]
    color_assign = pending["data"].get("color_assign", {})

    # Найти первого без цвета
    first_free = next((i for i in range(len(players)) if str(i) not in color_assign), None)
    if first_free is None:
        await query.answer("Все цвета уже назначены."); return

    emoji, name = COLORS[color_idx]
    if any(v["emoji"] == emoji for v in color_assign.values()):
        await query.answer("Этот цвет уже занят.", show_alert=True); return

    color_assign[str(first_free)] = {"emoji": emoji, "name": name}
    pending["data"]["color_assign"] = color_assign
    db.set_pending_action(user_id, "assigning_colors", pending["data"])

    await query.answer(f"{emoji} → {players[first_free]['username']}")
    await query.edit_message_text(
        _color_assign_text(players, color_assign),
        parse_mode="Markdown",
        reply_markup=_color_assign_kb(players, color_assign, admin_id),
    )


async def cb_assign_color_random(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gcolrnd_<admin_id> — случайно назначает цвета всем оставшимся."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)
    if user_id != admin_id:
        await query.answer(); return

    pending = db.get_pending_action(user_id)
    if not pending or pending["action"] != "assigning_colors":
        await query.answer(); return

    players      = pending["data"]["players"]
    target       = pending["data"]["target"]
    color_assign = pending["data"].get("color_assign", {})

    used   = {v["emoji"] for v in color_assign.values()}
    avail  = [(i, e, n) for i, (e, n) in enumerate(COLORS) if e not in used]
    free   = [i for i in range(len(players)) if str(i) not in color_assign]
    random.shuffle(avail)
    for pi, (ci, emoji, name) in zip(free, avail):
        color_assign[str(pi)] = {"emoji": emoji, "name": name}

    pending["data"]["color_assign"] = color_assign
    db.set_pending_action(user_id, "assigning_colors", pending["data"])

    await query.answer("Цвета розыгрышем!")
    await query.edit_message_text(
        _color_assign_text(players, color_assign),
        parse_mode="Markdown",
        reply_markup=_color_assign_kb(players, color_assign, admin_id),
    )


async def cb_first_player_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gfirstshow_<admin_id> — показывает экран выбора первого игрока."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)
    if user_id != admin_id:
        await query.answer(); return

    pending = db.get_pending_action(user_id)
    if not pending or pending["action"] != "assigning_colors":
        await query.answer(); return

    players      = pending["data"]["players"]
    target       = pending["data"]["target"]
    color_assign = pending["data"].get("color_assign", {})

    # Применяем цвета
    for i, p in enumerate(players):
        a = color_assign.get(str(i)) or {"emoji": "", "name": ""}
        p["color_emoji"] = a["emoji"]
        p["color_name"]  = a["name"]

    db.set_pending_action(user_id, "assigning_first", {"players": players, "target": target})

    rows = [
        [InlineKeyboardButton(
            f"{p['color_emoji']} {p['username']}",
            callback_data=f"gfirst_{i}_{admin_id}",
        )]
        for i, p in enumerate(players)
    ]
    rows.append([InlineKeyboardButton("🎲 Случайный", callback_data=f"gfirstrnd_{admin_id}")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_admin")])
    await query.answer()
    await query.edit_message_text(
        "👤 *Кто ходит первым?*\n_(Первый игрок получает +50 бонусных очков)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _start_game_with_first(context, query, user_id: int, admin_id: int, first_idx: int):
    """Применяет выбор первого игрока и стартует игру."""
    pending = db.get_pending_action(user_id)
    if not pending or pending["action"] != "assigning_first":
        return

    players = pending["data"]["players"]
    target  = pending["data"]["target"]
    players[first_idx]["total"] = players[first_idx].get("total", 0) + 50

    game_data = {
        "players":              players,
        "current_idx":          first_idx,
        "turn_score":           0,
        "turn_history":         [],
        "turn_crosses":         0,
        "turn_freezes":         0,
        "next_effects":         {},
        "target":               target,
        "board_chat_id":        None,
        "board_message_id":     None,
        "turn_message_id":      None,
        "turn_player_id":       None,
        "admin_id":             admin_id,
        "first_player_user_id":   players[first_idx]["user_id"],
        "final_round_trigger_idx": None,  # индекс игрока, первым набравшего цель
    }
    db.set_pending_action(user_id, "in_game", game_data)

    first = players[first_idx]
    color_lines = "\n".join(
        f"{p['color_emoji']} *{p['username']}* — {p['color_name']}" for p in players
    )
    await query.edit_message_text(
        f"🎲 *Игра начинается!*\n\n{color_lines}\n\n"
        f"Первым ходит {first['color_emoji']} *{first['username']}* _(+50 бонус)_",
        parse_mode="Markdown",
    )

    sent_board = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=game_board_text(players, first_idx, target, 0),
        parse_mode="Markdown",
        reply_markup=board_kb(admin_id),
    )
    game_data["board_chat_id"]    = sent_board.chat_id
    game_data["board_message_id"] = sent_board.message_id
    db.set_pending_action(user_id, "in_game", game_data)
    await _send_turn(context, admin_id, game_data)


async def cb_first_player_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gfirst_<player_idx>_<admin_id> — выбирает первого игрока вручную."""
    query    = update.callback_query
    user_id  = query.from_user.id
    parts    = query.data.split("_")   # gfirst_<idx>_<admin_id>
    first_idx = int(parts[1])
    admin_id  = int(parts[2])
    if user_id != admin_id:
        await query.answer(); return
    await query.answer()
    await _start_game_with_first(context, query, user_id, admin_id, first_idx)


async def cb_first_player_random(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gfirstrnd_<admin_id> — случайный первый игрок."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)
    if user_id != admin_id:
        await query.answer(); return

    pending = db.get_pending_action(user_id)
    if not pending or pending["action"] != "assigning_first":
        await query.answer(); return

    first_idx = random.randrange(len(pending["data"]["players"]))
    await query.answer("🎲 Случайный!")
    await _start_game_with_first(context, query, user_id, admin_id, first_idx)


# ── Core game turn logic ───────────────────────────────────────────────────────

def _first_player_idx(data: dict) -> int:
    """Возвращает индекс игрока, с которого начинается каждый круг."""
    first_uid = data.get("first_player_user_id")
    players   = data.get("players", [])
    for i, p in enumerate(players):
        if p["user_id"] == first_uid:
            return i
    return 0


def _apply_crosses(data: dict, player_uid: int):
    """Переносит кресты текущего хода в next_effects игрока."""
    n = data.get("turn_crosses", 0)
    if n:
        next_effects = data.get("next_effects") or {}
        e = next_effects.get(str(player_uid)) or {"crosses": 0, "frozen": 0}
        e["crosses"] = e.get("crosses", 0) + n
        next_effects[str(player_uid)] = e
        data["next_effects"] = next_effects


async def _send_turn(context, admin_id: int, data: dict):
    """Отправляет сообщение хода текущему игроку, обновляет доску."""
    players     = data["players"]
    current_idx = data["current_idx"]
    player      = players[current_idx]
    target      = data["target"]
    turn_score  = data.get("turn_score", 0)
    player_uid  = player["user_id"]

    # Извлекаем и очищаем эффекты текущего игрока (показываем один раз)
    next_effects = data.get("next_effects") or {}
    player_effects = next_effects.pop(str(player_uid), {})
    data["next_effects"] = next_effects

    # Чистим my_turn предыдущего игрока (write-through cache, фоновый HTTP)
    old_tp = data.get("turn_player_id")
    if old_tp and old_tp != admin_id:
        fire_and_forget(adb(db.clear_pending_action, old_tp))

    # Ставим my_turn новому игроку (если не сам админ)
    if player_uid != admin_id:
        fire_and_forget(adb(db.set_pending_action, player_uid, "my_turn",
                            {"game_admin_id": admin_id}))

    # Победная фраза и кулдаун таунта для текущего игрока
    _puser      = db.get_user(player_uid)
    _win_msg    = (_puser.get("combo_win_message") or None) if _puser else None
    # Отправляем сообщение хода в личку текущему игроку — это критический путь
    try:
        sent = await context.bot.send_message(
            chat_id=player_uid,
            text=turn_message_text(player, target, turn_score,
                                   effects=player_effects or None,
                                   all_players=players),
            parse_mode="Markdown",
            reply_markup=turn_message_kb(turn_score, admin_id, num_players=len(players),
                                         white_tokens=_white_tokens(player),
                                         win_message=_win_msg),
        )
        data["turn_message_id"] = sent.message_id
        data["turn_player_id"]  = player_uid
    except Exception as e:
        logger.warning("Не удалось отправить ход игроку %s: %s", player_uid, e)
        data["turn_player_id"] = player_uid

    # Сохраняем состояние — cache обновится синхронно, HTTP уходит в thread
    await adb(db.set_pending_action, admin_id, "in_game", data)

    # Обновляем доску + рассылаем уведомления остальным — всё параллельно
    board_text = game_board_text(players, current_idx, target, turn_score)

    async def _update_board():
        try:
            await context.bot.edit_message_text(
                chat_id=data["board_chat_id"],
                message_id=data["board_message_id"],
                text=board_text,
                parse_mode="Markdown",
                reply_markup=board_kb(admin_id),
            )
        except Exception as e:
            logger.warning("Не удалось обновить доску: %s", e)

    notify_text = (
        f"🎯 Ход переходит к {player.get('color_emoji','')} "
        f"*{player.get('display_name') or player['username']}*\n\n"
        + board_text
    )
    tasks = [_update_board()]
    for p in players:
        uid = p["user_id"]
        if uid == player_uid or uid == admin_id:
            continue
        tasks.append(_safe_send(context, uid, notify_text, parse_mode="Markdown"))
    await asyncio.gather(*tasks)


async def _handle_game_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    user_id: int, text: str, data: dict,
):
    """Текстовый ввод очков от игрока или админа-игрока.
    Поддерживает +N (прибавить), -N (отнять), N (прибавить).
    """
    raw = text.replace(" ", "").replace(" ", "").replace(",", "")
    try:
        amount = int(raw)   # int() корректно обрабатывает +500, -500, 500
    except ValueError:
        await update.message.reply_text(
            "❌ Введите число, например: *350*, *+500* или *-200*", parse_mode="Markdown"
        ); return

    if amount == 0:
        await update.message.reply_text("❌ Число не может быть нулём."); return

    abs_amount = abs(amount)
    if abs_amount % 50 != 0:
        nearest_abs = round(abs_amount / 50) * 50
        sign = "-" if amount < 0 else ""
        await update.message.reply_text(
            f"❌ Число должно быть кратно 50.\nВозможно: *{sign}{fmt(nearest_abs)}*?",
            parse_mode="Markdown"
        ); return

    admin_id = data["game_admin_id"]
    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await update.message.reply_text("❌ Игра не найдена."); db.clear_pending_action(user_id); return

    gdata   = gp["data"]
    players = gdata["players"]
    cidx    = gdata["current_idx"]

    if players[cidx]["user_id"] != user_id:
        await update.message.reply_text("Сейчас не твой ход!"); return

    gdata["turn_score"] = max(0, gdata.get("turn_score", 0) + amount)
    history = gdata.get("turn_history") or []
    history.append(amount)
    gdata["turn_history"] = history

    # Обновляем сообщение хода
    tid = gdata.get("turn_message_id")
    if tid:
        try:
            await context.bot.edit_message_text(
                chat_id=user_id,
                message_id=tid,
                text=turn_message_text(players[cidx], gdata["target"], gdata["turn_score"]),
                parse_mode="Markdown",
                reply_markup=turn_message_kb(gdata["turn_score"], admin_id, can_undo=True,
                                             white_tokens=_white_tokens(players[cidx])),
            )
        except Exception as e:
            logger.warning("edit turn msg: %s", e)

    # Обновляем доску
    try:
        await context.bot.edit_message_text(
            chat_id=gdata["board_chat_id"],
            message_id=gdata["board_message_id"],
            text=game_board_text(players, cidx, gdata["target"], gdata["turn_score"]),
            parse_mode="Markdown",
            reply_markup=board_kb(admin_id),
        )
    except Exception as e:
        logger.warning("edit board: %s", e)

    db.set_pending_action(admin_id, "in_game", gdata)

    try:
        await update.message.delete()
    except Exception:
        pass


# ── Game helpers ──────────────────────────────────────────────────────────────

async def _resend_turn_message(context, query, chat_id: int,
                                text: str, markup, old_msg_id: int | None) -> int | None:
    """Удаляет старое сообщение хода и отправляет новое внизу чата.
    Возвращает message_id нового сообщения (или None при ошибке)."""
    # Убираем кнопки у старого сообщения чтобы не путать
    if old_msg_id:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=old_msg_id, reply_markup=None
            )
        except Exception:
            pass
    try:
        sent = await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=markup,
        )
        return sent.message_id
    except Exception as e:
        logger.warning("_resend_turn_message: %s", e)
        return None


# ── Game button callbacks ──────────────────────────────────────────────────────

async def cb_game_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    user_id  = query.from_user.id
    parts    = query.data.split("_")       # gadd_<amount>_<admin_id>
    amount   = int(parts[1])
    admin_id = int(parts[2])

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return

    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]

    if players[cidx]["user_id"] != user_id:
        await query.answer("Сейчас не твой ход!", show_alert=True); return

    data["turn_score"]   = data.get("turn_score", 0) + amount
    history = data.get("turn_history") or []
    history.append(amount)
    data["turn_history"] = history
    # Cache обновится синхронно, HTTP — в thread-pool, event loop свободен
    await adb(db.set_pending_action, admin_id, "in_game", data)

    await query.answer(f"+{fmt(amount)}")

    # Личное сообщение игроку и обновление доски — параллельно
    async def _do_board_edit():
        try:
            await context.bot.edit_message_text(
                chat_id=data["board_chat_id"],
                message_id=data["board_message_id"],
                text=game_board_text(players, cidx, data["target"], data["turn_score"]),
                parse_mode="Markdown",
                reply_markup=board_kb(admin_id),
            )
        except Exception as e:
            logger.warning("edit board (gadd): %s", e)

    new_mid, _ = await asyncio.gather(
        _resend_turn_message(
            context, query, user_id,
            turn_message_text(players[cidx], data["target"], data["turn_score"],
                              turn_crosses=data.get("turn_crosses", 0),
                              all_players=players),
            turn_message_kb(data["turn_score"], admin_id,
                            can_undo=bool(history),
                            turn_crosses=data.get("turn_crosses", 0),
                            num_players=len(players),
                            white_tokens=_white_tokens(players[cidx])),
            data.get("turn_message_id"),
        ),
        _do_board_edit(),
    )
    if new_mid:
        data["turn_message_id"] = new_mid
        fire_and_forget(adb(db.set_pending_action, admin_id, "in_game", data))


async def cb_game_end_turn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game": return

    data       = gp["data"]
    players    = data["players"]
    cidx       = data["current_idx"]
    turn_score = data.get("turn_score", 0)
    target     = data["target"]

    if players[cidx]["user_id"] != user_id:
        await query.answer("Сейчас не твой ход!", show_alert=True); return

    old_total = players[cidx]["total"]
    players[cidx]["total"] += turn_score
    new_total = players[cidx]["total"]
    pname     = players[cidx].get("display_name") or players[cidx]["username"]

    # Bank fx — кастомный текст-эффект игрока при банке (если куплен)
    bank_fx_line = ""
    try:
        banker_user = db.get_user(players[cidx]["user_id"])
        bank_fx = (banker_user or {}).get("combo_bank_fx") or ""
        if bank_fx:
            bank_fx_line = f"\n{bank_fx}"
    except Exception:
        pass

    # Редактируем сообщение хода → результат
    await query.edit_message_text(
        f"✅ *{pname}* завершил ход!\n"
        f"Заработано: *{fmt(turn_score)}*  |  Итого: *{fmt(new_total)}*"
        f"{bank_fx_line}",
        parse_mode="Markdown",
    )

    # Напоминания о магазине — рассылаем в фоне параллельно,
    # ход переходит к следующему игроку без ожидания.
    board_id = data["board_chat_id"]
    crossed = crossed_thresholds(old_total, new_total, target, num_players=len(players))
    if crossed:
        async def _send_shop_notifications():
            notification_tasks = []
            for thr in crossed:
                shop_text = (
                    f"🛒 {players[cidx]['color_emoji']} *{pname}* прошёл рубеж *{fmt(thr)}*!\n"
                    f"Пора зайти в магазин за новыми кубиками! 🎲"
                )
                targets = {board_id}
                targets.update(p["user_id"] for p in players)
                for chat_id in targets:
                    notification_tasks.append(
                        _safe_send(context, chat_id, shop_text, parse_mode="Markdown")
                    )
            await asyncio.gather(*notification_tasks)
        fire_and_forget(_send_shop_notifications())

    data["players"] = players

    # Переносим кресты этого хода в next_effects текущего игрока
    _apply_crosses(data, players[cidx]["user_id"])

    trigger   = data.get("final_round_trigger_idx")
    n         = len(players)
    first_idx = _first_player_idx(data)
    next_idx  = (cidx + 1) % n

    if new_total >= target and trigger is None:
        # Игрок достиг цели — фиксируем триггер
        data["final_round_trigger_idx"] = cidx
        trigger = cidx
        # Оповещаем о финальном круге ТОЛЬКО если круг ещё не закончен.
        # Шлём в фоне — не задерживаем переход к следующему игроку.
        if next_idx != first_idx:
            fire_and_forget(_safe_send(
                context, data["board_chat_id"],
                f"🏁 *{pname}* набрал *{fmt(new_total)}* и достиг цели!\n"
                f"Остальные игроки делают последний ход.",
                parse_mode="Markdown",
            ))

    # Следующий игрок
    data["current_idx"]  = next_idx
    data["turn_score"]   = 0
    data["turn_history"] = []
    data["turn_crosses"] = 0
    data["turn_freezes"] = 0

    # Круг завершён (вернулись к первому игроку) И кто-то уже достиг цели — конец игры
    if trigger is not None and next_idx == first_idx:
        await _finish_game(context, admin_id, data)
        return

    await _send_turn(context, admin_id, data)


async def cb_game_lose_turn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game": return

    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]

    if players[cidx]["user_id"] != user_id:
        await query.answer("Сейчас не твой ход!", show_alert=True); return

    pname = players[cidx].get("display_name") or players[cidx]["username"]
    await query.edit_message_text(
        f"💀 *{pname}* потерял ход!  Накопленные очки, кресты и заморозки отменяются.",
        parse_mode="Markdown",
    )

    # Откатываем заморозки этого хода (ход сгорел — эффекты не переходят)
    freeze_count = data.get("turn_freezes", 0)
    if freeze_count:
        next_effects = data.get("next_effects") or {}
        others = [p for p in players if p["user_id"] != user_id]
        for p in others:
            uid_s = str(p["user_id"])
            if uid_s in next_effects:
                next_effects[uid_s]["frozen"] = max(
                    0, next_effects[uid_s].get("frozen", 0) - freeze_count
                )
                if not any(next_effects[uid_s].values()):
                    del next_effects[uid_s]
        data["next_effects"] = next_effects

    # Кресты тоже сгорают — просто не вызываем _apply_crosses
    next_idx = (cidx + 1) % len(players)
    data["current_idx"]  = next_idx
    data["turn_score"]   = 0
    data["turn_history"] = []
    data["turn_crosses"] = 0
    data["turn_freezes"] = 0

    # Круг завершён (вернулись к первому игроку) И кто-то уже достиг цели — конец игры
    trigger = data.get("final_round_trigger_idx")
    if trigger is not None and next_idx == _first_player_idx(data):
        await _finish_game(context, admin_id, data)
        return

    await _send_turn(context, admin_id, data)


async def cb_game_taunt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gtaunt_<admin_id> — игрок отправляет свою победную фразу как таунт в игровой чат."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer("Игра не найдена.", show_alert=True); return

    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]

    # Проверяем что сейчас ход этого игрока
    if players[cidx]["user_id"] != user_id:
        await query.answer("Сейчас не твой ход.", show_alert=True); return

    # Берём победную фразу из профиля
    puser   = db.get_user(user_id)
    win_msg = (puser.get("combo_win_message") or "").strip() if puser else ""
    if not win_msg:
        await query.answer("У тебя не установлена победная фраза. Купи в магазине!", show_alert=True); return

    await query.answer("💬 Таунт отправлен!")

    # Формируем имя отправителя
    player  = players[cidx]
    pname   = player.get("display_name") or player["username"]
    color   = player.get("color_emoji", "")

    # Отправляем таунт в игровой чат
    board_chat_id = data.get("board_chat_id")
    if board_chat_id:
        try:
            await context.bot.send_message(
                chat_id=board_chat_id,
                text=f"💬 {color} *{pname}*:\n_{win_msg}_",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Не удалось отправить таунт в чат: %s", e)



async def cb_game_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return

    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]

    if players[cidx]["user_id"] != user_id:
        await query.answer("Сейчас не твой ход!", show_alert=True); return

    history = data.get("turn_history") or []
    if not history:
        await query.answer("Нечего отменять!", show_alert=True); return

    last_amount      = history.pop()
    data["turn_history"] = history
    data["turn_score"]   = max(0, data.get("turn_score", 0) - last_amount)
    db.set_pending_action(admin_id, "in_game", data)

    tc = data.get("turn_crosses", 0)
    await query.answer(f"↩️ Отменено: -{fmt(last_amount)}")
    await query.edit_message_text(
        turn_message_text(players[cidx], data["target"], data["turn_score"],
                          turn_crosses=tc, all_players=players),
        parse_mode="Markdown",
        reply_markup=turn_message_kb(data["turn_score"], admin_id, can_undo=bool(history),
                                     turn_crosses=tc, num_players=len(players),
                                     white_tokens=_white_tokens(players[cidx])),
    )
    try:
        await context.bot.edit_message_text(
            chat_id=data["board_chat_id"],
            message_id=data["board_message_id"],
            text=game_board_text(players, cidx, data["target"], data["turn_score"]),
            parse_mode="Markdown",
            reply_markup=board_kb(admin_id),
        )
    except Exception as e:
        logger.warning("edit board (undo): %s", e)


async def cb_effect_cross(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Игрок нажал ✝️ Кресты — увеличивает счётчик крестов на этот ход."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return

    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    if players[cidx]["user_id"] != user_id:
        await query.answer("Сейчас не твой ход!", show_alert=True); return

    data["turn_crosses"] = data.get("turn_crosses", 0) + 1
    tc = data["turn_crosses"]
    db.set_pending_action(admin_id, "in_game", data)
    log_combo_bg(user_id, "special_cross")

    await query.answer(f"✝️ Кресты #{tc} отмечены!")
    await query.edit_message_text(
        turn_message_text(players[cidx], data["target"], data.get("turn_score", 0),
                          turn_crosses=tc, all_players=players),
        parse_mode="Markdown",
        reply_markup=turn_message_kb(data.get("turn_score", 0), admin_id,
                                     can_undo=bool(data.get("turn_history")),
                                     turn_crosses=tc, num_players=len(players),
                                     white_tokens=_white_tokens(players[cidx])),
    )


async def cb_effect_freeze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Игрок нажал ❄️ Заморозка — добавляет -1 кубик всем остальным игрокам."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return

    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    if players[cidx]["user_id"] != user_id:
        await query.answer("Сейчас не твой ход!", show_alert=True); return

    next_effects = data.get("next_effects") or {}
    others = [p for p in players if p["user_id"] != user_id]
    for p in others:
        uid_s = str(p["user_id"])
        e = next_effects.get(uid_s) or {"crosses": 0, "frozen": 0}
        e["frozen"] = e.get("frozen", 0) + 1
        next_effects[uid_s] = e
    data["next_effects"]  = next_effects
    data["turn_freezes"]  = data.get("turn_freezes", 0) + 1
    db.set_pending_action(admin_id, "in_game", data)
    log_combo_bg(user_id, "special_freeze")

    names = ", ".join(p["username"] for p in others)
    await query.answer(f"❄️ Заморозка! {names} получат -1 кубик", show_alert=True)


async def cb_effect_thief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gfx_thief_<admin_id> — Вор: показывает выбор значения кубика."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return
    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    if players[cidx]["user_id"] != user_id:
        await query.answer("Сейчас не твой ход!", show_alert=True); return

    total     = len(players)
    steal_map = {v: _thief_steal(v, total) for v in (4, 5, 6)}

    data["thief_multiplier"] = 1
    db.set_pending_action(admin_id, "in_game", data)
    await query.answer()
    others = [p for p in players if p["user_id"] != user_id]
    lines  = [
        f"🥷 *Вор!*  Введи значение выпавшего кубика.\n"
        f"Украдёшь у: {', '.join(p['username'] for p in others)}\n",
        f"4️⃣ → {fmt(steal_map[4])} очков с каждого",
        f"5️⃣ → {fmt(steal_map[5])} очков",
        f"6️⃣ → {fmt(steal_map[6])} очков",
        f"1️⃣-3️⃣ → ничего",
    ]
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=_thief_kb(admin_id, 1),
    )


async def cb_apply_thief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gthief_<val>_<admin_id> — применяет кражу вора."""
    query    = update.callback_query
    user_id  = query.from_user.id
    parts    = query.data.split("_")   # gthief_<val>_<admin_id>
    val      = int(parts[1])
    admin_id = int(parts[2])

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return
    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    if players[cidx]["user_id"] != user_id:
        await query.answer("Сейчас не твой ход!", show_alert=True); return

    total       = len(players)
    mult        = data.pop("thief_multiplier", 1)
    steal_total = _thief_steal(val, total) * mult
    others      = [p for p in players if p["user_id"] != user_id]

    for p in others:
        p["total"] = max(0, p["total"] - steal_total)
    data["turn_score"] = data.get("turn_score", 0) + steal_total * len(others)
    db.set_pending_action(admin_id, "in_game", data)
    log_combo_bg(user_id, "special_thief")

    await query.answer(f"🥷 Украдено {fmt(steal_total)} у каждого!")
    stolen_total = steal_total * len(others)
    turn_score   = data["turn_score"]
    await query.edit_message_text(
        turn_message_text(players[cidx], data["target"], turn_score,
                          turn_crosses=data.get("turn_crosses", 0),
                          all_players=players),
        parse_mode="Markdown",
        reply_markup=turn_message_kb(turn_score, admin_id,
                                     can_undo=bool(data.get("turn_history")),
                                     turn_crosses=data.get("turn_crosses", 0),
                                     num_players=total,
                                     white_tokens=_white_tokens(players[cidx])),
    )
    # Уведомляем ограбленных
    for p in others:
        try:
            await context.bot.send_message(
                chat_id=p["user_id"],
                text=f"🥷 *{players[cidx]['username']}* украл у тебя *{fmt(steal_total)}* очков!",
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def cb_effect_robbery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gfx_robbery_<admin_id> — Разбой: инициатор и все остальные бросают кубик."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return
    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    if players[cidx]["user_id"] != user_id:
        await query.answer("Сейчас не твой ход!", show_alert=True); return

    initiator  = players[cidx]
    others     = [p for p in players if p["user_id"] != user_id]
    total      = len(players)
    BASE_ROB   = _robbery_bonus(total)

    data["robbery_active"]     = True
    data["robbery_initiator"]  = user_id
    data["robbery_rolls"]      = {}
    data["robbery_multiplier"] = 1
    db.set_pending_action(admin_id, "in_game", data)

    await query.answer()
    # Инициатору — клавиатура с утроением и отменой
    await query.edit_message_text(
        f"⚔️ *Разбой!*\n"
        f"Победишь каждого (твой кубик > чужого) → *+{BASE_ROB}* за каждого\n"
        f"Проиграешь (его ≥ твоего) → ничего не происходит\n\n"
        f"*{initiator['username']}*, введи своё значение кубика:",
        parse_mode="Markdown",
        reply_markup=_robbery_kb(admin_id, 1, show_triple=True),
    )
    # Остальным — клавиатура без утроения и без отмены
    for p in others:
        try:
            await context.bot.send_message(
                chat_id=p["user_id"],
                text=(
                    f"⚔️ *Разбой от {initiator['username']}!*\n"
                    f"Если твой кубик ≥ его → ничего не теряешь.\n"
                    f"Если его кубик > твоего → минус *{BASE_ROB}* очков.\n\n"
                    f"Введи своё значение кубика:"
                ),
                parse_mode="Markdown",
                reply_markup=_robbery_kb(admin_id, 1, show_triple=False),
            )
        except Exception:
            pass


async def cb_apply_robbery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """grobbery_<val>_<admin_id> — каждый игрок вводит свой бросок разбоя."""
    query    = update.callback_query
    user_id  = query.from_user.id
    parts    = query.data.split("_")
    val      = int(parts[1])
    admin_id = int(parts[2])

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return
    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]

    if not data.get("robbery_active"):
        await query.answer(); return

    # Проверяем, что пользователь — участник этой игры
    if not any(p["user_id"] == user_id for p in players):
        await query.answer("Ты не участник этой игры.", show_alert=True); return

    rolls = data.get("robbery_rolls", {})
    if str(user_id) in rolls:
        await query.answer("Ты уже ввёл своё значение.", show_alert=True); return

    rolls[str(user_id)] = val
    data["robbery_rolls"] = rolls
    db.set_pending_action(admin_id, "in_game", data)

    # Ещё не все ввели
    all_uids  = [p["user_id"] for p in players]
    remaining = [uid for uid in all_uids if str(uid) not in rolls]
    if remaining:
        n = len(remaining)
        await query.edit_message_text(
            f"⚔️ *Разбой!*\nТвой бросок: *{val}* ✅\nЖдём ещё {n} игрок(ов)…",
            parse_mode="Markdown",
        )
        return

    # Все ввели — считаем результаты пара за парой
    mult       = data.get("robbery_multiplier", 1)
    total      = len(players)
    base_rob   = _robbery_bonus(total)
    bonus      = base_rob * mult
    initiator_uid  = data.get("robbery_initiator", players[cidx]["user_id"])
    initiator_roll = rolls.get(str(initiator_uid), 0)
    initiator_p    = next(p for p in players if p["user_id"] == initiator_uid)

    result_lines = [f"⚔️ *Разбой — результаты:*{'  ✖️×'+str(mult) if mult > 1 else ''}\n"]
    for p in players:
        r = rolls.get(str(p["user_id"]), "—")
        result_lines.append(f"{p.get('color_emoji','')} {p['username']}: {r}")
    result_lines.append("")

    initiator_gain  = 0
    penalty_map: dict[int, int] = {}
    for p in players:
        if p["user_id"] == initiator_uid:
            continue
        opp_roll = rolls.get(str(p["user_id"]), 0)
        if initiator_roll > opp_roll:
            initiator_gain += bonus
            penalty_map[p["user_id"]] = bonus
            result_lines.append(
                f"✅ *{p['username']}* проиграл ({opp_roll} < {initiator_roll}) → -{fmt(bonus)}"
            )
        else:
            result_lines.append(
                f"🛡 *{p['username']}* устоял ({opp_roll} ≥ {initiator_roll}) → ничего"
            )

    # Применяем штрафы проигравшим
    for p in players:
        if p["user_id"] in penalty_map:
            p["total"] = max(0, p["total"] - penalty_map[p["user_id"]])

    if initiator_gain > 0:
        data["turn_score"] = data.get("turn_score", 0) + initiator_gain
        result_lines.append(f"\n🏆 *{initiator_p['username']}* забирает *+{fmt(initiator_gain)}* суммарно!")
    else:
        result_lines.append(f"\n😞 *{initiator_p['username']}* никого не победил — ничего не получает.")

    data["robbery_active"]    = False
    data["robbery_initiator"] = None
    data["robbery_rolls"]     = {}
    data["robbery_multiplier"] = 1
    data["players"] = players
    db.set_pending_action(admin_id, "in_game", data)
    log_combo_bg(initiator_uid, "special_robbery")
    await query.answer()

    result_text = "\n".join(result_lines)
    turn_score  = data["turn_score"]
    turn_text   = turn_message_text(initiator_p, data["target"], turn_score,
                                    turn_crosses=data.get("turn_crosses", 0),
                                    all_players=players)
    turn_kb     = turn_message_kb(turn_score, admin_id,
                                  can_undo=bool(data.get("turn_history")),
                                  turn_crosses=data.get("turn_crosses", 0),
                                  num_players=total,
                                  white_tokens=_white_tokens(initiator_p))

    # Редактируем сообщение того, кто ввёл последним (показываем результаты)
    try:
        await query.edit_message_text(result_text, parse_mode="Markdown")
    except Exception:
        pass

    # Инициатору отправляем результат + карточку хода
    try:
        sent = await context.bot.send_message(
            chat_id=initiator_uid,
            text=result_text + "\n\n" + turn_text,
            parse_mode="Markdown",
            reply_markup=turn_kb,
        )
        data["turn_message_id"] = sent.message_id
        db.set_pending_action(admin_id, "in_game", data)
    except Exception:
        pass

    # Уведомляем проигравших
    for uid, pen in penalty_map.items():
        if uid == user_id:   # уже видит через edit
            continue
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    f"⚔️ Разбой!\n"
                    f"*{initiator_p['username']}* выбросил {initiator_roll}, ты — {rolls.get(str(uid),'?')}.\n"
                    f"Ты потерял *{fmt(pen)}* очков."
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass


def _pname(players: list, uid: int) -> str:
    """Имя игрока по user_id."""
    p = next((x for x in players if x["user_id"] == uid), None)
    return p["username"] if p else str(uid)


# ── 📋 Обновить — переотправить сообщение хода вниз чата ────────────────────

async def cb_game_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """grefresh_<admin_id> — пересылает текущее сообщение хода вниз."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer("Игра уже завершена."); return
    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    if players[cidx]["user_id"] != user_id:
        await query.answer("Сейчас не твой ход!"); return

    await query.answer("📋 Обновлено!")
    ts = data.get("turn_score", 0)
    tc = data.get("turn_crosses", 0)
    new_mid = await _resend_turn_message(
        context, query, user_id,
        turn_message_text(players[cidx], data["target"], ts,
                          turn_crosses=tc, all_players=players),
        turn_message_kb(ts, admin_id, can_undo=bool(data.get("turn_history")),
                        turn_crosses=tc, num_players=len(players),
                        white_tokens=_white_tokens(players[cidx])),
        data.get("turn_message_id"),
    )
    if new_mid:
        data["turn_message_id"] = new_mid
        db.set_pending_action(admin_id, "in_game", data)


# ── ⚙️ Админ-правка очков во время игры ─────────────────────────────────────

def _admin_edit_kb(players: list, admin_id: int) -> InlineKeyboardMarkup:
    rows = []
    for p in players:
        rows.append([InlineKeyboardButton(
            f"{p.get('color_emoji','')} {p['username']} — {fmt(p['total'])}",
            callback_data=f"gadmedit_{p['user_id']}_{admin_id}",
        )])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"gadmback_{admin_id}")])
    return InlineKeyboardMarkup(rows)


async def cb_game_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gadm_<admin_id> — показывает панель правки очков (только для admin)."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    if user_id != admin_id and not is_admin(user_id):
        await query.answer("⛔ Только администратор", show_alert=True); return

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer("Игра не найдена."); return
    data    = gp["data"]
    players = data["players"]

    await query.answer()
    lines = ["⚙️ *Правка очков*\n", "Выбери игрока для изменения:"]
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=_admin_edit_kb(players, admin_id),
    )


async def cb_game_admin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gadmedit_<uid>_<admin_id> — показывает кнопки +/- для конкретного игрока."""
    query    = update.callback_query
    user_id  = query.from_user.id
    parts    = query.data.split("_")   # gadmedit_<uid>_<admin_id>
    target_uid = int(parts[1])
    admin_id   = int(parts[2])

    if user_id != admin_id and not is_admin(user_id):
        await query.answer("⛔ Только администратор", show_alert=True); return

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return
    data    = gp["data"]
    players = data["players"]
    tp      = next((p for p in players if p["user_id"] == target_uid), None)
    if not tp:
        await query.answer("Игрок не найден."); return

    await query.answer()
    amounts = [50, 100, 200, 500, 1000, 2000, 5000]
    plus_row  = [InlineKeyboardButton(f"+{fmt(a)}", callback_data=f"gadmdelta_{target_uid}_{a}_{admin_id}") for a in amounts]
    minus_row = [InlineKeyboardButton(f"-{fmt(a)}", callback_data=f"gadmdelta_{target_uid}_{-a}_{admin_id}") for a in amounts]
    rows = [plus_row[:4], plus_row[4:], minus_row[:4], minus_row[4:],
            [InlineKeyboardButton("◀️ Назад", callback_data=f"gadm_{admin_id}")]]
    await query.edit_message_text(
        f"⚙️ *{tp.get('color_emoji','')} {tp['username']}* — текущий счёт: *{fmt(tp['total'])}*\n"
        f"Выбери корректировку:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_game_admin_delta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gadmdelta_<uid>_<delta>_<admin_id> — применяет изменение очков."""
    query    = update.callback_query
    user_id  = query.from_user.id
    parts    = query.data.split("_")   # gadmdelta_<uid>_<delta>_<admin_id>
    target_uid = int(parts[1])
    delta      = int(parts[2])
    admin_id   = int(parts[3])

    if user_id != admin_id and not is_admin(user_id):
        await query.answer("⛔ Только администратор", show_alert=True); return

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return
    data    = gp["data"]
    players = data["players"]
    tp      = next((p for p in players if p["user_id"] == target_uid), None)
    if not tp:
        await query.answer(); return

    old_score  = tp["total"]
    tp["total"] = max(0, old_score + delta)
    db.set_pending_action(admin_id, "in_game", data)

    sign = "+" if delta >= 0 else ""
    await query.answer(f"{sign}{fmt(delta)} → {fmt(tp['total'])}", show_alert=True)
    # Обновляем доску
    try:
        await context.bot.edit_message_text(
            chat_id=data["board_chat_id"],
            message_id=data["board_message_id"],
            text=game_board_text(players, data["current_idx"], data["target"],
                                  data.get("turn_score", 0)),
            parse_mode="Markdown",
            reply_markup=board_kb(admin_id),
        )
    except Exception:
        pass
    # Возвращаемся к списку игроков
    await query.edit_message_text(
        f"⚙️ *Правка очков*\n✅ {tp['username']}: {fmt(old_score)} → {fmt(tp['total'])}",
        parse_mode="Markdown",
        reply_markup=_admin_edit_kb(players, admin_id),
    )


async def cb_game_admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gadmback_<admin_id> — возврат к ходу из админ-панели."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    # Доступ — только для админа игры (совпадает с cb_game_admin_panel)
    if user_id != admin_id and not is_admin(user_id):
        await query.answer("⛔ Только администратор", show_alert=True); return

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return
    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    ts      = data.get("turn_score", 0)
    tc      = data.get("turn_crosses", 0)
    await query.answer()
    await query.edit_message_text(
        turn_message_text(players[cidx], data["target"], ts,
                          turn_crosses=tc, all_players=players),
        parse_mode="Markdown",
        reply_markup=turn_message_kb(ts, admin_id,
                                     can_undo=bool(data.get("turn_history")),
                                     turn_crosses=tc, num_players=len(players),
                                     white_tokens=_white_tokens(players[cidx])),
    )


async def cb_thief_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gthiefcancel_<admin_id> — отмена вора, возврат к карточке хода."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return
    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    if players[cidx]["user_id"] != user_id:
        await query.answer(); return

    data.pop("thief_multiplier", None)
    db.set_pending_action(admin_id, "in_game", data)

    ts = data.get("turn_score", 0)
    tc = data.get("turn_crosses", 0)
    await query.answer("Вор отменён")
    await query.edit_message_text(
        turn_message_text(players[cidx], data["target"], ts,
                          turn_crosses=tc, all_players=players),
        parse_mode="Markdown",
        reply_markup=turn_message_kb(ts, admin_id,
                                     can_undo=bool(data.get("turn_history")),
                                     turn_crosses=tc, num_players=len(players),
                                     white_tokens=_white_tokens(players[cidx])),
    )


async def cb_robbery_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """grobcancel_<admin_id> — отмена разбоя инициатором, возврат к ходу."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return
    data = gp["data"]
    if data.get("robbery_initiator") != user_id:
        await query.answer("Только инициатор может отменить разбой.", show_alert=True); return

    players = data["players"]
    cidx    = data["current_idx"]
    data["robbery_active"]     = False
    data["robbery_initiator"]  = None
    data["robbery_rolls"]      = {}
    data["robbery_multiplier"] = 1
    db.set_pending_action(admin_id, "in_game", data)

    ts = data.get("turn_score", 0)
    tc = data.get("turn_crosses", 0)
    await query.answer("Разбой отменён")
    await query.edit_message_text(
        turn_message_text(players[cidx], data["target"], ts,
                          turn_crosses=tc, all_players=players),
        parse_mode="Markdown",
        reply_markup=turn_message_kb(ts, admin_id,
                                     can_undo=bool(data.get("turn_history")),
                                     turn_crosses=tc, num_players=len(players),
                                     white_tokens=_white_tokens(players[cidx])),
    )

    # Уведомляем остальных участников, что разбой отменён
    for p in players:
        if p["user_id"] == user_id:
            continue
        try:
            await context.bot.send_message(
                chat_id=p["user_id"],
                text=f"⚔️ *Разбой отменён* — {players[cidx]['username']} передумал.",
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def cb_game_white_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gtoken_<admin_id> — белый игрок записывает жетон (макс 2)."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return
    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    player  = players[cidx]

    if player["user_id"] != user_id:
        await query.answer("Сейчас не твой ход!", show_alert=True); return
    if player.get("color_emoji") != "⚪":
        await query.answer("Жетоны только для белого!", show_alert=True); return

    current_tokens = player.get("tokens", 0)
    if current_tokens >= 2:
        await query.answer("Максимум 2 жетона!", show_alert=True); return

    player["tokens"] = current_tokens + 1
    db.set_pending_action(admin_id, "in_game", data)
    log_combo_bg(user_id, "white_token")

    ts = data.get("turn_score", 0)
    tc = data.get("turn_crosses", 0)
    await query.answer(f"⚪ Жетон записан ({player['tokens']}/2)")
    await query.edit_message_text(
        turn_message_text(player, data["target"], ts,
                          turn_crosses=tc, all_players=players),
        parse_mode="Markdown",
        reply_markup=turn_message_kb(ts, admin_id,
                                     can_undo=bool(data.get("turn_history")),
                                     turn_crosses=tc, num_players=len(players),
                                     white_tokens=player["tokens"]),
    )


async def cb_game_use_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gusetoken_<admin_id> — белый игрок тратит один жетон."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return
    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    player  = players[cidx]

    if player["user_id"] != user_id:
        await query.answer("Сейчас не твой ход!", show_alert=True); return
    if player.get("color_emoji") != "⚪":
        await query.answer("Жетоны только для белого!", show_alert=True); return

    current_tokens = player.get("tokens", 0)
    if current_tokens <= 0:
        await query.answer("Нет жетонов для использования!", show_alert=True); return

    player["tokens"] = current_tokens - 1
    db.set_pending_action(admin_id, "in_game", data)
    log_combo_bg(user_id, "white_token_used")

    ts = data.get("turn_score", 0)
    tc = data.get("turn_crosses", 0)
    await query.answer(f"🔘 Жетон использован! Осталось: {player['tokens']}")
    await query.edit_message_text(
        turn_message_text(player, data["target"], ts,
                          turn_crosses=tc, all_players=players),
        parse_mode="Markdown",
        reply_markup=turn_message_kb(ts, admin_id,
                                     can_undo=bool(data.get("turn_history")),
                                     turn_crosses=tc, num_players=len(players),
                                     white_tokens=player["tokens"]),
    )


# ── ✖️ Утроение в диалогах Вора и Разбоя ─────────────────────────────────────

def _thief_kb(admin_id: int, mult: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора кубика вора с кнопкой утроения и отмены."""
    mult_label = {1: "✖️ Утроение (1×)", 3: "✖️×3 → ×9", 9: "✖️×9 (макс)"}
    rows = [
        [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"gthief_{v}_{admin_id}")
         for v in (1, 2, 3, 4, 5, 6)],
    ]
    if mult < 9:
        rows.append([InlineKeyboardButton(
            mult_label.get(mult, f"✖️×{mult}"),
            callback_data=f"gthiefx3_{admin_id}",
        )])
    else:
        rows.append([InlineKeyboardButton("✖️×9 (максимум)", callback_data="noop")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"gthiefcancel_{admin_id}")])
    return InlineKeyboardMarkup(rows)


def _robbery_kb(admin_id: int, mult: int, show_triple: bool = True) -> InlineKeyboardMarkup:
    """Клавиатура броска разбоя. show_triple=False для оппонентов (без кнопки утроения)."""
    mult_label = {1: "✖️ Утроение (1×)", 3: "✖️×3 → ×9", 9: "✖️×9 (макс)"}
    rows = [
        [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"grobbery_{v}_{admin_id}")
         for v in (1, 2, 3, 4, 5, 6)],
    ]
    if show_triple and mult < 9:
        rows.append([InlineKeyboardButton(
            mult_label.get(mult, f"✖️×{mult}"),
            callback_data=f"grobbery_x3_{admin_id}",
        )])
    if show_triple:
        rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"grobcancel_{admin_id}")])
    return InlineKeyboardMarkup(rows)


async def cb_thief_triple(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gthiefx3_<admin_id> — увеличивает множитель утроения для вора."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return
    data = gp["data"]
    if data["players"][data["current_idx"]]["user_id"] != user_id:
        await query.answer(); return

    mult = data.get("thief_multiplier", 1)
    mult = 3 if mult == 1 else 9
    data["thief_multiplier"] = mult
    db.set_pending_action(admin_id, "in_game", data)
    await query.answer(f"✖️ Утроение ×{mult}!")

    total = len(data["players"])
    steal_map = {4: max(0, 150-(total-3)*50), 5: max(0, 200-(total-3)*50), 6: max(0, 250-(total-3)*50)}
    others = [p for p in data["players"] if p["user_id"] != user_id]
    lines = [
        f"🥷 *Вор!* ✖️×{mult} Введи значение кубика:\n",
        *[f"{DICE_EMOJI[v]} → {fmt(steal_map[v] * mult)} (×{mult})" for v in (4, 5, 6)],
        "\n1️⃣-3️⃣ → ничего не крадёт",
    ]
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=_thief_kb(admin_id, mult),
    )


async def cb_robbery_triple(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """grobbery_x3_<admin_id> — утроение для разбоя (только инициатор)."""
    query    = update.callback_query
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return
    data = gp["data"]
    # Утроение может нажимать только инициатор разбоя
    if data.get("robbery_initiator") != user_id:
        await query.answer("Только инициатор разбоя может нажимать утроение.", show_alert=True); return

    mult = data.get("robbery_multiplier", 1)
    mult = 3 if mult == 1 else 9
    data["robbery_multiplier"] = mult
    db.set_pending_action(admin_id, "in_game", data)
    await query.answer(f"✖️ Утроение ×{mult}!")

    base_rob  = _robbery_bonus(len(data["players"]))
    win_bonus = base_rob * mult
    await query.edit_message_text(
        f"⚔️ *Разбой!* ✖️×{mult}\n"
        f"Победишь → *+{fmt(win_bonus)}* с каждого\n\n"
        f"Введи своё значение кубика:",
        parse_mode="Markdown",
        reply_markup=_robbery_kb(admin_id, mult, show_triple=True),
    )


async def cb_game_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game": return

    # Разрешаем только текущему игроку или админу
    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    if user_id != admin_id and players[cidx]["user_id"] != user_id:
        await query.answer("Только текущий игрок или админ может завершить игру.", show_alert=True); return

    # Показываем подтверждение вместо немедленного завершения
    confirm_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, закончить", callback_data=f"gfinish_yes_{admin_id}"),
        InlineKeyboardButton("❌ Отмена",        callback_data=f"gfinish_no_{admin_id}"),
    ]])
    try:
        await query.edit_message_text(
            "🏁 *Закончить игру?*\n\nЭто завершит партию и предложит записать результат.",
            parse_mode="Markdown",
            reply_markup=confirm_kb,
        )
    except Exception:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🏁 *Закончить игру?*\n\nЭто завершит партию и предложит записать результат.",
            parse_mode="Markdown",
            reply_markup=confirm_kb,
        )


async def cb_finish_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gfinish_yes_<admin_id> — подтверждение завершения игры."""
    query    = update.callback_query
    await query.answer()
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.edit_message_text("❌ Игра не найдена."); return

    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    if user_id != admin_id and players[cidx]["user_id"] != user_id:
        await query.answer("Только текущий игрок или админ.", show_alert=True); return

    try:
        await query.edit_message_text("🏁 Игра завершена досрочно!")
    except Exception:
        pass
    await _finish_game(context, admin_id, data)


async def cb_finish_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gfinish_no_<admin_id> — отмена завершения, возврат к ходу."""
    query    = update.callback_query
    await query.answer("Отменено")
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.edit_message_text("❌ Игра не найдена."); return

    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    # Доступ — только текущий игрок или админ (совпадает с cb_game_finish)
    if user_id != admin_id and players[cidx]["user_id"] != user_id:
        return
    ts      = data.get("turn_score", 0)
    tc      = data.get("turn_crosses", 0)
    try:
        await query.edit_message_text(
            turn_message_text(players[cidx], data["target"], ts,
                              turn_crosses=tc, all_players=players),
            parse_mode="Markdown",
            reply_markup=turn_message_kb(ts, admin_id,
                                         can_undo=bool(data.get("turn_history")),
                                         turn_crosses=tc, num_players=len(players),
                                         white_tokens=_white_tokens(players[cidx])),
        )
    except Exception:
        pass


async def cb_gfx_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gfx_menu_<admin_id> — показать подменю спецэффектов."""
    query    = update.callback_query
    await query.answer()
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return

    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    if players[cidx]["user_id"] != user_id and user_id != admin_id:
        await query.answer("Сейчас не твой ход!", show_alert=True); return

    tc = data.get("turn_crosses", 0)
    cross_label = f"✝️ Крест ({tc})" if tc else "✝️ Крест"
    fx_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(cross_label,      callback_data=f"gfx_cross_{admin_id}"),
            InlineKeyboardButton("❄️ Заморозка",   callback_data=f"gfx_freeze_{admin_id}"),
        ],
        [
            InlineKeyboardButton("🥷 Вор",         callback_data=f"gfx_thief_{admin_id}"),
            InlineKeyboardButton("⚔️ Разбой",      callback_data=f"gfx_robbery_{admin_id}"),
        ],
        [InlineKeyboardButton("◀ Назад", callback_data=f"gfx_back_{admin_id}")],
    ])
    try:
        await query.edit_message_text(
            "⚔️ *Эффекты* — выбери действие:",
            parse_mode="Markdown",
            reply_markup=fx_kb,
        )
    except Exception:
        pass


async def cb_gfx_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gfx_back_<admin_id> — вернуться из меню эффектов к ходу."""
    query    = update.callback_query
    await query.answer()
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        await query.answer(); return

    data    = gp["data"]
    players = data["players"]
    cidx    = data["current_idx"]
    # Доступ — только текущий игрок или админ (совпадает с cb_gfx_menu)
    if players[cidx]["user_id"] != user_id and user_id != admin_id:
        return
    ts      = data.get("turn_score", 0)
    tc      = data.get("turn_crosses", 0)
    try:
        await query.edit_message_text(
            turn_message_text(players[cidx], data["target"], ts,
                              turn_crosses=tc, all_players=players),
            parse_mode="Markdown",
            reply_markup=turn_message_kb(ts, admin_id,
                                         can_undo=bool(data.get("turn_history")),
                                         turn_crosses=tc, num_players=len(players),
                                         white_tokens=_white_tokens(players[cidx])),
        )
    except Exception:
        pass


async def _finish_game(context, admin_id: int, data: dict):
    players = data["players"]
    target  = data["target"]
    total   = len(players)

    first_uid = data.get("first_player_user_id")
    sorted_p  = sorted(players, key=lambda p: p["total"], reverse=True)
    for rank, p in enumerate(sorted_p, 1):
        p["place"]         = rank
        p["points_change"] = rating_change_v2(rank, total, p["total"], target)
        p["went_first"]    = (p["user_id"] == first_uid)

    # Чистим my_turn текущего игрока
    tp = data.get("turn_player_id")
    if tp and tp != admin_id:
        db.clear_pending_action(tp)

    data["results"] = sorted_p
    db.set_pending_action(admin_id, "confirm_game", data)
    # Резервная копия в отдельной таблице — защита от потери данных
    try:
        db.save_game_draft(admin_id, sorted_p, target)
    except Exception as e:
        logger.warning("Не удалось сохранить game_draft: %s", e)

    winner      = sorted_p[0]
    winner_name = winner.get("display_name") or _user_display_name(winner["user_id"])

    # Праздничная шапка победителя с баннером/титулом/префиксами
    winner_header = _winner_celebration(
        winner["user_id"], winner.get("color_emoji", ""), winner["total"],
    )

    lines = [
        winner_header,
        "",
        f"🏁 *Игра завершена!*  _(цель: {fmt(target)})_",
        "",
    ]
    for p in sorted_p:
        change = p["points_change"]
        sign   = "+" if change >= 0 else ""
        dname  = p.get("display_name") or _user_display_name(p["user_id"])
        puser  = db.get_user(p["user_id"])
        is_cal = puser.get("is_calibrated", True) if puser else True
        if is_cal:
            rating_tag = f"`({sign}{change})`"
        else:
            calib_count = (puser.get("calibration_games_count", 0) if puser else 0) + 1
            dot = "🟢" if change >= 0 else "🔴"
            rating_tag  = f"`⏳ {min(calib_count, 8)}/8 {dot}`"
        lines.append(
            f"{place_emoji(p['place'], total)} {p.get('color_emoji','')} *{dname}*"
            f"  —  {fmt(p['total'])} очков  {rating_tag}"
        )

    result_text = "\n".join(lines)
    confirm_kb  = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Записать в рейтинг", callback_data=f"grecord_{admin_id}"),
            InlineKeyboardButton("❌ Отменить",           callback_data=f"gdiscard_{admin_id}"),
        ],
        [InlineKeyboardButton("✏️ Правка результатов",   callback_data=f"gresult_edit_{admin_id}")],
    ])

    sent = False
    try:
        await context.bot.edit_message_text(
            chat_id=data["board_chat_id"],
            message_id=data["board_message_id"],
            text=result_text,
            parse_mode="Markdown",
            reply_markup=confirm_kb,
        )
        sent = True
    except Exception as e:
        logger.warning("finish_game edit board: %s", e)

    # Если редактирование не удалось — шлём новым сообщением (всегда дойдёт)
    if not sent:
        try:
            await context.bot.send_message(
                chat_id=data["board_chat_id"],
                text=result_text,
                parse_mode="Markdown",
                reply_markup=confirm_kb,
            )
        except Exception as e2:
            logger.error("finish_game fallback send failed: %s", e2)
            # Последний резерв — отправить каждому игроку лично
            for p in sorted_p:
                try:
                    await context.bot.send_message(
                        chat_id=p["user_id"],
                        text=result_text,
                        parse_mode="Markdown",
                        reply_markup=confirm_kb,
                    )
                except Exception:
                    pass

    # ── Личные уведомления для всех не-хост игроков ──────────────────────────
    # Хост уже видит результат на доске; остальные получают персональное сообщение.
    board_id = data["board_chat_id"]
    for p in sorted_p:
        uid = p["user_id"]
        if uid == admin_id or uid == board_id:
            continue   # хост / совпадает с чатом доски — уже уведомлён
        dname = p.get("display_name") or _user_display_name(uid)
        if p["place"] == 1:
            header = f"🏆 *Поздравляем, {dname}! Ты победил в этой партии!*\n\n"
        elif p["place"] == 2:
            header = f"🥈 *Второе место, {dname}!* Совсем чуть-чуть не хватило!\n\n"
        elif p["place"] == 3:
            header = f"🥉 *Третье место, {dname}!*\n\n"
        else:
            header = (
                f"🎮 *Игра завершена, {dname}!*\n"
                f"Победил {winner['color_emoji']} *{winner_name}*\n\n"
            )
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=header + result_text,
                parse_mode="Markdown",
            )
        except Exception:
            pass


def _confirm_game_kb(admin_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Записать в рейтинг", callback_data=f"grecord_{admin_id}"),
            InlineKeyboardButton("❌ Отменить",           callback_data=f"gdiscard_{admin_id}"),
        ],
        [InlineKeyboardButton("✏️ Правка результатов",   callback_data=f"gresult_edit_{admin_id}")],
    ])


def _result_text(sorted_p: list, target: int) -> str:
    total = len(sorted_p)
    winner = sorted_p[0] if sorted_p else None
    lines: list[str] = []
    if winner:
        lines.append(_winner_celebration(
            winner["user_id"], winner.get("color_emoji", ""), winner["total"],
        ))
        lines.append("")
    lines.append(f"🏁 *Игра завершена!*  _(цель: {fmt(target)})_\n")
    for p in sorted_p:
        change = p["points_change"]
        sign   = "+" if change >= 0 else ""
        dname  = p.get("display_name") or _user_display_name(p["user_id"])
        puser  = db.get_user(p["user_id"])
        is_cal = puser.get("is_calibrated", True) if puser else True
        if is_cal:
            rating_tag = f"`({sign}{change})`"
        else:
            calib_count = (puser.get("calibration_games_count", 0) if puser else 0) + 1
            dot = "🟢" if change >= 0 else "🔴"
            rating_tag  = f"`⏳ {min(calib_count, 8)}/8 {dot}`"
        lines.append(
            f"{place_emoji(p['place'], total)} {p.get('color_emoji','')} *{dname}*"
            f"  —  {fmt(p['total'])} очков  {rating_tag}"
        )
    return "\n".join(lines)


async def cb_result_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gresult_edit_<admin_id> — редактор результатов перед записью."""
    query    = update.callback_query
    await query.answer()
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)
    if not is_admin(user_id):
        await query.answer("Только админ", show_alert=True); return

    results, target = _load_confirm_data(admin_id)
    if not results:
        await query.edit_message_text("❌ Данные игры не найдены.", reply_markup=back_admin_kb()); return

    total = len(results)
    lines = [f"✏️ *Правка результатов*  _(цель: {fmt(target)})_\n"]
    for p in results:
        lines.append(f"{place_emoji(p['place'], total)} *{p['username']}*: {fmt(p['total'])}")

    rows = []
    for p in results:
        rows.append([InlineKeyboardButton(
            f"✏️ {p['username']} ({fmt(p['total'])})",
            callback_data=f"grescore_{p['user_id']}_{admin_id}",
        )])
    rows.append([InlineKeyboardButton("◀ Назад к результатам", callback_data=f"gresback_{admin_id}")])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_result_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """grescore_<uid>_<admin_id> — кнопки +/- для конкретного игрока."""
    query    = update.callback_query
    await query.answer()
    user_id  = query.from_user.id
    if not is_admin(user_id):
        await query.answer("Только админ", show_alert=True); return

    parts    = query.data.split("_")   # grescore_<uid>_<admin_id>
    target_uid = int(parts[1])
    admin_id   = int(parts[2])

    results, target = _load_confirm_data(admin_id)
    if not results:
        await query.edit_message_text("❌ Данные не найдены."); return

    p = next((x for x in results if x["user_id"] == target_uid), None)
    if not p:
        await query.edit_message_text("❌ Игрок не найден."); return

    amounts   = [50, 100, 200, 500, 1000]
    plus_row  = [InlineKeyboardButton(f"+{fmt(a)}", callback_data=f"gresdelta_{target_uid}_{a}_{admin_id}")  for a in amounts]
    minus_row = [InlineKeyboardButton(f"-{fmt(a)}", callback_data=f"gresdelta_{target_uid}_{-a}_{admin_id}") for a in amounts]

    await query.edit_message_text(
        f"✏️ *{p['username']}* — текущий счёт: *{fmt(p['total'])}*\nВыбери поправку:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            plus_row, minus_row,
            [InlineKeyboardButton("◀ Назад", callback_data=f"gresult_edit_{admin_id}")],
        ]),
    )


async def cb_result_delta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gresdelta_<uid>_<delta>_<admin_id> — применяет поправку к счёту."""
    query    = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("Только админ", show_alert=True); return

    parts      = query.data.split("_")   # gresdelta_<uid>_<delta>_<admin_id>
    target_uid = int(parts[1])
    delta      = int(parts[2])
    admin_id   = int(parts[3])

    # Обновляем в pending_action
    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "confirm_game":
        await query.edit_message_text("❌ Сессия устарела."); return

    data    = gp["data"]
    results = data["results"]
    total_n = len(results)

    for p in results:
        if p["user_id"] == target_uid:
            p["total"] = max(0, p["total"] + delta)

    # Пересчитываем места и очки рейтинга
    results.sort(key=lambda x: x["total"], reverse=True)
    first_uid   = data.get("first_player_user_id")
    edit_target = data.get("target", 0)
    for rank, p in enumerate(results, 1):
        p["place"]         = rank
        p["points_change"] = rating_change_v2(rank, total_n, p["total"], edit_target)
        p["went_first"]    = (p["user_id"] == first_uid)

    data["results"] = results
    db.set_pending_action(admin_id, "confirm_game", data)
    # Обновляем резервную копию
    try:
        db.save_game_draft(admin_id, results, data.get("target", 0))
    except Exception:
        pass

    # Возвращаемся к редактору
    await query.edit_message_text(
        f"✏️ *Правка результатов*\n\nПрименено: `{'+' if delta >= 0 else ''}{fmt(delta)}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✏️ {p['username']} ({fmt(p['total'])})",
                                  callback_data=f"grescore_{p['user_id']}_{admin_id}")]
            for p in results
        ] + [[InlineKeyboardButton("◀ Назад к результатам", callback_data=f"gresback_{admin_id}")]]),
    )


async def cb_result_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """gresback_<admin_id> — возврат к экрану подтверждения."""
    query    = update.callback_query
    await query.answer()
    admin_id = _admin_id_from(query.data)

    results, target = _load_confirm_data(admin_id)
    if not results:
        await query.edit_message_text("❌ Данные не найдены.", reply_markup=back_admin_kb()); return

    await query.edit_message_text(
        _result_text(results, target),
        parse_mode="Markdown",
        reply_markup=_confirm_game_kb(admin_id),
    )


def _load_confirm_data(admin_id: int):
    """Загружает results и target из pending_action или game_draft."""
    gp = db.get_pending_action(admin_id)
    if gp and gp["action"] == "confirm_game":
        return gp["data"]["results"], gp["data"].get("target", 0)
    draft = db.get_game_draft(admin_id)
    if draft:
        return draft["results"], draft.get("target", 0)
    return None, None


async def cb_admin_recover_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """admin_recover_game — восстановить экран финала из резервной копии."""
    query    = update.callback_query
    await query.answer()
    user_id  = query.from_user.id
    if not is_admin(user_id):
        await query.answer("⛔ Доступ запрещён", show_alert=True); return

    results, target = _load_confirm_data(user_id)
    if not results:
        await query.edit_message_text(
            "❌ Незавершённой игры не найдено.\nНачните новую игру.",
            reply_markup=back_admin_kb(),
        )
        return

    # Восстанавливаем confirm_game если нет
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "confirm_game":
        db.set_pending_action(user_id, "confirm_game", {"results": results, "target": target})

    await query.edit_message_text(
        _result_text(results, target),
        parse_mode="Markdown",
        reply_markup=_confirm_game_kb(user_id),
    )


async def cb_record_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    user_id  = query.from_user.id
    admin_id = _admin_id_from(query.data)

    if user_id != admin_id and not is_admin(user_id):
        try:
            await query.edit_message_text(
                "⛔ Только администратор игры может записать результат.",
                reply_markup=_confirm_game_kb(admin_id),
            )
        except Exception:
            pass
        return

    # Пробуем получить данные из pending_action, иначе — из резервной таблицы
    results = None
    target  = None
    gp = db.get_pending_action(admin_id)
    if gp and gp["action"] == "confirm_game":
        results = gp["data"]["results"]
        target  = gp["data"].get("target")
    else:
        draft = db.get_game_draft(admin_id)
        if draft:
            results = draft["results"]
            target  = draft["target"]
            logger.info("cb_record_game: восстановлено из game_drafts для admin %s", admin_id)

    if not results:
        await query.edit_message_text(
            "❌ Данные игры не найдены ни в кэше, ни в резерве.\n"
            "Начните новую игру.",
            reply_markup=back_admin_kb(),
        )
        return

    total = len(results)
    try:
        db.record_game(
            total, admin_id,
            [
                (
                    p["user_id"], p["place"], p["total"], p["points_change"],
                    p.get("color_emoji", ""),
                    p.get("went_first", False),
                )
                for p in results
            ],
        )
    except Exception as exc:
        logger.error("cb_record_game: ошибка записи игры admin=%s: %s", admin_id, exc)
        await query.edit_message_text(
            f"❌ *Ошибка записи игры:*\n`{exc}`\n\nПопробуй ещё раз или обратись к суперадмину.",
            parse_mode="Markdown",
            reply_markup=_confirm_game_kb(admin_id),
        )
        return
    db.clear_pending_action(admin_id)
    db.delete_game_draft(admin_id)

    # ── Калибровка ────────────────────────────────────────────────────────────
    newly_calibrated: list[tuple[int, str, int]] = []  # (uid, username, final_rating)
    for p in results:
        uid  = p["user_id"]
        user = db.get_user(uid)
        if not user or user.get("is_calibrated", True):
            continue
        perf = min(p["total"] / target, 1.0) if target else 0.0
        db.update_calibration(uid, perf)
        user = db.get_user(uid)   # свежие данные после обновления
        calib_count = user.get("calibration_games_count", 0)
        if calib_count >= 8:
            avg_perf     = user.get("calibration_perf_sum", 0) / calib_count
            base_rating  = user.get("calibration_base_rating", 0)
            final_rating = round(avg_perf * 3000) + base_rating
            db.complete_calibration(uid, final_rating)
            newly_calibrated.append((uid, p["username"], final_rating, base_rating, round(avg_perf * 100)))
            await _check_achievements(uid, context, user=db.get_user(uid),
                                      game_score=p["total"], game_target=target)
            # Оповещаем игрока в личку
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=(
                        f"🎉 *Калибровка завершена!*\n\n"
                        f"Сыграно 8 калибровочных игр.\n"
                        f"Средняя производительность: *{round(avg_perf * 100)}%* от цели\n"
                        f"Калибровочный рейтинг: *{fmt(round(avg_perf * 3000))}*\n"
                        f"Базовый рейтинг (до калибровки): *{fmt(base_rating)}*\n"
                        f"Итоговый рейтинг: *{fmt(final_rating)}* очков"
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    # Проверяем достижения для каждого игрока
    for p in results:
        try:
            fresh_user = db.get_user(p["user_id"])
            await _check_achievements(
                p["user_id"], context,
                user=fresh_user,
                game_score=p["total"],
                game_target=target,
                game_place=p.get("place"),
                game_total_players=total,
                game_went_first=p.get("went_first", False),
            )
        except Exception as exc:
            logger.error("cb_record_game: ошибка достижений uid=%s: %s", p["user_id"], exc)

    # ── Начисляем монеты за игру ───────────────────────────────────────────
    coins_earned: dict[int, int] = {}
    for p in results:
        try:
            earned = _coins_for_game(p["place"], p["total"], target or 0, is_solo=False)
            if earned > 0:
                db.add_coins(p["user_id"], earned)
                coins_earned[p["user_id"]] = earned
        except Exception as exc:
            logger.error("cb_record_game: ошибка монет uid=%s: %s", p["user_id"], exc)

    lines = ["🎮 *Игра записана!*\n"]
    for p in results:
        change = p["points_change"]
        sign   = "+" if change >= 0 else ""
        user   = db.get_user(p["user_id"])
        is_cal      = user.get("is_calibrated", True) if user else True
        calib_count = user.get("calibration_games_count", 0) if user else 0
        dname  = p.get("display_name") or _user_display_name(p["user_id"], user=user)
        if is_cal:
            rating_tag = f"({sign}{change})"
        else:
            dot = "🟢" if change >= 0 else "🔴"
            rating_tag = f"⏳ {calib_count}/8 {dot}"
        coin_tag = f"  💰+{coins_earned.get(p['user_id'], 0)}" if coins_earned.get(p["user_id"]) else ""
        lines.append(
            f"{place_emoji(p['place'], total)} {p.get('color_emoji','')} {dname}"
            f"  —  {fmt(p['total'])} очков  {rating_tag}{coin_tag}"
        )
    if newly_calibrated:
        lines.append("")
        lines.append("✅ *Калибровка завершена:*")
        for uid, uname, frat, brat, avg_pct in newly_calibrated:
            lines.append(f"  • {uname}: {fmt(frat)} очков (ср. {avg_pct}% + база {fmt(brat)})")
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=back_admin_kb())


async def cb_discard_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    admin_id = _admin_id_from(query.data)
    db.clear_pending_action(admin_id)
    db.delete_game_draft(admin_id)
    await query.edit_message_text("⚙️ *Админ панель*", parse_mode="Markdown", reply_markup=admin_kb())


# ── Combo tracking ────────────────────────────────────────────────────────────

def _game_current_player(admin_id: int) -> tuple[dict | None, dict | None]:
    """Возвращает (game_data, current_player) или (None, None)."""
    gp = db.get_pending_action(admin_id)
    if not gp or gp["action"] != "in_game":
        return None, None
    data   = gp["data"]
    player = data["players"][data["current_idx"]]
    return data, player


async def cb_combo_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает главное меню категорий комбинаций."""
    query    = update.callback_query
    await query.answer()
    admin_id = _admin_id_from(query.data)

    data, player = _game_current_player(admin_id)
    if not player:
        await query.edit_message_text("❌ Игра не найдена."); return

    await query.edit_message_text(
        f"🎲 *Записать комбинацию*\n"
        f"_{player.get('color_emoji','')} {player['username']}_",
        parse_mode="Markdown",
        reply_markup=combo_cat_kb(admin_id),
    )


async def cb_combo_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает подкатегорию комбинаций."""
    query    = update.callback_query
    await query.answer()
    admin_id = _admin_id_from(query.data)
    sub      = query.data.split("_")[1]   # csub_<sub>_<admin_id>

    titles = {
        "straight": "📏 Стриты",
        "three":    "×3 Три одинаковых",
        "four":     "×4 Четыре одинаковых",
        "five":     "×5 Пять одинаковых",
        "six":      "×6 Шесть одинаковых",
        "ulta":     "🔥 Ульты",
        "specials": "🎲 Спец кубики",
    }
    await query.edit_message_text(
        f"*{titles.get(sub, sub)}:*",
        parse_mode="Markdown",
        reply_markup=combo_sub_kb(sub, admin_id),
    )


async def cb_combo_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фиксирует комбинацию и предлагает записать ещё."""
    query    = update.callback_query
    await query.answer()
    admin_id  = _admin_id_from(query.data)
    # combo_key — всё между "clog_" и "_{admin_id}"
    combo_key = query.data[5 : query.data.rfind("_")]

    data, player = _game_current_player(admin_id)
    if not player:
        await query.edit_message_text("❌ Игра не найдена."); return

    db.log_combo(player["user_id"], combo_key)
    combo_name = COMBOS.get(combo_key, combo_key)

    # Проверяем повышение уровня
    uid        = player["user_id"]
    new_count  = db.get_combo_count(uid)
    old_count  = new_count - 1
    await _check_achievements(uid, context)
    old_lvl    = _combo_level(old_count)
    new_lvl    = _combo_level(new_count)
    levelup_text = ""
    if new_lvl["level"] > old_lvl["level"]:
        badge = new_lvl["badge"]
        unlocks_desc = {
            "combo_count":      "📊 Счётчик комбо в профиле",
            "profile_badge":    "🎖 Значок уровня в профиле",
            "prefix1":          "✏️ Prefix-эмодзи ника (слот 1)",
            "top5_combos":      "📈 Топ-5 комбо в профиле",
            "leaderboard_badge":"🏅 Значок в таблице лидеров",
            "fav_combo":        "❤️ Любимое комбо в профиле",
            "first_combo":      "🕰 Первое записанное комбо",
            "suffix1":          "✏️ Suffix-эмодзи ника (слот 1)",
            "title":            "🏷 Кастомный титул",
            "rarity_stats":     "💎 Рейтинг редкости комбо",
            "prefix2":          "✏️ Prefix-эмодзи (слот 2)",
            "banner":           "🎨 Баннер профиля",
            "avg_per_game":     "📊 Среднее комбо за игру",
            "suffix2":          "✏️ Suffix-эмодзи (слот 2)",
            "about":            "📝 Строка «О себе»",
            "all_slots":        "🔓 Все слоты без ограничений",
            "global_stats":     "🌍 Глобальная статистика комбо",
            "divider":          "🎨 Кастомный разделитель профиля",
            "top_leaderboard":  "👑 Отдельная строка в топе лидеров",
        }
        new_unlocks = "\n".join(
            f"  • {unlocks_desc.get(u, u)}" for u in new_lvl["unlocks"]
        )
        unlock_section = f"\n\n🔓 *Разблокировано:*\n{new_unlocks}" if new_unlocks else ""
        levelup_text = (
            f"\n\n🎉 *Новый уровень!* {badge} *{new_lvl['name']}* (ур. {new_lvl['level']})"
            f"{unlock_section}"
        )
        # Пытаемся отправить отдельное сообщение игроку
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    f"🎉 *Новый уровень комбо!*\n\n"
                    f"{badge} *{new_lvl['name']}* — уровень {new_lvl['level']}\n"
                    f"Всего комбо: *{new_count}*"
                    f"{unlock_section}"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    await query.edit_message_text(
        f"✅ *Записано:* {combo_name}{levelup_text}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Ещё комбо",      callback_data=f"ccat_{admin_id}")],
            [InlineKeyboardButton("↩️ Отменить запись", callback_data=f"cundo_{admin_id}")],
            [InlineKeyboardButton("◀ К ходу",          callback_data=f"cclose_{admin_id}")],
        ]),
    )


async def cb_combo_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет последнюю записанную комбинацию текущего игрока."""
    query    = update.callback_query
    await query.answer()
    admin_id = _admin_id_from(query.data)

    data, player = _game_current_player(admin_id)
    if not player:
        await query.edit_message_text("❌ Игра не найдена."); return

    deleted = db.undo_last_combo(player["user_id"])
    if deleted:
        await query.edit_message_text(
            "↩️ *Последняя комбинация отменена.*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Записать комбо", callback_data=f"ccat_{admin_id}")],
                [InlineKeyboardButton("◀ К ходу",         callback_data=f"cclose_{admin_id}")],
            ]),
        )
    else:
        await query.answer("Нечего отменять!", show_alert=True)


async def cb_combo_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возвращает сообщение хода к нормальному виду."""
    query    = update.callback_query
    await query.answer()
    admin_id = _admin_id_from(query.data)

    data, player = _game_current_player(admin_id)
    if not player:
        try: await query.message.delete()
        except Exception: pass
        return

    ts = data.get("turn_score", 0)
    tc = data.get("turn_crosses", 0)
    players = data["players"]
    await query.edit_message_text(
        turn_message_text(player, data["target"], ts, turn_crosses=tc, all_players=players),
        parse_mode="Markdown",
        reply_markup=turn_message_kb(
            ts, admin_id,
            can_undo=bool(data.get("turn_history")),
            turn_crosses=tc,
            num_players=len(players),
            white_tokens=_white_tokens(player),
        ),
    )


# ── Admin: edit rating ────────────────────────────────────────────────────────

async def cb_admin_edit_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    db.set_pending_action(query.from_user.id, "edit_rating_name")
    await query.edit_message_text(
        "✏️ *Изменить рейтинг*\n\nВведите никнейм игрока (или /cancel):", parse_mode="Markdown",
    )


async def _handle_dbg_shop_edit(update: Update, user_id: int, text: str, data: dict):
    """Применяет ввод текста для редактирования поля товара в магазине."""
    if not _is_superadmin(user_id):
        return
    item_id = data.get("item_id")
    field   = data.get("field")
    if not item_id or not field:
        db.clear_pending_action(user_id)
        await update.message.reply_text("❌ Контекст потерян."); return

    raw = text.strip()
    if field == "price":
        try:
            value = int(raw.replace(" ", ""))
            if value < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Цена должна быть целым числом ≥ 0. Попробуй ещё раз или /cancel:"
            ); return
    elif field == "description":
        value = None if raw == "-" else raw[:200]
    else:
        # name / content — текст, лимит 100 символов
        limit = 100 if field == "content" else 60
        if len(raw) > limit:
            await update.message.reply_text(f"❌ Слишком длинно (макс. {limit}). Попробуй ещё раз:"); return
        if not raw:
            await update.message.reply_text("❌ Пусто. Попробуй ещё раз:"); return
        value = raw

    ok = db.update_shop_item(item_id, **{field: value})
    db.clear_pending_action(user_id)
    if ok:
        item = db.get_shop_item(item_id)
        slot = item["slot_type"] if item else None
        await update.message.reply_text(
            f"✅ Обновлено поле «{field}» у #{item_id}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀ К предмету",
                                     callback_data=f"dbg_shop_item_{item_id}"),
                InlineKeyboardButton("📋 К списку",
                                     callback_data=f"dbg_shop_list_{slot}" if slot else "dbg_shop_menu"),
            ]]),
        )
    else:
        await update.message.reply_text("❌ Ошибка обновления.")


async def _handle_dbg_shop_add(update: Update, user_id: int, text: str, data: dict):
    """Добавляет новый предмет магазина из текста: 'content|name|price|rarity[|description]'."""
    if not _is_superadmin(user_id):
        return
    slot = data.get("slot")
    if not slot:
        db.clear_pending_action(user_id)
        await update.message.reply_text("❌ Контекст потерян."); return

    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 4:
        await update.message.reply_text(
            "❌ Нужно минимум 4 поля через `|`:\n"
            "`контент | название | цена | редкость [| описание]`",
            parse_mode="Markdown",
        ); return

    content, name, price_s, rarity = parts[0], parts[1], parts[2], parts[3].lower()
    description = parts[4] if len(parts) >= 5 and parts[4] else None

    if not content or not name:
        await update.message.reply_text("❌ Контент и название не могут быть пустыми."); return
    try:
        price = int(price_s.replace(" ", ""))
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Цена должна быть числом ≥ 0."); return
    if rarity not in _DBG_RARITY_OPTIONS:
        await update.message.reply_text(
            f"❌ Редкость должна быть одной из: {', '.join(_DBG_RARITY_OPTIONS)}"
        ); return

    new_id = db.insert_shop_item(slot, content, name, price, rarity, description)
    db.clear_pending_action(user_id)
    if new_id:
        await update.message.reply_text(
            f"✅ Добавлен #{new_id}: `{content}` — *{name}* ({price} 💰, {rarity})",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👁 Открыть", callback_data=f"dbg_shop_item_{new_id}"),
                InlineKeyboardButton("📋 К списку", callback_data=f"dbg_shop_list_{slot}"),
            ]]),
        )
    else:
        await update.message.reply_text(
            "❌ Ошибка добавления. Возможно, такой content в этом слоте уже есть."
        )


async def _handle_edit_name(update: Update, user_id: int, text: str):
    u = db.get_user_by_username(text)
    if not u:
        await update.message.reply_text(f"Игрок *{text}* не найден. Попробуйте ещё раз (или /cancel):", parse_mode="Markdown"); return
    db.set_pending_action(user_id, "edit_rating_value",
                          {"user_id": u["user_id"], "username": u["username"], "old_rating": u["rating"]})
    await update.message.reply_text(
        f"Текущий рейтинг *{u['username']}*: {fmt(u['rating'])}\n\nВведите новое значение:", parse_mode="Markdown",
    )


async def _handle_edit_value(update: Update, user_id: int, text: str, data: dict):
    try:
        new_rating = int(text.replace(" ", "").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("Введите целое число:"); return
    db.update_rating(data["user_id"], new_rating)
    db.clear_pending_action(user_id)
    await update.message.reply_text(
        f"✅ Рейтинг *{data['username']}*: {fmt(data['old_rating'])} → *{fmt(new_rating)}*",
        parse_mode="Markdown", reply_markup=back_admin_kb(),
    )


# ── Admin: delete player ──────────────────────────────────────────────────────

async def cb_admin_delete_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    db.set_pending_action(query.from_user.id, "delete_player")
    await query.edit_message_text("🗑️ *Удалить игрока*\n\nВведите никнейм (или /cancel):", parse_mode="Markdown")


async def _handle_delete_name(update: Update, user_id: int, text: str):
    u = db.get_user_by_username(text)
    if not u:
        await update.message.reply_text("Игрок не найден. Попробуйте ещё раз (или /cancel):"); return
    if u["user_id"] in config.SUPERADMIN_IDS:
        await update.message.reply_text("❌ Нельзя удалить суперадмина."); return
    db.set_pending_action(user_id, "confirm_delete",
                          {"user_id": u["user_id"], "username": u["username"], "rating": u["rating"]})
    await update.message.reply_text(
        f"Удалить *{u['username']}* (рейтинг: {fmt(u['rating'])})? Это необратимо.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Удалить", callback_data="confirm_delete"),
            InlineKeyboardButton("❌ Отмена",  callback_data="cancel_to_admin"),
        ]]),
    )


async def cb_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    pending = db.get_pending_action(user_id)
    if not pending or pending["action"] != "confirm_delete":
        await query.edit_message_text("❌ Данные потеряны.", reply_markup=back_admin_kb()); return
    target = pending["data"]
    db.delete_user(target["user_id"])
    db.clear_pending_action(user_id)
    await query.edit_message_text(
        f"✅ Игрок *{target['username']}* удалён.", parse_mode="Markdown", reply_markup=back_admin_kb(),
    )


# ── Menu callbacks ────────────────────────────────────────────────────────────

async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    user   = db.get_user(uid)
    dname  = _user_display_name(uid, user=user)
    await query.edit_message_text(
        f"👋 Главное меню, *{dname}*",
        parse_mode="Markdown", reply_markup=main_kb(is_admin(uid)),
    )


def _leaderboard_text(board: list, combo_counts: dict) -> str:
    """Строит текст таблицы лидеров с калибровкой и комбо-значками."""
    if not board:
        return "Рейтинговая таблица пуста."

    lines = ["🏆 *Рейтинговая таблица*\n"]

    top_lb_rows = []
    regular_rows = []
    for row in board:
        uid = row.get("user_id", 0)
        cc  = combo_counts.get(uid, 0)
        if _combo_level(cc)["level"] >= 19:
            top_lb_rows.append(row)
        else:
            regular_rows.append(row)

    if top_lb_rows:
        lines.append("👑 *Легенды комбо:*")
        for row in top_lb_rows:
            uid       = row.get("user_id", 0)
            cc        = combo_counts.get(uid, 0)
            clvl      = _combo_level(cc)
            badge     = clvl["badge"]
            dname     = _combo_display_name(row, clvl["level"])
            calib_tag = _calib_tag(row)
            lines.append(
                f"{badge} *{dname}*{calib_tag}  —  {fmt(row['rating'])} очков"
                f"  ({row['games_played']} игр, {row['wins']} побед)"
            )
        lines.append("──────────")

    for i, row in enumerate(regular_rows, 1):
        medal     = MEDALS[i - 1] if i <= 3 else f"{i}."
        uid       = row.get("user_id", 0)
        cc        = combo_counts.get(uid, 0)
        clvl      = _combo_level(cc)
        badge     = clvl["badge"] if clvl["level"] >= 6 else ""
        dname     = _combo_display_name(row, clvl["level"])
        display   = f"{badge} {dname}" if badge else dname
        calib_tag = _calib_tag(row)
        lines.append(
            f"{medal} *{display}*{calib_tag}  —  {fmt(row['rating'])} очков"
            f"  ({row['games_played']} игр, {row['wins']} побед)"
        )
    return "\n".join(lines)


def _calib_tag(row: dict) -> str:
    """Возвращает ' ⏳ X/8' для некалиброванных игроков, иначе ''."""
    if row.get("is_calibrated", True):
        return ""
    count = row.get("calibration_games_count", 0)
    return f" ⏳ {count}/8"


async def cb_show_ratings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    board        = db.get_leaderboard()
    combo_counts = db.get_all_combo_counts()
    text         = _leaderboard_text(board, combo_counts)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_main_kb())


def _plural(n: int, one: str, two: str, five: str) -> str:
    """Русские окончания: 1 победа, 2 победы, 5 побед."""
    n = abs(n) % 100
    if 11 <= n <= 19: return five
    n %= 10
    if n == 1: return one
    if 2 <= n <= 4: return two
    return five


def _profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Мои комбо",          callback_data="show_combos")],
        [InlineKeyboardButton("🏅 Уровни комбо",        callback_data="show_combo_levels")],
        [
            InlineKeyboardButton("🏆 Достижения",       callback_data="show_achievements"),
            InlineKeyboardButton("📊 Статистика",       callback_data="show_ext_stats"),
        ],
        [InlineKeyboardButton("📜 История игр",         callback_data="show_history")],
        [InlineKeyboardButton("⚔️ Сравнить с игроком",  callback_data="compare_start")],
        [InlineKeyboardButton("🔥 Топ комбо (все)",     callback_data="show_global_combos")],
        [InlineKeyboardButton("🎨 Кастомизация",        callback_data="combo_customize")],
        [InlineKeyboardButton("💸 Перевести монеты",    callback_data="transfer_start")],
        [InlineKeyboardButton("◀ Назад",                callback_data="main_menu")],
    ])


def _build_profile_text(user: dict, uid: int, board: list, stats: dict, avg: dict, streak: dict) -> str:
    """Builds profile text with combo level info."""
    combo_count = db.get_combo_count(uid)
    lvl         = _combo_level(combo_count)
    level_n     = lvl["level"]
    display_name = _combo_display_name(user, level_n)

    rank    = next((i + 1 for i, u in enumerate(board) if u["username"] == user["username"]), "?")
    games   = user["games_played"]
    wins    = user["wins"]
    losses  = games - wins
    winrate = round(wins / games * 100) if games else 0
    first_n = stats["first_count"]

    # Стрик
    if streak["count"] >= 2:
        streak_emoji = "🔥" if streak["type"] == "win" else "💀"
        streak_label = "побед подряд" if streak["type"] == "win" else "поражений подряд"
        streak_line  = f"{streak_emoji} Серия: *{streak['count']}* {streak_label}"
    else:
        streak_line = None

    # Divider — отображаем если слот заполнен (через ачивку или магазин)
    if user.get("combo_divider"):
        div = user["combo_divider"]
        divider_line = div * 8
    else:
        divider_line = "──────────"

    # Калибровка
    is_calibrated   = user.get("is_calibrated", True)
    calib_count     = user.get("calibration_games_count", 0)
    rating_line     = f"📊 Рейтинг: *{fmt(user['rating'])}* очков"
    if not is_calibrated:
        rating_line += f"  ⏳ Калибруется ({calib_count}/8)"

    coins_balance = int(user.get("coins", 0))
    lines = [
        f"👤 *{display_name}*\n",
        rating_line,
        f"💰 Монеты: *{fmt(coins_balance)}*",
        f"🏆 Место в таблице: *#{rank}*",
        f"🎮 Игр сыграно: *{games}*",
        f"🟢 *{wins}W* — 🔴 *{losses}L*  ({winrate}%)",
        f"🎲 Начинал первым: *{first_n}* {_plural(first_n, 'раз', 'раза', 'раз')}",
    ]
    if avg["count"]:
        lines.append(
            f"📈 Среднее: место *{avg['avg_place']}*, счёт *{fmt(avg['avg_score'])}*"
        )
    if streak_line:
        lines.append(streak_line)

    ach_count = len(db.get_user_achievements(uid))
    if ach_count > 0:
        lines.append(f"🏅 Достижений: *{ach_count}* / {len(ACHIEVEMENTS)}")

    color_wins = stats["color_wins"]
    if color_wins:
        lines.append("")
        lines.append("🏅 *Победы по фракциям:*")
        for emoji, w in sorted(color_wins.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  {emoji}  — {w} {_plural(w, 'победа', 'победы', 'побед')}")

    # Combo level section (shown if level >= 1 which unlocks combo_count)
    if _has_unlock(level_n, "combo_count"):
        next_lvl = _combo_next_level(combo_count)
        badge    = lvl["badge"]
        lines.append("")
        lines.append(divider_line)
        lines.append(f"🎲 *Комбо:* {badge} {lvl['name']} (ур. {level_n}) — *{combo_count}* записей")
        if next_lvl:
            needed = next_lvl["threshold"] - combo_count
            lines.append(f"  До ур. {next_lvl['level']} ({next_lvl['name']}): ещё *{needed}*")

    # About section (level 15+)
    if _has_unlock(level_n, "about") and user.get("combo_about"):
        lines.append(f"📝 _{user['combo_about']}_")

    # Title (level 10+) — insert after username line
    if _has_unlock(level_n, "title") and user.get("combo_title"):
        lines.insert(1, f"_{user['combo_title']}_")

    # Banner (level 13+) — prepend
    if _has_unlock(level_n, "banner") and user.get("combo_banner"):
        banner_line = f"{'━' * 3} {user['combo_banner']} {'━' * 3}"
        lines = [banner_line] + lines

    return "\n".join(lines)


def _calibration_notice(user: dict) -> str | None:
    """Возвращает текст уведомления о калибровке если игрок ещё не видел его, иначе None."""
    if user.get("is_calibrated", True):
        return None
    if user.get("calibration_notified", True):
        return None
    base = user.get("calibration_base_rating", 0)
    count = user.get("calibration_games_count", 0)
    base_note = f"\nТвой базовый рейтинг *{fmt(base)}* будет добавлен к результату." if base else ""
    return (
        "🔔 *Запущена система калибровки!*\n\n"
        "Новая рейтинговая система требует 10 игр для точного определения уровня. "
        f"Сыграно: *{count}/10*.{base_note}\n\n"
        "Рейтинг после калибровки = avg\\_score% × 3000 + базовый рейтинг\n"
        "─────────────────────\n"
    )


async def cb_show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    user    = db.get_user(uid)
    board   = db.get_leaderboard()
    stats   = db.get_player_stats(uid)
    avg     = db.get_avg_stats(uid)
    streak  = db.get_player_streak(uid)

    notice = _calibration_notice(user)
    if notice:
        db.mark_calibration_notified(uid)

    text = _build_profile_text(user, uid, board, stats, avg, streak)
    if notice:
        text = notice + text
    await query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=_profile_kb(),
    )


async def cb_show_combo_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экран статистики комбинаций игрока."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    user  = db.get_user(uid)
    combo_stats = db.get_combo_stats(uid)

    if not combo_stats:
        text = f"🎲 *Статистика комбо — {user['username']}*\n\n_Ещё нет записанных комбинаций._"
    else:
        lines = [f"🎲 *Статистика комбо — {user['username']}*\n"]
        for cs in combo_stats:
            name  = COMBOS.get(cs["combo_key"], cs["combo_key"])
            count = cs["count"]
            lines.append(f"`{name}` — {count} {_plural(count, 'раз', 'раза', 'раз')}")
        text = "\n".join(lines)

    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀ Назад к профилю", callback_data="show_profile"),
        ]]),
    )


async def cb_show_combo_levels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экран прогресса всех 20 уровней комбо."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    count = db.get_combo_count(uid)
    lvl   = _combo_level(count)
    level_n = lvl["level"]

    # Описание того что даёт каждый unlock
    UNLOCK_LABELS: dict[str, str] = {
        "combo_count":       "📊 Счётчик комбо в профиле",
        "profile_badge":     "🎖 Значок уровня в профиле",
        "prefix1":           "✏️ Prefix-эмодзи к нику (слот 1)",
        "top5_combos":       "📈 Топ-5 комбо в профиле",
        "leaderboard_badge": "🏅 Значок в таблице лидеров",
        "fav_combo":         "❤️ Любимое комбо в профиле",
        "first_combo":       "🕰 Первое записанное комбо",
        "suffix1":           "✏️ Suffix-эмодзи к нику (слот 1)",
        "title":             "🏷 Кастомный титул",
        "rarity_stats":      "💎 Статистика редкости комбо",
        "prefix2":           "✏️ Prefix-эмодзи к нику (слот 2)",
        "banner":            "🎨 Баннер профиля",
        "avg_per_game":      "📊 Среднее комбо за игру",
        "suffix2":           "✏️ Suffix-эмодзи к нику (слот 2)",
        "about":             "📝 Строка «О себе»",
        "all_slots":         "🔓 Все слоты без ограничений",
        "global_stats":      "🌍 Глобальная статистика комбо",
        "divider":           "🎨 Кастомный разделитель профиля",
        "top_leaderboard":   "👑 Отдельная строка в топе лидеров",
    }

    next_lvl = _combo_next_level(count)
    # Прогресс-бар к следующему уровню
    if next_lvl:
        prev_thr = lvl["threshold"]
        span     = next_lvl["threshold"] - prev_thr
        done     = count - prev_thr
        pct      = done / span if span else 1.0
        filled   = round(pct * 10)
        bar      = "█" * filled + "░" * (10 - filled)
        progress_line = (
            f"\n📈 Прогресс до ур.{next_lvl['level']}: "
            f"`[{bar}]` {done}/{span}\n"
        )
    else:
        progress_line = "\n🌌 *Максимальный уровень достигнут!*\n"

    lines = [
        f"🏅 *Уровни комбо* — всего записей: *{count}*",
        f"Текущий: {lvl['badge']} *{lvl['name']}* (ур. {level_n})",
        progress_line,
    ]

    for entry in COMBO_LEVELS:
        n        = entry["level"]
        badge    = entry["badge"]
        name     = entry["name"]
        thr      = entry["threshold"]
        unlocks  = entry["unlocks"]

        if n < level_n:
            # Пройденный уровень
            status = "✅"
        elif n == level_n:
            # Текущий уровень
            status = "▶️"
        else:
            # Будущий уровень
            status = "🔒"

        unlock_str = ""
        if unlocks:
            descs = [UNLOCK_LABELS.get(u, u) for u in unlocks]
            unlock_str = "\n    " + "\n    ".join(f"→ {d}" for d in descs)

        thr_str = f"{fmt(thr)} комбо" if thr > 0 else "старт"
        lines.append(
            f"{status} *Ур.{n}* {badge} {name}  _({thr_str})_{unlock_str}"
        )

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀ Назад к профилю", callback_data="show_profile"),
        ]]),
    )


async def cb_show_achievements(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экран всех достижений игрока."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    earned = set(db.get_user_achievements(uid))

    CAT_NAMES = {
        "games":     "🎮 Игры",
        "wins":      "🏆 Победы",
        "streaks":   "🔥 Серии",
        "colors":    "🎨 Цвета фракций",
        "combos":    "🎲 Комбо",
        "special":   "✨ Особые",
        "hidden":    "🔮 Скрытые",
        "legendary": "👑 Легендарные",
    }

    # Скрытые ачивки не учитываем в счётчике пока не получены
    total_visible = sum(
        1 for a in ACHIEVEMENTS.values()
        if not a.get("hidden", False)
    )
    hidden_earned = sum(
        1 for k, a in ACHIEVEMENTS.items()
        if a.get("hidden", False) and k in earned
    )
    hidden_total = sum(1 for a in ACHIEVEMENTS.values() if a.get("hidden", False))
    done_visible = sum(
        1 for k, a in ACHIEVEMENTS.items()
        if not a.get("hidden", False) and k in earned
    )

    lines = [
        f"🏅 *Достижения* — {done_visible}/{total_visible}",
        f"🔮 Скрытые: {hidden_earned}/{hidden_total} раскрыто",
        "",
    ]

    by_cat: dict[str, list] = {}
    for ach_id, a in ACHIEVEMENTS.items():
        cat = a.get("cat", "special")
        by_cat.setdefault(cat, []).append((ach_id, a))

    for cat_key in ["games", "wins", "streaks", "colors", "combos", "special",
                    "legendary", "hidden"]:
        items = by_cat.get(cat_key, [])
        if not items:
            continue
        lines.append(f"\n*{CAT_NAMES.get(cat_key, cat_key)}*")
        for ach_id, a in items:
            is_earned = ach_id in earned
            is_hidden = a.get("hidden", False)

            # Скрытая + не получена → показываем как «???»
            if is_hidden and not is_earned:
                lines.append("  ❓ *???* — _выполни секретное условие_")
                continue

            mark = f"✅ {a['emoji']}" if is_earned else "🔒"
            rtype = a.get("reward_type", "")
            rval  = a.get("reward_value", "")
            reward_str = {
                "prefix":      f"prefix `{rval}`",
                "suffix":      f"suffix `{rval}`",
                "title":       f"титул «{rval}»",
                "banner":      f"баннер `{rval}`",
                "divider":     f"разделитель `{rval}`",
                "color_frame": f"рамка `{rval}`",
                "bank_fx":     f"банк-эффект `{rval}`",
                "win_message": f"слова победы «{rval}»",
            }.get(rtype, rval)
            lines.append(f"  {mark} *{a['name']}* — {a['desc']}\n      🎁 {reward_str}")

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀ Назад к профилю", callback_data="show_profile"),
        ]]),
    )


async def cb_show_extended_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экран расширенной статистики."""
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    user = db.get_user(uid)
    if not user:
        await query.answer("Игрок не найден.", show_alert=True); return

    bw      = db.get_best_worst_score(uid)
    by_n    = db.get_wins_by_player_count(uid)
    delta   = db.get_total_rating_delta(uid)
    colors  = db.get_color_play_counts(uid)
    avg     = db.get_avg_stats(uid)

    lines = [f"📊 *Подробная статистика — {user['username']}*\n"]

    # Лучшая / худшая игра
    if bw["best"] is not None:
        lines.append(f"🏆 Лучшая игра: *{fmt(bw['best'])}* очков")
        lines.append(f"💀 Худшая игра: *{fmt(bw['worst'])}* очков")

    # Суммарный рейтинг-дельта
    sign = "+" if delta >= 0 else ""
    lines.append(f"📈 Суммарно к рейтингу: *{sign}{fmt(delta)}*")

    # Среднее
    if avg["count"]:
        lines.append(f"📉 Среднее место: *{avg['avg_place']}*, средний счёт: *{fmt(avg['avg_score'])}*")

    # Винрейт по формату
    wins_map  = by_n.get("wins", {})
    games_map = by_n.get("games", {})
    if games_map:
        lines.append("\n🎮 *Винрейт по формату:*")
        for n in sorted(games_map):
            g = games_map[n]
            w = wins_map.get(n, 0)
            pct = round(w / g * 100) if g else 0
            lines.append(f"  {n} игрока: {w}/{g} ({pct}%)")

    # Самый частый цвет
    if colors:
        top_color = max(colors, key=lambda k: colors[k])
        lines.append(f"\n🎨 Любимый цвет: {top_color} — {colors[top_color]} {_plural(colors[top_color], 'игра', 'игры', 'игр')}")
        for emoji, cnt in sorted(colors.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  {emoji} — {cnt} {_plural(cnt, 'игра', 'игры', 'игр')}")

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀ Назад к профилю", callback_data="show_profile"),
        ]]),
    )


async def cb_ach_equip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ach_equip_<type>_<value> — быстрое надевание награды из достижения."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    # data format: ach_equip_prefix_🔥  or  ach_equip_title_Победитель
    parts  = query.data.split("_", 3)  # ['ach', 'equip', type, value]
    if len(parts) < 4:
        return
    rtype = parts[2]
    rval  = parts[3]

    # Map type to DB field (use slot 1 by default)
    field_map = {
        "prefix":  "combo_prefix1",
        "suffix":  "combo_suffix1",
        "title":   "combo_title",
        "banner":  "combo_banner",
        "divider": "combo_divider",
    }
    field = field_map.get(rtype)
    if not field:
        return
    db.update_combo_customization(uid, **{field: rval})
    await query.answer(f"✅ Надет: {rval}", show_alert=False)
    # Re-show customize menu
    await cb_combo_customize(update, context)


async def cb_show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Последние 10 игр игрока."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    user  = db.get_user(uid)
    rows  = db.get_game_history(uid, limit=10)

    back = InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад к профилю", callback_data="show_profile")]])

    if not rows:
        await query.edit_message_text(
            f"📜 *История игр — {user['username']}*\n\n_Ещё нет сыгранных партий._",
            parse_mode="Markdown", reply_markup=back,
        )
        return

    # Батч-запрос соперников для всех игр сразу
    game_ids   = [r["game_id"] for r in rows]
    co_players = db.get_game_co_players(game_ids, exclude_uid=uid)

    lines = [f"📜 *История игр — {user['username']}*\n"]
    for r in rows:
        place    = r["place"]
        score    = r["score"]
        color    = r.get("color") or ""
        pts      = r.get("points_change")
        meta     = r.get("games") or {}
        total_pl = meta.get("total_players", "?")
        date_str = (meta.get("played_at") or "")[:10]   # YYYY-MM-DD

        # Место
        place_str = place_emoji(place, total_pl if isinstance(total_pl, int) else 99)

        # Изменение рейтинга
        if pts is not None:
            sign    = "+" if pts >= 0 else ""
            pts_str = f"  `({sign}{pts})`"
        else:
            pts_str = ""

        # Строка партии
        lines.append(f"{place_str} {color}  *{fmt(score)}* очков{pts_str}  _{date_str}_")

        # Соперники
        others = co_players.get(r["game_id"], [])
        if others:
            parts = []
            for o in others:
                op_emoji = place_emoji(o["place"], total_pl if isinstance(total_pl, int) else 99)
                parts.append(f"{op_emoji}{o['color']} {o['username']}")
            lines.append(f"   └ {' · '.join(parts)}")

    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=back,
    )


async def cb_compare_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает никнейм для сравнения."""
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    db.set_pending_action(user_id, "compare_waiting", {})
    await query.edit_message_text(
        "⚔️ *Сравнение игроков*\n\nВведите никнейм игрока, с которым хотите сравниться:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="show_profile"),
        ]]),
    )


async def _do_compare(update: Update, uid1: int, username2: str):
    """Показывает h2h статистику двух игроков."""
    user1  = db.get_user(uid1)
    user2  = db.get_user_by_username(username2)
    db.clear_pending_action(uid1)
    back   = InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад к профилю", callback_data="show_profile")]])

    if not user2:
        await update.message.reply_text(
            f"❌ Игрок *{username2}* не найден.", parse_mode="Markdown", reply_markup=back,
        )
        return
    if user2["user_id"] == uid1:
        await update.message.reply_text(
            "❌ Нельзя сравнивать себя с собой 😄", reply_markup=back,
        )
        return

    h2h = db.get_head_to_head(uid1, user2["user_id"])
    n   = h2h["total"]
    if n == 0:
        text = (
            f"⚔️ *{user1['username']}* vs *{user2['username']}*\n\n"
            "_Совместных игр ещё не было._"
        )
    else:
        w1, w2 = h2h["uid1_wins"], h2h["uid2_wins"]
        text = (
            f"⚔️ *{user1['username']}* vs *{user2['username']}*\n\n"
            f"🎮 Совместных игр: *{n}*\n\n"
            f"🟢 *{user1['username']}* финишировал выше: *{w1}* раз\n"
            f"🔴 *{user2['username']}* финишировал выше: *{w2}* раз"
        )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=back)


async def cb_show_global_combos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Топ-10 комбинаций среди всех игроков."""
    query = update.callback_query
    await query.answer()
    stats = db.get_global_combo_stats(limit=10)
    back  = InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад к профилю", callback_data="show_profile")]])

    if not stats:
        await query.edit_message_text(
            "🔥 *Топ комбо*\n\n_Ещё нет записанных комбинаций._",
            parse_mode="Markdown", reply_markup=back,
        )
        return

    lines = ["🔥 *Топ комбо — все игроки*\n"]
    medals = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
    for i, cs in enumerate(stats):
        name  = COMBOS.get(cs["combo_key"], cs["combo_key"])
        count = cs["count"]
        lines.append(f"{medals[i]} `{name}` — {count} {_plural(count, 'раз', 'раза', 'раз')}")

    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=back,
    )


async def cmd_vs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/vs Nickname — сравнить с игроком напрямую."""
    uid  = update.effective_user.id
    user = db.get_user(uid)
    if not user:
        await update.message.reply_text("Нажмите /start для начала.")
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚔️ Использование: `/vs Никнейм`\nНапример: `/vs Иван`",
            parse_mode="Markdown",
        )
        return
    await _do_compare(update, uid, args[0])


# ══════════════════════════════════════════════════════════════════════════════
#  SOLO MODE (Фаза 2A: цветные кубики + базовые цветные бонусы)
# ══════════════════════════════════════════════════════════════════════════════

DICE_EMOJI = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣"}


def _die_label(die: dict) -> str:
    """Отображение одного кубика. Нейтральный: '5️⃣'. Цветной: '🟥5️⃣'. Спец: '🃏6️⃣'."""
    val = die["value"]
    num = DICE_EMOJI[val]
    special = die.get("special")
    if special:
        info = SPECIAL_INFO.get(special, {})
        sp_em = info.get("emoji", "?")
        face  = info.get("face")
        # Рубен: показываем цвет грани
        if special == "ruben":
            rf = RUBEN_FACTION.get(val)
            if rf:
                return f"{sp_em}{COLOR_SQUARE.get(rf, '')}{num}"
        # Спец грань активна → помечаем ✨
        if face and val == face:
            return f"✨{sp_em}{num}"
        return f"{sp_em}{num}"
    faction = die.get("faction")
    if faction:
        return f"{COLOR_SQUARE.get(faction, '')}{num}"
    return num


def _format_dice(dice: list[dict], selected: list[int] | None = None) -> str:
    sel = set(selected or [])
    return " ".join(
        f"*[{_die_label(d)}]*" if i in sel else _die_label(d)
        for i, d in enumerate(dice)
    )


def _scoring_dice(dice: list[dict]) -> list[dict]:
    """Преобразует игровые кубики в формат для движка очков."""
    result = []
    for d in dice:
        special = d.get("special")
        if special == "ruben":
            # Рубен: цвет зависит от значения (одиночный бонус, не ульта)
            color = RUBEN_FACTION.get(d["value"])
        else:
            color = d.get("faction")
        result.append({"value": d["value"], "color": color})
    return result


def _make_dice(neutrals: list[int], color_vals: list[int],
               color_emoji: str, faction: str) -> list[dict]:
    """Собирает броску из введённых нейтральных и цветных значений."""
    dice = [{"value": v, "color_emoji": "", "faction": None} for v in neutrals]
    dice += [
        {"value": v, "color_emoji": color_emoji, "faction": faction}
        for v in color_vals
    ]
    return dice


def _build_special_dice(vals_keys: list[tuple[int, str]]) -> list[dict]:
    """Строит dict-кубики из [(value, special_key), ...]."""
    result = []
    for val, skey in vals_keys:
        faction = RUBEN_FACTION.get(val) if skey == "ruben" else None
        result.append({
            "value":       val,
            "color_emoji": "",
            "faction":     faction,
            "special":     skey,
        })
    return result


def _roll_comeback_die() -> int:
    """Камбек-кубик: 4 грани = 1️⃣, 6 граней = 5️⃣."""
    return random.choice([1, 1, 1, 1, 5, 5, 5, 5, 5, 5])


def _bot_roll(neutral_count: int, color_count: int,
              color_emoji: str, faction: str,
              bot_reserve: list[str] | None = None,
              comeback: bool = False) -> list[dict]:
    """Кидает броску за бота с учётом его цветных и спец-кубиков."""
    dice = [
        {"value": random.randint(1, 6), "color_emoji": "", "faction": None}
        for _ in range(neutral_count)
    ]
    dice += [
        {"value": random.randint(1, 6), "color_emoji": color_emoji, "faction": faction}
        for _ in range(color_count)
    ]
    # Спец-кубики из резерва бота
    for key in (bot_reserve or []):
        info  = SPECIAL_INFO.get(key, {})
        face  = info.get("face")
        v     = random.randint(1, 6)
        # Джокер: если выпала спец-грань — бот всегда ставит 6 (максимум)
        if key.startswith("joker") and v == face:
            v = 6
        f_val = RUBEN_FACTION.get(v) if key == "ruben" else None
        dice.append({
            "value":       v,
            "color_emoji": "",
            "faction":     f_val,
            "special":     key,
        })
    # Камбек-кубик (если бот отстаёт на 10k+)
    if comeback:
        v = _roll_comeback_die()
        dice.append({"value": v, "color_emoji": "", "faction": None, "comeback": True})
    random.shuffle(dice)
    return dice


def _bot_shop_pick(options: list[str]) -> str:
    """Бот выбирает лучший вариант из доступных спец-кубиков."""
    priority = [
        "thief", "robbery", "joker_6", "tripling",
        "cross", "freeze_6", "reroll_6",
        "joker_5", "ruben", "reroll_5", "freeze_1",
    ]
    for p in priority:
        if p in options:
            return p
    return options[0] if options else "joker_6"


# ── Phase 2B: faction ability helpers ─────────────────────────────────────────

def _bust_has_blue_save(roll: list[dict]) -> bool:
    """Синяя 2 в броске → спасение от бюста (фракция blue)."""
    return any(d.get("faction") == "blue" and d["value"] == 2 for d in roll)


def _green_3_indices(roll: list[dict], excluded: list[int] | None = None) -> list[int]:
    """Индексы зелёных 3 в броске, не входящих в excluded (выбранные кубики)."""
    excl = set(excluded or [])
    return [
        i for i, d in enumerate(roll)
        if d.get("faction") == "green" and d["value"] == 3 and i not in excl
    ]


def _has_purple_246(selected_dice: list[dict]) -> bool:
    """True если в выделении есть фиолетовая 6 + любая 2 + любая 4 (доп. ход 🟣)."""
    return (
        any(d.get("faction") == "purple" and d["value"] == 6 for d in selected_dice)
        and any(d["value"] == 2 for d in selected_dice)
        and any(d["value"] == 4 for d in selected_dice)
    )


def _count_white_1(selected_dice: list[dict]) -> int:
    """Кол-во белых 1 в выделении (начисление жетонов ⚪)."""
    return sum(1 for d in selected_dice if d.get("faction") == "white" and d["value"] == 1)


# ── Ulta / super-game data ────────────────────────────────────────────────────

# Условия победы в супер-игре: (success_values, reroll_values)
ULTA_SUPERGAME: dict[str, tuple[list[int], list[int]]] = {
    "red":    ([5],    [6]),
    "yellow": ([1, 6], []),
    "blue":   ([2, 3], []),
    "green":  ([3, 4], []),
    "purple": ([1, 6], []),
    "white":  ([4, 5], []),
}

ULTA_BUFF_LABEL: dict[str, str] = {
    "red":    "🔴 +250 к каждому красному комбо",
    "yellow": "🟡 +500 к жёлтым комбо",
    "blue":   "🔵 3 свободных реролла за игру",
    "green":  "🟢 Любая пара зелёных — джокер",
    "purple": "🟣 +500 к 2-4-6 с фиолетовой 6",
    "white":  "⚪ Белый бафф активен",
}

ULTA_EMOJI: dict[str, str] = {
    "red": "🔴", "yellow": "🟡", "blue": "🔵",
    "green": "🟢", "purple": "🟣", "white": "⚪",
}


def _detect_ulta(selected_dice: list[dict]) -> str | None:
    """
    Ищет ультовую комбинацию в выделении.
    Возвращает faction-ключ или None.
    Зелёная (джокер) и жёлтая 1-2-3/стрит уже засчитаны в scoring.py;
    здесь детектируем red/blue/purple/white по два фракционных кубика нужного значения.
    """
    pair_triggers = [("red", 5), ("blue", 2), ("purple", 6), ("white", 1)]
    for faction, val in pair_triggers:
        cnt = sum(1 for d in selected_dice if d.get("faction") == faction and d["value"] == val)
        if cnt >= 2:
            return faction
    # Жёлтые ультовые комбо (≥2 жёлтых в 1-2-3 или жёлтые в обоих частях стрита)
    yellows = [d for d in selected_dice if d.get("faction") == "yellow"]
    if len(yellows) >= 2:
        vals = sorted(d["value"] for d in selected_dice)
        y_vals = [d["value"] for d in yellows]
        # Стрит 1-6 с жёлтыми в обеих половинах
        if vals == [1, 2, 3, 4, 5, 6]:
            low_y  = any(v in (1, 2, 3) for v in y_vals)
            high_y = any(v in (4, 5, 6) for v in y_vals)
            if low_y and high_y:
                return "yellow"
        # 1-2-3 с ≥2 жёлтыми
        if 1 in vals and 2 in vals and 3 in vals:
            if sum(1 for v in y_vals if v in (1, 2, 3)) >= 2:
                return "yellow"
    return None


def _detect_green_joker(roll: list[dict], ulta_green: bool) -> list[int]:
    """
    Возвращает индексы джокер-кубиков в броске.
    Без ульты: ≥2 зелёных тройки.
    С ультой: ≥2 зелёных с одинаковым значением.
    """
    green_dice = [(i, d) for i, d in enumerate(roll) if d.get("faction") == "green"]
    if not ulta_green:
        threes = [i for i, d in green_dice if d["value"] == 3]
        return threes[:2] if len(threes) >= 2 else []
    # Ульта: любая пара зелёных с одинаковым значением
    val_counts: dict[int, list[int]] = {}
    for i, d in green_dice:
        v = d["value"]
        val_counts.setdefault(v, []).append(i)
    for v, idxs in val_counts.items():
        if len(idxs) >= 2:
            return idxs[:2]
    return []


def _apply_ulta_buffs(base_score: int, selected_dice: list[dict],
                      ultas_active: list[str]) -> tuple[int, list[str]]:
    """
    Добавляет бонусы от активных ульт к base_score.
    Возвращает (итоговый_счёт, список подсказок для отображения).
    """
    bonus = 0
    hints: list[str] = []
    if "red" in ultas_active:
        red5s = sum(1 for d in selected_dice if d.get("faction") == "red" and d["value"] == 5)
        if red5s:
            bonus += 250 * red5s
            hints.append(f"🔴 +{250 * red5s} (ульта красных)")
    if "yellow" in ultas_active:
        has_y = any(d.get("faction") == "yellow" for d in selected_dice)
        if has_y:
            bonus += 500
            hints.append("🟡 +500 (ульта жёлтых)")
    if "purple" in ultas_active and _has_purple_246(selected_dice):
        bonus += 500
        hints.append("🟣 +500 (ульта фиолетовых)")
    return base_score + bonus, hints


def _solo_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 Рейтинг",      callback_data="solo_start_rated")],
        [InlineKeyboardButton("🏋️ Тренировка",  callback_data="solo_start_training")],
        [InlineKeyboardButton("📊 Соло-рейтинг", callback_data="solo_leaderboard")],
        [InlineKeyboardButton("📜 Соло-история", callback_data="solo_history")],
        [InlineKeyboardButton("◀ Назад",         callback_data="main_menu")],
    ])


def _solo_target_kb(rated: bool) -> InlineKeyboardMarkup:
    suffix = "rated" if rated else "training"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎯 до 20 000", callback_data=f"solo_target_20000_{suffix}"),
            InlineKeyboardButton("🎯 до 25 000", callback_data=f"solo_target_25000_{suffix}"),
        ],
        [InlineKeyboardButton("◀ Назад", callback_data="solo_menu")],
    ])


def _entry_kb(can_back: bool, can_done: bool) -> InlineKeyboardMarkup:
    """Клавиатура ввода значений выпавших кубиков."""
    rows = [
        [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (1, 2, 3)],
        [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (4, 5, 6)],
    ]
    bottom = []
    if can_back:
        bottom.append(InlineKeyboardButton("↩️ Стереть", callback_data="sin_back"))
    if can_done:
        bottom.append(InlineKeyboardButton("✅ Готово", callback_data="sin_done"))
    if bottom:
        rows.append(bottom)
    rows.append([InlineKeyboardButton("🏳️ Сдаться", callback_data="solo_quit")])
    return InlineKeyboardMarkup(rows)


def _select_kb(roll: list[dict], selected: list[int],
               green3_idx: list[int] | None = None,
               tokens: int = 0,
               blue_rerolls: int = 0,
               spec_active: list[int] | None = None,
               turn_banked: list[dict] | None = None) -> InlineKeyboardMarkup:
    """Клавиатура для выбора кубиков из текущего броска.

    Цветной кубик уже несёт цветной квадрат → не дублируем маркер.
    Нейтральный — ставим ⚫ для невыбранных, ✅ для выбранных.
    Цветной выбранный — добавляем ✅ перед квадратом.
    green3_idx — индексы зелёных 3, которые можно перебросить.
    tokens — кол-во белых жетонов (если > 0, показываем кнопку).
    """
    spec_active  = spec_active  or []
    turn_banked  = turn_banked  or []
    row = []
    for i, d in enumerate(roll):
        is_colored = d.get("faction") is not None
        if i in selected:
            label = f"✅{_die_label(d)}"
        else:
            label = _die_label(d) if is_colored else f"⚫{DICE_EMOJI[d['value']]}"
        row.append(InlineKeyboardButton(label, callback_data=f"ssel_{i}"))
    rows = [row[:3], row[3:]] if len(row) > 3 else [row]

    # 🟢 Переброска зелёных 3 (по одной кнопке на каждый такой кубик)
    if green3_idx:
        g_row = [
            InlineKeyboardButton(f"🟩3️⃣ ↩️ ({i + 1})", callback_data=f"sgreen_{i}")
            for i in green3_idx
        ]
        rows.append(g_row)

    # ⚪ Жетоны: воскрешение забранного кубика (только если есть что воскрешать)
    if tokens > 0:
        if turn_banked:
            last = turn_banked[-1]
            die_name = _die_label(last)
            rows.append([InlineKeyboardButton(
                f"🔮 Воскресить {die_name} (1 жетон, переброс)",
                callback_data="sres_1",
            )])
            if tokens >= 2:
                rows.append([InlineKeyboardButton(
                    f"🔮🔮 Воскресить {die_name} точно (2 жетона)",
                    callback_data="sres_2",
                )])
        else:
            rows.append([InlineKeyboardButton(
                f"⚪ Жетон ({tokens}/2) — сначала забери кубики",
                callback_data="noop",
            )])

    # 🔵 Синий реролл (от ульты): свободный перебросок любых кубиков
    if blue_rerolls > 0:
        rows.append([InlineKeyboardButton(
            f"🔵 Реролл ({blue_rerolls}) — перебросить выбранные",
            callback_data="sbrol",
        )])

    # ✨ Активация спец-граней (один ряд на каждый спец-кубик с активной гранью)
    reroll_button_shown = False
    for i, d in enumerate(roll):
        sp = d.get("special")
        if not sp:
            continue
        info = SPECIAL_INFO.get(sp, {})
        face = info.get("face")
        # Показываем кнопку только если выпала спец-грань и кубик не выбран (нельзя одновременно)
        if face and d["value"] == face and i not in selected:
            if sp in ("reroll_6", "reroll_5"):
                # Рерол: своя кнопка → экран выбора кубиков для переброса
                if not reroll_button_shown:
                    rows.append([InlineKeyboardButton(
                        "🔄 Рерол выпал! Перебросить кубики",
                        callback_data="srp_start",
                    )])
                    reroll_button_shown = True
            else:
                is_on = i in spec_active
                label = (f"✅ {info['emoji']} {info['name']} активирован"
                         if is_on else
                         f"✨ {info['emoji']} Использовать {info['name']}")
                rows.append([InlineKeyboardButton(label, callback_data=f"sspec_{i}")])
        # Рубен — всегда подсказка (грань не требуется активировать, просто цвет)
        elif sp == "ruben" and RUBEN_FACTION.get(d["value"]) and i not in selected:
            rf = RUBEN_FACTION[d["value"]]
            rows.append([InlineKeyboardButton(
                f"💪 Рубен: {COLOR_SQUARE.get(rf, '')} бонус цвета (авто)",
                callback_data="noop"
            )])

    rows.append([
        InlineKeyboardButton("🎲 Бросить ещё", callback_data="ssub_reroll"),
        InlineKeyboardButton("✅ Забрать",      callback_data="ssub_bank"),
    ])
    rows.append([InlineKeyboardButton("🏳️ Сдаться", callback_data="solo_quit")])
    return InlineKeyboardMarkup(rows)


def _solo_shop_kb(data: dict) -> InlineKeyboardMarkup:
    """Клавиатура экрана магазина (бросок + кнопки обмена на жетон)."""
    has_token   = data.get("shop_token", False)
    reserve     = data.get("reserve", [])
    neutral     = data.get("player_neutral", 0)
    rows = [
        [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (1, 2, 3)],
        [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (4, 5, 6)],
    ]
    if has_token:
        rows.append([InlineKeyboardButton("🎟️ Жетон активен (+1 вариант к броску!)", callback_data="noop")])
    else:
        trade_row = []
        if neutral >= 3:   # оставляем минимум 1 нейтральный
            trade_row.append(InlineKeyboardButton(
                "🎟️ 2 нейтральных → жетон", callback_data="sshoptoken_2n"))
        if reserve:
            trade_row.append(InlineKeyboardButton(
                "🎟️ Спец-кубик → жетон", callback_data="sshoptoken_spec"))
        if trade_row:
            rows.append(trade_row)
    return InlineKeyboardMarkup(rows)


def _solo_shop_text(data: dict) -> str:
    """Текст экрана магазина."""
    has_token = data.get("shop_token", False)
    if has_token:
        table = "1–2 → 1 кубик  •  3–4 → 2 кубика  •  5–6 → 3 кубика"
    else:
        table = "1–3 → 1 кубик  •  4–6 → 2 кубика"
    return (
        f"\n🛒 *Магазин!* Кинь свой фракционный кубик {data['player_color']}:\n"
        f"{table}"
    )


def _solo_status_text(d: dict, header: str = "") -> str:
    p_color = d["player_color"]
    b_color = d["bot_color"]
    pname   = d.get("player_display_name") or d.get("player_username", "Ты")
    target  = d["target"]
    p_total = d["player_total"]
    b_total = d["bot_total"]
    parts = [f"🎯 *Соло до {fmt(target)}* ({'рейтинг' if d['rated'] else 'тренировка'})"]
    # Показываем резервные спец-кубики если есть
    reserve = d.get("reserve", [])
    if reserve:
        rv_str = "  ".join(
            f"{SPECIAL_INFO.get(k, {}).get('emoji', '?')}{SPECIAL_INFO.get(k, {}).get('name', k)}"
            for k in reserve
        )
        parts.append(f"🎒 Резерв: {rv_str}")
    if header:
        parts.append(header)
    parts.append("─────────────────────")
    parts.append(f"{p_color} *{pname}*: {fmt(p_total)} / {fmt(target)}")
    parts.append(f"{b_color} *🤖 Бот*: {fmt(b_total)} / {fmt(target)}")
    return "\n".join(parts)


# ── Solo menu handlers ────────────────────────────────────────────────────────

async def cb_solo_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db.clear_pending_action(query.from_user.id)
    await query.edit_message_text(
        "🎯 *Сингл-плеер*\n\n"
        "Сыграй против AI-бота. Кидай 6 настоящих кубиков и записывай результат.\n\n"
        "• *Рейтинг* — победа +30, поражение −15, ничья 0\n"
        "• *Тренировка* — без рейтинга и истории",
        parse_mode="Markdown", reply_markup=_solo_menu_kb(),
    )


async def cmd_solo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Нажмите /start для начала.")
        return
    db.clear_pending_action(update.effective_user.id)
    await update.message.reply_text(
        "🎯 *Сингл-плеер*\n\nВыбери режим:",
        parse_mode="Markdown", reply_markup=_solo_menu_kb(),
    )


async def cb_solo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """solo_start_rated / solo_start_training → выбор цели."""
    query = update.callback_query
    await query.answer()
    rated = query.data.endswith("_rated")
    await query.edit_message_text(
        "🎯 *Выбери цель игры:*",
        parse_mode="Markdown", reply_markup=_solo_target_kb(rated),
    )


async def cb_solo_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """solo_target_<target>_<rated|training> → старт игры."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user    = db.get_user(user_id)
    if not user:
        await query.edit_message_text("Нажмите /start для начала."); return

    parts  = query.data.split("_")
    target = int(parts[2])
    rated  = parts[3] == "rated"

    # Случайные цвета фракций
    p_color = random.choice(COLORS)
    b_color = random.choice([c for c in COLORS if c != p_color])
    p_faction = FACTION_BY_EMOJI[p_color[0]]
    b_faction = FACTION_BY_EMOJI[b_color[0]]

    # Случайный первый ход + бонус +50
    first = random.choice(["player", "bot"])

    data = {
        "kind":             "solo",
        "rated":            rated,
        "target":           target,
        "player_username":  user["username"],
        "player_display_name": _user_display_name(user["user_id"], user=user),
        "player_color":     p_color[0],
        "player_color_name": p_color[1],
        "player_faction":   p_faction,
        "player_neutral":   5,
        "player_color_n":   1,
        "bot_color":        b_color[0],
        "bot_color_name":   b_color[1],
        "bot_faction":      b_faction,
        "bot_neutral":      5,
        "bot_color_n":      1,
        "player_total":     50 if first == "player" else 0,
        "bot_total":        50 if first == "bot"    else 0,
        "current":          first,
        "phase":            "pre_roll",
        "turn_score":       0,
        # Счётчики ввода для текущего броска
        "need_neutral":     5,
        "need_color":       1,
        "entered_neutral":  [],
        "entered_color":    [],
        "current_roll":     [],   # list of die dicts {value, color_emoji, faction}
        "selected":         [],
        # ── Phase 2B: faction abilities ──────────────────────────────
        "tokens":              0,   # ⚪ белые жетоны (0–2)
        "token_bonus_dice":    0,   # +N кубиков к следующему броску (жетон)
        "green_reroll_idx":    -1,  # индекс зелёного 3 в текущем броске
        "purple_extra_phase":  False,  # ждём решения о доп. броске 🟣
        "ultas_active":        [],  # перманентные баффы этой партии
        # ── Ульты / супер-игра ───────────────────────────────────────
        "super_game_pending":  None,  # faction ожидающей супер-игры
        "blue_rerolls":        0,     # свободные реролы от синей ульты
        "joker_queue":         [],    # очередь индексов джокер-кубиков
        # ── Магазин / резерв ─────────────────────────────────────────
        "reserve":             [],    # список ключей спец-кубиков в резерве
        "shop_visited":        [],    # уже посещённые пороги
        "shop_pending":        None,  # порог, ожидающий посещения
        "shop_draw_opts":      [],    # варианты выбора (1 или 2 кубика)
        # ── Спец-кубики в текущем броске ─────────────────────────────
        "need_special":        [],    # ключи спец-кубиков для ввода этого броска
        "entered_special":     [],    # [(value, key), ...] введённые значения
        "spec_active_indices": [],    # индексы в roll с активированной спец-гранью
        # ── Эффекты за текущий ход ───────────────────────────────────
        "bot_dice_bonus":      0,     # +/- кубиков боту на следующий ход
        "player_dice_bonus":   0,     # +/- кубиков игроку на следующий ход (заморозка от бота)
        "player_color_bonus":  0,     # +N цветных кубиков игроку на следующий ход (воскрешение)
        # ── Резерв и магазин бота ────────────────────────────────────
        "bot_reserve":         [],    # спец-кубики бота
        "bot_shop_visited":    [],    # пройденные пороги магазина (бот)
        # ── Воскрешение кубиков (белые жетоны) ───────────────────────────────
        "turn_banked":         [],    # кубики, забранные за текущий ход (для воскрешения)
        "preseeded_dice":      [],    # кубики, которые войдут в следующий бросок без ввода
        # ── Очередь магазина (несколько рубежей за один банк) ─────────────
        "shop_queue":          [],    # рубежи, ещё не посещённые
    }
    db.set_pending_action(user_id, "solo_game", data)

    intro = (
        f"🎲 *Игра началась!*\n\n"
        f"{p_color[0]} Ты — *{p_color[1]}*\n"
        f"{b_color[0]} Бот — *{b_color[1]}*\n\n"
        f"Первым ходит: "
        + (f"*{user['username']}* (+50)" if first == "player" else "*🤖 Бот* (+50)")
    )
    await query.edit_message_text(intro, parse_mode="Markdown")

    # Запускаем первый ход
    if first == "player":
        await _solo_player_pre_roll(context, user_id, data, query.message.chat_id)
    else:
        await _solo_bot_turn(context, user_id, data, query.message.chat_id)


# ── Player turn: pre-roll → entering → selecting ──────────────────────────────

async def _solo_show_lineup_msg(context, user_id: int, data: dict, chat_id: int,
                                *, send_new: bool = False, query=None):
    """Экран выбора состава. Каждый спец-кубик ЗАМЕНЯЕТ 1 нейтральный (итого всегда то же кол-во)."""
    reserve  = data.get("reserve", [])
    sel      = data.get("lineup_selected", [False] * len(reserve))
    n_base   = data.get("need_neutral", data["player_neutral"])   # базовое кол-во нейтральных
    c_need   = data.get("need_color",   data["player_color_n"])
    c_emoji  = data["player_color"]
    c_name   = data["player_color_name"]

    active_count = sum(1 for i, on in enumerate(sel) if on and i < len(reserve))
    eff_neutral  = n_base - active_count          # нейтральных после замен
    total        = eff_neutral + c_need + active_count  # = n_base + c_need, всегда постоянно
    slots_left   = n_base - active_count          # сколько ещё нейтральных можно заменить

    comeback = f"\n{data['comeback_note']}" if data.get("comeback_note") else ""

    # Строим строку "состав броска" динамически
    parts = [f"⚪ {eff_neutral} нейтральных"] if eff_neutral > 0 else []
    parts.append(f"{c_emoji} {c_need} {c_name.lower()}")
    if active_count:
        active_names = ", ".join(
            SPECIAL_INFO.get(reserve[i], {}).get("emoji", "?")
            for i, on in enumerate(sel) if on and i < len(reserve)
        )
        parts.append(f"{active_names} {active_count} спец")
    compose_str = "  •  ".join(parts)

    hint = ""
    if n_base == 0:
        hint = "\n_Нет нейтральных кубиков для замены._"
    elif slots_left == 0:
        hint = "\n_Все нейтральные слоты заняты спец-кубиками._"

    header = (
        f"\n▶️ *Твой ход!*{comeback}\n\n"
        f"🎲 *Состав броска:* {compose_str}\n"
        f"📦 *Резерв* — каждый спец-кубик заменяет 1 нейтральный:{hint}"
    )
    text = _solo_status_text(data, header=header)

    rows: list[list] = []
    for i, key in enumerate(reserve):
        info   = SPECIAL_INFO.get(key, {})
        is_on  = sel[i] if i < len(sel) else False
        face_h = f"  (грань {DICE_EMOJI[info['face']]})" if info.get("face") else ""
        # Кнопка недоступна (серая) если нельзя включить: нет нейтральных слотов
        can_toggle = is_on or slots_left > 0
        if is_on:
            label = f"✅ {info.get('emoji','?')} {info.get('name', key)}{face_h}  ← заменяет 1 нейтральный"
        elif can_toggle:
            label = f"☐  {info.get('emoji','?')} {info.get('name', key)}{face_h}"
        else:
            label = f"🔒 {info.get('emoji','?')} {info.get('name', key)}{face_h}  (нет свободных слотов)"
        rows.append([InlineKeyboardButton(label, callback_data=f"slup_{i}")])

    dice_word = "кубик" if total == 1 else ("кубика" if 2 <= total <= 4 else "кубиков")
    rows.append([InlineKeyboardButton(
        f"🎲 Бросить — {total} {dice_word}", callback_data="slup_go",
    )])
    rows.append([InlineKeyboardButton("🏳️ Сдаться", callback_data="solo_quit")])
    kb = InlineKeyboardMarkup(rows)

    if send_new or query is None:
        await context.bot.send_message(chat_id=chat_id, text=text,
                                       parse_mode="Markdown", reply_markup=kb)
    else:
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=text,
                                           parse_mode="Markdown", reply_markup=kb)


async def cb_solo_lineup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """slup_N — переключить спец-кубик; slup_go — подтвердить состав и начать ввод."""
    query   = update.callback_query
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.answer(); return
    data = gp["data"]
    if data.get("current") != "player" or data.get("phase") != "lineup_select":
        await query.answer(); return

    cmd     = query.data
    reserve = data.get("reserve", [])
    sel     = list(data.get("lineup_selected", [False] * len(reserve)))
    n_base  = data.get("need_neutral", data["player_neutral"])

    if cmd == "slup_go":
        # Подтверждаем состав: спец-кубики заменяют нейтральные по 1:1
        active = [reserve[i] for i, on in enumerate(sel) if on]
        data["need_special"] = active
        data["need_neutral"] = max(0, n_base - len(active))
        data["phase"]        = "entering"
        db.set_pending_action(user_id, "solo_game", data)
        await query.answer("🎲 Бросок!")
        await _solo_show_entry(context, user_id, data, query.message.chat_id, query=query)
        return

    # Переключаем конкретный кубик
    try:
        idx = int(cmd.split("_")[1])
    except (IndexError, ValueError):
        await query.answer(); return

    if 0 <= idx < len(sel):
        if not sel[idx]:   # пытаемся включить
            active_now = sum(1 for on in sel if on)
            if active_now >= n_base:
                await query.answer("Нет свободных нейтральных для замены!", show_alert=True)
                return
        sel[idx] = not sel[idx]

    data["lineup_selected"] = sel
    db.set_pending_action(user_id, "solo_game", data)
    await query.answer()
    await _solo_show_lineup_msg(context, user_id, data, query.message.chat_id, query=query)


async def _solo_player_pre_roll(context, user_id: int, data: dict, chat_id: int,
                               *, needs_preset: bool = False):
    """Показывает приглашение ввести бросок (нейтралы → цветные → спец-кубики).

    needs_preset=True — need_neutral/need_color уже выставлены вызывающим кодом
    (реролл с остатком), не перезаписываем их.
    """
    if not needs_preset:
        # Применяем заморозку от бота (если была) + бонусы от воскрешения/крест/etc.
        p_bonus = data.pop("player_dice_bonus", 0)
        c_bonus = data.pop("player_color_bonus", 0)
        if p_bonus != 0:
            data["need_neutral"] = max(1, data["player_neutral"] + p_bonus)
        else:
            data["need_neutral"] = data["player_neutral"]
        data["need_color"] = data["player_color_n"] + c_bonus
    else:
        # need_* уже заданы; всё равно применим бонусные поля если они есть
        p_bonus = data.pop("player_dice_bonus", 0)
        c_bonus = data.pop("player_color_bonus", 0)
        if p_bonus != 0:
            data["need_neutral"] = max(1, data.get("need_neutral", data["player_neutral"]) + p_bonus)
        if c_bonus != 0:
            data["need_color"] = data.get("need_color", data["player_color_n"]) + c_bonus

    data["phase"]               = "entering"   # может быть переопределена ниже
    data["entered_neutral"]     = []
    data["entered_color"]       = []
    data["entered_special"]     = []
    # need_special будет задан ниже (через экран состава или напрямую)
    data["current_roll"]        = []
    data["selected"]            = []
    data["spec_active_indices"] = []
    data["blue_reroll_partial"] = False

    # ── Воскрешённые кубики (2-жетонный метод) ──────────────────────────────────
    preseeded = data.pop("preseeded_dice", [])
    for d in preseeded:
        v = d["value"] if isinstance(d, dict) else d
        if isinstance(d, dict) and d.get("faction"):
            data["entered_color"].append(v)
            data["need_color"] = max(0, data.get("need_color", 0) - 1)
        else:
            data["entered_neutral"].append(v)
            data["need_neutral"] = max(0, data.get("need_neutral", 0) - 1)

    # ── Камбек-кубик игрока ──────────────────────────────────────────────────────
    if data["player_total"] + 10000 <= data["bot_total"] and data.get("need_neutral", 0) > 0:
        cv = _roll_comeback_die()
        data["entered_neutral"] = [cv] + data.get("entered_neutral", [])
        data["need_neutral"] = max(0, data.get("need_neutral", 0) - 1)
        data["comeback_note"] = f"🎯 *Камбек!* +1 авто-кубик: {DICE_EMOJI[cv]}"
    else:
        data.pop("comeback_note", None)

    # ── Экран состава (только для нового хода, не для реролла внутри хода) ───────
    if not needs_preset:
        reserve = data.get("reserve", [])
        if reserve:
            # Показываем экран выбора — какие спец-кубики брать в этот бросок
            # По умолчанию все ВЫКЛЮЧЕНЫ: игрок явно выбирает что брать
            data["lineup_selected"] = [False] * len(reserve)
            data["need_special"]    = []   # уточнится после подтверждения
            data["phase"]           = "lineup_select"
            db.set_pending_action(user_id, "solo_game", data)
            await _solo_show_lineup_msg(context, user_id, data, chat_id, send_new=True)
            return
        data["need_special"] = []   # нет резерва — спец-кубиков нет
    # needs_preset=True: need_special сохраняется из lineup confirm предыдущего шага

    db.set_pending_action(user_id, "solo_game", data)
    await _solo_show_entry(context, user_id, data, chat_id, send_new=True)


async def _solo_show_entry(context, user_id: int, data: dict, chat_id: int,
                           *, send_new: bool = False, query=None):
    """Показывает экран ввода: нейтралы → цветные → спец-кубики."""
    n_need    = data["need_neutral"]
    c_need    = data["need_color"]
    s_need    = data.get("need_special", [])
    n_done    = data["entered_neutral"]
    c_done    = data["entered_color"]
    s_done    = data.get("entered_special", [])
    color_emoji = data["player_color"]

    entering_neutral = len(n_done) < n_need
    entering_colored = not entering_neutral and len(c_done) < c_need
    entering_special = not entering_neutral and not entering_colored and len(s_done) < len(s_need)

    n_str = " ".join(DICE_EMOJI[v] for v in n_done) if n_done else "_пусто_"
    c_str = " ".join(f"{color_emoji}{DICE_EMOJI[v]}" for v in c_done) if c_done else "_пусто_"
    s_str_parts = []
    for i, (v, k) in enumerate(s_done):
        em = SPECIAL_INFO.get(k, {}).get("emoji", "?")
        s_str_parts.append(f"{em}{DICE_EMOJI[v]}")
    s_str = " ".join(s_str_parts) if s_str_parts else ("_пусто_" if s_need else "_нет_")

    total_need = n_need + c_need + len(s_need)
    total_done = len(n_done) + len(c_done) + len(s_done)

    if entering_neutral:
        prompt = f"⚪ Введи *{n_need}* нейтральных ({len(n_done)}/{n_need}):"
    elif entering_colored:
        prompt = f"{color_emoji} Введи *{c_need}* {data['player_color_name'].lower()} ({len(c_done)}/{c_need}):"
    elif entering_special:
        next_sp = s_need[len(s_done)]
        sp_info = SPECIAL_INFO.get(next_sp, {})
        sp_face = sp_info.get("face")
        face_hint = f"  _(спец. грань: {DICE_EMOJI[sp_face]})_" if sp_face else ""
        prompt = f"{sp_info.get('emoji','?')} Введи *{sp_info.get('name','спец')}* ({len(s_done)+1}/{len(s_need)}){face_hint}:"
    else:
        prompt = "✅ Все кубики введены!"

    spec_line    = f"\n🎲 Спец-кубики: {s_str}" if s_need else ""
    comeback_line = f"\n{data['comeback_note']}" if data.get("comeback_note") else ""
    header = (
        f"\n🎲 *Твой ход!*  Накоплено: *{fmt(data['turn_score'])}*\n"
        f"Всего кубиков: *{total_need}*."
        f"{comeback_line}\n\n"
        f"⚪ Нейтральные: {n_str}\n"
        f"{color_emoji} {data['player_color_name']}: {c_str}"
        f"{spec_line}\n\n"
        f"{prompt}"
    )
    text = _solo_status_text(data, header=header)
    can_back = bool(n_done or c_done or s_done)
    can_done = total_done == total_need
    kb = _entry_kb(can_back=can_back, can_done=can_done)

    if send_new or query is None:
        await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb,
        )
    else:
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            pass


async def cb_solo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """sin_1 / sin_2 / ... / sin_back / sin_done"""
    query = update.callback_query
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.answer(); return
    data = gp["data"]

    if data.get("current") != "player":
        await query.answer("Сейчас не время вводить!", show_alert=True); return

    cmd = query.data[4:]   # sin_<cmd>

    # ── 🟢 Фаза выбора значения для джокер-кубиков ──────────────────────────────
    if data.get("phase") == "joker_picking":
        if cmd == "back":
            await query.answer("Введи значение для джокера")
            return
        try:
            v = int(cmd)
        except ValueError:
            await query.answer(); return
        queue = list(data.get("joker_queue", []))
        roll  = list(data["current_roll"])
        if not queue:
            await query.answer(); return
        idx = queue.pop(0)
        roll[idx] = {
            "value":       v,
            "color_emoji": data["player_color"],
            "faction":     data["player_faction"],
        }
        data["current_roll"] = roll
        data["joker_queue"]  = queue

        await query.answer(f"🟢 Джокер → {DICE_EMOJI[v]}")

        if queue:
            # Ещё джокеры
            db.set_pending_action(user_id, "solo_game", data)
            rem = len(queue)
            text = (
                _solo_status_text(data) + "\n\n"
                f"🟢 *Джокер {2 - rem + 1}/2* — введи значение:\n"
                f"Бросок пока: {_format_dice(roll)}"
            )
            await query.edit_message_text(
                text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(DICE_EMOJI[vv], callback_data=f"sin_{vv}") for vv in (1, 2, 3)],
                    [InlineKeyboardButton(DICE_EMOJI[vv], callback_data=f"sin_{vv}") for vv in (4, 5, 6)],
                ]),
            )
            return

        # Все джокеры расставлены → проверяем бюст / супер-игру
        data["phase"] = "selecting"
        data["selected"] = []

        # Если ожидает супер-игра для зелёной (сработала при броске)
        if data.get("super_game_pending") == "green":
            db.set_pending_action(user_id, "solo_game", data)
            await _solo_start_super_game(context, user_id, data, query.message.chat_id, query)
            return

        if not scoring.has_any_score(_scoring_dice(roll)):
            await _solo_player_bust(context, user_id, data, query.message.chat_id, query)
            return
        db.set_pending_action(user_id, "solo_game", data)
        await _show_select_screen(context, query, data)
        return

    # ── 🎲 Фаза верификационного броска (супер-игра) ────────────────────────────
    if data.get("phase") == "super_game":
        if cmd == "back":
            await query.answer("Введи значение проверочного кубика")
            return
        try:
            v = int(cmd)
        except ValueError:
            await query.answer(); return
        await _solo_resolve_super_game(context, user_id, data, query.message.chat_id, query, v)
        return

    # ── ⚪ Фаза кражи кубика (белая ульта) ─────────────────────────────────────
    if data.get("phase") == "white_steal_roll":
        if cmd == "back":
            await query.answer("Брось кубик чтобы определить тип кражи")
            return
        try:
            v = int(cmd)
        except ValueError:
            await query.answer(); return
        # 1-3 = нейтральный, 4-6 = фракционный
        if 1 <= v <= 3:
            stolen_type = "нейтральный"
            bot_neutral = data.get("bot_neutral", 5)
            if bot_neutral > 1:          # оставляем боту хотя бы 1 нейтральный
                data["bot_neutral"]       = bot_neutral - 1
                data["player_neutral"]    = data.get("player_neutral", 5) + 1
                steal_msg = f"⚪ {DICE_EMOJI[v]} → украден 1 нейтральный кубик у бота!"
            else:
                steal_msg = f"⚪ {DICE_EMOJI[v]} → у бота нет лишних нейтральных кубиков."
        else:
            stolen_type = "фракции"
            bot_color_n = data.get("bot_color_n", 1)
            if bot_color_n > 0:
                data["bot_color_n"]       = bot_color_n - 1
                data["player_color_n"]    = data.get("player_color_n", 1) + 1
                steal_msg = f"⚪ {DICE_EMOJI[v]} → украден 1 фракционный кубик у бота!"
            else:
                steal_msg = f"⚪ {DICE_EMOJI[v]} → у бота нет фракционных кубиков для кражи."
        data["phase"] = "pre_roll"
        db.set_pending_action(user_id, "solo_game", data)
        await query.answer(steal_msg)
        await query.edit_message_text(
            _solo_status_text(data, header=f"\n{steal_msg}"),
            parse_mode="Markdown",
        )
        await _solo_player_bank(context, user_id, data, query.message.chat_id, None)
        return

    # ── 🟢 Фаза ввода нового значения для перебрасываемой зелёной 3 ────────────
    if data.get("phase") == "green_rerolling":
        if cmd == "back":
            # Отмена переброски — вернуться к выбору
            data["phase"] = "selecting"
            data["green_reroll_idx"] = -1
            db.set_pending_action(user_id, "solo_game", data)
            await query.answer("Переброска отменена")
            await _show_select_screen(context, query, data)
            return
        try:
            v = int(cmd)
        except ValueError:
            await query.answer(); return
        idx = data.get("green_reroll_idx", -1)
        roll = list(data["current_roll"])
        if 0 <= idx < len(roll):
            roll[idx] = {
                "value":       v,
                "color_emoji": data["player_color"],
                "faction":     data["player_faction"],
            }
        data["current_roll"]    = roll
        data["phase"]           = "selecting"
        data["green_reroll_idx"] = -1
        # Проверяем бюст с новым кубиком
        if not scoring.has_any_score(_scoring_dice(roll)):
            await query.answer()
            await _solo_player_bust(context, user_id, data, query.message.chat_id, query)
            return
        db.set_pending_action(user_id, "solo_game", data)
        await query.answer(f"🟩 Перебросили → {DICE_EMOJI[v]}")
        await _show_select_screen(context, query, data)
        return

    # ── 🟣 Фаза ввода доп. броска после фиолетовой 2-4-6 ──────────────────────
    if data.get("phase") == "purple_rolling":
        n_need = data["need_neutral"]
        n_done = list(data.get("entered_neutral", []))

        if cmd == "back":
            if n_done:
                n_done.pop()
        elif cmd == "done":
            if len(n_done) != n_need:
                await query.answer("Нужно ввести все кубики", show_alert=True); return
            roll = [{"value": v, "color_emoji": "", "faction": None} for v in n_done]
            random.shuffle(roll)
            data["current_roll"]    = roll
            data["entered_neutral"] = []
            data["entered_color"]   = []
            data["selected"]        = []
            # Бюст при доп. броске — сохраняем основной счёт, не добавляем
            if not scoring.has_any_score(_scoring_dice(roll)):
                data["phase"] = "pre_roll"
                db.set_pending_action(user_id, "solo_game", data)
                await query.answer()
                bust_text = (
                    f"💥 *Бюст на доп. броске!*  Бросок: {_format_dice(roll)}\n"
                    f"Доп. очки не добавляются, основной счёт сохраняется."
                )
                await query.edit_message_text(bust_text, parse_mode="Markdown")
                await _solo_player_bank(context, user_id, data, query.message.chat_id, None)
                return
            data["phase"] = "purple_selecting"
            db.set_pending_action(user_id, "solo_game", data)
            await query.answer()
            await _show_select_screen(context, query, data)
            return
        else:
            try:
                v = int(cmd)
            except ValueError:
                await query.answer(); return
            if len(n_done) < n_need:
                n_done.append(v)
            else:
                await query.answer("Все значения уже введены", show_alert=True); return

        data["entered_neutral"] = n_done
        db.set_pending_action(user_id, "solo_game", data)
        await query.answer()
        await _solo_show_entry(context, user_id, data, query.message.chat_id, query=query)
        return

    # ── 🛒 Фаза броска для магазина ─────────────────────────────────────────────
    if data.get("phase") == "shop_roll":
        if cmd in ("back", "done"):
            await query.answer("Введи значение своего фракционного кубика"); return
        try:
            v = int(cmd)
        except ValueError:
            await query.answer(); return
        # Определяем сколько вариантов тянем
        has_token = data.pop("shop_token", False)
        if has_token:
            count = 3 if v >= 5 else (2 if v >= 3 else 1)
        else:
            count = 2 if v >= 4 else 1
        pool  = [k for k in SHOP_POOL if SHOP_POOL.count(k) > data.get("reserve", []).count(k)]
        opts  = random.sample(pool, min(count, len(pool)))
        data["shop_draw_opts"] = opts
        data["phase"] = "shop_pick"
        db.set_pending_action(user_id, "solo_game", data)
        await query.answer(f"{DICE_EMOJI[v]} — тянешь {count} кубик{'а' if count>1 else ''}!")
        lines = [_solo_status_text(data),
                 f"\n🛒 *Ты тянешь {count} кубик{'а' if count>1 else ''}:*"]
        for k in opts:
            info = SPECIAL_INFO.get(k, {})
            lines.append(f"  {info.get('emoji','?')} {info.get('name','?')}"
                         + (f" (спец. грань: {DICE_EMOJI[info['face']]})" if info.get("face") else ""))
        if len(opts) == 1:
            lines.append("\n_Забираешь его автоматически._")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"{SPECIAL_INFO.get(k,{}).get('emoji','?')} {SPECIAL_INFO.get(k,{}).get('name','?')}",
                    callback_data=f"spick_{k}"
                )]
                for k in opts
            ] + [[InlineKeyboardButton("🏳️ Сдаться", callback_data="solo_quit")]]),
        )
        return

    # ── 🥷 Фаза броска вора ─────────────────────────────────────────────────────
    if data.get("phase") == "thief_rolling":
        if cmd in ("back", "done"):
            await query.answer("Введи значение кубика вора"); return
        try:
            v = int(cmd)
        except ValueError:
            await query.answer(); return
        steal = {4: 150, 5: 200, 6: 250}.get(v, 0)
        tripling_was = data.pop("thief_tripling", False)
        if tripling_was:
            steal *= 3
        old_bot = data["bot_total"]
        data["bot_total"] = max(0, old_bot - steal)
        data["player_total"] += steal
        data["thief_pending"] = False
        data["phase"] = "pre_roll"
        data["spec_active_indices"] = []
        db.set_pending_action(user_id, "solo_game", data)
        await query.answer(f"🥷 Вор! +{fmt(steal)}" if steal else "🥷 Мимо!")
        steal_txt = (
            f"🥷 *Вор!*  Выпало {DICE_EMOJI[v]}\n"
            + (f"+{fmt(steal)} очков от бота!" if steal else "Ничего не украл.")
        )
        await query.edit_message_text(
            _solo_status_text(data, header=f"\n{steal_txt}"),
            parse_mode="Markdown",
        )
        await _solo_player_bank(context, user_id, data, query.message.chat_id, None)
        return

    # ── ⚔️ Фаза разбоя ─────────────────────────────────────────────────────────
    if data.get("phase") == "robbery_rolling":
        if cmd in ("back", "done"):
            await query.answer("Введи значение своего кубика"); return
        try:
            v = int(cmd)
        except ValueError:
            await query.answer(); return
        bot_roll = random.randint(1, 6)
        gain = 400 if v > bot_roll else 0
        tripling_was = data.pop("robbery_tripling", False)
        if tripling_was:
            gain *= 3
        data["player_total"] += gain
        data["robbery_pending"] = False
        data["phase"] = "pre_roll"
        data["spec_active_indices"] = []
        db.set_pending_action(user_id, "solo_game", data)
        await query.answer(f"⚔️ {DICE_EMOJI[v]} vs 🤖{DICE_EMOJI[bot_roll]}")
        rob_txt = (
            f"⚔️ *Разбой!*\n"
            f"Ты: {DICE_EMOJI[v]}  |  Бот: {DICE_EMOJI[bot_roll]}\n"
            + (f"*Победа!* +{fmt(gain)} очков!" if gain else "Проигрыш — ничего не получил.")
        )
        await query.edit_message_text(
            _solo_status_text(data, header=f"\n{rob_txt}"),
            parse_mode="Markdown",
        )
        await _solo_player_bank(context, user_id, data, query.message.chat_id, None)
        return

    # ── Обычная фаза ввода ─────────────────────────────────────────────────────
    if data.get("phase") != "entering":
        await query.answer("Сейчас не время вводить!", show_alert=True); return

    n_need = data["need_neutral"]
    c_need = data["need_color"]
    s_need = list(data.get("need_special", []))
    n_done = list(data.get("entered_neutral", []))
    c_done = list(data.get("entered_color", []))
    s_done = list(data.get("entered_special", []))

    entering_neutral = len(n_done) < n_need
    entering_colored = not entering_neutral and len(c_done) < c_need
    entering_special = not entering_neutral and not entering_colored and len(s_done) < len(s_need)

    if cmd == "back":
        # Стираем последний введённый (спец → цветные → нейтралы)
        if s_done:
            s_done.pop()
        elif c_done:
            c_done.pop()
        elif n_done:
            n_done.pop()
    elif cmd == "done":
        if len(n_done) != n_need or len(c_done) != c_need or len(s_done) != len(s_need):
            await query.answer("Нужно ввести все кубики", show_alert=True); return
        # Собираем бросок
        new_dice = _make_dice(n_done, c_done, data["player_color"], data["player_faction"])
        new_dice += _build_special_dice(s_done)
        # ── Синий реролл: мержим новые кубики с оставшимися ─────────────────
        if data.get("blue_reroll_partial"):
            data["blue_reroll_partial"] = False
            roll = data.get("current_roll", []) + new_dice
        elif "reroll_kept_dice" in data:
            # 🔄 Рерол спец-кубика: мержим сохранённые кубики с перебросанными
            roll = data.pop("reroll_kept_dice") + new_dice
        else:
            roll = new_dice
        random.shuffle(roll)
        data["current_roll"]    = roll
        data["entered_neutral"] = []
        data["entered_color"]   = []
        data["entered_special"] = []
        data["selected"]        = []
        data["spec_active_indices"] = []

        # ── 🟢 Проверка на джокеры (две зелёных 3 или ульта-пара) ─────────────
        ulta_green  = "green" in data.get("ultas_active", [])
        joker_idx   = _detect_green_joker(roll, ulta_green)
        if joker_idx:
            data["joker_queue"] = joker_idx[:]
            data["phase"] = "joker_picking"
            # Если это НЕ ульта (т.е. простые две тройки) → запустить супер-игру
            if not ulta_green:
                data["super_game_pending"] = "green"
            db.set_pending_action(user_id, "solo_game", data)
            await query.answer("🟢 Джокеры! Выбери значения")
            n_jokers = len(joker_idx)
            text = (
                _solo_status_text(data) + "\n\n"
                f"🟢 *{n_jokers} ДЖОКЕРА!* Зелёные 3 заменяют любое значение.\n"
                f"Введи значение для джокера 1/{n_jokers}:\n"
                f"Бросок пока: {_format_dice(roll)}"
            )
            await query.edit_message_text(
                text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (1, 2, 3)],
                    [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (4, 5, 6)],
                ]),
            )
            return

        data["phase"] = "selecting"
        # Проверка на бюст
        if not scoring.has_any_score(_scoring_dice(roll)):
            await query.answer()
            await _solo_player_bust(context, user_id, data, query.message.chat_id, query)
            return
        db.set_pending_action(user_id, "solo_game", data)
        await query.answer()
        await _show_select_screen(context, query, data)
        return
    else:
        try:
            v = int(cmd)
        except ValueError:
            await query.answer(); return
        # Заполняем: нейтралы → цветные → спец-кубики
        if len(n_done) < n_need:
            n_done.append(v)
        elif len(c_done) < c_need:
            c_done.append(v)
        elif len(s_done) < len(s_need):
            s_done.append((v, s_need[len(s_done)]))
        else:
            await query.answer("Все значения уже введены", show_alert=True); return

    data["entered_neutral"] = n_done
    data["entered_color"]   = c_done
    data["entered_special"] = s_done
    db.set_pending_action(user_id, "solo_game", data)
    await query.answer()
    await _solo_show_entry(context, user_id, data, query.message.chat_id, query=query)


async def _show_reroll_pick(context, query, data: dict):
    """Экран выбора кубиков для переброса (фаза reroll_pick)."""
    roll = data["current_roll"]
    sel  = data.get("reroll_pick_sel", [False] * len(roll))

    reroll_count = sum(1 for on in sel if on)
    dice_word = "кубик" if reroll_count == 1 else ("кубика" if 2 <= reroll_count <= 4 else "кубиков")

    header = (
        f"\n🔄 *Рерол выпал!*\n"
        f"Отметь кубики которые хочешь перебросить.\n"
        f"Рерол-кубик перебрасывается всегда.\n"
        f"Накоплено за ход: *{fmt(data['turn_score'])}*\n\n"
        f"Текущий бросок:"
    )
    text = _solo_status_text(data, header=header)

    row = []
    for i, d in enumerate(roll):
        sp = d.get("special")
        is_reroll_die = (sp in ("reroll_6", "reroll_5") and
                         d["value"] == SPECIAL_INFO.get(sp, {}).get("face"))
        is_on = sel[i] if i < len(sel) else False
        if is_on:
            label = f"✅{_die_label(d)}"
        elif d.get("faction") is not None:
            label = _die_label(d)
        else:
            label = f"⚫{DICE_EMOJI[d['value']]}"
        if is_reroll_die:
            label += "🔄"
        row.append(InlineKeyboardButton(label, callback_data=f"srp_{i}"))

    rows = [row[:3], row[3:]] if len(row) > 3 else [row]
    if reroll_count:
        rows.append([InlineKeyboardButton(
            f"🔄 Перебросить {reroll_count} {dice_word}", callback_data="srp_go",
        )])
    else:
        rows.append([InlineKeyboardButton(
            "🔄 Выбери кубики для переброса", callback_data="noop",
        )])
    rows.append([InlineKeyboardButton("◀️ Отмена", callback_data="solo_quit_cancel")])
    rows.append([InlineKeyboardButton("🏳️ Сдаться", callback_data="solo_quit")])

    try:
        await query.edit_message_text(text, parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text,
                                       parse_mode="Markdown",
                                       reply_markup=InlineKeyboardMarkup(rows))


async def cb_solo_reroll_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """srp_start / srp_N / srp_go — экран выбора кубиков для переброса рерол-кубиком."""
    query   = update.callback_query
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.answer(); return
    data = gp["data"]

    if data.get("current") != "player":
        await query.answer(); return

    cmd = query.data

    # ── Вход в фазу выбора (из экрана selecting) ─────────────────────────────
    if cmd == "srp_start":
        if data.get("phase") not in ("selecting", "purple_selecting"):
            await query.answer(); return
        roll = data["current_roll"]
        # По умолчанию: рерол-кубики отмечены (обязательны), остальные — нет
        default_sel = [False] * len(roll)
        for i, d in enumerate(roll):
            sp = d.get("special")
            if sp in ("reroll_6", "reroll_5") and d["value"] == SPECIAL_INFO.get(sp, {}).get("face"):
                default_sel[i] = True
        data["reroll_pick_sel"] = default_sel
        data["phase"] = "reroll_pick"
        db.set_pending_action(user_id, "solo_game", data)
        await query.answer()
        await _show_reroll_pick(context, query, data)
        return

    if data.get("phase") != "reroll_pick":
        await query.answer(); return

    # ── Подтверждение переброса ───────────────────────────────────────────────
    if cmd == "srp_go":
        roll = data["current_roll"]
        sel  = data.get("reroll_pick_sel", [False] * len(roll))
        reroll_indices = [i for i, on in enumerate(sel) if on]
        keep_indices   = [i for i in range(len(roll)) if i not in reroll_indices]

        if not reroll_indices:
            await query.answer("Выбери хотя бы один кубик!", show_alert=True); return

        reroll_dice = [roll[i] for i in reroll_indices]
        kept_dice   = [roll[i] for i in keep_indices]

        # Определяем сколько и каких кубиков нужно ввести заново
        new_need_neutral = sum(1 for d in reroll_dice
                               if not d.get("special") and d.get("faction") is None)
        new_need_color   = sum(1 for d in reroll_dice
                               if not d.get("special") and d.get("faction") is not None)
        new_need_special = [d["special"] for d in reroll_dice if d.get("special")]

        data["reroll_kept_dice"] = kept_dice
        data["need_neutral"]     = new_need_neutral
        data["need_color"]       = new_need_color
        data["need_special"]     = new_need_special
        data["entered_neutral"]  = []
        data["entered_color"]    = []
        data["entered_special"]  = []
        data["selected"]         = []
        data["spec_active_indices"] = []
        data["phase"]            = "entering"
        data.pop("reroll_pick_sel", None)

        db.set_pending_action(user_id, "solo_game", data)
        await query.answer("🔄 Бросай!")
        await _solo_show_entry(context, user_id, data, query.message.chat_id, query=query)
        return

    # ── Переключение кубика ───────────────────────────────────────────────────
    if cmd.startswith("srp_"):
        try:
            idx = int(cmd[4:])
        except ValueError:
            await query.answer(); return

        roll = data["current_roll"]
        sel  = list(data.get("reroll_pick_sel", [False] * len(roll)))
        if idx >= len(roll):
            await query.answer(); return

        d  = roll[idx]
        sp = d.get("special")
        # Рерол-кубик нельзя снять — он перебрасывается принудительно
        if sp in ("reroll_6", "reroll_5") and d["value"] == SPECIAL_INFO.get(sp, {}).get("face"):
            await query.answer("Рерол-кубик перебрасывается всегда!", show_alert=True)
            return

        sel[idx] = not sel[idx]
        data["reroll_pick_sel"] = sel
        db.set_pending_action(user_id, "solo_game", data)
        await query.answer()
        await _show_reroll_pick(context, query, data)
        return

    await query.answer()


async def _show_select_screen(context, query, data: dict):
    roll     = data["current_roll"]
    selected = data["selected"]
    selected_dice = [roll[i] for i in selected]
    sel_score = scoring.score_selection(_scoring_dice(selected_dice))
    sel_str = (
        f"*+{fmt(sel_score)}*" if sel_score
        else ("*недопустимый набор*" if selected else "_не выбрано_")
    )
    header = (
        f"\n🎲 Бросок: {_format_dice(roll, selected)}\n"
        f"Выбрано: {sel_str}\n"
        f"Накоплено за ход: *{fmt(data['turn_score'])}*"
    )
    # Faction ability hints in header
    g3 = _green_3_indices(roll, excluded=selected)
    if g3:
        header += f"\n🟩 _Зелёная 3 — можно перебросить!_"
    tokens = data.get("tokens", 0)
    if tokens:
        header += f"\n⚪ _Жетонов: {tokens} — можно добрать кубик_"

    blue_rerolls = data.get("blue_rerolls", 0)
    await query.edit_message_text(
        _solo_status_text(data, header=header),
        parse_mode="Markdown",
        reply_markup=_select_kb(roll, selected, green3_idx=g3, tokens=tokens,
                                blue_rerolls=blue_rerolls,
                                spec_active=data.get("spec_active_indices", []),
                                turn_banked=data.get("turn_banked", [])),
    )


async def cb_solo_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ssel_<index> — переключение выбора кубика."""
    query = update.callback_query
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.answer(); return
    data = gp["data"]
    if data.get("current") != "player" or data.get("phase") not in ("selecting", "purple_selecting"):
        await query.answer(); return

    idx = int(query.data.split("_")[1])
    selected = list(data.get("selected", []))
    if idx in selected:
        selected.remove(idx)
    else:
        selected.append(idx)
    data["selected"] = selected
    db.set_pending_action(user_id, "solo_game", data)
    await query.answer()
    await _show_select_screen(context, query, data)


async def cb_solo_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ssub_reroll / ssub_bank — забрать выделенные и продолжить/закончить."""
    query = update.callback_query
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.answer(); return
    data = gp["data"]
    if data.get("current") != "player" or data.get("phase") not in ("selecting", "purple_selecting"):
        await query.answer(); return

    is_purple_extra = (data.get("phase") == "purple_selecting")
    action = query.data.split("_")[1]
    roll = data["current_roll"]
    selected = data.get("selected", [])
    if not selected:
        await query.answer("Сначала выбери кубики", show_alert=True); return
    selected_dice = [roll[i] for i in selected]
    sel_score = scoring.score_selection(_scoring_dice(selected_dice))
    if sel_score is None:
        await query.answer("В выделении есть кубики без комбинации", show_alert=True); return

    # ── Записываем забранные кубики для воскрешения ────────────────────────────
    data["turn_banked"] = data.get("turn_banked", []) + selected_dice

    # ── ⚪ Белые жетоны — начисляем за белую 1 ─────────────────────────────────
    w1s = _count_white_1(selected_dice)
    if w1s:
        old_tok = data.get("tokens", 0)
        data["tokens"] = min(2, old_tok + w1s)
        gained = data["tokens"] - old_tok
        tok_msg = f" | ⚪ +{gained} жетон!" if gained else ""
    else:
        tok_msg = ""

    # ── Ульта-баффы на очки ─────────────────────────────────────────────────────
    ultas_active = data.get("ultas_active", [])
    boosted_score, buff_hints = _apply_ulta_buffs(sel_score, selected_dice, ultas_active)

    # ── ✖️ Утроение спец-кубика ────────────────────────────────────────────────
    spec_active_idx = data.get("spec_active_indices", [])
    tripling_on = (action == "bank" and not is_purple_extra and
                   any(roll[i].get("special") == "tripling"
                       for i in spec_active_idx if i < len(roll)))
    if tripling_on:
        boosted_score *= 3
        buff_hints.append("✖️×3")

    data["turn_score"] += boosted_score
    buff_suffix = "  " + "  ".join(buff_hints) if buff_hints else ""
    await query.answer(f"+{fmt(boosted_score)}{tok_msg}")

    # ── ✝️ Крест / ❄️ Заморозка ───────────────────────────────────────────────
    if action == "bank" and not is_purple_extra:
        for i in spec_active_idx:
            if i >= len(roll):
                continue
            sp = roll[i].get("special")
            if sp == "cross":
                data["player_dice_bonus"] = data.get("player_dice_bonus", 0) + 1
            elif sp in ("freeze_6", "freeze_1"):
                data["bot_dice_bonus"] = data.get("bot_dice_bonus", 0) - 1

    # ── Доп. бросок фиолетовой: банк = просто завершить, не предлагаем ещё раз ─
    if is_purple_extra or action == "bank":
        if is_purple_extra:
            # Просто добавляем очки и уходим в банк
            data["phase"] = "pre_roll"
            db.set_pending_action(user_id, "solo_game", data)
            await _solo_player_bank(context, user_id, data, query.message.chat_id, query)
            return

        # ── Ульта-детектор (кроме зелёной — та триггерится при броске) ─────────
        ulta_faction = _detect_ulta(selected_dice)
        if ulta_faction and ulta_faction not in data.get("ultas_active", []):
            data["super_game_pending"] = ulta_faction
            db.set_pending_action(user_id, "solo_game", data)
            await _solo_start_super_game(context, user_id, data, query.message.chat_id, query)
            return

        # ── 🟣 Фиолетовая 2-4-6: предложить доп. бросок ───────────────────────
        if _has_purple_246(selected_dice):
            data["phase"] = "purple_extra"
            db.set_pending_action(user_id, "solo_game", data)
            await query.edit_message_text(
                _solo_status_text(data, header=(
                    f"\n🟣 *Фиолетовая 2-4-6!* Доп. бросок заслужен!\n"
                    f"Накоплено за ход: *{fmt(data['turn_score'])}*\n"
                    f"Бросай ещё 3 кубика. Бюст = основной счёт сохранится."
                )),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎲 Доп. бросок (3 кубика)", callback_data="spex_roll")],
                    [InlineKeyboardButton("✅ Забрать и передать ход",  callback_data="spex_skip")],
                ]),
            )
            return

        # ── 🥷 Вор: если активирован — бросаем кубик воровства ─────────────────
        thief_on = any(roll[i].get("special") == "thief"
                       for i in spec_active_idx if i < len(roll))
        if thief_on:
            data["thief_pending"]   = True
            data["thief_tripling"]  = tripling_on
            data["phase"] = "thief_rolling"
            db.set_pending_action(user_id, "solo_game", data)
            text = (
                _solo_status_text(data) + "\n\n"
                f"🥷 *Вор активирован!*\n"
                f"Брось кубик вора и введи результат.\n"
                f"4 → +150  |  5 → +200  |  6 → +250"
            )
            await query.edit_message_text(
                text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (1, 2, 3)],
                    [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (4, 5, 6)],
                ]),
            )
            return

        # ── ⚔️ Разбой: если активирован — кидаем с ботом ──────────────────────
        robbery_on = any(roll[i].get("special") == "robbery"
                         for i in spec_active_idx if i < len(roll))
        if robbery_on:
            data["robbery_pending"]   = True
            data["robbery_tripling"]  = tripling_on
            data["phase"] = "robbery_rolling"
            db.set_pending_action(user_id, "solo_game", data)
            text = (
                _solo_status_text(data) + "\n\n"
                f"⚔️ *Разбой!*\n"
                f"Кинь один кубик и введи значение.\n"
                f"Превысишь бота → +400 очков!"
            )
            await query.edit_message_text(
                text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (1, 2, 3)],
                    [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (4, 5, 6)],
                ]),
            )
            return

        data["spec_active_indices"] = []
        await _solo_player_bank(context, user_id, data, query.message.chat_id, query)
        return

    data["spec_active_indices"] = []

    remaining_dice = [d for i, d in enumerate(roll) if i not in selected]
    rem_specials = [d for d in remaining_dice if d.get("special")]
    rem_regular  = [d for d in remaining_dice if not d.get("special")]
    rem_color    = sum(1 for d in rem_regular if d.get("faction") is not None)
    rem_neutral  = len(rem_regular) - rem_color
    rem_sp_keys  = [d["special"] for d in rem_specials]

    bonus = data.get("token_bonus_dice", 0)
    data["token_bonus_dice"] = 0

    if not remaining_dice:
        # HOT DICE — снова полный бросок с той же расстановкой, что была в этом ходу
        # need_special берём из lineup (уже установлен), не переопределяем,
        # но нейтральных = player_neutral минус сколько слотов отдали под спец-кубики
        active_specials = len(data.get("need_special", []))
        data["need_neutral"] = data["player_neutral"] - active_specials + bonus
        data["need_color"]   = data["player_color_n"]
    else:
        data["need_neutral"] = rem_neutral + bonus
        data["need_color"]   = rem_color
        data["need_special"] = rem_sp_keys  # только оставшиеся спец-кубики (не весь lineup)
    await _solo_player_pre_roll(context, user_id, data, query.message.chat_id,
                                needs_preset=True)


async def _solo_player_bank(context, user_id: int, data: dict, chat_id: int, query):
    """Игрок забирает накопленные очки."""
    score     = data["turn_score"]
    old_total = data["player_total"]
    data["player_total"] += score
    pname  = data.get("player_display_name") or data["player_username"]
    target = data["target"]
    new_total = data["player_total"]

    msg_lines = [f"✅ *{pname}* забирает *+{fmt(score)}*  →  итого *{fmt(new_total)}*"]

    # Магазин (рубежи)
    # Сбрасываем журнал банка этого хода
    data["turn_banked"]    = []
    data["preseeded_dice"] = []

    shop_visited = data.get("shop_visited", [])
    new_thresholds = [
        t for t in crossed_thresholds(old_total, new_total, target, num_players=2)
        if t not in shop_visited
    ]

    # Триггер 2-го цветного кубика на 5000 — всегда, без условия на нейтральные
    if old_total < FACTION_2ND_THRESHOLD <= new_total and data["player_color_n"] < 2:
        data["player_color_n"] += 1
        msg_lines.append(
            f"🎉 *Бонус 5000!* Получен 2-й {data['player_color']} {data['player_color_name']}!"
        )
        # Гарантируем открытие магазина на 5000 (спец-кубик), даже если
        # crossed_thresholds не поймал порог (например, разные конфиги таргета)
        if FACTION_2ND_THRESHOLD not in new_thresholds and FACTION_2ND_THRESHOLD not in shop_visited:
            new_thresholds.append(FACTION_2ND_THRESHOLD)

    for thr in new_thresholds:
        msg_lines.append(f"🛒 Прошёл рубеж *{fmt(thr)}* — открывается магазин!")

    # ── Хелпер: отправить/изменить сообщение ───────────────────────────────────
    async def _send(text: str, markup=None):
        if query is not None:
            try:
                await query.edit_message_text(text, parse_mode="Markdown",
                                              reply_markup=markup)
                return
            except Exception:
                pass
        await context.bot.send_message(chat_id=chat_id, text=text,
                                       parse_mode="Markdown", reply_markup=markup)

    if new_total >= target:
        await _send("\n".join(msg_lines))
        await _solo_finish(context, user_id, data, chat_id, winner="player")
        return

    # ── Открываем магазин для каждого нового рубежа (очередь) ─────────────────
    if new_thresholds:
        data["shop_visited"] = data.get("shop_visited", []) + new_thresholds
        data["shop_queue"]   = new_thresholds[1:]  # остальные посетим после первого

        data["turn_score"]      = 0
        data["token_bonus_dice"] = 0
        data["need_neutral"]    = data["player_neutral"]
        data["need_color"]      = data["player_color_n"]
        data["entered_neutral"] = []
        data["entered_color"]   = []
        data["current_roll"]    = []
        data["selected"]        = []
        data["phase"]           = "shop_roll"
        data["shop_token"]      = False
        data["current"]         = "player"
        db.set_pending_action(user_id, "solo_game", data)

        msg_lines.append(_solo_shop_text(data))
        await _send("\n".join(msg_lines), _solo_shop_kb(data))
        return

    # Передаём ход боту
    data["turn_score"]      = 0
    data["token_bonus_dice"] = 0
    data["need_neutral"]    = data["player_neutral"]
    data["need_color"]      = data["player_color_n"]
    data["entered_neutral"] = []
    data["entered_color"]   = []
    data["current_roll"]    = []
    data["selected"]        = []
    data["current"]         = "bot"
    data["phase"]           = "bot"
    db.set_pending_action(user_id, "solo_game", data)

    await _send("\n".join(msg_lines))
    await _solo_bot_turn(context, user_id, data, chat_id)


async def _solo_player_bust(context, user_id: int, data: dict, chat_id: int, query):
    """Бросок без комбинаций — все накопленные очки сгорают, ход боту."""
    bust_roll = data["current_roll"]
    pname     = data.get("player_display_name") or data["player_username"]
    burned    = data["turn_score"]

    # ── 🔵 Синяя 2 спасает от бюста ────────────────────────────────────────────
    if _bust_has_blue_save(bust_roll):
        blue_text = (
            f"🔵 *Синяя 2 спасает от бюста!*\n"
            f"Бросок: {_format_dice(bust_roll)}\n"
            f"Накоплено за ход ({fmt(burned)}) *сохраняется*.\n"
            f"Перебрасываем все {len(bust_roll)} кубиков..."
        )
        data["need_neutral"]    = data["player_neutral"]
        data["need_color"]      = data["player_color_n"]
        data["entered_neutral"] = []
        data["entered_color"]   = []
        data["current_roll"]    = []
        data["selected"]        = []
        data["turn_banked"]     = []
        data["preseeded_dice"]  = []
        data["token_bonus_dice"] = 0  # жетон уже был «потрачен» в этом ходу
        # turn_score НЕ обнуляем
        db.set_pending_action(user_id, "solo_game", data)
        if query is not None:
            try:
                await query.edit_message_text(blue_text, parse_mode="Markdown")
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text=blue_text, parse_mode="Markdown")
        else:
            await context.bot.send_message(chat_id=chat_id, text=blue_text, parse_mode="Markdown")
        await _solo_player_pre_roll(context, user_id, data, chat_id)
        return

    # ── Обычный бюст ────────────────────────────────────────────────────────────
    text = (
        f"💥 *Бюст!*  Бросок: {_format_dice(bust_roll)}\n"
        f"_{pname}_ теряет *{fmt(burned)}* за ход."
    )
    data["turn_score"]      = 0
    data["need_neutral"]    = data["player_neutral"]
    data["need_color"]      = data["player_color_n"]
    data["entered_neutral"] = []
    data["entered_color"]   = []
    data["current_roll"]    = []
    data["selected"]        = []
    data["token_bonus_dice"] = 0
    data["turn_banked"]     = []
    data["preseeded_dice"]  = []
    data["current"]         = "bot"
    data["phase"]           = "bot"
    db.set_pending_action(user_id, "solo_game", data)

    if query is not None:
        try:
            await query.edit_message_text(text, parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    await _solo_bot_turn(context, user_id, data, chat_id)


# ── Bot AI turn ───────────────────────────────────────────────────────────────

def _bot_should_bank(turn_score: int, dice_remaining: int,
                     bot_total: int, player_total: int, target: int) -> bool:
    """Риск-ориентированная стратегия.

    Базовый порог банка зависит от числа оставшихся кубиков (= вероятность бюста):
      1 куб: ~67%  2: ~44%  3: ~30%  4: ~20%  5: ~13%  6: ~9%
    Порог корректируется на близость к цели и отставание от соперника.
    """
    # Всегда банкуй если это победа
    if bot_total + turn_score >= target:
        return True
    # Hot dice — все кубики в очко, кидаем снова без выбора
    if dice_remaining == 0:
        return False

    gap    = player_total - bot_total       # >0 — бот отстаёт
    needed = target - bot_total - turn_score  # сколько ещё нужно после банка

    # Базовый порог банка по числу кубиков
    BASE = {1: 100, 2: 250, 3: 500, 4: 800, 5: 1200, 6: 1700}
    threshold = BASE.get(dice_remaining, 2200)

    # Близко к победе → банкуй агрессивнее
    if needed < 1500:
        threshold = int(threshold * 0.55)
    elif needed < 3500:
        threshold = int(threshold * 0.75)

    # Сильно отстаём от игрока → рискуем, поднимаем порог
    if gap > 8000:
        threshold = int(threshold * 1.6)
    elif gap > 4000:
        threshold = int(threshold * 1.25)

    return turn_score >= threshold


async def _solo_bot_turn(context, user_id: int, data: dict, chat_id: int):
    """Полный ход бота — последовательно показываем все броски."""
    import asyncio
    target       = data["target"]
    b_color      = data["bot_color"]
    b_color_name = data["bot_color_name"]
    b_faction    = data["bot_faction"]
    bot_reserve  = data.get("bot_reserve", [])

    turn_score        = 0
    activated_specials: list[str] = []
    last_kept_dice:    list[dict] = []   # для проверки фиолетового 2-4-6 после банка

    # ✝️ Крест / ❄️ Заморозка
    # Спец-кубики резерва заменяют нейтральные (не добавляются сверху)
    dice_bonus = data.pop("bot_dice_bonus", 0)
    n_count = max(0, data["bot_neutral"] + dice_bonus - len(bot_reserve))
    c_count = data["bot_color_n"]

    # 🎯 Камбек-кубик бота
    bot_comeback = data["bot_total"] + 10000 <= data["player_total"]

    notes = []
    if dice_bonus:
        notes.append(f"{'+' if dice_bonus > 0 else ''}{dice_bonus} кубик от эффекта")
    if bot_comeback:
        notes.append("🎯 камбек-кубик")
    bonus_note = f" ({', '.join(notes)})" if notes else ""

    reserve_hint = ""
    if bot_reserve:
        reserve_hint = "  Резерв: " + " ".join(
            SPECIAL_INFO.get(k, {}).get("emoji", "?") for k in bot_reserve
        )
    log_lines = [f"\n{b_color} *Ход бота* ({b_color_name}){bonus_note}:"]
    if reserve_hint:
        log_lines.append(reserve_hint)

    async def _edit_msg():
        try:
            await msg.edit_text(
                _solo_status_text(data, header="\n".join(log_lines)),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=_solo_status_text(data, header="\n".join(log_lines)),
        parse_mode="Markdown",
    )

    while True:
        roll = _bot_roll(n_count, c_count, b_color, b_faction, bot_reserve,
                         comeback=bot_comeback)
        # Показываем камбек-кубик отдельно если есть
        cb_die = next((d for d in roll if d.get("comeback")), None)
        roll_label = f"🎲 Бросает {len(roll)}: {_format_dice(roll)}"
        if cb_die:
            roll_label += f" _(в т.ч. 🎯 {DICE_EMOJI[cb_die['value']]})_"
        log_lines.append(roll_label)

        if not scoring.has_any_score(_scoring_dice(roll)):
            # 🔵 Синяя 2 спасает бота от бюста
            if _bust_has_blue_save(roll):
                log_lines.append(
                    f"🔵 *Синяя 2 спасает от бюста!* "
                    f"Накоплено ({fmt(turn_score)}) сохраняется, перебрасывает всё."
                )
                n_count = max(0, data["bot_neutral"] - len(bot_reserve))
                c_count = data["bot_color_n"]
                await _edit_msg()
                await asyncio.sleep(1.0)
                continue
            log_lines.append(f"💥 *Бюст!*  Накоплено *{fmt(turn_score)}* сгорает.")
            turn_score = 0
            activated_specials.clear()
            await _edit_msg()
            break

        score, indices = scoring.best_score_subset(_scoring_dice(roll))
        kept_dice = [roll[i] for i in indices]

        # Собираем активированные спец-грани из ВСЕХ кубиков броска
        # (спец-кубик активируется когда показал свою грань, независимо от скоринга)
        for d in roll:
            sp = d.get("special")
            if sp and d["value"] == SPECIAL_INFO.get(sp, {}).get("face"):
                if sp not in activated_specials:
                    activated_specials.append(sp)

        # ✖️ Утроение срабатывает немедленно
        if "tripling" in activated_specials:
            activated_specials.remove("tripling")
            score *= 3
            log_lines.append(f"  ✖️ *Утроение!* Очки за этот набор ×3")

        turn_score += score
        log_lines.append(
            f"  Берёт {' '.join(_die_label(d) for d in kept_dice)} = *+{fmt(score)}* "
            f"(всего за ход: {fmt(turn_score)})"
        )

        # Считаем что осталось бросать (спец-кубики всегда возвращаются)
        remaining = [d for i, d in enumerate(roll) if i not in indices]
        n_count = sum(1 for d in remaining if not d.get("special") and d.get("faction") is None)
        c_count = sum(1 for d in remaining if not d.get("special") and d.get("faction") is not None)

        # 🔄 Рерол: перебросить все оставшиеся кубики (+ сам рерол-кубик там же)
        reroll_triggered = [sp for sp in list(activated_specials) if sp in ("reroll_6", "reroll_5")]
        if reroll_triggered and remaining:
            for sp in reroll_triggered:
                activated_specials.remove(sp)
            new_remaining = [dict(d, value=random.randint(1, 6)) for d in remaining]
            log_lines.append(
                f"  🔄 *Рерол!* {_format_dice(remaining)} → {_format_dice(new_remaining)}"
            )
            await _edit_msg()
            await asyncio.sleep(0.8)
            # Пересобираем бросок: scoring-кубики + перебросанные
            roll = kept_dice + new_remaining
            random.shuffle(roll)
            score, indices = scoring.best_score_subset(_scoring_dice(roll))
            kept_dice = [roll[i] for i in indices]
            remaining  = [d for i, d in enumerate(roll) if i not in indices]
            n_count    = sum(1 for d in remaining if not d.get("special") and d.get("faction") is None)
            c_count    = sum(1 for d in remaining if not d.get("special") and d.get("faction") is not None)

        await _edit_msg()
        await asyncio.sleep(1.2)

        total_remaining = n_count + c_count
        if _bot_should_bank(turn_score, total_remaining, data["bot_total"], data["player_total"], target):
            log_lines.append(f"  ✅ Бот забирает *+{fmt(turn_score)}*")
            last_kept_dice = kept_dice   # сохраняем для проверки фиолетового
            old_bot = data["bot_total"]
            data["bot_total"] += turn_score

            # 2-й цветной кубик у бота при 5000 — всегда
            if old_bot < FACTION_2ND_THRESHOLD <= data["bot_total"] and data["bot_color_n"] < 2:
                data["bot_color_n"] += 1
                log_lines.append(f"  🎉 *Бонус 5000!* Бот добирает 2-й {b_color} кубик")

            # ── Применяем спец-эффекты конца хода ──────────────────────────────
            for sp in activated_specials:
                if sp == "thief":
                    v     = random.randint(1, 6)
                    steal = _thief_steal(v, 2)   # solo: 2 «игрока»
                    if steal:
                        data["player_total"] = max(0, data["player_total"] - steal)
                        data["bot_total"]    += steal
                        log_lines.append(
                            f"  🥷 *Вор!* {DICE_EMOJI[v]} → бот крадёт *{fmt(steal)}* у игрока!"
                        )
                    else:
                        log_lines.append(f"  🥷 Вор мимо ({DICE_EMOJI[v]})")

                elif sp == "robbery":
                    bot_v    = random.randint(1, 6)
                    player_v = random.randint(1, 6)
                    bonus    = _robbery_bonus(2)   # solo всегда 2 «игрока»
                    if bot_v > player_v:
                        data["bot_total"]    += bonus
                        data["player_total"] = max(0, data["player_total"] - bonus)
                        log_lines.append(
                            f"  ⚔️ *Разбой!* {DICE_EMOJI[bot_v]} vs {DICE_EMOJI[player_v]} "
                            f"→ бот выигрывает *+{fmt(bonus)}*!"
                        )
                    else:
                        log_lines.append(
                            f"  ⚔️ Разбой: {DICE_EMOJI[bot_v]} vs {DICE_EMOJI[player_v]} "
                            f"→ бот проиграл (ничего)"
                        )

                elif sp == "cross":
                    data["bot_dice_bonus"] = data.get("bot_dice_bonus", 0) + 1
                    log_lines.append("  ✝️ *Крест!* +1 кубик боту в следующий ход")

                elif sp in ("freeze_6", "freeze_1"):
                    data["player_dice_bonus"] = data.get("player_dice_bonus", 0) - 1
                    log_lines.append("  ❄️ *Заморозка!* −1 кубик игроку в следующий ход")

            # ── Магазин бота ────────────────────────────────────────────────────
            new_thr = [
                t for t in crossed_thresholds(old_bot, data["bot_total"], target, num_players=2)
                if t not in data.get("bot_shop_visited", [])
            ]
            for thr in new_thr:
                bsv = list(data.get("bot_shop_visited", []))
                bsv.append(thr)
                data["bot_shop_visited"] = bsv

                br   = list(data.get("bot_reserve", []))
                v    = random.randint(1, 6)
                cnt  = 2 if v >= 4 else 1
                pool = [k for k in SHOP_POOL if SHOP_POOL.count(k) > br.count(k)]
                opts = random.sample(pool, min(cnt, len(pool)))
                pick = _bot_shop_pick(opts)
                br.append(pick)
                data["bot_reserve"] = br
                bot_reserve = br   # обновляем локальную переменную
                info = SPECIAL_INFO.get(pick, {})
                log_lines.append(
                    f"  🛒 Рубеж *{fmt(thr)}* → бот выбирает "
                    f"{info.get('emoji','?')} *{info.get('name', pick)}*"
                )

            await _edit_msg()
            break

        if total_remaining == 0:
            log_lines.append("  🔥 Hot dice — все кубики в очко, бросает заново!")
            n_count = max(0, data["bot_neutral"] - len(bot_reserve))
            c_count = data["bot_color_n"]

    # ── 🟣 Фиолетовый доп. бросок бота (если 2-4-6 попало в банк) ─────────────────
    if b_faction == "purple" and last_kept_dice and _has_purple_246(last_kept_dice):
        import asyncio as _asyncio
        extra_roll = _bot_roll(3, 0, b_color, b_faction)
        log_lines.append(f"  🟣 *Фиолетовый 2-4-6!* Доп. бросок: {_format_dice(extra_roll)}")
        if scoring.has_any_score(_scoring_dice(extra_roll)):
            extra_score, _ = scoring.best_score_subset(_scoring_dice(extra_roll))
            ulta_bonus = 500 if "purple" in data.get("ultas_active", []) else 0
            extra_score += ulta_bonus
            data["bot_total"] += extra_score
            suffix = f" +500 (ульта)" if ulta_bonus else ""
            log_lines.append(f"  = *+{fmt(extra_score)}*{suffix}")
        else:
            log_lines.append("  💥 Бюст на доп. броске — ничего")
        await _edit_msg()
        await _asyncio.sleep(0.8)

    if data["bot_total"] >= target:
        db.set_pending_action(user_id, "solo_game", data)
        await _solo_finish(context, user_id, data, chat_id, winner="bot")
        return

    data["turn_score"]      = 0
    data["entered_neutral"] = []
    data["entered_color"]   = []
    data["current_roll"]    = []
    data["selected"]        = []
    data["current"]         = "player"
    data["phase"]           = "pre_roll"
    db.set_pending_action(user_id, "solo_game", data)
    await _solo_player_pre_roll(context, user_id, data, chat_id)


# ── Game finish ───────────────────────────────────────────────────────────────

async def _solo_finish(context, user_id: int, data: dict, chat_id: int, winner: str):
    target  = data["target"]
    rated   = data["rated"]
    p_total = data["player_total"]
    b_total = data["bot_total"]
    pname   = data.get("player_display_name") or data["player_username"]
    p_color = data["player_color"]
    b_color = data["bot_color"]

    win = (winner == "player")
    if win:
        # Бонус за отрыв: насколько бот отстал к концу игры
        bot_pct = b_total / target if target else 0
        if bot_pct < 0.50:
            delta = 45   # доминирующая победа
        elif bot_pct < 0.70:
            delta = 38
        elif bot_pct < 0.85:
            delta = 32
        else:
            delta = 28   # победа в упорной борьбе
        title = f"🏆 *Победа!*  {p_color} *{pname}* выиграл."
    else:
        # Смягчаем поражение если игрок дошёл далеко
        player_pct = p_total / target if target else 0
        if player_pct >= 0.90:
            delta = -5    # почти дошёл
        elif player_pct >= 0.75:
            delta = -10
        elif player_pct >= 0.50:
            delta = -15
        else:
            delta = -20   # провальная игра
        title = f"💀 *Поражение.*  {b_color} *Бот* выиграл."

    save_error = False
    if rated:
        try:
            db.update_solo_stats(user_id, win=win, rated=True, rating_delta=delta)
            db.record_solo_game(
                user_id, score=p_total, bot_score=b_total, target=target,
                win=win, rated=True,
            )
            rating_line = f"\n{('+' if delta>=0 else '')}{delta} к соло-рейтингу"
        except Exception as e:
            logger.error("_solo_finish: DB save failed for user %s: %s", user_id, e)
            save_error = True
            rating_line = "\n⚠️ _Ошибка записи рейтинга — обратитесь к администратору._"
    else:
        rating_line = "\n_(тренировка — рейтинг не меняется)_"

    # Монеты — начисляются всегда (rated/unrated не важно, главное победа/счёт)
    coin_line = ""
    try:
        place    = 1 if win else 2
        coins    = _coins_for_game(place, p_total, target, is_solo=True)
        if coins > 0:
            db.add_coins(user_id, coins)
            coin_line = f"\n💰 +{coins} монет"
    except Exception as e:
        logger.error("_solo_finish: ошибка начисления монет uid=%s: %s", user_id, e)

    text = (
        f"{title}\n\n"
        f"{p_color} *{pname}*: {fmt(p_total)}\n"
        f"{b_color} *🤖 Бот*: {fmt(b_total)}\n"
        f"Цель: {fmt(target)}\n"
        f"{rating_line}{coin_line}"
    )
    if save_error:
        logger.error(
            "_solo_finish UNSAVED: user=%s winner=%s p=%s b=%s target=%s",
            user_id, winner, p_total, b_total, target,
        )
    db.clear_pending_action(user_id)
    await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 Ещё раз", callback_data="solo_menu")],
            [InlineKeyboardButton("🏠 В меню", callback_data="main_menu")],
        ]),
    )


async def cb_solo_quit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Первое нажатие «Сдаться» — запрашивает подтверждение."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.edit_message_text("Игра не найдена.", reply_markup=back_main_kb())
        return
    data   = gp["data"]
    p_total = data.get("player_total", 0)
    b_total = data.get("bot_total", 0)
    target  = data.get("target", 0)
    rated   = data.get("rated", False)
    rated_warn = "\n\n⚠️ *Рейтинговая игра* — сдача засчитается как поражение." if rated else ""
    await query.edit_message_text(
        f"🏳️ *Сдаться?*\n\n"
        f"Ты: {fmt(p_total)}  |  Бот: {fmt(b_total)}  |  Цель: {fmt(target)}"
        f"{rated_warn}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Да, сдаться", callback_data="solo_quit_confirm"),
                InlineKeyboardButton("❌ Продолжить", callback_data="solo_quit_cancel"),
            ],
        ]),
    )


async def cb_solo_quit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение сдачи — записываем поражение."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.edit_message_text("Игра не найдена.", reply_markup=back_main_kb())
        return
    await _solo_finish(context, user_id, gp["data"], query.message.chat_id, winner="bot")


async def cb_solo_quit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена сдачи — возвращаем экран хода."""
    query = update.callback_query
    await query.answer("Продолжаем игру!")
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.edit_message_text("Игра не найдена.", reply_markup=back_main_kb())
        return
    data  = gp["data"]
    phase = data.get("phase", "")
    roll  = data.get("current_roll", [])
    # Восстанавливаем нужный экран по текущей фазе
    if phase == "lineup_select":
        await _solo_show_lineup_msg(context, user_id, data, query.message.chat_id, query=query)
    elif phase == "reroll_pick":
        data["phase"] = "selecting"
        data.pop("reroll_pick_sel", None)
        db.set_pending_action(user_id, "solo_game", data)
        await _show_select_screen(context, query, data)
    elif phase == "selecting" and roll:
        await _show_select_screen(context, query, data)
    elif phase == "purple_extra":
        await query.edit_message_text(
            _solo_status_text(data, header=(
                f"\n🟣 *Фиолетовая 2-4-6!* Накоплено: *{fmt(data['turn_score'])}*\n"
                f"Бросай ещё 3 кубика. Бюст = основной счёт сохранится."
            )),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎲 Доп. бросок (3 кубика)", callback_data="spex_roll")],
                [InlineKeyboardButton("✅ Забрать и передать ход",  callback_data="spex_skip")],
            ]),
        )
    else:
        # Fallback — показываем статус без кнопок хода
        await _solo_player_pre_roll(context, user_id, data, query.message.chat_id)


# ── Solo leaderboard / history ────────────────────────────────────────────────

async def cb_solo_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    board = db.get_solo_leaderboard()
    back  = InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="solo_menu")]])
    if not board:
        await query.edit_message_text(
            "🏆 *Соло-рейтинг*\n\n_Ещё нет рейтинговых партий._",
            parse_mode="Markdown", reply_markup=back,
        )
        return
    lines = ["🏆 *Соло-рейтинг*\n"]
    for i, row in enumerate(board, 1):
        medal = MEDALS[i - 1] if i <= 3 else f"{i}."
        wins  = row["solo_wins"]
        gp    = row["solo_games_played"]
        lines.append(
            f"{medal} *{row['username']}*  —  {fmt(row['solo_rating'])}"
            f"  ({gp} игр, {wins} побед)"
        )
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=back,
    )


async def cb_solo_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rows  = db.get_solo_history(query.from_user.id, limit=10)
    back  = InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="solo_menu")]])
    if not rows:
        await query.edit_message_text(
            "📜 *Соло-история*\n\n_Ещё нет партий._",
            parse_mode="Markdown", reply_markup=back,
        )
        return
    lines = ["📜 *Соло-история* (последние 10)\n"]
    for r in rows:
        emoji = "🏆" if r["result"] == "win" else "💀"
        date  = (r.get("played_at") or "")[:10]
        tag   = "" if r["rated"] else " _(тренировка)_"
        lines.append(
            f"{emoji} {fmt(r['score'])} vs 🤖 {fmt(r['bot_score'])} "
            f"(до {fmt(r['target'])}) — {date}{tag}"
        )
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=back,
    )


# ── Phase 2B: новые solo-обработчики ─────────────────────────────────────────

async def _solo_start_super_game(context, user_id: int, data: dict,
                                  chat_id: int, query=None):
    """Запускает супер-игру: показывает экран ввода проверочного кубика."""
    faction = data.get("super_game_pending", "?")
    sg_cfg  = ULTA_SUPERGAME.get(faction, ([1], []))
    success_vals, reroll_vals = sg_cfg
    faction_emoji = ULTA_EMOJI.get(faction, "🎲")
    buff_name     = ULTA_BUFF_LABEL.get(faction, "Бафф")

    sv_str = " или ".join(str(v) for v in success_vals)
    rv_str = f" (при {', '.join(str(v) for v in reroll_vals)} — перебросить)" if reroll_vals else ""

    text = (
        _solo_status_text(data) + "\n\n"
        f"✨ *УЛЬТА {faction_emoji}!*\n\n"
        f"Брось проверочный кубик.\n"
        f"Выбить *{sv_str}* → получаешь перманентный бафф:\n"
        f"_{buff_name}_\n"
        f"Остальные значения — провал{rv_str}."
    )
    data["phase"] = "super_game"
    db.set_pending_action(user_id, "solo_game", data)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (1, 2, 3)],
        [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (4, 5, 6)],
    ])
    if query:
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id=chat_id, text=text,
                                   parse_mode="Markdown", reply_markup=kb)


async def _solo_resolve_super_game(context, user_id: int, data: dict,
                                    chat_id: int, query, rolled: int):
    """Обрабатывает результат проверочного броска супер-игры."""
    faction = data.get("super_game_pending", "?")
    sg_cfg  = ULTA_SUPERGAME.get(faction, ([1], []))
    success_vals, reroll_vals = sg_cfg
    faction_emoji = ULTA_EMOJI.get(faction, "🎲")
    buff_name     = ULTA_BUFF_LABEL.get(faction, "Бафф")

    if rolled in reroll_vals:
        # Перебросить — показываем тот же экран снова
        await query.answer(f"Выпало {DICE_EMOJI[rolled]} — перебрось ещё раз!")
        text = (
            _solo_status_text(data) + "\n\n"
            f"✨ *УЛЬТА {faction_emoji}*\n"
            f"Выпало {DICE_EMOJI[rolled]} — перебрось проверочный кубик:"
        )
        await query.edit_message_text(text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (1, 2, 3)],
                [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (4, 5, 6)],
            ]))
        return

    data["super_game_pending"] = None

    if rolled in success_vals:
        # Успех — активируем бафф
        ultas = list(data.get("ultas_active", []))
        if faction not in ultas:
            ultas.append(faction)
        data["ultas_active"] = ultas
        # Специфичные эффекты
        if faction == "blue":
            data["blue_rerolls"] = data.get("blue_rerolls", 0) + 3
        result_text = (
            f"🎉 *Успех!* Выпало {DICE_EMOJI[rolled]}\n"
            f"Активирован перманентный бафф:\n"
            f"_{buff_name}_"
        )

        # ⚪ Белая ульта — кража кубика у бота
        if faction == "white":
            data["phase"] = "white_steal_roll"
            db.set_pending_action(user_id, "solo_game", data)
            steal_text = (
                _solo_status_text(data, header=f"\n{result_text}") + "\n\n"
                "⚪ *Кража кубика!*\n"
                "Брось кубик чтобы определить тип:\n"
                "1-3 → нейтральный кубик\n"
                "4-6 → кубик фракции бота"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (1, 2, 3)],
                [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (4, 5, 6)],
            ])
            if query:
                await query.edit_message_text(steal_text, parse_mode="Markdown", reply_markup=kb)
            else:
                await context.bot.send_message(chat_id=chat_id, text=steal_text,
                                               parse_mode="Markdown", reply_markup=kb)
            return
    else:
        result_text = (
            f"😞 Выпало {DICE_EMOJI[rolled]} — провал.\n"
            f"Бафф не получен."
        )

    # После супер-игры возобновляем нормальный ход: банкуем текущий turn_score
    data["phase"] = "pre_roll"
    db.set_pending_action(user_id, "solo_game", data)

    await query.edit_message_text(
        _solo_status_text(data, header=f"\n{result_text}"),
        parse_mode="Markdown",
    )
    await _solo_player_bank(context, user_id, data, chat_id, None)


async def cb_solo_blue_reroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """sbrol — синий реролл (ульта): перебросить выбранные кубики бесплатно."""
    query = update.callback_query
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.answer(); return
    data = gp["data"]
    if data.get("current") != "player" or data.get("phase") not in ("selecting",):
        await query.answer(); return
    if data.get("blue_rerolls", 0) <= 0:
        await query.answer("Нет свободных рероллов!", show_alert=True); return

    selected = data.get("selected", [])
    if not selected:
        await query.answer("Сначала выбери кубики для переброски", show_alert=True); return

    roll = data["current_roll"]
    # Перебрасываем: выбранные кубики заменяются на ввод
    # Считаем тип каждого (цветной/нейтральный)
    reroll_neutral = sum(1 for i in selected if roll[i].get("faction") is None)
    reroll_color   = sum(1 for i in selected if roll[i].get("faction") is not None)

    # Убираем выбранные из roll
    new_roll = [d for i, d in enumerate(roll) if i not in selected]
    data["current_roll"] = new_roll
    data["selected"]     = []
    data["blue_rerolls"] -= 1
    # Нужно ввести новые значения
    data["phase"]           = "entering"   # ← КРИТИЧНО: без этого cb_solo_input отклоняет ввод
    data["need_neutral"]    = reroll_neutral
    data["need_color"]      = reroll_color
    data["need_special"]    = []           # спец-кубики не перебрасываются
    data["entered_neutral"] = []
    data["entered_color"]   = []
    data["entered_special"] = []
    data["blue_reroll_partial"] = True  # флаг: после ввода мержить с остатком
    db.set_pending_action(user_id, "solo_game", data)

    await query.answer(f"🔵 Синий реролл! Осталось: {data['blue_rerolls']}")
    await _solo_show_entry(context, user_id, data, query.message.chat_id, query=query)


async def cb_solo_resurrect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """sres_1 / sres_2 — воскрешение последнего забранного кубика (1 или 2 жетона)."""
    query   = update.callback_query
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.answer(); return
    data = gp["data"]
    if data.get("current") != "player" or data.get("phase") != "selecting":
        await query.answer(); return

    method = int(query.data.split("_")[1])   # 1 or 2
    tokens     = data.get("tokens", 0)
    turn_banked = data.get("turn_banked", [])

    if not turn_banked:
        await query.answer("Нет забранных кубиков!", show_alert=True); return
    if tokens < method:
        await query.answer(f"Нужно {method} жетона!", show_alert=True); return

    last_die = turn_banked[-1]
    data["tokens"] = tokens - method

    if method == 1:
        # +1 кубик к следующему броску (случайная грань)
        # Используем бонусные поля, которые _solo_player_pre_roll применит
        # поверх базовых player_neutral / player_color_n, а не перезаписывают их.
        if last_die.get("faction"):
            data["player_color_bonus"] = data.get("player_color_bonus", 0) + 1
        else:
            data["player_dice_bonus"] = data.get("player_dice_bonus", 0) + 1
        await query.answer("🔮 Кубик вернётся в следующий бросок!")
    else:
        # 2 жетона: точная копия последнего кубика пресидируется
        data["preseeded_dice"] = data.get("preseeded_dice", []) + [last_die]
        await query.answer(f"🔮🔮 {_die_label(last_die)} войдёт в следующий бросок!")

    db.set_pending_action(user_id, "solo_game", data)
    await _show_select_screen(context, query, data)


async def cb_solo_green_reroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """sgreen_<idx> — переброска зелёной 3 (🟢 фракционная способность)."""
    query = update.callback_query
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.answer(); return
    data = gp["data"]
    if data.get("current") != "player" or data.get("phase") not in ("selecting",):
        await query.answer(); return

    idx = int(query.data.split("_")[1])
    roll = data["current_roll"]
    if idx >= len(roll) or roll[idx].get("faction") != "green" or roll[idx]["value"] != 3:
        await query.answer("Этот кубик уже нельзя перебросить", show_alert=True); return

    # Если этот кубик был выбран — снимаем выбор
    selected = [i for i in data.get("selected", []) if i != idx]
    data["selected"] = selected
    data["green_reroll_idx"] = idx
    data["phase"] = "green_rerolling"
    db.set_pending_action(user_id, "solo_game", data)

    await query.answer("🟩 Кинь зелёный кубик и введи значение")
    text = (
        _solo_status_text(data) + "\n\n"
        f"🟩 *Переброска зелёной 3*\n"
        f"Бросок: {_format_dice(roll)}  (кубик №{idx + 1})\n"
        f"Введи новое выпавшее значение зелёного кубика:"
    )
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (1, 2, 3)],
            [InlineKeyboardButton(DICE_EMOJI[v], callback_data=f"sin_{v}") for v in (4, 5, 6)],
            [InlineKeyboardButton("↩️ Отмена",   callback_data="sin_back")],
        ]),
    )


async def cb_solo_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """stoken — использовать белый жетон (+1 нейтральный кубик к след. броску)."""
    query = update.callback_query
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.answer(); return
    data = gp["data"]
    if data.get("current") != "player" or data.get("phase") not in ("selecting",):
        await query.answer(); return
    if data.get("tokens", 0) <= 0:
        await query.answer("Нет жетонов!", show_alert=True); return

    data["tokens"] -= 1
    data["token_bonus_dice"] = data.get("token_bonus_dice", 0) + 1
    db.set_pending_action(user_id, "solo_game", data)
    await query.answer("♻️ Жетон применён — +1 кубик к следующему броску!")
    await _show_select_screen(context, query, data)


async def cb_solo_purple_extra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """spex_roll / spex_skip — выбор доп. броска после фиолетовой 2-4-6."""
    query = update.callback_query
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.answer(); return
    data = gp["data"]
    if data.get("phase") != "purple_extra":
        await query.answer(); return

    action = query.data.split("_")[1]   # "roll" или "skip"

    if action == "skip":
        # Пропустить доп. бросок — обычный банк
        data["phase"] = "pre_roll"
        db.set_pending_action(user_id, "solo_game", data)
        await query.answer()
        await _solo_player_bank(context, user_id, data, query.message.chat_id, query)
        return

    # Доп. бросок: 3 нейтральных кубика
    data["phase"]           = "purple_rolling"
    data["need_neutral"]    = 3
    data["need_color"]      = 0
    data["entered_neutral"] = []
    data["entered_color"]   = []
    data["current_roll"]    = []
    data["selected"]        = []
    db.set_pending_action(user_id, "solo_game", data)
    await query.answer("🎲 Кидай 3 кубика!")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=_solo_status_text(data, header=(
            f"\n🟣 *Доп. бросок!* Накоплено: *{fmt(data['turn_score'])}*\n"
            f"Введи 3 кубика (бюст = доп. очки не считаются, основной счёт сохранится):"
        )),
        parse_mode="Markdown",
        reply_markup=_entry_kb(can_back=False, can_done=False),
    )


async def cb_solo_spec_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """sspec_<idx> — вкл/выкл активацию спец-грани кубика."""
    query = update.callback_query
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.answer(); return
    data = gp["data"]
    if data.get("current") != "player" or data.get("phase") not in ("selecting",):
        await query.answer(); return

    idx = int(query.data.split("_")[1])
    roll = data.get("current_roll", [])
    if idx >= len(roll):
        await query.answer(); return

    sp = roll[idx].get("special")
    if not sp:
        await query.answer(); return

    active = list(data.get("spec_active_indices", []))
    if idx in active:
        active.remove(idx)
        await query.answer(f"✖️ {SPECIAL_INFO.get(sp, {}).get('name', sp)} отключён")
    else:
        # Снимаем выбор с этого кубика (нельзя и скорить и спец одновременно)
        selected = [i for i in data.get("selected", []) if i != idx]
        data["selected"] = selected
        active.append(idx)
        await query.answer(f"✨ {SPECIAL_INFO.get(sp, {}).get('name', sp)} активирован!")

    data["spec_active_indices"] = active
    db.set_pending_action(user_id, "solo_game", data)
    await _show_select_screen(context, query, data)


async def cb_solo_shop_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """spick_<key> — выбор спец-кубика в магазине."""
    query = update.callback_query
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.answer(); return
    data = gp["data"]
    if data.get("phase") != "shop_pick":
        await query.answer(); return

    key = query.data[6:]   # убираем "spick_"
    opts = data.get("shop_draw_opts", [])
    if key not in opts and key not in SPECIAL_INFO:
        await query.answer("Некорректный выбор", show_alert=True); return

    reserve = list(data.get("reserve", []))
    reserve.append(key)
    data["reserve"]        = reserve
    data["shop_draw_opts"] = []

    info = SPECIAL_INFO.get(key, {})
    await query.answer(f"✅ Получен {info.get('name', key)}!")
    face_hint = f"\nСпец. грань: {DICE_EMOJI[info['face']]}" if info.get("face") else ""
    got_text = (
        f"\n🛒 *Ты получил {info.get('emoji','?')} {info.get('name', key)}!*{face_hint}\n"
        f"Кубик добавлен в резерв и будет бросаться каждый ход."
    )

    # Следующий рубеж из очереди?
    shop_queue = data.get("shop_queue", [])
    if shop_queue:
        data["shop_queue"] = shop_queue[1:]
        data["phase"]      = "shop_roll"
        data["shop_token"] = False
        data["current"]    = "player"
        db.set_pending_action(user_id, "solo_game", data)
        await query.edit_message_text(
            _solo_status_text(data, header=got_text + _solo_shop_text(data)),
            parse_mode="Markdown",
            reply_markup=_solo_shop_kb(data),
        )
        return

    data["phase"]   = "bot"
    data["current"] = "bot"
    db.set_pending_action(user_id, "solo_game", data)
    await query.edit_message_text(
        _solo_status_text(data, header=got_text),
        parse_mode="Markdown",
    )
    await _solo_bot_turn(context, user_id, data, query.message.chat_id)


async def cb_solo_shop_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """sshoptoken_2n / sshoptoken_spec — обмен кубиков на жетон магазина."""
    query   = update.callback_query
    user_id = query.from_user.id
    gp = db.get_pending_action(user_id)
    if not gp or gp["action"] != "solo_game":
        await query.answer(); return
    data = gp["data"]
    if data.get("phase") != "shop_roll":
        await query.answer(); return
    if data.get("shop_token"):
        await query.answer("Жетон уже активен!", show_alert=True); return

    trade_type = query.data  # "sshoptoken_2n" or "sshoptoken_spec"

    if trade_type == "sshoptoken_2n":
        neutral = data.get("player_neutral", 0)
        if neutral < 3:
            await query.answer("Нужно минимум 3 нейтральных кубика!", show_alert=True); return
        data["player_neutral"] = neutral - 2
        msg = "🎟️ Отдал 2 нейтральных кубика → жетон магазина получен!"
    else:  # sshoptoken_spec
        reserve = list(data.get("reserve", []))
        if not reserve:
            await query.answer("Нет спец-кубиков в резерве!", show_alert=True); return
        # Убираем последний спец-кубик
        removed = reserve.pop()
        data["reserve"] = reserve
        info = SPECIAL_INFO.get(removed, {})
        msg = f"🎟️ Отдал {info.get('emoji','?')} {info.get('name', removed)} → жетон магазина получен!"

    data["shop_token"] = True
    db.set_pending_action(user_id, "solo_game", data)
    await query.answer(msg)
    await query.edit_message_text(
        _solo_status_text(data, header=_solo_shop_text(data)),
        parse_mode="Markdown",
        reply_markup=_solo_shop_kb(data),
    )


# ── Магазин и инвентарь ───────────────────────────────────────────────────────

_SHOP_SLOTS = [
    ("prefix",       "⭐ Префиксы"),
    ("suffix",       "✨ Суффиксы"),
    ("title",        "🏷 Титулы"),
    ("banner",       "🎨 Баннеры"),
    ("divider",      "➖ Разделители"),
    ("color_frame",  "🟦 Цветная рамка"),
    ("bank_fx",      "💥 Эффект банка"),
    ("win_message",  "💬 Слова победы"),
]
_RARITY_LABEL = {
    "common":    "🤍 Обычный",
    "rare":      "💙 Редкий",
    "epic":      "💜 Эпический",
    "legendary": "💛 Легендарный",
}


def _shop_item_label(item: dict, owned: bool = False) -> str:
    """Короткая строка для кнопки/листинга."""
    content = item["content"]
    slot = item["slot_type"]
    if slot in ("title", "banner", "win_message", "bank_fx"):
        short = content if len(content) <= 20 else content[:18] + "…"
        body  = f"«{short}»"
    else:
        body = content
    marker = "✓ " if owned else ""
    stock  = item.get("stock")
    if stock is not None:
        stock_tag = f" [{stock} шт]" if stock > 0 else " [нет]"
    else:
        stock_tag = ""
    return f"{marker}{body}{stock_tag} — {fmt(item['price'])} 💰"


async def cb_shop_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """shop_menu — главный экран магазина."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    coins = await adb(db.get_coins, uid)

    lines = [
        "🛒 *Магазин кастомизации*",
        "",
        f"💰 Твой баланс: *{fmt(coins)}*",
        "",
        "_Зарабатывай монеты, играя в мультиплеер и соло._",
        "_Покупай украшения для своего ника и профиля._",
    ]
    rows = [
        [InlineKeyboardButton(label, callback_data=f"shop_browse_{slot}")]
        for slot, label in _SHOP_SLOTS
    ]
    rows.append([InlineKeyboardButton("🎒 Мой инвентарь", callback_data="shop_inventory")])
    rows.append([InlineKeyboardButton("◀ В меню", callback_data="main_menu")])
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_shop_browse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """shop_browse_<slot> — каталог по типу."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    slot  = query.data[len("shop_browse_"):]
    items, coins, owned_list = await asyncio.gather(
        adb(db.list_shop_items, slot),
        adb(db.get_coins, uid),
        adb(db.get_user_items, uid, slot),
    )
    owned_ids = {it["item_id"] for it in owned_list}

    slot_label = dict(_SHOP_SLOTS).get(slot, slot)
    lines = [
        f"🛒 *{slot_label}*",
        f"💰 Баланс: *{fmt(coins)}*",
        "",
    ]
    if not items:
        lines.append("_Пусто._")
    rows = []
    for it in items:
        is_owned = it["item_id"] in owned_ids
        rows.append([InlineKeyboardButton(
            _shop_item_label(it, owned=is_owned),
            callback_data=f"shop_item_{it['item_id']}",
        )])
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="shop_menu")])
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _render_shop_item_card(query, uid: int, item_id: int):
    """Рисует карточку товара без вызова query.answer()."""
    item   = await adb(db.get_shop_item, item_id)
    if not item:
        await query.edit_message_text(
            "❌ Товар не найден.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ Магазин", callback_data="shop_menu")]]
            ),
        )
        return

    coins, owned, owners = await asyncio.gather(
        adb(db.get_coins, uid),
        adb(db.user_owns_item, uid, item_id),
        adb(db.count_item_owners, item_id),
    )
    rarity = _RARITY_LABEL.get(item.get("rarity", "common"), item.get("rarity", ""))
    stock  = item.get("stock")

    slot_label = dict(_SHOP_SLOTS).get(item["slot_type"], item["slot_type"])
    lines = [
        f"*{item['name']}*",
        slot_label,
        "",
        f"`{item['content']}`",
    ]
    if item.get("description"):
        lines.append(f"\n_{item['description']}_")
    lines.append("")
    lines.append(f"💰 Цена: *{fmt(item['price'])}*")
    lines.append(f"Редкость: {rarity}")
    # Тираж
    if stock is not None:
        if stock > 0:
            lines.append(f"📦 Осталось: *{stock}* из {stock + owners} шт")
        else:
            lines.append("📦 *Тираж закончился*")
    else:
        lines.append("📦 Тираж: безлимитный")
    lines.append(f"👥 Владельцев: *{owners}*")
    lines.append(f"\n💼 У тебя: *{fmt(coins)}* монет")
    if owned:
        lines.append("\n✅ *У тебя уже есть этот предмет.*")

    rows = []
    if owned:
        rows.append([InlineKeyboardButton("🎒 К инвентарю", callback_data="shop_inventory")])
    elif stock is not None and stock <= 0:
        rows.append([InlineKeyboardButton("❌ Тираж закончился", callback_data="shop_no_money")])
    else:
        can_afford = coins >= item["price"]
        label = f"💰 Купить за {fmt(item['price'])}" if can_afford else "❌ Недостаточно монет"
        rows.append([InlineKeyboardButton(
            label,
            callback_data=f"shop_buy_{item_id}" if can_afford else "shop_no_money",
        )])
    rows.append([InlineKeyboardButton(
        "◀ Назад", callback_data=f"shop_browse_{item['slot_type']}",
    )])
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_shop_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """shop_item_<id> — детали + покупка."""
    query = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    item_id = int(query.data[len("shop_item_"):])
    await _render_shop_item_card(query, uid, item_id)


async def cb_shop_no_money(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Заглушка для кнопки 'недостаточно монет'."""
    await update.callback_query.answer(
        "Сыграй ещё несколько игр чтобы накопить!", show_alert=True,
    )


async def cb_shop_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """shop_buy_<id> — покупка."""
    import logging
    query   = update.callback_query
    uid     = query.from_user.id
    try:
        item_id = int(query.data[len("shop_buy_"):])
    except (ValueError, IndexError):
        await query.answer("❌ Ошибка запроса.", show_alert=True)
        return
    try:
        ok, msg, new_balance = await adb(db.buy_item, uid, item_id)
        await query.answer(msg, show_alert=True)
        if ok:
            await _render_inventory(query, uid)
        else:
            await _render_shop_item_card(query, uid, item_id)
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "cb_shop_buy: uid=%s item_id=%s error: %s", uid, item_id, exc
        )
        try:
            await query.answer("❌ Произошла ошибка при покупке.", show_alert=True)
        except Exception:
            pass


async def _render_inventory(query, uid: int):
    items, coins, user = await asyncio.gather(
        adb(db.get_user_items, uid),
        adb(db.get_coins, uid),
        adb(db.get_user, uid),
    )

    lines = [
        "🎒 *Мой инвентарь*",
        f"💰 Баланс: *{fmt(coins)}*",
        "",
    ]
    if not items:
        lines.append("_Пусто. Купи что-нибудь в магазине!_")
        rows = [[InlineKeyboardButton("🛒 В магазин", callback_data="shop_menu")]]
        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    # Группируем по типу слота
    by_slot: dict[str, list] = {}
    for it in items:
        by_slot.setdefault(it["slot_type"], []).append(it)

    rows: list = []
    # Поля кастомизации в users — что сейчас надето
    equipped = {
        "prefix1":     user.get("combo_prefix1"),
        "prefix2":     user.get("combo_prefix2"),
        "suffix1":     user.get("combo_suffix1"),
        "suffix2":     user.get("combo_suffix2"),
        "title":       user.get("combo_title"),
        "banner":      user.get("combo_banner"),
        "divider":     user.get("combo_divider"),
        "color_frame": user.get("combo_color_frame"),
        "bank_fx":     user.get("combo_bank_fx"),
        "win_message": user.get("combo_win_message"),
    }
    for slot, slot_label in _SHOP_SLOTS:
        slot_items = by_slot.get(slot, [])
        if not slot_items:
            continue
        lines.append(f"*{slot_label}*")
        for it in slot_items:
            # Определяем надет ли (где именно)
            equipped_in = []
            if slot == "prefix":
                if equipped["prefix1"] == it["content"]: equipped_in.append("1")
                if equipped["prefix2"] == it["content"]: equipped_in.append("2")
            elif slot == "suffix":
                if equipped["suffix1"] == it["content"]: equipped_in.append("1")
                if equipped["suffix2"] == it["content"]: equipped_in.append("2")
            else:
                if equipped.get(slot) == it["content"]: equipped_in.append("✓")

            marker = ""
            if equipped_in:
                marker = "🟢 " + (f"(слот {','.join(equipped_in)})" if slot in ("prefix","suffix") else "(надето)")
            # Контент урезаем — у win_message может быть длинный
            ct = it["content"]
            ct_short = ct if len(ct) <= 24 else ct[:22] + "…"
            label = f"{marker} {ct_short} — {it['name']}".strip()
            rows.append([InlineKeyboardButton(
                label[:64],
                callback_data=f"inv_equip_{it['item_id']}",
            )])
        lines.append("")
    rows.append([InlineKeyboardButton("🛒 В магазин", callback_data="shop_menu")])
    rows.append([InlineKeyboardButton("◀ Меню",       callback_data="main_menu")])
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_shop_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """shop_inventory — мой инвентарь."""
    query = update.callback_query
    await query.answer()
    await _render_inventory(query, query.from_user.id)


async def _render_inv_equip(query, uid: int, item_id: int):
    """Рисует экран надевания/снятия предмета без query.answer()."""
    item, user = await asyncio.gather(
        adb(db.get_shop_item, item_id),
        adb(db.get_user, uid),
    )
    if not item or not await adb(db.user_owns_item, uid, item_id):
        await query.edit_message_text(
            "❌ Предмет не найден.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀ Инвентарь", callback_data="shop_inventory")]]
            ),
        )
        return
    slot = item["slot_type"]

    lines = [
        f"*{item['name']}*",
        f"`{item['content']}`",
        "",
    ]
    rows: list = []
    if slot in ("prefix", "suffix"):
        # Два слота — slot_field = prefix1 / prefix2
        cur1 = user.get(f"combo_{slot}1") if user else None
        cur2 = user.get(f"combo_{slot}2") if user else None
        is_in_1 = (cur1 == item["content"])
        is_in_2 = (cur2 == item["content"])
        lines.append(f"Слот 1: {cur1 or '_пусто_'}")
        lines.append(f"Слот 2: {cur2 or '_пусто_'}")
        if is_in_1:
            rows.append([InlineKeyboardButton("❌ Снять со слота 1",
                callback_data=f"inv_unequip_{slot}1_{item_id}")])
        else:
            rows.append([InlineKeyboardButton("⬇ Надеть в слот 1",
                callback_data=f"inv_set_{slot}1_{item_id}")])
        if is_in_2:
            rows.append([InlineKeyboardButton("❌ Снять со слота 2",
                callback_data=f"inv_unequip_{slot}2_{item_id}")])
        else:
            rows.append([InlineKeyboardButton("⬇ Надеть в слот 2",
                callback_data=f"inv_set_{slot}2_{item_id}")])
    else:
        # Один слот (title, banner, divider, color_frame, bank_fx, win_message)
        cur = user.get(f"combo_{slot}") if user else None
        is_on = (cur == item["content"])
        lines.append(f"Текущее: {cur or '_пусто_'}")
        if is_on:
            rows.append([InlineKeyboardButton("❌ Снять",
                callback_data=f"inv_unequip_{slot}_{item_id}")])
        else:
            rows.append([InlineKeyboardButton("⬇ Надеть",
                callback_data=f"inv_set_{slot}_{item_id}")])

    rows.append([InlineKeyboardButton("◀ Инвентарь", callback_data="shop_inventory")])
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_inv_equip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """inv_equip_<item_id> — экран выбора слота для надевания/снятия."""
    query = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    item_id = int(query.data[len("inv_equip_"):])
    await _render_inv_equip(query, uid, item_id)


def _parse_inv_slotfield_and_id(data: str, prefix: str) -> tuple[str, int]:
    """Парсит callback_data вида «inv_set_color_frame_42» → ('color_frame', 42).
    item_id — последний сегмент, slot_field — всё между prefix и item_id."""
    tail  = data[len(prefix):]          # «color_frame_42»
    parts = tail.rsplit("_", 1)         # ['color_frame', '42']
    return parts[0], int(parts[1])


async def cb_inv_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """inv_set_<slotfield>_<item_id> — надеть предмет в слот."""
    query      = update.callback_query
    uid        = query.from_user.id
    slot_field, item_id = _parse_inv_slotfield_and_id(query.data, "inv_set_")
    item = await adb(db.get_shop_item, item_id)
    if not item or not await adb(db.user_owns_item, uid, item_id):
        await query.answer("Предмет не найден.", show_alert=True)
        return
    await adb(db.update_combo_customization, uid, **{f"combo_{slot_field}": item["content"]})
    await query.answer(f"✅ Надето: {item['name']}")
    await _render_inv_equip(query, uid, item_id)


async def cb_inv_unequip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """inv_unequip_<slotfield>_<item_id> — снять предмет."""
    query      = update.callback_query
    uid        = query.from_user.id
    slot_field, item_id = _parse_inv_slotfield_and_id(query.data, "inv_unequip_")
    await adb(db.update_combo_customization, uid, **{f"combo_{slot_field}": None})
    await query.answer("❌ Снято.")
    await _render_inv_equip(query, uid, item_id)


# ── Casino ────────────────────────────────────────────────────────────────────

# Сектора колеса: (вес, название, эмодзи, множитель_выплаты)
# mult=0 → потеря; mult=1 → возврат/предмет; mult>1 → выигрыш
# (вес, метка, emoji, mult)
# mult=-2 = особый сектор "Слив": теряешь ×2 ставки (но не уходишь в минус)
_ROULETTE_SECTORS = [
    (43, "Пусто",    "💀",  0),   # 43% — потеря
    (8,  "Слив",     "💣", -2),   # 8%  — ×2 потеря
    (10, "Возврат",  "🔄",  1),   # 10% — возврат ставки
    (20, "×2",       "💰",  2),   # 20% — удвоение
    (10, "×3",       "💎",  3),   # 10% — утроение
    (4,  "×5",       "🌟",  5),   # 4%  — пятикратно
    (2,  "×10",      "💥",  10),  # 2%  — десятикратно
    (1,  "×25",      "⚡",  25),  # 1%  — мега-выигрыш
    (2,  "Предмет",  "🎁",  1),   # 2%  — предмет из магазина
]

_ROULETTE_BETS = [50, 100, 250, 500, 1_000, 2_500]

# ── VIP Зал ──────────────────────────────────────────────────────────────────
# ── Зал Богатства ─────────────────────────────────────────────────────────────
_HALL_PLAQUE_TIERS = {
    "bronze":  {"price":   100_000, "emoji": "🥉", "label": "Бронзовая",      "desc": "Первый шаг к вечности"},
    "silver":  {"price":   500_000, "emoji": "🥈", "label": "Серебряная",     "desc": "Серьёзная заявка"},
    "gold":    {"price": 2_000_000, "emoji": "🥇", "label": "Золотая",        "desc": "Настоящий меценат"},
    "diamond": {"price":10_000_000, "emoji": "💎", "label": "Бриллиантовая",  "desc": "Легенда клуба навсегда"},
}
_HALL_TIER_ORDER = ["diamond", "gold", "silver", "bronze"]  # порядок отображения

_VIP_ENTRY_MIN_BALANCE = 100_000
_VIP_ENTRY_FEE         = 10_000

_VIP_ROULETTE_SECTORS = [
    (45, "Пусто",   "💀",    0),   # 45% — потеря
    (10, "Слив",    "💣",   -2),   # 10% — двойная потеря
    (8,  "Возврат", "🔄",    1),   # 8%  — возврат
    (15, "×2",      "💰",    2),   # 15% — удвоение
    (8,  "×5",      "💎",    5),   # 8%  — пятикратно
    (6,  "×10",     "🌟",   10),   # 6%  — десятикратно
    (4,  "×25",     "💥",   25),   # 4%  — мега
    (2,  "×50",     "⚡",   50),   # 2%  — супер
    (2,  "×100",    "👑",  100),   # 2%  — легенда
]

_VIP_ROULETTE_BETS = [10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000]
_VIP_LOBBY_FEES    = [10_000, 25_000, 50_000, 100_000, 250_000, 500_000]

# ── Casino Shares ─────────────────────────────────────────────────────────────
_SHARES_BUY_AMOUNTS  = [1, 5, 10, 25, 50]   # варианты при покупке
_SHARES_SELL_AMOUNTS = [1, 5, 10, 25]        # варианты при продаже

_VIP_WHEEL_DISPLAY = (
    "╔══════════════════════════╗\n"
    "║  💀 Пусто        45%   ║\n"
    "║  💣 Слив ×2      10%   ║\n"
    "║  🔄 Возврат       8%   ║\n"
    "║  💰 ×2           15%   ║\n"
    "║  💎 ×5            8%   ║\n"
    "║  🌟 ×10           6%   ║\n"
    "║  💥 ×25           4%   ║\n"
    "║  ⚡ ×50            2%   ║\n"
    "║  👑 ×100           2%   ║\n"
    "╚══════════════════════════╝"
)

_VIP_SPIN_FRAMES = [
    "💀  💣  🔄  💰  💎  🌟  💥  ⚡  👑",
    "👑  💀  💣  🔄  💰  💎  🌟  💥  ⚡",
    "⚡  👑  💀  💣  🔄  💰  💎  🌟  💥",
    "💥  ⚡  👑  💀  💣  🔄  💰  💎  🌟",
]

# Покер — масти и ранги
_VIP_DECK_SUITS = ["♠", "♥", "♦", "♣"]
_VIP_RANK_NAMES = {2:"2",3:"3",4:"4",5:"5",6:"6",7:"7",8:"8",9:"9",
                   10:"10",11:"J",12:"Q",13:"K",14:"A"}
_VIP_HAND_NAMES = {
    8: "🔥 Стрит-Флеш",
    7: "💎 Каре",
    6: "🃏 Фулл-Хаус",
    5: "♠ Флеш",
    4: "➡ Стрит",
    3: "🎯 Тройка",
    2: "✌ Две Пары",
    1: "👌 Пара",
    0: "🃏 Старшая Карта",
}

_WHEEL_DISPLAY = (
    "╔═══════════════════════╗\n"
    "║  💀 Пусто      43%   ║\n"
    "║  💣 Слив ×2     8%   ║\n"
    "║  🔄 Возврат    10%   ║\n"
    "║  💰 ×2         20%   ║\n"
    "║  💎 ×3         10%   ║\n"
    "║  🌟 ×5          4%   ║\n"
    "║  💥 ×10         2%   ║\n"
    "║  ⚡ ×25          1%   ║\n"
    "║  🎁 Предмет     2%   ║\n"
    "╚═══════════════════════╝"
)

_SPIN_FRAMES = [
    "💀  💣  🔄  💰  💎  🌟  💥  ⚡  🎁",
    "🎁  💀  💣  🔄  💰  💎  🌟  💥  ⚡",
    "⚡  🎁  💀  💣  🔄  💰  💎  🌟  💥",
    "💥  ⚡  🎁  💀  💣  🔄  💰  💎  🌟",
]


def _spin_roulette() -> dict:
    """Случайный сектор с учётом весов."""
    weights = [s[0] for s in _ROULETTE_SECTORS]
    w, label, emoji, mult = random.choices(_ROULETTE_SECTORS, weights=weights, k=1)[0]
    is_item = (label == "Предмет")
    is_doom = (mult == -2)          # сектор "Слив" — теряешь ×2 ставки
    return {"label": label, "emoji": emoji, "mult": mult, "is_item": is_item, "is_doom": is_doom}


def _roulette_item_prize(uid: int):
    """Случайный не-купленный предмет. Common встречается чаще."""
    all_items  = db.list_shop_items()
    owned_ids  = {it["item_id"] for it in db.get_user_items(uid)}
    candidates = [it for it in all_items if it["item_id"] not in owned_ids]
    if not candidates:
        return None
    rw = {"common": 40, "rare": 20, "epic": 8, "legendary": 2}
    weights = [rw.get(it.get("rarity", "common"), 20) for it in candidates]
    return random.choices(candidates, weights=weights, k=1)[0]


# ── Зал Богатства — хендлеры ─────────────────────────────────────────────────

async def cb_hall_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """hall_menu — главный экран Зала Богатства."""
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    user = await adb(db.get_user, uid)
    if not user:
        await query.answer("Профиль не найден.", show_alert=True); return

    coins          = user.get("coins", 0)
    max_bal        = user.get("max_balance", 0)
    max_win        = user.get("max_casino_win", 0)
    my_plaques     = await adb(db.get_user_plaques, uid)
    my_tiers       = {p["tier"] for p in my_plaques}
    all_plaques    = await adb(db.get_hall_plaques, 50)

    # Считаем сколько табличек каждого тира
    tier_counts = {t: 0 for t in _HALL_TIER_ORDER}
    for p in all_plaques:
        if p["tier"] in tier_counts:
            tier_counts[p["tier"]] += 1

    tier_summary = "  ".join(
        f"{_HALL_PLAQUE_TIERS[t]['emoji']}{tier_counts[t]}"
        for t in _HALL_TIER_ORDER
    )

    # Личные рекорды
    records_lines = (
        f"📈 Макс. баланс:       *{fmt(max_bal)}* 💰\n"
        f"🎰 Рекорд казино:      *{fmt(max_win)}* 💰"
    )

    # Мои таблички
    if my_tiers:
        my_pl_str = "  ".join(
            _HALL_PLAQUE_TIERS[t]["emoji"]
            for t in _HALL_TIER_ORDER if t in my_tiers
        )
        my_pl_line = f"🏛 Мои таблички: {my_pl_str}"
    else:
        my_pl_line = "🏛 Табличек ещё нет"

    text = (
        "🏛 *ЗАЛ БОГАТСТВА*\n\n"
        f"💰 Баланс: *{fmt(coins)}* монет\n\n"
        f"{records_lines}\n\n"
        f"{my_pl_line}\n"
        f"Всего табличек: {tier_summary}\n\n"
        "_Увековечь своё имя в истории клуба._"
    )
    rows = [
        [InlineKeyboardButton("🏛 Таблички",       callback_data="hall_plaques"),
         InlineKeyboardButton("🏆 Рекорды",        callback_data="hall_records")],
        [InlineKeyboardButton("🛍 Эксклюзив Зала", callback_data="hall_shop")],
        [InlineKeyboardButton("💎 Купить табличку", callback_data="hall_buy")],
        [InlineKeyboardButton("◀ В меню",          callback_data="main_menu")],
    ]
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))


async def cb_hall_plaques(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """hall_plaques — список всех именных табличек."""
    query = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    plaques = await adb(db.get_hall_plaques, 50)

    if not plaques:
        text = "🏛 *ИМЕННЫЕ ТАБЛИЧКИ*\n\n_Зал пуст. Стань первым!_"
    else:
        lines = ["🏛 *ИМЕННЫЕ ТАБЛИЧКИ*\n"]
        current_tier = None
        for p in plaques:
            tier_cfg = _HALL_PLAQUE_TIERS.get(p["tier"], {})
            if p["tier"] != current_tier:
                current_tier = p["tier"]
                lines.append(f"\n*{tier_cfg.get('emoji','')} {tier_cfg.get('label','')}*")
            u     = await adb(db.get_user, p["user_id"])
            uname = (u.get("username") or u.get("first_name", "?")) if u else "?"
            msg   = f" — _{p['message']}_" if p.get("message") else ""
            mine  = " ◄" if p["user_id"] == uid else ""
            lines.append(f"@{uname}{msg}{mine}")
        text = "\n".join(lines)

    rows = [
        [InlineKeyboardButton("💎 Купить табличку", callback_data="hall_buy")],
        [InlineKeyboardButton("◀ Назад",           callback_data="hall_menu")],
    ]
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))


async def cb_hall_records(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """hall_records — таблица рекордов клуба."""
    query = update.callback_query
    await query.answer()

    # Топ-5 по макс. балансу и топ-5 по макс. выигрышу в казино
    def _fetch_records():
        client = db._db()
        r_bal = client.table("users").select(
            "user_id,username,first_name,max_balance"
        ).order("max_balance", desc=True).limit(5).execute()
        r_win = client.table("users").select(
            "user_id,username,first_name,max_casino_win"
        ).order("max_casino_win", desc=True).limit(5).execute()
        return r_bal.data or [], r_win.data or []

    bal_data, win_data = await adb(_fetch_records)

    uid  = query.from_user.id
    user = await adb(db.get_user, uid)

    def _name(row):
        return row.get("username") or row.get("first_name") or "?"

    bal_lines = []
    for i, row in enumerate(bal_data, 1):
        me = " ◄" if row["user_id"] == uid else ""
        bal_lines.append(f"{i}. @{_name(row)} — *{fmt(row['max_balance'])}* 💰{me}")

    win_lines = []
    for i, row in enumerate(win_data, 1):
        me = " ◄" if row["user_id"] == uid else ""
        win_lines.append(f"{i}. @{_name(row)} — *{fmt(row['max_casino_win'])}* 💰{me}")

    my_bal = user.get("max_balance", 0) if user else 0
    my_win = user.get("max_casino_win", 0) if user else 0

    text = (
        "🏆 *РЕКОРДЫ КЛУБА*\n\n"
        "📈 *Максимальный баланс:*\n"
        + "\n".join(bal_lines or ["_нет данных_"]) +
        "\n\n🎰 *Лучший выигрыш в казино:*\n"
        + "\n".join(win_lines or ["_нет данных_"]) +
        f"\n\n_Твой макс. баланс: {fmt(my_bal)} 💰_\n"
        f"_Твой рекорд казино: {fmt(my_win)} 💰_"
    )
    rows = [[InlineKeyboardButton("◀ Назад", callback_data="hall_menu")]]
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))


async def cb_hall_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """hall_shop — эксклюзивные предметы Зала Богатства."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    coins = await adb(db.get_coins, uid)
    items = await adb(db.list_hall_shop_items)
    owned_ids = {it["item_id"] for it in await adb(db.get_user_items, uid)}

    text = (
        "🛍 *ЭКСКЛЮЗИВ ЗАЛА БОГАТСТВА*\n\n"
        f"💰 Баланс: *{fmt(coins)}* монет\n\n"
        "_Предметы доступны только здесь._\n"
    )
    rows = []
    for it in items:
        is_owned = it["item_id"] in owned_ids
        rows.append([InlineKeyboardButton(
            _shop_item_label(it, owned=is_owned),
            callback_data=f"shop_item_{it['item_id']}",
        )])
    if not items:
        text += "\n_Пусто._"
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="hall_menu")])
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))


async def cb_hall_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """hall_buy — выбор тира таблички."""
    query = update.callback_query
    await query.answer()
    uid       = query.from_user.id
    coins     = await adb(db.get_coins, uid)
    my_tiers  = {p["tier"] for p in await adb(db.get_user_plaques, uid)}

    text = (
        "💎 *ИМЕННАЯ ТАБЛИЧКА*\n\n"
        f"💰 Баланс: *{fmt(coins)}* монет\n\n"
        "Твоё имя навсегда останется в Зале Богатства.\n"
        "После покупки введёшь личную подпись (до 60 символов).\n\n"
        "Выбери тир:\n"
    )
    rows = []
    for tier in _HALL_TIER_ORDER:
        cfg = _HALL_PLAQUE_TIERS[tier]
        if tier in my_tiers:
            rows.append([InlineKeyboardButton(
                f"✅ {cfg['emoji']} {cfg['label']} — уже куплена",
                callback_data="noop",
            )])
        else:
            can = coins >= cfg["price"]
            rows.append([InlineKeyboardButton(
                f"{'✓' if can else '🚫'} {cfg['emoji']} {cfg['label']} — {fmt(cfg['price'])} 💰",
                callback_data=f"hall_buytier_{tier}" if can else "shop_no_money",
            )])
        text += f"\n{cfg['emoji']} *{cfg['label']}* — {fmt(cfg['price'])} 💰\n_{cfg['desc']}_\n"

    rows.append([InlineKeyboardButton("◀ Назад", callback_data="hall_menu")])
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))


async def cb_hall_buy_tier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """hall_buytier_<tier> — покупаем табличку, просим ввести подпись."""
    query = update.callback_query
    uid   = query.from_user.id
    tier  = query.data[len("hall_buytier_"):]
    cfg   = _HALL_PLAQUE_TIERS.get(tier)
    if not cfg:
        await query.answer("Неизвестный тир.", show_alert=True); return

    await query.answer()
    # Ставим pending_action — ждём текстовый ввод подписи
    await adb(db.set_pending_action, uid, "hall_plaque_msg", {
        "tier": tier, "price": cfg["price"]
    })

    text = (
        f"{cfg['emoji']} *{cfg['label']} табличка*\n\n"
        f"Цена: *{fmt(cfg['price'])} 💰*\n\n"
        "Введи личную подпись для таблички:\n"
        "_Например: «Первый среди равных» или «Сюда заходил @username»_\n\n"
        "_(До 60 символов. Отправь точку `.` чтобы оставить пустой)_"
    )
    rows = [[InlineKeyboardButton("◀ Отмена", callback_data="hall_buy")]]
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))


# ── VIP helpers ──────────────────────────────────────────────────────────────

def _vip_spin_roulette() -> dict:
    """Случайный VIP сектор."""
    weights = [s[0] for s in _VIP_ROULETTE_SECTORS]
    w, label, emoji, mult = random.choices(_VIP_ROULETTE_SECTORS, weights=weights, k=1)[0]
    return {"label": label, "emoji": emoji, "mult": mult, "is_doom": (mult == -2)}


def _vip_deal_hands(num_players: int) -> list:
    """Раздаёт по 5 карт каждому (стандартная колода 52 карты)."""
    deck = [[r, s] for r in range(2, 15) for s in range(4)]
    random.shuffle(deck)
    return [deck[i * 5:(i + 1) * 5] for i in range(num_players)]


def _vip_hand_score(hand: list) -> tuple:
    """Оценивает 5-карточную покерную руку.
    Возвращает (rank_int 0–8, tiebreakers_list). Больше — лучше."""
    from collections import Counter
    ranks = sorted([c[0] for c in hand], reverse=True)
    suits = [c[1] for c in hand]
    is_flush    = len(set(suits)) == 1
    is_straight = (ranks == list(range(ranks[0], ranks[0] - 5, -1)))
    # Колесо (A-2-3-4-5): Туз считается как 1
    if set(ranks) == {14, 2, 3, 4, 5}:
        is_straight = True
        ranks = [5, 4, 3, 2, 1]
    cnt    = Counter(ranks)
    counts = sorted(cnt.values(), reverse=True)
    groups = sorted(cnt.keys(), key=lambda r: (cnt[r], r), reverse=True)
    if is_straight and is_flush:
        return (8, ranks)
    if counts == [4, 1]:
        return (7, groups)
    if counts == [3, 2]:
        return (6, groups)
    if is_flush:
        return (5, ranks)
    if is_straight:
        return (4, ranks)
    if counts == [3, 1, 1]:
        return (3, groups)
    if counts == [2, 2, 1]:
        return (2, groups)
    if counts == [2, 1, 1, 1]:
        return (1, groups)
    return (0, ranks)


def _vip_card_str(card: list) -> str:
    r, s = card[0], card[1]
    return f"{_VIP_RANK_NAMES.get(r, str(r))}{_VIP_DECK_SUITS[s]}"


def _vip_hand_str(hand: list) -> str:
    return "  ".join(_vip_card_str(c) for c in hand)


# ══════════════════════════════════════════════════════════════════════════════
# 📈  АКЦИИ КАЗИНО
# ══════════════════════════════════════════════════════════════════════════════

def _shares_menu_text(eco: dict, portfolio: dict, coins: int) -> str:
    """Формирует текст главного экрана акций."""
    sold      = int(eco.get("shares_sold", 0))
    pool      = int(eco.get("dividend_pool", 0))
    available = db.TOTAL_SHARES - sold
    my_shares = int(portfolio.get("shares", 0))
    claimed   = int(portfolio.get("dividends_claimed", 0))
    my_owed   = max(0, int(pool * my_shares / db.TOTAL_SHARES) - claimed) if my_shares else 0

    lines = [
        "📈 *АКЦИИ КАЗИНО*\n",
        f"🏦 *Рынок:*",
        f"  Всего акций: *{db.TOTAL_SHARES:,}*",
        f"  Продано: *{sold:,}*  |  Доступно: *{available:,}*",
        f"  Цена покупки: *{fmt(db.SHARE_PRICE_BUY)} 💰*",
        f"  Цена продажи: *{fmt(db.SHARE_PRICE_SELL)} 💰*\n",
        f"📊 *Дивидендный пул: {fmt(pool)} 💰*",
        f"  _(5% каждого проигрыша уходит в пул)_\n",
        f"💼 *Твой портфель:*",
        f"  Акций: *{my_shares}* из макс. {db.MAX_SHARES_PER_PLAYER}",
        f"  Уже получено дивидендов: *{fmt(claimed)} 💰*",
    ]
    if my_shares > 0:
        lines.append(f"  Доступно к выплате: *{fmt(my_owed)} 💰*")
    lines.append(f"\n💰 Твой баланс: *{fmt(coins)} 💰*")
    return "\n".join(lines)


def _shares_menu_keyboard(eco: dict, portfolio: dict, coins: int) -> InlineKeyboardMarkup:
    """Строит клавиатуру для экрана акций."""
    sold      = int(eco.get("shares_sold", 0))
    pool      = int(eco.get("dividend_pool", 0))
    available = db.TOTAL_SHARES - sold
    my_shares = int(portfolio.get("shares", 0))
    claimed   = int(portfolio.get("dividends_claimed", 0))
    my_owed   = max(0, int(pool * my_shares / db.TOTAL_SHARES) - claimed) if my_shares else 0
    rows = []
    if available > 0 and my_shares < db.MAX_SHARES_PER_PLAYER:
        buy_row = []
        for n in _SHARES_BUY_AMOUNTS:
            can_buy = (available >= n
                       and coins >= db.SHARE_PRICE_BUY * n
                       and my_shares + n <= db.MAX_SHARES_PER_PLAYER)
            buy_row.append(InlineKeyboardButton(
                f"{'🛒' if can_buy else '🚫'} +{n}",
                callback_data=f"shares_buy_{n}" if can_buy else "shares_cant",
            ))
        rows.append(buy_row)
    if my_shares > 0:
        sell_row = []
        for n in _SHARES_SELL_AMOUNTS:
            can_sell = my_shares >= n
            sell_row.append(InlineKeyboardButton(
                f"💸 -{n}" if can_sell else f"🚫 -{n}",
                callback_data=f"shares_sell_{n}" if can_sell else "shares_cant",
            ))
        rows.append(sell_row)
    if my_shares > 0:
        lbl = f"💰 Получить {fmt(my_owed)} 💰" if my_owed > 0 else "⏳ Дивиденды"
        rows.append([InlineKeyboardButton(lbl, callback_data="shares_claim")])
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="casino_shares"),
                 InlineKeyboardButton("◀ Казино",   callback_data="casino_menu")])
    return InlineKeyboardMarkup(rows)


async def _render_shares_screen(query, uid: int) -> None:
    """Перерисовывает экран акций казино."""
    def _fetch():
        return db.get_casino_economy(), db.get_casino_shares(uid), db.get_coins(uid)
    eco, portfolio, coins = await adb(_fetch)
    await query.edit_message_text(
        _shares_menu_text(eco, portfolio, coins),
        parse_mode="Markdown",
        reply_markup=_shares_menu_keyboard(eco, portfolio, coins),
    )


async def cb_casino_shares(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_shares — главный экран акций казино."""
    query = update.callback_query
    await query.answer()
    await _render_shares_screen(query, query.from_user.id)


async def cb_shares_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """shares_buy_<count> — покупаем count акций."""
    query = update.callback_query
    uid   = query.from_user.id
    try:
        count = int(query.data[len("shares_buy_"):])
    except ValueError:
        await query.answer("Ошибка.", show_alert=True); return

    ok, err = await adb(db.buy_casino_shares, uid, count)
    if not ok:
        await query.answer(err, show_alert=True); return

    total = db.SHARE_PRICE_BUY * count
    await query.answer(f"✅ Куплено {count} акций за {fmt(total)} монет!")
    await _render_shares_screen(query, uid)


async def cb_shares_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """shares_sell_<count> — продаём count акций."""
    query = update.callback_query
    uid   = query.from_user.id
    try:
        count = int(query.data[len("shares_sell_"):])
    except ValueError:
        await query.answer("Ошибка.", show_alert=True); return

    ok, err = await adb(db.sell_casino_shares, uid, count)
    if not ok:
        await query.answer(err, show_alert=True); return

    payout = db.SHARE_PRICE_SELL * count
    await query.answer(f"✅ Продано {count} акций за {fmt(payout)} монет!")
    await _render_shares_screen(query, uid)


async def cb_shares_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """shares_claim — получаем дивиденды (60-минутный кулдаун)."""
    query = update.callback_query
    uid   = query.from_user.id

    ok, amount, err = await adb(db.claim_dividends, uid)
    if not ok:
        await query.answer(err, show_alert=True)
    else:
        await query.answer(f"💰 Получено {fmt(amount)} монет дивидендов!", show_alert=True)

    await _render_shares_screen(query, uid)


async def cb_shares_cant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """shares_cant — заглушка для неактивных кнопок акций."""
    await update.callback_query.answer(
        "Недоступно: недостаточно монет, акций или превышен лимит.",
        show_alert=True,
    )


async def cb_casino_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_menu — главный экран казино."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    coins = await adb(db.get_coins, uid)

    state = await adb(db.get_global_roulette_state)
    pot   = state.get("pot", 0)
    text = (
        "🎰 *КАЗИНО*\n\n"
        f"💰 Твой баланс: *{fmt(coins)}* монет\n\n"
        f"🌍 Глобальный котёл: *{fmt(pot)} 💰*\n\n"
        f"{_WHEEL_DISPLAY}"
    )
    rows = [
        [InlineKeyboardButton("🎡 Рулетка",        callback_data="casino_roulette"),
         InlineKeyboardButton("🎰 Слоты",          callback_data="casino_slots")],
        [InlineKeyboardButton("🃏 Блэкджек",       callback_data="casino_bj"),
         InlineKeyboardButton("🌍 Глоб. рулетка",  callback_data="casino_global")],
        [InlineKeyboardButton("💎 ВИП Зал",        callback_data="vip_hall"),
         InlineKeyboardButton("📈 Акции казино",   callback_data="casino_shares")],
        [InlineKeyboardButton("◀ В меню",          callback_data="main_menu")],
    ]
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_casino_roulette(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_roulette — выбор ставки перед спином."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    coins = await adb(db.get_coins, uid)

    rows = []
    bets  = _ROULETTE_BETS
    for i in range(0, len(bets), 2):
        pair = bets[i:i + 2]
        row  = []
        for amt in pair:
            can = coins >= amt
            row.append(InlineKeyboardButton(
                f"{'🪙' if can else '🚫'} {fmt(amt)}",
                callback_data=f"casino_spin_{amt}" if can else "casino_no_coins",
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="casino_menu")])

    await query.edit_message_text(
        f"🎡 *РУЛЕТКА*\n\n"
        f"💰 Баланс: *{fmt(coins)}* монет\n\n"
        "Выбери ставку — и крути!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_casino_no_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажали на кнопку со ставкой которую не потянуть."""
    await update.callback_query.answer(
        "Недостаточно монет для этой ставки! Сыграй ещё несколько игр.", show_alert=True,
    )


async def cb_casino_spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_spin_<amount> — списываем ставку, анимация, результат."""
    query = update.callback_query
    uid   = query.from_user.id
    try:
        bet = int(query.data[len("casino_spin_"):])
    except ValueError:
        await query.answer("Ошибка ставки.", show_alert=True); return

    # Списываем ставку до анимации — чтобы не было двойного спина
    ok, balance_after_bet = await adb(db.spend_coins, uid, bet)
    if not ok:
        await query.answer("Недостаточно монет!", show_alert=True); return
    await query.answer()

    retry_btn = InlineKeyboardButton("🎡 Крутить ещё", callback_data="casino_roulette")
    back_btn  = InlineKeyboardButton("◀ Казино",       callback_data="casino_menu")

    try:
        # ── Анимация ─────────────────────────────────────────────────────────────
        for frame in _SPIN_FRAMES:
            await query.edit_message_text(
                f"🎰 *Колесо крутится...*\n\n"
                f"`{frame}`\n\n"
                f"_Ставка: {fmt(bet)} 💰_",
                parse_mode="Markdown",
            )
            await asyncio.sleep(0.7)

        # ── Спин ─────────────────────────────────────────────────────────────────
        result   = _spin_roulette()
        mult     = result["mult"]
        emoji    = result["emoji"]
        label    = result["label"]
        is_item  = result["is_item"]
        is_doom  = result["is_doom"]

        # ── Потеря ───────────────────────────────────────────────────────────────
        if mult == 0:
            fire_and_forget(adb(db.add_to_dividend_pool, max(1, bet // 20)))
            new_balance = await adb(db.get_coins, uid)
            text = (
                f"💀 *ПУСТО*\n\n"
                f"Выпало: {emoji} *{label}*\n\n"
                f"Ставка *{fmt(bet)} 💰* — потеряна.\n\n"
                f"💼 Баланс: *{fmt(new_balance)} 💰*"
            )
            rows = [[retry_btn, back_btn]]

        # ── Слив (×2 потеря, но не в минус) ──────────────────────────────────────
        elif is_doom:
            # Первая ставка уже снята до анимации; снимаем вторую (floor=0)
            _, new_balance = await adb(db.spend_coins, uid, bet)
            fire_and_forget(adb(db.add_to_dividend_pool, max(1, bet * 2 // 20)))
            text = (
                f"💣 *СЛИВ!*\n\n"
                f"Выпало: {emoji} *{label}*\n\n"
                f"Штраф: ×2 ставки — ещё *{fmt(bet)} 💰* сгорело!\n\n"
                f"💼 Баланс: *{fmt(new_balance)} 💰*"
            )
            rows = [[retry_btn, back_btn]]

        # ── Предмет ──────────────────────────────────────────────────────────────
        elif is_item:
            prize = await adb(_roulette_item_prize, uid)
            if prize:
                item_granted = await adb(db.grant_item, uid, prize["item_id"])
                # Ставка возвращается
                new_balance = await adb(db.add_coins, uid, bet)
                slot_label  = dict(_SHOP_SLOTS).get(prize["slot_type"], prize["slot_type"])
                rar_label   = _RARITY_LABEL.get(prize.get("rarity", "common"), "")
                if item_granted:
                    text = (
                        f"🎁 *РЕДКИЙ ПРИЗ!*\n\n"
                        f"Выпало: {emoji} *{label}*\n\n"
                        f"🏆 *{prize['name']}*\n"
                        f"{slot_label}  |  {rar_label}\n"
                        f"`{prize['content']}`\n\n"
                        f"Предмет добавлен в инвентарь!\n"
                        f"Ставка *{fmt(bet)} 💰* возвращена.\n\n"
                        f"💼 Баланс: *{fmt(new_balance)} 💰*"
                    )
                    rows = [
                        [InlineKeyboardButton("🎒 Инвентарь", callback_data="shop_inventory")],
                        [retry_btn, back_btn],
                    ]
                else:
                    # Предмет уже есть → x3 вместо
                    bonus = bet * 3
                    new_balance = await adb(db.add_coins, uid, bonus)
                    text = (
                        f"🎁 *ПОДАРОК!*\n\n"
                        f"Выпало: {emoji} *{label}*\n\n"
                        f"Все доступные предметы уже у тебя!\n"
                        f"Получаешь компенсацию *×3 монеты*.\n\n"
                        f"Выигрыш: *+{fmt(bonus)} 💰*\n"
                        f"💼 Баланс: *{fmt(new_balance)} 💰*"
                    )
                    rows = [[retry_btn, back_btn]]
            else:
                # Нет не-купленных предметов → x3 монеты
                bonus = bet * 3
                new_balance = await adb(db.add_coins, uid, bonus)
                text = (
                    f"🎁 *ПОДАРОК!*\n\n"
                    f"Выпало: {emoji} *{label}*\n\n"
                    f"Вся коллекция уже твоя — *×3 монеты* вместо предмета!\n\n"
                    f"Выигрыш: *+{fmt(bonus)} 💰*\n"
                    f"💼 Баланс: *{fmt(new_balance)} 💰*"
                )
                rows = [[retry_btn, back_btn]]

        # ── Монеты (возврат или множитель) ───────────────────────────────────────
        else:
            winnings    = bet * mult
            net_gain    = winnings - bet
            new_balance = await adb(db.add_coins, uid, winnings)
            if net_gain > 0 and mult >= 5:
                fire_and_forget(adb(db.update_max_casino_win, uid, net_gain))
            if mult == 1:
                text = (
                    f"🔄 *ВОЗВРАТ*\n\n"
                    f"Выпало: {emoji} *{label}*\n\n"
                    f"Ставка *{fmt(bet)} 💰* возвращена.\n\n"
                    f"💼 Баланс: *{fmt(new_balance)} 💰*"
                )
            else:
                if mult >= 25:
                    stars = "⚡"
                    hdr = f"⚡ *МЕГА-ВЫИГРЫШ {label}!*"
                elif mult >= 10:
                    stars = "💥"
                    hdr = f"💥 *ОГРОМНЫЙ ВЫИГРЫШ {label}!*"
                else:
                    stars = "🏆"
                    hdr = f"🏆 *ВЫИГРЫШ {emoji} {label}!*"
                text = (
                    f"{hdr}\n\n"
                    f"Выпало: {emoji} *{label}*\n\n"
                    f"Ставка: *{fmt(bet)} 💰*\n"
                    f"Выплата: *{fmt(winnings)} 💰*\n"
                    f"Чистый выигрыш: *+{fmt(net_gain)} 💰*\n\n"
                    f"💼 Баланс: *{fmt(new_balance)} 💰*"
                )
            rows = [[retry_btn, back_btn]]

        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    except Exception as exc:
        logging.getLogger(__name__).exception("roulette_spin crash (uid=%s bet=%s): %s", uid, bet, exc)
        try:
            refund_balance = await adb(db.add_coins, uid, bet)
            await query.edit_message_text(
                f"❌ *Что-то пошло не так*\n\n"
                f"Ставка *{fmt(bet)} 💰* возвращена.\n\n"
                f"💼 Баланс: *{fmt(refund_balance)} 💰*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[retry_btn, back_btn]]),
            )
        except Exception:
            pass


# ── Slot machine ──────────────────────────────────────────────────────────────

# (emoji, вес на барабан, множитель за тройку)
_SLOT_SYMBOLS: list[tuple[str, int, int]] = [
    ("🍒", 28,  4),
    ("🍋", 22,  8),
    ("🍊", 18, 12),
    ("🍇", 14, 18),
    ("🔔", 10, 30),
    ("💎",  6, 75),
    ("⭐",  2, 150),
]

# Бонус: шанс случайного выпадения (4%), стоимость покупки (×20 ставки)
_SLOT_BONUS_CHANCE  = 0.04
_SLOT_BONUS_COST    = 20       # multiply by bet
_SLOT_SPIN_SYM      = "🌀"

# ── Бонусная игра — 3 уровня ──────────────────────────────────────────────────
_BONUS_WILD    = "🌟"   # Wild: заменяет любой символ
_BONUS_SCATTER = "🔥"   # Scatter: даёт выплату + мгновенный апгрейд уровня

# Level: spins — кол-во спинов, mult — бонусный множитель,
#        wild_w / scatter_w — вес спецсимволов на барабанах
_BONUS_LEVELS: dict[int, dict] = {
    1: {"spins": 3, "mult": 2,  "wild_w": 5,  "scatter_w": 7,  "name": "🥉 Уровень 1"},
    2: {"spins": 3, "mult": 4,  "wild_w": 8,  "scatter_w": 7,  "name": "🥈 Уровень 2"},
    3: {"spins": 3, "mult": 8,  "wild_w": 12, "scatter_w": 0,  "name": "🥇 ФИНАЛ"},
}

# Улучшенные веса базовых символов на каждом уровне бонуса
# Ур.1 — как в обычной игре (Wild/Scatter добавляют ~10% выигрышности)
# Ур.2 — небольшой буст к средним символам
# Ур.3 — умеренный буст, но Wild ×12 ещё добавит иксов
_BONUS_SYM_WEIGHTS: dict[int, list] = {
    1: [28, 22, 18, 14, 10, 6,  2],
    2: [26, 20, 17, 13, 10, 7,  3],
    3: [22, 18, 16, 13, 10, 8,  4],
}

_SLOT_LEGEND = (
    "🍒 🍒 🍒 → ×4       🍋 🍋 🍋 → ×8\n"
    "🍊 🍊 🍊 → ×12      🍇 🍇 🍇 → ×18\n"
    "🔔 🔔 🔔 → ×30      💎 💎 💎 → ×75\n"
    "⭐ ⭐ ⭐ → ×150  🏆 ДЖЕКПОТ\n"
    "Любая пара → ставка возвращается\n"
    f"🔥 Бонус-игра: 4% шанс или купить за ×{_SLOT_BONUS_COST} ставки\n"
    "🌟 Wild (бонус) — заменяет любой символ\n"
    "🔥 Scatter (бонус) — апгрейд на след. уровень"
)


def _spin_slot_reels() -> tuple[str, str, str]:
    """Крутит 3 барабана независимо."""
    emojis  = [s[0] for s in _SLOT_SYMBOLS]
    weights = [s[1] for s in _SLOT_SYMBOLS]
    s1, s2, s3 = random.choices(emojis, weights=weights, k=3)
    return s1, s2, s3


def _eval_slots(s1: str, s2: str, s3: str) -> tuple[str, int]:
    """Оценивает результат барабанов.
    Возвращает ('triple'|'double'|'miss', множитель_выплаты).
    """
    if s1 == s2 == s3:
        mult = next((s[2] for s in _SLOT_SYMBOLS if s[0] == s1), 4)
        return "triple", mult
    if s1 == s2 or s1 == s3 or s2 == s3:
        return "double", 1
    return "miss", 0


def _spin_bonus_reels(level: int) -> tuple[str, str, str]:
    """Крутит барабаны бонус-игры: добавляет Wild и Scatter к пулу символов."""
    cfg  = _BONUS_LEVELS.get(level, _BONUS_LEVELS[1])
    base = [s[0] for s in _SLOT_SYMBOLS]
    wts  = list(_BONUS_SYM_WEIGHTS.get(level, _BONUS_SYM_WEIGHTS[1]))
    syms = base + [_BONUS_WILD]
    wts  = wts  + [cfg["wild_w"]]
    if cfg["scatter_w"] > 0:
        syms += [_BONUS_SCATTER]
        wts  += [cfg["scatter_w"]]
    s1, s2, s3 = random.choices(syms, weights=wts, k=3)
    return s1, s2, s3


def _eval_bonus_spin(s1: str, s2: str, s3: str) -> tuple[str, int, bool, str]:
    """Оценивает бонусный спин с Wild/Scatter.
    Wild 🌟 заменяет любой символ. Scatter 🔥 = Wild + trigger level-up.
    Возвращает: (kind, base_mult, scatter_hit, display_sym).
    """
    reels       = [s1, s2, s3]
    scatter_hit = _BONUS_SCATTER in reels
    # Scatter работает как Wild при подсчёте выплаты
    norm     = [_BONUS_WILD if s == _BONUS_SCATTER else s for s in reels]
    non_wild = [s for s in norm if s != _BONUS_WILD]
    n_wild   = 3 - len(non_wild)

    if n_wild == 3:
        # Все Wild → джекпот ⭐⭐⭐
        return "triple", 150, scatter_hit, "⭐"

    if n_wild == 2:
        # Два Wild + один символ → тройка того символа
        sym  = non_wild[0]
        mult = next((s[2] for s in _SLOT_SYMBOLS if s[0] == sym), 4)
        return "triple", mult, scatter_hit, sym

    if n_wild == 1:
        if len(non_wild) >= 2 and non_wild[0] == non_wild[1]:
            # Wild + пара → тройка
            sym  = non_wild[0]
            mult = next((s[2] for s in _SLOT_SYMBOLS if s[0] == sym), 4)
            return "triple", mult, scatter_hit, sym
        # Wild + два разных → пара
        return "double", 1, scatter_hit, non_wild[0] if non_wild else _BONUS_WILD

    # Без Wild — обычная оценка
    n1, n2, n3 = norm
    if n1 == n2 == n3:
        mult = next((s[2] for s in _SLOT_SYMBOLS if s[0] == n1), 4)
        return "triple", mult, scatter_hit, n1
    if n1 == n2 or n1 == n3 or n2 == n3:
        return "double", 1, scatter_hit, n1
    return "miss", 0, scatter_hit, n1


def _slot_board(r1: str, r2: str, r3: str) -> str:
    """Красивое поле слот-машины (monospace)."""
    return (
        "╔═════╦═════╦═════╗\n"
        f"║  {r1}  ║  {r2}  ║  {r3}  ║\n"
        "╚═════╩═════╩═════╝"
    )


def _slot_result_text(kind: str, mult: int, s1: str, s2: str, s3: str,
                      bet: int, new_balance: int, bonus: bool = False) -> str:
    """Форматирует текст результата спина."""
    board = f"```\n{_slot_board(s1, s2, s3)}\n```"
    b_tag = "🌟 *БОНУС* " if bonus else ""

    if kind == "miss":
        return (
            f"🎰 {b_tag}*СЛОТЫ*\n\n{board}\n\n"
            f"💀 *Мимо* — ставка сгорела\n\n"
            f"💼 Баланс: *{fmt(new_balance)} 💰*"
        )
    if kind == "double":
        match_sym = s1 if (s1 == s2 or s1 == s3) else s2
        return (
            f"🎰 {b_tag}*СЛОТЫ*\n\n{board}\n\n"
            f"🔄 *Пара {match_sym} {match_sym}* — ставка возвращается!\n\n"
            f"💼 Баланс: *{fmt(new_balance)} 💰*"
        )
    # triple
    winnings = bet * mult
    net_gain = winnings - bet
    if s1 == "⭐":
        hdr = "🏆 *ДЖЕКПОТ!*"
    elif mult >= 75:
        hdr = "💥 *МЕГА-ТРОЙКА!*"
    elif bonus:
        hdr = f"✨ *ТРОЙКА! ({s1}{s1}{s1})*"
    else:
        hdr = f"🎉 *ТРОЙКА! ({s1}{s1}{s1})*"

    bonus_line = ""  # текст бонуса строится отдельно в cb_casino_bonus_spin
    return (
        f"🎰 {b_tag}*СЛОТЫ*\n\n{board}\n\n"
        f"{hdr} — ×{mult}{bonus_line}\n\n"
        f"Ставка: *{fmt(bet)} 💰*\n"
        f"Выплата: *{fmt(winnings)} 💰* _(+{fmt(net_gain)} 💰)_\n\n"
        f"💼 Баланс: *{fmt(new_balance)} 💰*"
    )


async def _do_slot_spin(uid: int, bet: int, bonus_mult: int = 1) -> tuple[str, int, str, str, str]:
    """Крутит барабаны и выдаёт монеты. Возвращает (kind, mult, s1, s2, s3)."""
    s1, s2, s3 = _spin_slot_reels()
    kind, mult = _eval_slots(s1, s2, s3)
    if kind == "miss":
        pass  # ставка уже снята
    elif kind == "double":
        await adb(db.add_coins, uid, bet)
    else:
        await adb(db.add_coins, uid, bet * mult * bonus_mult)
    return kind, mult, s1, s2, s3


async def cb_casino_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_slots — экран выбора ставки для слот-машины."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    coins = await adb(db.get_coins, uid)

    rows = []
    for i in range(0, len(_ROULETTE_BETS), 2):
        pair = _ROULETTE_BETS[i:i + 2]
        row  = []
        for amt in pair:
            can = coins >= amt
            row.append(InlineKeyboardButton(
                f"{'🪙' if can else '🚫'} {fmt(amt)}",
                callback_data=f"casino_slotspin_{amt}" if can else "casino_no_coins",
            ))
        rows.append(row)

    # Кнопки покупки бонусной игры (по одной — текст длинный)
    for amt in _ROULETTE_BETS:
        cost = amt * _SLOT_BONUS_COST
        can  = coins >= cost
        rows.append([InlineKeyboardButton(
            f"{'🌟' if can else '🚫'} Бонус (ставка {fmt(amt)}, цена {fmt(cost)})",
            callback_data=f"casino_bonusbuy_{amt}" if can else "casino_no_coins",
        )])

    rows.append([InlineKeyboardButton("◀ Назад", callback_data="casino_menu")])

    await query.edit_message_text(
        f"🎰 *СЛОТ-МАШИНА*\n\n"
        f"💰 Баланс: *{fmt(coins)}* монет\n\n"
        f"{_SLOT_LEGEND}\n\n"
        "Выбери ставку:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_casino_slots_spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_slotspin_<amount> — крутим барабаны + шанс бонус-игры."""
    query = update.callback_query
    uid   = query.from_user.id
    try:
        bet = int(query.data[len("casino_slotspin_"):])
    except ValueError:
        await query.answer("Ошибка ставки.", show_alert=True); return

    ok, _ = await adb(db.spend_coins, uid, bet)
    if not ok:
        await query.answer("Недостаточно монет!", show_alert=True); return
    await query.answer()

    back_btn   = InlineKeyboardButton("◀ Казино",       callback_data="casino_menu")
    retry_btn  = InlineKeyboardButton(f"🎰 ×{fmt(bet)} снова", callback_data=f"casino_slotspin_{bet}")
    change_btn = InlineKeyboardButton("🔄 Другая ставка",  callback_data="casino_slots")

    try:
        # Крутим заранее — анимация декоративная
        s1, s2, s3 = _spin_slot_reels()
        spin = _SLOT_SPIN_SYM

        # ── Анимация (3 кадра) ────────────────────────────────────────────────
        frames = [
            (spin, spin, spin),
            (s1,   spin, spin),
            (s1,   s2,   spin),
        ]
        for f1, f2, f3 in frames:
            await query.edit_message_text(
                f"🎰 *СЛОТЫ*\n\n```\n{_slot_board(f1, f2, f3)}\n```\n\n"
                f"_Ставка: {fmt(bet)} 💰_",
                parse_mode="Markdown",
            )
            await asyncio.sleep(0.45)

        # ── Выплата ───────────────────────────────────────────────────────────
        kind, mult = _eval_slots(s1, s2, s3)
        if kind == "miss":
            fire_and_forget(adb(db.add_to_dividend_pool, max(1, bet // 20)))
            new_balance = await adb(db.get_coins, uid)
        elif kind == "double":
            new_balance = await adb(db.add_coins, uid, bet)
        else:
            new_balance = await adb(db.add_coins, uid, bet * mult)

        text = _slot_result_text(kind, mult, s1, s2, s3, bet, new_balance)

        # ── Проверка бонуса ───────────────────────────────────────────────────
        bonus_triggered = random.random() < _SLOT_BONUS_CHANCE
        if bonus_triggered:
            lvl1 = _BONUS_LEVELS[1]
            text += (
                f"\n\n{'━' * 22}\n"
                f"🔥🔥 *Б О Н У С - И Г Р А!* 🔥🔥\n"
                f"🥉[○○○] → 🥈[○○○] → 🥇[○○○]\n"
                f"_Wild🌟 заменяет символы_\n"
                f"_Scatter🔥 = апгрейд на след. уровень_\n"
                f"{'━' * 22}"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"🥉 Начать! Ур.1 ({lvl1['spins']} спина × ×{lvl1['mult']})",
                    callback_data=f"casino_bonusspin_{bet}_{lvl1['spins']}_0_1",
                ),
            ], [back_btn]])
        else:
            kb = InlineKeyboardMarkup([[retry_btn, change_btn], [back_btn]])

        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    except Exception as exc:
        logger.exception("slots_spin crash uid=%s bet=%s: %s", uid, bet, exc)
        try:
            rb = await adb(db.add_coins, uid, bet)
            await query.edit_message_text(
                f"❌ *Ошибка* — ставка возвращена.\n\n💼 Баланс: *{fmt(rb)} 💰*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[retry_btn, change_btn], [back_btn]]),
            )
        except Exception:
            pass


async def cb_casino_bonus_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_bonusbuy_<bet> — покупка гарантированной бонус-игры."""
    query = update.callback_query
    uid   = query.from_user.id
    try:
        bet = int(query.data[len("casino_bonusbuy_"):])
    except ValueError:
        await query.answer("Ошибка.", show_alert=True); return

    cost = bet * _SLOT_BONUS_COST
    ok, _ = await adb(db.spend_coins, uid, cost)
    if not ok:
        await query.answer("Недостаточно монет!", show_alert=True); return
    await query.answer(f"🌟 Бонус-игра активирована!")

    lvl1     = _BONUS_LEVELS[1]
    lvl2     = _BONUS_LEVELS[2]
    lvl3     = _BONUS_LEVELS[3]
    back_btn = InlineKeyboardButton("◀ Казино", callback_data="casino_menu")
    await query.edit_message_text(
        f"🔥 *БОНУС-ИГРА* 🔥\n"
        f"_Потрачено: {fmt(cost)} 💰 | Ставка: {fmt(bet)} 💰/спин_\n\n"
        f"```\n"
        f"┌──────────────────────────┐\n"
        f"│  🥉 Ур.1  {lvl1['spins']} спина  ×{lvl1['mult']}       │\n"
        f"│  🥈 Ур.2  {lvl2['spins']} спина  ×{lvl2['mult']}  🔥Scatter │\n"
        f"│  🥇 Финал {lvl3['spins']} спина  ×{lvl3['mult']}  🔥Scatter │\n"
        f"│  ⚡ Гранд-Джекпот ⭐⭐⭐         │\n"
        f"└──────────────────────────┘\n"
        f"```\n"
        f"🌟 Wild — заменяет любой символ\n"
        f"🔥 Scatter — мгновенный апгрейд уровня",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"🥉 Начать! Ур.1 ({lvl1['spins']} спина × ×{lvl1['mult']})",
                callback_data=f"casino_bonusspin_{bet}_{lvl1['spins']}_0_1",
            ),
        ], [back_btn]]),
    )


def _bonus_progress_bar(level: int, spin_num: int, remaining: int) -> str:
    """Прогресс-бар бонуса: уровни + заполненные точки текущего уровня.
    spin_num и remaining — значения ПОСЛЕ декремента (т.е. после текущего спина).
    """
    parts = []
    for l in [1, 2, 3]:
        cfg_l = _BONUS_LEVELS[l]
        ico   = cfg_l["name"].split()[0]        # 🥉 / 🥈 / 🥇
        if l < level:
            parts.append(f"{ico}✅")
        elif l == level:
            dots = "●" * spin_num + "○" * remaining
            parts.append(f"{ico}\\[{dots}\\]")   # экранируем [ для MarkdownV1
        else:
            parts.append(f"{ico}\\[{'○' * cfg_l['spins']}\\]")
    return " → ".join(parts)


def _bonus_board_block(s1: str, s2: str, s3: str, level: int) -> str:
    """Слот-борда в code-block с декоративной рамкой под текущий уровень."""
    board = _slot_board(s1, s2, s3)
    deco = {
        1: ("┌─── 🥉 BRONZE ───┐", "└─────────────────┘"),
        2: ("╔═══ 🥈 SILVER ═══╗", "╚═════════════════╝"),
        3: ("╔⭐⭐ 🥇  GOLD ⭐⭐╗", "╚⭐⭐⭐⭐⭐⭐⭐⭐⭐╝"),
    }
    top, bot = deco.get(level, ("─────────────────", "─────────────────"))
    return f"```\n{top}\n{board}\n{bot}\n```"


def _bonus_outcome_text(kind: str, base_mult: int, bm: int,
                        disp_sym: str, bet: int, spin_won: int,
                        scatter_hit: bool, wild_hit: bool, level: int) -> str:
    """Форматирует блок результата спина с уникальным стилем для каждого исхода."""
    lines = []

    if kind == "miss":
        lines += [
            "╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
            "💀  *М И М О*",
            "_Ставка сгорела_",
            "╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
        ]
    elif kind == "double":
        match_sym = disp_sym
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🔄  *П А Р А*  {match_sym}{match_sym}",
            f"_Ставка возвращается: +{fmt(bet)} 💰_",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
    else:
        total_mult = base_mult * bm
        is_jackpot = (level == 3 and disp_sym == "⭐")
        is_mega    = (base_mult >= 75 and not is_jackpot)
        syms       = f"{disp_sym}{disp_sym}{disp_sym}"

        if is_jackpot:
            lines += [
                "⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡",
                f"⚡  *ГРАНД-ДЖЕКПОТ!*  ⚡",
                f"⭐⭐⭐ — ×{base_mult} × ×{bm} = *×{total_mult}*",
                f"*Выплата: {fmt(spin_won)} 💰*",
                "⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡⚡",
            ]
        elif is_mega:
            lines += [
                "💥💥💥💥💥💥💥💥💥💥💥💥",
                f"💥  *М Е Г А - Т Р О Й К А!*",
                f"{syms} — ×{base_mult} × ×{bm} = *×{total_mult}*",
                f"*Выплата: {fmt(spin_won)} 💰*",
                "💥💥💥💥💥💥💥💥💥💥💥💥",
            ]
        else:
            lines += [
                "══════════════════════",
                f"✨  *Т Р О Й К А!*  {syms}",
                f"×{base_mult} × ×{bm} = *×{total_mult}*",
                f"*Выплата: {fmt(spin_won)} 💰*",
                "══════════════════════",
            ]

    # Спецсимволы
    if wild_hit and not scatter_hit:
        lines.append("🌟 _Wild сработал — помог тройке!_")
    if scatter_hit:
        lines.append("🔥 _Scatter — выплата + апгрейд!_")

    return "\n".join(lines)


async def cb_casino_bonus_spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_bonusspin_<bet>_<remaining>_<total_won>_<level> — один спин бонус-игры.
    Состояние полностью закодировано в callback_data (serverless-совместимо).
    Три уровня: ×2 → ×4 → ×8. Scatter 🔥 = немедленный апгрейд уровня.
    Wild 🌟 = заменяет любой символ."""
    query = update.callback_query
    uid   = query.from_user.id

    # Парсим: casino_bonusspin_<bet>_<remaining>_<won>_<level>
    try:
        parts     = query.data.split("_")  # ["casino","bonusspin",bet,remaining,won,level]
        bet       = int(parts[2])
        remaining = int(parts[3])
        total_won = int(parts[4])
        level     = int(parts[5])
    except (IndexError, ValueError):
        await query.answer("Бонус-игра не активна.", show_alert=True)
        return

    if remaining <= 0:
        await query.answer("Бонус-игра завершена.", show_alert=True)
        return

    await query.answer()

    cfg      = _BONUS_LEVELS.get(level, _BONUS_LEVELS[1])
    bm       = cfg["mult"]
    spin_num = cfg["spins"] - remaining + 1   # 1-indexed, до декремента
    spin     = _SLOT_SPIN_SYM
    back_btn = InlineKeyboardButton("◀ Казино", callback_data="casino_menu")

    # Анимационные символы по уровню
    anim_sym = {1: "✨", 2: "💫", 3: "⭐"}[level]

    try:
        # ── Анимация (3 кадра) ────────────────────────────────────────────────
        for f1, f2, f3 in [(spin, spin, spin), (anim_sym, spin, spin), (anim_sym, anim_sym, spin)]:
            await query.edit_message_text(
                f"{cfg['name']}\n"
                f"_Спин {spin_num}/{cfg['spins']} | ×{bm} к выигрышу_\n\n"
                f"{_bonus_board_block(f1, f2, f3, level)}",
                parse_mode="Markdown",
            )
            await asyncio.sleep(0.4)

        # ── Бонусные барабаны ─────────────────────────────────────────────────
        s1, s2, s3 = _spin_bonus_reels(level)
        kind, base_mult, scatter_hit, disp_sym = _eval_bonus_spin(s1, s2, s3)
        wild_hit = _BONUS_WILD in [s1, s2, s3]

        # ── Выплата ───────────────────────────────────────────────────────────
        if kind == "miss":
            spin_won = 0
        elif kind == "double":
            spin_won = bet
            await adb(db.add_coins, uid, spin_won)
        else:
            spin_won = bet * base_mult * bm
            await adb(db.add_coins, uid, spin_won)

        total_won += spin_won
        remaining -= 1                          # декремент после подсчёта spin_num
        new_balance = await adb(db.get_coins, uid)

        # ── Строим сообщение ──────────────────────────────────────────────────
        prog  = _bonus_progress_bar(level, spin_num, remaining)
        board = _bonus_board_block(s1, s2, s3, level)
        outcome = _bonus_outcome_text(
            kind, base_mult, bm, disp_sym, bet, spin_won,
            scatter_hit, wild_hit, level,
        )
        footer = (
            f"\n💼 Баланс: *{fmt(new_balance)} 💰*\n"
            f"_+{fmt(spin_won)} 💰 этот спин  |  Итого бонус: +{fmt(total_won)} 💰_"
        )

        text = f"{cfg['name']}\n{prog}\n\n{board}\n\n{outcome}{footer}"

        # ── Следующее состояние ───────────────────────────────────────────────
        if scatter_hit and level < 3:
            # ── LEVEL UP ─────────────────────────────────────────────────────
            next_lvl = level + 1
            next_cfg = _BONUS_LEVELS[next_lvl]
            next_ico = next_cfg["name"].split()[0]
            next_cd  = f"casino_bonusspin_{bet}_{next_cfg['spins']}_{total_won}_{next_lvl}"
            text += (
                f"\n\n{'━' * 22}\n"
                f"🔥🔥 *А П Г Р Е Й Д !* 🔥🔥\n"
                f"{cfg['name'].split()[0]} → {next_cfg['name']}\n"
                f"Множитель: ×{bm} → *×{next_cfg['mult']}*\n"
                f"{'━' * 22}"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"{next_ico} На уровень {next_lvl}! ({next_cfg['spins']} спина × ×{next_cfg['mult']})",
                    callback_data=next_cd,
                ),
            ], [back_btn]])

        elif remaining > 0:
            # ── Продолжаем ───────────────────────────────────────────────────
            next_cd = f"casino_bonusspin_{bet}_{remaining}_{total_won}_{level}"
            next_num = spin_num + 1
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"{cfg['name'].split()[0]} Спин {next_num}/{cfg['spins']}  →",
                    callback_data=next_cd,
                ),
            ], [back_btn]])

        else:
            # ── Бонус завершён ────────────────────────────────────────────────
            if level == 3:
                # Красивый финальный экран
                all_done = "🥉✅ → 🥈✅ → 🥇✅"
                total_box = (
                    "```\n"
                    "┌─────────────────────┐\n"
                    f"│   💰 ИТОГО БОНУС:   │\n"
                    f"│  {fmt(total_won):^19} │\n"
                    "│       монет         │\n"
                    "└─────────────────────┘\n"
                    "```"
                )
                text = (
                    f"🏆 *ГРАНД-ФИНАЛ ЗАВЕРШЁН!* 🏆\n\n"
                    f"{all_done}\n\n"
                    f"{total_box}\n"
                    f"💼 Баланс: *{fmt(new_balance)} 💰*"
                )
            else:
                completed = " → ".join(
                    f"{_BONUS_LEVELS[l]['name'].split()[0]}✅" if l <= level
                    else f"{_BONUS_LEVELS[l]['name'].split()[0]}✗"
                    for l in [1, 2, 3]
                )
                text += f"\n\n🏁 *Бонус завершён*\n{completed}\nОбщий выигрыш: *+{fmt(total_won)} 💰*"

            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎰 Ещё раз", callback_data="casino_slots"),
                back_btn,
            ]])

        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    except Exception as exc:
        logger.exception("bonus_spin crash uid=%s level=%s: %s", uid, level, exc)
        try:
            back_btn = InlineKeyboardButton("◀ Казино", callback_data="casino_menu")
            await query.edit_message_text(
                "❌ *Ошибка в бонус-игре.* Прогресс сохранён в балансе.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[back_btn]]),
            )
        except Exception:
            pass


# ── Переводы монет ────────────────────────────────────────────────────────────

_TRANSFER_AMOUNTS = [100, 500, 1_000, 5_000, 10_000, 50_000, 100_000, 500_000, 1_000_000]


def _transfer_amount_kb(target_uid: int, coins: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора суммы перевода."""
    rows = []
    row  = []
    for amt in _TRANSFER_AMOUNTS:
        can = coins >= amt
        row.append(InlineKeyboardButton(
            f"{'✅' if can else '🚫'} {fmt(amt)}",
            callback_data=f"transfer_amt_{target_uid}_{amt}" if can else "transfer_no_coins",
        ))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Своя сумма", callback_data=f"transfer_custom_{target_uid}")])
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="transfer_start")])
    return InlineKeyboardMarkup(rows)


async def cb_transfer_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """transfer_start — экран выбора получателя."""
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    opponents = await adb(db.get_recent_opponents, uid, 8)
    coins     = await adb(db.get_coins, uid)

    rows = []
    for opp in opponents:
        name = opp.get("username", "?")
        opp_coins = opp.get("coins", 0)
        rows.append([InlineKeyboardButton(
            f"👤 {name}  ({fmt(opp_coins)} 💰)",
            callback_data=f"transfer_to_{opp['user_id']}",
        )])

    rows.append([InlineKeyboardButton("🔍 Найти по @username", callback_data="transfer_search")])
    rows.append([InlineKeyboardButton("◀ Профиль", callback_data="show_profile")])

    await query.edit_message_text(
        f"💸 *ПЕРЕВОД МОНЕТ*\n\n"
        f"💼 Твой баланс: *{fmt(coins)} 💰*\n\n"
        f"{'_Недавние соперники:_' if opponents else '_Нет истории игр — введи @username вручную_'}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_transfer_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """transfer_to_<uid> — выбор суммы перевода."""
    query = update.callback_query
    uid   = query.from_user.id
    try:
        target_uid = int(query.data[len("transfer_to_"):])
    except ValueError:
        await query.answer("Ошибка.", show_alert=True); return

    if target_uid == uid:
        await query.answer("Нельзя переводить самому себе 😄", show_alert=True); return

    target = await adb(db.get_user, target_uid)
    if not target:
        await query.answer("Игрок не найден.", show_alert=True); return

    await query.answer()
    coins       = await adb(db.get_coins, uid)
    target_name = target.get("username", "?")

    await query.edit_message_text(
        f"💸 *ПЕРЕВОД → {target_name}*\n\n"
        f"💼 Твой баланс: *{fmt(coins)} 💰*\n\n"
        f"Выбери сумму:",
        parse_mode="Markdown",
        reply_markup=_transfer_amount_kb(target_uid, coins),
    )


async def cb_transfer_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """transfer_amt_<uid>_<amount> — экран подтверждения."""
    query = update.callback_query
    uid   = query.from_user.id
    try:
        rest           = query.data[len("transfer_amt_"):]
        target_uid_str, amount_str = rest.rsplit("_", 1)
        target_uid     = int(target_uid_str)
        amount         = int(amount_str)
    except (ValueError, AttributeError):
        await query.answer("Ошибка.", show_alert=True); return

    target = await adb(db.get_user, target_uid)
    if not target:
        await query.answer("Игрок не найден.", show_alert=True); return

    await query.answer()
    coins       = await adb(db.get_coins, uid)
    target_name = target.get("username", "?")

    if coins < amount:
        await query.answer("Недостаточно монет!", show_alert=True); return

    await query.edit_message_text(
        f"💸 *ПОДТВЕРЖДЕНИЕ ПЕРЕВОДА*\n\n"
        f"Получатель: *{target_name}*\n"
        f"Сумма: *{fmt(amount)} 💰*\n\n"
        f"Твой баланс сейчас: *{fmt(coins)} 💰*\n"
        f"После перевода: *{fmt(coins - amount)} 💰*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"transfer_ok_{target_uid}_{amount}"),
            InlineKeyboardButton("❌ Отмена",      callback_data=f"transfer_to_{target_uid}"),
        ]]),
    )


async def cb_transfer_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """transfer_ok_<uid>_<amount> — выполняем перевод."""
    query = update.callback_query
    uid   = query.from_user.id
    try:
        rest           = query.data[len("transfer_ok_"):]
        target_uid_str, amount_str = rest.rsplit("_", 1)
        target_uid     = int(target_uid_str)
        amount         = int(amount_str)
    except (ValueError, AttributeError):
        await query.answer("Ошибка.", show_alert=True); return

    target = await adb(db.get_user, target_uid)
    if not target:
        await query.answer("Игрок не найден.", show_alert=True); return

    ok, err = await adb(db.transfer_coins, uid, target_uid, amount)
    if not ok:
        await query.answer(err, show_alert=True); return

    await query.answer(f"✅ Переведено {fmt(amount)} 💰!")

    sender_name = query.from_user.username or query.from_user.full_name or str(uid)
    target_name = target.get("username", "?")
    new_balance = await adb(db.get_coins, uid)

    await query.edit_message_text(
        f"✅ *ПЕРЕВОД ВЫПОЛНЕН*\n\n"
        f"→ *{target_name}* получил *{fmt(amount)} 💰*\n\n"
        f"💼 Твой баланс: *{fmt(new_balance)} 💰*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("💸 Ещё перевод", callback_data="transfer_start"),
            InlineKeyboardButton("◀ Профиль",      callback_data="show_profile"),
        ]]),
    )

    # Уведомление получателю в личку
    try:
        await context.bot.send_message(
            chat_id=target_uid,
            text=(
                f"💸 *Входящий перевод!*\n\n"
                f"*{sender_name}* отправил тебе *{fmt(amount)} 💰*\n\n"
                f"Проверь баланс: /profile"
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass  # юзер мог заблокировать бота


async def cb_transfer_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """transfer_search — запрашиваем @username текстом."""
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    await adb(db.set_pending_action, uid, "transfer_search", {})
    await query.edit_message_text(
        "🔍 *ПОИСК ИГРОКА*\n\n"
        "Введи @username или имя игрока:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="transfer_start"),
        ]]),
    )


async def cb_transfer_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """transfer_custom_<uid> — запрашиваем произвольную сумму текстом."""
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    try:
        target_uid = int(query.data[len("transfer_custom_"):])
    except ValueError:
        await query.answer("Ошибка.", show_alert=True); return

    target      = await adb(db.get_user, target_uid)
    target_name = target.get("username", "?") if target else "?"
    coins       = await adb(db.get_coins, uid)

    await adb(db.set_pending_action, uid, "transfer_custom", {"target_uid": target_uid})
    await query.edit_message_text(
        f"✏️ *СВОЯ СУММА*\n\n"
        f"Переводим → *{target_name}*\n"
        f"💼 Твой баланс: *{fmt(coins)} 💰*\n\n"
        f"Введи сумму числом:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data=f"transfer_to_{target_uid}"),
        ]]),
    )


async def cb_transfer_no_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """transfer_no_coins — заглушка для недоступных кнопок."""
    await update.callback_query.answer("Недостаточно монет!", show_alert=True)


# ── Кросс-бот обменник ────────────────────────────────────────────────────────
# Укажи username FUT-бота ниже (видно в меню при нажатии ▶ Обменник)
_FUT_BOT_LINK = "https://t.me/TransferGamebot_bot"  # ← замени на реальный юзернейм


async def cb_exchange_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """exchange_menu — главный экран обменника."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    coins = await adb(db.get_coins, uid)

    pending_c2f = await adb(db.get_pending_cross_transfers, uid, "cube_to_fut")
    pending_f2c = await adb(db.get_pending_cross_transfers, uid, "fut_to_cube")

    rate_out = int(100 * db.CROSS_RATE * (1 - db.CROSS_FEE))
    lines = [
        "🔄 *ОБМЕННИК*\n\n",
        f"💰 Твои CUB: *{fmt(coins)}*\n",
        f"📈 Курс: 1 CUB = {db.CROSS_RATE} FUT (комиссия {int(db.CROSS_FEE * 100)}%)\n",
        f"💱 Пример: 100 CUB → *{rate_out} FUT*\n",
    ]
    if pending_c2f:
        total = sum(t["amount_out"] for t in pending_c2f)
        lines.append(f"\n⏳ Ожидает в FUT-боте: *{fmt(total)} FUT* ({len(pending_c2f)} перев.)")
    if pending_f2c:
        total = sum(t["amount_out"] for t in pending_f2c)
        lines.append(f"\n✅ Готово к получению: *{fmt(total)} CUB*")

    rows = [
        [InlineKeyboardButton("💎 CUB → FUT", callback_data="exchange_start_cube")],
    ]
    if pending_f2c:
        total = sum(t["amount_out"] for t in pending_f2c)
        rows.append([InlineKeyboardButton(
            f"📥 Забрать FUT→CUB ({fmt(total)} 💰)",
            callback_data="exchange_claim_f2c",
        )])
    if pending_c2f:
        rows.append([InlineKeyboardButton(
            "📋 Мои ожидающие переводы",
            callback_data="exchange_pending",
        )])
    rows.append([InlineKeyboardButton("◀ В меню", callback_data="main_menu")])

    await query.edit_message_text(
        "".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_exchange_start_cube(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """exchange_start_cube — начать конвертацию CUB→FUT."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    coins = await adb(db.get_coins, uid)

    if coins <= 0:
        await query.answer("Недостаточно монет!", show_alert=True)
        return

    await adb(db.set_pending_action, uid, "exchange_cube_to_fut", {})
    await query.edit_message_text(
        f"💎 *CUB → FUT*\n\n"
        f"💰 Твой баланс: *{fmt(coins)} CUB*\n"
        f"📈 Курс: 1 CUB = {db.CROSS_RATE} FUT (комиссия {int(db.CROSS_FEE * 100)}%)\n\n"
        f"Введи сумму CUB для конвертации:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="exchange_menu"),
        ]]),
    )


async def cb_exchange_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """exchange_confirm_<amount> — подтвердить конвертацию CUB→FUT."""
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    amount = int(query.data.split("_")[-1])

    coins = await adb(db.get_coins, uid)
    if coins < amount:
        await query.answer("Недостаточно монет!", show_alert=True)
        return

    ok, tid, err = await adb(db.create_cross_transfer, uid, "cube_to_fut", amount)
    if not ok:
        await query.edit_message_text(
            f"❌ {err}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀ Обменник", callback_data="exchange_menu"),
            ]]),
        )
        return

    out, comm = db._cross_calc(amount, "cube_to_fut")
    await query.edit_message_text(
        f"✅ *ПЕРЕВОД СОЗДАН!*\n\n"
        f"Потрачено: *{fmt(amount)} CUB*\n"
        f"К получению: *{fmt(out)} FUT*\n"
        f"Комиссия сожжена: *{fmt(comm)} FUT*\n\n"
        f"Перейди в FUT-бот и нажми *🔄 Обменник → Забрать*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀ Обменник", callback_data="exchange_menu"),
        ]]),
    )


async def cb_exchange_claim_f2c(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """exchange_claim_f2c — забрать FUT→CUB переводы."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id

    ok, total, err = await adb(db.claim_cross_transfers, uid, "fut_to_cube")
    if not ok:
        await query.answer(err, show_alert=True)
        return

    coins = await adb(db.get_coins, uid)
    await query.edit_message_text(
        f"✅ *МОНЕТЫ ПОЛУЧЕНЫ!*\n\n"
        f"Зачислено: *{fmt(total)} CUB*\n"
        f"Твой баланс: *{fmt(coins)} CUB*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀ Обменник", callback_data="exchange_menu"),
        ]]),
    )


async def cb_exchange_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """exchange_pending — список ожидающих CUB→FUT переводов с отменой."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id

    transfers = await adb(db.get_pending_cross_transfers, uid, "cube_to_fut")
    if not transfers:
        await query.answer("Нет ожидающих переводов.", show_alert=True)
        return

    lines = ["📋 *ОЖИДАЮЩИЕ ПЕРЕВОДЫ (CUB→FUT)*\n\n"]
    rows  = []
    for t in transfers:
        lines.append(f"• {fmt(t['amount_in'])} CUB → {fmt(t['amount_out'])} FUT\n")
        rows.append([InlineKeyboardButton(
            f"🚫 Отменить ({fmt(t['amount_in'])} CUB)",
            callback_data=f"exchange_cancel_{t['id']}",
        )])
    rows.append([InlineKeyboardButton("◀ Обменник", callback_data="exchange_menu")])

    await query.edit_message_text(
        "".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_exchange_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """exchange_cancel_<id> — отменить конкретный перевод CUB→FUT."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    tid   = int(query.data.split("_")[-1])

    ok, err = await adb(db.cancel_cross_transfer, tid, uid)
    if not ok:
        await query.answer(err, show_alert=True)
        return

    await query.answer("✅ Перевод отменён, монеты возвращены!", show_alert=True)
    # Обновляем список ожидающих
    await cb_exchange_pending(update, context)


# ── Blackjack ────────────────────────────────────────────────────────────────

_BJ_RANKS = ['A','2','3','4','5','6','7','8','9','10','J','Q','K']
_BJ_SUITS = ['♠','♥','♦','♣']
_BJ_BETS  = [50, 100, 250, 500, 1_000, 2_500]


def _bj_card_val(rank: str) -> int:
    if rank in ('J', 'Q', 'K'): return 10
    if rank == 'A':              return 11
    return int(rank)


def _bj_hand_val(hand: list[tuple[str,str]]) -> int:
    total = sum(_bj_card_val(r) for r, _ in hand)
    aces  = sum(1 for r, _ in hand if r == 'A')
    while total > 21 and aces:
        total -= 10
        aces  -= 1
    return total


def _bj_card(card: tuple[str, str]) -> str:
    return f"{card[0]}{card[1]}"


def _bj_hand_str(hand: list[tuple[str,str]], hide_last: bool = False) -> str:
    if hide_last and len(hand) >= 2:
        return f"{_bj_card(hand[0])} 🂠"
    return "  ".join(_bj_card(c) for c in hand)


def _bj_new_hand(deck: list) -> tuple[list[tuple[str,str]], list]:
    """Раздаёт 2 карты из deck. Возвращает (hand, remaining_deck)."""
    import copy
    d    = copy.copy(deck)
    hand = [d.pop(), d.pop()]
    return hand, d


def _bj_deal_one(deck: list) -> tuple[tuple[str,str], list]:
    """Берёт одну карту из deck."""
    import copy
    d    = copy.copy(deck)
    card = d.pop()
    return card, d


def _bj_make_deck() -> list[tuple[str,str]]:
    """6 колод, перемешанные."""
    deck = [(r, s) for r in _BJ_RANKS for s in _BJ_SUITS] * 6
    random.shuffle(deck)
    return deck


def _bj_status(hand: list[tuple[str,str]]) -> str:
    v = _bj_hand_val(hand)
    if v > 21:    return "bust"
    if v == 21 and len(hand) == 2: return "blackjack"
    return "ok"


def _bj_state_encode(player: list, dealer: list, deck: list, bet: int) -> dict:
    return {"player": player, "dealer": dealer, "deck": deck, "bet": bet}


def _bj_board(player: list, dealer: list, hide_dealer: bool = True) -> str:
    """Красивое отображение стола."""
    pv   = _bj_hand_val(player)
    dv   = _bj_hand_val(dealer) if not hide_dealer else "?"
    p_str = _bj_hand_str(player)
    d_str = _bj_hand_str(dealer, hide_last=hide_dealer)
    return (
        f"🃏 *Дилер:* `{d_str}`   _{('?' if hide_dealer else str(dv))}_\n"
        f"👤 *Вы:*    `{p_str}`   _{pv}_"
    )


async def cb_casino_bj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_bj — выбор ставки для блэкджека."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    coins = await adb(db.get_coins, uid)

    rows = []
    for i in range(0, len(_BJ_BETS), 2):
        pair = _BJ_BETS[i:i+2]
        row  = []
        for amt in pair:
            can = coins >= amt
            row.append(InlineKeyboardButton(
                f"{'🪙' if can else '🚫'} {fmt(amt)}",
                callback_data=f"casino_bjbet_{amt}" if can else "casino_no_coins",
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="casino_menu")])

    await query.edit_message_text(
        "🃏 *БЛЭКДЖЕК*\n\n"
        "Набери 21 или ближе к нему, чем дилер.\n"
        "• Блэкджек (туз + 10) → ×2.5\n"
        "• Победа → ×2\n"
        "• Ничья → ставка возвращается\n"
        "• Дабл-даун: удваиваешь ставку, получаешь ровно 1 карту\n\n"
        f"💰 Баланс: *{fmt(coins)}* монет\n\n"
        "Выбери ставку:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_casino_bj_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_bjbet_<amount> — раздача карт, начало игры."""
    query = update.callback_query
    uid   = query.from_user.id
    try:
        bet = int(query.data[len("casino_bjbet_"):])
    except ValueError:
        await query.answer("Ошибка ставки.", show_alert=True); return

    ok, _ = await adb(db.spend_coins, uid, bet)
    if not ok:
        await query.answer("Недостаточно монет!", show_alert=True); return
    await query.answer()

    deck         = _bj_make_deck()
    player, deck = _bj_new_hand(deck)
    dealer, deck = _bj_new_hand(deck)

    state = _bj_state_encode(player, dealer, deck, bet)
    await adb(db.set_pending_action, uid, "bj_solo", state)

    back_btn = InlineKeyboardButton("◀ Казино", callback_data="casino_menu")
    pstatus  = _bj_status(player)

    if pstatus == "blackjack":
        # Сразу проверяем дилера
        dstatus = _bj_status(dealer)
        if dstatus == "blackjack":
            payout = bet
            result = "🤝 *НИЧЬЯ — оба блэкджека!*"
        else:
            payout = int(bet * 2.5)
            result = "♠ *БЛЭКДЖЕК! Выигрыш ×2.5!*"
        new_balance = await adb(db.add_coins, uid, payout)
        await adb(db.clear_pending_action, uid)
        await query.edit_message_text(
            f"🃏 *БЛЭКДЖЕК*\n\n"
            f"{_bj_board(player, dealer, hide_dealer=False)}\n\n"
            f"{result}\n"
            f"Ставка: *{fmt(bet)} 💰*  →  Выплата: *{fmt(payout)} 💰*\n\n"
            f"💼 Баланс: *{fmt(new_balance)} 💰*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🃏 Ещё раз", callback_data="casino_bj"),
                back_btn,
            ]]),
        )
        return

    pv = _bj_hand_val(player)
    can_double = True  # всегда можно при старте
    await query.edit_message_text(
        f"🃏 *БЛЭКДЖЕК*\n\n"
        f"{_bj_board(player, dealer)}\n\n"
        f"Ставка: *{fmt(bet)} 💰*\n\n"
        "Твой ход:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👆 Ещё",  callback_data="casino_bjhit"),
             InlineKeyboardButton("✋ Стоп", callback_data="casino_bjstand")],
            [InlineKeyboardButton("💰 Дабл", callback_data="casino_bjdouble")],
            [back_btn],
        ]),
    )


async def _bj_get_state(uid: int) -> dict | None:
    """Читает текущее состояние bj_solo из pending_actions."""
    pa = await adb(db.get_pending_action, uid)
    if pa and pa.get("action") == "bj_solo":
        return pa["data"]
    return None


async def _bj_finish(query, uid: int, player: list, dealer: list,
                     bet: int, reason: str = "") -> None:
    """Дилер добирает карты, считаем результат, выдаём монеты."""
    deck  = []  # дилер добирает из "бесконечной" колоды
    # Дилер берёт карты до 17+
    d     = list(dealer)
    while _bj_hand_val(d) < 17:
        rank  = random.choice(_BJ_RANKS)
        suit  = random.choice(_BJ_SUITS)
        d.append((rank, suit))

    pv = _bj_hand_val(player)
    dv = _bj_hand_val(d)
    ps = _bj_status(player)
    ds = _bj_status(d)

    if ps == "bust":
        payout = 0
        result = "💥 *ВЫ ПЕРЕБРАЛИ! Проигрыш.*"
    elif ds == "bust":
        payout = bet * 2
        result = f"🏆 *Дилер перебрал! Выигрыш!*"
    elif pv > dv:
        payout = bet * 2
        result = f"🏆 *Победа! {pv} против {dv}*"
    elif pv == dv:
        payout = bet
        result = f"🤝 *Ничья! {pv} = {dv}*"
    else:
        payout = 0
        result = f"😞 *Проигрыш. {pv} против {dv}*"

    if payout == 0:
        fire_and_forget(adb(db.add_to_dividend_pool, max(1, bet // 20)))
    new_balance = await adb(db.add_coins, uid, payout)
    await adb(db.clear_pending_action, uid)

    back_btn = InlineKeyboardButton("◀ Казино", callback_data="casino_menu")
    net      = payout - bet
    net_str  = f"+{fmt(net)}" if net >= 0 else fmt(net)
    await query.edit_message_text(
        f"🃏 *БЛЭКДЖЕК*\n\n"
        f"{_bj_board(player, d, hide_dealer=False)}\n\n"
        f"{result}\n"
        f"Ставка: *{fmt(bet)} 💰*  |  Изменение: *{net_str} 💰*\n\n"
        f"💼 Баланс: *{fmt(new_balance)} 💰*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🃏 Ещё раз", callback_data="casino_bj"),
            back_btn,
        ]]),
    )


async def cb_casino_bj_hit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_bjhit — игрок берёт карту."""
    query = update.callback_query
    uid   = query.from_user.id
    state = await _bj_get_state(uid)
    if not state:
        await query.answer("Игра не найдена.", show_alert=True); return
    await query.answer()

    player = state["player"]
    dealer = state["dealer"]
    deck   = state["deck"]
    bet    = state["bet"]

    card, deck = _bj_deal_one(deck)
    player.append(card)

    pstatus = _bj_status(player)
    if pstatus == "bust":
        await adb(db.clear_pending_action, uid)
        new_balance = await adb(db.get_coins, uid)
        back_btn = InlineKeyboardButton("◀ Казино", callback_data="casino_menu")
        await query.edit_message_text(
            f"🃏 *БЛЭКДЖЕК*\n\n"
            f"{_bj_board(player, dealer, hide_dealer=False)}\n\n"
            f"💥 *ПЕРЕБОР! {_bj_hand_val(player)} > 21*\n"
            f"Ставка *{fmt(bet)} 💰* — потеряна.\n\n"
            f"💼 Баланс: *{fmt(new_balance)} 💰*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🃏 Ещё раз", callback_data="casino_bj"),
                back_btn,
            ]]),
        )
        return

    # Обновляем состояние
    state["player"] = player
    state["deck"]   = deck
    await adb(db.set_pending_action, uid, "bj_solo", state)

    back_btn = InlineKeyboardButton("◀ Казино", callback_data="casino_menu")
    pv = _bj_hand_val(player)
    await query.edit_message_text(
        f"🃏 *БЛЭКДЖЕК*\n\n"
        f"{_bj_board(player, dealer)}\n\n"
        f"Ставка: *{fmt(bet)} 💰*\n\n"
        "Твой ход:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👆 Ещё",  callback_data="casino_bjhit"),
             InlineKeyboardButton("✋ Стоп", callback_data="casino_bjstand")],
            [back_btn],
        ]),
    )


async def cb_casino_bj_stand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_bjstand — игрок останавливается, дилер играет."""
    query = update.callback_query
    uid   = query.from_user.id
    state = await _bj_get_state(uid)
    if not state:
        await query.answer("Игра не найдена.", show_alert=True); return
    await query.answer()
    await _bj_finish(query, uid, state["player"], state["dealer"], state["bet"])


async def cb_casino_bj_double(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_bjdouble — дабл-даун: ×2 ставка, одна карта, автостоп."""
    query = update.callback_query
    uid   = query.from_user.id
    state = await _bj_get_state(uid)
    if not state:
        await query.answer("Игра не найдена.", show_alert=True); return

    bet    = state["bet"]
    player = state["player"]
    dealer = state["dealer"]
    deck   = state["deck"]

    ok, _ = await adb(db.spend_coins, uid, bet)  # доплачиваем ещё ставку
    if not ok:
        await query.answer("Недостаточно монет для дабла!", show_alert=True); return
    await query.answer("💰 Дабл-даун!")

    card, deck = _bj_deal_one(deck)
    player.append(card)
    new_bet = bet * 2
    state["bet"]    = new_bet
    state["player"] = player
    state["deck"]   = deck
    await adb(db.set_pending_action, uid, "bj_solo", state)
    await _bj_finish(query, uid, player, dealer, new_bet)


# ── Global Roulette ──────────────────────────────────────────────────────────

_GLOBAL_OUTCOMES = [
    # (вес, код, emoji, заголовок, описание)
    (35, "burn",     "💀", "КОТЁЛ СГОРЕЛ",      "Все ставки потеряны"),
    (20, "jackpot",  "🏆", "ДЖЕКПОТ",           "Один игрок забирает весь банк"),
    (20, "split",    "💰", "ДЕЛЕЖ",             "Банк делится поровну"),
    (15, "x2",       "💥", "КОТЁЛ УДВОЕН",      "Банк ×2, делится поровну"),
    (7,  "carryover","🔄", "КОТЁЛ РАСТЁТ",      "Банк переходит в следующий раунд"),
    (3,  "x5",       "🌟", "МЕГА КОТЁЛ ×5",     "Банк ×5, делится поровну"),
]

_GLOBAL_BETS = [100, 250, 500, 1_000, 2_500]


async def cb_casino_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_global — экран глобальной рулетки.
    В serverless-режиме выполняет lazy-spin: если прошла минута — крутим."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id

    # ── Lazy spin (serverless): крутим если прошло ≥60 сек с последнего спина ─
    state = await adb(db.get_global_roulette_state)
    last_spin = state.get("last_spin_at")
    if last_spin:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            ls = datetime.datetime.fromisoformat(last_spin)
            if ls.tzinfo is None:
                ls = ls.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            ls = now
        if (now - ls).total_seconds() >= 60:
            await _global_roulette_tick(context)
            state = await adb(db.get_global_roulette_state)

    coins = await adb(db.get_coins, uid)
    pot   = state.get("pot", 0)
    rnd   = state.get("round", 0)

    rows = []
    for i in range(0, len(_GLOBAL_BETS), 2):
        pair = _GLOBAL_BETS[i:i+2]
        row  = []
        for amt in pair:
            can = coins >= amt
            row.append(InlineKeyboardButton(
                f"{'🎲' if can else '🚫'} {fmt(amt)}",
                callback_data=f"casino_globalbet_{amt}" if can else "casino_no_coins",
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton("◀ Казино", callback_data="casino_menu")])

    bets_list = await adb(db.get_global_roulette_bets, rnd)
    n_players = len({b["user_id"] for b in bets_list})

    outcomes_txt = "\n".join(
        f"{e} {hdr} — {desc}" for _, _, e, hdr, desc in _GLOBAL_OUTCOMES
    )

    await query.edit_message_text(
        f"🌍 *ГЛОБАЛЬНАЯ РУЛЕТКА*\n\n"
        f"🏦 Текущий банк: *{fmt(pot)} 💰*\n"
        f"👥 Участников: *{n_players}*\n"
        f"⏱ Прокрутка каждую минуту\n\n"
        f"*Возможные исходы:*\n{outcomes_txt}\n\n"
        f"💰 Твой баланс: *{fmt(coins)}* монет\n\n"
        "Выбери ставку — и подожди результата!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_casino_global_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """casino_globalbet_<amount> — поставить в глобальный котёл."""
    query = update.callback_query
    uid   = query.from_user.id
    try:
        bet = int(query.data[len("casino_globalbet_"):])
    except ValueError:
        await query.answer("Ошибка ставки.", show_alert=True); return

    ok, new_balance = await adb(db.add_global_roulette_bet, uid, bet)
    if not ok:
        await query.answer("Недостаточно монет!", show_alert=True); return

    state = await adb(db.get_global_roulette_state)
    pot   = state.get("pot", 0)
    await query.answer(f"✅ Ставка {fmt(bet)} 💰 принята!")
    await query.edit_message_text(
        f"🌍 *СТАВКА ПРИНЯТА*\n\n"
        f"Ты поставил *{fmt(bet)} 💰* в глобальный котёл.\n\n"
        f"🏦 Текущий банк: *{fmt(pot)} 💰*\n\n"
        f"_Результат придёт в личку в конце текущей минуты._\n\n"
        f"💼 Баланс: *{fmt(new_balance)} 💰*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🌍 Ещё ставка", callback_data="casino_global"),
            InlineKeyboardButton("◀ Казино",       callback_data="casino_menu"),
        ]]),
    )


async def _global_roulette_tick(context) -> None:
    """Вызывается каждую минуту job queue'ем."""
    try:
        state = await adb(db.get_global_roulette_state)
        rnd   = state.get("round", 0)
        pot   = int(state.get("pot", 0))

        bets = await adb(db.get_global_roulette_bets, rnd)
        if not bets:
            # Никто не ставил — крутим тихо, котёл переходит
            await adb(db.close_global_roulette_round, pot)
            return

        # Сводим по игрокам (один игрок мог поставить несколько раз)
        players: dict[int, int] = {}
        for b in bets:
            players[b["user_id"]] = players.get(b["user_id"], 0) + b["amount"]

        # Спин
        weights  = [o[0] for o in _GLOBAL_OUTCOMES]
        outcome  = random.choices(_GLOBAL_OUTCOMES, weights=weights, k=1)[0]
        _, code, emoji, title, desc = outcome

        winners: dict[int, int] = {}   # uid → выплата

        if code == "burn":
            new_pot = 0
            summary = f"{emoji} *{title}*\n_{desc}_\n\nВсе ставки сгорели. 😢"

        elif code == "jackpot":
            winner_uid = random.choice(list(players.keys()))
            winners[winner_uid] = pot
            new_pot = 0
            winner_user = await adb(db.get_user, winner_uid)
            w_name = winner_user.get("username", "???") if winner_user else "???"
            summary = (
                f"{emoji} *{title}!*\n_{desc}_\n\n"
                f"🎊 Победитель: *{w_name}*\n"
                f"Выигрыш: *{fmt(pot)} 💰*"
            )

        elif code == "split":
            share   = pot // len(players)
            winners = {uid: share for uid in players}
            new_pot = pot - share * len(players)  # остаток в следующий раунд
            summary = (
                f"{emoji} *{title}!*\n_{desc}_\n\n"
                f"Каждый получает: *{fmt(share)} 💰*\n"
                f"Участников: *{len(players)}*"
            )

        elif code == "x2":
            total   = pot * 2
            share   = total // len(players)
            winners = {uid: share for uid in players}
            new_pot = 0
            summary = (
                f"{emoji} *{title}!*\n_{desc}_\n\n"
                f"Банк удвоен → *{fmt(total)} 💰*\n"
                f"Каждый получает: *{fmt(share)} 💰*"
            )

        elif code == "carryover":
            new_pot = pot   # всё в следующий раунд
            summary = (
                f"{emoji} *{title}*\n_{desc}_\n\n"
                f"Банк *{fmt(pot)} 💰* переходит в следующий раунд!\n"
                f"_Ставьте снова — джекпот растёт..._"
            )

        else:  # x5
            total   = pot * 5
            share   = total // len(players)
            winners = {uid: share for uid in players}
            new_pot = 0
            summary = (
                f"{emoji} *{title}!*\n_{desc}_\n\n"
                f"Банк ×5 → *{fmt(total)} 💰*\n"
                f"Каждый получает: *{fmt(share)} 💰*"
            )

        # Зачисляем выигрыши
        for uid, payout in winners.items():
            await adb(db.add_coins, uid, payout)

        # Закрываем раунд
        await adb(db.close_global_roulette_round, new_pot)

        # Отправляем результаты всем участникам в личку
        for uid, stake in players.items():
            payout    = winners.get(uid, 0)
            net       = payout - stake
            net_str   = f"+{fmt(net)}" if net >= 0 else fmt(net)
            bal       = await adb(db.get_coins, uid)
            personal  = (
                f"🌍 *Глобальная рулетка — результат*\n\n"
                f"{summary}\n\n"
                f"Твоя ставка: *{fmt(stake)} 💰*\n"
                f"Выплата: *{fmt(payout)} 💰*  ({net_str})\n\n"
                f"💼 Баланс: *{fmt(bal)} 💰*"
            )
            try:
                await context.bot.send_message(
                    uid, personal, parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🌍 Поставить снова",
                                             callback_data="casino_global"),
                        InlineKeyboardButton("◀ Казино",
                                             callback_data="casino_menu"),
                    ]]),
                )
            except Exception:
                pass  # юзер заблокировал бота — пропускаем

    except Exception as exc:
        logger.exception("global_roulette_tick error: %s", exc)


# ── VIP Зал ───────────────────────────────────────────────────────────────────

async def cb_vip_hall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vip_hall — проверка баланса и экран подтверждения входа."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    coins = await adb(db.get_coins, uid)

    if coins < _VIP_ENTRY_MIN_BALANCE:
        await query.answer(
            f"💎 Минимальный баланс для входа в ВИП Зал: {fmt(_VIP_ENTRY_MIN_BALANCE)} монет.\n"
            f"Ваш баланс: {fmt(coins)} монет.",
            show_alert=True,
        )
        return

    text = (
        "💎 *ВИП ЗАЛ*\n\n"
        "Добро пожаловать! Это эксклюзивная зона для состоятельных игроков.\n\n"
        f"💰 Ваш баланс: *{fmt(coins)}* монет\n"
        f"🎫 Стоимость входа: *{fmt(_VIP_ENTRY_FEE)}* монет\n\n"
        "После оплаты вам откроются:\n"
        f"🎡 *ВИП Рулетка* — ставки от {fmt(min(_VIP_ROULETTE_BETS))}, множители до ×100\n"
        "♠ *ВИП Покер* — сыграй против других игроков на крупные суммы\n\n"
        "_Войти?_"
    )
    rows = [
        [InlineKeyboardButton(f"✅ Войти  ({fmt(_VIP_ENTRY_FEE)} 💰)", callback_data="vip_enter")],
        [InlineKeyboardButton("◀ Казино", callback_data="casino_menu")],
    ]
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))


async def cb_vip_enter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vip_enter — списываем взнос и показываем VIP меню."""
    query = update.callback_query
    uid   = query.from_user.id
    ok, balance = await adb(db.spend_coins, uid, _VIP_ENTRY_FEE)
    if not ok:
        await query.answer("Недостаточно монет!", show_alert=True)
        return
    await query.answer("💎 Добро пожаловать в ВИП Зал!")
    await _show_vip_menu(query, balance)


async def _show_vip_menu(query, balance: int):
    """Отрисовывает главное меню VIP зала (без повторного списания)."""
    text = (
        "💎 *ВИП ЗАЛ КАЗИНО*\n\n"
        f"💰 Баланс: *{fmt(balance)}* монет\n\n"
        "Выбери игру:"
    )
    rows = [
        [InlineKeyboardButton("🎡 ВИП Рулетка",  callback_data="vip_roulette"),
         InlineKeyboardButton("♠ ВИП Покер",     callback_data="vip_lobby")],
        [InlineKeyboardButton("◀ В казино",       callback_data="casino_menu")],
    ]
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))


async def cb_vip_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vip_menu — ВИП меню (бесплатная навигация внутри зала)."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    coins = await adb(db.get_coins, uid)
    await _show_vip_menu(query, coins)


# ── VIP Рулетка ────────────────────────────────────────────────────────────────

async def cb_vip_roulette(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vip_roulette — выбор ставки для ВИП рулетки."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    coins = await adb(db.get_coins, uid)

    rows = []
    for i in range(0, len(_VIP_ROULETTE_BETS), 2):
        pair = _VIP_ROULETTE_BETS[i:i + 2]
        row  = []
        for amt in pair:
            can = coins >= amt
            row.append(InlineKeyboardButton(
                f"{'💎' if can else '🚫'} {fmt(amt)}",
                callback_data=f"vip_rspin_{amt}" if can else "casino_no_coins",
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton("◀ ВИП Зал", callback_data="vip_menu")])

    await query.edit_message_text(
        f"🎡 *ВИП РУЛЕТКА*\n\n"
        f"💰 Баланс: *{fmt(coins)}* монет\n\n"
        f"{_VIP_WHEEL_DISPLAY}\n\n"
        "Выбери ставку:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_vip_roulette_spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vip_rspin_<amount> — крутим ВИП рулетку."""
    query = update.callback_query
    uid   = query.from_user.id
    try:
        bet = int(query.data[len("vip_rspin_"):])
    except ValueError:
        await query.answer("Ошибка ставки.", show_alert=True); return

    ok, _ = await adb(db.spend_coins, uid, bet)
    if not ok:
        await query.answer("Недостаточно монет!", show_alert=True); return
    await query.answer()

    retry_btn = InlineKeyboardButton(f"🎡 Ещё ×{fmt(bet)}", callback_data=f"vip_rspin_{bet}")
    back_btn  = InlineKeyboardButton("◀ ВИП Зал", callback_data="vip_menu")

    for frame in _VIP_SPIN_FRAMES:
        await query.edit_message_text(
            f"🎡 *ВИП Колесо крутится...*\n\n"
            f"`{frame}`\n\n"
            f"_Ставка: {fmt(bet)} 💰_",
            parse_mode="Markdown",
        )
        await asyncio.sleep(0.7)

    result  = _vip_spin_roulette()
    mult    = result["mult"]
    emoji   = result["emoji"]
    label   = result["label"]
    is_doom = result["is_doom"]

    if mult == 0:
        fire_and_forget(adb(db.add_to_dividend_pool, max(1, bet // 20)))
        new_balance = await adb(db.get_coins, uid)
        text = (
            f"💀 *ПУСТО*\n\n"
            f"Выпало: {emoji} *{label}*\n"
            f"Ставка *{fmt(bet)} 💰* — потеряна.\n\n"
            f"💼 Баланс: *{fmt(new_balance)} 💰*"
        )
    elif is_doom:
        _, new_balance = await adb(db.spend_coins, uid, bet)
        fire_and_forget(adb(db.add_to_dividend_pool, max(1, bet * 2 // 20)))
        text = (
            f"💣 *СЛИВ!*\n\n"
            f"Выпало: {emoji} *{label}*\n"
            f"Штраф: ещё *{fmt(bet)} 💰* сгорело!\n\n"
            f"💼 Баланс: *{fmt(new_balance)} 💰*"
        )
    else:
        winnings    = bet * mult
        net_gain    = winnings - bet
        new_balance = await adb(db.add_coins, uid, winnings)
        if net_gain > 0 and mult >= 5:
            fire_and_forget(adb(db.update_max_casino_win, uid, net_gain))
        if mult == 1:
            text = (
                f"🔄 *ВОЗВРАТ*\n\n"
                f"Выпало: {emoji} *{label}*\n"
                f"Ставка *{fmt(bet)} 💰* возвращена.\n\n"
                f"💼 Баланс: *{fmt(new_balance)} 💰*"
            )
        else:
            if mult >= 100:
                hdr = f"👑 *ЛЕГЕНДАРНЫЙ ВЫИГРЫШ {label}!*"
            elif mult >= 50:
                hdr = f"⚡ *НЕВЕРОЯТНЫЙ ВЫИГРЫШ {label}!*"
            elif mult >= 25:
                hdr = f"💥 *МЕГА-ВЫИГРЫШ {label}!*"
            elif mult >= 10:
                hdr = f"🌟 *БОЛЬШОЙ ВЫИГРЫШ {label}!*"
            else:
                hdr = f"💰 *ВЫИГРЫШ {label}!*"
            text = (
                f"{hdr}\n\n"
                f"Выпало: {emoji} *{label}*\n"
                f"Ставка: {fmt(bet)} 💰  ➜  Выигрыш: *+{fmt(net_gain)} 💰*\n\n"
                f"💼 Баланс: *{fmt(new_balance)} 💰*"
            )

    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[retry_btn, back_btn]]),
    )


# ── VIP Покер-лобби ────────────────────────────────────────────────────────────

async def _render_vip_lobby_list(query, uid: int):
    """Отрисовывает список открытых ВИП лобби."""
    coins   = await adb(db.get_coins, uid)
    lobbies = await adb(db.get_active_vip_lobbies)

    text = (
        "♠ *ВИП ПОКЕР — ЛОББИ*\n\n"
        f"💰 Ваш баланс: *{fmt(coins)}* монет\n\n"
    )
    rows = []

    if lobbies:
        text += "Открытые лобби:\n"
        for lb in lobbies:
            players = await adb(db.get_vip_lobby_players, lb["id"])
            n       = len(players)
            host    = await adb(db.get_user, lb["host_uid"])
            hname   = (host.get("username") or host.get("first_name", "?")) if host else "?"
            fee_str = fmt(lb["entry_fee"])
            text += (
                f"\n♠ *#{lb['id']}* — взнос {fee_str} 💰\n"
                f"   👥 {n}/6  |  @{hname}  |  Котёл: {fmt(lb['pot'])} 💰\n"
            )
            rows.append([InlineKeyboardButton(
                f"♠ #{lb['id']}  {fee_str} 💰  ({n}/6)",
                callback_data=f"vip_view_{lb['id']}",
            )])
    else:
        text += "_Нет открытых лобби. Создай своё!_\n"

    rows.append([InlineKeyboardButton("➕ Создать лобби", callback_data="vip_create"),
                 InlineKeyboardButton("🔄 Обновить",      callback_data="vip_lobby")])
    rows.append([InlineKeyboardButton("◀ ВИП Зал",       callback_data="vip_menu")])
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))


async def cb_vip_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vip_lobby — список ВИП покер-лобби."""
    query = update.callback_query
    await query.answer()
    await _render_vip_lobby_list(query, query.from_user.id)


async def cb_vip_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vip_create — выбор взноса для нового лобби."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    coins = await adb(db.get_coins, uid)

    rows = []
    for i in range(0, len(_VIP_LOBBY_FEES), 3):
        row = []
        for fee in _VIP_LOBBY_FEES[i:i + 3]:
            can = coins >= fee
            row.append(InlineKeyboardButton(
                f"{'♠' if can else '🚫'} {fmt(fee)}",
                callback_data=f"vip_cfee_{fee}" if can else "casino_no_coins",
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="vip_lobby")])

    await query.edit_message_text(
        f"♠ *НОВОЕ ЛОББИ*\n\n"
        f"💰 Баланс: *{fmt(coins)}* монет\n\n"
        "Выбери размер взноса для всех участников.\n"
        "Ожидай игроков — победитель забирает весь котёл!\n\n"
        "Выбери взнос:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_vip_create_fee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vip_cfee_<amount> — создаём лобби и списываем взнос."""
    query = update.callback_query
    uid   = query.from_user.id
    try:
        fee = int(query.data[len("vip_cfee_"):])
    except ValueError:
        await query.answer("Ошибка.", show_alert=True); return

    lobby_id = await adb(db.create_vip_lobby, uid, fee)
    if not lobby_id:
        await query.answer("Недостаточно монет или ошибка создания.", show_alert=True); return
    await query.answer(f"♠ Лобби #{lobby_id} создано!")
    await _show_vip_lobby_view(query, lobby_id, uid)


async def _show_vip_lobby_view(query, lobby_id: int, uid: int):
    """Отрисовывает экран конкретного лобби."""
    lobby = await adb(db.get_vip_lobby, lobby_id)
    if not lobby:
        await query.answer("Лобби не найдено.", show_alert=True); return

    players = await adb(db.get_vip_lobby_players, lobby_id)
    is_host = (lobby["host_uid"] == uid)
    in_lobby = any(p["user_id"] == uid for p in players)
    n   = len(players)
    pot = lobby["pot"]
    fee = lobby["entry_fee"]

    host  = await adb(db.get_user, lobby["host_uid"])
    hname = (host.get("username") or host.get("first_name", "?")) if host else "?"

    text = (
        f"♠ *ЛОББИ #{lobby_id}*\n\n"
        f"👑 Хост: @{hname}\n"
        f"💰 Взнос: *{fmt(fee)}* монет\n"
        f"🏆 Котёл: *{fmt(pot)}* монет\n"
        f"👥 Игроков: *{n}/6*\n\n"
        "Участники:\n"
    )
    for p in players:
        u     = await adb(db.get_user, p["user_id"])
        uname = (u.get("username") or u.get("first_name", "?")) if u else str(p["user_id"])
        crown = " 👑" if p["user_id"] == lobby["host_uid"] else ""
        text += f"  • @{uname}{crown}\n"

    rows = []
    if lobby["status"] == "waiting":
        if in_lobby:
            if is_host:
                if n >= 2:
                    rows.append([InlineKeyboardButton(
                        f"▶ Начать игру! ({n} игрока)", callback_data=f"vip_start_{lobby_id}")])
                else:
                    rows.append([InlineKeyboardButton(
                        "⏳ Нужно ≥2 игроков", callback_data="noop")])
            rows.append([
                InlineKeyboardButton("🔄 Обновить",      callback_data=f"vip_view_{lobby_id}"),
                InlineKeyboardButton("🚪 Покинуть",      callback_data=f"vip_leave_{lobby_id}"),
            ])
        else:
            coins = await adb(db.get_coins, uid)
            can   = (coins >= fee and n < 6)
            label = (f"♠ Войти ({fmt(fee)} 💰)" if can
                     else ("🚫 Мест нет" if n >= 6 else "🚫 Недостаточно монет"))
            rows.append([InlineKeyboardButton(
                label,
                callback_data=f"vip_join_{lobby_id}" if can else "casino_no_coins",
            )])
            rows.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"vip_view_{lobby_id}")])
        text += "\n_Ожидаем игроков... Хост нажимает «Начать игру»._"
    elif lobby["status"] == "playing":
        text += "\n_Карты уже розданы — ждите результатов._"
        rows.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"vip_view_{lobby_id}")])
    else:
        # Финиш — показываем руки и победителя если они сохранены
        winner_uid = lobby.get("winner_uid")
        has_hands  = any(p.get("hand") and p["hand"] != [] for p in players)
        if has_hands and winner_uid:
            def _hk(p): return (p.get("hand_rank", -1), [])
            sorted_pl = sorted(players, key=_hk, reverse=True)
            text += "\n🃏 *Итоги игры:*\n\n"
            for place, p in enumerate(sorted_pl, 1):
                u      = await adb(db.get_user, p["user_id"])
                uname  = (u.get("username") or u.get("first_name", "?")) if u else str(p["user_id"])
                medal  = "🏆" if p["user_id"] == winner_uid else f"{place}."
                hstr   = _vip_hand_str(p["hand"]) if p.get("hand") else "—"
                hname  = p.get("hand_name") or "—"
                text  += f"{medal} @{uname}\n`{hstr}`\n_{hname}_\n\n"
            wu     = await adb(db.get_user, winner_uid)
            wname  = (wu.get("username") or wu.get("first_name", "?")) if wu else "?"
            text  += f"🏆 Победитель: *@{wname}* — *+{fmt(pot)}* монет!"
        else:
            text += "\n_Игра завершена._"

    rows.append([InlineKeyboardButton("◀ Все лобби", callback_data="vip_lobby")])
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))


async def cb_vip_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vip_view_<id> — просмотр лобби."""
    query    = update.callback_query
    await query.answer()
    uid      = query.from_user.id
    lobby_id = int(query.data[len("vip_view_"):])
    await _show_vip_lobby_view(query, lobby_id, uid)


async def cb_vip_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vip_join_<id> — присоединиться к лобби."""
    query    = update.callback_query
    uid      = query.from_user.id
    lobby_id = int(query.data[len("vip_join_"):])

    ok, err = await adb(db.join_vip_lobby, lobby_id, uid)
    if not ok:
        await query.answer(err, show_alert=True); return
    await query.answer("♠ Вы в лобби!")

    # Уведомляем всех участников (кроме вошедшего) о новом игроке
    joiner   = await adb(db.get_user, uid)
    jname    = (joiner.get("username") or joiner.get("first_name", "?")) if joiner else "?"
    lobby    = await adb(db.get_vip_lobby, lobby_id)
    players  = await adb(db.get_vip_lobby_players, lobby_id)
    n        = len(players)
    notif    = (
        f"♠ *ВИП Покер — Лобби #{lobby_id}*\n\n"
        f"@{jname} вошёл в лобби! Теперь игроков: *{n}/6*.\n"
        f"{'Хост может начинать игру!' if n >= 2 else 'Ждём ещё игроков...'}"
    )
    for p in players:
        if p["user_id"] != uid:
            fire_and_forget(_safe_send(context, p["user_id"], notif, parse_mode="Markdown"))

    await _show_vip_lobby_view(query, lobby_id, uid)


async def cb_vip_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vip_leave_<id> — покинуть лобби."""
    query    = update.callback_query
    uid      = query.from_user.id
    lobby_id = int(query.data[len("vip_leave_"):])

    was_host, refund = await adb(db.leave_vip_lobby, lobby_id, uid)
    if refund > 0:
        msg = (f"Лобби закрыто. {fmt(refund)} 💰 возвращены всем участникам."
               if was_host else f"Вы покинули лобби. {fmt(refund)} 💰 возвращены.")
        await query.answer(msg, show_alert=was_host)
    else:
        await query.answer("Лобби не найдено или игра уже идёт.", show_alert=True)
    await _render_vip_lobby_list(query, uid)


async def cb_vip_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vip_start_<id> — хост запускает игру: раздаём карты, считаем победителя."""
    query    = update.callback_query
    uid      = query.from_user.id
    lobby_id = int(query.data[len("vip_start_"):])

    lobby = await adb(db.get_vip_lobby, lobby_id)
    if not lobby:
        await query.answer("Лобби не найдено.", show_alert=True); return
    if lobby["host_uid"] != uid:
        await query.answer("Только хост может начать игру.", show_alert=True); return
    if lobby["status"] != "waiting":
        await query.answer("Игра уже началась или завершена.", show_alert=True); return

    players = await adb(db.get_vip_lobby_players, lobby_id)
    n       = len(players)
    if n < 2:
        await query.answer("Нужно минимум 2 игрока!", show_alert=True); return

    await query.answer()

    pot_str   = fmt(lobby["pot"])
    other_ids = [p["user_id"] for p in players if p["user_id"] != uid]

    # ── Уведомляем всех остальных: игра началась ──────────────────────────────
    dealing_msg = (
        f"♠ *ВИП ПОКЕР — Лобби #{lobby_id}*\n\n"
        f"🎴 *Хост начинает раздачу карт...*\n\n"
        f"🂠  🂠  🂠  🂠  🂠\n\n"
        f"💰 Котёл: *{pot_str}* монет  |  👥 Игроков: *{n}*"
    )
    # Шлём одновременно, не ждём — хост сразу видит анимацию
    notify_tasks = [
        asyncio.create_task(_safe_send(context, pid, dealing_msg, parse_mode="Markdown"))
        for pid in other_ids
    ]

    # ── Анимация раздачи (хост) ───────────────────────────────────────────────
    dealing_frames = [
        "🂠  🂠  🂠  🂠  🂠",
        "🂠  🂠  🂠  🂠  🂡",
        "🂠  🂠  🂠  🂡  🂡",
        "🂠  🂠  🂡  🂡  🂡",
        "🂠  🂡  🂡  🂡  🂡",
        "🂡  🂡  🂡  🂡  🂡",
    ]
    for frame in dealing_frames:
        await query.edit_message_text(
            f"♠ *ВИП ПОКЕР — Лобби #{lobby_id}*\n\n"
            f"🎴 *Раздаём карты...*\n\n"
            f"`{frame}`\n\n"
            f"💰 Котёл: *{pot_str}* монет  |  👥 Игроков: *{n}*",
            parse_mode="Markdown",
        )
        await asyncio.sleep(0.6)

    # Ждём, пока уведомления отправились (чтобы не потерять)
    if notify_tasks:
        await asyncio.gather(*notify_tasks, return_exceptions=True)

    # ── Раздача карт ──────────────────────────────────────────────────────────
    raw_hands  = _vip_deal_hands(n)
    hands_data = []
    for i, p in enumerate(players):
        hand        = raw_hands[i]
        rank_int, _ = _vip_hand_score(hand)
        hands_data.append({
            "user_id":   p["user_id"],
            "hand":      hand,
            "hand_rank": rank_int,
            "hand_name": _VIP_HAND_NAMES.get(rank_int, "?"),
        })

    await adb(db.start_vip_game, lobby_id, hands_data)

    # ── Пауза «вскрываем карты» (хост) ───────────────────────────────────────
    await query.edit_message_text(
        f"♠ *ВИП ПОКЕР — Лобби #{lobby_id}*\n\n"
        f"🔍 *Вскрываем карты...*\n\n"
        f"💰 Котёл: *{pot_str}* монет",
        parse_mode="Markdown",
    )
    await asyncio.sleep(1.5)

    # ── Определяем победителя ─────────────────────────────────────────────────
    def _hand_key(hd):
        rank_int, tb = _vip_hand_score(hd["hand"])
        return (rank_int, tb)

    sorted_hands = sorted(hands_data, key=_hand_key, reverse=True)
    winner       = sorted_hands[0]
    prize        = lobby["pot"]
    await adb(db.finish_vip_game, lobby_id, winner["user_id"], prize)

    # ── Формируем текст результата ────────────────────────────────────────────
    winner_u    = await adb(db.get_user, winner["user_id"])
    winner_name = (winner_u.get("username") or winner_u.get("first_name", "?")) if winner_u else "?"

    result_header = (
        f"♠ *ВИП ПОКЕР — РЕЗУЛЬТАТ*\n"
        f"Лобби *#{lobby_id}*\n\n"
        f"💰 Котёл: *{fmt(prize)}* монет\n\n"
        "🃏 *Расклад:*\n\n"
    )
    result_body = ""
    for place, hd in enumerate(sorted_hands, 1):
        u      = await adb(db.get_user, hd["user_id"])
        uname  = (u.get("username") or u.get("first_name", "?")) if u else str(hd["user_id"])
        medal  = "🏆" if place == 1 else f"{place}."
        result_body += f"{medal} @{uname}\n`{_vip_hand_str(hd['hand'])}`\n_{hd['hand_name']}_\n\n"

    result_footer = f"🏆 Победитель: *@{winner_name}* — *+{fmt(prize)} 💰*!"
    result_text   = result_header + result_body + result_footer

    nav_rows = [
        [InlineKeyboardButton("♠ Новое лобби", callback_data="vip_lobby")],
        [InlineKeyboardButton("◀ ВИП Зал",    callback_data="vip_menu")],
    ]

    # ── Хосту показываем результат ────────────────────────────────────────────
    await query.edit_message_text(result_text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(nav_rows))

    # ── Рассылаем персональный результат всем остальным игрокам ──────────────
    result_tasks = []
    for hd in hands_data:
        if hd["user_id"] == uid:
            continue   # хост уже видит результат на экране
        my_hand_str  = _vip_hand_str(hd["hand"])
        my_hand_name = hd["hand_name"]
        won_str      = f"*+{fmt(prize)} 💰* 🏆" if hd["user_id"] == winner["user_id"] else "—"
        personal     = (
            f"♠ *ВИП ПОКЕР — итоги лобби #{lobby_id}*\n\n"
            f"Твои карты:\n`{my_hand_str}`\n_{my_hand_name}_\n\n"
            f"Твой результат: {won_str}\n\n"
            + result_header + result_body + result_footer
        )
        result_tasks.append(asyncio.create_task(_safe_send(
            context, hd["user_id"], personal,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(nav_rows),
        )))
    if result_tasks:
        await asyncio.gather(*result_tasks, return_exceptions=True)


# ── Combo customization ───────────────────────────────────────────────────────

async def cb_combo_customize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """combo_customize — главное меню кастомизации."""
    query   = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    user    = db.get_user(uid)
    count   = db.get_combo_count(uid)
    level_n = _combo_level(count)["level"]

    # Маппинг слота → reward_type
    _SLOT_RTYPE = {
        "prefix1": "prefix", "prefix2": "prefix",
        "suffix1": "suffix", "suffix2": "suffix",
        "title":   "title",  "banner":  "banner",
        "divider": "divider",
    }

    slot_info = [
        ("prefix1",  "✏️ Prefix 1",    "combo_prefix1"),
        ("prefix2",  "✏️ Prefix 2",    "combo_prefix2"),
        ("suffix1",  "✏️ Suffix 1",    "combo_suffix1"),
        ("suffix2",  "✏️ Suffix 2",    "combo_suffix2"),
        ("title",    "🏷 Титул",       "combo_title"),
        ("banner",   "🎨 Баннер",      "combo_banner"),
        ("about",    "📝 О себе",      "combo_about"),
        ("divider",  "〰️ Разделитель", "combo_divider"),
    ]

    # Unlocked slots
    unlocked_rows = []
    for key, label, field in slot_info:
        if _has_unlock(level_n, key):
            current_val = user.get(field) or "—"
            unlocked_rows.append([InlineKeyboardButton(
                f"{label}: {current_val}",
                callback_data=f"combo_edit_{key}",
            )])

    # Locked slots where the user has at least one achievement waiting
    earned = set(db.get_user_achievements(uid))
    locked_rows = []
    for key, label, field in slot_info:
        if _has_unlock(level_n, key):
            continue  # already shown above
        rtype = _SLOT_RTYPE.get(key)
        if not rtype:
            continue
        has_waiting = any(
            ACHIEVEMENTS[aid].get("reward_type") == rtype
            for aid in earned
            if aid in ACHIEVEMENTS
        )
        if has_waiting:
            # Find unlock level info
            unlock_info = next(
                (lvl for lvl in COMBO_LEVELS if key in lvl["unlocks"]),
                None,
            )
            if unlock_info:
                locked_label = (
                    f"🔒 {label} — ур.{unlock_info['level']} "
                    f"«{unlock_info['name']}» ({unlock_info['threshold']} комбо)"
                )
            else:
                locked_label = f"🔒 {label}"
            locked_rows.append([InlineKeyboardButton(
                locked_label,
                callback_data=f"combo_locked_{key}",
            )])

    rows = unlocked_rows + locked_rows

    if not rows:
        await query.edit_message_text(
            "🎨 *Кастомизация ника*\n\n"
            "Слоты кастомизации открываются с уровнем.\n"
            "Первый слот (Prefix) откроется на уровне 4 *Следопыт* (45 комбо).",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="show_profile")]]),
        )
        return

    rows.append([InlineKeyboardButton("◀ Назад к профилю", callback_data="show_profile")])

    # Build preview of current name
    display = _combo_display_name(user, level_n)
    title_txt = f"\n_{user['combo_title']}_" if _has_unlock(level_n, "title") and user.get("combo_title") else ""

    # Footer hint if there are locked-but-waiting slots
    locked_hint = (
        "\n_🔒 Некоторые слоты ждут тебя — заработай комбо для разблокировки!_"
        if locked_rows else ""
    )

    await query.edit_message_text(
        f"🎨 *Кастомизация ника*\n\n"
        f"Предпросмотр: *{display}*{title_txt}\n\n"
        f"Выбери что изменить:{locked_hint}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_combo_edit_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """combo_edit_<slot> — показывает доступные варианты из достижений для этого слота."""
    query   = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    slot    = query.data[len("combo_edit_"):]   # prefix1 / suffix2 / title / banner / divider / about

    user    = db.get_user(uid)
    current = (user.get(f"combo_{slot}") or "") if user else ""

    SLOT_LABELS = {
        "prefix1": "✏️ Prefix 1",  "prefix2": "✏️ Prefix 2",
        "suffix1": "✏️ Suffix 1",  "suffix2": "✏️ Suffix 2",
        "title":   "🏷 Титул",     "banner":  "🎨 Баннер",
        "about":   "📝 О себе",    "divider": "〰️ Разделитель",
    }
    # Маппинг слота → reward_type в ACHIEVEMENTS
    SLOT_RTYPE = {
        "prefix1": "prefix", "prefix2": "prefix",
        "suffix1": "suffix", "suffix2": "suffix",
        "title":   "title",  "banner":  "banner",
        "divider": "divider",
    }
    label = SLOT_LABELS.get(slot, slot)

    # Слот "about" — только текстовый ввод
    if slot == "about":
        db.set_pending_action(uid, "combo_edit", {"slot": "about"})
        cur_txt = f"`{current}`" if current else "_пусто_"
        await query.edit_message_text(
            f"📝 *О себе*\n\nТекущее: {cur_txt}\n\n"
            "Напиши новый текст (до 80 символов).\n"
            "Чтобы очистить — отправь `-`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="combo_customize"),
            ]]),
        )
        return

    # Для остальных слотов — кнопочный выбор из заработанных достижений
    rtype  = SLOT_RTYPE.get(slot)
    earned = set(db.get_user_achievements(uid))

    # Собираем все доступные варианты для этого типа награды
    options: list[tuple[str, str, str]] = []   # (ach_id, value, name)
    for ach_id, a in ACHIEVEMENTS.items():
        if ach_id in earned and a.get("reward_type") == rtype:
            options.append((ach_id, a["reward_value"], a["name"]))

    rows: list = []
    cur_txt = f"`{current}`" if current else "_пусто_"

    if options:
        for ach_id, val, name in options:
            mark = " ✅" if val == current else ""
            rows.append([InlineKeyboardButton(
                f"{val}  {name}{mark}",
                callback_data=f"combo_set_{slot}_{ach_id}",
            )])
    else:
        # Нет доступных вариантов — подсказываем какие ачивки дают этот тип
        hints = [
            f"  • {a['emoji']} {a['name']} ({a['desc']})"
            for a in ACHIEVEMENTS.values()
            if a.get("reward_type") == rtype
        ]
        hint_block = "\n".join(hints[:5])  # показываем первые 5

    if current:
        rows.append([InlineKeyboardButton("🗑 Очистить", callback_data=f"combo_set_{slot}_CLEAR")])
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="combo_customize")])

    if options:
        body = f"Текущее: {cur_txt}\n\nВыбери вариант:"
    else:
        body = (
            f"Текущее: {cur_txt}\n\n"
            f"_У тебя пока нет наград для этого слота._\n"
            f"Зарабатывай достижения:\n{hint_block}"
        )

    await query.edit_message_text(
        f"{label}\n\n{body}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_combo_set_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """combo_set_<slot>_<ach_id|CLEAR> — устанавливает значение слота."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id

    # Парсим: combo_set_<slot>_<rest>
    # Известные слоты — ни один не содержит несколько слов с _
    rest  = query.data[len("combo_set_"):]
    SLOTS = ["prefix1", "prefix2", "suffix1", "suffix2", "title", "banner", "divider", "about"]
    slot  = next((s for s in SLOTS if rest.startswith(s + "_")), None)
    if not slot:
        return
    ach_id = rest[len(slot) + 1:]

    field = f"combo_{slot}"

    if ach_id == "CLEAR":
        db.update_combo_customization(uid, **{field: None})
        await query.answer("🗑 Очищено", show_alert=False)
    else:
        ach = ACHIEVEMENTS.get(ach_id)
        if not ach:
            await query.answer("❌ Достижение не найдено", show_alert=True)
            return
        earned = set(db.get_user_achievements(uid))
        if ach_id not in earned:
            await query.answer("❌ Достижение не заработано", show_alert=True)
            return
        db.update_combo_customization(uid, **{field: ach["reward_value"]})
        await query.answer(f"✅ {ach['reward_value']} — надет!", show_alert=False)

    # Возвращаемся в меню слота чтобы видеть обновлённый ✅
    query.data = f"combo_edit_{slot}"
    await cb_combo_edit_slot(update, context)


async def cb_combo_locked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """combo_locked_<slot> — тап по заблокированному слоту, показывает условие разблокировки."""
    query = update.callback_query
    slot  = query.data[len("combo_locked_"):]

    unlock_lvl = next(
        (lvl for lvl in COMBO_LEVELS if slot in lvl["unlocks"]),
        None,
    )
    if unlock_lvl:
        await query.answer(
            f"🔒 Слот разблокируется на уровне {unlock_lvl['level']} "
            f"«{unlock_lvl['name']}»\n"
            f"Нужно {unlock_lvl['threshold']} комбо — продолжай собирать!",
            show_alert=True,
        )
    else:
        await query.answer("🔒 Этот слот пока заблокирован", show_alert=True)


# ══════════════════════════════════════════════════════════════════════════════
# ── 🛠 DEBUG PANEL (superadmin only) ─────────────────────────────────────────
# Не трогает рейтинги и историю игр — только ачивки и комбо-логи.
# ─────────────────────────────────────────────────────────────────────────────

def _is_superadmin(user_id: int) -> bool:
    return user_id in config.SUPERADMIN_IDS


def _debug_snap_info(context) -> str:
    """Текст о текущем снэпшоте для вставки в сообщение панели."""
    snap = context.user_data.get("debug_snapshot")
    if not snap:
        return ""
    n_ach  = len(snap.get("achievements", []))
    n_cmb  = len(snap.get("combo_logs",   []))
    return f"\n\n📸 Снэпшот: *{n_ach}* ачивок · *{n_cmb}* комбо-логов"


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/debug — открывает дебаг-панель (только суперадмин)."""
    if not _is_superadmin(update.effective_user.id):
        return
    has_snap = bool(context.user_data.get("debug_snapshot"))
    await update.message.reply_text(
        "🛠 *Debug Panel*\n\n"
        "Всё что здесь делается — только ачивки и комбо-логи.\n"
        "Рейтинг и история игр *не затрагиваются*."
        + _debug_snap_info(context),
        parse_mode="Markdown",
        reply_markup=_debug_kb(has_snapshot=has_snap),
    )


def _debug_kb(has_snapshot: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📸 Создать снэпшот",    callback_data="dbg_snap_create")],
        [InlineKeyboardButton("🏅 Выдать ачивку",      callback_data="dbg_grant_list_games")],
        [InlineKeyboardButton("🗑 Отозвать ачивку",    callback_data="dbg_revoke_list")],
        [InlineKeyboardButton("🎲 Добавить комбо-лог", callback_data="dbg_combo_menu")],
        [InlineKeyboardButton("🧹 Сбросить мои комбо-логи", callback_data="dbg_clear_combos_confirm")],
        [InlineKeyboardButton("✅ Проверить ачивки сейчас",  callback_data="dbg_check_ach")],
        [InlineKeyboardButton("💰 Управление монетами", callback_data="dbg_coins_list")],
        [InlineKeyboardButton("🎒 Инвентарь игроков",   callback_data="dbg_inv_list")],
        [InlineKeyboardButton("🛠 Управление магазином", callback_data="dbg_shop_menu")],
    ]
    if has_snapshot:
        rows.insert(1, [InlineKeyboardButton(
            "🔄 Восстановить из снэпшота", callback_data="dbg_snap_restore_confirm",
        )])
    return InlineKeyboardMarkup(rows)


# ── Coin management (superadmin only) ─────────────────────────────────────────

async def cb_dbg_coins_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_coins_list — список пользователей для управления балансом."""
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return

    users = db.get_all_users()
    rows = []
    for u in users:
        balance = int(u.get("coins", 0))
        rows.append([InlineKeyboardButton(
            f"{u['username']}  —  💰 {fmt(balance)}",
            callback_data=f"dbg_coins_user_{u['user_id']}",
        )])
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="dbg_back_to_panel")])
    await query.edit_message_text(
        "💰 *Управление монетами*\n\nВыбери игрока:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_dbg_coins_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_coins_user_<uid> — экран изменения баланса конкретного игрока."""
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return

    uid  = int(query.data[len("dbg_coins_user_"):])
    user = db.get_user(uid)
    if not user:
        await query.answer("Игрок не найден", show_alert=True); return
    balance = int(user.get("coins", 0))

    # Пресеты: +/- 100, 500, 1000, 5000
    presets = [100, 500, 1000, 5000]
    rows = []
    for amt in presets:
        rows.append([
            InlineKeyboardButton(f"➕ {fmt(amt)}", callback_data=f"dbg_coins_adj_{uid}_{amt}"),
            InlineKeyboardButton(f"➖ {fmt(amt)}", callback_data=f"dbg_coins_adj_{uid}_-{amt}"),
        ])
    rows.append([InlineKeyboardButton("🧹 Обнулить", callback_data=f"dbg_coins_zero_{uid}")])
    rows.append([InlineKeyboardButton("◀ К списку", callback_data="dbg_coins_list")])

    await query.edit_message_text(
        f"💰 *Управление монетами*\n\n"
        f"Игрок: *{user['username']}*\n"
        f"Текущий баланс: *{fmt(balance)}* монет\n\n"
        f"_Выбери операцию:_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_dbg_coins_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_coins_adj_<uid>_<delta> — прибавляет/отнимает монеты."""
    query = update.callback_query
    if not _is_superadmin(query.from_user.id):
        await query.answer(); return

    # dbg_coins_adj_<uid>_<delta>
    rest  = query.data[len("dbg_coins_adj_"):]
    parts = rest.split("_")
    uid   = int(parts[0])
    delta = int(parts[1])

    new_balance = db.add_coins(uid, delta)
    user = db.get_user(uid)
    sign = "+" if delta >= 0 else ""
    await query.answer(
        f"✅ {user['username']}: {sign}{fmt(delta)} → 💰 {fmt(new_balance)}",
        show_alert=True,
    )
    # Перерисовываем экран игрока
    await cb_dbg_coins_user(update, context)


async def cb_dbg_coins_zero(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_coins_zero_<uid> — обнуляет баланс игрока."""
    query = update.callback_query
    if not _is_superadmin(query.from_user.id):
        await query.answer(); return

    uid  = int(query.data[len("dbg_coins_zero_"):])
    user = db.get_user(uid)
    if not user:
        await query.answer("Игрок не найден", show_alert=True); return
    cur = int(user.get("coins", 0))
    if cur > 0:
        db.add_coins(uid, -cur)
    await query.answer(f"✅ Баланс {user['username']} обнулён", show_alert=True)
    await cb_dbg_coins_user(update, context)


async def _render_dbg_inv_user(query, uid: int) -> None:
    """Рендерит список предметов игрока. НЕ вызывает query.answer()."""
    user  = await adb(db.get_user, uid)
    if not user:
        await query.edit_message_text("Игрок не найден.")
        return
    items = await adb(db.get_user_items, uid)

    lines = [f"🎒 *Инвентарь: {user['username']}*", ""]
    rows  = []
    if not items:
        lines.append("_Пусто._")
    else:
        by_slot: dict[str, list] = {}
        for it in items:
            by_slot.setdefault(it["slot_type"], []).append(it)
        for slot, slot_label in _SHOP_SLOTS:
            for it in by_slot.get(slot, []):
                ct = it["content"]
                ct_short = ct if len(ct) <= 18 else ct[:16] + "…"
                label = f"🗑 [{slot_label}] {ct_short} — {it['name']}"
                rows.append([InlineKeyboardButton(
                    label[:64],
                    callback_data=f"dbg_inv_rm_{uid}_{it['item_id']}",
                )])
    rows.append([InlineKeyboardButton("◀ К списку", callback_data="dbg_inv_list")])
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_dbg_inv_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_inv_list — выбор игрока для просмотра инвентаря."""
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return

    users = await adb(db.get_all_users)
    rows  = []
    for u in users:
        items = await adb(db.get_user_items, u["user_id"])
        n = len(items)
        if n == 0:
            continue
        rows.append([InlineKeyboardButton(
            f"{u['username']}  —  🎒 {n} {_plural(n, 'предмет', 'предмета', 'предметов')}",
            callback_data=f"dbg_inv_user_{u['user_id']}",
        )])
    if not rows:
        rows.append([InlineKeyboardButton("_(инвентарь пуст у всех)_",
                                          callback_data="dbg_inv_list")])
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="dbg_back_to_panel")])
    await query.edit_message_text(
        "🎒 *Инвентари игроков*\n\nПоказаны только те, у кого есть хоть один предмет:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_dbg_inv_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_inv_user_<uid> — список предметов игрока с кнопками удаления."""
    query = update.callback_query
    if not _is_superadmin(query.from_user.id):
        await query.answer("Нет доступа", show_alert=True); return
    await query.answer()
    uid = int(query.data[len("dbg_inv_user_"):])
    await _render_dbg_inv_user(query, uid)


async def cb_dbg_inv_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_inv_rm_<uid>_<item_id> — удаление предмета из инвентаря игрока."""
    query = update.callback_query
    if not _is_superadmin(query.from_user.id):
        await query.answer("Нет доступа", show_alert=True); return

    tail = query.data[len("dbg_inv_rm_"):]
    uid_str, item_id_str = tail.rsplit("_", 1)
    uid, item_id = int(uid_str), int(item_id_str)

    user = await adb(db.get_user, uid)
    item = await adb(db.get_shop_item, item_id)
    if not user or not item:
        await query.answer("Не найдено", show_alert=True); return

    ok = await adb(db.remove_user_item, uid, item_id)
    if ok:
        await query.answer(f"🗑 Удалено: {item['name']}", show_alert=True)
    else:
        await query.answer("❌ Ошибка удаления", show_alert=True)

    # Обновляем экран инвентаря — answer() уже вызван выше, хелпер его не трогает
    await _render_dbg_inv_user(query, uid)


async def cb_dbg_back_to_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_back_to_panel — возврат на главный экран дебаг-панели."""
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return
    has_snap = bool(context.user_data.get("debug_snapshot"))
    await query.edit_message_text(
        "🛠 *Debug Panel*\n\n"
        "Всё что здесь делается — только ачивки и комбо-логи.\n"
        "Рейтинг и история игр *не затрагиваются*."
        + _debug_snap_info(context),
        parse_mode="Markdown",
        reply_markup=_debug_kb(has_snapshot=has_snap),
    )


# ── Shop catalog management (superadmin only) ─────────────────────────────────

_DBG_SHOP_FIELDS = {
    "name":        ("Название",         "text",   60),
    "content":     ("Контент",          "text",   100),
    "price":       ("Цена",             "int",    100000),
    "rarity":      ("Редкость",         "choice", None),
    "description": ("Описание",         "text",   200),
}
_DBG_RARITY_OPTIONS = ["common", "rare", "epic", "legendary"]


async def cb_dbg_shop_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_shop_menu — главный экран управления магазином."""
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return

    rows = [
        [InlineKeyboardButton(label, callback_data=f"dbg_shop_list_{slot}")]
        for slot, label in _SHOP_SLOTS
    ]
    rows.append([InlineKeyboardButton("➕ Добавить новый предмет",
                                       callback_data="dbg_shop_add_pick")])
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="dbg_back_to_panel")])
    await query.edit_message_text(
        "🛠 *Управление магазином*\n\n"
        "Выбери тип слота для редактирования\n"
        "или добавь новый предмет.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_dbg_shop_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_shop_list_<slot> — список ВСЕХ предметов слота (включая скрытые)."""
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return

    slot  = query.data[len("dbg_shop_list_"):]
    items = db.list_all_shop_items(slot)
    slot_label = dict(_SHOP_SLOTS).get(slot, slot)

    rows = []
    for it in items:
        hidden_mark = "🚫 " if not it.get("available", True) else ""
        # Контент урезаем чтобы влезло в кнопку
        ct = it["content"]
        ct_short = ct if len(ct) <= 16 else ct[:14] + "…"
        rows.append([InlineKeyboardButton(
            f"{hidden_mark}{ct_short} — {it['name']} ({it['price']}💰)",
            callback_data=f"dbg_shop_item_{it['item_id']}",
        )])
    rows.append([InlineKeyboardButton("➕ Добавить в этот слот",
                                       callback_data=f"dbg_shop_add_{slot}")])
    rows.append([InlineKeyboardButton("◀ К магазину", callback_data="dbg_shop_menu")])

    text = f"🛠 *{slot_label}* — {len(items)} {_plural(len(items), 'предмет', 'предмета', 'предметов')}"
    if not items:
        text += "\n\n_Пусто._"
    await query.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_dbg_shop_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_shop_item_<id> — экран редактирования одного предмета."""
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return

    item_id = int(query.data[len("dbg_shop_item_"):])
    item = db.get_shop_item(item_id)
    if not item:
        await query.answer("Не найдено", show_alert=True); return
    owners = db.count_item_owners(item_id)

    lines = [
        f"🛠 *Редактирование #{item_id}*",
        "",
        f"Слот: `{item['slot_type']}`",
        f"Название: *{item['name']}*",
        f"Контент: `{item['content']}`",
        f"Цена: *{item['price']}* 💰",
        f"Редкость: *{item['rarity']}*",
    ]
    if item.get("description"):
        lines.append(f"Описание: _{item['description']}_")
    avail_label = "✅ доступен" if item.get("available", True) else "🚫 скрыт"
    lines.append(f"Статус: {avail_label}")
    if owners:
        lines.append(f"\n📦 Куплен *{owners}* {_plural(owners, 'игроком', 'игроками', 'игроками')}")

    rows = [
        [InlineKeyboardButton("✏️ Название",  callback_data=f"dbg_shop_edit_name_{item_id}"),
         InlineKeyboardButton("✏️ Контент",   callback_data=f"dbg_shop_edit_content_{item_id}")],
        [InlineKeyboardButton("💰 Цена",       callback_data=f"dbg_shop_edit_price_{item_id}"),
         InlineKeyboardButton("💎 Редкость",   callback_data=f"dbg_shop_edit_rarity_{item_id}")],
        [InlineKeyboardButton("📝 Описание",   callback_data=f"dbg_shop_edit_description_{item_id}")],
        [InlineKeyboardButton(
            "🚫 Скрыть" if item.get("available", True) else "✅ Показать",
            callback_data=f"dbg_shop_toggle_{item_id}",
        )],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"dbg_shop_delete_{item_id}")],
        [InlineKeyboardButton("◀ К списку",
                              callback_data=f"dbg_shop_list_{item['slot_type']}")],
    ]
    await query.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_dbg_shop_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_shop_edit_<field>_<item_id> — запрос текстового ввода для поля."""
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return

    # dbg_shop_edit_<field>_<item_id>
    rest = query.data[len("dbg_shop_edit_"):]
    # field может быть многословным: 'description'/'name'/'content'/'price'/'rarity'
    # т.к. поля одиночные, парсим: последний _ перед числом
    last_us = rest.rfind("_")
    field   = rest[:last_us]
    item_id = int(rest[last_us+1:])

    item = db.get_shop_item(item_id)
    if not item:
        await query.answer("Не найдено", show_alert=True); return
    label, kind, _ = _DBG_SHOP_FIELDS.get(field, (field, "text", 100))

    if kind == "choice":
        # Для rarity — сразу показываем выбор
        rows = [[InlineKeyboardButton(
            ("✅ " if r == item.get("rarity") else "") + r,
            callback_data=f"dbg_shop_setrar_{r}_{item_id}",
        )] for r in _DBG_RARITY_OPTIONS]
        rows.append([InlineKeyboardButton("◀ Назад",
                                          callback_data=f"dbg_shop_item_{item_id}")])
        await query.edit_message_text(
            f"💎 *Редкость* для #{item_id}\nТекущая: *{item.get('rarity','common')}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    # Сохраняем в pending_action чтобы следующее сообщение текстом применилось
    db.set_pending_action(query.from_user.id, "dbg_shop_edit", {
        "item_id": item_id,
        "field":   field,
    })
    current = item.get(field, "")
    await query.edit_message_text(
        f"✏️ *Изменение поля «{label}»* для #{item_id}\n\n"
        f"Текущее значение:\n`{current}`\n\n"
        f"Отправь новое значение текстом.\n"
        f"_Отправь `-` чтобы очистить (для описания)._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data=f"dbg_shop_item_{item_id}"),
        ]]),
    )


async def cb_dbg_shop_setrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_shop_setrar_<rarity>_<item_id> — выставить редкость."""
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return

    rest = query.data[len("dbg_shop_setrar_"):]
    last_us = rest.rfind("_")
    rarity  = rest[:last_us]
    item_id = int(rest[last_us+1:])
    if rarity not in _DBG_RARITY_OPTIONS:
        await query.answer("Невалидная редкость", show_alert=True); return
    db.update_shop_item(item_id, rarity=rarity)
    await cb_dbg_shop_item(update, context)


async def cb_dbg_shop_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_shop_toggle_<id> — переключение available."""
    query = update.callback_query
    if not _is_superadmin(query.from_user.id):
        await query.answer(); return
    item_id = int(query.data[len("dbg_shop_toggle_"):])
    item = db.get_shop_item(item_id)
    if not item:
        await query.answer("Не найдено", show_alert=True); return
    new_state = not item.get("available", True)
    db.update_shop_item(item_id, available=new_state)
    await query.answer("✅ Скрыт" if not new_state else "✅ Доступен")
    await cb_dbg_shop_item(update, context)


async def cb_dbg_shop_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_shop_delete_<id> — подтверждение удаления."""
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return
    item_id = int(query.data[len("dbg_shop_delete_"):])
    item = db.get_shop_item(item_id)
    if not item:
        await query.answer("Не найдено", show_alert=True); return
    owners = db.count_item_owners(item_id)
    warning = ""
    if owners:
        warning = (f"\n\n⚠️ *ВНИМАНИЕ:* предмет уже куплен *{owners}* игроком/ами!\n"
                   f"При удалении он пропадёт у них из инвентаря.")
    await query.edit_message_text(
        f"🗑 *Удалить #{item_id}?*\n\n"
        f"`{item['content']}` — *{item['name']}*"
        + warning,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, удалить",
                                  callback_data=f"dbg_shop_deldo_{item_id}")],
            [InlineKeyboardButton("❌ Отмена",
                                  callback_data=f"dbg_shop_item_{item_id}")],
        ]),
    )


async def cb_dbg_shop_deldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_shop_deldo_<id> — выполнить удаление."""
    query = update.callback_query
    if not _is_superadmin(query.from_user.id):
        await query.answer(); return
    item_id = int(query.data[len("dbg_shop_deldo_"):])
    item = db.get_shop_item(item_id)
    slot = item.get("slot_type") if item else None
    ok = db.delete_shop_item(item_id)
    await query.answer("🗑 Удалено" if ok else "❌ Ошибка", show_alert=True)
    if slot:
        # Возвращаем на список этого слота
        query.data = f"dbg_shop_list_{slot}"
        await cb_dbg_shop_list(update, context)
    else:
        await cb_dbg_shop_menu(update, context)


async def cb_dbg_shop_add_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_shop_add_pick — выбор слота для нового предмета."""
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return
    rows = [[InlineKeyboardButton(label, callback_data=f"dbg_shop_add_{slot}")]
            for slot, label in _SHOP_SLOTS]
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="dbg_shop_menu")])
    await query.edit_message_text(
        "➕ *Новый предмет*\n\nВыбери слот:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_dbg_shop_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_shop_add_<slot> — начало добавления нового предмета."""
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return
    slot = query.data[len("dbg_shop_add_"):]
    if slot not in {s for s, _ in _SHOP_SLOTS}:
        await query.answer("Неверный слот", show_alert=True); return

    db.set_pending_action(query.from_user.id, "dbg_shop_add", {"slot": slot})
    await query.edit_message_text(
        f"➕ *Новый предмет ({slot})*\n\n"
        f"Отправь данные одной строкой через `|`:\n\n"
        f"`контент | название | цена | редкость [| описание]`\n\n"
        f"*Пример:*\n"
        f"`🌈 | Радуга | 300 | rare | Семицветный`\n\n"
        f"_Редкость: common / rare / epic / legendary_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="dbg_shop_menu"),
        ]]),
    )


# ── Grant list ────────────────────────────────────────────────────────────────

_DBG_CAT_ORDER = ["games", "wins", "streaks", "colors", "combos", "special",
                  "hidden", "legendary"]
_DBG_CAT_NAMES = {
    "games": "🎮 Игры", "wins": "🏆 Победы", "streaks": "🔥 Серии",
    "colors": "🎨 Цвета", "combos": "🎲 Комбо", "special": "✨ Особые",
    "hidden": "🔮 Скрытые", "legendary": "👑 Легендарные",
}


async def cb_dbg_grant_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_grant_list_<cat> — список ачивок категории для выдачи."""
    query   = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    if not _is_superadmin(uid): return

    cat     = query.data[len("dbg_grant_list_"):]
    earned  = set(db.get_user_achievements(uid))
    items   = [(k, v) for k, v in ACHIEVEMENTS.items() if v.get("cat") == cat]

    rows = []
    for ach_id, a in items:
        mark  = "✅ " if ach_id in earned else ""
        rows.append([InlineKeyboardButton(
            f"{mark}{a['emoji']} {a['name']}",
            callback_data=f"dbg_grant_{ach_id}",
        )])

    # Навигация по категориям
    cat_row = []
    for c in _DBG_CAT_ORDER:
        label = ("▶" if c == cat else "") + _DBG_CAT_NAMES[c]
        cat_row.append(InlineKeyboardButton(label, callback_data=f"dbg_grant_list_{c}"))
    rows.append(cat_row[:3])
    rows.append(cat_row[3:])
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="dbg_back")])

    await query.edit_message_text(
        f"🏅 *Выдать ачивку* — {_DBG_CAT_NAMES.get(cat, cat)}\n"
        f"_(✅ = уже есть, повторное нажатие — снова выдаст уведомление)_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_dbg_grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_grant_<ach_id> — выдаёт ачивку суперадмину."""
    query  = update.callback_query
    uid    = query.from_user.id
    if not _is_superadmin(uid):
        await query.answer(); return

    ach_id = query.data[len("dbg_grant_"):]
    a      = ACHIEVEMENTS.get(ach_id)
    if not a:
        await query.answer("❌ Не найдено", show_alert=True); return

    # Сначала отзываем чтобы выдача сработала даже если уже была
    db.revoke_achievement(uid, ach_id)
    db.grant_achievement(uid, ach_id)
    await query.answer(f"✅ Выдана: {a['emoji']} {a['name']}", show_alert=True)

    # Показываем уведомление как в реальной игре
    rtype = a.get("reward_type", "")
    rval  = a.get("reward_value", "")
    reward_str = {
        "prefix":  f"prefix-эмодзи `{rval}`",
        "suffix":  f"suffix-эмодзи `{rval}`",
        "title":   f"титул «{rval}»",
        "banner":  f"баннер `{rval}`",
        "divider": f"разделитель `{rval}`",
    }.get(rtype, rval)
    try:
        await context.bot.send_message(
            chat_id=uid,
            text=(
                f"🏅 *Новые достижения!*\n\n"
                f"{a['emoji']} *{a['name']}*\n"
                f"  _{a['desc']}_\n"
                f"  🎁 Награда: {reward_str}\n\n"
                "_Открой Профиль → Кастомизация чтобы надеть._"
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass

    # Обновляем список
    cat = a.get("cat", "special")
    query.data = f"dbg_grant_list_{cat}"
    await cb_dbg_grant_list(update, context)


# ── Revoke list ───────────────────────────────────────────────────────────────

async def cb_dbg_revoke_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_revoke_list — список заработанных ачивок для отзыва."""
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    if not _is_superadmin(uid): return

    earned = db.get_user_achievements(uid)
    if not earned:
        await query.edit_message_text(
            "🗑 *Отозвать ачивку*\n\n_Нет заработанных ачивок._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀ Назад", callback_data="dbg_back"),
            ]]),
        )
        return

    rows = []
    for ach_id in earned:
        a = ACHIEVEMENTS.get(ach_id, {})
        rows.append([InlineKeyboardButton(
            f"🗑 {a.get('emoji','?')} {a.get('name', ach_id)}",
            callback_data=f"dbg_revoke_{ach_id}",
        )])
    rows.append([InlineKeyboardButton("◀ Назад", callback_data="dbg_back")])

    await query.edit_message_text(
        "🗑 *Отозвать ачивку* — нажми чтобы убрать:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_dbg_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_revoke_<ach_id> — отзывает ачивку."""
    query  = update.callback_query
    uid    = query.from_user.id
    if not _is_superadmin(uid):
        await query.answer(); return

    ach_id = query.data[len("dbg_revoke_"):]
    a      = ACHIEVEMENTS.get(ach_id, {})
    db.revoke_achievement(uid, ach_id)
    await query.answer(f"🗑 Удалена: {a.get('name', ach_id)}", show_alert=True)
    await cb_dbg_revoke_list(update, context)


# ── Combo log ─────────────────────────────────────────────────────────────────

async def cb_dbg_combo_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_combo_menu — категории комбо для добавления тест-лога."""
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return

    rows = [
        [InlineKeyboardButton("1-2-3-4-5-6 (стрит)",  callback_data="dbg_combo_add_straight_full")],
        [InlineKeyboardButton("six_1 (шесть 1️⃣)",     callback_data="dbg_combo_add_six_1"),
         InlineKeyboardButton("six_6 (шесть 6️⃣)",     callback_data="dbg_combo_add_six_6")],
        [InlineKeyboardButton("ulta_red 🔴",           callback_data="dbg_combo_add_ulta_red"),
         InlineKeyboardButton("ulta_purple 🟣",        callback_data="dbg_combo_add_ulta_purple")],
        [InlineKeyboardButton("special_joker 🃏",      callback_data="dbg_combo_add_special_joker"),
         InlineKeyboardButton("special_thief 🥷",      callback_data="dbg_combo_add_special_thief")],
        [InlineKeyboardButton("special_cross ✝️",      callback_data="dbg_combo_add_special_cross"),
         InlineKeyboardButton("special_freeze ❄️",     callback_data="dbg_combo_add_special_freeze")],
        [InlineKeyboardButton("◀ Назад", callback_data="dbg_back")],
    ]
    uid    = query.from_user.id
    count  = db.get_combo_count(uid)
    lvl    = _combo_level(count)
    await query.edit_message_text(
        f"🎲 *Добавить комбо-лог*\n\n"
        f"Текущий счётчик: *{count}* комбо  ({lvl['badge']} {lvl['name']})\n"
        f"Выбери тип — добавится 1 запись:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_dbg_combo_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_combo_add_<key> — добавляет 1 запись в combo_logs."""
    query     = update.callback_query
    uid       = query.from_user.id
    if not _is_superadmin(uid):
        await query.answer(); return

    combo_key = query.data[len("dbg_combo_add_"):]
    db.log_combo(uid, combo_key)
    count     = db.get_combo_count(uid)
    lvl       = _combo_level(count)
    combo_name = COMBOS.get(combo_key, combo_key)
    await query.answer(f"✅ Добавлено: {combo_name} (всего {count})", show_alert=False)
    # Обновляем счётчик в меню
    await cb_dbg_combo_menu(update, context)


# ── Clear combos ──────────────────────────────────────────────────────────────

async def cb_dbg_clear_combos_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return

    count = db.get_combo_count(query.from_user.id)
    await query.edit_message_text(
        f"🧹 *Сбросить комбо-логи?*\n\n"
        f"Будет удалено *{count}* записей.\n"
        f"Уровень и ачивки, связанные с комбо, останутся (ачивки — отдельная таблица).\n\n"
        f"⚠️ Это *необратимо*.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, удалить всё", callback_data="dbg_clear_combos_do"),
            InlineKeyboardButton("❌ Отмена",          callback_data="dbg_back"),
        ]]),
    )


async def cb_dbg_clear_combos_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id
    if not _is_superadmin(uid):
        await query.answer(); return
    db.clear_user_combo_logs(uid)
    await query.answer("🧹 Комбо-логи очищены", show_alert=True)
    await query.edit_message_text(
        "🛠 *Debug Panel*",
        parse_mode="Markdown",
        reply_markup=_debug_kb(),
    )


# ── Check achievements ────────────────────────────────────────────────────────

async def cb_dbg_check_ach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Немедленно запускает _check_achievements для суперадмина."""
    query = update.callback_query
    await query.answer("⏳ Проверяю…")
    uid   = query.from_user.id
    if not _is_superadmin(uid): return

    newly = await _check_achievements(uid, context)
    if newly:
        result = "✅ Выданы новые ачивки: " + ", ".join(
            ACHIEVEMENTS.get(a, {}).get("name", a) for a in newly
        )
    else:
        result = "ℹ️ Новых ачивок нет."
    await query.edit_message_text(
        f"🛠 *Debug Panel*\n\n{result}" + _debug_snap_info(context),
        parse_mode="Markdown",
        reply_markup=_debug_kb(has_snapshot=bool(context.user_data.get("debug_snapshot"))),
    )


async def cb_dbg_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_superadmin(query.from_user.id): return
    has_snap = bool(context.user_data.get("debug_snapshot"))
    await query.edit_message_text(
        "🛠 *Debug Panel*" + _debug_snap_info(context),
        parse_mode="Markdown",
        reply_markup=_debug_kb(has_snapshot=has_snap),
    )


# ── Snapshot ──────────────────────────────────────────────────────────────────

async def cb_dbg_snap_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_snap_create — сохраняет текущее состояние ачивок и комбо-логов."""
    query = update.callback_query
    uid   = query.from_user.id
    if not _is_superadmin(uid):
        await query.answer(); return

    achievements = db.get_user_achievements(uid)
    combo_keys   = db.get_user_combo_log_keys(uid)

    context.user_data["debug_snapshot"] = {
        "achievements": achievements,
        "combo_logs":   combo_keys,
    }

    await query.answer("📸 Снэпшот создан!", show_alert=True)
    n_ach = len(achievements)
    n_cmb = len(combo_keys)
    await query.edit_message_text(
        f"🛠 *Debug Panel*\n\n"
        f"📸 Снэпшот создан:\n"
        f"  • *{n_ach}* ачивок\n"
        f"  • *{n_cmb}* комбо-логов\n\n"
        f"Теперь делай что нужно — кнопка «🔄 Восстановить» вернёт всё обратно.",
        parse_mode="Markdown",
        reply_markup=_debug_kb(has_snapshot=True),
    )


async def cb_dbg_snap_restore_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_snap_restore_confirm — показывает что именно будет восстановлено."""
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    if not _is_superadmin(uid): return

    snap = context.user_data.get("debug_snapshot")
    if not snap:
        await query.answer("❌ Снэпшот не найден", show_alert=True); return

    n_ach_snap = len(snap.get("achievements", []))
    n_cmb_snap = len(snap.get("combo_logs",   []))
    n_ach_now  = len(db.get_user_achievements(uid))
    n_cmb_now  = db.get_combo_count(uid)

    await query.edit_message_text(
        f"🔄 *Восстановить из снэпшота?*\n\n"
        f"Сейчас:        *{n_ach_now}* ачивок · *{n_cmb_now}* комбо-логов\n"
        f"После отката:  *{n_ach_snap}* ачивок · *{n_cmb_snap}* комбо-логов\n\n"
        f"⚠️ Все изменения сделанные через дебаг-панель будут отменены.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Восстановить", callback_data="dbg_snap_restore_do"),
            InlineKeyboardButton("❌ Отмена",       callback_data="dbg_back"),
        ]]),
    )


async def cb_dbg_snap_restore_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """dbg_snap_restore_do — выполняет восстановление."""
    query = update.callback_query
    uid   = query.from_user.id
    if not _is_superadmin(uid):
        await query.answer(); return

    snap = context.user_data.get("debug_snapshot")
    if not snap:
        await query.answer("❌ Снэпшот не найден", show_alert=True); return

    await query.answer("⏳ Восстанавливаю…")

    db.restore_achievements(uid, snap.get("achievements", []))
    db.restore_combo_logs(uid,   snap.get("combo_logs",   []))

    # Снэпшот сбрасываем — он уже применён
    context.user_data.pop("debug_snapshot", None)

    n_ach = len(snap.get("achievements", []))
    n_cmb = len(snap.get("combo_logs",   []))
    await query.edit_message_text(
        f"🛠 *Debug Panel*\n\n"
        f"✅ *Восстановлено!*\n"
        f"  • {n_ach} ачивок\n"
        f"  • {n_cmb} комбо-логов",
        parse_mode="Markdown",
        reply_markup=_debug_kb(has_snapshot=False),
    )


# ══════════════════════════════════════════════════════════════════════════════

async def cb_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True); return
    await query.edit_message_text("⚙️ *Админ панель*", parse_mode="Markdown", reply_markup=admin_kb())


async def cb_manage_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    users   = db.get_all_users()
    buttons = []
    for u in users:
        is_super = u["user_id"] in config.SUPERADMIN_IDS
        if is_super:
            label, cb_data = f"👑 {u['username']} (суперадмин)", "noop"
        elif u["is_admin"]:
            label, cb_data = f"✅ {u['username']} — снять", f"toggle_admin_{u['user_id']}"
        else:
            label, cb_data = f"👤 {u['username']} — назначить", f"toggle_admin_{u['user_id']}"
        buttons.append([InlineKeyboardButton(label, callback_data=cb_data)])
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])
    await query.edit_message_text(
        "🔑 *Управление админами*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_toggle_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Нет доступа", show_alert=True); return
    target_id = int(query.data.split("_")[-1])
    if target_id in config.SUPERADMIN_IDS:
        await query.answer("Нельзя изменить права суперадмина", show_alert=True); return
    target = db.get_user(target_id)
    if not target: return
    new_status = not bool(target["is_admin"])
    db.set_admin(target_id, new_status)
    await query.answer(f"{target['username']} {'получил права' if new_status else 'лишён прав'}", show_alert=True)
    await cb_manage_admins(update, context)


async def cb_admin_reset_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    await query.edit_message_text(
        "⚠️ *Сброс статистики*\n\nВыберите что именно сбросить:",
        parse_mode="Markdown", reply_markup=reset_menu_kb(),
    )


async def cb_reset_ratings_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    await query.edit_message_text(
        "⚠️ Сбросить *только рейтинги* (очки → 0)?\nИгры и победы останутся.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да", callback_data="confirm_reset_ratings"),
            InlineKeyboardButton("❌ Отмена", callback_data="admin_reset_menu"),
        ]]),
    )


async def cb_reset_games_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    await query.edit_message_text(
        "⚠️ Сбросить *только игры и победы* (games_played, wins → 0)?\nРейтинг останется.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да", callback_data="confirm_reset_games"),
            InlineKeyboardButton("❌ Отмена", callback_data="admin_reset_menu"),
        ]]),
    )


async def cb_reset_combos_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    await query.edit_message_text(
        "⚠️ Удалить *все записи комбинаций* у всех игроков?\nСтатистика кубиков будет стёрта.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да", callback_data="confirm_reset_combos"),
            InlineKeyboardButton("❌ Отмена", callback_data="admin_reset_menu"),
        ]]),
    )


async def cb_confirm_reset_ratings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    db.reset_ratings_only()
    await query.edit_message_text("✅ Рейтинги обнулены.", reply_markup=back_admin_kb())


async def cb_confirm_reset_games(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    db.reset_games_only()
    await query.edit_message_text("✅ Игры и победы обнулены.", reply_markup=back_admin_kb())


async def cb_confirm_reset_combos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    db.reset_combos()
    await query.edit_message_text("✅ Все комбо-логи удалены.", reply_markup=back_admin_kb())


async def cb_reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    await query.edit_message_text(
        "⚠️ *Сбросить ВСЁ?*\nРейтинги, игры, победы — всё будет обнулено. Необратимо.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, сбросить всё", callback_data="confirm_reset"),
            InlineKeyboardButton("❌ Отмена",            callback_data="admin_reset_menu"),
        ]]),
    )


async def cb_confirm_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    db.reset_all_ratings()
    await query.edit_message_text("✅ Вся статистика сброшена.", reply_markup=back_admin_kb())


async def cb_cancel_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    pending = db.get_pending_action(user_id)
    if pending and pending["action"] == "in_game":
        tp = pending["data"].get("turn_player_id")
        if tp and tp != user_id:
            db.clear_pending_action(tp)
    db.clear_pending_action(user_id)
    await query.edit_message_text("⚙️ *Админ панель*", parse_mode="Markdown", reply_markup=admin_kb())


async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ── Slash-command shortcuts ───────────────────────────────────────────────────

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user    = db.get_user(user_id)
    if not user:
        await update.message.reply_text("Нажмите /start для начала.")
        return
    adm   = is_admin(user_id)
    dname = _user_display_name(user_id, user=user)
    await update.message.reply_text(
        f"👋 Главное меню, *{dname}*",
        parse_mode="Markdown",
        reply_markup=main_kb(adm),
    )


async def cmd_ratings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    board        = db.get_leaderboard()
    combo_counts = db.get_all_combo_counts()
    text         = _leaderboard_text(board, combo_counts)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=back_main_kb())


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    user  = db.get_user(uid)
    if not user:
        await update.message.reply_text("Нажмите /start для начала.")
        return
    board  = db.get_leaderboard()
    stats  = db.get_player_stats(uid)
    avg    = db.get_avg_stats(uid)
    streak = db.get_player_streak(uid)

    notice = _calibration_notice(user)
    if notice:
        db.mark_calibration_notified(uid)

    text = _build_profile_text(user, uid, board, stats, avg, streak)
    if notice:
        text = notice + text
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_profile_kb())


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ У вас нет прав администратора.")
        return
    await update.message.reply_text("⚙️ *Админ панель*", parse_mode="Markdown", reply_markup=admin_kb())


async def cmd_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только администраторы могут начинать игру.")
        return
    all_users = db.get_all_users()
    if len(all_users) < 2:
        await update.message.reply_text("❌ В системе меньше 2 игроков.", reply_markup=back_admin_kb())
        return
    db.set_pending_action(user_id, "selecting_players", {"selected": []})
    await update.message.reply_text(
        "🎮 *Запись игры*\n\nВыберите участников (2–6 человек):",
        parse_mode="Markdown",
        reply_markup=player_selection_kb(all_users, []),
    )


# ── /help ─────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pages = [
        # ── Страница 1: Основы ──────────────────────────────────────────────────
        (
            "🎲 *ПРАВИЛА ИГРЫ — Основы*\n\n"
            "*Цель:* первым набрать 20 000 или 25 000 очков.\n\n"
            "*Ход:*\n"
            "1. Бросаешь все кубики\n"
            "2. Выбираешь минимум одну комбинацию\n"
            "3. Бросаешь остаток или забираешь очки\n"
            "💥 *Бюст* — нет комбинации → все очки за ход сгорают\n"
            "🔥 *Hot dice* — все кубики в комбинации → бросаешь снова с полным набором\n\n"
            "*Кубики:* 5 нейтральных + 1 фракционный\n"
            "При 5 000 очков добавляется 2-й фракционный кубик\n\n"
            "*Комбинации:*\n"
            "1️⃣ одиночная — 100 очков\n"
            "5️⃣ одиночная — 50 очков\n"
            "N-N-N тройка → N×100 *(три 1️⃣ = 1000)*\n"
            "Четыре одинак. → ×2 от тройки\n"
            "Пять одинак. → ×3 от тройки\n"
            "Шесть одинак. → ×4 от тройки\n"
            "Стрит 1-2-3-4-5 → 500\n"
            "Стрит 2-3-4-5-6 → 750\n"
            "Стрит 1-2-3-4-5-6 → 1000\n\n"
            "_1 и 5 не входят в большую комбинацию — засчитываются отдельно_"
        ),
        # ── Страница 2: Фракции ─────────────────────────────────────────────────
        (
            "🎭 *ПРАВИЛА ИГРЫ — Фракции*\n\n"
            "🔴 *Красный*\n"
            "Красная 5️⃣ = 100 очков (вместо 50)\n"
            "Ульта: +250 к каждому красному комбо\n\n"
            "🟡 *Жёлтый*\n"
            "Ульта от 1-2-3 (≥2 жёлтых) или 1-2-3-4-5-6 (≥1 жёлтый в каждой половине)\n"
            "Ульта: +500 к жёлтым комбо\n\n"
            "🔵 *Синий*\n"
            "Синяя 2️⃣ спасает от бюста — очки сохраняются, бросаешь снова\n"
            "Ульта: 3 бесплатных переброска за игру\n\n"
            "🟢 *Зелёный*\n"
            "Зелёная 3️⃣ в броске — можно перебросить бесплатно\n"
            "До ульты джокер = только пара зелёных 3️⃣\n"
            "После ульты джокер = любые парные зелёные значения (2-2, 4-4…)\n\n"
            "🟣 *Фиолетовый*\n"
            "Комбо 2-4-6 (≥1 фиолетового) → доп. бросок нейтральными\n"
            "Ульта: +500 к комбо 2-4-6 *плюс* доп. бросок\n\n"
            "⚪ *Белый (нежить)*\n"
            "Белая 1️⃣ → жетон (макс. 2 одновременно)\n"
            "1 жетон → вернуть последний кубик в след. бросок (случайная грань)\n"
            "2 жетона → вернуть его с точным значением\n"
            "Ульта: украсть кубик у противника навсегда"
        ),
        # ── Страница 3: Ульта ───────────────────────────────────────────────────
        (
            "✨ *ПРАВИЛА ИГРЫ — Ульта*\n\n"
            "Условие зависит от фракции (обычно 2 одинаковых фракц. кубика).\n"
            "Затем бросаешь *проверочный кубик:*\n\n"
            "🔴 Красный: успех 5️⃣ · перебросить 6️⃣\n"
            "🟡 Жёлтый: успех 1️⃣ или 6️⃣\n"
            "🔵 Синий: успех 2️⃣ или 3️⃣\n"
            "🟢 Зелёный: успех 3️⃣ или 4️⃣\n"
            "🟣 Фиолетовый: успех 2️⃣ или 5️⃣\n"
            "⚪ Белый: успех 4️⃣ или 5️⃣\n\n"
            "Значение на 1 выше победного = *переброс* (как у красного: 5→успех, 6→перебрось)\n\n"
            "Бафф активируется *один раз* и действует до конца игры.\n\n"
            "*Камбек-механика:*\n"
            "Отстаёшь от любого соперника на 10 000+ очков?\n"
            "Получаешь спец-кубик: 4 грани = 1️⃣, 6 граней = 5️⃣\n"
            "Кубик действует пока отставание не сократится."
        ),
        # ── Страница 4: Спец-кубики ─────────────────────────────────────────────
        (
            "⚙️ *ПРАВИЛА ИГРЫ — Спец-кубики*\n\n"
            "Спец. грань заменяет одно значение кубика.\n"
            "При выпадении — сам выбираешь: способность или цифра.\n\n"
            "🃏 *Джокер* (×2: грань 6️⃣ / 5️⃣)\n"
            "Принимает любое нужное значение\n\n"
            "🥷 *Вор* (грань 6️⃣)\n"
            "При завершении хода бросаешь вора и крадёшь у каждого:\n"
            "2 игр: 4️⃣=200 · 5️⃣=250 · 6️⃣=300\n"
            "3 игр: 4️⃣=150 · 5️⃣=200 · 6️⃣=250\n"
            "4+ игр: 4️⃣=100 · 5️⃣=150 · 6️⃣=200\n\n"
            "🔄 *Рерол* (×2: грань 6️⃣ / 5️⃣)\n"
            "Перебрасываешь любые кубики; рерол-кубик тоже перебрасывается\n\n"
            "⚔️ *Разбой* (грань 6️⃣)\n"
            "При завершении хода — каждый бросает один кубик.\n"
            "Победишь (твой > чужой) → ты +бонус, он −бонус\n"
            "2 игрока: ±500 · 3: ±400 · 4+: ±300\n\n"
            "✖️ *Утроение* (грань 6️⃣)\n"
            "Утраивает комбинацию, вора или разбой при совместном выводе\n\n"
            "✝️ *Крест* (грань 6️⃣)\n"
            "Даёт +1 кубик тебе на следующий ход\n\n"
            "❄️ *Заморозка* (×2: грань 6️⃣ / 1️⃣)\n"
            "Даёт −1 кубик всем остальным на их следующий ход\n\n"
            "💪 *Рубен* (несколько граней)\n"
            "5️⃣=🔴бонус · 4️⃣=🟡бонус · 3️⃣=🟢бонус · 2️⃣=🔵бонус · 1️⃣/6️⃣=нет эффекта\n"
            "Даёт одиночный бонус цвета, но не собирает комбинации"
        ),
        # ── Страница 5: Магазин ─────────────────────────────────────────────────
        (
            "🛒 *ПРАВИЛА ИГРЫ — Магазин*\n\n"
            "Открывается при пересечении рубежей в ходе игры.\n"
            "Количество рубежей зависит от числа игроков: больше игроков — меньше рубежей.\n\n"
            "*До 20 000:*\n"
            "3 игрока: 2000 · 5000 · 10000 · 12000 · 15000\n"
            "4 игрока: 2000 · 5000 · 8000 · 15000\n\n"
            "*До 25 000:*\n"
            "3 игрока: 2000 · 5000 · 8000 · 12000 · 15000\n"
            "4 игрока: 2000 · 5000 · 8000 · 15000\n\n"
            "*В магазине:*\n"
            "Бросаешь фракционный кубик:\n"
            "Без жетона: 1–3 = 1 вариант · 4–6 = 2 варианта\n"
            "С жетоном:  1–2 = 1 · 3–4 = 2 · 5–6 = 3 варианта\n\n"
            "*Как получить жетон магазина:*\n"
            "🎟️ Обменяй 2 нейтральных кубика → жетон\n"
            "🎟️ Или 1 спец-кубик из резерва → жетон\n\n"
            "Выбранный кубик добавляется в резерв и бросается каждый ход."
        ),
        # ── Страница 6: Бот и команды ───────────────────────────────────────────
        (
            "📱 *КОМАНДЫ БОТА*\n\n"
            "/start — перезапустить бот\n"
            "/menu — главное меню\n"
            "/ratings — таблица рейтинга\n"
            "/profile — мой профиль и статистика\n"
            "/vs @username — сравнение с другим игроком\n"
            "/solo — режим игры против бота\n"
            "/game — начать мультиплеер-игру *(только для админов)*\n"
            "/admin — панель администратора\n"
            "/cancel — отменить текущее действие\n"
            "/help — эти правила\n\n"
            "*Мультиплеер — кнопки во время хода:*\n"
            "+50 … +1000 — добавить очки\n"
            "✅ Конец хода — зафиксировать и передать ход\n"
            "💥 Ничего — ход провалился, очки за ход сгорают\n"
            "📝 Комбо — записать комбинацию в статистику\n"
            "📋 Обновить — обновить карточку хода\n"
            "⚙️ Правка — ручная корректировка (только админ)\n\n"
            "*Рейтинг (мультиплеер):*\n"
            "🥇 +30 · 🥈 +15 · 🥉 +5 · последнее место (4+ игр.) −15\n\n"
            "*Соло-рейтинг:*\n"
            "Победа +30 · Поражение −15"
        ),
    ]
    for text in pages:
        await update.message.reply_text(text, parse_mode="Markdown")


# ── Post-init: регистрация меню команд ────────────────────────────────────────

async def _post_init(application: Application) -> None:
    commands = [
        BotCommand("start",   "🏠 Начало / перезапустить бот"),
        BotCommand("menu",    "📋 Главное меню"),
        BotCommand("ratings", "📊 Рейтинговая таблица"),
        BotCommand("profile", "👤 Мой профиль"),
        BotCommand("admin",   "⚙️ Админ панель"),
        BotCommand("game",    "🎮 Начать новую игру"),
        BotCommand("cancel",  "❌ Отменить текущее действие"),
        BotCommand("vs",      "⚔️ Сравнить себя с игроком"),
        BotCommand("solo",    "🎯 Сингл-плеер vs бот"),
        BotCommand("help",    "❓ Справка по командам и правилам"),
    ]
    await application.bot.set_my_commands(commands)


# ── Application factory ───────────────────────────────────────────────────────

def create_application() -> Application:
    app       = Application.builder().token(config.BOT_TOKEN).post_init(_post_init).job_queue(None).build()
    text_only = filters.TEXT & ~filters.COMMAND

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CommandHandler("menu",    cmd_menu))
    app.add_handler(CommandHandler("ratings", cmd_ratings))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("admin",   cmd_admin))
    app.add_handler(CommandHandler("game",    cmd_game))
    app.add_handler(CommandHandler("vs",      cmd_vs))
    app.add_handler(CommandHandler("solo",    cmd_solo))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("debug",   cmd_debug))
    app.add_handler(MessageHandler(text_only, handle_message))

    for pattern, cb in [
        # Debug panel (superadmin)
        ("^dbg_back$",                cb_dbg_back),
        ("^dbg_snap_create$",         cb_dbg_snap_create),
        ("^dbg_snap_restore_confirm$",cb_dbg_snap_restore_confirm),
        ("^dbg_snap_restore_do$",     cb_dbg_snap_restore_do),
        ("^dbg_grant_list_",          cb_dbg_grant_list),
        ("^dbg_grant_",               cb_dbg_grant),
        ("^dbg_revoke_list$",         cb_dbg_revoke_list),
        ("^dbg_revoke_",              cb_dbg_revoke),
        ("^dbg_combo_menu$",          cb_dbg_combo_menu),
        ("^dbg_combo_add_",           cb_dbg_combo_add),
        ("^dbg_clear_combos_confirm$",cb_dbg_clear_combos_confirm),
        ("^dbg_clear_combos_do$",     cb_dbg_clear_combos_do),
        ("^dbg_check_ach$",           cb_dbg_check_ach),
        # Coin management — порядок важен (специфичные перед общими)
        ("^dbg_coins_list$",          cb_dbg_coins_list),
        ("^dbg_coins_user_",          cb_dbg_coins_user),
        ("^dbg_coins_adj_",           cb_dbg_coins_adjust),
        ("^dbg_coins_zero_",          cb_dbg_coins_zero),
        ("^dbg_back_to_panel$",       cb_dbg_back_to_panel),
        # Inventory management
        ("^dbg_inv_list$",            cb_dbg_inv_list),
        ("^dbg_inv_user_",            cb_dbg_inv_user),
        ("^dbg_inv_rm_",              cb_dbg_inv_remove),
        # Shop admin — порядок: длиннее перед короче, чтобы не было пересечений
        ("^dbg_shop_menu$",           cb_dbg_shop_menu),
        ("^dbg_shop_add_pick$",       cb_dbg_shop_add_pick),
        ("^dbg_shop_setrar_",         cb_dbg_shop_setrar),
        ("^dbg_shop_toggle_",         cb_dbg_shop_toggle),
        ("^dbg_shop_deldo_",          cb_dbg_shop_deldo),
        ("^dbg_shop_delete_",         cb_dbg_shop_delete),
        ("^dbg_shop_edit_",           cb_dbg_shop_edit),
        ("^dbg_shop_list_",           cb_dbg_shop_list),
        ("^dbg_shop_item_",           cb_dbg_shop_item),
        ("^dbg_shop_add_",            cb_dbg_shop_add),
        # Меню
        ("^main_menu$",           cb_main_menu),
        ("^show_ratings$",        cb_show_ratings),
        ("^show_profile$",        cb_show_profile),
        ("^show_combos$",         cb_show_combo_stats),
        ("^show_combo_levels$",   cb_show_combo_levels),
        ("^show_achievements$",   cb_show_achievements),
        ("^show_ext_stats$",      cb_show_extended_stats),
        ("^show_history$",        cb_show_history),
        ("^compare_start$",       cb_compare_start),
        ("^show_global_combos$",  cb_show_global_combos),
        # Переводы монет
        ("^transfer_start$",      cb_transfer_start),
        ("^transfer_to_",         cb_transfer_to),
        ("^transfer_amt_",        cb_transfer_amt),
        ("^transfer_ok_",         cb_transfer_ok),
        ("^transfer_search$",     cb_transfer_search),
        ("^transfer_custom_",     cb_transfer_custom),
        ("^transfer_no_coins$",   cb_transfer_no_coins),
        # Кросс-бот обменник
        ("^exchange_menu$",       cb_exchange_menu),
        ("^exchange_start_cube$", cb_exchange_start_cube),
        ("^exchange_confirm_",    cb_exchange_confirm),
        ("^exchange_claim_f2c$",  cb_exchange_claim_f2c),
        ("^exchange_pending$",    cb_exchange_pending),
        ("^exchange_cancel_",     cb_exchange_cancel),
        # Combo customization (must be before clog_ etc.)
        ("^combo_customize$",     cb_combo_customize),
        ("^combo_locked_",        cb_combo_locked),
        ("^combo_edit_",          cb_combo_edit_slot),
        ("^combo_set_",           cb_combo_set_value),
        # Зал Богатства
        ("^hall_menu$",            cb_hall_menu),
        ("^hall_plaques$",         cb_hall_plaques),
        ("^hall_records$",         cb_hall_records),
        ("^hall_shop$",            cb_hall_shop),
        ("^hall_buytier_",         cb_hall_buy_tier),
        ("^hall_buy$",             cb_hall_buy),
        # VIP Зал — порядок: длинные паттерны раньше коротких
        ("^vip_hall$",             cb_vip_hall),
        ("^vip_enter$",            cb_vip_enter),
        ("^vip_menu$",             cb_vip_menu),
        ("^vip_roulette$",         cb_vip_roulette),
        ("^vip_rspin_",            cb_vip_roulette_spin),
        ("^vip_lobby$",            cb_vip_lobby),
        ("^vip_create$",           cb_vip_create),
        ("^vip_cfee_",             cb_vip_create_fee),
        ("^vip_view_",             cb_vip_view),
        ("^vip_join_",             cb_vip_join),
        ("^vip_leave_",            cb_vip_leave),
        ("^vip_start_",            cb_vip_start),
        # Акции казино — длинные паттерны раньше коротких
        ("^casino_shares$",        cb_casino_shares),
        ("^shares_buy_",           cb_shares_buy),
        ("^shares_sell_",          cb_shares_sell),
        ("^shares_claim$",         cb_shares_claim),
        ("^shares_cant$",          cb_shares_cant),
        # Casino — порядок: длинные паттерны раньше коротких
        ("^casino_menu$",          cb_casino_menu),
        ("^casino_roulette$",      cb_casino_roulette),
        ("^casino_slotspin_",      cb_casino_slots_spin),   # перед casino_spin_
        ("^casino_slots$",         cb_casino_slots),
        ("^casino_spin_",          cb_casino_spin),
        ("^casino_bonusbuy_",      cb_casino_bonus_buy),    # перед casino_bonusspin
        ("^casino_bonusspin_",     cb_casino_bonus_spin),
        ("^casino_bj$",            cb_casino_bj),
        ("^casino_bjbet_",         cb_casino_bj_bet),
        ("^casino_bjhit$",         cb_casino_bj_hit),
        ("^casino_bjstand$",       cb_casino_bj_stand),
        ("^casino_bjdouble$",      cb_casino_bj_double),
        ("^casino_global$",        cb_casino_global),
        ("^casino_globalbet_",     cb_casino_global_bet),
        ("^casino_no_coins$",      cb_casino_no_coins),
        # Shop — порядок важен: более конкретные паттерны перед более общими
        ("^shop_menu$",           cb_shop_menu),
        ("^shop_inventory$",      cb_shop_inventory),
        ("^shop_browse_",         cb_shop_browse),
        ("^shop_item_",           cb_shop_item),
        ("^shop_buy_",            cb_shop_buy),
        ("^shop_no_money$",       cb_shop_no_money),
        ("^inv_equip_",           cb_inv_equip),
        ("^inv_set_",             cb_inv_set),
        ("^inv_unequip_",         cb_inv_unequip),
        # Solo
        ("^solo_menu$",           cb_solo_menu),
        ("^solo_start_",          cb_solo_start),
        ("^solo_target_",         cb_solo_target),
        ("^solo_leaderboard$",    cb_solo_leaderboard),
        ("^solo_history$",        cb_solo_history),
        ("^slup_",                 cb_solo_lineup),
        ("^srp_",                  cb_solo_reroll_pick),
        ("^solo_quit$",           cb_solo_quit),
        ("^solo_quit_confirm$",   cb_solo_quit_confirm),
        ("^solo_quit_cancel$",    cb_solo_quit_cancel),
        ("^sin_",                 cb_solo_input),
        ("^ssel_",                cb_solo_select),
        ("^ssub_",                cb_solo_submit),
        ("^sgreen_",              cb_solo_green_reroll),
        ("^stoken$",              cb_solo_token),
        ("^spex_",                cb_solo_purple_extra),
        ("^sbrol$",               cb_solo_blue_reroll),
        ("^sres_",                cb_solo_resurrect),
        ("^sspec_",               cb_solo_spec_toggle),
        ("^spick_",               cb_solo_shop_pick),
        ("^sshoptoken_",          cb_solo_shop_token),
        ("^admin_panel$",         cb_admin_panel),
        # Запись игры
        ("^admin_record_game$",   cb_admin_record_game),
        ("^sel_",                 cb_select_player),
        ("^start_scoring$",       cb_start_scoring),
        ("^gmode_",               cb_select_mode),
        # Игровые кнопки
        ("^gadd_",                cb_game_add),
        ("^gend_",                cb_game_end_turn),
        ("^glose_",               cb_game_lose_turn),
        ("^gtaunt_",              cb_game_taunt),
        ("^gfinish_yes_",         cb_finish_confirm),
        ("^gfinish_no_",          cb_finish_cancel),
        ("^gfinish_",             cb_game_finish),
        ("^gfx_menu_",            cb_gfx_menu),
        ("^gfx_back_",            cb_gfx_back),
        ("^grecord_",             cb_record_game),
        ("^gdiscard_",            cb_discard_game),
        ("^gresult_edit_",        cb_result_edit),
        ("^grescore_",            cb_result_score),
        ("^gresdelta_",           cb_result_delta),
        ("^gresback_",            cb_result_back),
        ("^admin_recover_game$",  cb_admin_recover_game),
        # Запись комбо
        ("^ccat_",                cb_combo_cat),
        ("^csub_",                cb_combo_sub),
        ("^clog_",                cb_combo_log),
        ("^cclose_",              cb_combo_close),
        ("^cundo_",               cb_combo_undo),
        ("^gundo_",               cb_game_undo),
        ("^gfx_cross_",           cb_effect_cross),
        ("^gfx_freeze_",          cb_effect_freeze),
        ("^gfx_thief_",           cb_effect_thief),
        ("^gthiefx3_",            cb_thief_triple),
        ("^gthief_",              cb_apply_thief),
        ("^gfx_robbery_",         cb_effect_robbery),
        ("^grobbery_x3_",         cb_robbery_triple),
        ("^grobbery_",            cb_apply_robbery),
        # Игровые утилиты
        ("^grefresh_",            cb_game_refresh),
        ("^gadmedit_",            cb_game_admin_edit),
        ("^gadmdelta_",           cb_game_admin_delta),
        ("^gadmback_",            cb_game_admin_back),
        ("^gadm_",                cb_game_admin_panel),
        # Отмена эффектов / жетон
        ("^gthiefcancel_",        cb_thief_cancel),
        ("^grobcancel_",          cb_robbery_cancel),
        ("^gtoken_",              cb_game_white_token),
        ("^gusetoken_",           cb_game_use_token),
        # Настройка игры (цвета, первый игрок)
        ("^gcolrnd_",             cb_assign_color_random),
        ("^gcol_",                cb_assign_color),
        ("^gfirstshow_",          cb_first_player_show),
        ("^gfirstrnd_",           cb_first_player_random),
        ("^gfirst_",              cb_first_player_pick),
        # Управление
        ("^admin_manage_admins$", cb_manage_admins),
        ("^toggle_admin_",        cb_toggle_admin),
        ("^admin_edit_rating$",   cb_admin_edit_rating),
        ("^admin_delete_player$", cb_admin_delete_player),
        ("^confirm_delete$",      cb_confirm_delete),
        ("^admin_reset_menu$",      cb_admin_reset_menu),
        ("^reset_ratings_only$",    cb_reset_ratings_only),
        ("^reset_games_only$",      cb_reset_games_only),
        ("^reset_combos_only$",     cb_reset_combos_only),
        ("^confirm_reset_ratings$", cb_confirm_reset_ratings),
        ("^confirm_reset_games$",   cb_confirm_reset_games),
        ("^confirm_reset_combos$",  cb_confirm_reset_combos),
        ("^admin_reset_confirm$",   cb_reset_confirm),
        ("^confirm_reset$",         cb_confirm_reset),
        ("^cancel_to_admin$",     cb_cancel_to_admin),
        ("^noop$",                cb_noop),
    ]:
        app.add_handler(CallbackQueryHandler(cb, pattern=pattern))

    # ── Job Queue: глобальная рулетка (только в polling-режиме) ─────────────
    # В serverless (Vercel) job_queue == None — глобальная рулетка
    # запускается лениво при открытии экрана (см. cb_casino_global).
    if app.job_queue is not None:
        app.job_queue.run_repeating(
            _global_roulette_tick,
            interval=60,
            first=60,
            name="global_roulette",
        )

    return app


def main():
    create_application().run_polling()


if __name__ == "__main__":
    main()