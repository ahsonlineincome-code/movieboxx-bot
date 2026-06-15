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
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse
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
# 2. FSM States (নতুন স্টেট যোগ করা হয়েছে)
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
    # মাল্টি কোয়ালিটি আপলোডের জন্য নতুন স্টেট
    waiting_for_add_file = State()
    waiting_for_add_file_quality = State()

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
    await c.message.answer("✍️ আপনার মেসেজ লিখুন:")
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
# 7.5 Single Movie Upload (Updated for Multi Quality)
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
    await m.answer("⚠️ পোস্টার হিসেবে শুধুমাত্র <b>ছবি</b> পাঠান। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer("✅ এবার <b>কোয়ালিটি</b> লিখুন (যেমন: 720p, S01E01)।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=m.text.strip())
    await state.set_state(AdminStates.waiting_for_year)
    await m.answer("✅ এবার <b>রিলিজ সাল</b> লিখুন।", parse_mode="HTML")

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
    
    # মাল্টিপল কোয়ালিটি স্টোর করার জন্য ডাটাবেস ফরম্যাট আপডেট
    movie_data = {
        "title": data["title"], 
        "photo_id": data["photo_id"], 
        "year": data.get("year", "N/A"), 
        "categories": selected_cats, 
        "clicks": 0, 
        "created_at": datetime.datetime.utcnow(),
        "files": {
            data["quality"]: {
                "file_id": data["file_id"], 
                "file_type": data["file_type"]
            }
        },
        "total_files": 1
    }
    await db.movies.insert_one(movie_data)
    
    await c.message.edit_text(f"🎉 <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!\n\n💡 এই মুভিতে আরও কোয়ালিটি যোগ করতে: <code>/addfile {data['title']}</code>", parse_mode="HTML")
    
    if LOG_CHANNEL_ID:
        try:
            log_kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", url="https://t.me/MovieeBoxx_Bot?start=new")]]
            await bot.send_photo(LOG_CHANNEL_ID, photo=data["photo_id"], caption=f"🎬 <b>{data['title']} [{data['quality']}]</b>\n📅 Year: <b>{data.get('year', 'N/A')}</b>", parse_mode="HTML", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=log_kb))
        except: pass

    await broadcast_queue.put({"data": data, "selected_cats": selected_cats, "admin_id": c.from_user.id})
    await c.answer()

# ==========================================
# 7.6 Multi Quality Add Command (/addfile)
# ==========================================
@dp.message(Command("addfile"))
async def add_file_start(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    try:
        title = m.text.split(" ", 1)[1].strip()
        movie = await db.movies.find_one({"title": title})
        if not movie:
            return await m.answer("⚠️ এই নামে কোনো মুভি পাওয়া যায়নি!", parse_mode="HTML")
        
        await state.set_state(AdminStates.waiting_for_add_file)
        await state.update_data(movie_title=title)
        await m.answer(f"📥 <b>{title}</b> মুভিতে নতুন ফাইল যোগ করতে ভিডিও/ডকুমেন্ট পাঠান:", parse_mode="HTML")
    except:
        await m.answer("⚠️ /addfile Movie_Title লিখুন\n\n💡 টাইটেল হুবহু মিলতে হবে!", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_add_file, F.content_type.in_({'video', 'document'}))
async def receive_add_file(m: types.Message, state: FSMContext):
    fid = m.video.file_id if m.video else m.document.file_id
    ftype = "video" if m.video else "document"
    await state.update_data(new_file_id=fid, new_file_type=ftype)
    await state.set_state(AdminStates.waiting_for_add_file_quality)
    await m.answer("✅ ফাইল পেয়েছি! এবার <b>কোয়ালিটির নাম</b> লিখুন (যেমন: 1080p, 420p):", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_add_file_quality, F.text)
async def save_add_file(m: types.Message, state: FSMContext):
    data = await state.get_data()
    title = data["movie_title"]
    quality_name = m.text.strip()
    
    await db.movies.update_one(
        {"title": title},
        {
            "$set": {f"files.{quality_name}": {"file_id": data["new_file_id"], "file_type": data["new_file_type"]}},
            "$inc": {"total_files": 1}
        }
    )
    await state.clear()
    await m.answer(f"✅ <b>{title}</b> মুভিতে <b>{quality_name}</b> কোয়ালিটি সফলভাবে যোগ হয়েছে!", parse_mode="HTML")

# Broadcast Logic
async def run_movie_broadcast(data, selected_cats, admin_id):
    bcast_success = 0
    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"
    web_app_url = APP_URL if APP_URL else "https://t.me/" 
    bcast_kb = [
        [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=web_app_url))], 
        [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link), types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)]
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
    prog_msg = await m.answer("⏳ <b>Broadcast started...</b>", parse_mode="HTML")
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
    try:
        await prog_msg.edit_text(f"✅ <b>Broadcast Complete!</b>\n\n👥 Total: <b>{total_users}</b>\n✅ Success: <b>{success}</b>\n🚫 Blocked: <b>{blocked}</b>", parse_mode="HTML")
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
    # (পুরানো অ্যাডমিন প্যানেল কোড একই থাকবে, স্পেস বাঁচাতে স্কিপ করা হলো)
    return HTMLResponse("<h1>Admin Panel</h1>")

@app.get("/api/admin/stats")
async def admin_stats(auth: bool = Depends(verify_admin)):
    now = datetime.datetime.utcnow(); today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total_users = await db.users.count_documents({}); today_users = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    total_clicks_res = await db.movies.aggregate([{"$group": {"_id": None, "total": {"$sum": "$clicks"}}}]).to_list(1); total_clicks = total_clicks_res[0]["total"] if total_clicks_res else 0
    return {"total_users": total_users, "today_users": today_users, "total_clicks": total_clicks}

@app.delete("/api/admin/movie/{movie_id}")
async def delete_movie(movie_id: str, auth: bool = Depends(verify_admin)):
    result = await db.movies.delete_one({"_id": ObjectId(movie_id)})
    if result.deleted_count == 1: return {"ok": True}
    raise HTTPException(status_code=404, detail="Movie not found")

# ==========================================
# 9. User Web App APIs & UI (Updated)
# ==========================================
# ট্রেন্ডিং মুভি API (গত ৭ দিনের সবচেয়ে বেশি ক্লিক পাওয়া)
@app.get("/api/trending")
async def get_trending_movies():
    seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    movies = await db.movies.find({"created_at": {"$gte": seven_days_ago}}).sort("clicks", -1).limit(10).to_list(10)
    for m in movies:
        m["_id"] = str(m["_id"])
    return movies

# সাধারণ মুভি লিস্ট API
@app.get("/api/movies")
async def get_movies(cat: str = "All", search: str = ""):
    query = {}
    if cat != "All": query["categories"] = cat
    if search: query["$or"] = [{"title": {"$regex": search, "$options": "i"}}]
    
    movies = await db.movies.find(query).sort("created_at", -1).limit(50).to_list(50)
    for m in movies:
        m["_id"] = str(m["_id"])
    return movies

# মুভি ডিটেইলস API
@app.get("/api/movie/{movie_id}")
async def get_movie_details(movie_id: str):
    movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
    if not movie: raise HTTPException(404)
    movie["_id"] = str(movie["_id"])
    return movie

# পোস্টার লোড করার জন্য টেলিগ্রাম রিডাইরেক্ট (Render Storage বাঁচাতে)
@app.get("/poster/{movie_id}")
async def get_poster(movie_id: str):
    movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
    if not movie: raise HTTPException(404)
    try:
        file = await bot.get_file(movie["photo_id"])
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
        return RedirectResponse(url)
    except:
        raise HTTPException(404)

# ফাইল আনলক এবং বটে পাঠানোর API
class UnlockRequest(BaseModel):
    user_id: int
    movie_id: str
    quality: str

@app.post("/api/unlock")
async def unlock_file(req: UnlockRequest):
    movie = await db.movies.find_one({"_id": ObjectId(req.movie_id)})
    if not movie: return {"ok": False, "error": "Movie not found"}
    
    file_data = movie.get("files", {}).get(req.quality)
    if not file_data: return {"ok": False, "error": "Quality not found"}

    # ক্লিক বাড়ানো
    await db.movies.update_one({"_id": ObjectId(req.movie_id)}, {"$inc": {"clicks": 1}})
    
    try:
        if file_data["file_type"] == "video":
            await bot.send_video(req.user_id, file_data["file_id"], caption=f"🎬 {movie['title']} [{req.quality}]")
        else:
            await bot.send_document(req.user_id, file_data["file_id"], caption=f"🎬 {movie['title']} [{req.quality}]")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ==========================================
# 10. Main Web App Frontend UI (YouTube + Slider Style)
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
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { background: #0f172a; font-family: 'Segoe UI', sans-serif; color: #fff; }
            header { display: flex; justify-content: center; padding: 15px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: #0f172ae6; backdrop-filter: blur(10px); z-index: 100; }
            .logo { font-size: 24px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .cat-row { display: flex; gap: 8px; padding: 12px; overflow-x: auto; scrollbar-width: none; }
            .cat-row::-webkit-scrollbar { display: none; }
            .cat-chip { background: #1e293b; padding: 8px 16px; border-radius: 20px; white-space: nowrap; cursor: pointer; border: 1px solid #334155; font-weight: 600; font-size: 12px; color: #94a3b8; }
            .cat-chip.active { background: linear-gradient(45deg, #ef4444, #dc2626); border-color: #ef4444; color: white; }
            
            /* Trending Slider */
            .trending-section { padding: 5px 12px; }
            .section-title { font-size: 16px; font-weight: 800; margin-bottom: 10px; color: #fff; }
            .t-slider { display: flex; gap: 10px; overflow-x: auto; scroll-behavior: smooth; padding-bottom: 10px; scrollbar-width: none; }
            .t-slider::-webkit-scrollbar { display: none; }
            .t-card { min-width: 120px; cursor: pointer; transition: 0.2s; flex-shrink: 0; }
            .t-card:active { transform: scale(0.95); }
            .t-card img { width: 120px; height: 170px; object-fit: cover; border-radius: 8px; }
            .t-badge { position: absolute; bottom: 5px; left: 5px; background: rgba(0,0,0,0.8); color: #ef4444; font-size: 9px; font-weight: 700; padding: 2px 5px; border-radius: 4px; }
            .t-title { font-size: 11px; font-weight: 600; margin-top: 5px; display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical; overflow: hidden; }

            /* YouTube Style Grid */
            .movie-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; padding: 10px 12px; padding-bottom: 80px; }
            .thumb { cursor: pointer; position: relative; }
            .img-box { position: relative; width: 100%; border-radius: 10px; overflow: hidden; aspect-ratio: 2/3; background: #1e293b; }
            .img-box img { width: 100%; height: 100%; object-fit: cover; }
            .overlay { position: absolute; bottom: 0; left: 0; width: 100%; padding: 5px; display: flex; justify-content: space-between; background: linear-gradient(transparent, rgba(0,0,0,0.9)); }
            .badge { padding: 2px 6px; border-radius: 4px; font-size: 9px; font-weight: 700; }
            .b-year { background: rgba(255,255,255,0.2); color: white; }
            .b-files { background: #3b82f6; color: white; }
            .b-18 { position: absolute; top: 8px; right: 8px; background: #ef4444; color: white; border-radius: 4px; padding: 2px 5px; font-size: 9px; font-weight: 700; }
            .fav-btn { position: absolute; top: 8px; left: 8px; background: rgba(0,0,0,0.6); border: none; width: 25px; height: 25px; border-radius: 50%; color: white; font-size: 10px; cursor: pointer; }
            .thumb-info { padding: 6px 2px; }
            .thumb-title { font-size: 12px; font-weight: 700; line-height: 1.2; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
            .thumb-cat { font-size: 10px; color: #94a3b8; margin-top: 2px; }

            /* Modal & Downloads */
            .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: flex-end; z-index: 200; }
            .modal-content { background: #1e293b; width: 100%; padding: 20px; border-radius: 20px 20px 0 0; max-height: 80vh; overflow-y: auto; }
            .close-icon { position: absolute; top: 12px; right: 15px; width: 30px; height: 30px; border-radius: 50%; background: rgba(0,0,0,0.6); color: #fff; font-size: 16px; display: flex; align-items: center; justify-content: center; cursor: pointer; border: none; }
            .detail-img { width: 100%; height: 220px; object-fit: cover; border-radius: 12px; margin-bottom: 12px; }
            .detail-title { font-size: 20px; font-weight: 800; margin-bottom: 5px; }
            .detail-meta { color: #94a3b8; font-size: 13px; margin-bottom: 15px; }
            .dl-btn { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 14px; background: #0f172a; border: 1px solid #334155; color: white; font-weight: 700; border-radius: 10px; margin-bottom: 10px; cursor: pointer; }
            .dl-btn i { color: #3b82f6; }

            .bottom-nav { position: fixed; bottom: 0; left: 0; width: 100%; background: #0f172ae6; backdrop-filter: blur(10px); border-top: 1px solid #1e293b; display: flex; justify-content: space-around; padding: 10px 0; z-index: 100; }
            .nav-item { display: flex; flex-direction: column; align-items: center; color: #64748b; font-size: 10px; font-weight: 600; cursor: pointer; border: none; background: none; }
            .nav-item i { font-size: 18px; margin-bottom: 2px; }
            .nav-item.active { color: #ef4444; }
        </style>
    </head>
    <body>
        <header><div class="logo">Movie Box</div></header>

        <div class="cat-row" id="catRow"></div>
        
        <div class="trending-section">
            <div class="section-title">🔥 Trending Now</div>
            <div class="t-slider" id="tSlider"></div>
        </div>

        <div class="movie-grid" id="mGrid"></div>

        <div class="modal" id="mModal">
            <div class="modal-content" id="mContent"></div>
        </div>

        <div class="bottom-nav">
            <button class="nav-item active"><i class="fa-solid fa-house"></i>Home</button>
            <button class="nav-item"><i class="fa-solid fa-magnifying-glass"></i>Search</button>
            <button class="nav-item"><i class="fa-solid fa-heart"></i>Favs</button>
        </div>

        <script>
            const tg = window.Telegram.WebApp;
            const userId = tg.initDataUnsafe?.user?.id || 123;
            let currentCat = 'All';
            const cats = ['All', 'Bangla', 'Hindi Dubbed', 'Hollywood', 'K-Drama', 'Anime', 'Web Series', 'Adult Content'];

            function init() {
                renderCats();
                fetchTrending();
                fetchMovies();
            }

            function renderCats() {
                document.getElementById('catRow').innerHTML = cats.map(c => `<div class="cat-chip ${c===currentCat?'active':''}" onclick="selectCat('${c}')">${c}</div>`).join('');
            }

            function selectCat(c) { currentCat = c; renderCats(); fetchMovies(); }

            async function fetchTrending() {
                try {
                    const res = await fetch('/api/trending'); 
                    const movies = await res.json();
                    document.getElementById('tSlider').innerHTML = movies.map(m => `
                        <div class="t-card" onclick="openMovie('${m._id}')">
                            <div style="position:relative">
                                <img src="/poster/${m._id}">
                                <div class="t-badge">🔥 ${m.clicks || 0} Views</div>
                            </div>
                            <div class="t-title">${m.title}</div>
                        </div>
                    `).join('');
                } catch(e) {}
            }

            async function fetchMovies() {
                try {
                    const res = await fetch(`/api/movies?cat=${currentCat}`);
                    const movies = await res.json();
                    document.getElementById('mGrid').innerHTML = movies.map(m => `
                        <div class="thumb" onclick="openMovie('${m._id}')">
                            <div class="img-box">
                                <img src="/poster/${m._id}">
                                ${m.categories.includes('Adult Content') ? '<div class="b-18">18+</div>' : ''}
                                <button class="fav-btn" onclick="event.stopPropagation();"><i class="fa-solid fa-heart"></i></button>
                                <div class="overlay">
                                    <span class="badge b-year">${m.year || 'N/A'}</span>
                                    <span class="badge b-files">${m.total_files || 1} Files</span>
                                </div>
                            </div>
                            <div class="thumb-info">
                                <div class="thumb-title">${m.title}</div>
                                <div class="thumb-cat">${m.categories[0] || 'Movie'}</div>
                            </div>
                        </div>
                    `).join('');
                } catch(e) {}
            }

            async function openMovie(id) {
                try {
                    const res = await fetch(`/api/movie/${id}`);
                    const m = await res.json();
                    const modal = document.getElementById('mModal');
                    const content = document.getElementById('mContent');
                    
                    let filesHtml = '';
                    if(m.files) {
                        for (const [quality, fileData] of Object.entries(m.files)) {
                            filesHtml += `<button class="dl-btn" onclick="unlockFile('${m._id}', '${quality}')">
                                <span><i class="fa-solid fa-download"></i> Download ${quality}</span>
                                <i class="fa-solid fa-arrow-right"></i>
                            </button>`;
                        }
                    }

                    content.innerHTML = `
                        <button class="close-icon" onclick="document.getElementById('mModal').style.display='none'"><i class="fa-solid fa-xmark"></i></button>
                        <img class="detail-img" src="/poster/${m._id}">
                        <div class="detail-title">${m.title}</div>
                        <div class="detail-meta">${m.year} • ${m.categories.join(', ')}</div>
                        ${filesHtml}
                    `;
                    modal.style.display = 'flex';
                } catch(e) {}
            }

            async function unlockFile(movieId, quality) {
                try {
                    const res = await fetch('/api/unlock', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({user_id: userId, movie_id: movieId, quality: quality})
                    });
                    const data = await res.json();
                    if(data.ok) {
                        tg.showPopup({ title: '✅ Unlocked!', message: `Movie will be sent to your bot inbox in ${quality}. Check Bot!` });
                    } else {
                        alert('Error unlocking file. Start the bot first.');
                    }
                } catch(e) {}
            }
            init();
        </script>
    </body>
    </html>'''
    return HTMLResponse(html_code)

# ==========================================
# 11. Runner
# ==========================================
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
