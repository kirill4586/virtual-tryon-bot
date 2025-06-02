import os
import uuid
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from utils.supabase_client import upload_to_supabase
from utils.baserow_client import get_or_create_user_row, update_user_row, get_user_row

load_dotenv()

bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher(storage=MemoryStorage())

PRELOADER_IMAGES = ["images/slide1.jpg", "images/slide2.jpg", "images/slide3.jpg"]
FREE_USERS = [973853935, 6320348591]

class UploadStates(StatesGroup):
    waiting_for_first_photo = State()
    waiting_for_second_photo = State()


@dp.message(F.text == "/start")
async def cmd_start(message: types.Message, state: FSMContext):
    await get_or_create_user_row(message.from_user.id, message.from_user.username)
    for path in PRELOADER_IMAGES:
        await bot.send_photo(message.chat.id, InputFile(path))

    await bot.send_message(
        message.chat.id,
        "Добро пожаловать в виртуальную примерочную!\n\nВыберите категорию моделей:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Женщины", callback_data="category_women")],
            [InlineKeyboardButton(text="Мужчины", callback_data="category_men")],
            [InlineKeyboardButton(text="Дети", callback_data="category_kids")],
        ])
    )


@dp.callback_query(F.data.startswith("category_"))
async def handle_category_selection(callback: types.CallbackQuery, state: FSMContext):
    category = callback.data.replace("category_", "")
    await update_user_row(callback.from_user.id, {"selected_model": category})
    await show_model_preview(callback, category, page=0)


async def show_model_preview(callback, category, page):
    photo_path = f"images/models/{category}/{page}.jpg"
    markup = InlineKeyboardBuilder()
    if page > 0:
        markup.button(text="⬅️ Назад", callback_data=f"prev_{category}_{page-1}")
    markup.button(text="Выбрать", callback_data=f"select_{category}_{page}")
    markup.button(text="➡️ Далее", callback_data=f"next_{category}_{page+1}")
    markup.adjust(3)

    await bot.send_photo(
        callback.message.chat.id,
        InputFile(photo_path),
        caption="Пример модели",
        reply_markup=markup.as_markup()
    )


@dp.callback_query(F.data.startswith("next_"))
async def next_model(callback: types.CallbackQuery):
    _, category, page = callback.data.split("_")
    await show_model_preview(callback, category, int(page))


@dp.callback_query(F.data.startswith("prev_"))
async def prev_model(callback: types.CallbackQuery):
    _, category, page = callback.data.split("_")
    await show_model_preview(callback, category, int(page))


@dp.callback_query(F.data.startswith("select_"))
async def select_model(callback: types.CallbackQuery, state: FSMContext):
    _, category, page = callback.data.split("_")
    await update_user_row(callback.from_user.id, {"selected_model": category, "model_index": int(page)})
    await update_user_row(callback.from_user.id, {"photo1_received": False, "photo2_received": False})

    await bot.send_message(callback.message.chat.id, "Пожалуйста, отправьте ваше первое фото для примерки")
    await state.set_state(UploadStates.waiting_for_first_photo)


@dp.message(UploadStates.waiting_for_first_photo, F.photo)
async def receive_first_photo(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    photo_file = await bot.get_file(photo.file_id)
    photo_data = await bot.download_file(photo_file.file_path)

    filename = f"uploads/{message.from_user.id}/photo1_{uuid.uuid4()}.jpg"
    await upload_to_supabase(filename, photo_data.read())
    await update_user_row(message.from_user.id, {"photo1_received": True})

    await message.answer("Фото 1 получено. Теперь отправьте второе фото (если нужно).")
    await state.set_state(UploadStates.waiting_for_second_photo)


@dp.message(UploadStates.waiting_for_second_photo, F.photo)
async def receive_second_photo(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    photo_file = await bot.get_file(photo.file_id)
    photo_data = await bot.download_file(photo_file.file_path)

    filename = f"uploads/{message.from_user.id}/photo2_{uuid.uuid4()}.jpg"
    await upload_to_supabase(filename, photo_data.read())
    await update_user_row(message.from_user.id, {"photo2_received": True})

    user_row = await get_user_row(message.from_user.id)
    if not user_row:
        await message.answer("Ошибка: не удалось найти ваш профиль.")
        return

    if user_row.get("free_try_used") is False or message.from_user.id in FREE_USERS:
        await update_user_row(message.from_user.id, {"free_try_used": True})
        await message.answer("Запускаем примерку... Пожалуйста, подождите.")
    else:
        pay_btn = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить 30₽", url="https://yoomoney.ru/to/4100118668019515")]
        ])
        await message.answer("Бесплатная примерка уже использована. Чтобы продолжить, оплатите 30₽.", reply_markup=pay_btn)

    await state.clear()


if __name__ == "__main__":
    import asyncio
    from aiogram import executor

    async def main():
        await dp.start_polling(bot)

    asyncio.run(main())
