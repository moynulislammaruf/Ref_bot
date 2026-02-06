import os
import sqlite3
import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

# লগিং সেটআপ
logging.basicConfig(level=logging.INFO)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# বাধ্যতামূলক চ্যানেল লিস্ট
CHANNELS = ["@cryptomininginformer", "@Click_To_Earn_By_Nobab_Channel"]

# --- ডাটাবেস কানেকশন ---
def get_db():
    conn = sqlite3.connect("bot_db.sqlite")
    return conn

def db_init():
    conn = get_db()
    cursor = conn.cursor()
    # ইউজার টেবিল
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                   (id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0, 
                   referred_by INTEGER, is_verified INTEGER DEFAULT 0)''')
    # সেটিংস টেবিল (অ্যাডমিন কন্ট্রোলের জন্য)
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings 
                   (key TEXT PRIMARY KEY, value REAL)''')
    
    # ডিফল্ট সেটিংস যদি না থাকে
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('reward', 0.5)")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('min_withdraw', 5.0)")
    
    conn.commit()
    conn.close()

db_init()

# --- হেল্পার ফাংশন ---
async def check_sub(user_id):
    for channel in CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ["left", "kicked"]:
                return False
        except:
            return False
    return True

def get_setting(key):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    val = cursor.fetchone()[0]
    conn.close()
    return val

# --- কিবোর্ড মেনু ---
def main_menu():
    kb = [
        [InlineKeyboardButton(text="✅ Verify Join", callback_data="verify")],
        [InlineKeyboardButton(text="💰 Balance", callback_data="balance"), InlineKeyboardButton(text="🔗 Refer", callback_data="refer")],
        [InlineKeyboardButton(text="💳 Withdraw", callback_data="withdraw")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- হ্যান্ডলারস ---

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split()
    referrer = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    if not cursor.fetchone():
        # নতুন ইউজার হলে ডাটাবেসে সেভ
        cursor.execute("INSERT INTO users (id, referred_by) VALUES (?, ?)", (user_id, referrer))
        conn.commit()
    conn.close()

    text = (
        "👋 **Welcome to Refer To Earn Bot!**\n\n"
        "টাকা আয় করতে নিচের চ্যানেলগুলোতে অবশ্যই জয়েন করুন:\n"
        f"1️⃣ {CHANNELS[0]}\n"
        f"2️⃣ {CHANNELS[1]}\n\n"
        "জয়েন করার পর '✅ Verify Join' বাটনে ক্লিক করুন।"
    )
    await message.answer(text, reply_markup=main_menu(), parse_mode="Markdown")

@dp.callback_query(F.data == "verify")
async def verify_user(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if await check_sub(user_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT is_verified, referred_by FROM users WHERE id = ?", (user_id,))
        res = cursor.fetchone()
        
        if res and res[0] == 0: # আগে ভেরিফাই না হলে
            reward = get_setting('reward')
            cursor.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (user_id,))
            
            if res[1]: # রেফারারকে বোনাস দেওয়া
                cursor.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (reward, res[1]))
                try:
                    await bot.send_message(res[1], f"🎊 অভিনন্দন! আপনার লিঙ্কে একজন জয়েন করেছে। আপনি {reward} USDT পেয়েছেন।")
                except: pass
            
            conn.commit()
            await callback.answer("✅ ভেরিফিকেশন সফল! এখন আপনি রেফার করতে পারবেন।", show_alert=True)
        else:
            await callback.answer("আপনি ইতিমধ্যে ভেরিফাইড!")
        conn.close()
    else:
        await callback.answer("❌ আপনি সব চ্যানেলে জয়েন করেননি!", show_alert=True)

@dp.callback_query(F.data == "balance")
async def balance_check(callback: types.CallbackQuery):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM users WHERE id = ?", (callback.from_user.id,))
    bal = cursor.fetchone()[0]
    conn.close()
    await callback.message.answer(f"💵 আপনার বর্তমান ব্যালেন্স: **{bal} USDT**", parse_mode="Markdown")

@dp.callback_query(F.data == "refer")
async def refer_info(callback: types.CallbackQuery):
    bot_info = await bot.get_me()
    reward = get_setting('reward')
    link = f"https://t.me/{bot_info.username}?start={callback.from_user.id}"
    await callback.message.answer(f"🔗 আপনার রেফারেল লিঙ্ক:\n`{link}`\n\n🎁 প্রতি সফল রেফারে পাবেন: **{reward} USDT**", parse_mode="Markdown")

# --- অ্যাডমিন প্যানেল কমান্ডস ---

@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_main(message: types.Message):
    kb = [
        [InlineKeyboardButton(text="📊 Stats", callback_data="admin_stats"), InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_bc")],
        [InlineKeyboardButton(text="⚙️ Set Reward", callback_data="admin_reward")]
    ]
    await message.answer("🛠 **অ্যাডমিন প্যানেল**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "admin_stats", F.from_user.id == ADMIN_ID)
async def admin_stats(callback: types.CallbackQuery):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(id), SUM(balance) FROM users")
    stats = cursor.fetchone()
    conn.close()
    await callback.message.answer(f"📊 **বোট পরিসংখ্যান:**\n\nমোট ইউজার: {stats[0]}\nমোট ব্যালেন্স: {stats[1]} USDT")

@dp.message(Command("broadcast"), F.from_user.id == ADMIN_ID)
async def broadcast_text(message: types.Message):
    msg = message.text.replace("/broadcast ", "")
    if not msg: return await message.answer("মেসেজ লিখুন: `/broadcast হ্যালো ইউজারস`")
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users")
    users = cursor.fetchall()
    conn.close()
    
    sent = 0
    for u in users:
        try:
            await bot.send_message(u[0], msg)
            sent += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.answer(f"✅ {sent} জন ইউজারকে মেসেজ পাঠানো হয়েছে।")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
