import asyncio
import logging
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")

bot = Bot(token=TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# Функция опроса
async def send_squash_poll(day_name: str, extra_options: list = None):
    options = ["Да", "Нет", "Резерв"]
    if extra_options:
        options.extend(extra_options)
    options.append("Тренер")

    try:
        await bot.send_poll(
            chat_id=GROUP_ID,
            question=f"📊 Сквош в {day_name}?",
            options=options,
            is_anonymous=False
        )
        logging.info(f"Опрос на {day_name} отправлен")
    except Exception as e:
        logging.error(f"Ошибка при отправке опроса ({day_name}): {e}")

async def send_reminder():
    try:
        await bot.send_message(chat_id=GROUP_ID, text="🔔 Голосование через 5 минут!")
    except Exception as e:
        logging.error(f"Ошибка напоминания: {e}")

# Задачи
def setup_scheduler():
    # ПН (отправка в пятницу)
    scheduler.add_job(send_squash_poll, 'cron', day_of_week='fri', hour=18, minute=0, args=['пн'])

    # ЧТ (отправка во вторник) + доп. опция
    scheduler.add_job(send_squash_poll, 'cron', day_of_week='tue', hour=18, minute=00, args=['чт', ['Резерв, я был(а) в пн']])

    # Напоминалка за 5 минут до опроса
    scheduler.add_job(send_reminder, 'cron', day_of_week='tue,fri', hour=17, minute=55)

async def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    setup_scheduler()
    scheduler.start()

    try:
        logging.info("Бот запущен...")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен")