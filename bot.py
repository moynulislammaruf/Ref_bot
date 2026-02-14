import os
import re
import asyncio
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    Text,
    func,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# -----------------------------
# ENV / CONFIG
# -----------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = set()
_admin_raw = os.getenv("ADMIN_IDS", "").strip()  # e.g. "12345,67890"
if _admin_raw:
    for x in _admin_raw.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

# Economy settings (can be changed from admin panel; defaults are fallback)
DEFAULT_REF_REWARD = int(os.getenv("REF_REWARD", "5"))          # coins per referral
DEFAULT_MIN_WITHDRAW = int(os.getenv("MIN_WITHDRAW", "50"))     # coins
CURRENCY_LABEL = os.getenv("CURRENCY_LABEL", "coins")          # label only

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL:
    # Railway Postgres often provides postgres:// ; SQLAlchemy prefers postgresql+psycopg2://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
    elif DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
else:
    DATABASE_URL = "sqlite:///bot.db"

Base = declarative_base()
engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# -----------------------------
# DB MODELS
# -----------------------------
class Settings(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True)
    ref_reward = Column(Integer, default=DEFAULT_REF_REWARD)
    min_withdraw = Column(Integer, default=DEFAULT_MIN_WITHDRAW)
    updated_at = Column(DateTime, default=datetime.utcnow)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)  # telegram user_id
    username = Column(String(64), nullable=True)
    first_name = Column(String(128), nullable=True)
    joined_at = Column(DateTime, default=datetime.utcnow)
    referred_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    referrals = Column(Integer, default=0)
    balance = Column(Integer, default=0)
    is_banned = Column(Boolean, default=False)

    referrer = relationship("User", remote_side=[id], uselist=False)

class WithdrawRequest(Base):
    __tablename__ = "withdraw_requests"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Integer, nullable=False)
    payout_method = Column(String(64), default="manual")
    payout_address = Column(String(256), nullable=False)
    status = Column(String(32), default="pending")  # pending/approved/rejected
    created_at = Column(DateTime, default=datetime.utcnow)
    reviewed_at = Column(DateTime, nullable=True)
    admin_note = Column(Text, nullable=True)

    user = relationship("User")

def init_db():
    Base.metadata.create_all(bind=engine)
    # ensure one settings row
    db = SessionLocal()
    try:
        s = db.query(Settings).first()
        if not s:
            s = Settings(ref_reward=DEFAULT_REF_REWARD, min_withdraw=DEFAULT_MIN_WITHDRAW)
            db.add(s)
            db.commit()
    finally:
        db.close()

def get_settings(db) -> Settings:
    s = db.query(Settings).first()
    if not s:
        s = Settings(ref_reward=DEFAULT_REF_REWARD, min_withdraw=DEFAULT_MIN_WITHDRAW)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s

# -----------------------------
# UI HELPERS
# -----------------------------
def main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("👤 My Account", callback_data="me"),
         InlineKeyboardButton("🔗 Referral Link", callback_data="reflink")],
        [InlineKeyboardButton("💸 Withdraw", callback_data="withdraw"),
         InlineKeyboardButton("📜 Rules", callback_data="rules")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def admin_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Stats", callback_data="a_stats"),
         InlineKeyboardButton("📣 Broadcast", callback_data="a_broadcast")],
        [InlineKeyboardButton("💰 Set Ref Reward", callback_data="a_set_reward"),
         InlineKeyboardButton("🏧 Set Min Withdraw", callback_data="a_set_minw")],
        [InlineKeyboardButton("🧾 Pending Withdraws", callback_data="a_withdraws")],
        [InlineKeyboardButton("⛔ Ban User", callback_data="a_ban"),
         InlineKeyboardButton("✅ Unban User", callback_data="a_unban")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(rows)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def safe_user_tag(u: User) -> str:
    if u.username:
        return f"@{u.username} ({u.id})"
    name = (u.first_name or "User").strip()
    return f"{name} ({u.id})"

# -----------------------------
# STATE (simple in-memory steps)
# -----------------------------
# context.user_data keys:
# "await_withdraw": True/False
# "await_broadcast": True/False
# "await_set_reward": True/False
# "await_set_minw": True/False
# "await_ban": True/False
# "await_unban": True/False
# withdraw flow: "w_amount", "w_address"
#
# Format expected from user:
# Withdraw step 1: amount (number)
# step 2: payout address (bkash/nagad/binance etc. text)
#
# -----------------------------

# -----------------------------
# CORE LOGIC
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if not tg_user:
        return

    # Parse referral param
    ref_id: Optional[int] = None
    if context.args:
        raw = context.args[0].strip()
        if raw.isdigit():
            ref_id = int(raw)

    db = SessionLocal()
    try:
        s = get_settings(db)
        u = db.query(User).filter(User.id == tg_user.id).first()
        if not u:
            u = User(
                id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                referred_by=None,
            )

            # Apply referral only for new users, not self-ref, and only if ref exists
            if ref_id and ref_id != tg_user.id:
                ref_u = db.query(User).filter(User.id == ref_id).first()
                if ref_u and not ref_u.is_banned:
                    u.referred_by = ref_u.id
                    # reward referrer
                    ref_u.referrals += 1
                    ref_u.balance += int(s.ref_reward)

            db.add(u)
            db.commit()
        else:
            # update profile data
            u.username = tg_user.username
            u.first_name = tg_user.first_name
            db.commit()

        if u.is_banned:
            await update.message.reply_text("⛔ You are banned.")
            return

        text = (
            "👋 *Welcome!*\n\n"
            f"✅ Invite friends with your referral link and earn *{s.ref_reward} {CURRENCY_LABEL}* per valid referral.\n"
            f"🏧 Minimum withdraw: *{s.min_withdraw} {CURRENCY_LABEL}*\n\n"
            "Use the buttons below:"
        )
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_kb(is_admin=is_admin(tg_user.id)),
        )
    finally:
        db.close()

async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    tg_user = update.effective_user
    if not tg_user:
        return

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == tg_user.id).first()
        if not u:
            # force create
            u = User(id=tg_user.id, username=tg_user.username, first_name=tg_user.first_name)
            db.add(u)
            db.commit()

        if u.is_banned:
            await q.edit_message_text("⛔ You are banned.")
            return

        s = get_settings(db)
        data = q.data

        # reset waiting flags on menu clicks (safe)
        for k in list(context.user_data.keys()):
            if k.startswith("await_") or k.startswith("w_"):
                context.user_data.pop(k, None)

        if data == "back_main":
            await q.edit_message_text(
                "🏠 Main Menu",
                reply_markup=main_menu_kb(is_admin=is_admin(tg_user.id))
            )
            return

        if data == "me":
            text = (
                f"👤 *My Account*\n\n"
                f"🆔 ID: `{u.id}`\n"
                f"👥 Referrals: *{u.referrals}*\n"
                f"💰 Balance: *{u.balance} {CURRENCY_LABEL}*\n"
            )
            await q.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_kb(is_admin=is_admin(tg_user.id))
            )
            return

        if data == "reflink":
            bot_username = (await context.bot.get_me()).username
            link = f"https://t.me/{bot_username}?start={u.id}"
            text = (
                "🔗 *Your Referral Link*\n\n"
                f"{link}\n\n"
                f"Earn *{s.ref_reward} {CURRENCY_LABEL}* per referral."
            )
            await q.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_kb(is_admin=is_admin(tg_user.id))
            )
            return

        if data == "rules":
            text = (
                "📜 *Rules*\n\n"
                "• Only real users count as referrals.\n"
                "• Multiple accounts / fake traffic may be disqualified.\n"
                f"• Min withdraw: *{s.min_withdraw} {CURRENCY_LABEL}*\n\n"
                "Need help? Contact admin."
            )
            await q.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_kb(is_admin=is_admin(tg_user.id))
            )
            return

        if data == "withdraw":
            text = (
                "💸 *Withdraw*\n\n"
                f"Your balance: *{u.balance} {CURRENCY_LABEL}*\n"
                f"Minimum withdraw: *{s.min_withdraw} {CURRENCY_LABEL}*\n\n"
                "Step 1/2: Send withdraw amount (number only)."
            )
            context.user_data["await_withdraw"] = True
            context.user_data["w_step"] = 1
            await q.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_main")]])
            )
            return

        # ---------------- ADMIN ----------------
        if data == "admin":
            if not is_admin(tg_user.id):
                await q.edit_message_text("❌ Not authorized.")
                return
            await q.edit_message_text("🛠 *Admin Panel*", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu_kb())
            return

        if data == "a_stats":
            if not is_admin(tg_user.id):
                await q.edit_message_text("❌ Not authorized.")
                return
            total_users = db.query(func.count(User.id)).scalar() or 0
            banned = db.query(func.count(User.id)).filter(User.is_banned == True).scalar() or 0
            pending_w = db.query(func.count(WithdrawRequest.id)).filter(WithdrawRequest.status == "pending").scalar() or 0
            approved_w = db.query(func.count(WithdrawRequest.id)).filter(WithdrawRequest.status == "approved").scalar() or 0
            text = (
                "📊 *Stats*\n\n"
                f"👥 Total users: *{total_users}*\n"
                f"⛔ Banned users: *{banned}*\n"
                f"🧾 Pending withdraws: *{pending_w}*\n"
                f"✅ Approved withdraws: *{approved_w}*\n"
            )
            await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu_kb())
            return

        if data == "a_broadcast":
            if not is_admin(tg_user.id):
                await q.edit_message_text("❌ Not authorized.")
                return
            context.user_data["await_broadcast"] = True
            await q.edit_message_text(
                "📣 Send the broadcast message text now.\n\n(You can send plain text only in this simple version.)",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin")]])
            )
            return

        if data == "a_set_reward":
            if not is_admin(tg_user.id):
                await q.edit_message_text("❌ Not authorized.")
                return
            context.user_data["await_set_reward"] = True
            await q.edit_message_text(
                f"💰 Current ref reward: {s.ref_reward} {CURRENCY_LABEL}\n\nSend new reward amount (number).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin")]])
            )
            return

        if data == "a_set_minw":
            if not is_admin(tg_user.id):
                await q.edit_message_text("❌ Not authorized.")
                return
            context.user_data["await_set_minw"] = True
            await q.edit_message_text(
                f"🏧 Current min withdraw: {s.min_withdraw} {CURRENCY_LABEL}\n\nSend new minimum withdraw (number).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin")]])
            )
            return

        if data == "a_withdraws":
            if not is_admin(tg_user.id):
                await q.edit_message_text("❌ Not authorized.")
                return
            pending = (
                db.query(WithdrawRequest)
                .filter(WithdrawRequest.status == "pending")
                .order_by(WithdrawRequest.created_at.asc())
                .limit(10)
                .all()
            )
            if not pending:
                await q.edit_message_text("✅ No pending withdraws.", reply_markup=admin_menu_kb())
                return

            lines = ["🧾 *Pending Withdraws* (latest 10)\n"]
            for w in pending:
                lines.append(
                    f"ID: `{w.id}` | User: `{w.user_id}` | Amount: *{w.amount}* | Addr: `{w.payout_address}`"
                )
            lines.append("\nTo review: tap a request below.")
            buttons = []
            for w in pending:
                buttons.append([InlineKeyboardButton(f"Review #{w.id}", callback_data=f"a_wreview:{w.id}")])
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="admin")])
            await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
            return

        if data.startswith("a_wreview:"):
            if not is_admin(tg_user.id):
                await q.edit_message_text("❌ Not authorized.")
                return
            w_id = int(data.split(":")[1])
            w = db.query(WithdrawRequest).filter(WithdrawRequest.id == w_id).first()
            if not w or w.status != "pending":
                await q.edit_message_text("Request not found or already processed.", reply_markup=admin_menu_kb())
                return
            text = (
                f"🧾 *Withdraw Review*\n\n"
                f"Request ID: `{w.id}`\n"
                f"User ID: `{w.user_id}`\n"
                f"Amount: *{w.amount} {CURRENCY_LABEL}*\n"
                f"Address: `{w.payout_address}`\n"
                f"Status: *{w.status}*\n"
                f"Created: {w.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve", callback_data=f"a_wapprove:{w.id}"),
                 InlineKeyboardButton("❌ Reject", callback_data=f"a_wreject:{w.id}")],
                [InlineKeyboardButton("⬅️ Back", callback_data="a_withdraws")]
            ])
            await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            return

        if data.startswith("a_wapprove:"):
            if not is_admin(tg_user.id):
                await q.edit_message_text("❌ Not authorized.")
                return
            w_id = int(data.split(":")[1])
            w = db.query(WithdrawRequest).filter(WithdrawRequest.id == w_id).first()
            if not w or w.status != "pending":
                await q.edit_message_text("Request not found or already processed.", reply_markup=admin_menu_kb())
                return
            w.status = "approved"
            w.reviewed_at = datetime.utcnow()
            w.admin_note = f"Approved by {tg_user.id}"
            db.commit()

            # notify user
            try:
                await context.bot.send_message(
                    chat_id=w.user_id,
                    text=f"✅ Your withdraw request #{w.id} has been *APPROVED*.\n\nAdmin will process payout manually.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass

            await q.edit_message_text(f"✅ Approved request #{w.id}.", reply_markup=admin_menu_kb())
            return

        if data.startswith("a_wreject:"):
            if not is_admin(tg_user.id):
                await q.edit_message_text("❌ Not authorized.")
                return
            w_id = int(data.split(":")[1])
            w = db.query(WithdrawRequest).filter(WithdrawRequest.id == w_id).first()
            if not w or w.status != "pending":
                await q.edit_message_text("Request not found or already processed.", reply_markup=admin_menu_kb())
                return

            # refund back to user balance
            u2 = db.query(User).filter(User.id == w.user_id).first()
            if u2:
                u2.balance += int(w.amount)

            w.status = "rejected"
            w.reviewed_at = datetime.utcnow()
            w.admin_note = f"Rejected by {tg_user.id} (auto-refund)"
            db.commit()

            try:
                await context.bot.send_message(
                    chat_id=w.user_id,
                    text=f"❌ Your withdraw request #{w.id} has been *REJECTED*.\nAmount refunded to your balance.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass

            await q.edit_message_text(f"❌ Rejected request #{w.id} (refunded).", reply_markup=admin_menu_kb())
            return

        if data == "a_ban":
            if not is_admin(tg_user.id):
                await q.edit_message_text("❌ Not authorized.")
                return
            context.user_data["await_ban"] = True
            await q.edit_message_text("⛔ Send the user ID to BAN.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin")]]))
            return

        if data == "a_unban":
            if not is_admin(tg_user.id):
                await q.edit_message_text("❌ Not authorized.")
                return
            context.user_data["await_unban"] = True
            await q.edit_message_text("✅ Send the user ID to UNBAN.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin")]]))
            return

        # fallback
        await q.edit_message_text("Unknown action.", reply_markup=main_menu_kb(is_admin=is_admin(tg_user.id)))

    finally:
        db.close()

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if not tg_user or not update.message:
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == tg_user.id).first()
        if not u:
            u = User(id=tg_user.id, username=tg_user.username, first_name=tg_user.first_name)
            db.add(u)
            db.commit()

        if u.is_banned:
            await update.message.reply_text("⛔ You are banned.")
            return

        s = get_settings(db)

        # ---------------- Withdraw flow ----------------
        if context.user_data.get("await_withdraw"):
            step = context.user_data.get("w_step", 1)

            if step == 1:
                if not text.isdigit():
                    await update.message.reply_text("❌ Please send a number only (withdraw amount).")
                    return
                amount = int(text)
                if amount <= 0:
                    await update.message.reply_text("❌ Amount must be positive.")
                    return
                if amount < int(s.min_withdraw):
                    await update.message.reply_text(f"❌ Minimum withdraw is {s.min_withdraw} {CURRENCY_LABEL}.")
                    return
                if amount > int(u.balance):
                    await update.message.reply_text("❌ Insufficient balance.")
                    return

                context.user_data["w_amount"] = amount
                context.user_data["w_step"] = 2
                await update.message.reply_text(
                    "Step 2/2: Send payout address/method (e.g., bKash number / Nagad number / Binance UID)."
                )
                return

            if step == 2:
                addr = text
                amount = int(context.user_data.get("w_amount", 0))
                if len(addr) < 5:
                    await update.message.reply_text("❌ Address looks too short. Send again.")
                    return

                # deduct balance, create request
                u.balance -= amount
                w = WithdrawRequest(
                    user_id=u.id,
                    amount=amount,
                    payout_address=addr,
                    status="pending",
                )
                db.add(w)
                db.commit()
                db.refresh(w)

                # notify admins
                notif = (
                    "🧾 *New Withdraw Request*\n\n"
                    f"Request ID: `{w.id}`\n"
                    f"User: `{u.id}`\n"
                    f"Amount: *{w.amount} {CURRENCY_LABEL}*\n"
                    f"Address: `{w.payout_address}`\n"
                )
                for aid in ADMIN_IDS:
                    try:
                        await context.bot.send_message(aid, notif, parse_mode=ParseMode.MARKDOWN)
                    except Exception:
                        pass

                # clear flow
                context.user_data.pop("await_withdraw", None)
                context.user_data.pop("w_step", None)
                context.user_data.pop("w_amount", None)

                await update.message.reply_text(
                    f"✅ Withdraw request submitted! (ID: {w.id})\nAdmin will review soon.",
                    reply_markup=main_menu_kb(is_admin=is_admin(tg_user.id))
                )
                return

        # ---------------- Admin actions via text ----------------
        if context.user_data.get("await_broadcast"):
            if not is_admin(tg_user.id):
                await update.message.reply_text("❌ Not authorized.")
                context.user_data.pop("await_broadcast", None)
                return

            context.user_data.pop("await_broadcast", None)
            users = db.query(User).filter(User.is_banned == False).all()
            sent = 0
            for uu in users:
                try:
                    await context.bot.send_message(chat_id=uu.id, text=text)
                    sent += 1
                    await asyncio.sleep(0.03)  # tiny pacing
                except Exception:
                    continue
            await update.message.reply_text(f"📣 Broadcast done. Sent to {sent} users.", reply_markup=admin_menu_kb())
            return

        if context.user_data.get("await_set_reward"):
            if not is_admin(tg_user.id):
                await update.message.reply_text("❌ Not authorized.")
                context.user_data.pop("await_set_reward", None)
                return
            if not text.isdigit():
                await update.message.reply_text("❌ Send a number only.")
                return
            new_val = int(text)
            if new_val < 0 or new_val > 10_000_000:
                await update.message.reply_text("❌ Value out of range.")
                return
            s.ref_reward = new_val
            s.updated_at = datetime.utcnow()
            db.commit()
            context.user_data.pop("await_set_reward", None)
            await update.message.reply_text(f"✅ Ref reward updated: {new_val} {CURRENCY_LABEL}", reply_markup=admin_menu_kb())
            return

        if context.user_data.get("await_set_minw"):
            if not is_admin(tg_user.id):
                await update.message.reply_text("❌ Not authorized.")
                context.user_data.pop("await_set_minw", None)
                return
            if not text.isdigit():
                await update.message.reply_text("❌ Send a number only.")
                return
            new_val = int(text)
            if new_val < 0 or new_val > 10_000_000:
                await update.message.reply_text("❌ Value out of range.")
                return
            s.min_withdraw = new_val
            s.updated_at = datetime.utcnow()
            db.commit()
            context.user_data.pop("await_set_minw", None)
            await update.message.reply_text(f"✅ Min withdraw updated: {new_val} {CURRENCY_LABEL}", reply_markup=admin_menu_kb())
            return

        if context.user_data.get("await_ban"):
            if not is_admin(tg_user.id):
                await update.message.reply_text("❌ Not authorized.")
                context.user_data.pop("await_ban", None)
                return
            if not text.isdigit():
                await update.message.reply_text("❌ Send numeric user ID.")
                return
            uid = int(text)
            tgt = db.query(User).filter(User.id == uid).first()
            if not tgt:
                await update.message.reply_text("User not found.")
                return
            tgt.is_banned = True
            db.commit()
            context.user_data.pop("await_ban", None)
            await update.message.reply_text(f"⛔ Banned user {uid}.", reply_markup=admin_menu_kb())
            return

        if context.user_data.get("await_unban"):
            if not is_admin(tg_user.id):
                await update.message.reply_text("❌ Not authorized.")
                context.user_data.pop("await_unban", None)
                return
            if not text.isdigit():
                await update.message.reply_text("❌ Send numeric user ID.")
                return
            uid = int(text)
            tgt = db.query(User).filter(User.id == uid).first()
            if not tgt:
                await update.message.reply_text("User not found.")
                return
            tgt.is_banned = False
            db.commit()
            context.user_data.pop("await_unban", None)
            await update.message.reply_text(f"✅ Unbanned user {uid}.", reply_markup=admin_menu_kb())
            return

        # default help
        await update.message.reply_text(
            "Use /start to open the menu.",
            reply_markup=main_menu_kb(is_admin=is_admin(tg_user.id))
        )

    finally:
        db.close()

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if not tg_user or not update.message:
        return
    if not is_admin(tg_user.id):
        await update.message.reply_text("❌ Not authorized.")
        return
    await update.message.reply_text("🛠 Admin Panel", reply_markup=admin_menu_kb())

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing. Set BOT_TOKEN env var.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(menu_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("Bot running...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
