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
from urllib.parse import quote

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
UPLOADS_BUCKET = "uploads"  # Бакет для загружаемых файлов
SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
MODELS_PER_PAGE = 3
EXAMPLES_PER_PAGE = 3
DONATION_ALERTS_TOKEN = os.getenv("DONATION_ALERTS_TOKEN", "").strip()
PORT = int(os.getenv("PORT", 4000))
DONATION_ALERTS_USERNAME = "primerochnay777"  # Имя пользователя DonationAlerts

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

# Инициализация Supabase с настройками для реального времени
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
        self.last_payment_amounts = {}  # Кэш последних значений платежей
        self.last_tries_values = {}     # Кэш последних значений примерок

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

            # Если есть оплата, но доступ не предоставлен - предоставляем доступ
            if payment_amount > 0 and not row.get(ACCESS_FIELD, False):
                tries_left = int(payment_amount / PRICE_PER_TRY)
                await self.update_user_row(user_id, {
                    ACCESS_FIELD: True,
                    TRIES_FIELD: tries_left,
                    STATUS_FIELD: "Оплачено"
                })
                return tries_left

            # Проверяем бесплатные попытки
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
            
            # Для бесплатной проверки не вычитаем сумму
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
            
            # Отправляем уведомления о списании
            if updated:
                await self.send_payment_update_notifications(user_id, new_amount, new_tries, "Списание за примерку")
            
            return updated is not None

        except Exception as e:
            logger.error(f"Error decrementing tries: {e}")
            return False

    async def send_payment_update_notifications(self, user_id: int, new_amount: float, new_tries: int, reason: str):
        """Отправляет уведомления об изменении баланса"""
        try:
            # Получаем данные пользователя
            user_row = await self.get_user_row(user_id)
            if not user_row:
                return

            username = user_row.get('username', '')
            
            # Уведомление пользователю
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

            # Уведомление администратору
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
            
            # Проверяем, была ли изменена сумма оплаты
            if 'payment_amount' in data and data['payment_amount'] > 0:
                payment_amount = data['payment_amount']
                tries_left = int(payment_amount / PRICE_PER_TRY)
                
                # Обновляем кэш
                self.last_payment_amounts[user_id] = payment_amount
                self.last_tries_values[user_id] = tries_left
                
                # Отправляем уведомления
                await self.send_payment_update_notifications(
                    user_id, 
                    payment_amount, 
                    tries_left, 
                    "Пополнение баланса"
                )
                
                # Обновляем доступ и количество попыток
                await self.update_user_row(user_id, {
                    ACCESS_FIELD: True,
                    TRIES_FIELD: tries_left,
                    STATUS_FIELD: "Оплачено",
                    FREE_TRIES_FIELD: True  # Помечаем, что бесплатная проверка использована
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
            # Проверяем, есть ли уже пользователь в базе
            user_row = await self.get_user_row(user_id)
            
            # Если пользователя нет, создаем запись с нулевым балансом
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
    """Получает список моделей для указанной категории с кешированием"""
    if not supabase:
        logger.warning("Supabase client not available")
        return []
    
    # Проверяем кеш
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
        
        # Обновляем кеш
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
        # Инициализируем пользователя (если нужно)
        await supabase_api.initialize_user(user_id, username)
        
        # Отправляем первые три примера
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
        
        # Получаем текущее количество попыток
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
    """Выбор модели"""
    if await is_processing(callback_query.from_user.id):
        try:
            await callback_query.answer("✅ Оба файла получены. Ожидайте результат!", show_alert=True)
        except TelegramBadRequest:
            logger.warning("Callback query expired for processing check")
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
        if await is_processing(callback_query.from_user.id):
            try:
                await callback_query.answer("✅ Оба файла получены. Ожидайте результат!", show_alert=True)
            except TelegramBadRequest:
                logger.warning("Callback query expired for processing check")
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
            keyboard_buttons = [
                InlineKeyboardButton(
                    text="⬇️ Показать еще",
                    callback_data=f"models_{category}_{page + 1}"
                ),
                InlineKeyboardButton(
                    text="📷 Своё фото",
                    callback_data="upload_person"
                )
            ]
            
            await callback_query.message.answer(
                "Выберите действие:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[keyboard_buttons])
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
    
    # Проверяем количество оставшихся примерок
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
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    try:
        await callback_query.message.delete()
        
        clothes_photo_exists = any(
            f.startswith("photo_1") and f.endswith(tuple(SUPPORTED_EXTENSIONS))
            for f in os.listdir(user_dir)
        )

        model_display_name = os.path.splitext(model_name)[0]
        await supabase_api.upsert_row(user_id, callback_query.from_user.username, {
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
                    await supabase_api.upsert_row(user_id, callback_query.from_user.username, {
                        "photo_person": True,
                        "status": "В обработке",
                        "photo1_received": True,
                        "photo2_received": True,
                        "last_try_date": time.strftime("%Y-%m-%d %H:%M:%S")
                    })
                    
                    await supabase_api.decrement_tries(user_id)
                    
                    await notify_admin(f"📸 Все фото получены от @{callback_query.from_user.username} ({user_id})")
                else:
                    response_text = (
                        f"✅ Модель {model_display_name} выбрана.\n\n"
                        "📸 Теперь отправьте фото одежды."
                    )
                    await supabase_api.upsert_row(user_id, callback_query.from_user.username, {
                        "photo1_received": False,
                        "photo2_received": True
                    })
                
                # Отправляем новое сообщение с фото модели в самый низ
                await bot.send_photo(
                    chat_id=user_id,
                    photo=model_url,
                    caption=response_text
                )
                await callback_query.answer()
                
            except Exception as e:
                logger.error(f"Error downloading model: {e}")
                await bot.send_message(
                    user_id,
                    "❌ Ошибка загрузки модели. Попробуйте выбрать другую."
                )
                await callback_query.answer()
                return
            
    except Exception as e:
        logger.error(f"Error in model_selected: {e}")
        await bot.send_message(
            user_id,
            "⚠️ Произошла ошибка при выборе модели. Попробуйте позже."
        )
        await callback_query.answer()

@dp.callback_query(F.data.startswith("view_examples_"))
async def view_examples(callback_query: types.CallbackQuery):
    """Обработчик просмотра примеров работ"""
    try:
        page = int(callback_query.data.split("_")[-1])
        await send_examples_page(callback_query.from_user.id, page)
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in view_examples: {e}")
        await callback_query.answer("⚠️ Ошибка при загрузке примеров")

@dp.callback_query(F.data.startswith("more_examples_"))
async def more_examples(callback_query: types.CallbackQuery):
    """Обработчик кнопки 'Посмотреть ещё' примеров"""
    try:
        page = int(callback_query.data.split("_")[-1])
        await send_examples_page(callback_query.from_user.id, page)
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in more_examples: {e}")
        await callback_query.answer("⚠️ Ошибка при загрузке примеров")

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
        await callback_query.answer("⚠️ Ошибка при возврате в меню")

@dp.callback_query(F.data == "upload_person")
async def upload_person_handler(callback_query: types.CallbackQuery):
    """Обработчик кнопки загрузки фото человека"""
    try:
        if await is_processing(callback_query.from_user.id):
            await callback_query.answer("✅ Оба файла получены. Ожидайте результат!", show_alert=True)
            return
            
        await callback_query.message.answer(
            "👤 <b>Нажмите на Скрепку 📎 и загрузите фото человека (желательно в полный рост)</b>\n"
            "👇     👇     👇     👇    👇     👇"
        )
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in upload_person_handler: {e}")
        await callback_query.message.answer("⚠️ Ошибка при обработке запроса")
        await callback_query.answer()

async def show_payment_options(user: types.User):
    """Показывает варианты оплаты"""
    try:
        payment_message = (
            "⚠️‼️ <b>Скопируйте это сообщение, нажмите кнопку \"Оплатить\" и на странице оплаты вставьте его в поле для сообщений, которое находится под оплатой</b>\n"
            "👇👇👇👇👇👇👇👇👇👇\n"
            f"<code>ОПЛАТА ЗА ПРИМЕРКИ от @{user.username}</code>\n\n"
            "Просто нажмите на это сообщение, оно скопируется и вставьте его в поле для сообщений\n\n"
            "🤷‍♂️Иначе не будет понятно кому начислять баланс.\n"
            "‼️<b>Ничего не меняйте в сообщении‼️</b>"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="💳 Оплатить", 
                url=f"https://www.donationalerts.com/r/{DONATION_ALERTS_USERNAME}"
            )]
        ])
        
        await bot.send_message(
            user.id,
            payment_message,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error showing payment options: {e}")

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    """Обработчик загружаемых фото"""
    user_id = message.from_user.id
    username = message.from_user.username
    
    # Проверяем, идет ли уже обработка
    if await is_processing(user_id):
        await message.answer("✅ Оба файла получены.\n🔄 Идёт примерка. Ожидайте результат!")
        return
        
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    # Получаем информацию о файле
    file_id = message.photo[-1].file_id
    file = await bot.get_file(file_id)
    file_path = file.file_path
    file_ext = os.path.splitext(file_path)[1].lower()
    
    if file_ext not in SUPPORTED_EXTENSIONS:
        await message.answer("❌ Формат файла не поддерживается. Отправьте фото в формате JPG, PNG или WEBP.")
        return
    
    try:
        # Определяем тип фото (одежда или человек)
        existing_photos = [
            f for f in os.listdir(user_dir) 
            if f.startswith("photo_") and f.endswith(tuple(SUPPORTED_EXTENSIONS))
        ]
        
        photo_type = 1 if len(existing_photos) == 0 else 2
        photo_name = f"photo_{photo_type}{file_ext}"
        local_path = os.path.join(user_dir, photo_name)
        
        # Скачиваем фото
        await bot.download_file(file_path, local_path)
        
        # Загружаем в Supabase
        await upload_to_supabase(local_path, user_id, "photos")
        
        # Обновляем статус в базе данных
        if photo_type == 1:
            await supabase_api.upsert_row(user_id, username, {
                "photo1_received": True,
                "photo_clothes": True,
                "status": "clothes_received"
            })
            response_text = (
                "✅ Фото одежды получено.\n\n"
                "👇 Теперь выберите модель или отправьте фото человека:"
            )
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👕 Выбрать модель", callback_data="choose_model")],
                [InlineKeyboardButton(text="📷 Загрузить фото человека", callback_data="upload_person")]
            ])
            
            await message.answer(response_text, reply_markup=keyboard)
        else:
            # Проверяем количество оставшихся примерок
            tries_left = await get_user_tries(user_id)
            if tries_left <= 0:
                await show_payment_options(message.from_user)
                return
                
            await supabase_api.upsert_row(user_id, username, {
                "photo2_received": True,
                "photo_person": True,
                "status": "В обработке",
                "last_try_date": time.strftime("%Y-%m-%d %H:%M:%S")
            })
            
            await supabase_api.decrement_tries(user_id)
            
            response_text = (
                "✅ Оба файла получены.\n"
                "🔄 Идёт примерка. Ожидайте результат!"
            )
            
            await message.answer(response_text)
            await notify_admin(f"📸 Все фото получены от @{username} ({user_id})")
            
    except Exception as e:
        logger.error(f"Error handling photo for {user_id}: {e}")
        await message.answer("❌ Ошибка при обработке фото. Попробуйте еще раз.")

@dp.message(F.text)
async def handle_text(message: types.Message):
    """Обработчик текстовых сообщений"""
    if await is_processing(message.from_user.id):
        await message.answer("✅ Оба файла получены.\n🔄 Идёт примерка. Ожидайте результат!")
        return
        
    # Если сообщение - это сумма платежа (только цифры)
    if message.text.isdigit():
        await handle_payment(message)
    else:
        await message.answer("ℹ️ Для начала работы отправьте /start")

async def handle_payment(message: types.Message):
    """Обработчик сообщений с суммой платежа"""
    try:
        amount = int(message.text)
        if amount < PRICE_PER_TRY:
            await message.answer(f"❌ Минимальная сумма оплаты - {PRICE_PER_TRY} руб.")
            return
            
        tries = amount // PRICE_PER_TRY
        await message.answer(
            f"💳 Для оплаты {amount} руб. ({tries} примерок) следуйте инструкции ниже:\n\n"
            "1. Нажмите кнопку 'Оплатить'\n"
            "2. Введите сумму {amount} руб.\n"
            "3. Вставьте сообщение из следующего сообщения в поле для комментария"
        )
        
        await show_payment_options(message.from_user)
        
    except Exception as e:
        logger.error(f"Error handling payment: {e}")
        await message.answer("❌ Ошибка обработки платежа. Попробуйте еще раз.")

async def main():
    """Основная функция запуска бота"""
    try:
        logger.info("Starting bot...")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
    finally:
        await cleanup_resources()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")