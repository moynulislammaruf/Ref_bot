import os
import asyncio
import random
import sqlite3
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

# এনভায়রনমেন্ট ভেরিয়েবল লোড করা
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
XR_API_KEY = os.getenv("XR_API_KEY")

# সেটিংস (এগুলো অ্যাডমিন প্যানেল থেকেও পরিবর্তনযোগ্য করা যাবে)
REWARD_AMOUNT = 0.5  # প্রতি রেফার
MIN_WITHDRAW = 5.0
CHANNELS = ["@ExampleChannel"] # আপনার চ্যানেলের ইউজারনেম

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- ডাটাবেস সেটআপ ---
def db_init():
    conn = sqlite3.connect("bot_db.sqlite")
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                   (id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0, 
                   referred_by INTEGER, is_verified INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

db_init()

# --- হেল্পার ফাংশন ---
async def is_subscribed(user_id):
    for channel in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ["left", "kicked"]:
                return False
        except:
            return False
    return True

# --- হ্যান্ডলারস ---
@dp.message(CommandStart())
async def start(message: types.Message):
    user_id = message.from_id
    args = message.text.split()
    referrer = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    
    conn = sqlite3.connect("bot_db.sqlite")
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    
    if not user:
        # নতুন ইউজার হলে ডাটাবেসে সেভ করা
        cursor.execute("INSERT INTO users (id, referred_by) VALUES (?, ?)", (user_id, referrer))
        conn.commit()
    
    conn.close()
    
    # মেইন মেনু UI
    kb = [
        [InlineKeyboardButton(text="✅ জয়ন চেক করুন", callback_data="check_sub")],
        [InlineKeyboardButton(text="💰 ব্যালেন্স", callback_data="balance"), InlineKeyboardButton(text="🔗 রেফার", callback_data="refer")],
        [InlineKeyboardButton(text="💳 উইথড্র", callback_data="withdraw")]
    ]
    reply_markup = InlineKeyboardMarkup(inline_keyboard=kb)
    
    await message.answer(f"👋 আমাদের **Refer To Earn** বোটে স্বাগতম!\n\n"
                         f"টাকা আয় করতে নিচের চ্যানেলগুলোতে জয়েন করুন:\n{', '.join(CHANNELS)}",
                         reply_markup=reply_markup, parse_mode="Markdown")

@dp.callback_query(F.data == "check_sub")
async def verify_subscription(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if await is_subscribed(user_id):
        conn = sqlite3.connect("bot_db.sqlite")
        cursor = conn.cursor()
        cursor.execute("SELECT is_verified, referred_by FROM users WHERE id = ?", (user_id,))
        res = cursor.fetchone()
        
        if res and res[0] == 0: # যদি আগে ভেরিফাইড না থাকে
            # ক্যাপচা সিস্টেম (সহজ ম্যাথ)
            num1, num2 = random.randint(1, 9), random.randint(1, 9)
            # এখানে সেশন বা ডিকশনারিতে উত্তর সেভ করে চেক করা যাবে।
            # ডেমো হিসেবে সরাসরি ভেরিফাইড করে দিচ্ছি
            cursor.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (user_id,))
            
            # রেফারারকে টাকা দেওয়া
            if res[1]:
                cursor.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (REWARD_AMOUNT, res[1]))
                try:
                    await bot.send_message(res[1], f"🎊 আপনার লিঙ্কে একজন জয়েন করেছে! আপনি {REWARD_AMOUNT} USDT পেয়েছেন।")
                except: pass
            
            conn.commit()
            await callback.answer("✅ ভেরিফিকেশন সফল!", show_alert=True)
        else:
            await callback.answer("আপনি ইতিমধ্যে ভেরিফাইড!")
        conn.close()
    else:
        await callback.answer("❌ আপনি সব চ্যানেলে জয়েন করেননি!", show_alert=True)

@dp.callback_query(F.data == "balance")
async def show_balance(callback: types.CallbackQuery):
    conn = sqlite3.connect("bot_db.sqlite")
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE id = ?", (callback.from_user.id,))
    bal = cursor.fetchone()[0]
    conn.close()
    await callback.message.answer(f"💵 আপনার বর্তমান ব্যালেন্স: **{bal} USDT**", parse_mode="Markdown")

@dp.callback_query(F.data == "refer")
async def refer_link(callback: types.CallbackQuery):
    bot_username = (await bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={callback.from_user.id}"
    await callback.message.answer(f"🔗 আপনার রেফারেল লিঙ্ক:\n`{link}`\n\nপ্রতি সফল রেফারে পাবেন {REWARD_AMOUNT} USDT।", parse_mode="Markdown")

# --- উইথড্র ও xRocket লজিক ---
@dp.callback_query(F.data == "withdraw")
async def withdraw_request(callback: types.CallbackQuery):
    # এখানে xRocket API কল করে পেমেন্ট রিকোয়েস্ট পাঠাতে হবে
    await callback.answer("উইথড্র সিস্টেম শীঘ্রই আসছে...", show_alert=True)

# --- এডমিন প্যানেল (সংক্ষিপ্ত) ---
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_panel(message: types.Message):
    await message.answer("🛠 অ্যাডমিন প্যানেলে স্বাগতম। আপনি এখান থেকে ইউজার কন্ট্রোল করতে পারবেন।")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
