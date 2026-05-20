# PROJECT_CONTEXT.md
# Кубасы — полный контекстный справочник для Claude

Этот файл — быстрый старт. Читай его вместо перечитывания bot.py.
Актуален на: май 2026.

---

## ЧТО ЭТО

**Кубасы** — физическая настольная игра (жанр: push-your-luck dice, как Farkle/Qwixx) в процессе коммерциализации. Бот — это:
1. **Трекер очков** для реальных партий за столом (мультиплеер — очки вводятся вручную)
2. **Эталонный скоринг** для соло-режима (бот бросает виртуальные кубики)
3. **Социальный хаб** — рейтинг, инвентарь, казино, достижения — удерживают между физическими сессиями

Физическая игра: прототип. Аудитория сейчас: ~10–30 человек. Бюджет на коммерциализацию: $10k.

---

## СТЕК

| Компонент | Технология |
|---|---|
| Язык | Python 3.11+ |
| Telegram SDK | python-telegram-bot v22 (async) |
| БД | Supabase (PostgreSQL) через supabase-py v2 (синхронный REST) |
| Хостинг | Serverless-совместимый (нет persistent state) |
| Файлы | `bot.py` ~12 500 строк, `database.py` ~1 500 строк, `scoring.py` 412 строк, `config.py` 10 строк |

Запуск: `.\.venv\Scripts\python.exe bot.py`
Тесты скоринга: `.\.venv\Scripts\python.exe scoring.py`

---

## МЕХАНИКА ИГРЫ

### Базовый ход
1. Игрок бросает **6 кубиков** (5 нейтральных + 1 фракционный)
2. Выбирает минимум одну скоринговую комбинацию
3. Решает: **банк** (зафиксировать очки) или **ещё бросок** (push your luck)
4. **Бюст** — нет комбинаций в броске → все очки хода сгорают
5. **Hot dice** — все 6 кубиков в комбинации → бросаешь снова все 6

### Цель
- **20 000** или **25 000 очков** (выбор при старте)
- При достижении цели — **финальный круг**: все остальные делают по одному ходу

### Фракции (цвета)
🔴🟡🔵🟢🟣⚪ — у каждой бонусы и **ульта** (спец-комбинация с усиленным эффектом).
Второй фракционный кубик открывается при `score >= 5000` (`FACTION_2ND_THRESHOLD`).
При отставании `>= 10000` от лидера — **камбек-кубик** (грани: 4×1️⃣, 2×5️⃣).

### Спец-кубики (компоненты коробки)
`joker_6`, `joker_5` 🃏, `thief` 🥷, `reroll_6`, `reroll_5` 🔄, `robbery` ⚔️, `ruben` 💪, `tripling` ✖️, `cross` ✝️, `freeze_6`, `freeze_1` ❄️

### Экономика монет
```
COIN_BASE_BY_PLACE   = {1:50, 2:30, 3:20, 4:15, 5:10, 6:10}
COIN_RESULT_DIVISOR  = 1000    # +1 монета за каждые 1000 очков
COIN_PERFECT_BONUS   = 50      # если score >= target
SOLO_COIN_MULTIPLIER = 0.5     # антифарм в соло
```

---

## АРХИТЕКТУРНЫЕ ПАТТЕРНЫ

### `adb(func, *args)` — ОБЯЗАТЕЛЬНО для всех DB вызовов
```python
result = await adb(db.get_user, user_id)
```
supabase-py синхронный → блокирует event loop. `adb` = `asyncio.to_thread`. Без него бот зависает.

### `fire_and_forget(coro)` — для некритичных фоновых операций
```python
fire_and_forget(adb(db.log_combo, user_id, combo_key))
fire_and_forget(adb(db.add_to_dividend_pool, amount))
```
Используй для: логирования, уведомлений, пополнения пулов. НЕ используй когда нужен результат.

### Параллельные отправки сообщениям нескольким игрокам
```python
tasks = [asyncio.create_task(_safe_send(context, uid, text)) for uid in player_ids]
await asyncio.gather(*tasks, return_exceptions=True)
```

### State machine через `pending_actions`

| action | user_id = | Содержит |
|---|---|---|
| `in_game` | admin_id | Полное состояние игры: players, current_idx, target, turn_score, turn_history, turn_crosses, turn_freezes, next_effects, board_chat_id, board_message_id, final_round_trigger_idx |
| `confirm_game` | admin_id | Игра завершена, ждём `grecord_` |
| `my_turn` | player_uid | `{"game_admin_id": admin_id}` — указатель |
| `selecting_players` | admin_id | Создание новой игры |
| `solo_game` | user_id | Полное состояние соло |
| `combo_edit` | user_id | Ждём текстовый ввод для слота |
| `hall_plaque_msg` | user_id | Ждём подпись для таблички |
| `bj_solo` | user_id | Состояние блэкджека |

### Регистрация хэндлеров — порядок КРИТИЧЕН
Более конкретные паттерны ПЕРЕД общими:
```python
("^casino_slotspin_", cb_casino_slots_spin),  # ПЕРЕД casino_spin_
("^shares_buy_",      cb_shares_buy),          # ПЕРЕД потенциально похожим
```

### Кэш
- `_cache_user` — TTL 5 сек, инвалидируется при любом `update`/`add_coins`/`spend_coins`
- `_cache_pending` — TTL 600 сек, **write-through** (обновляется синхронно до HTTP)

### `_admin_id_from(cb_data)` — роутинг мультиплеера
Все мультиплеер-callback'и: `prefix_<param>_<admin_id>`. Парсит admin_id из конца строки.

---

## СХЕМА БД

| Таблица | PK | Назначение |
|---|---|---|
| `users` | `user_id` | Профиль, рейтинг, монеты, инвентарь-слоты |
| `games` | `game_id` | Шапка записанной игры |
| `game_results` | (`game_id`, `user_id`) | Результаты игроков |
| `pending_actions` | `user_id` | State machine (action + data jsonb) |
| `combo_logs` | `id` | История комбо для уровней |
| `user_achievements` | (`user_id`, `achievement_id`) | Заработанные ачивки |
| `game_drafts` | `admin_id` | Backup до подтверждения записи |
| `solo_history` | `id` | История соло-партий |
| `shop_items` | `item_id` | Косметика (stock=NULL безлим, stock=0 скрыт) |
| `user_items` | `id` | Купленные предметы |
| `global_roulette_state` | `id=1` | Котёл глобальной рулетки |
| `global_roulette_bets` | `id` | Ставки текущего раунда |
| `vip_lobbies` | `id` | Покер-лобби (waiting/playing/finished) |
| `vip_lobby_players` | (`lobby_id`, `user_id`) | Руки игроков |
| `hall_plaques` | `id` | Таблички Зала Богатства |
| `casino_economy` | `id=1` | `shares_sold`, `dividend_pool` |
| `casino_shares` | `user_id` | `shares`, `dividends_claimed`, `last_claim_at` |
| `cross_transfers` | `id` | Обменник Кубик↔FUT (pending/claimed/expired) |

### Ключевые поля `users`
`rating`, `games_played`, `wins`, `is_admin`, `is_calibrated`, `calibration_perf_sum`, `calibration_games_count`, `coins`, `max_balance`, `max_casino_win`, `solo_rating`, `solo_games_played`, `solo_wins`, `combo_prefix1`, `combo_prefix2`, `combo_suffix1`, `combo_suffix2`, `combo_title`, `combo_banner`, `combo_about`, `combo_divider`, `combo_color_frame`, `combo_bank_fx`, `combo_win_message`

---

## ВСЕ РЕАЛИЗОВАННЫЕ ФИЧИ

### Мультиплеер
- 2–6 игроков, цели 20000/25000
- Назначение цветов (ручное / рандом), первого игрока
- Доска = закрепленное сообщение (редактируется каждый ход)
- DM каждому игроку с кнопками хода
- Спец-эффекты: ✝️ Кресты, ❄️ Заморозки, 🥷 Вор, ⚔️ Разбой, ✖️ Утроение
- Финальный круг, undo хода, правка результатов
- Восстановление прерванной игры (`game_drafts`)
- Уведомления магазинных рубежей всем игрокам

### Соло режим
- Виртуальные кубики, AI-бот с эвристикой банка
- Магазин по рубежам, реролы, камбек-кубик, воскрешение кубиков
- Ульта-комбинации с супер-играми
- Рейтинг: Победа +30 / Поражение −15

### Казино
| Игра | Ключевые детали |
|---|---|
| 🎡 Рулетка | 9 секторов, ставки 50–2500, шанс предмета 2% |
| 🎰 Слоты | 3 барабана + бонус-игра 3 уровней (Wild🌟 Scatter🔥) |
| 🃏 Блэкджек | Стандарт: hit/stand/double, дилер тянет до 17 |
| 🌍 Глобальная рулетка | Общий котёл, lazy-spin раз в минуту, 6 исходов |

Все проигрыши в казино → **5% в дивидендный пул акций**.

### ВИП Зал (доступ: баланс ≥ 100 000, вход 10 000)
- ВИП Рулетка: ставки 10k–1M, множители до ×100
- ВИП Покер: 5-карточный, до 6 игроков, победитель забирает котёл

### Магазин
- Слоты: prefix, suffix, title, banner, divider, color_frame, bank_fx, win_message
- Редкости: common / rare / epic / legendary
- stock=NULL (безлим), stock=N (ограниченный тираж), hall_only=true (эксклюзив)

### Комбо-система
20 уровней (0–19), порог = count в `combo_logs`.
Критические пороги разблокировки слотов:
- Lvl 4 (45) → prefix1
- Lvl 9 (600) → suffix1
- Lvl 10 (900) → title
- Lvl 12 (1800) → prefix2
- Lvl 13 (2500) → banner
- Lvl 15 (4800) → suffix2 + about
- Lvl 18 (9000) → divider

### Зал Богатства
- Таблички: bronze 100k / silver 500k / gold 2M / diamond 10M монет
- Рекорды: max_balance, max_casino_win
- Hall-only эксклюзивы в магазине

### Акции казино
```
SHARE_PRICE_BUY  = 5 000 000
SHARE_PRICE_SELL = 4 500 000
TOTAL_SHARES     = 10 000
MAX_SHARES_PER_PLAYER = 100
```
Дивиденды: `owed = pool * shares / 10000 - dividends_claimed`, кулдаун 60 мин.
5% от каждого проигрыша → `dividend_pool` (никогда не убывает).

### Достижения (~50 штук)
Категории: games, wins, streaks, colors (×6 фракций ×3 уровня), combos, special, hidden, legendary.
Скрытые: night_owl, early_bird, weekend_warrior, lucky_seven (7777 очков), underdog, no_first_move, rainbow (все 6 цветов), double_calib.
Легендарные: all_colors_25, grandmaster (lvl19 + 250 побед + 500 игр).

### Переводы монет
Между игроками. Суммы: 100 / 500 / 1k / 5k / 10k / 50k / 100k / 500k / 1M.

### Кросс-бот обменник
Кубик ↔ FUT-монета. `CROSS_RATE = 3` (1 кубик = 3 FUT), `CROSS_FEE = 10%` (сжигается).
Статусы: pending → claimed / expired.

### Калибровка
Первые 8 игр нового игрока без изменения рейтинга.
Накапливается `calibration_perf_sum + calibration_games_count` для "вердикта" после калибровки.

### Дебаг-панель `/debug`
Только `SUPERADMIN_IDS`. Grant/revoke ачивок, add/clear комбо, shop CRUD, снэпшот состояния.
Снэпшот в `context.user_data["debug_snapshot"]` — теряется при рестарте.

---

## КЛЮЧЕВЫЕ КОНСТАНТЫ

### Рейтинг (`_RATING_RANGES`)
Линейная интерполяция по `score/target`. Диапазоны `[min%, max%]` изменения рейтинга по местам:
- 2 игр.: [+2…+35] / [−22…−2]
- 4 игр.: [+8…+35] / [+2…+18] / [−14…+2] / [−22…−10]
- 6 игр.: [+8…+35] / [+2…+15] / [−2…+10] / [−10…+2] / [−18…−5] / [−22…−12]

### Магазинные рубежи (`SHOP_THRESHOLDS`)
Пример (target=20000, 4 игрока): [2000, 5000, 8000, 15000].
При пересечении рубежа — уведомления всем + пополнение магазина.

### Рулетка секторы
Основная: Пусто 43%, Слив(−2) 8%, Возврат 10%, ×2 20%, ×3 10%, ×5 4%, ×10 2%, ×25 1%, Предмет 2%.
ВИП: Пусто 45%, Слив 10%, Возврат 8%, ×2 15%, ×5 8%, ×10 6%, ×25 4%, ×50 2%, ×100 2%.

### Глобальная рулетка исходы
burn 35%, jackpot 20%, split 20%, x2 15%, carryover 7%, x5 3%.

---

## МИГРАЦИИ БД

Все в `migrations/`. Применять в Supabase Dashboard → SQL Editor.
| Файл | Что добавляет |
|---|---|
| 001–005 | Базовые таблицы (users, games, shop, combo, vip) |
| 006_shop_stock.sql | Колонка `stock` в shop_items |
| 007_premium_items.sql | 63 предмета 10k–1M |
| 008_hall_of_wealth.sql | max_balance, max_casino_win, hall_plaques, hall_only |
| 009_casino_shares.sql | casino_economy, casino_shares |

---

## КОММЕРЧЕСКИЙ СТАТУС

- Физическая игра: **прототип** (не издана)
- Текущая аудитория: ~10–30 человек, узкий круг
- Бюджет на запуск: **$10 000**
- Рынок: не Россия (считать в долларах)
- План: PnP-валидация → BGG + соцсети (6 мес.) → Kickstarter → тираж 500 копий
- Главный риск: конверсия бот-аудитории в покупателей физики не гарантирована
- Прямые конкуренты: Farkle, Qwixx ($15), Can't Stop ($30)
- УТП: механика (combo-система, фракции, спец-кубики)

---

## НЕДАВНИЕ ИЗМЕНЕНИЯ (май 2026)

1. **Акции казино** — полная реализация (database.py + bot.py handlers + 5% дивидендов от проигрышей)
2. **Кросс-бот обменник** — Кубик ↔ FUT с комиссией 10%
3. **ВИП Покер** — 5-карточный с параллельными уведомлениями всем игрокам
4. **Зал Богатства** — таблички, рекорды, hall_only магазин
5. **Магазин 2.0** — stock-система, 63 новых предмета 10k–1M, 18 hall-only предметов
6. **Бонус-игра в слотах** — 3 уровня с Wild/Scatter
