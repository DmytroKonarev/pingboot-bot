"""
Telegram game bot: Ping Boot (with own SQLite leaderboard)

- /play  -> sends the game (works in groups)
- HTTP POST /score  -> game reports a result; we save best-per-user-per-chat
                       AND update Telegram's built-in board
- HTTP GET  /scores -> game fetches the top list for the current chat

Install:
    pip install "python-telegram-bot>=21" aiohttp

Env vars:
    BOT_TOKEN   token from @BotFather
    GAME_URL    https URL where game index.html is hosted (trailing slash ok)
    GAME_SHORT  game short name from BotFather (e.g. game_short)
    PUBLIC_URL  public https URL of THIS bot (e.g. https://xxx.up.railway.app)
    PORT        provided by Railway automatically; default 8080
    DB_PATH     optional, default /tmp/scores.db
"""

import os
import logging
import sqlite3
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
GAME_SHORT = os.environ.get("GAME_SHORT", "game_short")
PORT = int(os.environ.get("PORT", "8080"))
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "/tmp/scores.db")

application = Application.builder().token(BOT_TOKEN).build()


# ---------------- database ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            chat_id TEXT,
            user_id TEXT,
            name    TEXT,
            best    INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    return conn


def save_best(chat_id, user_id, name, score):
    """Insert or update, keeping only the highest score per user per chat.
    Returns True if this was a new best."""
    conn = db()
    cur = conn.execute(
        "SELECT best FROM scores WHERE chat_id=? AND user_id=?",
        (str(chat_id), str(user_id)),
    )
    row = cur.fetchone()
    is_new_best = False
    if row is None:
        conn.execute(
            "INSERT INTO scores (chat_id, user_id, name, best) VALUES (?,?,?,?)",
            (str(chat_id), str(user_id), name, int(score)),
        )
        is_new_best = True
    elif int(score) > int(row[0]):
        conn.execute(
            "UPDATE scores SET best=?, name=? WHERE chat_id=? AND user_id=?",
            (int(score), name, str(chat_id), str(user_id)),
        )
        is_new_best = True
    else:
        # keep name fresh even if score not beaten
        conn.execute(
            "UPDATE scores SET name=? WHERE chat_id=? AND user_id=?",
            (name, str(chat_id), str(user_id)),
        )
    conn.commit()
    conn.close()
    return is_new_best


def top_scores(chat_id, limit=15):
    conn = db()
    cur = conn.execute(
        "SELECT name, best FROM scores WHERE chat_id=? ORDER BY best DESC LIMIT ?",
        (str(chat_id), limit),
    )
    rows = cur.fetchall()
    conn.close()
    return [{"name": r[0], "score": r[1]} for r in rows]


# ---------------- bot commands ----------------
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

    u = query.from_user
    display = u.username or u.full_name or f"id{u.id}"
    params = {
        "uid": u.id,
        "name": display,
        "cid": query.message.chat_id if query.message else "",
        "mid": query.message.message_id if query.message else "",
        "iid": query.inline_message_id or "",
        "ep": f"{PUBLIC_URL}/score",
        "top": f"{PUBLIC_URL}/scores",
    }
    url = f"{GAME_URL}?{urlencode(params)}"
    await query.answer(url=url)


# ---------------- HTTP endpoints ----------------
async def score_handler(request: web.Request):
    try:
        data = await request.json()
        uid = int(data["uid"])
        score = int(data["score"])
        name = (data.get("name") or f"id{uid}")[:64]
        cid = data.get("cid")
        mid = data.get("mid")
        iid = data.get("iid")
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)

    # 1) our own leaderboard (used for the in-game table)
    board_chat = cid if cid else f"inline_{iid}"
    save_best(board_chat, uid, name, score)

    # 2) also update Telegram's built-in board (best effort)
    kwargs = {"user_id": uid, "score": score, "force": False}
    if iid:
        kwargs["inline_message_id"] = iid
    elif cid and mid:
        kwargs["chat_id"] = int(cid)
        kwargs["message_id"] = int(mid)
    if "inline_message_id" in kwargs or "chat_id" in kwargs:
        try:
            await application.bot.set_game_score(**kwargs)
        except Exception as e:
            log.info("set_game_score: %s", e)  # lower score etc, fine

    return web.json_response({"ok": True})


async def scores_handler(request: web.Request):
    cid = request.query.get("cid")
    iid = request.query.get("iid")
    board_chat = cid if cid else (f"inline_{iid}" if iid else None)
    if not board_chat:
        return web.json_response({"ok": False, "error": "no chat"}, status=400)
    return web.json_response({"ok": True, "top": top_scores(board_chat)})


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
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

    app.middlewares.append(cors_mw)
    app.router.add_post("/score", score_handler)
    app.router.add_get("/scores", scores_handler)
    app.router.add_options("/score", lambda r: web.Response())
    app.router.add_options("/scores", lambda r: web.Response())

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP endpoints (/score, /scores) on port %s", PORT)


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
