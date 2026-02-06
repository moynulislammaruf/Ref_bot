import os
import sqlite3
import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode

# লগিং সেটআপ (রেলওয়ে ড্যাশবোর্ডে স্ট্যাটাস দেখার জন্য)
logging.basicConfig(level=logging.INFO)

# এনভায়রনমেন্ট ভেরিয়েবল
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNELS = ["@cryptomininginformer", "@Click_To_Earn_By_Nobab_Channel"]

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- ডাটাবেস ফাংশন ---
def db_init():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                   (id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0, 
                   referred_by INTEGER, is_verified INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

db_init()

# --- মেম্বারশিপ চেক ---
async def is_subscribed(user_id):
    for channel in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ["left", "kicked"]:
                return False
        except:
            return False
    return True

# --- মেনু বাটন ---
def main_menu():
    buttons = [
        [InlineKeyboardButton(text="✅ Verify Join", callback_data="verify")],
        [InlineKeyboardButton(text="💰 Balance", callback_data="bal"), InlineKeyboardButton(text="🔗 Refer", callback_data="ref")],
        [InlineKeyboardButton(text="💳 Withdraw", callback_data="withdraw")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- স্টার্ট হ্যান্ডলার ---
@dp.message(CommandStart())
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split()
    referrer = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    if not cursor.fetchone():
        if referrer == user_id: referrer = None
        cursor.execute("INSERT INTO users (id, referred_by) VALUES (?, ?)", (user_id, referrer))
        conn.commit()
    conn.close()

    welcome_msg = (
        f"<b>Welcome to NOBAB MASTER BOT!</b>\n\n"
        f"টাকা আয় করতে নিচের ২ টি চ্যানেলে অবশ্যই জয়েন করুন:\n"
        f"1️⃣ {CHANNELS[0]}\n"
        f"2️⃣ {CHANNELS[1]}\n\n"
        f"জয়েন শেষ হলে <b>Verify Join</b> বাটনে ক্লিক করুন।"
    )
    await message.answer(welcome_msg, reply_markup=main_menu(), parse_mode=ParseMode.HTML)

# --- ভেরিফাই বাটন লজিক ---
@dp.callback_query(F.data == "verify")
async def verify_logic(call: types.CallbackQuery):
    user_id = call.from_user.id
    if await is_subscribed(user_id):
        conn = sqlite3.connect("bot_data.db")
        cursor = conn.cursor()
        cursor.execute("SELECT is_verified, referred_by FROM users WHERE id = ?", (user_id,))
        user_info = cursor.fetchone()

        if user_info and user_info[0] == 0:
            cursor.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (user_id,))
            if user_info[1]:
                cursor.execute("UPDATE users SET balance = balance + 0.5 WHERE id = ?", (user_info[1],))
                try:
                    await bot.send_message(user_info[1], "🎊 অভিনন্দন! আপনার রেফারে একজন নতুন সদস্য যুক্ত হয়েছে। আপনি <b>0.5 USDT</b> পেয়েছেন।", parse_mode=ParseMode.HTML)
                except: pass
            conn.commit()
            await call.answer("✅ ভেরিফিকেশন সফল হয়েছে!", show_alert=True)
        else:
            await call.answer("আপনি ইতিমধ্যে ভেরিফাইড।")
        conn.close()
    else:
        await call.answer("❌ আপনি সব চ্যানেলে জয়েন করেননি!", show_alert=True)

# --- ব্যালেন্স চেক ---
@dp.callback_query(F.data == "bal")
async def balance_logic(call: types.CallbackQuery):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE id = ?", (call.from_user.id,))
    res = cursor.fetchone()
    conn.close()
    balance = res[0] if res else 0.0
    await call.message.answer(f"💵 আপনার বর্তমান ব্যালেন্স: <b>{balance} USDT</b>", parse_mode=ParseMode.HTML)

# --- রেফার লিংক ---
@dp.callback_query(F.data == "ref")
async def refer_logic(call: types.CallbackQuery):
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={call.from_user.id}"
    text = (
        f"🔗 <b>আপনার রেফারেল লিঙ্ক:</b>\n"
        f"<code>{link}</code>\n\n"
        f"প্রতিটি ভেরিফাইড রেফারে আপনি পাবেন <b>0.5 USDT</b>। শেয়ার করুন এবং আয় করুন!"
    )
    await call.message.answer(text, parse_mode=ParseMode.HTML)

# --- এডমিন প্যানেল কমান্ডস ---
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_panel(message: types.Message):
    await message.answer("🛠 <b>এডমিন কন্ট্রোল প্যানেল</b>\n\n"
                         "/stats - মোট ইউজার দেখুন\n"
                         "/broadcast - সবাইকে মেসেজ পাঠান", parse_mode=ParseMode.HTML)

@dp.message(Command("stats"), F.from_user.id == ADMIN_ID)
async def stats(message: types.Message):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(id) FROM users")
    total = cursor.fetchone()[0]
    conn.close()
    await message.answer(f"📊 মোট ইউজার সংখ্যা: <b>{total}</b>", parse_mode=ParseMode.HTML)

@dp.message(Command("broadcast"), F.from_user.id == ADMIN_ID)
async def broadcast(message: types.Message):
    text = message.text.replace("/broadcast ", "")
    if not text or text == "/broadcast":
        return await message.answer("মেসেজ লিখুন। উদাহরণ: <code>/broadcast নতুন আপডেট আসছে!</code>", parse_mode=ParseMode.HTML)
    
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users")
    users = cursor.fetchall()
    conn.close()
    
    success = 0
    for user in users:
        try:
            await bot.send_message(user[0], text)
            success += 1
            await asyncio.sleep(0.05)
        except: continue
    await message.answer(f"✅ ব্রডকাস্ট সফল! <b>{success}</b> জনকে পাঠানো হয়েছে।", parse_mode=ParseMode.HTML)

# --- মেইন ফাংশন ---
async def main():
    # কনফ্লিক্ট এড়াতে পুরোনো সেশন ক্লিয়ার করা
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
