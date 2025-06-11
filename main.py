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

async def process_photo(message: types.Message, user: types.User, user_dir: str):
    """Обработка и сохранение фотографии"""
    try:
        user_id = user.id
        user_dir = os.path.join(UPLOAD_DIR, str(user_id))
        os.makedirs(user_dir, exist_ok=True)

        # Получаем файл фотографии
        photo = message.photo[-1]  # Берем фото наибольшего размера
        file_id = photo.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path

        # Определяем тип фото (одежда или человек)
        existing_photos = [
            f for f in os.listdir(user_dir)
            if f.startswith("photo_") and f.endswith(tuple(SUPPORTED_EXTENSIONS))
        ]

        if not existing_photos:
            # Первое фото - одежда
            photo_type = 1
            filename = f"photo_1{os.path.splitext(file_path)[1]}"
            caption = "✅ Фото одежды получено. Теперь выберите действие:"
            
            # Добавляем кнопки после получения фото одежды
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="👤 Загрузить фото человека", callback_data="upload_person"),
                    InlineKeyboardButton(text="👫 Выбрать модель", callback_data="choose_model")
                ]
            ])
        else:
            # Второе фото - человек
            photo_type = 2
            filename = f"photo_2{os.path.splitext(file_path)[1]}"
            caption = "✅ Оба файла получены.\n🔄 Идёт примерка. Ожидайте результат!"
            keyboard = None

            # Уведомление администратору о получении всех фото
            await notify_admin(f"📸 Все фото получены от @{user.username} ({user_id})")

        # Сохраняем фото локально
        local_path = os.path.join(user_dir, filename)
        await bot.download_file(file_path, local_path)

        # Загружаем фото в Supabase
        await upload_to_supabase(local_path, user_id, "photos")

        # Получаем текущие данные пользователя, чтобы сохранить username
        user_row = await supabase_api.get_user_row(user_id)
        current_username = user_row.get('username', '') if user_row else (user.username or '')

        # Обновляем статус в базе данных, сохраняя username
        if photo_type == 1:
            await supabase_api.upsert_row(user_id, current_username, {
                "photo1_received": True,
                "photo2_received": False,
                "status": "Ожидается фото человека",
                "username": current_username  # Явно сохраняем username
            })
        else:
            await supabase_api.upsert_row(user_id, current_username, {
                "photo1_received": True,
                "photo2_received": True,
                "status": "В обработке",
                "last_try_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                "username": current_username  # Явно сохраняем username
            })

            await supabase_api.decrement_tries(user_id)

        if keyboard:
            await message.answer(caption, reply_markup=keyboard)
        else:
            await message.answer(caption)

    except Exception as e:
        logger.error(f"Error processing photo: {e}")
        await message.answer("❌ Ошибка при обработке фото. Попробуйте ещё раз.")
        raise

@dp.callback_query(F.data == "upload_person")
async def upload_person_handler(callback_query: types.CallbackQuery):
    """Обработчик кнопки загрузки фото человека"""
    try:
        await callback_query.message.answer(
            "👤 Чтобы загрузить фото человека, нажмите на Скрепку, "
            "которая находится рядом с сообщением и загрузите изображение человека для примерки."
        )
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in upload_person_handler: {e}")
        await callback_query.message.answer("⚠️ Ошибка при обработке запроса. Попробуйте позже.")
        await callback_query.answer()

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    """Обработчик фотографий"""
    user = message.from_user
    user_id = user.id
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    try:
        tries_left = await get_user_tries(user_id)
        
        if tries_left <= 0:
            await show_payment_options(user)
            return
            
        await process_photo(message, user, user_dir)
        
    except Exception as e:
        logger.error(f"Error handling photo: {e}")
        await message.answer("❌ Ошибка при сохранении файла. Попробуйте ещё раз.")

async def show_balance_info(user: types.User):
    """Показывает информацию о балансе пользователя"""
    try:
        # Получаем данные пользователя из Supabase
        user_row = await supabase_api.get_user_row(user.id)
        
        if not user_row:
            await bot.send_message(user.id, "❌ Ваши данные не найдены. Пожалуйста, начните с команды /start")
            return
            
        payment_amount = float(user_row.get(AMOUNT_FIELD, 0)) if user_row.get(AMOUNT_FIELD) else 0.0
        tries_left = int(user_row.get(TRIES_FIELD, 0)) if user_row.get(TRIES_FIELD) else 0
        status = user_row.get(STATUS_FIELD, "Неизвестно")
        free_tries_used = bool(user_row.get(FREE_TRIES_FIELD, False))
        
        message_text = (
            "💰 <b>Ваш баланс:</b>\n\n"
            f"💳 Сумма на счету: <b>{payment_amount} руб.</b>\n"
            f"🎁 Доступно примерок: <b>{tries_left}</b>\n"
            f"📊 Статус: <b>{status}</b>\n"
            f"🆓 Бесплатная проверка: <b>{'использована' if free_tries_used else 'доступна'}</b>\n\n"
            f"ℹ️ Стоимость одной примерки: <b>{PRICE_PER_TRY} руб.</b>"
        )
        
        await bot.send_message(
            user.id,
            message_text,
            parse_mode=ParseMode.HTML
        )
        
    except Exception as e:
        logger.error(f"Error showing balance info: {e}")
        await bot.send_message(
            user.id,
            "❌ Ошибка при получении информации о балансе. Попробуйте позже."
        )

async def show_payment_options(user: types.User):
    """Показывает варианты оплаты через DonationAlerts"""
    try:
        # Формируем сообщение для DonationAlerts (username и ID)
        payment_message = f"Оплата за примерки от @{user.username} (ID: {user.id})"
        encoded_message = quote(payment_message)
        
        # Создаем клавиатуру с кнопкой оплаты и проверки баланса
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить доступ",
                    url=f"https://www.donationalerts.com/r/{DONATION_ALERTS_USERNAME}?message={encoded_message}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Проверить баланс",
                    callback_data="check_balance"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Назад",
                    callback_data="back_to_menu"
                )
            ]
        ])
        
        payment_text = (
            "🚫 У вас закончились бесплатные примерки.\n\n"
            "📌 <b>Для продолжения работы необходимо оплатить услугу:</b>\n\n"
            "1. Нажмите на кнопку 'Оплатить доступ'\n"
            "2. Вас перенаправит на страницу оплаты DonationAlerts\n"
            "3. После успешной оплаты доступ будет автоматически предоставлен\n\n"
            f"⚠️ <b>Внимание!</b> В поле 'Сообщение' на странице оплаты должно быть указано:\n"
            f"<code>Оплата за примерки от @{user.username} (ID: {user.id})</code>\n\n"
            "Не изменяйте это сообщение, иначе оплата не будет засчитана!"
        )
        
        await bot.send_message(
            user.id,
            payment_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
        
        # Уведомление администратору о начале оплаты
        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    f"💸 Пользователь @{user.username} ({user.id}) начал процесс оплаты\n"
                    f"ℹ️ Сообщение для DonationAlerts: 'Оплата за примерки от @{user.username} (ID: {user.id})'"
                )
            except Exception as e:
                logger.error(f"Error sending admin payment notification: {e}")
                
    except Exception as e:
        logger.error(f"Error sending payment options: {e}")
        await bot.send_message(
            user.id,
            "❌ Ошибка при формировании ссылки оплаты. Пожалуйста, свяжитесь с администратором."
        )

@dp.callback_query(F.data == "check_balance")
async def check_balance_handler(callback_query: types.CallbackQuery):
    """Обработчик кнопки проверки баланса"""
    try:
        await show_balance_info(callback_query.from_user)
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in check_balance_handler: {e}")
        await callback_query.message.answer("⚠️ Ошибка при проверке баланса. Попробуйте позже.")
        await callback_query.answer()

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

                # Ищем result-файлы с любым поддерживаемым расширением
                result_files = [
                    f for f in os.listdir(user_dir)
                    if f.startswith("result") and f.lower().endswith(tuple(SUPPORTED_EXTENSIONS))
                ]

                # Если не найдено локально — пробуем скачать из Supabase
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

                # Если файлы найдены, обрабатываем первый подходящий
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
                        
                        # Создаем клавиатуру с кнопками
                        keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="🔄 Продолжить примерку",
                                        callback_data="continue_tryon"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        text="💳 Оплатить доступ",
                                        callback_data="show_payment_options"
                                    ),
                                    InlineKeyboardButton(
                                        text="💰 Проверить баланс",
                                        callback_data="check_balance"
                                    )
                                ]
                            ]
                        )

                        await bot.send_photo(
                            chat_id=user_id,
                            photo=photo,
                            caption="🎉 Ваша виртуальная примерка готова!",
                            reply_markup=keyboard
                        )

                        # Получаем текущие данные пользователя, чтобы сохранить username
                        user_row = await supabase_api.get_user_row(user_id)
                        current_username = user_row.get('username', '') if user_row.get('username') else ''

                        # Уведомление администратору
                        if ADMIN_CHAT_ID:
                            try:
                                await bot.send_message(
                                    ADMIN_CHAT_ID,
                                    f"✅ Пользователь @{current_username} ({user_id}) получил результат примерки"
                                )
                            except Exception as e:
                                logger.error(f"Error sending admin notification: {e}")

                        # Загружаем результат в Supabase с новым уникальным именем
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

                        # Обновляем Supabase, сохраняя username
                        try:
                            await supabase_api.upsert_row(user_id, current_username, {
                                "status": "Результат отправлен",
                                "result_sent": True,
                                "ready": True,
                                "result_url": supabase_path if 'supabase_path' in locals() else None,
                                "username": current_username  # Явно сохраняем username
                            })
                        except Exception as db_error:
                            logger.error(f"❌ Ошибка обновления Supabase: {db_error}")

                        # Полная очистка локальной папки пользователя
                        try:
                            shutil.rmtree(user_dir)
                            logger.info(f"🗑️ Папка {user_dir} полностью удалена")
                        except Exception as cleanup_error:
                            logger.error(f"❌ Ошибка удаления папки: {cleanup_error}")

                        # Удаляем все файлы пользователя из Supabase
                        try:
                            base = supabase.storage.from_(UPLOADS_BUCKET)
                            files_to_delete = []

                            # Добавляем все возможные фото пользователя
                            for ext in SUPPORTED_EXTENSIONS:
                                files_to_delete.extend([
                                    f"{user_id_str}/photos/photo_1{ext}",
                                    f"{user_id_str}/photos/photo_2{ext}",
                                    f"{user_id_str}/models/selected_model{ext}"
                                ])

                            # Добавляем result-файлы
                            files_to_delete.extend([
                                f"{user_id_str}/result{ext}" for ext in SUPPORTED_EXTENSIONS
                            ])

                            # Добавляем файлы из папки results
                            try:
                                result_files_in_supabase = base.list(f"{user_id_str}/results")
                                for f in result_files_in_supabase:
                                    if f['name'].startswith("result"):
                                        files_to_delete.append(f"{user_id_str}/results/{f['name']}")
                            except Exception as e:
                                logger.warning(f"⚠️ Не удалось получить список result-файлов: {e}")

                            # Удаляем только существующие файлы
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

@dp.callback_query(F.data == "continue_tryon")
async def continue_tryon_handler(callback_query: types.CallbackQuery):
    """Обработчик кнопки продолжения примерки"""
    try:
        await send_welcome(
            callback_query.from_user.id,
            callback_query.from_user.username,
            callback_query.from_user.full_name
        )
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in continue_tryon_handler: {e}")
        await callback_query.message.answer("⚠️ Ошибка при обработке запроса. Попробуйте позже.")
        await callback_query.answer()

@dp.callback_query(F.data == "show_payment_options")
async def show_payment_options_handler(callback_query: types.CallbackQuery):
    """Обработчик кнопки показа вариантов оплаты"""
    try:
        await show_payment_options(callback_query.from_user)
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Error in show_payment_options_handler: {e}")
        await callback_query.message.answer("⚠️ Ошибка при обработке запроса. Попробуйте позже.")
        await callback_query.answer()

async def monitor_payment_changes_task():
    """Фоновая задача для мониторинга изменений payment_amount"""
    logger.info("Starting payment amount monitoring task...")
    while True:
        try:
            # Получаем всех пользователей из базы данных
            res = supabase.table(USERS_TABLE)\
                .select("user_id, payment_amount, username")\
                .execute()
            
            current_payments = {int(user['user_id']): float(user['payment_amount']) 
                              for user in res.data if user.get('payment_amount')}
            
            # Сравниваем с предыдущими значениями
            for user_id, current_amount in current_payments.items():
                previous_amount = supabase_api.last_payment_amounts.get(user_id, 0)
                
                if current_amount != previous_amount:
                    # Рассчитываем количество примерок
                    tries_left = int(current_amount / PRICE_PER_TRY)
                    
                    # Обновляем кэш
                    supabase_api.last_payment_amounts[user_id] = current_amount
                    supabase_api.last_tries_values[user_id] = tries_left
                    
                    # Получаем данные пользователя
                    user_row = await supabase_api.get_user_row(user_id)
                    if not user_row:
                        continue
                    
                    username = user_row.get('username', '') if user_row.get('username') else ''
                    
                    # Обновляем доступ и количество попыток
                    await supabase_api.update_user_row(user_id, {
                        ACCESS_FIELD: True if current_amount > 0 else False,
                        TRIES_FIELD: tries_left,
                        STATUS_FIELD: "Оплачено" if current_amount > 0 else "Не оплачено",
                        FREE_TRIES_FIELD: True  # Помечаем, что бесплатная проверка использована
                    })
                    
                    # Отправляем уведомления
                    await supabase_api.send_payment_update_notifications(
                        user_id,
                        current_amount,
                        tries_left,
                        "Изменение баланса"
                    )
            
            await asyncio.sleep(10)  # Проверяем каждые 10 секунд
            
        except Exception as e:
            logger.error(f"Error in payment monitoring task: {e}")
            await asyncio.sleep(30)  # При ошибке ждем дольше

async def handle(request):
    """Обработчик корневого запроса"""
    return web.Response(text="Bot is running")

async def health_check(request):
    """Обработчик health check"""
    return web.Response(text="OK", status=200)

def setup_web_server():
    """Настройка веб-сервера"""
    app = web.Application()
    
    app.router.add_get('/', handle)
    app.router.add_get('/health', health_check)
    app.router.add_post(f'/{BOT_TOKEN.split(":")[1]}', webhook_handler)
    return app

async def webhook_handler(request):
    """Обработчик вебхука"""
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
        
        # Запуск веб-сервера
        await start_web_server()
        
        # Установка вебхука
        webhook_url = f"https://virtual-tryon-bot.onrender.com/{BOT_TOKEN.split(':')[1]}"
        await bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
        )
        logger.info(f"Webhook set to: {webhook_url}")
        
        # Запуск фоновой задачи проверки результатов
        asyncio.create_task(check_results())
        
        # Запуск мониторинга изменений payment_amount
        asyncio.create_task(monitor_payment_changes_task())
        
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