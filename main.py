import asyncio
import logging
import os
import json
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, BotCommand
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

def get_required_env(var_name: str) -> str:
    """Получить требуемую переменную окружения, бросить ошибку если она пустая."""
    value = os.getenv(var_name, "").strip()
    if not value:
        raise ValueError(f"Переменная окружения {var_name} не задана")
    return value


def parse_allowed_user_ids(raw_value: str) -> set[int]:
    try:
        user_ids = {int(user_id.strip()) for user_id in raw_value.split(",") if user_id.strip()}
    except ValueError as exc:
        raise ValueError("ALLOWED_USER_ID должен содержать только числовые Telegram ID через запятую") from exc
    if not user_ids:
        raise ValueError("ALLOWED_USER_ID должен содержать хотя бы один Telegram ID")
    return user_ids


# --- Proxy
def get_proxy_url_for_telegram() -> str | None:
    # Если переменная NO_PROXY определена (даже пустая), прокси не используем
    if "NO_PROXY" in os.environ:
        logging.info("NO_PROXY отключает прокси для Telegram")
        return None

    # Иначе берём значение HTTPS_PROXY
    proxy = os.getenv("HTTPS_PROXY", "").strip()
    return proxy or None


# --- Запуск бота и загрузка настроек окружения
load_dotenv()
TOKEN = get_required_env("BOT_TOKEN")
GROUP_ID = int(get_required_env("GROUP_ID"))
ALLOWED_USER_IDS = parse_allowed_user_ids(get_required_env("ALLOWED_USER_ID"))

_proxy_url = get_proxy_url_for_telegram()
if _proxy_url:
    # AiohttpSession(proxy=...) требует aiohttp-socks.
    try:
        bot_session = AiohttpSession(proxy=_proxy_url)
    except Exception as exc:
        logging.warning("Прокси игнорируется: %s", exc)
        bot_session = AiohttpSession()
else:
    bot_session = AiohttpSession()

bot = Bot(token=TOKEN, session=bot_session)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# --- Основные константы и текстовые шаблоны
CONFIG_FILE = "schedule_config.json"
MINUTES_OPTIONS = list(range(0, 60, 5))
HOURS_RANGE = range(10, 22)
SELECT_SEND_DAY_TEXT = "📅 Выберите день отправки опроса:"
MAIN_COMMANDS_TEXT = (
    "📋 Доступные команды:\n"
    "/set_days — добавить опрос\n"
    "/list_days — посмотреть расписание\n"
    "/remove_days — удалить опрос из расписания"
)
DAY_NAMES = {
    "mon": "Понедельник", "tue": "Вторник", "wed": "Среда",
    "thu": "Четверг", "fri": "Пятница", "sat": "Суббота", "sun": "Воскресенье"
}

# --- Состояния диалога (FSM), через которые проходит пользователь
class ScheduleStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_options = State()

# --- Middleware, который пускает к боту только разрешённых пользователей
class AccessMiddleware:
    async def __call__(self, handler, event, data):
        user_id = getattr(event.from_user, "id", None)
        if user_id and user_id not in ALLOWED_USER_IDS:
            msg = "❌ У вас нет доступа к этому боту."
            if isinstance(event, CallbackQuery):
                await event.answer(msg, show_alert=True)
            else:
                await event.answer(msg)
            return
        return await handler(event, data)

dp.message.middleware(AccessMiddleware())
dp.callback_query.middleware(AccessMiddleware())

EMPTY_CONFIG = {"schedules": []}


# --- Работа с файлом расписания: чтение, сохранение и обновление
def load_config():
    """Загрузить расписания из schedule_config.json, вернуть пусто если файл отсутствует."""
    if not os.path.exists(CONFIG_FILE):
        return EMPTY_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.warning("Не удалось прочитать %s: %s", CONFIG_FILE, e)
        return EMPTY_CONFIG.copy()

def save_config(cfg):
    """Сохранить конфигурацию расписаний в JSON-файл."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def update_config(schedule_id, data=None):
    cfg = load_config()
    cfg["schedules"] = [s for s in cfg["schedules"] if s["id"] != schedule_id]
    if data:
        cfg["schedules"].append(data)
    save_config(cfg)
    setup_scheduler()

# --- Настройка планировщика задач (когда отправлять опросы и напоминания)
def calculate_reminder(hour, minute):
    """Рассчитать время напоминания (за 5 минут до основного события)."""
    return (hour - (minute < 5)) % 24, (minute - 5) % 60

def setup_scheduler():
    """Перестроить все задачи планировщика на основе текущей конфигурации."""
    scheduler.remove_all_jobs()
    cfg = load_config()

    for s in cfg["schedules"]:
        poll_options = s.get("options") or s.get("extra_options")
        poll_title = s.get("poll_title")
        scheduler.add_job(
            send_squash_poll,
            "cron",
            day_of_week=s["send_day"],
            hour=s["hour"],
            minute=s["minute"],
            args=[s["poll_day"], poll_options, poll_title],
            id=f"poll_{s['id']}",
            replace_existing=True
        )

        rh, rm = calculate_reminder(s["hour"], s["minute"])
        scheduler.add_job(
            send_reminder,
            "cron",
            day_of_week=s["send_day"],
            hour=rh,
            minute=rm,
            id=f"rem_{s['id']}",
            replace_existing=True
        )

# --- Что именно делает бот по расписанию (опросы и напоминания)
async def send_squash_poll(day_name, options, poll_title=None):
    """Отправить опрос в группу (только если минимум 2 варианта)."""
    if not options or len(options) < 2:
        logging.warning("Опрос '%s' пропущен: требуется минимум 2 варианта", day_name)
        return
    question = poll_title or f"📊 Сквош в {day_name}?"
    await bot.send_poll(
        chat_id=GROUP_ID,
        question=question,
        options=options,
        is_anonymous=False
    )

async def send_reminder():
    """Отправить напоминание за 5 минут до опроса."""
    await bot.send_message(GROUP_ID, "🔔 Голосование через 5 минут!")

# --- Вспомогательные функции интерфейса: клавиатуры и тексты для сообщений
def send_day_selected_text(send_day):
    """Сформировать текст после выбора дня отправки."""
    return f"✅ Вы выбрали: {DAY_NAMES[send_day]}\nТеперь выберите день тренировки:"


def kb_days(prefix, back=None):
    """Собрать inline-клавиатуру со всеми днями недели и кнопкой возврата."""
    kb = InlineKeyboardBuilder()
    for code, name in DAY_NAMES.items():
        kb.row(InlineKeyboardButton(text=name, callback_data=f"{prefix}_{code}"))
    if back:
        kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data=back))
    return kb.as_markup()

def kb_poll_days(send_day):
    """Собрать inline-клавиатуру со всеми днями для выбора дня тренировки."""
    kb = InlineKeyboardBuilder()
    for name in DAY_NAMES.values():
        kb.row(InlineKeyboardButton(text=name, callback_data=f"poll_{send_day}_{name}"))
    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data="back_send"))
    return kb.as_markup()

def kb_time(send_day, poll_day, hour=None):
    """Собрать inline-клавиатуру часов (если hour=None) или минут (если hour задан)."""
    kb = InlineKeyboardBuilder()
    if hour is None:
        for h in HOURS_RANGE:
            kb.add(InlineKeyboardButton(text=str(h), callback_data=f"hour_{send_day}_{poll_day}_{h}"))
        kb.adjust(6)
    else:
        for m in MINUTES_OPTIONS:
            kb.add(InlineKeyboardButton(text=f"{m:02d}", callback_data=f"time_{send_day}_{poll_day}_{hour}_{m}"))
        kb.adjust(4)
        kb.row(InlineKeyboardButton(text="🔄 Изменить час", callback_data=f"hour_{send_day}_{poll_day}"))

    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"back_poll_{send_day}"))
    return kb.as_markup()

def kb_back_to_time(send_day, poll_day, hour):
    """Собрать минимальную клавиатуру только с кнопкой возврата к выбору времени."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="◀️ Назад",
        callback_data=f"back_time_{send_day}_{poll_day}_{hour}"
    ))
    return kb.as_markup()

def kb_back_to_title():
    """Собрать минимальную клавиатуру только с кнопкой возврата к названию опроса."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data="back_title_input"))
    return kb.as_markup()

def _hour_selected_text(send_day, poll_day, hour):
    return (
        f"✅ День отправки: {DAY_NAMES[send_day]}\n"
        f"✅ День тренировки: {poll_day}\n"
        f"✅ Час отправки: {hour}\n"
        "⏰ Выберите минуты:"
    )

# --- Обработчики текстовых команд Telegram (/start, /set_days и т.д.)
@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "👋 Привет! Я бот по управлению опросами для группы SH Cквош Здоровье.\n\n"
        f"{MAIN_COMMANDS_TEXT}"
    )

@dp.message(Command("set_days"))
async def set_days(message: Message):
    """Начать диалог создания нового расписания с выбора дня отправки."""
    await message.answer(SELECT_SEND_DAY_TEXT, reply_markup=kb_days("send"))

@dp.message(Command("list_days"))
async def list_days(message: Message):
    """Показать все активные расписания в человекочитаемом формате."""
    cfg = load_config()
    if not cfg["schedules"]:
        return await message.answer("📭 Нет активных расписаний.")

    rows = ["📋 Текущие расписания:"]
    for s in cfg["schedules"]:
        poll_title = s.get("poll_title") or f"📊 Сквош в {s['poll_day']}?"
        opts = s.get("options") or s.get("extra_options") or []
        opts_str = ", ".join(opts) if opts else "не заданы"
        rows.append(
            f"📅 День тренировки: {s['poll_day']}\n"
            f"🏷️ Название опроса: {poll_title}\n"
            f"📤 Отправка: {DAY_NAMES[s['send_day']]} в {s['hour']:02d}:{s['minute']:02d}\n"
            f"📝 Варианты: {opts_str}\n"
        )
    await message.answer("\n".join(rows))

@dp.message(Command("remove_days"))
async def remove_days(message: Message):
    """Показать список расписаний с кнопками для удаления конкретного."""
    cfg = load_config()
    if not cfg["schedules"]:
        return await message.answer("📭 Нет расписаний.")

    kb = InlineKeyboardBuilder()
    for s in cfg["schedules"]:
        kb.row(InlineKeyboardButton(
            text=f"{DAY_NAMES[s['send_day']]} → {s['poll_day']}",
            callback_data=f"del_{s['id']}"
        ))
    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data="back_main"))
    await message.answer("🗑️ Выберите расписание:", reply_markup=kb.as_markup())

# --- Обработчики нажатий на inline-кнопки (callback-запросы)
@dp.callback_query(F.data == "back_send")
async def back_to_send_day(callback: CallbackQuery):
    await callback.message.edit_text(SELECT_SEND_DAY_TEXT, reply_markup=kb_days("send"))
    await callback.answer()

@dp.callback_query(F.data.startswith("back_poll_"))
async def back_to_poll_day(callback: CallbackQuery):
    send_day = callback.data.split("_", 2)[2]
    await callback.message.edit_text(
        send_day_selected_text(send_day),
        reply_markup=kb_poll_days(send_day)
    )
    await callback.answer()

@dp.callback_query(F.data == "back_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text(MAIN_COMMANDS_TEXT)
    await callback.answer()

@dp.callback_query(F.data.startswith("send_"))
async def choose_send(callback: CallbackQuery):
    send_day = callback.data.split("_", 1)[1]
    await callback.message.edit_text(
        send_day_selected_text(send_day),
        reply_markup=kb_poll_days(send_day)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("poll_"))
async def choose_poll(callback: CallbackQuery):
    _, send_day, poll_day = callback.data.split("_", 2)
    await callback.message.edit_text(
        f"✅ День отправки: {DAY_NAMES[send_day]}\n✅ День тренировки: {poll_day}\n⏰ Выберите час:",
        reply_markup=kb_time(send_day, poll_day)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("hour_"))
async def choose_hour(callback: CallbackQuery):
    parts = callback.data.split("_")
    send_day, poll_day = parts[1], parts[2]
    hour = int(parts[3]) if len(parts) > 3 else None
    await callback.message.edit_text(
        _hour_selected_text(send_day, poll_day, hour),
        reply_markup=kb_time(send_day, poll_day, hour)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("time_"))
async def choose_time(callback: CallbackQuery, state: FSMContext):
    _, send_day, poll_day, hour, minute = callback.data.split("_")
    await state.update_data(
        send_day=send_day, poll_day=poll_day, hour=int(hour), minute=int(minute),
        edit_chat_id=callback.message.chat.id,
        edit_message_id=callback.message.message_id,
    )
    await state.set_state(ScheduleStates.waiting_for_title)
    await callback.message.edit_text(
        f"✅ День отправки: {DAY_NAMES[send_day]}\n✅ День тренировки: {poll_day}\n✅ Время отправки {int(hour):02d}:{int(minute):02d}\n"
        "🏷️ Введите название опроса.\n"
        "\n"
        "Пример: 📊 Сквош в четверг?",
        parse_mode="HTML",
        reply_markup=kb_back_to_time(send_day, poll_day, hour)
    )
    await callback.answer()

@dp.message(ScheduleStates.waiting_for_title)
async def title_input(message: Message, state: FSMContext):
    poll_title = (message.text or "").strip()
    if not poll_title or poll_title == "-":
        return await message.answer("❗ Название опроса обязательно. Введите название.")

    data = await state.get_data()
    await state.update_data(poll_title=poll_title)
    await state.set_state(ScheduleStates.waiting_for_options)

    options_text = (
        "📝 Введите варианты опроса через ';' (минимум 2).\n"
        "\n"
        "Пример: Да; Нет; Резерв; Резерв, я был(а) в пн; Тренер"
    )
    try:
        await bot.edit_message_text(
            chat_id=data["edit_chat_id"],
            message_id=data["edit_message_id"],
            text=options_text,
            parse_mode="HTML",
            reply_markup=kb_back_to_title(),
        )
    except Exception:
        await message.answer(options_text, parse_mode="HTML", reply_markup=kb_back_to_title())

@dp.callback_query(F.data.startswith("back_time_"))
async def back_to_time(callback: CallbackQuery, state: FSMContext):
    _, _, send_day, poll_day, hour = callback.data.split("_", 4)
    await state.clear()
    hour_int = int(hour)
    await callback.message.edit_text(
        _hour_selected_text(send_day, poll_day, hour_int),
        reply_markup=kb_time(send_day, poll_day, hour_int)
    )
    await callback.answer()

@dp.callback_query(F.data == "back_title_input")
async def back_to_title_input(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    send_day = data.get("send_day")
    poll_day = data.get("poll_day")
    hour = data.get("hour")
    minute = data.get("minute")

    if not send_day or not poll_day or hour is None or minute is None:
        await state.clear()
        await callback.message.edit_text(SELECT_SEND_DAY_TEXT, reply_markup=kb_days("send"))
        await callback.answer()
        return

    await state.update_data(
        edit_chat_id=callback.message.chat.id,
        edit_message_id=callback.message.message_id,
    )
    await state.set_state(ScheduleStates.waiting_for_title)
    await callback.message.edit_text(
         f"✅ День отправки: {DAY_NAMES[send_day]}\n✅ День тренировки: {poll_day}\n✅ Время отправки {int(hour):02d}:{int(minute):02d}\n"
        "🏷️ Введите название опроса.\n"
        "\n"
        "Пример: 📊 Сквош в четверг?",
        reply_markup=kb_back_to_time(send_day, poll_day, hour)
    )
    await callback.answer()

@dp.message(ScheduleStates.waiting_for_options)
async def options_input(message: Message, state: FSMContext):
    options = [o.strip() for o in (message.text or "").split(";") if o.strip()]
    if len(options) < 2:
        return await message.answer("❗ Нужно минимум 2 варианта. Введите снова через ';'")

    data = await state.get_data()
    schedule_id = f"{data['send_day']}_{data['poll_day']}"

    update_config(schedule_id, {
        "id": schedule_id,
        **data,
        "options": options
    })

    send_day_name = DAY_NAMES.get(data["send_day"], data["send_day"])
    text = (
        f"✅ Задание создано: новый опрос добавлен для группы SH Сквош Здоровье!\n\n"
        f"📅 День тренировки: {data['poll_day']}\n"
        f"📤 День отправки: {send_day_name}\n"
        f"⏰ Время отправки: {data['hour']:02d}:{data['minute']:02d}\n"
    )

    await state.clear()
    await message.answer(text)

@dp.callback_query(F.data.startswith("del_"))
async def delete_schedule(callback: CallbackQuery):
    schedule_id = callback.data[4:]
    update_config(schedule_id, None)
    await callback.message.edit_text("🗑️ Расписание удалено!")
    await callback.answer()

async def main():
    logging.basicConfig(level=logging.INFO)
    setup_scheduler()
    scheduler.start()
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Запуск"),
            BotCommand(command="set_days", description="Добавить опрос"),
            BotCommand(command="list_days", description="Посмотреть расписание"),
            BotCommand(command="remove_days", description="Удалить из расписания"),
        ])
        await dp.start_polling(bot)
    finally:
        # Корректное закрытие HTTP-сессии и остановка планировщика.
        await bot.session.close()
        scheduler.shutdown(wait=False)

if __name__ == "__main__":
    asyncio.run(main())
