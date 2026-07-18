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
import time
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
# Your Telegram numeric user id, allowed to run /reset_all. Optional.
OWNER_ID = os.environ.get("OWNER_ID", "").strip()

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
    # journal of every play, with a timestamp, for time-window leaderboards
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plays (
            chat_id TEXT,
            user_id TEXT,
            name    TEXT,
            score   INTEGER,
            ts      INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plays ON plays(chat_id, ts)")
    return conn


def save_best(chat_id, user_id, name, score):
    """Keep best-per-user in `scores`, and log every play in `plays` (with time).
    Returns True if this was a new all-time best."""
    conn = db()
    now = int(time.time())
    # log the play
    conn.execute(
        "INSERT INTO plays (chat_id, user_id, name, score, ts) VALUES (?,?,?,?,?)",
        (str(chat_id), str(user_id), name, int(score), now),
    )
    # update all-time best
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


def week_top(chat_id, days=7, limit=10):
    """Best score per user within the last `days` days, in this chat."""
    conn = db()
    since = int(time.time()) - days * 86400
    cur = conn.execute(
        """
        SELECT name, MAX(score) AS best
        FROM plays
        WHERE chat_id=? AND ts>=?
        GROUP BY user_id
        ORDER BY best DESC
        LIMIT ?
        """,
        (str(chat_id), since, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return [{"name": r[0], "score": r[1]} for r in rows]


def active_chats(days=7):
    """Chats that had at least one play in the window (for weekly auto-post)."""
    conn = db()
    since = int(time.time()) - days * 86400
    cur = conn.execute(
        "SELECT DISTINCT chat_id FROM plays WHERE ts>=?", (since,)
    )
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


def clear_chat(chat_id):
    conn = db()
    cur = conn.execute("DELETE FROM scores WHERE chat_id=?", (str(chat_id),))
    n = cur.rowcount
    conn.execute("DELETE FROM plays WHERE chat_id=?", (str(chat_id),))
    conn.commit()
    conn.close()
    return n


def clear_all():
    conn = db()
    cur = conn.execute("DELETE FROM scores")
    n = cur.rowcount
    conn.execute("DELETE FROM plays")
    conn.commit()
    conn.close()
    return n


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


def format_week(rows):
    if not rows:
        return "За останній тиждень ще ніхто не грав. Напишіть /play і почніть!"
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 Підсумок тижня — Ping Boot\nТоп гравців за 7 днів:\n"]
    for i, r in enumerate(rows):
        tag = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{tag} {r['name']} — {r['score']}")
    champ = rows[0]
    lines.append(f"\nЧемпіон тижня: {champ['name']} 👑")
    return "\n".join(lines)


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the last-7-days leaderboard for this chat, on demand."""
    rows = week_top(update.effective_chat.id, days=7, limit=10)
    await update.message.reply_text(format_week(rows))


async def _is_chat_admin(update, context):
    """True if the sender is an admin/creator of the current chat, or in a private chat."""
    chat = update.effective_chat
    if chat.type == "private":
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def reset_scores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear the leaderboard of THIS chat. Allowed for chat admins or the owner."""
    uid = str(update.effective_user.id)
    if not (uid == OWNER_ID or await _is_chat_admin(update, context)):
        await update.message.reply_text("Скинути таблицю може лише адмін чату.")
        return
    n = clear_chat(update.effective_chat.id)
    await update.message.reply_text(
        f"Таблицю лідерів цього чату очищено (видалено записів: {n}). "
        "Нові рекорди почнуться з чистого аркуша."
    )


async def reset_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear ALL leaderboards in every chat. Owner only."""
    uid = str(update.effective_user.id)
    if not OWNER_ID or uid != OWNER_ID:
        await update.message.reply_text("Ця команда доступна лише власнику бота.")
        return
    n = clear_all()
    await update.message.reply_text(
        f"Усі таблиці лідерів очищено (видалено записів: {n})."
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


async def weekly_autopost(context: ContextTypes.DEFAULT_TYPE):
    """Runs daily; posts the weekly summary once a week (Mondays).
    Sends to every chat that had plays in the last 7 days."""
    import datetime
    # only fire on Mondays (weekday()==0)
    if datetime.datetime.now().weekday() != 0:
        return
    chats = active_chats(days=7)
    for chat_id in chats:
        rows = week_top(chat_id, days=7, limit=10)
        if not rows:
            continue
        try:
            await context.bot.send_message(chat_id=int(chat_id), text=format_week(rows))
        except Exception as e:
            log.info("weekly_autopost to %s failed: %s", chat_id, e)


def main():
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("play", play))
    application.add_handler(CommandHandler("week", week))
    application.add_handler(CommandHandler("reset_scores", reset_scores))
    application.add_handler(CommandHandler("reset_all", reset_all))
    application.add_handler(CallbackQueryHandler(game_callback))
    application.post_init = post_init

    # schedule the weekly auto-post check: run daily at 12:00 server time
    try:
        import datetime
        application.job_queue.run_daily(
            weekly_autopost,
            time=datetime.time(hour=12, minute=0),
        )
    except Exception as e:
        log.warning("Could not schedule weekly auto-post: %s", e)

    application.run_polling()


if __name__ == "__main__":
    main()
