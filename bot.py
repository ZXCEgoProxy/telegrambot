"""
Telegram-бот для приёма заявок в приватный канал.
Стек: Python 3.10+, aiogram 3.x, asyncpg.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Dict

import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.deep_linking import create_start_link
from dotenv import load_dotenv

# ---------- Загрузка конфигурации ----------
# Загружаем .env только локально (не на Railway)
if not os.getenv("RAILWAY_ENVIRONMENT"):
    load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN")
ADMIN_ID: int = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None
CHANNEL_ID: int = int(os.getenv("CHANNEL_ID")) if os.getenv("CHANNEL_ID") else None
INVITE_LINK: Optional[str] = os.getenv("INVITE_LINK")
DATABASE_URL: str = os.getenv("DATABASE_URL")

if not BOT_TOKEN or ADMIN_ID is None or CHANNEL_ID is None or not DATABASE_URL:
    raise EnvironmentError("Environment variables not set")

logger = logging.getLogger(__name__)

# ---------- Инициализация бота и диспетчера ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ---------- Работа с БД ----------
pool = None

async def get_db():
    """Возвращает пул соединений с БД."""
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL)
    return pool

async def init_db():
    """Creates the table if it doesn't exist."""
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id SERIAL PRIMARY KEY,
                user_id BIGINT UNIQUE,
                name TEXT NOT NULL,
                username TEXT,
                status TEXT DEFAULT 'approved',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                invite_link TEXT
            )
        """)

# ---------- Защита от спама ----------
# Простейшая реализация: 5 секунд между любыми сообщениями от одного пользователя
user_last_message: Dict[int, float] = {}

async def throttle_check(message: types.Message) -> bool:
    """Returns True if the message can be processed (5+ seconds since last)."""
    user_id = message.from_user.id
    now = datetime.now().timestamp()
    last = user_last_message.get(user_id, 0)
    if now - last < 5:
        await message.answer("⚠️ Too fast! Wait 5 seconds.")
        return False
    user_last_message[user_id] = now
    return True

# ---------- Вспомогательные функции ----------
async def create_invite_for_user(user_id: int) -> Optional[str]:
    """
    Creates a unique invite link to the channel with 1 use limit and 7 days expiry.
    If the bot can't create the link, returns the static link from environment.
    """
    expire_date = datetime.now() + timedelta(days=7)
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            expire_date=expire_date,
            creates_join_request=False
        )
        return invite.invite_link
    except Exception:
        return INVITE_LINK

async def send_invite_to_user(user_id: int, invite_link: str):
    """Sends the invite message to the user."""
    try:
        await bot.send_message(
            user_id,
            f"✅ Your application is approved! Here's the join link:\n"
            f"{invite_link}\n"
            f"Link is valid for 7 days and one use only."
        )
    except Exception:
        pass

# ---------- Административные команды ----------

def is_admin(message_or_callback) -> bool:
    if hasattr(message_or_callback, 'from_user'):
        return message_or_callback.from_user.id == ADMIN_ID
    return False

# ---------- Обработчики команд ----------

@dp.message(Command("admin"), F.func(is_admin))
async def cmd_admin(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistics", callback_data="stats")]
    ])
    await message.answer("🔧 Admin Panel:", reply_markup=keyboard)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not await throttle_check(message):
        return

    user_id = message.from_user.id
    user = message.from_user
    name = user.first_name or "Unknown"
    username = user.username

    async with (await get_db()).acquire() as db:
        # Auto-approve application, allow resubmit
        await db.execute(
            """
            INSERT INTO applications (user_id, name, username, status)
            VALUES ($1, $2, $3, 'approved')
            ON CONFLICT (user_id) DO UPDATE SET
                name = EXCLUDED.name,
                username = EXCLUDED.username,
                status = 'approved',
                created_at = CURRENT_TIMESTAMP
            """,
            user_id, name, username
        )

        # Create invite
        invite_link = await create_invite_for_user(user_id)
        if invite_link:
            await db.execute("UPDATE applications SET invite_link = $1 WHERE user_id = $2", invite_link, user_id)
        else:
            invite_link = INVITE_LINK
            if invite_link:
                await db.execute("UPDATE applications SET invite_link = $1 WHERE user_id = $2", invite_link, user_id)

    await message.answer("👋 Welcome!")

    await asyncio.sleep(1)

    if invite_link:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Join Channel", url=invite_link)]
        ])
        await message.answer("Click the button to join the channel:", reply_markup=keyboard)
    else:
        await message.answer("❌ Failed to create invite link. Contact admin.")

# ---------- Административные команды ----------

@dp.callback_query(F.data == "stats")
async def callback_stats(callback: CallbackQuery):
    if not is_admin(callback):
        await callback.answer("Access denied.")
        return

    last_24h = datetime.now() - timedelta(hours=24)

    async with (await get_db()).acquire() as db:
        count_total = await db.fetchval("SELECT COUNT(*) FROM applications WHERE status = 'approved'")
        count_24h = await db.fetchval("SELECT COUNT(*) FROM applications WHERE status = 'approved' AND created_at >= $1", last_24h)

    text = (
        f"📊 Statistics:\n"
        f"• Total subscribers via bot: {count_total}\n"
        f"• Subscribers in last 24 hours: {count_24h}\n"
        f"• Bot status: Running"
    )
    await callback.message.edit_text(text)

# ---------- Обработка исключений ----------
@dp.errors()
async def errors_handler(update: types.Update, exception: Exception):
    logger.error(f"Unhandled exception: {exception}", exc_info=True)
    return True  # подавляем падение бота

# ---------- Start bot ----------
async def main():
    # Init DB
    await init_db()

    # Start polling
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
# telegrambot
# telegrambot