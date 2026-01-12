# bot.py — версия 14: полностью на PostgreSQL (asyncpg)
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
import asyncpg
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL не задан в .env")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не задан в .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

class TaskCreation(StatesGroup):
    waiting_for_assignee = State()
    waiting_for_text = State()
    waiting_for_date = State()
    waiting_for_hour = State()
    waiting_for_minute = State()
    waiting_for_problem_description = State()

# === ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ===
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                full_name TEXT,
                username TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                creator_id BIGINT NOT NULL,
                assignee_id BIGINT NOT NULL,
                text TEXT NOT NULL,
                deadline TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                last_check_time TIMESTAMP,
                checkpoints_enabled BOOLEAN DEFAULT TRUE
            )
        """)
    finally:
        await conn.close()

async def get_db():
    return await asyncpg.connect(DATABASE_URL)

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
async def save_user(user):
    conn = await get_db()
    try:
        await conn.execute(
            """
            INSERT INTO users (user_id, full_name, username) 
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE 
            SET full_name = $2, username = $3
            """,
            user.id, user.full_name or "", user.username or ""
        )
    finally:
        await conn.close()

def format_name(user_id: int, full_name: str, username: str) -> str:
    if username:
        return f"@{username}"
    if full_name and full_name.strip():
        return full_name
    return f"Пользователь {user_id}"

async def get_frequent_assignees(creator_id: int):
    conn = await get_db()
    try:
        rows = await conn.fetch("""
            SELECT DISTINCT u.user_id, u.full_name, u.username
            FROM tasks t
            JOIN users u ON t.assignee_id = u.user_id
            WHERE t.creator_id = $1 AND u.user_id != $1
            ORDER BY t.created_at DESC
            LIMIT 10
        """, creator_id)
        return rows
    finally:
        await conn.close()

# === ОБРАБОТКА ПЕРЕСЫЛКИ ===
@router.message(F.forward_date)
async def handle_any_forward(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return

    await save_user(message.from_user)
    text = message.text or message.caption or "Без текста"
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Создать задачу", callback_data="quick_task_from_forward")
    builder.button(text="❌ Отмена", callback_data="ignore")
    builder.adjust(2)
    
    await message.answer(
        f"📩 Создать задачу из этого сообщения?\n\n«{text[:150]}{'...' if len(text) > 150 else ''}»",
        reply_markup=builder.as_markup()
    )
    await state.update_data(quick_task_text=text)

@router.callback_query(F.data == "quick_task_from_forward")
async def start_quick_task(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    quick_text = data.get("quick_task_text", "Задача из переписки")
    await state.update_data(text=quick_text, is_quick_task=True)
    
    creator_id = callback.from_user.id
    frequent = await get_frequent_assignees(creator_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Себе", callback_data="assign_to_self")
    if frequent:
        builder.button(text="— ⭐ Ранее назначали —", callback_data="ignore")
        for row in frequent:
            uid = row["user_id"]
            name = row["full_name"]
            uname = row["username"]
            label = format_name(uid, name, uname)
            builder.button(text=label[:25], callback_data=f"pick_user_{uid}")
    builder.button(text="📨 Другой пользователь", callback_data="assign_by_forward")
    builder.adjust(1)
    
    await callback.message.edit_text("👥 Кому назначить задачу?", reply_markup=builder.as_markup())
    await state.set_state(TaskCreation.waiting_for_assignee)
    await callback.answer()

@router.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery):
    await callback.answer()

# === УНИВЕРСАЛЬНЫЙ ПЕРЕХОД ПОСЛЕ ВЫБОРА ИСПОЛНИТЕЛЯ ===
async def proceed_after_assignee(callback_or_message, state: FSMContext):
    data = await state.get_data()
    is_quick = data.get("is_quick_task", False)
    
    if is_quick:
        kb = create_7day_calendar()
        if isinstance(callback_or_message, CallbackQuery):
            await callback_or_message.message.edit_text("📅 Выберите дату:", reply_markup=kb.as_markup())
            await callback_or_message.answer()
        else:
            await callback_or_message.answer("📅 Выберите дату:", reply_markup=kb.as_markup())
        await state.set_state(TaskCreation.waiting_for_date)
    else:
        if isinstance(callback_or_message, CallbackQuery):
            await callback_or_message.message.edit_text("📝 Напишите текст задачи:")
            await callback_or_message.answer()
        else:
            await callback_or_message.answer("📝 Напишите текст задачи:")
        await state.set_state(TaskCreation.waiting_for_text)

# === ОСНОВНЫЕ КОМАНДЫ ===
@router.message(Command("start"))
async def cmd_start(message: Message):
    await save_user(message.from_user)
    await message.answer(
        "👋 Привет! Я бот *Deadline* — помогаю ставить задачи и следить за их выполнением.\n\n"
        "Команды:\n"
        "/newtask — создать задачу\n"
        "/mytasks — ваши задачи"
    )

@router.message(Command("mytasks"))
async def my_tasks(message: Message):
    await save_user(message.from_user)
    user_id = message.from_user.id
    conn = await get_db()
    try:
        rows = await conn.fetch(
            """
            SELECT id, text, deadline, status, creator_id 
            FROM tasks 
            WHERE (assignee_id = $1 OR creator_id = $1) 
              AND status IN ('pending', 'in_progress') 
            ORDER BY deadline
            """,
            user_id
        )
    finally:
        await conn.close()

    if not rows:
        await message.answer("📭 У вас нет активных задач.")
        return

    text = "📋 Ваши активные задачи:\n\n"
    for row in rows:
        t_text = row["text"]
        deadline = row["deadline"]
        creator_id = row["creator_id"]
        deadline_fmt = deadline.strftime("%d.%m %H:%M")
        role = "👤 Вы поставили" if creator_id == user_id else "🧑 Вам назначили"
        text += f"• {t_text}\n  📅 {deadline_fmt} | {role}\n\n"
    await message.answer(text, parse_mode=ParseMode.HTML)

@router.message(Command("newtask"))
async def new_task_start(message: Message, state: FSMContext):
    await save_user(message.from_user)
    creator_id = message.from_user.id
    frequent = await get_frequent_assignees(creator_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Себе", callback_data="assign_to_self")
    if frequent:
        builder.button(text="— ⭐ Ранее назначали —", callback_data="ignore")
        for row in frequent:
            uid = row["user_id"]
            name = row["full_name"]
            uname = row["username"]
            label = format_name(uid, name, uname)
            builder.button(text=label[:25], callback_data=f"pick_user_{uid}")
    builder.button(text="📨 Другой пользователь", callback_data="assign_by_forward")
    builder.adjust(1)
    
    await message.answer("👥 Кому назначить задачу?", reply_markup=builder.as_markup())
    await state.set_state(TaskCreation.waiting_for_assignee)

# === ВЫБОР ИСПОЛНИТЕЛЯ ===
@router.callback_query(F.data == "assign_to_self")
async def assign_to_self(callback: CallbackQuery, state: FSMContext):
    await state.update_data(assignee_id=callback.from_user.id, assignee_name="вам")
    await proceed_after_assignee(callback, state)

@router.callback_query(F.data.startswith("pick_user_"))
async def pick_user(callback: CallbackQuery, state: FSMContext):
    assignee_id = int(callback.data.split("_")[2])
    conn = await get_db()
    try:
        row = await conn.fetchrow(
            "SELECT user_id, full_name, username FROM users WHERE user_id = $1",
            assignee_id
        )
    finally:
        await conn.close()
        
    if not row:
        await callback.message.edit_text("❌ Пользователь не найден.")
        await state.clear()
        return

    uid = row["user_id"]
    full_name = row["full_name"]
    username = row["username"]
    assignee_name = format_name(uid, full_name, username)
    await state.update_data(assignee_id=assignee_id, assignee_name=assignee_name)
    await proceed_after_assignee(callback, state)

@router.callback_query(F.data == "assign_by_forward")
async def assign_by_forward(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📨 Перешлите любое сообщение от пользователя.")
    await state.set_state(TaskCreation.waiting_for_assignee)
    await callback.answer()

@router.message(TaskCreation.waiting_for_assignee, F.forward_date)
async def handle_forwarded(message: Message, state: FSMContext):
    if message.forward_from:
        user = message.forward_from
    elif message.forward_sender_name:
        await message.answer("❌ Невозможно определить пользователя. Перешлите из обычного чата.")
        return
    else:
        await message.answer("❌ Не удалось определить пользователя.")
        return

    if user.is_bot:
        await message.answer("🚫 Нельзя назначать задачи ботам.")
        return

    try:
        await message.bot.send_chat_action(user.id, "typing")
    except:
        await message.answer("❌ Не могу отправить сообщение этому пользователю.")
        return

    await save_user(user)
    name = format_name(user.id, user.full_name or "", user.username or "")
    await state.update_data(assignee_id=user.id, assignee_name=name)
    await proceed_after_assignee(message, state)

@router.message(TaskCreation.waiting_for_assignee)
async def not_forwarded(message: Message):
    await message.answer("⚠️ Пожалуйста, перешлите сообщение от пользователя.")

# === ВВОД ТЕКСТА И ВРЕМЕНИ ===
@router.message(TaskCreation.waiting_for_text)
async def process_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    kb = create_7day_calendar()
    await message.answer("📅 Выберите дату:", reply_markup=kb.as_markup())
    await state.set_state(TaskCreation.waiting_for_date)

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
    try:
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
        text = data["text"]
        deadline_iso = deadline.isoformat()
        duration = (deadline - datetime.now()).total_seconds()
        checkpoints_enabled = duration > 600

        conn = await get_db()
        try:
            task_id = await conn.fetchval(
                """
                INSERT INTO tasks (creator_id, assignee_id, text, deadline, checkpoints_enabled)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                creator_id, assignee_id, text, deadline_iso, checkpoints_enabled
            )
        finally:
            await conn.close()

        asyncio.create_task(schedule_all_checks(callback.bot, task_id, creator_id, assignee_id, text, deadline, checkpoints_enabled))

        deadline_fmt = deadline.strftime("%d.%m в %H:%M")
        assignee_name = data["assignee_name"]
        await callback.message.edit_text(f"✅ Задача назначена {assignee_name}!\n📅 Дедлайн: {deadline_fmt}")

        if assignee_id != creator_id:
            try:
                await callback.bot.send_message(
                    assignee_id,
                    f"🔔 Вам назначена новая задача от {callback.from_user.full_name}:\n\n"
                    f"«{text}»\n"
                    f"📅 Дедлайн: {deadline_fmt}"
                )
            except:
                pass

        await state.clear()
        await callback.answer()

    except Exception as e:
        print(f"[ERROR] {e}")
        await callback.message.edit_text("⚠️ Произошла ошибка. Попробуйте снова.")
        await state.clear()
        await callback.answer()

# === ПЛАНИРОВЩИК ===
async def schedule_all_checks(bot: Bot, task_id: int, creator_id: int, assignee_id: int, task_text: str, deadline: datetime, checkpoints_enabled: bool):
    now = datetime.now()
    if deadline <= now:
        return
    total_seconds = (deadline - now).total_seconds()
    if checkpoints_enabled:
        delay_50 = total_seconds * 0.5
        asyncio.create_task(schedule_intermediate_check(bot, task_id, creator_id, assignee_id, task_text, delay_50))
        delay_90 = total_seconds * 0.9
        asyncio.create_task(schedule_intermediate_check(bot, task_id, creator_id, assignee_id, task_text, delay_90))
    delay_final = total_seconds
    asyncio.create_task(schedule_final_check(bot, task_id, creator_id, assignee_id, task_text, delay_final))

async def schedule_intermediate_check(bot: Bot, task_id: int, creator_id: int, assignee_id: int, task_text: str, delay: float):
    await asyncio.sleep(delay)
    msg = f"🔄 Как продвигается задача?\n\n«{task_text}»"
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=f"interim_done_{task_id}_{creator_id}")
    kb.button(text="⏳ В процессе", callback_data=f"interim_ok_{task_id}")
    kb.button(text="⚠️ Проблемы", callback_data=f"interim_problem_{task_id}_{creator_id}")
    kb.adjust(1)
    try:
        await bot.send_message(assignee_id, msg, reply_markup=kb.as_markup())
    except:
        pass

async def schedule_final_check(bot: Bot, task_id: int, creator_id: int, assignee_id: int, task_text: str, delay: float):
    await asyncio.sleep(delay)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выполнено", callback_data=f"done_{task_id}_{creator_id}")
    kb.button(text="❌ Не сделано", callback_data=f"notdone_{task_id}_{creator_id}")
    kb.adjust(1)
    try:
        await bot.send_message(assignee_id, f"⏰ Время вышло! Вы выполнили задачу?\n\n«{task_text}»", reply_markup=kb.as_markup())
    except:
        pass

# === ОБРАБОТКА КНОПОК ===
@router.callback_query(F.data.startswith("interim_done_"))
async def interim_done(callback: CallbackQuery):
    parts = callback.data.split("_")
    task_id = int(parts[2])
    creator_id = int(parts[3])
    conn = await get_db()
    try:
        await conn.execute("UPDATE tasks SET status = 'done' WHERE id = $1", task_id)
    finally:
        await conn.close()
    await callback.message.edit_text("✅ Задача завершена досрочно!")
    try:
        await callback.bot.send_message(creator_id, "🔔 Исполнитель завершил задачу раньше срока!")
    except:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("interim_ok_"))
async def interim_ok(callback: CallbackQuery):
    await callback.message.edit_text("👍 Молодец! Времени ещё достаточно.")
    await callback.answer()

@router.callback_query(F.data.startswith("interim_problem_"))
async def interim_problem(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    task_id = int(parts[2])
    creator_id = int(parts[3])
    await state.update_data(problem_task_id=task_id, problem_creator_id=creator_id)
    await callback.message.edit_text("🔧 Опишите проблему:")
    await state.set_state(TaskCreation.waiting_for_problem_description)
    await callback.answer()

@router.message(TaskCreation.waiting_for_problem_description)
async def handle_problem_description(message: Message, state: FSMContext):
    data = await state.get_data()
    creator_id = data["problem_creator_id"]
    problem_text = message.text
    try:
        await message.bot.send_message(
            creator_id,
            f"⚠️ У исполнителя возникла проблема с задачей:\n\n«{problem_text}»"
        )
    except:
        pass
    await message.answer("📤 Проблема отправлена заказчику.")
    await state.clear()

@router.callback_query(F.data.startswith("done_"))
async def task_done(callback: CallbackQuery):
    parts = callback.data.split("_")
    task_id = int(parts[1])
    creator_id = int(parts[2])
    conn = await get_db()
    try:
        await conn.execute("UPDATE tasks SET status = 'done' WHERE id = $1", task_id)
    finally:
        await conn.close()
    await callback.message.edit_text("✅ Задача выполнена!")
    try:
        await callback.bot.send_message(creator_id, "🔔 Задача отмечена как **выполненная**!")
    except:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("notdone_"))
async def task_not_done(callback: CallbackQuery):
    parts = callback.data.split("_")
    task_id = int(parts[1])
    creator_id = int(parts[2])
    conn = await get_db()
    try:
        await conn.execute("UPDATE tasks SET status = 'failed' WHERE id = $1", task_id)
    finally:
        await conn.close()
    await callback.message.edit_text("❌ Задача не выполнена в срок.")
    try:
        await callback.bot.send_message(creator_id, "🔔 Задача **не была выполнена** в срок.")
    except:
        pass
    await callback.answer()

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

