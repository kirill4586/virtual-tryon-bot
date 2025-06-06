async def main():
    try:
        await bot.delete_webhook()
        logger.info("✅ Вебхук Telegram отключен (режим polling)")
        
        # Создаем веб-приложение для обработки донатов
        app = web.Application()
        app.router.add_post('/donation_callback', handle_donation_webhook)
        
        # Настраиваем и запускаем веб-сервер
        runner = web.AppRunner(app)
        await runner.setup()
        
        # Получаем порт из переменной окружения (Render сам его задает)
        port = int(os.getenv("PORT", 8081))  # 8081 — запасной вариант
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        
        logger.info(f"🚀 Сервер запущен на порту {port}")
        logger.info("🔄 Вебхук DonationAlerts работает (/donation_callback)")
        
        # Запускаем бота в режиме polling
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
        await asyncio.sleep(5)  # Пауза перед перезапуском
        await main()  # Перезапуск
