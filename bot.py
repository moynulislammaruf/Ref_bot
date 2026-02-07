import os
import json
import asyncio
import random
from typing import Dict, Optional, Tuple

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
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

  -- bot status
  bot_on INTEGER NOT NULL DEFAULT 1,
  maintenance_on INTEGER NOT NULL DEFAULT 0,

  -- captcha / verification
  captcha_on INTEGER NOT NULL DEFAULT 1,

  -- channels
  joining_channels TEXT NOT NULL DEFAULT '[]', -- JSON list
  check_channels   TEXT NOT NULL DEFAULT '[]', -- JSON list

  -- economics
  currency TEXT NOT NULL DEFAULT 'POINTS',
  reward_per_ref INTEGER NOT NULL DEFAULT 1,

  -- withdraw
  withdraw_on INTEGER NOT NULL DEFAULT 1,
  withdraw_confirmation_on INTEGER NOT NULL DEFAULT 0,
  min_withdraw REAL NOT NULL DEFAULT 10,
  max_withdraw REAL NOT NULL DEFAULT 1000000,
  withdraw_tax REAL NOT NULL DEFAULT 0,

  -- payout
  payout_mode TEXT NOT NULL DEFAULT 'manual',   -- manual|xrocket
  xrocket_api_key TEXT,

  -- texts
  home_text TEXT NOT NULL DEFAULT '🎉 Welcome!',

  FOREIGN KEY(tenant_id) REFERENCES tenants(id)
);

CREATE TABLE IF NOT EXISTS tenant_admins (
  tenant_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS users (
  tenant_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  referrer_user_id INTEGER,
  joined_ok INTEGER NOT NULL DEFAULT 0,
  captcha_ok INTEGER NOT NULL DEFAULT 0,
  ref_applied INTEGER NOT NULL DEFAULT 0,
  referrals_count INTEGER NOT NULL DEFAULT 0,
  balance REAL NOT NULL DEFAULT 0,
  is_banned INTEGER NOT NULL DEFAULT 0,
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
  amount REAL NOT NULL,
  method TEXT NOT NULL,         -- manual|xrocket
  status TEXT NOT NULL,         -- requested|paid|rejected
  details TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS admin_state (
  tenant_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  payload TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, user_id)
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
        [InlineKeyboardButton(text="➕ Create Bot", callback_data="m:new_bot")],
        [InlineKeyboardButton(text="📦 My Bots", callback_data="m:list_bots")],
        [InlineKeyboardButton(text="ℹ️ Help", callback_data="m:help")],
    ])

def kb_user_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 BALANCE", callback_data="u:bal"),
         InlineKeyboardButton(text="👥 MY TEAM", callback_data="u:refs")],
        [InlineKeyboardButton(text="🔗 REFER", callback_data="u:link"),
         InlineKeyboardButton(text="🎁 BONUS", callback_data="u:bonus")],
        [InlineKeyboardButton(text="🏧 WITHDRAW", callback_data="u:wd"),
         InlineKeyboardButton(text="📊 STATISTICS", callback_data="u:stats")],
    ])

def kb_join_check(channels: list[str]):
    rows = []
    for ch in channels:
        rows.append([InlineKeyboardButton(text=f"🔗 {ch}", url=f"https://t.me/{ch.lstrip('@')}")])
    rows.append([InlineKeyboardButton(text="✅ Check", callback_data="u:check_join")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_tenant_admin_panel(s: dict, is_owner: bool):
    # mimic “toggle” panel
    def onoff(v: int) -> str:
        return "✅" if int(v) == 1 else "❌"
    def ena(v: int) -> str:
        return "✅ ENABLE" if int(v) == 1 else "❌ DISABLE"

    # You can reorder to match your screenshot style
    rows = [
        [InlineKeyboardButton(text=f"Bot Status ~ {ena(s['bot_on'])}", callback_data="a:toggle_bot"),
         InlineKeyboardButton(text=f"Maintenance ~ {ena(1-int(s['maintenance_on']))}".replace("ENABLE","DISABLE") if s['maintenance_on'] else f"Maintenance ~ {ena(1)}", callback_data="a:toggle_maint")],

        [InlineKeyboardButton(text=f"Withdraw Status ~ {ena(s['withdraw_on'])}", callback_data="a:toggle_withdraw"),
         InlineKeyboardButton(text=f"Withdraw Confirm ~ {onoff(s['withdraw_confirmation_on'])}", callback_data="a:toggle_wdconf")],

        [InlineKeyboardButton(text="Set Minimum Withdraw", callback_data="a:set_minwd"),
         InlineKeyboardButton(text="Set Maximum Withdraw", callback_data="a:set_maxwd")],

        [InlineKeyboardButton(text="Set Withdraw Tax", callback_data="a:set_tax"),
         InlineKeyboardButton(text="Set Per Refer", callback_data="a:set_reward")],

        [InlineKeyboardButton(text="Set Payout Currency", callback_data="a:set_currency"),
         InlineKeyboardButton(text="Set Bot Currency (alias)", callback_data="a:set_currency")],

        [InlineKeyboardButton(text="Set Joining Channel", callback_data="a:add_join_ch"),
         InlineKeyboardButton(text="Remove Joining Channel", callback_data="a:rm_join_ch")],

        [InlineKeyboardButton(text="Set Check Channel", callback_data="a:add_check_ch"),
         InlineKeyboardButton(text="Remove Check Channel", callback_data="a:rm_check_ch")],

        [InlineKeyboardButton(text=f"Captcha Verification ~ {ena(s['captcha_on'])}", callback_data="a:toggle_captcha"),
         InlineKeyboardButton(text="Broadcast", callback_data="a:broadcast")],

        [InlineKeyboardButton(text="Find User Details", callback_data="a:find_user"),
         InlineKeyboardButton(text="Withdraw Requests", callback_data="a:wd_list")],

        [InlineKeyboardButton(text="Ban User", callback_data="a:ban_user"),
         InlineKeyboardButton(text="Unban User", callback_data="a:unban_user")],

        [InlineKeyboardButton(text="Add Balance", callback_data="a:add_balance"),
         InlineKeyboardButton(text="Remove Balance", callback_data="a:rm_balance")],
    ]

    # owner-only controls
    if is_owner:
        rows.append([
            InlineKeyboardButton(text="Add Admin", callback_data="a:add_admin"),
            InlineKeyboardButton(text="Remove Admin", callback_data="a:rm_admin"),
        ])
        rows.append([
            InlineKeyboardButton(text="Get Admin List", callback_data="a:list_admins"),
            InlineKeyboardButton(text="Check Ban List", callback_data="a:list_bans"),
        ])
        rows.append([
            InlineKeyboardButton(text="Set API Key (xRocket)", callback_data="a:set_xrocket"),
            InlineKeyboardButton(text="Set Home Text", callback_data="a:set_home"),
        ])
        rows.append([
            InlineKeyboardButton(text="Toggle Payout Mode (manual/xrocket)", callback_data="a:toggle_payoutmode")
        ])

    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_withdraw_request_actions(payout_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve (Paid)", callback_data=f"wd:approve:{payout_id}"),
         InlineKeyboardButton(text="❌ Reject", callback_data=f"wd:reject:{payout_id}")]
    ])


# =========================
# Tenant runtime manager
# =========================
class TenantRuntime:
    def __init__(self):
        self.tasks: Dict[int, asyncio.Task] = {}

    async def start_tenant(self, tenant_id: int, bot_token: str):
        if tenant_id in self.tasks and not self.tasks[tenant_id].done():
            return

        bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher()
        dp.include_router(build_tenant_router(tenant_id))

        async def runner():
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

        self.tasks[tenant_id] = asyncio.create_task(runner())

tenant_runtime = TenantRuntime()


# =========================
# Helpers
# =========================
def make_captcha() -> Tuple[str, str]:
    a = random.randint(2, 9)
    b = random.randint(2, 9)
    return f"{a} + {b} = ?", str(a + b)

async def get_settings(tenant_id: int) -> dict:
    row = await q_one("SELECT * FROM tenant_settings WHERE tenant_id=?", (tenant_id,))
    if not row:
        await exec_sql("INSERT INTO tenant_settings(tenant_id) VALUES(?)", (tenant_id,))
        row = await q_one("SELECT * FROM tenant_settings WHERE tenant_id=?", (tenant_id,))
    s = dict(row)
    s["joining_channels"] = json.loads(s["joining_channels"])
    s["check_channels"] = json.loads(s["check_channels"])
    return s

async def set_settings_json_list(tenant_id: int, field: str, value: list[str]):
    await exec_sql(
        f"UPDATE tenant_settings SET {field}=? WHERE tenant_id=?",
        (json.dumps(value, ensure_ascii=False), tenant_id)
    )

async def ensure_user(tenant_id: int, user_id: int):
    row = await q_one("SELECT 1 FROM users WHERE tenant_id=? AND user_id=?", (tenant_id, user_id))
    if not row:
        await exec_sql("INSERT INTO users(tenant_id,user_id) VALUES(?,?)", (tenant_id, user_id))

async def is_admin(tenant_id: int, user_id: int) -> Tuple[bool, bool]:
    t = await q_one("SELECT owner_user_id FROM tenants WHERE id=?", (tenant_id,))
    if not t:
        return False, False
    if int(t["owner_user_id"]) == int(user_id):
        return True, True
    a = await q_one("SELECT 1 FROM tenant_admins WHERE tenant_id=? AND user_id=?", (tenant_id, user_id))
    return (a is not None), False

async def check_join(bot: Bot, user_id: int, channels: list[str]) -> bool:
    # requires bot to be admin in those channels in many cases
    for ch in channels:
        try:
            m = await bot.get_chat_member(chat_id=ch, user_id=user_id)
            if m.status in ("left", "kicked"):
                return False
        except TelegramBadRequest:
            return False
    return True

async def set_admin_state(tenant_id: int, user_id: int, action: str, payload: str = ""):
    await exec_sql(
        "INSERT OR REPLACE INTO admin_state(tenant_id,user_id,action,payload) VALUES(?,?,?,?)",
        (tenant_id, user_id, action, payload)
    )

async def pop_admin_state(tenant_id: int, user_id: int) -> Tuple[Optional[str], str]:
    row = await q_one("SELECT action, payload FROM admin_state WHERE tenant_id=? AND user_id=?",
                      (tenant_id, user_id))
    if row:
        await exec_sql("DELETE FROM admin_state WHERE tenant_id=? AND user_id=?",
                       (tenant_id, user_id))
        return row["action"], (row["payload"] or "")
    return None, ""

async def apply_referral_if_ready(tenant_id: int, new_user_id: int):
    u = await q_one("SELECT * FROM users WHERE tenant_id=? AND user_id=?", (tenant_id, new_user_id))
    if not u:
        return
    if int(u["is_banned"]) == 1:
        return
    if u["joined_ok"] != 1:
        return
    if u["captcha_ok"] != 1:
        return
    if not u["referrer_user_id"]:
        return
    if u["ref_applied"] == 1:
        return

    s = await get_settings(tenant_id)
    reward = float(s["reward_per_ref"])
    referrer_id = int(u["referrer_user_id"])

    # mark applied
    await exec_sql(
        "UPDATE users SET ref_applied=1 WHERE tenant_id=? AND user_id=?",
        (tenant_id, new_user_id)
    )
    # credit referrer
    await exec_sql(
        "UPDATE users SET referrals_count=referrals_count+1, balance=balance+? "
        "WHERE tenant_id=? AND user_id=?",
        (reward, tenant_id, referrer_id)
    )


# =========================
# Tenant Router (child bot)
# =========================
def build_tenant_router(tenant_id: int) -> Router:
    r = Router()

    @r.message(Command("adminpanel"))
    @r.message(Command("admin"))
    async def open_admin_panel(message: Message):
        admin_ok, is_owner = await is_admin(tenant_id, message.from_user.id)
        if not admin_ok:
            return await message.answer("❌ You are not admin.")
        s = await get_settings(tenant_id)
        panel_text = (
            "✅ <b>WELCOME TO ADMIN PANEL</b>\n\n"
            f"Bot ON/OFF: {'ON' if s['bot_on'] else 'OFF'}\n"
            f"Maintenance: {'ON' if s['maintenance_on'] else 'OFF'}\n"
            f"Withdraw: {'ON' if s['withdraw_on'] else 'OFF'}\n"
            f"Captcha: {'ON' if s['captcha_on'] else 'OFF'}\n"
            f"Per Refer: {s['reward_per_ref']} {s['currency']}\n"
            f"Min Withdraw: {s['min_withdraw']}\n"
            f"Max Withdraw: {s['max_withdraw']}\n"
            f"Withdraw Tax: {s['withdraw_tax']}\n"
            f"Payout Mode: {s['payout_mode']}\n"
        )
        await message.answer(panel_text, reply_markup=kb_tenant_admin_panel(s, is_owner))

    @r.callback_query(F.data.startswith("a:"))
    async def admin_actions(call: CallbackQuery):
        admin_ok, is_owner = await is_admin(tenant_id, call.from_user.id)
        if not admin_ok:
            return await call.answer("Not admin", show_alert=True)
        s = await get_settings(tenant_id)

        def refresh_text(ss: dict) -> str:
            return (
                "✅ <b>WELCOME TO ADMIN PANEL</b>\n\n"
                f"Bot ON/OFF: {'ON' if ss['bot_on'] else 'OFF'}\n"
                f"Maintenance: {'ON' if ss['maintenance_on'] else 'OFF'}\n"
                f"Withdraw: {'ON' if ss['withdraw_on'] else 'OFF'}\n"
                f"Captcha: {'ON' if ss['captcha_on'] else 'OFF'}\n"
                f"Per Refer: {ss['reward_per_ref']} {ss['currency']}\n"
                f"Min Withdraw: {ss['min_withdraw']}\n"
                f"Max Withdraw: {ss['max_withdraw']}\n"
                f"Withdraw Tax: {ss['withdraw_tax']}\n"
                f"Payout Mode: {ss['payout_mode']}\n"
            )

        action = call.data

        # toggles
        if action == "a:toggle_bot":
            await exec_sql("UPDATE tenant_settings SET bot_on=1-bot_on WHERE tenant_id=?", (tenant_id,))
        elif action == "a:toggle_maint":
            await exec_sql("UPDATE tenant_settings SET maintenance_on=1-maintenance_on WHERE tenant_id=?", (tenant_id,))
        elif action == "a:toggle_captcha":
            await exec_sql("UPDATE tenant_settings SET captcha_on=1-captcha_on WHERE tenant_id=?", (tenant_id,))
        elif action == "a:toggle_withdraw":
            await exec_sql("UPDATE tenant_settings SET withdraw_on=1-withdraw_on WHERE tenant_id=?", (tenant_id,))
        elif action == "a:toggle_wdconf":
            await exec_sql("UPDATE tenant_settings SET withdraw_confirmation_on=1-withdraw_confirmation_on WHERE tenant_id=?", (tenant_id,))
        elif action == "a:toggle_payoutmode":
            if not is_owner:
                return await call.answer("Owner only", show_alert=True)
            new_mode = "xrocket" if s["payout_mode"] == "manual" else "manual"
            await exec_sql("UPDATE tenant_settings SET payout_mode=? WHERE tenant_id=?", (new_mode, tenant_id))
            await call.message.answer(f"✅ Payout mode set: <b>{new_mode}</b>")

        # setters (wizard)
        elif action == "a:set_minwd":
            await set_admin_state(tenant_id, call.from_user.id, "set_minwd")
            await call.message.answer("Send minimum withdraw amount (number).")
        elif action == "a:set_maxwd":
            await set_admin_state(tenant_id, call.from_user.id, "set_maxwd")
            await call.message.answer("Send maximum withdraw amount (number).")
        elif action == "a:set_tax":
            await set_admin_state(tenant_id, call.from_user.id, "set_tax")
            await call.message.answer("Send withdraw tax amount (number).")
        elif action == "a:set_reward":
            await set_admin_state(tenant_id, call.from_user.id, "set_reward")
            await call.message.answer("Send reward per refer (number).")
        elif action == "a:set_currency":
            await set_admin_state(tenant_id, call.from_user.id, "set_currency")
            await call.message.answer("Send currency text (e.g. USDT / POINTS).")

        elif action == "a:add_join_ch":
            await set_admin_state(tenant_id, call.from_user.id, "add_join_ch")
            await call.message.answer("Send channel username to ADD in Joining Channels (example: @mychannel).")
        elif action == "a:rm_join_ch":
            await set_admin_state(tenant_id, call.from_user.id, "rm_join_ch")
            await call.message.answer("Send channel username to REMOVE from Joining Channels (example: @mychannel).")
        elif action == "a:add_check_ch":
            await set_admin_state(tenant_id, call.from_user.id, "add_check_ch")
            await call.message.answer("Send channel username to ADD in Check Channels (example: @mychannel).")
        elif action == "a:rm_check_ch":
            await set_admin_state(tenant_id, call.from_user.id, "rm_check_ch")
            await call.message.answer("Send channel username to REMOVE from Check Channels (example: @mychannel).")

        elif action == "a:broadcast":
            await set_admin_state(tenant_id, call.from_user.id, "broadcast")
            await call.message.answer("Send broadcast text now (will be sent to all users).")

        elif action == "a:find_user":
            await set_admin_state(tenant_id, call.from_user.id, "find_user")
            await call.message.answer("Send user_id to find details.")
        elif action == "a:ban_user":
            await set_admin_state(tenant_id, call.from_user.id, "ban_user")
            await call.message.answer("Send user_id to BAN.")
        elif action == "a:unban_user":
            await set_admin_state(tenant_id, call.from_user.id, "unban_user")
            await call.message.answer("Send user_id to UNBAN.")
        elif action == "a:add_balance":
            await set_admin_state(tenant_id, call.from_user.id, "add_balance")
            await call.message.answer("Send: user_id amount  (example: 123456789 10)")
        elif action == "a:rm_balance":
            await set_admin_state(tenant_id, call.from_user.id, "rm_balance")
            await call.message.answer("Send: user_id amount  (example: 123456789 10)")

        elif action == "a:add_admin":
            if not is_owner:
                return await call.answer("Owner only", show_alert=True)
            await set_admin_state(tenant_id, call.from_user.id, "add_admin")
            await call.message.answer("Send user_id to ADD as admin.")
        elif action == "a:rm_admin":
            if not is_owner:
                return await call.answer("Owner only", show_alert=True)
            await set_admin_state(tenant_id, call.from_user.id, "rm_admin")
            await call.message.answer("Send user_id to REMOVE admin.")
        elif action == "a:list_admins":
            if not is_owner:
                return await call.answer("Owner only", show_alert=True)
            rows = await q_all("SELECT user_id FROM tenant_admins WHERE tenant_id=?", (tenant_id,))
            lst = "\n".join([str(r["user_id"]) for r in rows]) or "(none)"
            await call.message.answer("👮 Admin list:\n" + lst)
        elif action == "a:list_bans":
            if not is_owner:
                return await call.answer("Owner only", show_alert=True)
            rows = await q_all("SELECT user_id FROM users WHERE tenant_id=? AND is_banned=1", (tenant_id,))
            lst = "\n".join([str(r["user_id"]) for r in rows]) or "(none)"
            await call.message.answer("🚫 Banned users:\n" + lst)

        elif action == "a:set_xrocket":
            if not is_owner:
                return await call.answer("Owner only", show_alert=True)
            await set_admin_state(tenant_id, call.from_user.id, "set_xrocket")
            await call.message.answer("Send xRocket API key.")
        elif action == "a:set_home":
            if not is_owner:
                return await call.answer("Owner only", show_alert=True)
            await set_admin_state(tenant_id, call.from_user.id, "set_home")
            await call.message.answer("Send new home text for /start (users).")

        elif action == "a:wd_list":
            # show recent withdraw requests
            rows = await q_all(
                "SELECT id, user_id, amount, method, status, created_at FROM payouts "
                "WHERE tenant_id=? ORDER BY id DESC LIMIT 10",
                (tenant_id,)
            )
            if not rows:
                await call.message.answer("No withdraw requests yet.")
            else:
                for rrow in rows:
                    txt = (
                        f"🏧 <b>Withdraw Request</b>\n"
                        f"ID: <code>{rrow['id']}</code>\n"
                        f"User: <code>{rrow['user_id']}</code>\n"
                        f"Amount: <b>{rrow['amount']}</b>\n"
                        f"Method: <b>{rrow['method']}</b>\n"
                        f"Status: <b>{rrow['status']}</b>\n"
                        f"At: {rrow['created_at']}"
                    )
                    if rrow["status"] == "requested":
                        await call.message.answer(txt, reply_markup=kb_withdraw_request_actions(int(rrow["id"])))
                    else:
                        await call.message.answer(txt)

        # refresh panel message (best effort)
        try:
            ss = await get_settings(tenant_id)
            await call.message.edit_text(refresh_text(ss), reply_markup=kb_tenant_admin_panel(ss, is_owner))
        except:
            pass

        await call.answer()

    @r.callback_query(F.data.startswith("wd:"))
    async def withdraw_moderation(call: CallbackQuery):
        admin_ok, _is_owner = await is_admin(tenant_id, call.from_user.id)
        if not admin_ok:
            return await call.answer("Not admin", show_alert=True)

        parts = call.data.split(":")
        if len(parts) != 3:
            return await call.answer("Bad action", show_alert=True)
        action, payout_id = parts[1], int(parts[2])

        row = await q_one(
            "SELECT id, user_id, amount, status FROM payouts WHERE tenant_id=? AND id=?",
            (tenant_id, payout_id)
        )
        if not row:
            return await call.answer("Not found", show_alert=True)
        if row["status"] != "requested":
            return await call.answer("Already processed", show_alert=True)

        if action == "approve":
            await exec_sql(
                "UPDATE payouts SET status='paid' WHERE tenant_id=? AND id=?",
                (tenant_id, payout_id)
            )
            await call.message.answer(f"✅ Marked as PAID. (ID: {payout_id})")
        elif action == "reject":
            # refund back to user balance
            await exec_sql(
                "UPDATE payouts SET status='rejected' WHERE tenant_id=? AND id=?",
                (tenant_id, payout_id)
            )
            await exec_sql(
                "UPDATE users SET balance=balance+? WHERE tenant_id=? AND user_id=?",
                (float(row["amount"]), tenant_id, int(row["user_id"]))
            )
            await call.message.answer(f"❌ Rejected + refunded. (ID: {payout_id})")

        await call.answer("Done")

    @r.message(CommandStart())
    async def start(message: Message):
        s = await get_settings(tenant_id)

        # bot OFF
        if int(s["bot_on"]) != 1:
            return await message.answer("❌ Bot is currently OFF.")
        # maintenance
        if int(s["maintenance_on"]) == 1:
            return await message.answer("🛠 Bot is under maintenance. Please try later.")

        await ensure_user(tenant_id, message.from_user.id)

        # banned?
        u0 = await q_one("SELECT is_banned FROM users WHERE tenant_id=? AND user_id=?", (tenant_id, message.from_user.id))
        if u0 and int(u0["is_banned"]) == 1:
            return await message.answer("🚫 You are banned.")

        # referral param: /start ref_123
        referrer = None
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith("ref_"):
            try:
                referrer = int(parts[1].replace("ref_", "").strip())
            except:
                referrer = None

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

        # Joining channels enforcement
        join_ok = await check_join(message.bot, message.from_user.id, s["joining_channels"])
        if not join_ok and s["joining_channels"]:
            await exec_sql("UPDATE users SET joined_ok=0 WHERE tenant_id=? AND user_id=?",
                           (tenant_id, message.from_user.id))
            return await message.answer(
                "⚠️ You Need To Join Channels Before Using This Bot!",
                reply_markup=kb_join_check(s["joining_channels"])
            )

        await exec_sql("UPDATE users SET joined_ok=1 WHERE tenant_id=? AND user_id=?",
                       (tenant_id, message.from_user.id))

        # Captcha if enabled
        u = await q_one("SELECT captcha_ok FROM users WHERE tenant_id=? AND user_id=?",
                        (tenant_id, message.from_user.id))
        if int(s["captcha_on"]) == 1 and u and u["captcha_ok"] != 1:
            q, ans = make_captcha()
            await exec_sql(
                "INSERT OR REPLACE INTO captcha_state(tenant_id,user_id,question,answer) VALUES(?,?,?,?)",
                (tenant_id, message.from_user.id, q, ans)
            )
            return await message.answer(f"🧩 Captcha: <b>{q}</b>\nSend answer:")

        # show home
        await message.answer(s["home_text"], reply_markup=kb_user_main())

    @r.callback_query(F.data == "u:check_join")
    async def user_check_join(call: CallbackQuery):
        s = await get_settings(tenant_id)
        ok = await check_join(call.bot, call.from_user.id, s["joining_channels"])
        if not ok:
            return await call.answer("Not joined yet", show_alert=True)
        await exec_sql("UPDATE users SET joined_ok=1 WHERE tenant_id=? AND user_id=?",
                       (tenant_id, call.from_user.id))
        await call.message.answer("✅ Joined verified. Now send /start again.")
        await call.answer()

    @r.message(F.text)
    async def text_router(message: Message):
        s = await get_settings(tenant_id)

        # 1) Admin wizard input
        admin_ok, is_owner = await is_admin(tenant_id, message.from_user.id)
        if admin_ok:
            action, payload = await pop_admin_state(tenant_id, message.from_user.id)
            if action:
                txt = (message.text or "").strip()

                try:
                    if action == "set_minwd":
                        v = float(txt); await exec_sql("UPDATE tenant_settings SET min_withdraw=? WHERE tenant_id=?", (v, tenant_id))
                        return await message.answer(f"✅ Min withdraw set: {v}")
                    if action == "set_maxwd":
                        v = float(txt); await exec_sql("UPDATE tenant_settings SET max_withdraw=? WHERE tenant_id=?", (v, tenant_id))
                        return await message.answer(f"✅ Max withdraw set: {v}")
                    if action == "set_tax":
                        v = float(txt); await exec_sql("UPDATE tenant_settings SET withdraw_tax=? WHERE tenant_id=?", (v, tenant_id))
                        return await message.answer(f"✅ Withdraw tax set: {v}")
                    if action == "set_reward":
                        v = float(txt); await exec_sql("UPDATE tenant_settings SET reward_per_ref=? WHERE tenant_id=?", (v, tenant_id))
                        return await message.answer(f"✅ Reward per refer set: {v}")
                    if action == "set_currency":
                        if len(txt) > 20: return await message.answer("❌ Too long. Max 20 chars.")
                        await exec_sql("UPDATE tenant_settings SET currency=? WHERE tenant_id=?", (txt, tenant_id))
                        return await message.answer(f"✅ Currency set: {txt}")

                    if action in ("add_join_ch", "rm_join_ch", "add_check_ch", "rm_check_ch"):
                        if not txt.startswith("@"):
                            return await message.answer("❌ Send channel like @channelusername")
                        field = "joining_channels" if "join" in action else "check_channels"
                        lst = s[field].copy()
                        if action.startswith("add_"):
                            if txt not in lst:
                                lst.append(txt)
                            await set_settings_json_list(tenant_id, field, lst)
                            return await message.answer(f"✅ Added to {field}: {txt}")
                        else:
                            if txt in lst:
                                lst.remove(txt)
                            await set_settings_json_list(tenant_id, field, lst)
                            return await message.answer(f"✅ Removed from {field}: {txt}")

                    if action == "broadcast":
                        # send to all users (best effort)
                        users = await q_all("SELECT user_id FROM users WHERE tenant_id=? AND is_banned=0", (tenant_id,))
                        okc = 0
                        for ur in users:
                            try:
                                await message.bot.send_message(int(ur["user_id"]), txt)
                                okc += 1
                            except:
                                pass
                        return await message.answer(f"✅ Broadcast done. Delivered: {okc}/{len(users)}")

                    if action == "find_user":
                        uid = int(txt)
                        u = await q_one("SELECT * FROM users WHERE tenant_id=? AND user_id=?", (tenant_id, uid))
                        if not u:
                            return await message.answer("Not found.")
                        return await message.answer(
                            "👤 User Details\n"
                            f"ID: <code>{u['user_id']}</code>\n"
                            f"Balance: <b>{u['balance']}</b>\n"
                            f"Refs: <b>{u['referrals_count']}</b>\n"
                            f"Banned: <b>{u['is_banned']}</b>\n"
                            f"Referrer: <code>{u['referrer_user_id']}</code>\n"
                        )

                    if action in ("ban_user", "unban_user"):
                        uid = int(txt)
                        v = 1 if action == "ban_user" else 0
                        await exec_sql("UPDATE users SET is_banned=? WHERE tenant_id=? AND user_id=?", (v, tenant_id, uid))
                        return await message.answer(f"✅ {'BANNED' if v==1 else 'UNBANNED'}: {uid}")

                    if action in ("add_balance", "rm_balance"):
                        parts = txt.split()
                        if len(parts) != 2:
                            return await message.answer("❌ Send: user_id amount (example: 123 10)")
                        uid = int(parts[0]); amt = float(parts[1])
                        if amt < 0:
                            return await message.answer("❌ amount must be positive")
                        sign = 1 if action == "add_balance" else -1
                        await exec_sql(
                            "UPDATE users SET balance=balance+? WHERE tenant_id=? AND user_id=?",
                            (sign*amt, tenant_id, uid)
                        )
                        return await message.answer(f"✅ Balance updated: {uid} ({'+' if sign==1 else '-'}{amt})")

                    if action == "add_admin":
                        if not is_owner:
                            return await message.answer("Owner only.")
                        uid = int(txt)
                        await exec_sql("INSERT OR IGNORE INTO tenant_admins(tenant_id,user_id) VALUES(?,?)", (tenant_id, uid))
                        return await message.answer(f"✅ Admin added: {uid}")

                    if action == "rm_admin":
                        if not is_owner:
                            return await message.answer("Owner only.")
                        uid = int(txt)
                        await exec_sql("DELETE FROM tenant_admins WHERE tenant_id=? AND user_id=?", (tenant_id, uid))
                        return await message.answer(f"✅ Admin removed: {uid}")

                    if action == "set_xrocket":
                        if not is_owner:
                            return await message.answer("Owner only.")
                        await exec_sql("UPDATE tenant_settings SET xrocket_api_key=? WHERE tenant_id=?", (txt, tenant_id))
                        return await message.answer("✅ xRocket API key saved.")

                    if action == "set_home":
                        if not is_owner:
                            return await message.answer("Owner only.")
                        await exec_sql("UPDATE tenant_settings SET home_text=? WHERE tenant_id=?", (txt, tenant_id))
                        return await message.answer("✅ Home text updated.")

                except ValueError:
                    return await message.answer("❌ Invalid number format.")
                except Exception:
                    return await message.answer("❌ Failed. Try again.")

        # 2) Captcha answer (for normal users)
        st = await q_one("SELECT * FROM captcha_state WHERE tenant_id=? AND user_id=?",
                         (tenant_id, message.from_user.id))
        if st:
            ans = (message.text or "").strip()
            if ans == st["answer"]:
                await exec_sql("DELETE FROM captcha_state WHERE tenant_id=? AND user_id=?",
                               (tenant_id, message.from_user.id))
                await exec_sql("UPDATE users SET captcha_ok=1 WHERE tenant_id=? AND user_id=?",
                               (tenant_id, message.from_user.id))
                await apply_referral_if_ready(tenant_id, message.from_user.id)
                s2 = await get_settings(tenant_id)
                return await message.answer("✅ Captcha OK! Send /start again.", reply_markup=None)
            return await message.answer("❌ Wrong captcha. Try again:")

    # User callbacks
    @r.callback_query(F.data == "u:bal")
    async def u_bal(call: CallbackQuery):
        s = await get_settings(tenant_id)
        u = await q_one("SELECT balance FROM users WHERE tenant_id=? AND user_id=?", (tenant_id, call.from_user.id))
        bal = float(u["balance"]) if u else 0.0
        await call.message.answer(f"💰 Balance: <b>{bal}</b> {s['currency']}")
        await call.answer()

    @r.callback_query(F.data == "u:refs")
    async def u_refs(call: CallbackQuery):
        u = await q_one("SELECT referrals_count FROM users WHERE tenant_id=? AND user_id=?", (tenant_id, call.from_user.id))
        cnt = int(u["referrals_count"]) if u else 0
        await call.message.answer(f"👥 Total referrals: <b>{cnt}</b>")
        await call.answer()

    @r.callback_query(F.data == "u:link")
    async def u_link(call: CallbackQuery):
        me = await call.bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{call.from_user.id}"
        await call.message.answer(f"🔗 Your refer link:\n{link}")
        await call.answer()

    @r.callback_query(F.data == "u:bonus")
    async def u_bonus(call: CallbackQuery):
        await call.message.answer("🎁 Bonus feature not configured in this build.")
        await call.answer()

    @r.callback_query(F.data == "u:stats")
    async def u_stats(call: CallbackQuery):
        rows = await q_one("SELECT COUNT(*) AS users FROM users WHERE tenant_id=?", (tenant_id,))
        await call.message.answer(f"📊 Total users: <b>{rows['users'] if rows else 0}</b>")
        await call.answer()

    @r.callback_query(F.data == "u:wd")
    async def u_withdraw(call: CallbackQuery):
        s = await get_settings(tenant_id)

        if int(s["withdraw_on"]) != 1:
            await call.message.answer("❌ Withdraw is disabled.")
            return await call.answer()

        u = await q_one("SELECT balance FROM users WHERE tenant_id=? AND user_id=?", (tenant_id, call.from_user.id))
        bal = float(u["balance"]) if u else 0.0

        if bal < float(s["min_withdraw"]):
            await call.message.answer(f"❌ Minimum withdraw: {s['min_withdraw']} {s['currency']}")
            return await call.answer()

        if bal > float(s["max_withdraw"]):
            await call.message.answer(f"❌ Maximum withdraw: {s['max_withdraw']} {s['currency']}")
            return await call.answer()

        tax = float(s["withdraw_tax"])
        net = max(0.0, bal - tax)

        # confirmation toggle
        if int(s["withdraw_confirmation_on"]) == 1:
            await call.message.answer(
                f"⚠️ Confirm withdraw?\nAmount: {bal}\nTax: {tax}\nNet: {net}\n\nSend /start then press withdraw again to proceed."
            )
            return await call.answer()

        # create payout request
        await exec_sql(
            "INSERT INTO payouts(tenant_id,user_id,amount,method,status) VALUES(?,?,?,?,?)",
            (tenant_id, call.from_user.id, net, s["payout_mode"], "requested")
        )
        await exec_sql("UPDATE users SET balance=0 WHERE tenant_id=? AND user_id=?",
                       (tenant_id, call.from_user.id))

        await call.message.answer("✅ Withdraw request submitted. Admin will process it.")
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
            f"❗ Join required: {MASTER_FORCE_JOIN_CHANNEL}\nThen send /start again."
        )
    await message.answer("🧠 Referral Master Panel", reply_markup=kb_master_home())

@master_router.callback_query(F.data == "m:help")
async def m_help(call: CallbackQuery):
    await call.message.answer(
        "How it works:\n"
        "1) Create a bot in BotFather\n"
        "2) Send token here\n"
        "3) Your bot starts on this server\n"
        "4) Open your bot and send /adminpanel\n\n"
        "Note: Channel verification works best if the tenant bot is admin in required channels."
    )
    await call.answer()

@master_router.callback_query(F.data == "m:list_bots")
async def m_list(call: CallbackQuery):
    rows = await q_all(
        "SELECT id, bot_username, plan, is_active FROM tenants WHERE owner_user_id=?",
        (call.from_user.id,)
    )
    if not rows:
        await call.message.answer("No bots registered.")
        return await call.answer()

    lines = []
    for r in rows:
        lines.append(f"#{r['id']} • @{r['bot_username'] or 'unknown'} • plan={r['plan']} • active={r['is_active']}")
    await call.message.answer("\n".join(lines))
    await call.answer()

@master_router.callback_query(F.data == "m:new_bot")
async def m_newbot(call: CallbackQuery):
    await call.message.answer("Send BotFather token here (format: 123456:ABC...).")
    await call.answer()

@master_router.message(F.text)
async def master_token_receive(message: Message):
    txt = (message.text or "").strip()
    if ":" not in txt or len(txt) < 30:
        return

    ok = await is_joined_master(message.bot, message.from_user.id)
    if not ok:
        return await message.answer(f"Join required: {MASTER_FORCE_JOIN_CHANNEL}")

    token = txt

    exists = await q_one("SELECT id FROM tenants WHERE bot_token=?", (token,))
    if exists:
        return await message.answer("❌ Token already registered.")

    # insert tenant
    await exec_sql(
        "INSERT INTO tenants(owner_user_id, bot_token, plan, is_active) VALUES(?,?,?,1)",
        (message.from_user.id, token, "basic")
    )
    t = await q_one("SELECT id FROM tenants WHERE bot_token=?", (token,))
    tenant_id = int(t["id"])

    # fetch username
    temp_bot = Bot(token=token)
    try:
        me = await temp_bot.get_me()
        await exec_sql("UPDATE tenants SET bot_username=? WHERE id=?", (me.username, tenant_id))
        await message.answer(f"✅ Registered: @{me.username}\nOpen that bot and send /adminpanel")
    except Exception:
        await message.answer("✅ Registered, but failed to read bot username.")
    finally:
        await temp_bot.session.close()

    await tenant_runtime.start_tenant(tenant_id, token)


# =========================
# App main
# =========================
async def main():
    await init_db()

    # start all active tenants
    tenants = await q_all("SELECT id, bot_token FROM tenants WHERE is_active=1")
    for t in tenants:
        await tenant_runtime.start_tenant(int(t["id"]), t["bot_token"])

    # start master
    bot = Bot(
        token=MASTER_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(master_router)

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
