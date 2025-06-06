i
                "🚫 У вас закончились бесплатные примерки.\n\n"
                "Для продолжения работы оплатите услугу:\n"
                "💵 Стоимость одной примерки: 30 руб.\n"
                "После оплаты вам будет доступно количество примерок в соответствии с внесенной суммой.\n\n"
                "Например:\n"
                "30 руб = 1 примерка\n"
                "60 руб = 2 примерки\n"
                "90 руб = 3 примерки и т.д.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💳 Оплатить 30 руб (1 примерка)", 
                            url=make_donation_link(user, 30)
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="💳 Оплатить 60 руб (2 примерки)", 
                            url=make_donation_link(user, 60)
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="💳 Оплатить произвольную сумму", 
                            url=make_donation_link(user, 30, False)
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
            
        # Если попытки есть, обрабатываем фото
        await process_photo(message, user, user_dir)
        
    except Exception as e:
        logger.error(f"Error handling photo: {e}")
        await message.answer("❌ Ошибка при сохранении файла. Попробуйте ещё раз.")

async def process_photo(message: types.Message, user: types.User, user_dir: str):
    """Обрабатывает загруженное фото"""
    try:
        existing_photos = [
            f for f in os.listdir(user_dir)
            if f.startswith("photo_") and f.endswith(tuple(SUPPORTED_EXTENSIONS))
        ]
        
        photo_number = len(existing_photos) + 1
        
        if photo_number > 2:
            await message.answer("✅ Вы уже загрузили 2 файла. Ожидайте результат.")
            return
            
        # Проверяем, есть ли уже модель или первое фото
        model_selected = os.path.exists(os.path.join(user_dir, "selected_model.jpg"))
        first_photo_exists = any(f.startswith("photo_1") for f in existing_photos)
        
        # Если это второе фото и нет модели, но есть первое фото
        if photo_number == 2 and not model_selected and first_photo_exists:
            photo = message.photo[-1]
            file_ext = os.path.splitext(photo.file_id)[1] or '.jpg'
            file_name = f"photo_{photo_number}{file_ext}"
            file_path = os.path.join(user_dir, file_name)
            
            await bot.download(photo, destination=file_path)
            
            # Загружаем фото в Supabase
            await upload_to_supabase(file_path, user.id, "photos")
            
            # Уменьшаем количество попыток
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
            
        # Если это первое фото
        if photo_number == 1:
            photo = message.photo[-1]
            file_ext = os.path.splitext(photo.file_id)[1] or '.jpg'
            file_name = f"photo_{photo_number}{file_ext}"
            file_path = os.path.join(user_dir, file_name)
            
            await bot.download(photo, destination=file_path)
            
            # Загружаем фото в Supabase
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
    """Упрощенное меню оплаты с одной кнопкой"""
    user = callback_query.from_user
    await callback_query.message.edit_text(
        "Для продолжения работы с ботом необходимо оплатить услугу:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить 30 руб (1 примерка)", 
                    url=make_donation_link(user, 30)
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Оплатить 60 руб (2 примерки)", 
                    url=make_donation_link(user, 60)
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Оплатить произвольную сумму", 
                    url=make_donation_link(user, 30, False)
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
        # Проверяем токен авторизации
        auth_token = request.headers.get('Authorization')
        if auth_token != f"Bearer {DONATION_ALERTS_TOKEN}":
            logger.warning(f"Invalid auth token: {auth_token}")
            return web.Response(status=403)
        
        data = await request.json()
        logger.info(f"Donation received: {data}")

        # Проверяем, что это валидный платеж
        if data.get('status') == 'success':
            amount = int(float(data.get('amount', 0)))
            user_message = data.get('message', '')
            
            # Извлекаем Telegram username или ID из сообщения
            telegram_username = None
            telegram_id = None
            if user_message.startswith('@'):
                telegram_username = user_message[1:].strip()
            elif 'TelegramID_' in user_message:
                try:
                    telegram_id = int(user_message.replace('TelegramID_', '').strip())
                except ValueError:
                    logger.error(f"Invalid Telegram ID format in message: {user_message}")
            
            # Рассчитываем количество примерок (1 примерка = 30 руб)
            tries_added = max(1, amount // PRICE_PER_TRY)
            
            if not telegram_username and not telegram_id:
                logger.warning("No valid user identifier in donation message")
                return web.Response(status=200)
            
            # Получаем данные пользователя для обновления
            user_identifier = telegram_username or f"TelegramID_{telegram_id}"
            logger.info(f"Processing payment for {user_identifier}, amount: {amount} руб, tries to add: {tries_added}")

            # Обновляем данные в Baserow
            update_success = False
            try:
                # Формируем данные для обновления
                update_data = {
                    "tries_left": tries_added,
                    "last_payment_amount": amount,
                    "last_payment_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "payment_status": "Оплачено",
                    "status": "Активен"
                }

                # Если есть username, обновляем его тоже
                if telegram_username:
                    update_data["username"] = telegram_username

                # Ищем пользователя в Baserow
                if telegram_id:
                    filter_field = "user_id"
                    filter_value = str(telegram_id)
                else:
                    filter_field = "username"
                    filter_value = telegram_username

                # Выполняем upsert в Baserow
                result = await baserow.upsert_row(
                    user_id=telegram_id if telegram_id else 0,  # 0 если нет ID
                    username=telegram_username or "",
                    data=update_data
                )

                if result:
                    update_success = True
                    logger.info(f"Successfully updated Baserow for {user_identifier}")
                else:
                    logger.error(f"Failed to update Baserow for {user_identifier}")

            except Exception as e:
                logger.error(f"Error updating Baserow for {user_identifier}: {e}")

            # Отправляем уведомления
            try:
                # Уведомление администратору
                admin_message = (
                    f"💰 Получен платеж через DonationAlerts:\n"
                    f"• Сумма: {amount} руб\n"
                    f"• Примерок добавлено: {tries_added}\n"
                    f"• Пользователь: {user_identifier}\n"
                    f"• Статус обновления: {'Успешно' if update_success else 'Ошибка'}"
                )
                await notify_admin(admin_message)

                # Уведомление пользователю (если есть telegram_id)
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
                        await notify_admin(f"⚠️ Не удалось отправить уведомление пользователю {user_identifier}")

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

                # 1. Ищем локально result-файлы с любым поддерживаемым расширением
                result_files = [
                    f for f in os.listdir(user_dir)
                    if f.startswith("result") and f.lower().endswith(tuple(SUPPORTED_EXTENSIONS))
                ]

                # 2. Если не найдено локально — пробуем скачать из Supabase
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
                            break  # Прерываем цикл после успешной загрузки
                        except Exception as e:
                            logger.warning(f"❌ Не удалось скачать result{ext} из Supabase для {user_id_str}: {e}")
                            continue

                # 3. Если файлы найдены, обрабатываем первый подходящий
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

                        # Обновляем Baserow
                        try:
                            await baserow.upsert_row(user_id, "", {
                                "status": "Результат отправлен",
                                "result_sent": True,
                                "ready": True,
                                "result_url": supabase_path if 'supabase_path' in locals() else None
                            })
                        except Exception as db_error:
                            logger.error(f"❌ Ошибка обновления Baserow: {db_error}")

                        # Удаляем локальную папку
                        try:
                            shutil.rmtree(user_dir)
                            logger.info(f"🗑️ Папка {user_dir} удалена")
                        except Exception as cleanup_error:
                            logger.error(f"❌ Ошибка удаления папки: {cleanup_error}")

                        # Удаляем файлы пользователя из Supabase
                        try:
                            base = supabase.storage.from_(UPLOADS_BUCKET)
                            files_to_delete = []

                            # Добавляем все возможные фото пользователя
                            for ext in SUPPORTED_EXTENSIONS:
                                files_to_delete.extend([
                                    f"{user_id_str}/photos/photo_1{ext}",
                                    f"{user_id_str}/photos/photo_2{ext}"
                                ])

                            # Добавляем result-файлы из папки results
                            try:
                                result_files_in_supabase = base.list(f"{user_id_str}/results")
                                for f in result_files_in_supabase:
                                    if f['name'].startswith("result"):
                                        files_to_delete.append(f"{user_id_str}/results/{f['name']}")
                            except Exception as e:
                                logger.warning(f"⚠️ Не удалось получить список result-файлов из results/: {e}")

                            # Добавляем result-файлы из корня uploads/{user_id}/
                            try:
                                root_files = base.list(user_id_str)
                                for f in root_files:
                                    if f['name'].startswith("result") and any(f['name'].lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS):
                                        files_to_delete.append(f"{user_id_str}/{f['name']}")
                            except Exception as e:
                                logger.warning(f"⚠️ Не удалось получить список result-файлов из корня: {e}")

                            # Удаляем только существующие
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
        # Получаем обновление от Telegram
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
    await bot.delete_webhook()  # Удаляем вебхук при завершении
    logger.info("Webhook removed")

async def main():
    try:
        logger.info("Starting bot...")
        
        # Запуск веб-сервера
        app = setup_web_server()
        runner = web.AppRunner(app)
        await runner.setup()
        
        # Устанавливаем вебхук
        webhook_url = f"https://virtual-tryon-bot.onrender.com/{BOT_TOKEN.split(':')[1]}"
        await bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
        )
        logger.info(f"Webhook set to: {webhook_url}")
        
        # Запускаем веб-сервер
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logger.info(f"Web server started on port {PORT}")
        
        # Запускаем фоновую задачу проверки результатов
        asyncio.create_task(check_results())
        
        # Бесконечный цикл (чтобы бот не завершался)
        while True:
            await asyncio.sleep(3600)  # Просто ждём, пока сервер работает
            
    except Exception as e:
        logger.error(f"Error in main: {e}")
        raise

if __name__ == "__main__":
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Запуск main() с обработкой завершения
        loop.run_until_complete(main())

    except KeyboardInterrupt:
        logger.info("Bot stopped by keyboard interrupt")

    except Exception as e:
        logger.critical(f"Fatal error: {e}")

    finally:
        # Всегда вызываем on_shutdown() перед выходом
        loop.run_until_complete(on_shutdown())
        loop.close()
        logger.info("Bot successfully shut down")