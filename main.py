# -*- coding: utf-8 -*-
"""
🎬 Movie Box Bot — আপডেটেড v4 (আর্নিং বুস্ট + বাগফিক্স সংস্করণ)

v4-এ নতুন (ইউজারের রিকোয়েস্ট অনুযায়ী):
  1. 💰 অ্যাড ১০-সেকেন্ড এনফোর্সমেন্ট: ইউজার আগে ফিরে এলে "⛔ আপনি সম্পূর্ণ ১০ সেকেন্ড
     অ্যাডটা দেখেন নি! আরো X সেকেন্ড বাকি" — আবার অ্যাডে ক্লিক করতেই হবে (প্রতি রি-ক্লিকে
     নতুন অ্যাড ইমপ্রেশন), পুরো সময় না হলে আনলক হবে না।
  2. 🔥 Watch Now গেট (মুভির নাম + স্পন্সর অ্যাড) এখন সব ইউজারের জন্য — শুধু নতুনদের জন্য নয়।
  3. 🎁 নতুন ইউজার: গেট পাস করে "বটে যান" চাপলে বটে /start open_<id> পেলোড যায় —
     স্টার্ট করার সাথে সাথেই বট নিজে থেকে সব কোয়ালিটির ফাইল পাঠিয়ে দেয়।
  4. 🖥️ ডাউনলোড পপ-আপ: ব্যানার অ্যাড ডাউনলোড বাটনের উপরে উঠে গেছে; স্পন্সর কার্ড
     ছোট (মুভি-কার্ড সাইজ) + "নিশ্চিন্তে এমনিতে ক্লিক করুন"-এর বদলে কিউরিওসিটি
     হেডলাইন (প্রতিবার র‍্যান্ডম — ইউজার ক্লিক করতে বাধ্য হয়)।
  5. 📺 লগ চ্যানেল এখন আরও রিলায়েবল: LOG_CHANNEL/LOG_CHANNEL_ID — যেকোনো env নাম
     ধরে, বট যে চ্যানেলে অ্যাডমিন সেখানে পোস্ট এলে অটো-সেট, ফেইল হলে আসল এরর দেখায়।

v3-এ ঠিক করা: Watch Now ডিপ-লিংক, আপলোড ফ্রিজ, সাল/ক্যাটাগরি মিক্সআপ, ফেক-সাকসেস।

আগের সব ফিচার অক্ষত:
  • ৩-বাটন আপলোড: Log Only / Broadcast & Log / Add File Only
  • ডিপ-লিংক (?open=<id>), ব্রডকাস্ট অটো-ডিলিট (ডিফল্ট ৭২ ঘণ্টা, রিস্টার্টেও বাঁচে)
  • অ্যাড: ৩০ মিনিট অটো-রোটেশন (সাধারণ ৩ + ১৮+ ২ লিংক হার্ডকোডেড সিড), স্পন্সর কার্ড
    (ফিডে প্রতি ৬ পোস্টে + ডিটেইলে), ব্যানার ×৩, VIP no-ad, কাউন্টডাউন
"""
import os, re, io, json, math, time, hmac, asyncio, logging, random
import html as html_mod
from hashlib import sha256
from urllib.parse import parse_qsl
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

import motor.motor_asyncio
from bson import ObjectId
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest,
                                TelegramForbiddenError)
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("moviebox")

# ---------------- কনফিগ ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MONGO_URI = (os.environ.get("MONGO_URI") or os.environ.get("MONGO_URL")
             or os.environ.get("MONGODB_URI") or "")
if not MONGO_URI and (os.environ.get("DATABASE_URL") or "").startswith("mongodb"):
    MONGO_URI = os.environ["DATABASE_URL"]
MONGO_DB_NAME = os.environ.get("MONGO_DB", "moviebox")
# 📺 লগ চ্যানেল env — একাধিক পরিচিত নাম চেক করে (যেটাই সেট থাকুক ধরবে)
LOG_CHANNEL_ENV = ""
for _k in ("LOG_CHANNEL_ID", "LOG_CHANNEL", "LOGCHAT_ID", "LOG_CHAT_ID", "LOG_ID"):
    _v = (os.environ.get(_k) or "").strip()
    if _v:
        LOG_CHANNEL_ENV = _v
        break
WEBAPP_ENV = (os.environ.get("WEBAPP_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip()
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
PORT = int(os.environ.get("PORT", "8000"))

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN সেট করা নেই! Render Environment-এ BOT_TOKEN যোগ করুন।")
if not MONGO_URI:
    raise SystemExit("❌ MongoDB URI সেট করা নেই! Render Environment-এ MONGO_URI যোগ করুন (MongoDB Atlas)।")

def _parse_ids(raw):
    try:
        return {int(x) for x in re.findall(r"-?\d+", raw or "")}
    except Exception:
        return set()

ADMIN_IDS = _parse_ids(os.environ.get("ADMIN_IDS") or os.environ.get("ADMIN_ID") or "")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()  # MemoryStorage (single instance)
router = Router()
dp.include_router(router)

mongo = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=15000)
db = mongo[MONGO_DB_NAME]

BOT_USERNAME = ""           # lifespan-এ সেট হয়
WEBHOOK_SECRET = sha256(BOT_TOKEN.encode()).hexdigest()

def utcnow():
    return datetime.utcnow()

# ---------------- অ্যাড কনফিগ (৩০ মিনিট রোটেশন) ----------------
DEFAULT_DIRECT_LINKS = [
    "https://asiafilm.org/4/7ef304669bcfc15f3404e1a5e5dcf1ba",   # Adsterra Direct Link 1
    "https://asiafilm.org/4/dd496e8de5fdbe10f2ccf3defc93c342",   # Adsterra Direct Link 2
    "https://uplcm.com/4/12080503",                              # Monetag Direct Link
]
DEFAULT_ADULT_DIRECT_LINKS = [
    "https://auctionr.org/4/1b6df569ebc094e3e8e592d2fd017b4f",   # Adult Direct Link 1
    "https://auctionr.org/4/ef5b10414fe8fdd3f602e74bcdd4225c",   # Adult Direct Link 2
]
ROTATION_SECONDS = 1800  # 🔁 প্রতি ৩০ মিনিটে অ্যাক্টিভ লিংক বদলাবে

def pick_active_link(links):
    """⏱️ সময়ভিত্তিক রোটেশন — প্রতি ৩০ মিনিটে লিস্টের পরের লিংকটি 'অ্যাক্টিভ' হয় (সার্ভার-সাইড)।"""
    if not links:
        return None
    try:
        return links[math.floor(time.time() / ROTATION_SECONDS) % len(links)]
    except Exception:
        return links[0]

# 300x250 ব্যানার অ্যাড (Web App Home + অ্যাড মোডালে)
BANNER_AD_JS = """<script>
  atOptions = {
    'key' : '091649489016302f51dd722fff20f22b',
    'format' : 'iframe',
    'height' : 250,
    'width' : 300,
    'params' : {}
  };
</script>
<script src="https://bicea.org/22/091649489016302f51dd722fff20f22b"></script>"""

# 320x50 ব্যানার অ্যাড (মুভি ডাউনলোড পপ-আপে, কোয়ালিটি বাটনের নিচে)
BANNER_AD_JS_320 = """<script>
  atOptions = {
    'key' : 'a19161feecb13d6d32cfa3a7f599f5a2',
    'format' : 'iframe',
    'height' : 50,
    'width' : 320,
    'params' : {}
  };
</script>
<script src="https://bicea.org/22/a19161feecb13d6d32cfa3a7f599f5a2"></script>"""

# 300x250 অ্যাডাল্ট ব্যানার (১৮+ মুভির ডাউনলোড পপ-আপে)
BANNER_AD_JS_ADULT = """<script>
  atOptions = {
    'key' : 'f1ac37c4ab0a5dab60781b14dd5696bb',
    'format' : 'iframe',
    'height' : 250,
    'width' : 300,
    'params' : {}
  };
</script>
<script src="https://brijmohan.org/22/f1ac37c4ab0a5dab60781b14dd5696bb"></script>"""

CATEGORIES = ["Hollywood", "Bollywood", "South Movie", "Bangla", "Web Series", "TV Show", "Anime", "Korean", "Adult Content"]
QUALITIES = ["480p", "720p", "1080p", "4K"]
PAGE_SIZE = 12
PHOTO_CACHE_DIR = "/tmp/photocache"
os.makedirs(PHOTO_CACHE_DIR, exist_ok=True)
DEFAULT_CHANNEL_URL = "https://t.me/addlist/MwbWNafSFK4yZjhl"

# ---------------- সেটিংস হেল্পার ----------------
async def get_setting(key, default=None):
    try:
        doc = await db.settings.find_one({"id": key})
        if doc and doc.get("value") is not None:
            return doc["value"]
    except Exception:
        pass
    if key == "log_channel" and LOG_CHANNEL_ENV:
        try:
            return int(LOG_CHANNEL_ENV)
        except Exception:
            return LOG_CHANNEL_ENV
    return default

async def set_setting(key, value):
    await db.settings.update_one({"id": key}, {"$set": {"value": value}}, upsert=True)

async def seed_default_ads():
    try:
        dl_cfg = await db.settings.find_one({"id": "direct_links"})
        if not dl_cfg or not dl_cfg.get("links"):
            await db.settings.update_one({"id": "direct_links"}, {"$set": {"links": DEFAULT_DIRECT_LINKS}}, upsert=True)
            print(f"💰 Seeded {len(DEFAULT_DIRECT_LINKS)} default direct ad links.")
        adl_cfg = await db.settings.find_one({"id": "adult_direct_links"})
        if not adl_cfg or not adl_cfg.get("links"):
            await db.settings.update_one({"id": "adult_direct_links"}, {"$set": {"links": DEFAULT_ADULT_DIRECT_LINKS}}, upsert=True)
            print(f"🔴 Seeded {len(DEFAULT_ADULT_DIRECT_LINKS)} default adult direct links.")
        for key, val in {"ad_count": 1, "ad_wait": 10, "bcast_del_hours": 72, "channel_url": DEFAULT_CHANNEL_URL}.items():
            cfg = await db.settings.find_one({"id": key})
            if not cfg:
                await set_setting(key, val)
        if LOG_CHANNEL_ENV:
            cfg = await db.settings.find_one({"id": "log_channel"})
            if not cfg:
                try:
                    await set_setting("log_channel", int(LOG_CHANNEL_ENV))
                    print(f"📺 Seeded log channel from env: {LOG_CHANNEL_ENV}")
                except Exception:
                    pass
        if WEBAPP_ENV:
            cfg = await db.settings.find_one({"id": "webapp_url"})
            if not cfg:
                await set_setting("webapp_url", WEBAPP_ENV)
                print(f"🌐 Seeded web app URL: {WEBAPP_ENV}")
    except Exception as e:
        print(f"Seed error: {e}")

# ---------------- অ্যাডমিন চেক ----------------
_admin_cache = {"ids": ADMIN_IDS, "at": 0.0}

async def is_admin(uid):
    if not uid:
        return False
    if uid in ADMIN_IDS:
        return True
    now = time.time()
    if now - _admin_cache["at"] > 60:
        ids = set()
        try:
            async for d in db.admins.find({}):
                ids.add(d.get("user_id"))
        except Exception:
            pass
        _admin_cache["ids"] = ADMIN_IDS | ids
        _admin_cache["at"] = now
    return uid in _admin_cache["ids"]

# ---------------- Telegram WebApp InitData ভ্যালিডেশন (HMAC) ----------------
def validate_init_data(init_data: str):
    """টেলিগ্রাম WebApp initData ভেরিফাই করে ইউজার dict রিটার্ন করে; ভুল হলে None।"""
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), sha256).digest()
        calc_hash = hmac.new(secret, data_check_string.encode(), sha256).hexdigest()
        if calc_hash != received_hash:
            return None
        user = json.loads(pairs.get("user", "{}"))
        return user if user.get("id") else None
    except Exception:
        return None

# ---------------- ব্যাকগ্রাউন্ড মেইনটেন্যান্স ----------------
async def maintenance_loop():
    """প্রতি ১০ মিনিটে: ব্রডকাস্ট অটো-ডিলিট (রিস্টার্টেও বাঁচে) + ২৪ ঘণ্টা আনলক টিটিএল + ছবি ক্যাশ ক্লিনআপ"""
    while True:
        try:
            now = utcnow()
            bdel = int(await get_setting("bcast_del_hours", 72) or 0)
            if bdel > 0:
                cutoff = now - timedelta(hours=bdel)
                async for m in db.broadcast_msgs.find({"sent_at": {"$lte": cutoff}}).limit(100):
                    try:
                        await bot.delete_message(m["chat_id"], m["message_id"])
                    except TelegramAPIError:
                        pass  # ইউজার নিজে ডিলিট করেছে / ব্লক করেছে
                    await db.broadcast_msgs.delete_one({"_id": m["_id"]})
            await db.user_unlocks.delete_many({"unlocked_at": {"$lte": now - timedelta(hours=24)}})
            async for c in db.file_cache.find({"expires_at": {"$lte": now}}).limit(200):
                try:
                    p = c.get("file_path")
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
                await db.file_cache.delete_one({"_id": c["_id"]})
        except Exception as e:
            log.warning(f"maintenance: {e}")
        await asyncio.sleep(600)

# ---------------- FastAPI + lifespan ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global BOT_USERNAME
    await seed_default_ads()
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username or ""
        log.info(f"✅ Bot started: @{BOT_USERNAME}")
    except Exception as e:
        log.warning(f"get_me failed: {e}")
    asyncio.create_task(maintenance_loop())
    if RENDER_URL:
        try:
            await bot.set_webhook(f"{RENDER_URL.rstrip('/')}/webhook",
                                  secret_token=WEBHOOK_SECRET, drop_pending_updates=True,
                                  allowed_updates=["message", "callback_query", "channel_post"])
            log.info(f"🔗 Webhook set: {RENDER_URL}/webhook")
        except Exception as e:
            log.warning(f"Webhook set failed, falling back to polling: {e}")
            asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    else:
        await bot.delete_webhook(drop_pending_updates=False)
        asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        log.info("🔄 Polling mode (local)")
    yield
    try:
        await mongo.close()
    except Exception:
        pass

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    from aiogram.types import Update
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        return JSONResponse({"ok": False}, status_code=403)
    try:
        update = Update.model_validate(await request.json(), context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as e:
        log.warning(f"webhook feed error: {e}")
    return {"ok": True}

# ================================================================
# 📤 আপলোড FSM (ফ্রিজ-প্রুফ): ফাইল → কোয়ালিটি → টাইটেল → রিলিজ সাল → পোস্টার → ক্যাটাগরি → ৩টি অ্যাকশন
#   • স্টেট-হ্যান্ডলারগুলো সবার আগে রেজিস্টার্ড (জেনেরিক হ্যান্ডলার চুরি করতে পারে না)
#   • ভুল ইনপুট দিলে বট সবসময় উত্তর দেয় — আর কখনো চুপচাপ থামে না
#   • /cancel যেকোনো ধাপে কাজ করে
# ================================================================
class Upload(StatesGroup):
    waiting_file = State()
    waiting_quality = State()
    waiting_title = State()
    waiting_year = State()
    waiting_poster = State()
    waiting_category = State()
    waiting_action = State()

def admin_kb_cancel():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ বাতিল করুন", callback_data="upl_cancel")
    kb.adjust(1)
    return kb.as_markup()

async def admin_guard(obj) -> bool:
    uid = obj.from_user.id if getattr(obj, "from_user", None) else 0
    if not await is_admin(uid):
        return False
    return True

@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel_any(m: Message, state: FSMContext):
    if not await admin_guard(m):
        return
    cur = await state.get_state()
    if cur:
        await state.clear()
        await m.reply("🚫 আপলোড বাতিল করা হয়েছে। নতুন পোস্ট দিতে ফাইল পাঠান অথবা /upload লিখুন।")
    else:
        await m.reply("কোনো চলমান আপলোড নেই। নতুন পোস্ট শুরু করতে মুভির ফাইল পাঠান অথবা /upload লিখুন।")

@router.callback_query(F.data == "upl_cancel", StateFilter("*"))
async def cb_cancel_upload(cb: CallbackQuery, state: FSMContext):
    if not await admin_guard(cb):
        return
    await state.clear()
    await cb.message.edit_text("🚫 আপলোড বাতিল করা হয়েছে।")
    await cb.answer("বাতিল ✅")

@router.message(Command("upload"), StateFilter(None))
async def cmd_upload(m: Message, state: FSMContext):
    if not await admin_guard(m):
        return
    await state.set_state(Upload.waiting_file)
    await m.reply(
        "📤 <b>নতুন পোস্ট শুরু!</b>\n\n"
        "প্রথমে মুভির ফাইল (ভিডিও/ডকুমেন্ট) পাঠান। একাধিক কোয়ালিটি দিতে পারবেন (৪৮০p/৭২০p/১০৮০p/৪K)।\n\n"
        "❌ বাতিল করতে: /cancel",
        reply_markup=admin_kb_cancel())

async def ask_quality(msg: Message, fname: str, size_mb: float):
    kb = InlineKeyboardBuilder()
    for q in QUALITIES:
        kb.button(text=f"🎞️ {q}", callback_data=f"upl_quality:{q}")
    kb.button(text="❌ বাতিল করুন", callback_data="upl_cancel")
    kb.adjust(2, 2, 1)
    await msg.reply(f"📁 ফাইল পাওয়া গেছে: <b>{html_mod.escape(fname)}</b> ({size_mb:.0f} MB)\n\nএই ফাইলের কোয়ালিটি সিলেক্ট করুন:", reply_markup=kb.as_markup())

@router.message(StateFilter(Upload.waiting_file), F.video | F.document)
async def upl_receive_file(m: Message, state: FSMContext):
    if not await admin_guard(m):
        return
    if m.video:
        fid, ftype, fname, fsize = m.video.file_id, "video", (m.video.file_name or "movie"), (m.video.file_size or 0)
    else:
        fid, ftype, fname, fsize = m.document.file_id, "document", (m.document.file_name or "movie"), (m.document.file_size or 0)
    await state.update_data(pending={"id": fid, "ftype": ftype, "name": fname, "size": round(fsize / 1e6, 1)})
    await state.set_state(Upload.waiting_quality)
    await ask_quality(m, fname, fsize / 1e6)

@router.callback_query(StateFilter(Upload.waiting_quality), F.data.startswith("upl_quality:"))
async def upl_pick_quality(cb: CallbackQuery, state: FSMContext):
    if not await admin_guard(cb):
        return
    q = cb.data.split(":", 1)[1]
    data = await state.get_data()
    pend = data.get("pending")
    if not pend:
        await state.set_state(Upload.waiting_file)
        await cb.answer("আবার ফাইল পাঠান", show_alert=True)
        return
    files = data.get("files", [])
    pend["quality"] = q
    files.append(pend)
    await state.update_data(files=files, pending=None)
    await state.set_state(Upload.waiting_file)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ ফাইল শেষ — টাইটেল দিন", callback_data="upl_files_done")
    kb.button(text="❌ বাতিল করুন", callback_data="upl_cancel")
    kb.adjust(1)
    qlist = ", ".join(f["quality"] for f in files)
    await cb.message.edit_text(f"✅ <b>{q}</b> ফাইল যোগ হয়েছে। (মোট {len(files)}টা — {qlist})\n\n➕ আরেকটা কোয়ালিটির ফাইল পাঠান, অথবা নিচের বাটন চাপুন:", reply_markup=kb.as_markup())
    await cb.answer("যোগ হয়েছে ✅")

@router.callback_query(StateFilter(Upload.waiting_file), F.data == "upl_files_done")
async def upl_files_done(cb: CallbackQuery, state: FSMContext):
    if not await admin_guard(cb):
        return
    data = await state.get_data()
    if not data.get("files"):
        await cb.answer("আগে অন্তত একটা ফাইল পাঠান!", show_alert=True)
        return
    await state.set_state(Upload.waiting_title)
    await cb.message.edit_text(
        "📝 <b>মুভির টাইটেল লিখুন</b>\n\n(যেমন: Djinn 2025 Bengali Dubbed)\n❌ বাতিল: /cancel")
    await cb.answer()

@router.message(StateFilter(Upload.waiting_title), F.text & ~F.text.startswith("/"))
async def upl_title(m: Message, state: FSMContext):
    if not await admin_guard(m):
        return
    title = (m.text or "").strip()
    if not title:
        await m.reply("⚠️ টাইটেল খালি রাখা যাবে না — আবার লিখুন:")
        return  # স্টেট অপরিবর্তিত — আবার লিখতে পারবে (ফ্রিজ নেই)
    if len(title) > 120:
        title = title[:120]
    await state.update_data(title=title)
    await state.set_state(Upload.waiting_year)
    await m.reply(f"✅ টাইটেল: <b>{html_mod.escape(title)}</b>\n\n📅 <b>রিলিজ সাল লিখুন</b>\n(যেমন: 2024)\n❌ বাতিল: /cancel")

@router.message(StateFilter(Upload.waiting_year), F.text)
async def upl_year(m: Message, state: FSMContext):
    if not await admin_guard(m):
        return
    # 🆕 ফিক্স: যেকোনো টেক্সট থেকে ৪ ডিজিটের সাল খুঁজে নেয় (যেমন "movie 2024" লিখলেও হবে)
    mt = re.search(r"(19|20)\d{2}", m.text or "")
    if not mt:
        await m.reply("⚠️ সালটা বোঝা যায়নি। ৪ ডিজিটের রিলিজ সাল লিখুন (যেমন: <b>2024</b>):")
        return  # স্টেট অপরিবর্তিত — যতবার খুশি আবার লেখা যাবে, বট থামবে না
    year = mt.group(0)
    await state.update_data(year=year)
    await state.set_state(Upload.waiting_poster)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭️ পোস্টার ছাড়া রাখুন", callback_data="upl_skip_poster")
    kb.button(text="❌ বাতিল করুন", callback_data="upl_cancel")
    kb.adjust(1)
    await m.reply(f"✅ রিলিজ সাল: <b>{year}</b>\n\n🖼️ এখন মুভির <b>পোস্টার/কভার ছবি</b> পাঠান\n(ছবি না থাকলে ⏭️ স্কিপ চাপুন):", reply_markup=kb.as_markup())

@router.callback_query(StateFilter(Upload.waiting_poster), F.data == "upl_skip_poster")
async def upl_skip_poster(cb: CallbackQuery, state: FSMContext):
    if not await admin_guard(cb):
        return
    await state.update_data(photo_id=None)
    await ask_categories(cb.message, state)
    await cb.answer("পোস্টার ছাড়া হবে")

@router.message(StateFilter(Upload.waiting_poster), F.photo)
async def upl_poster(m: Message, state: FSMContext):
    if not await admin_guard(m):
        return
    await state.update_data(photo_id=m.photo[-1].file_id)
    await m.reply("✅ পোস্টার নেওয়া হয়েছে।")
    await ask_categories(m, state)

@router.message(StateFilter(Upload.waiting_poster), F.text)
async def upl_poster_text(m: Message, state: FSMContext):
    if not await admin_guard(m):
        return
    await m.reply("⚠️ এই ধাপে <b>ছবি (Photo)</b> পাঠান, অথবা নিচের ⏭️ স্কিপ বাটন চাপুন। (কমান্ড দিতে চাইলে /cancel)")

async def ask_categories(msg: Message, state: FSMContext):
    await state.update_data(selected_cats=[])
    await state.set_state(Upload.waiting_category)
    kb = categories_kb([])
    await msg.reply("🏷️ <b>ক্যাটাগরি সিলেক্ট করুন</b> (একাধিক সিলেক্ট করা যাবে, ✅ চিহ্ন = সিলেক্টেড):\n⚠️ ১৮+ মুভি হলে অবশ্যই <b>Adult Content</b> সিলেক্ট করুন!", reply_markup=kb)

def categories_kb(selected):
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(CATEGORIES):
        mark = "✅" if c in selected else "◻️"
        kb.button(text=f"{mark} {c}", callback_data=f"cat_toggle:{i}")
    kb.button(text="✅ সম্পন্ন — পোস্ট অপশন দেখাও", callback_data="cat_done")
    kb.button(text="❌ বাতিল করুন", callback_data="upl_cancel")
    kb.adjust(2, 2, 2, 2, 1, 1)
    return kb.as_markup()

@router.callback_query(StateFilter(Upload.waiting_category), F.data.startswith("cat_toggle:"))
async def cat_toggle(cb: CallbackQuery, state: FSMContext):
    if not await admin_guard(cb):
        return
    idx = int(cb.data.split(":")[1])
    if idx < 0 or idx >= len(CATEGORIES):
        await cb.answer()
        return
    data = await state.get_data()
    sel = data.get("selected_cats", [])
    c = CATEGORIES[idx]
    sel = [x for x in sel if x != c] if c in sel else sel + [c]
    await state.update_data(selected_cats=sel)
    await cb.message.edit_reply_markup(reply_markup=categories_kb(sel))
    await cb.answer(c)

@router.callback_query(StateFilter(Upload.waiting_category), F.data == "cat_done")
async def cat_done(cb: CallbackQuery, state: FSMContext):
    if not await admin_guard(cb):
        return
    data = await state.get_data()
    sel = data.get("selected_cats", [])
    if not sel:
        await cb.answer("অন্তত একটা ক্যাটাগরি সিলেক্ট করুন!", show_alert=True)
        return
    await state.set_state(Upload.waiting_action)
    files = data.get("files", [])
    quals = ", ".join(f["quality"] for f in files)
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Log Only (No Broadcast)", callback_data="action_log_only")
    kb.button(text="🚀 New Movie (Broadcast & Log)", callback_data="action_bcast_log")
    kb.button(text="➕ Add File Only (No Broadcast)", callback_data="action_add_only")
    kb.button(text="❌ বাতিল করুন", callback_data="upl_cancel")
    kb.adjust(1)
    await cb.message.edit_text(
        "✅ <b>সব তথ্য রেডি!</b>\n\n"
        f"📝 টাইটেল: <b>{html_mod.escape(data.get('title', ''))}</b>\n"
        f"📅 রিলিজ সাল: <b>{data.get('year', 'N/A')}</b>\n"
        f"🏷️ ক্যাটাগরি: <b>{html_mod.escape(', '.join(sel))}</b>\n"
        f"🎞️ ফাইল: <b>{quals}</b> ({len(files)}টা)\n"
        f"🖼️ পোস্টার: {'✅ আছে' if data.get('photo_id') else '❌ নেই'}\n\n"
        "নিচের ৩টা বাটন থেকে একটা বেছে নিন:",
        reply_markup=kb.as_markup())
    await cb.answer()

# স্টেটে ভুল ধরনের কনটেন্ট এলে বট সবসময় জবাব দেবে — কোথাও ফ্রিজ হবে না
@router.message(StateFilter(Upload.waiting_file, Upload.waiting_quality, Upload.waiting_title, Upload.waiting_year, Upload.waiting_poster, Upload.waiting_action))
async def upl_state_fallback(m: Message, state: FSMContext):
    if not await admin_guard(m):
        return
    cur = (await state.get_state() or "").split(":")[-1]
    hints = {
        "waiting_file": "📁 এখন মুভির ফাইল (ভিডিও/ডকুমেন্ট) পাঠান, অথবা ✅ ফাইল শেষ বাটন চাপুন।",
        "waiting_quality": "🎞️ উপরের বাটন থেকে কোয়ালিটি সিলেক্ট করুন।",
        "waiting_title": "📝 মুভির টাইটেল <b>টেক্সট হিসেবে</b> লিখুন।",
        "waiting_year": "📅 রিলিজ সাল লিখুন (যেমন: 2024)।",
        "waiting_poster": "🖼️ পোস্টার <b>ছবি (Photo)</b> হিসেবে পাঠান অথবা ⏭️ স্কিপ চাপুন।",
        "waiting_action": "✅ নিচের ৩টা বাটন থেকে একটা বেছে নিন অথবা /cancel দিন।",
    }
    await m.reply(f"⚠️ {hints.get(cur, 'উপরের নির্দেশনা অনুযায়ী পাঠান।')}\n\nবাতিল করতে: /cancel")

# ================================================================
# ✅ পোস্ট ফাইনালাইজ + লগ চ্যানেল + ব্রডকাস্ট
# ================================================================
def build_caption(doc):
    """📌 ক্যাপশনে টাইটেল / রিলিজ সাল / ক্যাটাগরি / কোয়ালিটি — আলাদা আলাদা লাইনে
    (আগে সালের জায়গায় ক্যাটাগরি চলে যাওয়ার বাগের ফিক্স)"""
    cats = " | ".join(doc.get("categories", [])) or "N/A"
    quals = " | ".join(f.get("quality", "?") for f in doc.get("files", [])) or "Full Movie"
    lines = [
        f"🎬 <b>{html_mod.escape(str(doc.get('title', 'Movie')))}</b>",
        f"📅 রিলিজ: <b>{doc.get('year') or 'N/A'}</b>",
        f"🏷️ ক্যাটাগরি: {html_mod.escape(cats)}",
        f"🎞️ কোয়ালিটি: {html_mod.escape(quals)}",
        "",
        "📥 ডাউনলোড করতে নিচের <b>▶️ Watch Now</b> বাটনে ক্লিক করুন!",
    ]
    if BOT_USERNAME:
        lines.append(f"🤖 @{BOT_USERNAME}")
    return "\n".join(lines)

async def watch_now_kb(movie_id: str):
    """▶️ Watch Now বাটন — Web App ডিপ-লিংক সহ।
    HTTPS URL থাকলে web_app বাটন (অ্যাপ ভেতরে খোলে), না হলে বট লিংক fallback।"""
    kb = InlineKeyboardBuilder()
    url = str(await get_setting("webapp_url", "") or "").strip()
    if url.startswith("https://"):
        kb.button(text="▶️ Watch Now", web_app=WebAppInfo(url=f"{url.rstrip('/')}/?open={movie_id}"))
    elif url.startswith("http://"):
        # web_app বাটনে শুধু HTTPS চলে — ব্রাউজার লিংক fallback
        kb.button(text="▶️ Watch Now", url=f"{url.rstrip('/')}/?open={movie_id}")
    elif BOT_USERNAME:
        # ওয়েব অ্যাপ URL না থাকলে বট ডিপ-লিংক ফলব্যাক — /start করলেই সাথে সাথে ফাইল
        kb.button(text="▶️ Watch Now", url=f"https://t.me/{BOT_USERNAME}?start=open_{movie_id}")
    kb.adjust(1)
    return kb.as_markup()

def app_open_kb():
    kb = InlineKeyboardBuilder()
    url = str(WEBAPP_ENV or "").strip()
    if url.startswith("https://"):
        kb.button(text="🎬 মুভি অ্যাপ খুলুন", web_app=WebAppInfo(url=url))
    elif BOT_USERNAME:
        kb.button(text="🎬 মুভি অ্যাপ খুলুন", url=f"https://t.me/{BOT_USERNAME}")
    kb.adjust(1)
    return kb.as_markup()

async def save_movie(data):
    """টাইটেল রেজিস্টার্ড করে মুভি সেভ/আপডেট করে। রিটার্ন: (doc, created_bool)"""
    title = (data.get("title") or "").strip()
    files = data.get("files", [])
    existing = await db.movies.find_one({"title": {"$regex": f"^{re.escape(title)}$", "$options": "i"}})
    if existing:
        await db.movies.update_one(
            {"_id": existing["_id"]},
            {"$addToSet": {"files": {"$each": files}},
             "$set": {"year": data.get("year"), "categories": data.get("selected_cats", []),
                      **({"photo_id": data["photo_id"]} if data.get("photo_id") else {})}})
        doc = await db.movies.find_one({"_id": existing["_id"]})
        return doc, False
    doc = {
        "_id": ObjectId(), "title": title, "year": data.get("year"),
        "categories": data.get("selected_cats", []), "photo_id": data.get("photo_id"),
        "files": files, "clicks": 0, "created_at": utcnow(),
    }
    await db.movies.insert_one(doc)
    return doc, True

def post_link(chat, message_id):
    """পোস্ট সফল হলে অ্যাডমিনকে ক্লিকযোগ্য লিংক দেয় — চোখে দেখে নিশ্চিত হওয়ার জন্য"""
    cid = str(chat.id)
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{message_id}"
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return ""

async def post_to_log_channel(doc):
    """📺 লগ চ্যানেলে পোস্ট — আসল এরর রিপোর্ট সহ।
    রিটার্ন: (ok_bool, message_text_for_admin)"""
    ch = await get_setting("log_channel", None)
    if not ch:
        return False, ("লগ চ্যানেল সেট করা নেই!\n"
                       "সেট করতে: চ্যানেলের যেকোনো পোস্ট আমাকে <b>ফরওয়ার্ড</b> করুন — আমি চ্যানেল আইডি দেখাবো,\n"
                       "তারপর <code>/setlogchannel &lt;chat_id&gt;</code> দিন।")
    kb = await watch_now_kb(str(doc["_id"]))
    try:
        if doc.get("photo_id"):
            msg = await bot.send_photo(ch, doc["photo_id"], caption=build_caption(doc), reply_markup=kb)
        else:
            msg = await bot.send_message(ch, build_caption(doc), reply_markup=kb)
        link = post_link(msg.chat, msg.message_id)
        ok_text = f"✅ লগ চ্যানেলে পোস্ট হয়েছে — দেখুন: {link}" if link else f"✅ লগ চ্যানেলে পোস্ট হয়েছে (message_id: {msg.message_id})"
        return True, ok_text
    except TelegramForbiddenError:
        return False, ("❌ বটের চ্যানেলে পোস্ট করার পারমিশন নেই!\n"
                       "🔧 ফিক্স: চ্যানেলে গিয়ে বটকে <b>Admin</b> বানান এবং 'Post Messages' পারমিশন দিন।")
    except TelegramBadRequest as e:
        return False, f"❌ টেলিগ্রাম এরর: {html_mod.escape(str(e))}\n🔧 চ্যানেল আইডি ঠিক আছে কিনা দেখুন (/setlogchannel আবার দিন)।"
    except TelegramAPIError as e:
        return False, f"❌ টেলিগ্রাম এরর: {html_mod.escape(str(e))}"
    except Exception as e:
        return False, f"❌ অপ্রত্যাশিত এরর: {html_mod.escape(str(e))}"

async def do_broadcast(chat_id, doc, caption=None, markup=None):
    """📤 সব ইউজারকে পোস্ট — রেট-লিমিটেড + প্রোগ্রেস রিপোর্ট + অটো-ডিলিট শিডিউল (রিস্টার্টেও বাঁচে)"""
    caption = caption or build_caption(doc)
    markup = markup if markup is not None else await watch_now_kb(str(doc["_id"]))
    bdel = int(await get_setting("bcast_del_hours", 72) or 0)
    status = await bot.send_message(chat_id, "📤 ব্রডকাস্ট শুরু হয়েছে...")
    sent = failed = blocked = 0
    batch = []
    total = await db.users.count_documents({"blocked": {"$ne": True}})
    processed = 0
    async for u in db.users.find({"blocked": {"$ne": True}}, {"user_id": 1}):
        uid = u.get("user_id")
        if not uid:
            continue
        processed += 1
        try:
            if doc.get("photo_id"):
                m1 = await bot.send_photo(uid, doc["photo_id"], caption=caption, reply_markup=markup)
            else:
                m1 = await bot.send_message(uid, caption, reply_markup=markup)
            sent += 1
            if bdel > 0:
                batch.append({"chat_id": uid, "message_id": m1.message_id, "sent_at": utcnow()})
                if len(batch) >= 200:
                    await db.broadcast_msgs.insert_many(batch)
                    batch = []
        except TelegramForbiddenError:
            blocked += 1
            await db.users.update_one({"user_id": uid}, {"$set": {"blocked": True}})
        except TelegramAPIError:
            failed += 1
        if sent % 25 == 0 and sent > 0:
            await asyncio.sleep(1)  # ~২৫/সেকেন্ড — টেলিগ্রাম লিমিট সেফ
        if processed % 500 == 0:
            try:
                await status.edit_text(f"📤 প্রগ্রেস: {processed}/{total} — সফল: {sent}, ব্লকড: {blocked}, ফেইল: {failed}")
            except TelegramAPIError:
                pass
    if batch:
        await db.broadcast_msgs.insert_many(batch)
    try:
        await status.edit_text(
            f"✅ ব্রডকাস্ট শেষ!\n\n📨 সফল: <b>{sent}</b>\n🚫 ব্লক করেছে (বট বন্ধ): {blocked}\n⚠️ ফেইল: {failed}\n\n"
            + (f"🗑️ এই পোস্টগুলো <b>{bdel} ঘণ্টা</b> পর অটো-ডিলিট হবে।" if bdel > 0 else "🗑️ অটো-ডিলিট অফ আছে।"))
    except TelegramAPIError:
        pass

@router.callback_query(StateFilter(Upload.waiting_action), F.data == "action_log_only")
async def action_log_only(cb: CallbackQuery, state: FSMContext):
    if not await admin_guard(cb):
        return
    data = await state.get_data()
    await state.clear()
    doc, created = await save_movie(data)
    db_note = "🆕 নতুন মুভি DB-তে সেভ হয়েছে" if created else "➕ একই টাইটেলের মুভিতে ফাইল যোগ হয়েছে"
    ok, detail = await post_to_log_channel(doc)
    await cb.message.edit_text(f"{db_note} (ID: <code>{doc['_id']}</code>)\n\n{detail}")
    await cb.answer("সম্পন্ন ✅")

@router.callback_query(StateFilter(Upload.waiting_action), F.data == "action_bcast_log")
async def action_bcast_log(cb: CallbackQuery, state: FSMContext):
    if not await admin_guard(cb):
        return
    data = await state.get_data()
    await state.clear()
    doc, created = await save_movie(data)
    db_note = "🆕 নতুন মুভি DB-তে সেভ হয়েছে" if created else "➕ একই টাইটেলের মুভিতে ফাইল যোগ হয়েছে"
    ok, detail = await post_to_log_channel(doc)
    await cb.message.edit_text(f"{db_note} (ID: <code>{doc['_id']}</code>)\n\n{detail}\n\n📤 ব্রডকাস্ট শুরু হচ্ছে...")
    await cb.answer("ব্রডকাস্ট চলছে...")
    asyncio.create_task(do_broadcast(cb.message.chat.id, doc))

@router.callback_query(StateFilter(Upload.waiting_action), F.data == "action_add_only")
async def action_add_only(cb: CallbackQuery, state: FSMContext):
    if not await admin_guard(cb):
        return
    data = await state.get_data()
    await state.clear()
    doc, created = await save_movie(data)
    note = "🆕 নতুন মুভি শুধু DB/অ্যাপে যোগ হয়েছে" if created else "➕ একই টাইটেলের মুভিতে ফাইল যোগ হয়েছে"
    await cb.message.edit_text(f"{note} (ID: <code>{doc['_id']}</code>)\n📭 লগ বা ব্রডকাস্ট কিছুই হয়নি।")
    await cb.answer("সম্পন্ন ✅")

# স্টেট ছাড়া অ্যাডমিন ফাইল পাঠালে অটো-স্টার্ট (আগের মতোই — ফাইল পাঠালেই পোস্ট শুরু)
@router.message(StateFilter(None), F.video | F.document)
async def admin_auto_start(m: Message, state: FSMContext):
    if not await admin_guard(m):
        # নন-অ্যাডমিন ফাইল পাঠালে নীরব না থেকে ব্যবহার গাইড দেখায়
        await register_user(m.from_user.model_dump(exclude_none=True) if m.from_user else None)
        await m.reply("🎬 <b>Movie Box</b> — মুভি পেতে নিচের অ্যাপ বাটন ব্যবহার করুন!",
                      reply_markup=app_open_kb())
        return
    if m.video:
        fid, ftype, fname, fsize = m.video.file_id, "video", (m.video.file_name or "movie"), (m.video.file_size or 0)
    else:
        fid, ftype, fname, fsize = m.document.file_id, "document", (m.document.file_name or "movie"), (m.document.file_size or 0)
    await state.set_state(Upload.waiting_file)
    await state.update_data(files=[], pending={"id": fid, "ftype": ftype, "name": fname, "size": round(fsize / 1e6, 1)})
    await state.set_state(Upload.waiting_quality)
    await m.reply("📤 <b>নতুন পোস্ট শুরু হলো!</b>")
    await ask_quality(m, fname, fsize / 1e6)

# অ্যাডমিন চ্যানেল পোস্ট ফরওয়ার্ড করলে চ্যানেল আইডি দেখায় (লগ চ্যানেল সেটআপ সহজ করতে)
@router.message(StateFilter(None), F.forward_origin.chat)
async def admin_forward_channel_id(m: Message):
    if not await admin_guard(m):
        return
    ch = m.forward_origin.chat
    await m.reply(f"📺 ফরওয়ার্ড করা চ্যানেল: <b>{html_mod.escape(ch.title or '')}</b>\n"
                  f"🆔 চ্যানেল আইডি: <code>{ch.id}</code>\n\n"
                  f"লগ চ্যানেল সেট করতে লিখুন: <code>/setlogchannel {ch.id}</code>\n"
                  f"⚠️ মনে রাখবেন: বটকে ওই চ্যানেলে <b>Admin (Post Messages)</b> পারমিশন দিতে হবে!")

# ================================================================
# 👤 ইউজার কমান্ড
# ================================================================
async def register_user(u):
    if not u or not u.get("id"):
        return
    await db.users.update_one(
        {"user_id": u["id"]},
        {"$set": {"first_name": u.get("first_name", ""), "username": u.get("username", ""), "last_active": utcnow()},
         "$setOnInsert": {"first_seen": utcnow(), "vip_until": None, "downloaded": False, "blocked": False}},
        upsert=True)

async def deliver_movie_files(chat_id, movie_id):
    """🎁 /start open_<movie_id> পেলোড — গেট (অ্যাড) পাস করে আসা ইউজারকে সাথে সাথে সব ফাইল"""
    doc = None
    try:
        doc = await db.movies.find_one({"_id": ObjectId(movie_id)})
    except Exception:
        doc = None
    if not doc:
        doc = await db.movies.find_one({"title": {"$regex": f"^{re.escape(movie_id)}$", "$options": "i"}})
    if not doc:
        await bot.send_message(chat_id, "❌ মুভিটা পাওয়া যায়নি — হয়তো ডিলিট হয়ে গেছে। অ্যাপ থেকে খুঁজে নিন 👇",
                               reply_markup=app_open_kb())
        return
    files = doc.get("files", [])
    if not files:
        await bot.send_message(chat_id, "❌ এই মুভির কোনো ফাইল যোগ করা হয়নি।", reply_markup=app_open_kb())
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🎬 মুভি অ্যাপ খুলুন", web_app=WebAppInfo(url=WEBAPP_ENV)) if WEBAPP_ENV.startswith("https://") else kb.button(text="🎬 মুভি অ্যাপ খুলুন", url=f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "https://t.me")
    kb.button(text="📢 চ্যানেল জয়েন করুন", url=DEFAULT_CHANNEL_URL)
    kb.adjust(1)
    sent_any = False
    for f in files:
        cap = f"🎬 <b>{html_mod.escape(str(doc.get('title', 'Movie')))}</b> [{f.get('quality', '')}]" + (f"\n\n➕ আরও মুভি: @{BOT_USERNAME}" if BOT_USERNAME else "")
        try:
            if f.get("ftype") == "document":
                await bot.send_document(chat_id, f["id"], caption=cap)
            else:
                await bot.send_video(chat_id, f["id"], caption=cap)
            sent_any = True
        except TelegramForbiddenError:
            return  # ইউজার বট ব্লক করেছে
        except TelegramAPIError:
            continue
        await asyncio.sleep(0.6)
    if sent_any:
        # আনলক রেকর্ড + downloaded মার্ক → পরেরবার অ্যাপে এই মুভি ফ্রি, গেটও আর দেখাবে না
        await db.user_unlocks.update_one({"user_id": chat_id, "movie_id": str(doc["_id"])},
                                         {"$set": {"unlocked_at": utcnow()}}, upsert=True)
        await db.users.update_one({"user_id": chat_id}, {"$set": {"downloaded": True, "last_active": utcnow()}}, upsert=True)
        await db.movies.update_one({"_id": doc["_id"]}, {"$inc": {"clicks": 1}})
        await bot.send_message(chat_id, "✅ <b>ফাইল পাঠানো শেষ!</b>\n\n🔥 প্রতিদিন নতুন মুভি পেতে অ্যাপ খুলুন 👇", reply_markup=kb.as_markup())

@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject = None):
    await register_user(m.from_user.model_dump(exclude_none=True) if m.from_user else None)
    payload = (command.args or "").strip() if command else ""
    if payload.startswith("open_"):
        # 🎁 Watch Now (গেট পাস) → বটে এসে /start করলেই সাথে সাথে ফাইল
        await deliver_movie_files(m.chat.id, payload[5:])
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🎬 মুভি অ্যাপ খুলুন", web_app=WebAppInfo(url=WEBAPP_ENV)) if WEBAPP_ENV.startswith("https://") else kb.button(text="🎬 মুভি অ্যাপ খুলুন", url=f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "https://t.me")
    kb.button(text="📢 চ্যানেল জয়েন করুন", url=DEFAULT_CHANNEL_URL)
    kb.adjust(1)
    await m.reply(
        "👋 <b>স্বাগতম!</b>\n\n🎬 সব ধরনের মুভি, ওয়েব সিরিজ — ফ্রি ডাউনলোড!\n\n"
        "▶️ নিচের <b>মুভি অ্যাপ</b> বাটনে ক্লিক করে সব মুভি দেখুন।\n"
        "🔥 প্রতিদিন নতুন মুভি আপডেট পেতে চ্যানেল জয়েন করুন!",
        reply_markup=kb.as_markup())

# 📺 অটো-লগ-চ্যানেল: বট যে চ্যানেলে অ্যাডমিন সেখানে কেউ পোস্ট করলে এবং লগ চ্যানেল
# এখনো আনসেট থাকলে — ওই চ্যানেলটাই লগ চ্যানেল হিসেবে অটো-সেট হয়ে যায়
@router.channel_post()
async def auto_set_log_channel(m: Message):
    try:
        if await get_setting("log_channel", None):
            return
        await set_setting("log_channel", m.chat.id)
        log.info(f"📺 Auto-set log channel from channel_post: {m.chat.id}")
    except Exception:
        pass

# ================================================================
# 🛠️ অ্যাডমিন কমান্ড
# ================================================================
@router.message(Command("id"))
async def cmd_id(m: Message):
    await m.reply(f"🆔 আপনার আইডি: <code>{m.from_user.id}</code>\n💬 এই চ্যাটের আইডি: <code>{m.chat.id}</code>")

@router.message(Command("ads"))
async def cmd_ads(m: Message):
    if not await admin_guard(m):
        return
    dl_cfg = await db.settings.find_one({"id": "direct_links"}); links = dl_cfg.get("links", []) if dl_cfg else []
    adl_cfg = await db.settings.find_one({"id": "adult_direct_links"}); alinks = adl_cfg.get("links", []) if adl_cfg else []
    ac = await get_setting("ad_count", 1); aw = await get_setting("ad_wait", 10)
    bdel = int(await get_setting("bcast_del_hours", 72) or 0)
    bdel_text = "অফ (কখনো ডিলিট হবে না)" if bdel == 0 else f"{bdel} ঘণ্টা"
    ch = await get_setting("log_channel", None)
    wu = await get_setting("webapp_url", "")
    active_now = pick_active_link(links) or "—"
    active_adult_now = pick_active_link(alinks) or "—"
    text = (f"💰 <b>বর্তমান অ্যাড সেটিংস</b>\n\n"
            f"🔗 সাধারণ ডাইরেক্ট লিংক: <b>{len(links)}</b>টি\n"
            f"🔴 ১৮+ ডাইরেক্ট লিংক: <b>{len(alinks)}</b>টি\n"
            f"🔁 রোটেশন: প্রতি <b>৩০ মিনিটে</b> অ্যাক্টিভ লিংক অটো-চেঞ্জ\n"
            f"▶️ এখন অ্যাক্টিভ (সাধারণ): <code>{active_now}</code>\n"
            f"▶️ এখন অ্যাক্টিভ (১৮+): <code>{active_adult_now}</code>\n"
            f"🎯 প্রতি আনলকে অ্যাড সংখ্যা: <b>{ac}</b>টি\n"
            f"⏱️ প্রতি অ্যাডে অপেক্ষার সময়: <b>{aw} সেকেন্ড</b>\n"
            f"📢 ব্রডকাস্ট অটো-ডিলিট: <b>{bdel_text}</b>\n"
            f"📺 লগ চ্যানেল: <code>{ch if ch else '⚠️ সেট করা নেই — /setlogchannel'}</code>\n"
            f"🌐 ওয়েব অ্যাপ URL: <code>{wu if wu else '⚠️ সেট করা নেই — /setwebapp'}</code>\n\n"
            f"<i>কমান্ড: /addlink /dellink /addadultlink /setadcount /setadwait /setbcastdel /setlogchannel /setwebapp /setchannel /stats /addvip /removevip /delmovie /broadcast</i>")
    if links:
        text += "\n\n🔗 <b>সাধারণ লিংক লিস্ট:</b>\n" + "\n".join(f"{i+1}. <code>{l}</code>" for i, l in enumerate(links))
    if alinks:
        text += "\n\n🔴 <b>১৮+ লিংক লিস্ট:</b>\n" + "\n".join(f"{i+1}. <code>{l}</code>" for i, l in enumerate(alinks))
    await m.reply(text)

def _link_arg(args: str):
    parts = (args or "").split()
    return parts[1].strip() if len(parts) > 1 else ""

@router.message(Command("addlink"))
async def cmd_addlink(m: Message, command: CommandObject):
    if not await admin_guard(m):
        return
    url = _link_arg(command.args or f"addlink {m.text or ''}")
    if not url.startswith("http"):
        await m.reply("❌ ব্যবহার: <code>/addlink https://example.com/4/xxxx</code>")
        return
    await db.settings.update_one({"id": "direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
    await m.reply(f"✅ সাধারণ লিংক যোগ হয়েছে (মোট {(await db.settings.find_one({'id': 'direct_links'}))['links'].__len__()}টি)। এখন থেকে ৩০ মিনিট রোটেশনে চলবে।")

@router.message(Command("dellink"))
async def cmd_dellink(m: Message, command: CommandObject):
    if not await admin_guard(m):
        return
    url = _link_arg(command.args or f"dellink {m.text or ''}")
    if not url:
        await m.reply("❌ ব্যবহার: <code>/dellink https://example.com/4/xxxx</code>")
        return
    await db.settings.update_one({"id": "direct_links"}, {"$pull": {"links": url}}, upsert=True)
    await m.reply("✅ লিংক মুছে ফেলা হয়েছে।")

@router.message(Command("addadultlink"))
async def cmd_addadultlink(m: Message, command: CommandObject):
    if not await admin_guard(m):
        return
    url = _link_arg(command.args or f"addadultlink {m.text or ''}")
    if not url.startswith("http"):
        await m.reply("❌ ব্যবহার: <code>/addadultlink https://example.com/4/xxxx</code>")
        return
    await db.settings.update_one({"id": "adult_direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
    await m.reply(f"✅ ১৮+ লিংক যোগ হয়েছে। ৩০ মিনিট রোটেশনে চলবে।")

@router.message(Command("setadcount"))
async def cmd_setadcount(m: Message, command: CommandObject):
    if not await admin_guard(m):
        return
    try:
        n = int((command.args or "").split()[0])
    except Exception:
        await m.reply("❌ ব্যবহার: <code>/setadcount 1</code> (১-৫)")
        return
    await set_setting("ad_count", max(1, min(5, n)))
    await m.reply(f"✅ প্রতি আনলকে অ্যাড: <b>{max(1, min(5, n))}</b>টি (ডিফল্ট ১ — ইউজার-ফ্রেন্ডলি)")

@router.message(Command("setadwait"))
async def cmd_setadwait(m: Message, command: CommandObject):
    if not await admin_guard(m):
        return
    try:
        n = int((command.args or "").split()[0])
    except Exception:
        await m.reply("❌ ব্যবহার: <code>/setadwait 10</code> (সর্বনিন্ম ৫ সেকেন্ড)")
        return
    await set_setting("ad_wait", max(5, n))
    await m.reply(f"✅ অ্যাড অপেক্ষার সময়: <b>{max(5, n)} সেকেন্ড</b>")

@router.message(Command("setbcastdel"))
async def cmd_setbcastdel(m: Message, command: CommandObject):
    if not await admin_guard(m):
        return
    try:
        n = int((command.args or "").split()[0])
    except Exception:
        await m.reply("❌ ব্যবহার: <code>/setbcastdel 72</code> (ঘণ্টা; ০ = কখনো ডিলিট হবে না)")
        return
    await set_setting("bcast_del_hours", max(0, n))
    await m.reply(f"✅ ব্রডকাস্ট অটো-ডিলিট: <b>{'অফ' if n == 0 else f'{n} ঘণ্টা'}</b>\n<i>রিস্টার্ট হলেও শিডিউল বাঁচে।</i>")

@router.message(Command("setlogchannel"))
async def cmd_setlogchannel(m: Message, command: CommandObject):
    if not await admin_guard(m):
        return
    arg = _link_arg(command.args or f"setlogchannel {m.text or ''}")
    if not arg:
        await m.reply("❌ ব্যবহার: <code>/setlogchannel -1001234567890</code> (অথবা @username)\n"
                      "💡 সহজ উপায়: চ্যানেলের যেকোনো পোস্ট আমাকে ফরওয়ার্ড করুন — আইডি দেখাবো।")
        return
    val = int(arg) if arg.lstrip("-").isdigit() else arg
    await set_setting("log_channel", val)
    ok, detail = await post_to_log_channel({"_id": ObjectId(), "title": "✅ টেস্ট পোস্ট — লগ চ্যানেল সেটআপ ঠিক আছে!", "year": "", "categories": [], "files": []})
    await m.reply(f"{'✅' if ok else '⚠️'} লগ চ্যানেল সেট হয়েছে: <code>{val}</code>\n\n{detail}")

@router.message(Command("setwebapp"))
async def cmd_setwebapp(m: Message, command: CommandObject):
    if not await admin_guard(m):
        return
    arg = _link_arg(command.args or f"setwebapp {m.text or ''}")
    if not arg.startswith("http"):
        await m.reply("❌ ব্যবহার: <code>/setwebapp https://your-app.onrender.com</code>\n"
                      "⚠️ Watch Now বাটন কাজ করার জন্য এই URL দরকার + BotFather-এ ডোমেইন সেটআপ (README দেখুন)।")
        return
    await set_setting("webapp_url", arg.rstrip("/"))
    await m.reply(f"✅ ওয়েব অ্যাপ URL সেট হয়েছে: {arg}\n\nএখন সব পোস্টের Watch Now বাটনে সরাসরি মুভি খুলবে।")

@router.message(Command("setchannel"))
async def cmd_setchannel(m: Message, command: CommandObject):
    if not await admin_guard(m):
        return
    arg = _link_arg(command.args or f"setchannel {m.text or ''}")
    if not arg.startswith("http"):
        await m.reply("❌ ব্যবহার: <code>/setchannel https://t.me/yourchannel</code>")
        return
    await set_setting("channel_url", arg)
    await m.reply("✅ চ্যানেল লিংক সেট হয়েছে (অ্যাপের Join Channel বাটনে দেখাবে)।")

@router.message(Command("stats"))
async def cmd_stats(m: Message):
    if not await admin_guard(m):
        return
    now = utcnow()
    total_users = await db.users.count_documents({})
    new_today = await db.users.count_documents({"first_seen": {"$gte": now - timedelta(days=1)}})
    online = await db.users.count_documents({"last_active": {"$gte": now - timedelta(minutes=5)}})
    movies = await db.movies.count_documents({})
    unlocks_today = await db.user_unlocks.count_documents({"unlocked_at": {"$gte": now - timedelta(days=1)}})
    vip = await db.users.count_documents({"vip_until": {"$gt": now}})
    pending_del = await db.broadcast_msgs.count_documents({})
    await m.reply(f"📊 <b>স্ট্যাটস</b>\n\n👥 মোট ইউজার: <b>{total_users}</b>\n"
                  f"🆕 আজকের নতুন: {new_today}\n🟢 এখন অনলাইন (৫ মিনিট): <b>{online}</b>\n\n"
                  f"🎬 মোট মুভি: <b>{movies}</b>\n🔓 আজকের আনলক/ডাউনলোড: <b>{unlocks_today}</b>\n"
                  f"💎 অ্যাক্টিভ VIP: {vip}\n🗑️ অটো-ডিলিট পেন্ডিং পোস্ট: {pending_del}")

@router.message(Command("addvip"))
async def cmd_addvip(m: Message, command: CommandObject):
    if not await admin_guard(m):
        return
    parts = (command.args or "").split()
    try:
        uid, days = int(parts[0]), int(parts[1])
    except Exception:
        await m.reply("❌ ব্যবহার: <code>/addvip 123456789 30</code> (ইউজার আইডি + কত দিন)")
        return
    until = utcnow() + timedelta(days=days)
    await db.users.update_one({"user_id": uid}, {"$set": {"vip_until": until}}, upsert=True)
    await m.reply(f"💎 ইউজার <code>{uid}</code> এখন <b>{days} দিনের VIP</b> (অ্যাড ছাড়া সরাসরি ডাউনলোড)।")

@router.message(Command("removevip"))
async def cmd_removevip(m: Message, command: CommandObject):
    if not await admin_guard(m):
        return
    try:
        uid = int((command.args or "").split()[0])
    except Exception:
        await m.reply("❌ ব্যবহার: <code>/removevip 123456789</code>")
        return
    await db.users.update_one({"user_id": uid}, {"$set": {"vip_until": None}})
    await m.reply(f"✅ ইউজার <code>{uid}</code> এর VIP বাতিল।")

@router.message(Command("addadmin"))
async def cmd_addadmin(m: Message, command: CommandObject):
    if m.from_user.id not in ADMIN_IDS:
        return  # শুধু env-অ্যাডমিন নতুন অ্যাডমিন বানাতে পারে
    try:
        uid = int((command.args or "").split()[0])
    except Exception:
        await m.reply("❌ ব্যবহার: <code>/addadmin 123456789</code>")
        return
    await db.admins.update_one({"user_id": uid}, {"$set": {"added_at": utcnow()}}, upsert=True)
    await m.reply(f"✅ <code>{uid}</code> এখন অ্যাডমিন।")

@router.message(Command("delmovie"))
async def cmd_delmovie(m: Message, command: CommandObject):
    if not await admin_guard(m):
        return
    title = (command.args or "").strip()
    if not title:
        await m.reply("❌ ব্যবহার: <code>/delmovie Movie Title</code>")
        return
    res = await db.movies.delete_one({"title": {"$regex": f"^{re.escape(title)}$", "$options": "i"}})
    if res.deleted_count:
        await m.reply(f"✅ '<b>{html_mod.escape(title)}</b>' মুভি ডিলিট হয়েছে।")
    else:
        await m.reply("❌ এই নামে কোনো মুভি পাওয়া যায়নি।")

@router.message(Command("broadcast"))
async def cmd_broadcast(m: Message, command: CommandObject):
    if not await admin_guard(m):
        return
    text = (command.args or "").strip()
    if not text:
        await m.reply("❌ ব্যবহার: <code>/broadcast আপনার মেসেজ</code>\n(মুভি ব্রডকাস্ট করতে ফাইল পাঠিয়ে 🚀 New Movie বাটন ব্যবহার করুন)")
        return
    await m.reply("📤 টেক্সট ব্রডকাস্ট শুরু হচ্ছে...")
    asyncio.create_task(do_broadcast(m.chat.id, {"photo_id": None}, caption=text))

@router.message(Command("help"))
async def cmd_help(m: Message):
    if await admin_guard(m):
        await m.reply(
            "🛠️ <b>অ্যাডমিন কমান্ড</b>\n\n"
            "📤 পোস্ট: মুভির ফাইল পাঠান (অথবা /upload) → কোয়ালিটি → টাইটেল → সাল → পোস্টার → ক্যাটাগরি → ৩ বাটন\n"
            "❌ বাতিল: /cancel\n\n"
            "💰 /ads — সব অ্যাড সেটিংস + রোটেশন\n"
            "🔗 /addlink, /dellink — সাধারণ ডাইরেক্ট লিংক\n"
            "🔴 /addadultlink — ১৮+ লিংক\n"
            "🎯 /setadcount, /setadwait — আনলক অ্যাড সেটিং\n"
            "📢 /setbcastdel — ব্রডকাস্ট অটো-ডিলিট ঘণ্টা\n"
            "📺 /setlogchannel — লগ চ্যানেল সেট (ফরওয়ার্ড করেও আইডি পাওয়া যায়)\n"
            "🌐 /setwebapp — ওয়েব অ্যাপ URL (Watch Now বাটনের জন্য জরুরি)\n"
            "📢 /setchannel — চ্যানেল জয়েন লিংক\n"
            "📊 /stats — ইউজার/মুভি/ডাউনলোড স্ট্যাটস\n"
            "💎 /addvip /removevip — VIP ম্যানেজ\n"
            "🗑️ /delmovie — মুভি ডিলিট\n"
            "📣 /broadcast — টেক্সট ব্রডকাস্ট\n"
            "➕ /addadmin — নতুন অ্যাডমিন (শুধু মেইন অ্যাডমিন)")
    else:
        await m.reply("🎬 মুভি পেতে নিচের অ্যাপ বাটন ব্যবহার করুন!\n\n📥 ডাউনলোড: মুভি খুলে কোয়ালিটি সিলেক্ট করুন → অ্যাড দেখে আনলক → ফাইল পাবেন।\n💎 VIP: অ্যাড ছাড়া সরাসরি ডাউনলোড।",
                      reply_markup=app_open_kb())

# অ্যাডমিন ছাড়া অন্য প্রাইভেট মেসেজ → ব্যবহার গাইড
@router.message(StateFilter(None), F.private)
async def catch_all_user(m: Message):
    await register_user(m.from_user.model_dump(exclude_none=True) if m.from_user else None)
    await m.reply("🎬 <b>Movie Box</b> — সব মুভি ফ্রি!\n\n▶️ নিচের অ্যাপ বাটনে ক্লিক করে মুভি দেখুন ও ডাউনলোড করুন।",
                  reply_markup=app_open_kb())

# ================================================================
# 🌐 ওয়েব অ্যাপ API
# ================================================================
def movie_json(doc):
    return {
        "_id": str(doc["_id"]),
        "title": doc.get("title"),
        "year": doc.get("year"),
        "categories": doc.get("categories", []),
        "photo_id": doc.get("photo_id"),
        "clicks": doc.get("clicks", 0),
        "files": [{"id": f.get("id"), "quality": f.get("quality"), "ftype": f.get("ftype", "video")}
                  for f in doc.get("files", [])],
    }

def is_vip_user(u):
    return bool(u and u.get("vip_until") and u["vip_until"] > utcnow())

def adult_guard_query(adult):
    """১৮+ কনটেন্ট শুধু ভেরিফায়েড (adult=1) ইউজারকে দেখাবে"""
    if adult == "1":
        return {}
    return {"categories": {"$ne": "Adult Content"}}

@app.get("/api/list")
async def api_list(cat: str = "Home", q: str = "", uid: int = 0, page: int = 1, adult: str = "0"):
    try:
        query = adult_guard_query(adult)
        if q.strip():
            query["title"] = {"$regex": re.escape(q.strip()), "$options": "i"}
        elif cat and cat not in ("Home", "All"):
            if cat == "Adult Content" and adult != "1":
                return {"movies": [], "page": 1, "total_pages": 0, "total": 0}
            query["categories"] = cat
        total = await db.movies.count_documents(query)
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, total_pages))
        cursor = db.movies.find(query).sort([("created_at", -1), ("_id", -1)]).skip((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
        movies = [movie_json(d) async for d in cursor]
        return {"movies": movies, "page": page, "total_pages": total_pages if total else 0, "total": total}
    except Exception as e:
        log.warning(f"api_list: {e}")
        return {"movies": [], "page": 1, "total_pages": 0, "total": 0}

@app.get("/api/trending")
async def api_trending(adult: str = "0"):
    try:
        query = adult_guard_query(adult)
        cursor = db.movies.find(query).sort([("clicks", -1), ("created_at", -1)]).limit(8)
        return {"movies": [movie_json(d) async for d in cursor]}
    except Exception as e:
        log.warning(f"api_trending: {e}")
        return {"movies": []}

@app.get("/api/surprise")
async def api_surprise(adult: str = "0"):
    try:
        query = adult_guard_query(adult)
        async for d in db.movies.aggregate([{"$match": query}, {"$sample": {"size": 1}}]):
            return {"movie": movie_json(d)}
    except Exception as e:
        log.warning(f"api_surprise: {e}")
    return {"movie": None}

# ✅ ডিপ-লিংক API: একটি নির্দিষ্ট মুভি (সব ফাইলসহ) ওয়েব অ্যাপে সরাসরি খোলার জন্য
# is_new = ইউজার আগে কখনো ডাউনলোড করেনি → ওয়েব অ্যাপে Watch Now গেট দেখানো হয়
@app.get("/api/movie/{movie_id}")
async def api_single_movie(movie_id: str, uid: int = 0):
    try:
        unlocked_ids = []
        is_new = False
        if uid != 0:
            time_limit = utcnow() - timedelta(hours=24)
            async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
                unlocked_ids.append(u["movie_id"])
            udoc = await db.users.find_one({"user_id": uid}, {"downloaded": 1})
            is_new = not (udoc and udoc.get("downloaded"))
        doc = None
        try:
            doc = await db.movies.find_one({"_id": ObjectId(movie_id)})
        except Exception:
            doc = None
        if not doc:
            doc = await db.movies.find_one({"title": {"$regex": f"^{re.escape(movie_id)}$", "$options": "i"}})
        if not doc:
            return {"movie": None, "is_new": False}
        m = movie_json(doc)
        m["is_adult"] = "Adult Content" in m["categories"]
        unlocked = str(doc["_id"]) in unlocked_ids
        for f in m["files"]:
            f["is_unlocked"] = unlocked
        return {"movie": m, "is_new": is_new}
    except Exception as e:
        log.warning(f"api_movie: {e}")
        return {"movie": None, "is_new": False}

@app.get("/api/settings/public")
async def api_public_settings():
    try:
        ac = await get_setting("ad_count", 1); aw = await get_setting("ad_wait", 10)
        dl_cfg = await db.settings.find_one({"id": "direct_links"}); links = dl_cfg.get("links", []) if dl_cfg else []
        adl_cfg = await db.settings.find_one({"id": "adult_direct_links"}); alinks = adl_cfg.get("links", []) if adl_cfg else []
        cu = await get_setting("channel_url", DEFAULT_CHANNEL_URL)
        # 🔁 ৩০ মিনিট রোটেশন: এই মুহূর্তের অ্যাক্টিভ লিংক সার্ভার থেকে ঠিক করে পাঠানো হয়
        return {"ad_count": max(1, int(ac or 1)), "ad_wait": max(5, int(aw or 10)),
                "active_link": pick_active_link(links) or "", "active_adult_link": pick_active_link(alinks) or "",
                "rotation_minutes": ROTATION_SECONDS // 60,
                "channel_url": cu or DEFAULT_CHANNEL_URL, "bot_username": BOT_USERNAME}
    except Exception:
        return {"ad_count": 1, "ad_wait": 10, "active_link": "", "active_adult_link": "", "rotation_minutes": 30,
                "channel_url": DEFAULT_CHANNEL_URL, "bot_username": BOT_USERNAME}

@app.post("/api/user/ping")
async def api_user_ping(request: Request):
    try:
        d = await request.json()
        user = validate_init_data(d.get("initData", ""))
        if not user:
            return {"ok": False}
        await register_user(user)
        udoc = await db.users.find_one({"user_id": user["id"]})
        return {"ok": True, "uid": user["id"], "vip": is_vip_user(udoc), "downloaded": bool(udoc and udoc.get("downloaded")),
                "is_admin": await is_admin(user["id"])}
    except Exception as e:
        log.warning(f"api_ping: {e}")
        return {"ok": False}

@app.get("/api/fav")
async def api_fav_get(uid: int = 0):
    try:
        ids = []
        async for f in db.favorites.find({"user_id": uid}):
            try:
                ids.append(ObjectId(f["movie_id"]))
            except Exception:
                pass
        movies = [movie_json(d) async for d in db.movies.find({"_id": {"$in": ids}}).sort([("created_at", -1)])]
        return {"movies": movies, "fav_ids": [str(i) for i in ids]}
    except Exception as e:
        log.warning(f"api_fav: {e}")
        return {"movies": [], "fav_ids": []}

@app.post("/api/fav")
async def api_fav_toggle(request: Request):
    try:
        d = await request.json()
        user = validate_init_data(d.get("initData", ""))
        if not user or not d.get("movieId"):
            return {"ok": False}
        uid, mid = user["id"], str(d["movieId"])
        existing = await db.favorites.find_one({"user_id": uid, "movie_id": mid})
        if existing:
            await db.favorites.delete_one({"_id": existing["_id"]})
            return {"ok": True, "fav": False}
        await db.favorites.insert_one({"user_id": uid, "movie_id": mid, "added_at": utcnow()})
        return {"ok": True, "fav": True}
    except Exception as e:
        log.warning(f"api_fav_toggle: {e}")
        return {"ok": False}

@app.post("/api/send")
async def api_send(request: Request):
    """📁 ফাইল ডেলিভারি — initData ভেরিফাই + VIP/আনলক চেক (সার্ভার সাইড, বাইপাস করা যায় না)"""
    try:
        d = await request.json()
        user = validate_init_data(d.get("initData", ""))
        if not user or int(user["id"]) != int(d.get("userId", 0)):
            return {"ok": False, "error": "auth"}
        uid = user["id"]
        file_id = str(d.get("fileId", ""))
        doc = await db.movies.find_one({"files.id": file_id})
        if not doc:
            return {"ok": False, "error": "notfound"}
        udoc = await db.users.find_one({"user_id": uid})
        vip = is_vip_user(udoc)
        time_limit = utcnow() - timedelta(hours=24)
        unlocked = await db.user_unlocks.find_one({"user_id": uid, "movie_id": str(doc["_id"]), "unlocked_at": {"$gt": time_limit}}) is not None
        if not vip and not unlocked:
            return {"ok": False, "error": "locked"}
        fentry = next((f for f in doc.get("files", []) if f.get("id") == file_id), doc.get("files", [{}])[0] if doc.get("files") else None)
        quality = (fentry or {}).get("quality", "")
        cap = f"🎬 <b>{html_mod.escape(str(doc.get('title', 'Movie')))}</b> [{quality}]\n\n➕ আরও মুভি: @{BOT_USERNAME}" if BOT_USERNAME else f"🎬 <b>{html_mod.escape(str(doc.get('title', 'Movie')))}</b> [{quality}]"
        try:
            if (fentry or {}).get("ftype") == "document":
                await bot.send_document(uid, file_id, caption=cap)
            else:
                await bot.send_video(uid, file_id, caption=cap)
        except TelegramForbiddenError:
            return {"ok": False, "error": "blocked"}
        if not vip:
            await db.user_unlocks.update_one({"user_id": uid, "movie_id": str(doc["_id"])},
                                             {"$set": {"unlocked_at": utcnow()}}, upsert=True)
        await db.users.update_one({"user_id": uid}, {"$set": {"downloaded": True, "last_active": utcnow()}}, upsert=True)
        await db.movies.update_one({"_id": doc["_id"]}, {"$inc": {"clicks": 1}})
        return {"ok": True}
    except TelegramAPIError as e:
        log.warning(f"api_send tg: {e}")
        return {"ok": False, "error": "telegram"}
    except Exception as e:
        log.warning(f"api_send: {e}")
        return {"ok": False, "error": "server"}

@app.get("/api/image/{photo_id}")
async def api_image(photo_id: str):
    try:
        cache = await db.file_cache.find_one({"photo_id": photo_id})
        now = utcnow()
        file_path = None
        if cache and cache.get("expires_at", now) > now and cache.get("file_path") and os.path.exists(cache["file_path"]):
            file_path = cache["file_path"]
        else:
            f = await bot.get_file(photo_id)
            file_path = f.file_path
            await db.file_cache.update_one({"photo_id": photo_id},
                                           {"$set": {"file_path": file_path, "expires_at": now + timedelta(hours=1)}}, upsert=True)
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}") as r:
                data = await r.read()
        if not data:
            return Response(status_code=404)
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})
    except Exception as e:
        log.warning(f"api_image: {e}")
        return Response(status_code=404)

# ================================================================
# 🖥️ ওয়েব অ্যাপ UI
# ================================================================
WEB_UI_TEMPLATE = r'''<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>🎬 Movie Box</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
<style>
   * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
   :root { --bg: #0f172a; --card: #1e293b; --card2: #0f172a; --border: #334155; --text: #f1f5f9; --muted: #94a3b8; --accent: #f97316; --accent2: #ef4444; }
   body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', Tahoma, sans-serif; padding-bottom: 80px; min-height: 100vh; }
   body.oled-mode { --bg: #000; --card: #0a0a0a; --card2: #000; --border: #1a1a1a; }
   .container { max-width: 600px; margin: 0 auto; padding: 12px; }
   .app-header { background: linear-gradient(135deg, #f97316, #ef4444); padding: 14px 16px; font-size: 20px; font-weight: 800; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 50; }
   .app-header .admin-badge { background: rgba(0,0,0,.35); color: #fff; font-size: 11px; padding: 3px 8px; border-radius: 20px; }
   .sec-title { font-size: 16px; font-weight: 800; margin: 14px 0 8px 0; color: #fbbf24; }
   .cat-chips { display: flex; gap: 8px; overflow-x: auto; padding: 4px 0 10px 0; scrollbar-width: none; }
   .cat-chips::-webkit-scrollbar { display: none; }
   .chip { flex: 0 0 auto; background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 7px 14px; border-radius: 20px; font-size: 13px; font-weight: 600; cursor: pointer; }
   .chip.active { background: linear-gradient(135deg, #f97316, #ef4444); color: #fff; border-color: transparent; }
   .banner-box { margin: 6px 0 12px 0; display: flex; justify-content: center; }
   .movie-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
   .movie-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; cursor: pointer; position: relative; }
   .movie-card:active { transform: scale(0.97); }
   .movie-info { padding: 8px; }
   .movie-title { font-size: 13px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
   .movie-meta-row { display: flex; justify-content: space-between; align-items: center; margin-top: 6px; }
   .year-badge { background: var(--card2); border: 1px solid var(--border); color: var(--accent); font-size: 11px; font-weight: 700; padding: 2px 6px; border-radius: 6px; }
   .cat-small-tag { color: var(--muted); font-size: 10px; margin-left: 4px; }
   .fav-btn { position: absolute; top: 8px; right: 8px; background: rgba(0,0,0,.55); border: none; color: #fff; width: 32px; height: 32px; border-radius: 50%; font-size: 14px; cursor: pointer; }
   .fav-btn.active { color: #ef4444; }
   .admin-view-badge { position: absolute; top: 8px; left: 8px; background: rgba(0,0,0,.65); color: #fbbf24; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 12px; }
   .adult-lock-overlay { position: absolute; inset: 0; background: rgba(0,0,0,.45); display: flex; align-items: center; justify-content: center; font-size: 30px; color: #ef4444; }
   .trending-row { display: flex; gap: 10px; overflow-x: auto; padding-bottom: 6px; scrollbar-width: none; }
   .trending-row::-webkit-scrollbar { display: none; }
   .trend-card { flex: 0 0 140px; background: var(--card); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; cursor: pointer; position: relative; }
   .trend-card img { width: 100%; height: 80px; object-fit: cover; }
   .trend-title { font-size: 11px; font-weight: 700; padding: 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
   .trend-fire { position: absolute; top: 4px; left: 4px; font-size: 11px; background: rgba(0,0,0,.6); border-radius: 8px; padding: 1px 6px; }
   .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75); z-index: 100; align-items: center; justify-content: center; padding: 16px; }
   .modal-content { background: var(--card); border: 1px solid var(--border); border-radius: 16px; width: 100%; max-width: 420px; max-height: 88vh; overflow-y: auto; padding: 16px; position: relative; }
   .close-icon { position: absolute; top: 10px; right: 10px; background: var(--card2); border: 1px solid var(--border); color: var(--muted); width: 34px; height: 34px; border-radius: 50%; cursor: pointer; font-size: 15px; z-index: 5; }
   .detail-img { width: 100%; border-radius: 12px; aspect-ratio: 16/9; object-fit: cover; }
   .detail-title { font-size: 18px; font-weight: 800; margin-top: 10px; }
   .detail-meta { color: var(--muted); font-size: 13px; margin-top: 4px; }
   .movie-cat-tag { display: inline-block; background: var(--card2); border: 1px solid var(--border); color: var(--accent); font-size: 11px; font-weight: 700; padding: 3px 9px; border-radius: 12px; margin: 3px 4px 0 0; }
   .dl-file-btn { display: block; width: 100%; background: var(--card2); border: 2px solid var(--border); color: var(--text); padding: 12px; border-radius: 12px; font-size: 15px; font-weight: 800; margin-top: 10px; cursor: pointer; }
   .dl-file-btn.unlocked { background: linear-gradient(135deg, #f97316, #ef4444); border-color: transparent; color: #fff; }
   .dl-file-btn:active { transform: scale(0.98); }
   .ad-icon { font-size: 44px; text-align: center; }
   .ad-title { text-align: center; font-size: 20px; font-weight: 800; margin-top: 6px; }
   .ad-box-orange { background: rgba(249,115,22,.15); border: 1px solid rgba(249,115,22,.4); color: #fb923c; text-align: center; font-weight: 700; font-size: 13px; padding: 9px; border-radius: 10px; margin-top: 12px; }
   .ad-box-black { background: var(--card2); border: 1px solid var(--border); color: var(--muted); font-size: 13px; padding: 10px; border-radius: 10px; margin-top: 8px; text-align: center; line-height: 1.5; }
   .ad-warn-box { background: rgba(239,68,68,.15); border: 1px solid rgba(239,68,68,.5); color: #f87171; text-align: center; font-weight: 700; font-size: 13px; padding: 10px; border-radius: 10px; margin-top: 10px; line-height: 1.5; }
   .ad-action-btn { display: block; width: 100%; padding: 13px; border-radius: 12px; font-size: 15px; font-weight: 800; border: none; margin-top: 10px; cursor: pointer; color: #fff; }
   .btn-ad-open { background: linear-gradient(135deg, #f97316, #ef4444); }
   .btn-ad-unlock { background: linear-gradient(135deg, #16a34a, #15803d); }
   .btn-ad-tryagain { background: #475569; }
   .ad-action-btn:disabled { opacity: .55; }
   .sponsor-card { width: 100%; background: var(--card2); border: 1.5px solid var(--border); border-radius: 12px; overflow: hidden; cursor: pointer; margin-top: 10px; display: flex; align-items: stretch; transition: transform .15s; }
   .sponsor-card:active { transform: scale(0.98); }
   .sponsor-thumb { background: linear-gradient(135deg, #f97316, #ef4444); display: flex; align-items: center; justify-content: center; font-size: 24px; padding: 0 14px; flex: 0 0 auto; }
   .sponsor-inner { padding: 10px 10px; text-align: left; flex: 1; min-width: 0; }
   .sponsor-title { color: #fbbf24; font-weight: 800; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
   .sponsor-sub { color: var(--muted); font-size: 11px; margin-top: 3px; }
   .sponsor-footer { background: linear-gradient(135deg, #ff9966, #ff4b2b); color: #fff; font-weight: 800; font-size: 10px; padding: 0 12px; display: flex; align-items: center; flex: 0 0 auto; }
   .page-btn { background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 7px 12px; border-radius: 8px; font-size: 13px; font-weight: 700; cursor: pointer; }
   .page-btn.active { background: linear-gradient(135deg, #f97316, #ef4444); border-color: transparent; }
   .page-btn:disabled { opacity: .4; }
   .pagination { display: flex; gap: 6px; justify-content: center; align-items: center; margin-top: 14px; flex-wrap: wrap; }
   .skeleton { height: 120px; border-radius: 12px; background: linear-gradient(90deg, var(--card) 25%, #243247 50%, var(--card) 75%); background-size: 200% 100%; animation: sk 1.2s infinite; }
   @keyframes sk { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
   .search-bar { display: flex; gap: 8px; margin: 10px 0; }
   .search-bar input { flex: 1; background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 11px 14px; border-radius: 12px; font-size: 14px; outline: none; }
   .search-bar button { background: linear-gradient(135deg, #f97316, #ef4444); border: none; color: #fff; padding: 0 16px; border-radius: 12px; font-weight: 800; cursor: pointer; }
   .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: var(--card); border-top: 1px solid var(--border); display: flex; z-index: 60; max-width: 600px; margin: 0 auto; }
   .nav-item { flex: 1; text-align: center; padding: 10px 0 8px 0; color: var(--muted); font-size: 10px; font-weight: 700; cursor: pointer; }
   .nav-item i { display: block; font-size: 18px; margin-bottom: 3px; }
   .nav-item.active { color: var(--accent); }
   .join-channel-btn { display: block; text-align: center; background: linear-gradient(135deg, #2563eb, #1d4ed8); color: #fff; text-decoration: none; padding: 12px; border-radius: 12px; font-weight: 800; margin-top: 10px; }
   .profile-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 16px; margin-top: 12px; }
   .profile-row { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 14px; }
   .profile-row:last-child { border-bottom: none; }
   .toggle-on { color: #22c55e; font-weight: 800; }
   .toggle-off { color: var(--muted); font-weight: 700; }
   .surprise-box { text-align: center; padding: 50px 20px; }
   .vip-box { background: linear-gradient(135deg, rgba(250,204,21,.12), rgba(249,115,22,.12)); border: 1px solid rgba(250,204,21,.4); border-radius: 14px; padding: 14px; margin-top: 12px; }
   .empty-note { text-align: center; color: var(--muted); padding: 30px; font-size: 14px; }
</style>
</head>
<body>
<div class="app-header">🎬 Movie Box <span class="admin-badge" id="adminBadge" style="display:none;">ADMIN</span></div>

<div class="container">
    <!-- 🏠 HOME -->
    <div id="page-home" class="tab-page active">
        <div class="cat-chips" id="catChips"></div>
        <div class="banner-box">__BANNER_AD__</div>
        <div id="trendingSection">
            <h3 class="sec-title">🔥 ট্রেন্ডিং</h3>
            <div class="trending-row" id="trendingRow"></div>
        </div>
        <h3 class="sec-title">🆕 সাম্প্রতিক যোগ হয়েছে</h3>
        <div class="movie-grid" id="movieListHome"></div>
        <div id="paginationHome" class="pagination"></div>
    </div>

    <!-- 🔍 SEARCH -->
    <div id="page-search" class="tab-page">
        <div class="search-bar">
            <input id="searchInputMain" placeholder="🔍 মুভির নাম লিখুন..." onkeydown="if(event.key==='Enter') searchMovies(1);">
            <button onclick="searchMovies(1);">খুঁজুন</button>
        </div>
        <div class="movie-grid" id="movieListSearch"></div>
        <div id="paginationSearch" class="pagination"></div>
    </div>

    <!-- ❤️ FAVORITES -->
    <div id="page-fav" class="tab-page">
        <h3 class="sec-title">❤️ আমার প্রিয় তালিকা</h3>
        <div class="movie-grid" id="movieListFav"></div>
    </div>

    <!-- 🎁 SURPRISE -->
    <div id="page-surprise" class="tab-page">
        <div class="surprise-box">
            <div style="font-size:50px;">🎁</div>
            <h2 style="margin-top:10px;">সারপ্রাইজ মুভি!</h2>
            <p style="color:#94a3b8; margin-top:8px;">বাটন চাপলেই এলোমেলো একটা মুভি খুলে যাবে — কী পাবেন কে জানে!</p>
            <button class="dl-file-btn unlocked" style="max-width:260px; margin:20px auto 0 auto;" onclick="surpriseMovie()">⚡ সারপ্রাইজ দাও</button>
        </div>
    </div>

    <!-- 👤 PROFILE -->
    <div id="page-profile" class="tab-page">
        <div class="profile-card" id="profileInfo"></div>
        <div class="vip-box" id="vipBox"></div>
        <div class="profile-card" id="profileSettings"></div>
    </div>
</div>

<div class="bottom-nav">
    <div class="nav-item active" onclick="showTab('home')"><i class="fa-solid fa-house"></i>Home</div>
    <div class="nav-item" onclick="showTab('search')"><i class="fa-solid fa-magnifying-glass"></i>Search</div>
    <div class="nav-item" onclick="showTab('fav')"><i class="fa-solid fa-heart"></i>Favorites</div>
    <div class="nav-item" onclick="showTab('surprise')"><i class="fa-solid fa-dice"></i>Surprise</div>
    <div class="nav-item" onclick="showTab('profile')"><i class="fa-solid fa-user"></i>Profile</div>
</div>

<div id="detailModal" class="modal">
    <div class="modal-content">
        <button class="close-icon" onclick="closeModal('detailModal')"><i class="fa-solid fa-xmark"></i></button>
        <img id="detailImg" class="detail-img" src="">
        <h2 id="detailTitle" class="detail-title"></h2>
        <div id="detailMeta" class="detail-meta"></div>
        <div id="detailCats" style="margin-bottom: 10px;"></div>
        <!-- 💰 ব্যানার অ্যাড ডাউনলোড বাটনের উপরে (সাধারণ মুভিতে 320x50, ১৮+ মুভিতে অ্যাডাল্ট ব্যানার) -->
        <div id="detailBannerNormal" style="display:none; text-align:center; margin:10px 0 2px 0;">__BANNER_320__</div>
        <div id="detailBannerAdult" style="display:none; text-align:center; margin:10px 0 2px 0;">__BANNER_ADULT__</div>
        <div id="fileButtonsContainer"></div>
        <!-- 🔥 স্পন্সর অ্যাড কার্ড (ছোট কার্ড, মুভি-কার্ড সাইজ) -->
        <div id="detailSponsor"></div>
    </div>
</div>

<!-- 🎯 সবার জন্য গেট: প্রথম ক্লিকে স্পন্সর লিংক, ফিরে এসে আবার চাপলে ফাইল (নতুন ইউজার হলে বট থেকে সাথে সাথে ফাইল) -->
<div id="fileGateModal" class="modal">
    <div class="modal-content" style="text-align:center;">
        <div class="ad-icon">🎬</div>
        <h2 class="ad-title">ফাইলটি প্রস্তুত!</h2>
        <div class="ad-box-black">আপনি যে ফাইলটি চাচ্ছেন: <b id="gateMovieTitle"></b><br><br>ফাইলটি দেখার জন্য নিচের <b>Watch Now</b> বাটনে আবার ক্লিক করুন — ক্লিক করলে সাথে সাথেই ওই ফাইলে নিয়ে যাবে।</div>
        <button class="ad-action-btn btn-ad-open" id="gateWatchBtn" onclick="gateNextStep()">🎬 Watch Now</button>
        <p style="color:#64748b; font-size:12px; margin-top:8px;">✨ প্রতিদিন নতুন মুভি আপডেট পেতে চ্যানেলে জয়েন করুন।</p>
    </div>
</div>

<div id="adModal" class="modal">
    <div class="modal-content ad-box">
        <div class="ad-icon">⚠️</div>
        <h2 class="ad-title">সতর্কতা!</h2>
        <div class="ad-box-orange">ডাউনলোড করতে হলে অবশ্যই বিজ্ঞাপন দেখুন!</div>
        <div class="ad-box-black"><span id="adStepText">লিংকে ক্লিক করে বিজ্ঞাপনটি দেখুন এবং কমপক্ষে <b>১০ সেকেন্ড</b> দেখে তারপর ফিরে এসে ✅ বাটনে ক্লিক করুন।</span></div>
        <div id="adWarnBox" class="ad-warn-box" style="display:none;"></div>
        <button class="ad-action-btn btn-ad-open" id="adClickBtn" onclick="openAdLink()">বিজ্ঞাপন খুলুন</button>
        <button class="ad-action-btn btn-ad-unlock" id="adVerifyBtn" onclick="checkAdWatched()" style="display:none;">✅ অ্যাড দেখে ফিরে এসেছি</button>
        <button class="ad-action-btn btn-ad-tryagain" id="adTryAgainBtn" onclick="resetAdModal()" style="display:none;">TRY AGAIN</button>
        <p style="color:#64748b; font-size:12px; margin-top:5px;">💎 VIP হলে কোনো অ্যাড দেখতে হবে না — Profile ট্যাব থেকে VIP নিন!</p>
        <div style="display:flex; justify-content:center; margin-top:5px;">__BANNER_AD__</div>
    </div>
</div>

<div id="successModal" class="modal">
    <div class="modal-content" style="text-align:center; padding-top:40px;">
        <i class="fa-solid fa-circle-check" style="font-size:70px; color:#4ade80; margin-bottom:20px;"></i>
        <h2>ফাইল পাঠানো হয়েছে!</h2>
        <p style="color:#94a3b8; margin-top:10px;">বট চেক করুন। নতুন মুভি আপডেট পেতে চ্যানেলে জয়েন করুন!</p>
        <a id="joinChannelBtn" href="__CHANNEL_URL__" target="_blank" class="join-channel-btn">🚀 Join Channel</a>
        <button class="dl-file-btn unlocked" onclick="closeModal('successModal'); tg.close();"><i class="fa-solid fa-check"></i> বটে যান</button>
    </div>
</div>

<div id="adultModal" class="modal">
    <div class="modal-content" style="text-align:center; padding-top:30px;">
        <div class="ad-icon">🔞</div>
        <h2 class="ad-title">১৮+ ভেরিফিকেশন</h2>
        <div class="ad-box-black">এই কনটেন্ট শুধু প্রাপ্তবয়স্কদের (১৮+) জন্য। আপনি কি ১৮ বছরের বেশি?</div>
        <button class="ad-action-btn btn-ad-unlock" onclick="confirm18()">হ্যাঁ, আমি ১৮+</button>
        <button class="ad-action-btn btn-ad-tryagain" onclick="closeModal('adultModal')">না</button>
    </div>
</div>

<script>
            const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
            if (tg) { try { tg.ready(); tg.expand(); } catch(e) {} }
            const INIT_DATA = tg ? (tg.initData || '') : '';
            let uid = 0;
            try { uid = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) ? tg.initDataUnsafe.user.id : 0; } catch(e) { uid = 0; }
            const tgUser = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) ? tg.initDataUnsafe.user : null;
            
            let DIRECT_LINKS = __DL_JSON__;
            let ADULT_DIRECT_LINKS = __ADL_JSON__;
            const CHANNEL_URL = "__CHANNEL_URL__";
            const BOT_USERNAME = "__BOT_USERNAME__";
            const CATS = ["Hollywood", "Bollywood", "South Movie", "Bangla", "Web Series", "TV Show", "Anime", "Korean"];
            
            let activeCat = "All"; let isAdmin = false; let isUserVip = false; let userFavs = [];
            let currentViewMovies = []; let homeCurrentPage = 1; let searchCurrentPage = 1;
            let activeFileId = null; let activeIsAdult = false; let adStartTime = 0; let adRound = 0;
            let adSettings = {ad_count: 1, ad_wait: 10, active_link: '', active_adult_link: ''};
            let gateMovie = null; let gateStep = 0; let gateIsNew = false;
            
            function isAdultVerified() { return localStorage.getItem('isAdult') === 'true'; }
            function closeModal(id) { document.getElementById(id).style.display = 'none'; }
            
            function showTab(name) {
                ['home', 'search', 'fav', 'surprise', 'profile'].forEach(function(t) {
                    const pg = document.getElementById('page-' + t);
                    if (pg) pg.classList.toggle('active', t === name);
                });
                document.querySelectorAll('.nav-item').forEach(function(n, i) {
                    n.classList.toggle('active', ['home', 'search', 'fav', 'surprise', 'profile'][i] === name);
                });
                window.scrollTo({top: 0, behavior: 'smooth'});
                if (name === 'fav') loadFavorites();
                if (name === 'profile') renderProfile();
            }
            
            function verify18Open() { document.getElementById('adultModal').style.display = 'flex'; }
            function confirm18() {
                localStorage.setItem('isAdult', 'true');
                closeModal('adultModal');
                renderChips(); loadHomeMovies(1); loadTrending();
                if (tg && tg.showAlert) tg.showAlert('✅ ১৮+ আনলক হয়েছে! এখন সব কনটেন্ট দেখতে পাবেন।');
            }
            
            function renderChips() {
                const wrap = document.getElementById('catChips');
                let cats = ['All'].concat(CATS);
                if (isAdultVerified()) cats.push('Adult Content');
                wrap.innerHTML = cats.map(function(c) {
                    return `<div class="chip ${activeCat === c ? 'active' : ''}" onclick="activeCat='${c}'; renderChips(); loadHomeMovies(1);">${c === 'All' ? '🏠 All' : (c === 'Adult Content' ? '🔞 Adult' : c)}</div>`;
                }).join('');
            }
            
            function createMovieCard(m, index) {
                let isFav = userFavs.indexOf(m._id) !== -1;
                let isAdult = m.categories && m.categories.indexOf("Adult Content") !== -1;
                let verified = isAdultVerified();
                let imgSrc = (isAdult && !verified) ? 'https://via.placeholder.com/300x169/1e293b/ef4444?text=18%2B+%F0%9F%94%92' : (m.photo_id ? `/api/image/${m.photo_id}` : 'https://via.placeholder.com/300x169/1e293b/334155?text=🎬+No+Poster');
                let lockOverlay = (isAdult && !verified) ? `<div class="adult-lock-overlay"><i class="fa-solid fa-lock"></i></div>` : '';
                let clickAction = (isAdult && !verified) ? `onclick="verify18Open()"` : `onclick="openDetail(${index})"`;
                let adminViewBadge = isAdmin ? `<div class="admin-view-badge"><i class="fa-solid fa-eye"></i> ${m.clicks || 0}</div>` : '';
                return `<div class="movie-card" ${clickAction}>
                    <div style="position:relative;">
                        <img src="${imgSrc}" style="width:100%; aspect-ratio:16/9; object-fit:cover; display:block;">
                        ${lockOverlay}
                        ${adminViewBadge}
                    </div>
                    <div class="movie-info">
                        <div class="movie-title">${m.title}</div>
                        <div class="movie-meta-row">
                            <span class="year-badge">${m.year || 'N/A'}</span>
                            <span>${(m.categories || []).filter(function(c) { return c !== 'Adult Content'; }).slice(0, 2).map(function(c) { return `<span class="cat-small-tag">${c}</span>`; }).join('')}</span>
                        </div>
                    </div>
                    <button class="fav-btn ${isFav ? 'active' : ''}" onclick="event.stopPropagation(); toggleFav('${m._id}', this)"><i class="fa-solid fa-heart"></i></button>
                </div>`;
            }
            
            // 🔁 ৩০ মিনিট রোটেশন: সার্ভারের অ্যাক্টিভ লিংক ব্যবহার হয়; সেটিং না পেলে ক্লায়েন্ট-সাইডে সময় ধরে রোটেট
            function getActiveLink(isAdult) {
                let pool = (isAdult && ADULT_DIRECT_LINKS && ADULT_DIRECT_LINKS.length > 0) ? ADULT_DIRECT_LINKS : (DIRECT_LINKS && DIRECT_LINKS.length > 0 ? DIRECT_LINKS : null);
                if (!pool) return null;
                let serverLink = isAdult ? adSettings.active_adult_link : adSettings.active_link;
                if (serverLink && pool.indexOf(serverLink) !== -1) return serverLink;
                return pool[Math.floor(Date.now() / 1800000) % pool.length];
            }
            
            // 🔥 স্পন্সর অ্যাড কার্ড — কিউরিওসিটি হেডলাইন (প্রতিবার র‍্যান্ডম) + ক্লিকে অ্যাক্টিভ ডাইরেক্ট লিংক
            const SPONSOR_HEADLINES = [
                '🔥 ভেতরে যা আছে, দেখলে চমকে যাবেন!',
                '😱 এই ভুলটা ৯০% মানুষই করে — আপনি?',
                '🤫 সবাই লুকিয়ে রাখে এই ট্রিকটা!',
                '💰 এক ক্লিকে যা পাবেন, ভাবতেও পারবেন না!',
                '⚡ সবার আগে জানুন — এখনই দেখুন!',
                '🎁 আজকের বিশেষ সাপ্রাইজ ভেতরে অপেক্ষা করছে!'
            ];
            function sponsorCardHTML(isAdult) {
                let link = getActiveLink(isAdult);
                if (!link) return '';
                let hl = SPONSOR_HEADLINES[Math.floor(Math.random() * SPONSOR_HEADLINES.length)];
                return `<div class="sponsor-card" onclick="event.stopPropagation(); openExternal('${link}')">
                    <div class="sponsor-thumb">🔥</div>
                    <div class="sponsor-inner">
                        <div class="sponsor-title">${hl}</div>
                        <div class="sponsor-sub">ট্যাপ করে দেখুন — একদম ফ্রি!</div>
                    </div>
                    <div class="sponsor-footer">Sponsored</div>
                </div>`;
            }
            
            async function loadHomeMovies(page = 1) {
                homeCurrentPage = page;
                const list = document.getElementById('movieListHome');
                list.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
                try {
                    const res = await fetch('/api/list?cat=' + encodeURIComponent(activeCat) + '&uid=' + uid + '&page=' + page + '&adult=' + (isAdultVerified() ? '1' : '0'));
                    const data = await res.json();
                    currentViewMovies = data.movies || [];
                    // 🔥 প্রতি ৬টা মুভি কার্ডের পর পর স্পন্সর অ্যাড কার্ড ঢুকবে
                    list.innerHTML = currentViewMovies.length > 0 ? currentViewMovies.map(function(m, index) {
                        let card = createMovieCard(m, index);
                        if ((index + 1) % 6 === 0 && index < currentViewMovies.length - 1) card += sponsorCardHTML(false);
                        return card;
                    }).join('') : '<p class="empty-note">কোনো মুভি পাওয়া যায়নি!</p>';
                    renderPagination(data.total_pages, homeCurrentPage, 'paginationHome', 'loadHomeMovies');
                } catch(e) {}
            }
            
            async function searchMovies(page = 1) {
                const q = document.getElementById('searchInputMain').value.trim();
                const list = document.getElementById('movieListSearch');
                if (!q) { list.innerHTML = ''; document.getElementById('paginationSearch').innerHTML = ''; return; }
                searchCurrentPage = page;
                try {
                    const res = await fetch('/api/list?q=' + encodeURIComponent(q) + '&uid=' + uid + '&page=' + page + '&adult=' + (isAdultVerified() ? '1' : '0'));
                    const data = await res.json();
                    currentViewMovies = data.movies || [];
                    list.innerHTML = currentViewMovies.length > 0 ? currentViewMovies.map(function(m, index) { return createMovieCard(m, index); }).join('') : '<p class="empty-note">খুঁজে পাওয়া যায়নি!</p>';
                    renderPagination(data.total_pages, searchCurrentPage, 'paginationSearch', 'searchMovies');
                } catch(e) {}
            }
            
            async function loadTrending() {
                try {
                    const res = await fetch('/api/trending?adult=' + (isAdultVerified() ? '1' : '0'));
                    const data = await res.json();
                    const row = document.getElementById('trendingRow');
                    const movies = (data.movies || []).slice(0, 8);
                    document.getElementById('trendingSection').style.display = movies.length ? 'block' : 'none';
                    row.innerHTML = movies.map(function(m, i) {
                        const verified = isAdultVerified();
                        const isAdult = m.categories && m.categories.indexOf("Adult Content") !== -1;
                        if (isAdult && !verified) return '';
                        return `<div class="trend-card" onclick="openTrending(${i})">
                            <img src="${m.photo_id ? '/api/image/' + m.photo_id : 'https://via.placeholder.com/280x160/1e293b/334155?text=🎬'}">
                            <span class="trend-fire">🔥 #${i + 1}</span>
                            <div class="trend-title">${m.title}</div>
                        </div>`;
                    }).join('');
                    trendingCache = movies;
                } catch(e) {}
            }
            let trendingCache = [];
            function openTrending(i) {
                const m = trendingCache[i];
                if (!m) return;
                currentViewMovies = trendingCache.slice();
                openDetail(currentViewMovies.indexOf(m));
            }
            
            async function loadFavorites() {
                const list = document.getElementById('movieListFav');
                if (!uid) { list.innerHTML = '<p class="empty-note">প্রিয় যোগ করতে বট থেকে অ্যাপ খুলুন!</p>'; return; }
                try {
                    const res = await fetch('/api/fav?uid=' + uid);
                    const data = await res.json();
                    userFavs = data.fav_ids || [];
                    const movies = data.movies || [];
                    currentViewMovies = movies.slice();  // ফেভারিট কার্ড থেকেও ডিটেইল খুলতে পারে
                    list.innerHTML = movies.length > 0 ? movies.map(function(m, i) { return createMovieCard(m, i); }).join('') : '<p class="empty-note">এখনো কোনো প্রিয় যোগ করেননি ❤️</p>';
                } catch(e) {}
            }
            
            async function toggleFav(id, el) {
                el.classList.toggle('active');
                const isFav = el.classList.contains('active');
                if (isFav) { if (userFavs.indexOf(id) === -1) userFavs.push(id); }
                else { userFavs = userFavs.filter(function(x) { return x !== id; }); }
                try {
                    await fetch('/api/fav', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({initData: INIT_DATA, movieId: id}) });
                } catch(e) {}
            }
            
            async function surpriseMovie() {
                try {
                    const res = await fetch('/api/surprise?adult=' + (isAdultVerified() ? '1' : '0'));
                    const data = await res.json();
                    if (data && data.movie) {
                        currentViewMovies = [data.movie];
                        showTab('home');
                        openDetail(0);
                    } else {
                        if (tg && tg.showAlert) tg.showAlert('⚠️ এখনো কোনো মুভি যোগ হয়নি!');
                    }
                } catch(e) {}
            }
            
            function renderPagination(totalPages, currentPage, containerId, functionName) {
                const container = document.getElementById(containerId);
                if (totalPages <= 1) { container.innerHTML = ''; return; }
                let html = '';
                html += `<button class="page-btn" onclick="${functionName}(${currentPage - 1})" ${currentPage === 1 ? 'disabled' : ''}><i class="fa-solid fa-chevron-left"></i> Prev</button>`;
                let startPage = Math.max(1, currentPage - 1);
                let endPage = Math.min(totalPages, currentPage + 1);
                if (startPage > 1) { html += `<button class="page-btn" onclick="${functionName}(1)">1</button>`; if (startPage > 2) html += `<span style="color:#64748b;">...</span>`; }
                for (let i = startPage; i <= endPage; i++) { html += `<button class="page-btn ${i === currentPage ? 'active' : ''}" onclick="${functionName}(${i})">${i}</button>`; }
                if (endPage < totalPages) { if (endPage < totalPages - 1) html += `<span style="color:#64748b;">...</span>`; html += `<button class="page-btn" onclick="${functionName}(${totalPages})">${totalPages}</button>`; }
                html += `<button class="page-btn" onclick="${functionName}(${currentPage + 1})" ${currentPage === totalPages ? 'disabled' : ''}>Next <i class="fa-solid fa-chevron-right"></i></button>`;
                container.innerHTML = html;
            }
            
            function renderProfile() {
                const name = tgUser ? (tgUser.first_name || 'User') : 'Guest';
                document.getElementById('profileInfo').innerHTML = `
                    <div style="display:flex; align-items:center; gap:14px; padding-bottom:12px;">
                        <div style="font-size:42px;">👤</div>
                        <div>
                            <div style="font-weight:800; font-size:16px;">${name}</div>
                            <div style="color:#94a3b8; font-size:12px;">ID: ${uid || '—'}</div>
                        </div>
                    </div>
                    <div class="profile-row"><span>💎 VIP স্ট্যাটাস</span><span class="${isUserVip ? 'toggle-on' : 'toggle-off'}">${isUserVip ? 'অ্যাক্টিভ ✅' : 'নেই'}</span></div>
                    <div class="profile-row"><span>❤️ প্রিয় মুভি</span><span>${userFavs.length}টি</span></div>
                    <div class="profile-row"><span>👁️ ১৮+ কনটেন্ট</span><span class="${isAdultVerified() ? 'toggle-on' : 'toggle-off'}" onclick="verify18Open()" style="cursor:pointer;">${isAdultVerified() ? 'আনলকড ✅' : 'লকড 🔒 — খুলতে ট্যাপ'}</span></div>
                    <div class="profile-row"><span>🌑 OLED ডার্ক মোড</span><span class="${localStorage.getItem('oled') === '1' ? 'toggle-on' : 'toggle-off'}" onclick="toggleOled()" style="cursor:pointer;">${localStorage.getItem('oled') === '1' ? 'অন ✅' : 'অফ'}</span></div>`;
                document.getElementById('vipBox').innerHTML = isUserVip
                    ? `<div style="font-weight:800; font-size:15px;">💎 আপনি VIP মেম্বার!</div><div style="color:#94a3b8; font-size:13px; margin-top:6px;">অ্যাড ছাড়া সরাসরি ডাউনলোড — ধন্যবাদ সাপোর্টের জন্য! 🙏</div>`
                    : `<div style="font-weight:800; font-size:15px;">💎 VIP কিনুন</div><div style="color:#94a3b8; font-size:13px; margin-top:6px;">অ্যাড ছাড়া সরাসরি ডাউনলোড + প্রায়োরিটি সাপোর্ট। বটে গিয়ে VIP নিন!</div>
                       <button class="dl-file-btn unlocked" style="margin-top:12px;" onclick="buyVip()">💎 VIP কিনতে বটে যান</button>`;
                document.getElementById('profileSettings').innerHTML = `
                    <a href="${CHANNEL_URL}" target="_blank" class="join-channel-btn">🚀 Join Channel</a>
                    <button class="dl-file-btn" style="margin-top:10px;" onclick="if(tg) tg.close();"><i class="fa-solid fa-right-from-bracket"></i> বটে ফিরে যান</button>`;
            }
            function toggleOled() {
                const on = localStorage.getItem('oled') === '1';
                localStorage.setItem('oled', on ? '0' : '1');
                document.body.classList.toggle('oled-mode', !on);
                renderProfile();
            }
            function buyVip() {
                if (BOT_USERNAME && tg) tg.openTelegramLink('https://t.me/' + BOT_USERNAME);
                else if (tg) tg.close();
            }
            
            // ⚙️ অ্যাড সেটিং লোড (ad_count / ad_wait / অ্যাক্টিভ লিংক — ৫ মিনিট পর পর রিফ্রেশ হয়)
            async function loadAdSettings() {
                try {
                    const res = await fetch('/api/settings/public');
                    const s = await res.json();
                    adSettings.ad_count = Math.max(1, s.ad_count || 1);
                    adSettings.ad_wait = Math.max(5, s.ad_wait || 10);
                    adSettings.active_link = s.active_link || '';
                    adSettings.active_adult_link = s.active_adult_link || '';
                } catch(e) {}
            }
            
            async function fetchUserInfo() {
                try {
                    const res = await fetch('/api/user/ping', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({initData: INIT_DATA}) });
                    const d = await res.json();
                    if (d.ok) {
                        isUserVip = !!d.vip;
                        isAdmin = !!d.is_admin;
                        document.getElementById('adminBadge').style.display = isAdmin ? 'inline-block' : 'none';
                    }
                } catch(e) {}
            }
            // ✅ ডিটেইল পপ-আপ (সিঙ্গেল/মাল্টি ফাইল দুটোই সাপোর্ট করে)
            function openDetail(index) {
                let m = currentViewMovies[index];
                if (!m) return;
                let isAdult = m.categories && m.categories.indexOf("Adult Content") !== -1;
                let verified = isAdultVerified();
                document.getElementById('detailImg').src = (isAdult && !verified) ? 'https://via.placeholder.com/300x169/1e293b/ef4444?text=18%2B+%F0%9F%94%92' : (m.photo_id ? `/api/image/${m.photo_id}` : 'https://via.placeholder.com/300x169/1e293b/334155?text=🎬');
                document.getElementById('detailTitle').innerText = m.title || 'Unknown Movie';
                document.getElementById('detailMeta').innerHTML = `<span>📅 ${m.year || 'N/A'}</span> &nbsp; <span>👁️ ${m.clicks || 0} বার ডাউনলোড</span>`;
                document.getElementById('detailCats').innerHTML = (m.categories || []).map(function(c) { return `<span class="movie-cat-tag">${c}</span>`; }).join(' ');

                let btnsHtml = "";
                if (m.files && Array.isArray(m.files) && m.files.length > 0) {
                    btnsHtml = m.files.map(function(f) {
                        let isFree = f.is_unlocked || isUserVip;
                        return `<button class="dl-file-btn ${isFree ? 'unlocked' : ''}" onclick="handleFileClick('${f.id}', ${isFree ? 'true' : 'false'}, ${isAdult ? 'true' : 'false'})"><span><i class="fa-solid fa-${isFree ? 'lock-open' : 'lock'}"></i> Download ${f.quality}</span></button>`;
                    }).join('');
                } else {
                    let isFree = isUserVip;
                    btnsHtml = `<button class="dl-file-btn ${isFree ? 'unlocked' : ''}" onclick="handleFileClick('${m._id}', ${isFree ? 'true' : 'false'}, ${isAdult ? 'true' : 'false'})"><span><i class="fa-solid fa-${isFree ? 'lock-open' : 'lock'}"></i> Download ${m.quality || 'Full Movie'}</span></button>`;
                }
                document.getElementById('fileButtonsContainer').innerHTML = btnsHtml;
                // 💰 ডাউনলোড সেকশন: সাধারণ মুভিতে 320x50 ব্যানার, ১৮+ মুভিতে অ্যাডাল্ট ব্যানার + স্পন্সর কার্ড
                document.getElementById('detailBannerNormal').style.display = isAdult ? 'none' : 'block';
                document.getElementById('detailBannerAdult').style.display = isAdult ? 'block' : 'none';
                document.getElementById('detailSponsor').innerHTML = sponsorCardHTML(isAdult);
                document.getElementById('detailModal').style.display = 'flex';
            }

            function handleFileClick(fileId, isFree, isAdult) {
                activeFileId = fileId; activeIsAdult = isAdult;
                if (isFree) { sendFileRequest(fileId); }
                else { closeModal('detailModal'); resetAdModal(); document.getElementById('adModal').style.display = 'flex'; }
            }

            // 💰 অ্যাড সিস্টেম: ১০ সেকেন্ড পুরো না দেখলে "আরো X সেকেন্ড বাকি" — আবার অ্যাডে ক্লিক করতে হবে
            function resetAdModal() {
                adStartTime = 0; adRound = 0;
                let stepEl = document.getElementById('adStepText');
                if (adSettings.ad_count > 1) { stepEl.innerHTML = `আপনাকে মোট <b>${adSettings.ad_count}টি</b> বিজ্ঞাপন দেখতে হবে। প্রতিটিতে ক্লিক করে কমপক্ষে <b>${adSettings.ad_wait} সেকেন্ড</b> দেখে তারপর ফিরে আসুন।`; }
                else { stepEl.innerHTML = `লিংকে ক্লিক করে বিজ্ঞাপনটি দেখুন এবং কমপক্ষে <b>${adSettings.ad_wait} সেকেন্ড</b> দেখে তারপর ফিরে এসে ✅ বাটনে ক্লিক করুন।`; }
                document.getElementById('adClickBtn').innerText = 'বিজ্ঞাপন খুলুন';
                document.getElementById('adClickBtn').style.display = 'block';
                document.getElementById('adVerifyBtn').style.display = 'none';
                document.getElementById('adTryAgainBtn').style.display = 'none';
                document.getElementById('adWarnBox').style.display = 'none';
            }

            function openExternal(url) {
                if (tg && tg.openLink) { try { tg.openLink(url); return; } catch(e) {} }
                window.open(url, '_blank');
            }

            function openAdLink() {
                let linkToOpen = getActiveLink(activeIsAdult);
                if (!linkToOpen) {
                    // কোনো অ্যাড লিংক সেট করা নেই → সরাসরি আনলক (ইউজার যেন আটকে না থাকে)
                    finishAdRounds();
                    return;
                }
                // 🔁 রোটেশন: এই মুহূর্তের অ্যাক্টিভ লিংক (৩০ মিনিট পর পর সার্ভার নিজে থেকে বদলায়)
                openExternal(linkToOpen);
                if (!adStartTime) adStartTime = Date.now();  // প্রথম ক্লিক থেকেই সময় গোনা হয়
                document.getElementById('adClickBtn').innerText = '🔁 আবার অ্যাড খুলুন';
                document.getElementById('adClickBtn').style.display = 'block';
                document.getElementById('adVerifyBtn').style.display = 'block';
                document.getElementById('adWarnBox').style.display = 'none';
                let stepEl = document.getElementById('adStepText');
                if (adSettings.ad_count > 1) stepEl.innerHTML = `বিজ্ঞাপন দেখে ফিরে আসুন! (রাউন্ড ${adRound + 1}/${adSettings.ad_count})`;
            }

            function checkAdWatched() {
                if (!adStartTime) { if (tg && tg.showAlert) tg.showAlert('⚠️ আগে বিজ্ঞাপন খুলুন!'); return; }
                const elapsed = (Date.now() - adStartTime) / 1000;
                const remain = Math.ceil(adSettings.ad_wait - elapsed);
                if (remain > 0) {
                    // ⛔ আগে ফিরে এসেছে — পুরো অ্যাড দেখেনি: বাকি সেকেন্ড জানিয়ে আবার অ্যাডে পাঠানো হয়
                    const warn = document.getElementById('adWarnBox');
                    warn.innerHTML = `⛔ আপনি সম্পূর্ণ <b>${adSettings.ad_wait} সেকেন্ড</b> অ্যাডটা দেখেন নি!<br>আরো <b>${remain} সেকেন্ড</b> বাকি — আবার অ্যাডে ক্লিক করে সময় পূরণ করুন।`;
                    warn.style.display = 'block';
                    document.getElementById('adClickBtn').innerText = '🔁 আবার অ্যাড খুলুন';
                    document.getElementById('adClickBtn').style.display = 'block';
                    if (tg && tg.showAlert) tg.showAlert(`⛔ আরো ${remain} সেকেন্ড অ্যাড দেখতে হবে!`);
                    return;  // এই রাউন্ড কাউন্ট হবে না — যতক্ষণ না পুরো সময় হয়
                }
                adRound++;
                if (adRound < adSettings.ad_count) {
                    adStartTime = 0;  // নতুন রাউন্ড — নতুন করে সময় গোনা হবে
                    document.getElementById('adWarnBox').style.display = 'none';
                    document.getElementById('adVerifyBtn').style.display = 'none';
                    document.getElementById('adClickBtn').style.display = 'block';
                    document.getElementById('adClickBtn').innerText = `পরের বিজ্ঞাপন খুলুন (${adRound + 1}/${adSettings.ad_count})`;
                } else {
                    finishAdRounds();
                }
            }

            function finishAdRounds() {
                closeModal('adModal');
                if (activeFileId) sendFileRequest(activeFileId);
            }

            async function sendFileRequest(fileId) {
                try {
                    const res = await fetch('/api/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({userId: uid, fileId: fileId, initData: INIT_DATA}) });
                    const data = await res.json();
                    if (data.ok) {
                        closeModal('detailModal');
                        document.getElementById('successModal').style.display = 'flex';
                        fetchUserInfo();
                    } else if (data.error === 'locked') {
                        closeModal('detailModal');
                        resetAdModal();
                        document.getElementById('adModal').style.display = 'flex';
                    } else if (data.error === 'blocked') {
                        if (tg && tg.showAlert) tg.showAlert('⚠️ আগে বটে গিয়ে /start দিন, তারপর আবার চেষ্টা করুন!');
                    } else {
                        if (tg && tg.showAlert) tg.showAlert('⚠️ Failed! আবার চেষ্টা করুন।');
                    }
                } catch(e) { if (tg && tg.showAlert) tg.showAlert('⚠️ নেটওয়ার্ক এরর! আবার চেষ্টা করুন।'); }
            }

            // 🔗 ডিপ-লিংক: ব্রডকাস্ট/লগ পোস্টের Watch Now বাটনে ?open=<movie_id> থাকে —
            // 🔥 সবার জন্য গেট: অ্যাপ খুললেই মুভির নাম + স্পন্সর অ্যাড দেখায়;
            //    অ্যাডে ক্লিক করে ফিরে এসে আবার Watch Now চাপলে:
            //    • পুরনো ইউজার → সরাসরি ওই মুভির ডাউনলোড পপ-আপ
            //    • নতুন ইউজার (আগে কখনো ডাউনলোড করেনি) → বটে যান → /start করলেই সাথে সাথে ফাইল
            async function openFromDeepLink() {
                try {
                    let openId = new URLSearchParams(window.location.search).get('open') || (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || '';
                    if (!openId) return;
                    const res = await fetch('/api/movie/' + encodeURIComponent(openId) + '?uid=' + uid);
                    const data = await res.json();
                    if (data && data.movie) {
                        currentViewMovies = [data.movie];
                        gateIsNew = !!data.is_new;
                        showFileGate(data.movie);  // 🔥 সবার জন্য গেট
                    }
                } catch(e) {}
            }

            function showFileGate(m) {
                gateMovie = m; gateStep = 0;
                let q = (m.files && m.files[0] && m.files[0].quality) ? ' [' + m.files[0].quality + ']' : '';
                document.getElementById('gateMovieTitle').innerText = (m.title || 'Movie') + q;
                document.getElementById('gateWatchBtn').innerText = '🎬 Watch Now';
                document.getElementById('fileGateModal').style.display = 'flex';
            }

            function gateNextStep() {
                let btn = document.getElementById('gateWatchBtn');
                if (gateStep === 0) {
                    // প্রথম ক্লিকে স্পন্সর (Adsterra/Monetag) লিংক খুলবে
                    let isAdult = gateMovie && gateMovie.categories && gateMovie.categories.indexOf('Adult Content') !== -1;
                    let link = getActiveLink(isAdult);
                    if (link) openExternal(link);
                    gateStep = 1;
                    btn.innerText = gateIsNew ? '🤖 বটে যান — সাথে সাথে ফাইল নিন' : '✅ ফাইলটি দেখুন';
                    localStorage.setItem('mb_gate_seen', '1');
                } else {
                    closeModal('fileGateModal');
                    if (!gateMovie) return;
                    if (gateIsNew && BOT_USERNAME && tg) {
                        // 🎁 নতুন ইউজার: বটে যান — /start করলেই সাথে সাথে ফাইল চলে আসবে
                        tg.openTelegramLink('https://t.me/' + BOT_USERNAME + '?start=open_' + gateMovie._id);
                        setTimeout(function() { try { tg.close(); } catch(e) {} }, 600);
                    } else {
                        openDetail(0);
                    }
                }
            }

            // ⚙️ বুটস্ট্র্যাপ: আগে ডিপ-লিংক চেক (is_new ঠিক করার জন্য), তারপর ইউজার রেজিস্ট্রেশন ও লিস্ট লোড
            (async function() {
                await openFromDeepLink();
                loadAdSettings();
                fetchUserInfo();
                renderChips();
                loadHomeMovies(1);
                loadTrending();
                setInterval(loadAdSettings, 5 * 60 * 1000);
                setInterval(fetchUserInfo, 60 * 1000);  // লাইভ অ্যাক্টিভ পিং
            })();
            if (localStorage.getItem('oled') === '1') document.body.classList.add('oled-mode');
        </script>
    </body>
</html>'''


async def build_web_ui():
    """টেমপ্লেটে লাইভ সেটিংস (অ্যাড লিংক পুল, চ্যানেল, বানার) বসিয়ে সম্পূর্ণ HTML বানায়"""
    try:
        dl_cfg = await db.settings.find_one({"id": "direct_links"})
        links = dl_cfg.get("links", []) if dl_cfg else []
        adl_cfg = await db.settings.find_one({"id": "adult_direct_links"})
        alinks = adl_cfg.get("links", []) if adl_cfg else []
        cu = await get_setting("channel_url", DEFAULT_CHANNEL_URL)
    except Exception:
        links, alinks, cu = [], [], DEFAULT_CHANNEL_URL
    if not links:
        links = list(DEFAULT_DIRECT_LINKS)
    if not alinks:
        alinks = list(DEFAULT_ADULT_DIRECT_LINKS)
    safe_cu = (cu or DEFAULT_CHANNEL_URL).replace('"', '').strip() or DEFAULT_CHANNEL_URL
    html = WEB_UI_TEMPLATE
    html = html.replace("__DL_JSON__", json.dumps(links))
    html = html.replace("__ADL_JSON__", json.dumps(alinks))
    html = html.replace("__BANNER_AD__", BANNER_AD_JS)
    html = html.replace("__BANNER_320__", BANNER_AD_JS_320)
    html = html.replace("__BANNER_ADULT__", BANNER_AD_JS_ADULT)
    html = html.replace("__CHANNEL_URL__", safe_cu)
    html = html.replace("__BOT_USERNAME__", BOT_USERNAME or "")
    return html

@app.get("/")
async def web_app_page():
    return HTMLResponse(await build_web_ui())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
