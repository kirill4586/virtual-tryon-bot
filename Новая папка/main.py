import logging
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils import executor

API_TOKEN = 'YOUR_TELEGRAM_BOT_TOKEN'
YOOMONEY_ACCOUNT = 'YOUR_YOOMONEY_WALLET'
BOT_USERNAME = 'YOUR_BOT_USERNAME'  # Без @

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# Пользователи с неограниченным доступом
FREE_USERS = {973853935, 6320348591}

# Эмуляция базы данных
user_usage = {}
user_paid_tries = {}

# Генерация кнопки оплаты
def generate_payment_keyboard(user_id: int):
    amount = 30  # базовая цена за примерку
    url = (
        f"https://yoomoney.ru/quickpay/shop-widget?"
        f"writer=seller"
        f"&targets=Оплата примерки"
        f"&targets-hint=Поддержка сервиса и оплата виртуальной примерки"
        f"&default-sum={amount}"
        f"&button-text=11"
        f"&payment-type-choice=on"
        f"&successURL=https://t.me/{BOT_USERNAME}"
        f"&quickpay=shop"
        f"&account={YOOMONEY_ACCOUNT}"
        f"&label={user_id}"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить 30₽", url=url)]
    ])
    return keyboard

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.answer("👋 Добро пожаловать! Пришлите первое фото для примерки.")

@dp.message_handler(content_types=['photo'])
async def handle_photo(message: types.Message):
    user_id = message.from_user.id

    # Разрешение для бесплатных пользователей
    if user_id in FREE_USERS:
        await message.answer("✅ Вы можете использовать примерку без ограничений.")
        return

    used = user_usage.get(user_id, 0)
    paid_tries = user_paid_tries.get(user_id, 0)

    if used == 0:
        user_usage[user_id] = 1
        await message.answer("✅ Первая примерка бесплатна. Начинаем обработку...")
    elif paid_tries > 0:
        user_usage[user_id] += 1
        user_paid_tries[user_id] -= 1
        await message.answer("✅ Оплаченная примерка. Начинаем обработку...")
    else:
        keyboard = generate_payment_keyboard(user_id)
        await message.answer(
            "Спасибо, что воспользовались нашей виртуальной примерочной. Первая примерка была демонстрационной."
            " Последующие стоят 30₽. Сумма символическая, чтобы и Вам помочь стать красивыми, и окупить проект.",
            reply_markup=keyboard
        )

# Webhook обработка от ЮMoney (эмуляция)
async def handle_yoomoney_webhook(data):
    label = int(data.get("label"))
    amount = int(float(data.get("amount", 0)))
    tries = amount // 30

    if tries:
        user_paid_tries[label] = user_paid_tries.get(label, 0) + tries
        await bot.send_message(
            label,
            f"✅ Оплата {amount}₽ получена! Вам доступно {tries} примерок."
        )
        # Уведомление админу
        admin_id = 973853935  # пример
        await bot.send_message(admin_id, f"💰 Платная примерка от пользователя {label}. Сумма: {amount}₽.")

# Для запуска
if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
