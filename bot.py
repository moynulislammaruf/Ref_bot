import os
import asyncio
import sqlite3
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# লগিং চালু রাখা যাতে রেলওয়ে ড্যাশবোর্ডে ভুল ধরা যায়
logging.basicConfig(level=logging.INFO)

# এনভায়রনমেন্ট ভেরিয়েবল (রেলওয়ে ড্যাশবোর্ড থেকে আসবে)
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNELS = ["@cryptomininginformer", "@Click_To_Earn_By_Nobab_Channel"]

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- ডাটাবেস সেটআপ ---
def db_init():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                   (id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0, 
                   referred_by INTEGER, is_verified INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

db_init()

# --- মেম্বারশিপ চেক ফাংশন ---
async def is_subscribed(user_id):
    for channel in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ["left", "kicked"]:
                return False
        except Exception as e:
            logging.error(f"Error checking sub: {e}")
            return False
    return True

# --- কিবোর্ড ---
def main_menu():
    buttons = [
        [InlineKeyboardButton(text="✅ Verify Join", callback_data="verify")],
        [InlineKeyboardButton(text="💰 Balance", callback_data="bal"), InlineKeyboardButton(text="🔗 Refer", callback_data="ref")],
        [InlineKeyboardButton(text="💳 Withdraw", callback_data="withdraw")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- স্টার্ট হ্যান্ডলার ---
@dp.message(CommandStart())
async def start(message: types.Message):
    user_id = message.from_user.id
    
    # রেফারেল আইডি বের করা
    args = message.text.split()
    referrer = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    
    if not cursor.fetchone():
        # নিজে নিজেকে রেফার করা আটকাতে
        if referrer == user_id:
            referrer = None
        cursor.execute("INSERT INTO users (id, referred_by) VALUES (?, ?)", (user_id, referrer))
        conn.commit()
    conn.close()

    welcome_text = (
        "❤️ **Welcome to Refer & Earn Bot!**\n\n"
        "টাকা আয় করতে নিচের চ্যানেলগুলোতে জয়েন করুন:\n"
        f"🔹 {CHANNELS[0]}\n"
        f"🔹 {CHANNELS[1]}\n\n"
        "জয়েন করার পর **Verify Join** বাটনে ক্লিক করুন।"
    )
    await message.answer(welcome_text, reply_markup=main_menu(), parse_mode="Markdown")

# --- ভেরিফাই বাটন হ্যান্ডলার ---
@dp.callback_query(F.data == "verify")
async def verify_click(call: types.CallbackQuery):
    user_id = call.from_user.id
    
    if await is_subscribed(user_id):
        conn = sqlite3.connect("bot_data.db")
        cursor = conn.cursor()
        cursor.execute("SELECT is_verified, referred_by FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        
        if row and row[0] == 0:
            # ইউজারকে ভেরিফাইড করা
            cursor.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (user_id,))
            
            # রেফারারকে বোনাস দেওয়া (ধরি ০.৫ USDT)
            if row[1]:
                cursor.execute("UPDATE users SET balance = balance + 0.5 WHERE id = ?", (row[1],))
                try:
                    await bot.send_message(row[1], "🎊 অভিনন্দন! আপনার লিঙ্কে একজন জয়েন করেছে। **0.5 USDT** বোনাস পেয়েছেন।")
                except: pass
            
            conn.commit()
            await call.answer("✅ অভিনন্দন! আপনি সফলভাবে ভেরিফাই হয়েছেন।", show_alert=True)
        else:
            await call.answer("আপনি আগে থেকেই ভেরিফাইড!")
        conn.close()
    else:
        await call.answer("❌ আপনি সব চ্যানেলে জয়েন করেননি!", show_alert=True)

# --- ব্যালেন্স চেক ---
@dp.callback_query(F.data == "bal")
async def check_balance(call: types.CallbackQuery):
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE id = ?", (call.from_user.id,))
    res = cursor.fetchone()
    conn.close()
    
    balance = res[0] if res else 0.0
    await call.message.answer(f"💵 আপনার বর্তমান ব্যালেন্স: **{balance} USDT**", parse_mode="Markdown")

# --- রেফারাল লিংক ---
@dp.callback_query(F.data == "ref")
async def refer_link(call: types.CallbackQuery):
    bot_user = await bot.get_me()
    link = f"https://t.me/{bot_user.username}?start={call.from_user.id}"
    await call.message.answer(f"🔗 আপনার রেফারেল লিংক:\n`{link}`\n\nপ্রতিটি ভেরিফাইড রেফারে পাবেন **0.5 USDT**।", parse_mode="Markdown")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
