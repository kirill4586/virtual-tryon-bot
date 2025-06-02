import os
import logging
import asyncio
import aiohttp
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
PRICE_PER_TRY = 30  # Цена за одну примерку в рублях
FREE_USERS = {6320348591, 973853935}  # Пользователи с бесплатным доступом
UPLOAD_DIR = "uploads"  # Корневая папка в Supabase Storage
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

# Инициализация Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Supabase client initialized successfully")
    
    # Проверяем существование корневой папки для загрузок
    try:
        res = supabase.storage.from_(UPLOAD_DIR).list()
        logger.info(f"Uploads folder exists in Supabase storage")
    except Exception as e:
        logger.info(f"Uploads folder doesn't exist, creating...")
        supabase.storage.create_bucket(UPLOAD_DIR, public=True)
        logger.info(f"Uploads folder created in Supabase storage")
        
except Exception as e:
    logger.error(f"Failed to initialize Supabase client: {e}")
    supabase = None

class BaserowAPI:
    def __init__(self):
        self.base_url = f"https://api.baserow.io/api/database/rows/table/{TABLE_ID}"
        self.headers = {
            "Authorization": f"Token {BASEROW_TOKEN}",
            "Content-Type": "application/json"
        }

    async def upsert_row(self, user_id: int, username: str, data: dict):
        try:
            url = f"{self.base_url}/?user_field_names=true&filter__user_id__equal={user_id}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as resp:
                    if resp.status != 200:
                        logger.error(f"Baserow GET error: {resp.status}")
                        return None
                    rows = await resp.json()
                    
                base_data = {
                    "user_id": str(user_id),
                    "username": username or ""
                }
                    
                if rows.get("results"):
                    row_id = rows["results"][0]["id"]
                    update_url = f"{self.base_url}/{row_id}/?user_field_names=true"
                    async with session.patch(update_url, headers=self.headers, json={**base_data, **data}) as resp:
                        return await resp.json()
                else:
                    async with session.post(f"{self.base_url}/?user_field_names=true", 
                                         headers=self.headers, 
                                         json={**base_data, **data}) as resp:
                        return await resp.json()
        except Exception as e:
            logger.error(f"Baserow API exception: {e}")
            return None

    async def reset_flags(self, user_id: int):
        """Сбрасывает все флажки для указанного пользователя"""
        try:
            url = f"{self.base_url}/?user_field_names=true&filter__user_id__equal={user_id}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as resp:
                    if resp.status != 200:
                        logger.error(f"Baserow GET error: {resp.status}")
                        return False
                    rows = await resp.json()
                    
                if rows.get("results"):
                    row_id = rows["results"][0]["id"]
                    update_url = f"{self.base_url}/{row_id}/?user_field_names=true"
                    reset_data = {
                        "photo1_received": False,
                        "photo2_received": False,
                        "ready": False
                    }
                    async with session.patch(update_url, headers=self.headers, json=reset_data) as resp:
                        return resp.status == 200
        except Exception as e:
            logger.error(f"Error resetting flags: {e}")
            return False

baserow = BaserowAPI()

async def upload_to_supabase(user_id: int, file_name: str, file_data: bytes) -> str:
    """Загружает файл в Supabase Storage и возвращает URL"""
    try:
        # Формируем путь в формате "uploads/user_id/filename"
        file_path = f"{user_id}/{file_name}"
        
        # Загружаем файл
        res = supabase.storage.from_(UPLOAD_DIR).upload(file_path, file_data)
        
        # Получаем публичный URL
        url = supabase.storage.from_(UPLOAD_DIR).get_public_url(file_path)
        
        logger.info(f"File uploaded to Supabase: {url}")
        return url
        
    except Exception as e:
        logger.error(f"Error uploading to Supabase: {e}")
        raise

async def download_from_supabase(user_id: int, file_name: str) -> bytes:
    """Скачивает файл из Supabase Storage"""
    try:
        file_path = f"{user_id}/{file_name}"
        res = supabase.storage.from_(UPLOAD_DIR).download(file_path)
        return res
    except Exception as e:
        logger.error(f"Error downloading from Supabase: {e}")
        raise

async def list_user_files(user_id: int) -> list:
    """Возвращает список файлов пользователя в Supabase"""
    try:
        res = supabase.storage.from_(UPLOAD_DIR).list(str(user_id))
        return [file['name'] for file in res if any(file['name'].lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS)]
    except Exception as e:
        logger.error(f"Error listing user files: {e}")
        return []

async def delete_user_files(user_id: int):
    """Удаляет все файлы пользователя из Supabase"""
    try:
        files = await list_user_files(user_id)
        for file in files:
            supabase.storage.from_(UPLOAD_DIR).remove([f"{user_id}/{file}"])
        logger.info(f"Deleted all files for user {user_id}")
    except Exception as e:
        logger.error(f"Error deleting user files: {e}")

async def is_processing(user_id: int) -> bool:
    """Проверяет, есть ли у пользователя достаточно файлов для обработки"""
    try:
        files = await list_user_files(user_id)
        
        photos = [f for f in files if f.startswith("photo_")]
        model_selected = any(f.startswith("selected_model") for f in files)
        
        return (len(photos) >= 2 or (len(photos) >= 1 and model_selected))
    except Exception as e:
        logger.error(f"Error in is_processing: {e}")
        return False

async def get_user_tries(user_id: int) -> int:
    """Возвращает количество оставшихся попыток"""
    # Временная реализация - всегда возвращаем 1 для теста
    return 1

async def update_user_tries(user_id: int, tries: int):
    """Обновляет количество попыток пользователя"""
    pass

async def notify_admin(message: str):
    """Отправляет уведомление администратору"""
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(ADMIN_CHAT_ID, message)
        except Exception as e:
            logger.error(f"Error sending admin notification: {e}")

# ===================== ОБРАБОТЧИКИ КОМАНД =====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    user = message.from_user
    await baserow.upsert_row(user.id, user.username, {
        "status": "Бот запущен",
        "first_start": True
    })
    
    welcome_text = (
        "👋 Добро пожаловать в бота для виртуальной примерки одежды!\n\n"
        "📸 Чтобы начать, отправьте:\n"
        "1. Фото одежды (на вешалке или без)\n"
        "2. Фото человека или выберите модель из каталога\n\n"
        "🔄 После загрузки двух фото бот сделает виртуальную примерку!"
    )
    
    await message.answer(welcome_text)

@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Обработчик команды /help"""
    help_text = (
        "ℹ️ <b>Как пользоваться ботом:</b>\n\n"
        "1. Отправьте фото одежды (на вешалке или без фона)\n"
        "2. Отправьте фото человека или выберите модель\n"
        "3. Получите результат виртуальной примерки!\n\n"
        "🛠 Если возникли проблемы - @support"
    )
    await message.answer(help_text, parse_mode=ParseMode.HTML)

@dp.message(Command("models"))
async def cmd_models(message: Message):
    """Показывает доступные модели"""
    try:
        # Получаем список моделей из Supabase Storage
        models = supabase.storage.from_(MODELS_BUCKET).list()
        
        if not models:
            await message.answer("😕 Модели временно недоступны. Попробуйте позже.")
            return
            
        # Создаем клавиатуру с моделями
        keyboard = InlineKeyboardMarkup(inline_keyboard=[])
        
        for model in models[:MODELS_PER_PAGE]:
            model_name = os.path.splitext(model['name'])[0]
            callback_data = f"model_{MODELS_BUCKET}/{model['name']}"
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(text=model_name, callback_data=callback_data)
            ])
        
        await message.answer(
            "👗 Выберите модель для примерки:",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error showing models: {e}")
        await message.answer("❌ Ошибка при загрузке моделей. Попробуйте позже.")

# ===================== ОБРАБОТЧИКИ СООБЩЕНИЙ =====================

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user = message.from_user
    user_id = user.id
    
    try:
        # Проверяем, есть ли у пользователя бесплатный доступ
        if user_id in FREE_USERS:
            await process_photo(message, user)
            return
            
        # Получаем количество оставшихся попыток
        tries_left = await get_user_tries(user_id)
        
        # Если попыток нет, предлагаем оплатить
        if tries_left <= 0:
            payment_label = f"tryon_{user_id}"
            payment_link = f"https://yoomoney.ru/quickpay/confirm.xml?receiver={YMONEY_WALLET}&quickpay-form=small&targets={payment_label}&sum={PRICE_PER_TRY}&label={payment_label}"
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оплатить 30 руб", url=payment_link)],
                [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_payment_{payment_label}")]
            ])
            
            await message.answer(
                "🚫 У вас закончились бесплатные примерки.\n\n"
                "💵 Стоимость одной примерки: 30 руб.\n"
                "После оплаты вам будет доступно количество примерок в соответствии с внесенной суммой.\n\n"
                "Например:\n"
                "30 руб = 1 примерка\n"
                "60 руб = 2 примерки\n"
                "90 руб = 3 примерки и т.д.",
                reply_markup=keyboard
            )
            return
            
        # Если попытки есть, обрабатываем фото
        await process_photo(message, user)
        
    except Exception as e:
        logger.error(f"Error handling photo: {e}")
        await message.answer("❌ Ошибка при сохранении файла. Попробуйте ещё раз.")

async def process_photo(message: types.Message, user: types.User):
    """Обрабатывает загруженное фото и сохраняет в Supabase"""
    try:
        files = await list_user_files(user.id)
        
        photo_number = len([f for f in files if f.startswith("photo_")]) + 1
        
        if photo_number > 2:
            await message.answer("✅ Вы уже загрузили 2 файла. Ожидайте результат.")
            return
            
        # Проверяем, есть ли уже модель или первое фото
        model_selected = any(f.startswith("selected_model") for f in files)
        first_photo_exists = any(f.startswith("photo_1") for f in files)
        
        # Если это второе фото и нет модели, но есть первое фото
        if photo_number == 2 and not model_selected and first_photo_exists:
            photo = message.photo[-1]
            file_ext = os.path.splitext(photo.file_id)[1] or '.jpg'
            file_name = f"photo_{photo_number}{file_ext}"
            
            # Скачиваем фото и загружаем в Supabase
            file_data = await bot.download(photo)
            await upload_to_supabase(user.id, file_name, file_data)
            
            # Уменьшаем количество попыток
            tries_left = await get_user_tries(user.id)
            if tries_left > 0:
                await update_user_tries(user.id, tries_left - 1)
            
            await baserow.upsert_row(user.id, user.username, {
                "photo_person": True,
                "status": "В обработке",
                "photo1_received": True,
                "photo2_received": True
            })
            
            await message.answer(
                "✅ Оба файла получены.\n\n"
                "🔄 Идёт примерка. Ожидайте результат!"
            )
            await notify_admin(f"📸 Новые фото от @{user.username} ({user.id})")
            return
            
        # Если это первое фото
        if photo_number == 1:
            photo = message.photo[-1]
            file_ext = os.path.splitext(photo.file_id)[1] or '.jpg'
            file_name = f"photo_{photo_number}{file_ext}"
            
            # Скачиваем фото и загружаем в Supabase
            file_data = await bot.download(photo)
            await upload_to_supabase(user.id, file_name, file_data)
            
            await baserow.upsert_row(user.id, user.username, {
                "photo_clothes": True,
                "status": "Ожидается фото человека/модели",
                "photo1_received": True,
                "photo2_received": False
            })
            
            response_text = (
                "✅ Фото одежды получено.\n\n"
                "Теперь выберите модель из меню или отправьте фото человека."
            )
            await message.answer(response_text)
            
    except Exception as e:
        logger.error(f"Error processing photo: {e}")
        await message.answer("❌ Ошибка при обработке файла. Попробуйте ещё раз.")

# ===================== ОБРАБОТЧИКИ КНОПОК =====================

@dp.callback_query(F.data.startswith("model_"))
async def model_selected(callback_query: types.CallbackQuery):
    if await is_processing(callback_query.from_user.id):
        await callback_query.answer("✅ Оба файла получены. Ожидайте результат!", show_alert=True)
        return
        
    model_path = callback_query.data.replace("model_", "")
    category, model_name = model_path.split('/')
    user_id = callback_query.from_user.id
    
    try:
        await callback_query.message.delete()
        
        files = await list_user_files(user_id)
        clothes_photo_exists = any(f.startswith("photo_1") for f in files)

        model_display_name = os.path.splitext(model_name)[0]
        await baserow.upsert_row(user_id, callback_query.from_user.username, {
            "model_selected": model_path,
            "status": "model_selected"
        })
        
        if supabase:
            try:
                model_url = supabase.storage.from_(MODELS_BUCKET).get_public_url(f"{model_path}")
                
                # Скачиваем модель и сохраняем в Supabase
                model_data = supabase.storage.from_(MODELS_BUCKET).download(f"{model_path}")
                await upload_to_supabase(user_id, "selected_model.jpg", model_data)
                
                logger.info(f"Model {model_path} downloaded successfully")
                
                if clothes_photo_exists:
                    response_text = (
                        f"✅ Модель {model_display_name} выбрана.\n\n"
                        "✅ Оба файла получены.\n"
                        "🔄 Идёт примерка. Ожидайте результат!"
                    )
                    await baserow.upsert_row(user_id, callback_query.from_user.username, {
                        "photo_person": True,
                        "status": "В обработке",
                        "photo1_received": True,
                        "photo2_received": True
                    })
                    await notify_admin(f"📸 Все фото получены от @{callback_query.from_user.username} ({user_id})")
                else:
                    response_text = (
                        f"✅ Модель {model_display_name} выбрана.\n\n"
                        "📸 Теперь отправьте фото одежды."
                    )
                    await baserow.upsert_row(user_id, callback_query.from_user.username, {
                        "photo1_received": False,
                        "photo2_received": True
                    })
                
                # Отправляем новое сообщение с фото модели в самый низ
                await bot.send_photo(
                    chat_id=user_id,
                    photo=model_url,
                    caption=response_text
                )
                
            except Exception as e:
                logger.error(f"Error downloading model: {e}")
                await bot.send_message(
                    user_id,
                    "❌ Ошибка загрузки модели. Попробуйте выбрать другую."
                )
                return
            
    except Exception as e:
        logger.error(f"Error in model_selected: {e}")
        await bot.send_message(
            user_id,
            "⚠️ Произошла ошибка при выборе модели. Попробуйте позже."
        )

@dp.callback_query(F.data.startswith("check_payment_"))
async def check_payment(callback_query: types.CallbackQuery):
    """Проверяет оплату и обновляет количество попыток"""
    payment_label = callback_query.data.replace("check_payment_", "")
    user_id = callback_query.from_user.id
    
    try:
        # Здесь должна быть проверка оплаты через YooMoney API
        # Временно имитируем успешную оплату
        payment_success = True
        
        if payment_success:
            await callback_query.answer("✅ Оплата подтверждена! Попытки добавлены.", show_alert=True)
            await update_user_tries(user_id, 1)  # Добавляем 1 попытку
            await callback_query.message.delete()
        else:
            await callback_query.answer("❌ Оплата не найдена. Попробуйте позже.", show_alert=True)
    except Exception as e:
        logger.error(f"Error checking payment: {e}")
        await callback_query.answer("⚠️ Ошибка проверки оплаты. Попробуйте позже.", show_alert=True)

# ===================== ФОНОВЫЕ ЗАДАЧИ =====================

async def check_results():
    """Проверяет готовые результаты в Supabase и отправляет пользователям"""
    while True:
        try:
            # Получаем список всех пользовательских папок в Supabase
            res = supabase.storage.from_(UPLOAD_DIR).list()
            user_folders = [item['name'] for item in res if item['name'].isdigit()]
            
            for user_id in user_folders:
                # Проверяем наличие файла результата
                files = supabase.storage.from_(UPLOAD_DIR).list(user_id)
                result_file = next((f for f in files if f['name'].startswith("result")), None)
                
                if result_file:
                    try:
                        # Скачиваем результат
                        result_data = supabase.storage.from_(UPLOAD_DIR).download(f"{user_id}/{result_file['name']}")
                        
                        # Отправляем пользователю
                        await bot.send_photo(
                            chat_id=int(user_id),
                            photo=result_data,
                            caption="🎉 Ваша виртуальная примерка готова!\n\n👚 Если хотите ещё примерить, отправьте новое фото"
                        )
                        
                        # Обновляем статус
                        await baserow.upsert_row(int(user_id), "", {
                            "status": "Результат отправлен",
                            "result_sent": True,
                            "ready": True
                        })
                        
                        # Удаляем файлы пользователя
                        await delete_user_files(user_id)
                        logger.info(f"Результат отправлен пользователю {user_id}")
                        
                    except Exception as e:
                        logger.error(f"Error sending result to {user_id}: {e}")
                        
        except Exception as e:
            logger.error(f"Error in results watcher: {e}")
        
        await asyncio.sleep(10)

# ===================== ЗАПУСК БОТА =====================

async def on_startup():
    """Действия при запуске бота"""
    logger.info("Bot started")
    asyncio.create_task(check_results())  # Запускаем фоновую задачу

async def on_shutdown():
    """Действия при остановке бота"""
    logger.info("Bot stopped")

async def main():
    # Устанавливаем обработчики запуска/остановки
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    # Запускаем бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
