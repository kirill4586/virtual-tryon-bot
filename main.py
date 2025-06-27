import os
import logging
import asyncio
import aiohttp
import shutil
import time
import sys
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
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
from supabase.lib.client_options import ClientOptions
from aiogram.fsm.state import StatesGroup, State

# Определение состояний для FSM
class PaymentFSM(StatesGroup):
    waiting_for_fio_and_amount = State()

if sys.platform == "linux":
    import fcntl
    try:
        fcntl.flock(sys.stdout, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        logger.error("Another instance is already running. Exiting.")
        sys.exit(1)

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
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
PRICE_PER_TRY = 30  # Цена за одну примерку в рублях
UPLOAD_DIR = "uploads"
MODELS_BUCKET = "models"
EXAMPLES_BUCKET = "primery"
UPLOADS_BUCKET = "uploads"
SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
MODELS_PER_PAGE = 3
EXAMPLES_PER_PAGE = 3
PORT = int(os.getenv("PORT", 4000))

# Настройки ЮMoney
YOO_MONEY_WALLET = "4100118533855458"
YOO_MONEY_PHONE = "77055412709"
YOO_MONEY_CARD_LINK = "https://donate.stream/yoomoney4100118533855458?nickname=@{user.username}"
YOO_MONEY_SBP_LINK = "https://yoomoney.ru/prepaid?w=sbpme2me"

# Названия полей в Supabase
USERS_TABLE = "users"
ACCESS_FIELD = "access_granted"
AMOUNT_FIELD = "payment_amount"
TRIES_FIELD = "tries_left"
STATUS_FIELD = "status"
FREE_TRIES_FIELD = "free_tries_used"

# Middleware для обработки устаревших callback-запросов
class CallbackTimeoutMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        except TelegramBadRequest as e:
            if "query is too old" in str(e):
                logger.warning(f"Callback query expired: {e}")
                return None
            raise

# Инициализация клиентов с таймаутами
client_options = ClientOptions(
    postgrest_client_timeout=10,
    storage_client_timeout=10
)
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
dp.update.middleware(CallbackTimeoutMiddleware())
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Кеш для списка моделей
models_cache = {
    "man": {"time": 0, "data": []},
    "woman": {"time": 0, "data": []},
    "child": {"time": 0, "data": []}
}
CACHE_EXPIRATION = 300  # 5 минут

# Инициализация Supabase
try:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY, options=client_options)
    logger.info("Supabase client initialized successfully")
    
    # Проверка существования таблицы пользователей
    try:
        res = supabase.table(USERS_TABLE).select("*").limit(1).execute()
        logger.info(f"Users table exists with {len(res.data)} records")
    except Exception as e:
        logger.error(f"Users table check failed: {e}")
        raise Exception("Users table not found in Supabase")
    
    # Проверка бакетов хранилища
    try:
        buckets = supabase.storage.list_buckets()
        logger.info(f"Available buckets: {buckets}")
        
        required_buckets = [MODELS_BUCKET, EXAMPLES_BUCKET, UPLOADS_BUCKET]
        for bucket in required_buckets:
            if bucket not in [b.name for b in buckets]:
                logger.error(f"Bucket '{bucket}' not found in Supabase storage")
                raise Exception(f"Required bucket '{bucket}' not found")
    except Exception as e:
        logger.error(f"Error checking buckets: {e}")
        raise

except Exception as e:
    logger.error(f"Failed to initialize Supabase: {e}")
    raise

class SupabaseAPI:
    def __init__(self):
        self.supabase = supabase
        self.last_payment_amounts = {}
        self.last_tries_values = {}

    async def get_user_row(self, user_id: int):
        """Получение данных пользователя из Supabase"""
        try:
            res = self.supabase.table(USERS_TABLE)\
                .select("*")\
                .eq("user_id", str(user_id))\
                .execute()
            
            if res.data and len(res.data) > 0:
                return res.data[0]
            return None
        except Exception as e:
            logger.error(f"Error getting user row: {e}")
            return None

    async def update_user_row(self, user_id: int, data: dict):
        """Обновление данных пользователя в Supabase"""
        try:
            res = self.supabase.table(USERS_TABLE)\
                .update(data)\
                .eq("user_id", str(user_id))\
                .execute()
            
            return res.data[0] if res.data else None
        except Exception as e:
            logger.error(f"Error updating user row: {e}")
            return None

    async def check_and_update_access(self, user_id: int):
        """Проверяет доступ пользователя и обновляет количество примерок"""
        try:
            row = await self.get_user_row(user_id)
            if not row:
                return 0

            payment_amount = float(row.get(AMOUNT_FIELD, 0)) if row.get(AMOUNT_FIELD) else 0.0
            tries_left = int(row.get(TRIES_FIELD, 0)) if row.get(TRIES_FIELD) else 0
            free_tries_used = bool(row.get(FREE_TRIES_FIELD, False))

            if payment_amount > 0 and not row.get(ACCESS_FIELD, False):
                tries_left = int(payment_amount / PRICE_PER_TRY)
                await self.update_user_row(user_id, {
                    ACCESS_FIELD: True,
                    TRIES_FIELD: tries_left,
                    STATUS_FIELD: "Оплачено"
                })
                return tries_left

            if not free_tries_used and payment_amount == 0:
                await self.update_user_row(user_id, {
                    ACCESS_FIELD: True,
                    TRIES_FIELD: 1,
                    STATUS_FIELD: "Бесплатная проверка",
                    FREE_TRIES_FIELD: True
                })
                return 1

            if not row.get(ACCESS_FIELD, False):
                return 0

            if tries_left <= 0:
                await self.update_user_row(user_id, {
                    ACCESS_FIELD: False,
                    STATUS_FIELD: "Не оплачено"
                })
                return 0

            return tries_left

        except Exception as e:
            logger.error(f"Error in check_and_update_access: {e}")
            return None

    async def decrement_tries(self, user_id: int):
        """Уменьшает количество примерок на 1 и вычитает стоимость из суммы"""
        try:
            row = await self.get_user_row(user_id)
            if not row:
                return False

            tries_left = int(row.get(TRIES_FIELD, 0)) if row.get(TRIES_FIELD) else 0
            amount = float(row.get(AMOUNT_FIELD, 0)) if row.get(AMOUNT_FIELD) else 0.0
            free_tries_used = bool(row.get(FREE_TRIES_FIELD, False))

            new_tries = max(0, tries_left - 1)
            
            if free_tries_used and amount == 0 and tries_left == 1:
                new_amount = 0.0
            else:
                new_amount = max(0, amount - PRICE_PER_TRY)

            update_data = {
                TRIES_FIELD: new_tries,
                AMOUNT_FIELD: new_amount,
                "last_try_date": time.strftime("%Y-%m-%d %H:%M:%S")
            }

            if new_tries <= 0:
                update_data[ACCESS_FIELD] = False
                update_data[STATUS_FIELD] = "Не оплачено"

            updated = await self.update_user_row(user_id, update_data)
            
            if updated:
                await self.send_payment_update_notifications(user_id, new_amount, new_tries, "Списание за примерку")
            
            return updated is not None

        except Exception as e:
            logger.error(f"Error decrementing tries: {e}")
            return False

    async def send_payment_update_notifications(self, user_id: int, new_amount: float, new_tries: int, reason: str):
        """Отправляет уведомления об изменении баланса"""
        try:
            user_row = await self.get_user_row(user_id)
            if not user_row:
                return

            username = user_row.get('username', '')
            
            try:
                await bot.send_message(
                    user_id,
                    f"💰 Мой баланс обновлен!\n"
                    f"📝 Причина: {reason}\n"
                    f"💳 Текущая сумма: {new_amount} руб.\n"
                    f"🎁 Доступно примерок: {new_tries}"
                )
            except Exception as e:
                logger.error(f"Error sending payment update to user: {e}")

            if ADMIN_CHAT_ID:
                try:
                    await bot.send_message(
                        ADMIN_CHAT_ID,
                        f"🔄 Изменение баланса у @{username} ({user_id})\n"
                        f"📝 Причина: {reason}\n"
                        f"💳 Текущая сумма: {new_amount} руб.\n"
                        f"🎁 Доступно примерок: {new_tries}"
                    )
                except Exception as e:
                    logger.error(f"Error sending admin payment notification: {e}")
                    
        except Exception as e:
            logger.error(f"Error in send_payment_update_notifications: {e}")

    async def upsert_row(self, user_id: int, username: str, data: dict):
        """Создает или обновляет запись пользователя в Supabase"""
        try:
            row = await self.get_user_row(user_id)
            data.update({
                "user_id": str(user_id),
                "username": username or "",
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
            })

            if row:
                res = self.supabase.table(USERS_TABLE)\
                    .update(data)\
                    .eq("user_id", str(user_id))\
                    .execute()
                result = res.data[0] if res.data else None
            else:
                data["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                res = self.supabase.table(USERS_TABLE)\
                    .insert(data)\
                    .execute()
                result = res.data[0] if res.data else None
            
            if 'payment_amount' in data and data['payment_amount'] > 0:
                payment_amount = data['payment_amount']
                tries_left = int(payment_amount / PRICE_PER_TRY)
                
                self.last_payment_amounts[user_id] = payment_amount
                self.last_tries_values[user_id] = tries_left
                
                await self.send_payment_update_notifications(
                    user_id, 
                    payment_amount, 
                    tries_left, 
                    "Пополнение баланса"
                )
                
                await self.update_user_row(user_id, {
                    ACCESS_FIELD: True,
                    TRIES_FIELD: tries_left,
                    STATUS_FIELD: "Оплачено",
                    FREE_TRIES_FIELD: True
                })
            
            return result
        except Exception as e:
            logger.error(f"Error in upsert_row: {e}")
            return None

    async def reset_flags(self, user_id: int):
        """Сбрасывает флаги обработки для пользователя"""
        try:
            return await self.update_user_row(user_id, {
                "photo1_received": False,
                "photo2_received": False,
                "ready": False
            }) is not None
        except Exception as e:
            logger.error(f"Error resetting flags: {e}")
            return False

    async def initialize_user(self, user_id: int, username: str):
        """Инициализирует пользователя в базе данных без начального баланса"""
        try:
            user_row = await self.get_user_row(user_id)
            
            if not user_row:
                await self.upsert_row(user_id, username, {
                    AMOUNT_FIELD: 0.0,
                    TRIES_FIELD: 0,
                    ACCESS_FIELD: False,
                    STATUS_FIELD: "Не оплачено",
                    FREE_TRIES_FIELD: False
                })
                
                logger.info(f"Initialized user {user_id} with zero balance")
            
        except Exception as e:
            logger.error(f"Error initializing user: {e}")

supabase_api = SupabaseAPI()

async def cleanup_resources():
    """Закрытие всех ресурсов и соединений"""
    logger.info("Cleaning up resources...")
    
    if 'session' in globals():
        await session.close()
    
    await bot.session.close()
    
    logger.info("All resources cleaned up")

async def on_shutdown():
    """Обработчик завершения работы"""
    try:
        logger.info("Shutting down...")
        
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        
        await asyncio.gather(*tasks, return_exceptions=True)
        
        await bot.delete_webhook()
        logger.info("Webhook removed")
        
        await cleanup_resources()
        
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")
    finally:
        logger.info("Bot successfully shut down")

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

async def get_user_tries(user_id: int) -> int:
    """Получение количества оставшихся примерок у пользователя с проверкой доступа"""
    tries = await supabase_api.check_and_update_access(user_id)
    return tries if tries is not None else 0

async def is_processing(user_id: int) -> bool:
    """Проверка, идет ли обработка для пользователя"""
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    if not os.path.exists(user_dir):
        return False
        
    photos = [
        f for f in os.listdir(user_dir)
        if f.startswith("photo_") and f.endswith(tuple(SUPPORTED_EXTENSIONS))
    ]
    model_selected = os.path.exists(os.path.join(user_dir, "selected_model.jpg"))
    
    return len(photos) >= 2 or (len(photos) >= 1 and model_selected)

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
    """Получает список примеров из папки primery в Supabase и сортирует их по имени"""
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
        
        examples.sort()
        
        logger.info(f"Found {len(examples)} examples")
        return examples
        
    except Exception as e:
        logger.error(f"Error getting examples list: {str(e)}", exc_info=True)
        return []

async def send_examples_page(chat_id: int, page: int = 0):
    """Отправляет страницу с примерами в строгом порядке"""
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
                f"Примеры {start_idx + 1}-{min(end_idx, len(examples))} из {len(examples)}. Выберите действие:",
                reply_markup=keyboard
            )
            
    except Exception as e:
        logger.error(f"Error sending examples: {e}")
        await bot.send_message(chat_id, "❌ Ошибка при загрузке примеров. Попробуйте позже.")

async def get_models_list(category: str):
    """Получает список моделей для указанной категории с кешированием"""
    if not supabase:
        logger.warning("Supabase client not available")
        return []
    
    current_time = time.time()
    if (current_time - models_cache[category]["time"]) < CACHE_EXPIRATION:
        logger.info(f"Using cached models for {category}")
        return models_cache[category]["data"]
    
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
        
        models_cache[category] = {
            "time": current_time,
            "data": models
        }
        
        logger.info(f"Found {len(models)} models in {category} category")
        return models
        
    except Exception as e:
        logger.error(f"Error getting models list for {category}: {str(e)}", exc_info=True)
        return []

async def notify_admin(message: str):
    """Отправка уведомления администратору"""
    if not ADMIN_CHAT_ID:
        return
        
    try:
        await bot.send_message(ADMIN_CHAT_ID, message)
    except Exception as e:
        logger.error(f"Error sending admin notification: {e}")

async def send_welcome(user_id: int, username: str, full_name: str):
    """Отправка приветственного сообщения"""
    try:
        await supabase_api.initialize_user(user_id, username)
        
        await send_initial_examples(user_id)
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👕 Загрузить одежду", callback_data="upload_clothes")],
            [InlineKeyboardButton(text="📸 Посмотреть примеры", callback_data="view_examples_0")]
        ])
        
        await bot.send_message(
            user_id,
            "<b>ВИРТУАЛЬНАЯ ПРИМЕРОЧНАЯ</b>\n\n"
            "👋 Привет! Это бот виртуальной примерки одежды.\n\n"
            "📌 <b>Как это работает:</b> \n\n"
            "1️⃣ Отправьте первое фото – <b>Одежда</b> (отправляйте только 1 фото, можно как отдельно, так и одетой на ком-нибудь)\n"
            "2️⃣ Отправьте второе фото – <b>Человек</b> (желательно в полный рост, 1 фото) или <b>Выберите готовую модель</b>\n\n"
            "💥 <b>Получите результат изображения виртуальной примерки!!💥</b> \n\n"
            "🔴 <b>Отправляйте по порядку сначала фото одежды, затем фото человека или выберите модель для примерки!!!</b> \n\n" 
            "👇 <b>Загрузите фото одежды:</b>👇",
            reply_markup=keyboard
        )
        
        await supabase_api.reset_flags(user_id)
        
        tries_left = await get_user_tries(user_id)
        
        await supabase_api.upsert_row(user_id, username, {
            "status": "started",
            "photo_clothes": False,
            "photo_person": False,
            "model_selected": None,
            "tries_left": tries_left
        }) 
        
        await notify_admin(f"🆕 Пользователь: @{username} ({user_id})")
        
    except Exception as e:
        logger.error(f"Welcome error for {user_id}: {e}")

@dp.message(Command("start"))
@dp.message(F.text & ~F.text.regexp(r'^\d+$'))
async def handle_start(message: types.Message):
    """Обработчик команды /start"""
    if await is_processing(message.from_user.id):
        await message.answer("✅ Оба файла получены.\n🔄 Идёт примерка. Ожидайте результат!")
        return
        
    await send_welcome(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )

@dp.callback_query(F.data == "upload_clothes")
async def upload_clothes_handler(callback_query: types.CallbackQuery):
    """Обработчик кнопки загрузки одежды"""
    try:
        await callback_query.message.answer(
            "👕 <b>Нажмите на Скрепку 📎,рядом с сообщением и загрузите своё изображение Одежды для примерки.</b>\n"
            "👇     👇     👇     👇    👇     👇"       
        )       
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in upload_clothes_handler: {e}")
        await callback_query.message.answer("⚠️ Ошибка при обработке запроса. Попробуйте позже.")
        await callback_query.answer()

@dp.callback_query(F.data == "choose_model")
async def choose_model(callback_query: types.CallbackQuery):
    """Выбор модели без преждевременной проверки is_processing"""
    try:
        user_id = callback_query.from_user.id

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👩 Женщины", callback_data="models_woman_0")],
            [InlineKeyboardButton(text="👨 Мужчины", callback_data="models_man_0")],
            [InlineKeyboardButton(text="🧒 Дети", callback_data="models_child_0")]
        ])

        await callback_query.message.answer("👇 Выберите категорию моделей:", reply_markup=keyboard)
        await callback_query.answer()

    except Exception as e:
        logger.error(f"Error in choose_model: {e}")
        await callback_query.message.answer("⚠️ Ошибка при загрузке категорий. Попробуйте позже.")
        await callback_query.answer()

@dp.callback_query(F.data.startswith("models_"))
async def show_category_models(callback_query: types.CallbackQuery):
    """Показывает модели выбранной категории"""
    start_time = time.time()
    try:
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
        
        models = await get_models_list(category)
        logger.info(f"Models to display for {category}: {models}")
        
        if not models:
            await callback_query.message.answer(f"❌ В данной категории пока нет доступных моделей.")
            await callback_query.answer()
            return

        start_idx = page * MODELS_PER_PAGE
        end_idx = start_idx + MODELS_PER_PAGE
        current_models = models[start_idx:end_idx]
        
        if page == 0:
            await callback_query.message.answer(f"{category_names.get(category, 'Модели')}:")
            await callback_query.answer()

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
                                text="⬇️ Показать ещё",
                                callback_data=f"models_{category}_{page + 1}"
                            ),
                            InlineKeyboardButton(
                                text="👤 Своё фото",
                                callback_data="upload_person"
                            ),
                            InlineKeyboardButton(
                                text="🔙 Назад к категориям",
                                callback_data="choose_model"
                            )
                        ]
                    ]
                )
            )
            await callback_query.answer()
        else:
            await callback_query.message.answer("✅ Это все доступные модели в данной категории.")
            await callback_query.answer()

    except Exception as e:
        logger.error(f"Error in show_category_models: {e}")
        try:
            await callback_query.message.answer("⚠️ Ошибка при загрузке моделей. Попробуйте позже.")
        except:
            pass
    finally:
        logger.info(f"show_category_models executed in {time.time() - start_time:.2f}s")

@dp.callback_query(F.data.startswith("model_"))
async def model_selected(callback_query: types.CallbackQuery):
    """Обработчик выбора конкретной модели"""
    user_id = callback_query.from_user.id

    tries_left = await get_user_tries(user_id)
    if tries_left <= 0:
        await show_payment_options(callback_query.from_user)
        await callback_query.answer()
        return

    if await is_processing(user_id):
        try:
            await callback_query.answer("✅ Оба файла получены. Ожидайте результат!", show_alert=True)
        except TelegramBadRequest:
            logger.warning("Callback query expired for processing check")
        return

    model_path = callback_query.data.replace("model_", "")
    category, model_name = model_path.split('/')

    try:
        user_dir = os.path.join(UPLOAD_DIR, str(user_id))
        os.makedirs(user_dir, exist_ok=True)
        model_local_path = os.path.join(user_dir, "photo_2.jpg")

        res = supabase.storage.from_(MODELS_BUCKET).download(f"{category}/{model_name}")
        with open(model_local_path, 'wb') as f:
            f.write(res)

        logger.info(f"✅ Модель {model_name} загружена и сохранена как photo_2.jpg для пользователя {user_id}")

        model_preview = FSInputFile(model_local_path)
        await bot.send_photo(
            chat_id=user_id,
            photo=model_preview,
            caption="📸 Вы выбрали эту модель для примерки."
        )

        await upload_to_supabase(model_local_path, user_id, "photos")
        await supabase_api.upsert_row(user_id, callback_query.from_user.username or "", {
            "photo1_received": True,
            "photo2_received": True,
            "status": "В обработке",
            "last_try_date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "username": callback_query.from_user.username or ""
        })

        await supabase_api.decrement_tries(user_id)

        await callback_query.message.answer("✅ Модель выбрана. 🔄 Идёт примерка. Ожидайте результат!")
        await notify_admin(f"📸 Все фото получены от @{callback_query.from_user.username} ({user_id})")
        await callback_query.answer()

    except Exception as e:
        logger.error(f"❌ Ошибка при выборе модели: {e}")
        await callback_query.message.answer("⚠️ Не удалось загрузить модель. Попробуйте другую.")
        await callback_query.answer()

@dp.callback_query(F.data.startswith("view_examples_"))
async def view_examples(callback_query: types.CallbackQuery):
    """Просмотр примеров работ"""
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
    """Возврат в главное меню"""
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
    """Загрузка дополнительных примеров"""
    try:
        page = int(callback_query.data.split("_")[-1])
        await send_examples_page(callback_query.from_user.id, page)
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in more_examples: {e}")
        await callback_query.message.answer("⚠️ Ошибка при загрузке примеров. Попробуйте позже.")
        await callback_query.answer()

async def process_photo(message: types.Message, user: types.User, user_dir: str):
    """Обработка и сохранение фотографии"""
    try:
        user_id = user.id
        user_dir = os.path.join(UPLOAD_DIR, str(user_id))
        os.makedirs(user_dir, exist_ok=True)

        photo = message.photo[-1]
        file_id = photo.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path

        try:
            supabase_files = supabase.storage.from_(UPLOADS_BUCKET).list(f"{user_id}/photos")
            existing = [f['name'] for f in supabase_files]
        except Exception as e:
            logger.warning(f"⚠️ Ошибка чтения файлов из Supabase для {user_id}: {e}")
            existing = []

        if "photo_1.jpg" not in existing:
            photo_type = 1
            filename = f"photo_1{os.path.splitext(file_path)[1]}"
            caption = "✅ Фото одежды получено! Теперь отправьте фото на кого будем примерять 👩‍⚖️👨‍⚕"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="👤 Своё фото", callback_data="upload_person"),
                    InlineKeyboardButton(text="👫 Выбрать модель", callback_data="choose_model")
                ]
            ])
        elif "photo_2.jpg" not in existing:
            photo_type = 2
            filename = f"photo_2{os.path.splitext(file_path)[1]}"
            caption = "✅ Оба файла получены.\n🔄 Идёт примерка. Ожидайте результат!"
            keyboard = None
            await notify_admin(f"📸 Все фото получены от @{user.username} ({user_id})")
        else:
            await message.answer("❗ Вы уже загрузили оба фото. Ожидайте результат.")
            return
            
        local_path = os.path.join(user_dir, filename)
        await bot.download_file(file_path, local_path)

        await upload_to_supabase(local_path, user_id, "photos")

        user_row = await supabase_api.get_user_row(user_id)
        current_username = user_row.get('username', '') if user_row else (user.username or '')

        if photo_type == 1:
            await supabase_api.upsert_row(user_id, current_username, {
                "photo1_received": True,
                "photo2_received": False,
                "status": "Ожидается фото человека",
                "username": current_username
            })
        else:
            await supabase_api.upsert_row(user_id, current_username, {
                "photo1_received": True,
                "photo2_received": True,
                "status": "В обработке",
                "last_try_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                "username": current_username
            })

            await supabase_api.decrement_tries(user_id)

        if keyboard:
            await message.answer(caption, reply_markup=keyboard)
        else:
            await message.answer(caption)

    except Exception as e:
        logger.error(f"Error processing photo: {e}")
        await message.answer("❌ Ошибка при обработке фото. Попробуйте ещё раз.")
        raise

@dp.callback_query(F.data == "upload_person")
async def upload_person_handler(callback_query: types.CallbackQuery):
    """Обработчик кнопки загрузки фото человека"""
    try:
        await callback_query.message.answer(
            "<b>👤Нажмите на Скрепку📎, рядом с сообщением и загрузите фото Человека для примерки</b>\n"
            "👇     👇     👇     👇    👇     👇"       
        )
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in upload_person_handler: {e}")
        await callback_query.message.answer("⚠️ Ошибка при обработке запроса. Попробуйте позже.")
        await callback_query.answer()

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    """Обработчик фотографий"""
    user = message.from_user
    user_id = user.id
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    try:
        tries_left = await get_user_tries(user_id)
        
        if tries_left <= 0:
            await show_payment_options(user)
            return
            
        await process_photo(message, user, user_dir)
        
    except Exception as e:
        logger.error(f"Error handling photo: {e}")
        await message.answer("❌ Ошибка при сохранении файла. Попробуйте ещё раз.")

async def show_balance_info(user: types.User):
    """Показывает информацию о балансе пользователя"""
    try:
        user_row = await supabase_api.get_user_row(user.id)
        
        if not user_row:
            await bot.send_message(user.id, "❌ Ваши данные не найдены. Пожалуйста, начните с команды /start")
            return
            
        payment_amount = float(user_row.get(AMOUNT_FIELD, 0)) if user_row.get(AMOUNT_FIELD) else 0.0
        tries_left = int(user_row.get(TRIES_FIELD, 0)) if user_row.get(TRIES_FIELD) else 0
        status = user_row.get(STATUS_FIELD, "Неизвестно")
        free_tries_used = bool(user_row.get(FREE_TRIES_FIELD, False))
        
        message_text = (
            "💰 <b>Мой баланс:</b>\n\n"
            f"💳 Сумма на счету: <b>{payment_amount} руб.</b>\n"
            f"🎁 Доступно примерок: <b>{tries_left}</b>\n"
            f"📊 Статус: <b>{status}</b>\n"
            f"🆓 Бесплатная проверка: <b>{'использована' if free_tries_used else 'доступна'}</b>\n\n"
            f"ℹ️ Стоимость одной примерки: <b>{PRICE_PER_TRY} руб.</b>"
        )
        
        await bot.send_message(
            user.id,
            message_text,
            parse_mode=ParseMode.HTML
        )
        
    except Exception as e:
        logger.error(f"Error showing balance info: {e}")
        await bot.send_message(
            user.id,
            "❌ Ошибка при получении информации о балансе. Попробуйте позже."
        )

async def show_payment_options(user: types.User):
    """Показывает варианты оплаты через ЮMoney"""
    try:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить картой (минималка 60 рублей)", 
                    url=YOO_MONEY_CARD_LINK.format(user=user)
                )
            ],
            [
                InlineKeyboardButton(
                    text="📱 Оплатить СБП", 
                    callback_data="pay_sbp"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Оплатить по QR", 
                    callback_data="pay_qr"
                )    
            ],
            [
                InlineKeyboardButton(
                    text="✅ Я Оплатил(а)",
                    callback_data="payment_confirmation"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Мой баланс",
                    callback_data="check_balance"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Назад",
                    callback_data="back_to_menu"
                )
            ]
        ])
        
        payment_text = (
            "🚫 У вас закончились бесплатные примерки.\n\n"
            "❤️Спасибо, что воспользовались нашей Виртуальной примерочной!!!🥰\n"
            "Первая примерка была демонстрационной, последующие примерки стоят 30 рублей за примерку.\n"
            "Сумма символическая, но которая поможет Вам стать стильными, модными и красивыми\n"
            "👗👔🩳👙👠👞👒👟🧢🧤👛👜\n\n"
            "📌 <b>Для продолжения примерок необходимо пополнить баланс:</b>\n\n"
            "💰 <b>Тарифы:</b>\n"
            f"- 30 руб = 1 примерка\n"
            f"- 60 руб = 2 примерки\n"
            f"- 90 руб = 3 примерки\n"
            "и так далее...\n\n"
            "1️⃣ Выберите удобный способ оплаты (картой или через СБП)\n\n"
            "2️⃣ Введите сумму кратно 30 рублям (30, 60, 90 и т.д.)\n\n"
            "3️⃣ <b>Обязательно укажите в комментарии к платежу:</b>\n\n"
            "👇👇👇👇👇👇👇👇👇👇\n"
            f"<code>ОПЛАТА ЗА ПРИМЕРКИ от @{user.username or 'ваш_ник'}</code>\n\n"
            "<b>Просто нажмите на это сообщение, оно скопируется и вставьте его в поле для комментария</b>\n\n"
            "<b>🤷‍♂️Иначе не будет понятно кому начислять баланс.</b>\n"
            "‼️<b>Ничего не меняйте в сообщении‼️</b>\n\n"
            "❓Свой баланс Вы можете отслеживать по кнопке 'Мой баланс'"
        )
        
        await bot.send_message(
            user.id,
            payment_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
        
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    f"💸 Пользователь @{user.username} ({user.id}) начал процесс оплаты\n"
                    f"ℹ️ Сообщение для ЮMoney: 'ОПЛАТА ЗА ПРИМЕРКИ от @{user.username}'"
                )
            except Exception as e:
                logger.error(f"Error sending admin payment notification: {e}")
                
    except Exception as e:
        logger.error(f"Error sending payment options: {e}")
        await bot.send_message(
            user.id,
            "❌ Ошибка при формировании ссылки оплаты. Пожалуйста, свяжитесь с администратором."
        )

@dp.callback_query(F.data == "pay_qr")
async def pay_qr_handler(callback_query: types.CallbackQuery):
    """Обработчик кнопки оплаты по QR-коду"""
    user = callback_query.from_user
    qr_image = "https://jikkylblsmeuhbsewbkz.supabase.co/storage/v1/object/public/qr/yoomoney_qr.png"

    caption = (
        "📲 <b>Как оплатить по QR-коду:</b>\n\n"
        "1️⃣ Откройте приложение вашего банка\n"
        "2️⃣ Перейдите в раздел «СБП» или «Сканировать QR»\n"
        "3️⃣ Отсканируйте этот код\n\n"
        "💰 Введите сумму (например, 30 ₽)\n\n"
        "‼️💬 <b>ОБЯЗАТЕЛЬНО в комментарии к оплате вставьте:</b>\n"
        f"<code>ОПЛАТА ЗА ПРИМЕРКИ от @{user.username or 'ваш_ник'}</code>\n"
        "🔹 <i>Нажмите, чтобы скопировать</i>\n\n"
        "📌 <b>Если вы открыли бот на телефоне:</b>\n"
        "Сохраните изображение, откройте его на другом устройстве и отсканируйте QR\n\n"
        "🔁 После оплаты — нажмите кнопку «Я оплатил»"
    )

    await bot.send_photo(
        chat_id=callback_query.from_user.id,
        photo=qr_image,
        caption=caption,
        parse_mode=ParseMode.HTML
    )
    await callback_query.answer()

@dp.callback_query(F.data == "payment_confirmation")
async def payment_confirmation_handler(callback_query: types.CallbackQuery, state: FSMContext):
    """Обработчик подтверждения оплаты"""
    try:
        user = callback_query.from_user
        await state.set_data({"user_id": user.id})
        
        # Уведомление администратору
        if ADMIN_CHAT_ID:
            admin_message = (
                f"💰 Пользователь нажал 'Я оплатил'\n\n"
                f"👤 ID: {user.id}\n"
                f"📛 Username: @{user.username if user.username else 'нет'}\n"
                f"📝 Имя в Telegram: {user.full_name}\n\n"
                f"Ожидает подтверждения платежа"
            )
            await bot.send_message(ADMIN_CHAT_ID, admin_message)
        
        # Уведомление клиенту
        await bot.send_message(
            user.id,
            "✅ Ваше подтверждение оплаты получено!\n\n"
            "🔍 Ваш платёж проверяется администратором.\n"
            "После проверки вам придёт уведомление о пополнении баланса,\n"
            "и вы сможете продолжить примерки.\n\n"
            "⏳ Обычно проверка занимает не более 15 минут."
        )
        
        await callback_query.answer("Ваше подтверждение оплаты получено!")
        
    except Exception as e:
        logger.error(f"Error in payment_confirmation_handler: {e}")
        await callback_query.message.answer("⚠️ Ошибка при обработке запроса. Попробуйте позже.")
        await callback_query.answer()

@dp.message(PaymentFSM.waiting_for_fio_and_amount, F.text)
async def process_fio_and_amount(message: types.Message, state: FSMContext):
    """Обработка введенных ФИО и суммы платежа"""
    try:
        user_data = await state.get_data()
        user_id = user_data.get("user_id", message.from_user.id)
        user = message.from_user
        input_text = message.text.strip()
        
        parts = input_text.rsplit(' ', 1)
        if len(parts) != 2:
            await message.answer("⚠️ Неверный формат. Введите ФИО и сумму через пробел.\nПример: <code>Иванов Иван Иванович 60</code>", parse_mode="HTML")
            return
            
        fio, amount_str = parts
        
        try:
            amount = float(amount_str)
            if amount < 30:
                await message.answer("⚠️ Минимальная сумма платежа - 30 рублей.")
                return
        except ValueError:
            await message.answer("⚠️ Сумма должна быть числом. Пример: <code>Иванов Иван Иванович 60</code>", parse_mode="HTML")
            return

        await supabase_api.upsert_row(
            user.id,
            user.username or "",
            {
                "fio": fio,
                "payment_amount": amount,
                "status": "Ожидает подтверждения",
                "payment_confirmation": True,
                "username": user.username or ""
            }
        )

        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    f"💰 Новое подтверждение оплаты\n\n"
                    f"👤 Пользователь: @{user.username or 'нет'} ({user.id})\n"
                    f"📛 ФИО: {fio}\n"
                    f"💳 Сумма: {amount} руб.\n\n"
                    f"Для подтверждения используйте команду:\n"
                    f"<code>/confirm_payment {user.id} {amount}</code>",
                    parse_mode="HTML"
                )
            except Exception as admin_error:
                logger.error(f"Error sending admin notification: {admin_error}")

        await message.answer(
            "✅ Ваши данные и сумма платежа получены!\n\n"
            "Администратор проверит платёж и откроет доступ.\n"
            "Вы получите уведомление, когда баланс будет пополнен.\n\n"
            "Обычно это занимает не более 15 минут."
        )
        await state.clear()

    except Exception as e:
        logger.error(f"Ошибка обработки ФИО и суммы: {e}", exc_info=True)
        await message.answer("❌ Ошибка при обработке данных. Попробуйте позже.")
        await state.clear()

@dp.message(Command("confirm_payment"))
async def confirm_payment_cmd(message: types.Message):
    """Подтверждение платежа администратором"""
    if str(message.from_user.id) != ADMIN_CHAT_ID:
        return await message.answer("⛔ Доступ запрещен")

    try:
        _, user_id_str, amount_str = message.text.split()
        user_id = int(user_id_str)
        amount = float(amount_str)
        
        tries = int(amount // PRICE_PER_TRY)
        
        user_row = await supabase_api.get_user_row(user_id)
        if not user_row:
            return await message.answer("❌ Пользователь не найден")
            
        fio = user_row.get('fio', 'Не указано')
        username = user_row.get('username', 'Не указано')
        
        await supabase_api.upsert_row(
            user_id,
            username,
            {
                "payment_amount": amount,
                "tries_left": tries,
                "access_granted": True,
                "status": "Оплачено",
                "payment_confirmation": False
            }
        )
        
        try:
            await bot.send_message(
                user_id,
                f"🎉 Ваш баланс пополнен на {amount} руб.! Доступно {tries} примерок.\n\n"
                f"📛 Ваше ФИО: {fio}\n"
                "Теперь вы можете продолжить примерку!"
            )
        except Exception as user_notify_error:
            logger.error(f"Error notifying user: {user_notify_error}")
            await message.answer(f"✅ Баланс обновлён, но не удалось уведомить пользователя: {user_notify_error}")
            return
            
        await message.answer(
            f"✅ Баланс пользователя {user_id} пополнен на {amount} руб. ({tries} примерок)\n"
            f"👤 @{username}\n"
            f"📛 ФИО: {fio}"
        )

    except Exception as e:
        logger.error(f"Ошибка подтверждения платежа: {e}", exc_info=True)
        await message.answer("❌ Используйте: /confirm_payment user_id amount")

@dp.callback_query(F.data == "check_balance")
async def check_balance_handler(callback_query: types.CallbackQuery):
    """Обработчик кнопки проверки баланса"""
    try:
        await show_balance_info(callback_query.from_user)
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in check_balance_handler: {e}")
        await callback_query.message.answer("⚠️ Ошибка при проверке баланса. Попробуйте позже.")
        await callback_query.answer()

async def check_results():
    """Отслеживает загрузку result.png или result.jpg и отправляет его нужному клиенту"""
    logger.info("🔄 Starting check_results() loop...")
    while True:
        try:
            logger.info("🔍 Scanning for ready results in Supabase...")

            folders = supabase.storage.from_(UPLOADS_BUCKET).list()
            user_dirs = [f['name'].rstrip('/') for f in folders if f['name'].isdigit()]
            for user_id in user_dirs:
                user_dir = os.path.join(UPLOAD_DIR, str(user_id))
                os.makedirs(user_dir, exist_ok=True)

                user_row = await supabase_api.get_user_row(int(user_id))
                if not user_row:
                    continue

                result_files = supabase.storage.from_(UPLOADS_BUCKET).list(f"{user_id}")
                result_names = [f["name"] for f in result_files]
                logger.info(f"📦 Файлы пользователя {user_id}: {result_names}")

                has_png = "result.png" in result_names
                has_jpg = "result.jpg" in result_names

                if not has_png and not has_jpg:
                    logger.info(f"⏩ Пропускаем пользователя {user_id} — результат не найден")
                    continue

                if user_row.get("result_sent"):
                    expected_file = "result.png" if has_png else "result.jpg"
                    if expected_file not in result_names:
                        logger.info(f"⏩ Пропускаем пользователя {user_id} — результат уже отправлен и файл отсутствует")
                        continue
                    else:
                        logger.warning(f"⚠️ {user_id} помечен как отправленный, но файл {expected_file} всё ещё существует — пробуем повторно")

                result_filename = "result.png" if has_png else "result.jpg"
                result_path = f"{user_id}/{result_filename}"
                result_file_local = os.path.join(user_dir, result_filename)

                try:
                    res = supabase.storage.from_(UPLOADS_BUCKET).download(result_path)
                    with open(result_file_local, 'wb') as f:
                        f.write(res)
                    logger.info(f"✅ Загружен {result_filename} для {user_id}")
                except Exception as e:
                    logger.warning(f"⚠️ {result_filename} ещё не загружен для {user_id}: {e}")
                    continue

                current_username = user_row.get("username", "") if user_row else ""
                try:
                    photo = FSInputFile(result_file_local)
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="🔄 Продолжить примерку", callback_data="continue_tryon")],
                            [
                                InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="show_payment_options"),
                                InlineKeyboardButton(text="💰 Мой баланс", callback_data="check_balance")
                            ]
                        ]
                    )
                    await bot.send_photo(
                        chat_id=user_id,
                        photo=photo,
                        caption="🎉 Ваша виртуальная примерка готова!",
                        reply_markup=keyboard
                    )
                except Exception as send_error:
                    logger.error(f"❌ Ошибка отправки результата для {user_id}: {send_error}")
                    continue

                try:
                    supabase.storage.from_(UPLOADS_BUCKET).remove([
                        f"{user_id}/result.jpg",
                        f"{user_id}/result.png",
                        f"{user_id}/photos/photo_1.jpg",
                        f"{user_id}/photos/photo_1.png",
                        f"{user_id}/photos/photo_2.jpg",
                        f"{user_id}/photos/photo_2.png"
                    ])
                    logger.info(f"🧹 Удалены все файлы пользователя {user_id} из Supabase")
                except Exception as e:
                    logger.error(f"❌ Не удалось удалить файлы пользователя {user_id}: {e}")

                await notify_admin(f"📤 Отправлен результат для @{current_username} ({user_id})")
                await supabase_api.upsert_row(user_id, current_username, {
                    "ready": True,
                    "status": "Результат отправлен",
                    "result_sent": True,
                    "username": current_username
                })

            await asyncio.sleep(30)

        except Exception as e:
            logger.error(f"❌ Ошибка в check_results(): {e}")
            await asyncio.sleep(30)

@dp.callback_query(F.data == "pay_sbp")
async def pay_sbp_handler(callback_query: types.CallbackQuery):
    """Обработчик кнопки оплаты через СБП"""
    user = callback_query.from_user
    sbp_instructions = f"""📲 <b>Инструкция по оплате через СБП на ЮMoney:</b>

🔧 Как оплатить по СБП:

1️⃣ Зайдите в приложение вашего банка  
2️⃣ Найдите раздел «Переводы по номеру телефона» или «СБП»  
3️⃣ Введите номер получателя:
📞 <code>+7 705 541-27-09</code>
🔹 <i>Нажмите, чтобы скопировать</i>

4️⃣ Обязательно выберите из списка банков:
🏦 <b>ЮMoney</b>  
(если не выбрать вручную — платёж может не пройти)

5️⃣ Введите сумму (например, 30, 60, 90 рублей)

‼️ 6️⃣ <b>ОБЯЗАТЕЛЬНО</b> вставьте в комментарий к переводу:
💬 <code>ОПЛАТА ЗА ПРИМЕРКИ от @{user.username or 'ваш_никнейм'}</code>
🔹 <i>Нажмите, чтобы скопировать</i>

7️⃣ Подтвердите платёж 👍

⚠️ Без комментария мы не сможем определить, от кого платёж.

🔁 После оплаты — нажмите кнопку «Я оплатил».
"""
    await callback_query.message.answer(sbp_instructions, parse_mode=ParseMode.HTML)
    await callback_query.answer()

@dp.callback_query(F.data == "continue_tryon")
async def continue_tryon_handler(callback_query: types.CallbackQuery):
    """Обработчик кнопки продолжения примерки"""
    try:
        user_id = callback_query.from_user.id

        try:
            supabase.storage.from_(UPLOADS_BUCKET).remove([
                f"{user_id}/photos/photo_1.jpg",
                f"{user_id}/photos/photo_2.jpg"
            ])
            logger.info(f"🧹 Старые фото удалены для пользователя {user_id}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось удалить старые фото для {user_id}: {e}")

        await supabase_api.reset_flags(user_id)
        try:
            user_dir = os.path.join(UPLOAD_DIR, str(user_id))
            if os.path.exists(user_dir):
                for filename in os.listdir(user_dir):
                    file_path = os.path.join(user_dir, filename)
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                logger.info(f"🧹 Локальные файлы удалены для пользователя {user_id}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось удалить локальные файлы для {user_id}: {e}")

        await send_welcome(
            user_id,
            callback_query.from_user.username,
            callback_query.from_user.full_name
        )

        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in continue_tryon_handler: {e}")
        await callback_query.message.answer("⚠️ Ошибка при обработке запроса. Попробуйте позже.")
        await callback_query.answer()

@dp.callback_query(F.data == "show_payment_options")
async def show_payment_options_handler(callback_query: types.CallbackQuery):
    """Обработчик кнопки показа вариантов оплаты"""
    try:
        await show_payment_options(callback_query.from_user)
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in show_payment_options_handler: {e}")
        await callback_query.message.answer("⚠️ Ошибка при обработке запроса. Попробуйте позже.")
        await callback_query.answer()

async def monitor_payment_changes_task():
    """Фоновая задача для мониторинга изменений payment_amount"""
    logger.info("Starting payment amount monitoring task...")
    while True:
        try:
            res = supabase.table(USERS_TABLE)\
                .select("user_id, payment_amount, username")\
                .execute()
            
            current_payments = {int(user['user_id']): float(user['payment_amount']) 
                              for user in res.data if user.get('payment_amount')}
            
            for user_id, current_amount in current_payments.items():
                previous_amount = supabase_api.last_payment_amounts.get(user_id, 0)
                
                if current_amount != previous_amount:
                    tries_left = int(current_amount / PRICE_PER_TRY)
                    
                    supabase_api.last_payment_amounts[user_id] = current_amount
                    supabase_api.last_tries_values[user_id] = tries_left
                    
                    user_row = await supabase_api.get_user_row(user_id)
                    if not user_row:
                        continue
                    
                    username = user_row.get('username', '') if user_row.get('username') else ''
                    
                    await supabase_api.update_user_row(user_id, {
                        ACCESS_FIELD: True if current_amount > 0 else False,
                        TRIES_FIELD: tries_left,
                        STATUS_FIELD: "Оплачено" if current_amount > 0 else "Не оплачено",
                        FREE_TRIES_FIELD: True
                    })
                    
                    await supabase_api.send_payment_update_notifications(
                        user_id,
                        current_amount,
                        tries_left,
                        "Изменение баланса"
                    )
            
            await asyncio.sleep(5)
            
        except Exception as e:
            logger.error(f"Error in payment monitoring task: {e}")
            await asyncio.sleep(3)

async def handle(request):
    """Обработчик корневого запроса"""
    return web.Response(text="Bot is running")

async def health_check(request):
    """Обработчик health check"""
    return web.Response(text="OK", status=200)

def setup_web_server():
    """Настройка веб-сервера"""
    app = web.Application()
    
    app.router.add_get('/', handle)
    app.router.add_get('/health', health_check)
    app.router.add_post(f'/{BOT_TOKEN.split(":")[1]}', webhook_handler)
    return app

async def webhook_handler(request):
    """Обработчик вебхука"""
    try:
        update = await request.json()
        await dp.feed_webhook_update(bot, update)
        return web.Response(text="OK")
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500, text="Internal Server Error")

async def start_web_server():
    """Запуск веб-сервера"""
    app = setup_web_server()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

async def main():
    """Основная функция запуска бота"""
    try:
        logger.info("Starting bot...")
        
        await start_web_server()
        
        webhook_url = f"https://virtual-tryon-bot-3n0o.onrender.com/{BOT_TOKEN.split(':')[1]}"
        await bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
        )
        logger.info(f"Webhook set to: {webhook_url}")
        
        asyncio.create_task(check_results())
        asyncio.create_task(monitor_payment_changes_task())
        
        while True:
            await asyncio.sleep(3600)
            
    except Exception as e:
        logger.error(f"Error in main: {e}")
        raise

if __name__ == "__main__":
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by keyboard interrupt")
        
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        
    finally:
        loop.run_until_complete(on_shutdown())
        loop.close()
        logger.info("Bot successfully shut down")

@dp.message()
async def fallback_handler(message: types.Message, state: FSMContext):
    current = await state.get_state()
    if current:
        return
        
    await message.answer("👋 Выберите действие из меню ниже.")