import logging
import os
import random
import sqlite3
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from words import WORDS

# ---------- Настройка ----------

# Токен можно задать через переменную окружения BOT_TOKEN
# или прямо здесь, вместо "ВСТАВЬ_СЮДА_ТОКЕН".
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН")

DB_PATH = Path(__file__).parent / "scores.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# game_state хранит текущее слово для каждого чата, пока бот запущен.
# Формат: {chat_id: {"word": "яблоко", "shuffled": "олкояб"}}
game_state: dict[int, dict[str, str]] = {}


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
    conn.commit()
    conn.close()


def db_add_point(chat_id: int, user_id: int, username: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO scores (chat_id, user_id, username, score)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(chat_id, user_id)
        DO UPDATE SET score = score + 1, username = excluded.username
        """,
        (chat_id, user_id, username),
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
        """
        SELECT username, score FROM scores
        WHERE chat_id = ?
        ORDER BY score DESC
        LIMIT ?
        """,
        (chat_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------- Игровая логика ----------

def shuffle_word(word: str) -> str:
    """Перемешивает буквы так, чтобы результат отличался от исходного слова."""
    letters = list(word)
    shuffled = word
    attempts = 0
    while shuffled == word and attempts < 20:
        random.shuffle(letters)
        shuffled = "".join(letters)
        attempts += 1
    return shuffled


def start_new_round(chat_id: int) -> str:
    word = random.choice(WORDS)
    shuffled = shuffle_word(word)
    game_state[chat_id] = {"word": word, "shuffled": shuffled}
    return shuffled


# ---------- Хендлеры команд ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я бот-игра «Угадай слово».\n\n"
        "Команды:\n"
        "/game — начать игру в этом чате\n"
        "/stop — остановить игру\n"
        "/score — твой счёт в этом чате\n"
        "/top — таблица лидеров чата\n\n"
        "Добавь меня в группу, чтобы играть всей компанией!"
    )


async def cmd_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    shuffled = start_new_round(chat_id)
    await update.message.reply_text(
        f"🔤 Игра началась! Угадайте слово по перемешанным буквам:\n\n"
        f"<b>{shuffled.upper()}</b>\n\n"
        f"Просто напишите слово в чат. Каждый верный ответ = 1 очко.\n"
        f"Чтобы остановить игру — /stop",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id in game_state:
        word = game_state.pop(chat_id)["word"]
        await update.message.reply_text(f"Игра остановлена. Загаданное слово было: «{word}».")
    else:
        await update.message.reply_text("Сейчас игра не идёт. Введите /game, чтобы начать.")


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
    lines = ["🏆 Таблица лидеров:\n"]
    for i, (username, score) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} {username or 'Игрок'} — {score}")
    await update.message.reply_text("\n".join(lines))


# ---------- Хендлер обычных сообщений (попытки угадать слово) ----------

async def handle_guess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in game_state:
        return  # игра не запущена — игнорируем обычные сообщения

    guess = update.message.text.strip().lower()
    correct_word = game_state[chat_id]["word"]

    if guess == correct_word:
        user = update.effective_user
        username = user.username or user.first_name or "Игрок"
        new_score = db_add_point(chat_id, user.id, username)
        await update.message.reply_text(
            f"✅ Верно, {username}! Это было слово «{correct_word}».\n"
            f"Твой счёт: {new_score}"
        )
        shuffled = start_new_round(chat_id)
        await update.message.reply_text(
            f"Следующее слово:\n\n<b>{shuffled.upper()}</b>",
            parse_mode=ParseMode.HTML,
        )


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
    app.add_handler(CommandHandler("game", cmd_game))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("score", cmd_score))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_guess))

    logger.info("Бот запущен. Ожидание сообщений...")
    app.run_polling()


if __name__ == "__main__":
    main()
