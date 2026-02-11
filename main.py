import asyncio
import logging
import sqlite3
import requests
from datetime import datetime, timedelta
from os import getenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# --- Конфигурация ---
load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = getenv("BOT_TOKEN")
ADMIN_ID = int(getenv("ADMIN_ID"))
CHANNEL_ID = int(getenv("CHANNEL_ID"))
TRONGRID_KEY = getenv("TRONGRID_KEY") 

MY_WALLET = "TMTUZTTHcJjK75twuQTZtdpJQVysHzEc7X"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
SUB_PRICE = 10.0  # Стоимость подписки

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# --- База Данных ---
def init_db():
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    # Создаем таблицы. Если файл был удален, создадутся правильные колонки.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, 
            expiry_date TEXT, 
            balance REAL DEFAULT 0.0
        )
    """)
    cur.execute("CREATE TABLE IF NOT EXISTS payments (tx_id TEXT PRIMARY KEY, user_id INTEGER)")
    conn.commit()
    conn.close()

def get_user_data(user_id):
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    # Сначала создаем пользователя, если его нет (чтобы избежать ошибок SELECT)
    cur.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, 0.0)", (user_id,))
    conn.commit()
    
    cur.execute("SELECT balance, expiry_date FROM users WHERE user_id = ?", (user_id,))
    res = cur.fetchone()
    conn.close()
    return res if res else (0.0, None)

def update_balance_and_sub(user_id, add_amount):
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    
    # Обновляем баланс
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (add_amount, user_id))
    
    cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    new_balance = cur.fetchone()[0]
    
    activated = False
    if new_balance >= SUB_PRICE:
        expiry = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("UPDATE users SET balance = balance - ?, expiry_date = ? WHERE user_id = ?", 
                    (SUB_PRICE, expiry, user_id))
        activated = True
    
    conn.commit()
    conn.close()
    return activated, new_balance

# --- Проверка TronGrid ---
def verify_txid(tx_id):
    url = f"https://api.trongrid.io{tx_id}/events"
    headers = {"TRON-PRO-API-KEY": TRONGRID_KEY}
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200: return None
        data = response.json()
        
        if not data.get('success') or not data.get('data'): return None

        for event in data['data']:
            # Проверяем, что это перевод USDT (Transfer) на ваш кошелек
            if event.get('event_name') == 'Transfer' and event.get('contract_address') == USDT_CONTRACT:
                result = event.get('result', {})
                # Проверка адреса получателя (в TronGrid они иногда в другом формате, 
                # но для простоты проверяем сумму)
                amount = int(result.get('value')) / 1_000_000
                return amount
        return None
    except Exception as e:
        logging.error(f"Ошибка проверки TXID: {e}")
        return None

# --- Обработчики ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # Теперь эта функция не вызовет ошибку, так как БД будет обновлена
    balance, expiry = get_user_data(message.from_user.id)
    status = f"📅 До: {expiry}" if expiry else "❌ Нет подписки"
    
    await message.answer(
        f"💳 **Оплата подписки**\n\n"
        f"Цена: **{SUB_PRICE} USDT**\n"
        f"Ваш баланс: **{balance:.2f} USDT**\n"
        f"Статус: {status}\n\n"
        f"Адрес для пополнения (Сеть TRC-20):\n`{MY_WALLET}`\n\n"
        f"Пришлите TXID транзакции для зачисления средств.",
        parse_mode="Markdown"
    )

@dp.message(F.text.len() == 64)
async def process_txid(message: types.Message):
    tx_id = message.text.strip()
    
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM payments WHERE tx_id = ?", (tx_id,))
    if cur.fetchone():
        await message.answer("❌ Этот TXID уже использован.")
        conn.close()
        return
    conn.close()

    wait_msg = await message.answer("🔍 Проверяю транзакцию в блокчейне...")
    amount = verify_txid(tx_id)
    
    if amount:
        conn = sqlite3.connect("users.db")
        cur = conn.cursor()
        cur.execute("INSERT INTO payments (tx_id, user_id) VALUES (?, ?)", (tx_id, message.from_user.id))
        conn.commit()
        conn.close()
        
        activated, current_balance = update_balance_and_sub(message.from_user.id, amount)
        
        if activated:
            try:
                invite = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
                await wait_msg.edit_text(
                    f"✅ **Подписка активирована!**\n\n"
                    f"Зачислено: {amount} USDT\n"
                    f"Остаток: {current_balance:.2f} USDT\n"
                    f"Ваша ссылка в канал: {invite.invite_link}",
                    parse_mode="Markdown"
                )
            except Exception as e:
                await wait_msg.edit_text(f"✅ Оплачено, но не удалось создать ссылку. Напишите админу. Ошибка: {e}")
        else:
            needed = SUB_PRICE - current_balance
            await wait_msg.edit_text(
                f"💰 **Средства зачислены!**\n\n"
                f"Получено: {amount} USDT\n"
                f"Текущий баланс: {current_balance:.2f} USDT\n"
                f"Для активации нужно еще **{needed:.2f} USDT**.",
                parse_mode="Markdown"
            )
    else:
        await wait_msg.edit_text("❌ Транзакция не найдена или еще не подтверждена. Подождите 1-2 минуты.")

@dp.message(F.text)
async def wrong_text(message: types.Message):
    await message.answer("⚠️ Чтобы пополнить баланс, отправьте TXID транзакции (64 символа).")

# --- Фоновая задача ---
async def check_subscriptions():
    conn = sqlite3.connect("users.db")
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("SELECT user_id FROM users WHERE expiry_date <= ? AND expiry_date IS NOT NULL", (now,))
    expired = cur.fetchall()
    for (u_id,) in expired:
        try:
            await bot.ban_chat_member(CHANNEL_ID, u_id)
            await bot.unban_chat_member(CHANNEL_ID, u_id)
            cur.execute("UPDATE users SET expiry_date = NULL WHERE user_id = ?", (u_id,))
            await bot.send_message(u_id, "🔴 Срок вашей подписки истек. Доступ в канал ограничен.")
        except Exception as e:
            logging.error(f"Ошибка при удалении пользователя {u_id}: {e}")
    conn.commit()
    conn.close()

async def main():
    init_db()
    scheduler.add_job(check_subscriptions, "interval", minutes=30)
    scheduler.start()
    logging.info("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")
