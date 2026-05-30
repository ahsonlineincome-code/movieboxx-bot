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
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

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

CATEGORIES = ["Bangla", "Bangla Dubbed", "Hindi Dubbed", "Hollywood", "K-Drama", "Horror", "Action", "Web Series", "Adult Content"]

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

@dp.message(lambda m: m.chat.type == "private" and m.from_user.id not in admin_cache)
async def forward_to_admin(m: types.Message):
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ রিপ্লাই", callback_data=f"reply_{m.from_user.id}")
        await bot.send_message(OWNER_ID, f"📩 <a href='tg://user?id={m.from_user.id}'>{m.from_user.first_name}</a>:\n\n{m.text or 'Media'}", parse_mode="HTML", reply_markup=builder.as_markup())
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

@dp.message(Command("setad"))
async def set_ad_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer("✅ অ্যাড জোন লিংক অ্যাড হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /setad url", parse_mode="HTML")

@dp.message(Command("settg"))
async def set_tg_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "tg_link"}, {"$set": {"url": link}}, upsert=True)
        await m.answer("✅ টেলিগ্রাম চ্যানেল লিংক আপডেট হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /settg https://t.me/...", parse_mode="HTML")

@dp.message(Command("set18"))
async def set_18_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "adult_direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer("✅ ১৮+ অ্যাড লিংক অ্যাড হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /set18 url", parse_mode="HTML")

@dp.message(Command("del"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        title = m.text.split(" ", 1)[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0: await m.answer(f"✅ '<b>{title}</b>' ডিলিট হয়েছে!", parse_mode="HTML")
        else: await m.answer("⚠️ পাওয়া যায়নি")
    except: await m.answer("⚠️ /del মুভির নাম", parse_mode="HTML")

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

@dp.message(F.content_type.in_({'video', 'document'}), lambda m: m.from_user.id in admin_cache)
async def receive_movie_file(m: types.Message, state: FSMContext):
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

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer("✅ এবার <b>এপিসোড বা কোয়ালিটি</b> লিখুন।", parse_mode="HTML")

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
    for cat in CATEGORIES: builder.button(text=cat, callback_data=f"selcat_{cat}")
    builder.button(text="✅ Done", callback_data="cats_done")
    builder.adjust(2)
    await m.answer("✅ এবার <b>ক্যাটাগরি সিলেক্ট</b> করুন।", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(AdminStates.waiting_for_cats, F.data.startswith("selcat_"))
async def process_category_selection(c: types.CallbackQuery, state: FSMContext):
    cat = c.data.split("_")[1]
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    if cat in selected_cats: selected_cats.remove(cat)
    else: selected_cats.append(cat)
    await state.update_data(categories=selected_cats)
    builder = InlineKeyboardBuilder()
    for ct in CATEGORIES:
        prefix = "✅ " if ct in selected_cats else ""
        builder.button(text=f"{prefix}{ct}", callback_data=f"selcat_{ct}")
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
    
    await db.movies.insert_one({"title": data["title"], "quality": data["quality"], "photo_id": data["photo_id"], "file_id": data["file_id"], "file_type": data["file_type"], "year": data.get("year", "N/A"), "categories": selected_cats, "clicks": 0, "created_at": datetime.datetime.utcnow()})
    await c.message.edit_text(f"🎉 <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!\n\n📢 সকল ইউজারকে নোটিফিকেশন পাঠানো হচ্ছে...", parse_mode="HTML")
    
    bcast_success = 0
    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"
    
    bcast_kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))], [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link), types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)]]
    bcast_markup = types.InlineKeyboardMarkup(inline_keyboard=bcast_kb)
    bcast_text = f"🆕 <b>New Movie Alert!</b>\n\n🎬 <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n📅 Year: <b>{data.get('year', 'N/A')}</b>\n\n👇 এখনই দেখুন!"
    
    now = datetime.datetime.utcnow()
    time_cfg = await db.settings.find_one({"id": "del_time"})
    del_minutes = time_cfg['minutes'] if time_cfg else 60
    delete_at = now + datetime.timedelta(minutes=del_minutes)
    
    async for u in db.users.find():
        try:
            sent_msg = await bot.send_photo(u['user_id'], photo=data["photo_id"], caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
            await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at})
            bcast_success += 1
            await asyncio.sleep(0.05)
        except: pass
            
    await c.message.answer(f"✅ অটো-ব্রডকাস্ট শেষ!\n\nসফলভাবে পাঠানো হয়েছে: <b>{bcast_success}</b> জনকে।\n⏳ নোটিফিকেশনগুলো <b>{del_minutes} মিনিট</b> পর অটো-ডিলিট হবে।", parse_mode="HTML")

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_bcast)
    await m.answer("📢 ব্রডকাস্ট মেসেজ পাঠান।")

@dp.message(AdminStates.waiting_for_bcast)
async def execute_broadcast(m: types.Message, state: FSMContext):
    await state.clear()
    success = 0
    async for u in db.users.find():
        try: 
            # মেসেজটি হুবহু কপি করে ইউজারের কাছে পাঠানো হচ্ছে, কোনো অতিরিক্ত বাটন যুক্ত করা হচ্ছে না
            await m.copy_to(chat_id=u['user_id'])
            success += 1
            await asyncio.sleep(0.05)
        except: pass
    await m.answer(f"✅ {success} জনকে পাঠানো হয়েছে।", parse_mode="HTML")

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
            .ad-timer-text { color: #94a3b8; margin-bottom: 15px; font-size: 14px; display: none; }
            .ad-action-btn { width: 100%; padding: 15px; border-radius: 8px; font-weight: 700; border: none; font-size: 16px; cursor: pointer; }
            .btn-ad-open { background: #ea580c; color: white; margin-bottom: 10px; }
            .btn-ad-unlock { background: #10b981; color: white; margin-bottom: 10px; }
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
        </style>
    </head>
    <body>
        <div id="welcomeScreen"><div class="ws-brand">Movie Box</div><div class="ws-bn">মুভি বক্স জগতে স্বাগতম</div></div>
        <header onclick="switchTab('home')"><div class="logo"><svg width="35" height="35" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" style="margin-right: 8px; vertical-align: middle;"><defs><linearGradient id="logoGrad" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" style="stop-color:#ff416c;stop-opacity:1" /><stop offset="100%" style="stop-color:#ff4b2b;stop-opacity:1" /></linearGradient></defs><rect x="10" y="15" width="80" height="70" rx="15" ry="15" fill="none" stroke="url(#logoGrad)" stroke-width="6"/><polygon points="40,32 40,68 72,50" fill="url(#logoGrad)"/><path d="M 35 85 L 25 95 L 75 95 L 65 85" stroke="url(#logoGrad)" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>Movie Box</div></header>

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
                <div class="cat-chip" onclick="filterCat('Horror', this)">HORROR</div>
                <div class="cat-chip" onclick="verify18(this)">ADULT CONTENT</div>
            </div>
            <div class="movie-list" id="movieListHome"><div class="skeleton"></div><div class="skeleton"></div></div>
        </div>

        <div id="tabSearch" class="page-section"><div class="search-box" style="padding-top:15px;"><input type="text" id="searchInputMain" class="search-input" placeholder="🔍 সার্চ..." oninput="searchMovies()"></div><div class="movie-list" id="movieListSearch"></div></div>
        <div id="tabFav" class="page-section"><h3 style="padding: 15px; color: #fbbf24;">❤️ ফেভারিট</h3><div class="movie-list" id="movieListFav"></div></div>
        
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

        <div id="detailModal" class="modal">
            <div class="modal-content">
                <button class="close-icon" onclick="closeModal('detailModal')"><i class="fa-solid fa-xmark"></i></button>
                <img id="detailImg" class="detail-img" src="">
                <h2 id="detailTitle" class="detail-title"></h2>
                <div id="detailMeta" class="detail-meta"></div>
                <div id="detailCats" style="margin-bottom: 15px;"></div>
                <div id="fileButtonsContainer"></div>
            </div>
        </div>

        <div id="adModal" class="modal">
            <div class="modal-content ad-box">
                <div class="ad-icon">⚠️</div>
                <h2 class="ad-title">সতর্কতা!</h2>
                <div class="ad-box-orange">ডাউনলোড করতে হলে অবশ্যই বিজ্ঞাপন দেখুন!</div>
                <div class="ad-box-black">ক্লিক করে কমপক্ষে <b>১৫ সেকেন্ড</b> অপেক্ষা করুন।</div>
                <p id="adTimerText" class="ad-timer-text">অপেক্ষা করুন... <span id="timerCount">15</span>s</p>
                <button class="ad-action-btn btn-ad-open" id="adClickBtn" onclick="openAdLink()">বিজ্ঞাপন খুলুন</button>
                <button class="ad-action-btn btn-ad-tryagain" id="adTryAgainBtn" onclick="adTryAgainAction()" style="display:none;">TRY AGAIN</button>
            </div>
        </div>

        <div id="successModal" class="modal">
            <div class="modal-content" style="text-align: center; padding-top: 40px;">
                <i class="fa-solid fa-circle-check" style="font-size:70px; color:#4ade80; margin-bottom:20px;"></i>
                <h2>ফাইল পাঠানো হয়েছে!</h2>
                <p style="color:#94a3b8; margin-top:10px;">বট চেক করুন। নতুন মুভি আপডেট পেতে চ্যানেলে জয়েন করুন!</p>
                <a href="https://t.me/addlist/MwbWNafSFK4yZjhl" target="_blank" class="join-channel-btn">🚀 Join Channel</a>
                <button class="dl-file-btn unlocked" onclick="closeModal('successModal'); tg.close();"><i class="fa-solid fa-check"></i> বটে যান</button>
            </div>
        </div>

        <script>
            let tg = window.Telegram.WebApp; tg.expand();
            const DIRECT_LINKS = __DL_JSON__; const ADULT_DIRECT_LINKS = __ADL_JSON__; const INIT_DATA = tg.initData || ""; 
            let uid = tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user.id : 0; let isUserVip = false; let activeCat = "Home"; let userFavs = []; let active18Btn = null; let activeFileId = null; let activeIsAdult = false; let adInterval = null; let adTimeLeft = 15; let adCompleted = false; let adAborted = false; let currentViewMovies = [];

            setTimeout(function() { document.getElementById('welcomeScreen').classList.add('hide'); }, 2500);
            if(tg.initDataUnsafe && tg.initDataUnsafe.user) { document.getElementById('profileName').innerText = tg.initDataUnsafe.user.first_name; }
            async function fetchUserInfo() { try { const res = await fetch('/api/user/' + uid); const data = await res.json(); isUserVip = data.vip; } catch(e) {} }
            function switchTab(tabName, btnEl) { document.querySelectorAll('.page-section').forEach(function(el) { el.classList.remove('active'); }); document.querySelectorAll('.nav-item').forEach(function(el) { el.classList.remove('active'); }); if(tabName === 'home') { activeCat = 'Home'; document.querySelectorAll('.cat-chip').forEach(function(el) { el.classList.remove('active'); }); var fc = document.querySelector('.cat-chip'); if(fc) fc.classList.add('active'); } document.getElementById('tab' + tabName.charAt(0).toUpperCase() + tabName.slice(1)).classList.add('active'); if(btnEl) btnEl.classList.add('active'); if(tabName === 'home') loadHomeMovies(); if(tabName === 'fav') loadFavorites(); window.scrollTo({top:0, behavior:'smooth'}); }
            function filterCat(cat, btnEl) { activeCat = cat; document.querySelectorAll('.cat-chip').forEach(function(el) { el.classList.remove('active'); }); btnEl.classList.add('active'); loadHomeMovies(); }
            
            function verify18(btnEl) { active18Btn = btnEl; if(localStorage.getItem('isAdult')) { if(btnEl) filterCat('Adult Content', btnEl); } else { document.getElementById('ageModal').style.display = 'flex'; } }
            function access18() { localStorage.setItem('isAdult', 'true'); closeModal('ageModal'); if(active18Btn) { filterCat('Adult Content', active18Btn); } else { loadHomeMovies(); } }
            
            function closeModal(id) { document.getElementById(id).style.display = 'none'; }
            function toggleOledMode() { document.body.classList.toggle('oled-mode'); let sEl = document.getElementById('darkModeStatus'); if(document.body.classList.contains('oled-mode')) { sEl.innerText = 'ON'; localStorage.setItem('oledMode', 'true'); } else { sEl.innerText = 'OFF'; localStorage.setItem('oledMode', 'false'); } }
            if(localStorage.getItem('oledMode') === 'true') { document.body.classList.add('oled-mode'); document.getElementById('darkModeStatus').innerText = 'ON'; }
            async function loadHomeMovies() { const list = document.getElementById('movieListHome'); list.innerHTML = '<div class="skeleton"></div>'; try { const res = await fetch('/api/list?cat='+activeCat+'&uid='+uid); const data = await res.json(); currentViewMovies = data.movies || []; list.innerHTML = currentViewMovies.length > 0 ? currentViewMovies.map(function(m, index) { return createMovieCard(m, index); }).join('') : '<p style="text-align:center; color:#64748b; padding:30px;">কোনো মুভি পাওয়া যায়নি!</p>'; } catch(e) {} }
            async function searchMovies() { const q = document.getElementById('searchInputMain').value.trim(); const list = document.getElementById('movieListSearch'); if(!q) { list.innerHTML = ''; return; } try { const res = await fetch('/api/list?q='+encodeURIComponent(q)+'&uid='+uid); const data = await res.json(); currentViewMovies = data.movies || []; list.innerHTML = currentViewMovies.length > 0 ? currentViewMovies.map(function(m, index) { return createMovieCard(m, index); }).join('') : '<p style="text-align:center; color:#64748b;">খুঁজে পাওয়া যায়নি!</p>'; } catch(e) {} }

            function createMovieCard(m, index) { 
                let isFav = userFavs.includes(m._id); 
                let isAdult = m.categories && m.categories.includes("Adult Content");
                let isVerified = localStorage.getItem('isAdult') === 'true';
                let catsHtml = (m.categories || []).map(function(c) { return `<span class="movie-cat-tag">${c}</span>`; }).join(''); 
                let imgSrc = (isAdult && !isVerified) ? 'https://via.placeholder.com/110x160/1e293b/ef4444?text=18%2B+🔒' : `/api/image/${m.photo_id}`;
                let lockOverlay = (isAdult && !isVerified) ? `<div class="adult-lock-overlay"><i class="fa-solid fa-lock"></i></div>` : '';
                let clickAction = (isAdult && !isVerified) ? `onclick="verify18(null)"` : `onclick="openDetail(${index})"`;
                
                return `<div class="movie-card" ${clickAction}>
                            <div style="position: relative; flex-shrink: 0;">
                                <img src="${imgSrc}" style="width: 110px; height: 160px; object-fit: cover;">
                                ${lockOverlay}
                            </div>
                            <div class="movie-info"><div class="movie-title">${m._id}</div><div class="movie-meta"><span>${m.year || 'N/A'}</span><span>${m.files ? m.files.length : 0} Files</span></div><div class="movie-cats">${catsHtml}</div></div>
                            <button class="fav-btn ${isFav ? 'active' : ''}" onclick="event.stopPropagation(); toggleFav('${m._id}', this)"><i class="fa-solid fa-heart"></i></button>
                        </div>`; 
            }

            function openDetail(index) { let m = currentViewMovies[index]; if(!m) return; document.getElementById('detailImg').src = `/api/image/${m.photo_id}`; document.getElementById('detailTitle').innerText = m._id; document.getElementById('detailMeta').innerHTML = `<span>${m.year || 'N/A'}</span>`; document.getElementById('detailCats').innerHTML = (m.categories || []).map(function(c) { return `<span class="movie-cat-tag">${c}</span>`; }).join(' '); let isAdult = m.is_adult || false; let btnsHtml = m.files.map(function(f) { let isFree = f.is_unlocked || isUserVip; return `<button class="dl-file-btn ${isFree ? 'unlocked' : ''}" onclick="handleFileClick('${f.id}', ${isFree ? 'true' : 'false'}, ${isAdult ? 'true' : 'false'})"><span><i class="fa-solid fa-${isFree ? 'lock-open' : 'lock'}"></i> Download ${f.quality}</span></button>`; }).join(''); document.getElementById('fileButtonsContainer').innerHTML = btnsHtml; document.getElementById('detailModal').style.display = 'flex'; }
            
            function handleFileClick(fileId, isFree, isAdult) { activeFileId = fileId; activeIsAdult = isAdult; if(isFree) { sendFileRequest(fileId); } else { closeModal('detailModal'); resetAdModal(); document.getElementById('adModal').style.display = 'flex'; } }
            function resetAdModal() { clearInterval(adInterval); adTimeLeft = 15; adCompleted = false; adAborted = false; document.getElementById('adTimerText').style.display = 'none'; document.getElementById('adClickBtn').style.display = 'block'; document.getElementById('adClickBtn').className = 'ad-action-btn btn-ad-open'; document.getElementById('adTryAgainBtn').style.display = 'none'; }
            
            function handleAppFocus() { if(!adCompleted && !adAborted && adTimeLeft > 0) { clearInterval(adInterval); adAborted = true; document.getElementById('adTimerText').style.display = 'none'; document.getElementById('adClickBtn').style.display = 'none'; document.getElementById('adTryAgainBtn').style.display = 'block'; document.getElementById('adTryAgainBtn').innerText = 'TRY AGAIN'; document.getElementById('adTryAgainBtn').className = 'ad-action-btn btn-ad-tryagain'; window.removeEventListener('focus', handleAppFocus); document.removeEventListener('visibilitychange', handleVisibilityChange); } }
            function handleVisibilityChange() { if (document.visibilityState === 'visible' && !adCompleted && adTimeLeft > 0) { clearInterval(adInterval); adAborted = true; document.getElementById('adTimerText').style.display = 'none'; document.getElementById('adTryAgainBtn').style.display = 'block'; document.getElementById('adTryAgainBtn').innerText = 'TRY AGAIN'; document.getElementById('adTryAgainBtn').className = 'ad-action-btn btn-ad-tryagain'; document.removeEventListener('visibilitychange', handleVisibilityChange); window.removeEventListener('focus', handleAppFocus); } }
            
            function openAdLink() { let linkToOpen = null; if(activeIsAdult && ADULT_DIRECT_LINKS && ADULT_DIRECT_LINKS.length > 0) { linkToOpen = ADULT_DIRECT_LINKS[Math.floor(Math.random() * ADULT_DIRECT_LINKS.length)]; } else if(DIRECT_LINKS && DIRECT_LINKS.length > 0) { linkToOpen = DIRECT_LINKS[Math.floor(Math.random() * DIRECT_LINKS.length)]; } if(linkToOpen) { tg.openLink(linkToOpen); } document.getElementById('adClickBtn').style.display = 'none'; document.getElementById('adTimerText').style.display = 'block'; window.addEventListener('focus', handleAppFocus); document.addEventListener('visibilitychange', handleVisibilityChange); adInterval = setInterval(function() { adTimeLeft--; document.getElementById('timerCount').innerText = adTimeLeft; if(adTimeLeft <= 0) { clearInterval(adInterval); adCompleted = true; window.removeEventListener('focus', handleAppFocus); document.removeEventListener('visibilitychange', handleVisibilityChange); document.getElementById('adTimerText').style.display = 'none'; document.getElementById('adTryAgainBtn').style.display = 'block'; document.getElementById('adTryAgainBtn').innerText = 'UNLOCK FILE'; document.getElementById('adTryAgainBtn').className = 'ad-action-btn btn-ad-unlock'; } }, 1000); }
            function adTryAgainAction() { if(adCompleted) { closeModal('adModal'); sendFileRequest(activeFileId); } else { resetAdModal(); } }

            async function sendFileRequest(fileId) { try { const res = await fetch('/api/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({userId: uid, movieId: fileId, initData: INIT_DATA})}); const data = await res.json(); if(data.ok) { closeModal('detailModal'); document.getElementById('successModal').style.display = 'flex'; fetchUserInfo(); } else { tg.showAlert("⚠️ Failed!"); } } catch(e) {} }
            async function loadFavorites() { const list = document.getElementById('movieListFav'); list.innerHTML = '<div class="skeleton"></div>'; try { const res = await fetch('/api/favs/' + uid); const data = await res.json(); userFavs = data.map(function(m) { return m._id; }); currentViewMovies = data; list.innerHTML = data.length > 0 ? data.map(function(m, index) { return createMovieCard(m, index); }).join('') : '<p style="text-align:center; color:#64748b; padding:30px;">কোনো ফেভারিট নেই!</p>'; } catch(e) {} }
            async function toggleFav(title, btnEl) { try { const res = await fetch('/api/fav/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({uid: uid, title: title, initData: INIT_DATA})}); const data = await res.json(); if(data.isFav) { btnEl.classList.add('active'); userFavs.push(title); } else { btnEl.classList.remove('active'); userFavs = userFavs.filter(function(t) { return t !== title; }); } } catch(e) {} }
            async function loadSurprise() { try { const res = await fetch('/api/random'); const data = await res.json(); if(data.movie) { currentViewMovies = [data.movie]; openDetail(0); } else { tg.showAlert("⚠️ ডাটাবেসে কোনো মুভি নেই!"); } } catch(e) {} }
            document.getElementById('searchInput').addEventListener('focus', function() { document.querySelector('.nav-item:nth-child(2)').click(); setTimeout(function() { document.getElementById('searchInputMain').focus(); }, 100); });
            fetchUserInfo(); loadHomeMovies(); loadFavorites();
        </script>
    </body></html>'''
    html_code = html_code.replace("__DL_JSON__", dl_json)
    html_code = html_code.replace("__ADL_JSON__", adl_json) 
    return html_code

# ==========================================
# 10. Main Web App APIs
# ==========================================
@app.get("/api/user/{uid}")
async def get_user_info(uid: int):
    now = datetime.datetime.utcnow()
    await db.users.update_one({"user_id": uid}, {"$set": {"last_active": now}})
    user = await db.users.find_one({"user_id": uid})
    if not user: return {"vip": False}
    vip_until = user.get("vip_until")
    is_vip = vip_until and vip_until > now
    return {"vip": is_vip}

@app.get("/api/list")
async def list_movies(page: int = 1, q: str = "", uid: int = 0, cat: str = "Home"):
    if uid in banned_cache: return {"movies": []}
    limit = 20
    unlocked_ids = []
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}): unlocked_ids.append(u["movie_id"])
    match_stage = {}
    if q: match_stage["title"] = {"$regex": q, "$options": "i"}
    if cat and cat != "Home": match_stage["categories"] = {"$in": [cat]}
    pipeline = [
        {"$match": match_stage}, 
        {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "clicks": {"$sum": "$clicks"}, "created_at": {"$max": "$created_at"}, "year": {"$first": "$year"}, "categories": {"$first": "$categories"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main"]}}}}}, 
        {"$sort": {"created_at": -1}}, {"$skip": (page - 1) * limit}, {"$limit": limit}
    ]
    movies = await db.movies.aggregate(pipeline).to_list(limit)
    for m in movies:
        m["is_adult"] = "Adult Content" in m.get("categories", [])
        for f in m["files"]: f["is_unlocked"] = f["id"] in unlocked_ids
    return {"movies": movies}

@app.get("/api/random")
async def random_movie():
    pipeline = [{"$sample": {"size": 1}}]
    movies = await db.movies.aggregate(pipeline).to_list(1)
    if not movies: return {"movie": None}
    m = movies[0]
    return {"movie": {"_id": m["title"], "photo_id": m["photo_id"], "year": m.get("year", "N/A"), "categories": m.get("categories", []), "is_adult": "Adult Content" in m.get("categories", []), "files": [{"id": str(m["_id"]), "quality": m.get("quality", "Main")}]}}

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

class SendRequestModel(BaseModel):
    userId: int; movieId: str; initData: str

@app.post("/api/send")
async def send_file(d: SendRequestModel):
    if d.userId == 0 or d.userId in banned_cache or not validate_tg_data(d.initData): return {"ok": False}
    try:
        m = await db.movies.find_one({"_id": ObjectId(d.movieId)})
        if m:
            now = datetime.datetime.utcnow()
            user_data = await db.users.find_one({"user_id": d.userId})
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
                caption = base_caption + f"\n\n⏳ সতর্কতা: সিকিউরিটির জন্য এই ভিডিওটি {del_minutes} মিনিট পর অটোমেটিক ডিলিট হয়ে যাবে!"
            
            if m.get("file_type") == "video": sent_msg = await bot.send_video(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            else: sent_msg = await bot.send_document(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            
            await db.movies.update_one({"_id": ObjectId(d.movieId)}, {"$inc": {"clicks": 1}})
            await db.user_unlocks.update_one({"user_id": d.userId, "movie_id": d.movieId}, {"$set": {"unlocked_at": now}}, upsert=True)
            if sent_msg and not is_vip:
                delete_at = now + datetime.timedelta(minutes=del_minutes)
                await db.auto_delete.insert_one({"chat_id": d.userId, "message_id": sent_msg.message_id, "delete_at": delete_at})
        return {"ok": True}
    except: return {"ok": False}

@app.get("/api/favs/{uid}")
async def get_favs(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user: return []
    fav_titles = user.get("favorites", [])
    if not fav_titles: return []
    pipeline = [{"$match": {"title": {"$in": fav_titles}}}, {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "year": {"$first": "$year"}, "categories": {"$first": "$categories"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main"]}}}}}]
    movies = await db.movies.aggregate(pipeline).to_list(len(fav_titles))
    for m in movies: m["is_adult"] = "Adult Content" in m.get("categories", [])
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
# 11. Main Application Startup
# ==========================================
async def start():
    await init_db()
    await load_admins()
    await load_banned_users()
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    asyncio.create_task(auto_delete_worker())
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(server.serve(), dp.start_polling(bot))

if __name__ == "__main__": 
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start())
