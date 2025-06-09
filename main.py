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
FREE_USERS = {6320348591, 973853935}  # Пользователи с бесплатным доступом
UPLOAD_DIR = "uploads"
MODELS_BUCKET = "models"
EXAMPLES_BUCKET = "primery"
UPLOADS_BUCKET = "uploads"  # Бакет для загружаемых файлов
SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
MODELS_PER_PAGE = 3
EXAMPLES_PER_PAGE = 3
DONATION_ALERTS_TOKEN = os.getenv("DONATION_ALERTS_TOKEN", "").strip()
PORT = int(os.getenv("PORT", 4000))

# Названия полей в Supabase
USERS_TABLE = "users"
ACCESS_FIELD = "access_granted"
AMOUNT_FIELD = "payment_amount"
TRIES_FIELD = "tries_left"
STATUS_FIELD = "status"

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
    
    # Проверка существования таблицы пользователей
    try:
        res = supabase.table(USERS_TABLE).select("*").limit(1).execute()
        logger.info(f"Users table exists with {len(res.data)} records")
    except Exception as e:
        logger.error(f"Users table check failed: {e}")
        raise Exception("Users table not found in Supabase")
    
    # Проверка бакетов хранилища
    buckets = supabase.storage.list_buckets()
    logger.info(f"Available buckets: {buckets}")
    
    required_buckets = [MODELS_BUCKET, EXAMPLES_BUCKET, UPLOADS_BUCKET]
    for bucket in required_buckets:
        if bucket not in [b.name for b in buckets]:
            logger.error(f"Bucket '{bucket}' not found in Supabase storage")
            raise Exception(f"Required bucket '{bucket}' not found")

except Exception as e:
    logger.error(f"Failed to initialize Supabase: {e}")
    raise

class SupabaseAPI:
    def __init__(self):
        self.supabase = supabase
        self.user_states = {}  # Для хранения последних состояний пользователей

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

            # Если есть оплата, но доступ не предоставлен - предоставляем доступ
            if payment_amount > 0 and not row.get(ACCESS_FIELD, False):
                tries_left = int(payment_amount / PRICE_PER_TRY)
                await self.update_user_row(user_id, {
                    ACCESS_FIELD: True,
                    TRIES_FIELD: tries_left,
                    STATUS_FIELD: "Оплачено"
                })
                return tries_left

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

            new_tries = max(0, tries_left - 1)
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
            
            # Отправляем уведомление пользователю о списании
            if updated:
                try:
                    await bot.send_message(
                        user_id,
                        f"✅ Примерка использована. Осталось примерок: {new_tries}\n"
                        f"Остаток на счету: {new_amount} руб."
                    )
                except Exception as e:
                    logger.error(f"Error sending tries update to user: {e}")
            
            return updated is not None

        except Exception as e:
            logger.error(f"Error decrementing tries: {e}")
            return False

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
                
                # Отправляем уведомления
                try:
                    # Уведомление пользователю
                    await bot.send_message(
                        user_id,
                        f"✅ Оплата {payment_amount} руб. подтверждена!\n"
                        f"🎁 Вам доступно: {tries_left} примерок"
                    )
                    
                    # Уведомление администратору
                    if ADMIN_CHAT_ID:
                        await bot.send_message(
                            ADMIN_CHAT_ID,
                            f"💰 Пользователь @{username} ({user_id}) "
                            f"оплатил {payment_amount} руб. Получено {tries_left} примерок."
                        )
                except Exception as e:
                    logger.error(f"Error sending payment notifications: {e}")
                
                # Обновляем доступ и количество попыток
                await self.update_user_row(user_id, {
                    ACCESS_FIELD: True,
                    TRIES_FIELD: tries_left,
                    STATUS_FIELD: "Оплачено"
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

    async def check_payment_changes(self):
        """Проверяет изменения в платежах и отправляет уведомления"""
        while True:
            try:
                logger.info("🔍 Checking for payment changes...")
                
                # Получаем всех пользователей с ненулевым payment_amount
                res = self.supabase.table(USERS_TABLE)\
                    .select("user_id, username, payment_amount, tries_left")\
                    .neq("payment_amount", 0)\
                    .execute()
                
                if not res.data:
                    await asyncio.sleep(10)
                    continue
                
                for user_data in res.data:
                    user_id = int(user_data['user_id'])
                    current_amount = float(user_data['payment_amount'])
                    current_tries = int(user_data['tries_left'])
                    
                    # Получаем предыдущее состояние из кеша
                    prev_state = self.user_states.get(user_id, {})
                    prev_amount = prev_state.get('payment_amount', 0)
                    prev_tries = prev_state.get('tries_left', 0)
                    
                    # Если сумма изменилась
                    if current_amount != prev_amount or current_tries != prev_tries:
                        logger.info(f"💰 Payment change detected for user {user_id}: "
                                  f"amount {prev_amount}->{current_amount}, "
                                  f"tries {prev_tries}->{current_tries}")
                        
                        # Обновляем кеш
                        self.user_states[user_id] = {
                            'payment_amount': current_amount,
                            'tries_left': current_tries
                        }
                        
                        # Отправляем уведомления
                        try:
                            # Пользователю
                            await bot.send_message(
                                user_id,
                                f"💰 Ваш баланс обновлён:\n"
                                f"Сумма: {current_amount} руб.\n"
                                f"Доступно примерок: {current_tries}"
                            )
                            
                            # Администратору
                            if ADMIN_CHAT_ID:
                                await bot.send_message(
                                    ADMIN_CHAT_ID,
                                    f"🔄 Баланс пользователя @{user_data['username']} ({user_id}) обновлён:\n"
                                    f"Сумма: {current_amount} руб.\n"
                                    f"Доступно примерок: {current_tries}"
                                )
                        except Exception as e:
                            logger.error(f"Error sending payment change notification: {e}")
                
                await asyncio.sleep(10)
                
            except Exception as e:
                logger.error(f"Error in check_payment_changes: {e}")
                await asyncio.sleep(30)

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
    if user_id in FREE_USERS:
        return 100

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
            "🔔 Если хотите примерить верхнюю и нижнюю одежду, отправьте сначала фото (верхней или нижней одежды) выполните примерку - получите результат обработки, затем уже отправляйте 2-ое фото (верхней или нижней одежды) и результат первой обработки\n\n" 
            "📸 <b>ОТПРАВЬТЕ ПЕРВОЕ ФОТО (одежда), ЖДУ!!!:</b>",
            reply_markup=keyboard
        )
        
        await supabase_api.reset_flags(user_id)
        
        await supabase_api.upsert_row(user_id, username, {
            "status": "started",
            "photo_clothes": False,
            "photo_person": False,
            "model_selected": None,
            "tries_left": await get_user_tries(user_id)
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
        
    except Exception as e:
        logger.error(f"Error in choose_model: {e}")
        await callback_query.message.answer("⚠️ Ошибка при загрузке категорий. Попробуйте позже.")

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
    """Обработчик выбора конкретной модели"""
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
                    
                    if user_id not in FREE_USERS:
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

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    """Обработчик фотографий"""
    user = message.from_user
    user_id = user.id
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    try:
        if user_id in FREE_USERS:
            await process_photo(message, user, user_dir)
            return
            
        tries_left = await get_user_tries(user_id)
        
        if tries_left <= 0:
            await show_payment_options(user)
            return
            
        await process_photo(message, user, user_dir)
        
    except Exception as e:
        logger.error(f"Error handling photo: {e}")
        await message.answer("❌ Ошибка при сохранении файла. Попробуйте ещё раз.")

async def show_payment_options(user: types.User):
    """Показывает варианты оплаты"""
    payment_instructions = (
        "🚫 У вас закончились бесплатные примерки.\n\n"
        "📌 <b>Для продолжения работы необходимо оплатить услугу:</b>\n\n"
        "1. <b>Свяжитесь с администратором</b> для получения реквизитов оплаты.\n"
        "2. После оплаты администратор вручную подтвердит ваш платеж.\n"
        "3. Вы получите уведомление о подтверждении оплаты.\n\n"
        "💰 <b>Тарифы:</b>\n"
        f"- 30 руб = 1 примерка\n"
        f"- 60 руб = 2 примерки\n"
        f"- 90 руб = 3 примерки\n"
        "и так далее..."
    )
    
    await bot.send_message(
        user.id,
        payment_instructions,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Проверить доступ", 
                    callback_data="check_access"
                )
            ]
        ])
    )
async def check_payment_changes():
    """Проверяет изменения в платежах и отправляет уведомления"""
    while True:
        try:
            logger.info("🔍 Checking for payment changes...")
            
            # Получаем всех пользователей с ненулевым payment_amount
            res = supabase.table(USERS_TABLE)\
                .select("user_id, username, payment_amount, tries_left")\
                .neq("payment_amount", 0)\
                .execute()
            
            if not res.data:
                await asyncio.sleep(10)
                continue
            
            for user_data in res.data:
                user_id = int(user_data['user_id'])
                current_amount = float(user_data['payment_amount'])
                current_tries = int(user_data['tries_left'])
                
                # Получаем предыдущее состояние из кеша
                prev_state = supabase_api.user_states.get(user_id, {})
                prev_amount = prev_state.get('payment_amount', 0)
                prev_tries = prev_state.get('tries_left', 0)
                
                # Если сумма изменилась
                if current_amount != prev_amount or current_tries != prev_tries:
                    logger.info(f"💰 Payment change detected for user {user_id}: "
                              f"amount {prev_amount}->{current_amount}, "
                              f"tries {prev_tries}->{current_tries}")
                    
                    # Обновляем кеш
                    supabase_api.user_states[user_id] = {
                        'payment_amount': current_amount,
                        'tries_left': current_tries
                    }
                    
                    # Отправляем уведомления
                    try:
                        # Пользователю
                        await bot.send_message(
                            user_id,
                            f"💰 Ваш баланс обновлён:\n"
                            f"Сумма: {current_amount} руб.\n"
                            f"Доступно примерок: {current_tries}"
                        )
                        
                        # Администратору
                        if ADMIN_CHAT_ID:
                            await bot.send_message(
                                ADMIN_CHAT_ID,
                                f"🔄 Баланс пользователя @{user_data['username']} ({user_id}) обновлён:\n"
                                f"Сумма: {current_amount} руб.\n"
                                f"Доступно примерок: {current_tries}"
                            )
                    except Exception as e:
                        logger.error(f"Error sending payment change notification: {e}")
            
            await asyncio.sleep(10)
            
        except Exception as e:
            logger.error(f"Error in check_payment_changes: {e}")
            await asyncio.sleep(30)

# ... продолжение кода ...

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

                # Поиск файлов результата
                result_files = [
                    f for f in os.listdir(user_dir)
                    if f.startswith("result") and f.lower().endswith(tuple(SUPPORTED_EXTENSIONS))
                ]

                if not result_files:
                    await asyncio.sleep(5)
                    continue

                result_file = os.path.join(user_dir, result_files[0])
                user_id = int(user_id_str)

                try:
                    # Отправка результата пользователю
                    photo = FSInputFile(result_file)
                    await bot.send_photo(
                        chat_id=user_id,
                        photo=photo,
                        caption="🎉 Ваша виртуальная примерка готова!"
                    )

                    # Уведомление администратора
                    if ADMIN_CHAT_ID:
                        user_info = await supabase_api.get_user_row(user_id)
                        username = user_info.get('username', 'неизвестно') if user_info else 'неизвестно'
                        
                        await bot.send_message(
                            ADMIN_CHAT_ID,
                            f"✅ Пользователь @{username} ({user_id}) получил результат примерки"
                        )

                    # Загрузка результата в Supabase
                    file_ext = os.path.splitext(result_file)[1].lower()
                    supabase_path = f"{user_id_str}/results/result_{int(time.time())}{file_ext}"
                    
                    with open(result_file, 'rb') as f:
                        supabase.storage.from_(UPLOADS_BUCKET).upload(
                            path=supabase_path,
                            file=f,
                            file_options={"content-type": "image/jpeg" if file_ext in ('.jpg', '.jpeg') else
                                      "image/png" if file_ext == '.png' else
                                      "image/webp"}
                        )

                    # Обновление статуса в базе данных
                    await supabase_api.upsert_row(user_id, "", {
                        "status": "Результат отправлен",
                        "result_sent": True,
                        "ready": True,
                        "result_url": supabase_path
                    })

                    # Очистка локальных файлов
                    try:
                        shutil.rmtree(user_dir)
                        logger.info(f"🗑️ Папка {user_dir} удалена")
                    except Exception as e:
                        logger.error(f"Error cleaning up user dir: {e}")

                except Exception as e:
                    logger.error(f"Error processing result for user {user_id}: {e}")
                    continue

            await asyncio.sleep(30)

        except Exception as e:
            logger.error(f"Critical error in check_results: {e}")
            await asyncio.sleep(30)

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
        
        # Запуск фоновых задач
        asyncio.create_task(check_results())
        asyncio.create_task(supabase_api.check_payment_changes())
        
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