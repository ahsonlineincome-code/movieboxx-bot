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
BOT_USERNAME = "bdlatestmovie_bot" 

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

CATEGORIES = ["Bangla", "Bangla Dubbed", "Hindi Dubbed", "Hollywood", "K-Drama", "Anime", "Horror", "Web Series", "Adult Content"]

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
    waiting_for_confirm = State()  # ✅ NEW: Confirmation step for smooth upload
    waiting_for_upc_photo = State()
    waiting_for_upc_title = State()
    waiting_for_upc_date = State()

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
    
    if not user:
        args = message.text.split(" ")
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
        await m.answer("⚠️ দুঃখিত! আমি শুধুমাত্র টেক্সট মেসেজ গ্রহণ করি।\n\n🎬 মুভি দেখতে নিচের 'Watch Now' বাটনে ক্লিক করুন।", parse_mode="HTML")
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

# ==========================================
# 7.5 ✅ SMOOTH Movie Upload with /upload command
# ==========================================
@dp.message(Command("upload"))
async def upload_cmd(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()
    await m.answer(
        "🎬 <b>Movie Upload Process Started!</b>\n\n"
        "📌 <b>Step 1/5:</b> এখন <b>Video/Document</b> ফাইল পাঠান।\n\n"
        "📋 <b>Upload Steps:</b>\n"
        "1️⃣ Video/Document ফাইল\n"
        "2️⃣ পোস্টার ছবি\n"
        "3️⃣ মুভির নাম\n"
        "4️⃣ কোয়ালিটি/এপিসোড\n"
        "5️⃣ রিলিজ সাল + ক্যাটাগরি\n\n"
        "⚠️ বাতিল করতে /cancel লিখুন।",
        parse_mode="HTML"
    )

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
    await m.answer(
        "✅ <b>ফাইল রিসিভ হয়েছে!</b>\n\n"
        "📌 <b>Step 2/5:</b> এখন <b>পোস্টার (ছবি)</b> পাঠান।\n\n"
        "⚠️ শুধুমাত্র Photo পাঠান, ফাইল নয়।",
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_for_photo, F.photo)
async def receive_movie_photo(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_title)
    await m.answer(
        "✅ <b>পোস্টার রিসিভ হয়েছে!</b>\n\n"
        "📌 <b>Step 3/5:</b> এখন <b>মুভি/সিরিজের নাম</b> লিখুন।",
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_for_photo)
async def fallback_photo(m: types.Message):
    await m.answer("⚠️ পোস্টার হিসেবে শুধুমাত্র <b>ছবি (Photo)</b> পাঠান। ফাইল হিসেবে পাঠাবেন না। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer(
        "✅ <b>মুভির নাম সেভ হয়েছে!</b>\n\n"
        "📌 <b>Step 4/5:</b> এখন <b>এপিসোড বা কোয়ালিটি</b> লিখুন।\n"
        "📝 উদাহরণ: 1080p, 720p, EP-01, Season-01 ইত্যাদি",
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_for_title)
async def fallback_title(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>মুভির নাম (টেক্সট)</b> লিখুন। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=m.text.strip())
    await state.set_state(AdminStates.waiting_for_year)
    await m.answer(
        "✅ <b>কোয়ালিটি সেভ হয়েছে!</b>\n\n"
        "📌 <b>Step 5/5:</b> এখন <b>রিলিজ সাল</b> লিখুন।\n"
        "📝 উদাহরণ: 2024, 2025 ইত্যাদি",
        parse_mode="HTML"
    )

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
    await m.answer(
        "✅ <b>রিলিজ সাল সেভ হয়েছে!</b>\n\n"
        "📌 <b>Final Step:</b> এখন <b>ক্যাটাগরি সিলেক্ট</b> করুন এবং Done চাপুন।",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )

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
    builder.adjust(2)
    await c.message.edit_reply_markup(reply_markup=builder.as_markup())
    await c.answer()

@dp.callback_query(AdminStates.waiting_for_cats, F.data == "cats_done")
async def finish_category_selection(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    if not selected_cats: return await c.answer("⚠️ অন্তত ১টি সিলেক্ট করুন!", show_alert=True)
    
    # ✅ NEW: Show confirmation preview before saving
    await state.set_state(AdminStates.waiting_for_confirm)
    
    preview_text = (
        f"📋 <b>Movie Upload Preview</b>\n\n"
        f"🎬 Title: <b>{data['title']}</b>\n"
        f"📺 Quality: <b>{data['quality']}</b>\n"
        f"📅 Year: <b>{data.get('year', 'N/A')}</b>\n"
        f"📂 Categories: <b>{', '.join(selected_cats)}</b>\n"
        f"📁 File Type: <b>{data['file_type']}</b>\n\n"
        f"🔍 সব ঠিক আছে কি? কনফার্ম করুন।"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Confirm & Upload", callback_data="confirm_upload")
    builder.button(text="❌ Cancel", callback_data="cancel_upload")
    builder.adjust(2)
    
    try:
        await bot.send_photo(
            c.from_user.id,
            photo=data["photo_id"],
            caption=preview_text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    except:
        await c.message.answer(preview_text, reply_markup=builder.as_markup(), parse_mode="HTML")
    
    await c.answer()

# ✅ NEW: Confirm upload callback
@dp.callback_query(AdminStates.waiting_for_confirm, F.data == "confirm_upload")
async def confirm_movie_upload(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    await state.clear()
    
    # Save movie to database
    await db.movies.insert_one({
        "title": data["title"],
        "quality": data["quality"],
        "photo_id": data["photo_id"],
        "file_id": data["file_id"],
        "file_type": data["file_type"],
        "year": data.get("year", "N/A"),
        "categories": selected_cats,
        "clicks": 0,
        "created_at": datetime.datetime.utcnow()
    })
    
    await c.message.edit_caption(
        caption=f"🎉 <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!\n\n📢 সকল ইউজারকে নোটিফিকেশন পাঠানো হচ্ছে...\n⏰ নোটিফিকেশন ১ দিন পর অটো-ডিলিট হবে।",
        parse_mode="HTML"
    )
    
    # Send to LOG channel
    if LOG_CHANNEL_ID:
        try:
            log_kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", url="https://t.me/MovieeBoxx_Bot?start=new")]]
            log_markup = types.InlineKeyboardMarkup(inline_keyboard=log_kb)
            log_text = f"🎬 <b>New Movie Uploaded</b>\n\n🏷 Title: <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n📅 Year: <b>{data.get('year', 'N/A')}</b>\n📂 Categories: {', '.join(selected_cats)}\n\n👤 Uploaded by Admin"
            await bot.send_photo(LOG_CHANNEL_ID, photo=data["photo_id"], caption=log_text, parse_mode="HTML", reply_markup=log_markup)
        except: pass

    # ✅ Auto broadcast to ALL users
    asyncio.create_task(run_movie_broadcast(data, selected_cats, c.from_user.id))
    await c.answer()

# ✅ NEW: Cancel upload callback
@dp.callback_query(AdminStates.waiting_for_confirm, F.data == "cancel_upload")
async def cancel_movie_upload(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_caption(caption="❌ <b>Movie Upload বাতিল করা হয়েছে!</b>", parse_mode="HTML")
    await c.answer()

# ✅ UPDATED: Auto broadcast with 1 DAY auto-delete
async def run_movie_broadcast(data, selected_cats, admin_id):
    bcast_success = 0
    bcast_fail = 0
    total_users = await db.users.count_documents({})
    
    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"
    web_app_url = APP_URL if APP_URL else "https://t.me/" 
    
    bcast_kb = [
        [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=web_app_url))],
        [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link), types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)]
    ]
    bcast_markup = types.InlineKeyboardMarkup(inline_keyboard=bcast_kb)
    bcast_text = (
        f"🆕 <b>New Movie Alert!</b>\n\n"
        f"🎬 <b>{data['title']}</b>\n"
        f"📺 Quality: <b>{data['quality']}</b>\n"
        f"📅 Year: <b>{data.get('year', 'N/A')}</b>\n"
        f"📂 Category: <b>{', '.join(selected_cats)}</b>\n\n"
        f"👇 এখনই দেখুন!"
    )
    
    now = datetime.datetime.utcnow()
    # ✅ CHANGED: Auto-delete after 1 DAY (24 hours = 1440 minutes)
    delete_at = now + datetime.timedelta(days=1)
    
    async for u in db.users.find():
        try:
            sent_msg = await bot.send_photo(
                u['user_id'],
                photo=data["photo_id"],
                caption=bcast_text,
                reply_markup=bcast_markup,
                parse_mode="HTML"
            )
            # ✅ Save for auto-delete after 1 day
            await db.auto_delete.insert_one({
                "chat_id": u['user_id'],
                "message_id": sent_msg.message_id,
                "delete_at": delete_at
            })
            bcast_success += 1
            await asyncio.sleep(0.3)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                sent_msg = await bot.send_photo(
                    u['user_id'],
                    photo=data["photo_id"],
                    caption=bcast_text,
                    reply_markup=bcast_markup,
                    parse_mode="HTML"
                )
                await db.auto_delete.insert_one({
                    "chat_id": u['user_id'],
                    "message_id": sent_msg.message_id,
                    "delete_at": delete_at
                })
                bcast_success += 1
            except:
                bcast_fail += 1
        except:
            bcast_fail += 1
        
    try:
        await bot.send_message(
            admin_id,
            f"✅ <b>অটো-ব্রডকাস্ট শেষ!</b>\n\n"
            f"👥 Total Users: <b>{total_users}</b>\n"
            f"✅ সফল: <b>{bcast_success}</b>\n"
            f"❌ ব্যর্থ: <b>{bcast_fail}</b>\n\n"
            f"⏰ নোটিফিকেশনগুলো <b>১ দিন (২৪ ঘণ্টা)</b> পর অটো-ডিলিট হবে।",
            parse_mode="HTML"
        )
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
        </style>
    </head>
    <body>
        <div class="header"><h1><i class="fa-solid fa-shield-halved"></i> Admin Panel</h1><p>Movie Box Control Center</p></div>
        <div class="stats-grid">
            <div class="stat-card users"><h3>Total Users</h3><div class="value"><i class="fa-solid fa-users"></i> <span id="totalUsers">0</span></div></div>
            <div class="stat-card today-users"><h3>Today's New Users</h3><div class="value"><i class="fa-solid fa-user-plus"></i> <span id="todayUsers">0</span></div></div>
            <div class="stat-card clicks"><h3>Total Clicks</h3><div class="value"><i class="fa-solid fa-eye"></i> <span id="totalClicks">0</span></div></div>
            <div class="stat-card today-clicks"><h3>Today's Clicks</h3><div class="value"><i class="fa-solid fa-chart-line"></i> <span id="todayClicks">0</span></div></div>
            <div class="stat-card live-users"><h3>Live Active (5m)</h3><div class="value"><i class="fa-solid fa-signal"></i> <span id="activeUsers">0</span></div></div>
        </div>
        <div class="table-container"><div class="table-header"><h2><i class="fa-solid fa-film"></i> Uploaded Movies</h2></div><table><thead><tr><th>Title</th><th>Quality</th><th>Category</th><th>Views</th><th>Action</th></tr></thead><tbody id="movieTableBody"><tr><td colspan="5" class="empty-state">Loading data...</td></tr></tbody></table></div>
        <script>
            async function fetchStats() { try { const res = await fetch('/api/admin/stats'); const data = await res.json(); document.getElementById('totalUsers').innerText = data.total_users; document.getElementById('todayUsers').innerText = data.today_users; document.getElementById('totalClicks').innerText = data.total_clicks; document.getElementById('todayClicks').innerText = data.today_clicks; document.getElementById('activeUsers').innerText = data.active_users; } catch(e) {} }
            async function fetchMovies() { try { const res = await fetch('/api/admin/movies'); const movies = await res.json(); const tbody = document.getElementById('movieTableBody'); if(movies.length === 0) { tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No movies yet.</td></tr>'; return; } tbody.innerHTML = movies.map(m => `<tr id="row-${m._id}"><td><strong>${m.title}</strong><br><small>${m.year || 'N/A'}</small></td><td>${m.quality || 'Main'}</td><td>${(m.categories || []).join(', ')}</td><td><span class="view-badge"><i class="fa-solid fa-eye"></i> ${m.clicks || 0}</span></td><td><button class="delete-btn" onclick="deleteMovie('${m._id}')"><i class="fa-solid fa-trash"></i> Delete</button></td></tr>`).join(''); } catch(e) {} }
            async function deleteMovie(id) { if(!confirm("Delete this file?")) return; try { const res = await fetch(`/api/admin/movie/${id}`, { method: 'DELETE' }); const data = await res.json(); if(data.ok) { document.getElementById(`row-${id}`).remove(); fetchStats(); } } catch(e) {} }
            fetchStats(); fetchMovies(); setInterval(fetchStats, 60000);
        </script>
    </body></html>'''
    return HTMLResponse(html_code)

@app.get("/api/admin/stats")
async def admin_stats(auth: bool = Depends(verify_admin)):
    now = datetime.datetime.utcnow(); today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total_users = await db.users.count_documents({}); today_users = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    five_mins_ago = now - datetime.timedelta(minutes=5); active_users = await db.users.count_documents({"last_active": {"$gte": five_mins_ago}})
    total_clicks_res = await db.movies.aggregate([{"$group": {"_id": None, "total": {"$sum": "$clicks"}}}]).to_list(1); total_clicks = total_clicks_res[0]["total"] if total_clicks_res else 0
    today_clicks = await db.user_unlocks.count_documents({"unlocked_at": {"$gte": today_start}})
    return {"total_users": total_users, "today_users": today_users, "active_users": active_users, "total_clicks": total_clicks, "today_clicks": today_clicks}

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
            .movie-card { display: flex; background: rgba(30, 41, 59, 0.6); border-radius: 16px; overflow: hidden; border: 1px solid #334155; cursor: pointer; transition: 0.3s; position: relative; }
            body.oled-mode .movie-card { background: #0a0a0a; border-color: #1a1a1a; }
            .movie-card:active { transform: scale(0.98); }
            .movie-card img { width: 110px; height: 160px; object-fit: cover; flex-shrink: 0; }
            .movie-info { padding: 12px; display: flex; flex-direction: column; justify-content: center; flex: 1; }
            .movie-title { font-size: 16px; font-weight: 700; margin-bottom: 5px; line-height: 1.3; }
            .movie-meta { font-size: 12px; color: #94a3b8; margin-bottom: 8px; display: flex; gap: 10px; }
            .movie-cats { display: flex; flex-wrap: wrap; gap: 5px; }
            .movie-cat-tag { background: rgba(255,255,255,0.1); padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: 600; color: #cbd5e1; }
            .fav-btn { position: absolute; top: 10px; right: 10px; background: rgba(0,0,0,0.6); border: none; width: 30px; height: 30px; border-radius: 50%; color: white; font-size: 14px; cursor: pointer; display: flex; align-items: center; justify-content: center; z-index: 10; }
            .fav-btn.active { color: #ef4444; }
            .adult-lock-overlay { position: absolute; top: 0; left: 0; width: 110px; height: 160px; background: rgba(0,0,0,0.7); display: flex; align-items: center; justify-content: center; color: #ef4444; font-size: 30px; z-index: 5; }
            .floating-btn { position: fixed; right: 15px; width: 50px; height: 50px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 20px; z-index: 500; cursor: pointer; box-shadow: 0 4px 15px rgba(0,0,0,0.5); border: 2px solid white; text-decoration: none; color: white; }
            .btn-tg { bottom: 160px; background: linear-gradient(45deg, #24A1DE, #1b7ba8); }
            .btn-18 { bottom: 100px; background: linear-gradient(45deg, #ef4444, #b91c1c); font-weight: bold; }
            .bottom-nav { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); border-top: 1px solid #1e293b; display: flex; justify-content: space-around; padding: 10px 0; z-index: 1000; }
            body.oled-mode .bottom-nav { background: rgba(0, 0, 0, 0.95); border-color: #1a1a1a; }
            .nav-item { display: flex; flex-direction: column; align-items: center; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer; border: none; background: none; }
            .nav-item i { font-size: 20px; margin-bottom: 3px; }
            .nav-item.active { color: #ef4444; }
            .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: flex-end; justify-content: center; z-index: 3000; }
            .modal-content { background: #1e293b; width: 100%; max-width: 400px; padding: 25px; border-radius: 20px 20px 0 0; max-height: 90vh; overflow-y: auto; position: relative; }
            body.oled-mode .modal-content { background: #000000; }
            .detail-img { width: 100%; height: 250px; object-fit: cover; border-radius: 12px; margin-bottom: 15px; }
            .detail-title { font-size: 22px; font-weight: 800; margin-bottom: 5px; }
            .detail-meta { color: #94a3b8; font-size: 14px; margin-bottom: 15px; }
            .close-icon { position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: rgba(0,0,0,0.6); color: #fff; font-size: 18px; display: flex; align-items: center; justify-content: center; cursor: pointer; border: none; }
            .dl-file-btn { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 15px; background: #0f172a; border: 1px solid #334155; color: white; font-weight: 700; border-radius: 10px; margin-bottom: 10px; cursor: pointer; }
            body.oled-mode .dl-file-btn { background: #050505; border-color: #1a1a1a; }
            .dl-file-btn i { color: #ef4444; font-size: 18px; }
            .dl-file-btn.unlocked i { color: #10b981; }
            .age-box { text-align: center; }
            .age-btn { width: 100%; padding: 15px; border-radius: 12px; font-weight: 700; border: none; font-size: 16px; cursor: pointer; margin-top: 15px; }
            .age-yes { background: #ef4444; color: white; }
            .age-no { background: #334155; color: white; }
            .ad-box { text-align: center; padding: 20px; }
            .ad-icon { font-size: 60px; margin-bottom: 10px; color: #fbbf24; }
            .ad-title { color: #fbbf24; font-size: 20px; font-weight: 800; margin-bottom: 15px; }
            .ad-box-orange { background: #ea580c; color: white; padding: 12px; border-radius: 8px; margin-bottom: 10px; font-weight: 600; }
            .ad-box-black { background: #000000; color: #e2e8f0; padding: 12px; border-radius: 8px; margin-bottom: 20px; font-size: 14px; }
            .ad-action-btn { width: 100%; padding: 15px; border-radius: 8px; font-weight: 700; border: none; font-size: 16px; cursor: pointer; margin-bottom: 10px; }
            .btn-ad-open { background: #ea580c; color: white; }
            .btn-ad-unlock { background: #10b981; color: white; }
            .btn-ad-tryagain { background: #ef4444; color: white; }
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
            .pagination-container { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 20px 15px 80px 15px; }
            .page-btn { background: #1e293b; color: #cbd5e1; border: 1px solid #334155; padding: 10px 15px; border-radius: 10px; font-weight: 700; cursor: pointer; transition: 0.2s; font-size: 14px; }
            body.oled-mode .page-btn { background: #0a0a0a; border-color: #1a1a1a; }
            .page-btn:hover { background: #334155; color: white; }
            .page-btn.active { background: linear-gradient(45deg, #ef4444, #dc2626); color: white; border-color: #ef4444; box-shadow: 0 0 8px rgba(239, 68, 68, 0.3); }
            .page-btn:disabled { background: #1e293b; color: #475569; cursor: not-allowed; border-color: #1e293b; }
        </style>
    </head>
    <body>
        <div id="welcomeScreen"><div class="ws-brand">Movie Box</div><div class="ws-bn">মুভি বক্স জগতে স্বাগতম</div></div>
        <header onclick="switchTab('home')"><div class="logo">Movie Box</div></header>

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
            <div class="movie-list" id="movieListHome"><div class="skeleton"></div><div class="skeleton"></div></div>
            <div id="paginationHome" class="pagination-container"></div>
        </div>

        <div id="tabSearch" class="page-section"><div class="search-box" style="padding-top:15px;"><input type="text" id="searchInputMain" class="search-input" placeholder="🔍 সার্চ..." oninput="searchMovies()"></div><div class="movie-list" id="movieListSearch"></div><div id="paginationSearch" class="pagination-container"></div></div>
        <div id="tabFav" class="page-section"><h3 style="padding: 15px; color: #fbbf24;">❤️ ফেভারিট</h3><div class="movie-list" id="movieListFav"></div></div>
        
        <div id="tabSurprise" class="page-section">
            <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 60vh; text-align: center; padding: 20px;">
                <div style="font-size: 80px; margin-bottom: 20px; animation: pulse 1.5s infinite;">🎲</div>
                <h2 style="margin-bottom: 15px; color: #fbbf24;">মুভি রুলেট!</h2>
                <p style="color: #94a3b8; margin-bottom: 30px;">কী দেখবেন ঠিক করতে পারছেন না? বট আপনার জন্য একটি মুভি বেছে নিচ্ছে!</p>
                <button onclick="loadSurprise()" style="padding: 15px 40px; background: linear-gradient(45deg, #ff416c, #ff4b2b); color: white; font-weight: 700; border: none; border-radius: 12px; font-size: 18px; cursor: pointer; box-shadow: 0 4px 15px rgba(255,65,108,0.4);">🎲 Spin Roulette</button>
                <div id="surpriseResult" style="margin-top: 30px; width: 100%;"></div>
            </div>
        </div>

        <div id="tabProfile" class="page-section">
            <div class="profile-card" style="text-align: center;">
                <div style="font-size: 60px; margin-bottom: 10px;">👤</div>
                <h2 id="profileName" style="margin-bottom: 5px;">Loading...</h2>
                <p id="profileId" style="color: #94a3b8; font-size: 13px;"></p>
                <div style="display: flex; justify-content: center; gap: 20px; margin-top: 15px;">
                    <div style="text-align: center;"><div style="font-size: 24px; font-weight: 800; color: #fbbf24;" id="profileCoins">0</div><div style="font-size: 11px; color: #94a3b8;">Coins</div></div>
                    <div style="text-align: center;"><div style="font-size: 24px; font-weight: 800; color: #10b981;" id="profileVip">Free</div><div style="font-size: 11px; color: #94a3b8;">Status</div></div>
                </div>
            </div>
            <div class="profile-card">
                <button class="profile-action-btn btn-dark-mode" onclick="toggleOled()"><i class="fa-solid fa-moon"></i> OLED Dark Mode</button>
                <a class="profile-action-btn btn-main-ch" href="https://t.me/addlist/MwbWNafSFK4yZjhl" target="_blank"><i class="fa-solid fa-tv"></i> Main Channel</a>
                <a class="profile-action-btn btn-18-ch" href="https://t.me/+W5V9-mn08jMyYTE1" target="_blank"><i class="fa-solid fa-fire"></i> 18+ Channel</a>
            </div>
        </div>

        <a class="floating-btn btn-tg" href="https://t.me/addlist/MwbWNafSFK4yZjhl" target="_blank"><i class="fa-brands fa-telegram"></i></a>
        <a class="floating-btn btn-18" href="https://t.me/+W5V9-mn08jMyYTE1" target="_blank">18+</a>

        <div class="bottom-nav">
            <button class="nav-item active" onclick="switchTab('home')"><i class="fa-solid fa-house"></i>Home</button>
            <button class="nav-item" onclick="switchTab('search')"><i class="fa-solid fa-magnifying-glass"></i>Search</button>
            <button class="nav-item" onclick="switchTab('fav')"><i class="fa-solid fa-heart"></i>Fav</button>
            <button class="nav-item" onclick="switchTab('surprise')"><i class="fa-solid fa-dice"></i>Surprise</button>
            <button class="nav-item" onclick="switchTab('profile')"><i class="fa-solid fa-user"></i>Profile</button>
        </div>

        <!-- Movie Detail Modal -->
        <div class="modal" id="movieModal">
            <div class="modal-content" id="movieModalContent"></div>
        </div>

        <!-- Age Verification Modal -->
        <div class="modal" id="ageModal">
            <div class="modal-content">
                <div class="age-box">
                    <div style="font-size: 60px;">🔞</div>
                    <h2 style="margin: 15px 0; color: #ef4444;">Age Verification</h2>
                    <p style="color: #94a3b8; margin-bottom: 15px;">এই কন্টেন্টটি শুধুমাত্র ১৮+ দর্শকদের জন্য। আপনি কি ১৮ বছরের বেশি বয়সী?</p>
                    <button class="age-btn age-yes" onclick="confirmAge()">✅ হ্যাঁ, আমি ১৮+</button>
                    <button class="age-btn age-no" onclick="denyAge()">❌ না, ফিরে যান</button>
                </div>
            </div>
        </div>

        <!-- Ad Modal -->
        <div class="modal" id="adModal">
            <div class="modal-content">
                <div class="ad-box">
                    <div class="ad-icon">📺</div>
                    <div class="ad-title">Watch Ad to Unlock</div>
                    <div class="ad-box-orange">🎬 বিজ্ঞাপন দেখে মুভি আনলক করুন!</div>
                    <div class="ad-box-black">নিচের লিংকে ক্লিক করে ১০ সেকেন্ড অপেক্ষা করুন, তারপর ফিরে এসে আনলক বাটনে ক্লিক করুন।</div>
                    <button class="ad-action-btn btn-ad-open" id="adOpenBtn" onclick="openAdLink()">🔗 Open Ad Link</button>
                    <button class="ad-action-btn btn-ad-unlock" id="adUnlockBtn" onclick="verifyAdUnlock()" style="display:none;">✅ Unlock Movie</button>
                    <button class="ad-action-btn btn-ad-tryagain" onclick="closeAdModal()">❌ Cancel</button>
                </div>
            </div>
        </div>

        <script>
            let tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
            let currentUser = null;
            let allMovies = [];
            let favMovies = JSON.parse(localStorage.getItem('moviebox_favs') || '[]');
            let currentCat = 'Home';
            let currentPage = 1;
            let adultVerified = false;
            let adClickTime = 0;
            let directLinks = ''' + dl_json + ''';
            let adultDirectLinks = ''' + adl_json + ''';
            const PER_PAGE = 10;

            if(tg) { tg.ready(); tg.expand(); }

            setTimeout(() => { let ws = document.getElementById('welcomeScreen'); if(ws) ws.classList.add('hide'); }, 2000);

            async function init() {
                if(tg && tg.initDataUnsafe && tg.initDataUnsafe.user) {
                    currentUser = tg.initDataUnsafe.user;
                }
                await fetchMovies();
                loadProfile();
            }

            async function fetchMovies() {
                try {
                    const res = await fetch('/api/movies');
                    allMovies = await res.json();
                    renderMovies();
                } catch(e) { console.error(e); }
            }

            function renderMovies() {
                let filtered = currentCat === 'Home' ? allMovies : allMovies.filter(m => m.categories && m.categories.includes(currentCat));
                if(currentCat === 'Home' || currentCat !== 'Adult Content') {
                    // show all
                }
                const total = Math.ceil(filtered.length / PER_PAGE);
                if(currentPage > total) currentPage = total || 1;
                const start = (currentPage - 1) * PER_PAGE;
                const pageMovies = filtered.slice(start, start + PER_PAGE);
                
                const container = document.getElementById('movieListHome');
                if(pageMovies.length === 0) {
                    container.innerHTML = '<div style="text-align:center;padding:40px;color:#64748b;">🎬 কোনো মুভি পাওয়া যায়নি</div>';
                } else {
                    container.innerHTML = pageMovies.map(m => renderCard(m)).join('');
                }
                renderPagination('paginationHome', total);
            }

            function renderCard(m) {
                const isFav = favMovies.includes(m._id);
                const isAdult = m.categories && m.categories.includes('Adult Content');
                return `<div class="movie-card" onclick="openMovie('${m._id}')">
                    <div style="position:relative;">
                        <img src="https://api.telegram.org/file/bot''' + TOKEN + '''/${m.photo_id}" onerror="this.src='https://via.placeholder.com/110x160/1e293b/94a3b8?text=🎬'" alt="${m.title}">
                        ${isAdult && !adultVerified ? '<div class="adult-lock-overlay">🔒</div>' : ''}
                    </div>
                    <div class="movie-info">
                        <div class="movie-title">${m.title}</div>
                        <div class="movie-meta"><span>📺 ${m.quality || 'HD'}</span><span>📅 ${m.year || 'N/A'}</span></div>
                        <div class="movie-cats">${(m.categories||[]).map(c=>'<span class="movie-cat-tag">'+c+'</span>').join('')}</div>
                    </div>
                    <button class="fav-btn ${isFav?'active':''}" onclick="event.stopPropagation();toggleFav('${m._id}')"><i class="fa-solid fa-heart"></i></button>
                </div>`;
            }

            function renderPagination(containerId, totalPages) {
                const c = document.getElementById(containerId);
                if(totalPages <= 1) { c.innerHTML = ''; return; }
                let btns = '';
                btns += `<button class="page-btn" onclick="goPage(${currentPage-1})" ${currentPage===1?'disabled':''}><i class="fa-solid fa-chevron-left"></i></button>`;
                for(let i = 1; i <= totalPages; i++) {
                    if(i === 1 || i === totalPages || (i >= currentPage-2 && i <= currentPage+2)) {
                        btns += `<button class="page-btn ${i===currentPage?'active':''}" onclick="goPage(${i})">${i}</button>`;
                    } else if(i === currentPage-3 || i === currentPage+3) {
                        btns += `<span style="color:#64748b;">...</span>`;
                    }
                }
                btns += `<button class="page-btn" onclick="goPage(${currentPage+1})" ${currentPage===totalPages?'disabled':''}><i class="fa-solid fa-chevron-right"></i></button>`;
                c.innerHTML = btns;
            }

            function goPage(p) { currentPage = p; renderMovies(); window.scrollTo(0,0); }

            function filterCat(cat, el) {
                currentCat = cat; currentPage = 1;
                document.querySelectorAll('.cat-chip').forEach(c=>c.classList.remove('active'));
                if(el) el.classList.add('active');
                renderMovies();
            }

            function switchTab(tab) {
                document.querySelectorAll('.page-section').forEach(s=>s.classList.remove('active'));
                document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
                document.getElementById('tab'+tab.charAt(0).toUpperCase()+tab.slice(1)).classList.add('active');
                event.target.closest('.nav-item')?.classList.add('active');
                if(tab==='fav') renderFavs();
            }

            function toggleFav(id) {
                if(favMovies.includes(id)) favMovies = favMovies.filter(f=>f!==id);
                else favMovies.push(id);
                localStorage.setItem('moviebox_favs', JSON.stringify(favMovies));
                renderMovies();
            }

            function renderFavs() {
                const favList = allMovies.filter(m => favMovies.includes(m._id));
                const c = document.getElementById('movieListFav');
                if(favList.length === 0) { c.innerHTML = '<div style="text-align:center;padding:40px;color:#64748b;">❤️ কোনো ফেভারিট নেই</div>'; return; }
                c.innerHTML = favList.map(m => renderCard(m)).join('');
            }

            function openMovie(id) {
                const m = allMovies.find(x => x._id === id);
                if(!m) return;
                const isAdult = m.categories && m.categories.includes('Adult Content');
                if(isAdult && !adultVerified) { document.getElementById('ageModal').style.display = 'flex'; return; }
                showMovieModal(m);
            }

            function showMovieModal(m) {
                const isUnlocked = true; // simplified
                let html = `<button class="close-icon" onclick="closeModal()"><i class="fa-solid fa-xmark"></i></button>`;
                html += `<img class="detail-img" src="https://api.telegram.org/file/bot''' + TOKEN + '''/${m.photo_id}" onerror="this.src='https://via.placeholder.com/400x250/1e293b/94a3b8?text=🎬'" alt="${m.title}">`;
                html += `<div class="detail-title">${m.title}</div>`;
                html += `<div class="detail-meta">📺 ${m.quality || 'HD'} &nbsp;📅 ${m.year || 'N/A'} &nbsp;👁 ${m.clicks || 0} views</div>`;
                html += `<div style="margin-bottom:15px;">${(m.categories||[]).map(c=>'<span class="movie-cat-tag" style="margin-right:5px;">'+c+'</span>').join('')}</div>`;
                html += `<button class="dl-file-btn ${isUnlocked?'unlocked':''}" onclick="downloadMovie('${m._id}')"><span><i class="fa-solid fa-${isUnlocked?'unlock':'lock'}"></i> ${isUnlocked?'Download File':'Watch Ad to Unlock'}</span><i class="fa-solid fa-download"></i></button>`;
                document.getElementById('movieModalContent').innerHTML = html;
                document.getElementById('movieModal').style.display = 'flex';
            }

            function closeModal() { document.getElementById('movieModal').style.display = 'none'; }
            function closeAdModal() { document.getElementById('adModal').style.display = 'none'; }

            function confirmAge() { adultVerified = true; document.getElementById('ageModal').style.display = 'none'; renderMovies(); }
            function denyAge() { document.getElementById('ageModal').style.display = 'none'; }

            function verify18(el) { document.getElementById('ageModal').style.display = 'flex'; }

            async function downloadMovie(id) {
                try {
                    await fetch('/api/click', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({movie_id: id}) });
                    const m = allMovies.find(x => x._id === id);
                    if(m) {
                        const isAdult = m.categories && m.categories.includes('Adult Content');
                        const links = isAdult ? adultDirectLinks : directLinks;
                        if(links && links.length > 0) {
                            showAdModal(m, links);
                        } else {
                            sendFile(m);
                        }
                    }
                } catch(e) { console.error(e); }
            }

            function showAdModal(m, links) {
                const randomLink = links[Math.floor(Math.random() * links.length)];
                document.getElementById('adOpenBtn').setAttribute('data-link', randomLink);
                document.getElementById('adOpenBtn').setAttribute('data-movieid', m._id);
                document.getElementById('adOpenBtn').style.display = 'block';
                document.getElementById('adUnlockBtn').style.display = 'none';
                document.getElementById('adModal').style.display = 'flex';
            }

            function openAdLink() {
                const link = document.getElementById('adOpenBtn').getAttribute('data-link');
                window.open(link, '_blank');
                adClickTime = Date.now();
                document.getElementById('adOpenBtn').style.display = 'none';
                document.getElementById('adUnlockBtn').style.display = 'block';
            }

            function verifyAdUnlock() {
                const elapsed = (Date.now() - adClickTime) / 1000;
                if(elapsed < 5) {
                    alert('⚠️ দয়া করে কিছুক্ষণ অপেক্ষা করুন!');
                    return;
                }
                const movieId = document.getElementById('adOpenBtn').getAttribute('data-movieid');
                const m = allMovies.find(x => x._id === movieId);
                if(m) sendFile(m);
                closeAdModal();
            }

            async function sendFile(m) {
                try {
                    if(tg) {
                        await tg.sendData(JSON.stringify({action:'download', movie_id: m._id, file_id: m.file_id}));
                    }
                    closeModal();
                } catch(e) {
                    alert('❌ ফাইল পাঠাতে সমস্যা হয়েছে। বটে ফিরে গিয়ে আবার চেষ্টা করুন।');
                }
            }

            async function searchMovies() {
                const q = document.getElementById('searchInputMain').value.trim().toLowerCase();
                if(!q) { document.getElementById('movieListSearch').innerHTML = ''; return; }
                const results = allMovies.filter(m => m.title.toLowerCase().includes(q));
                const c = document.getElementById('movieListSearch');
                if(results.length === 0) { c.innerHTML = '<div style="text-align:center;padding:40px;color:#64748b;">🔍 কিছু পাওয়া যায়নি</div>'; return; }
                c.innerHTML = results.map(m => renderCard(m)).join('');
            }

            async function loadSurprise() {
                if(allMovies.length === 0) return;
                const m = allMovies[Math.floor(Math.random() * allMovies.length)];
                document.getElementById('surpriseResult').innerHTML = renderCard(m);
            }

            function loadProfile() {
                if(currentUser) {
                    document.getElementById('profileName').innerText = currentUser.first_name || 'User';
                    document.getElementById('profileId').innerText = 'ID: ' + currentUser.id;
                }
            }

            function toggleOled() { document.body.classList.toggle('oled-mode'); }

            init();
        </script>
    </body></html>'''
    return HTMLResponse(html_code)

# ==========================================
# 10. API Endpoints for Web App
# ==========================================
@app.get("/api/movies")
async def get_movies():
    movies = await db.movies.find({}).sort("created_at", -1).to_list(1000)
    for m in movies:
        m["_id"] = str(m["_id"])
    return movies

@app.post("/api/click")
async def record_click(data: dict = Body(...)):
    movie_id = data.get("movie_id")
    if movie_id:
        try:
            await db.movies.update_one({"_id": ObjectId(movie_id)}, {"$inc": {"clicks": 1}})
            await db.user_unlocks.insert_one({"movie_id": movie_id, "unlocked_at": datetime.datetime.utcnow()})
        except: pass
    return {"ok": True}

# ==========================================
# 11. Startup & Main
# ==========================================
async def on_startup():
    await init_db()
    await load_admins()
    await load_banned_users()
    asyncio.create_task(auto_delete_worker())

async def on_shutdown():
    pass

@app.on_event("startup")
async def startup_event():
    await on_startup()

@app.on_event("shutdown")
async def shutdown_event():
    await on_shutdown()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
