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

# ✅ অটো-ডিলিট টাইম কনফিগারেশন
BOT_MSG_DELETE_HOURS = 24       # বট মেসেজ ২৪ ঘণ্টা পর ডিলিট
MOVIE_FILE_DELETE_HOURS = 1     # মুভি ফাইল ১ ঘণ্টা পর ডিলিট

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
    # ✅ ফিক্স: প্রতিটি index তে try/except যাতে conflict এ crash না হয়
    try:
        await db.movies.create_index([("title", "text")])
    except: pass
    try:
        await db.movies.create_index("title")
    except: pass
    try:
        await db.movies.create_index("created_at")
    except: pass
    try:
        await db.movies.create_index("categories")
    except: pass
    try:
        await db.auto_delete.create_index("delete_at")
    except: pass
    try:
        await db.users.create_index("joined_at")
    except: pass
    try:
        await db.payments.create_index("trx_id", unique=True)
    except: pass
    try:
        await db.favorites.create_index([("user_id", 1), ("movie_id", 1)])
    except: pass
    try:
        await db.user_unlocks.create_index("unlocked_at")
    except: pass

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

def get_user_id_from_initdata(init_data: str) -> int:
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data))
        user_data = json.loads(parsed.get('user', '{}'))
        return user_data.get('id', 0)
    except:
        return 0

# ✅ অটো-ডিলিট শিডিউল হেল্পার
async def schedule_auto_delete(message: types.Message, hours: float = BOT_MSG_DELETE_HOURS):
    delete_at = datetime.datetime.utcnow() + datetime.timedelta(hours=hours)
    await db.auto_delete.insert_one({
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "delete_at": delete_at,
        "type": "bot_message"
    })

async def schedule_file_auto_delete(chat_id: int, message_id: int, hours: float = MOVIE_FILE_DELETE_HOURS):
    delete_at = datetime.datetime.utcnow() + datetime.timedelta(hours=hours)
    await db.auto_delete.insert_one({
        "chat_id": chat_id,
        "message_id": message_id,
        "delete_at": delete_at,
        "type": "movie_file"
    })

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
        await asyncio.sleep(30)

# ==========================================
# 6. Telegram Bot Commands
# ==========================================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if uid in banned_cache:
        msg = await message.answer("🚫 আপনাকে ব্যান করা হয়েছে।", parse_mode="HTML")
        await schedule_auto_delete(msg, hours=1)
        return
        
    await state.clear()
    now = datetime.datetime.utcnow()
    user = await db.users.find_one({"user_id": uid})
    
    if not user:
        # ✅ রেফারেল VIP সিস্টেম সরানো হয়েছে - শুধু ইউজার রেজিস্টার
        await db.users.insert_one({
            "user_id": uid, 
            "first_name": message.from_user.first_name, 
            "joined_at": now, 
            "refer_count": 0, 
            "coins": 0, 
            "last_checkin": now - datetime.timedelta(days=2), 
            "vip_until": now - datetime.timedelta(days=1), 
            "last_active": now, 
            "is_adult_verified": False
        })
    else:
        await db.users.update_one({"user_id": uid}, {"$set": {"first_name": message.from_user.first_name, "last_active": now}})

    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"

    kb = [
        [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))],
        [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link), types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)]
    ]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    text = f"👋 <b>স্বাগতম {message.from_user.first_name}!</b>\n\n🎬 Movie Box জগতে আপনাকে স্বাগতম। নিচের বাটনে ক্লিক করে মুভি উপভোগ করুন।\n\n⚠️ <i>মেসেজ ২৪ ঘণ্টা পর অটো-ডিলিট হবে। মুভি ফাইল ১ ঘণ্টা পর ডিলিট হবে।</i>"
    if uid in admin_cache: text += "\n\n⚙️ <b>অ্যাডমিন মোড অন.</b>"
    msg = await message.answer(text, reply_markup=markup, parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(Command("stats"))
async def bot_stats(m: types.Message):
    if m.from_user.id not in admin_cache: return
    total_users = await db.users.count_documents({})
    total_movies = await db.movies.count_documents({})
    vip_users = await db.users.count_documents({"vip_until": {"$gt": datetime.datetime.utcnow()}})
    pending_del = await db.auto_delete.count_documents({})
    text = f"📊 <b>Bot Statistics</b>\n\n👥 Total Users: <b>{total_users}</b>\n💎 VIP Users: <b>{vip_users}</b>\n🎬 Total Movies: <b>{total_movies}</b>\n⏳ Pending Auto-Delete: <b>{pending_del}</b>"
    msg = await m.answer(text, parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(Command("ban"))
async def ban_user(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        uid = int(m.text.split()[1])
        await db.banned.update_one({"user_id": uid}, {"$set": {"user_id": uid}}, upsert=True)
        banned_cache.add(uid)
        msg = await m.answer(f"🚫 User <code>{uid}</code> ব্যান করা হয়েছে।", parse_mode="HTML")
        await schedule_auto_delete(msg)
    except: 
        msg = await m.answer("⚠️ /ban USER_ID", parse_mode="HTML")
        await schedule_auto_delete(msg)

@dp.message(Command("unban"))
async def unban_user(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        uid = int(m.text.split()[1])
        await db.banned.delete_one({"user_id": uid})
        banned_cache.discard(uid)
        msg = await m.answer(f"✅ User <code>{uid}</code> আনব্যান করা হয়েছে।", parse_mode="HTML")
        await schedule_auto_delete(msg)
    except: 
        msg = await m.answer("⚠️ /unban USER_ID", parse_mode="HTML")
        await schedule_auto_delete(msg)

@dp.message(lambda m: m.chat.type == "private" and m.from_user.id not in admin_cache)
async def handle_user_messages(m: types.Message):
    if m.content_type not in ['text']:
        msg = await m.answer("⚠️ দুঃখিত! আমি শুধুমাত্র টেক্সট মেসেজ গ্রহণ করি।\n\n🎬 মুভি দেখতে নিচের 'Watch Now' বাটনে ক্লিক করুন।", parse_mode="HTML")
        await schedule_auto_delete(msg)
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
    await c.message.answer("✍️ আপনার মেসেজ লিখুন (রিপ্লাই দেওয়ার জন্য):")
    await c.answer()

@dp.message(AdminStates.waiting_for_reply)
async def send_reply_to_user(m: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("reply_user_id")
    await state.clear()
    if user_id:
        try:
            sent = await m.copy_to(chat_id=user_id)
            await schedule_auto_delete(sent)
            await m.answer("✅ রিপ্লাই পাঠানো হয়েছে!")
        except:
            await m.answer("❌ রিপ্লাই পাঠাতে ব্যর্থ হয়েছে।")

# ==========================================
# 7. Admin Commands & Movie Upload
# ==========================================
@dp.message(Command("cancel"))
async def cancel_cmd(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.clear()
    msg = await m.answer("❌ বর্তমান প্রসেস বাতিল করা হয়েছে!", parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(Command("protect"))
async def toggle_protect(m: types.Message):
    if m.from_user.id not in admin_cache: return
    cfg = await db.settings.find_one({"id": "protect_content"})
    current = cfg.get("status", False) if cfg else False
    new_status = not current
    await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": new_status}}, upsert=True)
    status_text = "অন 🔒" if new_status else "অফ 🔓"
    msg = await m.answer(f"✅ ফরোয়ার্ড প্রোটেকশন এখন <b>{status_text}</b>", parse_mode="HTML")
    await schedule_auto_delete(msg)

# ❌ /setadcount সরানো হয়েছে

@dp.message(Command("settime"))
async def set_delete_time(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        minutes = int(m.text.split()[1])
        await db.settings.update_one({"id": "del_time"}, {"$set": {"minutes": minutes}}, upsert=True)
        msg = await m.answer(f"✅ অটো-ডিলিট টাইম <b>{minutes} মিনিট</b> এ সেট করা হয়েছে।", parse_mode="HTML")
        await schedule_auto_delete(msg)
    except: 
        msg = await m.answer("⚠️ /settime 60 (মিনিট লিখুন)", parse_mode="HTML")
        await schedule_auto_delete(msg)

@dp.message(Command("addlink"))
async def add_link_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        msg = await m.answer("✅ অ্যাড জোন লিংক অ্যাড হয়েছে।", parse_mode="HTML")
        await schedule_auto_delete(msg)
    except: 
        msg = await m.answer("⚠️ /addlink url", parse_mode="HTML")
        await schedule_auto_delete(msg)

@dp.message(Command("addadultlink"))
async def add_adult_link_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "adult_direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        msg = await m.answer("✅ ১৮+ অ্যাড লিংক অ্যাড হয়েছে।", parse_mode="HTML")
        await schedule_auto_delete(msg)
    except: 
        msg = await m.answer("⚠️ /addadultlink url", parse_mode="HTML")
        await schedule_auto_delete(msg)

@dp.message(Command("settg"))
async def set_tg_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "tg_link"}, {"$set": {"url": link}}, upsert=True)
        msg = await m.answer("✅ টেলিগ্রাম চ্যানেল লিংক আপডেট হয়েছে।", parse_mode="HTML")
        await schedule_auto_delete(msg)
    except: 
        msg = await m.answer("⚠️ /settg https://t.me/...", parse_mode="HTML")
        await schedule_auto_delete(msg)

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        title = m.text.split(" ", 1)[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0: 
            msg = await m.answer(f"✅ '<b>{title}</b>' ডিলিট হয়েছে!", parse_mode="HTML")
        else: 
            msg = await m.answer("⚠️ পাওয়া যায়নি")
        await schedule_auto_delete(msg)
    except: 
        msg = await m.answer("⚠️ /delmovie মুভির নাম", parse_mode="HTML")
        await schedule_auto_delete(msg)

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
        msg = await m.answer(f"✅ <code>{target_uid}</code> কে {days} দিনের VIP দেওয়া হয়েছে!", parse_mode="HTML")
        await schedule_auto_delete(msg)
    except: 
        msg = await m.answer("⚠️ /addvip ID দিন", parse_mode="HTML")
        await schedule_auto_delete(msg)

@dp.message(Command("addupcoming"))
async def add_upcoming_start(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_upc_photo)
    msg = await m.answer("🌟 আপকামিং মুভির <b>পোস্টার</b> পাঠান।\n\n📌 Step 1/3", parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(AdminStates.waiting_for_upc_photo, F.photo)
async def receive_upc_photo(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_upc_title)
    msg = await m.answer("✅ পোস্টার পেয়েছি! এবার <b>মুভির নাম</b> লিখুন।\n\n📌 Step 2/3", parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(AdminStates.waiting_for_upc_title, F.text)
async def receive_upc_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_upc_date)
    msg = await m.answer("✅ এবার <b>রিলিজ তারিখ</b> লিখুন।\n\n📌 Step 3/3", parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(AdminStates.waiting_for_upc_date, F.text)
async def receive_upc_date(m: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await db.upcoming.insert_one({"title": data["title"], "photo_id": data["photo_id"], "release_date": m.text.strip()})
    msg = await m.answer(f"🌟 <b>{data['title']}</b> আপকামিং লিস্টে যুক্ত হয়েছে!", parse_mode="HTML")
    await schedule_auto_delete(msg)

# ==========================================
# 7.5 ✅ স্মুথ মুভি আপলোড
# ==========================================
@dp.message(F.content_type.in_({'video', 'document'}), lambda m: m.from_user.id in admin_cache)
async def receive_movie_file(m: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        msg = await m.answer("⚠️ আপনি অন্য একটি প্রসেসে আটকে আছেন! আগে /cancel করুন।", parse_mode="HTML")
        await schedule_auto_delete(msg)
        return
    
    fid = m.video.file_id if m.video else m.document.file_id
    ftype = "video" if m.video else "document"

    auto_title = ""
    if m.video and m.video.file_name:
        auto_title = m.video.file_name.rsplit('.', 1)[0] if '.' in m.video.file_name else m.video.file_name
    elif m.document and m.document.file_name:
        auto_title = m.document.file_name.rsplit('.', 1)[0] if '.' in m.document.file_name else m.document.file_name

    await state.set_state(AdminStates.waiting_for_photo)
    await state.update_data(file_id=fid, file_type=ftype, categories=[], auto_title=auto_title)

    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(text="❌ Cancel Upload", callback_data="cancel_upload")

    text = "📥 <b>Movie Upload Started!</b>\n\n✅ ফাইল পেয়েছি!\n"
    if auto_title:
        text += f"📝 অটো-ডিটেক্ট: <i>{auto_title}</i>\n"
    text += f"\n📌 <b>Step 1/5:</b> এবার <b>পোস্টার (ছবি)</b> পাঠান।"
    
    msg = await m.answer(text, reply_markup=cancel_kb.as_markup(), parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.callback_query(F.data == "cancel_upload")
async def cancel_upload_callback(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in admin_cache: return
    await state.clear()
    await c.message.edit_text("❌ আপলোড বাতিল করা হয়েছে!")
    await c.answer()

@dp.message(AdminStates.waiting_for_photo, F.photo)
async def receive_movie_photo(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_title)
    
    data = await state.get_data()
    auto_title = data.get("auto_title", "")
    
    text = "✅ পোস্টার পেয়েছি!\n\n📌 <b>Step 2/5:</b> এবার <b>মুভি/সিরিজের নাম</b> লিখুন।"
    if auto_title:
        text += f"\n\n💡 <i>সাজেস্টেড: {auto_title}</i>"
    
    msg = await m.answer(text, parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(AdminStates.waiting_for_photo)
async def fallback_photo(m: types.Message):
    msg = await m.answer("⚠️ পোস্টার হিসেবে শুধুমাত্র <b>ছবি (Photo)</b> পাঠান। অথবা /cancel লিখুন।", parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    title = m.text.strip()
    if not title:
        data = await state.get_data()
        title = data.get("auto_title", "Untitled")
    await state.update_data(title=title)
    await state.set_state(AdminStates.waiting_for_quality)
    msg = await m.answer(f"✅ টাইটেল: <b>{title}</b>\n\n📌 <b>Step 3/5:</b> এবার <b>কোয়ালিটি</b> লিখুন।\n(যেমন: 720p, 1080p, S01E01)", parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(AdminStates.waiting_for_title)
async def fallback_title(m: types.Message):
    msg = await m.answer("⚠️ দয়া করে <b>মুভির নাম (টেক্সট)</b> লিখুন। অথবা /cancel লিখুন।", parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=m.text.strip())
    await state.set_state(AdminStates.waiting_for_year)
    msg = await m.answer("✅ কোয়ালিটি সেট!\n\n📌 <b>Step 4/5:</b> এবার <b>রিলিজ সাল</b> লিখুন।", parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(AdminStates.waiting_for_quality)
async def fallback_quality(m: types.Message):
    msg = await m.answer("⚠️ দয়া করে <b>কোয়ালিটি (টেক্সট)</b> লিখুন। অথবা /cancel লিখুন।", parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(AdminStates.waiting_for_year, F.text)
async def receive_movie_year(m: types.Message, state: FSMContext):
    await state.update_data(year=m.text.strip())
    await state.set_state(AdminStates.waiting_for_cats)
    
    builder = InlineKeyboardBuilder()
    for index, cat in enumerate(CATEGORIES): 
        builder.button(text=cat, callback_data=f"selcat_{index}")
    builder.button(text="✅ Done", callback_data="cats_done")
    builder.adjust(2) 
    msg = await m.answer("✅ সাল সেট!\n\n📌 <b>Step 5/5:</b> এবার <b>ক্যাটাগরি সিলেক্ট</b> করুন।", reply_markup=builder.as_markup(), parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(AdminStates.waiting_for_year)
async def fallback_year(m: types.Message):
    msg = await m.answer("⚠️ দয়া করে <b>রিলিজ সাল (টেক্সট)</b> লিখুন। অথবা /cancel লিখুন।", parse_mode="HTML")
    await schedule_auto_delete(msg)

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
    await state.clear()

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

    await c.message.edit_text(
        f"🎉 <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!\n\n"
        f"📂 ক্যাটাগরি: {', '.join(selected_cats)}\n"
        f"📢 সকল ইউজারকে নোটিফিকেশন পাঠানো হচ্ছে...",
        parse_mode="HTML"
    )

    if LOG_CHANNEL_ID:
        try:
            log_kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", url=f"https://t.me/{BOT_USERNAME}?start=new")]]
            log_markup = types.InlineKeyboardMarkup(inline_keyboard=log_kb)
            log_text = (
                f"🎬 <b>New Movie Uploaded</b>\n\n"
                f"🏷 Title: <b>{data['title']}</b>\n"
                f"📺 Quality: <b>{data['quality']}</b>\n"
                f"📅 Year: <b>{data.get('year', 'N/A')}</b>\n"
                f"📂 Categories: {', '.join(selected_cats)}\n\n"
                f"👤 Uploaded by Admin"
            )
            await bot.send_photo(LOG_CHANNEL_ID, photo=data["photo_id"], caption=log_text, parse_mode="HTML", reply_markup=log_markup)
        except: pass

    # ✅ সকল ইউজারকে অটো-ব্রডকাস্ট (২৪ ঘণ্টা পর অটো-ডিলিট)
    asyncio.create_task(run_movie_broadcast(data, selected_cats, c.from_user.id))
    await c.answer()

async def run_movie_broadcast(data, selected_cats, admin_id):
    bcast_success = 0
    bcast_fail = 0
    
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
        f"📂 {', '.join(selected_cats)}\n\n"
        f"👇 এখনই দেখুন!"
    )
    
    delete_at = datetime.datetime.utcnow() + datetime.timedelta(hours=BOT_MSG_DELETE_HOURS)
    
    async for u in db.users.find():
        try:
            sent_msg = await bot.send_photo(u['user_id'], photo=data["photo_id"], caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
            await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at, "type": "broadcast"})
            bcast_success += 1
            await asyncio.sleep(0.3)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                sent_msg = await bot.send_photo(u['user_id'], photo=data["photo_id"], caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
                await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at, "type": "broadcast"})
                bcast_success += 1
            except: 
                bcast_fail += 1
        except: 
            bcast_fail += 1
            
    try:
        report_text = f"✅ <b>অটো-ব্রডকাস্ট শেষ!</b>\n\n✅ সফল: <b>{bcast_success}</b>\n❌ ব্যর্থ: <b>{bcast_fail}</b>\n⏳ নোটিফিকেশন <b>২৪ ঘণ্টা</b> পর অটো-ডিলিট হবে।"
        await bot.send_message(admin_id, report_text, parse_mode="HTML")
    except: pass

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_bcast)
    msg = await m.answer("📢 ব্রডকাস্ট মেসেজ পাঠান।\n\n⚠️ বাতিল করতে /cancel লিখুন।\n⏳ মেসেজ <b>২৪ ঘণ্টা</b> পর অটো-ডিলিট হবে।", parse_mode="HTML")
    await schedule_auto_delete(msg)

@dp.message(AdminStates.waiting_for_bcast)
async def execute_broadcast(m: types.Message, state: FSMContext):
    if m.text and m.text.startswith("/"):
        await state.clear()
        msg = await m.answer("⚠️ ব্রডকাস্ট বাতিল হয়েছে।", parse_mode="HTML")
        await schedule_auto_delete(msg)
        return
    if m.reply_to_message:
        await state.clear()
        msg = await m.answer("⚠️ ব্রডকাস্ট বাতিল! আপনি রিপ্লাই করেছেন!", parse_mode="HTML")
        await schedule_auto_delete(msg)
        return
    await state.clear()
    prog_msg = await m.answer("⏳ <b>Broadcast started...</b>", parse_mode="HTML")
    asyncio.create_task(run_manual_broadcast(m, prog_msg, m.from_user.id))

async def run_manual_broadcast(m, prog_msg, admin_id):
    total_users = await db.users.count_documents({})
    success = 0
    blocked = 0
    delete_at = datetime.datetime.utcnow() + datetime.timedelta(hours=BOT_MSG_DELETE_HOURS)
    
    async for u in db.users.find():
        try: 
            sent_msg = await m.copy_to(chat_id=u['user_id'])
            await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at, "type": "broadcast"})
            success += 1
            await asyncio.sleep(0.05)
        except: 
            blocked += 1
            
    stats_text = f"✅ <b>Broadcast Complete!</b>\n\n👥 Total: <b>{total_users}</b>\n✅ Successful: <b>{success}</b>\n🚫 Blocked: <b>{blocked}</b>\n⏳ মেসেজ <b>২৪ ঘণ্টা</b> পর অটো-ডিলিট হবে।"
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
    if not payment or payment["status"] != "pending": return await c.answer("⚠️ প্রসেস করা হয়েছে!", show_alert=True)
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
            .stat-card { background: #1e293b; padding: 20px; border-radius: 16px; border: 1px solid #334155; }
            .stat-card h3 { margin: 0 0 10px 0; font-size: 14px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }
            .stat-card .value { font-size: 32px; font-weight: 800; color: #fff; }
            .stat-card.users .value i { color: #3b82f6; } .stat-card.today-users .value i { color: #10b981; } .stat-card.clicks .value i { color: #f59e0b; } .stat-card.today-clicks .value i { color: #ef4444; } .stat-card.pending-del .value i { color: #a855f7; }
            .table-container { background: #1e293b; border-radius: 16px; border: 1px solid #334155; overflow-x: auto; }
            .table-header { padding: 20px; border-bottom: 1px solid #334155; display: flex; justify-content: space-between; align-items: center; }
            .table-header h2 { margin: 0; color: #fff; font-size: 20px; }
            table { width: 100%; border-collapse: collapse; min-width: 600px; } th { text-align: left; padding: 15px; color: #94a3b8; font-size: 12px; text-transform: uppercase; border-bottom: 1px solid #334155; } td { padding: 15px; border-bottom: 1px solid #334155; font-size: 14px; color: #e2e8f0; } tr:last-child td { border-bottom: none; } tr:hover { background: rgba(255,255,255,0.03); }
            .view-badge { background: rgba(59, 130, 246, 0.2); color: #60a5fa; padding: 4px 10px; border-radius: 12px; font-weight: 600; font-size: 12px; }
            .delete-btn { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); padding: 6px 12px; border-radius: 8px; cursor: pointer; font-weight: 600; } .delete-btn:hover { background: #ef4444; color: white; }
            .empty-state { text-align: center; padding: 40px; color: #64748b; }
        </style>
    </head>
    <body>
        <div class="header"><h1><i class="fa-solid fa-shield-halved"></i> Admin Panel</h1><p>Movie Box Control Center</p></div>
        <div class="stats-grid">
            <div class="stat-card users"><h3>Total Users</h3><div class="value"><i class="fa-solid fa-users"></i> <span id="totalUsers">0</span></div></div>
            <div class="stat-card today-users"><h3>Today Users</h3><div class="value"><i class="fa-solid fa-user-plus"></i> <span id="todayUsers">0</span></div></div>
            <div class="stat-card clicks"><h3>Total Clicks</h3><div class="value"><i class="fa-solid fa-eye"></i> <span id="totalClicks">0</span></div></div>
            <div class="stat-card pending-del"><h3>Pending Delete</h3><div class="value"><i class="fa-solid fa-clock"></i> <span id="pendingDelete">0</span></div></div>
        </div>
        <div class="table-container"><div class="table-header"><h2><i class="fa-solid fa-film"></i> Movies</h2></div><table><thead><tr><th>Title</th><th>Quality</th><th>Category</th><th>Views</th><th>Action</th></tr></thead><tbody id="movieTableBody"><tr><td colspan="5" class="empty-state">Loading...</td></tr></tbody></table></div>
        <script>
            async function fetchStats() { try { const res = await fetch('/api/admin/stats'); const data = await res.json(); document.getElementById('totalUsers').innerText = data.total_users; document.getElementById('todayUsers').innerText = data.today_users; document.getElementById('totalClicks').innerText = data.total_clicks; document.getElementById('pendingDelete').innerText = data.pending_deletes || 0; } catch(e) {} }
            async function fetchMovies() { try { const res = await fetch('/api/admin/movies'); const movies = await res.json(); const tbody = document.getElementById('movieTableBody'); if(movies.length === 0) { tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No movies.</td></tr>'; return; } tbody.innerHTML = movies.map(m => `<tr id="row-${m._id}"><td><strong>${m.title}</strong><br><small>${m.year || 'N/A'}</small></td><td>${m.quality || 'HD'}</td><td>${(m.categories || []).join(', ')}</td><td><span class="view-badge"><i class="fa-solid fa-eye"></i> ${m.clicks || 0}</span></td><td><button class="delete-btn" onclick="deleteMovie('${m._id}')"><i class="fa-solid fa-trash"></i></button></td></tr>`).join(''); } catch(e) {} }
            async function deleteMovie(id) { if(!confirm("Delete?")) return; try { const res = await fetch(`/api/admin/movie/${id}`, { method: 'DELETE' }); const data = await res.json(); if(data.ok) { document.getElementById(`row-${id}`).remove(); fetchStats(); } } catch(e) {} }
            fetchStats(); fetchMovies(); setInterval(fetchStats, 60000);
        </script>
    </body></html>'''
    return HTMLResponse(html_code)

@app.get("/api/admin/stats")
async def admin_stats(auth: bool = Depends(verify_admin)):
    now = datetime.datetime.utcnow(); today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total_users = await db.users.count_documents({}); today_users = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    total_clicks_res = await db.movies.aggregate([{"$group": {"_id": None, "total": {"$sum": "$clicks"}}}]).to_list(1); total_clicks = total_clicks_res[0]["total"] if total_clicks_res else 0
    try:
        today_clicks = await db.user_unlocks.count_documents({"unlocked_at": {"$gte": today_start}})
    except:
        today_clicks = 0
    pending_deletes = await db.auto_delete.count_documents({})
    return {"total_users": total_users, "today_users": today_users, "total_clicks": total_clicks, "today_clicks": today_clicks, "pending_deletes": pending_deletes}

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
# 9. User API Endpoints
# ==========================================
@app.get("/api/movies")
async def get_movies(request: Request, category: str = "Home", page: int = 1, search: str = ""):
    per_page = 10
    query = {}
    if category != "Home" and category != "Adult Content":
        query["categories"] = category
    elif category == "Adult Content":
        query["categories"] = "Adult Content"
    if search:
        query["$or"] = [{"title": {"$regex": search, "$options": "i"}}, {"year": {"$regex": search, "$options": "i"}}]
    total = await db.movies.count_documents(query)
    total_pages = math.ceil(total / per_page) if total > 0 else 1
    skip = (page - 1) * per_page
    movies = await db.movies.find(query, {"file_id": 0, "file_type": 0}).sort("created_at", -1).skip(skip).limit(per_page).to_list(per_page)
    for m in movies: m["_id"] = str(m["_id"])
    return {"movies": movies, "page": page, "total_pages": total_pages, "total": total}

@app.get("/api/movie/{movie_id}")
async def get_movie_detail(movie_id: str, request: Request):
    movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
    if not movie: raise HTTPException(status_code=404, detail="Movie not found")
    movie["_id"] = str(movie["_id"])
    await db.movies.update_one({"_id": ObjectId(movie_id)}, {"$inc": {"clicks": 1}})
    init_data = request.headers.get("X-Init-Data", "")
    if init_data and validate_tg_data(init_data):
        uid = get_user_id_from_initdata(init_data)
        if uid:
            await db.users.update_one({"user_id": uid}, {"$set": {"last_active": datetime.datetime.utcnow()}})
            try:
                await db.user_unlocks.insert_one({"user_id": uid, "movie_id": movie_id, "unlocked_at": datetime.datetime.utcnow()})
            except: pass
    return movie

@app.post("/api/send_file")
async def send_movie_file(request: Request):
    body = await request.json()
    movie_id = body.get("movie_id", "")
    init_data = body.get("init_data", "")
    if not init_data or not validate_tg_data(init_data):
        raise HTTPException(status_code=403, detail="Invalid auth")
    uid = get_user_id_from_initdata(init_data)
    if not uid: raise HTTPException(status_code=403, detail="Invalid user")
    if uid in banned_cache: raise HTTPException(status_code=403, detail="Banned")

    movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
    if not movie: raise HTTPException(status_code=404, detail="Movie not found")

    if "Adult Content" in movie.get("categories", []):
        user = await db.users.find_one({"user_id": uid})
        if not user or not user.get("is_adult_verified", False):
            return {"status": "need_adult_verify"}

    protect_cfg = await db.settings.find_one({"id": "protect_content"})
    protect = protect_cfg.get("status", False) if protect_cfg else False

    try:
        caption_text = (
            f"🎬 <b>{movie['title']}</b>\n"
            f"📺 {movie['quality']} | 📅 {movie.get('year', 'N/A')}\n\n"
            f"⏳ এই ফাইল <b>১ ঘণ্টা</b> পর অটো-ডিলিট হবে।\n"
            f"📁 দ্রুত ডাউনলোড করুন!"
        )
        if movie["file_type"] == "video":
            sent_msg = await bot.send_video(chat_id=uid, video=movie["file_id"], caption=caption_text, parse_mode="HTML", protect_content=protect)
        else:
            sent_msg = await bot.send_document(chat_id=uid, document=movie["file_id"], caption=caption_text, parse_mode="HTML", protect_content=protect)

        # ✅ মুভি ফাইল ১ ঘণ্টা পর অটো-ডিলিট
        await schedule_file_auto_delete(uid, sent_msg.message_id, hours=MOVIE_FILE_DELETE_HOURS)

        return {"status": "sent", "message": "ফাইল পাঠানো হয়েছে! ⏳ ১ ঘণ্টা পর ডিলিট হবে।"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed: {str(e)}")

@app.get("/api/user")
async def get_user_info(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    if not init_data or not validate_tg_data(init_data): raise HTTPException(status_code=403, detail="Invalid auth")
    uid = get_user_id_from_initdata(init_data)
    if not uid: raise HTTPException(status_code=403, detail="Invalid user")
    user = await db.users.find_one({"user_id": uid})
    if not user: raise HTTPException(status_code=404, detail="User not found")
    now = datetime.datetime.utcnow()
    is_vip = user.get("vip_until", now) > now
    vip_days_left = (user.get("vip_until", now) - now).days if is_vip else 0
    try:
        fav_count = await db.favorites.count_documents({"user_id": uid})
    except:
        fav_count = 0
    return {"user_id": uid, "first_name": user.get("first_name", ""), "is_vip": is_vip, "vip_days_left": vip_days_left, "coins": user.get("coins", 0), "refer_count": user.get("refer_count", 0), "is_adult_verified": user.get("is_adult_verified", False), "fav_count": fav_count}

@app.post("/api/favorite")
async def toggle_favorite(request: Request):
    body = await request.json()
    movie_id = body.get("movie_id", "")
    init_data = body.get("init_data", "")
    if not init_data or not validate_tg_data(init_data): raise HTTPException(status_code=403, detail="Invalid auth")
    uid = get_user_id_from_initdata(init_data)
    if not uid: raise HTTPException(status_code=403, detail="Invalid user")
    existing = await db.favorites.find_one({"user_id": uid, "movie_id": movie_id})
    if existing:
        await db.favorites.delete_one({"_id": existing["_id"]})
        return {"status": "removed"}
    else:
        await db.favorites.insert_one({"user_id": uid, "movie_id": movie_id, "added_at": datetime.datetime.utcnow()})
        return {"status": "added"}

@app.get("/api/favorites")
async def get_favorites(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    if not init_data or not validate_tg_data(init_data): raise HTTPException(status_code=403, detail="Invalid auth")
    uid = get_user_id_from_initdata(init_data)
    if not uid: raise HTTPException(status_code=403, detail="Invalid user")
    favs = await db.favorites.find({"user_id": uid}).to_list(1000)
    movie_ids = [ObjectId(f["movie_id"]) for f in favs]
    movies = await db.movies.find({"_id": {"$in": movie_ids}}, {"file_id": 0, "file_type": 0}).to_list(1000)
    for m in movies: m["_id"] = str(m["_id"])
    return {"movies": movies}

@app.post("/api/verify_18")
async def verify_adult(request: Request):
    body = await request.json()
    init_data = body.get("init_data", "")
    if not init_data or not validate_tg_data(init_data): raise HTTPException(status_code=403, detail="Invalid auth")
    uid = get_user_id_from_initdata(init_data)
    if not uid: raise HTTPException(status_code=403, detail="Invalid user")
    await db.users.update_one({"user_id": uid}, {"$set": {"is_adult_verified": True}})
    return {"status": "verified"}

@app.get("/api/surprise")
async def get_surprise_movie(request: Request):
    result = await db.movies.aggregate([{"$sample": {"size": 1}}]).to_list(1)
    if not result: return {"movie": None}
    movie = result[0]
    movie["_id"] = str(movie["_id"])
    return {"movie": movie}

@app.post("/api/checkin")
async def daily_checkin(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    if not init_data or not validate_tg_data(init_data): raise HTTPException(status_code=403, detail="Invalid auth")
    uid = get_user_id_from_initdata(init_data)
    if not uid: raise HTTPException(status_code=403, detail="Invalid user")
    user = await db.users.find_one({"user_id": uid})
    if not user: raise HTTPException(status_code=404, detail="User not found")
    now = datetime.datetime.utcnow()
    last_checkin = user.get("last_checkin", now - datetime.timedelta(days=2))
    if (now - last_checkin).days < 1:
        hours_left = 24 - int((now - last_checkin).seconds / 3600)
        return {"status": "already", "message": f"Already checked in! Come back in {hours_left} hours."}
    await db.users.update_one({"user_id": uid}, {"$set": {"last_checkin": now}, "$inc": {"coins": 5}})
    return {"status": "success", "coins_earned": 5, "total_coins": user.get("coins", 0) + 5}

@app.get("/api/upcoming")
async def get_upcoming():
    movies = await db.upcoming.find({}).sort("release_date", 1).to_list(20)
    for m in movies: m["_id"] = str(m["_id"])
    return {"movies": movies}

@app.get("/api/settings")
async def get_settings(request: Request):
    init_data = request.headers.get("X-Init-Data", "")
    if not init_data or not validate_tg_data(init_data): raise HTTPException(status_code=403, detail="Invalid auth")
    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    dl_cfg = await db.settings.find_one({"id": "direct_links"})
    direct_links = dl_cfg.get('links', []) if dl_cfg else []
    adl_cfg = await db.settings.find_one({"id": "adult_direct_links"})
    adult_direct_links = adl_cfg.get('links', []) if adl_cfg else []
    return {"tg_link": tg_link, "direct_links": direct_links, "adult_direct_links": adult_direct_links, "adult_channel": "https://t.me/+W5V9-mn08jMyYTE1"}

# ==========================================
# 10. ✅ Main Web App UI
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def web_ui():
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
        body { background: #0f172a; font-family: 'Inter', sans-serif; color: #fff; overscroll-behavior-y: none; } 
        body.oled-mode { background: #000; }
        #welcomeScreen { position: fixed; top:0; left:0; width:100%; height:100%; background: #0f172a; z-index: 99999; display: flex; flex-direction: column; align-items: center; justify-content: center; transition: opacity 0.8s; }
        body.oled-mode #welcomeScreen { background: #000; }
        #welcomeScreen.hide { opacity: 0; visibility: hidden; pointer-events: none; }
        .ws-brand { font-size: 48px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; animation: pulse 1.5s infinite; }
        .ws-bn { font-size: 18px; color: #94a3b8; margin-top: 10px; opacity: 0; animation: fadeUp 1s 0.5s forwards; }
        @keyframes pulse { 0%{transform:scale(1)}50%{transform:scale(1.05)}100%{transform:scale(1)} }
        @keyframes fadeUp { to{opacity:1;transform:translateY(-10px)} }
        header { display: flex; justify-content: center; align-items: center; padding: 15px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: rgba(15,23,42,0.95); backdrop-filter: blur(10px); z-index: 1000; cursor: pointer; }
        body.oled-mode header { background: rgba(0,0,0,0.95); border-color: #1a1a1a; }
        .logo { font-size: 24px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .page-section { display: none; padding-bottom: 80px; }
        .page-section.active { display: block; }
        .cat-row { display: flex; flex-wrap: wrap; gap: 8px; padding: 15px; }
        .cat-chip { background: #1e293b; padding: 8px 16px; border-radius: 20px; white-space: nowrap; cursor: pointer; border: 1px solid #ef4444; font-weight: 600; font-size: 12px; transition: 0.3s; color: #cbd5e1; }
        .cat-chip.active { background: linear-gradient(45deg, #ef4444, #dc2626); border-color: #ef4444; color: white; box-shadow: 0 0 12px rgba(239,68,68,0.4); }
        body.oled-mode .cat-chip { background: #0a0a0a; border-color: #1a1a1a; }
        .movie-list { padding: 0 15px; display: flex; flex-direction: column; gap: 15px; }
        .movie-card { display: flex; background: rgba(30,41,59,0.6); border-radius: 16px; overflow: hidden; border: 1px solid #334155; cursor: pointer; transition: 0.3s; position: relative; }
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
        .bottom-nav { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(15,23,42,0.95); backdrop-filter: blur(10px); border-top: 1px solid #1e293b; display: flex; justify-content: space-around; padding: 10px 0; z-index: 1000; }
        body.oled-mode .bottom-nav { background: rgba(0,0,0,0.95); border-color: #1a1a1a; }
        .nav-item { display: flex; flex-direction: column; align-items: center; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer; border: none; background: none; }
        .nav-item i { font-size: 20px; margin-bottom: 3px; }
        .nav-item.active { color: #ef4444; }
        .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: flex-end; justify-content: center; z-index: 3000; }
        .modal.show { display: flex; }
        .modal-content { background: #1e293b; width: 100%; max-width: 400px; padding: 25px; border-radius: 20px 20px 0 0; max-height: 90vh; overflow-y: auto; position: relative; }
        body.oled-mode .modal-content { background: #000; }
        .detail-img { width: 100%; height: 250px; object-fit: cover; border-radius: 12px; margin-bottom: 15px; }
        .detail-title { font-size: 22px; font-weight: 800; margin-bottom: 5px; }
        .detail-meta { color: #94a3b8; font-size: 14px; margin-bottom: 15px; }
        .close-icon { position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: rgba(0,0,0,0.6); color: #fff; font-size: 18px; display: flex; align-items: center; justify-content: center; cursor: pointer; border: none; }
        .dl-file-btn { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 15px; background: #0f172a; border: 1px solid #334155; color: white; font-weight: 700; border-radius: 10px; margin-bottom: 10px; cursor: pointer; }
        body.oled-mode .dl-file-btn { background: #050505; border-color: #1a1a1a; }
        .dl-file-btn i { color: #ef4444; font-size: 18px; }
        .age-btn { width: 100%; padding: 15px; border-radius: 12px; font-weight: 700; border: none; font-size: 16px; cursor: pointer; margin-top: 15px; }
        .age-yes { background: #ef4444; color: white; }
        .age-no { background: #334155; color: white; }
        .ad-box { text-align: center; padding: 20px; }
        .ad-icon { font-size: 60px; margin-bottom: 10px; color: #fbbf24; }
        .ad-title { color: #fbbf24; font-size: 20px; font-weight: 800; margin-bottom: 15px; }
        .ad-box-orange { background: #ea580c; color: white; padding: 12px; border-radius: 8px; margin-bottom: 10px; font-weight: 600; }
        .ad-box-black { background: #000; color: #e2e8f0; padding: 12px; border-radius: 8px; margin-bottom: 20px; font-size: 14px; }
        .ad-action-btn { width: 100%; padding: 15px; border-radius: 8px; font-weight: 700; border: none; font-size: 16px; cursor: pointer; margin-bottom: 10px; }
        .btn-ad-open { background: #ea580c; color: white; }
        .btn-ad-unlock { background: #10b981; color: white; }
        .btn-ad-tryagain { background: #ef4444; color: white; }
        .search-input { width: 100%; padding: 14px; border-radius: 12px; border: none; outline: none; background: #1e293b; color: #fff; font-size: 15px; border: 1px solid #334155; }
        body.oled-mode .search-input { background: #0a0a0a; border-color: #1a1a1a; }
        .profile-card { background: #1e293b; margin: 15px; border-radius: 16px; padding: 20px; border: 1px solid #334155; }
        body.oled-mode .profile-card { background: #0a0a0a; border-color: #1a1a1a; }
        .profile-action-btn { display: block; width: 100%; padding: 14px; border-radius: 12px; font-weight: 700; text-align: center; margin-bottom: 10px; border: none; color: white; text-decoration: none; cursor: pointer; font-size: 15px; }
        .btn-dark-mode { background: #334155; display: flex; align-items: center; justify-content: center; gap: 10px; }
        .btn-main-ch { background: #24A1DE; }
        .btn-18-ch { background: #ef4444; }
        .skeleton { background: #1e293b; border-radius: 12px; height: 160px; position: relative; overflow: hidden; }
        .skeleton::after { content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.05), transparent); animation: shimmer 1.5s infinite; }
        @keyframes shimmer { 0%{transform:translateX(-100%)}100%{transform:translateX(100%)} }
        .pagination-container { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 20px 15px 80px 15px; }
        .page-btn { background: #1e293b; color: #cbd5e1; border: 1px solid #334155; padding: 10px 15px; border-radius: 10px; font-weight: 700; cursor: pointer; font-size: 14px; }
        body.oled-mode .page-btn { background: #0a0a0a; border-color: #1a1a1a; }
        .page-btn.active { background: linear-gradient(45deg, #ef4444, #dc2626); color: white; border-color: #ef4444; }
        .page-btn:disabled { background: #1e293b; color: #475569; cursor: not-allowed; }
        .empty-state { text-align: center; padding: 40px; color: #64748b; font-size: 16px; }
        .vip-badge { display: inline-block; background: linear-gradient(45deg, #f59e0b, #d97706); color: #000; padding: 4px 10px; border-radius: 8px; font-size: 12px; font-weight: 800; }
        .countdown-timer { color: #fbbf24; font-size: 13px; margin-top: 8px; font-weight: 600; }
    </style>
</head>
<body>
    <div id="welcomeScreen"><div class="ws-brand">🎬 Movie Box</div><div class="ws-bn">মুভি বক্স জগতে স্বাগতম</div></div>
    <header onclick="switchTab('home')"><div class="logo">🎬 Movie Box</div></header>

    <div id="tabHome" class="page-section active">
        <div class="cat-row">
            <div class="cat-chip active" onclick="filterCat('Home', this)">🏠 HOME</div>
            <div class="cat-chip" onclick="filterCat('Bangla', this)">🇧🇩 BANGLA</div>
            <div class="cat-chip" onclick="filterCat('Bangla Dubbed', this)">🗣️ BANGLA DUBBED</div>
            <div class="cat-chip" onclick="filterCat('Hindi Dubbed', this)">🇮🇳 HINDI DUBBED</div>
            <div class="cat-chip" onclick="filterCat('Hollywood', this)">🇺🇸 HOLLYWOOD</div>
            <div class="cat-chip" onclick="filterCat('Web Series', this)">📺 WEB SERIES</div>
            <div class="cat-chip" onclick="filterCat('K-Drama', this)">🇰🇷 K-DRAMA</div>
            <div class="cat-chip" onclick="filterCat('Anime', this)">🎌 ANIME</div>
            <div class="cat-chip" onclick="filterCat('Horror', this)">👻 HORROR</div>
            <div class="cat-chip" onclick="verify18(this)">🔞 ADULT</div>
        </div>
        <div class="movie-list" id="movieListHome"><div class="skeleton"></div><div class="skeleton"></div></div>
        <div id="paginationHome" class="pagination-container"></div>
    </div>

    <div id="tabSearch" class="page-section">
        <div class="search-box" style="padding:15px;"><input type="text" id="searchInputMain" class="search-input" placeholder="🔍 মুভি খুঁজুন..." oninput="debounceSearch()"></div>
        <div class="movie-list" id="movieListSearch"></div>
        <div id="paginationSearch" class="pagination-container"></div>
    </div>

    <div id="tabFav" class="page-section">
        <h3 style="padding:15px;color:#fbbf24;">❤️ ফেভারিট</h3>
        <div class="movie-list" id="movieListFav"></div>
    </div>

    <div id="tabSurprise" class="page-section">
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:60vh;text-align:center;padding:20px;">
            <div style="font-size:80px;margin-bottom:20px;animation:pulse 1.5s infinite;">🎲</div>
            <h2 style="margin-bottom:15px;color:#fbbf24;">মুভি রুলেট!</h2>
            <p style="color:#94a3b8;margin-bottom:30px;">কী দেখবেন ঠিক করতে পারছেন না? বট আপনার জন্য একটি মুভি বেছে নিচ্ছে!</p>
            <button onclick="loadSurprise()" style="padding:15px 40px;background:linear-gradient(45deg,#ff416c,#ff4b2b);color:white;font-weight:800;font-size:18px;border:none;border-radius:12px;cursor:pointer;">🎲 Spin Now!</button>
            <div id="surpriseResult" style="width:100%;margin-top:20px;"></div>
        </div>
    </div>

    <div id="tabProfile" class="page-section">
        <div class="profile-card">
            <div style="text-align:center;margin-bottom:15px;"><div style="font-size:60px;margin-bottom:10px;">👤</div><h2 id="profileName">Loading...</h2><div id="profileVipBadge"></div></div>
            <div style="display:flex;justify-content:space-around;margin-bottom:20px;text-align:center;">
                <div><div style="font-size:24px;font-weight:800;" id="profileCoins">0</div><div style="font-size:12px;color:#94a3b8;">Coins</div></div>
                <div><div style="font-size:24px;font-weight:800;" id="profileFavs">0</div><div style="font-size:12px;color:#94a3b8;">Favorites</div></div>
            </div>
            <button class="profile-action-btn" style="background:linear-gradient(45deg,#10b981,#059669);" onclick="doCheckin()">📋 Daily Check-in (+5 Coins)</button>
            <button class="profile-action-btn btn-dark-mode" onclick="toggleOLED()">🌙 OLED Mode</button>
            <a id="mainChLink" href="#" class="profile-action-btn btn-main-ch">🚀 Join Channel</a>
            <a id="adultChLink" href="#" class="profile-action-btn btn-18-ch">🔴 18+ Channel</a>
        </div>
    </div>

    <!-- Movie Detail Modal -->
    <div id="movieModal" class="modal">
        <div class="modal-content">
            <button class="close-icon" onclick="closeModal()">✕</button>
            <img id="modalImg" class="detail-img" src="" alt="">
            <h2 class="detail-title" id="modalTitle"></h2>
            <div class="detail-meta" id="modalMeta"></div>
            <div id="modalCats" style="margin-bottom:15px;"></div>
            <div id="modalActions"></div>
            <div class="countdown-timer" id="modalTimer"></div>
        </div>
    </div>

    <!-- Age Verification Modal -->
    <div id="ageModal" class="modal">
        <div class="modal-content" style="text-align:center;">
            <button class="close-icon" onclick="closeAgeModal()">✕</button>
            <div style="font-size:60px;margin-bottom:15px;">🔞</div>
            <h2 style="color:#ef4444;margin-bottom:10px;">Age Verification</h2>
            <p style="color:#94a3b8;margin-bottom:20px;">আপনি কি ১৮+?</p>
            <button class="age-btn age-yes" onclick="confirm18()">✅ হ্যাঁ, আমি ১৮+ আছি</button>
            <button class="age-btn age-no" onclick="closeAgeModal()">❌ না</button>
        </div>
    </div>

    <!-- ✅ Ad Modal (ইনকাম সিস্টেম - আগের মতোই) -->
    <div id="adModal" class="modal">
        <div class="modal-content" style="text-align:center;">
            <button class="close-icon" onclick="closeAdModal()">✕</button>
            <div class="ad-box">
                <div class="ad-icon">🎁</div>
                <div class="ad-title">Watch Ad to Unlock!</div>
                <div class="ad-box-orange" id="adLinkBox">
                    <a id="adLinkHref" href="#" target="_blank" style="color:white;text-decoration:none;font-weight:700;">📢 এই লিংকে ক্লিক করুন</a>
                </div>
                <div class="ad-box-black">
                    ⏱️ লিংকে ক্লিক করে ১০ সেকেন্ড অপেক্ষা করুন,<br>তারপর নিচের বাটনে ক্লিক করুন।<br><br>
                    <span id="adProgress">অ্যাড <b>0</b> / <b>2</b> দেখা হয়েছে</span>
                </div>
                <button class="ad-action-btn btn-ad-open" id="adOpenBtn" onclick="openAdLink()">📢 Open Ad Link</button>
                <button class="ad-action-btn btn-ad-unlock" id="adUnlockBtn" onclick="tryUnlock()" style="display:none;">✅ I Watched - Unlock Movie</button>
                <button class="ad-action-btn btn-ad-tryagain" id="adRetryBtn" onclick="tryUnlock()" style="display:none;">🔄 Try Again</button>
            </div>
        </div>
    </div>

    <div class="bottom-nav">
        <button class="nav-item active" onclick="switchTab('home')"><i class="fa-solid fa-house"></i>Home</button>
        <button class="nav-item" onclick="switchTab('search')"><i class="fa-solid fa-magnifying-glass"></i>Search</button>
        <button class="nav-item" onclick="switchTab('fav')"><i class="fa-solid fa-heart"></i>Favs</button>
        <button class="nav-item" onclick="switchTab('surprise')"><i class="fa-solid fa-dice"></i>Surprise</button>
        <button class="nav-item" onclick="switchTab('profile')"><i class="fa-solid fa-user"></i>Profile</button>
    </div>

    <script>
        const tg = window.Telegram && Telegram.WebApp ? Telegram.WebApp : null;
        let initData = '', userId = 0, currentCat = 'Home', currentPage = 1, searchQuery = '';
        let adultVerified = false, myFavorites = new Set(), pendingCatChip = null, searchTimeout = null;
        let appSettings = {}, currentMovieId = null, adsWatched = 0, adLinkOpened = false;
        const AD_COUNT = 2; // ✅ ফিক্সড ২টি অ্যাড

        if (tg) {
            tg.ready(); tg.expand();
            initData = tg.initData || '';
            try { const p = new URLSearchParams(initData); const u = JSON.parse(p.get('user')||'{}'); userId = u.id||0; } catch(e) {}
        }

        setTimeout(() => { const ws = document.getElementById('welcomeScreen'); if(ws) ws.classList.add('hide'); }, 2000);

        async function loadSettings() { try { const r = await fetch('/api/settings',{headers:{'X-Init-Data':initData}}); appSettings = await r.json(); } catch(e) {} }
        loadSettings();

        function switchTab(tab) {
            document.querySelectorAll('.page-section').forEach(s=>s.classList.remove('active'));
            document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
            const m={home:'tabHome',search:'tabSearch',fav:'tabFav',surprise:'tabSurprise',profile:'tabProfile'};
            document.getElementById(m[tab]).classList.add('active');
            const idx={home:0,search:1,fav:2,surprise:3,profile:4};
            document.querySelectorAll('.nav-item')[idx[tab]].classList.add('active');
            if(tab==='home') loadMovies(); if(tab==='fav') loadFavorites(); if(tab==='profile') loadProfile();
        }

        async function loadMovies(page=1) {
            currentPage=page;
            const l=document.getElementById('movieListHome'),p=document.getElementById('paginationHome');
            if(page===1) l.innerHTML='<div class="skeleton"></div><div class="skeleton"></div>';
            try {
                let url=`/api/movies?category=${encodeURIComponent(currentCat)}&page=${page}`;
                if(searchQuery) url+=`&search=${encodeURIComponent(searchQuery)}`;
                const r=await fetch(url,{headers:{'X-Init-Data':initData}}); const d=await r.json();
                renderMovies(d.movies,l); renderPagination(p,d.page,d.total_pages,'loadMovies');
            } catch(e){ l.innerHTML='<div class="empty-state">❌ Error</div>'; }
        }

        function renderMovies(movies,container) {
            if(!movies||!movies.length){container.innerHTML='<div class="empty-state">🎬 কোনো মুভি পাওয়া যায়নি</div>';return;}
            container.innerHTML=movies.map(m=>{
                const isAdult=(m.categories||[]).includes('Adult Content'),isFav=myFavorites.has(m._id);
                return `<div class="movie-card" onclick="openMovie('${m._id}')">
                    <img src="https://via.placeholder.com/110x160/1e293b/94a3b8?text=${encodeURIComponent(m.title.substring(0,8))}" onerror="this.src='https://via.placeholder.com/110x160/1e293b/94a3b8?text=🎬'" alt="${m.title}">
                    ${isAdult&&!adultVerified?'<div class="adult-lock-overlay">🔒</div>':''}
                    <div class="movie-info">
                        <div class="movie-title">${m.title}</div>
                        <div class="movie-meta"><span>📺 ${m.quality||'HD'}</span><span>📅 ${m.year||'N/A'}</span><span>👁️ ${m.clicks||0}</span></div>
                        <div class="movie-cats">${(m.categories||[]).slice(0,3).map(c=>`<span class="movie-cat-tag">${c}</span>`).join('')}</div>
                    </div>
                    <button class="fav-btn ${isFav?'active':''}" onclick="event.stopPropagation();toggleFav('${m._id}',this)">
                        <i class="fa-${isFav?'solid':'regular'} fa-heart"></i>
                    </button></div>`;
            }).join('');
        }

        function renderPagination(container,current,total,fnName) {
            if(total<=1){container.innerHTML='';return;}
            let h=`<button class="page-btn" ${current<=1?'disabled':''} onclick="${fnName}(${current-1})">◀</button>`;
            let s=Math.max(1,current-2),e=Math.min(total,current+2);
            for(let i=s;i<=e;i++) h+=`<button class="page-btn ${i===current?'active':''}" onclick="${fnName}(${i})">${i}</button>`;
            h+=`<button class="page-btn" ${current>=total?'disabled':''} onclick="${fnName}(${current+1})">▶</button>`;
            container.innerHTML=h;
        }

        function filterCat(cat,el) { currentCat=cat;searchQuery='';document.querySelectorAll('.cat-chip').forEach(c=>c.classList.remove('active'));if(el)el.classList.add('active');loadMovies(1); }

        function verify18(el) { if(!adultVerified){pendingCatChip=el;document.getElementById('ageModal').classList.add('show');}else filterCat('Adult Content',el); }
        function confirm18() { adultVerified=true; fetch('/api/verify_18',{method:'POST',headers:{'Content-Type':'application/json','X-Init-Data':initData},body:JSON.stringify({init_data:initData})}).catch(()=>{}); closeAgeModal(); if(pendingCatChip) filterCat('Adult Content',pendingCatChip); }
        function closeAgeModal() { document.getElementById('ageModal').classList.remove('show'); }

        async function openMovie(id) {
            try {
                const r=await fetch(`/api/movie/${id}`,{headers:{'X-Init-Data':initData}}); const m=await r.json();
                currentMovieId=m._id;
                document.getElementById('modalImg').src=`https://via.placeholder.com/400x250/1e293b/94a3b8?text=${encodeURIComponent(m.title.substring(0,12))}`;
                document.getElementById('modalTitle').textContent=m.title;
                document.getElementById('modalMeta').innerHTML=`📺 ${m.quality||'HD'} | 📅 ${m.year||'N/A'} | 👁️ ${m.clicks||0} views`;
                document.getElementById('modalCats').innerHTML=(m.categories||[]).map(c=>`<span style="background:rgba(255,255,255,0.1);padding:3px 8px;border-radius:6px;font-size:10px;font-weight:600;color:#cbd5e1;">${c}</span>`).join(' ');
                document.getElementById('modalActions').innerHTML=`<button class="dl-file-btn" onclick="requestFile('${m._id}')"><span>📥 Get Movie File</span><i class="fa-solid fa-download"></i></button><button class="dl-file-btn" onclick="toggleFav('${m._id}')" style="background:#1e293b;"><span>❤️ Add to Favorites</span><i class="fa-solid fa-heart" style="color:#ef4444;"></i></button>`;
                document.getElementById('modalTimer').innerHTML='⏳ ফাইল ১ ঘণ্টা পর অটো-ডিলিট হবে';
                document.getElementById('movieModal').classList.add('show');
            } catch(e){}
        }
        function closeModal() { document.getElementById('movieModal').classList.remove('show'); }

        // ✅ মুভি ফাইল রিকোয়েস্ট (অ্যাড সিস্টেম সহ - ইনকাম)
        async function requestFile(movieId) {
            let isVip=false;
            try { const r=await fetch('/api/user',{headers:{'X-Init-Data':initData}}); const d=await r.json(); isVip=d.is_vip; } catch(e){}
            if(isVip) { await sendFileNow(movieId); }
            else {
                adsWatched=0; adLinkOpened=false;
                const links=(appSettings.direct_links||[]).concat(appSettings.adult_direct_links||[]);
                if(links.length>0){ const rl=links[Math.floor(Math.random()*links.length)]; document.getElementById('adLinkHref').href=rl; document.getElementById('adLinkHref').textContent='📢 এই লিংকে ক্লিক করুন'; }
                document.getElementById('adTotal').textContent=AD_COUNT;
                document.getElementById('adProgress').innerHTML=`অ্যাড <b>0</b> / <b>${AD_COUNT}</b> দেখা হয়েছে`;
                document.getElementById('adOpenBtn').style.display='block';
                document.getElementById('adUnlockBtn').style.display='none';
                document.getElementById('adRetryBtn').style.display='none';
                document.getElementById('adModal').classList.add('show');
            }
        }

        function openAdLink() {
            const link=document.getElementById('adLinkHref').href;
            if(link&&link!=='#') { window.open(link,'_blank'); adLinkOpened=true; }
            setTimeout(()=>{
                adsWatched++;
                document.getElementById('adProgress').innerHTML=`অ্যাড <b>${adsWatched}</b> / <b>${AD_COUNT}</b> দেখা হয়েছে`;
                document.getElementById('adOpenBtn').style.display='none';
                if(adsWatched>=AD_COUNT) { document.getElementById('adUnlockBtn').style.display='block'; }
                else {
                    document.getElementById('adRetryBtn').style.display='block';
                    const links=(appSettings.direct_links||[]).concat(appSettings.adult_direct_links||[]);
                    if(links.length>0){ const rl=links[Math.floor(Math.random()*links.length)]; document.getElementById('adLinkHref').href=rl; }
                }
            },3000);
        }

        async function tryUnlock() {
            if(adsWatched<AD_COUNT){adLinkOpened=false;document.getElementById('adRetryBtn').style.display='none';document.getElementById('adOpenBtn').style.display='block';return;}
            closeAdModal(); await sendFileNow(currentMovieId);
        }
        function closeAdModal() { document.getElementById('adModal').classList.remove('show'); }

        async function sendFileNow(movieId) {
            try {
                const r=await fetch('/api/send_file',{method:'POST',headers:{'Content-Type':'application/json','X-Init-Data':initData},body:JSON.stringify({movie_id:movieId,init_data:initData})});
                const d=await r.json();
                if(d.status==='sent'){if(tg)tg.showAlert('✅ ফাইল পাঠানো হয়েছে!\\n⏳ ১ ঘণ্টা পর অটো-ডিলিট হবে।');closeModal();}
                else if(d.status==='need_adult_verify'){if(tg)tg.showAlert('🔞 প্রথমে ১৮+ ভেরিফিকেশন করুন।');}
                else{if(tg)tg.showAlert('❌ সমস্যা হয়েছে।');}
            } catch(e){if(tg)tg.showAlert('❌ Error');}
        }

        async function toggleFav(movieId,btnEl) {
            try {
                const r=await fetch('/api/favorite',{method:'POST',headers:{'Content-Type':'application/json','X-Init-Data':initData},body:JSON.stringify({movie_id:movieId,init_data:initData})});
                const d=await r.json();
                if(d.status==='added'){myFavorites.add(movieId);if(btnEl){btnEl.classList.add('active');btnEl.innerHTML='<i class="fa-solid fa-heart"></i>';}}
                else{myFavorites.delete(movieId);if(btnEl){btnEl.classList.remove('active');btnEl.innerHTML='<i class="fa-regular fa-heart"></i>';}}
            } catch(e){}
        }

        async function loadFavorites() {
            const l=document.getElementById('movieListFav'); l.innerHTML='<div class="skeleton"></div>';
            try { const r=await fetch('/api/favorites',{headers:{'X-Init-Data':initData}}); const d=await r.json(); d.movies.forEach(m=>myFavorites.add(m._id)); renderMovies(d.movies,l); } catch(e){l.innerHTML='<div class="empty-state">❌ Error</div>';}
        }

        function debounceSearch() { clearTimeout(searchTimeout); searchTimeout=setTimeout(()=>{searchQuery=document.getElementById('searchInputMain').value.trim();loadSearchResults(1);},500); }

        async function loadSearchResults(page=1) {
            const l=document.getElementById('movieListSearch'),p=document.getElementById('paginationSearch');
            if(!searchQuery){l.innerHTML='<div class="empty-state">🔍 কিছু টাইপ করে খুঁজুন...</div>';p.innerHTML='';return;}
            try { const r=await fetch(`/api/movies?category=Home&page=${page}&search=${encodeURIComponent(searchQuery)}`,{headers:{'X-Init-Data':initData}}); const d=await r.json(); renderMovies(d.movies,l); renderPagination(p,d.page,d.total_pages,'loadSearchResults'); } catch(e){l.innerHTML='<div class="empty-state">❌ Error</div>';}
        }

        async function loadSurprise() {
            const el=document.getElementById('surpriseResult'); el.innerHTML='<div class="skeleton"></div>';
            try { const r=await fetch('/api/surprise',{headers:{'X-Init-Data':initData}}); const d=await r.json();
                if(d.movie){const m=d.movie;el.innerHTML=`<div class="movie-card" onclick="openMovie('${m._id}')" style="margin-top:15px;"><div class="movie-info"><div class="movie-title">🎉 ${m.title}</div><div class="movie-meta"><span>📺 ${m.quality||'HD'}</span><span>📅 ${m.year||'N/A'}</span></div></div></div>`;}
                else{el.innerHTML='<div class="empty-state">কোনো মুভি পাওয়া যায়নি 😢</div>';}
            } catch(e){el.innerHTML='<div class="empty-state">❌ Error</div>';}
        }

        async function loadProfile() {
            try { const r=await fetch('/api/user',{headers:{'X-Init-Data':initData}}); const u=await r.json();
                document.getElementById('profileName').textContent=u.first_name||'User';
                document.getElementById('profileCoins').textContent=u.coins||0;
                document.getElementById('profileFavs').textContent=u.fav_count||0;
                document.getElementById('profileVipBadge').innerHTML=u.is_vip?`<span class="vip-badge">💎 VIP (${u.vip_days_left}d)</span>`:'';
                document.getElementById('mainChLink').href=appSettings.tg_link||'#';
                document.getElementById('adultChLink').href=appSettings.adult_channel||'#';
                if(u.is_adult_verified) adultVerified=true;
            } catch(e){}
        }

        async function doCheckin() {
            try { const r=await fetch('/api/checkin',{method:'POST',headers:{'Content-Type':'application/json','X-Init-Data':initData},body:JSON.stringify({init_data:initData})});
                const d=await r.json(); if(tg) tg.showAlert(d.status==='success'?`✅ +${d.coins_earned} Coins!`:`⚠️ ${d.message}`); loadProfile();
            } catch(e){if(tg)tg.showAlert('❌ Error');}
        }

        function toggleOLED() { document.body.classList.toggle('oled-mode'); localStorage.setItem('oled',document.body.classList.contains('oled-mode')); }
        if(localStorage.getItem('oled')==='true') document.body.classList.add('oled-mode');

        loadMovies();
    </script>
</body>
</html>'''
    return HTMLResponse(html_code)

# ==========================================
# 11. Startup & Main
# ==========================================
@app.on_event("startup")
async def on_startup():
    await init_db()
    await load_admins()
    await load_banned_users()
    asyncio.create_task(auto_delete_worker())
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
