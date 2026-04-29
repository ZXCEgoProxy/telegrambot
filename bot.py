"""
Telegram-бот для приёма заявок в приватный канал.
Стек: Python 3.10+, aiogram 3.x, aiosqlite.
"""

import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict

import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.deep_linking import create_start_link
from dotenv import load_dotenv

# ---------- Загрузка конфигурации ----------
load_dotenv()
BOT_TOKEN: str = os.getenv("BOT_TOKEN")
ADMIN_ID: int = int(os.getenv("ADMIN_ID"))          # Telegram ID администратора
CHANNEL_ID: int = int(os.getenv("CHANNEL_ID"))      # ID приватного канала (например, -1001234567890)
INVITE_LINK: Optional[str] = os.getenv("INVITE_LINK")  # Резервная ссылка, если бот не может создать приглашение
DB_PATH: str = os.getenv("DB_PATH", "applications.db")  # Путь к файлу БД

if not BOT_TOKEN or not ADMIN_ID or not CHANNEL_ID:
    raise ValueError("BOT_TOKEN, ADMIN_ID и CHANNEL_ID должны быть установлены в .env")

# ---------- Логирование ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------- Инициализация бота и диспетчера ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ---------- Работа с БД ----------
async def get_db() -> aiosqlite.Connection:
    """Возвращает соединение с БД (один экземпляр на всё приложение)."""
    return await aiosqlite.connect(DB_PATH)

async def init_db():
    """Создание таблицы, если её нет."""
    async with await get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                name TEXT NOT NULL,
                username TEXT,
                reason TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                invite_link TEXT
            )
        """)
        await db.commit()

# ---------- Модель FSM ----------
class ApplicationForm(StatesGroup):
    name = State()
    username = State()
    reason = State()

# ---------- Защита от спама ----------
# Простейшая реализация: 5 секунд между любыми сообщениями от одного пользователя
user_last_message: Dict[int, float] = {}

async def throttle_check(message: types.Message) -> bool:
    """Возвращает True, если сообщение можно обработать (прошло >= 5 сек с предыдущего)."""
    user_id = message.from_user.id
    now = datetime.now().timestamp()
    last = user_last_message.get(user_id, 0)
    if now - last < 5:
        await message.answer("⚠️ Слишком быстро! Подождите 5 секунд.")
        return False
    user_last_message[user_id] = now
    return True

# ---------- Вспомогательные функции ----------
async def create_invite_for_user(user_id: int) -> Optional[str]:
    """
    Создаёт уникальную пригласительную ссылку в канал с лимитом 1 использование и сроком 7 дней.
    Если бот не может создать ссылку (не хватает прав), возвращает статическую ссылку из окружения.
    """
    expire_date = datetime.now(timezone.utc) + timedelta(days=7)
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            expire_date=expire_date,
            creates_join_request=False
        )
        logger.info(f"Создана invite-ссылка для user_id={user_id}: {invite.invite_link}")
        return invite.invite_link
    except Exception as e:
        logger.warning(f"Не удалось создать invite-ссылку: {e}. Использую статическую ссылку.")
        return INVITE_LINK

async def send_invite_to_user(user_id: int, invite_link: str):
    """Отправляет пользователю сообщение с приглашением."""
    try:
        await bot.send_message(
            user_id,
            f"✅ Ваша заявка одобрена! Вот ссылка для вступления:\n"
            f"{invite_link}\n"
            f"Ссылка действительна 7 дней и только для одного входа."
        )
    except Exception as e:
        logger.error(f"Ошибка отправки ссылки пользователю {user_id}: {e}")

# ---------- Обработчики команд ----------

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    if not await throttle_check(message):
        return

    user_id = message.from_user.id
    async with await get_db() as db:
        cursor = await db.execute("SELECT id FROM applications WHERE user_id = ?", (user_id,))
        existing = await cursor.fetchone()
        if existing:
            await message.answer(
                "Вы уже подавали заявку. Ожидайте решения администратора. "
                "Если ссылка была утеряна, свяжитесь с администратором."
            )
            return

    # Предзаполняем username из Telegram, если есть
    tg_username = message.from_user.username
    await state.update_data(tg_username=tg_username)

    await message.answer(
        "👋 Добро пожаловать! Для вступления в приватный канал заполните короткую заявку.\n\n"
        "Введите ваше имя (настоящее или публичное):"
    )
    await state.set_state(ApplicationForm.name)

@dp.message(ApplicationForm.name)
async def process_name(message: types.Message, state: FSMContext):
    if not await throttle_check(message):
        return

    name = message.text.strip()
    if len(name) < 2 or len(name) > 100:
        await message.answer("❌ Имя должно быть от 2 до 100 символов. Попробуйте ещё раз.")
        return

    await state.update_data(name=name)

    # Предлагаем username: если есть в Telegram, просим подтвердить, иначе ввести
    data = await state.get_data()
    tg_username = data.get("tg_username")
    if tg_username:
        await message.answer(
            f"Ваш Telegram username: @{tg_username}\n"
            "Если хотите использовать другой, введите его сейчас, или просто отправьте точку (.), чтобы оставить этот."
        )
    else:
        await message.answer("Введите ваш Telegram username (например, @durov) или отправьте точку (.), если его нет:")
    await state.set_state(ApplicationForm.username)

@dp.message(ApplicationForm.username)
async def process_username(message: types.Message, state: FSMContext):
    if not await throttle_check(message):
        return

    user_input = message.text.strip()
    data = await state.get_data()
    tg_username = data.get("tg_username")

    if user_input == ".":
        # оставляем текущий или пустой
        username = tg_username if tg_username else ""
    else:
        # убираем возможный @ и проверяем формат
        username = user_input.lstrip("@")
        if not (3 <= len(username) <= 32) or not username.replace("_", "").isalnum():
            await message.answer("❌ Некорректный username. Введите ещё раз или точку, чтобы пропустить.")
            return

    await state.update_data(username=username)
    await message.answer("Почему вы хотите вступить? (кратко, можно пропустить, отправив точку)»")
    await state.set_state(ApplicationForm.reason)

@dp.message(ApplicationForm.reason)
async def process_reason(message: types.Message, state: FSMContext):
    if not await throttle_check(message):
        return

    reason = message.text.strip()
    if reason == ".":
        reason = None

    data = await state.get_data()
    user_id = message.from_user.id
    name = data["name"]
    username = data["username"]

    # Сохраняем заявку в БД
    async with await get_db() as db:
        try:
            await db.execute(
                "INSERT INTO applications (user_id, name, username, reason) VALUES (?, ?, ?, ?)",
                (user_id, name, username, reason)
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            await message.answer("Вы уже подавали заявку.")
            await state.clear()
            return

    # Автоматически создаём пригласительную ссылку и сохраняем в БД
    invite_link = await create_invite_for_user(user_id)
    if invite_link:
        async with await get_db() as db:
            await db.execute("UPDATE applications SET invite_link = ? WHERE user_id = ?", (invite_link, user_id))
            await db.commit()
        await send_invite_to_user(user_id, invite_link)
    else:
        # если ссылка не создалась и статической нет
        logger.error(f"Не удалось получить пригласительную ссылку для user_id={user_id}")
        await message.answer("❌ Произошла ошибка при создании приглашения. Администратор свяжется с вами вручную.")

    await message.answer("✅ Заявка принята! Приглашение отправлено вам в личные сообщения.")
    await state.clear()

# ---------- Административные команды ----------

def is_admin(message: types.Message) -> bool:
    return message.from_user.id == ADMIN_ID

@dp.message(Command("list"), F.func(lambda _, msg: is_admin(msg)))
async def cmd_list(message: types.Message):
    async with await get_db() as db:
        cursor = await db.execute(
            "SELECT id, user_id, name, username, reason, status, created_at FROM applications WHERE status = 'pending'"
        )
        rows = await cursor.fetchall()
        if not rows:
            await message.answer("Нет заявок в ожидании.")
            return

        text = "<b>Ожидающие заявки:</b>\n\n"
        for row in rows:
            id_, uid, name, username, reason, status, created = row
            username_str = f"@{username}" if username else "не указан"
            reason_str = reason or "не указана"
            text += (
                f"ID заявки: {id_}\n"
                f"User ID: {uid}\n"
                f"Имя: {name}\n"
                f"Username: {username_str}\n"
                f"Причина: {reason_str}\n"
                f"Статус: {status}\n"
                f"Дата: {created}\n"
                f"──────────────────\n"
            )
        await message.answer(text, parse_mode="HTML")

@dp.message(Command("approve"), F.func(lambda _, msg: is_admin(msg)))
async def cmd_approve(message: types.Message, command):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /approve <user_id>")
        return
    try:
        user_id = int(args[1])
    except ValueError:
        await message.answer("Неверный user_id")
        return

    async with await get_db() as db:
        cursor = await db.execute("SELECT invite_link, status FROM applications WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row:
            await message.answer("Заявка не найдена.")
            return

        invite_link, status = row
        if status != "pending":
            await message.answer(f"Заявка уже обработана (статус: {status}).")
            return

        # Если ссылка ещё не создана, создаём
        if not invite_link:
            invite_link = await create_invite_for_user(user_id)
            if not invite_link:
                await message.answer("❌ Не удалось создать пригласительную ссылку.")
                return
            await db.execute("UPDATE applications SET invite_link = ? WHERE user_id = ?", (invite_link, user_id))

        await db.execute("UPDATE applications SET status = 'approved' WHERE user_id = ?", (user_id,))
        await db.commit()

    await send_invite_to_user(user_id, invite_link)
    await message.answer(f"✅ Заявка пользователя {user_id} одобрена. Ссылка отправлена.")

@dp.message(Command("reject"), F.func(lambda _, msg: is_admin(msg)))
async def cmd_reject(message: types.Message, command):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /reject <user_id>")
        return
    try:
        user_id = int(args[1])
    except ValueError:
        await message.answer("Неверный user_id")
        return

    async with await get_db() as db:
        cursor = await db.execute("SELECT status FROM applications WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row:
            await message.answer("Заявка не найдена.")
            return
        status = row[0]
        if status != "pending":
            await message.answer(f"Заявка уже обработана (статус: {status}).")
            return

        await db.execute("UPDATE applications SET status = 'rejected' WHERE user_id = ?", (user_id,))
        await db.commit()

    try:
        await bot.send_message(user_id, "❌ Ваша заявка была отклонена администратором.")
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление об отказе пользователю {user_id}: {e}")

    await message.answer(f"❌ Заявка пользователя {user_id} отклонена. Пользователь уведомлён.")

@dp.message(Command("stats"), F.func(lambda _, msg: is_admin(msg)))
async def cmd_stats(message: types.Message):
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())

    async with await get_db() as db:
        row_today = await db.execute_fetchall(
            "SELECT COUNT(*) FROM applications WHERE created_at >= ?", (today_start.isoformat(),)
        )
        row_week = await db.execute_fetchall(
            "SELECT COUNT(*) FROM applications WHERE created_at >= ?", (week_start.isoformat(),)
        )
        row_pending = await db.execute_fetchall(
            "SELECT COUNT(*) FROM applications WHERE status = 'pending'", ()
        )
        count_today = row_today[0][0] if row_today else 0
        count_week = row_week[0][0] if row_week else 0
        count_pending = row_pending[0][0] if row_pending else 0

    await message.answer(
        f"📊 Статистика:\n"
        f"• За сегодня: {count_today}\n"
        f"• За неделю: {count_week}\n"
        f"• В ожидании: {count_pending}"
    )

# ---------- Обработка исключений ----------
@dp.errors()
async def errors_handler(update: types.Update, exception: Exception):
    logger.error(f"Unhandled exception: {exception}", exc_info=True)
    return True  # подавляем падение бота

# ---------- Запуск и graceful shutdown ----------
async def main():
    # Инициализация БД
    await init_db()
    logger.info("База данных инициализирована")

    # Запуск поллинга
    try:
        logger.info("Бот запущен")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logger.info("Бот остановлен")

if __name__ == "__main__":
    # Обработка сигналов для корректного завершения на хостингах
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("Получен сигнал остановки")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
# telegrambot
# telegrambot
