import os
import asyncio
import datetime
import uvicorn
import time
import aiohttp
import hmac
import hashlib
import urllib.parse
import secrets
import random
import html  # 🏷️ গ্রুপ-সার্চে ইউজারের নাম মেনশন করার সময় HTML-escape (নামে <>& থাকলেও ক্যাপশন ভাঙবে না)
import re
import json
import math

# ==========================================
# 🛑 FIX FOR EVENT LOOP ERROR
# ==========================================
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# ==========================================

from fastapi import FastAPI, Body, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError, TelegramForbiddenError

from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from pydantic import BaseModel

# ==========================================
# 1. Configuration & Global Variables
# ==========================================
TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))
APP_URL = os.getenv("APP_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1003904328439") 
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
BOT_USERNAME = os.getenv("BOT_USERNAME", "MovieeBoxx_Bot")

LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "-1004462840114")

# ==========================================
# 💰 ডিফল্ট অ্যাড কনফিগ (Adsterra + Monetag)
# বট প্রথমবার চালু হলে এই লিংকগুলো অটোমেটিক DB-তে বসে যাবে।
# পরে /addlink বা /dellink দিয়ে যেকোনো সময় বদলানো যাবে।
# ==========================================
DEFAULT_DIRECT_LINKS = [
    "https://asiafilm.org/4/1952b9f3548cd57e994e610fde24e41f",   # Normal Direct Link 1
    "https://asiafilm.org/4/7ef304669bcfc15f3404e1a5e5dcf1ba",   # Normal Direct Link 2
    "https://asiafilm.org/4/dd496e8de5fdbe10f2ccf3defc93c342",   # Normal Direct Link 3
    "https://uplcm.com/4/10918781",                              # Normal Direct Link 4
    "https://uplcm.com/4/12080503",                              # Normal Direct Link 5
]
DEFAULT_ADULT_DIRECT_LINKS = [
    "https://auctionr.org/4/1b6df569ebc094e3e8e592d2fd017b4f",   # Adult Direct Link 1
    "https://auctionr.org/4/ae2d07d4c0488d06e6aec709d40d437f",   # Adult Direct Link 2
    "https://auctionr.org/4/ef5b10414fe8fdd3f602e74bcdd4225c",   # Adult Direct Link 3
]

def pick_random_link(links, last_served=None):
    """🎲 র‍্যান্ডম পিক — পুল থেকে র‍্যান্ডম লিংক; সর্বশেষ দেখানো লিংকটি পরপর দুইবার যাবে না।"""
    if not links: return None
    pool = [u for u in links if u != last_served]
    if not pool: pool = list(links)
    try: return random.choice(pool)
    except Exception: return pool[0]

def is_adult_movie(m):
    """🎯 অ্যাডাল্ট ডিটেকশন — মুভির টাইটেলে '18+' লেখা থাকলে অ্যাডাল্ট (অ্যাডাল্ট ডাইরেক্ট লিংক যাবে);
    না থাকলে নরমাল। পুরনো মুভিগুলোর জন্য 'Adult Content' ক্যাটাগরিও ধরা হয়।"""
    try:
        if "Adult Content" in (m.get("categories") or []): return True
        return "18+" in (m.get("title") or "").upper()
    except Exception:
        return False

# 300x250 ব্যানার অ্যাড (Web App Home + অ্যাড মোডালে দেখাবে)
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

# 320x50 ব্যানার অ্যাড (মুভি ডিটেইল/ডাউনলোড পপ-আপে, কোয়ালিটি বাটনের নিচে)
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

# 300x250 অ্যাডাল্ট ব্যানার (১৮+ মুভির ডিটেইল পপ-আপে দেখাবে)
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

# 🚫 18+ মুভির জন্য ছোট 320x50 অ্যাডাল্ট ব্যানার (স্টিকি টপ বার + ডিটেইলস + ডাউনলোড পপ-আপে)
BANNER_AD_JS_320_ADULT = """<script>
  atOptions = {
    'key' : 'f96407c772e15f558541667b8d868edc',
    'format' : 'iframe',
    'height' : 50,
    'width' : 320,
    'params' : {}
  };
</script>
<script src="https://brijmohan.org/22/f96407c772e15f558541667b8d868edc"></script>"""

def _banner_conf(js: str):
    """🧩 JS ব্যানার স্ট্রিং থেকে (key/src/width/height) বের করে — ওয়েব অ্যাপ JS দিয়ে ফিডের ভেতরে
    iframe আকারে ব্যানার বসাতে (innerHTML দিলে <script> চলে না, তাই iframe.srcdoc ব্যবহার হয়)"""
    try:
        key = re.search(r"'key'\s*:\s*'([^']+)'", js).group(1)
        h = int(re.search(r"'height'\s*:\s*(\d+)", js).group(1))
        w = int(re.search(r"'width'\s*:\s*(\d+)", js).group(1))
        src = re.search(r'src="([^"]+)"', js).group(1)
        return {"w": w, "h": h, "key": key, "src": src}
    except Exception:
        return None

BANNER_300_CONF = _banner_conf(BANNER_AD_JS)
BANNER_320_CONF = _banner_conf(BANNER_AD_JS_320)
BANNER_ADULT_CONF = _banner_conf(BANNER_AD_JS_ADULT)
BANNER_320_ADULT_CONF = _banner_conf(BANNER_AD_JS_320_ADULT)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()
security = HTTPBasic()

app.add_middleware(
    CORSMiddleware, 
    allow_origins=["*"], 
    allow_credentials=True, 
    allow_methods=["*"], 
    allow_headers=["*"]
)

client = AsyncIOMotorClient(MONGO_URL)
db = client['movie_database']

admin_cache = set([OWNER_ID]) 
banned_cache = set() 

CATEGORIES = ["Bangla", "Bangla Dubbed", "Hindi Dubbed", "Hollywood", "K-Drama", "Anime", "Horror", "Web Series", "Adult Content"]

# কিউ সিস্টেম যোগ করা হলো (একসাথে একাধিক ব্রডকাস্ট রান করে বট যেন হ্যাং না করে)
broadcast_queue = asyncio.Queue()

# ==========================================
# 2. FSM States
# ==========================================
class AdminStates(StatesGroup):
    waiting_for_bcast = State()
    waiting_for_reply = State()
    waiting_for_photo = State()
    waiting_for_title = State()
    waiting_for_quality = State() 
    waiting_for_year = State()
    waiting_for_cats = State()
    waiting_for_upc_photo = State()
    waiting_for_upc_title = State()
    waiting_for_upc_date = State()
    waiting_for_photoid = State()

# ==========================================
# 3. Database Initialization & Caching
# ==========================================
async def load_admins():
    admin_cache.clear()
    admin_cache.add(OWNER_ID)
    async for admin in db.admins.find():
        admin_cache.add(admin["user_id"])

async def load_banned_users():
    banned_cache.clear()
    async for b_user in db.banned.find():
        banned_cache.add(b_user["user_id"])

async def init_db():
    await db.movies.create_index([("title", "text")])
    await db.movies.create_index("title")
    await db.movies.create_index("created_at")
    await db.movies.create_index("categories")
    await db.auto_delete.create_index("delete_at")
    await db.users.create_index("joined_at")
    await db.users.create_index("last_active")
    await db.payments.create_index("trx_id", unique=True)
    # 🎯 নতুন ইউজারের পেন্ডিং ফাইল ডেলিভারি (START চাপলেই অ্যাড দেখা ফাইল পাঠানো হয়)
    await db.pending_files.create_index("user_id")
    # 🗳️ রেটিং পোল (মুভির টাইটেল অনুযায়ী সব পার্ট শেয়ার করে) — ইউনিক ইনডেক্স: এক ইউজার এক ভোট
    await db.votes.create_index([("title", 1), ("uid", 1)], unique=True)
    await db.votes.create_index("title")

# 💰 অ্যাড সেটিং সিড: DB খালি থাকলে ডিফল্ট Adsterra/Monetag লিংক + সেটিং বসিয়ে দেয়
# (অ্যাডমিন নিজে লিংক বসালে তার লিংকই থাকবে, ওভাররাইট করবে না)
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
        # 🎲 র‍্যান্ডম অ্যাড সিস্টেম মাইগ্রেশন (একবারই চলে): নতুন নরমাল ৫টা + অ্যাডাল্ট ৩টা ডাইরেক্ট লিংক
        # পুলে যোগ হয় ($addToSet — যেগুলো আগে থেকেই আছে সেগুলো ডুপ্লিকেট হবে না)
        mig = await db.settings.find_one({"id": "random_links_migrated"})
        if not mig:
            await db.settings.update_one({"id": "direct_links"}, {"$addToSet": {"links": {"$each": DEFAULT_DIRECT_LINKS}}}, upsert=True)
            await db.settings.update_one({"id": "adult_direct_links"}, {"$addToSet": {"links": {"$each": DEFAULT_ADULT_DIRECT_LINKS}}}, upsert=True)
            await db.settings.update_one({"id": "random_links_migrated"}, {"$set": {"done": True}}, upsert=True)
            print("🎲 Random ad system: new direct links added to pools (normal+adult).")
        defaults = {"ad_count": 1, "ad_wait": 10, "bcast_del_hours": 72}
        for key, val in defaults.items():
            cfg = await db.settings.find_one({"id": key})
            if not cfg:
                await db.settings.update_one({"id": key}, {"$set": {"value": val}}, upsert=True)
    except Exception as e:
        print(f"Seed ads error: {e}")

# 📝 লগ চ্যানেল লোড: অ্যাডমিন /setlogchannel দিয়ে চ্যানেল বদলালে DB-তে সেভ হয়,
# বট রিস্টার্ট হলেও সেখান থেকে আবার লোড হয় (env এর LOG_CHANNEL_ID ডিফল্ট হিসেবে থাকে)
async def load_log_channel():
    global LOG_CHANNEL_ID
    try:
        cfg = await db.settings.find_one({"id": "log_channel"})
        if cfg and cfg.get("chat_id"):
            LOG_CHANNEL_ID = cfg["chat_id"]
            print(f"📝 Log channel loaded from DB: {LOG_CHANNEL_ID} ({cfg.get('title', '')})")
    except Exception as e:
        print(f"Log channel load error: {e}")

# ==========================================
# 4. Security & Authentication Methods
# ==========================================
def validate_tg_data(init_data: str) -> bool:
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        hash_val = parsed_data.pop('hash', None)
        auth_date = int(parsed_data.get('auth_date', 0))
        if not hash_val or time.time() - auth_date > 86400:
            return False
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return calculated_hash == hash_val
    except:
        return False

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (correct_username and correct_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect", headers={"WWW-Authenticate": "Basic"})
    return True

# ==========================================
# 5. Background Tasks
# ==========================================
async def auto_delete_worker():
    while True:
        try:
            now = datetime.datetime.utcnow()
            async for msg in db.auto_delete.find({"delete_at": {"$lte": now}}):
                try:
                    await bot.delete_message(chat_id=msg["chat_id"], message_id=msg["message_id"])
                except:
                    pass
                await db.auto_delete.delete_one({"_id": msg["_id"]})
                await asyncio.sleep(0.5)
        except:
            pass
        await asyncio.sleep(60)

async def auto_lock_worker():
    while True:
        try:
            # ২৪ ঘণ্টা আগের সময় বের করা হচ্ছে
            expire_time = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
            
            # ডেটাবেস থেকে ২৪ ঘণ্টা আগে আনলক করা মুভিগুলো ডিলিট করা হচ্ছে
            result = await db.user_unlocks.delete_many({"unlocked_at": {"$lte": expire_time}})
            
            # কনসোলে লগ দেখানোর জন্য (প্রয়োজন না হলে রাখতে পারেন)
            if result.deleted_count > 0:
                print(f"🔒 Auto-locked {result.deleted_count} movies (24 hrs expired).")
        except Exception as e:
            print(f"Auto-lock worker error: {e}")
            
        # প্রতি ১ ঘণ্টা পর পর চেক করবে
        await asyncio.sleep(3600)

# নতুন কিউ ওয়ার্কার (ব্রডকাস্ট একটি একটি করে পাঠাবে)
async def broadcast_queue_worker():
    while True:
        try:
            # কিউ থেকে ডেটা নিবে (আগের ব্রডকাস্ট শেষ না হলে এখানে অপেক্ষা করবে)
            task_data = await broadcast_queue.get()
            await run_movie_broadcast(task_data['data'], task_data['selected_cats'], task_data['admin_id'], task_data.get('movie_id'))
            broadcast_queue.task_done()
        except Exception as e:
            print(f"Queue Worker Error: {e}")
            await asyncio.sleep(5)

# স্টার্টআপ ইভেন্ট
@app.on_event("startup")
async def on_startup():
    await init_db()
    await load_admins()
    await load_banned_users()
    await seed_default_ads()
    await load_log_channel()
    # 🤖 বটের আসল ইউজারনেম নিজেই ধরে নিচ্ছি — BOT_USERNAME env ভুল/না দিলেও
    # চ্যানেল পোস্টের t.me ডিপ-লিংক (startapp) ঠিক থাকবে
    global BOT_USERNAME
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
        print(f"🤖 Bot: @{BOT_USERNAME}")
    except Exception as e:
        print(f"get_me failed: {e}")
    # 🎯 মেনু-বাটনে ওয়েব অ্যাপ বসিয়ে দিচ্ছি → t.me/<bot>?startapp= ডিপ-লিংক সবসময় কাজ করবে
    # (লগ চ্যানেলের Watch Now এই লিংক দিয়েই মিনি অ্যাপ খোলে; BotFather-এ আলাদা কিছু সেট না করলেও চলবে)
    if APP_URL:
        try:
            await bot.set_chat_menu_button(menu_button=types.MenuButtonWebApp(text="🎬 Open App", web_app=types.WebAppInfo(url=APP_URL)))
        except Exception as e:
            print(f"Menu button set failed: {e}")
    asyncio.create_task(auto_delete_worker())
    asyncio.create_task(broadcast_queue_worker())
    asyncio.create_task(auto_lock_worker())
    asyncio.create_task(self_pinger()) # ✅ এই লাইনটা যোগ করো

# ==========================================
# 6. Telegram Bot Commands
# ==========================================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if uid in banned_cache:
        return await message.answer("🚫 আপনাকে ব্যান করা হয়েছে।", parse_mode="HTML")

    await state.clear()
    now = datetime.datetime.utcnow()
    args = message.text.split(" ")
    payload = args[1].strip() if len(args) > 1 else ""

    # 🎯 নতুন ইউজার অটো-ডেলিভারি: ওয়েব অ্যাপে অ্যাড দেখা শেষ করে "বটে যান" চেপে এসে
    # START চাপলে পেন্ডিং ফাইলটি সাথে সাথে পাঠিয়ে দেওয়া হয় — ইউজার বটের মেম্বারও হয়ে যায়
    delivered = False
    if payload and ObjectId.is_valid(payload):
        pending = await db.pending_files.find_one({"user_id": uid, "movie_id": payload})
        if pending:
            m = await db.movies.find_one({"_id": ObjectId(payload)})
            if m:
                ok, _ = await deliver_movie_file(uid, m)
                if ok:
                    delivered = True
                    await db.movies.update_one({"_id": ObjectId(payload)}, {"$inc": {"clicks": 1}})
                    await db.user_unlocks.update_one({"user_id": uid, "movie_id": payload}, {"$set": {"unlocked_at": now}}, upsert=True)
                    await db.pending_files.delete_one({"_id": pending["_id"]})
            else:
                # মুভিটি আর DB-তে নেই → পেন্ডিং রেকর্ড মুছে ফেলা হলো
                await db.pending_files.delete_one({"_id": pending["_id"]})

    # 🎯 ওল্ড ইউজার ফাস্ট ডেলিভারি (?start=f_<movie_id>): ওয়েব অ্যাপে ডাউনলোড ফ্লো শেষ করে
    # "🤖 বটে গিয়ে ফাইল নিন" বাটনে ক্লিক করলে ইউজার এই লিংকে চলে আসে — ওল্ড ইউজারকে START
    # ওয়েলকাম মেসেজ ছাড়াই সুন্দর স্টাইলে সাথে সাথে ফাইল পেয়ে যাওয়ার অভিজ্ঞতা দেওয়া হয়
    # (নতুন ইউজারের অটো-ডেলিভারির মতোই, কিন্তু স্টাইল আলাদা)
    if payload.startswith("f_") and ObjectId.is_valid(payload[2:]):
        movie_id = payload[2:]
        m = await db.movies.find_one({"_id": ObjectId(movie_id)})
        if not m:
            payload = ""  # মুভি আর নেই → সাধারণ স্টার্ট
        else:
            user_row = await db.users.find_one({"user_id": uid})
            if user_row:
                _cfg = await db.settings.find_one({"id": "tg_link"})
                _link = _cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if _cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
                _kb = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="🚀 Join Channel", url=_link)]])
                # এইমাত্র (১০ মিনিটের ভেতরে) ওয়েব অ্যাপ থেকে ডেলিভারি হয়ে থাকলে ডুপ্লিকেট কপি
                # না পাঠিয়ে সুন্দর পয়েন্টার মেসেজ — "ফাইল ওপরের মেসেজে আছে 👆"
                recent = await db.user_unlocks.find_one({
                    "user_id": uid, "movie_id": movie_id,
                    "unlocked_at": {"$gte": now - datetime.timedelta(minutes=10)}})
                if recent:
                    try:
                        await message.answer(
                            f"🎉 <b>এই তো আপনার ফাইল!</b>\n\n✅ <b>{m.get('title', 'Movie')}</b> এই চ্যাটেই পাঠানো হয়েছে — ওপরে স্ক্রল করে ফাইলটায় 👆 ট্যাপ করলেই দেখতে/ডাউনলোড করতে পারবেন।\n\n✨ প্রতিদিন নতুন মুভি আপডেট পেতে চ্যানেলে জয়েন করুন!",
                            parse_mode="HTML", reply_markup=_kb, disable_web_page_preview=True)
                    except Exception: pass
                    return
                ok, _ = await deliver_movie_file(uid, m)
                if ok:
                    await db.movies.update_one({"_id": ObjectId(movie_id)}, {"$inc": {"clicks": 1}})
                    await db.user_unlocks.update_one({"user_id": uid, "movie_id": movie_id}, {"$set": {"unlocked_at": now}}, upsert=True)
                    try:
                        await message.answer(
                            f"🎉 <b>এই তো আপনার ফাইল!</b>\n\n📥 <b>{m.get('title', 'Movie')}</b> ওপরের মেসেজে পাঠানো হয়েছে 👆 — ট্যাপ করে দেখুন।\n\n✨ প্রতিদিন নতুন মুভি আপডেট পেতে চ্যানেলে জয়েন করুন!",
                            parse_mode="HTML", reply_markup=_kb, disable_web_page_preview=True)
                    except Exception: pass
                    return
                # ডেলিভারি ব্যর্থ → নিচের স্বাভাবিক স্টার্ট ফ্লোতে যাবে (ইউজার আটকে থাকবে না)
            else:
                # নতুন ইউজার f_ লিংকে এলে স্বাভাবিক রেজিস্ট্রেশন হবে; Watch Now বাটন ওই মুভির পেজ খুলবে
                payload = movie_id

    user = await db.users.find_one({"user_id": uid})

    if not user:
        if len(args) > 1 and args[1].startswith("ref_"):
            try:
                referrer_id = int(args[1].split("_")[1])
                if referrer_id != uid:
                    await db.users.update_one({"user_id": referrer_id}, {"$inc": {"refer_count": 1}})
                    ref_user = await db.users.find_one({"user_id": referrer_id})
                    if ref_user and ref_user.get("refer_count", 0) % 5 == 0:
                        current_vip = ref_user.get("vip_until", now)
                        if current_vip < now: current_vip = now
                        await db.users.update_one({"user_id": referrer_id}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=1)}})
                        try: await bot.send_message(referrer_id, "🎉 ৫ জন রেফারের জন্য ২৪ ঘণ্টা VIP!", parse_mode="HTML")
                        except: pass
            except: pass
        await db.users.insert_one({"user_id": uid, "first_name": message.from_user.first_name, "joined_at": now, "refer_count": 0, "coins": 0, "last_checkin": now - datetime.timedelta(days=2), "vip_until": now - datetime.timedelta(days=1)})
    else:
        await db.users.update_one({"user_id": uid}, {"$set": {"first_name": message.from_user.first_name}})

    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"

    kb = [
        [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))],
        [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link), types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)]
    ]

    # 🔗 ডিপ-লিংক (?start=<movie_id>) দিয়ে এসেছে কিন্তু পেন্ডিং ডেলিভারি হয়নি →
    # Watch Now বাটন সরাসরি ওই মুভির ডাউনলোড পেজ খুলবে (অ্যাড ফ্লো অপরিবর্তিত থাকবে)
    if payload and ObjectId.is_valid(payload) and not delivered and APP_URL:
        kb[0] = [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=f"{APP_URL}?open={payload}"))]

    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)

    text = f"👋 <b>স্বাগতম {message.from_user.first_name}!</b>\n\n🎬 Movie Box জগতে আপনাকে স্বাগতম। নিচের বাটনে ক্লিক করে মুভি উপভোগ করুন।\n\n🔍 <b>টিপস:</b> যেকোনো মুভি খুঁজতে শুধু মুভির নামটা লিখে পাঠান — পোস্টারসহ খুঁজে দেখাবো! (নামের একটা অংশ দিলেও চলবে)"
    if delivered:
        text = "✅ <b>আপনার ফাইল সফলভাবে ডেলিভারি হয়েছে — উপরে চেক করুন! 🎉</b>\n\n" + text
    if uid in admin_cache: text += "\n\n⚙️ <b>অ্যাডমিন মোড অন.</b>"
    await message.answer(text, reply_markup=markup, parse_mode="HTML")

@dp.message(Command("stats"))
async def bot_stats(m: types.Message):
    if m.from_user.id not in admin_cache: return
    total_users = await db.users.count_documents({})
    total_movies = await db.movies.count_documents({})
    vip_users = await db.users.count_documents({"vip_until": {"$gt": datetime.datetime.utcnow()}})
    text = f"📊 <b>Bot Statistics</b>\n\n👥 Total Users: <b>{total_users}</b>\n💎 VIP Users: <b>{vip_users}</b>\n🎬 Total Movies: <b>{total_movies}</b>"
    await m.answer(text, parse_mode="HTML")

@dp.message(Command("ban"))
async def ban_user(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        uid = int(m.text.split()[1])
        await db.banned.update_one({"user_id": uid}, {"$set": {"user_id": uid}}, upsert=True)
        banned_cache.add(uid)
        await m.answer(f"🚫 User <code>{uid}</code> ব্যান করা হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /ban USER_ID", parse_mode="HTML")

@dp.message(Command("unban"))
async def unban_user(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        uid = int(m.text.split()[1])
        await db.banned.delete_one({"user_id": uid})
        banned_cache.discard(uid)
        await m.answer(f"✅ User <code>{uid}</code> আনব্যান করা হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /unban USER_ID", parse_mode="HTML")

# ==========================================
# 🔍 ইউজারের মেসেজ = মুভি সার্চ: নামের যেকোনো অংশ (একটা অক্ষর হলেও) দিলেই
# পোস্টারসহ সুন্দর রেজাল্ট + Watch on Home বাটন। না পেলে সুন্দর গাইড মেসেজ +
# রিকোয়েস্ট অ্যাডমিনের কাছে চলে যাবে — অ্যাডমিন ✍️ রিপ্লাই চাপলে সরাসরি ইউজারকে উত্তর যাবে।
# ==========================================
async def search_movies_smart(q: str, limit: int = 6):
    """🔍 স্মার্ট মুভি সার্চ — নামের যেকোনো অংশ দিয়ে খোঁজা যায়।
    ধাপ ১: সব শব্দ টাইটেলে মিলতে হবে (AND — 'রঘু ডাকাত' দিলে 'রঘু ডাকাত' পাবে);
    ধাপ ২: না পেলে যেকোনো একটা শব্দ মিললেই (OR — ভুল/আংশিক নামে কাছাকাছি সাজেশন)।
    এক্স্যাক্ট ম্যাচ সবার আগে, তারপর বেশি ভিউ পাওয়া মুভি আগে।"""
    q = (q or "").strip()
    if not q or len(q) > 60: return []
    words = [w for w in re.split(r"\s+", q.lower()) if w]
    if not words: return []
    async def run_pipeline(match_stage):
        pipeline = [
            {"$match": match_stage},
            {"$group": {"_id": "$title", "doc_id": {"$first": {"$toString": "$_id"}}, "photo_id": {"$first": "$photo_id"}, "quality": {"$first": "$quality"}, "year": {"$first": "$year"}, "clicks": {"$sum": "$clicks"}, "created_at": {"$max": "$created_at"}, "parts": {"$sum": 1}, "qualities": {"$addToSet": {"$ifNull": ["$quality", "HD"]}}}},
            {"$sort": {"created_at": -1}},
            {"$limit": 40},
        ]
        try: return await db.movies.aggregate(pipeline).to_list(40)
        except Exception: return []
    results = await run_pipeline({"$and": [{"title": {"$regex": re.escape(w), "$options": "i"}} for w in words]})
    if not results and len(words) > 1:
        results = await run_pipeline({"$or": [{"title": {"$regex": re.escape(w), "$options": "i"}} for w in words]})
    if not results: return []
    ql = q.lower()
    def rank(mm):
        t = (mm["_id"] or "").lower()
        if t == ql: return 0
        if t.startswith(ql): return 1
        if ql in t: return 2
        return 3
    # 🆕 লেটেস্ট আগে: টাইটেল/কোয়ালিটি থেকে সর্বোচ্চ পর্ব-নম্বর বের করে নতুন আপলোড আগে দেখানো হয়,
    # তারপর বেশি ভিউ — ফলে 'Becholor Point' লিখলে নতুন চ্যাপ্টার (যেমন EP 129-134) সবার আগে আসে
    results.sort(key=lambda mm: (rank(mm), -_doc_latest_ep(mm["_id"], mm.get("qualities")), -(mm.get("clicks") or 0)))
    out = []
    for mm in results[:limit]:
        out.append({"title": mm["_id"], "doc_id": mm.get("doc_id"), "photo_id": mm.get("photo_id"), "quality": mm.get("quality"), "year": mm.get("year"), "parts": mm.get("parts", 1), "clicks": mm.get("clicks", 0), "qualities": _qualities_desc(mm.get("qualities"))})
    return out

# ==========================================
# 🔢 সার্চ হেল্পার + রেন্ডারার: এপিসোড নম্বর ধরা ('AP30'/'ep 30'/'নাম 30'), কোয়ালিটি লিস্ট
# গুছানো (নতুন→পুরনো), রেজাল্ট ক্যাপশন + বাটন (মুভির নাম / NEXT / সিমিলার) এক জায়গা থেকে
# বানানো — ইউজার ও অ্যাডমিন সার্চ দুটোই একই রেন্ডারার ব্যবহার করে।
# ==========================================
def _tok_number(tok):
    mm = re.search(r"(?<!\d)(\d{1,4})(?!\d)", tok or "")
    return int(mm.group()) if mm else None

def _qualities_desc(quals):
    """নতুন→পুরনো ক্রমে গুছানো (EP 96 • EP 95 • ...); সংখ্যা না থাকলে আলফাবেটিক। সর্বোচ্চ ১২টা।"""
    def key(tok):
        n = _tok_number(tok)
        return (0, -n, "") if n is not None else (1, 0, tok or "")
    return sorted([t for t in (quals or []) if t], key=key)[:12]

def _doc_latest_ep(title, quals=()):
    """ডকুমেন্টের সর্বোচ্চ পর্ব-নম্বর (টাইটেল 'EP 129-134' হলে 134)।
    বছর '2026' বা রেজোলিউশন '1080p' পর্ব হিসেবে ধরা যাবে না — শুধু EP/AP-ধাঁচের টোকেন।"""
    nums = [int(x) for x in re.findall(r"(?<!\d)\d{1,3}(?!\d)", title or "")]
    for tok in (quals or []):
        if re.search(r"(?i)(?:\bep|\bap|episode)", tok or ""):
            n = _tok_number(tok)
            if n: nums.append(n)
    return max(nums) if nums else 0

def _is_series_quals(quals):
    """কোয়ালিটি টোকেনগুলো পর্ব-ধাঁচের (EP/AP) কিনা — হলে ক্যাপশনে 'Episodes', নাহলে 'Quality'"""
    return any(re.search(r"(?i)(?:\bep|\bap|episode)", t or "") for t in (quals or []))

def _short_title(t, n=64):
    """বাটনে টাইটেল — নাম আগে; লম্বা হলেও শুরুর নাম অক্ষত রেখে শেষে কাটা হয়"""
    t = t or ""
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"

def _query_ep(q):
    """সার্চ টেক্সটে এপিসোড নম্বর খোঁজা — 'AP30', 'AP 30', 'ep 30', 'batchelor point 30' সব ধরবে"""
    mm = re.search(r"\b(?:ep|eps|episode|ap|bp|part|pt)\s*[._-]*\s*(\d{1,3})", q.lower())
    if mm: return int(mm.group(1))
    mm = re.search(r"(?<!\d)(\d{1,3})(?!\d)", q.lower())
    return int(mm.group(1)) if mm else None

def _doc_has_ep(mm, n):
    """ওই ডকুমেন্টে n নম্বর পর্ব আছে কিনা — টাইটেলে সরাসরি/রেঞ্জে ('EP 27-34' → 27≤30≤34), বা কোয়ালিটি লিস্টে"""
    t = mm.get("_id") or ""
    if re.search(rf"(?<!\d){n}(?!\d)", t): return True
    for a, b in re.findall(r"(\d{1,3})\s*[-–]\s*(\d{1,3})", t):
        if int(a) <= n <= int(b): return True
    for tok in (mm.get("qualities") or []):
        if _tok_number(tok) == n: return True
    return False

def build_search_reply(results, q, name=None, in_group=True):
    """🔍 সার্চ রেজাল্ট রেন্ডারার — লেটেস্ট আগে; 'AP30' লিখলে ৩০ নম্বর পর্বের ডকুমেন্ট টপে;
    নিচে সিমিলার বাটন (চাপলেই ওই পর্ব/মুভির পেজে সরাসরি ঢোকে)। Returns (caption, markup, best)"""
    q_num = _query_ep(q)
    best = results[0]
    if q_num is not None:
        for mm in results:
            if _doc_has_ep(mm, q_num):
                best = mm
                break
    # একই ডকুমেন্ট বারবার নয় — best বাদে বাকিগুলো (লেটেস্ট-ফার্স্ট অর্ডারেই)
    others = [mm for mm in results if mm.get("doc_id") != best.get("doc_id")]
    qd = _qualities_desc(best.get("qualities") or [best.get("quality") or "HD"])
    head = (f"🎉 <b>দারুণ পছন্দ {name}!</b>\n\nআপনার কাঙ্ক্ষিত মুভি — <b>«{best['title']}»</b> আমাদের লাইব্রেরিতে রয়েছে! 😍"
            if name else
            f"🎉 <b>পাওয়া গেছে!</b>\n\n🎬 <b>«{best['title']}»</b> লাইব্রেরিতে আছে!")
    ep_line = (f"\n🎯 আপনার খোঁজা <b>{q_num}</b> নম্বর পর্বটি এই লিংকেই আছে ✅"
               if q_num is not None and _doc_has_ep(best, q_num) else "")
    # সিরিজের EP-ধাঁচের লিস্ট হলে 'Episodes', মুভির রেজোলিউশন হলে 'Quality' লেবেল
    q_label = "Episodes (নতুন→পুরনো)" if _is_series_quals(qd) else "Quality (ভালো→কম)"
    caption = (
        f"{head}{ep_line}\n\n"
        f"📺 {q_label}: <b>{' • '.join(qd)}</b>\n"
        f"📅 Year: <b>{best.get('year') or 'N/A'}</b>\n"
        f"🔗 Download Links: <b>{best.get('parts', 1)}টি</b>\n\n"
        f"👉 নিচের <b>মুভির নামের বাটনে</b> ট্যাপ দিলেই মিনি অ্যাপে সরাসরি এই মুভির পেজ খুলে যাবে!\n"
        f"⏭ নিচের <b>সিমিলার বাটনে</b> চাপলে কাছাকাছি মুভি/পর্বেও সরাসরি ঢুকতে পারবেন।"
    )
    # 🎬 প্রথম বাটনেই মুভির পুরো নাম — 'Watch on Home' লেখার বদলে সার্চ দেওয়া মুভির নামটাই (ডিপ-লিংক)
    # ⚠️ টেলিগ্রাম-নিয়ম: web_app-বাটন শুধু প্রাইভেটে বৈধ — গ্রুপে পাঠালে BUTTON_TYPE_INVALID;
    # তাই গ্রুপে (in_group=True) url-বাটন: t.me/<bot>?startapp=<doc> — ট্যাপ করলে মিনি-অ্যাপই খোলে
    if APP_URL and not in_group:
        watch_row = [types.InlineKeyboardButton(text=f"🎬 {best['title']}", web_app=types.WebAppInfo(url=f"{APP_URL}?open={best['doc_id']}"))]
    else:
        watch_row = [types.InlineKeyboardButton(text=f"🎬 {best['title']}", url=f"https://t.me/{BOT_USERNAME}?startapp={best['doc_id']}")]
    kb = [watch_row]
    # 🎬 সিমিলার বাটন: ক্লিক করলেই ওই পর্ব/মুভির পেজ খুলে যাবে (টাইটেলের EP অংশ যেন দেখা যায়)
    if others:
        for o in others[0:4]:
            kb.append([types.InlineKeyboardButton(text=f"🎬 {_short_title(o['title'])}", url=f"https://t.me/{BOT_USERNAME}?startapp={o['doc_id']}")])
    kb.append([types.InlineKeyboardButton(text="📥 ডাউনলোড কিভাবে করবেন", url="https://t.me/SakibMovieBox/62")])
    return caption, types.InlineKeyboardMarkup(inline_keyboard=kb), best

# ==========================================
# 👥 গ্রুপ-সার্চ সিস্টেম — বট যেকোনো গ্রুপে মুভির নাম পেলে উত্তর দেয়
# ⚠️ জরুরি (টেলিগ্রামের নিয়ম): বট গ্রুপের সাধারণ টেক্সট তখনই পায় যখন —
#    ① বটকে ওই গ্রুপে Admin বানানো হয় (সবচেয়ে সহজ — অ্যাডমিন বট সব মেসেজ পায়), অথবা
#    ② BotFather → /mybots → বট → Bot Settings → Group Privacy → Turn off
#    কোনোটা করা না থাকলে টেলিগ্রামই বটকে মেসেজ পাঠায় না — তখন কোড চিত হোক বা না হোক, কিছুই হবে না।
#    বট নিজের পোস্ট নিজে ডিলিট করতে অ্যাডমিন-অধিকার লাগে না — মেসেজ পেলেই এই সিস্টেম চলে।
# ==========================================
GROUP_SEARCH_TTL_SEC = 60          # গ্রুপে সার্চ-রিপ্লাই কত সেকেন্ড থাকবে (এরপর অটো-ডিলিট)
GROUP_SEARCH_DEL_USER_MSG = False  # True দিলে সার্চদাতার মেসেজও ডিলিট হবে (এর জন্য অ্যাডমিন লাগবে)
GROUP_SEARCH_COOLDOWN = 10         # একই গ্রুপে পরপর রিপ্লাইয়ের ন্যূনতম ফাঁক (স্প্যাম-ব্রেক)
GROUP_SEARCH_MIN_LEN = 3           # এর চেয়ে ছোট টেক্সট এমনিতেই ইগনোর
# 1 দিলে কনসোলে গ্রুপ-সার্চ লগ দেখা যাবে (বট মেসেজ পাচ্ছে কি না যাচাইয়ের সহজ উপায়)
GROUP_SEARCH_DEBUG = os.getenv("GROUP_SEARCH_DEBUG", "0") == "1"

# এই সাধারণ চ্যাট-শব্দগুলো সার্চ-ম্যাচে ধরা হয় না ('রঘু ডাকাত টা দেও' → 'রঘু ডাকাত' খোঁজে)
GROUP_SEARCH_FILLERS = {
    "মুভি", "মুবি", "movie", "movies", "film", "নাম", "name", "দেখাও", "দেখো",
    "দেও", "দাও", "চাই", "chai", "লিংক", "link", "please", "pls", "plx",
    "ভাই", "bro", "কই", "এর", "টা", "টি", "দিবে", "দিবো", "দিও", "send", "do",
    "নতুন", "আছে", "হবে", "দেখি", "চলো", "কি", "কী", "নাকি", "লাগবে", "খুঁজছি",
    "খুজতেছি", "চাচ্ছি", "need", "want", "give", "old", "new", "একটা", "টার",
}

group_last_reply: dict = {}        # chat_id → শেষ রিপ্লাইয়ের ts (কুলডাউনের জন্য)

# না-পাওয়া গেলে গ্রুপে একটা সফট হিন্ট যাবে কি না (0 দিলে বন্ধ; ডিফল্ট চালু — বট যেন মৃত না দেখায়)
GROUP_SEARCH_MISS_REPLY = os.getenv("GROUP_SEARCH_MISS_REPLY", "1") == "1"
GROUP_SEARCH_MISS_TTL_SEC = 60     # হিন্ট-রিপ্লাই কত সেকেন্ড থাকবে (এরপর অটো-ডিলিট)

# ── 🌐 ল্যাটিন↔বাংলা ট্রান্সলিট-ফলব্যাক: ব্যঞ্জন-কঙ্কাল ম্যাচিং ──
# "Raghu Dakat" (ইংরেজি অক্ষরে) দিয়েও "রঘু ডাকাত"-এর মতো বাংলা টাইটেল ধরতে:
# স্বর/কার বাদ দিয়ে শুধু ব্যঞ্জন নিয়ে তুলনা — raghu→rgh, রঘু→rgh ✓ ; dakat→dkt, ডাকাত→dkt ✓
_BN_CONS = {
    "ক": "k", "খ": "kh", "গ": "g", "ঘ": "gh", "ঙ": "ng",
    "চ": "ch", "ছ": "chh", "জ": "j", "ঝ": "jh", "ঞ": "n",
    "ট": "t", "ঠ": "th", "ড": "d", "ঢ": "dh", "ণ": "n",
    "ত": "t", "থ": "th", "দ": "d", "ধ": "dh", "ন": "n",
    "প": "p", "ফ": "ph", "ব": "b", "ভ": "bh", "ম": "m",
    "য": "j", "র": "r", "ল": "l", "শ": "sh", "ষ": "sh",
    "স": "s", "হ": "h", "ড়": "r", "ঢ়": "rh", "য়": "y",
    "ৎ": "t", "জ়": "z",
}
_BN_SKIP = set("অআইঈউঊঋএঐওঔ" + "ািীুূৃেৈোৌ" + "ংঃঁ্ৗ়")

def _bn_skeleton(w: str) -> str:
    return "".join(_BN_CONS.get(ch, "") for ch in w if ch not in _BN_SKIP)

def _lat_skeleton(w: str) -> str:
    return "".join(ch for ch in w.lower() if ch.isalpha() and ch not in "aeiou")

def _word_skeleton(w: str) -> str:
    # বাংলা-অক্ষরের শব্দ হলে বাংলা-কঙ্কাল, নাহলে ল্যাটিন-কঙ্কাল
    if any("\u0980" <= ch <= "\u09FF" for ch in w): return _bn_skeleton(w)
    return _lat_skeleton(w)

def _title_words(t: str):
    return [tw for tw in re.split(r"[^\w\u0980-\u09FF]+", (t or "").lower()) if tw]

def _skeleton_hit(words, titles):
    """কুয়েরির সব নির্ভরযোগ্য শব্দ-কঙ্কাল যে টাইটেলে মেলে, সেই টাইটেল ফেরত; না মিললে None"""
    qsk = [(w, _word_skeleton(w)) for w in words]
    qsk = [(w, s) for w, s in qsk if len(s) >= 2]   # ২-অক্ষরের নিচে কঙ্কাল অনির্ভরযোগ্য
    if not qsk: return None
    best, best_score = None, 0
    for t in titles:
        twords = _title_words(t)
        tsk = [_word_skeleton(tw) for tw in twords]
        score = 0
        for w, s in qsk:
            hitw = False
            for tw, ts in zip(twords, tsk):
                if ts and (s == ts or (len(s) >= 3 and s in ts)):
                    hitw = True; break
            if hitw: score += 1
            else: score = -99; break    # একটা শব্দও না মিললে ওই টাইটেল বাতিল
        if score > best_score: best, best_score = t, score
    return best

@dp.message(lambda m: m.chat.type == "private" and m.from_user.id not in admin_cache)
async def handle_user_messages(m: types.Message):
    uid = m.from_user.id
    name = m.from_user.first_name or "Friend"
    if m.content_type not in ['text']:
        await m.answer("⚠️ দুঃখিত! আমি শুধুমাত্র টেক্সট মেসেজ গ্রহণ করি।\n\n🎬 মুভি দেখতে নিচের 'Watch Now' বাটনে ক্লিক করুন।", parse_mode="HTML")
        return
    q = (m.text or "").strip()
    if not q or q.startswith("/"): return
    results = await search_movies_smart(q)
    if results:
        # 🔍 লেটেস্ট-ফার্স্ট + সিমিলার বাটনসহ রেন্ডার ('AP30'-স্টাইল এপিসোড ম্যাচও ধরা হয়)
        caption, markup, best = build_search_reply(results, q, name)
        try:
            if best.get("photo_id"):
                await bot.send_photo(uid, photo=best["photo_id"], caption=caption, parse_mode="HTML", reply_markup=markup)
            else:
                await m.answer(caption, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            try:
                await m.answer(caption, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
            except Exception: pass
        return
    # ❌ পাওয়া যায়নি → সুন্দর গাইড মেসেজ (সঠিক নাম নিয়ে আবার লিখতে বলা) + অ্যাডমিনকে রিকোয়েস্ট
    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    nf_kb = []
    if APP_URL:
        nf_kb.append([types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))])
    nf_kb.append([types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link)])
    nf_text = (
        f"😔 <b>দুঃখিত {name}!</b>\n\n"
        f"আপনি যে মুভিটি খুঁজছেন — <b>«{q[:60]}»</b> — এটি এখনো আমাদের লাইব্রেরিতে যোগ হয়নি।\n\n"
        f"💡 <b>সম্ভবত নামটি সঠিক নয়!</b> গুগলে সার্চ করে মুভিটির <b>সঠিক নাম</b> নিয়ে এখানে আবার লিখুন — সাথে সাথেই খুঁজে পেয়ে যাবেন।\n\n"
        f"✍️ আপনার রিকোয়েস্টটি অ্যাডমিন টিমের কাছে পাঠিয়ে দেওয়া হয়েছে — খুব দ্রুত মুভিটি যোগ করা হবে ইনশাআল্লাহ!\n\n"
        f"👇 ততক্ষণ হাজারো মুভি ঘুরে দেখুন:"
    )
    try:
        await m.answer(nf_text, parse_mode="HTML", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=nf_kb), disable_web_page_preview=True)
    except Exception: pass
    # 📩 অ্যাডমিনের কাছে রিকোয়েস্ট — ✍️ রিপ্লাই বাটনে চাপলে সরাসরি ইউজারকে উত্তর পাঠানো যাবে
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ রিপ্লাই", callback_data=f"reply_{uid}")
        await bot.send_message(OWNER_ID, f"🔍 <b>নতুন মুভি রিকোয়েস্ট!</b>\n\n👤 <a href='tg://user?id={uid}'>{name}</a> (<code>{uid}</code>)\n🎬 খোঁজে: <b>{q[:60]}</b>\n\n<i>রিপ্লাই বাটনে চাপলে ইউজারকে সরাসরি উত্তর পাঠাতে পারবেন (যেমন: ঠিক আছে ভাই, নেক্সটে অ্যাড করে দিবো)।</i>", parse_mode="HTML", reply_markup=builder.as_markup())
    except Exception: pass

# ── 👥 গ্রুপ-সার্চ হ্যান্ডলার: গ্রুপের সাধারণ টেক্সট মেসেজ ──
# বট মেসেজ পেলেই সার্চ করে (কোনো অ্যাডমিন-গেট নেই); আসল নাম-ম্যাচ না হলে চুপ থাকে —
# তাই গ্রুপের র্যান্ডম কথাবার্তায় বট হস্তক্ষেপ করে না।
@dp.message(lambda m: m.chat.type in ("group", "supergroup")
            and m.from_user is not None
            and (m.text or "").strip() and not (m.text or "").strip().startswith("/"))
async def handle_group_search(m: types.Message):
    q = (m.text or "").strip()
    # বটের নিজের মেসেজ / লিংকড-চ্যানেলের ফরোয়ার্ড বাদ; তবে anonymous-admin (গ্রুপের নামে পোস্ট) গ্রহণ
    if m.from_user.is_bot:
        sc = m.sender_chat
        if not (sc and sc.id == m.chat.id and sc.type in ("group", "supergroup")): return
        s_name, s_id, s_linked = (sc.title or "অ্যাডমিন"), sc.id, False
    else:
        s_name, s_id, s_linked = m.from_user.first_name or "বন্ধু", m.from_user.id, True
    if len(q) < GROUP_SEARCH_MIN_LEN or len(q) > 60: return
    chat_id = m.chat.id
    if GROUP_SEARCH_DEBUG: print(f"👥 গ্রুপ-সার্চ [{chat_id}] {s_name}: {q!r}", flush=True)

    # 🔍 চ্যাট-শব্দ বাদ দিয়ে আসল নামটা বের করে সেটাই সার্চ — র‍্যাঙ্কিংও তখন শুদ্ধ হয়
    words = [w for w in re.split(r"\s+", q.lower()) if w and w not in GROUP_SEARCH_FILLERS and len(w) >= 2]
    if not words:
        if GROUP_SEARCH_DEBUG: print("👥 → শুধু চ্যাট-শব্দ → চুপ", flush=True)
        return
    s_query = " ".join(words)

    # ── ধাপ ১: সরাসরি সার্চ ──
    res = await search_movies_smart(s_query)
    hit = None
    ok = False
    if res:
        ok = any(words[0] in (mm.get("title") or "").lower() for mm in res) if len(words) == 1 \
            else any(all(w in (mm.get("title") or "").lower() for w in words) for mm in res)

    # ── ধাপ ২: ল্যাটিন-অক্ষরে লেখা সার্চও বাংলা টাইটেল ধরুক — ব্যঞ্জন-কঙ্কাল ফলব্যাক ──
    if not ok:
        try:
            cand_rows = await db.movies.aggregate([
                {"$sort": {"created_at": -1}}, {"$limit": 500}, {"$project": {"title": 1, "_id": 0}}
            ]).to_list(500)
        except Exception as e:
            if GROUP_SEARCH_DEBUG: print(f"👥 → ফলব্যাক-লিস্ট আনতে ব্যর্থ: {e}", flush=True)
            cand_rows = []
        cand_titles = []
        for r in cand_rows:
            t = r.get("title") or (r.get("_id") if isinstance(r.get("_id"), str) else "") or ""
            if t: cand_titles.append(t)
        hit = _skeleton_hit(words, cand_titles) if cand_titles else None
        if GROUP_SEARCH_DEBUG:
            print(f"👥 → সরাসরি ম্যাচ নেই ({s_query!r}) → ট্রান্সলিট-ফলব্যাক → {hit!r}", flush=True)
        if hit:
            res = await search_movies_smart(hit)
            ok = bool(res)
            if GROUP_SEARCH_DEBUG: print(f"👥 → ফলব্যাক-ম্যাচ {hit!r} → {'ফলাফল মিলেছে' if ok else 'ফলাফল খালি'}", flush=True)

    # ── ধাপ ৩: কিছুই না পেলে ফ্রেন্ডলি হিন্ট (বট যেন মৃত না দেখায়) ──
    if not ok:
        if GROUP_SEARCH_DEBUG: print(f"👥 → কিছুই পাওয়া যায়নি ({s_query!r}) → {'হিন্ট যাবে' if GROUP_SEARCH_MISS_REPLY else 'চুপ'}", flush=True)
        if not GROUP_SEARCH_MISS_REPLY: return
        # অবিশ্বস্ত শব্দ-কঙ্কাল (যেমন 'Haa'→'h') হলে হিন্ট দিয়ে সুবিধা না — চুপ
        if any(len(_word_skeleton(w)) < 2 for w in words):
            if GROUP_SEARCH_DEBUG: print("👥 → শব্দ-কঙ্কাল অবিশ্বস্ত → চুপ", flush=True)
            return
        now = time.time()
        if now - group_last_reply.get(chat_id, 0) < GROUP_SEARCH_COOLDOWN: return
        hint = (f"❓ <b>{html.escape(q)}</b> নামে কিছু পাই নাই 🙏\n"
                "বানান মিলিয়ে আবার লেখো — ইংরেজি বা বাংলা যেকোনো অক্ষরে লিখলেই খুঁজে নেব।\n"
                f"ব্যক্তিগত সার্চের জন্য আমাকে <a href='https://t.me/{BOT_USERNAME}'>প্রাইভেটে</a> /start দাও!")
        try:
            sent_hint = await bot.send_message(chat_id, hint, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            if GROUP_SEARCH_DEBUG: print(f"👥 → হিন্ট পাঠানো গেল না: {e}", flush=True)
            return
        group_last_reply[chat_id] = now
        del_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=GROUP_SEARCH_MISS_TTL_SEC)
        try:
            await db.auto_delete.insert_one({"chat_id": chat_id, "message_id": sent_hint.message_id, "delete_at": del_at})
        except Exception: pass
        async def _zap_hint():
            await asyncio.sleep(GROUP_SEARCH_MISS_TTL_SEC)
            try: await bot.delete_message(chat_id, sent_hint.message_id)
            except Exception: pass
            try: await db.auto_delete.delete_many({"chat_id": chat_id, "message_id": sent_hint.message_id})
            except Exception: pass
        asyncio.create_task(_zap_hint())
        if GROUP_SEARCH_DEBUG: print(f"👥 → হিন্ট গেছে (msg {sent_hint.message_id})", flush=True)
        return

    # ⏱ কুলডাউন — একই গ্রুপে টানা টানা রিপ্লাই নয়
    now = time.time()
    if now - group_last_reply.get(chat_id, 0) < GROUP_SEARCH_COOLDOWN: return

    # 🏷️ সার্চদাতার নাম মেনশন (anonymous-admin হলে সাদামাটা নাম — চ্যাট-আইডি লিংক করা যায় না)
    g_mention = (f"<a href='tg://user?id={s_id}'>{html.escape(s_name)}</a>" if s_linked else html.escape(s_name))
    caption, markup, best = build_search_reply(res, hit or s_query, name=g_mention, in_group=(m.chat.type != "private"))
    sent = None
    try:
        if best.get("photo_id"):
            sent = await bot.send_photo(chat_id, photo=best["photo_id"], caption=caption, parse_mode="HTML", reply_markup=markup)
        else:
            sent = await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        if GROUP_SEARCH_DEBUG: print(f"👥 → পোস্টার-পোস্ট ব্যর্থ ({e}) → টেক্সট-ফলব্যাক", flush=True)
        try:
            sent = await bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e2:
            if GROUP_SEARCH_DEBUG: print(f"👥 → রিপ্লাই পাঠানোই গেল না: {e2}", flush=True)
            return
    if sent is None: return
    group_last_reply[chat_id] = now
    if GROUP_SEARCH_DEBUG: print(f"👥 → রিপ্লাই গেছে (msg {sent.message_id})", flush=True)

    # ⏲ ১ মিনিট পর অটো-ডিলিট:
    #   ① মঙ্গোর auto_delete রেকর্ড → auto_delete_worker (বট রিস্টার্ট হলেও ডিলিট হবে)
    #   ② সাথে সাথে একটা টাইমার-টাস্ক → ঠিক GROUP_SEARCH_TTL_SEC পরপরই ডিলিট + রেকর্ড মুছে দেয়
    del_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=GROUP_SEARCH_TTL_SEC)
    ids = [sent.message_id]
    if GROUP_SEARCH_DEL_USER_MSG:
        ids.append(m.message_id)
        try: await db.auto_delete.insert_one({"chat_id": chat_id, "message_id": m.message_id, "delete_at": del_at})
        except Exception: pass
    try:
        await db.auto_delete.insert_one({"chat_id": chat_id, "message_id": sent.message_id, "delete_at": del_at})
    except Exception: pass

    async def _zap():
        await asyncio.sleep(GROUP_SEARCH_TTL_SEC)
        for mid in ids:
            try: await bot.delete_message(chat_id, mid)
            except Exception: pass
        try:
            await db.auto_delete.delete_many({"chat_id": chat_id, "message_id": {"$in": ids}})
        except Exception: pass

    asyncio.create_task(_zap())

@dp.callback_query(F.data.startswith("reply_"))
async def reply_to_user_callback(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in admin_cache: return
    user_id = int(c.data.split("_")[1])
    await state.set_state(AdminStates.waiting_for_reply)
    await state.update_data(reply_user_id=user_id)
    await c.message.answer("✍️ আপনার মেসেজ লিখুন (রিপ্লাই দেওয়ার জন্য):")
    await c.answer()

@dp.message(AdminStates.waiting_for_reply)
async def send_reply_to_user(m: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("reply_user_id")
    await state.clear()
    if user_id:
        try:
            await m.copy_to(chat_id=user_id)
            await m.answer("✅ রিপ্লাই পাঠানো হয়েছে!")
        except:
            await m.answer("❌ রিপ্লাই পাঠাতে ব্যর্থ হয়েছে।")

# ==========================================
# 7. Admin Commands & Movie Upload
# ==========================================
@dp.message(Command("cancel"))
async def cancel_cmd(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.clear()
    await m.answer("❌ বর্তমান প্রসেস বাতিল করা হয়েছে!", parse_mode="HTML")

@dp.message(Command("protect"))
async def toggle_protect(m: types.Message):
    if m.from_user.id not in admin_cache: return
    cfg = await db.settings.find_one({"id": "protect_content"})
    current = cfg.get("status", False) if cfg else False
    new_status = not current
    await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": new_status}}, upsert=True)
    status_text = "অন 🔒" if new_status else "অফ 🔓"
    await m.answer(f"✅ ফরোয়ার্ড প্রোটেকশন এখন <b>{status_text}</b>", parse_mode="HTML")

@dp.message(Command("setadcount"))
async def set_ad_count(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        count = int(m.text.split()[1])
        if count < 1: count = 1
        await db.settings.update_one({"id": "ad_count"}, {"$set": {"value": count}}, upsert=True)
        await m.answer(f"✅ অ্যাড সংখ্যা <b>{count}</b> এ সেট করা হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /setadcount 2 (প্রতি আনলকে কতগুলো অ্যাড দেখাবে)", parse_mode="HTML")

@dp.message(Command("settime"))
async def set_delete_time(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        minutes = int(m.text.split()[1])
        await db.settings.update_one({"id": "del_time"}, {"$set": {"minutes": minutes}}, upsert=True)
        await m.answer(f"✅ অটো-ডিলিট টাইম <b>{minutes} মিনিট</b> এ সেট করা হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /settime 60 (মিনিট লিখুন)", parse_mode="HTML")

@dp.message(Command("addlink"))
async def add_link_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer("✅ অ্যাড জোন লিংক অ্যাড হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /addlink url", parse_mode="HTML")

@dp.message(Command("addadultlink"))
async def add_adult_link_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "adult_direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer("✅ ১৮+ অ্যাড লিংক অ্যাড হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /addadultlink url", parse_mode="HTML")

@dp.message(Command("dellink"))
async def del_link_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "direct_links"}, {"$pull": {"links": url}})
        await m.answer("🗑️ অ্যাড জোন লিংক ডিলিট হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /dellink url", parse_mode="HTML")

@dp.message(Command("ads"))
async def list_ads_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    dl_cfg = await db.settings.find_one({"id": "direct_links"}); links = dl_cfg.get("links", []) if dl_cfg else []
    adl_cfg = await db.settings.find_one({"id": "adult_direct_links"}); alinks = adl_cfg.get("links", []) if adl_cfg else []
    ac_cfg = await db.settings.find_one({"id": "ad_count"}); ad_count = ac_cfg.get("value", 1) if ac_cfg else 1
    aw_cfg = await db.settings.find_one({"id": "ad_wait"}); ad_wait = aw_cfg.get("value", 10) if aw_cfg else 10
    bd_cfg = await db.settings.find_one({"id": "bcast_del"}); bdel = bd_cfg.get("value", 72) if bd_cfg else 72
    bdel_text = "অফ (কখনো ডিলিট হবে না)" if bdel == 0 else f"{bdel} ঘণ্টা"
    text = (f"💰 <b>বর্তমান অ্যাড সেটিংস</b>\n\n"
            f"🔗 সাধারণ ডাইরেক্ট লিংক: <b>{len(links)}</b>টি\n"
            f"🔴 ১৮+ ডাইরেক্ট লিংক: <b>{len(alinks)}</b>টি\n"
            f"🎲 র‍্যান্ডম সিস্টেম চালু: প্রতিবার ক্লিকে পুল থেকে র‍্যান্ডম লিংক — সেইম লিংক পরপর দুইবার যায় না\n"
            f"🎯 প্রতি আনলকে অ্যাড সংখ্যা: <b>{ad_count}</b>টি\n"
            f"⏱️ প্রতি অ্যাডে অপেক্ষার সময়: <b>{ad_wait} সেকেন্ড</b>\n"
            f"📢 ব্রডকাস্ট অটো-ডিলিট: <b>{bdel_text}</b>\n\n"
            f"<i>কমান্ড: /addlink, /dellink, /addadultlink, /setadcount, /setadwait, /setbcastdel</i>\n\n"
            f"<i>💡 টিপস: ব্যানার ইম্প্রেশন এখন শুধু ইউজারের চোখে আসা ব্যানারেরই গোনা হয় — লুকানো মোডালের "
            f"মৃত ইম্প্রেশন আর অদেখা ফিড-ব্যানার লোড বন্ধ করা হয়েছে। ইম্প্রেশন কমে গেলেও প্রতি ইম্প্রেশনের ভ্যালু বাড়বে, "
            f"তাই মোট CPM ওঠার কথা। আর ডাইরেক্ট লিংক র‍্যান্ডমলি ঘুরিয়ে দেওয়া ঠিকই আছে — এতে CPM পড়ে না।</i>")
    if links:
        text += "\n\n🔗 <b>সাধারণ লিংক লিস্ট:</b>\n" + "\n".join(f"{i+1}. <code>{l}</code>" for i, l in enumerate(links))
    if alinks:
        text += "\n\n🔴 <b>১৮+ লিংক লিস্ট:</b>\n" + "\n".join(f"{i+1}. <code>{l}</code>" for i, l in enumerate(alinks))
    await m.answer(text, parse_mode="HTML", disable_web_page_preview=True)

@dp.message(Command("setadwait"))
async def set_ad_wait(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        sec = int(m.text.split()[1])
        if sec < 5: sec = 5
        await db.settings.update_one({"id": "ad_wait"}, {"$set": {"value": sec}}, upsert=True)
        await m.answer(f"✅ অ্যাড অপেক্ষার সময় <b>{sec} সেকেন্ড</b> এ সেট হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /setadwait 10 (সেকেন্ড লিখুন, সর্বনিন্ম ৫)", parse_mode="HTML")

@dp.message(Command("setbcastdel"))
async def set_bcast_del(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        hours = int(m.text.split()[1])
        if hours < 0: hours = 0
        await db.settings.update_one({"id": "bcast_del"}, {"$set": {"value": hours}}, upsert=True)
        status_text = "অফ (মেসেজ কখনো অটো-ডিলিট হবে না)" if hours == 0 else f"{hours} ঘণ্টা"
        await m.answer(f"✅ ব্রডকাস্ট মেসেজ অটো-ডিলিট এখন: <b>{status_text}</b>", parse_mode="HTML")
    except: await m.answer("⚠️ /setbcastdel 72 (ঘণ্টা লিখুন, 0 দিলে অফ)", parse_mode="HTML")

@dp.message(Command("settg"))
async def set_tg_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "tg_link"}, {"$set": {"url": link}}, upsert=True)
        await m.answer("✅ টেলিগ্রাম চ্যানেল লিংক আপডেট হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /settg https://t.me/...", parse_mode="HTML")

# ==========================================
# 📝 লগ চ্যানেল ইজি-সেট সিস্টেম
# আপনার লগ চ্যানেল থেকে যেকোনো একটা মেসেজ বটে ফরোয়ার্ড করুন →
# বট চ্যানেলটি ধরে ফেলবে এবং একটা "✅ এই চ্যানেল লগ সেট করুন" বাটন দেখাবে,
# অথবা সেই ফরোয়ার্ড মেসেজে রিপ্লাই দিয়ে /setlogchannel লিখুন — সেট হয়ে যাবে।
# ==========================================
async def apply_log_channel(chat_obj, editor_message):
    """লগ চ্যানেল DB-তে সেভ + গ্লোবাল ভ্যারিয়েবল আপডেট — সাথে সাথে নতুন চ্যানেলে লগ যেতে শুরু করবে।"""
    global LOG_CHANNEL_ID
    LOG_CHANNEL_ID = chat_obj.id
    title = chat_obj.title or ""
    try:
        chat = await bot.get_chat(chat_obj.id)
        title = chat.title or title
        username = f"@{chat.username}" if getattr(chat, "username", None) else ""
    except: username = ""
    await db.settings.update_one({"id": "log_channel"}, {"$set": {"chat_id": chat_obj.id, "title": title}}, upsert=True)
    await editor_message.answer(
        f"✅ <b>লগ চ্যানেল সেট হয়ে গেছে!</b>\n\n"
        f"📺 চ্যানেল: <b>{title or chat_obj.id}</b>\n"
        f"🆔 ID: <code>{chat_obj.id}</code>{(' | ' + username) if username else ''}\n\n"
        f"🧪 টেস্ট করতে লিখুন: /testlog\n"
        f"❌ টেস্ট ফেল করলে বুঝবেন বটকে ওই চ্যানেলে অ্যাডমিন বানাতে হবে।",
        parse_mode="HTML", disable_web_page_preview=True)

@dp.message(Command("setlogchannel"))
async def set_log_channel_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    fwd = m.reply_to_message.forward_from_chat if m.reply_to_message else None
    if not fwd or fwd.type != "channel":
        return await m.answer(
            "⚠️ <b>ফরম্যাট:</b>\n\n"
            "১️⃣ আপনার লগ চ্যানেল থেকে <b>যেকোনো একটা মেসেজ ফরোয়ার্ড</b> করে এই বটে পাঠান\n"
            "২️⃣ সেই ফরোয়ার্ড করা মেসেজে <b>রিপ্লাই</b> দিয়ে <code>/setlogchannel</code> লিখুন\n\n"
            "✅ বাটন দিয়েও করতে পারবেন — ফরোয়ার্ড করলেই বট চ্যানেল ধরে বাটন দেখায়।",
            parse_mode="HTML")
    await apply_log_channel(fwd, m)

# 🤖 অ্যাডমিন টেক্সট মেসেজ ফরোয়ার্ড করলে বট নিজে থেকেই চ্যানেল চিনে বাটন দেখাবে
@dp.message(lambda m: m.chat.type == "private" and m.from_user.id in admin_cache
            and m.text and m.forward_from_chat and m.forward_from_chat.type == "channel")
async def detect_log_channel_forward(m: types.Message):
    ch = m.forward_from_chat
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ এই চ্যানেল লগ সেট করুন", callback_data=f"setlog_{ch.id}")
    await m.answer(
        f"📝 <b>চ্যানেল শনাক্ত হয়েছে!</b>\n\n"
        f"📺 <b>{ch.title or ch.id}</b>\n"
        f"🆔 ID: <code>{ch.id}</code>\n\n"
        f"👇 নিচের বাটনে ক্লিক করে এটিকে লগ চ্যানেল সেট করুন,\n"
        f"অথবা এই ফরোয়ার্ডে রিপ্লাই দিয়ে <code>/setlogchannel</code> লিখুন।",
        reply_markup=builder.as_markup(), parse_mode="HTML")

# ==========================================
# 🔍 অ্যাডমিন মুভি সার্চ: অ্যাডমিও বটে মুভির নাম লিখে সার্চ করতে পারবেন (কোনো সমস্যা নেই)।
# মুভি না পেলে ইউজারদের মতো "রিকোয়েস্ট অ্যাডমিনের কাছে গেল" মেসেজ যাবে না — অ্যাডমিনের
# জন্য আলাদা মেসেজ: "এই মুভিটা নাই, আপনি দয়া করে আপলোড করুন"।
# (StateFilter(None) → FSM আপলোড/ব্রডকাস্ট ফ্লো চলাকালীন এই হ্যান্ডলার কিছুই ধরবে না)
# ==========================================
@dp.message(StateFilter(None), F.text, lambda m: m.chat.type == "private" and m.from_user.id in admin_cache and not (m.text or "").startswith("/"))
async def admin_search_movies(m: types.Message):
    q = (m.text or "").strip()
    if not q: return
    results = await search_movies_smart(q)
    if results:
        # 🔍 অ্যাডমিন সার্চেও একই রেন্ডারার — লেটেস্ট-ফার্স্ট + NEXT/সিমিলার বাটন
        caption, markup, best = build_search_reply(results, q)
        try:
            if best.get("photo_id"):
                await bot.send_photo(m.from_user.id, photo=best["photo_id"], caption=caption, parse_mode="HTML", reply_markup=markup)
            else:
                await m.answer(caption, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            try:
                await m.answer(caption, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
            except Exception: pass
        return
    # ❌ অ্যাডমিনের জন্য আলাদা মেসেজ — রিকোয়েস্ট ফরওয়ার্ড হবে না, সরাসরি আপলোড করতে বলা হবে
    await m.answer(
        f"😔 <b>এই মুভিটি ডাটাবেজে নেই!</b>\n\n"
        f"🔍 খোঁজা হয়েছে: <b>«{q[:60]}»</b>\n\n"
        f"📤 আপনি দয়া করে মুভির ফাইল/ভিডিওটি এই চ্যাটে পাঠিয়ে দিন — আপলোড প্রসেস শুরু হয়ে যাবে।",
        parse_mode="HTML")

@dp.callback_query(F.data.startswith("setlog_"))
async def set_log_channel_callback(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    try:
        chat_id = int(c.data.split("_")[1])
    except:
        return await c.answer("⚠️ চ্যানেল আইডি পড়া যায়নি!", show_alert=True)
    try:
        chat_obj = await bot.get_chat(chat_id)
        await apply_log_channel(chat_obj, c.message)
    except Exception as e:
        await db.settings.update_one({"id": "log_channel"}, {"$set": {"chat_id": chat_id, "title": ""}}, upsert=True)
        global LOG_CHANNEL_ID
        LOG_CHANNEL_ID = chat_id
        await c.message.edit_text(f"✅ লগ চ্যানেল সেট হয়েছে (ID: <code>{chat_id}</code>)।\n\n⚠️ চ্যানেল ইনফো আনা যায়নি: <code>{e}</code>\n\n🧪 টেস্ট করতে /testlog লিখুন।", parse_mode="HTML")
    await c.answer("✅ সেট হয়েছে!")

# 🧪 টেস্ট কমান্ড: বর্তমান লগ চ্যানেলে একটা টেস্ট পোস্ট পাঠায় — চ্যানেলে গিয়ে দেখলেই বোঝা যাবে সব ঠিক আছে কিনা
@dp.message(Command("testlog"))
async def test_log_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    title = ""
    try:
        chat = await bot.get_chat(LOG_CHANNEL_ID)
        title = chat.title or ""
    except: pass
    try:
        await bot.send_message(
            LOG_CHANNEL_ID,
            "🧪 <b>Test Post</b>\n\n✅ লগ চ্যানেল ঠিকমতো কাজ করছে!\n🎬 এই পোস্টটি Movie Box বট অটোমেটিক পাঠিয়েছে।",
            parse_mode="HTML")
        await m.answer(
            f"✅ <b>টেস্ট সফল!</b>\n\n"
            f"📝 বর্তমান লগ চ্যানেল: <b>{title or LOG_CHANNEL_ID}</b> (<code>{LOG_CHANNEL_ID}</code>)\n\n"
            f"👉 এখন আপনার লগ চ্যানেলে গিয়ে দেখুন — সেখানে একটা 🧪 টেস্ট পোস্ট এসেছে।\n"
            f"মুভি আপলোডের পর Log / Broadcast & Log চাপলে পোস্ট এই চ্যানেলেই যাবে।",
            parse_mode="HTML")
    except Exception as e:
        await m.answer(
            f"❌ <b>টেস্ট ফেল হয়েছে!</b>\n\n"
            f"📝 বর্তমান লগ চ্যানেল: <code>{LOG_CHANNEL_ID}</code>\n"
            f"কারণ: <code>{e}</code>\n\n"
            f"🔧 <b>সমাধান:</b>\n"
            f"১️⃣ বটকে লগ চ্যানেলে <b>অ্যাডমিন</b> বানান (Post Messages পারমিশন দিন)\n"
            f"২️⃣ চ্যানেল বদলাতে চাইলে: নতুন চ্যানেল থেকে একটা মেসেজ ফরোয়ার্ড করে তাতে রিপ্লাই দিয়ে /setlogchannel দিন\n"
            f"৩️⃣ আবার /testlog দিয়ে চেক করুন",
            parse_mode="HTML")

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        title = m.text.split(" ", 1)[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0: await m.answer(f"✅ '<b>{title}</b>' ডিলিট হয়েছে!", parse_mode="HTML")
        else: await m.answer("⚠️ পাওয়া যায়নি")
    except: await m.answer("⚠️ /delmovie মুভির নাম", parse_mode="HTML")

@dp.message(Command("addvip"))
async def add_vip_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        args = m.text.split()
        target_uid = int(args[1])
        days = int(args[2]) if len(args) > 2 else 30 
        now = datetime.datetime.utcnow()
        user = await db.users.find_one({"user_id": target_uid})
        if not user: return await m.answer("⚠️ ইউজার নেই।")
        current_vip = user.get("vip_until", now)
        if current_vip < now: current_vip = now
        await db.users.update_one({"user_id": target_uid}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=days)}})
        await m.answer(f"✅ <code>{target_uid}</code> কে {days} দিনের VIP দেওয়া হয়েছে!", parse_mode="HTML")
    except: await m.answer("⚠️ /addvip ID দিন", parse_mode="HTML")

@dp.message(Command("addupcoming"))
async def add_upcoming_start(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_upc_photo)
    await m.answer("🌟 আপকামিং মুভির <b>পোস্টার</b> পাঠান।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_photo, F.photo)
async def receive_upc_photo(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_upc_title)
    await m.answer("✅ এবার <b>মুভির নাম</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_title, F.text)
async def receive_upc_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_upc_date)
    await m.answer("✅ এবার <b>রিলিজ তারিখ</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_date, F.text)
async def receive_upc_date(m: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await db.upcoming.insert_one({"title": data["title"], "photo_id": data["photo_id"], "release_date": m.text.strip()})
    await m.answer(f"🌟 <b>{data['title']}</b> আপকামিং লিস্টে যুক্ত হয়েছে!", parse_mode="HTML")

# ==========================================
# 7.5 Single Movie Upload
# ==========================================
@dp.message(F.content_type.in_({'video', 'document'}), lambda m: m.from_user.id in admin_cache)
async def receive_movie_file(m: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        await m.answer("⚠️ আপনি অন্য একটি প্রসেসে আটকে আছেন! আগে /cancel করুন।", parse_mode="HTML")
        return
    fid = m.video.file_id if m.video else m.document.file_id
    ftype = "video" if m.video else "document"
    await state.set_state(AdminStates.waiting_for_photo)
    await state.update_data(file_id=fid, file_type=ftype, categories=[])
    await m.answer("✅ ফাইল পেয়েছি! এবার মুভির <b>পোস্টার/ছবি</b> পাঠান (পোস্টার ছাড়া আপলোড করতে চাইলে <b>/skip</b> লিখুন)।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_photo, F.photo)
async def receive_movie_photo(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_title)
    await m.answer("✅ এবার <b>মুভি/সিরিজের নাম</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_photo, Command("skip"))
async def skip_movie_photo(m: types.Message, state: FSMContext):
    # 🆗 পোস্টার ঐচ্ছিক: /skip দিলে ছবি ছাড়াই পরের ধাপে যাওয়া যায়
    # (পোস্টার না থাকলে লগ চ্যানেলে/ব্রডকাস্টে ছবির বদলে টেক্সট পোস্ট যাবে)
    await state.update_data(photo_id=None)
    await state.set_state(AdminStates.waiting_for_title)
    await m.answer("✅ পোস্টার ছাড়াই এগোনো হচ্ছে। এবার <b>মুভি/সিরিজের নাম</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_photo)
async def fallback_photo(m: types.Message):
    await m.answer("⚠️ পোস্টার হিসেবে শুধুমাত্র <b>ছবি (Photo)</b> পাঠান, অথবা <b>/skip</b> লিখে পোস্টার ছাড়া এগিয়ে যান। বাতিল করতে /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer("✅ এবার <b>এপিসোড বা কোয়ালিটি</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title)
async def fallback_title(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>মুভির নাম (টেক্সট)</b> লিখুন। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=m.text.strip())
    await state.set_state(AdminStates.waiting_for_year)
    await m.answer("✅ এবার <b>রিলিজ সাল</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality)
async def fallback_quality(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>কোয়ালিটি (টেক্সট)</b> লিখুন। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_year, F.text)
async def receive_movie_year(m: types.Message, state: FSMContext):
    await state.update_data(year=m.text.strip())
    await state.set_state(AdminStates.waiting_for_cats)
    
    builder = InlineKeyboardBuilder()
    for index, cat in enumerate(CATEGORIES): 
        builder.button(text=cat, callback_data=f"selcat_{index}")
    builder.button(text="✅ Done", callback_data="cats_done")
    builder.adjust(2) 
    await m.answer("✅ এবার <b>ক্যাটাগরি সিলেক্ট</b> করুন।", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.message(AdminStates.waiting_for_year)
async def fallback_year(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>রিলিজ সাল (টেক্সট)</b> লিখুন। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.callback_query(AdminStates.waiting_for_cats, F.data.startswith("selcat_"))
async def process_category_selection(c: types.CallbackQuery, state: FSMContext):
    index = int(c.data.split("_")[1])
    cat = CATEGORIES[index]
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    if cat in selected_cats: selected_cats.remove(cat)
    else: selected_cats.append(cat)
    await state.update_data(categories=selected_cats)
    
    builder = InlineKeyboardBuilder()
    for i, ct in enumerate(CATEGORIES):
        prefix = "✅ " if ct in selected_cats else ""
        builder.button(text=f"{prefix}{ct}", callback_data=f"selcat_{i}")
    builder.button(text="✅ Done", callback_data="cats_done")
    builder.adjust(3) # এখানে 2 থেকে 3 করা হয়েছে
    await c.message.edit_reply_markup(reply_markup=builder.as_markup())
    await c.answer()

@dp.callback_query(AdminStates.waiting_for_cats, F.data == "cats_done")
async def finish_category_selection(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    if not selected_cats: return await c.answer("⚠️ অন্তত ১টি সিলেক্ট করুন!", show_alert=True)
    
    # state.clear() এখানে দেওয়া হলো না, কারণ নিচের বাটনে ক্লিক করার পর ডেটা লাগবে
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Log Only (No Broadcast)", callback_data="action_log_only")
    builder.button(text="🚀 New Movie (Broadcast & Log)", callback_data="action_new_bcast")
    builder.button(text="➕ Add File Only (No Broadcast)", callback_data="action_add_file")
    builder.adjust(1)
    await c.message.edit_text(
        "✅ সব তথ্য নেওয়া হয়েছে!\n\n👇 এখন আপনি কি করতে চান তা নিচের যেকোনো একটি বাটনে ক্লিক করে নির্বাচন করুন:\n\n"
        "📝 <b>Log Only</b> → লগ চ্যানেলে পোস্ট হবে, ব্রডকাস্ট হবে না\n"
        "🚀 <b>Broadcast & Log</b> → লগ + সব ইউজারকে নোটিফিকেশন\n"
        "➕ <b>Add File Only</b> → শুধু ওয়েব অ্যাপে যুক্ত হবে, লগ/ব্রডকাস্ট কিছুই হবে না",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await c.answer()


# 📝 লগ চ্যানেলের জন্য পোস্ট বানানোর হেল্পার (Log Only এবং Broadcast & Log দুটোতেই ব্যবহার হয়)
# ⚠️ web_app ইনলাইন বাটন শুধুমাত্র প্রাইভেট চ্যাটে চলে — চ্যানেলে দিলে Telegram পুরো পোস্টই
# রিজেক্ট করে (আগে এই কারণে "পোস্ট হয়েছে" দেখালেও চ্যানেলে কিছুই যেত না)।
# তাই চ্যানেলের Watch Now = t.me/<bot>?startapp=<movie_id> ডিপ-লিংক → মিনি অ্যাপ সরাসরি
# ওই মুভি নিয়ে খুলবে ("ফাইলটি প্রস্তুত" → অ্যাড → ডাউনলোড ফ্লো)। ফেরত দেয় (সফল, এরর)।
async def send_log_post(data, selected_cats, movie_id):
    if not LOG_CHANNEL_ID:
        return False, "লগ চ্যানেল সেট নেই — লগ চ্যানেল থেকে একটা টেক্সট মেসেজ ফরোয়ার্ড করে /setlogchannel দিন"
    try:
        if movie_id:
            watch_btn = [types.InlineKeyboardButton(text="🎬 Watch Now", url=f"https://t.me/{BOT_USERNAME}?startapp={movie_id}")]
        elif APP_URL:
            watch_btn = [types.InlineKeyboardButton(text="🎬 Watch Now", url=APP_URL)]
        else:
            watch_btn = [types.InlineKeyboardButton(text="🎬 Watch Now", url=f"https://t.me/{BOT_USERNAME}")]
        log_kb = [
            watch_btn,
            [types.InlineKeyboardButton(text="📥 ডাউনলোড কিভাবে করবেন", url="https://t.me/SakibMovieBox/62")],
            [types.InlineKeyboardButton(text="📝 Request Movie", url="https://t.me/requestmoviebox")]
        ]
        log_markup = types.InlineKeyboardMarkup(inline_keyboard=log_kb)
        log_text = f"🎬 <b>New Movie Uploaded</b>\n\n🏷 Title: <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n📅 Year: <b>{data.get('year', 'N/A')}</b>\n📂 Categories: {', '.join(selected_cats)}\n\n👤 Uploaded by Admin"
        # 🆗 পোস্টার ছাড়া আপলোড হলে ছবির বদলে টেক্সট পোস্ট যাবে —
        # (আগে পোস্টার না থাকলে send_photo ফেল করত আর চুপচাপ পোস্ট যেত না, অথচ বট "পোস্ট হয়েছে" বলত)
        if data.get("photo_id"):
            await bot.send_photo(LOG_CHANNEL_ID, photo=data["photo_id"], caption=log_text, parse_mode="HTML", reply_markup=log_markup)
        else:
            await bot.send_message(LOG_CHANNEL_ID, log_text, parse_mode="HTML", reply_markup=log_markup)
        return True, None
    except Exception as e:
        # 🛟 প্রাইমারি পোস্ট কোনো কারণে ব্যর্থ হলে বাটন ছাড়া প্লেইন টেক্সট পোস্ট অন্তত যেন যায়
        try:
            link_line = f"\n🔗 দেখুন: https://t.me/{BOT_USERNAME}?startapp={movie_id}\n" if movie_id else ""
            plain = f"🎬 <b>New Movie Uploaded</b>\n\n🏷 Title: <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n📅 Year: <b>{data.get('year', 'N/A')}</b>\n📂 Categories: {', '.join(selected_cats)}\n{link_line}\n👤 Uploaded by Admin"
            await bot.send_message(LOG_CHANNEL_ID, plain, parse_mode="HTML")
            return True, f"পোস্ট বাটন ছাড়া গেছে (কারণ: {e})"
        except Exception as e2:
            return False, str(e2)


# নতুন মুভি হিসেবে ব্রডকাস্ট করার ফাংশন (DB + লগ পোস্ট + সব ইউজারকে নোটিফিকেশন)
@dp.callback_query(F.data == "action_new_bcast")
async def action_new_broadcast(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    await state.clear()
    
    movie_id = (await db.movies.insert_one({"title": data["title"], "quality": data["quality"], "photo_id": data["photo_id"], "file_id": data["file_id"], "file_type": data["file_type"], "year": data.get("year", "N/A"), "categories": selected_cats, "clicks": 0, "created_at": datetime.datetime.utcnow()})).inserted_id

    # 🎯 এখন লগ পোস্টের আসল ফলাফল দেখানো হয় — ব্যর্থ হলে আর ভুয়া "পোস্ট হয়েছে" বলা হবে না
    ok, err = await send_log_post(data, selected_cats, str(movie_id))
    if ok:
        log_line = "📝 লগ চ্যানেলে পোস্ট হয়েছে।" + (f"\n⚠️ ছোট সমস্যা: {err}" if err else "")
    else:
        log_line = f"⚠️ <b>লগ পোস্ট ব্যর্থ হয়েছে!</b>\nকারণ: <code>{err}</code>\n🔧 চেক করুন: বট কি লগ চ্যানেলে অ্যাডমিন আছে? /testlog দিয়ে টেস্ট করুন।"
    await c.message.edit_text(f"🎉 <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!\n\n{log_line}\n⏳ <b>ব্রডকাস্ট কিউতে যোগ করা হয়েছে...</b>\nআপনি চাইলে আরও মুভি আপলোড করতে পারেন, বট একটি একটি করে ইউজারদের কাছে মেসেজ পাঠাবে।", parse_mode="HTML")

    await broadcast_queue.put({"data": data, "selected_cats": selected_cats, "admin_id": c.from_user.id, "movie_id": str(movie_id)})
    await c.answer("🚀 ব্রডকাস্ট শুরু হচ্ছে...")


# 📝 শুধু লগ চ্যানেলে পোস্ট হবে (DB-তে মুভি যুক্ত হবে, কিন্তু কোনো ব্রডকাস্ট হবে না)
@dp.callback_query(F.data == "action_log_only")
async def action_log_only(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    await state.clear()
    
    movie_id = (await db.movies.insert_one({"title": data["title"], "quality": data["quality"], "photo_id": data["photo_id"], "file_id": data["file_id"], "file_type": data["file_type"], "year": data.get("year", "N/A"), "categories": selected_cats, "clicks": 0, "created_at": datetime.datetime.utcnow()})).inserted_id

    # 🎯 এখন লগ পোস্টের আসল ফলাফল দেখানো হয় — ব্যর্থ হলে আর ভুয়া "পোস্ট হয়েছে" বলা হবে না
    ok, err = await send_log_post(data, selected_cats, str(movie_id))
    if ok:
        log_line = "📝 লগ চ্যানেলে পোস্ট হয়েছে।" + (f"\n⚠️ ছোট সমস্যা: {err}" if err else "")
    else:
        log_line = f"⚠️ <b>লগ পোস্ট ব্যর্থ হয়েছে!</b>\nকারণ: <code>{err}</code>\n🔧 চেক করুন: বট কি লগ চ্যানেলে অ্যাডমিন আছে? /testlog দিয়ে টেস্ট করুন।"
    await c.message.edit_text(f"✅ <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!\n\n{log_line}\n❌ কোনো ব্রডকাস্ট পাঠানো হয়নি।", parse_mode="HTML")
    await c.answer("📝 লগ পোস্ট হয়েছে!" if ok else "⚠️ লগ পোস্ট ব্যর্থ!")


# শুধু ফাইল অ্যাড করার ফাংশন (কোনো ব্রডকাস্ট বা লগ হবে না)
@dp.callback_query(F.data == "action_add_file")
async def action_add_file_only(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    await state.clear()
    
    await db.movies.insert_one({"title": data["title"], "quality": data["quality"], "photo_id": data["photo_id"], "file_id": data["file_id"], "file_type": data["file_type"], "year": data.get("year", "N/A"), "categories": selected_cats, "clicks": 0, "created_at": datetime.datetime.utcnow()})
    
    await c.message.edit_text(f"✅ <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!\n\n❌ কোনো ব্রডকাস্ট বা লগ পোস্ট করা হয়নি। ফাইলটি শুধুমাত্র ওয়েব অ্যাপে যুক্ত হয়েছে।", parse_mode="HTML")
    await c.answer("✅ ফাইল অ্যাড হয়েছে!")

async def run_movie_broadcast(data, selected_cats, admin_id, movie_id=None):
    bcast_success = 0
    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"
    # ✅ ডিপ-লিংক: ইউজার Watch Now চাপলে ওয়েব অ্যাপ খুলে সরাসরি এই মুভির ডাউনলোড পেজ দেখাবে
    # (APP_URL সেট না থাকলে WebApp বাটন কাজ করে না, তাই সাধারণ url বাটনে ফলব্যাক)
    if APP_URL and movie_id:
        watch_row = [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=f"{APP_URL}?open={movie_id}"))]
    else:
        watch_row = [types.InlineKeyboardButton(text="🎬 Watch Now", url=f"https://t.me/{BOT_USERNAME}?start=new")]
    bcast_kb = [
        watch_row, 
        [types.InlineKeyboardButton(text="📥 ডাউনলোড কিভাবে করবেন", url="https://t.me/SakibMovieBox/62")],
        [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link)],
        [types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)],
        [types.InlineKeyboardButton(text="📝 Request Movie", url="https://t.me/requestmoviebox")],
        
    ]
    bcast_markup = types.InlineKeyboardMarkup(inline_keyboard=bcast_kb)
    bcast_text = f"🆕 <b>New Movie Alert!</b>\n\n🎬 <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n📅 Year: <b>{data.get('year', 'N/A')}</b>\n\n👇 এখনই দেখুন!"
    
    now = datetime.datetime.utcnow()
    # 📢 ব্রডকাস্ট মেসেজ অটো-ডিলিট (অ্যাডমিন /setbcastdel দিয়ে কন্ট্রোল করতে পারবে, 0 = অফ)
    bd_cfg = await db.settings.find_one({"id": "bcast_del"})
    bcast_hours = bd_cfg.get("value", 72) if bd_cfg else 72
    delete_at = now + datetime.timedelta(hours=bcast_hours) if bcast_hours > 0 else None
    
    # 🆗 পোস্টার ছাড়া আপলোড হলে ব্রডকাস্ট ছবির বদলে টেক্সট মেসেজ হিসেবে যাবে
    has_poster = bool(data.get("photo_id"))

    async for u in db.users.find():
        try:
            if has_poster:
                sent_msg = await bot.send_photo(u['user_id'], photo=data["photo_id"], caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
            else:
                sent_msg = await bot.send_message(u['user_id'], bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
            if delete_at:
                await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at})
            bcast_success += 1
            await asyncio.sleep(0.05) # ১ লাখ ইউজারের জন্য স্পিড বাড়ানো হয়েছে
        except TelegramRetryAfter as e:
            # টেলিগ্রাম যদি বল কিছুক্ষন অপেক্ষা করতে, তাহলে অপেক্ষা করবে
            await asyncio.sleep(e.retry_after + 1)
            try:
                if has_poster:
                    sent_msg = await bot.send_photo(u['user_id'], photo=data["photo_id"], caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
                else:
                    sent_msg = await bot.send_message(u['user_id'], bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
                if delete_at:
                    await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at})
                bcast_success += 1
            except: pass
        except: pass
        
    try:
        del_note = f"\n⏳ নোটিফিকেশনগুলো <b>{bcast_hours} ঘণ্টা</b> পর অটো-ডিলিট হবে।" if bcast_hours > 0 else ""
        await bot.send_message(admin_id, f"✅ <b>{data['title']}</b> এর ব্রডকাস্ট শেষ!\n\nসফলভাবে পাঠানো হয়েছে: <b>{bcast_success}</b> জনকে।{del_note}", parse_mode="HTML")
    except: pass

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_bcast)
    await m.answer("📢 ব্রডকাস্ট মেসেজ পাঠান। (ভিডিও/ছবি/টেক্সট যেটা পাঠাবেন সেটাই হুবহু সবার কাছে যাবে, কোনো বাটন যুক্ত হবে না)\n\n⚠️ বাতিল করতে /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_bcast)
async def execute_broadcast(m: types.Message, state: FSMContext):
    if m.text and m.text.startswith("/"):
        await state.clear()
        await m.answer("⚠️ ব্রডকাস্ট বাতিল হয়েছে।", parse_mode="HTML")
        return
    if m.reply_to_message:
        await state.clear()
        await m.answer("⚠️ ব্রডকাস্ট বাতিল করা হয়েছে কারণ আপনি রিপ্লাই করেছেন!", parse_mode="HTML")
        return
    await state.clear()
    prog_msg = await m.answer("⏳ <b>Broadcast started in background...</b>", parse_mode="HTML")
    
    asyncio.create_task(run_manual_broadcast(m, prog_msg, m.from_user.id))

async def run_manual_broadcast(m, prog_msg, admin_id):
    total_users = await db.users.count_documents({})
    success = 0
    blocked = 0
    async for u in db.users.find():
        try: 
            await m.copy_to(chat_id=u['user_id'])
            success += 1
            await asyncio.sleep(0.05)
        except: 
            blocked += 1
            
    stats_text = f"✅ <b>Broadcast Complete!</b>\n\n👥 Total Users: <b>{total_users}</b>\n✅ Successful: <b>{success}</b>\n🚫 Blocked Users: <b>{blocked}</b>"
    try:
        await prog_msg.edit_text(stats_text, parse_mode="HTML")
    except:
        try: await bot.send_message(admin_id, stats_text, parse_mode="HTML")
        except: pass

@dp.callback_query(F.data.startswith("trx_"))
async def handle_trx_approval(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    action = c.data.split("_")[1]; pay_id = c.data.split("_")[2]
    payment = await db.payments.find_one({"_id": ObjectId(pay_id)})
    if not payment or payment["status"] != "pending": return await c.answer("⚠️ প্রসেস করা হয়েছে!", show_alert=True)
    user_id = payment["user_id"]; days = payment["days"]
    if action == "approve":
        now = datetime.datetime.utcnow(); user = await db.users.find_one({"user_id": user_id})
        current_vip = user.get("vip_until", now) if user else now
        if current_vip < now: current_vip = now
        await db.users.update_one({"user_id": user_id}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=days)}})
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "approved"}})
        await c.message.edit_text(c.message.text + "\n\n✅ <b>অ্যাপ্রুভ!</b>", parse_mode="HTML")
    else:
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "rejected"}})
        await c.message.edit_text(c.message.text + "\n\n❌ <b>রিজেক্ট!</b>", parse_mode="HTML")

# ==========================================
# 8. Web Admin Panel API & UI
# ==========================================
@app.get("/panel", response_class=HTMLResponse)
async def admin_panel_ui(auth: bool = Depends(verify_admin)):
    html_code = '''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Admin Panel - Movie Box</title>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0f172a; color: #cbd5e1; margin: 0; padding: 20px; }
            .header { text-align: center; margin-bottom: 30px; color: #fff; }
            .header h1 { margin: 0; font-size: 28px; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 40px; }
            .stat-card { background: #1e293b; padding: 20px; border-radius: 16px; border: 1px solid #334155; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
            .stat-card h3 { margin: 0 0 10px 0; font-size: 14px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }
            .stat-card .value { font-size: 32px; font-weight: 800; color: #fff; }
            .stat-card.users .value i { color: #3b82f6; } .stat-card.today-users .value i { color: #10b981; } .stat-card.clicks .value i { color: #f59e0b; } .stat-card.today-clicks .value i { color: #ef4444; }
            .stat-card.live-users { border-color: #10b981; } .stat-card.live-users .value { color: #10b981; }
            .table-container { background: #1e293b; border-radius: 16px; border: 1px solid #334155; overflow-x: auto; }
            .table-header { padding: 20px; border-bottom: 1px solid #334155; display: flex; justify-content: space-between; align-items: center; }
            .table-header h2 { margin: 0; color: #fff; font-size: 20px; }
            table { width: 100%; border-collapse: collapse; min-width: 600px; } th { text-align: left; padding: 15px; color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid #334155; } td { padding: 15px; border-bottom: 1px solid #334155; font-size: 14px; color: #e2e8f0; } tr:last-child td { border-bottom: none; } tr:hover { background: rgba(255,255,255,0.03); }
            .view-badge { background: rgba(59, 130, 246, 0.2); color: #60a5fa; padding: 4px 10px; border-radius: 12px; font-weight: 600; font-size: 12px; }
            .delete-btn { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); padding: 6px 12px; border-radius: 8px; cursor: pointer; font-weight: 600; transition: 0.2s; } .delete-btn:hover { background: #ef4444; color: white; }
            .empty-state { text-align: center; padding: 40px; color: #64748b; }
            
            /* Edit Modal Styles */
            .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 2000; align-items: center; justify-content: center; }
            .modal-content { background: #1e293b; padding: 25px; border-radius: 12px; width: 90%; max-width: 400px; color: #fff; }
            .modal-content h3 { margin-top: 0; color: #ef4444; }
            .form-group { margin-bottom: 15px; }
            .form-group label { display: block; margin-bottom: 5px; color: #94a3b8; font-size: 14px; }
            .form-group input { width: 100%; padding: 10px; border-radius: 6px; border: 1px solid #334155; background: #0f172a; color: #fff; box-sizing: border-box; }
            .modal-buttons { display: flex; gap: 10px; margin-top: 20px; }
            .btn-save { background: #22B8FF; color: white; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; font-weight: bold; flex: 1; }
            .btn-cancel { background: #334155; color: white; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; flex: 1; }
        </style>
    </head>
    <body>
        <div class="header"><h1><i class="fa-solid fa-shield-halved"></i> Admin Panel</h1><p>Movie Box Control Center</p></div>
        <div class="stats-grid">
            <div class="stat-card users"><h3>Total Users</h3><div class="value"><i class="fa-solid fa-users"></i> <span id="totalUsers">0</span></div></div>
            <div class="stat-card today-users"><h3>Today's New Users</h3><div class="value"><i class="fa-solid fa-user-plus"></i> <span id="todayUsers">0</span></div></div>
            <div class="stat-card clicks"><h3>Total Clicks</h3><div class="value"><i class="fa-solid fa-eye"></i> <span id="totalClicks">0</span></div></div>
            <div class="stat-card today-clicks"><h3>Today's Clicks</h3><div class="value"><i class="fa-solid fa-chart-line"></i> <span id="todayClicks">0</span></div></div>
            <div class="stat-card live-users"><h3>Live Active (1m)</h3><div class="value"><i class="fa-solid fa-signal"></i> <span id="activeUsers">0</span></div></div>
        </div>
        <div class="table-container"><div class="table-header">
    <h2><i class="fa-solid fa-film"></i> Uploaded Movies</h2>
    <input type="text" id="movieSearchInput" placeholder="🔍 Search movie..." style="padding: 8px 12px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #fff; outline: none; width: 150px;">
</div><table><thead><tr><th>Title</th><th>Quality</th><th>Category</th><th>Views</th><th>Action</th></tr></thead><tbody id="movieTableBody"><tr><td colspan="5" class="empty-state">Loading data...</td></tr></tbody></table></div>

        <!-- Edit Modal HTML -->
        <div id="editModal" class="modal">
            <div class="modal-content">
                <h3>✏️ Edit Movie</h3>
                <input type="hidden" id="editId">
                
                <div class="form-group">
                    <label>Title</label>
                    <input type="text" id="editTitle">
                </div>
                
                <div class="form-group">
                    <label>Poster Photo ID (File ID)</label>
                    <input type="text" id="editPhoto" placeholder="Paste new Telegram File ID here">
                </div>

                <div class="form-group">
                    <label>Quality</label>
                    <input type="text" id="editQuality">
                </div>

                <div class="form-group">
                    <label>Year</label>
                    <input type="text" id="editYear">
                </div>

                <div class="form-group">
                    <label>Categories (Comma separated)</label>
                    <input type="text" id="editCategories" placeholder="e.g. Action, Thriller">
                </div>

                <div class="modal-buttons">
                    <button class="btn-save" onclick="saveMovieEdit()">💾 Save Changes</button>
                    <button class="btn-cancel" onclick="closeEditModal()">❌ Cancel</button>
                </div>
            </div>
        </div>

        <script>
            async function fetchStats() { try { const res = await fetch('/api/admin/stats'); const data = await res.json(); document.getElementById('totalUsers').innerText = data.total_users; document.getElementById('todayUsers').innerText = data.today_users; document.getElementById('totalClicks').innerText = data.total_clicks; document.getElementById('todayClicks').innerText = data.today_clicks; document.getElementById('activeUsers').innerText = data.active_users; } catch(e) {} }
            let allMovies = [];
            async function fetchMovies() { 
                try { 
                    const res = await fetch('/api/admin/movies'); 
                    allMovies = await res.json(); 
                    renderMovies(allMovies); 
                } catch(e) {} 
            }

            function renderMovies(moviesToRender) {
                const tbody = document.getElementById('movieTableBody'); 
                if(moviesToRender.length === 0) { 
                    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No movies found.</td></tr>'; 
                    return; 
                } 
                tbody.innerHTML = moviesToRender.map(m => `
                    <tr id="row-${m._id}">
                        <td><strong>${m.title}</strong><br><small>ID: ${m._id}</small></td>
                        <td>${m.quality || 'N/A'}</td>
                        <td>${(m.categories || []).join(', ')}</td>
                        <td><span class="view-badge"><i class="fa-solid fa-eye"></i> ${m.clicks || 0}</span></td>
                        <td>
                            <button class="delete-btn" onclick="deleteMovie('${m._id}')" style="margin-right:5px;"><i class="fa-solid fa-trash"></i> Delete</button>
                            <button class="btn-save" onclick="openEditModal('${m._id}')" style="padding: 6px 12px; font-size: 12px; background: #22B8FF; border:none; color:white; border-radius:4px; cursor:pointer;">✏️ Edit</button>
                        </td>
                    </tr>`).join(''); 
            }

            document.getElementById('movieSearchInput').addEventListener('input', function(e) {
                const searchTerm = e.target.value.toLowerCase();
                const filteredMovies = allMovies.filter(m => 
                    (m.title || '').toLowerCase().includes(searchTerm) || 
                    (m.quality || '').toLowerCase().includes(searchTerm) ||
                    (m.categories || []).join(' ').toLowerCase().includes(searchTerm)
                );
                renderMovies(filteredMovies);
            });
            
            async function deleteMovie(id) { if(!confirm("Delete this file?")) return; try { const res = await fetch(`/api/admin/movie/${id}`, { method: 'DELETE' }); const data = await res.json(); if(data.ok) { document.getElementById(`row-${id}`).remove(); fetchStats(); } } catch(e) {} }
            
            // Edit Modal Functions
            function openEditModal(id) {
                const movie = allMovies.find(m => m._id === id);
                if (!movie) return;

                document.getElementById('editId').value = movie._id;
                document.getElementById('editTitle').value = movie.title || '';
                document.getElementById('editPhoto').value = movie.photo_id || '';
                document.getElementById('editQuality').value = movie.quality || '';
                document.getElementById('editYear').value = movie.year || '';
                document.getElementById('editCategories').value = (movie.categories || []).join(', ');

                document.getElementById('editModal').style.display = 'flex';
            }

            function closeEditModal() {
                document.getElementById('editModal').style.display = 'none';
            }

            async function saveMovieEdit() {
                const id = document.getElementById('editId').value;
                const data = {
                    title: document.getElementById('editTitle').value,
                    photo_id: document.getElementById('editPhoto').value,
                    quality: document.getElementById('editQuality').value,
                    year: document.getElementById('editYear').value,
                    categories: document.getElementById('editCategories').value.split(',').map(s => s.trim()).filter(s => s !== '')
                };

                try {
                    const res = await fetch(`/api/admin/movie/${id}`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(data)
                    });
                    
                    const result = await res.json();
                    if (result.ok) {
                        alert('✅ Movie Updated Successfully!');
                        closeEditModal();
                        fetchMovies(); // টেবিল রিফ্রেশ করার জন্য
                    } else {
                        alert('❌ Error updating movie: ' + (result.detail || 'Unknown error'));
                    }
                } catch (error) {
                    console.error(error);
                    alert('❌ An error occurred');
                }
            }

            fetchStats(); fetchMovies(); setInterval(fetchStats, 60000);
        </script>
    </body></html>'''
    return HTMLResponse(html_code)

@app.get("/api/admin/stats")
async def admin_stats(auth: bool = Depends(verify_admin)):
    now = datetime.datetime.utcnow(); today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total_users = await db.users.count_documents({}); today_users = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    one_min_ago = now - datetime.timedelta(minutes=1); active_users = await db.users.count_documents({"last_active": {"$gte": one_min_ago}})
    total_clicks_res = await db.movies.aggregate([{"$group": {"_id": None, "total": {"$sum": "$clicks"}}}]).to_list(1); total_clicks = total_clicks_res[0]["total"] if total_clicks_res else 0
    today_clicks = await db.user_unlocks.count_documents({"unlocked_at": {"$gte": today_start}})
    return {"total_users": total_users, "today_users": today_users, "active_users": active_users, "total_clicks": total_clicks, "today_clicks": today_clicks}

@app.get("/api/movies/trending")
async def get_trending_movies():
    try:
        now = datetime.datetime.utcnow()
        # গত ৩০ দিনের মধ্যে যেগুলো বেশি দেখা হয়েছে (Top 10)
        thirty_days_ago = now - datetime.timedelta(days=30)
        movies = await db.movies.find({"created_at": {"$gte": thirty_days_ago}}).sort("clicks", -1).limit(10).to_list(10)
        
        for m in movies:
            m["_id"] = str(m["_id"])
        return movies
    except Exception as e:
        return []

@app.get("/api/movies/recent")
async def get_recent_movies():
    try:
        # সবশেষে আপলোড করা ১০টি মুভি (created_at অনুযায়ী সর্ট করা)
        movies = await db.movies.find({}).sort("created_at", -1).limit(10).to_list(10)
        for m in movies:
            m["_id"] = str(m["_id"])
        return movies
    except Exception as e:
        return []

@app.get("/api/admin/movies")
async def admin_movies(auth: bool = Depends(verify_admin)):
    movies = await db.movies.find({}).sort("created_at", -1).to_list(1000)
    for m in movies: m["_id"] = str(m["_id"])
    return movies

@app.delete("/api/admin/movie/{movie_id}")
async def delete_movie(movie_id: str, auth: bool = Depends(verify_admin)):
    result = await db.movies.delete_one({"_id": ObjectId(movie_id)})
    if result.deleted_count == 1: return {"ok": True}
    raise HTTPException(status_code=404, detail="Movie not found")

@app.post("/api/user/ping")
async def user_ping(request: Request):
    try:
        body = await request.json()
        user_id = body.get("user_id")
        if user_id:
            await db.users.update_one({"user_id": user_id}, {"$set": {"last_active": datetime.datetime.utcnow()}})
        return {"ok": True}
    except:
        return {"ok": False}

@app.put("/api/admin/movie/{movie_id}")
async def update_movie(movie_id: str, movie_data: dict = Body(...), auth: bool = Depends(verify_admin)):
    # খালি ভ্যালু ফিল্টার করা
    update_data = {k: v for k, v in movie_data.items() if v is not None and v != ""}
    
    # ডাটাবেস আপডেট
    result = await db.movies.update_one({"_id": ObjectId(movie_id)}, {"$set": update_data})
    
    if result.modified_count > 0:
        return {"ok": True, "message": "Movie updated successfully"}
    raise HTTPException(status_code=400, detail="Failed to update movie")

# ==========================================
# Get Photo ID for Admin Panel — শুধু /newphoto কমান্ড দিলেই চলবে
# ==========================================
# 🛡️ আগে যেকোনো ছবি পাঠালেই সাথে সাথে File ID দিত — ওইটা আপলোডের পোস্টার-স্টেপ ঢুকে পড়ত।
# এখন শুধু /newphoto কমান্ড দিলে ছবি চাওয়া হবে, তারপর ছবি পাঠালেই আইডি দিবে — আপলোড ফ্লো আর ঝামেলা করবে না।
@dp.message(Command("newphoto"))
async def newphoto_cmd(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_photoid)
    await m.answer("📸 এবার আপনার <b>ছবিটা (Photo)</b> পাঠান — আমি এর File ID বানিয়ে দিব (Admin Panel-এর 'Poster Photo ID' বক্সে পেস্ট করার জন্য)।\n\nবাতিল করতে /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_photoid, F.photo)
async def receive_photoid(m: types.Message, state: FSMContext):
    file_id = m.photo[-1].file_id
    await state.clear()
    await m.answer(
        f"🖼️ <b>New Photo File ID:</b>\n\n<code>{file_id}</code>\n\n✅ এই আইডিটি কপি করে Admin Panel এর 'Poster Photo ID' বক্সে পেস্ট করুন।",
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_for_photoid)
async def fallback_photoid(m: types.Message):
    await m.answer("⚠️ শুধুমাত্র <b>ছবি (Photo)</b> পাঠান, অথবা বাতিল করতে /cancel লিখুন।", parse_mode="HTML")

# ==========================================
# 9. Main Web App UI
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def web_ui():
    dl_cfg = await db.settings.find_one({"id": "direct_links"}); direct_links = dl_cfg.get('links', []) if dl_cfg else []; dl_json = json.dumps(direct_links)
    adl_cfg = await db.settings.find_one({"id": "adult_direct_links"}); adult_direct_links = adl_cfg.get('links', []) if adl_cfg else []; adl_json = json.dumps(adult_direct_links)

    html_code = '''
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>Movie Box</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { background: #0f172a; font-family: 'Inter', sans-serif; color: #fff; overscroll-behavior-y: none; transition: background 0.3s; } 
            body.oled-mode { background: #000000; }
            #welcomeScreen { position: fixed; top:0; left:0; width:100%; height:100%; background: #0f172a; z-index: 99999; display: flex; flex-direction: column; align-items: center; justify-content: center; transition: opacity 0.8s ease; }
            #welcomeScreen.hide { opacity: 0; visibility: hidden; }
            .ws-brand { font-size: 48px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; animation: pulse 1.5s infinite; }
            .ws-bn { font-size: 18px; color: #94a3b8; margin-top: 10px; opacity: 0; animation: fadeUp 1s 0.5s forwards; }
            @keyframes pulse { 0% { transform: scale(1); } 50% { transform: scale(1.05); } 100% { transform: scale(1); } }
            @keyframes fadeUp { to { opacity: 1; transform: translateY(-10px); } }
            header { display: flex; justify-content: center; align-items: center; padding: 15px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); z-index: 1000; cursor: pointer; }
            body.oled-mode header { background: rgba(0, 0, 0, 0.95); border-color: #1a1a1a; }
            .logo { display: flex; align-items: center; font-size: 24px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .page-section { display: none; padding-bottom: 80px; }
            .page-section.active { display: block; }
            .cat-row { display: flex; flex-wrap: wrap; gap: 8px; padding: 15px; }
            .cat-chip { background: #1e293b; padding: 8px 16px; border-radius: 20px; white-space: nowrap; cursor: pointer; border: 1px solid #ef4444; font-weight: 600; font-size: 12px; transition: 0.3s; color: #cbd5e1; }
            .cat-chip.active { background: linear-gradient(45deg, #ef4444, #dc2626); border-color: #ef4444; color: white; box-shadow: 0 0 12px rgba(239, 68, 68, 0.4); }
            .movie-list { padding: 0 15px; display: flex; flex-direction: column; gap: 15px; }
            
            /* ✅ STATIC RAINBOW BORDER STYLE (No Animation) */
            .movie-card { 
                display: flex; 
                flex-direction: column; 
                background: #0f172a; 
                border-radius: 16px; 
                overflow: hidden; 
                cursor: pointer; 
                transition: 0.3s; 
                position: relative; 
                z-index: 1;
                border: none; 
            }
            body.oled-mode .movie-card { background: #000; }
            .movie-card:active { transform: scale(0.98); }
            
            /* Static Beautiful Gradient Border */
            .movie-card::before {
                content: "";
                position: absolute;
                inset: -3px; /* Border thickness */
                z-index: -1;
                /* Theme matching Gradient (Red to Orange to Purple) */
                background: linear-gradient(45deg, #ff416c, #ff4b2b, #ff8c00, #b91c1c);
                border-radius: 18px; /* Slightly larger than card radius */
            }

            .movie-card img { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }
            .movie-overlay { position: absolute; bottom: 0; left: 0; width: 100%; background: linear-gradient(to top, rgba(0,0,0,0.95) 0%, transparent 100%); padding: 40px 10px 10px 10px; }
            .movie-title { font-size: 14px; font-weight: 700; color: #fff; margin-bottom: 6px; white-space: normal; line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
            .movie-info { padding: 10px 12px 12px; background: rgba(30,41,59,0.55); }
            body.oled-mode .movie-info { background: rgba(10,10,10,0.65); }
            .movie-cats { display: flex; flex-wrap: wrap; gap: 5px; }
            .movie-cat-tag { background: rgba(239, 68, 68, 0.8); padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: 600; color: #fff; }
            .fav-btn { position: absolute; top: 8px; left: 8px; background: rgba(0,0,0,0.6); border: none; width: 30px; height: 30px; border-radius: 50%; color: white; font-size: 14px; cursor: pointer; display: flex; align-items: center; justify-content: center; z-index: 10; }
            .fav-btn.active { color: #ef4444; }
            .admin-view-badge {
                position: absolute;
                bottom: 8px;
                right: 8px;
                background: rgba(0,0,0,0.8);
                color: #fbbf24; /* Gold */
                padding: 2px 6px;
                border-radius: 4px;
                font-size: 10px;
                font-weight: 800;
                z-index: 10;
                border: 1px solid #fbbf24;
                display: flex;
                align-items: center;
                gap: 3px;
            }
            .adult-lock-overlay { position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); display: flex; align-items: center; justify-content: center; color: #ef4444; font-size: 40px; z-index: 5; }
            /* 🔧 ফ্লোটিং বাটন ডান দিকে, ছোট (42px) আর একটু উপরে — নিচের কনটেন্ট ঢেকে দেয় না */
            .floating-btn { position: fixed; right: 15px; width: 42px; height: 42px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 17px; z-index: 500; cursor: pointer; box-shadow: 0 4px 15px rgba(0,0,0,0.5); border: 2px solid white; text-decoration: none; color: white; }
            .btn-tg { bottom: 200px; background: linear-gradient(45deg, #24A1DE, #1b7ba8); }
            .btn-18 { bottom: 148px; background: linear-gradient(45deg, #ef4444, #b91c1c); font-weight: bold; }

            /* 💰 স্টিকি টপ অ্যাড বার (ছোট 320x50) — শুধু হোম ট্যাবে দেখাবে, header-এর ঠিক নিচে ফিক্সড */
            #topAdBar { position: fixed; left: 0; width: 100%; z-index: 900; background: rgba(15, 23, 42, 0.97); backdrop-filter: blur(8px); border-bottom: 1px solid #1e293b; padding: 5px 0; text-align: center; display: none; }
            body.oled-mode #topAdBar { background: rgba(0, 0, 0, 0.97); border-color: #1a1a1a; }
            #topAdBar.show { display: block; }
            #topAdBar iframe { display: inline-block; }

            /* 🎯 অ্যাড গেট ও সাকসেস মোডাল সব মোডালের উপরে — ডাউনলোড পপ-আপের পেছনে খুলে
               ইউজার আটকে যাওয়ার বাগ এখন নেই */
            #adModal { z-index: 4000; }
            #successModal, #newUserModal { z-index: 4100; }
            .bottom-nav { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); border-top: 1px solid #1e293b; display: flex; justify-content: space-around; padding: 10px 0; z-index: 1000; }
            body.oled-mode .bottom-nav { background: rgba(0, 0, 0, 0.95); border-color: #1a1a1a; }
            .nav-item { display: flex; flex-direction: column; align-items: center; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer; border: none; background: none; }
            .nav-item i { font-size: 20px; margin-bottom: 3px; }
            .nav-item.active { color: #ef4444; }
            .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: flex-end; justify-content: center; z-index: 3000; }
            .modal-content { background: #1e293b; width: 100%; max-width: 400px; padding: 25px; border-radius: 20px 20px 0 0; max-height: 90vh; overflow-y: auto; position: relative; }
            body.oled-mode .modal-content { background: #000000; }
            /* ✅ VIDEO DETAILS PAGE — ফুল পোস্টার + এনগেজমেন্ট (লাইক/ভোট/শেয়ার/সেভ) */
            .detail-img-full { width: 100%; max-height: 460px; object-fit: contain; background: #000; border-radius: 12px; display: block; }
            .details-topbar { display: flex; align-items: center; gap: 12px; padding: 12px 15px 4px 15px; }
            .details-topbar-title { font-size: 18px; font-weight: 800; color: #fff; }
            .back-btn { background: #1e293b; border: none; color: #fff; width: 36px; height: 36px; min-width: 36px; border-radius: 50%; font-size: 15px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
            .details-body { padding: 8px 15px 20px 15px; }
            .details-body .detail-title { font-size: 20px; font-weight: 800; margin: 12px 0 4px 0; line-height: 1.35; }
            .stats-row-wa { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 14px 0; }
            .stat-box-wa { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 10px 4px; display: flex; flex-direction: column; align-items: center; gap: 4px; font-size: 10px; font-weight: 700; color: #94a3b8; letter-spacing: 0.5px; }
            body.oled-mode .stat-box-wa { background: #0a0a0a; border-color: #1a1a1a; }
            .stat-box-wa b { color: #fff; font-size: 15px; }
            .stat-box-wa i { font-size: 16px; }
            .act-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
            .act-btn { padding: 13px; border-radius: 12px; font-weight: 800; font-size: 14px; border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 8px; font-family: 'Inter', sans-serif; }
            .act-btn:active { transform: scale(0.97); }
            .act-like { background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.5); color: #f87171; }
            .act-like.liked { background: linear-gradient(45deg,#ef4444,#b91c1c); color: #fff; border-color: #ef4444; }
            .act-dl { background: #fff; color: #000; }
            .act-save { background: rgba(251,191,36,0.12); border: 1px solid rgba(251,191,36,0.5); color: #fbbf24; }
            .act-save.saved { background: linear-gradient(45deg,#f59e0b,#b45309); color: #fff; border-color: #f59e0b; }
            .act-share { background: rgba(56,189,248,0.12); border: 1px solid rgba(56,189,248,0.5); color: #38bdf8; }
            /* 🗳️ রেটিং পোল — রেফারেন্স ডিজাইন হুবহু: প্রোগ্রেস হেডার + অপশন কার্ড (Must Watch/Good/Average/Worst) */
            .poll-wrap { margin: 16px 0 30px; }
            .poll-header { background: #232b3b; border: 1px solid #2f3a4f; border-radius: 16px; padding: 14px 16px; margin-bottom: 12px; }
            body.oled-mode .poll-header { background: #0a0a0a; border-color: #1a1a1a; }
            .poll-track { height: 10px; background: #3a4356; border-radius: 999px; overflow: hidden; }
            .poll-bar { height: 100%; width: 0%; background: #22d3ee; border-radius: 999px; transition: width .8s cubic-bezier(.22,1,.36,1); box-shadow: 0 0 10px rgba(34,211,238,.55); }
            .poll-headline { margin-top: 10px; font-size: 15px; color: #e2e8f0; }
            .poll-headline b { font-size: 18px; color: #fff; margin-right: 4px; }
            .poll-votes { margin-top: 4px; font-size: 13px; color: #94a3b8; }
            .poll-opt { display: flex; align-items: center; gap: 12px; padding: 13px 14px; border-radius: 16px; border: 1.5px solid transparent; margin-bottom: 10px; cursor: pointer; opacity: 0; transform: translateY(14px); animation: pollIn .45s cubic-bezier(.22,1,.36,1) forwards; transition: border-color .25s ease, box-shadow .25s ease; }
            .poll-opt:active { transform: scale(.985) !important; }
            @keyframes pollIn { to { opacity: 1; transform: translateY(0); } }
            .poll-opt.must { background: rgba(34,197,94,.14); border-color: rgba(34,197,94,.28); }
            .poll-opt.good { background: rgba(56,189,248,.13); border-color: rgba(56,189,248,.26); }
            .poll-opt.avg { background: rgba(245,158,11,.13); border-color: rgba(245,158,11,.26); }
            .poll-opt.worst { background: rgba(239,68,68,.13); border-color: rgba(239,68,68,.26); }
            .poll-check { width: 26px; height: 26px; min-width: 26px; border-radius: 9px; border: 3px solid; display: flex; align-items: center; justify-content: center; font-size: 13px; color: transparent; transition: background .25s ease, color .25s ease, transform .25s cubic-bezier(.34,1.56,.64,1); }
            .poll-opt.must .poll-check { border-color: #22c55e; }
            .poll-opt.good .poll-check { border-color: #38bdf8; }
            .poll-opt.avg .poll-check { border-color: #f59e0b; }
            .poll-opt.worst .poll-check { border-color: #ef4444; }
            .poll-emoji { width: 46px; height: 46px; min-width: 46px; border-radius: 13px; display: flex; align-items: center; justify-content: center; font-size: 24px; }
            .poll-opt.must .poll-emoji { background: rgba(34,197,94,.28); }
            .poll-opt.good .poll-emoji { background: rgba(56,189,248,.26); }
            .poll-opt.avg .poll-emoji { background: rgba(245,158,11,.26); }
            .poll-opt.worst .poll-emoji { background: rgba(239,68,68,.26); }
            .poll-txt { flex: 1; min-width: 0; }
            .poll-title { font-weight: 800; font-size: 16px; color: #fff; }
            .poll-sub { font-size: 12.5px; color: #cbd5e1; margin-top: 1px; }
            .poll-count { width: 46px; height: 46px; min-width: 46px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: 800; font-size: 15px; color: #fff; }
            .poll-opt.must .poll-count { background: #16a34a; }
            .poll-opt.good .poll-count { background: #0284c7; }
            .poll-opt.avg .poll-count { background: #d97706; }
            .poll-opt.worst .poll-count { background: #dc2626; }
            /* ✅ ভোট দেওয়া অবস্থা — চেক ভরা + কার্ড গ্লো (ভোট উঠানো যায় না, শুধু বদলানো যায়) */
            .poll-opt.voted { box-shadow: 0 6px 18px rgba(0,0,0,.3); }
            .poll-opt.must.voted { border-color: #22c55e; }
            .poll-opt.good.voted { border-color: #38bdf8; }
            .poll-opt.avg.voted { border-color: #f59e0b; }
            .poll-opt.worst.voted { border-color: #ef4444; }
            .poll-opt.voted .poll-check { color: #fff; }
            .poll-opt.must.voted .poll-check { background: #22c55e; }
            .poll-opt.good.voted .poll-check { background: #38bdf8; }
            .poll-opt.avg.voted .poll-check { background: #f59e0b; }
            .poll-opt.worst.voted .poll-check { background: #ef4444; }
            /* 🎬 পোল অ্যানিমেশন — ইমোজি বাউন্স + ব্যাজ পপ */
            .poll-emoji.bounce { animation: emojiBounce .5s cubic-bezier(.34,1.56,.64,1); }
            @keyframes emojiBounce { 0% { transform: scale(1); } 35% { transform: scale(1.3) rotate(-6deg); } 70% { transform: scale(.95) rotate(4deg); } 100% { transform: scale(1); } }
            .poll-count.pop { animation: countPop .45s cubic-bezier(.34,1.56,.64,1); }
            @keyframes countPop { 0% { transform: scale(1); } 40% { transform: scale(1.35); } 100% { transform: scale(1); } }
            .poll-note { font-size: 11.5px; color: #64748b; text-align: center; margin-top: 4px; line-height: 1.5; }
            /* 🧩 হোম ফিড কার্ড ব্যাজ + ফিড ব্যানার */
            .part-badge { position: absolute; top: 8px; right: 8px; background: linear-gradient(45deg,#10b981,#059669); color: #fff; font-size: 10px; font-weight: 800; padding: 4px 8px; border-radius: 6px; z-index: 6; display: flex; align-items: center; gap: 4px; box-shadow: 0 2px 6px rgba(0,0,0,0.4); }
            .views-badge { position: absolute; bottom: 8px; left: 8px; background: rgba(0,0,0,0.75); color: #fff; font-size: 10px; font-weight: 700; padding: 3px 8px; border-radius: 6px; z-index: 6; display: flex; align-items: center; gap: 4px; }
            .feed-ad-wrap { display: flex; justify-content: center; margin: 2px 0; }

            /* ✨ ইউনিক এনগেজমেন্ট অ্যানিমেশন (লাইক বার্স্ট + কাউন্ট বাম্প + পোল এন্ট্রি) */
            .burst-heart { position: fixed; pointer-events: none; z-index: 9999; animation: heartFly .85s ease-out forwards; }
            @keyframes heartFly {
                0% { transform: translate(0,0) scale(.5); opacity: 1; }
                70% { opacity: .9; }
                100% { transform: translate(var(--tx), var(--ty)) scale(1.35); opacity: 0; }
            }
            .like-pop { animation: likePop .35s cubic-bezier(.2,2.2,.4,1); }
            @keyframes likePop { 0% { transform: scale(1); } 45% { transform: scale(1.18); } 100% { transform: scale(1); } }
            .act-like.liked { animation: likeGlow .6s ease; }
            @keyframes likeGlow { 0% { box-shadow: 0 0 0 0 rgba(239,68,68,.55); } 100% { box-shadow: 0 0 0 14px rgba(239,68,68,0); } }
            .stat-box-wa b.bump { animation: countBump .4s ease; display: inline-block; }
            @keyframes countBump { 0% { transform: scale(1); } 40% { transform: scale(1.45); color: #fbbf24; } 100% { transform: scale(1); } }
            .page-section .details-body > * { animation: fadeUpIn .35s ease both; }
            @keyframes fadeUpIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

            /* ⬇ ডাউনলোড হিন্ট + অ্যানিমেটেড তীর — ইউজার সহজে বুঝবে কোথায় ডাউনলোড হবে */
            .dl-hint { background: linear-gradient(45deg, rgba(56,189,248,.14), rgba(59,130,246,.08)); border: 1px dashed #38bdf8; color: #7dd3fc; font-size: 12.5px; font-weight: 600; padding: 10px 14px; border-radius: 12px; margin: 10px 0 4px 0; display: flex; align-items: center; gap: 8px; }
            .dl-hint b { color: #fff; }
            .dl-hand { animation: handPoint .9s ease-in-out infinite; margin-right: 2px; font-size: 17px; }
            @keyframes handPoint { 0%, 100% { transform: translateX(0); } 50% { transform: translateX(5px); } }
            .act-dl { animation: dlPulse 2.2s ease-in-out infinite; }
            @keyframes dlPulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(56,189,248,.4); } 50% { box-shadow: 0 0 0 9px rgba(56,189,248,0); } }
            .detail-title { font-size: 22px; font-weight: 800; margin-bottom: 5px; }
            .detail-meta { color: #94a3b8; font-size: 14px; margin-bottom: 15px; }
            .close-icon { position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: rgba(0,0,0,0.6); color: #fff; font-size: 18px; display: flex; align-items: center; justify-content: center; cursor: pointer; border: none; }
            /* 🎯 কোয়ালিটি বাটন — বড়, পপ-আপ স্টাইল, পপ-আপ খুললে সুন্দর এন্ট্রি অ্যানিমেশন */
            .dl-file-btn { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 16px 18px; font-size: 15px; background: linear-gradient(135deg, #1e293b, #0f172a); border: 1px solid #3b82f6; color: white; font-weight: 800; border-radius: 14px; margin-bottom: 12px; cursor: pointer; box-shadow: 0 6px 18px rgba(0,0,0,.45); animation: filePop .3s ease both; }
            body.oled-mode .dl-file-btn { background: linear-gradient(135deg, #161616, #050505); border-color: #3b82f6; }
            .dl-file-btn:active { transform: scale(.97); }
            .dl-file-btn i { color: #60a5fa; font-size: 20px; }
            .dl-file-btn .dl-chev { font-size: 12px; color: #64748b; }
            .dl-file-btn.unlocked { border-color: #10b981; background: linear-gradient(135deg, rgba(16,185,129,.16), #0f172a); }
            .dl-file-btn.unlocked i { color: #10b981; }
            @keyframes filePop { from { opacity: 0; transform: translateY(10px) scale(.95); } to { opacity: 1; transform: none; } }

            /* 🤖 সাকসেস মোডালের "বটে গিয়ে ফাইল নিন" বাটন — লেখা মাঝখানে, সবুজ গ্লো পালস */
            .bot-go-btn { display: flex; align-items: center; justify-content: center; gap: 8px; width: 100%; padding: 16px 18px; font-size: 15.5px; font-weight: 800; color: white; background: linear-gradient(135deg, #10b981, #059669); border: none; border-radius: 14px; cursor: pointer; margin-top: 14px; animation: filePop .35s ease both, greenPulse 2s ease-in-out .5s infinite; box-shadow: 0 6px 18px rgba(16,185,129,.3); }
            .bot-go-btn i { font-size: 20px; animation: handPoint 1s ease-in-out infinite; }
            @keyframes greenPulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(16,185,129,.45); } 50% { box-shadow: 0 0 0 10px rgba(16,185,129,0); } }

            /* 🔙 অ্যাড পেজে ব্যাক বাটন — ভুল কোয়ালিটিতে ক্লিক করলে ফিরে গিয়ে ঠিকটা বেছে নেওয়া যায় */
            .ad-back-btn { width: 100%; margin-top: 10px; padding: 10px; font-size: 13px; font-weight: 700; color: #94a3b8; background: transparent; border: 1px dashed #475569; border-radius: 10px; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px; }
            .ad-back-btn:active { transform: scale(.97); color: #fff; border-color: #94a3b8; }
            .age-box { text-align: center; }
            .age-btn { width: 100%; padding: 15px; border-radius: 12px; font-weight: 700; border: none; font-size: 16px; cursor: pointer; margin-top: 15px; }
            .age-yes { background: #ef4444; color: white; }
            .age-no { background: #334155; color: white; }
            /* 🎬 অ্যাড মোডাল — নতুন ডিজাইন: পপ-ইন, গ্লো বার, পালসিং আইকন, শাইন, ভাইব্রেট তীর */
            .ad-box { text-align: center; padding: 24px 18px; position: relative; overflow: hidden; background: linear-gradient(165deg, #16213b 0%, #101a2e 55%, #0c1526 100%); border: 1px solid rgba(251,191,36,.22); border-radius: 18px; box-shadow: 0 18px 50px rgba(0,0,0,.55), 0 0 34px rgba(251,146,60,.14); animation: adPopIn .45s cubic-bezier(.34,1.4,.64,1) both; }
            @keyframes adPopIn { from { opacity: 0; transform: translateY(-22px) scale(.93); } to { opacity: 1; transform: none; } }
            .ad-glowbar { position: absolute; top: 0; left: 0; right: 0; height: 4px; background: linear-gradient(90deg, #f59e0b, #ef4444, #8b5cf6, #f59e0b); background-size: 300% 100%; animation: adGlowSlide 3.5s linear infinite; }
            @keyframes adGlowSlide { from { background-position: 0% 0; } to { background-position: 300% 0; } }
            .ad-icon { width: 68px; height: 68px; margin: 2px auto 10px; font-size: 34px; line-height: 68px; border-radius: 50%; background: radial-gradient(circle at 32% 28%, #fde68a, #f59e0b 62%, #d97706); box-shadow: 0 6px 18px rgba(245,158,11,.35); position: relative; animation: adIconBob 2.2s ease-in-out infinite; }
            .ad-icon::after { content: ''; position: absolute; inset: -7px; border-radius: 50%; border: 2px solid rgba(251,191,36,.55); animation: adIconPulse 1.8s ease-out infinite; }
            @keyframes adIconPulse { from { transform: scale(.85); opacity: .9; } to { transform: scale(1.35); opacity: 0; } }
            @keyframes adIconBob { 0%, 100% { transform: translateY(0) rotate(-3deg); } 50% { transform: translateY(-5px) rotate(3deg); } }
            .ad-title { font-size: 23px; font-weight: 900; margin-bottom: 14px; letter-spacing: .5px; background: linear-gradient(90deg, #fbbf24, #f97316, #fbbf24); background-size: 200% auto; -webkit-background-clip: text; background-clip: text; color: transparent; animation: adTitleFlow 2.8s linear infinite; }
            @keyframes adTitleFlow { to { background-position: 200% center; } }
            .ad-box-orange { background: linear-gradient(135deg, #ea580c, #f97316); color: white; padding: 12px 14px; border-radius: 12px; margin-bottom: 10px; font-weight: 800; font-size: 14px; position: relative; overflow: hidden; box-shadow: 0 6px 16px rgba(234,88,12,.3); }
            .ad-box-orange::before { content: ''; position: absolute; top: 0; left: -80%; width: 55%; height: 100%; background: linear-gradient(100deg, transparent, rgba(255,255,255,.35), transparent); transform: skewX(-20deg); animation: adShine 2.6s ease-in-out infinite; }
            @keyframes adShine { 0% { left: -80%; } 55%, 100% { left: 130%; } }
            .ad-box-black { background: rgba(2,6,23,.75); border: 1px dashed #475569; color: #e2e8f0; padding: 12px 14px; border-radius: 12px; margin-bottom: 18px; font-size: 13.5px; line-height: 1.75; text-align: left; }
            .ad-action-btn { width: 100%; padding: 15px; border-radius: 12px; font-weight: 800; border: none; font-size: 16px; cursor: pointer; margin-bottom: 10px; transition: transform .15s ease; }
            .ad-action-btn:active { transform: scale(.96); }
            .btn-ad-open { background: linear-gradient(135deg, #f97316 0%, #ea580c 55%, #dc2626 100%); color: white; font-size: 17px; font-weight: 900; letter-spacing: .4px; position: relative; overflow: hidden; box-shadow: 0 8px 22px rgba(234,88,12,.42); animation: adBtnBreathe 2s ease-in-out infinite; }
            @keyframes adBtnBreathe { 0%, 100% { box-shadow: 0 8px 22px rgba(234,88,12,.42); } 50% { box-shadow: 0 8px 30px rgba(234,88,12,.62); } }
            .btn-ad-open::before { content: ''; position: absolute; top: 0; left: -80%; width: 50%; height: 100%; background: linear-gradient(100deg, transparent, rgba(255,255,255,.38), transparent); transform: skewX(-20deg); animation: adShine 2.2s ease-in-out infinite; }
            /* 👉 তীর আইকন — লেবেলের পাশে সবসময় থাকবে (innerText বদলালেও হারাবে না), ভাইব্রেট করবে */
            .btn-ad-open::after { content: '↗'; display: inline-block; margin-left: 10px; font-size: 19px; animation: adArrowVibe 1.1s ease-in-out infinite; }
            @keyframes adArrowVibe { 0%, 100% { transform: translate(0,0) rotate(0deg); } 20% { transform: translate(2px,-2px) rotate(9deg); } 40% { transform: translate(-2px,2px) rotate(-9deg); } 60% { transform: translate(2px,1px) rotate(7deg); } 80% { transform: translate(-2px,-1px) rotate(-6deg); } }
            .btn-ad-unlock { background: linear-gradient(135deg, #10b981, #059669); color: white; box-shadow: 0 6px 18px rgba(16,185,129,.35); animation: greenPulse 1.8s infinite; }
            .btn-ad-tryagain { background: linear-gradient(135deg, #ef4444, #dc2626); color: white; }
            .search-box { padding: 0 15px 15px; }
            .search-input { width: 100%; padding: 14px; border-radius: 12px; border: none; outline: none; background: #1e293b; color: #fff; font-size: 15px; border: 1px solid #334155; }
            body.oled-mode .search-input { background: #0a0a0a; border-color: #1a1a1a; }
            .profile-card { background: #1e293b; margin: 15px; border-radius: 16px; padding: 20px; border: 1px solid #334155; }
            body.oled-mode .profile-card { background: #0a0a0a; border-color: #1a1a1a; }
            .profile-action-btn { display: block; width: 100%; padding: 14px; border-radius: 12px; font-weight: 700; text-align: center; margin-bottom: 10px; border: none; color: white; text-decoration: none; cursor: pointer; font-size: 15px; transition: 0.2s; }
            .profile-action-btn:active { transform: scale(0.97); }
            .btn-dark-mode { background: #334155; display: flex; align-items: center; justify-content: center; gap: 10px; }
            .btn-fb { background: #1877F2; }
            .btn-main-ch { background: #24A1DE; }
            .btn-18-ch { background: #ef4444; }
            .btn-sax-grp { background: #8B5CF6; }
            .skeleton { background: #1e293b; border-radius: 12px; height: 160px; position: relative; overflow: hidden; }
            .skeleton::after { content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.05), transparent); animation: shimmer 1.5s infinite; }
            @keyframes shimmer { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }
            .join-channel-btn { display: block; width: 100%; padding: 15px; border-radius: 12px; background: #24A1DE; color: white; font-weight: 700; text-decoration: none; font-size: 16px; text-align: center; margin-top: 15px; margin-bottom: 10px; box-shadow: 0 4px 10px rgba(36, 161, 222, 0.3); }
            
            /* ✅ Pagination Styles */
            .pagination-container { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 20px 15px 80px 15px; }
            .page-btn { background: #1e293b; color: #cbd5e1; border: 1px solid #334155; padding: 10px 15px; border-radius: 10px; font-weight: 700; cursor: pointer; transition: 0.2s; font-size: 14px; }
            body.oled-mode .page-btn { background: #0a0a0a; border-color: #1a1a1a; }
            .page-btn:hover { background: #334155; color: white; }
            .page-btn.active { background: linear-gradient(45deg, #ef4444, #dc2626); color: white; border-color: #ef4444; box-shadow: 0 0 8px rgba(239, 68, 68, 0.3); }
            .page-btn:disabled { background: #1e293b; color: #475569; cursor: not-allowed; border-color: #1e293b; }

           /* নতুন মেটা রো (Year এবং Category এর জন্য) */
           .movie-meta-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 5px;
           }
         .year-badge {
          background-color: #22B8FF;
          color: #fff;
          padding: 3px 8px;
          border-radius: 4px;
          font-size: 11px;
          font-weight: 700;
         }
         .cat-small-tag {
         background: rgba(255, 255, 255, 0.1);
         color: #cbd5e1;
         padding: 2px 6px;
         border-radius: 4px;
         font-size: 10px;
         margin-left: 4px;
         display: inline-block;
         }

         /* ========================= */
         /* ✅ TRENDING SLIDER STYLES */
         /* ========================= */
         .trending-section-wrapper {
            background: rgba(30, 41, 59, 0.4);
            margin: 10px 15px 20px 15px;
            padding: 15px;
            border-radius: 16px;
            border: 1px solid #334155;
         }
         body.oled-mode .trending-section-wrapper { background: #0a0a0a; border-color: #1a1a1a; }

         .section-header { 
            padding-bottom: 10px; 
            font-size: 18px; 
            font-weight: 800; 
            color: #fff; 
            display: flex; 
            align-items: center; 
            gap: 8px; 
            margin-bottom: 10px;
         }
         .section-header i { color: #ff4b2b; }

         .trending-slider-container {
            overflow-x: auto;
            display: flex;
            gap: 15px;
            scroll-behavior: smooth;
            padding-bottom: 5px;
            -ms-overflow-style: none;  
            scrollbar-width: none;  
         }
         .trending-slider-container::-webkit-scrollbar { display: none; }

         .trending-card { 
            flex: 0 0 auto;
            width: 240px; /* Wide Thumbnail Size */
            background: #0f172a;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 4px 10px rgba(0,0,0,0.5);
            border: 2px solid #334155;
            cursor: pointer;
            position: relative;
            transition: transform 0.2s;
         }
         body.oled-mode .trending-card { background: #000; border-color: #1a1a1a; }
         .trending-card:active { transform: scale(0.95); }

         .poster-slider { 
            width: 100%; 
            aspect-ratio: 16/9; 
            position: relative; 
         }
         .poster-slider img { 
            width: 100%; 
            height: 100%; 
            object-fit: cover; 
            display: block; 
         }

         .badge-18-slider { 
            position: absolute; 
            top: 5px; 
            left: 5px; 
            background: #ef4444; 
            color: white; 
            font-size: 9px; 
            font-weight: 800; 
            padding: 2px 6px; 
            border-radius: 4px; 
            z-index: 2; 
            box-shadow: 0 2px 4px rgba(0,0,0,0.5);
         }

         .rank-badge {
            position: absolute;
            top: 5px;
            right: 5px;
            background: rgba(0, 0, 0, 0.7);
            color: #fbbf24;
            width: 24px;
            height: 24px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 12px;
            font-weight: 800;
            z-index: 2;
            border: 1px solid #fbbf24;
         }

         .info-slider { 
            padding: 8px; 
         }
         .movie-title-slider { 
            font-size: 12px; 
            color: #fff; 
            font-weight: 600; 
            white-space: nowrap; 
            overflow: hidden; 
            text-overflow: ellipsis; 
            margin-bottom: 2px;
         }
         .movie-meta-slider { 
            font-size: 10px; 
            color: #94a3b8; 
            display: flex; 
            justify-content: space-between;
         }

         /* 🔥 স্পন্সর অ্যাড কার্ড — ডাউনলোড বাটনের মত সাইজ/স্টাইল (হোম ফিড + ডাউনলোড পপ-আপ) */
         .sponsor-card {
            width: 100%;
            display: flex;
            align-items: center;
            gap: 12px;
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 14px 15px;
            cursor: pointer;
            margin: 4px 0;
            transition: transform 0.15s, border-color 0.2s;
         }
         .sponsor-card:active { transform: scale(0.98); border-color: #fbbf24; }
         body.oled-mode .sponsor-card { background: #050505; border-color: #1a1a1a; }
         .sponsor-emoji { font-size: 24px; line-height: 1; }
         .sponsor-txt { flex: 1; text-align: left; min-width: 0; }
         .sponsor-title { color: #fbbf24; font-weight: 800; font-size: 14px; }
         .sponsor-sub { color: #94a3b8; font-size: 11px; margin-top: 3px; }
         .sponsor-tag {
            background: linear-gradient(135deg, #ff9966, #ff4b2b);
            color: #fff; font-weight: 700; font-size: 10px;
            padding: 4px 8px; border-radius: 6px; white-space: nowrap;
         }

        </style>
    </head>
    <body>
        <div id="welcomeScreen"><div class="ws-brand">Movie Box</div><div class="ws-bn">মুভি বক্স জগতে স্বাগতম</div></div>
        <header onclick="switchTab('home')"><div class="logo">Movie Box</div></header>

        <!-- 💰 স্টিকি টপ ব্যানার (ছোট 320x50): হোমে স্ক্রল করলেও সবসময় উপরে থাকবে — নরমাল ক্যাটাগরিতে নরমাল অ্যাড, অ্যাডাল্ট ক্যাটাগরিতে অ্যাডাল্ট অ্যাড -->
        <div id="topAdBar">
            <!-- 🧠 CPM: অ্যাডাল্ট ক্যাটাগরির ব্যানার খালি — ক্যাটাগরি সুইচে JS দিয়ে মাউন্ট হয় (loadHomeMovies)।
                 পেজ-লোডে লুকানো অবস্থায় লোড হলে ইউজার কিছু না দেখেই মৃত ইম্প্রেশন জমত। -->
            <div id="homeBannerNormal">__BANNER_320__</div>
            <div id="homeBannerAdult" style="display:none;"></div>
        </div>

        <div id="tabHome" class="page-section active">
            <div class="search-box"><input type="text" id="searchInput" class="search-input" placeholder="🔍 খুঁজুন..."></div>
            <div class="cat-row">
                <div class="cat-chip active" onclick="filterCat('Home', this)">HOME</div>
                <div class="cat-chip" onclick="filterCat('Bangla', this)">BANGLA</div>
                <div class="cat-chip" onclick="filterCat('Bangla Dubbed', this)">BANGLA DUBBED</div>
                <div class="cat-chip" onclick="filterCat('Hindi Dubbed', this)">HINDI DUBBED</div>
                <div class="cat-chip" onclick="filterCat('Hollywood', this)">HOLLYWOOD</div>
                <div class="cat-chip" onclick="filterCat('Web Series', this)">WEB SERIES</div>
                <div class="cat-chip" onclick="filterCat('K-Drama', this)">K-DRAMA</div>
                <div class="cat-chip" onclick="filterCat('Anime', this)">ANIME</div>
                <div class="cat-chip" onclick="filterCat('Horror', this)">HORROR</div>
                <div class="cat-chip" onclick="verify18(this)">ADULT CONTENT</div>
            </div>
            
            <!-- ✅ TRENDING NOW SLIDER -->
            <div class="trending-section-wrapper">
                <div class="section-header"><i class="fa-solid fa-fire"></i> Trending Now</div>
                <div class="trending-slider-container" id="trendingSlider">
                    <div style="width: 100%; text-align: center; color: #64748b; padding: 20px;">Loading...</div>
                </div>
            </div>

            <!-- ✅ RECENTLY ADDED HEADER (💰 ট্রেন্ডিং-এর নিচের 300x250 ব্যানার সরানো হয়েছে — অ্যাড এখন উপরে স্টিকি টপ বারে) -->
            <div style="padding: 0 15px 10px 15px;">
                <div class="section-header" style="margin-bottom: 5px; margin-left: 0px;">
                    <i class="fa-solid fa-clock"></i> Recently Added
                </div>
            </div>

            <div class="movie-list" id="movieListHome"><div class="skeleton"></div><div class="skeleton"></div></div>
            <div id="paginationHome" class="pagination-container"></div>
        </div>

        <div id="tabSearch" class="page-section"><div class="search-box" style="padding-top:15px;"><input type="text" id="searchInputMain" class="search-input" placeholder="🔍 সার্চ..." oninput="searchMovies()"></div><div class="movie-list" id="movieListSearch"></div><div id="paginationSearch" class="pagination-container"></div></div>
        <div id="tabFav" class="page-section"><h3 style="padding: 15px; color: #fbbf24;">❤️ ফেভারিট</h3><div class="movie-list" id="movieListFav"></div><h3 style="padding: 15px; color: #38bdf8;">💾 সেভ লেটার</h3><div class="movie-list" id="movieListSaved"></div></div>
        
        <div id="tabSurprise" class="page-section">
            <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 60vh; text-align: center; padding: 20px;">
                <div style="font-size: 80px; margin-bottom: 20px; animation: pulse 1.5s infinite;">🎲</div>
                <h2 style="margin-bottom: 15px; color: #fbbf24;">মুভি রুলেট!</h2>
                <p style="color: #94a3b8; margin-bottom: 30px;">কী দেখবেন ঠিক করতে পারছেন না? বট আপনার জন্য একটি মুভি বেছে নিচ্ছে!</p>
                <button onclick="loadSurprise()" style="padding: 15px 40px; background: linear-gradient(45deg, #ff416c, #ff4b2b); color: white; border: none; border-radius: 30px; font-size: 18px; font-weight: 800; cursor: pointer; box-shadow: 0 0 20px rgba(255, 65, 108, 0.5);">🎲 Surprise Me!</button>
            </div>
        </div>

        <div id="tabProfile" class="page-section">
            <div class="profile-card">
                <div style="text-align: center; margin-bottom: 20px;"><h2 id="profileName">User</h2></div>
                <button class="profile-action-btn btn-dark-mode" onclick="toggleOledMode()">🌙 ডার্ক মোড (OLED) <span id="darkModeStatus">OFF</span></button>
                <a href="https://facebook.com/" class="profile-action-btn btn-fb" target="_blank">📘 Facebook Group</a>
                <a href="https://t.me/addlist/MwbWNafSFK4yZjhl" class="profile-action-btn btn-main-ch" target="_blank">🚀 Main Channel</a>
                <a href="https://t.me/+W5V9-mn08jMyYTE1" class="profile-action-btn btn-18-ch" target="_blank">🔴 18+ Channel</a>
                <a href="#" class="profile-action-btn btn-sax-grp" target="_blank">🔥 Sax Group</a>
            </div>
        </div>

        <a href="https://t.me/addlist/MwbWNafSFK4yZjhl" class="floating-btn btn-tg"><i class="fa-brands fa-telegram"></i></a>
        <a href="https://t.me/+W5V9-mn08jMyYTE1" class="floating-btn btn-18">18+</a>

        <div class="bottom-nav">
            <button class="nav-item active" onclick="switchTab('home', this)"><i class="fa-solid fa-house"></i>Home</button>
            <button class="nav-item" onclick="switchTab('search', this)"><i class="fa-solid fa-magnifying-glass"></i>Search</button>
            <button class="nav-item" onclick="switchTab('fav', this)"><i class="fa-solid fa-heart"></i>Favorites</button>
            <button class="nav-item" onclick="switchTab('surprise', this)"><i class="fa-solid fa-dice"></i>Surprise</button>
            <button class="nav-item" onclick="switchTab('profile', this)"><i class="fa-solid fa-user"></i>Profile</button>
        </div>

        <div id="ageModal" class="modal"><div class="modal-content age-box"><h2 style="color:#ef4444;">⚠️ বয়স সীমাবদ্ধতা</h2><p style="color:#cbd5e1; margin:15px 0;">আপনার বয়স কি ১৮ বছরের বেশি?</p><button class="age-btn age-yes" onclick="access18()">হ্যাঁ, আমি ১৮+</button><button class="age-btn age-no" onclick="closeModal('ageModal')">না</button></div></div>

        <!-- ✅ VIDEO DETAILS PAGE (রেফারেন্স লেআউট): ফুল পোস্টার + লাইক/ডাউনলোড/ভোট/শেয়ার স্ট্যাট + Save Later + রেটিং পোল -->
        <div id="tabDetails" class="page-section">
            <div class="details-topbar">
                <button class="back-btn" onclick="closeDetails()"><i class="fa-solid fa-arrow-left"></i></button>
                <div class="details-topbar-title">Video Details</div>
            </div>
            <div class="details-body">
                <div style="position:relative;">
                    <img id="detailImg" class="detail-img-full" src="" alt="poster">
                </div>
                <h2 id="detailTitle" class="detail-title"></h2>
                <div id="detailMeta" class="detail-meta"></div>
                <div id="detailCats" style="margin: 8px 0 4px 0;"></div>
                <!-- ⬇ ডাউনলোড হিন্ট মেসেজ — ইউজার সহজে বুঝবে ডাউনলোড কোথায় হবে -->
                <div class="dl-hint"><i class="fa-solid fa-circle-arrow-down"></i> <span>ডাউনলোড করতে নিচের <b>Download</b> বাটনে ক্লিক করুন!</span></div>

                <div class="stats-row-wa">
                    <div class="stat-box-wa"><i class="fa-solid fa-heart" style="color:#ef4444"></i><b id="statLikes">0</b><span>LIKE</span></div>
                    <div class="stat-box-wa"><i class="fa-solid fa-download" style="color:#38bdf8"></i><b id="statDownloads">0</b><span>DOWNLOAD</span></div>
                    <div class="stat-box-wa"><i class="fa-solid fa-chart-simple" style="color:#a78bfa"></i><b id="statVotes">0</b><span>VOTES</span></div>
                    <div class="stat-box-wa"><i class="fa-solid fa-share-nodes" style="color:#fbbf24"></i><b id="statShares">0</b><span>SHARE</span></div>
                </div>

                <div class="act-row">
                    <button id="likeBtn" class="act-btn act-like" onclick="toggleLike()"><i class="fa-regular fa-heart"></i> Like</button>
                    <button class="act-btn act-dl" onclick="openDownloadModal()"><i class="fa-solid fa-hand-point-right dl-hand"></i> Download</button>
                </div>
                <div class="act-row">
                    <button id="saveBtn" class="act-btn act-save" onclick="toggleSaved()"><i class="fa-regular fa-bookmark"></i> Save Later</button>
                    <button class="act-btn act-share" onclick="shareMovie()"><i class="fa-solid fa-share"></i> Share</button>
                </div>

                <!-- 💰 ব্যানার অ্যাড: সাধারণ মুভিতে 320x50 ছোট ব্যানার, ১৮+ মুভিতে অ্যাডাল্ট ব্যানার।
                     🧠 CPM: যে ব্যানারটা দেখা যাবে শুধু সেটাই JS দিয়ে মাউন্ট হয় (renderDetailsPage) —
                     লুকানো টাটা লোড করলে মৃত ইম্প্রেশন জমত। -->
                <div id="detailBannerNormal" style="display:none; text-align:center; margin: 0 auto 12px auto;"></div>
                <div id="detailBannerAdult" style="display:none; text-align:center; margin: 0 auto 12px auto;"></div>
                <!-- 🗳️ রেটিং পোল (কমেন্টের বদলে): দর্শকের ভোট — এক ইউজার এক ভোট, বদলানো যায় উঠানো যায় না -->
                <div class="poll-wrap" id="pollWrap">
                    <div class="poll-header">
                        <div class="poll-track"><div class="poll-bar" id="pollBar"></div></div>
                        <div class="poll-headline"><b id="pollPercent">0%</b> <span>দর্শক এটি দেখার পরামর্শ দিয়েছেন</span></div>
                        <div class="poll-votes" id="pollTotal">0 votes</div>
                    </div>
                    <div id="pollOptions"></div>
                    <div class="poll-note" id="pollNote">আপনার ভোট দিন — একবার ভোট দিলে তা আর উঠানো যাবে না, চাইলে অন্য অপশনে বদলাতে পারবেন।</div>
                </div>
            </div>
        </div>

        <!-- 🎯 "ফাইলটি প্রস্তুত" মেসেজ — Watch Now (ব্রডকাস্ট/লগ পোস্ট) চাপলে নতুন-পুরাতন সব ইউজারই এটা দেখবে.
             প্রথম Watch Now ক্লিকে অ্যাক্টিভ স্পন্সর লিংক খোলে (ইনকাম), ফিরে এসে আবার চাপলে ফাইলের পেজে যায়. ক্লোজ বাটন দিয়ে বের হওয়া যায়. -->
        <div id="fileGateModal" class="modal">
            <div class="modal-content" style="text-align: center;">
                <button class="close-icon" onclick="closeModal('fileGateModal')"><i class="fa-solid fa-xmark"></i></button>
                <div class="ad-icon">🎉</div>
                <h2 class="ad-title">ফাইলটি প্রস্তুত!</h2>
                <div class="ad-box-black">✨ সুখবর! আপনি এই ফাইলটি পাচ্ছেন: <b id="gateMovieTitle"></b><br><br>নিচের <b>Watch Now</b> বাটনে ক্লিক করে ফাইলটি দেখুন।</div>
                <button class="ad-action-btn btn-ad-open" id="gateWatchBtn" onclick="gateNextStep()">🎬 Watch Now</button>
                <button class="ad-action-btn" style="background:#334155; color:#fff;" onclick="closeModal('fileGateModal')">❌ বন্ধ করুন</button>
                <p style="color:#64748b; font-size:12px; margin-top:8px;">✨ প্রতিদিন নতুন মুভি আপডেট পেতে চ্যানেলে জয়েন করুন।</p>
            </div>
        </div>

        <div id="adModal" class="modal">
            <div class="modal-content ad-box">
                <div class="ad-glowbar"></div>
                <div class="ad-icon">⚠️</div>
                <h2 class="ad-title">সতর্কতা!</h2>
                <div class="ad-box-orange">ডাউনলোড করতে হলে অবশ্যই বিজ্ঞাপন দেখুন!</div>
                <div class="ad-box-black"><span id="adStepText">লিংকে ক্লিক করে বিজ্ঞাপনটি দেখুন এবং কমপক্ষে <b>১০ সেকেন্ড</b> পর ফিরে এসে নিচের বাটনে ক্লিক করুন।</span></div>
                <button class="ad-action-btn btn-ad-open" id="adClickBtn" onclick="openAdLink()">বিজ্ঞাপন খুলুন</button>
                <button class="ad-action-btn btn-ad-unlock" id="adVerifyBtn" onclick="checkAdWatched()" style="display:none;">✅ অ্যাড দেখে ফিরে এসেছি</button>
                <button class="ad-action-btn btn-ad-tryagain" id="adTryAgainBtn" onclick="resetAdModal()" style="display:none;">TRY AGAIN</button>
                <button class="ad-back-btn" onclick="closeModal('adModal'); resetAdModal(); openDownloadModal();"><i class="fa-solid fa-arrow-left"></i> ব্যাক — ডাউনলোড লিস্টে ফিরুন</button>
                <p style="color:#64748b; font-size:12px; margin-top:5px;">💎 VIP হলে কোনো অ্যাড দেখতে হবে না — Profile ট্যাব থেকে VIP নিন!</p>
                <!-- 🧠 CPM: অ্যাড-গেট মোডাল খোলার মুহূর্তে JS দিয়ে ব্যানার মাউন্ট হয় (handleFileClick)।
                     পেজ-লোডে লুকানো মোডালে ব্যানার লোড হলে ইউজার কিছু না দেখেই "মৃত" ইম্প্রেশন জমে —
                     বিজ্ঞাপনদাতা বিড কমিয়ে দেয়, CPM পড়ে যায়। -->
                <div id="adModalAdSlot" style="display:flex; justify-content:center; margin-top:5px;"></div>
            </div>
        </div>

        <!-- ⬇ DOWNLOAD POP-UP: বড় Download বাটনে ক্লিক করলে খুলবে — ভিতরে সব কোয়ালিটি লিংক + 320x50 ব্যানার + 300x250 স্কয়ার অ্যাড + স্পন্সর (১৮+ মুভিতে অ্যাডাল্ট অ্যাড) -->
        <div id="downloadModal" class="modal">
            <div class="modal-content">
                <button class="close-icon" onclick="closeModal('downloadModal')"><i class="fa-solid fa-xmark"></i></button>
                <div style="display:flex; align-items:center; gap:12px; margin-bottom:14px;">
                    <i class="fa-solid fa-circle-down" style="font-size:28px; color:#38bdf8;"></i>
                    <div>
                        <div style="font-weight:800; font-size:17px; color:#fff;">Download Links</div>
                        <div id="dlModalTitle" style="font-size:12px; color:#94a3b8;"></div>
                    </div>
                </div>
                <div id="fileButtonsContainer"></div>
                <div style="text-align:center; margin: 14px 0 10px 0;">
                    <!-- 🧠 CPM: মোডাল খোলার মুহূর্তে JS দিয়ে মাউন্ট (openDownloadModal) — লুকানো মোডালে
                         পেজ-লোডেই ব্যানার লোড হলে দেখা-না-হওয়া ইম্প্রেশন জমে CPM পড়ে যায় -->
                    <div id="dlBannerNormal"></div>
                    <div id="dlBannerAdult" style="display:none;"></div>
                </div>
                <div id="dlSquareSlot" style="display:flex; justify-content:center; margin-bottom:10px;"></div>
                <div id="dlSponsorSlot"></div>
            </div>
        </div>

        <div id="successModal" class="modal">
            <div class="modal-content" style="text-align: center; padding-top: 40px;">
                <i class="fa-solid fa-circle-check" style="font-size:70px; color:#4ade80; margin-bottom:20px;"></i>
                <h2>ফাইল পাঠানো হয়েছে! 🎉</h2>
                <div class="ad-box-orange" style="margin-top:12px;">✨ সুখবর! আপনার মুভি বটে পাঠানো হয়েছে।</div>
                <div class="ad-box-black" style="margin-top:10px;">👇 নিচের <b>"🤖 বটে গিয়ে ফাইল নিন"</b> বাটনে ক্লিক করুন — সরাসরি বট খুলে যাবে আর সাথে সাথেই আপনার মুভি পেয়ে যাবেন! বটের ওপরের মেসেজে ফাইলটা ট্যাপ করলেই দেখতে পারবেন।</div>
                <button class="bot-go-btn" onclick="openBotForFile()"><i class="fa-solid fa-robot"></i> 🤖 বটে গিয়ে ফাইল নিন</button>
                <a href="https://t.me/addlist/MwbWNafSFK4yZjhl" target="_blank" class="join-channel-btn" style="margin-top:10px;">🚀 Join Channel</a>
            </div>
        </div>

        <!-- 🎯 নতুন ইউজার মোডাল: বটকে এখনো START করেননি, তাই ফাইল পাঠানো যায়নি —
             "বটে যান" চেপে START করলেই অ্যাড দেখা ফাইলটি অটোমেটিক ডেলিভারি হয়ে যাবে -->
        <div id="newUserModal" class="modal">
            <div class="modal-content" style="text-align: center; padding-top: 40px;">
                <i class="fa-solid fa-circle-check" style="font-size:70px; color:#4ade80; margin-bottom:20px;"></i>
                <h2>🎉 সাকসেসফুল!</h2>
                <p style="color:#94a3b8; margin-top:10px;">আপনার ফাইল প্রস্তুত! নিচের <b>বটে যান</b> বাটনে ক্লিক করে বট চ্যাট খুলুন, তারপর <b>START</b> চাপুন — ফাইলটি সাথে সাথেই আপনাকে পাঠিয়ে দেওয়া হবে।</p>
                <a id="newUserBotLink" href="#" class="join-channel-btn" style="background: linear-gradient(45deg, #ff416c, #ff4b2b);" onclick="event.preventDefault(); try { tg.openTelegramLink(this.href); } catch(e) { window.open(this.href, '_blank'); }">🤖 বটে যান</a>
                <p style="color:#64748b; font-size:12px; margin-top:8px;">⚠️ অবশ্যই START চাপবেন — START চাপলেই ফাইল অটো চলে আসবে!</p>
            </div>
        </div>

        <script>
            // Live Active Ping
            try {
                const tgPingUser = window.Telegram?.WebApp?.initDataUnsafe?.user;
                if (tgPingUser && tgPingUser.id) {
                    fetch('/api/user/ping', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({user_id: tgPingUser.id})
                    });
                    setInterval(() => {
                        fetch('/api/user/ping', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({user_id: tgPingUser.id})
                        });
                    }, 30000);
                }
            } catch(e) {}

            let tg = window.Telegram.WebApp; tg.expand();
            const DIRECT_LINKS = __DL_JSON__; const ADULT_DIRECT_LINKS = __ADL_JSON__; const INIT_DATA = tg.initData || ""; 
            const TOKEN = "__BOT_TOKEN__";
            const BOT_USERNAME = "__BOT_USERNAME__";
            /* 💰 ফিডের ভেতরে iframe আকারে বসানো ব্যানার কনফিগ (innerHTML দিলে <script> চলে না, তাই srcdoc iframe ব্যবহার) */
            const AD_300_CONF = __AD300_CONF__;
            const AD_320_CONF = __AD320_CONF__;
            const AD_ADULT_CONF = __ADULT_CONF__;
            const AD_320_ADULT_CONF = __AD320_ADULT_CONF__;
            /* ✅ ডিটেইলস পেজ + এনগেজমেন্ট স্টেট */
            let activeDetailMovie = null; let pollState = {counts: {}, total: 0, percent: 0, user_vote: null};
            let detailStats = {likes:0, downloads:0, votes:0, shares:0};
            let favMovies = []; let savedMovies = [];
            let uid = tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user.id : 0; 
            let isUserVip = false; 
            let isAdmin = false; // Admin Variable
            let activeCat = "Home"; let userFavs = []; let active18Btn = null; let activeFileId = null; let activeIsAdult = false; let adStartTime = 0; let currentViewMovies = [];
            // 🔙 ডিটেইলস খোলার আগের ট্যাব + স্ক্রল পজিশন — ব্যাক করলে সেখানেই ফেরা যাবে
            let detailsReturnTab = 'home'; let detailsReturnScroll = 0;
            let adRound = 0; let adCountdownTimer = null; let adSettings = {ad_count: 1, ad_wait: 10, active_link: '', active_adult_link: ''}; let adAwayMs = 0; let adAwayStart = null; let adWasHidden = false;
            let homeCurrentPage = 1;
            let searchCurrentPage = 1;
            let trendingMovies = []; 

            setTimeout(function() { document.getElementById('welcomeScreen').classList.add('hide'); }, 2500);
            if(tg.initDataUnsafe && tg.initDataUnsafe.user) { document.getElementById('profileName').innerText = tg.initDataUnsafe.user.first_name; }
            
            async function fetchUserInfo() { 
                try { 
                    const res = await fetch('/api/user/' + uid); 
                    const data = await res.json(); 
                    isUserVip = data.vip; 
                    isAdmin = data.is_admin || false; // Set Admin Status
                } catch(e) {} 
            }
            
            // 💰 টপ অ্যাড বার: শুধু হোম ট্যাব অ্যাক্টিভ থাকলে দেখাবে; header-এর উচ্চতা মেপে তার নিচে বসে,
            // হোম কনটেন্টে padding-top দিয়ে যাতে অ্যাড বার কোনো কার্ড ঢেকে না দেয়
            function updateTopAdBar() {
                let bar = document.getElementById('topAdBar'); if (!bar) return;
                let header = document.querySelector('header');
                if (header) bar.style.top = header.offsetHeight + 'px';
                let active = document.querySelector('.page-section.active');
                let show = !!active && active.id === 'tabHome';
                bar.classList.toggle('show', show);
                let homeSec = document.getElementById('tabHome');
                if (homeSec && header) homeSec.style.paddingTop = show ? (header.offsetHeight + bar.offsetHeight + 8) + 'px' : '';
            }
            window.addEventListener('resize', updateTopAdBar);
            function switchTab(tabName, btnEl, opts) {
                opts = opts || {};
                document.querySelectorAll('.page-section').forEach(function(el) { el.classList.remove('active'); });
                document.querySelectorAll('.nav-item').forEach(function(el) { el.classList.remove('active'); });
                // 🔙 noReload: ব্যাক করে ফেরার সময় হোম ফিড আবার লোড/রিসেট হবে না — আগের অবস্থাই থাকবে
                if(tabName === 'home' && !opts.noReload) { activeCat = 'Home'; homeCurrentPage = 1; document.querySelectorAll('.cat-chip').forEach(function(el) { el.classList.remove('active'); }); var fc = document.querySelector('.cat-chip'); if(fc) fc.classList.add('active'); }
                document.getElementById('tab' + tabName.charAt(0).toUpperCase() + tabName.slice(1)).classList.add('active');
                if(btnEl) btnEl.classList.add('active');
                if(tabName === 'home' && !opts.noReload) loadHomeMovies(1);
                if(tabName === 'fav' && !opts.noReload) loadFavorites();
                updateTopAdBar();
                if (!opts.noScrollTop) window.scrollTo({top:0, behavior:'smooth'});
            }
            function filterCat(cat, btnEl) { activeCat = cat; homeCurrentPage = 1; document.querySelectorAll('.cat-chip').forEach(function(el) { el.classList.remove('active'); }); btnEl.classList.add('active'); loadHomeMovies(1); }
            
            function verify18(btnEl) { active18Btn = btnEl; if(localStorage.getItem('isAdult')) { if(btnEl) filterCat('Adult Content', btnEl); } else { document.getElementById('ageModal').style.display = 'flex'; } }
            function access18() { localStorage.setItem('isAdult', 'true'); closeModal('ageModal'); if(active18Btn) { filterCat('Adult Content', active18Btn); } else { loadHomeMovies(1); } }
            
            function closeModal(id) { document.getElementById(id).style.display = 'none'; }
            function toggleOledMode() { document.body.classList.toggle('oled-mode'); let sEl = document.getElementById('darkModeStatus'); if(document.body.classList.contains('oled-mode')) { sEl.innerText = 'ON'; localStorage.setItem('oledMode', 'true'); } else { sEl.innerText = 'OFF'; localStorage.setItem('oledMode', 'false'); } }
            if(localStorage.getItem('oledMode') === 'true') { document.body.classList.add('oled-mode'); document.getElementById('darkModeStatus').innerText = 'ON'; }
            
            // ✅ TRENDING SLIDER LOAD FUNCTION
            async function loadTrending() {
                try {
                    const res = await fetch('/api/movies/trending');
                    const movies = await res.json();
                    trendingMovies = movies;
                    const container = document.getElementById('trendingSlider');
                    
                    if (movies.length === 0) {
                        document.querySelector('.trending-section-wrapper').style.display = 'none';
                        return;
                    }

                    container.innerHTML = movies.map((m, index) => {
                        const badgeHtml = m.categories && m.categories.includes('Adult Content') 
                            ? '<div class="badge-18-slider">18+</div>' 
                            : '';
                        
                        const imgUrl = `/api/image/${m.photo_id}`;
                        const rank = index + 1;

                        return `
                        <div class="trending-card" onclick="openTrendingDetail(${index})">
                            <div class="poster-slider">
                                <img src="${imgUrl}" loading="lazy" alt="${m.title}">
                                ${badgeHtml}
                                <div class="rank-badge">${rank}</div>
                            </div>
                            <div class="info-slider">
                                <div class="movie-title-slider">${m.title}</div>
                                <div class="movie-meta-slider">
                                    <span>${m.quality || 'HD'}</span>
                                    <span>${m.year || 'N/A'}</span>
                                </div>
                            </div>
                        </div>
                        `;
                    }).join('');

                    startAutoSlider('trendingSlider');
                    
                } catch (error) {
                    console.error("Trending Error:", error);
                    document.querySelector('.trending-section-wrapper').style.display = 'none';
                }
            }

            // Auto Scroll Function (Updated)
            function startAutoSlider(sliderId) {
                const slider = document.getElementById(sliderId);
                if(!slider) return;

                const intervalId = setInterval(() => {
                    const cardWidth = 260; // 240px card + 20px gap
                    const maxScroll = slider.scrollWidth - slider.clientWidth;
                    
                    if (slider.scrollLeft >= maxScroll) {
                        slider.scrollTo({ left: 0, behavior: 'smooth' });
                    } else {
                        slider.scrollBy({ left: cardWidth, behavior: 'smooth' });
                    }
                }, 3000); 
            }

            function openTrendingDetail(index) {
                // 🎯 সরাসরি মুভি অবজেক্ট পাঠানো হয় — currentViewMovies ওভাররাইট হলেও ভুল মুভি খুলবে না
                openDetail(index, '', trendingMovies[index]);
            }

            // ✅ Pagination Functions
            function activeCatIsAdult() { return activeCat && String(activeCat).toLowerCase().indexOf('adult') !== -1; }
            async function loadHomeMovies(page = 1) {
                homeCurrentPage = page;
                const list = document.getElementById('movieListHome');
                list.innerHTML = '<div class="skeleton"></div>';
                try {
                    const res = await fetch('/api/list?cat='+activeCat+'&uid='+uid+'&page='+page);
                    const data = await res.json();
                    currentViewMovies = data.movies || [];
                    // 💰 ক্যাটাগরি-ভিত্তিক অ্যাড: নরমাল ক্যাটাগরিতে নরমাল ব্যানার+স্কয়ার অ্যাড,
                    // অ্যাডাল্ট ক্যাটাগরিতে অ্যাডাল্ট টপ ব্যানার + অ্যাডাল্ট স্কয়ার + অ্যাডাল্ট স্পন্সর লিংক
                    // (অ্যাডাল্ট অ্যাড কখনো নরমাল ক্যাটাগরিতে দেখাবে না!)
                    let adultCat = activeCatIsAdult();
                    let hbN = document.getElementById('homeBannerNormal'); let hbA = document.getElementById('homeBannerAdult');
                    if (hbN && hbA) { hbN.style.display = adultCat ? 'none' : 'block'; hbA.style.display = adultCat ? 'block' : 'none'; }
                    // 🧠 CPM: যে টপ-ব্যানার সত্যিই দেখা যাবে শুধু সেটাই মাউন্ট (ক্যাটাগরি বদলালে ক্যাশড iframe রিইউজ —
                    // বারবার নতুন ইম্প্রেশন না)। লুকানো অ্যাডাল্ট ব্যানার পেজ-লোডেই লোড করলে মৃত ইম্প্রেশন জমত।
                    if (adultCat) mountSquareSlot('homeBannerAdult', AD_320_ADULT_CONF);
                    else mountSquareSlot('homeBannerNormal', AD_320_CONF);
                    // ফিড ইন্টারলিভ: প্রতি ৫টা মুভি কার্ডের পর 300x250 স্কয়ার ব্যানার, প্রতি ১০টার পর স্পন্সর অ্যাড কার্ড
                    let html = '';
                    currentViewMovies.forEach(function(m, index) {
                        html += createMovieCard(m, index);
                        // 🧠 CPM: প্রতি ৮টা কার্ডে ১টা ব্যানার (আগে ৫-তে ছিল) — কম ঘন ঘন ব্যানার মানে
                        // কম জাঙ্ক ইম্প্রেশন + ইউজার কম বিরক্ত, প্রতি ইম্প্রেশনের ভ্যালু বাড়ে → CPM ওঠে
                        if ((index + 1) % 8 === 0 && index < currentViewMovies.length - 1) html += '<div class="feed-ad-wrap"></div>';
                        if ((index + 1) % 10 === 0 && index < currentViewMovies.length - 1) html += sponsorCardHTML(adultCat);
                    });
                    list.innerHTML = html || '<p style="text-align:center; color:#64748b; padding:30px;">কোনো মুভি পাওয়া যায়নি!</p>';
                    mountFeedAds(list, adultCat ? AD_ADULT_CONF : AD_300_CONF);
                    renderPagination(data.total_pages, homeCurrentPage, 'paginationHome', 'loadHomeMovies');
                } catch(e) {}
            }

            // 💰 ফিড ব্যানার মাউন্ট: প্রতিটা .feed-ad-wrap এ Adsterra iframe (srcdoc) বসানো —
            // প্রতি iframe-এর নিজস্ব window থাকায় atOptions একে অপরকে overwrite করে না
            // 🧠 ইম্প্রেশন হাইজিন: একই জোনের iframe প্রতিবার নতুন করে বানালে ডুপ্লিকেট ইম্প্রেশন জমে —
            // বিজ্ঞাপনদাতার কাছে কোয়ালিটি খারাপ দেখায়, CPM প্রতিদিন পড়ে যায়। তাই reuseKey (স্লট-আইডি)
            // দিলে আগে বানানো iframe-টাই move করে বসানো হয় (move করলে reload হয় না → নতুন ইম্প্রেশন না)।
            // ক্যাশ-কী = জোন + স্লট — একই জোন দুই স্লটে থাকলে এক স্লটের iframe অন্য স্লটে চলে যায় না।
            const adIframeCache = {};
            function makeAdIframe(conf, reuseKey) {
                if (!conf) return null;
                let cacheKey = reuseKey ? conf.key + '::' + reuseKey : null;
                if (cacheKey && adIframeCache[cacheKey]) return adIframeCache[cacheKey];
                const f = document.createElement('iframe');
                f.setAttribute('scrolling', 'no');
                f.setAttribute('frameborder', '0');
                f.style.cssText = 'border:0;overflow:hidden;width:' + conf.w + 'px;height:' + conf.h + 'px;max-width:100%;';
                let inner = "<script>atOptions = { 'key' : '" + conf.key + "', 'format' : 'iframe', 'height' : " + conf.h + ", 'width' : " + conf.w + ", 'params' : {} };<\\/script>";
                inner += "<script src='" + conf.src + "'><\\/script>";
                f.srcdoc = "<html><body style='margin:0;padding:0;background:transparent'>" + inner + "</body></html>";
                if (cacheKey) adIframeCache[cacheKey] = f;
                return f;
            }
            // 🧠 স্কয়ার/ব্যানার স্লট — এক সেশনে একই স্লট = এক ইম্প্রেশন,
            // একই iframe আগে থেকেই বসা থাকলে ধুয়ে নতুন বসানো হয় না (CPM প্রোটেকশন)
            function mountSquareSlot(slotId, conf) {
                let slot = document.getElementById(slotId); if (!slot) return;
                let f = makeAdIframe(conf, slotId);
                if (!f) { slot.innerHTML = ''; return; }
                if (slot.firstChild !== f) { slot.innerHTML = ''; slot.appendChild(f); }
            }
            // 🧠 CPM: ফিড-ব্যানার স্ক্রিনে চোখে আসার আগে লোডই হয় না (IntersectionObserver, rootMargin 150px —
            // স্ক্রলে প্রায় চোখে আসতেই লোড হয়ে যায়)। আগে অদেখা, স্ক্রলে-পিছলে-যাওয়া ব্যানারও ইম্প্রেশন খেত —
            // ওই সব ভিউঅ্যাবল-না-হওয়া ইম্প্রেশনের কারণেই বিজ্ঞাপনদাতার বিড পড়ে CPM নামত। এখন প্রতিটা
            // ইম্প্রেশন সত্যিই দেখা ব্যানারের → ভিউঅ্যাবিলিটি স্কোর ভালো → CPM ওঠে।
            const adFeedObserver = ('IntersectionObserver' in window) ? new IntersectionObserver(function(entries) {
                entries.forEach(function(en) {
                    let box = en.target;
                    if (!box.isConnected) { adFeedObserver.unobserve(box); return; }
                    if (!en.isIntersecting) return;
                    adFeedObserver.unobserve(box);
                    if (box.getAttribute('data-mounted')) return;
                    box.setAttribute('data-mounted', '1');
                    let f = makeAdIframe(box.__adConf);
                    if (f) box.appendChild(f);
                });
            }, { rootMargin: '150px 0px' }) : null;
            function mountFeedAds(rootEl, conf) {
                if (!rootEl || !rootEl.querySelectorAll) return;
                rootEl.querySelectorAll('.feed-ad-wrap:not([data-mounted])').forEach(function(box) {
                    box.__adConf = conf || AD_300_CONF;
                    if (adFeedObserver) { adFeedObserver.observe(box); }
                    else { box.setAttribute('data-mounted', '1'); let f = makeAdIframe(box.__adConf); if (f) box.appendChild(f); }
                });
            }

            async function searchMovies(page = 1) { 
                const q = document.getElementById('searchInputMain').value.trim(); 
                const list = document.getElementById('movieListSearch'); 
                if(!q) { list.innerHTML = ''; document.getElementById('paginationSearch').innerHTML = ''; return; } 
                searchCurrentPage = page;
                try { 
                    const res = await fetch('/api/list?q='+encodeURIComponent(q)+'&uid='+uid+'&page='+page); 
                    const data = await res.json(); 
                    currentViewMovies = data.movies || []; 
                    list.innerHTML = currentViewMovies.length > 0 ? currentViewMovies.map(function(m, index) { return createMovieCard(m, index); }).join('') : '<p style="text-align:center; color:#64748b;">খুঁজে পাওয়া যায়নি!</p>'; 
                    renderPagination(data.total_pages, searchCurrentPage, 'paginationSearch', 'searchMovies'); 
                } catch(e) {} 
            }

            function renderPagination(totalPages, currentPage, containerId, functionName) {
                const container = document.getElementById(containerId);
                if(totalPages <= 1) { container.innerHTML = ''; return; }
                let html = '';
                html += `<button class="page-btn" onclick="${functionName}(${currentPage - 1})" ${currentPage === 1 ? 'disabled' : ''}><i class="fa-solid fa-chevron-left"></i> Prev</button>`;
                let startPage = Math.max(1, currentPage - 1);
                let endPage = Math.min(totalPages, currentPage + 1);
                if(startPage > 1) { html += `<button class="page-btn" onclick="${functionName}(1)">1</button>`; if(startPage > 2) html += `<span style="color:#64748b;">...</span>`; }
                for(let i = startPage; i <= endPage; i++) { html += `<button class="page-btn ${i === currentPage ? 'active' : ''}" onclick="${functionName}(${i})">${i}</button>`; }
                if(endPage < totalPages) { if(endPage < totalPages - 1) html += `<span style="color:#64748b;">...</span>`; html += `<button class="page-btn" onclick="${functionName}(${totalPages})">${totalPages}</button>`; }
                html += `<button class="page-btn" onclick="${functionName}(${currentPage + 1})" ${currentPage === totalPages ? 'disabled' : ''}>Next <i class="fa-solid fa-chevron-right"></i></button>`;
                container.innerHTML = html;
                window.scrollTo({top:0, behavior:'smooth'});
            }

            function createMovieCard(m, index, srcName) {
                let isFav = userFavs.includes(m._id);
                let isAdult = (m.categories && m.categories.indexOf("Adult Content") !== -1) || (m.title && m.title.toUpperCase().indexOf('18+') !== -1);
                let isVerified = localStorage.getItem('isAdult') === 'true';
                let imgSrc = (isAdult && !isVerified) ? 'https://via.placeholder.com/300x169/1e293b/ef4444?text=18%2B+🔒' : `/api/image/${m.photo_id}`;
                let lockOverlay = (isAdult && !isVerified) ? `<div class="adult-lock-overlay"><i class="fa-solid fa-lock"></i></div>` : '';
                let clickAction = (isAdult && !isVerified) ? `onclick="verify18(null)"` : `onclick="openDetail(${index}${srcName ? ", '" + srcName + "'" : ''})"`;

                // 🗂 ফাইল কাউন্ট ব্যাজ — ভিতরে কয়টা ফাইল/লিংক আছে ("পার্ট" বা 720p লেখা হয় না) + 👁 ভিউ ব্যাজ
                let filesArr = m.files || [];
                let nFiles = filesArr.length > 0 ? filesArr.length : 1;
                let fileLabel = nFiles > 1 ? nFiles + ' ফাইল' : '1 ফাইল';
                let viewsBadge = `<div class="views-badge"><i class="fa-solid fa-eye"></i> ${m.clicks || 0}</div>`;

                // 👑 Admin Badge (শুধু অ্যাডমিন দেখবে; বাম-উপরের কোণটা এখন লাভ বাটনের — তাই ডান-নিচে)
                let adminViewBadge = '';
                if(isAdmin) {
                    adminViewBadge = `<div class="admin-view-badge"><i class="fa-solid fa-crown"></i> ${m.clicks || 0}</div>`;
                }

    return `<div class="movie-card" ${clickAction}>
                <div style="position: relative; flex-shrink: 0;">
                    <img src="${imgSrc}" style="width: 100%; aspect-ratio: 16/9; object-fit: cover;" loading="lazy">
                    ${lockOverlay}
                    <div class="part-badge"><i class="fa-solid fa-layer-group"></i> ${fileLabel}</div>
                    ${viewsBadge}
                    ${adminViewBadge}
                </div>
                <div class="movie-info">
                    <div class="movie-title">${m.title}</div>
                    <div class="movie-meta-row">
                        <div class="left-meta">
                            <span class="year-badge">${m.year || 'N/A'}</span>
                        </div>
                        <div class="right-meta">
                            ${(m.categories || []).slice(0, 2).map(function(c) { return `<span class="cat-small-tag">${c}</span>`; }).join('')}
                        </div>
                    </div>
                </div>
                <button class="fav-btn ${isFav ? 'active' : ''}" onclick="event.stopPropagation(); toggleFav('${m._id}', this)"><i class="fa-solid fa-heart"></i></button>
            </div>`;
            }

            // ✅ openDetail — এখন মোডালের বদলে ফুল "Video Details" পেজ (রেফারেন্স লেআউট):
            // ফুল পোস্টার + লাইক/ডাউনলোড/ভোট/শেয়ার স্ট্যাট + Save Later + রেটিং পোল
            function openDetail(index, srcName, movieObj) {
                let arr = srcName === 'fav' ? favMovies : (srcName === 'saved' ? savedMovies : currentViewMovies);
                let m = movieObj || arr[index];
                if(!m) return;
                // 🔙 ব্যাক করলে যেন আগের ট্যাব + হুবহু আগের স্ক্রল পজিশনে ফেরা যায়
                let activeSec = document.querySelector('.page-section.active');
                if (activeSec && activeSec.id !== 'tabDetails') {
                    detailsReturnTab = activeSec.id.replace(/^tab/, '');
                    detailsReturnTab = detailsReturnTab.charAt(0).toLowerCase() + detailsReturnTab.slice(1);
                    detailsReturnScroll = window.scrollY || 0;
                }
                activeDetailMovie = m;
                // সব ট্যাব লুকিয়ে ডিটেইলস পেজ দেখানো
                document.querySelectorAll('.page-section').forEach(function(el) { el.classList.remove('active'); });
                document.querySelectorAll('.nav-item').forEach(function(el) { el.classList.remove('active'); });
                document.getElementById('tabDetails').classList.add('active');
                updateTopAdBar();
                window.scrollTo({top:0, behavior:'auto'});
                renderDetailsPage(m);
                loadDetailsStats(m);
                loadPoll(m);
            }

            function detailMovieId(m) { return (m.files && m.files.length && m.files[0] && m.files[0].id) ? m.files[0].id : m._id; }
            function detailTitle(m) { return m.title || 'Unknown Movie'; }
            function closeDetails() {
                // 🔙 আগের ট্যাবে (হোম/সার্চ/ফেভ...) ফেরা + স্ক্রল হুবহু আগের জায়গায় — ফিড আর রিলোড হয় না
                let navMap = {home:0, search:1, fav:2, favorites:2, surprise:3, profile:4};
                let navs = document.querySelectorAll('.nav-item');
                let idx = (navMap[detailsReturnTab] !== undefined) ? navMap[detailsReturnTab] : 0;
                switchTab(detailsReturnTab || 'home', navs[idx] || null, {noReload: true, noScrollTop: true});
                setTimeout(function(){ window.scrollTo({top: detailsReturnScroll || 0, behavior: 'auto'}); }, 60);
            }
            function openDownloadModal() {
                // 🧠 CPM: মোডাল খোলার মুহূর্তেই ব্যানার মাউন্ট — আগে পেজ-লোডেই লুকানো মোডালে ব্যানার
                // লোড হয়ে যেত, যা ইউজার দেখতই না (মৃত ইম্প্রেশন জমে CPM পড়ে যেত)।
                // শুধু যেটা দেখা যাবে সেটাই মাউন্ট + এক সেশনে এক স্লট = এক ইম্প্রেশন (ক্যাশড iframe রিইউজ)।
                if (activeIsAdult) mountSquareSlot('dlBannerAdult', AD_320_ADULT_CONF);
                else mountSquareSlot('dlBannerNormal', AD_320_CONF);
                mountSquareSlot('dlSquareSlot', activeIsAdult ? AD_ADULT_CONF : AD_300_CONF);
                document.getElementById('downloadModal').style.display = 'flex';
            }
            function fmtCount(n) { n = n || 0; if (n >= 1000000) return (n/1000000).toFixed(1) + 'M'; if (n >= 1000) return (n/1000).toFixed(1) + 'K'; return String(n); }

            function renderDetailsPage(m) {
                document.getElementById('detailImg').src = `/api/image/${m.photo_id}`;
                document.getElementById('detailTitle').innerText = detailTitle(m);
                // 🎞 কোয়ালিটি লিস্ট ("পার্ট" লেখা নেই — মানুষ যেন ভুল বুঝে মুভির আলাদা পার্ট মনে না করে)
                let filesArr = m.files || [];
                let quals = filesArr.map(function(f) { return (f.quality && f.quality !== 'Main') ? f.quality : 'HD'; });
                quals = quals.filter(function(v, i) { return quals.indexOf(v) === i; });
                if (!quals.length) quals = [(m.quality && m.quality !== 'Main') ? m.quality : 'HD'];
                document.getElementById('detailMeta').innerHTML = `<span>${m.year || 'N/A'}</span> • <span><i class="fa-solid fa-clapperboard"></i> ${quals.join(' • ')}</span> • <span><i class="fa-solid fa-eye"></i> ${m.clicks || 0} বার দেখা হয়েছে</span>`;
                document.getElementById('detailCats').innerHTML = (m.categories || []).map(function(c) { return `<span class="movie-cat-tag">${c}</span>`; }).join(' ');

                let isAdult = (m.categories && m.categories.indexOf("Adult Content") !== -1) || (m.title && m.title.toUpperCase().indexOf('18+') !== -1);
                activeIsAdult = isAdult;

                // ✅ ডাউনলোড সিস্টেম হুবহু আগের মতো (কোয়ালিটি অনুযায়ী বাটন + লক/আনলক + অ্যাড ফ্লো) —
                // বাটনগুলো এখন ডাউনলোড পপ-আপ মোডালের ভিতরে থাকে (পেজে স্ক্রল না করেই খোলে)
                let btnsHtml = "";
                if (filesArr.length > 0) {
                    btnsHtml = filesArr.map(function(f, idx) {
                        let isFree = f.is_unlocked || isUserVip;
                        let fq = (f.quality && f.quality !== 'Main') ? f.quality : 'HD';
                        return `<button class="dl-file-btn ${isFree ? 'unlocked' : ''}" style="animation-delay:${(idx * 0.08).toFixed(2)}s" onclick="handleFileClick('${f.id}', ${isFree ? 'true' : 'false'}, ${isAdult ? 'true' : 'false'})"><span><i class="fa-solid fa-${isFree ? 'lock-open' : 'lock'}"></i> Download ${fq}</span><i class="fa-solid fa-chevron-right dl-chev"></i></button>`;
                    }).join('');
                } else {
                    let isFree = isUserVip;
                    btnsHtml = `<button class="dl-file-btn ${isFree ? 'unlocked' : ''}" onclick="handleFileClick('${m._id}', ${isFree ? 'true' : 'false'}, ${isAdult ? 'true' : 'false'})"><span><i class="fa-solid fa-${isFree ? 'lock-open' : 'lock'}"></i> Download ${quals[0]}</span><i class="fa-solid fa-chevron-right dl-chev"></i></button>`;
                }

                document.getElementById('fileButtonsContainer').innerHTML = btnsHtml;
                // ⬇ পপ-আপ মোডালের ভিতরে: টাইটেল + 320x50 ব্যানার + 300x250 স্কয়ার অ্যাড + স্পন্সর (১৮+ হলে সব অ্যাডাল্ট)
                // 🧠 CPM: ব্যানারগুলো এখন openDownloadModal-এ (মোডাল খোলার মুহূর্তে) মাউন্ট হয় —
                // ডিটেইলস খোলা-খোলা প্রতিবার লুকানো মোডালে ব্যানার লোড করা = মৃত ইম্প্রেশন, তাই না
                document.getElementById('dlModalTitle').innerText = detailTitle(m) + ' — ' + quals.join(' • ');
                document.getElementById('dlBannerNormal').style.display = isAdult ? 'none' : 'block';
                document.getElementById('dlBannerAdult').style.display = isAdult ? 'block' : 'none';
                document.getElementById('dlSponsorSlot').innerHTML = sponsorCardHTML(isAdult);
                // 💰 ডিটেইলস পেজেও একটা ছোট ব্যানার থাকবে: সাধারণ মুভিতে 320x50, ১৮+ মুভিতে অ্যাডাল্ট ব্যানার।
                // 🧠 CPM: যেটা সত্যিই দেখা যাবে শুধু সেটাই মাউন্ট — লুকানোটা মাউন্ট করলে মৃত ইম্প্রেশন হতো
                document.getElementById('detailBannerNormal').style.display = isAdult ? 'none' : 'block';
                document.getElementById('detailBannerAdult').style.display = isAdult ? 'block' : 'none';
                if (isAdult) mountSquareSlot('detailBannerAdult', AD_320_ADULT_CONF);
                else mountSquareSlot('detailBannerNormal', AD_320_CONF);

                // স্ট্যাট রিসেট (আসল অবস্থা API থেকে আসবে)
                detailStats = {likes:0, downloads:0, votes:0, shares:0};
                document.getElementById('statLikes').innerText = '...';
                document.getElementById('statDownloads').innerText = fmtCount(m.clicks || 0);
                document.getElementById('statVotes').innerText = '...';
                document.getElementById('statShares').innerText = '...';
                setLikeUI(false, 0); setSaveUI(false);
                pollState = {counts: {}, total: 0, percent: 0, user_vote: null};
                document.getElementById('pollBar').style.width = '0%';
                document.getElementById('pollPercent').innerText = '0%';
                document.getElementById('pollTotal').innerText = '0 votes';
                document.getElementById('pollOptions').innerHTML = '<div class="poll-note">পোল লোড হচ্ছে...</div>';
            }

            async function loadDetailsStats(m) {
                try {
                    const res = await fetch('/api/web/details/' + detailMovieId(m) + '?uid=' + uid);
                    const d = await res.json();
                    if (!d || !d.movie) return;
                    // গ্রুপ করা আসল ডেটা দিয়ে আপডেট (ডিপ-লিংক/ট্রেন্ডিং থেকে এলেও পার্ট লিস্ট ঠিক থাকে)
                    activeDetailMovie = Object.assign({}, d.movie, m);
                    if (!(m.files && m.files.length) && d.movie.files && d.movie.files.length) { renderDetailsPage(activeDetailMovie); loadPoll(activeDetailMovie); }
                    setLikeUI(!!d.liked, d.likes || 0);
                    setSaveUI(!!d.saved);
                    detailStats = {likes: d.likes || 0, downloads: d.downloads || 0, votes: d.votes_count || 0, shares: d.shares || 0};
                    document.getElementById('statDownloads').innerText = fmtCount(detailStats.downloads);
                    document.getElementById('statVotes').innerText = fmtCount(detailStats.votes);
                    document.getElementById('statShares').innerText = fmtCount(detailStats.shares);
                } catch(e) {}
            }

            /* ❤️ লাইক সিস্টেম — ইউনিক: হার্ট পপ + ছোট হার্টগুলো উপরে উড়ে যাওয়া + কাউন্ট বাম্প */
            function heartBurst(btn) {
                if (!btn) return;
                let r = btn.getBoundingClientRect();
                let colors = ['#ef4444','#f43f5e','#ec4899','#fb7185'];
                for (let i = 0; i < 7; i++) {
                    let h = document.createElement('span');
                    h.className = 'burst-heart';
                    h.innerHTML = '<i class="fa-solid fa-heart"></i>';
                    h.style.left = (r.left + r.width/2 - 8) + 'px';
                    h.style.top = (r.top + r.height/2 - 8) + 'px';
                    h.style.setProperty('--tx', (Math.random()*140-70) + 'px');
                    h.style.setProperty('--ty', (-55 - Math.random()*95) + 'px');
                    h.style.fontSize = (10 + Math.random()*9) + 'px';
                    h.style.color = colors[i % colors.length];
                    document.body.appendChild(h);
                    (function(el){ setTimeout(function(){ el.remove(); }, 900); })(h);
                }
            }
            function bumpStat(id) {
                let el = document.getElementById(id);
                if (!el) return;
                el.classList.remove('bump'); void el.offsetWidth; el.classList.add('bump');
            }
            function setLikeUI(liked, count) {
                let b = document.getElementById('likeBtn');
                b.classList.toggle('liked', !!liked);
                b.innerHTML = `<i class="fa-${liked ? 'solid' : 'regular'} fa-heart"></i> ${liked ? 'Liked' : 'Like'} · ${fmtCount(count)}`;
                let sl = document.getElementById('statLikes');
                if (sl.innerText !== fmtCount(count)) { sl.innerText = fmtCount(count); bumpStat('statLikes'); }
            }
            async function toggleLike() {
                if (!activeDetailMovie) return;
                if (!uid) { tg.showAlert('বট থেকে অ্যাপ খুলে লাইক দিতে পারবেন!'); return; }
                try {
                    const res = await fetch('/api/web/like/toggle', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({movie_id: detailMovieId(activeDetailMovie), initData: INIT_DATA})});
                    const d = await res.json();
                    if (d.ok) {
                        setLikeUI(d.liked, d.likes); detailStats.likes = d.likes;
                        let b = document.getElementById('likeBtn');
                        b.classList.remove('like-pop'); void b.offsetWidth; b.classList.add('like-pop');
                        if (d.liked) heartBurst(b);
                        if (tg.HapticFeedback && tg.HapticFeedback.impactOccurred) { try { tg.HapticFeedback.impactOccurred('light'); } catch(e) {} }
                    }
                } catch(e) {}
            }

            /* 💾 সেভ লেটার */
            function setSaveUI(saved) {
                let b = document.getElementById('saveBtn');
                b.classList.toggle('saved', !!saved);
                b.innerHTML = `<i class="fa-${saved ? 'solid' : 'regular'} fa-bookmark"></i> ${saved ? 'Saved ✓' : 'Save Later'}`;
            }
            async function toggleSaved() {
                if (!activeDetailMovie) return;
                if (!uid) { tg.showAlert('বট থেকে অ্যাপ খুলে সেভ করতে পারবেন!'); return; }
                try {
                    const res = await fetch('/api/web/saved/toggle', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({movie_id: detailMovieId(activeDetailMovie), initData: INIT_DATA})});
                    const d = await res.json();
                    if (d.ok) setSaveUI(d.saved);
                } catch(e) {}
            }

            /* 📤 শেয়ার */
            async function shareMovie() {
                if (!activeDetailMovie) return;
                let mid = detailMovieId(activeDetailMovie);
                let url = 'https://t.me/' + BOT_USERNAME + '?startapp=' + mid;
                try {
                    tg.openTelegramLink('https://t.me/share/url?url=' + encodeURIComponent(url) + '&text=' + encodeURIComponent('🎬 ' + detailTitle(activeDetailMovie) + ' — Movie Box এ দেখুন!'));
                } catch(e) { window.open('https://t.me/share/url?url=' + encodeURIComponent(url), '_blank'); }
                try {
                    const res = await fetch('/api/web/share', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({movie_id: mid, initData: INIT_DATA})});
                    const d = await res.json();
                    if (d.ok) { detailStats.shares = d.shares; document.getElementById('statShares').innerText = fmtCount(detailStats.shares); }
                } catch(e) {}
            }

            /* 🗳️ রেটিং পোল (কমেন্ট সিস্টেমের বদলে) — রেফারেন্স ডিজাইন হুবহু:
               এক ইউজার এক ভোট — ভোট বদলানো যায়, উঠানো যায় না; হ্যাপটিক + অ্যানিমেশনসহ */
            function escHtml(s) { return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
            const POLL_OPTIONS = [
                {key: 'must', emoji: '🔥', title: 'Must Watch', sub: 'অবশ্যই দেখুন'},
                {key: 'good', emoji: '👍', title: 'Good', sub: 'ভালো লেগেছে'},
                {key: 'avg', emoji: '😐', title: 'Average', sub: 'মোটামুটি'},
                {key: 'worst', emoji: '👎', title: 'Worst', sub: 'সময় নষ্ট'}
            ];
            function pollOptionHtml(o, count, voted, idx) {
                return `<div class="poll-opt ${o.key}${voted ? ' voted' : ''}" style="animation-delay:${(idx * 0.08).toFixed(2)}s" onclick="votePoll('${o.key}')">
                    <div class="poll-check"><i class="fa-solid fa-check"></i></div>
                    <div class="poll-emoji">${o.emoji}</div>
                    <div class="poll-txt"><div class="poll-title">${o.title}</div><div class="poll-sub">${o.sub}</div></div>
                    <div class="poll-count" id="pollCount_${o.key}">${count}</div>
                </div>`;
            }
            function renderPoll(full) {
                let box = document.getElementById('pollOptions');
                if (full || !box.children.length) {
                    // প্রথম রেন্ডার — স্ট্যাগার্ড স্লাইড-ইন অ্যানিমেশনসহ সব কার্ড
                    box.innerHTML = POLL_OPTIONS.map(function(o, i) { return pollOptionHtml(o, pollState.counts[o.key] || 0, pollState.user_vote === o.key, i); }).join('');
                } else {
                    // ভোটের পরে — শুধু কাউন্ট/ভোটেড স্টেট আপডেট (এন্ট্রি অ্যানিমেশন আবার চলবে না)
                    POLL_OPTIONS.forEach(function(o) {
                        let el = box.querySelector('.poll-opt.' + o.key); if (!el) return;
                        el.querySelector('.poll-count').innerText = pollState.counts[o.key] || 0;
                        el.classList.toggle('voted', pollState.user_vote === o.key);
                    });
                }
                document.getElementById('pollBar').style.width = pollState.percent + '%';
                document.getElementById('pollPercent').innerText = pollState.percent + '%';
                document.getElementById('pollTotal').innerText = pollState.total + ' votes';
                let note = document.getElementById('pollNote');
                if (pollState.user_vote) {
                    let my = POLL_OPTIONS.filter(function(o) { return o.key === pollState.user_vote; })[0] || POLL_OPTIONS[0];
                    note.innerHTML = '✅ আপনার ভোট: <b>' + my.emoji + ' ' + my.title + '</b> — ভোট উঠানো যাবে না, অন্য অপশনে চেপে বদলাতে পারবেন।';
                } else {
                    note.textContent = 'আপনার ভোট দিন — একবার ভোট দিলে তা আর উঠানো যাবে না, চাইলে অন্য অপশনে বদলাতে পারবেন।';
                }
            }
            async function loadPoll(m) {
                if (!m) return;
                try {
                    const res = await fetch('/api/web/poll/' + detailMovieId(m) + '?uid=' + (uid || 0));
                    const d = await res.json();
                    pollState = {counts: d.counts || {}, total: d.total || 0, percent: d.percent || 0, user_vote: d.user_vote || null};
                    renderPoll(true);
                } catch(e) {
                    document.getElementById('pollOptions').innerHTML = '<div class="poll-note">পোল লোড করা যায়নি।</div>';
                }
            }
            async function votePoll(option) {
                if (!activeDetailMovie) return;
                if (!uid) { tg.showAlert('বট থেকে অ্যাপ খুলে ভোট দিতে পারবেন!'); return; }
                // 🚫 ভোট উঠানো যাবে না — একই অপশনে আবার চাপলে জানিয়ে দেওয়া হয়; অন্য অপশনে চাপলে ভোট বদলাবে
                if (pollState.user_vote === option) {
                    try { tg.HapticFeedback.impactOccurred('rigid'); } catch(e) {}
                    tg.showAlert('আপনি ইতিমধ্যে এই ভোটটি দিয়েছেন — ভোট উঠানো যায় না। চাইলে অন্য অপশনে চেপে বদলাতে পারবেন!');
                    return;
                }
                try { tg.HapticFeedback.impactOccurred('medium'); } catch(e) {}
                try { if (navigator.vibrate) navigator.vibrate(60); } catch(e) {}
                let emojiEl = document.querySelector('.poll-opt.' + option + ' .poll-emoji');
                if (emojiEl) { emojiEl.classList.remove('bounce'); void emojiEl.offsetWidth; emojiEl.classList.add('bounce'); }
                try {
                    const res = await fetch('/api/web/poll/vote', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({movie_id: detailMovieId(activeDetailMovie), option: option, initData: INIT_DATA})});
                    const d = await res.json();
                    if (d.ok) {
                        pollState = {counts: d.counts || {}, total: d.total || 0, percent: d.percent || 0, user_vote: d.user_vote || null};
                        renderPoll(false);
                        let cnt = document.getElementById('pollCount_' + option);
                        if (cnt) { cnt.classList.add('pop'); setTimeout(function() { cnt.classList.remove('pop'); }, 460); }
                        detailStats.votes = pollState.total;
                        document.getElementById('statVotes').innerText = fmtCount(pollState.total);
                    } else { tg.showAlert('⚠️ ভোট দেওয়া যায়নি! আবার চেষ্টা করুন।'); }
                } catch(e) {}
            }
            
            // 🎯 ফিক্স: আগে অ্যাড গেট (adModal) ডাউনলোড পপ-আপের পেছনে খুলে যেত — DOM-এ আগে আর
            // z-index সমান হওয়ায় পপ-আপটাই উপরে থেকে গেট ঢেকে দিত, ইউজার মনে করত "বাগে চলে গেছে"।
            // এখন: পপ-আপ বন্ধ করে সাথে সাথে শর্ত/অ্যাড পেজ সামনে আসবে; VIP ছাড়া সবাই গেট দেখবে
            // (আনলকড হোক বা না হোক), তারপর বিজ্ঞাপন দেখে ফাইল পাবে। 💎 VIP সরাসরি ফাইল পায়।
            function handleFileClick(fileId, isFree, isAdult) {
                activeFileId = fileId; activeIsAdult = isAdult;
                closeModal('downloadModal');
                if (isUserVip) { sendFileRequest(fileId); return; }
                resetAdModal();
                // 🧠 CPM: অ্যাড-গেট মোডালের ব্যানার এখন মোডাল খোলার মুহূর্তেই মাউন্ট — ইউজার দেখার মতো ব্যানারই ইম্প্রেশন খাবে
                mountSquareSlot('adModalAdSlot', AD_300_CONF);
                document.getElementById('adModal').style.display = 'flex';
            }
            
            // 💰 অ্যাড রাউন্ড সিস্টেম: ad_count সেটিং অনুযায়ী ১টির বেশি অ্যাড দেখানো যায় + লিংক রোটেশন (Adsterra ↔ Monetag)
            function resetAdModal() {
                adStartTime = 0; adRound = 0; adAwayMs = 0; adAwayStart = null; adWasHidden = false;
                if (adCountdownTimer) { clearInterval(adCountdownTimer); adCountdownTimer = null; }
                let stepEl = document.getElementById('adStepText');
                let warn = ' ⚠️ সময়ের আগে ফিরে এলে আবার শুরু থেকে দেখতে হবে!';
                if(adSettings.ad_count > 1) { stepEl.innerHTML = `আপনাকে মোট <b>${adSettings.ad_count}টি</b> বিজ্ঞাপন দেখতে হবে। প্রতিটি লিংকে ক্লিক করে কমপক্ষে <b>${adSettings.ad_wait} সেকেন্ড</b> অপেক্ষা করে ফিরে আসুন.${warn}`; }
                else { stepEl.innerHTML = `লিংকে ক্লিক করে বিজ্ঞাপনটি দেখুন এবং কমপক্ষে <b>${adSettings.ad_wait} সেকেন্ড</b> অপেক্ষা করে ফিরে এসে নিচের বাটনে ক্লিক করুন.${warn}`; }
                document.getElementById('adClickBtn').innerText = 'বিজ্ঞাপন খুলুন';
                document.getElementById('adClickBtn').style.display = 'block';
                document.getElementById('adVerifyBtn').style.display = 'none';
                document.getElementById('adTryAgainBtn').style.display = 'none';
            }
            
            // 🎲 র‍্যান্ডম লিংক পিক: প্রতিবার ক্লিকে পুল থেকে র‍্যান্ডম লিংক সিলেক্ট হয় —
            // শুধু সেইম লিংক পরপর দুইবার যাবে না (এই সেশনে সর্বশেষ যেটা খোলা হয়েছে সেটা বাদ দিয়ে)
            let lastOpenedNormal = ''; let lastOpenedAdult = '';
            function pickRandomLink(isAdult) {
                let pool = (isAdult && ADULT_DIRECT_LINKS && ADULT_DIRECT_LINKS.length > 0) ? ADULT_DIRECT_LINKS : (DIRECT_LINKS && DIRECT_LINKS.length > 0 ? DIRECT_LINKS : null);
                if (!pool) return null;
                let last = isAdult ? lastOpenedAdult : lastOpenedNormal;
                let candidates = pool.filter(function(u) { return u !== last; });
                if (candidates.length === 0) candidates = pool;
                let link = candidates[Math.floor(Math.random() * candidates.length)];
                if (isAdult) lastOpenedAdult = link; else lastOpenedNormal = link;
                return link;
            }
            
            // 🔥 স্পন্সর অ্যাড কার্ড — ডাউনলোড বাটনের মত সাইজ, এনগেজমেন্ট ক্যাপশন ("সারপ্রাইজ") যাতে মানুষ ক্লিক করতে চায়;
            // ক্লিক করলে র‍্যান্ডম ডাইরেক্ট লিংক খোলে (হোম ফিড + ডাউনলোড পপ-আপ)
            function sponsorCardHTML(isAdult) {
                let link = pickRandomLink(isAdult);
                if (!link) return '';
                return `<div class="sponsor-card" onclick="event.stopPropagation(); tg.openLink('${link}')">
                    <div class="sponsor-emoji">🎁</div>
                    <div class="sponsor-txt">
                        <div class="sponsor-title">আপনার জন্য সারপ্রাইজ অপেক্ষা করছে!</div>
                        <div class="sponsor-sub">ভিতরে কী আছে দেখতে ক্লিক করুন 👀</div>
                    </div>
                    <span class="sponsor-tag">🔥 Sponsored</span>
                </div>`;
            }
            
            // 🛡️ অ্যান্টি-বাইপাস হেল্পার: শুধু অ্যাড পেজে (মিনি অ্যাপ লুকানো থাকা অবস্থায়) কাটানো সময়ই গোনা হয়;
            // অ্যাপে ফিরে বসে থাকলে টাইমার এগোবে না — আগে ফিরলে আবার শুরু থেকে অ্যাড দেখতে হবে
            function adTotalAway() { return adAwayMs + (adAwayStart !== null ? (Date.now() - adAwayStart) : 0); }
            function adRemainSec() { return Math.max(0, Math.ceil((adSettings.ad_wait * 1000 - adTotalAway()) / 1000)); }

            function openAdLink() {
                // 📳 টেপ দিলেই ফোন ভাইব্রেট করবে (Telegram হ্যাপটিক ইঞ্জিন + ব্রাউজার ফলব্যাক)
                try { tg.HapticFeedback.impactOccurred('medium'); } catch (e) {}
                try { if (navigator.vibrate) navigator.vibrate(80); } catch (e) {}
                document.getElementById('adClickBtn').style.display = 'none';
                let linkToOpen = pickRandomLink(activeIsAdult);
                if (!linkToOpen) {
                    // কোনো অ্যাড লিংক সেট করা নেই → সরাসরি আনলক (ইউজার যেন আটকে না থাকে)
                    finishAdRounds();
                    return;
                }
                // 🎲 র‍্যান্ডম: এই ক্লিকে পুল থেকে র‍্যান্ডম লিংক (সেইম লিংক পরপর দুইবার যায় না)
                tg.openLink(linkToOpen);
                adStartTime = Date.now();
                // 🛡️ অ্যাড খুলল — এখন থেকে শুধু অ্যাড পেজে থাকার সময় গোনা হবে
                adAwayMs = 0; adAwayStart = Date.now(); adWasHidden = false;
                let vBtn = document.getElementById('adVerifyBtn');
                vBtn.style.display = 'block';
                vBtn.disabled = true;
                vBtn.innerText = `⏳ অপেক্ষা করুন ${adSettings.ad_wait}s`;
                if (adCountdownTimer) clearInterval(adCountdownTimer);
                adCountdownTimer = setInterval(() => {
                    // 🛡️ ওয়াচডগ: অ্যাপ দৃশ্যমান কিন্তু ট্র্যাকিং এখনো "অ্যাডে আছে" — visibilitychange মিস হলেও ফিরে-আসা ধরা পড়বে
                    if (!document.hidden && adWasHidden && adAwayStart !== null) { adUserReturned(); return; }
                    let remain = adRemainSec();
                    if (remain <= 0) {
                        if (!document.hidden) { vBtn.disabled = false; vBtn.innerText = '✅ অ্যাড দেখে ফিরে এসেছি'; }
                    }
                    else if (!document.hidden) { vBtn.innerText = `⏳ অপেক্ষা করুন ${remain}s`; }
                }, 1000);
            }
            
            function checkAdWatched() {
                if (adStartTime === 0) return;
                let elapsed = adTotalAway();  // 🛡️ শুধু অ্যাড পেজে কাটানো সময়
                if (elapsed >= adSettings.ad_wait * 1000) {
                    adRound++;
                    if (adRound >= adSettings.ad_count) {
                        finishAdRounds();
                    } else {
                        // পরের অ্যাড রাউন্ড
                        tg.showAlert(`✅ দারুণ! এবার পরের বিজ্ঞাপনটি দেখুন (${adRound + 1}/${adSettings.ad_count})`);
                        document.getElementById('adVerifyBtn').style.display = 'none';
                        let oBtn = document.getElementById('adClickBtn');
                        oBtn.style.display = 'block';
                        oBtn.innerText = `বিজ্ঞাপন ${adRound + 1}/${adSettings.ad_count} খুলুন`;
                    }
                } else {
                    let remaining = adRemainSec();
                    tg.showAlert(`⚠️ আপনি সময়ের আগেই ফিরে এসেছেন! বিজ্ঞাপন পুরো ${adSettings.ad_wait} সেকেন্ড থাকতে হবে। (${remaining}s বাকি ছিল)`);
                    document.getElementById('adVerifyBtn').style.display = 'none';
                    document.getElementById('adTryAgainBtn').style.display = 'block';
                }
            }

            function finishAdRounds() {
                adStartTime = 0; adAwayMs = 0; adAwayStart = null;
                if (adCountdownTimer) { clearInterval(adCountdownTimer); adCountdownTimer = null; }
                closeModal('adModal');
                sendFileRequest(activeFileId);
            }

            // 🛡️ অ্যান্টি-বাইপাস মূল চেক: ইউজার অ্যাড থেকে অ্যাপে ফিরে এলেই চেক হয় —
            // পুরো wait সেকেন্ড অ্যাডে থাকলে ভেরিফাই চালু হয়, আগে ফিরলে এলার্ট + আবার শুরু থেকে অ্যাড দেখতে হবে
            function adUserReturned() {
                if (adStartTime === 0) return;
                if (adAwayStart !== null) { adAwayMs += Date.now() - adAwayStart; adAwayStart = null; }
                let remain = adRemainSec();
                let vBtn = document.getElementById('adVerifyBtn');
                if (remain <= 0) {
                    if (adCountdownTimer) { clearInterval(adCountdownTimer); adCountdownTimer = null; }
                    vBtn.disabled = false; vBtn.style.display = 'block'; vBtn.innerText = '✅ অ্যাড দেখে ফিরে এসেছি';
                } else {
                    resetAdModal();
                    tg.showAlert(`⚠️ আপনি সময়ের আগেই ফিরে এসেছেন! বিজ্ঞাপন পুরো ${adSettings.ad_wait} সেকেন্ড থাকতে হবে। (${remain}s বাকি ছিল)`);
                }
            }

            document.addEventListener('visibilitychange', function() {
                if (adStartTime === 0) return;  // অ্যাড ফেজ চলছে না
                if (document.hidden) { adAwayStart = Date.now(); adWasHidden = true; }
                else { adUserReturned(); }
            });

            // 🤖 সাকসেস মোডালের "বটে গিয়ে ফাইল নিন" — ফাইল সার্ভার থেকেই ইউজারের বট চ্যাটে
            // ডেলিভারি হয়ে যায় (/api/send), তাই প্লেইন লিংক দিলেই ক্লিক করলে বট চ্যাটে ফাইল সামনে
            let botFileLink = 'https://t.me/' + BOT_USERNAME;
            // 🤖 "বটে গিয়ে ফাইল নিন" — ক্লিক করলেই সরাসরি বট চ্যাট খুলে যাবে (ফাইল আগেই বটে
            // ডেলিভারি হয়ে থাকে), সাথে মিনি অ্যাপ পেজটাও বন্ধ হয়ে ইউজার বটেই থাকবে
            function openBotForFile() { try { tg.openTelegramLink(botFileLink); setTimeout(function(){ try { tg.close(); } catch(e) {} }, 400); } catch(e) { window.open(botFileLink, '_blank'); } }

            async function sendFileRequest(fileId) { try { const res = await fetch('/api/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({userId: uid, movieId: fileId, initData: INIT_DATA})}); const data = await res.json(); if(data.ok) { botFileLink = 'https://t.me/' + BOT_USERNAME; document.getElementById('successModal').style.display = 'flex'; fetchUserInfo(); } else if(data.new_user) { document.getElementById('newUserBotLink').href = 'https://t.me/' + (data.bot_username || BOT_USERNAME) + '?start=' + data.movie_id; document.getElementById('newUserModal').style.display = 'flex'; } else { tg.showAlert("⚠️ Failed!"); } } catch(e) {} }
            async function loadFavorites() { const list = document.getElementById('movieListFav'); list.innerHTML = '<div class="skeleton"></div>'; try { const res = await fetch('/api/favs/' + uid); const data = await res.json(); userFavs = data.map(function(m) { return m._id; }); favMovies = data; list.innerHTML = data.length > 0 ? data.map(function(m, index) { return createMovieCard(m, index, 'fav'); }).join('') : '<p style="text-align:center; color:#64748b; padding:30px;">কোনো ফেভারিট নেই!</p>'; } catch(e) {} loadSavedList(); }
            /* 💾 সেভ লেটার লিস্ট (ডিটেইলস পেজ থেকে সেভ করা মুভিগুলো) */
            async function loadSavedList() { const list = document.getElementById('movieListSaved'); if (!list) return; list.innerHTML = '<div class="skeleton"></div>'; try { const res = await fetch('/api/web/saved/' + uid); const data = await res.json(); savedMovies = data; list.innerHTML = data.length > 0 ? data.map(function(m, index) { return createMovieCard(m, index, 'saved'); }).join('') : '<p style="text-align:center; color:#64748b; padding:30px;">সেভ করা কিছু নেই!</p>'; } catch(e) { list.innerHTML = ''; } }
            async function toggleFav(title, btnEl) { try { const res = await fetch('/api/fav/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({uid: uid, title: title, initData: INIT_DATA})}); const data = await res.json(); if(data.isFav) { btnEl.classList.add('active'); userFavs.push(title); } else { btnEl.classList.remove('active'); userFavs = userFavs.filter(function(t) { return t !== title; }); } } catch(e) {} }
            async function loadSurprise() { try { const res = await fetch('/api/random'); const data = await res.json(); if(data.movie) { currentViewMovies = [data.movie]; openDetail(0); } else { tg.showAlert("⚠️ ডাটাবেসে কোনো মুভি নেই!"); } } catch(e) {} }
            document.getElementById('searchInput').addEventListener('focus', function() { document.querySelector('.nav-item:nth-child(2)').click(); setTimeout(function() { document.getElementById('searchInputMain').focus(); }, 100); });
            
            // ⚙️ অ্যাড সেটিং লোড (ad_count / ad_wait অ্যাডমিন /setadcount ও /setadwait দিয়ে বদলাতে পারে)
            async function loadAdSettings() { try { const res = await fetch('/api/settings/public'); const s = await res.json(); adSettings.ad_count = Math.max(1, s.ad_count || 1); adSettings.ad_wait = Math.max(5, s.ad_wait || 10); } catch(e) {} }
            
            // 🔗 ডিপ-লিংক: ব্রডকাস্ট/লগ পোস্টের Watch Now বাটনে ?open=<movie_id> বা ?startapp=<movie_id> থাকে —
            // 🎯 নতুন-পুরাতন সব ইউজারই প্রথমে "ফাইলটি প্রস্তুত!" মেসেজ দেখবে; Watch Now চাপলে
            // একবার অ্যাক্টিভ স্পন্সর লিংক খুলে (ইনকাম), ফিরে এসে আবার চাপলে সরাসরি ফাইলের পেজে যাবে।
            let gateMovie = null; let gateStep = 0;
            async function openFromDeepLink() {
                try {
                    let openId = new URLSearchParams(window.location.search).get('open') || (tg.initDataUnsafe && tg.initDataUnsafe.start_param) || '';
                    if (!openId) return;
                    const res = await fetch('/api/movie/' + openId + '?uid=' + uid);
                    const data = await res.json();
                    if (data && data.movie) {
                        currentViewMovies = [data.movie];
                        showFileGate(data.movie);
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
                    // 🎯 টাইটেলে "18+" থাকলে অ্যাডাল্ট লিংক পুল, নাহলে নরমাল লিংক পুল;
                    // 🎲 র‍্যান্ডম: প্রতিবার Watch Now চাপলেই পুল থেকে র‍্যান্ডম লিংক খুলবে (সেইম লিংক পরপর দুইবার যাবে না);
                    // অ্যাড থেকে ফিরে এসে "ফাইলটি দেখুন" চাপলে মেইন ফাইল পেজে যাবে
                    let isAdult = gateMovie && (gateMovie.is_adult || (gateMovie.title && gateMovie.title.toUpperCase().indexOf('18+') !== -1));
                    let link = pickRandomLink(isAdult);
                    if (link) tg.openLink(link);
                    gateStep = 1;
                    btn.innerText = '✅ ফাইলটি দেখুন';
                } else {
                    closeModal('fileGateModal');
                    // 🔧 ফিক্স: ডিপ-লিংকের গেট খোলার পর ব্যাকগ্রাউন্ডে হোম ফিড লোড হয়ে currentViewMovies
                    // ওভাররাইট হয়ে যায় — ফলে openDetail(0) সবসময় লেটেস্ট মুভি খুলে ফেলত!
                    // এখন গেটে যে মুভিটা দেখানো হয়েছিল (gateMovie) — সেটাই খুলবে
                    if (gateMovie) { currentViewMovies = [gateMovie]; openDetail(0); }
                }
            }
            
            // ⚙️ অ্যাড সেটিং লোড (ad_count / ad_wait / অ্যাক্টিভ লিংক — অ্যাডমিন কমান্ডে বদলালে ৫ মিনিটে ধরা পড়ে)
            async function loadAdSettings() { try { const res = await fetch('/api/settings/public'); const s = await res.json(); adSettings.ad_count = Math.max(1, s.ad_count || 1); adSettings.ad_wait = Math.max(5, s.ad_wait || 10); adSettings.active_link = s.active_link || ''; adSettings.active_adult_link = s.active_adult_link || ''; } catch(e) {} }
            
            // বুটস্ট্র্যাপ অর্ডার: আগে ডিপ-লিংক চেক (is_new ঠিক করার জন্য), তারপর ইউজার রেজিস্ট্রেশন ও লিস্ট লোড
            (async function(){ updateTopAdBar(); await openFromDeepLink(); loadAdSettings(); fetchUserInfo(); loadHomeMovies(1); loadFavorites(); loadTrending(); setInterval(loadAdSettings, 5*60*1000); })();
        </script>
    </body></html>'''
    
    html_code = html_code.replace("__DL_JSON__", dl_json)
    html_code = html_code.replace("__ADL_JSON__", adl_json)
    html_code = html_code.replace("__BOT_TOKEN__", TOKEN)
    html_code = html_code.replace("__BOT_USERNAME__", BOT_USERNAME)
    html_code = html_code.replace("__BANNER_AD__", BANNER_AD_JS)
    html_code = html_code.replace("__BANNER_320__", BANNER_AD_JS_320)
    html_code = html_code.replace("__BANNER_ADULT__", BANNER_AD_JS_ADULT)
    html_code = html_code.replace("__BANNER_320_ADULT__", BANNER_AD_JS_320_ADULT)
    # 💰 ফিড ব্যানার কনফিগ (JS দিয়ে iframe বানাতে)
    html_code = html_code.replace("__AD300_CONF__", json.dumps(BANNER_300_CONF))
    html_code = html_code.replace("__AD320_CONF__", json.dumps(BANNER_320_CONF))
    html_code = html_code.replace("__ADULT_CONF__", json.dumps(BANNER_ADULT_CONF))
    html_code = html_code.replace("__AD320_ADULT_CONF__", json.dumps(BANNER_320_ADULT_CONF))
    return HTMLResponse(html_code)

# ==========================================
# 10. Main Web App APIs
# ==========================================

# ✅ UptimeRobot এর জন্য Health Check Route
@app.get("/ping")
async def ping():
    return {"status": "ok"}

# ✅ প্রতি 14 মিনিটে নিজেকে পিং করার সিস্টেম
async def self_pinger():
    import asyncio
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                url = "https://bdviralbot.onrender.com/ping"
                async with session.get(url) as resp:
                    print(f"Self-ping successful: {resp.status}")
        except Exception as e:
            print(f"Self-ping failed: {e}")
        # 14 মিনিট পর পর পিং করবে (Render 15 মিনিটে স্লিপ করে)
        await asyncio.sleep(14 * 60) 
        
@app.get("/api/user/{uid}")
async def get_user_info(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user:
        # ইউজার না থাকলে ডিফল্ট রিটার্ন
        return {"vip": False, "is_admin": False}
    
    # ভিআইপি চেক
    is_vip = user.get("vip_until", datetime.datetime.min) > datetime.datetime.utcnow()
    
    # অ্যাডমিন চেক (Owner ID এর সাথে মিলিয়ে)
    is_admin = (uid in admin_cache)
    
    return {
        "vip": is_vip,
        "is_admin": is_admin
    }

# ✅ Pagination API Updated
@app.get("/api/list")
async def list_movies(page: int = 1, q: str = "", uid: int = 0, cat: str = "Home"):
    if uid in banned_cache: return {"movies": [], "total_pages": 0}
    limit = 20
    unlocked_ids = []
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}): unlocked_ids.append(u["movie_id"])
    match_stage = {}
    if q: match_stage["title"] = {"$regex": q, "$options": "i"}
    if cat and cat != "Home": match_stage["categories"] = {"$in": [cat]}
    
    total_unique_titles = len(await db.movies.distinct("title", match_stage))
    total_pages = math.ceil(total_unique_titles / limit)
    
    pipeline = [
        {"$match": match_stage}, 
        {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "clicks": {"$sum": "$clicks"}, "created_at": {"$max": "$created_at"}, "year": {"$first": "$year"}, "categories": {"$first": "$categories"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main"]}}}}}, 
        {"$sort": {"created_at": -1}}, {"$skip": (page - 1) * limit}, {"$limit": limit},
        {"$addFields": {"title": "$_id"}}  # <--- এই লাইনটি যোগ করা হয়েছে
    ]
    movies = await db.movies.aggregate(pipeline).to_list(limit)
    for m in movies:
        m["is_adult"] = is_adult_movie(m)
        # নিচের লাইনে movie_id কনভার্সন করা হয়েছে, এটি ঠিক আছে কিন্তু চেক করে দেখুন
        for f in m["files"]: 
            # ফাইল আনলক চেক
            f["is_unlocked"] = f["id"] in unlocked_ids
            
    return {"movies": movies, "total_pages": total_pages}

@app.get("/api/random")
async def get_random_movie():
    try:
        # ডাটাবেস থেকে র‍্যান্ডমলি ১টি মুভি আনা
        movie = await db.movies.aggregate([{"$sample": {"size": 1}}]).to_list(1)
        if movie:
            m = movie[0]
            m["_id"] = str(m["_id"]) # ObjectId কে স্ট্রিং-এ কনভার্ট করা
            return {"movie": m}
        return {"movie": None}
    except Exception as e:
        return {"movie": None}

# ✅ ডিপ-লিংক API: একটি নির্দিষ্ট মুভি (একই টাইটেলের সব ফাইলসহ) ওয়েব অ্যাপে সরাসরি খোলার জন্য
# is_new = ইউজার আগে কোনো ফাইল আনলক/ডাউনলোড করেনি → তাদের জন্য ওয়েব অ্যাপে Watch Now গেট দেখানো হয়
@app.get("/api/movie/{movie_id}")
async def get_single_movie(movie_id: str, uid: int = 0):
    try:
        unlocked_ids = []
        is_new = False
        if uid != 0:
            time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
            async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
                unlocked_ids.append(u["movie_id"])
            is_new = (await db.user_unlocks.find_one({"user_id": uid}) is None)
        movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
        if not movie: return {"movie": None, "is_new": False}
        # একই টাইটেলের সব ফাইল একত্র করা (লিস্ট API-এর গ্রুপ ফরম্যাটেই)
        pipeline = [
            {"$match": {"title": movie["title"]}},
            {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "clicks": {"$sum": "$clicks"}, "created_at": {"$max": "$created_at"}, "year": {"$first": "$year"}, "categories": {"$first": "$categories"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main"]}}}}}
        ]
        movies = await db.movies.aggregate(pipeline).to_list(1)
        if not movies: return {"movie": None}
        m = movies[0]; m["title"] = m["_id"]; m["is_adult"] = is_adult_movie(m)
        for f in m["files"]: f["is_unlocked"] = f["id"] in unlocked_ids
        return {"movie": m, "is_new": is_new}
    except Exception as e:
        return {"movie": None, "is_new": False}

# ✅ পাবলিক সেটিংস: ওয়েব অ্যাপ কতগুলো অ্যাড (ad_count) ও কত সেকেন্ড (ad_wait) দেখাবে সেটা জানার জন্য
@app.get("/api/settings/public")
async def get_public_settings():
    try:
        ac = await db.settings.find_one({"id": "ad_count"}); ad_count = ac.get("value", 1) if ac else 1
        aw = await db.settings.find_one({"id": "ad_wait"}); ad_wait = aw.get("value", 10) if aw else 10
        dl_cfg = await db.settings.find_one({"id": "direct_links"}); links = dl_cfg.get("links", []) if dl_cfg else []
        adl_cfg = await db.settings.find_one({"id": "adult_direct_links"}); alinks = adl_cfg.get("links", []) if adl_cfg else []
        # 🎲 র‍্যান্ডম সিস্টেম: প্রতি কলে পুল থেকে র‍্যান্ডম লিংক (ওয়েব অ্যাপ ক্লায়েন্ট-সাইডেও প্রতিবার ক্লিকে র‍্যান্ডম নেয়)
        return {"ad_count": max(1, int(ad_count or 1)), "ad_wait": max(5, int(ad_wait or 10)),
                "active_link": pick_random_link(links) or "", "active_adult_link": pick_random_link(alinks) or ""}
    except:
        return {"ad_count": 1, "ad_wait": 10, "active_link": "", "active_adult_link": ""}

@app.get("/api/image/{photo_id}")
async def get_image(photo_id: str):
    try:
        cache = await db.file_cache.find_one({"photo_id": photo_id})
        now = datetime.datetime.utcnow()
        if cache and cache.get("expires_at", now) > now: file_path = cache["file_path"]
        else:
            file_info = await bot.get_file(photo_id); file_path = file_info.file_path
            await db.file_cache.update_one({"photo_id": photo_id}, {"$set": {"file_path": file_path, "expires_at": now + datetime.timedelta(hours=1)}}, upsert=True)
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        return RedirectResponse(url=file_url)
    except: return RedirectResponse(url="https://via.placeholder.com/110x160")

# ✅ Rate Limiter / Queue System added to prevent Telegram Ban
send_semaphore = asyncio.Semaphore(20)

class SendRequestModel(BaseModel):
    userId: int; movieId: str; initData: str

# 📦 কমন ডেলিভারি হেল্পার: ওয়েব অ্যাপের /api/send এবং /start এর পেন্ডিং ডেলিভারি — দুই জায়গায় একই ক্যাপশন/লজিক
# রিটার্ন: (সফল হলো কিনা, ফেলের কারণ) — 'forbidden' মানে ইউজার বটকে এখনো START করেনি
async def deliver_movie_file(uid: int, m: dict):
    now = datetime.datetime.utcnow()
    user_data = await db.users.find_one({"user_id": uid})
    is_vip = user_data and user_data.get("vip_until", now) > now
    protect_cfg = await db.settings.find_one({"id": "protect_content"})
    is_protected = protect_cfg.get("status", False) if protect_cfg else False
    time_cfg = await db.settings.find_one({"id": "del_time"})
    del_minutes = time_cfg['minutes'] if time_cfg else 60
    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"

    base_caption = f"🎥 <b>{m['title']} [{m.get('quality', '')}]</b>\n\n📥 Join: {tg_link}"
    if is_vip:
        caption = base_caption + "\n\n💎 VIP সুবিধা: এই ফাইলটি কখনো ডিলিট হবে না!"
    else:
        caption = base_caption + f"\n\n⏳ সতর্কতা: সিকিউরিটির জন্য এই ভিডিওটি {del_minutes} মিনিট পর অটোমেটিক ডিলিট হয়ে যাবে!"

    try:
        sent_msg = None
        try:
            if m.get("file_type") == "video":
                sent_msg = await bot.send_video(uid, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            else:
                sent_msg = await bot.send_document(uid, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            if m.get("file_type") == "video":
                sent_msg = await bot.send_video(uid, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            else:
                sent_msg = await bot.send_document(uid, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
        if sent_msg:
            if not is_vip:
                delete_at = now + datetime.timedelta(minutes=del_minutes)
                await db.auto_delete.insert_one({"chat_id": uid, "message_id": sent_msg.message_id, "delete_at": delete_at})
            return True, None
        return False, "send_failed"
    except TelegramForbiddenError:
        # 🎯 বট নতুন ইউজারকে নিজে থেকে মেসেজ পাঠাতে পারে না (START করেননি) → পেন্ডিং হিসেবে সেভ হবে
        return False, "forbidden"
    except Exception as e:
        print(f"Deliver file error: {e}")
        return False, "error"

@app.post("/api/send")
async def send_file(d: SendRequestModel):
    if d.userId == 0 or d.userId in banned_cache or not validate_tg_data(d.initData): return {"ok": False}

    async with send_semaphore:
        try:
            m = await db.movies.find_one({"_id": ObjectId(d.movieId)})
            if not m:
                return {"ok": False, "msg": "Movie not found"}

            now = datetime.datetime.utcnow()
            ok, reason = await deliver_movie_file(d.userId, m)
            if ok:
                await db.movies.update_one({"_id": ObjectId(d.movieId)}, {"$inc": {"clicks": 1}})
                await db.user_unlocks.update_one({"user_id": d.userId, "movie_id": d.movieId}, {"$set": {"unlocked_at": now}}, upsert=True)
                return {"ok": True}
            if reason == "forbidden":
                # 🎯 নতুন ইউজার: অ্যাড দেখা শেষ করেছে কিন্তু বটকে START করেনি →
                # ফাইলটি পেন্ডিং সেভ হলো। "বটে যান" চেপে START করলেই এই ফাইল অটোমেটিক ডেলিভারি হবে।
                await db.pending_files.update_one(
                    {"user_id": d.userId},
                    {"$set": {"movie_id": d.movieId, "title": m.get("title", ""), "created_at": now}},
                    upsert=True)
                return {"ok": False, "new_user": True, "bot_username": BOT_USERNAME, "movie_id": d.movieId}
            return {"ok": False}

        except Exception as e:
            print(f"Send File Error: {e}")
            return {"ok": False, "msg": "Server error"}

@app.get("/api/favs/{uid}")
async def get_favs(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user: return []
    fav_titles = user.get("favorites", [])
    if not fav_titles: return []
    pipeline = [{"$match": {"title": {"$in": fav_titles}}}, {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "clicks": {"$sum": "$clicks"}, "year": {"$first": "$year"}, "categories": {"$first": "$categories"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main"]}}}}}]
    movies = await db.movies.aggregate(pipeline).to_list(len(fav_titles))
    for m in movies: m["is_adult"] = is_adult_movie(m)
    return movies

class FavModel(BaseModel):
    uid: int; title: str; initData: str

@app.post("/api/fav/toggle")
async def toggle_fav(data: FavModel):
    if not validate_tg_data(data.initData): return {"isFav": False}
    user = await db.users.find_one({"user_id": data.uid})
    favs = user.get("favorites", []) if user else []
    if data.title in favs: await db.users.update_one({"user_id": data.uid}, {"$pull": {"favorites": data.title}}); return {"isFav": False}
    else: await db.users.update_one({"user_id": data.uid}, {"$push": {"favorites": data.title}}); return {"isFav": True}

class PaymentModel(BaseModel):
    uid: int; method: str; trx_id: str; days: int; price: int; initData: str

@app.post("/api/payment/submit")
async def submit_payment(data: PaymentModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    if await db.payments.find_one({"trx_id": data.trx_id}): return {"ok": False, "msg": "TrxID used!"}
    res = await db.payments.insert_one({"user_id": data.uid, "method": data.method, "trx_id": data.trx_id, "amount": data.price, "days": data.days, "status": "pending"})
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Approve", callback_data=f"trx_approve_{res.inserted_id}")
        builder.button(text="❌ Reject", callback_data=f"trx_reject_{res.inserted_id}")
        await bot.send_message(OWNER_ID, f"💰 <b>Payment!</b>\n👤 <code>{data.uid}</code>\n🏦 {data.method.upper()}\n🧾 <code>{data.trx_id}</code>\n💵 {data.price} BDT", parse_mode="HTML", reply_markup=builder.as_markup())
    except: pass
    return {"ok": True}

# ==========================================
# 10.5 Web App Engagement APIs — ❤️ লাইক / 🗳️ রেটিং পোল / 📤 শেয়ার / 💾 সেভ লেটার
# সব কী মুভির টাইটেল দিয়ে — একই মুভির সব পার্ট একই লাইক/পোল শেয়ার করে
# ক্লায়েন্টের পাঠানো uid-এর ওপর ভরসা না করে initData থেকে আসল ইউজার বের করা হয়
# ==========================================
def tg_user_from_init(init_data: str):
    """🪪 initData থেকে (user_id, first_name) বের করা — ভুয়া uid দিয়ে স্প্যাম আটকাতে"""
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data or ""))
        u = json.loads(parsed.get("user", "{}"))
        return int(u.get("id") or 0), (u.get("first_name") or "User")
    except Exception:
        return 0, "User"

async def movie_title_by_id(movie_id: str):
    try:
        doc = await db.movies.find_one({"_id": ObjectId(movie_id)}, {"title": 1})
        return doc["title"] if doc else None
    except Exception:
        return None

# ✅ ডিটেইলস পেজ API: মুভি (গ্রুপ করা) + লাইক/ডাউনলোড/ভোট/শেয়ার স্ট্যাট + ইউজারের liked/saved অবস্থা
@app.get("/api/web/details/{movie_id}")
async def web_details(movie_id: str, uid: int = 0):
    try:
        movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
        if not movie: return {"movie": None}
        pipeline = [
            {"$match": {"title": movie["title"]}},
            {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "clicks": {"$sum": "$clicks"}, "created_at": {"$max": "$created_at"}, "year": {"$first": "$year"}, "categories": {"$first": "$categories"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main"]}}}}}
        ]
        rows = await db.movies.aggregate(pipeline).to_list(1)
        if not rows: return {"movie": None}
        m = rows[0]; m["title"] = m["_id"]; m["is_adult"] = is_adult_movie(m)
        for f in m["files"]: f["is_unlocked"] = False
        if uid != 0:
            time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
            unlocked_ids = []
            async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
                unlocked_ids.append(u["movie_id"])
            for f in m["files"]: f["is_unlocked"] = f["id"] in unlocked_ids
        title = m["_id"]
        ldoc = await db.movie_likes.find_one({"key": title})
        likes = len(ldoc.get("users", [])) if ldoc else 0
        liked = (uid != 0 and ldoc is not None and uid in ldoc.get("users", []))
        sdoc = await db.movie_shares.find_one({"key": title})
        shares = sdoc.get("count", 0) if sdoc else 0
        votes_count = await db.votes.count_documents({"title": title})
        saved = False
        if uid != 0:
            u = await db.users.find_one({"user_id": uid}, {"saved": 1})
            saved = bool(u and title in u.get("saved", []))
        return {"movie": m, "likes": likes, "liked": liked, "downloads": m.get("clicks", 0), "shares": shares, "votes_count": votes_count, "saved": saved}
    except Exception as e:
        print(f"web details error: {e}")
        return {"movie": None}

class MovieActionModel(BaseModel):
    movie_id: str; initData: str

@app.post("/api/web/like/toggle")
async def web_like_toggle(d: MovieActionModel):
    wuid, _ = tg_user_from_init(d.initData)
    if not wuid or not validate_tg_data(d.initData): return {"ok": False}
    title = await movie_title_by_id(d.movie_id)
    if not title: return {"ok": False}
    doc = await db.movie_likes.find_one({"key": title})
    liked = bool(doc and wuid in doc.get("users", []))
    if liked:
        await db.movie_likes.update_one({"key": title}, {"$pull": {"users": wuid}}, upsert=True)
        liked = False
    else:
        await db.movie_likes.update_one({"key": title}, {"$addToSet": {"users": wuid}}, upsert=True)
        liked = True
    doc = await db.movie_likes.find_one({"key": title})
    return {"ok": True, "liked": liked, "likes": len(doc.get("users", [])) if doc else 0}

@app.post("/api/web/share")
async def web_share(d: MovieActionModel):
    wuid, _ = tg_user_from_init(d.initData)
    if not wuid or not validate_tg_data(d.initData): return {"ok": False}
    title = await movie_title_by_id(d.movie_id)
    if not title: return {"ok": False}
    await db.movie_shares.update_one({"key": title}, {"$inc": {"count": 1}}, upsert=True)
    doc = await db.movie_shares.find_one({"key": title})
    return {"ok": True, "shares": doc.get("count", 0) if doc else 0}

# 🗳️ রেটিং পোল APIs — Must Watch / Good / Average / Worst (কমেন্ট সিস্টেম সম্পূর্ণ বাদ)
# নিয়ম: এক ইউজার এক ভোট — ভোট বদলাতে পারবে (আগেরটা সরে নতুনটা বসে), কিন্তু উঠাতে পারবে না
# (কোনো unvote/ডিলিট API-ই নেই)। সব কী মুভির টাইটেল দিয়ে — একই মুভির সব পার্ট একই পোল শেয়ার করে।
VOTE_OPTIONS = {"must": "Must Watch", "good": "Good", "avg": "Average", "worst": "Worst"}

def _poll_percent(counts):
    """দর্শক-পরামর্শ পার্সেন্ট = (Must Watch + Good) ÷ মোট ভোট × ১০০"""
    total = sum(counts.values())
    if not total: return 0
    return int(round((counts.get("must", 0) + counts.get("good", 0)) * 100 / total))

async def poll_snapshot(title: str, uid: int = 0):
    """পোলের হিসাব — কাউন্ট সবসময় votes কালেকশন থেকে লাইভ অ্যাগ্রিগেট (ভোট বদলালেও সবসময় সঠিক থাকে)"""
    counts = {k: 0 for k in VOTE_OPTIONS}
    async for row in db.votes.aggregate([{"$match": {"title": title}}, {"$group": {"_id": "$option", "n": {"$sum": 1}}}]):
        if row["_id"] in counts: counts[row["_id"]] = row["n"]
    my = None
    if uid:
        doc = await db.votes.find_one({"title": title, "uid": uid}, {"option": 1})
        my = doc.get("option") if doc else None
    return {"counts": counts, "total": sum(counts.values()), "percent": _poll_percent(counts), "user_vote": my}

class VoteModel(BaseModel):
    movie_id: str; option: str; initData: str

@app.post("/api/web/poll/vote")
async def web_poll_vote(d: VoteModel):
    """🗳 ভোট দেওয়া/বদলানো — ভুয়া uid স্প্যাম আটকাতে initData ভেরিফাই করা হয়।
    ইউজার আগে ভোট দিয়ে থাকলে $set দিয়ে নতুন অপশনে বদলে যাবে — উঠানোর (মুছে ফেলার) কোনো পথ নেই।"""
    wuid, wname = tg_user_from_init(d.initData)
    if not wuid or not validate_tg_data(d.initData): return {"ok": False}
    if d.option not in VOTE_OPTIONS: return {"ok": False}
    title = await movie_title_by_id(d.movie_id)
    if not title: return {"ok": False}
    now = datetime.datetime.utcnow()
    await db.votes.update_one(
        {"title": title, "uid": wuid},
        {"$set": {"option": d.option, "name": wname, "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True)
    snap = await poll_snapshot(title, wuid)
    return {"ok": True, **snap}

@app.get("/api/web/poll/{movie_id}")
async def web_poll(movie_id: str, uid: int = 0):
    title = await movie_title_by_id(movie_id)
    if not title: return {"ok": True, "counts": {}, "total": 0, "percent": 0, "user_vote": None}
    return {"ok": True, **(await poll_snapshot(title, uid))}

@app.post("/api/web/saved/toggle")
async def web_saved_toggle(d: MovieActionModel):
    wuid, _ = tg_user_from_init(d.initData)
    if not wuid or not validate_tg_data(d.initData): return {"ok": False}
    title = await movie_title_by_id(d.movie_id)
    if not title: return {"ok": False}
    user = await db.users.find_one({"user_id": wuid})
    saved = user.get("saved", []) if user else []
    if title in saved:
        await db.users.update_one({"user_id": wuid}, {"$pull": {"saved": title}})
        return {"ok": True, "saved": False}
    await db.users.update_one({"user_id": wuid}, {"$addToSet": {"saved": title}}, upsert=True)
    return {"ok": True, "saved": True}

@app.get("/api/web/saved/{uid}")
async def web_get_saved(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user: return []
    saved_titles = user.get("saved", [])
    if not saved_titles: return []
    pipeline = [{"$match": {"title": {"$in": saved_titles}}}, {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "clicks": {"$sum": "$clicks"}, "year": {"$first": "$year"}, "categories": {"$first": "$categories"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main"]}}}}}]
    movies = await db.movies.aggregate(pipeline).to_list(len(saved_titles))
    for m in movies: m["is_adult"] = is_adult_movie(m)
    return movies

# ==========================================
# 11. Main Application Startup
# ==========================================
async def start():
    await init_db()
    await load_admins()
    await load_banned_users()
    await seed_default_ads()
    await load_log_channel()
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    asyncio.create_task(auto_delete_worker())
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(server.serve(), dp.start_polling(bot))

if __name__ == "__main__": 
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start())
