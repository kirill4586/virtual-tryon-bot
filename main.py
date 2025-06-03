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

                # 1. Ищем локально result-файлы
                result_files = [
                    f for f in os.listdir(user_dir)
                    if f.startswith("result") and f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
                ]

                # 2. Если не найдено — пробуем скачать из Supabase uploads/<user_id>/result.jpg
                if not result_files:
                    try:
                        result_supabase_path = f"{user_id_str}/result.jpg"
                        result_file_local = os.path.join(user_dir, "result.jpg")
                        os.makedirs(user_dir, exist_ok=True)

                        res = supabase.storage.from_(UPLOADS_BUCKET).download(result_supabase_path)
                        with open(result_file_local, 'wb') as f:
                            f.write(res)

                        logger.info(f"✅ Скачан result.jpg из Supabase для пользователя {user_id_str}")
                        result_files = ["result.jpg"]
                    except Exception as e:
                        logger.warning(f"❌ Не удалось скачать result.jpg из Supabase для {user_id_str}: {e}")
                        continue

                # 3. Отправляем файл
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

                    # Удаляем ВСЕ файлы пользователя из Supabase (включая photos/, result.jpg и results/)
                    try:
                        all_files = supabase.storage.from_(UPLOADS_BUCKET).list(user_id_str, {"recursive": True})
                        file_paths = [f"{user_id_str}/{file['name']}" for file in all_files]

                        if file_paths:
                            supabase.storage.from_(UPLOADS_BUCKET).remove(file_paths)
                            logger.info(f"🗑️ Все файлы пользователя {user_id_str} удалены из Supabase: {len(file_paths)} шт.")
                        else:
                            logger.info(f"ℹ️ В Supabase не найдено файлов для удаления у пользователя {user_id_str}")
                    except Exception as e:
                        logger.error(f"❌ Ошибка удаления файлов пользователя {user_id_str} из Supabase: {e}")

                except Exception as e:
                    logger.error(f"❌ Ошибка при отправке результата пользователю {user_id_str}: {e}")
                    continue

            await asyncio.sleep(30)

        except Exception as e:
            logger.error(f"❌ Критическая ошибка в check_results(): {e}")
            await asyncio.sleep(30)
