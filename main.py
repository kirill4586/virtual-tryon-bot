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
BASEROW_TOKEN = os.getenv("BASEROW_TOKEN")
TABLE_ID = int(os.getenv("TABLE_ID"))
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
DONATION_ALERTS_TOKEN = os.getenv("DONATION_ALERTS_TOKEN")
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

async def cleanup_resources():
    """Закрытие всех ресурсов и соединений"""
    logger.info("Cleaning up resources...")
    
    # Закрытие сессий aiohttp
    if 'session' in globals():
        await session.close()
    
    # Закрытие соединения с ботом
    await bot.session.close()
    
    # Закрытие соединения с Supabase
    if supabase:
        await supabase.postgrest.aclose()
    
    logger.info("All resources cleaned up")

async def on_shutdown():
    """Обработчик завершения работы"""
    try:
        logger.info("Shutting down...")
        
        # Отмена всех pending задач
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        
        # Ожидание завершения задач
        await asyncio.gather(*tasks, return_exceptions=True)
        
        # Удаление вебхука
        await bot.delete_webhook()
        logger.info("Webhook removed")
        
        # Очистка ресурсов
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
    """Получение количества оставшихся примерок у пользователя"""
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
    """Обновление количества оставшихся примерок"""
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
    """Проверка, идет ли обработка для пользователя"""
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
    """Отправка примеров работ"""
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

@dp.callback_query(F.data == "back_to_menu"))
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
    
    # Добавляем запись в Baserow о попытке оплаты
    await baserow.upsert_row(
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
    
    # Обновляем статус в Baserow
    await baserow.upsert_row(
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
    
    # Уведомляем администратора
    await notify_admin(
        f"💰 Пользователь @{user.username} ({user.id}) сообщил об оплате.\n"
        f"Требуется проверка и подтверждение в Baserow."
    )

async def process_photo(message: types.Message, user: types.User, user_dir: str):
    """Обработка загруженных фотографий"""
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

async def check_payment_confirmations():
    """Проверяет подтверждение оплаты администратором в Baserow"""
    logger.info("🔄 Starting payment confirmation check loop...")
    while True:
        try:
            # Получаем список пользователей, ожидающих подтверждения оплаты
            url = f"{baserow.base_url}/?user_field_names=true&filter__payment_requested__equal=true&filter__payment_confirmed__equal=false"
            headers = baserow.headers
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        logger.error(f"Error getting pending payments: {resp.status}")
                        await asyncio.sleep(60)
                        continue
                    
                    rows = await resp.json()
                    
                    if not rows.get("results"):
                        logger.info("ℹ️ No pending payments found")
                        await asyncio.sleep(60)
                        continue
                    
                    for row in rows["results"]:
                        user_id = int(row["user_id"])
                        username = row["username"]
                        tries_added = row.get("tries_left", 0)
                        
                        if tries_added > 0 and row.get("payment_confirmed") == True:
                            # Отправляем уведомление пользователю
                            try:
                                await bot.send_message(
                                    user_id,
                                    f"✅ Ваш платёж подтверждён!\n\n"
                                    f"Вам доступно {tries_added} примерок.\n"
                                    f"Теперь вы можете продолжить работу с ботом."
                                )
                                
                                # Обновляем статус в Baserow
                                update_url = f"{baserow.base_url}/{row['id']}/?user_field_names=true"
                                await session.patch(update_url, headers=headers, json={
                                    "payment_status": "Оплачено и подтверждено",
                                    "status": "Активен",
                                    "payment_confirmed": True,
                                    "confirmation_date": time.strftime("%Y-%m-%d %H:%M:%S")
                                })
                                
                                logger.info(f"💰 Payment confirmed for {username} ({user_id}), {tries_added} tries added")
                                
                            except Exception as e:
                                logger.error(f"Error notifying user {username} ({user_id}): {e}")
                                continue
                            
        except Exception as e:
            logger.error(f"Error in payment confirmation check: {e}")
        
        await asyncio.sleep(30)  # Проверяем каждые 30 секунд

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
                            await baserow.upsert_row(user_id, "", {
                                "status": "Результат отправлен",
                                "result_sent": True,
                                "ready": True,
                                "result_url": supabase_path if 'supabase_path' in locals() else None,
                                "last_try_date": time.strftime("%Y-%m-%d %H:%M:%S")
                            })
                        except Exception as db_error:
                            logger.error(f"❌ Ошибка обновления Baserow: {db_error}")

                        try:
                            shutil.rmtree(user_dir)
                            logger.info(f"🗑️ Папка {user_dir} удалена")
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
    
async def main():
    """Основная функция запуска бота"""
    try:
        logger.info("Starting bot...")
        
        # Создаем список задач для корректного завершения
        tasks = [
            asyncio.create_task(check_results()),
            asyncio.create_task(check_payment_confirmations())
        ]
        
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
        
        # Ожидаем завершения всех задач
        await asyncio.gather(*tasks)
        
    except asyncio.CancelledError:
        logger.info("Received cancel signal")
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