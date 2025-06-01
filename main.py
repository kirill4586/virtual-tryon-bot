import os
import asyncio
import shutil
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv
from utils import get_models_list, get_examples_list, is_processing, notify_admin, baserow, supabase, create_payment_link

# Загрузка переменных окружения
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
UPLOAD_DIR = "uploads"
MODELS_BUCKET = "models"
SUPPORTED_EXTENSIONS = [".jpg", ".jpeg", ".png"]
EXEMPT_USER_IDS = [973853935, 6320348591]
FREE_TRY_COUNT = 1

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODELS_PER_PAGE = 3

@dp.message(F.text)
async def welcome(message: types.Message):
    user = message.from_user
    user_id = user.id
    username = user.username or "unknown"
    
    os.makedirs(os.path.join(UPLOAD_DIR, str(user_id)), exist_ok=True)

    await bot.send_media_group(
        chat_id=user_id,
        media=[
            types.InputMediaPhoto(media=FSInputFile("assets/cover1.jpg")),
            types.InputMediaPhoto(media=FSInputFile("assets/cover2.jpg")),
            types.InputMediaPhoto(media=FSInputFile("assets/cover3.jpg"))
        ]
    )

    await bot.send_message(
        user_id,
        "👋 Добро пожаловать в виртуальную примерочную!\n\n"
        "1️⃣ Отправьте фото одежды.\n"
        "2️⃣ Затем выберите модель или отправьте фото человека.\n\n"
        "👗 Начнем? Просто пришлите фото."
    )

@dp.callback_query(F.data.startswith("models_"))
async def show_models(callback_query: types.CallbackQuery):
    try:
        _, category, page = callback_query.data.split("_")
        page = int(page)
    except:
        await callback_query.answer("Ошибка запроса")
        return

    category_names = {
        "man": "👨 Мужские модели",
        "woman": "👩 Женские модели", 
        "child": "🧒 Детские модели"
    }
    
    try:
        models = await get_models_list(category)
        if not models:
            await callback_query.message.answer("❌ В данной категории пока нет доступных моделей.")
            return

        start_idx = page * MODELS_PER_PAGE
        end_idx = start_idx + MODELS_PER_PAGE
        current_models = models[start_idx:end_idx]

        if page == 0:
            await callback_query.message.answer(f"{category_names.get(category, 'Модели')}:")

        for model in current_models:
            model_name = os.path.splitext(model)[0]
            image_url = supabase.storage.from_(MODELS_BUCKET).get_public_url(f"{category}/{model}")
            await bot.send_photo(
                chat_id=callback_query.from_user.id,
                photo=image_url,
                caption=f"Модель: {model_name}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="✅ Выбрать эту модель", callback_data=f"model_{category}/{model}")]]
                )
            )

        if end_idx < len(models):
            await callback_query.message.answer(
                "Показать еще модели?",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="⬇️ Показать еще", callback_data=f"models_{category}_{page + 1}")]]
                )
            )
        else:
            await callback_query.message.answer("✅ Это все доступные модели в данной категории.")

    except Exception as e:
        logger.error(f"Error in show_models: {e}")
        await callback_query.message.answer("⚠️ Ошибка при загрузке моделей. Попробуйте позже.")

@dp.callback_query(F.data.startswith("model_"))
async def model_selected(callback_query: types.CallbackQuery):
    if await is_processing(callback_query.from_user.id):
        await callback_query.answer("✅ Ожидайте результат!", show_alert=True)
        return

    model_path = callback_query.data.replace("model_", "")
    category, model_name = model_path.split('/')
    user_id = callback_query.from_user.id
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)

    await callback_query.message.delete()

    clothes_photo_exists = any(f.startswith("photo_1") for f in os.listdir(user_dir))

    model_display_name = os.path.splitext(model_name)[0]

    await baserow.upsert_row(user_id, callback_query.from_user.username, {
        "model_selected": model_path,
        "status": "model_selected"
    })

    model_url = supabase.storage.from_(MODELS_BUCKET).get_public_url(model_path)
    model_local_path = os.path.join(user_dir, "selected_model.jpg")

    res = supabase.storage.from_(MODELS_BUCKET).download(model_path)
    with open(model_local_path, 'wb') as f:
        f.write(res)

    if clothes_photo_exists:
        await baserow.upsert_row(user_id, callback_query.from_user.username, {
            "photo_person": True,
            "status": "В обработке",
            "photo1_received": True,
            "photo2_received": True
        })
        await notify_admin(f"📸 Все фото получены от @{callback_query.from_user.username} ({user_id})")
        await bot.send_photo(user_id, photo=model_url, caption=f"✅ Модель {model_display_name} выбрана.\n\n🔄 Идёт примерка. Ожидайте результат!")
    else:
        await baserow.upsert_row(user_id, callback_query.from_user.username, {
            "photo1_received": False,
            "photo2_received": True
        })
        await bot.send_photo(user_id, photo=model_url, caption=f"✅ Модель {model_display_name} выбрана.\n\n📸 Теперь отправьте фото одежды.")

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "unknown"
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)

    existing_photos = [f for f in os.listdir(user_dir) if f.startswith("photo_")]
    photo_number = len(existing_photos) + 1

    if photo_number > 2:
        await message.answer("✅ Вы уже загрузили 2 файла. Ожидайте результат.")
        return

    file = message.photo[-1]
    file_name = f"photo_{photo_number}.jpg"
    file_path = os.path.join(user_dir, file_name)
    await bot.download(file, destination=file_path)

    model_selected = os.path.exists(os.path.join(user_dir, "selected_model.jpg"))
    first_photo_exists = photo_number == 2

    if photo_number == 2 and not model_selected:
        await baserow.upsert_row(user_id, username, {
            "photo_person": True,
            "status": "В обработке",
            "photo1_received": True,
            "photo2_received": True
        })
        await notify_admin(f"📸 Новые фото от @{username} ({user_id})")
        await message.answer("✅ Оба файла получены.\n\n🔄 Идёт примерка. Ожидайте результат!")
        return

    if photo_number == 1:
        await baserow.upsert_row(user_id, username, {
            "photo_clothes": True,
            "status": "Ожидается второе фото",
            "photo1_received": True,
            "photo2_received": False
        })
        await message.answer("✅ Фото одежды получено.\n\nТеперь выберите модель или отправьте фото человека.")

async def check_results():
    while True:
        try:
            for user_id in os.listdir(UPLOAD_DIR):
                user_dir = os.path.join(UPLOAD_DIR, user_id)
                if not os.path.isdir(user_dir):
                    continue

                for ext in SUPPORTED_EXTENSIONS:
                    result_path = os.path.join(user_dir, f"result{ext}")
                    if os.path.exists(result_path):
                        await bot.send_photo(
                            chat_id=int(user_id),
                            photo=FSInputFile(result_path),
                            caption="🎉 Ваша виртуальная примерка готова!\n👚 Хотите ещё примерку? Просто отправьте новое фото."
                        )

                        await baserow.upsert_row(int(user_id), "", {
                            "status": "Результат отправлен",
                            "result_sent": True,
                            "ready": True,
                            "photo1_received": False,
                            "photo2_received": False
                        })

                        shutil.rmtree(user_dir)
                        logger.info(f"Result sent to {user_id}")

        except Exception as e:
            logger.error(f"Error in check_results: {e}")

        await asyncio.sleep(10)

async def main():
    logger.info("Starting bot...")

    if supabase:
        for category in ["man", "woman", "child"]:
            try:
                models = await get_models_list(category)
                logger.info(f"Loaded {len(models)} models for {category}")
            except Exception as e:
                logger.warning(f"No models for {category}: {e}")

    asyncio.create_task(check_results())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
