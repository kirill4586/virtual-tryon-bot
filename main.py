# Добавляем обработчик команды /pay
@dp.message(Command("pay"))
async def handle_pay_command(message: types.Message):
    try:
        amount = int(message.text.split()[1]) if len(message.text.split()) > 1 else None
        
        if amount is None:
            await message.answer(
                "💵 Введите сумму оплаты (минимум 30 руб):\n\n"
                "Пример: <code>50</code> - для оплаты 50 рублей (1 примерка = 30 руб)",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_payment")]
                ])
            )
            return
            
        if amount < MIN_PAYMENT_AMOUNT:
            await message.answer(f"❌ Минимальная сумма оплаты - {MIN_PAYMENT_AMOUNT} руб.")
            return
            
        await process_payment(message.from_user, amount)
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число (сумму в рублях)")
    except Exception as e:
        logger.error(f"Error in handle_pay_command: {e}")
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")

# Обработчик текстовых сообщений для ввода суммы
@dp.message(F.text & ~F.command)
async def handle_payment_amount(message: types.Message):
    try:
        if message.reply_to_message and "Введите сумму оплаты" in message.reply_to_message.text:
            try:
                amount = int(message.text)
                if amount < MIN_PAYMENT_AMOUNT:
                    await message.answer(f"❌ Минимальная сумма - {MIN_PAYMENT_AMOUNT} руб.")
                    return
                    
                await process_payment(message.from_user, amount)
            except ValueError:
                await message.answer("❌ Пожалуйста, введите число (сумму в рублях)")
    except Exception as e:
        logger.error(f"Error in handle_payment_amount: {e}")

# Обработчик кнопки "Оплатить произвольную сумму"
@dp.callback_query(F.data == "custom_payment")
async def handle_custom_payment(callback: types.CallbackQuery):
    try:
        await callback.message.answer(
            "💵 Введите сумму оплаты (минимум 30 руб):\n\n"
            "Пример: <code>50</code> - для оплаты 50 рублей (1 примерка = 30 руб)",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_payment")]
            ])
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in handle_custom_payment: {e}")
        await callback.answer("❌ Произошла ошибка. Попробуйте позже.", show_alert=True)

# Обработчик отмены оплаты
@dp.callback_query(F.data == "cancel_payment")
async def handle_cancel_payment(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text("❌ Оплата отменена")
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in handle_cancel_payment: {e}")
        await callback.answer("❌ Не удалось отменить оплату", show_alert=True)

# Обновленный обработчик команды /balance
@dp.message(Command("balance"))
async def handle_balance(message: types.Message):
    tries_left = await get_user_tries(message.from_user.id)
    await message.answer(
        f"🔄 У вас осталось {tries_left} примерок\n\n"
        "Чтобы пополнить баланс, нажмите кнопку ниже:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=CUSTOM_PAYMENT_BTN_TEXT, callback_data="custom_payment")],
            [InlineKeyboardButton(text="💳 Оплатить 30 руб (1 примерка)", callback_data="pay_30")],
            [InlineKeyboardButton(text="💳 Оплатить 90 руб (3 примерки)", callback_data="pay_90")]
        ])
    )

# Обработчики быстрых платежей
@dp.callback_query(F.data.startswith("pay_"))
async def handle_quick_payment(callback: types.CallbackQuery):
    try:
        amount = int(callback.data.split("_")[1])
        await process_payment(callback.from_user, amount)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in handle_quick_payment: {e}")
        await callback.answer("❌ Произошла ошибка. Попробуйте позже.", show_alert=True)

# Общая логика обработки платежа
async def process_payment(user: types.User, amount: int):
    try:
        payment_label = f"tryon_{user.id}_{int(time.time())}"
        payment_link = await PaymentManager.create_payment_link(amount, payment_label)
        
        tries = amount // PRICE_PER_TRY
        text = (
            f"💳 Оплатите <b>{amount} руб.</b> и получите <b>{tries} примерок</b>\n\n"
            f"👉 <a href='{payment_link}'>Ссылка для оплаты</a>\n\n"
            "После оплаты нажмите кнопку ниже:"
        )
        
        await bot.send_message(
            user.id,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_payment_{payment_label}")],
                [InlineKeyboardButton(text="🔄 Проверить ещё раз", callback_data=f"recheck_{payment_label}")]
            ])
        )
        
    except Exception as e:
        logger.error(f"Error in process_payment: {e}")
        await bot.send_message(user.id, "❌ Произошла ошибка при создании платежа. Попробуйте позже.")

# Обработчик повторной проверки платежа
@dp.callback_query(F.data.startswith("recheck_"))
async def handle_recheck_payment(callback: types.CallbackQuery):
    try:
        payment_label = callback.data.replace("recheck_", "")
        is_paid = await PaymentManager.check_payment(payment_label)
        
        if is_paid:
            amount = int(payment_label.split("_")[2])
            tries = amount // PRICE_PER_TRY
            user_id = callback.from_user.id
            
            current_tries = await get_user_tries(user_id)
            new_total = current_tries + tries
            await update_user_tries(user_id, new_total)
            
            await callback.message.edit_text(
                f"✅ Оплата {amount} руб. подтверждена!\n"
                f"🎁 Зачислено: <b>{tries} примерок</b>\n"
                f"Всего доступно: <b>{new_total}</b>"
            )
            await notify_admin(f"💰 @{callback.from_user.username} ({user_id}) оплатил {amount} руб.")
        else:
            await callback.answer("❌ Платёж пока не найден. Попробуйте позже.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in handle_recheck_payment: {e}")
        await callback.answer("❌ Ошибка при проверке платежа", show_alert=True)

# Добавляем команду помощи по оплате
@dp.message(Command("pay_help"))
async def pay_help(message: types.Message):
    await message.answer(
        "💡 Как оплатить:\n"
        "1. Введите <code>/pay 150</code> (число - сумма в рублях)\n"
        "2. Перейдите по ссылке и оплатите\n"
        "3. Нажмите «Я оплатил»\n\n"
        "🎁 Примеры:\n"
        "• 30 руб = 1 примерка\n"
        "• 90 руб = 3 примерки\n"
        "• 150 руб = 5 примерок\n"
        "• 300 руб = 10 примерок"
    )

# Далее идут существующие функции check_results() и main() без изменений
