async def process_sbp_payment(callback_query: types.CallbackQuery, amount: int):
    label = f"tryon_{callback_query.from_user.id}"
    payment_link = await PaymentManager.create_sbp_link(amount=amount, label=label)
    
    await callback_query.message.edit_text(
        f"📱 <b>Оплата {amount} руб. через СБП</b>\n\n"
        "1️⃣ Нажмите <b>«Перейти к оплате»</b>\n"
        "2️⃣ Введите <b>номер телефона, привязанный к моей карте</b>\n"
        "3️⃣ Подтвердите платеж в своем банке\n\n"
        "⚠️ <i>Платеж поступит мне автоматически.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Перейти к оплате", url=payment_link)],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_payment_{label}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="sbp_payment_menu")]
        ])
    )
    await callback_query.answer()
