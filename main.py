import os
import logging
import asyncio
import aiohttp
import shutil
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    InputMediaPhoto
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from supabase import create_client, Client
from dotenv import load_dotenv
from uuid import uuid4

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
UPLOAD_BUCKET = "uploads"
UPLOAD_DIR = "uploads"

# Инициализация Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
buckets = supabase.storage.list_buckets()
if UPLOAD_BUCKET not in [b.name for b in buckets]:
    supabase.storage.create_bucket(UPLOAD_BUCKET)

# Инициализация бота
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())

# Функция для загрузки файла в Supabase
async def upload_file_to_supabase(local_path: str, dest_path: str) -> bool:
    try:
        with open(local_path, "rb") as f:
            data = f.read()
        supabase.storage.from_(UPLOAD_BUCKET).upload(dest_path, data, {"upsert": True})
        logger.info(f"Uploaded to Supabase: {dest_path}")
        return True
    except Exception as e:
        logger.error(f"Supabase upload error: {e}")
        return False

# Обработка команды /start
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Привет! Отправь мне 2 фото: одежду и человека. Фото будут загружены в Supabase.")

# Обработка загруженного фото
@dp.message(F.photo)
async def handle_photo(message: Message):
    user_id = message.from_user.id
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)

    existing_photos = [f for f in os.listdir(user_dir) if f.endswith(('.jpg', '.jpeg', '.png', '.webp'))]
    photo_num = len(existing_photos) + 1

    file_name = f"photo_{photo_num}.jpg"
    local_path = os.path.join(user_dir, file_name)
    dest_path = f"{user_id}/{file_name}"

    await message.photo[-1].download(destination_file=local_path)
    success = await upload_file_to_supabase(local_path, dest_path)

    if success:
        await message.answer(f"✅ Фото {photo_num} получено и загружено.")
    else:
        await message.answer("❌ Ошибка загрузки фото. Попробуй ещё раз.")

# Запуск бота
async def main():
    logger.info("Starting bot...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
