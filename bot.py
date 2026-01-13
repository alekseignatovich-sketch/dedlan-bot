# bot.py — версия 6: сначала выбор исполнителя, потом задача
import os
import asyncio
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
import aiosqlite
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

class TaskCreation(StatesGroup):
    waiting_for_assignee = State()  # ← Сначала исполнитель
    waiting_for_text = State()
    waiting_for_date = State()
    waiting_for_hour = State()
    waiting_for_minute = State()

async def init_db():
    async with aiosqlite.connect("deadline.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                assignee_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                deadline DATETIME NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_check_time DATETIME,
                checkpoints_enabled BOOLEAN DEFAULT 1
            )
        """)
        await db.commit()

def create_7day_calendar() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    today = datetime.today().date()
    for i in range(7):
        date_obj = today + timedelta(days=i)
        if i == 0:
            label = f"Сегодня {date_obj.strftime('%d %b')}"
        elif i == 1:
            label = f"Завтра {date_obj.strftime('%d %b')}"
        else:
            label = date_obj.strftime("%d %b")
        date_str = date_obj.strftime("%Y-%m-%d")
        builder.button(text=label, callback_data=f"select_date_{date_str}")
    builder.adjust(1)
    return builder

async def save_task_and_schedule(bot: Bot, creator_id: int, assignee_id: int, task_data: dict):
    text = task_data["text"]
    deadline = datetime.fromisoformat(task_data["deadline"])
    
    duration = (deadline - datetime.now()).total_seconds()
    checkpoints_enabled = duration > 600

    async with aiosqlite.connect("deadline.db") as db:
        await db.execute(
            "INSERT INTO tasks (creator_id, assignee_id, text, deadline, checkpoints_enabled) VALUES (?, ?, ?, ?, ?)",
            (creator_id, assignee_id, text, deadline, int(checkpoints_enabled))
        )
        await db.commit()
        cursor = await db.execute("SELECT last_insert_rowid()")
        task_id = (await cursor.fetchone())[0]

    asyncio.create_task(schedule_all_checks(bot, task_id, creator_id, assignee_id, text, deadline, checkpoints_enabled))

# === КОМАНДЫ ===
@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот *Deadline* — помогаю ставить задачи и следить за их выполнением.\n\n"
        "Команды:\n"
        "/newtask — создать задачу\n"
        "/mytasks — ваши задачи"
    )

@router.message(Command("mytasks"))
async def my_tasks(message: Message):
    user_id = message.from_user.id
    async with aiosqlite.connect("deadline.db") as db:
        cursor = await db.execute(
            "SELECT id, text, deadline, status, creator_id FROM tasks WHERE (assignee_id = ? OR creator_id = ?) AND status IN ('pending', 'in_progress') ORDER BY deadline",
            (user_id, user_id)
        )
        rows = await cursor.fetchall()
    if not rows:
        await message.answer("📭 У вас нет активных задач.")
        return
    text = "📋 Ваши активные задачи:\n\n"
    for row in rows:
        _, t_text, deadline_str, _, creator_id = row
        deadline = datetime.fromisoformat(deadline_str)
        deadline_fmt = deadline.strftime("%d.%m %H:%M")
        role = "👤 Вы поставили" if creator_id == user_id else "🧑 Вам назначили"
        text += f"• {t_text}\n  📅 {deadline_fmt} | {role}\n\n"
    await message.answer(text, parse_mode=ParseMode.HTML)

@router.message(Command("newtask"))
async def new_task_start(message: Message, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Себе", callback_data="assign_to_self")
    builder.button(text="📨 Другому — перешлите его сообщение", callback_data="assign_to_other")
    builder.adjust(1)
    await message.answer("👥 Кому назначить задачу?", reply_markup=builder.as_markup())
    await state.set_state(TaskCreation.waiting_for_assignee)

# === ВЫБОР ИСПОЛНИТЕЛЯ ===
@router.callback_query(F.data == "assign_to_self")
async def assign_to_self(callback: CallbackQuery, state: FSMContext):
    await state.update_data(assignee_id=callback.from_user.id, assignee_name="вам")
    await callback.message.edit_text("📝 Напишите текст задачи:")
    await state.set_state(TaskCreation.waiting_for_text)
    await callback.answer()

@router.callback_query(F.data == "assign_to_other")
async def assign_to_other(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📨 Пожалуйста, **перешлите любое сообщение** от пользователя, которому хотите назначить задачу."
    )
    await state.set_state(TaskCreation.waiting_for_assignee)
    await callback.answer()

@router.message(TaskCreation.waiting_for_assignee, F.forward_from)
async def handle_forwarded_message(message: Message, state: FSMContext):
    forwarded_user = message.forward_from
    if not forwarded_user:
        await message.answer("❌ Не удалось определить пользователя. Перешлите сообщение от человека.")
        return
    if forwarded_user.is_bot:
        await message.answer("🚫 Нельзя назначать задачи ботам.")
        return

    # Проверка: можем ли писать?
    try:
        await message.bot.send_chat_action(forwarded_user.id, "typing")
    except Exception as e:
        if "blocked" in str(e):
            await message.answer("❌ Пользователь заблокировал бота.")
        elif "not found" in str(e):
            await message.answer("❌ Пользователь не разрешил сообщения от ботов.")
        else:
            await message.answer("❌ Не удалось назначить задачу.")
        return

    await state.update_data(
        assignee_id=forwarded_user.id,
        assignee_name=forwarded_user.full_name or f"@{forwarded_user.username}"
    )
    await message.answer("📝 Напишите текст задачи:")
    await state.set_state(TaskCreation.waiting_for_text)

@router.message(TaskCreation.waiting_for_assignee)
async def not_forwarded(message: Message):
    await message.answer("⚠️ Перешлите сообщение от пользователя.")

# === ВВОД ТЕКСТА И ВРЕМЕНИ ===
@router.message(TaskCreation.waiting_for_text)
async def process_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    kb = create_7day_calendar()
    await message.answer("📅 Выберите дату:", reply_markup=kb.as_markup())
    await state.set_state(TaskCreation.waiting_for_date)

@router.callback_query(F.data.startswith("select_date_"))
async def select_date(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data.split("_", 2)[2]
    await state.update_data(selected_date=date_str)
    builder = InlineKeyboardBuilder()
    for hour in range(24):
        builder.button(text=f"{hour:02d}:00", callback_data=f"select_hour_{hour}")
    builder.adjust(6)
    await callback.message.edit_text("🕗 Выберите час:", reply_markup=builder.as_markup())
    await state.set_state(TaskCreation.waiting_for_hour)
    await callback.answer()

@router.callback_query(F.data.startswith("select_hour_"))
async def select_hour(callback: CallbackQuery, state: FSMContext):
    hour = int(callback.data.split("_")[2])
    await state.update_data(selected_hour=hour)
    builder = InlineKeyboardBuilder()
    for minute in [0, 15, 30, 45]:
        builder.button(text=f":{minute:02d}", callback_data=f"select_minute_{minute}")
    builder.adjust(4)
    await callback.message.edit_text(f"🕗 Выбрано: {hour:02d} часов.\nВыберите минуты:", reply_markup=builder.as_markup())
    await state.set_state(TaskCreation.waiting_for_minute)
    await callback.answer()

@router.callback_query(F.data.startswith("select_minute_"))
async def select_minute(callback: CallbackQuery, state: FSMContext):
    minute = int(callback.data.split("_")[2])
    data = await state.get_data()
    date_part = data["selected_date"]
    hour = data["selected_hour"]
    deadline_str = f"{date_part} {hour:02d}:{minute:02d}"
    deadline = datetime.fromisoformat(deadline_str)
    
    if deadline <= datetime.now():
        await callback.message.edit_text("❌ Дедлайн не может быть в прошлом. Начните заново: /newtask")
        await state.clear()
        await callback.answer()
        return

    creator_id = callback.from_user.id
    assignee_id = data["assignee_id"]
    await save_task_and_schedule(callback.bot, creator_id, assignee_id, {
        "text": data["text"],
        "deadline": deadline.isoformat()
    })

    deadline_fmt = deadline.strftime("%d.%m в %H:%M")
    assignee_name = data["assignee_name"]
    await callback.message.edit_text(f"✅ Задача назначена {assignee_name}!\n📅 Дедлайн: {deadline_fmt}")

    # Уведомляем исполнителя
    if assignee_id != creator_id:
        try:
            await callback.bot.send_message(
                assignee_id,
                f"🔔 Вам назначена новая задача от {callback.from_user.full_name}:\n\n"
                f"«{data['text']}»\n"
                f"📅 Дедлайн: {deadline_fmt}"
            )
        except:
            pass

    await state.clear()
    await callback.answer()

# === ПЛАНИРОВЩИК ПРОВЕРОК ===
async def schedule_all_checks(bot: Bot, task_id: int, creator_id: int, assignee_id: int, task_text: str, deadline: datetime, checkpoints_enabled: bool):
    now = datetime.now()
    if deadline <= now:
        return
    total_seconds = (deadline - now).total_seconds()
    if checkpoints_enabled:
        delay_50 = total_seconds * 0.5
        asyncio.create_task(schedule_intermediate_check(bot, assignee_id, task_text, delay_50))
        delay_90 = total_seconds * 0.9
        asyncio.create_task(schedule_intermediate_check(bot, assignee_id, task_text, delay_90))
    delay_final = total_seconds
    asyncio.create_task(schedule_final_check(bot, task_id, creator_id, assignee_id, task_text, delay_final))

async def schedule_intermediate_check(bot: Bot, assignee_id: int, task_text: str, delay: float):
    await asyncio.sleep(delay)
    msg = f"🔄 Как продвигается задача?\n\n«{task_text}»"
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data="noop")
    kb.button(text="⏳ В процессе", callback_data="noop")
    kb.button(text="⚠️ Проблемы", callback_data="noop")
    kb.adjust(1)
    try:
        await bot.send_message(assignee_id, msg, reply_markup=kb.as_markup())
    except:
        pass

async def schedule_final_check(bot: Bot, task_id: int, creator_id: int, assignee_id: int, task_text: str, delay: float):
    await asyncio.sleep(delay)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выполнено", callback_data=f"done_{creator_id}")
    kb.button(text="❌ Не сделано", callback_data=f"notdone_{creator_id}")
    kb.adjust(1)
    try:
        await bot.send_message(assignee_id, f"⏰ Время вышло! Вы выполнили задачу?\n\n«{task_text}»", reply_markup=kb.as_markup())
    except:
        pass

@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer("Спасибо за обратную связь!", show_alert=True)

@router.callback_query(F.data.startswith("done_"))
async def task_done(callback: CallbackQuery):
    creator_id = int(callback.data.split("_")[1])
    await callback.message.edit_text("✅ Отметка о выполнении отправлена!")
    try:
        await callback.bot.send_message(creator_id, "🔔 Ваша задача отмечена как **выполненная**!")
    except:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("notdone_"))
async def task_not_done(callback: CallbackQuery):
    creator_id = int(callback.data.split("_")[1])
    await callback.message.edit_text("❌ Отметка о невыполнении отправлена.")
    try:
        await callback.bot.send_message(creator_id, "🔔 Ваша задача **не была выполнена** в срок.")
    except:
        pass
    await callback.answer()

# === ЗАПУСК ===
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
