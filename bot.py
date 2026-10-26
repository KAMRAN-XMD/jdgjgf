import asyncio
import base64
import hashlib
import hmac
import html
import logging
import os
import re
import sqlite3
import struct
import time

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, ChatMemberUpdated, InlineKeyboardButton,
                           FSInputFile, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

logging.basicConfig(level=logging.INFO)

# ================= CONFIG (Environment Variables) =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8806722900:AAH9md6vyEWu2pXKSbbHKmgHdSNBKhpFmmA")
OTP_API_URL = os.getenv("OTP_API_URL", "https://api.omegatech.app/api/tools/Temp-number?action=otps&limit=2000")
NUM_API_URL = os.getenv("NUM_API_URL", "https://api.omegatech.app/api/tools/Temp-number?action=numbers&limit=5000")

OWNER_ID = int(os.getenv("OWNER_ID", "8342567433"))
GROUP_ID = int(os.getenv("GROUP_ID", "-1003826213412"))
NUM_CHANNEL = os.getenv("NUM_CHANNEL", "https://t.me/+zuxHmgya7ZA3ZTU0")
OTP_GROUP = os.getenv("OTP_GROUP", "https://t.me/+zuxHmgya7ZA3ZTU0")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))
DB_PATH = os.getenv("DB_PATH", "bot.db")
ADMINS = {OWNER_ID} | {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
ADMIN = F.from_user.id.in_(ADMINS)

# ================= DATABASE =================
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS numbers(
  number TEXT PRIMARY KEY, range_name TEXT, service TEXT, user_id INTEGER DEFAULT NULL);
CREATE TABLE IF NOT EXISTS seen(h TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS otps(
  id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, service TEXT,
  otp TEXT, msg TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS groups(
  chat_id INTEGER PRIMARY KEY, channel_url TEXT, group_url TEXT);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS uledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, kind TEXT, note TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS uwd(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, method TEXT,
  details TEXT, status TEXT DEFAULT 'pending', ts REAL, done_ts REAL);
""")


def _ensure_cols(table, cols):
    have = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    for name, ddl in cols:
        if name not in have:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


_ensure_cols("users", [("username", "TEXT"), ("name", "TEXT"), ("balance", "REAL DEFAULT 0"),
                       ("earned", "REAL DEFAULT 0"), ("referred_by", "INTEGER"),
                       ("ref_earn", "REAL DEFAULT 0"), ("joined", "REAL"),
                       ("verified", "INTEGER DEFAULT 0")])
_ensure_cols("numbers", [("agent_id", "INTEGER"), ("alloc_ts", "REAL")])
db.execute("INSERT OR IGNORE INTO groups VALUES(?,?,?)", (GROUP_ID, NUM_CHANNEL, OTP_GROUP))
db.commit()


def S(k, default=""):
    r = db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def setS(k, v):
    db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(v)))
    db.commit()


WELCOME_IMG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "welcome.jpg")
DEFAULT_WELCOME = (
    "👑 <b>VIP OTP BOT</b> 👑\n━━━━━━━━━━━━━━\n"
    "Your fast &amp; reliable virtual number and OTP receiving bot.\n\n"
    "✨ <b>What you can do</b>\n"
    "📱 Get a number for your favourite service\n"
    "🔑 Receive OTP codes instantly, right in this chat\n"
    "💰 Earn on every OTP you receive\n"
    "👥 Invite friends and earn a commission\n"
    "🔐 Free online 2FA code generator\n"
    "💸 Withdraw your earnings anytime\n\n"
    "🚀 <b>How to start</b>\n"
    "1️⃣ Join our channel and group below\n"
    "2️⃣ Tap ✅ <b>Verify</b>\n"
    "3️⃣ Pick a service, get a number, receive your OTP")
MENU_TEXT = ("✅ <b>Verification complete!</b>\n━━━━━━━━━━━━━━\n"
             "You are all set. Use the menu below to get a number, check traffic, "
             "invite friends or withdraw 👇")
BOT_DESC = ("👑 VIP OTP BOT\n\nFast virtual numbers and instant OTP codes.\n\n"
            "📱 Pick a service and get a number\n🔑 Live OTP delivery right in the chat\n"
            "💰 Earn on every OTP\n👥 Refer friends and earn commission\n"
            "🔐 Free 2FA code generator\n💸 Easy withdrawals\n\nTap START to begin!")
BOT_SHORT = "Fast virtual numbers & instant OTP codes. Earn on every OTP. Tap START!"

SETTINGS = {
    "user_share": ("User share % (of each OTP)", "pct", "50"),
    "ref_pct": ("Referral commission % (of each OTP)", "pct", "10"),
    "u_min_withdraw": ("Minimum withdrawal", "num", "1"),
    "support_url": ("Support link (https://t.me/...)", "url", ""),
    "welcome_text": ("Welcome message (HTML allowed, max 1000 chars)", "text", DEFAULT_WELCOME),
    "support_text": ("Support message", "text", "🆘 Need help? Contact our support team."),
}

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

CE = {}
CE_FAILS = [0]


def load_ce():
    CE.clear()
    for k, v in db.execute("SELECT k, v FROM settings WHERE k LIKE 'ce:%'").fetchall():
        CE[k[3:]] = v


B_GET, B_SEARCH = "📱 GET NUMBER", "🔍 Search Number"
B_TRAFFIC, B_2FA = "📊 TRAFFIC", "🔐 2FA ONLINE"
B_REFER, B_WD = "👥 Refer", "💸 WITHDRAWAL"
B_SUPPORT, B_ADMIN = "🆘 SUPPORT", "🛠 Admin Panel"


class AddFlow(StatesGroup):
    range_name = State()
    service = State()
    numbers = State()


class SearchFlow(StatesGroup):
    q = State()


class TwoFA(StatesGroup):
    secret = State()


class WdFlow(StatesGroup):
    amount = State()
    method = State()
    details = State()


h = html.escape


def norm(n: str) -> str:
    return re.sub(r"\D", "", str(n))


def mask(n: str) -> str:
    return n[:4] + "•••" + n[-3:] if len(n) > 8 else n


def auto_style(text):
    t = text.strip()
    if t.startswith(("⬅️", "❌", "🗑", "⏸")) or "Reject" in t or "Release" in t or "Cancel" in t:
        return "danger"
    if t.startswith(("✅", "💾", "➕", "▶️", "📢", "🔄")):
        return "success"
    return "primary"


def btn(text, data=None, url=None, style=None):
    style = style or auto_style(text)
    if data is not None:
        return InlineKeyboardButton(text=text, callback_data=data, style=style)
    return InlineKeyboardButton(text=text, url=url, style=style)


def ikb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


MENU_STYLE = {B_GET: "primary", B_SEARCH: "primary", B_TRAFFIC: "success", B_2FA: "primary",
              B_REFER: "success", B_WD: "danger", B_SUPPORT: "primary", B_ADMIN: "danger"}


def kbtn(text):
    return KeyboardButton(text=text, style=MENU_STYLE.get(text))


def menu_kb(uid):
    rows = [[kbtn(B_GET), kbtn(B_SEARCH)],
            [kbtn(B_TRAFFIC), kbtn(B_2FA)],
            [kbtn(B_REFER), kbtn(B_WD)],
            [kbtn(B_SUPPORT)]]
    if uid in ADMINS:
        rows.append([kbtn(B_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def links_kb():
    return ikb([[btn("📢 Number Channel", url=NUM_CHANNEL), btn("💬 OTP Group", url=OTP_GROUP)]])


def group_kb(chat_id: int):
    row = db.execute("SELECT channel_url, group_url FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
    ch, gp = (row if row else (NUM_CHANNEL, OTP_GROUP))
    return ikb([[btn("📢 Main Channel", url=ch), btn("💬 OTP Group", url=gp)]])


async def edit(c: CallbackQuery, text, kb=None):
    try:
        await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logging.debug("edit fail: %s", e)


def ensure_user(u, ref=None):
    if not db.execute("SELECT 1 FROM users WHERE id=?", (u.id,)).fetchone():
        rb = None
        if ref and ref.startswith("ref") and ref[3:].isdigit():
            cand = int(ref[3:])
            if cand != u.id and db.execute("SELECT 1 FROM users WHERE id=?", (cand,)).fetchone():
                rb = cand
        db.execute("INSERT INTO users(id,username,name,referred_by,joined) VALUES(?,?,?,?,?)",
                   (u.id, u.username or "", u.full_name or "", rb, time.time()))
    else:
        db.execute("UPDATE users SET username=?, name=? WHERE id=?",
                   (u.username or "", u.full_name or "", u.id))
    db.commit()


def user_balance(uid):
    r = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
    return r[0] if r and r[0] else 0.0


def release_user_numbers(uid, keep=None):
    for n, ats in db.execute("SELECT number, alloc_ts FROM numbers WHERE user_id=?", (uid,)).fetchall():
        if n == keep:
            continue
        if db.execute("SELECT 1 FROM otps WHERE number=? AND ts>=?", (n, (ats or 0) - 1)).fetchone():
            db.execute("DELETE FROM numbers WHERE number=?", (n,))
        else:
            db.execute("UPDATE numbers SET user_id=NULL, alloc_ts=NULL WHERE number=?", (n,))


def take_number(uid, number) -> bool:
    cur = db.execute("UPDATE numbers SET user_id=?, alloc_ts=? WHERE number=? "
                     "AND user_id IS NULL AND agent_id IS NULL", (uid, time.time(), number))
    if cur.rowcount != 1:
        db.commit()
        return False
    release_user_numbers(uid, keep=number)
    db.commit()
    return True


def number_text(number, rng, svc):
    return (f"✅ <b>Number Assigned</b>\n━━━━━━━━━━━━━━\n📞 <code>+{number}</code>\n"
            f"🌍 Range: {h(rng)}\n🛎 Service: {h(svc)}\n━━━━━━━━━━━━━━\n"
            f"⏳ Waiting for the OTP... it will appear right here.")


def totp(secret: str, at=None, step=30, digits=6):
    s = re.sub(r"[\s-]", "", secret).upper()
    s += "=" * (-len(s) % 8)
    key = base64.b32decode(s)
    t = time.time() if at is None else at
    hm = hmac.new(key, struct.pack(">Q", int(t // step)), "sha1").digest()
    o = hm[-1] & 0x0F
    code = (struct.unpack(">I", hm[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{code:0{digits}d}", step - int(t % step)


TFA = {}


def chan_names():
    out = []
    for u in (NUM_CHANNEL, OTP_GROUP):
        m = re.match(r"https?://t\.me/([A-Za-z0-9_]{4,})/?$", u)
        if m:
            out.append((m.group(1), u))
    return out


async def not_joined(uid):
    if S("force_join", "1") != "1" or uid in ADMINS:
        return []
    miss = []
    for name, url in chan_names():
        try:
            mem = await bot.get_chat_member("@" + name, uid)
            if mem.status in ("left", "kicked"):
                miss.append(url)
        except Exception as e:
            logging.warning("join check skip %s: %s", name, e)
    return miss


def is_verified(uid) -> bool:
    r = db.execute("SELECT verified FROM users WHERE id=?", (uid,)).fetchone()
    return bool(r and r[0])


def join_buttons():
    return [[btn("📢 Join Channel", url=NUM_CHANNEL, style="primary"),
             btn("💬 Join Group", url=OTP_GROUP, style="primary")],
            [btn("✅ Verify", "joined", style="success")]]


async def send_welcome(m: Message):
    caption = S("welcome_text", DEFAULT_WELCOME)
    kb = ikb(join_buttons())
    photo = S("welcome_photo", "") or (FSInputFile(WELCOME_IMG) if os.path.exists(WELCOME_IMG) else None)
    try:
        if photo and len(caption) <= 1024:
            return await m.answer_photo(photo, caption=caption, reply_markup=kb, parse_mode="HTML")
        if photo:
            await m.answer_photo(photo)
    except Exception as e:
        logging.warning("welcome photo failed: %s", e)
    await m.answer(caption, reply_markup=kb, parse_mode="HTML")


async def join_gate(m: Message) -> bool:
    uid = m.from_user.id
    if uid in ADMINS:
        return True
    if not is_verified(uid) or await not_joined(uid):
        await send_welcome(m)
        return False
    return True


@dp.message(Command("start"))
async def start(m: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    ensure_user(m.from_user, command.args)
    uid = m.from_user.id
    if (is_verified(uid) or uid in ADMINS) and not await not_joined(uid):
        await m.answer("👑 <b>VIP OTP BOT</b> 👑\nWelcome back! Use the menu below 👇",
                       reply_markup=menu_kb(uid), parse_mode="HTML")
        return await m.answer("📢 Our channels:", reply_markup=links_kb())
    await send_welcome(m)


@dp.callback_query(F.data == "joined")
async def joined_cb(c: CallbackQuery):
    uid = c.from_user.id
    ensure_user(c.from_user)
    if await not_joined(uid):
        return await c.answer("❌ You have not joined everything yet.", show_alert=True)
    db.execute("UPDATE users SET verified=1 WHERE id=?", (uid,))
    db.commit()
    await c.answer("✅ Verified!")
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.message.answer(MENU_TEXT, reply_markup=menu_kb(uid), parse_mode="HTML")
    await c.message.answer("📢 Our channels:", reply_markup=links_kb())


@dp.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("❌ Cancelled.", reply_markup=menu_kb(m.from_user.id))


def services_view():
    rows = db.execute("SELECT service, COUNT(*) FROM numbers WHERE user_id IS NULL AND agent_id IS NULL "
                      "GROUP BY service").fetchall()
    if not rows:
        return None
    return ("🛎 <b>Select a service:</b>",
            ikb([[btn(f"🔹 {r[0]} ({r[1]})", f"svc|{r[0]}")] for r in rows]))


@dp.message(F.text == B_GET)
async def m_get(m: Message, state: FSMContext):
    await state.clear()
    ensure_user(m.from_user)
    if not await join_gate(m):
        return
    v = services_view()
    if not v:
        return await m.answer("❌ Out of stock right now. Please try again later.")
    await m.answer(v[0], reply_markup=v[1], parse_mode="HTML")


@dp.callback_query(F.data == "get")
async def pick_service(c: CallbackQuery):
    v = services_view()
    if not v:
        return await c.answer("❌ Out of stock right now", show_alert=True)
    await edit(c, v[0], v[1])


@dp.callback_query(F.data.startswith("svc|"))
async def pick_range(c: CallbackQuery):
    svc = c.data.split("|", 1)[1]
    rows = db.execute("SELECT range_name, COUNT(*) FROM numbers WHERE service=? AND user_id IS NULL "
                      "AND agent_id IS NULL GROUP BY range_name", (svc,)).fetchall()
    if not rows:
        return await c.answer("❌ No stock", show_alert=True)
    kb = [[btn(f"🌍 {r[0]} ({r[1]})", f"rng|{svc}|{r[0]}")] for r in rows] + [[btn("⬅️ Back", "get")]]
    await edit(c, f"🌍 <b>{h(svc)}</b> — select a range:", ikb(kb))


@dp.callback_query(F.data.startswith("rng|"))
async def give_number(c: CallbackQuery):
    _, svc, rng = c.data.split("|", 2)
    ensure_user(c.from_user)
    row = db.execute("SELECT number FROM numbers WHERE service=? AND range_name=? AND user_id IS NULL "
                     "AND agent_id IS NULL ORDER BY RANDOM() LIMIT 1", (svc, rng)).fetchone()
    if not row or not take_number(c.from_user.id, row[0]):
        return await c.answer("❌ This range is out of stock", show_alert=True)
    await edit(c, number_text(row[0], rng, svc),
               ikb([[btn("🔄 Change Number", f"rng|{svc}|{rng}")],
                    [btn("💬 OTP Group", url=OTP_GROUP)]]))


@dp.message(F.text == B_SEARCH)
async def m_search(m: Message, state: FSMContext):
    await state.clear()
    if not await join_gate(m):
        return
    await state.set_state(SearchFlow.q)
    await m.answer("🔍 Send part of a number (e.g. <code>92300</code>):", parse_mode="HTML")


@dp.callback_query(F.data.startswith("take|"))
async def take_cb(c: CallbackQuery):
    n = c.data.split("|", 1)[1]
    ensure_user(c.from_user)
    r = db.execute("SELECT range_name, service FROM numbers WHERE number=?", (n,)).fetchone()
    if not r or not take_number(c.from_user.id, n):
        return await c.answer("❌ This number is no longer available", show_alert=True)
    await edit(c, number_text(n, r[0], r[1]), ikb([[btn("💬 OTP Group", url=OTP_GROUP)]]))


@dp.message(F.text == B_TRAFFIC)
async def m_traffic(m: Message, state: FSMContext):
    await state.clear()
    since = time.time() - 86400
    svc = db.execute("SELECT service, COUNT(*) c FROM otps WHERE ts>=? GROUP BY service "
                     "ORDER BY c DESC LIMIT 8", (since,)).fetchall()
    mine = db.execute("SELECT COUNT(*) FROM uledger WHERE user_id=? AND kind='otp' AND ts>=?",
                      (m.from_user.id, since)).fetchone()[0]
    t = "📊 <b>TRAFFIC (last 24 hours)</b>\n━━━━━━━━━━━━━━\n"
    t += ("\n".join(f"🔹 {h(r[0] or '—')}: <b>{r[1]}</b> OTP" for r in svc) or "No traffic yet") + "\n"
    t += f"\n👤 Your OTPs: <b>{mine}</b>"
    await m.answer(t, parse_mode="HTML")


@dp.message(F.text == B_2FA)
async def m_2fa(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(TwoFA.secret)
    await m.answer("🔐 Send your 2FA <b>Secret Key</b>:", parse_mode="HTML")


def tfa_text(secret):
    code, left = totp(secret)
    return (f"🔐 <b>2FA Code</b>\n━━━━━━━━━━━━━━\n🔑 <code>{code}</code>\n"
            f"⏱ {left} seconds left"), ikb([[btn("🔄 Refresh", "tfa")]])


@dp.callback_query(F.data == "tfa")
async def tfa_cb(c: CallbackQuery):
    sec = TFA.get(c.from_user.id)
    if not sec:
        return await c.answer("Send your secret key first", show_alert=True)
    t, kb = tfa_text(sec)
    await edit(c, t, kb)
    await c.answer()


@dp.message(F.text == B_REFER)
async def m_refer(m: Message, state: FSMContext):
    await state.clear()
    ensure_user(m.from_user)
    me = await bot.get_me()
    n = db.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (m.from_user.id,)).fetchone()[0]
    e = db.execute("SELECT COALESCE(ref_earn,0) FROM users WHERE id=?", (m.from_user.id,)).fetchone()[0]
    await m.answer(f"👥 <b>REFER & EARN</b>\n━━━━━━━━━━━━━━\n"
                   f"Invite friends and earn commission.\n\n"
                   f"🔗 Your link:\n<code>https://t.me/{me.username}?start=ref{m.from_user.id}</code>\n\n"
                   f"👤 Referrals: <b>{n}</b>\n💰 Earnings: <b>{e:.4f}</b>", parse_mode="HTML")


@dp.message(F.text == B_WD)
async def m_wd(m: Message, state: FSMContext):
    await state.clear()
    ensure_user(m.from_user)
    r = db.execute("SELECT balance, earned FROM users WHERE id=?", (m.from_user.id,)).fetchone()
    await m.answer(f"💸 <b>WITHDRAWAL</b>\n━━━━━━━━━━━━━━\n💰 Balance: <b>{(r[0] or 0):.4f}</b>",
                   reply_markup=ikb([[btn("💸 Withdraw Request", "wdreq")], [btn("📜 History", "wdhist")]]),
                   parse_mode="HTML")


@dp.callback_query(F.data == "wdhist")
async def wd_hist(c: CallbackQuery):
    rows = db.execute("SELECT id, amount, method, status, ts FROM uwd WHERE user_id=? ORDER BY id DESC LIMIT 10", (c.from_user.id,)).fetchall()
    t = "📜 <b>Withdrawal History</b>\n" + ("\n".join(f"#{r[0]} • {r[1]:.4f} • {h(r[2])} • {r[3]}" for r in rows) if rows else "No requests")
    await c.message.answer(t, parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "wdreq")
async def wd_req(c: CallbackQuery, state: FSMContext):
    mn = float(S("u_min_withdraw", "1"))
    bal = user_balance(c.from_user.id)
    if bal < mn:
        return await c.answer(f"❌ Insufficient balance. Minimum is {mn}", show_alert=True)
    await state.set_state(WdFlow.amount)
    await c.message.answer("💸 How much do you want to withdraw?")
    await c.answer()


@dp.message(F.text == B_SUPPORT)
async def m_support(m: Message, state: FSMContext):
    await state.clear()
    url = S("support_url", "") or OTP_GROUP
    await m.answer("🆘 Need help? Contact support.", reply_markup=ikb([[btn("💬 Support", url=url)]]))


@dp.message(SearchFlow.q)
async def search_q(m: Message, state: FSMContext):
    q = norm(m.text or "")
    if len(q) < 3:
        return await m.answer("❌ Enter at least 3 digits. /cancel")
    rows = db.execute("SELECT number, service, range_name FROM numbers WHERE number LIKE ? AND user_id IS NULL AND agent_id IS NULL LIMIT 10", (f"%{q}%",)).fetchall()
    await state.clear()
    if not rows:
        return await m.answer("❌ No free number found.")
    await m.answer(f"🔍 {len(rows)} numbers found:", reply_markup=ikb([[btn(f"+{r[0]} • {r[1]}", f"take|{r[0]}")] for r in rows]))


@dp.message(TwoFA.secret)
async def tfa_secret(m: Message, state: FSMContext):
    try:
        sec = (m.text or "").strip()
        t, kb = tfa_text(sec)
    except Exception:
        return await m.answer("❌ Invalid secret key.")
    TFA[m.from_user.id] = sec
    await state.clear()
    await m.answer(t, reply_markup=kb, parse_mode="HTML")


@dp.message(WdFlow.amount)
async def wd_amount(m: Message, state: FSMContext):
    try:
        amt = round(float((m.text or "").strip()), 6)
    except ValueError:
        return await m.answer("❌ Please enter a valid number.")
    await state.update_data(amount=amt)
    await state.set_state(WdFlow.method)
    await m.answer("🏦 Choose a method:", reply_markup=ikb([[btn("Easypaisa", "wdm|Easypaisa"), btn("JazzCash", "wdm|JazzCash")]]))


@dp.callback_query(WdFlow.method, F.data.startswith("wdm|"))
async def wd_method(c: CallbackQuery, state: FSMContext):
    await state.update_data(method=c.data.split("|", 1)[1])
    await state.set_state(WdFlow.details)
    await c.message.answer("📝 Enter your account number:")
    await c.answer()


@dp.message(WdFlow.details)
async def wd_details(m: Message, state: FSMContext):
    det = (m.text or "").strip()[:200]
    d = await state.get_data()
    amt, uid = d["amount"], m.from_user.id
    db.execute("UPDATE users SET balance=balance-? WHERE id=?", (amt, uid))
    wid = db.execute("INSERT INTO uwd(user_id,amount,method,details,ts) VALUES(?,?,?,?,?)", (uid, amt, d["method"], det, time.time())).lastrowid
    db.commit()
    await state.clear()
    await m.answer(f"✅ Withdrawal request #{wid} submitted.")


# ================= DUAL API POLLER (NUMBERS + OTPS) =================
NUM_KEYS = ("number", "num", "msisdn", "to", "phone", "recipient", "mobile")
MSG_KEYS = ("message", "sms", "text", "body", "content", "msg")
SVC_KEYS = ("cli", "service", "sender", "from", "app")


def pick(d: dict, keys):
    low = {k.lower(): v for k, v in d.items()}
    for k in keys:
        if k in low and low[k] not in (None, ""):
            return str(low[k])
    return ""


def find_records(data):
    if isinstance(data, list):
        return data if data and isinstance(data[0], dict) else []
    if isinstance(data, dict):
        for v in data.values():
            r = find_records(v)
            if r:
                return r
    return []


def extract_otp(msg: str) -> str:
    m = re.search(r"\b(\d{3}[- ]\d{3})\b", msg) or re.search(r"\b(\d{4,8})\b", msg)
    return m.group(1) if m else "—"


async def fetch_api(url):
    async def get(ssl):
        async with aiohttp.ClientSession() as s:
            async with s.get(url, ssl=ssl, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    return None
                return find_records(await r.json(content_type=None))
    try:
        try:
            return await get(None)
        except aiohttp.ClientSSLError:
            return await get(False)
    except Exception:
        return None


async def sync_numbers():
    while True:
        recs = await fetch_api(NUM_API_URL)
        if recs:
            for rec in recs:
                num = norm(pick(rec, NUM_KEYS))
                svc = pick(rec, SVC_KEYS) or "General"
                rng = pick(rec, ("range", "country", "operator")) or "Omega Range"
                if num:
                    db.execute("INSERT OR IGNORE INTO numbers(number, range_name, service) VALUES(?, ?, ?)", 
                               (num, rng, svc))
            db.commit()
        await asyncio.sleep(60) # Har 1 minute mein fresh numbers sync honge


async def poll_otps():
    await asyncio.sleep(3)
    while True:
        recs = await fetch_api(OTP_API_URL)
        if recs:
            for rec in recs[:10]: # Latest OTPs
                number = norm(pick(rec, NUM_KEYS))
                msg = pick(rec, MSG_KEYS).replace("\\n", "\n")
                cli = pick(rec, SVC_KEYS)
                if not number or not msg:
                    continue
                
                hh = hashlib.md5(f"{rec.get('dt', '')}{number}{msg}".encode()).hexdigest()
                if db.execute("SELECT 1 FROM seen WHERE h=?", (hh,)).fetchone():
                    continue
                db.execute("INSERT INTO seen VALUES(?)", (hh,))
                db.commit()

                await forward(number, msg, cli)
        await asyncio.sleep(POLL_SECONDS)


async def forward(number: str, msg: str, cli: str):
    otp = extract_otp(msg)
    row = db.execute("SELECT user_id, range_name, service FROM numbers WHERE number=?", (number,)).fetchone()
    svc = (row[2] if row else cli) or "Unknown"
    rng = row[1] if row else "Omega Range"

    grp = f"👑 <b>NEW OTP</b> 👑\n━━━━━━━━━━━━━━\n📞 <code>{mask(number)}</code>\n🌍 {h(rng)}\n🛎 {h(svc)}\n🔑 OTP: <code>{h(otp)}</code>\n━━━━━━━━━━━━━━\n💬 <i>{h(msg[:300])}</i>"
    for (cid,) in db.execute("SELECT chat_id FROM groups").fetchall():
        try:
            await bot.send_message(cid, grp, parse_mode="HTML", reply_markup=group_kb(cid))
        except Exception:
            pass


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN!")
    asyncio.create_task(sync_numbers())
    asyncio.create_task(poll_otps())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
