import os
import logging
import asyncio
import aiohttp
import shutil
import time
from aiohttp import web
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
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
PRICE_PER_TRY = 1  # Цена за одну примерку в рублях
FREE_USERS = {6320348591, 973853935}  # Бесплатные пользователи
UPLOAD_DIR = "uploads"
MODELS_BUCKET = "models"
EXAMPLES_BUCKET = "examples"
UPLOADS_BUCKET = "uploads"
SUPPORTED_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
EXAMPLES_PER_PAGE = 3
MODELS_PER_PAGE = 3
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

            if not row.get(ACCESS_FIELD, False):
                return 0

            tries_left = int(row.get(TRIES_FIELD, 0)) if row.get(TRIES_FIELD) else 0
            amount = float(row.get(AMOUNT_FIELD, 0)) if row.get(AMOUNT_FIELD) else 0.0

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

            return await self.update_user_row(user_id, update_data) is not None

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
                return res.data[0] if res.data else None
            else:
                data["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                res = self.supabase.table(USERS_TABLE)\
                    .insert(data)\
                    .execute()
                return res.data[0] if res.data else None
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

    async def grant_access_for_payment(self, user_id: int):
        """Предоставляет доступ на основе оплаты и возвращает количество примерок"""
        try:
            row = await self.get_user_row(user_id)
            if not row:
                return 0

            payment_amount = float(row.get(AMOUNT_FIELD, 0)) if row.get(AMOUNT_FIELD) else 0.0
            if payment_amount <= 0:
                return 0

            tries_left = int(payment_amount / PRICE_PER_TRY)

            update_data = {
                ACCESS_FIELD: True,
                TRIES_FIELD: tries_left,
                STATUS_FIELD: "Оплачено",
                "payment_confirmed": True,
                "confirmation_date": time.strftime("%Y-%m-%d %H:%M:%S")
            }

            await self.update_user_row(user_id, update_data)
            return tries_left

        except Exception as e:
            logger.error(f"Error granting access for payment: {e}")
            return 0

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

def make_donation_link(user: types.User, amount: int = 10) -> str:
    """Генерация ссылки на оплату"""
    username = f"@{user.username}" if user.username else f"TelegramID_{user.id}"
    message = username.replace(" ", "_")
    return f"https://www.donationalerts.com/r/primerochnay777?amount={amount}&message={message}"

async def upload_to_supabase(file_path: str, user_id: int, file_type: str):
    """Загрузка файла в Supabase Storage"""
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

async def send_examples_page(chat_id: int, page: int):
    """Отправка страницы с примерами работ"""
    try:
        # Получаем список примеров из Supabase Storage
        examples = supabase.storage.from_(EXAMPLES_BUCKET).list()
        examples = [e for e in examples if e.name.lower().endswith(SUPPORTED_EXTENSIONS)]
        
        if not examples:
            await bot.send_message(chat_id, "📸 Примеры работ временно недоступны")
            return
            
        # Разбиваем на страницы
        total_pages = (len(examples) + EXAMPLES_PER_PAGE - 1) // EXAMPLES_PER_PAGE
        page = max(0, min(page, total_pages - 1))
        start_idx = page * EXAMPLES_PER_PAGE
        end_idx = min(start_idx + EXAMPLES_PER_PAGE, len(examples))
        
        # Создаем медиагруппу
        media = []
        for example in examples[start_idx:end_idx]:
            url = supabase.storage.from_(EXAMPLES_BUCKET).get_public_url(example.name)
            media.append(InputMediaPhoto(media=url))
        
        await bot.send_media_group(chat_id, media=media)
        
        # Создаем клавиатуру с навигацией
        keyboard = []
        if page > 0:
            keyboard.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"view_examples_{page-1}"))
        if page < total_pages - 1:
            keyboard.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"view_examples_{page+1}"))
        
        if keyboard:
            await bot.send_message(
                chat_id,
                f"Страница {page + 1} из {total_pages}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[keyboard])
            )
            
    except Exception as e:
        logger.error(f"Ошибка отправки примеров: {e}")
        await bot.send_message(chat_id, "📸 Примеры работ временно недоступны")

async def send_models_page(chat_id: int, category: str, page: int):
    """Отправка страницы с моделями"""
    try:
        # Получаем список моделей из Supabase Storage
        models = supabase.storage.from_(MODELS_BUCKET).list(category)
        models = [m for m in models if m.name.lower().endswith(SUPPORTED_EXTENSIONS)]
        
        if not models:
            await bot.send_message(chat_id, f"Модели в категории {category} временно недоступны")
            return
            
        # Разбиваем на страницы
        total_pages = (len(models) + MODELS_PER_PAGE - 1) // MODELS_PER_PAGE
        page = max(0, min(page, total_pages - 1))
        start_idx = page * MODELS_PER_PAGE
        end_idx = min(start_idx + MODELS_PER_PAGE, len(models))
        
        # Создаем медиагруппу
        media = []
        for model in models[start_idx:end_idx]:
            url = supabase.storage.from_(MODELS_BUCKET).get_public_url(f"{category}/{model.name}")
            media.append(InputMediaPhoto(media=url))
        
        await bot.send_media_group(chat_id, media=media)
        
        # Создаем клавиатуру с навигацией и кнопками выбора
        keyboard_buttons = []
        
        # Кнопки выбора модели
        for i, model in enumerate(models[start_idx:end_idx]):
            model_name = os.path.splitext(model.name)[0]
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"Выбрать {model_name}",
                    callback_data=f"select_model_{category}_{model.name}"
                )
            ])
        
        # Кнопки навигации
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"models_{category}_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"models_{category}_{page+1}"))
        
        if nav_buttons:
            keyboard_buttons.append(nav_buttons)
        
        # Кнопка возврата
        keyboard_buttons.append([
            InlineKeyboardButton(text="🔙 Назад к категориям", callback_data="choose_model")
        ])
        
        await bot.send_message(
            chat_id,
            f"Модели {category} - Страница {page + 1} из {total_pages}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        )
            
    except Exception as e:
        logger.error(f"Ошибка отправки моделей: {e}")
        await bot.send_message(chat_id, f"Модели в категории {category} временно недоступны")

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
async def handle_models_category(callback_query: types.CallbackQuery):
    """Обработчик выбора категории моделей"""
    try:
        parts = callback_query.data.split("_")
        category = parts[1]
        page = int(parts[2])
        
        await send_models_page(callback_query.from_user.id, category, page)
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in handle_models_category: {e}")
        await callback_query.message.answer("⚠️ Ошибка при загрузке моделей. Попробуйте позже.")
        await callback_query.answer()

@dp.callback_query(F.data.startswith("select_model_"))
async def select_model(callback_query: types.CallbackQuery):
    """Обработчик выбора конкретной модели"""
    try:
        user_id = callback_query.from_user.id
        parts = callback_query.data.split("_")
        category = parts[2]
        model_name = "_".join(parts[3:])
        
        user_dir = os.path.join(UPLOAD_DIR, str(user_id))
        os.makedirs(user_dir, exist_ok=True)
        
        # Скачиваем выбранную модель
        model_path = f"{category}/{model_name}"
        model_data = supabase.storage.from_(MODELS_BUCKET).download(model_path)
        
        # Сохраняем локально
        local_path = os.path.join(user_dir, "selected_model.jpg")
        with open(local_path, 'wb') as f:
            f.write(model_data)
        
        # Проверяем, есть ли уже фото одежды
        existing_photos = [
            f for f in os.listdir(user_dir)
            if f.startswith("photo_") and f.endswith(tuple(SUPPORTED_EXTENSIONS))
        ]
        
        if existing_photos:
            # Если есть фото одежды, можно начинать обработку
            await supabase_api.upsert_row(user_id, callback_query.from_user.username, {
                "model_selected": model_name,
                "photo_person": True,
                "status": "В обработке",
                "photo1_received": True,
                "photo2_received": True,
                "last_try_date": time.strftime("%Y-%m-%d %H:%M:%S")
            })
            
            # Списание одной примерки для платных пользователей
            if user_id not in FREE_USERS:
                await supabase_api.decrement_tries(user_id)
            
            await callback_query.message.answer(
                "✅ Модель выбрана и фото одежды получено.\n\n"
                "🔄 Идёт примерка. Ожидайте результат!"
            )
        else:
            # Если фото одежды еще нет, ждем его
            await supabase_api.upsert_row(user_id, callback_query.from_user.username, {
                "model_selected": model_name,
                "photo_person": True,
                "status": "Ожидается фото одежды",
                "photo1_received": False,
                "photo2_received": True
            })
            
            await callback_query.message.answer(
                "✅ Модель выбрана.\n\n"
                "Теперь отправьте фото одежды для примерки."
            )
        
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in select_model: {e}")
        await callback_query.message.answer("⚠️ Ошибка при выборе модели. Попробуйте позже.")
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
        "1. <b>Обязательно укажите ваш Telegram username</b> (начинается с @) в поле 'Сообщение' при оплате.\n"
        "2. Чтобы узнать ваш username:\n"
        "   - Откройте настройки Telegram\n"
        "   - Найдите раздел 'Username'\n"
        "   - Скопируйте текст (например: @username)\n"
        "   - Вставьте в поле 'Сообщение' при оплате\n\n"
        "3. Вы можете оплатить:\n"
        "   - 10 руб = 10 примерок\n"
        "   - Или любую другую сумму (количество примерок = сумма в рублях)\n\n"
        "4. После оплаты нажмите кнопку <b>'Я оплатил'</b>"
    )
    
    await bot.send_message(
        user.id,
        payment_instructions,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить примерку", 
                    callback_data="payment_options"
                )
            ]
        ])
    )
    
    await supabase_api.upsert_row(
        user_id=user.id,
        username=user.username or "",
        data={
            "status": "Ожидает оплаты",
            "payment_status": "Не оплачено",
            "last_payment_amount": 0,
            "tries_left": 0,
            "payment_requested": True,
            "payment_confirmed": False
        }
    )

@dp.callback_query(F.data == "payment_options")
async def payment_options(callback_query: types.CallbackQuery):
    """Показывает детали оплаты и кнопки"""
    user = callback_query.from_user
    
    payment_details = (
        "💳 <b>Оплата примерки</b>\n\n"
        "1. <b>Обязательно укажите ваш Telegram username</b> (начинается с @) в поле 'Сообщение' при оплате.\n"
        "2. Вы можете оплатить:\n"
        "   - 10 руб = 10 примерок\n"
        "   - Или любую другую сумму (количество примерок = сумма в рублях)\n\n"
        "3. После оплаты нажмите кнопку <b>'Я оплатил'</b>"
    )
    
    await callback_query.message.edit_text(
        payment_details,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить", 
                    url=make_donation_link(user, 10)
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил", 
                    callback_data="confirm_donation"
                )
            ]
        ])
    )
    await callback_query.answer()

@dp.callback_query(F.data == "confirm_donation")
async def confirm_donation(callback_query: types.CallbackQuery):
    """Подтверждение оплаты пользователем"""
    user = callback_query.from_user
    await callback_query.message.answer(
        "✅ Спасибо! Мы проверим ваш платёж и активируем доступ в течение нескольких минут.\n\n"
        "Если вы указали ваш Telegram username при оплате, это поможет быстрее вас найти."
    )
    
    await supabase_api.upsert_row(
        user_id=user.id,
        username=user.username or "",
        data={
            "status": "Ожидает подтверждения оплаты",
            "payment_status": "Ожидает проверки",
            "payment_requested": True,
            "payment_confirmed": False,
            "payment_date": time.strftime("%Y-%m-%d %H:%M:%S")
        }
    )
    
    await notify_admin(
        f"💰 Пользователь @{user.username} ({user.id}) сообщил об оплате.\n"
        f"Требуется проверка и подтверждение."
    )

async def process_photo(message: types.Message, user: types.User, user_dir: str):
    """Обработка загруженных фотографий с учетом списания примерок"""
    try:
        existing_photos = [
            f for f in os.listdir(user_dir)
            if f.startswith("photo_") and f.endswith(tuple(SUPPORTED_EXTENSIONS))
        ]
        
        photo_number = len(existing_photos) + 1
        
        if photo_number > 2:
            await message.answer("✅ Вы уже загрузили 2 файла. Ожидайте результат.")
            return
            
        model_selected = os.path.exists(os.path.join(user_dir, "selected_model.jpg"))
        first_photo_exists = any(f.startswith("photo_1") for f in existing_photos)
        
        if photo_number == 2 and not model_selected and first_photo_exists:
            photo = message.photo[-1]
            file_ext = os.path.splitext(photo.file_id)[1] or '.jpg'
            file_name = f"photo_{photo_number}{file_ext}"
            file_path = os.path.join(user_dir, file_name)
            
            await bot.download(photo, destination=file_path)
            
            await upload_to_supabase(file_path, user.id, "photos")
            
            if user.id not in FREE_USERS:
                await supabase_api.decrement_tries(user.id)
            
            await supabase_api.upsert_row(user.id, user.username, {
                "photo_person": True,
                "status": "В обработке",
                "photo1_received": True,
                "photo2_received": True,
                "last_try_date": time.strftime("%Y-%m-%d %H:%M:%S")
            })
            
            await message.answer(
                "✅ Оба файла получены.\n\n"
                "🔄 Идёт примерка. Ожидайте результат!"
            )
            await notify_admin(f"📸 Новые фото от @{user.username} ({user.id})")
            return
            
        if photo_number == 1:
            photo = message.photo[-1]
            file_ext = os.path.splitext(photo.file_id)[1] or '.jpg'
            file_name = f"photo_{photo_number}{file_ext}"
            file_path = os.path.join(user_dir, file_name)
            
            await bot.download(photo, destination=file_path)
            
            await upload_to_supabase(file_path, user.id, "photos")
            
            await supabase_api.upsert_row(user.id, user.username, {
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

async def check_results():
    """Проверяет готовые результаты примерки"""
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

                result_files = [
                    f for f in os.listdir(user_dir)
                    if f.startswith("result") and f.lower().endswith(tuple(SUPPORTED_EXTENSIONS))
                ]

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
                        await bot.send_photo(
                            chat_id=user_id,
                            photo=photo,
                            caption="🎉 Ваша виртуальная примерка готова!"
                        )

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

                        try:
                            await supabase_api.upsert_row(user_id, "", {
                                "status": "Результат отправлен",
                                "result_sent": True,
                                "ready": True,
                                "result_url": supabase_path if 'supabase_path' in locals() else None,
                                "last_try_date": time.strftime("%Y-%m-%d %H:%M:%S")
                            })
                        except Exception as db_error:
                            logger.error(f"❌ Ошибка обновления данных: {db_error}")

                        try:
                            # Удаляем все файлы в папке пользователя
                            for filename in os.listdir(user_dir):
                                file_path = os.path.join(user_dir, filename)
                                try:
                                    if os.path.isfile(file_path) or os.path.islink(file_path):
                                        os.unlink(file_path)
                                    elif os.path.isdir(file_path):
                                        shutil.rmtree(file_path)
                                except Exception as e:
                                    logger.error(f"❌ Ошибка удаления файла {file_path}: {e}")
                            
                            # Удаляем саму папку пользователя
                            shutil.rmtree(user_dir)
                            logger.info(f"🗑️ Папка {user_dir} и все её содержимое удалены")
                        except Exception as cleanup_error:
                            logger.error(f"❌ Ошибка удаления папки: {cleanup_error}")

                    except Exception as e:
                        logger.error(f"❌ Ошибка при отправке результата пользователю {user_id_str}: {e}")
                        continue

            await asyncio.sleep(30)

        except Exception as e:
            logger.error(f"❌ Критическая ошибка в check_results(): {e}")
            await asyncio.sleep(30)

async def handle(request):
    """Обработчик корневого URL"""
    return web.Response(text="Bot is running")

async def health_check(request):
    """Обработчик проверки здоровья сервера"""
    return web.Response(text="OK", status=200)

def setup_web_server():
    """Настройка веб-сервера"""
    app = web.Application()
    
    app.router.add_get('/', handle)
    app.router.add_get('/health', health_check)
    app.router.add_post(f'/{BOT_TOKEN.split(":")[1]}', webhook_handler)
    return app

async def webhook_handler(request):
    """Обработчик вебхука Telegram"""
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
    
    webhook_url = f"https://virtual-tryon-bot.onrender.com/{BOT_TOKEN.split(':')[1]}"
    await bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True
    )
    logger.info(f"Webhook set to {webhook_url}")

async def check_payment_confirmations():
    """Проверяет подтверждение оплаты администратором в Supabase"""
    logger.info("🔄 Starting payment confirmation check loop...")
    while True:
        try:
            # Получаем список пользователей с положительной суммой оплаты
            res = supabase.table(USERS_TABLE)\
                .select("*")\
                .gt(AMOUNT_FIELD, 0)\
                .eq(ACCESS_FIELD, False)\
                .execute()
            
            if not res.data:
                logger.info("ℹ️ No payments found")
                await asyncio.sleep(60)
                continue
            
            for row in res.data:
                try:
                    user_id = int(row.get("user_id", 0)) if row.get("user_id") else 0
                    if not user_id:
                        continue
                        
                    username = row.get("username", "")
                    payment_amount = float(row.get(AMOUNT_FIELD, 0)) if row.get(AMOUNT_FIELD) else 0.0
                    
                    if payment_amount <= 0:
                        continue
                        
                    # Предоставляем доступ
                    tries_left = await supabase_api.grant_access_for_payment(user_id)
                    
                    if tries_left > 0:
                        # Уведомляем пользователя
                        try:
                            await bot.send_message(
                                user_id,
                                f"✅ Ваш платёж подтверждён!\n\n"
                                f"Сумма оплаты: {payment_amount} руб.\n"
                                f"Вам доступно {tries_left} примерок.\n"
                                f"Теперь вы можете продолжить работу с ботом."
                            )
                            
                            # Уведомляем администратора
                            await notify_admin(
                                f"💰 Пользователь @{username} ({user_id}) получил доступ.\n"
                                f"Сумма: {payment_amount} руб, примерок: {tries_left}"
                            )
                            
                        except Exception as notify_error:
                            logger.error(f"Ошибка уведомления пользователя {user_id}: {notify_error}")
                            
                except Exception as row_error:
                    logger.error(f"Ошибка обработки строки платежа: {row_error}")
                    continue
                    
        except Exception as e:
            logger.error(f"Ошибка проверки платежей: {e}")
            
        await asyncio.sleep(60)

async def check_donation_alerts():
    """Проверяет платежи через DonationAlerts API"""
    if not DONATION_ALERTS_TOKEN:
        logger.warning("DonationAlerts token not configured")
        return
        
    logger.info("🔄 Starting DonationAlerts check loop...")
    headers = {
        "Authorization": f"Bearer {DONATION_ALERTS_TOKEN}",
        "Content-Type": "application/json"
    }
    
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                url = "https://www.donationalerts.com/api/v1/alerts/donations"
                params = {
                    "page": 1,
                    "per_page": 10
                }
                
                async with session.get(url, headers=headers, params=params) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"DonationAlerts API error: {resp.status} - {error_text}")
                        await asyncio.sleep(60)
                        continue
                        
                    data = await resp.json()
                    
                for donation in data.get("data", []):
                    try:
                        logger.info(f"Обрабатываем донат: {donation}")

                        message = (donation.get("message") or "").strip()
                        if not message or not message.startswith("@"):
                            continue
                            
                        username = message.split()[0]  # Берем первое слово как username
                        amount = float(donation.get("amount", 0))
                        currency = donation.get("currency", "RUB")
                        
                        if currency != "RUB":
                            logger.warning(f"Unsupported currency: {currency}")
                            continue
                            
                        if amount <= 0:
                            continue
                            
                        # Ищем пользователя в Supabase по username
                        res = supabase.table(USERS_TABLE)\
                            .select("*")\
                            .eq("username", username)\
                            .execute()
                            
                        if not res.data:
                            logger.warning(f"User {username} not found in database")
                            continue
                            
                        user_data = res.data[0]
                        user_id = int(user_data.get("user_id", 0))
                        
                        # Обновляем данные пользователя
                        tries_left = int(amount / PRICE_PER_TRY)
                        update_data = {
                            AMOUNT_FIELD: amount,
                            TRIES_FIELD: tries_left,
                            ACCESS_FIELD: True,
                            STATUS_FIELD: "Оплачено",
                            "payment_confirmed": True,
                            "payment_method": "DonationAlerts",
                            "payment_date": time.strftime("%Y-%m-%d %H:%M:%S")
                        }
                        
                        await supabase_api.update_user_row(user_id, update_data)
                        
                        # Уведомляем пользователя
                        try:
                            await bot.send_message(
                                user_id,
                                f"✅ Ваш платёж подтверждён!\n\n"
                                f"Сумма оплаты: {amount} руб.\n"
                                f"Вам доступно {tries_left} примерок.\n"
                                f"Теперь вы можете продолжить работу с ботом."
                            )
                            
                            await notify_admin(
                                f"💰 Получен платёж через DonationAlerts:\n"
                                f"Пользователь: {username} ({user_id})\n"
                                f"Сумма: {amount} руб\n"
                                f"Примерок: {tries_left}"
                            )
                            
                        except Exception as notify_error:
                            logger.error(f"Ошибка уведомления пользователя {user_id}: {notify_error}")
                            
                    except Exception as donation_error:
                        logger.error(f"Ошибка обработки доната: {donation_error}")
                        continue
                        
        except Exception as e:
            logger.error(f"Ошибка проверки DonationAlerts: {e}")
            
        await asyncio.sleep(60)


async def main():
    """Основная функция запуска бота"""
    try:
        logger.info("Starting bot...")
        
        # Создаем задачи
        tasks = [
            asyncio.create_task(start_web_server()),
            asyncio.create_task(check_results()),
            asyncio.create_task(check_payment_confirmations()),
            asyncio.create_task(check_donation_alerts())
        ]
        
        # Запускаем все задачи
        await asyncio.gather(*tasks)
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        await on_shutdown()
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        # Гарантированная очистка ресурсов
        asyncio.run(cleanup_resources())