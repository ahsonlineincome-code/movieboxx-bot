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

# ইউজারের মেসেজ অ্যাডমিনের চ্যাটে ফরওয়ার্ড হলে message_id ম্যাপিং করার জন্য ডিকশনারি
reply_mapping = {}

# ==========================================
# 2. FSM States
# ==========================================
class AdminStates(StatesGroup):
    waiting_for_bcast = State()
    waiting_for_photo = State()
    waiting_for_title = State()
    waiting_for_quality = State() 
    waiting_for_year = State()
    waiting_for_cats = State()
    waiting_for_upc_photo = State()
    waiting_for_upc_title = State()
    waiting_for_upc_date = State()
    waiting_for_batch_photo = State()
    waiting_for_batch_title = State()
    waiting_for_batch_year = State()
    waiting_for_batch_cats = State()
    waiting_for_batch_file = State()
    waiting_for_batch_quality = State()

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

async def update_monthly_users_bio():
    while True:
        try:
            now = datetime.datetime.utcnow()
            thirty_days_ago = now - datetime.timedelta(days=30)
            monthly_count = await db.users.count_documents({"last_active": {"$gte": thirty_days_ago}})
            formatted_count = f"{monthly_count:,}"
            short_bio = f"{formatted_count} monthly users"
            await bot.set_my_short_description(short_bio)
        except Exception as e:
            print(f"Bio Update Error: {e}")
        await asyncio.sleep(21600)

async def run_broadcast(admin_chat_id, photo_id, bcast_text, bcast_markup, del_minutes):
    bcast_success = 0
    now = datetime.datetime.utcnow()
    delete_at = now + datetime.timedelta(minutes=del_minutes)
    async for u in db.users.find():
        try:
            sent_msg = await bot.send_photo(u['user_id'], photo=photo_id, caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
            await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at})
            bcast_success += 1
            await asyncio.sleep(0.1) # Speed limit protection
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                sent_msg = await bot.send_photo(u['user_id'], photo=photo_id, caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
                await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at})
                bcast_success += 1
            except: pass
        except: pass
    try:
        await bot.send_message(admin_chat_id, f"✅ অটো-ব্রডকাস্ট শেষ!\n\nসফলভাবে পাঠানো হয়েছে: <b>{bcast_success}</b> জনকে।\n⏳ নোটিফিকেশনগুলো <b>{del_minutes}</b> মিনিট পর অটো-ডিলিট হবে।", parse_mode="HTML")
    except: pass

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

# FIX: Native Reply Method - অ্যাডমিন সরাসরি ইউজারের মেসেজে Reply দিলে স্টেট নষ্ট না করে রিপ্লাই পাঠাবে
@dp.message(F.reply_to_message, F.chat.type == "private", lambda m: m.from_user.id in admin_cache)
async def native_admin_reply(m: types.Message, state: FSMContext):
    if m.reply_to_message.message_id in reply_mapping:
        target_uid = reply_mapping[m.reply_to_message.message_id]
        try:
            await m.copy_to(chat_id=target_uid)
            await m.reply("✅ রিপ্লাই পাঠানো হয়েছে! (আপলোড প্রসেস নিরাপদ আছে)")
        except:
            await m.reply("❌ রিপ্লাই পাঠাতে ব্যর্থ হয়েছে।")

# FIX: ইউজারের মেসেজ অ্যাডমিনের কাছে ফরওয়ার্ড এবং message_id ম্যাপিং
@dp.message(lambda m: m.chat.type == "private" and m.from_user.id not in admin_cache)
async def handle_user_messages(m: types.Message):
    allowed_types = ['text', 'photo', 'video', 'voice', 'document']
    if m.content_type not in allowed_types:
        await m.answer("⚠️ দুঃখিত! এই ধরনের মেসেজ গ্রহণ করা হয় না।\n\n🎬 মুভি দেখতে নিচের 'Watch Now' বাটনে ক্লিক করুন।", parse_mode="HTML")
        return
        
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ রিপ্লাই", callback_data=f"reply_{m.from_user.id}")
        user_info = f"📩 <a href='tg://user?id={m.from_user.id}'>{m.from_user.first_name}</a>:\n\n"
        
        if m.content_type == 'text':
            sent_msg = await bot.send_message(OWNER_ID, user_info + m.text, parse_mode="HTML", reply_markup=builder.as_markup())
        else:
            caption = m.caption or ""
            new_caption = user_info + caption
            sent_msg = await m.copy_to(chat_id=OWNER_ID, caption=new_caption if new_caption.strip() != user_info.strip() else None, parse_mode="HTML", reply_markup=builder.as_markup())
        
        if sent_msg:
            reply_mapping[sent_msg.message_id] = m.from_user.id
            
    except Exception as e:
        print(f"Forward Error: {e}")

# FIX: Inline বাটনের মাধ্যমে রিপ্লাই (স্টেট চেঞ্জ না করে শুধু ডাটা সেভ করা)
@dp.callback_query(F.data.startswith("reply_"))
async def reply_to_user_callback(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in admin_cache: return
    user_id = int(c.data.split("_")[1])
    await state.update_data(reply_target_id=user_id)
    await c.message.answer("✍️ আপনার মেসেজ লিখুন (রিপ্লাই দেওয়ার জন্য)।\n\n⚠️ আপলোড প্রসেস থাকলে সেটি বাতিল হবে না।")
    await c.answer()

# FIX: অ্যাডমিন টেক্সট টাইপ করলে চেক করা রিপ্লাই কিনা
@dp.message(F.text, F.chat.type == "private", lambda m: m.from_user.id in admin_cache)
async def admin_text_handler(m: types.Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("reply_target_id")
    
    if target_id:
        try:
            await m.copy_to(chat_id=target_id)
            await m.answer("✅ রিপ্লাই পাঠানো হয়েছে! আপলোড প্রসেস অপরিবর্তিত রয়েছে।")
        except:
            await m.answer("❌ রিপ্লাই পাঠাতে ব্যর্থ।")
        await state.update_data(reply_target_id=None)
        return

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
# 7.5 Custom Batch Upload
# ==========================================
@dp.message(Command("batch"))
async def batch_upload_start(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: 
        await m.answer("⚠️ আপনি অ্যাডমিন নন!", parse_mode="HTML")
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_for_batch_photo)
    await m.answer("📦 <b>Batch Upload Mode</b>\n\nসিরিজ বা মাল্টি-এপিসোডের <b>পোস্টার</b> পাঠান।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_batch_photo, F.photo | F.document)
async def receive_batch_photo(m: types.Message, state: FSMContext):
    photo_id = m.photo[-1].file_id if m.photo else m.document.file_id
    await state.update_data(photo_id=photo_id)
    await state.set_state(AdminStates.waiting_for_batch_title)
    await m.answer("✅ এবার <b>সিরিজ/মুভির নাম</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_batch_title, F.text)
async def receive_batch_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip(), files=[])
    await state.set_state(AdminStates.waiting_for_batch_year)
    await m.answer("✅ এবার <b>রিলিজ সাল</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_batch_year, F.text)
async def receive_batch_year(m: types.Message, state: FSMContext):
    await state.update_data(year=m.text.strip())
    await state.set_state(AdminStates.waiting_for_batch_cats)
    builder = InlineKeyboardBuilder()
    for index, cat in enumerate(CATEGORIES): builder.button(text=cat, callback_data=f"batselcat_{index}")
    builder.button(text="✅ Done", callback_data="batcats_done")
    builder.adjust(2)
    await m.answer("✅ এবার <b>ক্যাটাগরি সিলেক্ট</b> করুন।", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(AdminStates.waiting_for_batch_cats, F.data.startswith("batselcat_"))
async def process_batch_category_selection(c: types.CallbackQuery, state: FSMContext):
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
        builder.button(text=f"{prefix}{ct}", callback_data=f"batselcat_{i}")
    builder.button(text="✅ Done", callback_data="batcats_done")
    builder.adjust(2)
    try:
        await c.message.edit_text(f"✅ ক্যাটাগরি সিলেক্ট করুন ({len(selected_cats)} টি সিলেক্ট করা হয়েছে):", reply_markup=builder.as_markup(), parse_mode="HTML")
    except: pass
    await c.answer()

@dp.callback_query(AdminStates.waiting_for_batch_cats, F.data == "batcats_done")
async def finish_batch_category_selection(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    if not selected_cats: return await c.answer("⚠️ অন্তত ১টি সিলেক্ট করুন!", show_alert=True)
    await state.set_state(AdminStates.waiting_for_batch_file)
    await c.message.edit_text(f"✅ ক্যাটাগরি সিলেক্ট হয়েছে!\n\nএখন প্রথম <b>ফাইলটি (ভিডিও/ডকুমেন্ট)</b> পাঠান।\n\nশেষ হলে <b>/done</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_batch_file, F.content_type.in_({'video', 'document'}))
async def receive_batch_file(m: types.Message, state: FSMContext):
    fid = m.video.file_id if m.video else m.document.file_id
    ftype = "video" if m.video else "document"
    await state.update_data(current_file_id=fid, current_file_type=ftype)
    await state.set_state(AdminStates.waiting_for_batch_quality)
    await m.answer("✅ ফাইল পেয়েছি! এবার এর <b>কোয়ালিটি/এপিসোড</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_batch_quality, F.text)
async def receive_batch_quality(m: types.Message, state: FSMContext):
    data = await state.get_data()
    files_list = data.get("files", [])
    files_list.append({"file_id": data["current_file_id"], "file_type": data["current_file_type"], "quality": m.text.strip()})
    await state.update_data(files=files_list)
    await state.set_state(AdminStates.waiting_for_batch_file)
    await m.answer(f"✅ <b>{m.text.strip()}</b> যুক্ত হয়েছে!\n\nমোট: <b>{len(files_list)}</b>টি ফাইল।\n\nপরবর্তী ফাইল পাঠান অথবা <b>/done</b> লিখুন।", parse_mode="HTML")

@dp.message(Command("done"), AdminStates.waiting_for_batch_file)
async def finish_batch_upload(m: types.Message, state: FSMContext):
    data = await state.get_data()
    files_list = data.get("files", [])
    if not files_list:
        await state.clear()
        return await m.answer("⚠️ কোনো ফাইল যুক্ত করা হয়নি!", parse_mode="HTML")
    await state.clear()
    title, photo_id, year, categories = data["title"], data["photo_id"], data.get("year", "N/A"), data["categories"]
    
    for f in files_list:
        await db.movies.insert_one({"title": title, "quality": f["quality"], "photo_id": photo_id, "file_id": f["file_id"], "file_type": f["file_type"], "year": year, "categories": categories, "clicks": 0, "created_at": datetime.datetime.utcnow()})
    
    await m.answer(f"🎉 <b>{title}</b> সফলভাবে যুক্ত হয়েছে! মোট: <b>{len(files_list)}</b>\n\n📢 নোটিফিকেশন পাঠানো হচ্ছে...", parse_mode="HTML")
    
    if LOG_CHANNEL_ID:
        try:
            log_kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", url="https://t.me/MovieeBoxx_Bot?start=new")]]
            log_text = f"🎬 <b>New Batch Upload</b>\n\n🏷 Title: <b>{title}</b>\n📂 Categories: {', '.join(categories)}\n📅 Year: <b>{year}</b>\n📺 Episodes: {', '.join([f['quality'] for f in files_list])}"
            await bot.send_photo(LOG_CHANNEL_ID, photo=photo_id, caption=log_text, parse_mode="HTML", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=log_kb))
        except: pass

    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    bcast_kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))], [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link), types.InlineKeyboardButton(text="🔴 18+ Channel", url="https://t.me/+W5V9-mn08jMyYTE1")]]
    bcast_text = f"🆕 <b>New Upload Alert!</b>\n\n🎬 <b>{title}</b>\n📺 Files: <b>{', '.join([f['quality'] for f in files_list])}</b>\n📅 Year: <b>{year}</b>"
    time_cfg = await db.settings.find_one({"id": "del_time"})
    del_minutes = time_cfg['minutes'] if time_cfg else 60
    asyncio.create_task(run_broadcast(m.from_user.id, photo_id, bcast_text, types.InlineKeyboardMarkup(inline_keyboard=bcast_kb), del_minutes))

@dp.message(Command("done"))
async def wrong_done_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    await m.answer("⚠️ আপনি কোনো ব্যাচ আপলোড প্রসেসে নেই।", parse_mode="HTML")

# ==========================================
# 7.6 Single Movie Upload
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
    for index, cat in enumerate(CATEGORIES): builder.button(text=cat, callback_data=f"selcat_{index}")
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
    builder.adjust(2)
    try:
        await c.message.edit_text(f"✅ ক্যাটাগরি সিলেক্ট করুন ({len(selected_cats)} টি সিলেক্ট করা হয়েছে):", reply_markup=builder.as_markup(), parse_mode="HTML")
    except: pass
    await c.answer()

@dp.callback_query(AdminStates.waiting_for_cats, F.data == "cats_done")
async def finish_category_selection(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    if not selected_cats: return await c.answer("⚠️ অন্তত ১টি সিলেক্ট করুন!", show_alert=True)
    await state.clear()
    await db.movies.insert_one({"title": data["title"], "quality": data["quality"], "photo_id": data["photo_id"], "file_id": data["file_id"], "file_type": data["file_type"], "year": data.get("year", "N/A"), "categories": selected_cats, "clicks": 0, "created_at": datetime.datetime.utcnow()})
    await c.message.edit_text(f"🎉 <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!\n\n📢 নোটিফিকেশন পাঠানো হচ্ছে...", parse_mode="HTML")
    
    if LOG_CHANNEL_ID:
        try:
            log_kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", url="https://t.me/MovieeBoxx_Bot?start=new")]]
            log_text = f"🎬 <b>New Movie Uploaded</b>\n\n🏷 Title: <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n📅 Year: <b>{data.get('year', 'N/A')}</b>\n📂 Categories: {', '.join(selected_cats)}"
            await bot.send_photo(LOG_CHANNEL_ID, photo=data["photo_id"], caption=log_text, parse_mode="HTML", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=log_kb))
        except: pass

    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    bcast_kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))], [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link), types.InlineKeyboardButton(text="🔴 18+ Channel", url="https://t.me/+W5V9-mn08jMyYTE1")]]
    bcast_text = f"🆕 <b>New Movie Alert!</b>\n\n🎬 <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n📅 Year: <b>{data.get('year', 'N/A')}</b>"
    time_cfg = await db.settings.find_one({"id": "del_time"})
    del_minutes = time_cfg['minutes'] if time_cfg else 60
    asyncio.create_task(run_broadcast(c.from_user.id, data["photo_id"], bcast_text, types.InlineKeyboardMarkup(inline_keyboard=bcast_kb), del_minutes))

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
    prog_msg = await m.answer("⏳ <b>Broadcast progressing...</b>", parse_mode="HTML")
    total_users = await db.users.count_documents({})
    success, blocked = 0, 0
    async for u in db.users.find():
        try: 
            await m.copy_to(chat_id=u['user_id'])
            success += 1
            await asyncio.sleep(0.05)
        except: blocked += 1
    stats_text = f"✅ <b>Broadcast Complete!</b>\n\n👥 Total Users: <b>{total_users}</b>\n✅ Successful: <b>{success}</b>\n🚫 Blocked Users: <b>{blocked}</b>"
    try: await prog_msg.edit_text(stats_text, parse_mode="HTML")
    except: await m.answer(stats_text, parse_mode="HTML")

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
    # (HTML Kept Minimal for code size, you can paste your full HTML here)
    html_code = '''<html><head><title>Admin Panel</title></head><body style="background:#0f172a;color:#fff;text-align:center;padding-top:100px;font-family:sans-serif;"><h1>Admin Panel Active</h1><p>Use API endpoints for management.</p></body></html>'''
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
    # (Paste your full Web App HTML here, unchanged)
    return HTMLResponse("<html><body><h1>Web App Running</h1></body></html>")

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
        # ✅ FIX: Cache Time Increased to 24 Hours for Maximum Speed
        if cache and cache.get("expires_at", now) > now: file_path = cache["file_path"]
        else:
            file_info = await bot.get_file(photo_id); file_path = file_info.file_path
            await db.file_cache.update_one({"photo_id": photo_id}, {"$set": {"file_path": file_path, "expires_at": now + datetime.timedelta(hours=24)}}, upsert=True)
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
            caption = base_caption + ("\n\n💎 VIP সুবিধা: এই ফাইলটি কখনো ডিলিট হবে না!" if is_vip else f"\n\n⏳ সতর্কতা: সিকিউরিটির জন্য এই ভিডিওটি {del_minutes} মিনিট পর অটোমেটিক ডিলিট হয়ে যাবে!")
            
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
# 11. FastAPI Startup & Main Execution
# ==========================================
@app.on_event("startup")
async def on_startup():
    await init_db()
    await load_admins()
    await load_banned_users()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(auto_delete_worker())
    asyncio.create_task(update_monthly_users_bio())
    asyncio.create_task(dp.start_polling(bot))

@app.on_event("shutdown")
async def on_shutdown():
    await dp.stop_polling()
    await bot.session.close()

if __name__ == "__main__": 
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, loop="asyncio")
