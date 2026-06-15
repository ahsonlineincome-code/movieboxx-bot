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
import re # নতুন যোগ করা হয়েছে (+ সাইন ফিক্স করার জন্য)

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
    
    # /addfile এর জন্য নতুন স্টেট
    waiting_for_addfile_title = State()
    waiting_for_addfile_file = State()
    waiting_for_addfile_quality = State()

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

async def auto_lock_worker():
    while True:
        try:
            expire_time = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
            result = await db.user_unlocks.delete_many({"unlocked_at": {"$lte": expire_time}})
            if result.deleted_count > 0:
                print(f"🔒 Auto-locked {result.deleted_count} movies (24 hrs expired).")
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
# 7.5 Single Movie Upload (Updated for Multiple Files Array)
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
    await m.answer("✅ ফাইল পেয়েছি! এবার <b>পোস্টার</b> পাঠান।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_photo, F.photo)
async def receive_movie_photo(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_title)
    await m.answer("✅ এবার <b>মুভি/সিরিজের নাম</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_photo)
async def fallback_photo(m: types.Message):
    await m.answer("⚠️ পোস্টার হিসেবে শুধুমাত্র <b>ছবি (Photo)</b> পাঠান। ফাইল হিসেবে পাঠাবেন না। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer("✅ এবার <b>এপিসোড বা কোয়ালিটি</b> লিখুন (যেমন: 720p S01)।", parse_mode="HTML")

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
    builder.adjust(3)
    await c.message.edit_reply_markup(reply_markup=builder.as_markup())
    await c.answer()

@dp.callback_query(AdminStates.waiting_for_cats, F.data == "cats_done")
async def finish_category_selection(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    if not selected_cats: return await c.answer("⚠️ অন্তত ১টি সিলেক্ট করুন!", show_alert=True)
    await state.clear()
    
    # নতুন স্ট্রাকচার: files এর মধ্যে ফাইল এবং কোয়ালিটি রাখা হচ্ছে
    initial_file = {
        "quality": data["quality"], 
        "file_id": data["file_id"], 
        "file_type": data["file_type"]
    }
    
    await db.movies.insert_one({
        "title": data["title"], 
        "photo_id": data["photo_id"], 
        "year": data.get("year", "N/A"), 
        "categories": selected_cats, 
        "files": [initial_file],  # এখানে অ্যারে হিসেবে সেভ হবে
        "clicks": 0, 
        "created_at": datetime.datetime.utcnow()
    })
    
    await c.message.edit_text(f"🎉 <b>{data['title']} [{data['quality']}]</b> সফলভাবে যোগ হয়েছে!\n\n⏳ <b>ব্রডকাস্ট কিউতে যোগ করা হয়েছে...</b>", parse_mode="HTML")
    
    if LOG_CHANNEL_ID:
        try:
            log_kb = [
                [types.InlineKeyboardButton(text="🎬 Watch Now", url="https://t.me/MovieeBoxx_Bot?start=new")],
                [types.InlineKeyboardButton(text="🔴 18+ Channel", url="https://t.me/+W5V9-mn08jMyYTE1")],
                [types.InlineKeyboardButton(text="📥 ডাউনলোড কিভাবে করবেন", url="https://t.me/SakibMovieBox/62")],
                [types.InlineKeyboardButton(text="📝 Request Movie", url="https://t.me/requestmoviebox")]
            ]
            log_markup = types.InlineKeyboardMarkup(inline_keyboard=log_kb)
            log_text = f"🎬 <b>New Movie Uploaded</b>\n\n🏷 Title: <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n📅 Year: <b>{data.get('year', 'N/A')}</b>\n📂 Categories: {', '.join(selected_cats)}\n\n👤 Uploaded by Admin"
            await bot.send_photo(LOG_CHANNEL_ID, photo=data["photo_id"], caption=log_text, parse_mode="HTML", reply_markup=log_markup)
        except: pass

    # সরাসরি রান না করে কিউতে পাঠানো হচ্ছে (শুধুমাত্র নতুন মুভির ক্ষেত্রে)
    await broadcast_queue.put({"data": data, "selected_cats": selected_cats, "admin_id": c.from_user.id})
    await c.answer()

# ==========================================
# 7.6 /addfile Command (Add Quality without Broadcast)
# ==========================================
@dp.message(Command("addfile"))
async def addfile_start(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_addfile_title)
    await m.answer("📝 যে মুভিতে নতুন কোয়ালিটি যোগ করতে চান তার <b>সঠিক নাম (Title)</b> লিখুন:\n\n⚠️ নামটি ডেটাবেসের সাথে হুবহু মিলতে হবে।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_addfile_title, F.text)
async def addfile_title_received(m: types.Message, state: FSMContext):
    title = m.text.strip()
    escaped_title = re.escape(title) # + সাইন ফিক্স
    movie = await db.movies.find_one({"title": {"$regex": f"^{escaped_title}$", "$options": "i"}})
    
    if not movie:
        await state.clear()
        return await m.answer("❌ এই নামে কোনো মুভি পাওয়া যায়নি!\n\nসমস্যা হতে পারে: আপনি যে নামটি দিচ্ছেন সেটি ডেটাবেসের নামের সাথে হুবহু মিলছে না।", parse_mode="HTML")
    
    await state.update_data(movie_id=movie["_id"], movie_title=movie["title"])
    await state.set_state(AdminStates.waiting_for_addfile_file)
    await m.answer(f"✅ মুভি পাওয়া গেছে: <b>{movie['title']}</b>!\n\nএখন নতুন <b>ফাইল (ভিডিও/ডকুমেন্ট)</b> পাঠান।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_addfile_file, F.content_type.in_({'video', 'document'}))
async def addfile_file_received(m: types.Message, state: FSMContext):
    fid = m.video.file_id if m.video else m.document.file_id
    ftype = "video" if m.video else "document"
    await state.update_data(file_id=fid, file_type=ftype)
    await state.set_state(AdminStates.waiting_for_addfile_quality)
    await m.answer("✅ ফাইল পেয়েছি! এখন এই ফাইলের <b>কোয়ালিটি (যেমন: 1080p, 420p)</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_addfile_quality, F.text)
async def addfile_quality_received(m: types.Message, state: FSMContext):
    data = await state.get_data()
    movie_id = data["movie_id"]
    new_quality = m.text.strip()
    
    new_file_obj = {
        "quality": new_quality,
        "file_id": data["file_id"],
        "file_type": data["file_type"]
    }
    
    await db.movies.update_one(
        {"_id": movie_id},
        {"$push": {"files": new_file_obj}}
    )
    
    await state.clear()
    await m.answer(f"✅ <b>{data['movie_title']}</b> মুভিতে নতুন কোয়ালিটি (<b>{new_quality}</b>) সফলভাবে যোগ হয়েছে!\n\n🚫 কোনো ব্রডকাস্ট যায়নি।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_addfile_title)
async def fallback_addfile_title(m: types.Message):
    await m.answer("⚠️ দয়া করে মুভির <b>নাম (টেক্সট)</b> লিখুন অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_addfile_file)
async def fallback_addfile_file(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>ভিডিও বা ডকুমেন্ট</b> ফাইল পাঠান। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_addfile_quality)
async def fallback_addfile_quality(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>কোয়ালিটি (টেক্সট)</b> লিখুন (যেমন: 720p)। অথবা /cancel লিখুন।", parse_mode="HTML")


# ==========================================
# Broadcast System
# ==========================================
async def run_movie_broadcast(data, selected_cats, admin_id):
    bcast_success = 0
    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"
    web_app_url = APP_URL if APP_URL else "https://t.me/" 
    bcast_kb = [
        [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=web_app_url))],
        [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link)],
        [types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)],
        [types.InlineKeyboardButton(text="📥 ডাউনলোড কিভাবে করবেন", url="https://t.me/SakibMovieBox/62")],
        [types.InlineKeyboardButton(text="📝 Request Movie", url="https://t.me/requestmoviebox")]
    ]
    bcast_markup = types.InlineKeyboardMarkup(inline_keyboard=bcast_kb)
    bcast_text = f"🆕 <b>New Movie Alert!</b>\n\n🎬 <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n📅 Year: <b>{data.get('year', 'N/A')}</b>\n\n👇 এখনই দেখুন!"
    
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
            async function fetchMovies() { try { const res = await fetch('/api/admin/movies'); const movies = await res.json(); const tbody = document.getElementById('movieTableBody'); if(movies.length === 0) { tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No movies yet.</td></tr>'; return; } tbody.innerHTML = movies.map(m => `<tr id="row-${m._id}"><td><strong>${m.title}</strong><br><small>${m.year || 'N/A'}</small></td><td>${(m.files || []).map(f => f.quality).join(', ') || 'N/A'}</td><td>${(m.categories || []).join(', ')}</td><td><span class="view-badge"><i class="fa-solid fa-eye"></i> ${m.clicks || 0}</span></td><td><button class="delete-btn" onclick="deleteMovie('${m._id}')"><i class="fa-solid fa-trash"></i> Delete</button></td></tr>`).join(''); } catch(e) {} }
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
# 9. Main Web App UI & APIs
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def web_ui():
    dl_cfg = await db.settings.find_one({"id": "direct_links"}); direct_links = dl_cfg.get('links', []) if dl_cfg else []; dl_json = json.dumps(direct_links)
    adl_cfg = await db.settings.find_one({"id": "adult_direct_links"}); adult_direct_links = adl_cfg.get('links', []) if adl_cfg else []; adl_json = json.dumps(adult_direct_links)

    html_code = f'''
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
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ background: #0f172a; font-family: 'Inter', sans-serif; color: #fff; overscroll-behavior-y: none; transition: background 0.3s; }} 
            body.oled-mode {{ background: #000000; }}
            header {{ display: flex; justify-content: center; align-items: center; padding: 15px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); z-index: 1000; cursor: pointer; }}
            .logo {{ display: flex; align-items: center; font-size: 24px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
            .movie-list {{ padding: 0 15px; display: flex; flex-direction: column; gap: 15px; padding-bottom: 80px; }}
            .movie-card {{ display: flex; background: rgba(30, 41, 59, 0.6); border-radius: 16px; overflow: hidden; border: 1px solid #334155; cursor: pointer; transition: 0.3s; position: relative; }}
            .movie-card:active {{ transform: scale(0.98); }}
            .movie-card img {{ width: 110px; height: 160px; object-fit: cover; flex-shrink: 0; }}
            .movie-info {{ padding: 12px; display: flex; flex-direction: column; justify-content: center; flex: 1; }}
            .movie-title {{ font-size: 16px; font-weight: 700; margin-bottom: 5px; line-height: 1.3; }}
            .movie-meta {{ font-size: 12px; color: #94a3b8; margin-bottom: 8px; display: flex; gap: 10px; }}
            .movie-cats {{ display: flex; flex-wrap: wrap; gap: 5px; }}
            .movie-cat-tag {{ background: rgba(255,255,255,0.1); padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: 600; color: #cbd5e1; }}
            
            /* Modal Styles */
            .modal {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: flex-end; justify-content: center; z-index: 3000; }}
            .modal-content {{ background: #1e293b; width: 100%; max-width: 400px; padding: 25px; border-radius: 20px 20px 0 0; max-height: 90vh; overflow-y: auto; position: relative; }}
            .detail-img {{ width: 100%; height: 250px; object-fit: cover; border-radius: 12px; margin-bottom: 15px; }}
            .detail-title {{ font-size: 22px; font-weight: 800; margin-bottom: 5px; }}
            .detail-meta {{ color: #94a3b8; font-size: 14px; margin-bottom: 15px; }}
            .close-icon {{ position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: rgba(0,0,0,0.6); color: #fff; font-size: 18px; display: flex; align-items: center; justify-content: center; cursor: pointer; border: none; }}
            .dl-file-btn {{ display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 15px; background: #0f172a; border: 1px solid #334155; color: white; font-weight: 700; border-radius: 10px; margin-bottom: 10px; cursor: pointer; }}
            .dl-file-btn i {{ color: #ef4444; font-size: 18px; }}
            
            /* Ad & Loading Styles */
            .ad-box {{ text-align: center; padding: 20px; }}
            .loader {{ border: 4px solid #f3f3f3; border-top: 4px solid #ef4444; border-radius: 50%; width: 30px; height: 30px; animation: spin 1s linear infinite; margin: 0 auto 15px auto; }}
            @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
            .btn-action {{ width: 100%; padding: 15px; border-radius: 8px; font-weight: 700; border: none; font-size: 16px; cursor: pointer; margin-bottom: 10px; color: white; }}
            .btn-green {{ background: #10b981; }}
            .btn-red {{ background: #ef4444; }}
        </style>
    </head>
    <body>
        <header onclick="window.location.href='/'"><div class="logo">Movie Box</div></header>

        <div id="movieList" class="movie-list">
            <div style="text-align: center; padding: 50px; color: #64748b;"><div class="loader"></div>Loading Movies...</div>
        </div>

        <!-- Movie Detail Modal -->
        <div id="detailModal" class="modal">
            <div class="modal-content">
                <button class="close-icon" onclick="closeDetailModal()"><i class="fa-solid fa-xmark"></i></button>
                <img id="detailImg" class="detail-img" src="" alt="">
                <h2 id="detailTitle" class="detail-title"></h2>
                <p id="detailMeta" class="detail-meta"></p>
                <div id="downloadButtonsContainer"></div>
            </div>
        </div>

        <!-- Ad Modal -->
        <div id="adModal" class="modal">
            <div class="modal-content" style="text-align: center;">
                <div class="ad-box">
                    <div id="adLoader" class="loader" style="display:none;"></div>
                    <i class="fa-solid fa-circle-play" style="font-size: 50px; color: #fbbf24; margin-bottom: 15px;"></i>
                    <h3 style="color: #fbbf24; margin-bottom: 10px;">Watch Ad to Download</h3>
                    <p style="color: #94a3b8; font-size: 14px; margin-bottom: 20px;">Wait 5 seconds and click the link below to get your file.</p>
                    <a id="adLink" href="#" target="_blank" class="btn-action btn-red" style="display:none; text-decoration: none;">🔗 Open Ad Link</a>
                    <button id="unlockBtn" class="btn-action btn-green" onclick="verifyAdAndUnlock()">✅ I've Watched the Ad</button>
                </div>
            </div>
        </div>

        <script>
            const tg = window.Telegram.WebApp;
            tg.expand();
            const directLinks = {dl_json};
            const adultDirectLinks = {adl_json};
            let currentUser = {{}};
            let currentFileId = null;
            let currentMovieId = null;

            // Init User
            if(tg.initDataUnsafe && tg.initDataUnsafe.user) {{
                currentUser = tg.initDataUnsafe.user;
            }}

            // Fetch Movies
            async function fetchMovies() {{
                try {{
                    const res = await fetch('/api/movies');
                    const movies = await res.json();
                    renderMovies(movies);
                }} catch(e) {{
                    document.getElementById('movieList').innerHTML = '<p style="text-align:center; color:red;">Failed to load movies.</p>';
                }}
            }}

            function renderMovies(movies) {{
                const list = document.getElementById('movieList');
                if(movies.length === 0) {{
                    list.innerHTML = '<p style="text-align:center; color:#64748b;">No movies found.</p>';
                    return;
                }}
                list.innerHTML = movies.map(m => `
                    <div class="movie-card" onclick="openDetail('${m._id}')">
                        <img src="https://telegra.ph/file/${m.photo_id}" alt="${{m.title}}" onerror="this.src='https://via.placeholder.com/110x160/1e293b/94a3b8?text=No+Img'">
                        <div class="movie-info">
                            <div class="movie-title">${{m.title}}</div>
                            <div class="movie-meta"><span>📅 ${{m.year || 'N/A'}}</span> <span>👁 ${{m.clicks || 0}}</span></div>
                            <div class="movie-cats">${{(m.categories || []).map(c => `<span class="movie-cat-tag">${{c}}</span>`).join('')}}</div>
                        </div>
                    </div>
                `).join('');
            }}

            // Movie Detail Modal
            async function openDetail(movieId) {{
                try {{
                    const res = await fetch(`/api/movie/${{movieId}}`);
                    const movie = await res.json();
                    
                    document.getElementById('detailImg').src = `https://telegra.ph/file/${{movie.photo_id}}`;
                    document.getElementById('detailTitle').innerText = movie.title;
                    document.getElementById('detailMeta').innerText = `Year: ${{movie.year || 'N/A'}} | Views: ${{movie.clicks || 0}}`;
                    
                    // Render Multiple Download Buttons
                    const btnContainer = document.getElementById('downloadButtonsContainer');
                    btnContainer.innerHTML = '';
                    
                    if(movie.files && movie.files.length > 0) {{
                        movie.files.forEach(f => {{
                            let btn = document.createElement('button');
                            btn.className = 'dl-file-btn';
                            btn.innerHTML = `<span>Download ${{f.quality}}</span> <i class="fa-solid fa-download"></i>`;
                            btn.onclick = () => initiateDownload(movie._id, f.file_id, movie.categories);
                            btnContainer.appendChild(btn);
                        }});
                    }} else {{
                        btnContainer.innerHTML = '<p style="text-align:center; color:#94a3b8;">No files available</p>';
                    }}
                    
                    document.getElementById('detailModal').style.display = 'flex';
                }} catch(e) {{
                    alert('Error loading movie details.');
                }}
            }}

            function closeDetailModal() {{
                document.getElementById('detailModal').style.display = 'none';
            }}

            // Download & Ad Logic
            function initiateDownload(movieId, fileId, categories) {{
                currentFileId = fileId;
                currentMovieId = movieId;
                
                // Check if user is VIP (simplified check, actual check should be backend)
                // Here we always show ad for demo, backend will verify anyway
                showAdModal(categories);
            }}

            function showAdModal(categories) {{
                const isAdult = categories && categories.includes("Adult Content");
                const links = isAdult ? adultDirectLinks : directLinks;
                
                if(links && links.length > 0) {{
                    const randomLink = links[Math.floor(Math.random() * links.length)];
                    document.getElementById('adLink').href = randomLink;
                    document.getElementById('adLink').style.display = 'block';
                }} else {{
                    document.getElementById('adLink').style.display = 'none';
                }}
                
                document.getElementById('adModal').style.display = 'flex';
                document.getElementById('unlockBtn').disabled = false;
            }}

            async function verifyAdAndUnlock() {{
                if(!currentFileId || !currentMovieId) return;
                
                document.getElementById('unlockBtn').disabled = true;
                document.getElementById('unlockBtn').innerText = 'Verifying...';
                
                try {{
                    const res = await fetch(`/api/unlock?movie_id=${{currentMovieId}}&file_id=${{currentFileId}}&user_id=${{currentUser.id || 0}}`);
                    const data = await res.json();
                    
                    if(data.ok && data.link) {{
                        // Success, redirect to telegram file
                        window.location.href = data.link;
                        document.getElementById('adModal').style.display = 'none';
                    }} else {{
                        alert(data.error || 'Failed to unlock. You might need to watch the ad first.');
                        document.getElementById('unlockBtn').disabled = false;
                        document.getElementById('unlockBtn').innerText = '✅ I\'ve Watched the Ad';
                    }}
                }} catch(e) {{
                    alert('Network error.');
                    document.getElementById('unlockBtn').disabled = false;
                    document.getElementById('unlockBtn').innerText = '✅ I\'ve Watched the Ad';
                }}
            }}

            fetchMovies();
        </script>
    </body></html>'''
    return HTMLResponse(html_code)

# ==========================================
# 10. Web App Backend APIs
# ==========================================
@app.get("/api/movies")
async def get_all_movies():
    movies = await db.movies.find({}).sort("created_at", -1).to_list(1000)
    for m in movies:
        m["_id"] = str(m["_id"])
    return movies

@app.get("/api/movie/{movie_id}")
async def get_movie_detail(movie_id: str):
    movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
    if not movie:
        raise HTTPException(status_code=404, detail="Movie not found")
    movie["_id"] = str(movie["_id"])
    return movie

@app.get("/api/unlock")
async def unlock_file(movie_id: str, file_id: str, user_id: int):
    if user_id == 0:
        raise HTTPException(status_code=403, detail="User not identified")
        
    movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
    if not movie:
        raise HTTPException(status_code=404, detail="Movie not found")
        
    # Find the specific file from the files array
    requested_file = None
    for f in movie.get("files", []):
        if f["file_id"] == file_id:
            requested_file = f
            break
            
    if not requested_file:
        raise HTTPException(status_code=404, detail="File not found")

    # VIP Check
    now = datetime.datetime.utcnow()
    user = await db.users.find_one({"user_id": user_id})
    is_vip = user and user.get("vip_until", now) > now

    if not is_vip:
        # Non-VIP logic: check ad unlock time (simplified, can be expanded)
        unlock_record = await db.user_unlocks.find_one({
            "user_id": user_id, 
            "movie_id": ObjectId(movie_id), 
            "unlocked_at": {"$gte": now - datetime.timedelta(hours=24)}
        })
        if not unlock_record:
            # For strict ad verification, you can return error here. 
            # For now, we log the unlock and allow
            await db.user_unlocks.insert_one({
                "user_id": user_id,
                "movie_id": ObjectId(movie_id),
                "unlocked_at": now
            })

    # Increment click count
    await db.movies.update_one({"_id": ObjectId(movie_id)}, {"$inc": {"clicks": 1}})

    # Generate Telegram link (Protect content check can be added here)
    cfg = await db.settings.find_one({"id": "protect_content"})
    protect = cfg.get("status", False) if cfg else False
    
    if requested_file["file_type"] == "video":
        link = f"https://t.me/{BOT_USERNAME}?start=dl_{requested_file['file_id']}" # You need a bot handler for this or use direct API
        # Direct API approach (Works if file is cached on Telegram server)
        try:
            file_info = await bot.get_file(requested_file["file_id"])
            link = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
        except:
            pass # Fallback to t.me link if file is too large
    else:
        link = f"https://t.me/{BOT_USERNAME}?start=dl_{requested_file['file_id']}"

    return {"ok": True, "link": link}
