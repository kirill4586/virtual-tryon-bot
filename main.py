from dotenv import load_dotenv
load_dotenv()  # Загружает переменные из .env
import os
import logging
import aiohttp
import hashlib
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

# Настройка логгирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Проверка загрузки переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') or os.getenv('BOT_TOKEN')
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Не найден TELEGRAM_BOT_TOKEN в переменных окружения")

# Инициализация бота и диспетчера
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

# Конфигурация платежей
YOOMONEY_WALLET = "4100118715530282"  # Ваш номер кошелька ЮMoney
YOOMONEY_SECRET = os.getenv('YOOMONEY_SECRET')  # Секретный ключ из настроек ЮMoney
PRICE_PER_TRY = 30  # Цена за одну примерку в рублях
TRIES_PER_PAYMENT = 5  # Количество примерок за одну оплату
FREE_USER_IDS = {973853935, 6320348591}  # ID пользователей с бесплатным доступом

class BaserowAPI:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.headers = {"Authorization": f"Token {token}"}
    
    async def upsert_row(self, user_id: int, username: str, update_data: dict):
        """Обновляет или создает запись пользователя"""
        url = f"{self.base_url}/?user_field_names=true&filter__user_id__equal={user_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as resp:
                data = await resp.json()
                
                if data.get("results"):
                    row_id = data["results"][0]["id"]
                    update_url = f"{self.base_url}/{row_id}/?user_field_names=true"
                    async with session.patch(update_url, json=update_data, headers=self.headers) as update_resp:
                        return await update_resp.json()
                else:
                    new_data = {"user_id": user_id, "username": username, **update_data}
                    async with session.post(self.base_url, json=new_data, headers=self.headers) as create_resp:
                        return await create_resp.json()
    
    async def get_user_tries(self, user_id: int) -> dict:
        """Получает данные о примерках пользователя"""
        url = f"{self.base_url}/?user_field_names=true&filter__user_id__equal={user_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("results"):
                        return {
                            "free_tries": data["results"][0].get("free_tries", 1),
                            "paid_tries": data["results"][0].get("paid_tries", 0),
                            "total_payments": data["results"][0].get("total_payments", 0)
                        }
        return {"free_tries": 1, "paid_tries": 0, "total_payments": 0}
    
    async def update_tries(self, user_id: int, username: str = "", free_tries: int = None, paid_tries: int = None, payment_amount: float = None):
        """Обновляет счётчики примерок и платежей"""
        update_data = {}
        if free_tries is not None:
            update_data["free_tries"] = free_tries
        if paid_tries is not None:
            update_data["paid_tries"] = paid_tries
        if payment_amount is not None:
            update_data["total_payments"] = payment_amount
        
        await self.upsert_row(user_id, username, update_data)

# Инициализация Baserow API
baserow = BaserowAPI(
    base_url=f"https://api.baserow.io/api/database/rows/table/{os.getenv('TABLE_ID', '12345')}/",
    token=os.getenv('BASEROW_TOKEN', 'your_baserow_token')
)

async def check_payment_required(user_id: int) -> bool:
    """Проверяет, требуется ли оплата для пользователя"""
    if user_id in FREE_USER_IDS:
        return False
    
    tries = await baserow.get_user_tries(user_id)
    return tries["free_tries"] <= 0 and tries["paid_tries"] <= 0

async def send_payment_request(user_id: int, message: types.Message = None):
    """Отправляет запрос на оплату"""
    payment_url = f"https://yoomoney.ru/quickpay/confirm.xml?receiver={YOOMONEY_WALLET}&quickpay-form=small&sum={PRICE_PER_TRY}&label={user_id}&targets=Оплата%20примерки"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {PRICE_PER_TRY}₽", url=payment_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data="check_payment")]
    ])
    
    text = (
        "Спасибо, что воспользовались нашей виртуальной примерочной!\n\n"
        "Первая примерка была демонстрационной, последующие примерки стоят 30 рублей за примерку. "
        "Сумма символическая, чтобы и Вам помочь стать красивыми и окупанию проекта."
    )
    
    if message:
        await message.answer(text, reply_markup=keyboard)
    else:
        await bot.send_message(user_id, text, reply_markup=keyboard)

@dp.message(Command("start"))
async def start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    full_name = message.from_user.full_name or ""
    
    await baserow.upsert_row(user_id, username, {"full_name": full_name})
    
    if await check_payment_required(user_id):
        await send_payment_request(user_id, message)
        return
    
    tries = await baserow.get_user_tries(user_id)
    if tries["free_tries"] == 1:
        await message.answer(
            "Добро пожаловать в виртуальную примерочную!\n\n"
            "Вам доступна 1 бесплатная примерка. После этого каждая примерка будет стоить 30 рублей."
        )
    else:
        remaining_tries = tries["paid_tries"] if tries["free_tries"] <= 0 else tries["free_tries"]
        await message.answer(
            f"С возвращением! У вас осталось {remaining_tries} примерок."
        )

@dp.callback_query(F.data.startswith("model_"))
async def model_selected(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    
    if await check_payment_required(user_id):
        await send_payment_request(user_id, callback_query.message)
        await callback_query.answer()
        return
    
    tries = await baserow.get_user_tries(user_id)
    if tries["free_tries"] > 0:
        await baserow.update_tries(user_id, free_tries=tries["free_tries"]-1)
    else:
        await baserow.update_tries(user_id, paid_tries=tries["paid_tries"]-1)
    
    model_path = callback_query.data.replace("model_", "")
    await callback_query.message.answer(f"Вы выбрали модель: {model_path}")
    await callback_query.answer()

def verify_webhook(data: dict, secret: str) -> bool:
    """Проверяет подпись уведомления от ЮMoney"""
    params = [
        data.get('notification_type'),
        data.get('operation_id'),
        data.get('amount'),
        data.get('currency'),
        data.get('datetime'),
        data.get('sender'),
        data.get('codepro'),
        secret,
        data.get('label')
    ]
    hash_str = '&'.join(str(param) for param in params)
    sha1 = hashlib.sha1(hash_str.encode()).hexdigest()
    return sha1 == data.get('sha1_hash')

@app.post('/payment_webhook')
async def payment_webhook(request: Request):
    """Обработчик вебхука для подтверждения платежей"""
    try:
        data = await request.form()
        data = dict(data)
        
        if not verify_webhook(data, YOOMONEY_SECRET):
            raise HTTPException(status_code=403, detail="Invalid signature")
        
        user_id = int(data.get('label'))
        amount = float(data.get('amount'))
        
        added_tries = int(amount // PRICE_PER_TRY) * TRIES_PER_PAYMENT
        await baserow.update_tries(
            user_id=user_id,
            paid_tries=added_tries,
            payment_amount=amount
        )
        
        await bot.send_message(
            user_id,
            f"✅ Оплата получена! Вам доступно {added_tries} примерок.\n"
            "Можете продолжать использование бота."
        )
        
        return JSONResponse(content={"status": "ok"})
    except Exception as e:
        logger.error(f"Payment webhook error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@dp.callback_query(F.data == "check_payment")
async def check_payment(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    tries = await baserow.get_user_tries(user_id)
    
    if tries["paid_tries"] > 0:
        await callback_query.answer("✅ Оплата подтверждена! Можете продолжать.", show_alert=True)
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Проверить ещё раз", callback_data="check_payment")]
    ])
    
    await callback_query.message.answer(
        "Если вы уже оплатили, но доступ не открылся, пожалуйста, подождите 1-2 минуты.\n"
        "Если прошло больше времени, свяжитесь с поддержкой.",
        reply_markup=keyboard
    )
    await callback_query.answer()

async def setup_webhook():
    """Установка вебхука при запуске"""
    try:
        from pyngrok import ngrok
        http_tunnel = ngrok.connect(8000)
        webhook_url = f"{http_tunnel.public_url}/payment_webhook"
        logger.info(f"Ngrok tunnel created: {webhook_url}")
        
        # Установка вебхука
        await bot.set_webhook(webhook_url)
        logger.info("Webhook set successfully")
    except Exception as e:
        logger.error(f"Failed to setup webhook: {e}")

@app.on_event("startup")
async def on_startup():
    logger.info("Starting up...")
    await setup_webhook()

if __name__ == "__main__":
    import uvicorn
    
    # Проверка всех необходимых переменных
    required_vars = ['TELEGRAM_BOT_TOKEN', 'BASEROW_TOKEN', 'YOOMONEY_SECRET']
    for var in required_vars:
        if not os.getenv(var):
            logger.error(f"Необходимо установить переменную окружения: {var}")
            exit(1)
    
    uvicorn.run(app, host="0.0.0.0", port=8000)