import asyncio
import random
import sqlite3
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================= CONFIG SECTION =================

BOT_TOKEN = "8508138633:AAE6NRVSAXXrkzFCA8DLRJb389xroV2HqCA"
ADMIN_IDS = [5988572342]  # তোমার Telegram ID বসাও

TOKEN_NAME = "STAR"
REFERRAL_REWARD = 1.0
MIN_WITHDRAW = 10.0
WITHDRAW_TAX_PERCENT = 5

MANDATORY_CHANNELS = [
    "@cryptomininginformer",
    "@Click_To_Earn_By_Nobab_Channel"
]

# ==================================================

bot = Bot(8508138633:AAE6NRVSAXXrkzFCA8DLRJb389xroV2HqCA, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ================= DATABASE =================

db = sqlite3.connect("bot.db")
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    ref_by INTEGER,
    balance REAL DEFAULT 0,
    captcha_ok INTEGER DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS referrals(
    referred INTEGER UNIQUE,
    referrer INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS withdrawals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    tax REAL,
    net REAL,
    status TEXT
)
""")

db.commit()

# ================= KEYBOARDS =================

def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Balance", callback_data="balance")
    kb.button(text="📤 Withdraw", callback_data="withdraw")
    kb.button(text="👥 Refer Link", callback_data="refer")
    kb.adjust(2)
    return kb.as_markup()

def verify_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Verify Join", callback_data="verify")
    return kb.as_markup()

# ================= UTILITIES =================

async def check_channels(user_id):
    for ch in MANDATORY_CHANNELS:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except:
            return False
    return True

def generate_captcha():
    a = random.randint(10, 99)
    b = random.randint(10, 99)
    return f"{a} + {b}", a + b

# ================= START =================

@router.message(Command("start"))
async def start(msg: Message):
    args = msg.text.split()
    ref = int(args[1]) if len(args) > 1 else None

    cursor.execute("SELECT user_id FROM users WHERE user_id=?", (msg.from_user.id,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (user_id, ref_by) VALUES (?,?)",
            (msg.from_user.id, ref)
        )
        db.commit()

    await msg.answer(
        f"""👋 <b>Welcome!</b>

🎁 প্রতি রেফারে আয় করো <b>{REFERRAL_REWARD} {TOKEN_NAME}</b>

📌 কাজের ধাপ:
1️⃣ সব চ্যানেল Join করো  
2️⃣ Verify দাও  
3️⃣ CAPTCHA Solve করো  

⬇️ শুরু করতে নিচের বাটন চাপো""",
        reply_markup=verify_keyboard()
    )

# ================= VERIFY =================

@router.callback_query(lambda c: c.data == "verify")
async def verify(call: CallbackQuery):
    if not await check_channels(call.from_user.id):
        await call.answer("❌ সব চ্যানেল Join করো", show_alert=True)
        return

    q, ans = generate_captcha()
    cursor.execute(
        "UPDATE users SET captcha_ok=? WHERE user_id=?",
        (ans, call.from_user.id)
    )
    db.commit()

    await call.message.answer(f"🧩 CAPTCHA Solve করো:\n<b>{q}</b>")
    await call.answer()

# ================= CAPTCHA ANSWER =================

@router.message()
async def captcha_answer(msg: Message):
    cursor.execute("SELECT captcha_ok, ref_by FROM users WHERE user_id=?", (msg.from_user.id,))
    row = cursor.fetchone()
    if not row:
        return

    correct, ref_by = row
    if correct == 0:
        return

    if msg.text.isdigit() and int(msg.text) == correct:
        cursor.execute("UPDATE users SET captcha_ok=0 WHERE user_id=?", (msg.from_user.id,))
        db.commit()

        if ref_by:
            try:
                cursor.execute(
                    "INSERT INTO referrals (referred, referrer) VALUES (?,?)",
                    (msg.from_user.id, ref_by)
                )
                cursor.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id=?",
                    (REFERRAL_REWARD, ref_by)
                )
                db.commit()
            except:
                pass

        await msg.answer("✅ Verification Complete!", reply_markup=main_menu())
    else:
        await msg.answer("❌ ভুল উত্তর")

# ================= BALANCE =================

@router.callback_query(lambda c: c.data == "balance")
async def balance(call: CallbackQuery):
    cursor.execute("SELECT balance FROM users WHERE user_id=?", (call.from_user.id,))
    bal = cursor.fetchone()[0]

    await call.message.answer(
        f"💰 <b>Your Balance</b>\n\n{bal} {TOKEN_NAME}"
    )
    await call.answer()

# ================= REFER =================

@router.callback_query(lambda c: c.data == "refer")
async def refer(call: CallbackQuery):
    link = f"https://t.me/{(await bot.me()).username}?start={call.from_user.id}"
    await call.message.answer(f"🔗 <b>Your Link</b>\n<code>{link}</code>")
    await call.answer()

# ================= WITHDRAW =================

@router.callback_query(lambda c: c.data == "withdraw")
async def withdraw(call: CallbackQuery):
    cursor.execute("SELECT balance FROM users WHERE user_id=?", (call.from_user.id,))
    bal = cursor.fetchone()[0]

    if bal < MIN_WITHDRAW:
        await call.answer("❌ Minimum withdraw হয়নি", show_alert=True)
        return

    tax = bal * WITHDRAW_TAX_PERCENT / 100
    net = bal - tax

    cursor.execute(
        "INSERT INTO withdrawals (user_id, amount, tax, net, status) VALUES (?,?,?,?,?)",
        (call.from_user.id, bal, tax, net, "PENDING")
    )
    cursor.execute("UPDATE users SET balance=0 WHERE user_id=?", (call.from_user.id,))
    db.commit()

    await call.message.answer(
        f"""💸 <b>Withdraw Requested</b>

Amount: {bal}
Tax: {tax}
Net: {net}

⏳ Payment Processing..."""
    )
    await call.answer()

# ================= ADMIN =================

@router.message(Command("admin"))
async def admin(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return
    cursor.execute("SELECT COUNT(*) FROM users")
    users = cursor.fetchone()[0]
    await msg.answer(f"👑 Admin Panel\n\n👥 Total Users: {users}")

# ================= RUN =================

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())