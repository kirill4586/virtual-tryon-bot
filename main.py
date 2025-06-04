@dp.message(F.photo)
async def handle_photo(message: types.Message):
    # ... предыдущий код ...
    if tries_left <= 0:
        await message.answer(
            "🚫 У вас закончились бесплатные примерки.\n\n"
            "💵 Стоимость одной примерки: 30 руб.\n"
            "После оплаты вам будет доступно количество примерок в соответствии с внесенной суммой.\n\n"
            "Например:\n"
            "30 руб = 1 примерка\n"
            "60 руб = 2 примерки\n"
            "90 руб = 3 примерки и т.д.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Оплатить картой", callback_data="payment_options")]  # Оставили только эту кнопку
                ]
            )
        )
        return
    # ... остальной код ...
