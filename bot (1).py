import asyncio
import logging
import os
import time
import random
from datetime import datetime, timedelta
from urllib.parse import quote

import aiosqlite
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError, TimedOut, NetworkError, Forbidden
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

# ========================================================
#  SOZLAMALAR
# ========================================================

TOKEN = os.environ.get("BOT_TOKEN", "8856340901:AAHZOhvRkqztuguZ58AzzGg-gzPe_yld8L8")
SUPER_ADMIN = 8057184376
ADMIN_PROFILE_ID = 8057184376

DB_PATH = os.environ.get("DB_PATH", "bot_data.db")
SUB_CACHE_TTL = 300
DEFAULT_REF_PRICE = 1000.0
MIN_WITHDRAW = 5000.0
NET_RETRIES = 4
NET_BASE_DELAY = 1.5

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

CANCEL_TEXT = "❌ Bekor qilish"
DIVIDER = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
CARD_TYPES = ["HUMO", "UZCARD", "Boshqa"]

# ========================================================
#  CACHE
# ========================================================

_sub_cache = {}
_settings_cache = {}
_admins_cache = None
_admins_cache_time = 0
ADMIN_CACHE_TTL = 10
SETTINGS_CACHE_TTL = 30

_bot_maintenance = False
_bot_maintenance_msg = "🔧 Botda texnik nosozlik bor. Tez orada tiklanadi, iltimos kuting!"


def cache_get_sub(user_id):
    entry = _sub_cache.get(user_id)
    if not entry:
        return None
    is_sub, expires_at = entry
    if time.monotonic() >= expires_at:
        _sub_cache.pop(user_id, None)
        return None
    return is_sub


def cache_set_sub(user_id, is_sub):
    _sub_cache[user_id] = (is_sub, time.monotonic() + SUB_CACHE_TTL)


def cache_clear_sub(user_id=None):
    if user_id is None:
        _sub_cache.clear()
    else:
        _sub_cache.pop(user_id, None)


def get_settings_cached(key):
    entry = _settings_cache.get(key)
    if not entry:
        return None
    val, expires_at = entry
    if time.monotonic() >= expires_at:
        _settings_cache.pop(key, None)
        return None
    return val


def set_settings_cache(key, value):
    _settings_cache[key] = (value, time.monotonic() + SETTINGS_CACHE_TTL)


def clear_settings_cache(key=None):
    if key is None:
        _settings_cache.clear()
    else:
        _settings_cache.pop(key, None)


# ========================================================
#  API CALL
# ========================================================

async def api_call(coro_factory, *, retries=NET_RETRIES, base_delay=NET_BASE_DELAY,
                    swallow=True, default=None, action_desc=""):
    last_exc = None
    for attempt in range(retries):
        try:
            return await coro_factory()
        except (TimedOut, NetworkError) as e:
            last_exc = e
            if attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning("Tarmoq xatosi%s: %s", f" [{action_desc}]" if action_desc else "", e)
                await asyncio.sleep(delay)
        except Forbidden:
            return default
        except TelegramError as e:
            logger.error("Telegram xatosi: %s", e)
            if not swallow:
                raise
            return default
    if swallow:
        return default
    if last_exc:
        raise last_exc
    return default
# ========================================================
#  BAZA FUNKSIYALARI
# ========================================================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0, referred_by INTEGER, joined_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        await db.execute('CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY, channel_name TEXT)')
        await db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
        await db.execute('CREATE TABLE IF NOT EXISTS withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, card_type TEXT, card_number TEXT, status TEXT DEFAULT "pending", created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        await db.execute('CREATE TABLE IF NOT EXISTS support_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, message_text TEXT, admin_id INTEGER, answer_text TEXT, status TEXT DEFAULT "pending", created_at TEXT DEFAULT CURRENT_TIMESTAMP, answered_at TEXT)')
        await db.execute('CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, added_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        await db.execute('CREATE TABLE IF NOT EXISTS promocodes (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, amount REAL, max_uses INTEGER, used_count INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP, is_active INTEGER DEFAULT 1)')
        await db.execute('CREATE TABLE IF NOT EXISTS promo_uses (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, promo_id INTEGER, used_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, promo_id))')
        await db.execute('CREATE TABLE IF NOT EXISTS bonus_claims (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, claimed_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        await db.execute('CREATE TABLE IF NOT EXISTS payment_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT, channel_name TEXT, description TEXT)')

        defaults = [
            ("ref_price", str(DEFAULT_REF_PRICE)),
            ("bonus_min", "10"),
            ("bonus_max", "900"),
            ("bonus_interval", "86400"),
            ("bonus_enabled", "1"),
        ]
        for k, v in defaults:
            await db.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))

        await db.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (SUPER_ADMIN,))
        await db.commit()


async def get_admins():
    global _admins_cache, _admins_cache_time
    now = time.monotonic()
    if _admins_cache is not None and (now - _admins_cache_time) < ADMIN_CACHE_TTL:
        return _admins_cache
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT user_id FROM admins')
        _admins_cache = [r[0] for r in await cursor.fetchall()]
        _admins_cache_time = now
    return _admins_cache


async def add_admin_db(user_id):
    global _admins_cache
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (user_id,))
        await db.commit()
    _admins_cache = None


async def remove_admin_db(user_id):
    global _admins_cache
    if user_id == SUPER_ADMIN:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM admins WHERE user_id=?', (user_id,))
        await db.commit()
    _admins_cache = None
    return True


async def get_setting(key, default=None):
    cached = get_settings_cached(key)
    if cached is not None:
        return cached
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT value FROM settings WHERE key=?', (key,))
        row = await cursor.fetchone()
        val = row[0] if row else default
    if val is not None:
        set_settings_cache(key, val)
    return val


async def set_setting(key, value):
    clear_settings_cache(key)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
        await db.commit()


async def get_ref_price():
    val = await get_setting("ref_price", str(DEFAULT_REF_PRICE))
    try:
        return float(val)
    except:
        return DEFAULT_REF_PRICE


async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT user_id, balance, referred_by FROM users WHERE user_id=?', (user_id,))
        return await cursor.fetchone()


async def create_user(user_id, referrer_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT INTO users (user_id, referred_by) VALUES (?, ?)', (user_id, referrer_id))
        await db.commit()


async def add_balance(user_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE users SET balance = MAX(0, balance + ?) WHERE user_id=?', (amount, user_id))
        await db.commit()


async def get_user_by_id(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT user_id, balance FROM users WHERE user_id=?', (user_id,))
        return await cursor.fetchone()


async def get_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT channel_id, channel_name FROM channels')
        return await cursor.fetchall()


async def add_channel_db(channel_id, channel_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT OR REPLACE INTO channels (channel_id, channel_name) VALUES (?, ?)', (channel_id, channel_name))
        await db.commit()


async def remove_channel_db(channel_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM channels WHERE channel_id=?', (channel_id,))
        await db.commit()


async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT COUNT(*), COALESCE(SUM(balance), 0.0) FROM users')
        return await cursor.fetchone()


async def get_all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT user_id FROM users')
        return [r[0] for r in await cursor.fetchall()]


async def create_withdrawal(user_id, amount, card_type, card_number):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            'INSERT INTO withdrawals (user_id, amount, card_type, card_number, status) VALUES (?, ?, ?, ?, "pending")',
            (user_id, amount, card_type, card_number))
        await db.commit()
        return cursor.lastrowid


async def get_withdrawal(wid):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT id, user_id, amount, card_type, card_number, status FROM withdrawals WHERE id=?', (wid,))
        return await cursor.fetchone()


async def set_withdrawal_status(wid, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE withdrawals SET status=? WHERE id=?', (status, wid))
        await db.commit()


async def create_support_message(user_id, message_text):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('INSERT INTO support_messages (user_id, message_text, status) VALUES (?, ?, "pending")', (user_id, message_text))
        await db.commit()
        return cursor.lastrowid


async def get_support_message(sid):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT id, user_id, message_text, status FROM support_messages WHERE id=?', (sid,))
        return await cursor.fetchone()


async def set_support_status(sid, status, answer_text=None, admin_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        if answer_text and admin_id:
            await db.execute('UPDATE support_messages SET status=?, answer_text=?, admin_id=?, answered_at=CURRENT_TIMESTAMP WHERE id=?', (status, answer_text, admin_id, sid))
        else:
            await db.execute('UPDATE support_messages SET status=? WHERE id=?', (status, sid))
        await db.commit()


async def create_promocode(code, amount, max_uses):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cursor = await db.execute('INSERT INTO promocodes (code, amount, max_uses) VALUES (?, ?, ?)', (code.upper(), amount, max_uses))
            await db.commit()
            return cursor.lastrowid
        except:
            return None


async def get_promocode(code):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT id, code, amount, max_uses, used_count, is_active FROM promocodes WHERE code=?', (code.upper(),))
        return await cursor.fetchone()


async def get_all_promocodes():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT id, code, amount, max_uses, used_count, is_active FROM promocodes ORDER BY id DESC')
        return await cursor.fetchall()


async def use_promocode(user_id, promo_id):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute('INSERT INTO promo_uses (user_id, promo_id) VALUES (?, ?)', (user_id, promo_id))
            await db.execute('UPDATE promocodes SET used_count = used_count + 1 WHERE id=?', (promo_id,))
            await db.commit()
            return True
        except:
            return False


async def has_used_promo(user_id, promo_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT id FROM promo_uses WHERE user_id=? AND promo_id=?', (user_id, promo_id))
        return await cursor.fetchone() is not None


async def delete_promocode(promo_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE promocodes SET is_active=0 WHERE id=?', (promo_id,))
        await db.commit()


async def last_bonus_claim(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT claimed_at FROM bonus_claims WHERE user_id=? ORDER BY id DESC LIMIT 1', (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def add_bonus_claim(user_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT INTO bonus_claims (user_id, amount) VALUES (?, ?)', (user_id, amount))
        await db.execute('UPDATE users SET balance = balance + ? WHERE user_id=?', (amount, user_id))
        await db.commit()


async def get_payment_channel():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT channel_id, channel_name, description FROM payment_channels ORDER BY id DESC LIMIT 1')
        return await cursor.fetchone()


async def set_payment_channel(channel_id, channel_name, description):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM payment_channels')
        await db.execute('INSERT INTO payment_channels (channel_id, channel_name, description) VALUES (?, ?, ?)', (channel_id, channel_name, description))
        await db.commit()
# ========================================================
#  OBUNANI TEKSHIRISH VA KLAVIATURALAR
# ========================================================

async def check_one_channel(context, channel_id, user_id):
    result = await api_call(
        lambda: context.bot.get_chat_member(chat_id=channel_id, user_id=user_id),
        action_desc=f"member:{channel_id}", swallow=True, default=None)
    if result is None:
        return True
    return result.status not in ('left', 'kicked')


async def is_subscribed(user_id, context, use_cache=True):
    admins = await get_admins()
    if user_id in admins:
        return True
    if use_cache:
        cached = cache_get_sub(user_id)
        if cached is not None:
            return cached
    channels = await get_channels()
    if not channels:
        result = True
    else:
        results = await asyncio.gather(*[check_one_channel(context, cid, user_id) for cid, _ in channels])
        result = all(results)
    cache_set_sub(user_id, result)
    return result


def main_keyboard(user_id):
    buttons = [
        [KeyboardButton("💰 Pul ishlash"), KeyboardButton("👤 Balans")],
        [KeyboardButton("💸 Pul yechish"), KeyboardButton("🎁 Bonus")],
        [KeyboardButton("🎟 Promokod"), KeyboardButton("💳 To'lov kanali")],
        [KeyboardButton("☎️ Murojaat")],
    ]
    if user_id == SUPER_ADMIN:
        buttons.append([KeyboardButton("⚙️ Admin Panel")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def cancel_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton(CANCEL_TEXT)]], resize_keyboard=True)


def subscription_keyboard(channels):
    btns = [[InlineKeyboardButton(f"📢 {name}", url=f"https://t.me/{cid.replace('@', '')}")] for cid, name in channels]
    btns.append([InlineKeyboardButton("✅ Obunani tasdiqlash", callback_data="check_sub")])
    return InlineKeyboardMarkup(btns)


def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Kanal qo'shish", callback_data="admin_add_channel"),
         InlineKeyboardButton("➖ Kanal o'chirish", callback_data="admin_remove_channel")],
        [InlineKeyboardButton("📋 Kanallar ro'yxati", callback_data="admin_list_channels")],
        [InlineKeyboardButton("💵 Referal narxi", callback_data="admin_set_price")],
        [InlineKeyboardButton("👑 Admin qo'shish", callback_data="admin_add_admin"),
         InlineKeyboardButton("🚫 Admin o'chirish", callback_data="admin_remove_admin")],
        [InlineKeyboardButton("💰 Pul qo'shish", callback_data="admin_add_money"),
         InlineKeyboardButton("💸 Pul ayirish", callback_data="admin_remove_money")],
        [InlineKeyboardButton("🛠 Texnik ish rejimi", callback_data="admin_maintenance")],
        [InlineKeyboardButton("🎁 Bonus sozlamalari", callback_data="admin_bonus_settings")],
        [InlineKeyboardButton("🎟 Promokod yaratish", callback_data="admin_create_promo"),
         InlineKeyboardButton("📋 Promokodlar", callback_data="admin_list_promos")],
        [InlineKeyboardButton("💳 To'lov kanali", callback_data="admin_payment_channel")],
        [InlineKeyboardButton("📊 Statistika", callback_data="admin_stats"),
         InlineKeyboardButton("📢 Xabar yuborish", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🚪 Egallikdan chiqish", callback_data="admin_leave_ownership")],
        [InlineKeyboardButton("✖️ Yopish", callback_data="admin_close")],
    ])


def remove_channel_keyboard(channels):
    rows = [[InlineKeyboardButton(f"🗑 {name} ({cid})", callback_data=f"rmch:{cid}")] for cid, name in channels]
    rows.append([InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")])
    return InlineKeyboardMarkup(rows)


def admin_list_keyboard(admins):
    rows = [[InlineKeyboardButton(f"🗑 Admin {aid}", callback_data=f"rmadm:{aid}")] for aid in admins if aid != SUPER_ADMIN]
    rows.append([InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")])
    return InlineKeyboardMarkup(rows)


def promo_list_keyboard(promos):
    rows = []
    for pid, code, amount, max_uses, used, active in promos[:20]:
        status = "✅" if active else "❌"
        rows.append([InlineKeyboardButton(f"{status} {code} | {amount:,.0f} | {used}/{max_uses}", callback_data=f"delpromo:{pid}")])
    rows.append([InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")])
    return InlineKeyboardMarkup(rows)


def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")]])


def share_keyboard(ref_link):
    share_text = "🎁 Bu bot orqali pul ishlashni boshla!"
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={quote(share_text)}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Do'stlarga ulashish", url=share_url)],
        [InlineKeyboardButton("🔄 Yangilash", callback_data="refresh_ref")],
    ])


def card_type_keyboard():
    btns = [[InlineKeyboardButton(f"💳 {c}", callback_data=f"wd_type:{c}")] for c in CARD_TYPES]
    btns.append([InlineKeyboardButton("‹ Bekor qilish", callback_data="wd_cancel")])
    return InlineKeyboardMarkup(btns)


def withdraw_submitted_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("👨‍💼 Admin", url=f"tg://user?id={ADMIN_PROFILE_ID}")]])


def admin_withdraw_keyboard(wid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"wdok:{wid}"),
         InlineKeyboardButton("❌ Rad etish", callback_data=f"wdno:{wid}")]
    ])


def support_answer_keyboard(sid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("✍️ Javob yozish", callback_data=f"sup_answer:{sid}")]])


def confirm_leave_1():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Ha, egallikdan chiqaman", callback_data="leave_yes_1")],
        [InlineKeyboardButton("❌ Bekor qilish", callback_data="leave_cancel")],
    ])


def confirm_leave_2():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 100% ha", callback_data="leave_yes_2")],
        [InlineKeyboardButton("❌ Yo'q", callback_data="leave_cancel")],
    ])


def confirm_leave_3():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Ha, egallikdan chiq", callback_data="leave_yes_3")],
    ])


async def animate(message, frames, delay=0.35, parse_mode="Markdown", reply_markup=None):
    last = len(frames) - 1
    for i, frame in enumerate(frames):
        await api_call(
            lambda f=frame, i=i: message.edit_text(f, parse_mode=parse_mode, reply_markup=reply_markup if i == last else None),
            action_desc="animate")
        if i != last:
            await asyncio.sleep(delay)


async def show_subscription_gate(update, context):
    channels = await get_channels()
    if not channels:
        return
    await api_call(lambda: update.effective_chat.send_message(
        "🔐 *Kirish cheklangan*", parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove()), action_desc="g1")
    await api_call(lambda: update.effective_chat.send_message(
        f"⚡️ *Kanal(lar)ga a'zo bo'ling:*\n{DIVIDER}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=subscription_keyboard(channels)), action_desc="g2")
# ========================================================
#  START, PUL ISHLASH, BALANS, BONUS, PROMO, MUROJAAT
# ========================================================

async def start(update, context):
    try:
        if not update.message:
            return
        user_id = update.effective_user.id

        if _bot_maintenance and user_id != SUPER_ADMIN:
            await api_call(lambda: update.message.reply_text(_bot_maintenance_msg, parse_mode=ParseMode.MARKDOWN), action_desc="maint")
            return

        if context.user_data.get("left_ownership"):
            user = await get_user(user_id)
            if not user:
                await create_user(user_id, None)
            await api_call(lambda: update.message.reply_text(
                f"👋 Xush kelibsiz, *{update.effective_user.first_name}*!\n\n🚪 Siz egallikdan chiqdingiz.\n{DIVIDER}",
                parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="left")
            return

        if not await is_subscribed(user_id, context):
            await show_subscription_gate(update, context)
            return

        user = await get_user(user_id)
        referrer_id = None
        if context.args and context.args[0].isdigit():
            referrer_id = int(context.args[0])

        is_new = False
        if not user:
            is_new = True
            await create_user(user_id, referrer_id)
            if referrer_id and referrer_id != user_id:
                referrer = await get_user(referrer_id)
                if referrer:
                    ref_price = await get_ref_price()
                    await add_balance(referrer_id, ref_price)
                    cache_clear_sub(referrer_id)
                    await api_call(lambda: context.bot.send_message(
                        chat_id=referrer_id,
                        text=f"🎉 *Yangi do'st qo'shildi!*\n💰 *+{ref_price:,.0f} so'm*",
                        parse_mode=ParseMode.MARKDOWN), action_desc="ref_notify")

        await api_call(lambda: context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING), action_desc="typing")

        greeting = "🎊 Xush kelibsiz" if is_new else "👋 Qaytib keldingiz"
        await api_call(lambda: update.message.reply_text(
            f"{greeting}, *{update.effective_user.first_name}*!\n\n"
            f"🤖 Do'stlaringizni taklif qilib pul ishlang!\n"
            f"💰 Bonus, promokod, to'lov kanali\n"
            f"💸 Pulni kartangizga yechib oling\n{DIVIDER}",
            reply_markup=main_keyboard(user_id), parse_mode=ParseMode.MARKDOWN), action_desc="start_r")
    except Exception:
        logger.exception("start xato")


async def check_sub_callback(update, context):
    try:
        query = update.callback_query
        user_id = query.from_user.id
        cache_clear_sub(user_id)
        await api_call(lambda: query.answer("🔄"), action_desc="ck")
        ok = await is_subscribed(user_id, context, use_cache=False)
        if ok:
            await animate(query.message, ["🔄...", "🔄..", "✅ *Obuna tasdiqlandi!*"], delay=0.35)
            await asyncio.sleep(0.4)
            await api_call(lambda: query.message.delete(), action_desc="del")
            user = await get_user(user_id)
            if not user:
                await create_user(user_id, None)
            await api_call(lambda: context.bot.send_message(
                chat_id=query.message.chat.id,
                text=f"✅ Obuna tasdiqlandi!\n{DIVIDER}\n👇 Menyudan tanlang:",
                parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="main")
        else:
            await api_call(lambda: query.answer("❌ Hali obuna bo'lmadingiz!", show_alert=True), action_desc="ck_fail")
    except Exception:
        logger.exception("check_sub xato")


async def handle_earn(update, context, user_id):
    placeholder = await api_call(lambda: update.message.reply_text("⏳"), action_desc="earn_ph")
    if not placeholder:
        return
    ref_price = await get_ref_price()
    bot_info = await api_call(lambda: context.bot.get_me(), action_desc="me", swallow=False)
    if not bot_info:
        return
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    await animate(placeholder, ["⏳", "⏳."], delay=0.35)
    text = (
        "🚀 *Sizning taklif havolangiz:*\n"
        f"`{ref_link}`\n\n"
        f"💵 Har bir do'st: *{ref_price:,.0f} so'm*\n{DIVIDER}\n📤 Ulashing!"
    )
    await api_call(lambda: placeholder.edit_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=share_keyboard(ref_link)), action_desc="earn_e")


async def refresh_ref_callback(update, context):
    try:
        query = update.callback_query
        await api_call(lambda: query.answer("🔄"), action_desc="ref_a")
        user_id = query.from_user.id
        ref_price = await get_ref_price()
        bot_info = await api_call(lambda: context.bot.get_me(), action_desc="me2", swallow=False)
        if not bot_info:
            return
        ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
        text = f"🚀 *Taklif havolangiz:*\n`{ref_link}`\n\n💵 *{ref_price:,.0f} so'm*"
        await api_call(lambda: query.message.edit_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=share_keyboard(ref_link)), action_desc="ref_e")
    except Exception:
        logger.exception("refresh xato")


async def handle_balance(update, user_id):
    cache_clear_sub(user_id)
    user = await get_user(user_id)
    balance = user[1] if user else 0.0
    await api_call(lambda: update.message.reply_text(
        f"💳 *Balans:* *{balance:,.0f} so'm*", parse_mode=ParseMode.MARKDOWN), action_desc="bal")


async def handle_stats(update):
    count, total = await get_stats()
    await api_call(lambda: update.message.reply_text(
        f"📊 *Statistika*\n{DIVIDER}\n👤 *{count}* ta foydalanuvchi\n💰 *{total:,.0f} so'm*",
        parse_mode=ParseMode.MARKDOWN), action_desc="stats")


async def handle_bonus(update, context, user_id):
    enabled = await get_setting("bonus_enabled", "1")
    if enabled != "1":
        await api_call(lambda: update.message.reply_text("❌ Bonus o'chirilgan.", parse_mode=ParseMode.MARKDOWN), action_desc="b_off")
        return
    last = await last_bonus_claim(user_id)
    interval = int(await get_setting("bonus_interval", "86400"))
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            next_claim = last_dt + timedelta(seconds=interval)
            if datetime.now() < next_claim:
                remaining = next_claim - datetime.now()
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                await api_call(lambda: update.message.reply_text(
                    f"⏰ Keyingi bonus: *{hours}s {minutes}m* dan so'ng", parse_mode=ParseMode.MARKDOWN), action_desc="b_wait")
                return
        except:
            pass
    bmin = int(await get_setting("bonus_min", "10"))
    bmax = int(await get_setting("bonus_max", "900"))
    amount = random.randint(bmin, bmax)
    await add_bonus_claim(user_id, amount)
    cache_clear_sub(user_id)
    await api_call(lambda: update.message.reply_text(
        f"🎁 *Tabriklaymiz!*\n💰 *+{amount} so'm* bonus!", parse_mode=ParseMode.MARKDOWN), action_desc="b_done")


async def handle_promo_start(update, context, user_id):
    context.user_data["state"] = "enter_promo"
    await api_call(lambda: update.message.reply_text(
        "🎟 *Promokodni kiriting:*", parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_keyboard()), action_desc="pr_p")


async def handle_promo_state(update, context, user_id, text):
    if context.user_data.get("state") != "enter_promo":
        return False
    context.user_data["state"] = None
    if text == CANCEL_TEXT:
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="pr_c")
        return True
    promo = await get_promocode(text.strip())
    if not promo or not promo[5]:
        await api_call(lambda: update.message.reply_text("❌ Topilmadi.", reply_markup=main_keyboard(user_id)), action_desc="pr_nf")
        return True
    pid, code, amount, max_uses, used_count, is_active = promo
    if used_count >= max_uses:
        await api_call(lambda: update.message.reply_text("❌ Limit tugagan.", reply_markup=main_keyboard(user_id)), action_desc="pr_lim")
        return True
    if await has_used_promo(user_id, pid):
        await api_call(lambda: update.message.reply_text("❌ Ishlatgansiz.", reply_markup=main_keyboard(user_id)), action_desc="pr_us")
        return True
    if await use_promocode(user_id, pid):
        await add_balance(user_id, amount)
        cache_clear_sub(user_id)
        await api_call(lambda: update.message.reply_text(
            f"🎉 *+{amount:,.0f} so'm!*", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="pr_ok")
    return True


async def handle_payment_channel(update, context, user_id):
    pc = await get_payment_channel()
    if not pc:
        await api_call(lambda: update.message.reply_text("💳 Kanal yo'q.", parse_mode=ParseMode.MARKDOWN), action_desc="pc_e")
        return
    cid, name, desc = pc
    await api_call(lambda: update.message.reply_text(
        f"💳 *{name}*\n{DIVIDER}\n{desc}\n\n👉 {cid}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 O'tish", url=f"https://t.me/{cid.replace('@', '')}")]])), action_desc="pc_s")


async def handle_support_start(update, context, user_id):
    context.user_data["state"] = "support_message"
    await api_call(lambda: update.message.reply_text(
        f"☎️ *Murojaat*\n{DIVIDER}\nXabaringizni yozing:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_keyboard()), action_desc="sup_p")


async def handle_support_state(update, context, user_id, text):
    if context.user_data.get("state") != "support_message":
        return False
    if text == CANCEL_TEXT:
        context.user_data["state"] = None
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="sup_c")
        return True
    context.user_data["state"] = None
    sid = await create_support_message(user_id, text)
    user_info = update.effective_user
    uname = f"@{user_info.username}" if user_info.username else "yo'q"
    admins = await get_admins()
    for aid in admins:
        await api_call(lambda a=aid: context.bot.send_message(
            chat_id=a,
            text=f"☎️ *Murojaat!*\n{DIVIDER}\n👤 {user_info.full_name} {uname}\n🆔 `{user_info.id}`\n{DIVIDER}\n💬 {text}\n\n📋 ID: `{sid}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=support_answer_keyboard(sid)), action_desc=f"sup_a:{aid}")
    await api_call(lambda: update.message.reply_text(
        "✅ *Yuborildi!*\nAdmin tez orada javob beradi.", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="sup_ok")
    return True


async def support_answer_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id not in await get_admins():
        await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="sa_d")
        return
    sid = int(query.data.split(":")[1])
    context.user_data["state"] = "support_reply"
    context.user_data["support_reply_sid"] = sid
    await api_call(lambda: query.message.edit_text("✍️ *Javobingizni yozing:*", parse_mode=ParseMode.MARKDOWN), action_desc="sa_p")
    await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="sa_kb")


async def handle_support_reply_state(update, context, user_id, text):
    if context.user_data.get("state") != "support_reply":
        return False
    sid = context.user_data.get("support_reply_sid")
    context.user_data["state"] = None
    context.user_data.pop("support_reply_sid", None)
    if text == CANCEL_TEXT:
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="sr_c")
        return True
    if not sid:
        return True
    msg = await get_support_message(sid)
    if not msg:
        return True
    target = msg[1]
    await set_support_status(sid, "answered", text, user_id)
    await api_call(lambda: context.bot.send_message(
        chat_id=target,
        text=f"💬 *Admin javobi:*\n{DIVIDER}\n{text}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(target)), action_desc="sr_s")
    await api_call(lambda: update.message.reply_text("✅ Yuborildi!", reply_markup=main_keyboard(user_id)), action_desc="sr_ok")
    return True
# ========================================================
#  PUL YECHISH LOGIKASI
# ========================================================

async def handle_withdraw_start(update, context, user_id):
    cache_clear_sub(user_id)
    user = await get_user(user_id)
    balance = user[1] if user else 0.0
    if balance < MIN_WITHDRAW:
        await api_call(lambda: update.message.reply_text(
            f"🚫 Yetarli emas. Minimal: *{MIN_WITHDRAW:,.0f} so'm*", parse_mode=ParseMode.MARKDOWN), action_desc="wd_l")
        return
    await api_call(lambda: update.message.reply_text(
        f"💸 *Pul yechish*\n{DIVIDER}\n💰 *{balance:,.0f} so'm*\nKarta turini tanlang:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=card_type_keyboard()), action_desc="wd_s")


async def withdraw_type_callback(update, context):
    try:
        query = update.callback_query
        await api_call(lambda: query.answer(), action_desc="wt_a")
        if query.data == "wd_cancel":
            await api_call(lambda: query.message.edit_text("🚫 Bekor qilindi."), action_desc="wt_c")
            return
        card_type = query.data.split(":", 1)[1]
        context.user_data["withdraw_card_type"] = card_type
        context.user_data["state"] = "withdraw_card_number"
        await api_call(lambda: query.message.edit_text(
            f"💳 *{card_type}*\nKarta raqamini kiriting (16 raqam):", parse_mode=ParseMode.MARKDOWN), action_desc="wt_p")
        await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="wt_kb")
    except Exception:
        logger.exception("wt xato")


async def handle_withdraw_state(update, context, user_id, text):
    if context.user_data.get("state") != "withdraw_card_number":
        return False
    if text == CANCEL_TEXT:
        context.user_data["state"] = None
        context.user_data.pop("withdraw_card_type", None)
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="ws_c")
        return True
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 16:
        await api_call(lambda: update.message.reply_text("❌ 16 raqam.", parse_mode=ParseMode.MARKDOWN), action_desc="ws_i")
        return True
    card_type = context.user_data.get("withdraw_card_type", "Boshqa")
    user = await get_user(user_id)
    balance = user[1] if user else 0.0
    if balance < MIN_WITHDRAW:
        context.user_data["state"] = None
        await api_call(lambda: update.message.reply_text("🚫 Yetarli emas.", reply_markup=main_keyboard(user_id)), action_desc="ws_l")
        return True
    amount = balance
    card_number = " ".join(digits[i:i+4] for i in range(0, 16, 4))
    await add_balance(user_id, -amount)
    cache_clear_sub(user_id)
    wid = await create_withdrawal(user_id, amount, card_type, card_number)
    context.user_data["state"] = None
    context.user_data.pop("withdraw_card_type", None)
    await api_call(lambda: update.message.reply_text(
        f"✅ *So'rov qabul qilindi!*\n{DIVIDER}\n💰 *{amount:,.0f} so'm*\n💳 *{card_type}* `{card_number}`\n\n⏳ Tez orada tushadi.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=withdraw_submitted_keyboard()), action_desc="ws_ok")
    await api_call(lambda: update.message.reply_text("👇", reply_markup=main_keyboard(user_id)), action_desc="ws_m")
    requester = update.effective_user
    uname = f"@{requester.username}" if requester.username else "yo'q"
    for aid in await get_admins():
        await api_call(lambda a=aid: context.bot.send_message(
            chat_id=a,
            text=f"💸 *Pul yechish*\n{DIVIDER}\n👤 {requester.full_name} {uname}\n🆔 `{requester.id}`\n💰 *{amount:,.0f} so'm*\n💳 *{card_type}* `{card_number}`\n📋 `{wid}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=admin_withdraw_keyboard(wid)), action_desc=f"ws_a:{aid}")
    return True


async def withdraw_admin_decision_callback(update, context):
    try:
        query = update.callback_query
        if query.from_user.id not in await get_admins():
            await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="wd_d")
            return
        action, wid_str = query.data.split(":", 1)
        wid = int(wid_str)
        row = await get_withdrawal(wid)
        if not row:
            await api_call(lambda: query.answer("❌ Topilmadi", show_alert=True), action_desc="wd_nf")
            return
        w_id, target, amount, ctype, cnum, status = row
        if status != "pending":
            await api_call(lambda: query.answer("ℹ️ Ko'rilgan", show_alert=True), action_desc="wd_dn")
            return
        await api_call(lambda: query.answer(), action_desc="wd_a")
        if action == "wdok":
            await set_withdrawal_status(wid, "approved")
            try:
                new_text = (query.message.text_markdown or query.message.text or "") + "\n\n✅ Tasdiqlandi"
            except:
                new_text = (query.message.text or "") + "\n\n✅ Tasdiqlandi"
            await api_call(lambda: query.message.edit_text(new_text, parse_mode=ParseMode.MARKDOWN), action_desc="wd_oe")
            await api_call(lambda: context.bot.send_message(
                chat_id=target, text=f"✅ *Pul tasdiqlandi!*\n💰 *{amount:,.0f} so'm*", parse_mode=ParseMode.MARKDOWN), action_desc="wd_uo")
        elif action == "wdno":
            context.user_data["state"] = "wd_reject_reason"
            context.user_data["wd_reject_id"] = wid
            await api_call(lambda: query.message.edit_text("✍️ Sababni yozing:", parse_mode=ParseMode.MARKDOWN), action_desc="wd_ne")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="wd_nk")
    except Exception:
        logger.exception("wdc xato")


async def handle_wd_reject_reason(update, context, user_id, text):
    if context.user_data.get("state") != "wd_reject_reason":
        return False
    wid = context.user_data.get("wd_reject_id")
    context.user_data["state"] = None
    context.user_data.pop("wd_reject_id", None)
    if text == CANCEL_TEXT:
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="wr_c")
        return True
    row = await get_withdrawal(wid) if wid else None
    if not row or row[5] != "pending":
        return True
    target, amount = row[1], row[2]
    await set_withdrawal_status(wid, "rejected")
    await add_balance(target, amount)
    cache_clear_sub(target)
    await api_call(lambda: context.bot.send_message(
        chat_id=target,
        text=f"❌ *Rad etildi*\n{DIVIDER}\n📝 {text}\n💰 *{amount:,.0f} so'm* qaytarildi",
        parse_mode=ParseMode.MARKDOWN), action_desc="wr_u")
    await api_call(lambda: update.message.reply_text("✅ Yuborildi", reply_markup=main_keyboard(user_id)), action_desc="wr_d")
    return True
# ========================================================
#  ADMIN PANEL METODLARI
# ========================================================

async def open_admin_panel(target, edit=False):
    ref_price = await get_ref_price()
    channels = await get_channels()
    text = (f"⚙️ *Admin Panel*\n{DIVIDER}\n💵 Ref: *{ref_price:,.0f}*\n📢 Kanallar: *{len(channels)}*")
    if edit:
        await api_call(lambda: target.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_panel_keyboard()), action_desc="ap_e")
    else:
        await api_call(lambda: target.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_panel_keyboard()), action_desc="ap_r")


async def admin_panel_callback(update, context):
    try:
        query = update.callback_query
        user_id = query.from_user.id
        if user_id not in await get_admins():
            await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="ad_d")
            return
        data = query.data
        await api_call(lambda: query.answer(), action_desc="ad_a")
        if data == "admin_close":
            await api_call(lambda: query.message.delete(), action_desc="ad_x")
            return
        if data == "admin_back":
            await open_admin_panel(query, edit=True)
            return
        if data == "admin_add_channel":
            context.user_data["state"] = "add_channel"
            await api_call(lambda: query.message.edit_text(f"➕ *Kanal qo'shish*\n{DIVIDER}\n`@username Nomi`\n⚠️ Bot admin bo'lishi kerak!", parse_mode=ParseMode.MARKDOWN), action_desc="ac_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="ac_kb")
            return
        if data == "admin_remove_channel":
            channels = await get_channels()
            if not channels:
                await api_call(lambda: query.message.edit_text("📋 Bo'sh", reply_markup=back_keyboard()), action_desc="rc_e")
                return
            await api_call(lambda: query.message.edit_text("➖ Tanlang:", parse_mode=ParseMode.MARKDOWN, reply_markup=remove_channel_keyboard(channels)), action_desc="rc_p")
            return
        if data == "admin_list_channels":
            channels = await get_channels()
            if not channels:
                body = "📋 Bo'sh"
            else:
                body = "📋 *Kanallar:*\n" + "\n".join([f"• *{n}* — `{c}`" for c, n in channels])
            await api_call(lambda: query.message.edit_text(body, parse_mode=ParseMode.MARKDOWN, reply_markup=back_keyboard()), action_desc="lc")
            return
        if data == "admin_set_price":
            context.user_data["state"] = "set_price"
            current = await get_ref_price()
            await api_call(lambda: query.message.edit_text(f"💵 Hozirgi: *{current:,.0f}*\nYangi narxni kiriting:", parse_mode=ParseMode.MARKDOWN), action_desc="sp_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="sp_kb")
            return
        if data == "admin_add_admin":
            if user_id != SUPER_ADMIN:
                await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="aa_d")
                return
            context.user_data["state"] = "add_admin"
            await api_call(lambda: query.message.edit_text("👑 *Admin ID kiriting:*", parse_mode=ParseMode.MARKDOWN), action_desc="aa_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="aa_kb")
            return
        if data == "admin_remove_admin":
            if user_id != SUPER_ADMIN:
                await api_call(lambda: query.answer("⛔", show_alert=True), action_desc="ra_d")
                return
            others = [a for a in await get_admins() if a != SUPER_ADMIN]
            if not others:
                await api_call(lambda: query.message.edit_text("Boshqa admin yo'q", reply_markup=back_keyboard()), action_desc="ra_e")
                return
            await api_call(lambda: query.message.edit_text("🚫 O'chirish:", reply_markup=admin_list_keyboard(await get_admins())), action_desc="ra_l")
            return
        if data == "admin_add_money":
            context.user_data["state"] = "add_money"
            await api_call(lambda: query.message.edit_text("💰 *Pul qo'shish*\n\n`user_id miqdor`", parse_mode=ParseMode.MARKDOWN), action_desc="am_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="am_kb")
            return
        if data == "admin_remove_money":
            context.user_data["state"] = "remove_money"
            await api_call(lambda: query.message.edit_text("💸 *Pul ayirish*\n\n`user_id miqdor`", parse_mode=ParseMode.MARKDOWN), action_desc="rm_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="rm_kb")
            return
        if data == "admin_maintenance":
            global _bot_maintenance
            if _bot_maintenance:
                _bot_maintenance = False
                await api_call(lambda: query.message.edit_text("✅ Bot yoqildi!", reply_markup=back_keyboard()), action_desc="mt_off")
            else:
                await api_call(lambda: query.message.edit_text("🛠 *Qanday xabar?*", parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("1️⃣ Standart", callback_data="maint_standard")],
                        [InlineKeyboardButton("2️⃣ O'zim yozaman", callback_data="maint_custom")],
                        [InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")],
                    ])), action_desc="mt_m")
            return
        if data == "maint_standard":
            _bot_maintenance = True
            await api_call(lambda: query.message.edit_text("✅ Standart xabar yoqildi!", reply_markup=back_keyboard()), action_desc="mt_s")
            return
        if data == "maint_custom":
            context.user_data["state"] = "maint_custom_msg"
            await api_call(lambda: query.message.edit_text("✍️ Xabarni kiriting:", parse_mode=ParseMode.MARKDOWN), action_desc="mt_c")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="mt_ck")
            return
        if data == "admin_bonus_settings":
            interval = await get_setting("bonus_interval", "86400")
            bmin = await get_setting("bonus_min", "10")
            bmax = await get_setting("bonus_max", "900")
            enabled = await get_setting("bonus_enabled", "1")
            interval_text = "⏰ 1 soatlik" if interval == "3600" else "⏰ 1 kunlik"
            enabled_text = "✅ Yoqilgan" if enabled == "1" else "❌ O'chirilgan"
            await api_call(lambda: query.message.edit_text(
                f"🎁 *Bonus*\n{DIVIDER}\n📊 {enabled_text}\n⏰ {interval_text}\n💰 *{bmin}-{bmax} so'm*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏰ 1 soat", callback_data="bonus_int_3600"),
                     InlineKeyboardButton("⏰ 1 kun", callback_data="bonus_int_86400")],
                    [InlineKeyboardButton("💰 Miqdor", callback_data="bonus_amount")],
                    [InlineKeyboardButton("✅/❌ Yoqish", callback_data="bonus_toggle")],
                    [InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")],
                ])), action_desc="bs")
            return
        if data.startswith("bonus_int_"):
            await set_setting("bonus_interval", data.split("_")[-1])
            await api_call(lambda: query.message.edit_text("✅", reply_markup=back_keyboard()), action_desc="bi")
            return
        if data == "bonus_amount":
            context.user_data["state"] = "bonus_amount"
            await api_call(lambda: query.message.edit_text("💰 `min max`", parse_mode=ParseMode.MARKDOWN), action_desc="ba_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="ba_kb")
            return
        if data == "bonus_toggle":
            cur = await get_setting("bonus_enabled", "1")
            await set_setting("bonus_enabled", "0" if cur == "1" else "1")
            await api_call(lambda: query.message.edit_text("✅", reply_markup=back_keyboard()), action_desc="bt")
            return
        if data == "admin_create_promo":
            context.user_data["state"] = "create_promo"
            await api_call(lambda: query.message.edit_text("🎟 `KOD miqdor max`\nMasalan: `SALE 5000 100`", parse_mode=ParseMode.MARKDOWN), action_desc="cp_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="cp_kb")
            return
        if data == "admin_list_promos":
            promos = await get_all_promocodes()
            if not promos:
                await api_call(lambda: query.message.edit_text("📋 Bo'sh", reply_markup=back_keyboard()), action_desc="lp_e")
                return
            await api_call(lambda: query.message.edit_text("🎟 Promokodlar:", reply_markup=promo_list_keyboard(promos)), action_desc="lp")
            return
        if data == "admin_payment_channel":
            pc = await get_payment_channel()
            if pc:
                await api_call(lambda: query.message.edit_text(
                    f"💳 *{pc[1]}*\n`{pc[0]}`\n{pc[2]}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✏️ O'zgartirish", callback_data="set_pay_channel")],
                        [InlineKeyboardButton("‹ Orqaga", callback_data="admin_back")],
                    ])), action_desc="pc_i")
            else:
                context.user_data["state"] = "set_payment_channel"
                await api_call(lambda: query.message.edit_text("💳 `@username Nomi|Tavsif`", parse_mode=ParseMode.MARKDOWN), action_desc="pc_p")
                await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="pc_kb")
                return
        if data == "set_pay_channel":
            context.user_data["state"] = "set_payment_channel"
            await api_call(lambda: query.message.edit_text("💳 `@username Nomi|Tavsif`", parse_mode=ParseMode.MARKDOWN), action_desc="spc_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="spc_kb")
            return
        if data == "admin_stats":
            count, total = await get_stats()
            await api_call(lambda: query.message.edit_text(f"📊 *{count}* ta foydalanuvchi\n💰 *{total:,.0f} so'm*", parse_mode=ParseMode.MARKDOWN, reply_markup=back_keyboard()), action_desc="as")
            return
        if data == "admin_broadcast":
            context.user_data["state"] = "broadcast"
            await api_call(lambda: query.message.edit_text("✍️ Xabar:", parse_mode=ParseMode.MARKDOWN), action_desc="bc_p")
            await api_call(lambda: query.message.chat.send_message("👇", reply_markup=cancel_keyboard()), action_desc="bc_kb")
            return
        if data == "admin_leave_ownership":
            await api_call(lambda: query.message.edit_text("⚠️ *Egallikdan chiqasizmi?*", parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_leave_1()), action_desc="lv1")
            return
        if data == "leave_cancel":
            await open_admin_panel(query, edit=True)
            return
        if data == "leave_yes_1":
            await api_call(lambda: query.message.edit_text("🤔 *100% ishonchingiz komilmi?*", parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_leave_2()), action_desc="lv2")
            return
        if data == "leave_yes_2":
            await api_call(lambda: query.message.edit_text("😢 *Rostdan ham?*", parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_leave_3()), action_desc="lv3")
            return
        if data == "leave_yes_3":
            context.user_data["left_ownership"] = True
            await api_call(lambda: query.message.edit_text("✅ Chiqdingiz!", parse_mode=ParseMode.MARKDOWN), action_desc="lvd")
            for aid in await get_admins():
                await api_call(lambda a=aid: context.bot.send_message(chat_id=a, text=f"⚠️ {query.from_user.full_name} chiqdi!", parse_mode=ParseMode.MARKDOWN), action_desc="lvn")
            return
        if data.startswith("rmch:"):
            cid = data.split(":", 1)[1]
            await remove_channel_db(cid)
            cache_clear_sub()
            channels = await get_channels()
            if channels:
                await api_call(lambda: query.message.edit_text("✅", reply_markup=remove_channel_keyboard(channels)), action_desc="rch_o")
            else:
                await api_call(lambda: query.message.edit_text("✅ Bo'sh", reply_markup=back_keyboard()), action_desc="rch_e")
            return
        if data.startswith("rmadm:"):
            aid = int(data.split(":", 1)[1])
            if await remove_admin_db(aid):
                await api_call(lambda: query.answer("✅"), action_desc="ra_a")
                others = [a for a in await get_admins() if a != SUPER_ADMIN]
                if not others:
                    await api_call(lambda: query.message.edit_text("Yo'q", reply_markup=back_keyboard()), action_desc="ra_ne")
                else:
                    await api_call(lambda: query.message.edit_text("🚫", reply_markup=admin_list_keyboard(await get_admins())), action_desc="ra_nl")
            return
        if data.startswith("delpromo:"):
            pid = int(data.split(":", 1)[1])
            await delete_promocode(pid)
            await api_call(lambda: query.answer("✅"), action_desc="dp_a")
            await api_call(lambda: query.message.edit_text("🎟:", reply_markup=promo_list_keyboard(await get_all_promocodes())), action_desc="dp_e")
            return
    except Exception:
        logger.exception("admin_panel xato")
# ========================================================
#  ADMIN MATN STATE, ASOSIY BUTTONLAR, ERROR HANDLER, MAIN
# ========================================================

async def handle_admin_text_state(update, context, user_id, text):
    state = context.user_data.get("state")
    if state not in ("add_channel", "set_price", "broadcast", "add_admin",
                     "add_money", "remove_money", "maint_custom_msg", "bonus_amount",
                     "create_promo", "set_payment_channel"):
        return False
    if text == CANCEL_TEXT:
        context.user_data["state"] = None
        await api_call(lambda: update.message.reply_text("🚫 Bekor qilindi.", reply_markup=main_keyboard(user_id)), action_desc="cs")
        return True
    if state == "add_channel":
        context.user_data["state"] = None
        parts = text.strip().split(maxsplit=1)
        cid = parts[0]
        given = parts[1] if len(parts) > 1 else None
        chat = await api_call(lambda: context.bot.get_chat(cid), action_desc="gc", swallow=True, default=None)
        title = given or (chat.title if chat else cid) or cid
        await add_channel_db(cid, title)
        cache_clear_sub()
        await api_call(lambda: update.message.reply_text(f"✅ *{title}*", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="ca")
        return True
    if state == "set_price":
        context.user_data["state"] = None
        try:
            price = float(text.strip())
        except:
            await api_call(lambda: update.message.reply_text("❌ Raqam", reply_markup=main_keyboard(user_id)), action_desc="spe")
            return True
        await set_setting("ref_price", price)
        await api_call(lambda: update.message.reply_text(f"✅ *{price:,.0f}*", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="spo")
        return True
    if state == "add_admin":
        context.user_data["state"] = None
        if user_id != SUPER_ADMIN:
            await api_call(lambda: update.message.reply_text("⛔", reply_markup=main_keyboard(user_id)), action_desc="aap")
            return True
        try:
            new_admin = int(text.strip())
        except:
            await api_call(lambda: update.message.reply_text("❌ ID", reply_markup=main_keyboard(user_id)), action_desc="aae")
            return True
        await add_admin_db(new_admin)
        await api_call(lambda: update.message.reply_text(f"✅ `{new_admin}`", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="aao")
        return True
    if state == "add_money":
        context.user_data["state"] = None
        try:
            parts = text.strip().split()
            tid = int(parts[0])
            amount = float(parts[1])
        except:
            await api_call(lambda: update.message.reply_text("❌ Format", reply_markup=main_keyboard(user_id)), action_desc="ame")
            return True
        if not await get_user_by_id(tid):
            await create_user(tid, None)
        await add_balance(tid, amount)
        cache_clear_sub(tid)
        await api_call(lambda: update.message.reply_text(f"✅ *+{amount:,.0f}* → `{tid}`", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="amo")
        await api_call(lambda: context.bot.send_message(chat_id=tid, text=f"💰 *+{amount:,.0f} so'm*", parse_mode=ParseMode.MARKDOWN), action_desc="amn")
        return True
    if state == "remove_money":
        context.user_data["state"] = None
        try:
            parts = text.strip().split()
            tid = int(parts[0])
            amount = float(parts[1])
        except:
            await api_call(lambda: update.message.reply_text("❌ Format", reply_markup=main_keyboard(user_id)), action_desc="rme")
            return True
        await add_balance(tid, -amount)
        cache_clear_sub(tid)
        await api_call(lambda: update.message.reply_text(f"✅ *-{amount:,.0f}* → `{tid}`", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="rmo")
        await api_call(lambda: context.bot.send_message(chat_id=tid, text=f"💸 *-{amount:,.0f} so'm*", parse_mode=ParseMode.MARKDOWN), action_desc="rmn")
        return True
    if state == "maint_custom_msg":
        context.user_data["state"] = None
        global _bot_maintenance_msg, _bot_maintenance
        _bot_maintenance_msg = text
        _bot_maintenance = True
        await api_call(lambda: update.message.reply_text("✅ Yoqildi!", reply_markup=main_keyboard(user_id)), action_desc="mcd")
        return True
    if state == "bonus_amount":
        context.user_data["state"] = None
        try:
            parts = text.strip().split()
            bmin, bmax = int(parts[0]), int(parts[1])
        except:
            await api_call(lambda: update.message.reply_text("❌ Format", reply_markup=main_keyboard(user_id)), action_desc="bae")
            return True
        await set_setting("bonus_min", bmin)
        await set_setting("bonus_max", bmax)
        await api_call(lambda: update.message.reply_text(f"✅ *{bmin}-{bmax}*", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="bao")
        return True
    if state == "create_promo":
        context.user_data["state"] = None
        try:
            parts = text.strip().split()
            code = parts[0]
            amount = float(parts[1])
            max_uses = int(parts[2])
        except:
            await api_call(lambda: update.message.reply_text("❌ Format", reply_markup=main_keyboard(user_id)), action_desc="cpe")
            return True
        pid = await create_promocode(code, amount, max_uses)
        if pid:
            await api_call(lambda: update.message.reply_text(f"✅ *{code.upper()}* - *{amount:,.0f}* ({max_uses})", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="cpo")
        else:
            await api_call(lambda: update.message.reply_text("❌ Mavjud", reply_markup=main_keyboard(user_id)), action_desc="cpd")
        return True
    if state == "set_payment_channel":
        context.user_data["state"] = None
        try:
            parts = text.split("|", 1)
            channel_part = parts[0].strip().split(maxsplit=1)
            cid = channel_part[0]
            cname = channel_part[1] if len(channel_part) > 1 else cid
            desc = parts[1].strip() if len(parts) > 1 else "To'lovlar"
        except:
            await api_call(lambda: update.message.reply_text("❌ Format", reply_markup=main_keyboard(user_id)), action_desc="pce")
            return True
        await set_payment_channel(cid, cname, desc)
        await api_call(lambda: update.message.reply_text(f"✅ *{cname}*", parse_mode=ParseMode.MARKDOWN, reply_markup=main_keyboard(user_id)), action_desc="pco")
        return True
    if state == "broadcast":
        context.user_data["state"] = None
        user_ids = await get_all_user_ids()
        total = len(user_ids)
        progress = await api_call(lambda: update.message.reply_text(f"📤 0/{total}"), action_desc="bs")
        sent = 0
        for i, uid in enumerate(user_ids, 1):
            res = await api_call(lambda u=uid: context.bot.send_message(chat_id=u, text=text, parse_mode=ParseMode.MARKDOWN),
                action_desc=f"b:{uid}", swallow=True, default=None)
            if res:
                sent += 1
            if i % 20 == 0 or i == total:
                if progress:
                    await api_call(lambda i=i: progress.edit_text(f"📤 {i}/{total}"), action_desc="bp")
            await asyncio.sleep(0.03)
        if progress:
            await api_call(lambda: progress.edit_text(f"✅ {sent}/{total}"), action_desc="bd")
        return True
    return False


async def handle_buttons(update, context):
    try:
        if not update.message or not update.message.text:
            return
        text = update.message.text
        user_id = update.effective_user.id
        is_admin = user_id in await get_admins()
        if _bot_maintenance and user_id != SUPER_ADMIN:
            await api_call(lambda: update.message.reply_text(_bot_maintenance_msg, parse_mode=ParseMode.MARKDOWN), action_desc="mb")
            return
        if context.user_data.get("left_ownership"):
            if text == "⚙️ Admin Panel":
                await api_call(lambda: update.message.reply_text("🚪 Chiqdingiz.", parse_mode=ParseMode.MARKDOWN), action_desc="loa")
                return
        if await handle_support_state(update, context, user_id, text):
            return
        if await handle_support_reply_state(update, context, user_id, text):
            return
        if await handle_wd_reject_reason(update, context, user_id, text):
            return
        if await handle_withdraw_state(update, context, user_id, text):
            return
        if await handle_promo_state(update, context, user_id, text):
            return
        if is_admin and await handle_admin_text_state(update, context, user_id, text):
            return
        if not await is_subscribed(user_id, context):
            await show_subscription_gate(update, context)
            return
        if text == "💰 Pul ishlash":
            await handle_earn(update, context, user_id)
        elif text == "💸 Pul yechish":
            await handle_withdraw_start(update, context, user_id)
        elif text == "👤 Balans":
            await handle_balance(update, user_id)
        elif text == "📊 Statistika" and is_admin:
            await handle_stats(update)
        elif text == "📢 Xabar yuborish" and is_admin:
            context.user_data["state"] = "broadcast"
            await api_call(lambda: update.message.reply_text("✍️ Xabar:", parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_keyboard()), action_desc="bcp")
        elif text == "☎️ Murojaat":
            await handle_support_start(update, context, user_id)
        elif text == "🎁 Bonus":
            await handle_bonus(update, context, user_id)
        elif text == "🎟 Promokod":
            await handle_promo_start(update, context, user_id)
        elif text == "💳 To'lov kanali":
            await handle_payment_channel(update, context, user_id)
        elif text == "⚙️ Admin Panel" and is_admin:
            await open_admin_panel(update.message)
        else:
            await api_call(lambda: update.message.reply_text("🤔 Tanlang 👇", reply_markup=main_keyboard(user_id)), action_desc="un")
    except Exception:
        logger.exception("handle_buttons xato")


async def error_handler(update, context):
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        return
    logger.error("Xato: %s", err, exc_info=err)


def main():
    if not TOKEN:
        print("❌ BOT_TOKEN yo'q!")
        return
    asyncio.run(init_db())
    request = HTTPXRequest(connect_timeout=20.0, read_timeout=20.0, write_timeout=20.0, pool_timeout=20.0)
    get_updates_request = HTTPXRequest(connect_timeout=20.0, read_timeout=30.0, write_timeout=20.0, pool_timeout=20.0)
    app = (ApplicationBuilder().token(TOKEN).request(request).get_updates_request(get_updates_request).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(refresh_ref_callback, pattern="^refresh_ref$"))
    app.add_handler(CallbackQueryHandler(support_answer_callback, pattern="^sup_answer:"))
    app.add_handler(CallbackQueryHandler(withdraw_type_callback, pattern="^(wd_type:|wd_cancel$)"))
    app.add_handler(CallbackQueryHandler(withdraw_admin_decision_callback, pattern="^(wdok:|wdno:)"))
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^(admin_|maint_|bonus_|rmch:|rmadm:|delpromo:|set_pay_channel|leave_)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.add_error_handler(error_handler)
    print("🚀 Bot ishga tushdi!")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == '__main__':
    main()
