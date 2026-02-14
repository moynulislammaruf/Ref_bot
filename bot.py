import os
import time
import sqlite3
from contextlib import contextmanager
from typing import Optional, Dict, Any, List, Set, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing. Set it in environment variables.")

DB_PATH = os.getenv("DB_PATH", "data.db").strip()

# Comma separated admin user ids, e.g. "123,456"
ENV_ADMIN_IDS: Set[int] = set()
_raw_admins = os.getenv("ADMIN_IDS", "").strip()
if _raw_admins:
    for x in _raw_admins.split(","):
        x = x.strip()
        if x.isdigit():
            ENV_ADMIN_IDS.add(int(x))


# =========================
# SQLITE DB LAYER
# =========================
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  referred_by INTEGER,
  balance REAL NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bans (
  user_id INTEGER PRIMARY KEY
);
"""

DEFAULT_SETTINGS = {
    "bot_on": "0",
    "maintenance_on": "0",
    "withdraw_mode": "0",
    "withdraw_confirmation": "0",
    "min_withdraw": "NOT SET",
    "max_withdraw": "NOT SET",
    "withdraw_tax": "NOT SET",
    "per_refer_amount": "NOT SET",
    "spin_reward": "NOT SET",
    "payout_currency": "None",
    "bot_currency": "None",
    "bot_channel": "None",          # mandatory join channel
    "withdraw_channel": "None",      # show-only / optional
    "withdraw_time": "NOT SET",
    "home_text": "👋 Hello! Welcome to the Main Menu!\n\nDiscover exciting features, earn rewards, and manage everything easily.\nTap below to begin your journey! 🚀",
    "api_key": "NOT SET",

    # extra toggles
    "device_verification": "0",
    "new_user_notification": "0",
    "share_contact": "0",
    "math_cap": "0",
    "animal_cap": "0",
    "color_cap": "0",
    "fruit_cap": "0",
    "emoji_cap": "0",
    "shape_captcha": "0",

    # persistent runtime admins (CSV)
    "extra_admins": "",  # "123,456"
}


class DB:
    def __init__(self, path: str):
        self.path = path
        self._init()

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def _init(self):
        with self.conn() as c:
            c.executescript(SCHEMA)
            for k, v in DEFAULT_SETTINGS.items():
                c.execute(
                    "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                    (k, v),
                )

    def get_setting(self, key: str) -> str:
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else ""

    def set_setting(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def toggle_setting_01(self, key: str) -> str:
        cur = self.get_setting(key)
        nxt = "0" if cur == "1" else "1"
        self.set_setting(key, nxt)
        return nxt

    def is_banned(self, user_id: int) -> bool:
        with self.conn() as c:
            row = c.execute("SELECT 1 FROM bans WHERE user_id=?", (user_id,)).fetchone()
            return bool(row)

    def ban(self, user_id: int) -> None:
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO bans(user_id) VALUES(?)", (user_id,))

    def unban(self, user_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM bans WHERE user_id=?", (user_id,))

    def user_exists(self, user_id: int) -> bool:
        with self.conn() as c:
            row = c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
            return bool(row)

    def ensure_user(self, user_id: int, ts: int, referred_by: Optional[int] = None) -> bool:
        """
        Returns True if created new user, False if already existed.
        """
        existed = self.user_exists(user_id)
        if existed:
            return False
        with self.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO users(user_id, referred_by, created_at) VALUES(?,?,?)",
                (user_id, referred_by, ts),
            )
        return True

    def add_balance(self, user_id: int, amount: float) -> None:
        # ensure user exists
        if not self.user_exists(user_id):
            self.ensure_user(user_id, int(time.time()), None)
        with self.conn() as c:
            c.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))

    def get_balance(self, user_id: int) -> float:
        with self.conn() as c:
            row = c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
            return float(row[0]) if row else 0.0

    def get_ban_list(self, limit: int = 50) -> List[int]:
        with self.conn() as c:
            rows = c.execute("SELECT user_id FROM bans ORDER BY user_id LIMIT ?", (limit,)).fetchall()
            return [int(r[0]) for r in rows]

    def get_all_user_ids(self) -> List[int]:
        with self.conn() as c:
            rows = c.execute("SELECT user_id FROM users").fetchall()
            return [int(r[0]) for r in rows]


db = DB(DB_PATH)

# =========================
# ADMIN IDS (ENV + DB persistent)
# =========================
def parse_csv_ids(raw: str) -> Set[int]:
    out: Set[int] = set()
    raw = (raw or "").strip()
    if not raw:
        return out
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            out.add(int(x))
    return out

def get_all_admins() -> Set[int]:
    extra = parse_csv_ids(db.get_setting("extra_admins"))
    return set(ENV_ADMIN_IDS) | extra

def is_admin(user_id: int) -> bool:
    return user_id in get_all_admins()

def set_extra_admins(ids_set: Set[int]) -> None:
    # persist only "extra_admins" (ENV admins are fixed)
    csv = ",".join(str(x) for x in sorted(ids_set))
    db.set_setting("extra_admins", csv)

def add_admin_id(uid: int) -> None:
    extra = parse_csv_ids(db.get_setting("extra_admins"))
    extra.add(uid)
    set_extra_admins(extra)

def remove_admin_id(uid: int) -> None:
    extra = parse_csv_ids(db.get_setting("extra_admins"))
    extra.discard(uid)
    set_extra_admins(extra)

# =========================
# UI HELPERS
# =========================
def yn_emoji(v01: str) -> str:
    return "✅ On" if v01 == "1" else "🚫 Off"

def status_line(name: str, val: str) -> str:
    return f"• <b>{name}</b>: <code>{val}</code>"

def user_main_menu() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("MY FUNDS-", callback_data="u:funds"),
         InlineKeyboardButton("PARTNERS-🤝", callback_data="u:partners")],
        [InlineKeyboardButton("INVITE & EARN-💎", callback_data="u:invite"),
         InlineKeyboardButton("ANALYTICS-📈", callback_data="u:analytics")],
        [InlineKeyboardButton("LUCKY SPIN-", callback_data="u:spin"),
         InlineKeyboardButton("HELP & SUPPORT 💬", callback_data="u:support")],
        [InlineKeyboardButton("COMPLETE TASK & EARN 🎯", callback_data="u:tasks")],
    ]
    return InlineKeyboardMarkup(kb)

def admin_panel_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("📢 WITHDRAW CHANNEL", callback_data="a:set_withdraw_channel")],
        [InlineKeyboardButton("👑 TRANSFER OWNERSHIP", callback_data="a:transfer_owner"),
         InlineKeyboardButton("🧾 SET WITHDRAW TAX", callback_data="a:set_withdraw_tax")],

        [InlineKeyboardButton(f"📲 DEVICE VERIFICATION ~ {yn_emoji(db.get_setting('device_verification'))}",
                              callback_data="a:toggle_device_verification")],

        [InlineKeyboardButton("👤 ADD ADMIN", callback_data="a:add_admin"),
         InlineKeyboardButton("👤 REMOVE ADMIN", callback_data="a:remove_admin")],

        [InlineKeyboardButton(f"🤖 BOT STATUS ~ {yn_emoji(db.get_setting('bot_on'))}", callback_data="a:toggle_bot_on"),
         InlineKeyboardButton(f"🛠 MAINTENANCE MODE ~ {yn_emoji(db.get_setting('maintenance_on'))}", callback_data="a:toggle_maintenance")],

        [InlineKeyboardButton("🚫 BAN USER", callback_data="a:ban_user"),
         InlineKeyboardButton("✅ UNBAN USER", callback_data="a:unban_user")],

        [InlineKeyboardButton("📌 SET BOT CHANNEL", callback_data="a:set_bot_channel"),
         InlineKeyboardButton("❌ REMOVE BOT CHANNEL", callback_data="a:remove_bot_channel")],

        [InlineKeyboardButton("✅ SET PER REFER", callback_data="a:set_per_refer")],

        [InlineKeyboardButton("➕ ADD BALANCE", callback_data="a:add_balance"),
         InlineKeyboardButton("➖ REMOVE BALANCE", callback_data="a:remove_balance")],

        [InlineKeyboardButton("⬇️ SET MIN WITHDRAW", callback_data="a:set_min_withdraw"),
         InlineKeyboardButton("⬆️ SET MAX WITHDRAW", callback_data="a:set_max_withdraw")],

        [InlineKeyboardButton("📣 ULTRA BROADCAST", callback_data="a:broadcast"),
         InlineKeyboardButton("💬 TALK WITH USER", callback_data="a:talk_user")],

        [InlineKeyboardButton("💱 SET PAYOUT CURRENCY", callback_data="a:set_payout_currency"),
         InlineKeyboardButton("💱 SET BOT CURRENCY", callback_data="a:set_bot_currency")],

        [InlineKeyboardButton(f"✅ WITHDRAW CONFIRMATION ~ {yn_emoji(db.get_setting('withdraw_confirmation'))}",
                              callback_data="a:toggle_withdraw_confirmation")],

        [InlineKeyboardButton("🔎 FIND USER DETAILS", callback_data="a:user_details"),
         InlineKeyboardButton("📋 CHECK BAN LIST", callback_data="a:ban_list")],

        [InlineKeyboardButton("🔑 SET API KEY", callback_data="a:set_api_key"),
         InlineKeyboardButton("🎰 SET SPIN REWARD", callback_data="a:set_spin_reward")],

        [InlineKeyboardButton(f"🆕 NEW USER NOTIFICATION ~ {yn_emoji(db.get_setting('new_user_notification'))}",
                              callback_data="a:toggle_new_user_notification")],

        [InlineKeyboardButton(f"🔢 Math cap ~ {yn_emoji(db.get_setting('math_cap'))}", callback_data="a:toggle_math_cap"),
         InlineKeyboardButton(f"📞 Share Contact ~ {yn_emoji(db.get_setting('share_contact'))}", callback_data="a:toggle_share_contact")],

        [InlineKeyboardButton(f"🐾 Animal cap~ {yn_emoji(db.get_setting('animal_cap'))}", callback_data="a:toggle_animal_cap"),
         InlineKeyboardButton(f"🎨 Color cap ~ {yn_emoji(db.get_setting('color_cap'))}", callback_data="a:toggle_color_cap")],

        [InlineKeyboardButton(f"🍏 Fruit Cap ~ {yn_emoji(db.get_setting('fruit_cap'))}", callback_data="a:toggle_fruit_cap"),
         InlineKeyboardButton(f"😃 Emoji Cap ~ {yn_emoji(db.get_setting('emoji_cap'))}", callback_data="a:toggle_emoji_cap")],

        [InlineKeyboardButton(f"🔷 SHAPE CAPTCHA ~ {yn_emoji(db.get_setting('shape_captcha'))}",
                              callback_data="a:toggle_shape_captcha")],

        [InlineKeyboardButton("⏱ Withdraw time", callback_data="a:set_withdraw_time"),
         InlineKeyboardButton("🏠 SET HOME TEXT", callback_data="a:set_home_text")],
    ]
    return InlineKeyboardMarkup(kb)

def build_admin_panel_text() -> str:
    admins = sorted(get_all_admins())
    main_owner = str(admins[0]) if admins else "NOT SET"

    lines = []
    lines.append("🛡 <b>WELCOME TO ADMIN PANEL</b>\n")
    lines.append("🔎 <b>Review Bot Details</b>\n")

    lines.append(status_line("Main Owner", main_owner))
    lines.append(status_line("Admins", ",".join(map(str, admins)) if admins else "None"))

    lines.append(status_line("Bot ON/OFF", yn_emoji(db.get_setting("bot_on"))))
    lines.append(status_line("Maintenance ON/OFF", yn_emoji(db.get_setting("maintenance_on"))))
    lines.append(status_line("Withdraw Mode", yn_emoji(db.get_setting("withdraw_mode"))))
    lines.append(status_line("Withdraw Confirmation", yn_emoji(db.get_setting("withdraw_confirmation"))))
    lines.append(status_line("Minimum Withdraw", db.get_setting("min_withdraw")))
    lines.append(status_line("Maximum Withdraw", db.get_setting("max_withdraw")))
    lines.append(status_line("Withdraw Tax Amount", db.get_setting("withdraw_tax")))
    lines.append(status_line("Per Refer Amount", db.get_setting("per_refer_amount")))
    lines.append(status_line("Spin Reward", db.get_setting("spin_reward")))
    lines.append(status_line("Payout Currency", db.get_setting("payout_currency")))
    lines.append(status_line("Bot Currency", db.get_setting("bot_currency")))
    lines.append(status_line("Bot Channel", db.get_setting("bot_channel")))
    lines.append(status_line("Withdraw Channel", db.get_setting("withdraw_channel")))
    lines.append(status_line("API Key", db.get_setting("api_key")))
    lines.append(status_line("Withdraw time", db.get_setting("withdraw_time")))
    return "\n".join(lines)

# =========================
# MANDATORY JOIN CHECK
# =========================
def normalize_channel(value: str) -> Optional[str]:
    """
    Accepts @channel or -100xxxx id.
    Returns normalized string or None if not set.
    """
    v = (value or "").strip()
    if not v or v.lower() == "none":
        return None
    return v

async def is_user_joined_required_channel(
    context: ContextTypes.DEFAULT_TYPE,
    channel: str,
    user_id: int
) -> Tuple[bool, Optional[str]]:
    """
    Returns (joined, error_message_if_any).
    joined=True if status is member/administrator/creator.
    """
    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
        status = getattr(member, "status", "")
        if status in ("member", "administrator", "creator"):
            return True, None
        return False, None
    except Forbidden:
        return False, "Bot has no access to check members. Make the bot an admin in the channel."
    except BadRequest as e:
        return False, f"BadRequest: {e}"
    except Exception as e:
        return False, f"Error: {e}"

def join_required_markup(channel: str) -> InlineKeyboardMarkup:
    # URL for public channels: https://t.me/<username> if channel startswith @
    url = None
    if channel.startswith("@"):
        url = f"https://t.me/{channel[1:]}"
    kb = []
    if url:
        kb.append([InlineKeyboardButton("📢 Join Channel", url=url)])
    else:
        # For private channels with invite link, admin should store invite link instead of id
        kb.append([InlineKeyboardButton("📢 Join Channel", callback_data="noop")])
    kb.append([InlineKeyboardButton("✅ Joined", callback_data="u:joined_check")])
    return InlineKeyboardMarkup(kb)

async def enforce_join_if_needed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Returns True if user can proceed, False if blocked due to not joined.
    """
    user = update.effective_user
    if not user:
        return False
    uid = user.id

    # Admin bypass
    if is_admin(uid):
        return True

    channel = normalize_channel(db.get_setting("bot_channel"))
    if not channel:
        return True

    joined, err = await is_user_joined_required_channel(context, channel, uid)
    if joined:
        return True

    msg = "⚠️ To use this bot, you must join our channel first."
    if err:
        msg += f"\n\n<b>Admin note:</b> <code>{err}</code>"

    # Respond appropriately depending on update type
    if update.message:
        await update.message.reply_text(
            msg,
            parse_mode=ParseMode.HTML,
            reply_markup=join_required_markup(channel),
            disable_web_page_preview=True
        )
    elif update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                msg,
                parse_mode=ParseMode.HTML,
                reply_markup=join_required_markup(channel),
                disable_web_page_preview=True
            )
        except Exception:
            await update.callback_query.message.reply_text(
                msg,
                parse_mode=ParseMode.HTML,
                reply_markup=join_required_markup(channel),
                disable_web_page_preview=True
            )
    return False

# =========================
# GUARD
# =========================
async def guard_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    uid = user.id

    if db.is_banned(uid):
        # optional: show message
        if update.message:
            await update.message.reply_text("🚫 You are banned.")
        elif update.callback_query:
            await update.callback_query.answer("You are banned.", show_alert=True)
        return False

    # Admin bypass for ON/OFF and maintenance
    if not is_admin(uid):
        if db.get_setting("bot_on") != "1":
            if update.message:
                await update.message.reply_text("🚫 Bot is currently OFF. Please try later.")
            elif update.callback_query:
                await update.callback_query.answer("Bot is OFF", show_alert=True)
            return False

        if db.get_setting("maintenance_on") == "1":
            if update.message:
                await update.message.reply_text("🛠 Bot is in maintenance mode. Please try later.")
            elif update.callback_query:
                await update.callback_query.answer("Maintenance mode", show_alert=True)
            return False

    # Mandatory Join enforcement (after ON/OFF & maintenance)
    ok = await enforce_join_if_needed(update, context)
    return ok

# =========================
# STATE (admin pending inputs)
# =========================
PENDING_KEY = "pending_action"

def set_pending(context: ContextTypes.DEFAULT_TYPE, action: str, meta: Optional[Dict[str, Any]] = None):
    context.user_data[PENDING_KEY] = {"action": action, "meta": meta or {}}

def clear_pending(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(PENDING_KEY, None)

def get_pending(context: ContextTypes.DEFAULT_TYPE):
    return context.user_data.get(PENDING_KEY)

# =========================
# COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # guard covers bot off/maintenance/ban + join requirement
    if not await guard_user(update, context):
        return

    user = update.effective_user
    uid = user.id
    ts = int(time.time())

    # referral: /start 12345
    referred_by = None
    if context.args and context.args[0].isdigit():
        referred_by = int(context.args[0])
        if referred_by == uid:
            referred_by = None

    created_new = db.ensure_user(uid, ts, referred_by=referred_by)

    # notify only for brand new user
    if created_new and db.get_setting("new_user_notification") == "1":
        for admin_id in get_all_admins():
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"🆕 New user started: <code>{uid}</code>",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

    home_text = db.get_setting("home_text")
    await update.message.reply_text(home_text, reply_markup=user_main_menu())

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_user(update, context):
        return
    home_text = db.get_setting("home_text")
    await update.message.reply_text(home_text, reply_markup=user_main_menu())

async def adminpanel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("❌ You are not authorized.")
        return

    text = build_admin_panel_text()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_panel_kb())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_pending(context)
    await update.message.reply_text("✅ Cancelled.")

# =========================
# USER CALLBACKS
# =========================
async def user_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_user(update, context):
        return

    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id

    if data == "u:joined_check":
        # after pressing "✅ Joined", re-check and if OK show menu
        ok = await enforce_join_if_needed(update, context)
        if not ok:
            return
        home_text = db.get_setting("home_text")
        await q.edit_message_text(home_text, reply_markup=user_main_menu())
        return

    if data == "u:funds":
        bal = db.get_balance(uid)
        await q.edit_message_text(
            f"💰 <b>My Funds</b>\n\nBalance: <code>{bal:.4f}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=user_main_menu(),
        )
    elif data == "u:partners":
        await q.edit_message_text(
            "🤝 <b>Partners</b>\n\nComing soon.",
            parse_mode=ParseMode.HTML,
            reply_markup=user_main_menu(),
        )
    elif data == "u:invite":
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start={uid}"
        per_ref = db.get_setting("per_refer_amount")
        await q.edit_message_text(
            f"💎 <b>Invite & Earn</b>\n\nYour link:\n<code>{link}</code>\n\nPer refer: <code>{per_ref}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=user_main_menu(),
            disable_web_page_preview=True,
        )
    elif data == "u:analytics":
        await q.edit_message_text(
            "📈 <b>Analytics</b>\n\nComing soon.",
            parse_mode=ParseMode.HTML,
            reply_markup=user_main_menu(),
        )
    elif data == "u:spin":
        reward = db.get_setting("spin_reward")
        await q.edit_message_text(
            f"🎰 <b>Lucky Spin</b>\n\nSpin reward: <code>{reward}</code>\n\n(Implement spin logic here)",
            parse_mode=ParseMode.HTML,
            reply_markup=user_main_menu(),
        )
    elif data == "u:support":
        await q.edit_message_text(
            "💬 <b>Help & Support</b>\n\nContact admin.",
            parse_mode=ParseMode.HTML,
            reply_markup=user_main_menu(),
        )
    elif data == "u:tasks":
        await q.edit_message_text(
            "🎯 <b>Complete Task & Earn</b>\n\nTasks module is ready to implement.",
            parse_mode=ParseMode.HTML,
            reply_markup=user_main_menu(),
        )
    else:
        await q.edit_message_text("Unknown action.", reply_markup=user_main_menu())

# =========================
# ADMIN CALLBACKS
# =========================
async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    if not user or not is_admin(user.id):
        await q.answer("Not authorized", show_alert=True)
        return

    await q.answer()
    data = q.data

    # Toggle actions
    toggle_map = {
        "a:toggle_bot_on": "bot_on",
        "a:toggle_maintenance": "maintenance_on",
        "a:toggle_withdraw_confirmation": "withdraw_confirmation",
        "a:toggle_device_verification": "device_verification",
        "a:toggle_new_user_notification": "new_user_notification",
        "a:toggle_share_contact": "share_contact",
        "a:toggle_math_cap": "math_cap",
        "a:toggle_animal_cap": "animal_cap",
        "a:toggle_color_cap": "color_cap",
        "a:toggle_fruit_cap": "fruit_cap",
        "a:toggle_emoji_cap": "emoji_cap",
        "a:toggle_shape_captcha": "shape_captcha",
    }
    if data in toggle_map:
        key = toggle_map[data]
        db.toggle_setting_01(key)
        text = build_admin_panel_text()
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_panel_kb())
        return

    prompt_actions = {
        "a:set_withdraw_tax": ("Set Withdraw Tax", "Send withdraw tax amount (example: 5 or 2.5)"),
        "a:set_min_withdraw": ("Set Minimum Withdraw", "Send minimum withdraw amount (example: 10)"),
        "a:set_max_withdraw": ("Set Maximum Withdraw", "Send maximum withdraw amount (example: 100)"),
        "a:set_per_refer": ("Set Per Refer Amount", "Send per refer reward (example: 1)"),
        "a:set_spin_reward": ("Set Spin Reward", "Send spin reward amount (example: 0.5)"),
        "a:set_payout_currency": ("Set Payout Currency", "Send payout currency (example: USDT)"),
        "a:set_bot_currency": ("Set Bot Currency", "Send bot currency (example: USDT)"),
        "a:set_bot_channel": ("Set Bot Channel", "Send channel @username OR invite link (recommended). Example: @mychannel"),
        "a:set_withdraw_channel": ("Set Withdraw Channel", "Send channel @username (example: @withdraws)"),
        "a:set_withdraw_time": ("Set Withdraw Time", "Send withdraw time/rules (example: 10AM-6PM)"),
        "a:set_home_text": ("Set Home Text", "Send new home text (will show on /start)"),
        "a:set_api_key": ("Set API Key", "Send API key/value (stored in DB)"),
        "a:ban_user": ("Ban User", "Send user id to BAN (numbers only)"),
        "a:unban_user": ("Unban User", "Send user id to UNBAN (numbers only)"),
        "a:add_balance": ("Add Balance", "Send: <user_id> <amount> (example: 12345 10)"),
        "a:remove_balance": ("Remove Balance", "Send: <user_id> <amount> (example: 12345 3)"),
        "a:user_details": ("Find User Details", "Send user id to lookup"),
        "a:broadcast": ("Ultra Broadcast", "Send broadcast message text"),
        "a:add_admin": ("Add Admin", "Send user id to add admin (persists in DB)"),
        "a:remove_admin": ("Remove Admin", "Send user id to remove admin (DB admins only)"),
        "a:transfer_owner": ("Transfer Ownership", "Send new owner admin user id (adds to DB admins)"),
        "a:talk_user": ("Talk With User", "Send: <user_id> <message>"),
    }

    if data in prompt_actions:
        title, msg = prompt_actions[data]
        set_pending(context, action=data)
        await q.message.reply_text(
            f"📝 <b>{title}</b>\n\n{msg}\n\nSend /cancel to cancel.",
            parse_mode=ParseMode.HTML
        )
        return

    if data == "a:remove_bot_channel":
        db.set_setting("bot_channel", "None")
        text = build_admin_panel_text()
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_panel_kb())
        return

    if data == "a:ban_list":
        bl = db.get_ban_list()
        if not bl:
            await q.message.reply_text("✅ Ban list is empty.")
        else:
            txt = "📋 <b>Banned Users</b>\n\n" + "\n".join([f"• <code>{x}</code>" for x in bl])
            await q.message.reply_text(txt, parse_mode=ParseMode.HTML)
        return

    await q.message.reply_text("⚠️ Unknown admin action.")

# =========================
# ADMIN MESSAGE INPUT HANDLER
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if not is_admin(user.id):
        return

    pending = get_pending(context)
    if not pending:
        return

    action = pending["action"]
    text = (update.message.text or "").strip()

    try:
        if action == "a:set_withdraw_tax":
            db.set_setting("withdraw_tax", text)
            await update.message.reply_text("✅ Withdraw tax updated.")

        elif action == "a:set_min_withdraw":
            db.set_setting("min_withdraw", text)
            await update.message.reply_text("✅ Minimum withdraw updated.")

        elif action == "a:set_max_withdraw":
            db.set_setting("max_withdraw", text)
            await update.message.reply_text("✅ Maximum withdraw updated.")

        elif action == "a:set_per_refer":
            db.set_setting("per_refer_amount", text)
            await update.message.reply_text("✅ Per refer amount updated.")

        elif action == "a:set_spin_reward":
            db.set_setting("spin_reward", text)
            await update.message.reply_text("✅ Spin reward updated.")

        elif action == "a:set_payout_currency":
            db.set_setting("payout_currency", text)
            await update.message.reply_text("✅ Payout currency updated.")

        elif action == "a:set_bot_currency":
            db.set_setting("bot_currency", text)
            await update.message.reply_text("✅ Bot currency updated.")

        elif action == "a:set_bot_channel":
            # allow @channel OR invite link; enforce_join uses it directly
            db.set_setting("bot_channel", text)
            await update.message.reply_text("✅ Bot channel updated. (Mandatory join will be enforced)")

        elif action == "a:set_withdraw_channel":
            db.set_setting("withdraw_channel", text)
            await update.message.reply_text("✅ Withdraw channel updated.")

        elif action == "a:set_withdraw_time":
            db.set_setting("withdraw_time", text)
            await update.message.reply_text("✅ Withdraw time updated.")

        elif action == "a:set_home_text":
            db.set_setting("home_text", text)
            await update.message.reply_text("✅ Home text updated.")

        elif action == "a:set_api_key":
            db.set_setting("api_key", text)
            await update.message.reply_text("✅ API key stored.")

        elif action == "a:ban_user":
            if not text.isdigit():
                await update.message.reply_text("❌ Invalid user id.")
            else:
                db.ban(int(text))
                await update.message.reply_text("✅ User banned.")

        elif action == "a:unban_user":
            if not text.isdigit():
                await update.message.reply_text("❌ Invalid user id.")
            else:
                db.unban(int(text))
                await update.message.reply_text("✅ User unbanned.")

        elif action == "a:add_balance":
            parts = text.split()
            if len(parts) != 2 or (not parts[0].isdigit()):
                await update.message.reply_text("❌ Format: user_id amount")
            else:
                uid = int(parts[0])
                amt = float(parts[1])
                db.add_balance(uid, amt)
                await update.message.reply_text(f"✅ Added {amt} to {uid}.")

        elif action == "a:remove_balance":
            parts = text.split()
            if len(parts) != 2 or (not parts[0].isdigit()):
                await update.message.reply_text("❌ Format: user_id amount")
            else:
                uid = int(parts[0])
                amt = float(parts[1])
                db.add_balance(uid, -abs(amt))
                await update.message.reply_text(f"✅ Removed {amt} from {uid}.")

        elif action == "a:user_details":
            if not text.isdigit():
                await update.message.reply_text("❌ Invalid user id.")
            else:
                uid = int(text)
                bal = db.get_balance(uid)
                banned = db.is_banned(uid)
                await update.message.reply_text(
                    f"🔎 <b>User Details</b>\n\n"
                    f"ID: <code>{uid}</code>\n"
                    f"Balance: <code>{bal:.4f}</code>\n"
                    f"Banned: <code>{'YES' if banned else 'NO'}</code>",
                    parse_mode=ParseMode.HTML
                )

        elif action == "a:broadcast":
            # send to all known users
            user_ids = db.get_all_user_ids()
            sent = 0
            failed = 0
            for uid in user_ids:
                try:
                    await context.bot.send_message(uid, f"📣 {text}")
                    sent += 1
                except Exception:
                    failed += 1
            await update.message.reply_text(f"✅ Broadcast done. Sent: {sent}, Failed: {failed}")

        elif action == "a:talk_user":
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or (not parts[0].isdigit()):
                await update.message.reply_text("❌ Format: user_id message")
            else:
                uid = int(parts[0])
                msg = parts[1]
                await context.bot.send_message(uid, f"📩 Admin:\n\n{msg}")
                await update.message.reply_text("✅ Sent.")

        elif action == "a:add_admin":
            if not text.isdigit():
                await update.message.reply_text("❌ Invalid user id.")
            else:
                add_admin_id(int(text))
                await update.message.reply_text("✅ Admin added (saved in DB).")

        elif action == "a:remove_admin":
            if not text.isdigit():
                await update.message.reply_text("❌ Invalid user id.")
            else:
                remove_admin_id(int(text))
                await update.message.reply_text("✅ Admin removed (DB admins).")

        elif action == "a:transfer_owner":
            if not text.isdigit():
                await update.message.reply_text("❌ Invalid user id.")
            else:
                add_admin_id(int(text))
                await update.message.reply_text("✅ Ownership transferred (added to DB admins).")

        else:
            await update.message.reply_text("⚠️ Unknown pending action.")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

    finally:
        clear_pending(context)
        # show panel again
        textp = build_admin_panel_text()
        await update.message.reply_text(textp, parse_mode=ParseMode.HTML, reply_markup=admin_panel_kb())

# =========================
# ROUTER FOR CALLBACKS
# =========================
async def callbacks_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    if q.data.startswith("u:"):
        await user_callbacks(update, context)
    elif q.data.startswith("a:"):
        await admin_callbacks(update, context)
    elif q.data == "noop":
        await q.answer("Ask admin for a public channel or invite link.", show_alert=True)
    else:
        await q.answer()

# =========================
# MAIN
# =========================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("adminpanel", adminpanel))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(CallbackQueryHandler(callbacks_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
