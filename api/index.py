"""
api/index.py — Vercel webhook entry point.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
import logging

from flask import Flask, request, Response
from telegram import Update
import bot
from config import WEBHOOK_SECRET

logger = logging.getLogger(__name__)

flask_app = Flask(__name__)

# Кешируем Application и event loop между запросами (в рамках одного warm Lambda).
# Холодный старт — только при первом запросе после простоя.
_loop: asyncio.AbstractEventLoop | None = None
_application = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


async def _ensure_app():
    global _application
    if _application is None:
        _application = bot.create_application()
        await _application.initialize()  # вызывается один раз: set_my_commands и т.д.
    return _application


async def _process(body: bytes) -> None:
    application = await _ensure_app()
    update = Update.de_json(json.loads(body), application.bot)
    await application.process_update(update)


@flask_app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@flask_app.route("/<path:path>", methods=["GET", "POST"])
def webhook(path=""):
    if request.method == "POST":
        if WEBHOOK_SECRET:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if token != WEBHOOK_SECRET:
                logger.warning("Unauthorized webhook request — invalid secret token")
                return Response("Forbidden", status=403)
        try:
            _get_loop().run_until_complete(_process(request.get_data()))
        except Exception as e:
            logger.exception("Ошибка обработки: %s", e)
    return Response("ok", status=200)


# Vercel ищет переменную `app` для WSGI
app = flask_app
