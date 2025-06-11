import os
import logging
import asyncio
import aiohttp
import shutil
import time
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
from supabase.lib.client_options import ClientOptions
from urllib.parse import quote

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

# Инициализация клиентов
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Инициализация Supabase с настройками для реального времени
try:
    client_options = ClientOptions(postgrest_client_timeout=None)
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
    """Получает список моделей для указанной категории"""
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
            "1️⃣ Отправьте первое фото – одежда (отправляйте только 1 фото)\n"
            "2️⃣ Отправьте второе фото – человек (желательно в полный рост, 1 фото) или выберите готовую модель \n"
            "👆 Фото прикрепляйте через скрепку, которая находится, где отправляете сообщения\n\n"
            "🌈 <b>Получите результат изображения виртуальной примерки!!</b> \n\n"
            "🔴 <b>Отправляйте по порядку сначала фото одежды, затем фото человека или выберите модель для примерки!!!</b> \n\n" 
            "🔔 Если хотите примерить верхнюю и нижнюю одежду, отправьте сначала фото (верхней или нижней одежды) выполните примерку - получите результат обработки, затем уже отправляйте 2-ое фото (верхней или нижней одежды) и результат первой обработки\n\n" 
            "👇 <b>Начните с загрузки фото одежды:</b>",
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
            "👕 Чтобы загрузить одежду, нажмите на Скрепку, "
            "которая находится рядом с сообщением и загрузите изображение одежды для примерки."
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
        await callback_query.answer()
        
    except Exception as e:
        logger.error(f"Error in choose_model: {e}")
        await callback_query.message.answer("⚠️ Ошибка при загрузке категорий. Попробуйте позже.")
        await callback_query.answer()

@dp.callback_query(F.data.startswith("models_"))
async def show_category_models(callback_query: types.CallbackQuery):
    """Показывает модели выбранной категории"""
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
                                text="⬇️ Показать еще",
                                callback_data=f"models_{category}_{page + 1}"
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
        await callback_query.message.answer("⚠️ Ошибка при загрузке моделей. Попробуйте позже.")
        await callback_query.answer()

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
        await callback_query.answer("✅ Оба файла получены. Ожидайте результат!", show_alert=True)
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

        # Получаем файл фотографии
        photo = message.photo[-1]  # Берем фото наибольшего размера
        file_id = photo.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path

        # Определяем тип фото (одежда или человек)
        existing_photos = [
            f for f in os.listdir(user_dir)
            if f.startswith("photo_") and f.endswith(tuple(SUPPORTED_EXTENSIONS))
        ]

        if not existing_photos:
            # Первое фото - одежда
            photo_type = 1
            filename = f"photo_1{os.path.splitext(file_path)[1]}"
            caption = "✅ Фото одежды получено. Теперь выберите действие:"
            
            # Добавляем кнопки после получения фото одежды
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="👤 Загрузить фото человека", callback_data="upload_person"),
                    InlineKeyboardButton(text="👫 Выбрать модель", callback_data="choose_model")
                ]
            ])
        else:
            # Второе фото - человек
            photo_type = 2
            filename = f"photo_2{os.path.splitext(file_path)[1]}"
            caption = "✅ Оба файла получены.\n🔄 Идёт примерка. Ожидайте результат!"
            keyboard = None

            # Уведомление администратору о получении всех фото
            await notify_admin(f"📸 Все фото получены от @{user.username} ({user_id})")

        # Сохраняем фото локально
        local_path = os.path.join(user_dir, filename)
        await bot.download_file(file_path, local_path)

        # Загружаем фото в Supabase
        await upload_to_supabase(local_path, user_id, "photos")

        # Получаем текущие данные пользователя, чтобы сохранить username
        user_row = await supabase_api.get_user_row(user_id)
        current_username = user_row.get('username', '') if user_row else (user.username or '')

        # Обновляем статус в базе данных, сохраняя username
        if photo_type == 1:
            await supabase_api.upsert_row(user_id, current_username, {
                "photo1_received": True,
                "photo2_received": False,
                "status": "Ожидается фото человека",
                "username": current_username  # Явно сохраняем username
            })
        else:
            await supabase_api.upsert_row(user_id, current_username, {
                "photo1_received": True,
                "photo2_received": True,
                "status": "В обработке",
                "last_try_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                "username": current_username  # Явно сохраняем username
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
            "👤 Чтобы загрузить фото человека, нажмите на Скрепку, "
            "которая находится рядом с сообщением и загрузите изображение человека для примерки."
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
        # Получаем данные пользователя из Supabase
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
    """Показывает варианты оплаты через DonationAlerts"""
    try:
        # Формируем сообщение для DonationAlerts (username и ID)
        payment_message = f"Оплата за примерки от @{user.username} (ID: {user.id})"
        encoded_message = quote(payment_message)
        
        # Создаем клавиатуру с кнопкой оплаты и проверки баланса
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Пополнить баланс",
                    url=f"https://www.donationalerts.com/r/{DONATION_ALERTS_USERNAME}?message={encoded_message}"
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
    "📌 <b>Для продолжения работы необходимо оплатить услугу:</b>\n\n"
    "1️⃣ Нажмите на кнопку 'Пополнить баланс'\n\n"
    "2️⃣ Указываете любую сумму не менее 30 руб (сколько хотите примерок)\n\n"
    "⚠️‼️ <b>ВНИМАНИЕ!</b> В поле для сообщений, которое находится под оплатой обязательно укажите:\n\n"
    "<b>ОПЛАТА ЗА ПРИМЕРКИ ОТ </code>@{user.username}</code> </b>\n\n"
    "🤷‍♂️Иначе не будет понятно кому начислять баланс.\n\n"
    "<b>⁉️Как это сделать:</b>\n\n"
    "Писать Вам ничего не нужно просто нажмите на это сообщение:\n\n"
	"👇👇👇👇👇👇👇👇👇👇👇👇👇👇👇👇\n"
    f"<code>ОПЛАТА ЗА ПРИМЕРКИ от @{user.username}</code>\n\n"
    "Вы увидите, что оно скопировано и вставьте в поле Для сообщений\n\n"
    "‼️<b>Ничего не меняйте в сообщении‼️</b>\n\n"
    "3️⃣ Выбираете удобный способ оплаты (Карта или СБП)\n\n"
    "4️⃣ В поле <b>e-mail</b> - укажите любую почту, можете свою, можете придумать(не имеет значения)\n"
    "Например: Maria@mail.ru\n\n"
    "💥<b>ВСЁ!!!</b>💥\n\n"
    "После успешной оплаты спокойно продолжаете примерку на ту сумму, которую внесёте\n\n"
    "💰 <b>Тарифы:</b>\n\n"
    f"- 30 руб = 1 примерка\n"
    f"- 60 руб = 2 примерки\n"
    f"- 90 руб = 3 примерки\n"
    "и так далее...\n\n"
    "❓Свой баланс Вы можете отслеживать по кнопке 'Мой баланс'"
)
        await bot.send_message(
            user.id,
            payment_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
        
        # Уведомление администратору о начале оплаты
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    f"💸 Пользователь @{user.username} ({user.id}) начал процесс оплаты\n"
                    f"ℹ️ Сообщение для DonationAlerts: 'Оплата за примерки от @{user.username} (ID: {user.id})'"
                )
            except Exception as e:
                logger.error(f"Error sending admin payment notification: {e}")
                
    except Exception as e:
        logger.error(f"Error sending payment options: {e}")
        await bot.send_message(
            user.id,
            "❌ Ошибка при формировании ссылки оплаты. Пожалуйста, свяжитесь с администратором."
        )

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
    """Проверяет наличие результатов для отправки пользователям"""
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

                logger.info(f"📁 Checking user dir: {user_dir}")

                # Ищем result-файлы с любым поддерживаемым расширением
                result_files = [
                    f for f in os.listdir(user_dir)
                    if f.startswith("result") and f.lower().endswith(tuple(SUPPORTED_EXTENSIONS))
                ]

                # Если не найдено локально — пробуем скачать из Supabase
                if not result_files:
                    for ext in SUPPORTED_EXTENSIONS:
                        try:
                            result_supabase_path = f"{user_id_str}/result{ext}"
                            result_file_local = os.path.join(user_dir, f"result{ext}")
                            os.makedirs(user_dir, exist_ok=True)

                            res = supabase.storage.from_(UPLOADS_BUCKET).download(result_supabase_path)
                            with open(result_file_local, 'wb') as f:
                                f.write(res)

                            logger.info(f"✅ Скачан result{ext} из Supabase для пользователя {user_id_str}")
                            result_files = [f"result{ext}"]
                            break
                        except Exception as e:
                            logger.warning(f"❌ Не удалось скачать result{ext} из Supabase для {user_id_str}: {e}")
                            continue

                # Если файлы найдены, обрабатываем первый подходящий
                if result_files:
                    result_file = os.path.join(user_dir, result_files[0])

                    try:
                        user_id = int(user_id_str)

                        if not os.path.isfile(result_file) or not os.access(result_file, os.R_OK):
                            logger.warning(f"🚫 Файл {result_file} недоступен или не читается")
                            continue

                        if os.path.getsize(result_file) == 0:
                            logger.warning(f"🚫 Файл {result_file} пуст")
                            continue

                        logger.info(f"📤 Отправляем результат для {user_id}")

                        photo = FSInputFile(result_file)
                        
                        # Создаем клавиатуру с кнопками
                        keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="🔄 Продолжить примерку",
                                        callback_data="continue_tryon"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        text="💳 Пополнить баланс",
                                        callback_data="show_payment_options"
                                    ),
                                    InlineKeyboardButton(
                                        text="💰 Мой баланс",
                                        callback_data="check_balance"
                                    )
                                ]
                            ]
                        )

                        await bot.send_photo(
                            chat_id=user_id,
                            photo=photo,
                            caption="🎉 Ваша виртуальная примерка готова!",
                            reply_markup=keyboard
                        )

                        # Получаем текущие данные пользователя, чтобы сохранить username
                        user_row = await supabase_api.get_user_row(user_id)
                        current_username = user_row.get('username', '') if user_row.get('username') else ''

                        # Уведомление администратору
                        if ADMIN_CHAT_ID:
                            try:
                                await bot.send_message(
                                    ADMIN_CHAT_ID,
                                    f"✅ Пользователь @{current_username} ({user_id}) получил результат примерки"
                                )
                            except Exception as e:
                                logger.error(f"Error sending admin notification: {e}")

                        # Загружаем результат в Supabase с новым уникальным именем
                        try:
                            file_ext = os.path.splitext(result_file)[1].lower()
                            supabase_path = f"{user_id}/results/result_{int(time.time())}{file_ext}"

                            with open(result_file, 'rb') as f:
                                supabase.storage.from_(UPLOADS_BUCKET).upload(
                                    path=supabase_path,
                                    file=f,
                                    file_options={"content-type": "image/jpeg" if file_ext in ('.jpg', '.jpeg') else
                                          "image/png" if file_ext == '.png' else
                                          "image/webp"}
                                )
                            logger.info(f"☁️ Результат загружен в Supabase: {supabase_path}")
                        except Exception as upload_error:
                            logger.error(f"❌ Ошибка загрузки результата в Supabase: {upload_error}")

                        # Обновляем Supabase, сохраняя username
                        try:
                            await supabase_api.upsert_row(user_id, current_username, {
                                "status": "Результат отправлен",
                                "result_sent": True,
                                "ready": True,
                                "result_url": supabase_path if 'supabase_path' in locals() else None,
                                "username": current_username  # Явно сохраняем username
                            })
                        except Exception as db_error:
                            logger.error(f"❌ Ошибка обновления Supabase: {db_error}")

                        # Полная очистка локальной папки пользователя
                        try:
                            shutil.rmtree(user_dir)
                            logger.info(f"🗑️ Папка {user_dir} полностью удалена")
                        except Exception as cleanup_error:
                            logger.error(f"❌ Ошибка удаления папки: {cleanup_error}")

                        # Удаляем все файлы пользователя из Supabase
                        try:
                            base = supabase.storage.from_(UPLOADS_BUCKET)
                            files_to_delete = []

                            # Добавляем все возможные фото пользователя
                            for ext in SUPPORTED_EXTENSIONS:
                                files_to_delete.extend([
                                    f"{user_id_str}/photos/photo_1{ext}",
                                    f"{user_id_str}/photos/photo_2{ext}",
                                    f"{user_id_str}/models/selected_model{ext}"
                                ])

                            # Добавляем result-файлы
                            files_to_delete.extend([
                                f"{user_id_str}/result{ext}" for ext in SUPPORTED_EXTENSIONS
                            ])

                            # Добавляем файлы из папки results
                            try:
                                result_files_in_supabase = base.list(f"{user_id_str}/results")
                                for f in result_files_in_supabase:
                                    if f['name'].startswith("result"):
                                        files_to_delete.append(f"{user_id_str}/results/{f['name']}")
                            except Exception as e:
                                logger.warning(f"⚠️ Не удалось получить список result-файлов: {e}")

                            # Удаляем только существующие файлы
                            existing_files = []
                            for file_path in files_to_delete:
                                try:
                                    base.download(file_path)
                                    existing_files.append(file_path)
                                except Exception:
                                    continue

                            if existing_files:
                                logger.info(f"➡️ Удаляем из Supabase: {existing_files}")
                                base.remove(existing_files)
                                logger.info(f"🗑️ Удалены файлы пользователя {user_id_str} из Supabase: {len(existing_files)} шт.")
                            else:
                                logger.info(f"ℹ️ Нет файлов для удаления у пользователя {user_id_str}")

                        except Exception as e:
                            logger.error(f"❌ Ошибка удаления файлов пользователя {user_id_str} из Supabase: {e}")

                    except Exception as e:
                        logger.error(f"❌ Ошибка при отправке результата пользователю {user_id_str}: {e}")
                        continue

            await asyncio.sleep(30)

        except Exception as e:
            logger.error(f"❌ Критическая ошибка в check_results(): {e}")
            await asyncio.sleep(30)

@dp.callback_query(F.data == "continue_tryon")
async def continue_tryon_handler(callback_query: types.CallbackQuery):
    """Обработчик кнопки продолжения примерки"""
    try:
        await send_welcome(
            callback_query.from_user.id,
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
            # Получаем всех пользователей из базы данных
            res = supabase.table(USERS_TABLE)\
                .select("user_id, payment_amount, username")\
                .execute()
            
            current_payments = {int(user['user_id']): float(user['payment_amount']) 
                              for user in res.data if user.get('payment_amount')}
            
            # Сравниваем с предыдущими значениями
            for user_id, current_amount in current_payments.items():
                previous_amount = supabase_api.last_payment_amounts.get(user_id, 0)
                
                if current_amount != previous_amount:
                    # Рассчитываем количество примерок
                    tries_left = int(current_amount / PRICE_PER_TRY)
                    
                    # Обновляем кэш
                    supabase_api.last_payment_amounts[user_id] = current_amount
                    supabase_api.last_tries_values[user_id] = tries_left
                    
                    # Получаем данные пользователя
                    user_row = await supabase_api.get_user_row(user_id)
                    if not user_row:
                        continue
                    
                    username = user_row.get('username', '') if user_row.get('username') else ''
                    
                    # Обновляем доступ и количество попыток
                    await supabase_api.update_user_row(user_id, {
                        ACCESS_FIELD: True if current_amount > 0 else False,
                        TRIES_FIELD: tries_left,
                        STATUS_FIELD: "Оплачено" if current_amount > 0 else "Не оплачено",
                        FREE_TRIES_FIELD: True  # Помечаем, что бесплатная проверка использована
                    })
                    
                    # Отправляем уведомления
                    await supabase_api.send_payment_update_notifications(
                        user_id,
                        current_amount,
                        tries_left,
                        "Изменение баланса"
                    )
            
            await asyncio.sleep(10)  # Проверяем каждые 10 секунд
            
        except Exception as e:
            logger.error(f"Error in payment monitoring task: {e}")
            await asyncio.sleep(30)  # При ошибке ждем дольше

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
        
        # Запуск веб-сервера
        await start_web_server()
        
        # Установка вебхука
        webhook_url = f"https://virtual-tryon-bot.onrender.com/{BOT_TOKEN.split(':')[1]}"
        await bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
        )
        logger.info(f"Webhook set to: {webhook_url}")
        
        # Запуск фоновой задачи проверки результатов
        asyncio.create_task(check_results())
        
        # Запуск мониторинга изменений payment_amount
        asyncio.create_task(monitor_payment_changes_task())
        
        # Бесконечный цикл
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