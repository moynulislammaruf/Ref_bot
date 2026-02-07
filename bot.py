import os
import json
import asyncio
import random
from typing import Dict, Optional

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# =========================
# Config
# =========================
load_dotenv()

MASTER_BOT_TOKEN = os.getenv("MASTER_BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
MASTER_FORCE_JOIN_CHANNEL = os.getenv("MASTER_FORCE_JOIN_CHANNEL", "").strip()
MASTER_ADMINS = {int(x) for x in os.getenv("MASTER_ADMINS", "").split(",") if x.strip().isdigit()}

if not MASTER_BOT_TOKEN:
    raise RuntimeError("MASTER_BOT_TOKEN missing")
if not MASTER_FORCE_JOIN_CHANNEL:
    raise RuntimeError("MASTER_FORCE_JOIN_CHANNEL missing")
if not MASTER_ADMINS:
    raise RuntimeError("MASTER_ADMINS missing")

# =========================
# DB
# =========================
SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS tenants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_user_id INTEGER NOT NULL,
  bot_token TEXT NOT NULL UNIQUE,
  bot_username TEXT,
  plan TEXT NOT NULL DEFAULT 'basic',
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tenant_settings (
  tenant_id INTEGER PRIMARY KEY,
  currency TEXT NOT NULL DEFAULT 'POINTS',
  reward_per_ref INTEGER NOT NULL DEFAULT 1,
  min_withdraw INTEGER NOT NULL DEFAULT 10,
  required_channels TEXT NOT NULL DEFAULT '[]', -- JSON list
  payout_mode TEXT NOT NULL DEFAULT 'manual',   -- manual|xrocket
  xrocket_api_key TEXT,
  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS users (
  tenant_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  referrer_user_id INTEGER,
  joined_ok INTEGER NOT NULL DEFAULT 0,
  captcha_ok INTEGER NOT NULL DEFAULT 0,
  ref_applied INTEGER NOT NULL DEFAULT 0, -- important for idempotency
  referrals_count INTEGER NOT NULL DEFAULT 0,
  balance INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS captcha_state (
  tenant_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS payouts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  method TEXT NOT NULL,
  status TEXT NOT NULL,
  details TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

async def q_one(sql: str, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return await cur.fetchone()

async def q_all(sql: str, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()

async def exec_sql(sql: str, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, params)
        await db.commit()

# =========================
# Keyboards
# =========================
def kb_master_home():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ নতুন Bot রেজিস্টার", callback_data="m:new_bot")],
        [InlineKeyboardButton(text="📦 আমার Bot লিস্ট", callback_data="m:list_bots")],
        [InlineKeyboardButton(text="ℹ️ Help", callback_data="m:help")],
    ])

def kb_tenant_admin_home(plan: str):
    rows = [
        [InlineKeyboardButton(text="⚙️ Settings (View)", callback_data="t:settings")],
        [InlineKeyboardButton(text="📣 Channels (View)", callback_data="t:channels")],
        [InlineKeyboardButton(text="🎁 Reward (View)", callback_data="t:reward")],
        [InlineKeyboardButton(text="💸 Payout (View)", callback_data="t:payout")],
        [InlineKeyboardButton(text="📊 Stats", callback_data="t:stats")],
    ]
    # later: add edit flows; plan gating can be applied here
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_user_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 আমার রেফার", callback_data="u:refs")],
        [InlineKeyboardButton(text="💰 ব্যালান্স", callback_data="u:bal")],
        [InlineKeyboardButton(text="🏧 Withdraw", callback_data="u:wd")],
    ])

# =========================
# Tenant runtime manager
# =========================
class TenantRuntime:
    def __init__(self):
        self.tasks: Dict[int, asyncio.Task] = {}   # tenant_id -> task

    async def start_tenant(self, tenant_id: int, bot_token: str):
        if tenant_id in self.tasks and not self.tasks[tenant_id].done():
            return

        bot = Bot(
            token=bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
        dp = Dispatcher()
        dp.include_router(build_tenant_router(tenant_id))

        async def runner():
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

        self.tasks[tenant_id] = asyncio.create_task(runner())

    async def stop_tenant(self, tenant_id: int):
        t = self.tasks.get(tenant_id)
        if t and not t.done():
            t.cancel()

tenant_runtime = TenantRuntime()

# =========================
# Shared helpers
# =========================
def make_captcha():
    a = random.randint(2, 9)
    b = random.randint(2, 9)
    return f"{a} + {b} = ?", str(a + b)

async def get_settings(tenant_id: int) -> dict:
    row = await q_one("SELECT * FROM tenant_settings WHERE tenant_id=?", (tenant_id,))
    if not row:
        await exec_sql("INSERT INTO tenant_settings(tenant_id) VALUES(?)", (tenant_id,))
        row = await q_one("SELECT * FROM tenant_settings WHERE tenant_id=?", (tenant_id,))
    s = dict(row)
    s["required_channels"] = json.loads(s["required_channels"])
    return s

async def ensure_user(tenant_id: int, user_id: int):
    row = await q_one("SELECT 1 FROM users WHERE tenant_id=? AND user_id=?", (tenant_id, user_id))
    if not row:
        await exec_sql("INSERT INTO users(tenant_id,user_id) VALUES(?,?)", (tenant_id, user_id))

async def check_force_join(bot: Bot, user_id: int, channels: list[str]) -> bool:
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
            if member.status in ("left", "kicked"):
                return False
        except TelegramBadRequest:
            # usually means bot can't check membership (not admin or invalid channel)
            return False
    return True

async def apply_referral_if_ready(tenant_id: int, new_user_id: int):
    u = await q_one("SELECT * FROM users WHERE tenant_id=? AND user_id=?", (tenant_id, new_user_id))
    if not u:
        return
    if u["joined_ok"] != 1 or u["captcha_ok"] != 1:
        return
    if not u["referrer_user_id"]:
        return
    if u["ref_applied"] == 1:
        return

    s = await get_settings(tenant_id)
    reward = int(s["reward_per_ref"])
    referrer_id = int(u["referrer_user_id"])

    # mark applied first (idempotency)
    await exec_sql(
        "UPDATE users SET ref_applied=1 WHERE tenant_id=? AND user_id=?",
        (tenant_id, new_user_id)
    )
    await exec_sql(
        "UPDATE users SET referrals_count=referrals_count+1, balance=balance+? "
        "WHERE tenant_id=? AND user_id=?",
        (reward, tenant_id, referrer_id)
    )

# =========================
# Tenant bot router
# =========================
def build_tenant_router(tenant_id: int) -> Router:
    r = Router()

    @r.message(Command("admin"))
    async def tenant_admin(message: Message):
        t = await q_one("SELECT owner_user_id, plan FROM tenants WHERE id=?", (tenant_id,))
        if not t or message.from_user.id != t["owner_user_id"]:
            return await message.answer("❌ আপনি এই বটের admin নন।")
        await message.answer(
            f"✅ Tenant Admin Panel\nPlan: <b>{t['plan']}</b>",
            reply_markup=kb_tenant_admin_home(t["plan"])
        )

    @r.callback_query(F.data.startswith("t:"))
    async def tenant_admin_callbacks(call: CallbackQuery):
        t = await q_one("SELECT owner_user_id, plan FROM tenants WHERE id=?", (tenant_id,))
        if not t or call.from_user.id != t["owner_user_id"]:
            await call.answer("Not admin", show_alert=True)
            return

        s = await get_settings(tenant_id)

        if call.data == "t:settings":
            await call.message.answer(
                "⚙️ Settings (View)\n"
                f"Currency: <b>{s['currency']}</b>\n"
                f"Reward per ref: <b>{s['reward_per_ref']}</b>\n"
                f"Min withdraw: <b>{s['min_withdraw']}</b>\n"
                f"Payout mode: <b>{s['payout_mode']}</b>\n"
            )
        elif call.data == "t:channels":
            ch = s["required_channels"]
            text = "\n".join([f"• {x}" for x in ch]) if ch else "(none)"
            await call.message.answer("📣 Required channels (View)\n" + text)
        elif call.data == "t:reward":
            await call.message.answer(
                "🎁 Reward (View)\n"
                f"Reward per ref: <b>{s['reward_per_ref']}</b> {s['currency']}"
            )
        elif call.data == "t:payout":
            await call.message.answer(
                "💸 Payout (View)\n"
                f"Mode: <b>{s['payout_mode']}</b>\n"
                "MVP এ withdraw request শুধু 'requested' হিসেবে সেভ হয়।"
            )
        elif call.data == "t:stats":
            rows = await q_one(
                "SELECT COUNT(*) AS users FROM users WHERE tenant_id=?",
                (tenant_id,)
            )
            req = await q_one(
                "SELECT COUNT(*) AS wreq FROM payouts WHERE tenant_id=? AND status='requested'",
                (tenant_id,)
            )
            await call.message.answer(
                "📊 Stats\n"
                f"Users: <b>{rows['users'] if rows else 0}</b>\n"
                f"Withdraw requests: <b>{req['wreq'] if req else 0}</b>\n"
            )

        await call.answer()

    @r.message(CommandStart())
    async def start(message: Message):
        await ensure_user(tenant_id, message.from_user.id)
        s = await get_settings(tenant_id)

        # referral: /start ref_123
        referrer = None
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith("ref_"):
            try:
                referrer = int(parts[1].replace("ref_", "").strip())
            except:
                referrer = None

        # store referrer if first time and not self
        if referrer and referrer != message.from_user.id:
            existing = await q_one(
                "SELECT referrer_user_id FROM users WHERE tenant_id=? AND user_id=?",
                (tenant_id, message.from_user.id)
            )
            if existing and existing["referrer_user_id"] is None:
                await exec_sql(
                    "UPDATE users SET referrer_user_id=? WHERE tenant_id=? AND user_id=?",
                    (referrer, tenant_id, message.from_user.id)
                )

        # tenant force-join
        ok = await check_force_join(message.bot, message.from_user.id, s["required_channels"])
        if not ok:
            ch_list = "\n".join([f"• {c}" for c in s["required_channels"]]) or "• (কোনো চ্যানেল সেট করা নেই)"
            return await message.answer(
                "✅ বট ব্যবহার করতে আগে নিচের চ্যানেলগুলোতে Join করুন, তারপর আবার /start দিন:\n"
                f"{ch_list}\n\n"
                "⚠️ নোট: বটকে ওই চ্যানেলগুলোতে admin না করলে verify ব্যর্থ হতে পারে।"
            )

        await exec_sql(
            "UPDATE users SET joined_ok=1 WHERE tenant_id=? AND user_id=?",
            (tenant_id, message.from_user.id)
        )

        # captcha
        u = await q_one(
            "SELECT captcha_ok FROM users WHERE tenant_id=? AND user_id=?",
            (tenant_id, message.from_user.id)
        )
        if u and u["captcha_ok"] != 1:
            q, ans = make_captcha()
            await exec_sql(
                "INSERT OR REPLACE INTO captcha_state(tenant_id,user_id,question,answer) VALUES(?,?,?,?)",
                (tenant_id, message.from_user.id, q, ans)
            )
            return await message.answer(f"🧩 Captcha: <b>{q}</b>\nউত্তর লিখুন:")

        return await message.answer("🎉 Welcome!\nরেফার লিংক পেতে: /link", reply_markup=kb_user_main())

    @r.message(F.text)
    async def text_handler(message: Message):
        # captcha answer
        st = await q_one(
            "SELECT * FROM captcha_state WHERE tenant_id=? AND user_id=?",
            (tenant_id, message.from_user.id)
        )
        if st:
            ans = (message.text or "").strip()
            if ans == st["answer"]:
                await exec_sql(
                    "DELETE FROM captcha_state WHERE tenant_id=? AND user_id=?",
                    (tenant_id, message.from_user.id)
                )
                await exec_sql(
                    "UPDATE users SET captcha_ok=1 WHERE tenant_id=? AND user_id=?",
                    (tenant_id, message.from_user.id)
                )
                await apply_referral_if_ready(tenant_id, message.from_user.id)
                return await message.answer("✅ Captcha OK! এখন /link বা মেনু ব্যবহার করুন।", reply_markup=kb_user_main())
            return await message.answer("❌ ভুল উত্তর। আবার চেষ্টা করুন:")

    @r.message(Command("link"))
    async def link(message: Message):
        me = await message.bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
        await message.answer(f"🔗 আপনার রেফার লিংক:\n{link}")

    @r.callback_query(F.data == "u:bal")
    async def cb_bal(call: CallbackQuery):
        s = await get_settings(tenant_id)
        u = await q_one(
            "SELECT balance FROM users WHERE tenant_id=? AND user_id=?",
            (tenant_id, call.from_user.id)
        )
        bal = int(u["balance"]) if u else 0
        await call.message.answer(f"💰 Balance: <b>{bal}</b> {s['currency']}")
        await call.answer()

    @r.callback_query(F.data == "u:refs")
    async def cb_refs(call: CallbackQuery):
        u = await q_one(
            "SELECT referrals_count FROM users WHERE tenant_id=? AND user_id=?",
            (tenant_id, call.from_user.id)
        )
        cnt = int(u["referrals_count"]) if u else 0
        await call.message.answer(f"👥 Total referrals: <b>{cnt}</b>")
        await call.answer()

    @r.callback_query(F.data == "u:wd")
    async def cb_wd(call: CallbackQuery):
        s = await get_settings(tenant_id)
        u = await q_one(
            "SELECT balance FROM users WHERE tenant_id=? AND user_id=?",
            (tenant_id, call.from_user.id)
        )
        bal = int(u["balance"]) if u else 0
        if bal < int(s["min_withdraw"]):
            await call.message.answer(f"❌ মিনিমাম withdraw: {s['min_withdraw']} {s['currency']}")
            return await call.answer()

        await exec_sql(
            "INSERT INTO payouts(tenant_id,user_id,amount,method,status) VALUES(?,?,?,?,?)",
            (tenant_id, call.from_user.id, bal, s["payout_mode"], "requested")
        )
        await exec_sql(
            "UPDATE users SET balance=0 WHERE tenant_id=? AND user_id=?",
            (tenant_id, call.from_user.id)
        )
        await call.message.answer("✅ Withdraw request নেওয়া হয়েছে। Admin যাচাই করে পেমেন্ট করবে।")
        await call.answer()

    return r

# =========================
# Master bot router
# =========================
master_router = Router()

async def is_joined_master(bot: Bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(MASTER_FORCE_JOIN_CHANNEL, user_id)
        return m.status not in ("left", "kicked")
    except TelegramBadRequest:
        return False

@master_router.message(CommandStart())
async def master_start(message: Message):
    ok = await is_joined_master(message.bot, message.from_user.id)
    if not ok:
        return await message.answer(
            f"❗ Master panel ব্যবহার করতে আগে Join করুন: {MASTER_FORCE_JOIN_CHANNEL}\n"
            f"Join করে আবার /start দিন।"
        )
    await message.answer("🧠 Referral Master Panel", reply_markup=kb_master_home())

@master_router.callback_query(F.data == "m:help")
async def m_help(call: CallbackQuery):
    await call.message.answer(
        "✅ কীভাবে কাজ করে:\n"
        "1) BotFather থেকে নতুন bot বানিয়ে token নিন\n"
        "2) ‘নতুন Bot রেজিস্টার’ এ token দিন\n"
        "3) আপনার bot চালু হবে (এই সার্ভারেই)\n"
        "4) আপনার bot-এ গিয়ে /admin দিন\n\n"
        "⚠️ Channel verify করতে child bot-কে required channel-এ admin করতে হতে পারে।"
    )
    await call.answer()

@master_router.callback_query(F.data == "m:list_bots")
async def m_list(call: CallbackQuery):
    rows = await q_all(
        "SELECT id, bot_username, plan, is_active FROM tenants WHERE owner_user_id=?",
        (call.from_user.id,)
    )
    if not rows:
        await call.message.answer("আপনার কোনো bot রেজিস্টার করা নেই।")
        return await call.answer()

    lines = []
    for r in rows:
        lines.append(f"#{r['id']} • @{r['bot_username'] or 'unknown'} • plan={r['plan']} • active={r['is_active']}")
    await call.message.answer("\n".join(lines))
    await call.answer()

@master_router.callback_query(F.data == "m:new_bot")
async def m_newbot(call: CallbackQuery):
    await call.message.answer(
        "BotFather থেকে token কপি করে এই চ্যাটে পাঠান (123456:ABC...)\n"
        "⚠️ Token দিলে এই সার্ভার ওই বট চালাবে।"
    )
    await call.answer()

@master_router.message(F.text)
async def master_text(message: Message):
    text = (message.text or "").strip()

    # quick token heuristic
    if ":" not in text or len(text) < 30:
        return

    ok = await is_joined_master(message.bot, message.from_user.id)
    if not ok:
        return await message.answer(f"আগে Join করুন: {MASTER_FORCE_JOIN_CHANNEL}")

    token = text

    # already registered?
    existing = await q_one("SELECT id FROM tenants WHERE bot_token=?", (token,))
    if existing:
        return await message.answer("❌ এই token আগেই রেজিস্টার করা আছে।")

    # insert tenant
    await exec_sql(
        "INSERT INTO tenants(owner_user_id, bot_token, plan, is_active) VALUES(?,?,?,1)",
        (message.from_user.id, token, "basic")
    )
    t = await q_one("SELECT id FROM tenants WHERE bot_token=?", (token,))
    tenant_id = int(t["id"])

    # try to read username
    temp_bot = Bot(token=token)
    try:
        me = await temp_bot.get_me()
        await exec_sql("UPDATE tenants SET bot_username=? WHERE id=?", (me.username, tenant_id))
        await message.answer(
            f"✅ Bot রেজিস্টার সফল: @{me.username}\n"
            f"এখন ওই বটে গিয়ে /admin দিন।"
        )
    except Exception:
        await message.answer("✅ Bot রেজিস্টার হয়েছে, কিন্তু username পড়া যায়নি।")
    finally:
        await temp_bot.session.close()

    # start tenant runner
    await tenant_runtime.start_tenant(tenant_id, token)

# =========================
# App main
# =========================
async def main():
    await init_db()

    # start all active tenants on boot
    tenants = await q_all("SELECT id, bot_token FROM tenants WHERE is_active=1")
    for t in tenants:
        await tenant_runtime.start_tenant(int(t["id"]), t["bot_token"])

    # start master bot polling
    master_bot = Bot(
        token=MASTER_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(master_router)

    await dp.start_polling(master_bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
