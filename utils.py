import os
import requests
from supabase import create_client, Client
from yookassa import Configuration, Payment

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Baserow
BASEROW_TOKEN = os.getenv("BASEROW_TOKEN")
BASEROW_BASE_ID = os.getenv("BASEROW_BASE_ID")
BASEROW_TABLE_ID = os.getenv("BASEROW_TABLE_ID")

# Админ ID и Telegram Token
ADMIN_ID = os.getenv("ADMIN_ID")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# YooMoney
YOOMONEY_SHOP_ID = os.getenv("YOOMONEY_SHOP_ID")
YOOMONEY_SECRET = os.getenv("YOOMONEY_SECRET")
Configuration.account_id = YOOMONEY_SHOP_ID
Configuration.secret_key = YOOMONEY_SECRET


def notify_admin(message: str):
    """Отправка уведомления администратору"""
    if ADMIN_ID and BOT_TOKEN:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": ADMIN_ID, "text": message}
        )


def get_user_paid_tries(user_id: int) -> int:
    """Получить количество доступных платных примерок у пользователя"""
    try:
        data = supabase.table("users").select("paid_tries").eq("user_id", user_id).execute()
        if data.data:
            return data.data[0]["paid_tries"]
    except Exception:
        pass
    return 0


def reduce_user_paid_tries(user_id: int):
    """Уменьшить количество платных примерок у пользователя на 1"""
    try:
        tries = get_user_paid_tries(user_id)
        if tries > 0:
            supabase.table("users").update({"paid_tries": tries - 1}).eq("user_id", user_id).execute()
    except Exception:
        pass


def create_payment_link(user_id: int, amount: int = 30) -> str:
    """Создать ссылку для оплаты с комментарием user_id"""
    return (
        f"https://yoomoney.ru/quickpay/shop-widget?"
        f"writer=seller"
        f"&targets=Оплата+примерки+для+user_{user_id}"
        f"&default-sum={amount}"
        f"&button-text=11"
        f"&payment-type-choice=on"
        f"&label=user_{user_id}"
        f"&successURL=https://t.me/your_bot_username"
    )
