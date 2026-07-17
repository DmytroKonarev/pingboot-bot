"""
Telegram game bot: Ping Boot
- Serves the game via /play (works in groups)
- Runs a small HTTP endpoint the game calls to save the score
- Uses Telegram's built-in per-chat high-score board

Install:
    pip install "python-telegram-bot>=21" aiohttp

Env vars:
    BOT_TOKEN   token from @BotFather
    GAME_URL    https URL where game/index.html is hosted (with trailing slash)
    GAME_SHORT  game short name from BotFather (e.g. pingboot)
    PORT        port for the score endpoint (default 8080)
    PUBLIC_URL  public https URL of THIS bot's score endpoint
                (e.g. https://yourbot.up.railway.app)
"""

import os
import logging
from urllib.parse import urlencode

from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pingboot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
GAME_URL = os.environ["GAME_URL"].rstrip("/") + "/"
GAME_SHORT = os.environ.get("GAME_SHORT", "pingboot")
PORT = int(os.environ.get("PORT", "8080"))
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")

application = Application.builder().token(BOT_TOKEN).build()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! /play запускає гру Ping Boot. "
        "Додай мене в групу, і я вестиму таблицю рекордів для всіх."
    )


async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_game(
        chat_id=update.effective_chat.id,
        game_short_name=GAME_SHORT,
    )


async def game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.game_short_name != GAME_SHORT:
        await query.answer()
        return

    params = {
        "uid": query.from_user.id,
        "cid": query.message.chat_id if query.message else "",
        "mid": query.message.message_id if query.message else "",
        "iid": query.inline_message_id or "",
        "ep": f"{PUBLIC_URL}/score",  # where the game POSTs the score
    }
    url = f"{GAME_URL}?{urlencode(params)}"
    await query.answer(url=url)


async def score_handler(request: web.Request):
    try:
        data = await request.json()
        uid = int(data["uid"])
        score = int(data["score"])
        cid = data.get("cid")
        mid = data.get("mid")
        iid = data.get("iid")
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)

    kwargs = {"user_id": uid, "score": score, "force": False}
    if iid:
        kwargs["inline_message_id"] = iid
    elif cid and mid:
        kwargs["chat_id"] = int(cid)
        kwargs["message_id"] = int(mid)
    else:
        return web.json_response({"ok": False, "error": "no target"}, status=400)

    try:
        await application.bot.set_game_score(**kwargs)
    except Exception as e:
        # Telegram rejects a score lower than the stored best unless force=True.
        log.info("set_game_score: %s", e)
        return web.json_response({"ok": True, "note": str(e)})

    return web.json_response({"ok": True})


async def run_web_app():
    app = web.Application()

    @web.middleware
    async def cors_mw(request, handler):
        if request.method == "OPTIONS":
            resp = web.Response()
        else:
            resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "content-type"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        return resp

    app.middlewares.append(cors_mw)
    app.router.add_post("/score", score_handler)
    app.router.add_options("/score", lambda r: web.Response())

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Score endpoint on port %s", PORT)


async def post_init(app):
    await run_web_app()


def main():
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("play", play))
    application.add_handler(CallbackQueryHandler(game_callback))
    application.post_init = post_init
    application.run_polling()


if __name__ == "__main__":
    main()
