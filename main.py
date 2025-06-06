import os
import logging
import asyncio
import aiohttp
import shutil
import sys
import time
import json
import websockets
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
CUSTOM_PAYMENT_BTN_TEXT = "💳 Оплатить произвольную сумму"
MIN_PAYMENT_AMOUNT = 1
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASEROW_TOKEN = os.getenv("BASEROW_TOKEN")
TABLE_ID = int(os.getenv("TABLE_ID"))
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
PRICE_PER_TRY = 1
FREE_USERS = {6320348591, 973853935}
UPLOAD_DIR = "uploads"
MODELS_BUCKET = "models"
EXAMPLES_BUCKET = "examples"
UPLOADS_BUCKET = "uploads"
SUPPORTED_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
EXAMPLES_PER_PAGE = 3
MODELS_PER_PAGE = 3
DONATION_ALERTS_TOKEN = os.getenv("DONATION_ALERTS_TOKEN", "86S92IBrd8PTovv8W9LHaIFAeBV2l1iuHbXeEa4m")
PORT = int(os.getenv("PORT", 4000))

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

async def donation_socket_listener():
    """WebSocket клиент для получения донатов в реальном времени"""
    logger.info("🔌 Запуск WebSocket-клиента DonationAlerts")
    uri = "wss://socket.donationalerts.ru:443/socket.io/?EIO=3&transport=websocket"
    token = DONATION_ALERTS_TOKEN
    last_donations = set()

    while True:
        try:
            async with websockets.connect(uri) as ws:
                await ws.send('40')  # Инициализация соединения
                await asyncio.sleep(1)
                await ws.send(f'42["add-user",{{"token":"{token}"}}]')

                while True:
                    msg = await ws.recv()

                    if msg.startswith('42'):
                        try:
                            parsed = json.loads(msg[2:])
                            event, data = parsed

                            if event == 'donation':
                                donation_id = data.get("id")
                                if donation_id in last_donations:
                                    continue

                                last_donations.add(donation_id)
                                amount = int(float(data.get("amount", 0)))
                                message = data.get("message", "")
                                status = data.get("status")

                                if status != "success":
                                    continue

                                # Определение пользователя
                                telegram_id = None
                                telegram_username = None

                                if message.startswith('@'):
                                    telegram_username = message[1:].strip()
                                elif "TelegramID_" in message:
                                    try:
                                        telegram_id = int(message.replace("TelegramID_", "").strip())
                                    except ValueError:
                                        continue

                                tries = max(1, amount // PRICE_PER_TRY)
                                logger.info(f"💸 [SOCKET] Донат: {amount} руб от {telegram_username or telegram_id}, примерок: {tries}")

                                result = await baserow.upsert_row(
                                    user_id=telegram_id if telegram_id else 0,
                                    username=telegram_username or "",
                                    data={
                                        "tries_left": tries,
                                        "payment_status": "Оплачено (через WebSocket)",
                                        "last_payment_amount": amount,
                                        "last_payment_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                                        "status": "Активен"
                                    }
                                )

                                if telegram_id:
                                    try:
                                        await bot.send_message(
                                            telegram_id,
                                            f"✅ Оплата {amount} руб получена!\nВам доступно {tries} примерок."
                                        )
                                    except Exception as e:
                                        logger.warning(f"⚠️ Не удалось отправить сообщение пользователю {telegram_id}: {e}")

                                await notify_admin(
                                    f"💰 [SOCKET] Оплата {amount} руб от {telegram_username or telegram_id}, примерок: {tries}"
                                )

                        except Exception as e:
                            logger.error(f"Ошибка парсинга WebSocket-сообщения: {e}")

        except Exception as e:
            logger.error(f"❌ Ошибка подключения к WebSocket DonationAlerts: {e}")
            await asyncio.sleep(10)

def make_donation_link(user: types.User, amount: int = 1, fixed: bool = True) -> str:
    username = f"@{user.username}" if user.username else f"TelegramID_{user.id}"
    message = username.replace(" ", "_")
    if fixed:
        return f"https://www.donationalerts.com/r/primerochnay777?amount={amount}&message={message}&fixed_amount=true"
    else:
        return f"https://www.donationalerts.com/r/primerochnay777?amount={amount}&message={message}"

async def upload_to_supabase(file_path: str, user_id: int, file_type: str):
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

def list_all_files(bucket, prefix):
    files = []
    try:
        items = bucket.list(prefix)
        for item in items:
            name = item.get('name')
            if name:
                full_path = f"{prefix}/{name}".strip("/")
                if name.endswith('/'):
                    files += list_all_files(bucket, full_path)
                else:
                    files.append(full_path)
    except Exception as e:
        logger.error(f"❌ Ошибка обхода Supabase Storage в {prefix}: {e}")
    return files

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
        
        await baserow.reset_flags(user_id)
        
        await baserow.upsert_row(user_id, username, {
            "status": "started",
            "photo_clothes": False,
            "photo_person": False,
            "model_selected": None,
            "tries_left": 1
        }) 
        
        await notify_admin(f"🆕 Пользователь: @{username} ({user_id})")
        
    except Exception as e:
        logger.error(f"Welcome error for {user_id}: {e}")

@dp.message(Command("start"))
@dp.message(F.text & ~F.text.regexp(r'^\d+$'))
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
        if user_id in FREE_USERS:
            await process_photo(message, user, user_dir)
            return
            
        tries_left = await get_user_tries(user_id)
        
        if tries_left <= 0:
            await message.answer(
                "🚫 У вас закончились бесплатные примерки.\n\n"
                "Для продолжения работы оплатите услугу:\n"
                "💵 Стоимость одной примерки: 1 руб.\n"
                "После оплаты вам будет доступно количество примерок в соответствии с внесенной суммой.\n\n"
                "Например:\n"
                "1 руб = 1 примерка\n"
                "10 руб = 10 примерок\n"
                "100 руб = 100 примерок и т.д.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💳 Оплатить 1 руб (1 примерка)", 
                            url=make_donation_link(user, 1)
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="💳 Оплатить 10 руб (10 примерок)", 
                            url=make_donation_link(user, 10)
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="💳 Оплатить 100 руб (100 примерок)", 
                            url=make_donation_link(user, 100)
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="💳 Оплатить произвольную сумму", 
                            url=make_donation_link(user, 1, False)
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
            return
            
        await process_photo(message, user, user_dir)
        
    except Exception as e:
        logger.error(f"Error handling photo: {e}")
        await message.answer("❌ Ошибка при сохранении файла. Попробуйте ещё раз.")

async def process_photo(message: types.Message, user: types.User, user_dir: str):
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
            
        if photo_number == 1:
            photo = message.photo[-1]
            file_ext = os.path.splitext(photo.file_id)[1] or '.jpg'
            file_name = f"photo_{photo_number}{file_ext}"
            file_path = os.path.join(user_dir, file_name)
            
            await bot.download(photo, destination=file_path)
            
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

@dp.callback_query(F.data == "payment_options")
async def show_payment_methods(callback_query: types.CallbackQuery):
    user = callback_query.from_user
    await callback_query.message.edit_text(
        "Для продолжения работы с ботом необходимо оплатить услугу:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить 1 руб (1 примерка)", 
                    url=make_donation_link(user, 1)
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Оплатить 10 руб (10 примерок)", 
                    url=make_donation_link(user, 10)
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Оплатить 100 руб (100 примерок)", 
                    url=make_donation_link(user, 100)
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Оплатить произвольную сумму", 
                    url=make_donation_link(user, 1, False)
                )  # <-- Добавлена закрывающая скобка
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
    user = callback_query.from_user
    await callback_query.message.answer(
        "✅ Спасибо! Мы проверим ваш платёж и активируем доступ в течение нескольких минут.\n\n"
        "Если вы указали ваш Telegram username при оплате, это поможет быстрее вас найти. "
        "При необходимости — напишите нам в поддержку."
    )
    await notify_admin(f"💰 Пользователь @{user.username} ({user.id}) сообщил об оплате через DonationAlerts. Требуется ручная проверка.")

async def handle_donation_webhook(request):
    """Обработчик вебхука DonationAlerts"""
    try:
        logger.info(f"Incoming webhook headers: {dict(request.headers)}")
        logger.info(f"Incoming webhook body: {await request.text()}")
        
        auth_token = request.headers.get('Authorization')
        if auth_token != f"Bearer {DONATION_ALERTS_TOKEN}":
            logger.warning(f"Invalid auth token: {auth_token}")
            return web.Response(status=403)
        
        data = await request.json()
        logger.info(f"Donation received: {data}")

        if data.get('status') == 'success':
            amount = int(float(data.get('amount', 0)))
            user_message = data.get('message', '')
            
            telegram_username = None
            telegram_id = None
            if user_message.startswith('@'):
                telegram_username = user_message[1:].strip()
            elif 'TelegramID_' in user_message:
                try:
                    telegram_id = int(user_message.replace('TelegramID_', '').strip())
                except ValueError:
                    logger.error(f"Invalid Telegram ID format in message: {user_message}")
            
            if not telegram_username and not telegram_id:
                admin_msg = (
                    f"⚠️ Получен платеж {amount} руб, но не удалось определить пользователя.\n"
                    f"Сообщение: {user_message}"
                )
                await notify_admin(admin_msg)
                return web.Response(status=200)
            
            tries_added = max(1, amount // PRICE_PER_TRY) if amount >= PRICE_PER_TRY else 0
            
            if tries_added == 0:
                logger.warning(f"Amount {amount} is less than PRICE_PER_TRY {PRICE_PER_TRY}")
                return web.Response(status=200)
            
            logger.info(f"Processing payment for {telegram_username or telegram_id}, amount: {amount} руб, tries to add: {tries_added}")

            update_success = False
            try:
                update_data = {
                    "tries_left": tries_added,
                    "last_payment_amount": amount,
                    "last_payment_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "payment_status": "Оплачено",
                    "status": "Активен"
                }

                if telegram_username:
                    update_data["username"] = telegram_username

                if telegram_id:
                    filter_field = "user_id"
                    filter_value = str(telegram_id)
                else:
                    filter_field = "username"
                    filter_value = telegram_username

                result = await baserow.upsert_row(
                    user_id=telegram_id if telegram_id else 0,
                    username=telegram_username or "",
                    data=update_data
                )

                if result:
                    update_success = True
                    logger.info(f"Successfully updated Baserow for {telegram_username or telegram_id}")
                else:
                    logger.error(f"Failed to update Baserow for {telegram_username or telegram_id}")

            except Exception as e:
                logger.error(f"Error updating Baserow for {telegram_username or telegram_id}: {e}")

            try:
                admin_message = (
                    f"💰 Получен платеж через DonationAlerts:\n"
                    f"• Сумма: {amount} руб\n"
                    f"• Примерок добавлено: {tries_added}\n"
                    f"• Пользователь: {telegram_username or f'TelegramID_{telegram_id}'}\n"
                    f"• Статус обновления: {'Успешно' if update_success else 'Ошибка'}"
                )
                await notify_admin(admin_message)

                if telegram_id:
                    try:
                        user_message = (
                            f"✅ Ваш платеж на {amount} руб успешно получен!\n\n"
                            f"Вам добавлено {tries_added} примерок.\n"
                            f"Теперь вы можете продолжить работу с ботом."
                        )
                        await bot.send_message(telegram_id, user_message)
                    except Exception as e:
                        logger.error(f"Error sending notification to user {telegram_id}: {e}")
                        await notify_admin(f"⚠️ Не удалось отправить уведомление пользователю {telegram_username or f'TelegramID_{telegram_id}'}")

            except Exception as e:
                logger.error(f"Error sending notifications: {e}")

    except Exception as e:
        logger.error(f"Error processing donation webhook: {e}", exc_info=True)
    
    return web.Response(status=200)

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
                            await baserow.upsert_row(user_id, "", {
                                "status": "Результат отправлен",
                                "result_sent": True,
                                "ready": True,
                                "result_url": supabase_path if 'supabase_path' in locals() else None
                            })
                        except Exception as db_error:
                            logger.error(f"❌ Ошибка обновления Baserow: {db_error}")

                        try:
                            shutil.rmtree(user_dir)
                            logger.info(f"🗑️ Папка {user_dir} удалена")
                        except Exception as cleanup_error:
                            logger.error(f"❌ Ошибка удаления папки: {cleanup_error}")

                        try:
                            base = supabase.storage.from_(UPLOADS_BUCKET)
                            files_to_delete = []

                            for ext in SUPPORTED_EXTENSIONS:
                                files_to_delete.extend([
                                    f"{user_id_str}/photos/photo_1{ext}",
                                    f"{user_id_str}/photos/photo_2{ext}"
                                ])

                            try:
                                result_files_in_supabase = base.list(f"{user_id_str}/results")
                                for f in result_files_in_supabase:
                                    if f['name'].startswith("result"):
                                        files_to_delete.append(f"{user_id_str}/results/{f['name']}")
                            except Exception as e:
                                logger.warning(f"⚠️ Не удалось получить список result-файлов из results/: {e}")

                            try:
                                root_files = base.list(user_id_str)
                                for f in root_files:
                                    if f['name'].startswith("result") and any(f['name'].lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS):
                                        files_to_delete.append(f"{user_id_str}/{f['name']}")
                            except Exception as e:
                                logger.warning(f"⚠️ Не удалось получить список result-файлов из корня: {e}")

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

async def handle(request):
    return web.Response(text="Bot is running")

async def health_check(request):
    return web.Response(text="OK", status=200)

def setup_web_server():
    app = web.Application()
    
    app.router.add_get('/', handle)
    app.router.add_get('/health', health_check)
    app.router.add_post('/donation_callback', handle_donation_webhook)
    app.router.add_post(f'/{BOT_TOKEN.split(":")[1]}', webhook_handler)
    return app

async def webhook_handler(request):
    try:
        update = await request.json()
        await dp.feed_webhook_update(bot, update)
        return web.Response(text="OK")
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500, text="Internal Server Error")
    
async def start_web_server():
    app = setup_web_server()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")
    
async def on_shutdown():
    logger.info("Shutting down...")
    await bot.delete_webhook()
    logger.info("Webhook removed")

async def check_donations_loop():
    logger.info("🔄 Запуск задачи проверки донатов через API DonationAlerts")
    last_donation_ids = set()

    headers = {
        "Authorization": f"Bearer {DONATION_ALERTS_TOKEN}"
    }

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://www.donationalerts.com/api/v1/alerts/donations/",
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"❌ Ошибка запроса донатов: {resp.status}")
                        await asyncio.sleep(60)
                        continue

                    data = await resp.json()
                    donations = data.get("donations", [])

                    for d in donations:
                        donation_id = d.get("id")
                        if donation_id in last_donation_ids:
                            continue

                        last_donation_ids.add(donation_id)
                        amount = int(float(d.get("amount", 0)))
                        message = d.get("message", "")
                        status = d.get("status")

                        if status != "success":
                            continue

                        telegram_id = None
                        telegram_username = None

                        if message.startswith('@'):
                            telegram_username = message[1:].strip()
                        elif "TelegramID_" in message:
                            try:
                                telegram_id = int(message.replace("TelegramID_", "").strip())
                            except ValueError:
                                continue

                        tries = max(1, amount // PRICE_PER_TRY)
                        logger.info(f"💸 Новый донат: {amount} руб от {telegram_username or telegram_id}, примерок: {tries}")

                        result = await baserow.upsert_row(
                            user_id=telegram_id if telegram_id else 0,
                            username=telegram_username or "",
                            data={
                                "tries_left": tries,
                                "payment_status": "Оплачено (через API)",
                                "last_payment_amount": amount,
                                "last_payment_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "status": "Активен"
                            }
                        )

                        if telegram_id:
                            try:
                                await bot.send_message(
                                    telegram_id,
                                    f"✅ Оплата {amount} руб получена!\nВам доступно {tries} примерок."
                                )
                            except Exception as e:
                                logger.warning(f"⚠️ Не удалось отправить сообщение пользователю {telegram_id}: {e}")

                        await notify_admin(
                            f"💰 Оплата {amount} руб от {telegram_username or telegram_id}, начислено {tries} примерок."
                        )

        except aiohttp.ClientConnectorError as e:
            logger.warning(f"⚠️ Ошибка подключения к DonationAlerts: {e}. Повтор через 5 минут...")
            await asyncio.sleep(300)
            continue

        except Exception as e:
            logger.error(f"❌ Ошибка в check_donations_loop: {e}")
            await asyncio.sleep(60)

        await asyncio.sleep(60)

async def main():
    try:
        logger.info("Starting bot...")
        
        app = setup_web_server()
        runner = web.AppRunner(app)
        await runner.setup()
        
        webhook_url = f"https://virtual-tryon-bot.onrender.com/{BOT_TOKEN.split(':')[1]}"
        await bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
        )
        logger.info(f"Webhook set to: {webhook_url}")
        
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logger.info(f"Web server started on port {PORT}")
        
        asyncio.create_task(check_results())
        asyncio.create_task(check_donations_loop())
        asyncio.create_task(start_socketio_donation_listener())


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
		
		import socketio

async def start_socketio_donation_listener():
    sio = socketio.AsyncClient()
    token = DONATION_ALERTS_TOKEN

    @sio.event
    async def connect():
        logger.info("🔌 Socket.IO соединение установлено с DonationAlerts")
        await sio.emit("add-user", {"token": token})

    @sio.event
    async def connect_error(data):
        logger.error(f"❌ Ошибка подключения к Socket.IO: {data}")

    @sio.event
    async def disconnect():
        logger.warning("⚠️ Socket.IO соединение разорвано")

    @sio.on("donation")
    async def on_donation(data):
        try:
            logger.info(f"💸 [SOCKET.IO] Получен донат: {data}")

            amount = int(float(data.get("amount", 0)))
            message = data.get("message", "")
            status = data.get("status")

            if status != "success":
                logger.warning("❌ Донат неуспешен, игнорируем.")
                return

            telegram_id = None
            telegram_username = None

            if message.startswith('@'):
                telegram_username = message[1:].strip()
            elif "TelegramID_" in message:
                try:
                    telegram_id = int(message.replace("TelegramID_", "").strip())
                except ValueError:
                    logger.warning(f"⚠️ Не удалось распарсить Telegram ID из сообщения: {message}")
                    return

            tries = max(1, amount // PRICE_PER_TRY)

            logger.info(f"🔗 Донат от {telegram_username or telegram_id} на {amount} руб → {tries} примерок")

            result = await baserow.upsert_row(
                user_id=telegram_id if telegram_id else 0,
                username=telegram_username or "",
                data={
                    "tries_left": tries,
                    "payment_status": "Оплачено (через WebSocket)",
                    "last_payment_amount": amount,
                    "last_payment_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "Активен"
                }
            )

            if telegram_id:
                try:
                    await bot.send_message(
                        telegram_id,
                        f"✅ Ваш платёж на {amount} руб получен!\n"
                        f"Вам доступно {tries} примерок."
                    )
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось отправить сообщение пользователю {telegram_id}: {e}")

            await notify_admin(
                f"💰 [SOCKET.IO] Оплата {amount} руб от {telegram_username or telegram_id}, начислено {tries} примерок."
            )

        except Exception as e:
            logger.error(f"❌ Ошибка обработки доната через socket.io: {e}")

    try:
        await sio.connect("https://socket.donationalerts.ru", transports=['websocket'])
        await sio.wait()
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к socket.io DonationAlerts: {e}")
