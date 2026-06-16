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
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError

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
BOT_USERNAME = os.getenv("BOT_USERNAME", "bdlatestmovie_bot") 

LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "-1003708048942")

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
file_path_cache = {} 

CATEGORIES = ["Bangla", "Bangla Dubbed", "Hindi Dubbed", "Hollywood", "K-Drama", "Anime", "Horror", "Web Series", "Adult Content"]
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
    
    waiting_for_addq_title = State()
    waiting_for_addq_file = State()
    waiting_for_addq_quality = State()

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
    await db.payments.create_index("trx_id", unique=True)

async def migrate_old_movies():
    async for m in db.movies.find({"qualities": {"$exists": False}}):
        if m.get("file_id"):
            new_q = [{"label": m.get("quality", "Main"), "file_id": m["file_id"], "file_type": m.get("file_type", "video")}]
            await db.movies.update_one({"_id": m["_id"]}, {"$set": {"qualities": new_q}})

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
            expire_time = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
            result = await db.user_unlocks.delete_many({"unlocked_at": {"$lte": expire_time}})
            if result.deleted_count > 0:
                print(f"🔒 Auto-locked {result.deleted_count} movies.")
        except Exception as e:
            print(f"Auto-lock worker error: {e}")
        await asyncio.sleep(3600)

async def broadcast_queue_worker():
    while True:
        try:
            task_data = await broadcast_queue.get()
            await run_movie_broadcast(task_data['data'], task_data['selected_cats'], task_data['admin_id'])
            broadcast_queue.task_done()
        except Exception as e:
            print(f"Queue Worker Error: {e}")
            await asyncio.sleep(5)

@app.on_event("startup")
async def on_startup():
    await init_db()
    await load_admins()
    await load_banned_users()
    await migrate_old_movies()
    asyncio.create_task(auto_delete_worker())
    asyncio.create_task(broadcast_queue_worker())
    asyncio.create_task(auto_lock_worker())

# ==========================================
# 6. Telegram Bot Commands
# ==========================================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if uid in banned_cache:
        return await message.answer("🚫 আপনাকে ব্যান করা হয়েছে।", parse_mode="HTML")
        
    await state.clear()
    now = datetime.datetime.utcnow()
    user = await db.users.find_one({"user_id": uid})
    
    args = message.text.split(" ")
    if len(args) > 1:
        deep_link_param = args[1]
        if deep_link_param == "addmovie" and uid in admin_cache:
            return await message.answer("🎬 নতুন মুভি আপলোড করতে <b>ভিডিও/ফাইল</b> পাঠান।", parse_mode="HTML")
        elif deep_link_param == "addquality" and uid in admin_cache:
            await state.set_state(AdminStates.waiting_for_addq_title)
            return await message.answer("📝 যে মুভিতে নতুন কোয়ালিটি যোগ করতে চান তার <b>নাম</b> লিখুন:", parse_mode="HTML")
        elif deep_link_param.startswith("ref_"):
            if not user:
                try:
                    referrer_id = int(deep_link_param.split("_")[1])
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

    if not user:
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
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    text = f"👋 <b>স্বাগতম {message.from_user.first_name}!</b>\n\n🎬 Movie Box জগতে আপনাকে স্বাগতম। নিচের বাটনে ক্লিক করে মুভি উপভোগ করুন।"
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

@dp.message(lambda m: m.chat.type == "private" and m.from_user.id not in admin_cache)
async def handle_user_messages(m: types.Message):
    if m.content_type not in ['text']:
        await m.answer("⚠️ দুঃখিত! আমি শুধুমাত্র টেক্সট মেসেজ গ্রহণ করি।", parse_mode="HTML")
        return
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ রিপ্লাই", callback_data=f"reply_{m.from_user.id}")
        await bot.send_message(OWNER_ID, f"📩 <a href='tg://user?id={m.from_user.id}'>{m.from_user.first_name}</a>:\n\n{m.text}", parse_mode="HTML", reply_markup=builder.as_markup())
    except: pass

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
        await db.settings.update_one({"id": "ad_count"}, {"$set": {"count": count}}, upsert=True)
        await m.answer(f"✅ অ্যাড সংখ্যা <b>{count}</b> এ সেট করা হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /setadcount 2", parse_mode="HTML")

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
        await m.answer("✅ ১৮+ অ্যাড লিংক অ্যাড হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /addadultlink url", parse_mode="HTML")

@dp.message(Command("settg"))
async def set_tg_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "tg_link"}, {"$set": {"url": link}}, upsert=True)
        await m.answer("✅ টেলিগ্রাম চ্যানেল লিংক আপডেট হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /settg https://t.me/...", parse_mode="HTML")

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

@dp.message(Command("addquality"))
async def add_quality_start(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_addq_title)
    await m.answer("📝 যে মুভিতে নতুন কোয়ালিটি যোগ করতে চান তার <b>নাম</b> লিখুন:", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_addq_title, F.text)
async def addq_title(m: types.Message, state: FSMContext):
    movie = await db.movies.find_one({"title": {"$regex": m.text.strip(), "$options": "i"}})
    if not movie:
        await state.clear()
        return await m.answer("⚠️ এই নামের কোনো মুভি পাওয়া যায়নি!", parse_mode="HTML")
    await state.update_data(movie_id=str(movie["_id"]), movie_title=movie["title"])
    await state.set_state(AdminStates.waiting_for_addq_file)
    await m.answer(f"✅ মুভি পাওয়া গেছে: <b>{movie['title']}</b>!\nএবার নতুন <b>ভিডিও/ফাইল</b> পাঠান।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_addq_file, F.content_type.in_({'video', 'document'}))
async def addq_file(m: types.Message, state: FSMContext):
    fid = m.video.file_id if m.video else m.document.file_id
    ftype = "video" if m.video else "document"
    await state.update_data(file_id=fid, file_type=ftype)
    await state.set_state(AdminStates.waiting_for_addq_quality)
    await m.answer("✅ এবার এই ফাইলের <b>কোয়ালিটি</b> লিখুন (যেমন: 1080p):", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_addq_quality, F.text)
async def addq_quality(m: types.Message, state: FSMContext):
    data = await state.get_data()
    movie_id = data['movie_id']
    new_q = {"label": m.text.strip(), "file_id": data['file_id'], "file_type": data['file_type']}
    
    await db.movies.update_one(
        {"_id": ObjectId(movie_id)},
        {"$push": {"qualities": new_q}}
    )
    await state.clear()
    await m.answer(f"✅ <b>{data['movie_title']}</b> এর নতুন কোয়ালিটি <b>{m.text.strip()}</b> যোগ করা হয়েছে!\n\n(কোনো ব্রডকাস্ট পাঠানো হয়নি।)", parse_mode="HTML")

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
    await m.answer("✅ ফাইল পেয়েছি! এবার <b>পোস্টার</b> পাঠান।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_photo, F.photo)
async def receive_movie_photo(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_title)
    await m.answer("✅ এবার <b>মুভি/সিরিজের নাম</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_photo)
async def fallback_photo(m: types.Message):
    await m.answer("⚠️ পোস্টার হিসেবে শুধুমাত্র <b>ছবি (Photo)</b> পাঠান।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer("✅ এবার <b>এপিসোড বা কোয়ালিটি</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title)
async def fallback_title(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>মুভির নাম (টেক্সট)</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=m.text.strip())
    await state.set_state(AdminStates.waiting_for_year)
    await m.answer("✅ এবার <b>রিলিজ সাল</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality)
async def fallback_quality(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>কোয়ালিটি (টেক্সট)</b> লিখুন।", parse_mode="HTML")

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
    await m.answer("⚠️ দয়া করে <b>রিলিজ সাল (টেক্সট)</b> লিখুন।", parse_mode="HTML")

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
    builder.adjust(3)
    await c.message.edit_reply_markup(reply_markup=builder.as_markup())
    await c.answer()

@dp.callback_query(AdminStates.waiting_for_cats, F.data == "cats_done")
async def finish_category_selection(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    if not selected_cats: return await c.answer("⚠️ অন্তত ১টি সিলেক্ট করুন!", show_alert=True)
    await state.clear()
    
    qualities_list = [{"label": data["quality"], "file_id": data["file_id"], "file_type": data["file_type"]}]
    
    await db.movies.insert_one({
        "title": data["title"], 
        "quality": data["quality"], 
        "photo_id": data["photo_id"], 
        "qualities": qualities_list, 
        "year": data.get("year", "N/A"), 
        "categories": selected_cats, 
        "clicks": 0, 
        "created_at": datetime.datetime.utcnow()
    })
    
    await c.message.edit_text(f"🎉 <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!", parse_mode="HTML")
    
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"
    if LOG_CHANNEL_ID:
        try:
            log_kb = [
                [types.InlineKeyboardButton(text="🎬 Watch Now", url="https://t.me/MovieeBoxx_Bot?start=new")],
                [types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)]
            ]
            log_markup = types.InlineKeyboardMarkup(inline_keyboard=log_kb)
            log_text = f"🎬 <b>New Movie Uploaded</b>\n\n🏷 Title: <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n📅 Year: <b>{data.get('year', 'N/A')}</b>"
            await bot.send_photo(LOG_CHANNEL_ID, photo=data["photo_id"], caption=log_text, parse_mode="HTML", reply_markup=log_markup)
        except: pass

    await broadcast_queue.put({"data": data, "selected_cats": selected_cats, "admin_id": c.from_user.id})
    await c.answer()

async def run_movie_broadcast(data, selected_cats, admin_id):
    bcast_success = 0
    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"
    web_app_url = APP_URL if APP_URL else "https://t.me/" 
    bcast_kb = [
        [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=web_app_url))],
        [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link)],
        [types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)]
    ]
    bcast_markup = types.InlineKeyboardMarkup(inline_keyboard=bcast_kb)
    bcast_text = f"🆕 <b>New Movie Alert!</b>\n\n🎬 <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n\n👇 এখনই দেখুন!"
    
    now = datetime.datetime.utcnow()
    delete_at = now + datetime.timedelta(days=1) 
    
    async for u in db.users.find():
        try:
            sent_msg = await bot.send_photo(u['user_id'], photo=data["photo_id"], caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
            await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at})
            bcast_success += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                sent_msg = await bot.send_photo(u['user_id'], photo=data["photo_id"], caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
                await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at})
                bcast_success += 1
            except: pass
        except: pass
        
    try:
        await bot.send_message(admin_id, f"✅ <b>{data['title']}</b> এর ব্রডকাস্ট শেষ!\n\nসফলভাবে পাঠানো হয়েছে: <b>{bcast_success}</b> জনকে।", parse_mode="HTML")
    except: pass

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_bcast)
    await m.answer("📢 ব্রডকাস্ট মেসেজ পাঠান।\n\n⚠️ বাতিল করতে /cancel লিখুন।", parse_mode="HTML")

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
# 8. APIs for Web App File Serving & Downloads
# ==========================================
@app.get("/api/poster/{file_id:path}")
async def get_poster(file_id: str):
    if file_id in file_path_cache:
        return RedirectResponse(f"https://api.telegram.org/file/bot{TOKEN}/{file_path_cache[file_id]}")
    try:
        file_info = await bot.get_file(file_id)
        file_path = file_info.file_path
        file_path_cache[file_id] = file_path
        return RedirectResponse(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}")
    except:
        return RedirectResponse("https://via.placeholder.com/320x180?text=No+Poster")

# New Download API - Sends file directly to user's Telegram DM
@app.post("/api/download")
async def download_file(request: Request):
    data = await request.json()
    init_data = data.get("init_data")
    file_id = data.get("file_id")
    file_type = data.get("file_type", "video")
    
    if not validate_tg_data(init_data):
        return {"ok": False, "error": "Invalid user"}
    
    parsed_data = dict(urllib.parse.parse_qsl(init_data))
    user_id = int(parsed_data.get('user_id', 0))
    
    if user_id in banned_cache:
        return {"ok": False, "error": "Banned"}
    
    try:
        if file_type == "video":
            await bot.send_video(chat_id=user_id, video=file_id, caption="🎬 এখানে আপনার মুভি ফাইল। উপভোগ করুন!")
        else:
            await bot.send_document(chat_id=user_id, document=file_id, caption="🎬 এখানে আপনার ফাইল। উপভোগ করুন!")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ==========================================
# 9. Web Admin Panel API & UI
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
            body { font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif; background: #0f172a; color: #cbd5e1; margin: 0; padding: 20px; }
            .header { text-align: center; margin-bottom: 30px; color: #fff; }
            .header h1 { margin: 0; font-size: 28px; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 40px; }
            .stat-card { background: #1e293b; padding: 20px; border-radius: 16px; border: 1px solid #334155; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
            .stat-card h3 { margin: 0 0 10px 0; font-size: 14px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }
            .stat-card .value { font-size: 32px; font-weight: 800; color: #fff; }
            .btn-group { display: flex; gap: 15px; margin-bottom: 30px; flex-wrap: wrap; }
            .admin-btn { padding: 15px 25px; border-radius: 12px; font-weight: 800; border: none; color: white; text-decoration: none; font-size: 16px; cursor: pointer; box-shadow: 0 4px 6px rgba(0,0,0,0.2); transition: 0.2s; }
            .admin-btn:hover { transform: translateY(-2px); }
            .btn-add-new { background: linear-gradient(45deg, #10b981, #059669); }
            .btn-add-quality { background: linear-gradient(45deg, #3b82f6, #2563eb); }
            .table-container { background: #1e293b; border-radius: 16px; border: 1px solid #334155; overflow-x: auto; }
            .table-header { padding: 20px; border-bottom: 1px solid #334155; display: flex; justify-content: space-between; align-items: center; }
            .table-header h2 { margin: 0; color: #fff; font-size: 20px; }
            table { width: 100%; border-collapse: collapse; min-width: 600px; } th { text-align: left; padding: 15px; color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid #334155; } td { padding: 15px; border-bottom: 1px solid #334155; font-size: 14px; color: #e2e8f0; } tr:last-child td { border-bottom: none; } tr:hover { background: rgba(255,255,255,0.03); }
            .view-badge { background: rgba(59, 130, 246, 0.2); color: #60a5fa; padding: 4px 10px; border-radius: 12px; font-weight: 600; font-size: 12px; }
            .delete-btn { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); padding: 6px 12px; border-radius: 8px; cursor: pointer; font-weight: 600; transition: 0.2s; } .delete-btn:hover { background: #ef4444; color: white; }
        </style>
    </head>
    <body>
        <div class="header"><h1><i class="fa-solid fa-shield-halved"></i> Admin Panel</h1><p>Movie Box Control Center</p></div>
        <div class="btn-group">
            <a href="https://t.me/__BOT_USERNAME__?start=addmovie" target="_blank" class="admin-btn btn-add-new"><i class="fa-solid fa-plus"></i> Add New Movie</a>
            <a href="https://t.me/__BOT_USERNAME__?start=addquality" target="_blank" class="admin-btn btn-add-quality"><i class="fa-solid fa-layer-group"></i> Add Quality</a>
        </div>
        <div class="stats-grid">
            <div class="stat-card"><h3>Total Users</h3><div class="value"><i class="fa-solid fa-users" style="color:#3b82f6"></i> <span id="totalUsers">0</span></div></div>
            <div class="stat-card"><h3>Today Users</h3><div class="value"><i class="fa-solid fa-user-plus" style="color:#10b981"></i> <span id="todayUsers">0</span></div></div>
            <div class="stat-card"><h3>Total Clicks</h3><div class="value"><i class="fa-solid fa-eye" style="color:#f59e0b"></i> <span id="totalClicks">0</span></div></div>
            <div class="stat-card"><h3>Today Clicks</h3><div class="value"><i class="fa-solid fa-chart-line" style="color:#ef4444"></i> <span id="todayClicks">0</span></div></div>
        </div>
        <div class="table-container"><div class="table-header"><h2><i class="fa-solid fa-film"></i> Uploaded Movies</h2></div><table><thead><tr><th>Poster</th><th>Title</th><th>Qualities</th><th>Views</th><th>Action</th></tr></thead><tbody id="movieTableBody"><tr><td colspan="5" style="text-align:center; padding:40px;">Loading data...</td></tr></tbody></table></div>
        
        <script>
            async function fetchStats() { try { const res = await fetch('/api/admin/stats'); const data = await res.json(); document.getElementById('totalUsers').innerText = data.total_users; document.getElementById('todayUsers').innerText = data.today_users; document.getElementById('totalClicks').innerText = data.total_clicks; document.getElementById('todayClicks').innerText = data.today_clicks; } catch(e) {} }
            async function fetchMovies() { try { const res = await fetch('/api/admin/movies'); const movies = await res.json(); const tbody = document.getElementById('movieTableBody'); if(movies.length === 0) { tbody.innerHTML = '<tr><td colspan="5" style="text-align:center">No movies yet.</td></tr>'; return; } tbody.innerHTML = movies.map(m => `<tr id="row-${m._id}"><td><img src="/api/poster/${m.photo_id}" style="width: 80px; height: 45px; object-fit: cover; border-radius: 6px;"></td><td><strong>${m.title}</strong><br><small>${m.year || 'N/A'}</small></td><td>${(m.qualities || []).map(q => '<span class="view-badge">' + q.label + '</span>').join(' ')}</td><td><span class="view-badge"><i class="fa-solid fa-eye"></i> ${m.clicks || 0}</span></td><td><button class="delete-btn" onclick="deleteMovie('${m._id}')"><i class="fa-solid fa-trash"></i> Delete</button></td></tr>`).join(''); } catch(e) {} }
            async function deleteMovie(id) { if(!confirm("Delete this file?")) return; try { const res = await fetch(`/api/admin/movie/${id}`, { method: 'DELETE' }); const data = await res.json(); if(data.ok) { document.getElementById(`row-${id}`).remove(); fetchStats(); } } catch(e) {} }
            fetchStats(); fetchMovies(); setInterval(fetchStats, 60000);
        </script>
    </body></html>'''
    html_code = html_code.replace("__BOT_USERNAME__", BOT_USERNAME)
    return HTMLResponse(html_code)

@app.get("/api/admin/stats")
async def admin_stats(auth: bool = Depends(verify_admin)):
    now = datetime.datetime.utcnow(); today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total_users = await db.users.count_documents({}); today_users = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    total_clicks_res = await db.movies.aggregate([{"$group": {"_id": None, "total": {"$sum": "$clicks"}}}]).to_list(1); total_clicks = total_clicks_res[0]["total"] if total_clicks_res else 0
    today_clicks = await db.user_unlocks.count_documents({"unlocked_at": {"$gte": today_start}})
    return {"total_users": total_users, "today_users": today_users, "total_clicks": total_clicks, "today_clicks": today_clicks}

@app.get("/api/admin/movies")
async def admin_movies(auth: bool = Depends(verify_admin)):
    movies = await db.movies.find({}).sort("created_at", -1).to_list(1000)
    for m in movies: 
        m["_id"] = str(m["_id"])
        if "qualities" not in m: 
            m["qualities"] = [{"label": m.get("quality", "Main"), "file_id": m.get("file_id"), "file_type": m.get("file_type", "video")}]
    return movies

@app.delete("/api/admin/movie/{movie_id}")
async def delete_movie(movie_id: str, auth: bool = Depends(verify_admin)):
    result = await db.movies.delete_one({"_id": ObjectId(movie_id)})
    if result.deleted_count == 1: return {"ok": True}
    raise HTTPException(status_code=404, detail="Movie not found")

# ==========================================
# 10. Main Web App UI
# ==========================================
@app.get("/api/trending")
async def get_trending():
    seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    movies = await db.movies.find({"created_at": {"$gte": seven_days_ago}}).sort("clicks", -1).limit(5).to_list(5)
    for m in movies:
        m["_id"] = str(m["_id"])
        if "qualities" not in m: 
            m["qualities"] = [{"label": m.get("quality", "Main"), "file_id": m.get("file_id"), "file_type": m.get("file_type", "video")}]
    return movies

@app.get("/api/movies")
async def api_movies():
    movies = await db.movies.find({}).sort("created_at", -1).to_list(1000)
    for m in movies:
        m["_id"] = str(m["_id"])
        if "qualities" not in m: 
            m["qualities"] = [{"label": m.get("quality", "Main"), "file_id": m.get("file_id"), "file_type": m.get("file_type", "video")}]
    return movies

@app.get("/", response_class=HTMLResponse)
async def web_ui():
    dl_cfg = await db.settings.find_one({"id": "direct_links"}); direct_links = dl_cfg.get('links', []) if dl_cfg else []
    adl_cfg = await db.settings.find_one({"id": "adult_direct_links"}); adult_direct_links = adl_cfg.get('links', []) if adl_cfg else []
    
    dl_json = json.dumps(direct_links).replace("'", "\\'")
    adl_json = json.dumps(adult_direct_links).replace("'", "\\'")

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
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { background: #0f172a; font-family: "Inter", system-ui, sans-serif; color: #fff; overscroll-behavior-y: none; } 
            #welcomeScreen { position: fixed; top:0; left:0; width:100%; height:100%; background: #0f172a; z-index: 99999; display: flex; flex-direction: column; align-items: center; justify-content: center; transition: opacity 0.8s ease; }
            #welcomeScreen.hide { opacity: 0; visibility: hidden; }
            .ws-brand { font-size: 48px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; animation: pulse 1.5s infinite; }
            @keyframes pulse { 0% { transform: scale(1); } 50% { transform: scale(1.05); } 100% { transform: scale(1); } }
            header { display: flex; justify-content: center; padding: 15px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); z-index: 1000; cursor: pointer; }
            .logo { font-size: 24px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .page-section { display: none; padding-bottom: 80px; }
            .page-section.active { display: block; }
            .cat-row { display: flex; flex-wrap: wrap; gap: 8px; padding: 15px; }
            .cat-chip { background: #1e293b; padding: 8px 16px; border-radius: 20px; white-space: nowrap; cursor: pointer; border: 1px solid #ef4444; font-weight: 600; font-size: 12px; transition: 0.3s; color: #cbd5e1; }
            .cat-chip.active { background: linear-gradient(45deg, #ef4444, #dc2626); border-color: #ef4444; color: white; box-shadow: 0 0 12px rgba(239, 68, 68, 0.4); }
            
            .trending-section { margin: 0 0 15px 0; }
            .trending-title { padding: 0 15px 10px; color: #ef4444; font-size: 18px; font-weight: 800; display: flex; align-items: center; gap: 8px; }
            .carousel-container { position: relative; overflow: hidden; border-radius: 12px; margin: 0 15px; }
            .carousel-track { display: flex; transition: transform 0.5s ease-in-out; }
            .carousel-slide { min-width: 100%; box-sizing: border-box; position: relative; cursor: pointer; }
            .carousel-slide img { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; border-radius: 12px; }
            .carousel-caption { position: absolute; bottom: 0; left: 0; right: 0; background: linear-gradient(transparent, rgba(0,0,0,0.9)); padding: 30px 15px 15px; border-radius: 0 0 12px 12px; }
            .carousel-caption h3 { font-size: 18px; margin-bottom: 5px; }
            .carousel-caption p { font-size: 12px; color: #94a3b8; }
            .carousel-dots { display: flex; justify-content: center; gap: 6px; margin-top: 10px; }
            .carousel-dot { width: 8px; height: 8px; border-radius: 50%; background: #334155; cursor: pointer; transition: 0.3s; }
            .carousel-dot.active { background: #ef4444; width: 20px; border-radius: 4px; }

            .movie-list { display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; padding: 0 15px; }
            .movie-card { display: flex; flex-direction: column; background: rgba(30, 41, 59, 0.6); border-radius: 12px; overflow: hidden; border: 1px solid #334155; cursor: pointer; transition: 0.3s; position: relative; }
            .movie-card:active { transform: scale(0.98); }
            .movie-card img { width: 100%; aspect-ratio: 16/9; object-fit: cover; }
            .movie-info { padding: 10px; display: flex; flex-direction: column; justify-content: center; flex: 1; }
            .movie-title { font-size: 14px; font-weight: 700; margin-bottom: 5px; line-height: 1.2; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
            .movie-meta { font-size: 11px; color: #94a3b8; margin-bottom: 8px; display: flex; gap: 10px; }
            .movie-cats { display: flex; flex-wrap: wrap; gap: 5px; }
            .movie-cat-tag { background: rgba(255,255,255,0.1); padding: 2px 6px; border-radius: 4px; font-size: 9px; font-weight: 600; color: #cbd5e1; }
            
            .bottom-nav { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); border-top: 1px solid #1e293b; display: flex; justify-content: space-around; padding: 10px 0; z-index: 1000; }
            .nav-item { display: flex; flex-direction: column; align-items: center; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer; border: none; background: none; }
            .nav-item i { font-size: 20px; margin-bottom: 3px; }
            .nav-item.active { color: #ef4444; }
            
            .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: flex-end; justify-content: center; z-index: 3000; }
            .modal-content { background: #1e293b; width: 100%; max-width: 400px; padding: 25px; border-radius: 20px 20px 0 0; max-height: 90vh; overflow-y: auto; position: relative; }
            .detail-img { width: 100%; aspect-ratio: 16/9; object-fit: cover; border-radius: 12px; margin-bottom: 15px; }
            .detail-title { font-size: 22px; font-weight: 800; margin-bottom: 5px; }
            .detail-meta { color: #94a3b8; font-size: 14px; margin-bottom: 15px; }
            .close-icon { position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: rgba(0,0,0,0.6); color: #fff; font-size: 18px; display: flex; align-items: center; justify-content: center; cursor: pointer; border: none; }
            .dl-file-btn { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 15px; background: #0f172a; border: 1px solid #334155; color: white; font-weight: 700; border-radius: 10px; margin-bottom: 10px; cursor: pointer; }
            .dl-file-btn i { color: #ef4444; font-size: 18px; }
            
            .ad-box { text-align: center; padding: 20px; }
            .ad-icon { font-size: 50px; margin-bottom: 10px; color: #fbbf24; }
            .ad-title { color: #fbbf24; font-size: 18px; font-weight: 800; margin-bottom: 15px; }
            .ad-action-btn { width: 100%; padding: 15px; border-radius: 8px; font-weight: 700; border: none; font-size: 16px; cursor: pointer; margin-bottom: 10px; }
            .btn-ad-unlock { background: #10b981; color: white; }
            .btn-ad-tryagain { background: #ef4444; color: white; opacity: 0.6; cursor: not-allowed; }
            
            .pagination-container { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 20px 15px 80px 15px; }
            .page-btn { background: #1e293b; color: #cbd5e1; border: 1px solid #334155; padding: 10px 15px; border-radius: 10px; font-weight: 700; cursor: pointer; transition: 0.2s; font-size: 14px; }
            .page-btn.active { background: linear-gradient(45deg, #ef4444, #dc2626); color: white; border-color: #ef4444; }
            .page-btn:disabled { background: #1e293b; color: #475569; cursor: not-allowed; border-color: #1e293b; }
        </style>
    </head>
    <body>
        <div id="welcomeScreen"><div class="ws-brand">Movie Box</div></div>
        <header onclick="switchTab('home')"><div class="logo">Movie Box</div></header>

        <div id="tabHome" class="page-section active">
            <div class="cat-row" id="catRow"></div>
            <div id="trendingSection" class="trending-section" style="display:none;">
                <div class="trending-title"><i class="fa-solid fa-fire"></i> Trending Now</div>
                <div class="carousel-container"><div class="carousel-track" id="trendingTrack"></div></div>
                <div class="carousel-dots" id="trendingDots"></div>
            </div>
            <div class="movie-list" id="movieList"></div>
            <div class="pagination-container" id="pagination"></div>
        </div>

        <div id="movieModal" class="modal">
            <div class="modal-content">
                <button class="close-icon" onclick="closeModal('movieModal')"><i class="fa-solid fa-xmark"></i></button>
                <img id="detailImg" class="detail-img" src="">
                <h2 id="detailTitle" class="detail-title"></h2>
                <p id="detailMeta" class="detail-meta"></p>
                <div id="detailQualities"></div>
            </div>
        </div>

        <div id="adModal" class="modal">
            <div class="modal-content">
                <button class="close-icon" onclick="closeModal('adModal')"><i class="fa-solid fa-xmark"></i></button>
                <div class="ad-box">
                    <div class="ad-icon"><i class="fa-solid fa-ad"></i></div>
                    <div class="ad-title">বিজ্ঞাপন দেখুন</div>
                    <p style="color:#94a3b8; margin-bottom:15px; font-size:14px;">ডাউনলোড করতে হলে অন্তত ১০ সেকেন্ড বিজ্ঞাপন দেখুন।</p>
                    <button id="adActionBtn" class="ad-action-btn btn-ad-tryagain" onclick="handleAdAction()">ডাউনলোড (10s)</button>
                </div>
            </div>
        </div>

        <div class="bottom-nav">
            <button class="nav-item active" onclick="switchTab('home')"><i class="fa-solid fa-house"></i>Home</button>
            <button class="nav-item" onclick="switchTab('search')"><i class="fa-solid fa-magnifying-glass"></i>Search</button>
            <button class="nav-item" onclick="switchTab('surprise')"><i class="fa-solid fa-shuffle"></i>Surprise</button>
            <button class="nav-item" onclick="switchTab('profile')"><i class="fa-solid fa-user"></i>Profile</button>
        </div>

        <script>
            const directLinks = JSON.parse('__DL_JSON__');
            const adultDirectLinks = JSON.parse('__ADL_JSON__');
            let allMovies = [];
            let filteredMovies = [];
            let currentCat = 'All';
            let currentPage = 1;
            const moviesPerPage = 10;
            
            let adTimer = null;
            let adCountdown = 10;
            let currentDownloadFileId = null;
            let currentDownloadFileType = null;
            let isAdValid = false;
            
            let trendingSlideIndex = 0;
            let trendingInterval;

            const tg = window.Telegram && window.Telegram.WebApp;
            if(tg) { tg.expand(); tg.ready(); }
            setTimeout(() => document.getElementById('welcomeScreen').classList.add('hide'), 1500);

            async function fetchMovies() {
                try {
                    const res = await fetch('/api/movies'); 
                    allMovies = await res.json();
                    filteredMovies = allMovies;
                    renderCategories();
                    renderMovies();
                    fetchTrending();
                } catch(e) { console.error(e); }
            }

            async function fetchTrending() {
                try {
                    const res = await fetch('/api/trending');
                    const trending = await res.json();
                    if(trending.length > 0) {
                        document.getElementById('trendingSection').style.display = 'block';
                        renderTrending(trending);
                    }
                } catch(e) {}
            }

            function renderTrending(movies) {
                const track = document.getElementById('trendingTrack');
                const dots = document.getElementById('trendingDots');
                track.innerHTML = movies.map(m => `
                    <div class="carousel-slide" onclick="showMovieDetail('${m._id}')">
                        <img src="/api/poster/${m.photo_id}" alt="${m.title}">
                        <div class="carousel-caption">
                            <h3>${m.title}</h3>
                            <p>${m.year || ''} | ${(m.qualities||[]).map(q=>q.label).join(', ')}</p>
                        </div>
                    </div>
                `).join('');
                
                dots.innerHTML = movies.map((_, i) => `<div class="carousel-dot ${i===0?'active':''}" onclick="goToSlide(${i})"></div>`).join('');
                
                clearInterval(trendingInterval);
                trendingInterval = setInterval(() => {
                    trendingSlideIndex = (trendingSlideIndex + 1) % movies.length;
                    goToSlide(trendingSlideIndex);
                }, 3000);
            }

            function goToSlide(index) {
                trendingSlideIndex = index;
                document.getElementById('trendingTrack').style.transform = `translateX(-${index * 100}%)`;
                document.querySelectorAll('.carousel-dot').forEach((d, i) => d.classList.toggle('active', i === index));
            }

            function renderCategories() {
                const cats = ['All', ...new Set(allMovies.flatMap(m => m.categories || []))];
                document.getElementById('catRow').innerHTML = cats.map(c => `<div class="cat-chip ${c===currentCat?'active':''}" onclick="filterCat('${c}')">${c}</div>`).join('');
            }

            function filterCat(cat) {
                currentCat = cat;
                currentPage = 1;
                filteredMovies = cat === 'All' ? allMovies : allMovies.filter(m => (m.categories||[]).includes(cat));
                renderCategories();
                renderMovies();
            }

            function renderMovies() {
                const start = (currentPage - 1) * moviesPerPage;
                const end = start + moviesPerPage;
                const pageMovies = filteredMovies.slice(start, end);
                
                document.getElementById('movieList').innerHTML = pageMovies.map(m => `
                    <div class="movie-card" onclick="showMovieDetail('${m._id}')">
                        <img src="/api/poster/${m.photo_id}" alt="${m.title}">
                        <div class="movie-info">
                            <div class="movie-title">${m.title}</div>
                            <div class="movie-meta"><span><i class="fa-solid fa-calendar"></i> ${m.year||'N/A'}</span></div>
                            <div class="movie-cats">${(m.qualities||[]).map(q=>`<span class="movie-cat-tag">${q.label}</span>`).join('')}</div>
                        </div>
                    </div>
                `).join('');
                
                renderPagination();
            }

            function renderPagination() {
                const totalPages = Math.ceil(filteredMovies.length / moviesPerPage);
                if(totalPages <= 1) { document.getElementById('pagination').innerHTML = ''; return; }
                let html = `<button class="page-btn" ${currentPage===1?'disabled':''} onclick="goToPage(${currentPage-1})">Prev</button>`;
                for(let i=1; i<=totalPages; i++) html += `<button class="page-btn ${i===currentPage?'active':''}" onclick="goToPage(${i})">${i}</button>`;
                html += `<button class="page-btn" ${currentPage===totalPages?'disabled':''} onclick="goToPage(${currentPage+1})">Next</button>`;
                document.getElementById('pagination').innerHTML = html;
            }

            function goToPage(p) { currentPage = p; renderMovies(); window.scrollTo(0,0); }

            function showMovieDetail(id) {
                const m = allMovies.find(x => x._id === id);
                if(!m) return;
                document.getElementById('detailImg').src = "/api/poster/" + m.photo_id;
                document.getElementById('detailTitle').innerText = m.title;
                document.getElementById('detailMeta').innerText = "Year: " + (m.year||'N/A') + " | Categories: " + (m.categories||[]).join(', ');
                
                const isAdult = (m.categories || []).includes("Adult Content");
                const qContainer = document.getElementById('detailQualities');
                qContainer.innerHTML = '';
                
                (m.qualities || []).forEach(q => {
                    const btn = document.createElement('button');
                    btn.className = 'dl-file-btn';
                    btn.innerHTML = `<span><i class="fa-solid fa-play"></i> ${q.label}</span> <i class="fa-solid fa-download"></i>`;
                    btn.onclick = () => showDownloadAd(q.file_id, q.file_type || "video", isAdult);
                    qContainer.appendChild(btn);
                });
                
                document.getElementById('movieModal').style.display = 'flex';
            }

            function showDownloadAd(fileId, fileType, isAdult) {
                currentDownloadFileId = fileId;
                currentDownloadFileType = fileType;
                isAdValid = false;
                adCountdown = 10;
                
                const adLinks = isAdult ? adultDirectLinks : directLinks;
                if(adLinks.length > 0) {
                    const randomAd = adLinks[Math.floor(Math.random() * adLinks.length)];
                    window.open(randomAd, '_blank');
                }
                
                document.getElementById('adModal').style.display = 'flex';
                const adBtn = document.getElementById('adActionBtn');
                adBtn.disabled = true;
                adBtn.innerText = "ডাউনলোড (10s)";
                adBtn.className = 'ad-action-btn btn-ad-tryagain';
                
                clearInterval(adTimer);
                adTimer = setInterval(() => {
                    adCountdown--;
                    if(adCountdown <= 0) {
                        clearInterval(adTimer);
                        isAdValid = true;
                        adBtn.disabled = false;
                        adBtn.innerText = "ডাউনলোড করুন ✅";
                        adBtn.className = 'ad-action-btn btn-ad-unlock';
                    } else {
                        adBtn.innerText = "ডাউনলোড (" + adCountdown + "s)";
                    }
                }, 1000);
            }

            async function handleAdAction() {
                if(!isAdValid) {
                    alert("আপনি এখনও সম্পূর্ণ ১০ সেকেন্ড বিজ্ঞাপন দেখেননি। অনুগ্রহ করে ১০ সেকেন্ড বিজ্ঞাপন দেখার পর Download করুন।");
                    return;
                }
                
                const initData = tg ? tg.initData : "";
                if(!initData) {
                    alert("ডাউনলোড করতে টেলিগ্রাম অ্যাপ থেকে খুলুন!");
                    return;
                }

                try {
                    const res = await fetch('/api/download', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            init_data: initData,
                            file_id: currentDownloadFileId,
                            file_type: currentDownloadFileType
                        })
                    });
                    const data = await res.json();
                    if(data.ok) {
                        alert("✅ ফাইলটি আপনার টেলিগ্রাম চ্যাটে পাঠানো হয়েছে!");
                        closeModal('adModal');
                    } else {
                        alert("❌ ফাইল পাঠাতে ব্যর্থ হয়েছে: " + data.error);
                    }
                } catch(e) {
                    alert("Network error!");
                }
            }

            function closeModal(id) { document.getElementById(id).style.display = 'none'; }
            
            function switchTab(tab) {
                document.querySelectorAll('.page-section').forEach(s => s.classList.remove('active'));
                document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
                
                if(tab === 'surprise') {
                    const randomIndex = Math.floor(Math.random() * allMovies.length);
                    showMovieDetail(allMovies[randomIndex]._id);
                    return;
                }
                
                document.getElementById('tab' + tab.charAt(0).toUpperCase() + tab.slice(1)).classList.add('active');
                event.currentTarget.classList.add('active');
            }

            fetchMovies();
        </script>
    </body></html>'''
    
    html_code = html_code.replace("__DL_JSON__", dl_json).replace("__ADL_JSON__", adl_json)
    return HTMLResponse(html_code)

# ==========================================
# 11. Main Async Runner (Bot + FastAPI)
# ==========================================
async def run_asyncio_loop():
    await dp.start_polling(bot)

if __name__ == "__main__":
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)), access_log=False)
    server = uvicorn.Server(config)
    
    loop = asyncio.get_event_loop()
    loop.create_task(run_asyncio_loop())
    loop.run_until_complete(server.serve())
