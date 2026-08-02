import logging
import os
import random
import sqlite3
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from words import WORDS

# ---------- Настройка ----------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН")
DB_PATH = Path(__file__).parent / "scores.db"

ROUND_SECONDS = 60          # сколько секунд даётся на раунд
MAX_HINTS = 2                # сколько подсказок можно взять за раунд
RECENT_WORDS_MEMORY = 15     # чтобы одно и то же слово не выпадало подряд

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Состояние игры в каждом чате. Живёт, пока бот запущен.
# {chat_id: {
#     "word": str, "category": str, "shuffled": str,
#     "hints_used": int, "active": bool,
#     "streak_user": int | None, "streak_count": int,
#     "streak_name": str, "recent": list[str],
# }}
game_state: dict[int, dict] = {}


# ---------- База данных ----------

def db_init() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scores (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            score INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS global_stats (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            total_score INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            best_streak INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def db_add_points(chat_id: int, user_id: int, username: str, points: int, streak: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO scores (chat_id, user_id, username, score)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id)
        DO UPDATE SET score = score + excluded.score, username = excluded.username
        """,
        (chat_id, user_id, username, points),
    )
    conn.execute(
        """
        INSERT INTO global_stats (user_id, username, total_score, wins, best_streak)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            total_score = total_score + excluded.total_score,
            wins = wins + 1,
            username = excluded.username,
            best_streak = MAX(best_streak, excluded.best_streak)
        """,
        (user_id, username, points, streak),
    )
    conn.commit()
    cur = conn.execute(
        "SELECT score FROM scores WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    score = cur.fetchone()[0]
    conn.close()
    return score


def db_get_score(chat_id: int, user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT score FROM scores WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


def db_get_top(chat_id: int, limit: int = 10) -> list[tuple[str, int]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT username, score FROM scores WHERE chat_id = ? ORDER BY score DESC LIMIT ?",
        (chat_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def db_get_profile(user_id: int) -> tuple[int, int, int] | None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT total_score, wins, best_streak FROM global_stats WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


LEVELS = [
    (0, "Новичок 🌱"), (30, "Игрок 🙂"), (100, "Знаток слов 📚"),
    (250, "Мастер угадывания 🎯"), (500, "Гений слова 🧠"),
    (1000, "Легенда чата 👑"),
]


def get_level(total_score: int) -> str:
    title = LEVELS[0][1]
    for threshold, name in LEVELS:
        if total_score >= threshold:
            title = name
    return title


# ---------- Игровая логика ----------

def shuffle_word(word: str) -> str:
    letters = list(word)
    shuffled = word
    attempts = 0
    while shuffled == word and attempts < 20:
        random.shuffle(letters)
        shuffled = "".join(letters)
        attempts += 1
    return shuffled


def round_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("💡 Подсказка", callback_data="hint"),
            InlineKeyboardButton("⏭ Пропустить", callback_data="skip"),
        ]]
    )


def pick_word(chat_id: int) -> tuple[str, str]:
    recent = game_state.get(chat_id, {}).get("recent", [])
    choices = [w for w in WORDS if w[0] not in recent] or WORDS
    return random.choice(choices)


def start_new_round(chat_id: int) -> None:
    word, category = pick_word(chat_id)
    shuffled = shuffle_word(word)

    prev = game_state.get(chat_id, {})
    recent = prev.get("recent", [])
    recent.append(word)
    recent = recent[-RECENT_WORDS_MEMORY:]

    game_state[chat_id] = {
        "word": word,
        "category": category,
        "shuffled": shuffled,
        "hints_used": 0,
        "active": True,
        "streak_user": prev.get("streak_user"),
        "streak_count": prev.get("streak_count", 0),
        "streak_name": prev.get("streak_name", ""),
        "recent": recent,
    }


def round_text(chat_id: int) -> str:
    st = game_state[chat_id]
    lines = [f"🔤 Угадайте слово:\n\n<b>{st['shuffled'].upper()}</b>\n"]
    if st["streak_count"] >= 2:
        lines.append(f"🔥 Серия побед: {st['streak_name']} — {st['streak_count']} подряд!")
    lines.append(f"⏱ У вас {ROUND_SECONDS} секунд. Просто напишите слово в чат.")
    return "\n".join(lines)


async def send_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    start_new_round(chat_id)
    await context.bot.send_message(
        chat_id,
        round_text(chat_id),
        parse_mode=ParseMode.HTML,
        reply_markup=round_keyboard(),
    )
    schedule_timeout(chat_id, context)


def schedule_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    cancel_timeout(chat_id, context)
    context.job_queue.run_once(
        round_timeout, ROUND_SECONDS, chat_id=chat_id, name=f"timeout_{chat_id}"
    )


def cancel_timeout(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    for job in context.job_queue.get_jobs_by_name(f"timeout_{chat_id}"):
        job.schedule_removal()


async def round_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    st = game_state.get(chat_id)
    if not st or not st["active"]:
        return
    word = st["word"]
    st["streak_user"] = None
    st["streak_count"] = 0
    st["streak_name"] = ""
    await context.bot.send_message(
        chat_id, f"⌛ Время вышло! Загаданное слово было: «{word}»."
    )
    await send_round(chat_id, context)


def score_for_round(hints_used: int, streak_count: int) -> int:
    base = 10 - hints_used * 3
    base = max(base, 3)
    bonus = min(streak_count - 1, 5) if streak_count > 1 else 0
    return base + bonus


def reveal_hint_text(st: dict) -> str:
    hints_used = st["hints_used"]
    if hints_used == 0:
        return f"📂 Категория: <b>{st['category']}</b>"
    if hints_used == 1:
        return f"🔡 Слово начинается на «{st['word'][0].upper()}»"
    return "Подсказки закончились для этого раунда."


# ---------- Хендлеры команд ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот-игра «Угадай слово» 🎮\n\n"
        "Пиши /help, чтобы увидеть все команды, или сразу /game, чтобы начать!"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>Как играть</b>\n"
        "Я показываю перемешанные буквы слова — нужно написать слово целиком "
        f"в чат. На раунд даётся {ROUND_SECONDS} секунд.\n\n"
        "🔥 Угадывай подряд, не давая победить другим — получишь бонус к очкам "
        "за серию.\n"
        "💡 Можно взять подсказку (категория, потом первая буква) — но за неё "
        "срезаются очки.\n\n"
        "<b>Команды</b>\n"
        "/game — начать игру в этом чате\n"
        "/stop — остановить игру\n"
        "/hint — подсказка\n"
        "/score — твой счёт в этом чате\n"
        "/top — таблица лидеров чата\n"
        "/profile — твой профиль и уровень (по всем чатам)",
        parse_mode=ParseMode.HTML,
    )


async def cmd_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if game_state.get(chat_id, {}).get("active"):
        await update.message.reply_text("Игра уже идёт! Угадывайте текущее слово 👇")
        return
    await send_round(chat_id, context)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    st = game_state.get(chat_id)
    if st and st["active"]:
        cancel_timeout(chat_id, context)
        word = st["word"]
        st["active"] = False
        await update.message.reply_text(
            f"Игра остановлена. Загаданное слово было: «{word}».\n"
            f"Введите /game, чтобы начать заново."
        )
    else:
        await update.message.reply_text("Сейчас игра не идёт. Введите /game, чтобы начать.")


async def cmd_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    st = game_state.get(chat_id)
    if not st or not st["active"]:
        await update.message.reply_text("Сейчас игра не идёт. Введите /game, чтобы начать.")
        return
    if st["hints_used"] >= MAX_HINTS:
        await update.message.reply_text("Подсказки на этот раунд закончились 🤷")
        return
    text = reveal_hint_text(st)
    st["hints_used"] += 1
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    score = db_get_score(chat_id, user.id)
    await update.message.reply_text(f"У тебя {score} очков в этом чате.")


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    rows = db_get_top(chat_id)
    if not rows:
        await update.message.reply_text("Пока никто не набрал очков. Сыграйте в /game!")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 Таблица лидеров чата:\n"]
    for i, (username, score) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} {username or 'Игрок'} — {score}")
    await update.message.reply_text("\n".join(lines))


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    profile = db_get_profile(user.id)
    if not profile:
        await update.message.reply_text(
            "Ты ещё не заработал очков. Сыграй в /game в любом чате со мной!"
        )
        return
    total_score, wins, best_streak = profile
    level = get_level(total_score)
    await update.message.reply_text(
        f"👤 <b>Профиль {user.first_name}</b>\n\n"
        f"Уровень: {level}\n"
        f"Очков всего (по всем чатам): {total_score}\n"
        f"Угадано слов: {wins}\n"
        f"Лучшая серия побед подряд: {best_streak}",
        parse_mode=ParseMode.HTML,
    )


# ---------- Кнопки под сообщением раунда ----------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    st = game_state.get(chat_id)
    await query.answer()

    if not st or not st["active"]:
        await query.answer("Игра уже закончилась.", show_alert=True)
        return

    if query.data == "hint":
        if st["hints_used"] >= MAX_HINTS:
            await query.answer("Подсказки закончились для этого раунда.", show_alert=True)
            return
        text = reveal_hint_text(st)
        st["hints_used"] += 1
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    elif query.data == "skip":
        cancel_timeout(chat_id, context)
        word = st["word"]
        st["streak_user"] = None
        st["streak_count"] = 0
        st["streak_name"] = ""
        await context.bot.send_message(chat_id, f"⏭ Пропущено. Слово было: «{word}».")
        await send_round(chat_id, context)


# ---------- Обычные сообщения (попытки угадать слово) ----------

async def handle_guess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    st = game_state.get(chat_id)
    if not st or not st["active"]:
        return

    guess = update.message.text.strip().lower()
    if guess != st["word"]:
        return

    cancel_timeout(chat_id, context)
    user = update.effective_user
    username = user.username or user.first_name or "Игрок"

    if st["streak_user"] == user.id:
        st["streak_count"] += 1
    else:
        st["streak_user"] = user.id
        st["streak_count"] = 1
        st["streak_name"] = username

    points = score_for_round(st["hints_used"], st["streak_count"])
    new_score = db_add_points(chat_id, user.id, username, points, st["streak_count"])

    streak_line = ""
    if st["streak_count"] >= 2:
        streak_line = f"\n🔥 Серия побед: {st['streak_count']} подряд!"

    await update.message.reply_text(
        f"✅ Верно, {username}! Слово: «{st['word']}» (+{points} очков){streak_line}\n"
        f"Счёт в этом чате: {new_score}"
    )
    await send_round(chat_id, context)


# ---------- Точка входа ----------

def main() -> None:
    if BOT_TOKEN == "ВСТАВЬ_СЮДА_ТОКЕН":
        raise SystemExit(
            "Не задан токен бота. Установи переменную окружения BOT_TOKEN "
            "или впиши токен прямо в bot.py."
        )

    db_init()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("game", cmd_game))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("hint", cmd_hint))
    app.add_handler(CommandHandler("score", cmd_score))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_guess))

    logger.info("Бот запущен. Ожидание сообщений...")
    app.run_polling()


if __name__ == "__main__":
    main()
