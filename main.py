res = supabase.storage.from_(MODELS_BUCKET).download(f"{category}/{model_name}")
with open(model_local_path, 'wb') as f:
    f.write(res)

# Показываем превью модели
model_preview = FSInputFile(model_local_path)
await bot.send_photo(
    chat_id=user_id,
    photo=model_preview,
    caption="📸 Вы выбрали эту модель для примерки."
)

# Загружаем файл модели как photo_2 в Supabase
await upload_to_supabase(model_local_path, user_id, "photos")

await callback_query.message.answer("✅ Модель выбрана. 🔄 Идёт примерка. Ожидайте результат!")
