import os
import logging
import asyncio
import aiohttp
import shutil
import sys
import sys
if sys.platform == "linux":
    import fcntl
    try:
        fcntl.flock(sys.stdout, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        logger.error("Another instance is already running. Exiting.")
        sys.exit(1)
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
from aiohttp import web

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
UPLOAD_DIR = "uploads"
MODELS_BUCKET = "models"
EXAMPLES_BUCKET = "primery"
UPLOADS_BUCKET = "uploads"  # Бакет для загружаемых файлов
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
    
    buckets = supabase.storage.list_buckets()
    logger.info(f"Available buckets: {buckets}")
    
    if MODELS_BUCKET not in [b.name for b in buckets]:
        logger.error(f"Bucket '{MODELS_BUCKET}' not found in Supabase storage")
    if EXAMPLES_BUCKET not in [b.name for b in buckets]:
        logger.error(f"Bucket '{EXAMPLES_BUCKET}' not found in Supabase storage")
    if UPLOADS_BUCKET not in [b.name for b in buckets]:
        logger.error(f"Bucket '{UPLOADS_BUCKET}' not found in Supabase storage")
except Exception as e:
    logger.error(f"Failed to initialize Supabase client: {e}")
    supabase = None

async def upload_to_supabase(file_path: str, user_id: int, file_type: str):
    """Загружает файл в Supabase Storage"""
    if not supabase:
        return False
    
    try:
        file_name = os.path.basename(file_path)
        destination_path = f"{user_id}/{file_type}/{file_name}"
        
        with open(file_path, 'rb') as f:
            res = supabase.storage.from_(UPLOADS_BUCKET).upload(
                path=destination_path,
                file=f,
                file_options={"content-type": "image/jpeg"}
            )
        
        logger.info(f"File {file_path} uploaded to Supabase as {destination_path}")
        return True
    except Exception as e:
        logger.error(f"Error uploading file to Supabase: {e}")
        return False

async def download_from_supabase(user_id: int, file_type: str, file_name: str, local_path: str):
    """Скачивает файл из Supabase Storage"""
    if not supabase:
        return False
    
    try:
        source_path = f"{user_id}/{file_type}/{file_name}"
        res = supabase.storage.from_(UPLOADS_BUCKET).download(source_path)
        
        with open(local_path, 'wb') as f:
            f.write(res)
        
        logger.info(f"File {source_path} downloaded from Supabase to {local_path}")
        return True
    except Exception as e:
        logger.error(f"Error downloading file from Supabase: {e}")
        return False

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

class PaymentManager:
    @staticmethod
    async def create_payment_link(amount: float, label: str) -> str:
        """Создает ссылку для оплаты через ЮMoney"""
        return f"https://yoomoney.ru/quickpay/confirm.xml?receiver={YMONEY_WALLET}&quickpay-form=small&targets={label}&paymentType=AC&sum={amount}&label={label}"

    @staticmethod
    async def check_payment(label: str) -> bool:
        """Проверяет наличие платежа по метке"""
        url = "https://yoomoney.ru/api/operation-history"
        headers = {
            "Authorization": f"Bearer {YMONEY_TOKEN}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = {
            "type": "deposition",
            "label": label,
            "records": "1"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=data) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return result.get("operations", []) != []
        except Exception as e:
            logger.error(f"Error checking payment: {e}")
        return False

async def get_user_tries(user_id: int) -> int:
    """Получает количество доступных примерок для пользователя"""
    try:
        url = f"https://api.baserow.io/api/database/rows/table/{TABLE_ID}/?user_field_names=true&filter__user_id__equal={user_id}"
        headers = {
            "Authorization": f"Token {BASEROW_TOKEN}",
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    rows = await resp.json()
                    if rows.get("results"):
                        return rows["results"][0].get("tries_left", 0)
    except Exception as e:
        logger.error(f"Error getting user tries: {e}")
    return 0

async def update_user_tries(user_id: int, tries: int):
    """Обновляет количество доступных примерок для пользователя"""
    try:
        url = f"https://api.baserow.io/api/database/rows/table/{TABLE_ID}/?user_field_names=true&filter__user_id__equal={user_id}"
        headers = {
            "Authorization": f"Token {BASEROW_TOKEN}",
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    rows = await resp.json()
                    if rows.get("results"):
                        row_id = rows["results"][0]["id"]
                        update_url = f"https://api.baserow.io/api/database/rows/table/{TABLE_ID}/{row_id}/?user_field_names=true"
                        await session.patch(update_url, headers=headers, json={"tries_left": tries})
    except Exception as e:
        logger.error(f"Error updating user tries: {e}")

async def is_processing(user_id: int) -> bool:
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    if not os.path.exists(user_dir):
        return False
        
    photos = [
        f for f in os.listdir(user_dir)
        if f.startswith("photo_") and f.endswith(tuple(SUPPORTED_EXTENSIONS))
    ]
    model_selected = os.path.exists(os.path.join(user_dir, "selected_model.jpg"))
    
    return (len(photos) >= 2 or (len(photos) >= 1 and model_selected))

async def send_initial_examples(chat_id: int):
    """Отправляет первые три примера перед приветствием"""
    try:
        media = [
            InputMediaPhoto(media="https://drive.google.com/uc?export=download&id=1013DE2SDg8u0V69ePxTYki2WWSNaGWVi"),
            InputMediaPhoto(media="https://drive.google.com/uc?export=download&id=1010hYD1PjCQX-hZQAfRPigkLyz1PAaCH"),
            InputMediaPhoto(media="https://drive.google.com/uc?export=download&id=104v4mW-4-HIH40RIg9-L86sTPWQsxCEF")
        ]
        await bot.send_media_group(chat_id, media=media)
        logger.info(f"Примеры фото отправлены {chat_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки примеров: {e}")
        await bot.send_message(chat_id, "📸 Примеры работ временно недоступны")

async def get_examples_list():
    """Получает список примеров из папки primery в Supabase"""
    if not supabase:
        logger.warning("Supabase client not available")
        return []
    
    try:
        res = supabase.storage.from_(EXAMPLES_BUCKET).list()
        logger.info(f"Supabase storage response for examples: {res}")
        
        if not res:
            logger.warning("No examples found")
            return []
            
        examples = [
            file['name'] for file in res 
            if any(file['name'].lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS)
        ]
        
        logger.info(f"Found {len(examples)} examples")
        return examples
        
    except Exception as e:
        logger.error(f"Error getting examples list: {str(e)}", exc_info=True)
        return []

async def send_examples_page(chat_id: int, page: int = 0):
    """Отправляет страницу с примерами"""
    try:
        examples = await get_examples_list()
        if not examples:
            await bot.send_message(chat_id, "📸 Примеры работ временно недоступны")
            return
            
        start_idx = page * EXAMPLES_PER_PAGE
        end_idx = start_idx + EXAMPLES_PER_PAGE
        current_examples = examples[start_idx:end_idx]
        
        if not current_examples:
            await bot.send_message(chat_id, "✅ Это все доступные примеры.")
            return
            
        media = []
        for example in current_examples:
            try:
                image_url = supabase.storage.from_(EXAMPLES_BUCKET).get_public_url(example)
                media.append(InputMediaPhoto(media=image_url))
            except Exception as e:
                logger.error(f"Error loading example {example}: {e}")
                continue
        
        if media:
            await bot.send_media_group(chat_id, media=media)
            
            # Создаем клавиатуру для навигации
            keyboard_buttons = []
            
            if end_idx < len(examples):
                keyboard_buttons.append(
                    InlineKeyboardButton(text="Посмотреть ещё", callback_data=f"more_examples_{page + 1}")
                )
            
            keyboard_buttons.append(
                InlineKeyboardButton(text="Возврат в меню", callback_data="back_to_menu")
            )
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[keyboard_buttons])
            
            await bot.send_message(
                chat_id,
                "Выберите действие:",
                reply_markup=keyboard
            )
            
    except Exception as e:
        logger.error(f"Error sending examples: {e}")
        await bot.send_message(chat_id, "❌ Ошибка при загрузке примеров. Попробуйте позже.")

async def get_models_list(category: str):
    if not supabase:
        logger.warning("Supabase client not available")
        return []
    
    try:
        res = supabase.storage.from_(MODELS_BUCKET).list(category)
        logger.info(f"Supabase storage response for {category}: {res}")
        
        if not res:
            logger.warning(f"No files found in {category} category")
            return []
            
        models = [
            file['name'] for file in res 
            if any(file['name'].lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS)
        ]
        
        logger.info(f"Found {len(models)} models in {category} category")
        return models
        
    except Exception as e:
        logger.error(f"Error getting models list for {category}: {str(e)}", exc_info=True)
        return []

async def notify_admin(message: str):
    if not ADMIN_CHAT_ID:
        return
        
    try:
        await bot.send_message(ADMIN_CHAT_ID, message)
    except Exception as e:
        logger.error(f"Error sending admin notification: {e}")

async def send_welcome(user_id: int, username: str, full_name: str):
    try:
        # Отправляем первые три примера
        await send_initial_examples(user_id)
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👫 Выбрать модель", callback_data="choose_model")],
            [InlineKeyboardButton(text="📸 Посмотреть примеры", callback_data="view_examples_0")]
        ])
        
        await bot.send_message(
            user_id,
            "<b>ВИРТУАЛЬНАЯ ПРИМЕРОЧНАЯ</b>\n\n"
            "👋 Привет! Это бот виртуальной примерки одежды.\n\n"
            "📌 <b>Как это работает:</b> \n\n"
            "1️⃣ Отправьте первое фото – одежда (отправляйте только 1 фото)\n"
            "2️⃣ Отправьте второе фото – человек (желательно в полный рост, 1 фото) или выберите готовую модель \n"
            "👆 Фото прикрепляйте через скрепку, которая находится, где отправляете сообщения\n\n"
            "🌈 <b>Получите результат изображения виртуальной примерки!!</b> \n\n"
            "🔴 <b>Отправляйте по порядку сначала фото одежды, затем фото человека или выберите модель для примерки!!!</b> \n\n" 
            "🔔 Если хотите примерить верхнюю и нижнюю одежду, отправьте сначала фото (верхней или нижней одежды) выполните примерку - получите результат обработки, затем уже отправляйте 2-ое фото (верхней или нижней одежды) и результат  первой обработки\n\n" 
            "📸 <b>ОТПРАВЬТЕ ПЕРВОЕ ФОТО (одежда), ЖДУ!!!:</b>",
            reply_markup=keyboard
        )
        
        # Сбрасываем флажки при старте
        await baserow.reset_flags(user_id)
        
        await baserow.upsert_row(user_id, username, {
            "status": "started",
            "photo_clothes": False,
            "photo_person": False,
            "model_selected": None,
            "tries_left": 1  # Первая примерка бесплатная
        }) 
        
        await notify_admin(f"🆕 Пользователь: @{username} ({user_id})")
        
    except Exception as e:
        logger.error(f"Welcome error for {user_id}: {e}")

@dp.message(Command("start"))
@dp.message(F.text)
async def handle_start(message: types.Message):
    if await is_processing(message.from_user.id):
        await message.answer("✅ Оба файла получены.\n🔄 Идёт примерка. Ожидайте результат!")
        return
        
    await send_welcome(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )

@dp.callback_query(F.data == "choose_model")
async def choose_model(callback_query: types.CallbackQuery):
    if await is_processing(callback_query.from_user.id):
        await callback_query.answer("✅ Оба файла получены. Ожидайте результат!", show_alert=True)
        return
        
    try:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Мужчины", callback_data="models_man_0")],
            [InlineKeyboardButton(text="👩 Женщины", callback_data="models_woman_0")],
            [InlineKeyboardButton(text="🧒 Дети", callback_data="models_child_0")]
        ])
        
        await callback_query.message.answer(
            "👇 Выберите категорию моделей:",
            reply_markup=keyboard
        )
        
    except Exception as e:
        logger.error(f"Error in choose_model: {e}")
        await callback_query.message.answer("⚠️ Ошибка при загрузке категорий. Попробуйте позже.")

@dp.callback_query(F.data.startswith("view_examples_"))
async def view_examples(callback_query: types.CallbackQuery):
    try:
        page = int(callback_query.data.split("_")[-1])
        await send_examples_page(callback_query.from_user.id, page)
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in view_examples: {e}")
        await callback_query.message.answer("⚠️ Ошибка при загрузке примеров. Попробуйте позже.")
        await callback_query.answer()

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback_query: types.CallbackQuery):
    try:
        await send_welcome(
            callback_query.from_user.id,
            callback_query.from_user.username,
            callback_query.from_user.full_name
        )
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in back_to_menu: {e}")
        await callback_query.message.answer("⚠️ Ошибка при возврате в меню. Попробуйте позже.")
        await callback_query.answer()

@dp.callback_query(F.data.startswith("more_examples_"))
async def more_examples(callback_query: types.CallbackQuery):
    try:
        page = int(callback_query.data.split("_")[-1])
        await send_examples_page(callback_query.from_user.id, page)
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in more_examples: {e}")
        await callback_query.message.answer("⚠️ Ошибка при загрузке примеров. Попробуйте позже.")
        await callback_query.answer()

@dp.callback_query(F.data.startswith("models_"))
async def show_category_models(callback_query: types.CallbackQuery):
    if await is_processing(callback_query.from_user.id):
        await callback_query.answer("✅ Оба файла получены. Ожидайте результат!", show_alert=True)
        return
        
    data_parts = callback_query.data.split("_")
    if len(data_parts) != 3:
        await callback_query.answer("⚠️ Ошибка параметров", show_alert=True)
        return
        
    category = data_parts[1]
    page = int(data_parts[2])
    
    category_names = {
        "man": "👨 Мужские модели",
        "woman": "👩 Женские модели", 
        "child": "🧒 Детские модели"
    }
    
    try:
        models = await get_models_list(category)
        logger.info(f"Models to display for {category}: {models}")
        
        if not models:
            await callback_query.message.answer(f"❌ В данной категории пока нет доступных моделей.")
            return

        start_idx = page * MODELS_PER_PAGE
        end_idx = start_idx + MODELS_PER_PAGE
        current_models = models[start_idx:end_idx]
        
        if page == 0:
            await callback_query.message.answer(f"{category_names.get(category, 'Модели')}:")

        for model in current_models:
            model_name = os.path.splitext(model)[0]
            
            try:
                image_url = supabase.storage.from_(MODELS_BUCKET).get_public_url(f"{category}/{model}")
                
                await bot.send_photo(
                    chat_id=callback_query.from_user.id,
                    photo=image_url,
                    caption=f"Модель: {model_name}",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="✅ Выбрать эту модель",
                                    callback_data=f"model_{category}/{model}"
                                )
                            ]
                        ]
                    )
                )
            except Exception as e:
                logger.error(f"Error displaying model {model}: {e}")
                continue

        if end_idx < len(models):
            await callback_query.message.answer(
                "Показать еще модели?",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="⬇️ Показать еще",
                                callback_data=f"models_{category}_{page + 1}"
                            )
                        ]
                    ]
                )
            )
        else:
            await callback_query.message.answer("✅ Это все доступные модели в данной категории.")

    except Exception as e:
        logger.error(f"Error in show_category_models: {e}")
        await callback_query.message.answer("⚠️ Ошибка при загрузке моделей. Попробуйте позже.")

@dp.callback_query(F.data.startswith("model_"))
async def model_selected(callback_query: types.CallbackQuery):
    if await is_processing(callback_query.from_user.id):
        await callback_query.answer("✅ Оба файла получены. Ожидайте результат!", show_alert=True)
        return
        
    model_path = callback_query.data.replace("model_", "")
    category, model_name = model_path.split('/')
    user_id = callback_query.from_user.id
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    try:
        await callback_query.message.delete()
        
        clothes_photo_exists = any(
            f.startswith("photo_1") and f.endswith(tuple(SUPPORTED_EXTENSIONS))
            for f in os.listdir(user_dir)
        )

        model_display_name = os.path.splitext(model_name)[0]
        await baserow.upsert_row(user_id, callback_query.from_user.username, {
            "model_selected": model_path,
            "status": "model_selected"
        })
        
        if supabase:
            try:
                model_url = supabase.storage.from_(MODELS_BUCKET).get_public_url(f"{model_path}")
                
                model_path_local = os.path.join(user_dir, "selected_model.jpg")
                with open(model_path_local, 'wb') as f:
                    res = supabase.storage.from_(MODELS_BUCKET).download(f"{model_path}")
                    f.write(res)
                logger.info(f"Model {model_path} downloaded successfully")
                
                # Загружаем модель в Supabase в папку uploads
                await upload_to_supabase(model_path_local, user_id, "models")
                
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

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user = message.from_user
    user_id = user.id
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    try:
        # Проверяем, есть ли у пользователя бесплатный доступ
        if user_id in FREE_USERS:
            await process_photo(message, user, user_dir)
            return
            
        # Получаем количество оставшихся попыток
        tries_left = await get_user_tries(user_id)
        
        # Если попыток нет, предлагаем оплатить
        if tries_left <= 0:
            payment_label = f"tryon_{user_id}"
            payment_link = await PaymentManager.create_payment_link(PRICE_PER_TRY, payment_label)
            
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
        await process_photo(message, user, user_dir)
        
    except Exception as e:
        logger.error(f"Error handling photo: {e}")
        await message.answer("❌ Ошибка при сохранении файла. Попробуйте ещё раз.")

async def process_photo(message: types.Message, user: types.User, user_dir: str):
    """Обрабатывает загруженное фото"""
    try:
        existing_photos = [
            f for f in os.listdir(user_dir)
            if f.startswith("photo_") and f.endswith(tuple(SUPPORTED_EXTENSIONS))
        ]
        
        photo_number = len(existing_photos) + 1
        
        if photo_number > 2:
            await message.answer("✅ Вы уже загрузили 2 файла. Ожидайте результат.")
            return
            
        # Проверяем, есть ли уже модель или первое фото
        model_selected = os.path.exists(os.path.join(user_dir, "selected_model.jpg"))
        first_photo_exists = any(f.startswith("photo_1") for f in existing_photos)
        
        # Если это второе фото и нет модели, но есть первое фото
        if photo_number == 2 and not model_selected and first_photo_exists:
            photo = message.photo[-1]
            file_ext = os.path.splitext(photo.file_id)[1] or '.jpg'
            file_name = f"photo_{photo_number}{file_ext}"
            file_path = os.path.join(user_dir, file_name)
            
            await bot.download(photo, destination=file_path)
            
            # Загружаем фото в Supabase
            await upload_to_supabase(file_path, user.id, "photos")
            
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
            file_path = os.path.join(user_dir, file_name)
            
            await bot.download(photo, destination=file_path)
            
            # Загружаем фото в Supabase
            await upload_to_supabase(file_path, user.id, "photos")
            
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

@dp.callback_query(F.data.startswith("check_payment_"))
async def check_payment(callback_query: types.CallbackQuery):
    payment_label = callback_query.data.replace("check_payment_", "")
    user_id = callback_query.from_user.id
    
    try:
        # Проверяем оплату
        is_paid = await PaymentManager.check_payment(payment_label)
        
        if is_paid:
            # Получаем сумму платежа (в реальной реализации нужно получать из API ЮMoney)
            # Здесь для примера считаем, что оплачено 30 руб = 1 примерка
            payment_amount = 30
            additional_tries = payment_amount // PRICE_PER_TRY
            
            # Обновляем количество попыток
            current_tries = await get_user_tries(user_id)
            new_tries = current_tries + additional_tries
            await update_user_tries(user_id, new_tries)
            
            await callback_query.message.answer(
                f"✅ Оплата получена! Вам доступно {additional_tries} дополнительных примерок.\n"
                f"Всего доступно примерок: {new_tries}\n\n"
                "Теперь вы можете продолжить работу с ботом."
            )
            
            await notify_admin(f"💰 Пользователь @{callback_query.from_user.username} ({user_id}) оплатил {payment_amount} руб.")
        else:
            await callback_query.answer("❌ Оплата не найдена. Попробуйте позже или свяжитесь с поддержкой.", show_alert=True)
            
    except Exception as e:
        logger.error(f"Error checking payment: {e}")
        await callback_query.answer("❌ Ошибка при проверке оплаты. Попробуйте позже.", show_alert=True)

async def check_results():
    logger.info("🔄 Starting check_results() loop...")
    while True:
        try:
            logger.info("🔍 Scanning for results...")
            if not os.path.exists(UPLOAD_DIR):
                logger.warning(f"Directory {UPLOAD_DIR} does not exist!")
                await asyncio.sleep(10)
                continue

            for user_id_str in os.listdir(UPLOAD_DIR):
                user_dir = os.path.join(UPLOAD_DIR, user_id_str)
                if not os.path.isdir(user_dir):
                    continue

                logger.info(f"Checking user dir: {user_dir}")
                result_file = os.path.join(user_dir, "result.jpg")  # или другой формат

                if os.path.exists(result_file):
                    logger.info(f"✅ Found result for user {user_id_str}")
                    try:
                        user_id = int(user_id_str)
                        
                        # Отправка фото
                        photo = FSInputFile(result_file)
                        await bot.send_photo(
                            chat_id=user_id,
                            photo=photo,
                            caption="🎉 Ваша виртуальная примерка готова!"
                        )
                        
                        # Загрузка в Supabase
                        supabase_path = f"{user_id}/photos/result.jpg"
                        with open(result_file, 'rb') as f:
                            await supabase.storage.from_(UPLOADS_BUCKET).upload(
                                path=supabase_path,
                                file=f,
                                file_options={"content-type": "image/jpeg"}
                            )
                        
                        # Обновление статуса
                        await baserow.upsert_row(user_id, "", {
                            "status": "Результат отправлен",
                            "result_sent": True,
                            "ready": True
                        })
                        
                        # Очистка папки
                        shutil.rmtree(user_dir)
                        logger.info(f"✅ Result sent to user {user_id}")

                    except Exception as e:
                        logger.error(f"❌ Error processing result for {user_id_str}: {e}")
                        try:
                            await bot.send_message(
                                user_id,
                                "⚠️ Ошибка при отправке результата. Попробуйте ещё раз."
                            )
                        except Exception as e:
                            logger.error(f"Failed to send error message: {e}")

            # Увеличиваем интервал проверки, чтобы не нагружать сервер
            await asyncio.sleep(30)  # Проверка каждые 30 секунд

        except Exception as e:
            logger.error(f"❌ Critical error in check_results(): {e}")
            await asyncio.sleep(30)  # Ждем перед повторной попыткой

async def handle(request):
    return web.Response(text="Bot is running")

def setup_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    return app

async def start_web_server():
    app = setup_web_server()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv('PORT', 4000)))
    await site.start()
    logger.info("Web server started")

async def main():
    try:
        logger.info("Starting bot...")
        
        # Запуск веб-сервера для Render
        asyncio.create_task(start_web_server())
        
        if supabase:
            try:
                # Проверяем доступность моделей и примеров
                for category in ["man", "woman", "child"]:
                    models = await get_models_list(category)
                    logger.info(f"Available {category} models count: {len(models)}")
                
                examples = await get_examples_list()
                logger.info(f"Available examples count: {len(examples)}")
            except Exception as e:
                logger.error(f"Supabase check failed: {e}")
        
        asyncio.create_task(check_results())
        
        # Удаляем веб-хук (если был) и переключаемся на поллинг
        await bot.delete_webhook(drop_pending_updates=True)
        
        # Используем skip_updates=True, чтобы пропустить старые обновления
        await dp.start_polling(bot, skip_updates=True)
        
    except asyncio.CancelledError:
        logger.info("Main task cancelled")
    except Exception as e:
        logger.error(f"Error in main: {e}")
        raise

if __name__ == "__main__":
    try:
        # Создаем новый event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Запускаем основную функцию
        loop.run_until_complete(main())
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by keyboard interrupt")
        
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        
    finally:
        # Всегда выполняем graceful shutdown
        loop.run_until_complete(on_shutdown())
        loop.close()
        logger.info("Bot successfully shut down")
