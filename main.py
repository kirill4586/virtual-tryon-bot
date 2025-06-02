import os
import time
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
BASEROW_TOKEN = os.getenv("BASEROW_TOKEN")
TABLE_ID = int(os.getenv("TABLE_ID"))
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
YMONEY_TOKEN = os.getenv("YMONEY_TOKEN")
YMONEY_WALLET = os.getenv("YMONEY_WALLET")
PRICE_PER_TRY = 30
FREE_USERS = {6320348591, 973853935}
UPLOAD_DIR = "uploads"
UPLOADS_BUCKET = "uploads"  # Бакет для пользовательских загрузок
MODELS_BUCKET = "models"
EXAMPLES_BUCKET = "primery"
SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
MODELS_PER_PAGE = 3
EXAMPLES_PER_PAGE = 3

# Инициализация клиентов
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Инициализация Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Supabase client initialized successfully")
    
    # Создаем необходимые бакеты, если их нет
    existing_buckets = [b.name for b in supabase.storage.list_buckets()]
    
    for bucket in [UPLOADS_BUCKET, MODELS_BUCKET, EXAMPLES_BUCKET]:
        if bucket not in existing_buckets:
            supabase.storage.create_bucket(bucket)
            logger.info(f"Created bucket: {bucket}")
    
except Exception as e:
    logger.error(f"Failed to initialize Supabase client: {e}")
    supabase = None

async def upload_to_supabase(file_path: str, user_id: int, file_type: str) -> str:
    """Загружает файл в Supabase Storage и возвращает URL"""
    try:
        file_name = f"{file_type}_{int(time.time())}{os.path.splitext(file_path)[1]}"
        remote_path = f"{user_id}/{file_name}"
        
        with open(file_path, 'rb') as f:
            supabase.storage.from_(UPLOADS_BUCKET).upload(
                path=remote_path,
                file=f
            )
        
        # Получаем публичный URL
        url = supabase.storage.from_(UPLOADS_BUCKET).get_public_url(remote_path)
        
        # Сохраняем информацию о файле в Baserow
        await baserow.upsert_row(user_id, "", {
            f"{file_type}_path": remote_path,
            f"{file_type}_url": url
        })
        
        return url
        
    except Exception as e:
        logger.error(f"Error uploading to Supabase: {e}")
        raise

async def download_from_supabase(remote_path: str, local_path: str):
    """Скачивает файл из Supabase Storage"""
    try:
        with open(local_path, 'wb') as f:
            res = supabase.storage.from_(UPLOADS_BUCKET).download(remote_path)
            f.write(res)
    except Exception as e:
        logger.error(f"Error downloading from Supabase: {e}")
        raise

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user = message.from_user
    user_id = user.id
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    try:
        # Проверка бесплатного доступа
        if user_id not in FREE_USERS:
            tries_left = await get_user_tries(user_id)
            if tries_left <= 0:
                await request_payment(user_id)
                return
        
        # Определяем тип фото (одежда или человек)
        existing_photos = [f for f in os.listdir(user_dir) if f.startswith("photo_")]
        photo_type = "clothes" if len(existing_photos) == 0 else "person"
        file_prefix = f"photo_{len(existing_photos)+1}"
        
        # Сохраняем локально
        photo = message.photo[-1]
        file_ext = os.path.splitext(photo.file_id)[1] or '.jpg'
        local_path = os.path.join(user_dir, f"{file_prefix}{file_ext}")
        await bot.download(photo, destination=local_path)
        
        # Загружаем в Supabase
        file_url = await upload_to_supabase(local_path, user_id, photo_type)
        
        # Обновляем статус
        if photo_type == "clothes":
            await baserow.upsert_row(user_id, user.username, {
                "photo_clothes": True,
                "status": "Ожидается фото человека/модели"
            })
            await message.answer("✅ Фото одежды сохранено!\nТеперь отправьте фото человека или выберите модель.")
        else:
            if user_id not in FREE_USERS:
                await update_user_tries(user_id, await get_user_tries(user_id) - 1)
            
            await baserow.upsert_row(user_id, user.username, {
                "photo_person": True,
                "status": "В обработке"
            })
            await message.answer("✅ Оба фото получены! Идет обработка...")
            
    except Exception as e:
        logger.error(f"Photo handling error: {e}")
        await message.answer("❌ Ошибка при обработке фото")

async def model_selected(callback_query: types.CallbackQuery, model_path: str):
    user_id = callback_query.from_user.id
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    try:
        # Скачиваем модель из Supabase
        local_path = os.path.join(user_dir, "selected_model.jpg")
        await download_from_supabase(model_path, local_path)
        
        # Сохраняем информацию о выборе модели
        await baserow.upsert_row(user_id, callback_query.from_user.username, {
            "model_selected": model_path,
            "photo_person": True
        })
        
        # Проверяем наличие фото одежды
        clothes_photos = [f for f in os.listdir(user_dir) if f.startswith("photo_1")]
        
        if clothes_photos:
            if user_id not in FREE_USERS:
                await update_user_tries(user_id, await get_user_tries(user_id) - 1)
            
            await callback_query.message.answer("✅ Все файлы получены! Идет обработка...")
            await baserow.upsert_row(user_id, "", {"status": "В обработке"})
        else:
            await callback_query.message.answer("✅ Модель выбрана! Теперь отправьте фото одежды.")
            
    except Exception as e:
        logger.error(f"Model selection error: {e}")
        await callback_query.message.answer("❌ Ошибка при выборе модели")

async def save_result_to_supabase(user_id: int, result_path: str):
    """Сохраняет результат обработки в Supabase"""
    try:
        remote_path = f"{user_id}/result_{int(time.time())}{os.path.splitext(result_path)[1]}"
        
        with open(result_path, 'rb') as f:
            supabase.storage.from_(UPLOADS_BUCKET).upload(
                path=remote_path,
                file=f
            )
        
        url = supabase.storage.from_(UPLOADS_BUCKET).get_public_url(remote_path)
        
        await baserow.upsert_row(user_id, "", {
            "result_path": remote_path,
            "result_url": url,
            "status": "Результат готов"
        })
        
        return url
        
    except Exception as e:
        logger.error(f"Error saving result: {e}")
        raise

async def check_results():
    while True:
        try:
            for user_id in os.listdir(UPLOAD_DIR):
                user_dir = os.path.join(UPLOAD_DIR, user_id)
                if not os.path.isdir(user_dir):
                    continue
                
                # Ищем результат обработки
                result_file = next(
                    (os.path.join(user_dir, f) for f in os.listdir(user_dir) 
                     if f.startswith("result") and f.endswith(tuple(SUPPORTED_EXTENSIONS))),
                    None
                )
                
                if result_file:
                    try:
                        # Сохраняем результат в Supabase
                        result_url = await save_result_to_supabase(int(user_id), result_file)
                        
                        # Отправляем пользователю
                        await bot.send_photo(
                            chat_id=int(user_id),
                            photo=result_url,
                            caption="🎉 Ваша виртуальная примерка готова!"
                        )
                        
                        # Очищаем папку пользователя
                        shutil.rmtree(user_dir)
                        
                    except Exception as e:
                        logger.error(f"Error sending result to {user_id}: {e}")
                        
        except Exception as e:
            logger.error(f"Results watcher error: {e}")
        
        await asyncio.sleep(10)

# ... (остальные функции остаются без изменений)

async def main():
    logger.info("Starting bot...")
    
    # Проверка и создание бакетов
    if supabase:
        try:
            for bucket in [UPLOADS_BUCKET, MODELS_BUCKET, EXAMPLES_BUCKET]:
                try:
                    supabase.storage.from_(bucket).list()
                except:
                    supabase.storage.create_bucket(bucket)
                    logger.info(f"Created bucket: {bucket}")
                    
            # Установка политик доступа
            try:
                supabase.rpc("create_storage_policies", {}).execute()
            except:
                logger.warning("Failed to create storage policies")
                
        except Exception as e:
            logger.error(f"Storage initialization error: {e}")
    
    asyncio.create_task(check_results())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
