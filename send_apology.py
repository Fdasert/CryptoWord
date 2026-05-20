import asyncio
from telegram import Bot

BOT_TOKEN = input("BOT_TOKEN: ").strip()

MESSAGE = (
    "😔 *Извинения от администрации*\n\n"
    "Сегодня бот работал некорректно — кнопки зависали и не отвечали. "
    "Мы нашли и устранили причину.\n\n"
    "В знак извинений ты получаешь титул *🛠️ Ветеран Бага* — legendary предмет, "
    "которых существует ровно 4 штуки. Только для участников сегодняшней игры.\n\n"
    "Надень в /profile → Мой инвентарь.\n\n"
    "Спасибо за терпение 🙏"
)

PLAYERS = [518544601, 6814788302, 798189415, 537025501]


async def main():
    bot = Bot(token=BOT_TOKEN)
    for uid in PLAYERS:
        try:
            await bot.send_message(chat_id=uid, text=MESSAGE, parse_mode="Markdown")
            print(f"✓ {uid}")
        except Exception as e:
            print(f"✗ {uid}: {e}")


asyncio.run(main())
