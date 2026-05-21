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
from fastapi.responses import HTMLResponse, StreamingResponse
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

# ==========================================
# 2. FSM States (For Uploading Flow)
# ==========================================
class AdminStates(StatesGroup):
    waiting_for_bcast = State()
    waiting_for_reply = State()
    waiting_for_photo = State()
    waiting_for_title = State()
    waiting_for_quality = State() 
    waiting_for_category = State() # NEW
    waiting_for_upc_photo = State()
    waiting_for_upc_title = State()
    waiting_for_upc_release = State() # NEW
    waiting_for_upc_lang = State() # NEW

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
    await db.movies.create_index("category") # NEW
    await db.auto_delete.create_index("delete_at")
    await db.users.create_index("joined_at")
    await db.reviews.create_index("movie_title")
    await db.payments.create_index("trx_id", unique=True)
    await db.requests.create_index("movie") 
    await db.favorites.create_index([("user_id", 1), ("movie_title", 1)]) # NEW

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
    except Exception: 
        return False

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASS)
    
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Incorrect username or password", 
            headers={"WWW-Authenticate": "Basic"}
        )
    return True

# ==========================================
# 5. Background Tasks (Auto Delete)
# ==========================================
async def auto_delete_worker():
    while True:
        try:
            now = datetime.datetime.utcnow()
            expired_msgs = db.auto_delete.find({"delete_at": {"$lte": now}})
            
            async for msg in expired_msgs:
                try: 
                    await bot.delete_message(chat_id=msg["chat_id"], message_id=msg["message_id"])
                except Exception: 
                    pass
                await db.auto_delete.delete_one({"_id": msg["_id"]})
        except Exception: 
            pass
        await asyncio.sleep(60)

# ==========================================
# 6. Telegram Bot Commands (General & Refer Logic)
# ==========================================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if uid in banned_cache: 
        return await message.answer("🚫 <b>আপনাকে এই বট থেকে স্থায়ীভাবে ব্যান করা হয়েছে।</b>", parse_mode="HTML")
        
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
                        new_vip = current_vip + datetime.timedelta(days=1)
                        await db.users.update_one({"user_id": referrer_id}, {"$set": {"vip_until": new_vip}})
                        
                        try:
                            await bot.send_message(referrer_id, "🎉 <b>অভিনন্দন!</b> আপনার ৫ জন রেফার পূর্ণ হয়েছে। আপনাকে ২৪ ঘণ্টার জন্য <b>VIP</b> দেওয়া হয়েছে!", parse_mode="HTML")
                        except: pass
            except Exception: pass

        await db.users.insert_one({
            "user_id": uid,
            "first_name": message.from_user.first_name,
            "joined_at": now,
            "refer_count": 0,
            "coins": 0,
            "last_checkin": now - datetime.timedelta(days=2),
            "vip_until": now - datetime.timedelta(days=1)
        })
    else:
        await db.users.update_one({"user_id": uid}, {"$set": {"first_name": message.from_user.first_name}})
    
    kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))]]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    if uid in admin_cache:
        text = (
            "👋 <b>হ্যালো অ্যাডমিন!</b>\n\n"
            "⚙️ <b>কমান্ড:</b>\n"
            "🔸 অ্যাডমিন: <code>/addadmin ID</code> | <code>/deladmin ID</code> | <code>/adminlist</code>\n"
            "🔸 ডাইরেক্ট লিংক: <code>/addlink লিংক</code> | <code>/dellink লিংক</code> | <code>/seelinks</code>\n"
            "🔸 অ্যাড জোন: <code>/setad ID</code> | অ্যাড সংখ্যা: <code>/setadcount সংখ্যা</code>\n"
            "🔸 টেলিগ্রাম: <code>/settg লিংক</code> | 18+: <code>/set18 লিংক</code>\n"
            "🔸 পেমেন্ট: <code>/setbkash নাম্বার</code> | <code>/setnagad নাম্বার</code>\n"
            "🔸 প্রোটেকশন: <code>/protect on</code> বা <code>/protect off</code>\n"
            "🔸 অটো-ডিলিট: <code>/settime [মিনিট]</code>\n"
            "🔸 স্ট্যাটাস: <code>/stats</code> | ব্রডকাস্ট: <code>/cast</code>\n"
            "🔸 মুভি ডিলিট: <code>/delmovie মুভির নাম</code>\n"
            "🔸 ব্যান: <code>/ban ID</code> | আনব্যান: <code>/unban ID</code>\n"
            "🔸 VIP দিন: <code>/addvip ID দিন</code> | VIP বাতিল: <code>/removevip ID</code>\n"
            "🔸 আপকামিং: <code>/addupcoming</code> | ডিলিট: <code>/delupcoming</code>\n\n"
            "🌐 <b>ওয়েব অ্যাডমিন প্যানেল:</b> <a href='{APP_URL}/admin'>এখানে ক্লিক করুন</a>\n\n"
            "📥 <b>মুভি অ্যাড করতে প্রথমে ভিডিও বা ডকুমেন্ট ফাইল পাঠান।</b>\n\n"
            "🌐 <b>প্রোফাইল সেটিংস:</b>\n"
            "🔸 <code>/setchannel লিংক</code> | <code>/setfb লিংক</code>\n"
            "🔸 <code>/setyt লিংক</code> | <code>/setweb লিংক</code>\n"
            "🔸 <code>/setabout টেক্সট</code>"
        )
    else: 
        text = f"👋 <b>স্বাগতম {message.from_user.first_name}!</b>\n\nমুভি পেতে নিচের বাটনে ক্লিক করুন।"
        
    await message.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)

@dp.message(lambda m: m.chat.type == "private" and m.from_user.id not in admin_cache)
async def forward_to_admin(m: types.Message):
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ রিপ্লাই দিন", callback_data=f"reply_{m.from_user.id}")
        await bot.send_message(OWNER_ID, f"📩 <b>New Message from <a href='tg://user?id={m.from_user.id}'>{m.from_user.first_name}</a></b>:\n\n{m.text or 'Media file'}", parse_mode="HTML", reply_markup=builder.as_markup())
    except Exception: pass

# ==========================================
# 7. Admin Settings, Profile & Manage 
# ==========================================
def format_views(n):
    if n >= 1000000: return f"{n/1000000:.1f}M".replace(".0M", "M")
    if n >= 1000: return f"{n/1000:.1f}K".replace(".0K", "K")
    return str(n)

# Profile Settings Commands
@dp.message(Command("setchannel"))
async def set_channel(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "social_links"}, {"$set": {"channel": link}}, upsert=True)
        await m.answer(f"✅ টেলিগ্রাম চ্যানেল লিংক সেট করা হয়েছে!", parse_mode="HTML")
    except: await m.answer("⚠️ সঠিক নিয়ম: <code>/setchannel https://t.me/...</code>", parse_mode="HTML")

@dp.message(Command("setfb"))
async def set_fb(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "social_links"}, {"$set": {"facebook": link}}, upsert=True)
        await m.answer(f"✅ ফেসবুক পেইজ লিংক সেট করা হয়েছে!", parse_mode="HTML")
    except: await m.answer("⚠️ সঠিক নিয়ম: <code>/setfb https://fb.com/...</code>", parse_mode="HTML")

@dp.message(Command("setyt"))
async def set_yt(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "social_links"}, {"$set": {"youtube": link}}, upsert=True)
        await m.answer(f"✅ ইউটিউব চ্যানেল লিংক সেট করা হয়েছে!", parse_mode="HTML")
    except: await m.answer("⚠️ সঠিক নিয়ম: <code>/setyt https://yt.com/...</code>", parse_mode="HTML")

@dp.message(Command("setweb"))
async def set_web(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "social_links"}, {"$set": {"website": link}}, upsert=True)
        await m.answer(f"✅ ওয়েবসাইট লিংক সেট করা হয়েছে!", parse_mode="HTML")
    except: await m.answer("⚠️ সঠিক নিয়ম: <code>/setweb https://...</code>", parse_mode="HTML")

@dp.message(Command("setabout"))
async def set_about(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        text = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "social_links"}, {"$set": {"about": text}}, upsert=True)
        await m.answer(f"✅ এবাউট সেকশন আপডেট হয়েছে!", parse_mode="HTML")
    except: await m.answer("⚠️ সঠিক নিয়ম: <code>/setabout আপনার টেক্সট</code>", parse_mode="HTML")

# Other Admin Commands
@dp.message(Command("addlink"))
async def add_direct_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer(f"✅ ডাইরেক্ট লিংক অ্যাড করা হয়েছে:\n<code>{url}</code>", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/addlink https://example.com</code>", parse_mode="HTML")

@dp.message(Command("dellink"))
async def del_direct_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        result = await db.settings.update_one({"id": "direct_links"}, {"$pull": {"links": url}})
        if result.modified_count > 0: await m.answer(f"❌ লিংকটি ডিলিট করা হয়েছে!", parse_mode="HTML")
        else: await m.answer("⚠️ লিংকটি পাওয়া যায়নি।")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/dellink https://example.com</code>", parse_mode="HTML")

@dp.message(Command("seelinks"))
async def see_direct_links(m: types.Message):
    if m.from_user.id not in admin_cache: return
    dl_cfg = await db.settings.find_one({"id": "direct_links"})
    links = dl_cfg.get("links", []) if dl_cfg else []
    if not links: return await m.answer("⚠️ কোনো ডাইরেক্ট লিংক নেই।")
    text = "🔗 <b>ডাইরেক্ট লিংক সমূহ:</b>\n\n"
    for i, link in enumerate(links, 1): text += f"{i}. <code>{link}</code>\n"
    await m.answer(text, parse_mode="HTML", disable_web_page_preview=True)

@dp.message(Command("addadmin"))
async def add_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return await m.answer("⚠️ শুধুমাত্র Owner পারবে!")
    try:
        target_uid = int(m.text.split()[1])
        await db.admins.update_one({"user_id": target_uid}, {"$set": {"user_id": target_uid}}, upsert=True)
        admin_cache.add(target_uid)
        await m.answer(f"✅ ইউজার <code>{target_uid}</code> কে অ্যাডমিন বানানো হয়েছে!", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/addadmin ইউজার_আইডি</code>", parse_mode="HTML")

@dp.message(Command("deladmin"))
async def del_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return await m.answer("⚠️ শুধুমাত্র Owner পারবে!")
    try:
        target_uid = int(m.text.split()[1])
        if target_uid == OWNER_ID: return await m.answer("⚠️ Main Owner কে ডিলিট করা যাবে না!")
        await db.admins.delete_one({"user_id": target_uid})
        admin_cache.discard(target_uid)
        await m.answer(f"❌ ইউজার <code>{target_uid}</code> কে রিমুভ করা হয়েছে!", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/deladmin ইউজার_আইডি</code>", parse_mode="HTML")

@dp.message(Command("adminlist"))
async def list_admin_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    text = f"👑 <b>Owner:</b> <code>{OWNER_ID}</code>\n\n👮‍♂️ <b>অ্যাডমিনগণ:</b>\n"
    count = 0
    async for a in db.admins.find(): text += f"▪️ <code>{a['user_id']}</code>\n"; count += 1
    if count == 0: text += "<i>কেউ নেই</i>"
    await m.answer(text, parse_mode="HTML")

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        title = m.text.split(" ", 1)[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0: await m.answer(f"✅ '<b>{title}</b>' ডিলিট হয়েছে!", parse_mode="HTML")
        else: await m.answer("⚠️ এই নামের মুভি পাওয়া যায়নি।")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/delmovie মুভির নাম</code>", parse_mode="HTML")

@dp.message(Command("stats"))
async def stats_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    uc = await db.users.count_documents({})
    mc = await db.movies.count_documents({})
    now = datetime.datetime.utcnow()
    today_start = datetime.datetime(now.year, now.month, now.day)
    new_users_today = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    top_pipeline = [{"$group": {"_id": "$title", "clicks": {"$sum": "$clicks"}}}, {"$sort": {"clicks": -1}}, {"$limit": 5}]
    top_movies = await db.movies.aggregate(top_pipeline).to_list(5)
    top_movies_text = "".join(f"{idx}. {mv['_id'][:20]}... - <b>{format_views(mv['clicks'])} views</b>\n" for idx, mv in enumerate(top_movies, 1))
    text = (f"📊 <b>স্ট্যাটাস:</b>\n\n👥 মোট ইউজার: <code>{uc}</code>\n🟢 আজকের নতুন: <code>{new_users_today}</code>\n🎬 মোট ফাইল: <code>{mc}</code>\n\n🔥 <b>টপ ৫:</b>\n{top_movies_text if top_movies_text else 'কোনো মুভি নেই'}")
    await m.answer(text, parse_mode="HTML")

@dp.message(Command("ban"))
async def ban_user_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        target_uid = int(m.text.split()[1])
        if target_uid in admin_cache: return await m.answer("⚠️ অ্যাডমিনকে ব্যান করা যাবে না!")
        await db.banned.update_one({"user_id": target_uid}, {"$set": {"user_id": target_uid}}, upsert=True)
        banned_cache.add(target_uid)
        await m.answer(f"🚫 ইউজার <code>{target_uid}</code> কে ব্যান করা হয়েছে!", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/ban ইউজার_আইডি</code>", parse_mode="HTML")

@dp.message(Command("unban"))
async def unban_user_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        target_uid = int(m.text.split()[1])
        await db.banned.delete_one({"user_id": target_uid})
        banned_cache.discard(target_uid)
        await m.answer(f"✅ ইউজার <code>{target_uid}</code> আনব্যান হয়েছে!", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/unban ইউজার_আইডি</code>", parse_mode="HTML")

@dp.message(Command("setadcount"))
async def set_ad_count_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        count = max(1, int(m.text.split(" ")[1]))
        await db.settings.update_one({"id": "ad_count"}, {"$set": {"count": count}}, upsert=True)
        await m.answer(f"✅ অ্যাড সংখ্যা সেট করা হয়েছে: <b>{count}</b>।", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/setadcount 3</code>", parse_mode="HTML")

@dp.message(Command("protect"))
async def protect_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        state = m.text.split(" ")[1].lower()
        if state == "on":
            await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": True}}, upsert=True)
            await m.answer("✅ প্রোটেকশন চালু করা হয়েছে।")
        elif state == "off":
            await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": False}}, upsert=True)
            await m.answer("✅ প্রোটেকশন বন্ধ করা হয়েছে।")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/protect on</code> অথবা <code>/protect off</code>")

@dp.message(Command("settime"))
async def set_del_time(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        mins = int(m.text.split(" ")[1])
        await db.settings.update_one({"id": "del_time"}, {"$set": {"minutes": mins}}, upsert=True)
        await m.answer("✅ অটো-ডিলিট টাইম সেট করা হয়েছে।")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/settime 60</code>", parse_mode="HTML")

@dp.message(Command("setad"))
async def set_ad(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        zone = m.text.split(" ")[1]
        await db.settings.update_one({"id": "ad_config"}, {"$set": {"zone_id": zone}}, upsert=True)
        await m.answer("✅ জোন আপডেট হয়েছে।")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/setad 1234567</code>")

@dp.message(Command("settg"))
async def set_tg_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ")[1]
        await db.settings.update_one({"id": "link_tg"}, {"$set": {"url": link}}, upsert=True)
        await m.answer(f"✅ টেলিগ্রাম লিংক সেট করা হয়েছে!", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/settg https://t.me/...</code>", parse_mode="HTML")

@dp.message(Command("set18"))
async def set_18_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ")[1]
        await db.settings.update_one({"id": "link_18"}, {"$set": {"url": link}}, upsert=True)
        await m.answer(f"✅ 18+ লিংক সেট করা হয়েছে!", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/set18 https://t.me/...</code>", parse_mode="HTML")

@dp.message(Command("setbkash"))
async def set_bkash(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        num = m.text.split(" ")[1]
        await db.settings.update_one({"id": "bkash_no"}, {"$set": {"number": num}}, upsert=True)
        await m.answer(f"✅ বিকাশ নাম্বার সেট করা হয়েছে!", parse_mode="HTML")
    except: await m.answer("⚠️ সঠিক নিয়ম: <code>/setbkash 017XXXXXXX</code>", parse_mode="HTML")

@dp.message(Command("setnagad"))
async def set_nagad(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        num = m.text.split(" ")[1]
        await db.settings.update_one({"id": "nagad_no"}, {"$set": {"number": num}}, upsert=True)
        await m.answer(f"✅ নগদ নাম্বার সেট করা হয়েছে!", parse_mode="HTML")
    except: await m.answer("⚠️ সঠিক নিয়ম: <code>/setnagad 017XXXXXXX</code>", parse_mode="HTML")

@dp.message(Command("addvip"))
async def add_vip_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        args = m.text.split()
        target_uid = int(args[1])
        days = int(args[2]) if len(args) > 2 else 30 
        now = datetime.datetime.utcnow()
        user = await db.users.find_one({"user_id": target_uid})
        if not user: return await m.answer("⚠️ ইউজার ডাটাবেসে নেই।")
        current_vip = user.get("vip_until", now)
        if current_vip < now: current_vip = now
        new_vip = current_vip + datetime.timedelta(days=days)
        await db.users.update_one({"user_id": target_uid}, {"$set": {"vip_until": new_vip}})
        await m.answer(f"✅ ইউজার <code>{target_uid}</code> কে <b>{days} দিনের</b> VIP দেওয়া হয়েছে!", parse_mode="HTML")
        try: await bot.send_message(target_uid, f"🎉 <b>অভিনন্দন!</b> আপনাকে <b>{days} দিনের</b> VIP দেওয়া হয়েছে!", parse_mode="HTML")
        except: pass
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/addvip ইউজার_আইডি দিন</code>", parse_mode="HTML")

@dp.message(Command("removevip"))
async def remove_vip_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        target_uid = int(m.text.split()[1])
        now = datetime.datetime.utcnow()
        await db.users.update_one({"user_id": target_uid}, {"$set": {"vip_until": now - datetime.timedelta(days=1)}})
        await m.answer(f"❌ ইউজার <code>{target_uid}</code> এর VIP বাতিল করা হয়েছে!", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/removevip ইউজার_আইডি</code>", parse_mode="HTML")

# ==========================================
# 8. Admin Inline Callback (Payment & Requests)
# ==========================================
@dp.callback_query(F.data.startswith("trx_"))
async def handle_trx_approval(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    action, _, pay_id = c.data.split("_")
    payment = await db.payments.find_one({"_id": ObjectId(pay_id)})
    if not payment or payment["status"] != "pending": return await c.answer("⚠️ ইতিমধ্যে প্রসেস করা হয়েছে!", show_alert=True)
    user_id = payment["user_id"]; days = payment["days"]
    if action == "approve":
        now = datetime.datetime.utcnow(); user = await db.users.find_one({"user_id": user_id})
        current_vip = user.get("vip_until", now) if user else now
        if current_vip < now: current_vip = now
        new_vip = current_vip + datetime.timedelta(days=days)
        await db.users.update_one({"user_id": user_id}, {"$set": {"vip_until": new_vip}})
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "approved"}})
        await c.message.edit_text(c.message.text + f"\n\n✅ <b>পেমেন্ট অ্যাপ্রুভ করা হয়েছে!</b>", parse_mode="HTML")
        try: await bot.send_message(user_id, f"🎉 <b>পেমেন্ট সফল!</b> আপনাকে VIP দেওয়া হয়েছে!", parse_mode="HTML")
        except: pass
    else:
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "rejected"}})
        await c.message.edit_text(c.message.text + "\n\n❌ <b>পেমেন্ট রিজেক্ট করা হয়েছে!</b>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("req_"))
async def handle_request_approval(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    action = c.data.split("_")[1]; req_id = c.data.split("_")[2]
    req = await db.requests.find_one({"_id": ObjectId(req_id)})
    if not req: return await c.answer("⚠️ রিকোয়েস্টটি নেই!", show_alert=True)
    movie_name = req["movie"]; voters = req.get("voters", [])
    if action == "acc":
        await c.message.edit_text(c.message.text + "\n\n✅ <b>Approve করা হয়েছে!</b>", parse_mode="HTML")
        for v_id in voters:
            try: await bot.send_message(v_id, f"🎉 <b>সুখবর!</b> মুভি <b>{movie_name}</b> আপলোড করা হয়েছে!", parse_mode="HTML")
            except: pass
        await db.requests.delete_one({"_id": ObjectId(req_id)})
    elif action == "rej":
        await c.message.edit_text(c.message.text + "\n\n❌ <b>Reject করা হয়েছে!</b>", parse_mode="HTML")
        await db.requests.delete_one({"_id": ObjectId(req_id)})

# ==========================================
# 9. Movie Upload Logic (Updated for Category)
# ==========================================
@dp.message(F.content_type.in_({'video', 'document'}), lambda m: m.from_user.id in admin_cache)
async def receive_movie_file(m: types.Message, state: FSMContext):
    fid = m.video.file_id if m.video else m.document.file_id
    ftype = "video" if m.video else "document"
    await state.set_state(AdminStates.waiting_for_photo)
    await state.update_data(file_id=fid, file_type=ftype)
    await m.answer("✅ ফাইল পেয়েছি! এবার মুভির <b>পোস্টার (Photo)</b> সেন্ড করুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_photo, F.photo)
async def receive_movie_photo(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_title)
    await m.answer("✅ পোস্টার পেয়েছি! এবার <b>মুভি বা ওয়েব সিরিজের নাম</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer("✅ নাম সেভ হয়েছে! এবার <b>কোয়ালিটি/এপিসোড</b> দিন।\n<i>(যেমন: 720p, Episode 01)</i>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=m.text.strip())
    await state.set_state(AdminStates.waiting_for_category)
    kb = types.ReplyKeyboardMarkup(keyboard=[
        [types.KeyboardButton(text="Bangla"), types.KeyboardButton(text="Bengali Dubbed")],
        [types.KeyboardButton(text="Hindi"), types.KeyboardButton(text="Hindi Dubbed")],
        [types.KeyboardButton(text="English"), types.KeyboardButton(text="Web Series")],
        [types.KeyboardButton(text="Korean"), types.KeyboardButton(text="Anime")],
        [types.KeyboardButton(text="18+")]
    ], resize_keyboard=True)
    await m.answer("✅ কোয়ালিটি সেভ হয়েছে! এবার <b>ক্যাটাগরি</b> সিলেক্ট করুন।", reply_markup=kb, parse_mode="HTML")

@dp.message(AdminStates.waiting_for_category, F.text)
async def receive_movie_category(m: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    await db.movies.insert_one({
        "title": data["title"], "quality": data["quality"], "category": m.text.strip(),
        "photo_id": data["photo_id"], "file_id": data["file_id"], "file_type": data["file_type"],
        "clicks": 0, "created_at": datetime.datetime.utcnow()
    })
    
    rm = types.ReplyKeyboardRemove()
    await m.answer(f"🎉 <b>{data['title']} [{data['quality']}]</b> অ্যাপে যুক্ত করা হয়েছে!", reply_markup=rm, parse_mode="HTML")
    
    req = await db.requests.find_one({"movie": {"$regex": f"^{data['title']}$", "$options": "i"}})
    if req:
        for v_id in req.get("voters", []):
            try: await bot.send_message(v_id, f"🎉 সুখবর! মুভি <b>{data['title']}</b> আপলোড করা হয়েছে!", parse_mode="HTML")
            except Exception: pass
        await db.requests.delete_one({"_id": req["_id"]})
    
    if CHANNEL_ID and CHANNEL_ID != "-100XXXXXXXXXX":
        try:
            bot_info = await bot.get_me()
            kb = [[types.InlineKeyboardButton(text="🎬 মুভিটি পেতে ক্লিক করুন", url=f"https://t.me/{bot_info.username}?start=new")]]
            markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
            caption = f"🎬 <b>নতুন ফাইল যুক্ত!</b>\n\n📌 <b>নাম:</b> {data['title']}\n🏷 <b>কোয়ালিটি:</b> {data['quality']}\n📂 <b>ক্যাটাগরি:</b> {m.text.strip()}"
            await bot.send_photo(chat_id=CHANNEL_ID, photo=data["photo_id"], caption=caption, parse_mode="HTML", reply_markup=markup)
        except Exception: pass

# ==========================================
# 10. Upcoming Movies Logic (Updated)
# ==========================================
@dp.message(Command("addupcoming"))
async def add_upc_cmd(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_upc_photo)
    await m.answer("🌟 <b>আপকামিং মুভির পোস্টার (Photo) সেন্ড করুন:</b>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_photo, F.photo)
async def upc_photo_step(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_upc_title)
    await m.answer("✅ পোস্টার পেয়েছি! এবার মুভির <b>টাইটেল</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_title, F.text)
async def upc_title_step(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_upc_release)
    await m.answer("✅ এবার মুভির <b>রিলিজ ডেট</b> দিন।\n<i>(যেমন: 15 August 2024 অথবা Coming Soon)</i>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_release, F.text)
async def upc_release_step(m: types.Message, state: FSMContext):
    await state.update_data(release_date=m.text.strip())
    await state.set_state(AdminStates.waiting_for_upc_lang)
    kb = types.ReplyKeyboardMarkup(keyboard=[
        [types.KeyboardButton(text="Bangla"), types.KeyboardButton(text="Hindi")],
        [types.KeyboardButton(text="English"), types.KeyboardButton(text="Korean")]
    ], resize_keyboard=True)
    await m.answer("✅ এবার মুভির <b>ভাষা</b> সিলেক্ট করুন।", reply_markup=kb, parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_lang, F.text)
async def upc_lang_step(m: types.Message, state: FSMContext):
    data = await state.get_data()
    await db.upcoming.insert_one({
        "photo_id": data["photo_id"], "title": data["title"],
        "release_date": data["release_date"], "language": m.text.strip(),
        "added_at": datetime.datetime.utcnow()
    })
    await state.clear()
    rm = types.ReplyKeyboardRemove()
    await m.answer("✅ আপকামিং মুভি সফলভাবে যুক্ত করা হয়েছে!", reply_markup=rm)

@dp.message(Command("delupcoming"))
async def del_upc_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    await db.upcoming.delete_many({})
    await m.answer("🗑 সব আপকামিং মুভি ডিলিট করা হয়েছে!")

# ==========================================
# 11. Broadcast & User Reply System
# ==========================================
@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_bcast)
    await m.answer("📢 যে মেসেজটি ব্রডকাস্ট করতে চান সেটি পাঠান।")

@dp.message(AdminStates.waiting_for_bcast)
async def execute_broadcast(m: types.Message, state: FSMContext):
    await state.clear()
    await m.answer("⏳ ব্রডকাস্ট শুরু হয়েছে...")
    kb = [[types.InlineKeyboardButton(text="🎬 ওপেন মুভি অ্যাপ", web_app=types.WebAppInfo(url=APP_URL))]]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    success = 0
    async for u in db.users.find():
        try:
            await m.copy_to(chat_id=u['user_id'], reply_markup=markup)
            success += 1; await asyncio.sleep(0.05)
        except Exception: pass
    await m.answer(f"✅ সম্পন্ন! <b>{success}</b> জনকে পাঠানো হয়েছে।", parse_mode="HTML")

@dp.callback_query(F.data.startswith("reply_"))
async def process_reply_cb(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in admin_cache: return
    user_id = int(c.data.split("_")[1])
    await state.set_state(AdminStates.waiting_for_reply)
    await state.update_data(target_uid=user_id)
    await c.message.reply("✍️ <b>ইউজারকে কী রিপ্লাই দিতে চান তা লিখুন:</b>", parse_mode="HTML")
    await c.answer()

@dp.message(AdminStates.waiting_for_reply)
async def send_reply(m: types.Message, state: FSMContext):
    data = await state.get_data(); target_uid = data.get("target_uid"); await state.clear()
    try:
        if m.text: await bot.send_message(target_uid, f"📩 <b>অ্যাডমিন রিপ্লাই:</b>\n\n{m.text}", parse_mode="HTML")
        else: await m.copy_to(target_uid, caption=f"📩 <b>অ্যাডমিন রিপ্লাই:</b>\n\n{m.caption or ''}", parse_mode="HTML")
        await m.answer("✅ রিপ্লাই পাঠানো হয়েছে!")
    except Exception: await m.answer("⚠️ রিপ্লাই পাঠানো যায়নি!")

# ==========================================
# 12. Web Admin Panel API & HTML
# ==========================================
@app.get("/admin", response_class=HTMLResponse)
async def web_admin_panel(auth: bool = Depends(verify_admin)):
    html_content = """
    <!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MovieZone Admin Panel</title><script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css"></head>
    <body class="bg-gray-900 text-white font-sans antialiased"><div class="max-w-6xl mx-auto p-5">
    <div class="flex justify-between items-center mb-8 border-b border-gray-700 pb-4">
    <h1 class="text-3xl font-bold text-red-500"><i class="fa-solid fa-shield-halved"></i> MovieZone Admin</h1></div>
    <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-10"><div class="bg-gray-800 p-6 rounded-xl shadow-lg border border-gray-700">
    <h3 class="text-gray-400 text-sm font-bold">TOTAL USERS</h3><p class="text-4xl font-bold text-green-400 mt-2" id="statUsers">...</p></div>
    <div class="bg-gray-800 p-6 rounded-xl shadow-lg border border-gray-700"><h3 class="text-gray-400 text-sm font-bold">UNIQUE GROUPS</h3>
    <p class="text-4xl font-bold text-blue-400 mt-2" id="statMovies">...</p></div><div class="bg-gray-800 p-6 rounded-xl shadow-lg border border-gray-700">
    <h3 class="text-gray-400 text-sm font-bold">NEW USERS TODAY</h3><p class="text-4xl font-bold text-yellow-400 mt-2" id="statNew">...</p></div></div>
    <div class="bg-gray-800 rounded-xl shadow-lg border border-gray-700 p-6"><h2 class="text-xl font-bold mb-4 text-gray-200">
    <i class="fa-solid fa-film text-red-400"></i> Manage Movies & Streams</h2><div class="overflow-x-auto">
    <table class="w-full text-left text-sm whitespace-nowrap"><thead class="bg-gray-700 text-gray-300"><tr>
    <th class="p-4 rounded-tl-lg">Movie Title</th><th class="p-4">Total Views</th><th class="p-4">Files</th><th class="p-4 rounded-tr-lg">Action</th></tr></thead>
    <tbody id="movieTableBody"><tr><td colspan="4" class="text-center p-8 text-gray-400">Loading data...</td></tr></tbody></table></div></div></div>
    <script>async function loadAdminData(){try{const r=await fetch('/api/admin/data');const d=await r.json();document.getElementById('statUsers').innerText=d.total_users;
    document.getElementById('statMovies').innerText=d.total_groups;document.getElementById('statNew').innerText=d.new_users_today;let h='';d.movies.forEach(m=>{h+=`<tr class="border-b border-gray-700 hover:bg-gray-750 transition"><td class="p-4 font-medium text-base">${m._id}</td><td class="p-4 text-gray-400 font-bold">${m.clicks} views</td><td class="p-4 text-green-400 font-bold">${m.file_count}</td><td class="p-4 flex gap-2"><button onclick="deleteMovie('${encodeURIComponent(m._id)}')" class="text-red-400 bg-red-900 bg-opacity-30 px-3 py-1 rounded">Delete</button></td></tr>`;});document.getElementById('movieTableBody').innerHTML=h;}catch(e){}}
    async function deleteMovie(t){if(!confirm('Delete ALL files for this movie?'))return;await fetch('/api/admin/movie/'+t,{method:'DELETE'});loadAdminData();}
    loadAdminData();</script></body></html>"""
    return HTMLResponse(content=html_content)

@app.get("/api/admin/data")
async def get_admin_data(auth: bool = Depends(verify_admin)):
    uc = await db.users.count_documents({}); now = datetime.datetime.utcnow()
    today_start = datetime.datetime(now.year, now.month, now.day)
    new_users = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    pipeline = [{"$group": {"_id": "$title", "clicks": {"$sum": "$clicks"}, "file_count": {"$sum": 1}, "created_at": {"$max": "$created_at"}}}, {"$sort": {"created_at": -1}}, {"$limit": 50}]
    movies = await db.movies.aggregate(pipeline).to_list(50)
    return {"total_users": uc, "total_groups": len(movies), "new_users_today": new_users, "movies": movies}

@app.delete("/api/admin/movie/{title}")
async def delete_movie_api(title: str, auth: bool = Depends(verify_admin)):
    await db.movies.delete_many({"title": title}); return {"ok": True}

# ==========================================
# 13. Main Web App UI (Frontend)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def web_ui():
    ad_cfg = await db.settings.find_one({"id": "ad_config"})
    tg_cfg = await db.settings.find_one({"id": "link_tg"})
    b18_cfg = await db.settings.find_one({"id": "link_18"})
    ad_count_cfg = await db.settings.find_one({"id": "ad_count"})
    bkash_cfg = await db.settings.find_one({"id": "bkash_no"})
    nagad_cfg = await db.settings.find_one({"id": "nagad_no"})
    dl_cfg = await db.settings.find_one({"id": "direct_links"})
    social_cfg = await db.settings.find_one({"id": "social_links"})
    
    zone_id = ad_cfg['zone_id'] if ad_cfg else "10916755"
    tg_url = tg_cfg['url'] if tg_cfg else "https://t.me/MovieeBD"
    link_18 = b18_cfg['url'] if b18_cfg else "https://t.me/MovieeBD"
    required_ads = ad_count_cfg['count'] if ad_count_cfg else 1
    bkash_no = bkash_cfg['number'] if bkash_cfg else "Not Set"
    nagad_no = nagad_cfg['number'] if nagad_cfg else "Not Set"
    direct_links = dl_cfg.get('links', []) if dl_cfg else []
    dl_json = json.dumps(direct_links)
    
    social_data = {
        "channel": social_cfg.get("channel", "#") if social_cfg else "#",
        "fb": social_cfg.get("facebook", "#") if social_cfg else "#",
        "yt": social_cfg.get("youtube", "#") if social_cfg else "#",
        "web": social_cfg.get("website", "#") if social_cfg else "#",
        "about": social_cfg.get("about", "No bio available.") if social_cfg else "No bio available."
    }
    social_json = json.dumps(social_data)

    html_code = r"""
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
            html { scroll-behavior: smooth; }
            body { background: #0f172a; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #fff; -webkit-font-smoothing: antialiased; overscroll-behavior-y: none; padding-bottom: 70px;} 
            
            /* Welcome Screen */
            #welcomeScreen { position: fixed; top:0; left:0; width:100%; height:100%; background: #0f172a; z-index: 99999; display: flex; align-items: center; justify-content: center; flex-direction: column; transition: opacity 0.8s ease, transform 0.8s ease; }
            #welcomeScreen.hide { opacity: 0; transform: scale(1.1); pointer-events: none; }
            .welcome-text { font-size: 28px; font-weight: 900; background: linear-gradient(45deg, #00f260, #0575e6, #ff416c); -webkit-background-clip: text; -webkit-text-fill-color: transparent; animation: pulse 1.5s infinite; }
            @keyframes pulse { 0% { transform: scale(1); } 50% { transform: scale(1.05); } 100% { transform: scale(1); } }

            /* Header */
            header { display: flex; justify-content: center; align-items: center; padding: 15px; border-bottom: 1px solid rgba(255,255,255,0.05); position: sticky; top: 0; background: rgba(15, 23, 42, 0.85); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); z-index: 1000; }
            .logo { font-size: 24px; font-weight: 900; color: #fff; text-shadow: 0 0 10px rgba(0, 245, 96, 0.5); cursor: pointer; letter-spacing: 2px; }

            /* App Pages */
            .app-page { display: none; }
            .app-page.active { display: block; animation: fadeIn 0.3s ease; }
            @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

            /* Search & Categories */
            .search-box { padding: 15px; }
            .search-input { width: 100%; padding: 14px; border-radius: 15px; border: 1px solid rgba(255,255,255,0.1); outline: none; text-align: center; background: rgba(255,255,255,0.05); backdrop-filter: blur(10px); color: #fff; font-size: 16px; font-weight: 500; transition: 0.3s; }
            .search-input:focus { border-color: #00f260; box-shadow: 0 0 15px rgba(0, 242, 96, 0.2); }
            .cat-container { display: flex; overflow-x: auto; gap: 10px; padding: 0 15px 15px; -webkit-overflow-scrolling: touch; }
            .cat-container::-webkit-scrollbar { display: none; }
            .cat-btn { padding: 8px 18px; border-radius: 20px; border: 1px solid rgba(255,255,255,0.1); background: rgba(255,255,255,0.05); color: #cbd5e1; font-weight: 700; font-size: 13px; white-space: nowrap; cursor: pointer; transition: 0.3s; }
            .cat-btn.active { background: linear-gradient(45deg, #00f260, #0575e6); color: #fff; border-color: transparent; box-shadow: 0 0 15px rgba(0, 242, 96, 0.4); }

            /* Movie Cards (Netflix Row Style) */
            .section-title { padding: 10px 15px; font-size: 18px; font-weight: 800; color: #e2e8f0; display: flex; align-items: center; gap: 8px; }
            .movie-list { display: flex; flex-direction: column; padding: 0 15px 15px; gap: 15px; }
            .movie-row { display: flex; align-items: center; background: rgba(255,255,255,0.03); border-radius: 12px; overflow: hidden; border: 1px solid rgba(255,255,255,0.05); transition: 0.3s; cursor: pointer; }
            .movie-row:active { transform: scale(0.98); background: rgba(255,255,255,0.08); }
            .movie-poster { width: 90px; height: 120px; object-fit: cover; flex-shrink: 0; }
            .movie-info { padding: 12px; flex: 1; display: flex; flex-direction: column; gap: 5px; }
            .movie-title-text { font-size: 15px; font-weight: 700; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
            .movie-meta { font-size: 12px; color: #94a3b8; display: flex; gap: 10px; align-items: center; }
            .badge-cat { background: rgba(0, 242, 96, 0.15); color: #00f260; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 700; }

            /* Modals General */
            .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: center; justify-content: center; z-index: 3000; backdrop-filter: blur(5px); }
            .modal-content { background: rgba(30, 41, 59, 0.95); backdrop-filter: blur(20px); width: 92%; max-width: 400px; padding: 25px; border-radius: 20px; text-align: center; border: 1px solid rgba(255,255,255,0.1); max-height: 85vh; overflow-y: auto; position: relative; }
            .close-icon { position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: rgba(255,255,255,0.1); color: #fff; font-size: 18px; display: flex; align-items: center; justify-content: center; cursor: pointer; z-index: 100; border: none; }

            /* Detail Modal */
            .detail-poster { width: 100%; height: 250px; object-fit: cover; border-radius: 12px; margin-bottom: 15px; }
            .btn-action { width: 100%; padding: 14px; border-radius: 12px; border: none; font-weight: 700; font-size: 16px; cursor: pointer; margin-bottom: 10px; display: flex; align-items: center; justify-content: center; gap: 8px; transition: 0.3s; }
            .btn-dl { background: linear-gradient(45deg, #00f260, #0575e6); color: #fff; box-shadow: 0 4px 15px rgba(0, 242, 96, 0.3); }
            .btn-fav { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); color: #fff; }

            /* Ad Timer Modal */
            .timer-circle { width: 80px; height: 80px; border-radius: 50%; border: 4px solid rgba(255,255,255,0.1); border-top: 4px solid #00f260; display: flex; align-items: center; justify-content: center; font-size: 24px; font-weight: 900; margin: 20px auto; animation: spin 1s linear infinite; }
            @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }

            /* 18+ Modal */
            .modal-18-bg { background: rgba(0,0,0,0.95); }
            .warning-icon { font-size: 60px; color: #ef4444; margin-bottom: 20px; text-shadow: 0 0 20px rgba(239, 68, 68, 0.5); }

            /* Bottom Navigation */
            .bottom-nav { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(20px); border-top: 1px solid rgba(255,255,255,0.05); display: flex; justify-content: space-around; padding: 10px 0; z-index: 2000; }
            .nav-item { display: flex; flex-direction: column; align-items: center; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer; transition: 0.3s; gap: 4px; }
            .nav-item.active { color: #00f260; text-shadow: 0 0 10px rgba(0, 242, 96, 0.5); }
            .nav-item i { font-size: 20px; }

            /* Upcoming & Profile */
            .upc-card { display: flex; background: rgba(255,255,255,0.03); border-radius: 12px; overflow: hidden; margin-bottom: 12px; border: 1px solid rgba(255,255,255,0.05); }
            .upc-poster { width: 80px; height: 100px; object-fit: cover; }
            .upc-info { padding: 10px; flex: 1; }
            .social-btn { display: inline-flex; align-items: center; justify-content: center; width: 50px; height: 50px; border-radius: 50%; background: rgba(255,255,255,0.05); color: #fff; font-size: 22px; margin: 0 5px; border: 1px solid rgba(255,255,255,0.1); transition: 0.3s; }
            .social-btn:hover { background: #00f260; color: #000; }

            .skeleton { background: rgba(255,255,255,0.05); border-radius: 12px; height: 100px; position: relative; overflow: hidden; }
            .skeleton::after { content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.05), transparent); animation: shimmer 1.5s infinite; }
            @keyframes shimmer { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }
        </style>
    </head>
    <body>
        <!-- Welcome Screen -->
        <div id="welcomeScreen">
            <div style="font-size: 60px; margin-bottom: 20px;">🎬</div>
            <div class="welcome-text">মুভি বক্স জগতে স্বাগতম</div>
        </div>

        <!-- Header -->
        <header>
            <div class="logo" onclick="switchPage('home')">Movie Box</div>
        </header>

        <!-- Home Page -->
        <div id="page-home" class="app-page active">
            <div class="search-box">
                <input type="text" id="searchInput" class="search-input" placeholder="🔍 মুভি বা সিরিজ খুঁজুন...">
            </div>
            <div class="cat-container" id="catContainer">
                <div class="cat-btn active" onclick="filterCat('All')">All</div>
                <div class="cat-btn" onclick="filterCat('Bangla')">Bangla</div>
                <div class="cat-btn" onclick="filterCat('Bengali Dubbed')">Dubbed</div>
                <div class="cat-btn" onclick="filterCat('Hindi')">Hindi</div>
                <div class="cat-btn" onclick="filterCat('Hindi Dubbed')">Hindi Dubbed</div>
                <div class="cat-btn" onclick="filterCat('English')">English</div>
                <div class="cat-btn" onclick="filterCat('Web Series')">Web Series</div>
                <div class="cat-btn" onclick="filterCat('Korean')">Korean</div>
                <div class="cat-btn" onclick="filterCat('Anime')">Anime</div>
                <div class="cat-btn" onclick="verifyAge('18+')">18+</div>
            </div>
            <div class="section-title">🔥 নতুন মুভি সমূহ</div>
            <div class="movie-list" id="movieGrid"></div>
            <div style="text-align:center; padding:15px;" id="paginationBox"></div>
        </div>

        <!-- Search Page -->
        <div id="page-search" class="app-page">
            <div class="search-box"><input type="text" id="searchInputFull" class="search-input" placeholder="🔍 সার্চ করুন..." oninput="searchFull()"></div>
            <div class="movie-list" id="searchResults"></div>
        </div>

        <!-- Favorites Page -->
        <div id="page-favorites" class="app-page">
            <div class="section-title">❤️ আমার ফেভারিট</div>
            <div class="movie-list" id="favGrid"></div>
        </div>

        <!-- Upcoming Page -->
        <div id="page-upcoming" class="app-page">
            <div class="section-title">🌟 আপকামিং মুভি</div>
            <div style="padding: 0 15px 15px;" id="upcomingGrid"></div>
        </div>

        <!-- Profile Page -->
        <div id="page-profile" class="app-page">
            <div style="text-align:center; padding: 30px 15px;">
                <img id="profPic" src="https://cdn-icons-png.flaticon.com/512/3135/3135715.png" style="width:90px; height:90px; border-radius:50%; border: 3px solid #00f260; margin-bottom:15px;">
                <h2 id="profName" style="color:#fff; font-size:22px; margin-bottom:5px;">Guest</h2>
                <p id="profAbout" style="color:#94a3b8; font-size:14px; margin-bottom:25px;"></p>
                <div style="display:flex; justify-content:center; gap:15px; margin-bottom:30px;">
                    <a id="socChannel" href="#" class="social-btn" style="display:none;"><i class="fa-brands fa-telegram"></i></a>
                    <a id="socFb" href="#" class="social-btn" style="display:none;"><i class="fa-brands fa-facebook"></i></a>
                    <a id="socYt" href="#" class="social-btn" style="display:none;"><i class="fa-brands fa-youtube"></i></a>
                    <a id="socWeb" href="#" class="social-btn" style="display:none;"><i class="fa-solid fa-globe"></i></a>
                </div>
                <button class="btn-action btn-dl" onclick="window.open('{{TG_LINK}}')">জয়েন করুন টেলিগ্রাম চ্যানেল</button>
            </div>
        </div>

        <!-- Detail Modal -->
        <div id="detailModal" class="modal">
            <div class="modal-content">
                <button class="close-icon" onclick="closeModal('detailModal')"><i class="fa-solid fa-xmark"></i></button>
                <img id="detailPoster" class="detail-poster" src="">
                <h2 id="detailTitle" style="color:#fff; font-size:20px; margin-bottom:5px;"></h2>
                <p id="detailMeta" style="color:#94a3b8; font-size:13px; margin-bottom:20px;"></p>
                <button class="btn-action btn-dl" id="dlBtn" onclick="startDownload()"><i class="fa-solid fa-download"></i> ডাউনলোড করুন</button>
                <button class="btn-action btn-fav" id="favBtn" onclick="toggleFavorite()"><i class="fa-regular fa-heart"></i> ফেভারিটে যোগ করুন</button>
            </div>
        </div>

        <!-- 18+ Verification Modal -->
        <div id="ageModal" class="modal modal-18-bg">
            <div class="modal-content">
                <div class="warning-icon"><i class="fa-solid fa-triangle-exclamation"></i></div>
                <h2 style="color:#ef4444; font-size:22px; margin-bottom:10px;">বয়স সীমাবদ্ধ কন্টেন্ট</h2>
                <p style="color:#cbd5e1; margin-bottom:25px;">আপনার বয়স কি ১৮ বছরের বেশি?</p>
                <button class="btn-action btn-dl" onclick="confirmAge()">হ্যাঁ, আমি ১৮+ বছরের</button>
                <button class="btn-action btn-fav" onclick="closeModal('ageModal')" style="margin-top:0;">না, ফিরে যান</button>
            </div>
        </div>

        <!-- Ad Timer Modal -->
        <div id="adModal" class="modal">
            <div class="modal-content">
                <h2 style="color:#fff; margin-bottom:10px;">ডাউনলোড আনলক করুন</h2>
                <p style="color:#94a3b8; font-size:14px; margin-bottom:5px;">অ্যাড লিংক ভিজিট করুন এবং ১৫ সেকেন্ড অপেক্ষা করুন</p>
                <div class="timer-circle" id="timerCircle">15</div>
                <button class="btn-action btn-dl" id="adLinkBtn" onclick="visitAdLink()"><i class="fa-solid fa-link"></i> অ্যাড লিংক ওপেন করুন</button>
            </div>
        </div>

        <!-- Bottom Navigation -->
        <div class="bottom-nav">
            <div class="nav-item active" onclick="switchPage('home')"><i class="fa-solid fa-house"></i><span>Home</span></div>
            <div class="nav-item" onclick="switchPage('search')"><i class="fa-solid fa-magnifying-glass"></i><span>Search</span></div>
            <div class="nav-item" onclick="switchPage('favorites')"><i class="fa-solid fa-heart"></i><span>Favorites</span></div>
            <div class="nav-item" onclick="switchPage('upcoming')"><i class="fa-solid fa-clock"></i><span>Upcoming</span></div>
            <div class="nav-item" onclick="switchPage('profile')"><i class="fa-solid fa-user"></i><span>Profile</span></div>
        </div>

        <script>
            let tg = window.Telegram.WebApp; tg.expand();
            const DIRECT_LINKS = {{DIRECT_LINKS}};
            const SOCIAL_DATA = {{SOCIAL_JSON}};
            const INIT_DATA = tg.initData || "";
            const BOT_UNAME = "{{BOT_USER}}";
            let uid = tg.initDataUnsafe?.user?.id || 0;
            let currentCat = "All";
            let activeMovieData = null;
            let isFav = false;
            let dlUnlocked = false;
            let timerInterval = null;
            let isUserVip = false;
            let currentPage = 1;

            // Init Welcome Screen
            setTimeout(() => { document.getElementById('welcomeScreen').classList.add('hide'); }, 2500);

            // Init User Info
            if(tg.initDataUnsafe?.user) {
                document.getElementById('uName').innerText = tg.initDataUnsafe.user.first_name;
                document.getElementById('profName').innerText = tg.initDataUnsafe.user.first_name;
                if(tg.initDataUnsafe.user.photo_url) document.getElementById('profPic').src = tg.initDataUnsafe.user.photo_url;
            }
            loadSocialLinks();

            function switchPage(pageId) {
                document.querySelectorAll('.app-page').forEach(p => p.classList.remove('active'));
                document.getElementById('page-'+pageId).classList.add('active');
                document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
                event.currentTarget.classList.add('active');
                if(pageId === 'home') loadMovies(1);
                if(pageId === 'favorites') loadFavorites();
                if(pageId === 'upcoming') loadUpcoming();
            }

            function openModal(id) { document.getElementById(id).style.display = 'flex'; }
            function closeModal(id) { document.getElementById(id).style.display = 'none'; }

            function verifyAge(cat) { openModal('ageModal'); currentCat = cat; }
            function confirmAge() { closeModal('ageModal'); filterCat(currentCat); }

            function filterCat(cat) {
                currentCat = cat;
                document.querySelectorAll('.cat-btn').forEach(b => b.classList.remove('active'));
                event.currentTarget.classList.add('active');
                loadMovies(1);
            }

            function formatViews(n) { if(n>=1000000) return (n/1000000).toFixed(1)+'M'; if(n>=1000) return (n/1000).toFixed(1)+'K'; return n; }

            async function loadMovies(page = 1) {
                currentPage = page;
                const grid = document.getElementById('movieGrid');
                grid.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
                try {
                    const r = await fetch(`/api/list?page=${page}&cat=${currentCat}&q=${document.getElementById('searchInput').value}&uid=${uid}`);
                    const data = await r.json();
                    if(data.movies && data.movies.length > 0) {
                        grid.innerHTML = data.movies.map(m => `
                            <div class="movie-row" onclick="openDetail('${m._id.replace(/'/g, "\\'")}')">
                                <img class="movie-poster" src="/api/image/${m.photo_id}" onerror="this.src='https://via.placeholder.com/90x120?text=No+Img'">
                                <div class="movie-info">
                                    <div class="movie-title-text">${m._id}</div>
                                    <div class="movie-meta">
                                        <span><i class="fa-solid fa-eye"></i> ${formatViews(m.clicks)}</span>
                                        <span><i class="fa-solid fa-list"></i> ${m.files.length} Files</span>
                                    </div>
                                    <div style="margin-top:5px;"><span class="badge-cat">${m.category || 'General'}</span></div>
                                </div>
                            </div>
                        `).join('');
                        
                        let pHtml = '';
                        if(data.total_pages > 1) {
                            for(let i=1; i<=data.total_pages; i++) {
                                pHtml += `<button style="padding:8px 12px; border-radius:8px; border:1px solid rgba(255,255,255,0.1); background:${i===page?'#00f260':'rgba(255,255,255,0.05)'}; color:${i===page?'#000':'#fff'}; margin:0 5px; cursor:pointer;" onclick="loadMovies(${i})">${i}</button>`;
                            }
                        }
                        document.getElementById('paginationBox').innerHTML = pHtml;
                    } else {
                        grid.innerHTML = '<p style="text-align:center; color:#94a3b8; padding:20px;">কোনো মুভি পাওয়া যায়নি!</p>';
                    }
                } catch(e) {}
            }

            let searchTimeout = null;
            document.getElementById('searchInput').addEventListener('input', (e) => {
                clearTimeout(searchTimeout);
                searchTimeout = setTimeout(() => loadMovies(1), 500);
            });

            async function searchFull() {
                const q = document.getElementById('searchInputFull').value;
                const grid = document.getElementById('searchResults');
                if(!q) return grid.innerHTML = '';
                try {
                    const r = await fetch(`/api/list?page=1&cat=All&q=${q}&uid=${uid}`);
                    const data = await r.json();
                    // Same rendering logic as loadMovies
                    if(data.movies && data.movies.length > 0) {
                        grid.innerHTML = data.movies.map(m => `<div class="movie-row" onclick="openDetail('${m._id.replace(/'/g, "\\'")}')"><img class="movie-poster" src="/api/image/${m.photo_id}"><div class="movie-info"><div class="movie-title-text">${m._id}</div><div class="movie-meta"><span class="badge-cat">${m.category || 'General'}</span></div></div></div>`).join('');
                    } else { grid.innerHTML = '<p style="text-align:center; color:#94a3b8;">পাওয়া যায়নি!</p>'; }
                } catch(e) {}
            }

            async function openDetail(title) {
                const r = await fetch(`/api/list?page=1&cat=All&q=${title}&uid=${uid}`);
                const data = await r.json();
                if(data.movies && data.movies.length > 0) {
                    activeMovieData = data.movies[0];
                    document.getElementById('detailPoster').src = `/api/image/${activeMovieData.photo_id}`;
                    document.getElementById('detailTitle').innerText = activeMovieData._id;
                    document.getElementById('detailMeta').innerText = `Views: ${formatViews(activeMovieData.clicks)} | Files: ${activeMovieData.files.length}`;
                    
                    // Check Fav
                    const fRes = await fetch(`/api/fav/check/${uid}/${encodeURIComponent(activeMovieData._id)}`);
                    const fData = await fRes.json();
                    isFav = fData.is_fav;
                    updateFavBtn();
                    
                    dlUnlocked = false;
                    openModal('detailModal');
                }
            }

            function updateFavBtn() {
                const btn = document.getElementById('favBtn');
                if(isFav) { btn.innerHTML = '<i class="fa-solid fa-heart" style="color:#ef4444;"></i> ফেভারিট থেকে সরান'; }
                else { btn.innerHTML = '<i class="fa-regular fa-heart"></i> ফেভারিটে যোগ করুন'; }
            }

            async function toggleFavorite() {
                if(!activeMovieData) return;
                const res = await fetch('/api/fav/toggle', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({uid: uid, title: activeMovieData._id, photo_id: activeMovieData.photo_id, initData: INIT_DATA}) });
                const data = await res.json();
                isFav = data.is_fav;
                updateFavBtn();
            }

            function startDownload() {
                if(isUserVip || dlUnlocked) { sendFile(); }
                else { startAdTimer(); }
            }

            function startAdTimer() {
                openModal('adModal');
                let timeLeft = 15;
                document.getElementById('adLinkBtn').style.display = 'block';
                document.getElementById('timerCircle').innerText = timeLeft;
                
                if(timerInterval) clearInterval(timerInterval);
                timerInterval = setInterval(() => {
                    timeLeft--;
                    document.getElementById('timerCircle').innerText = timeLeft;
                    if(timeLeft <= 0) {
                        clearInterval(timerInterval);
                        dlUnlocked = true;
                        closeModal('adModal');
                        sendFile();
                    }
                }, 1000);
            }

            function visitAdLink() {
                if(DIRECT_LINKS.length > 0) {
                    const link = DIRECT_LINKS[Math.floor(Math.random() * DIRECT_LINKS.length)];
                    tg.openLink(link);
                }
            }

            async function sendFile() {
                if(!activeMovieData) return;
                const fileId = activeMovieData.files[0].id; // Send first file
                try {
                    const res = await fetch('/api/send', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({userId: uid, movieId: fileId, initData: INIT_DATA}) });
                    const data = await res.json();
                    if(data.ok) { tg.showAlert("✅ ফাইল বটের ইনবক্সে পাঠানো হয়েছে!"); closeModal('detailModal'); }
                    else { tg.showAlert("⚠️ সমস্যা হয়েছে!"); }
                } catch(e) {}
            }

            async function loadFavorites() {
                const grid = document.getElementById('favGrid');
                grid.innerHTML = '<div class="skeleton"></div>';
                try {
                    const r = await fetch(`/api/fav/list/${uid}`);
                    const data = await r.json();
                    if(data.length > 0) {
                        grid.innerHTML = data.map(m => `<div class="movie-row" onclick="openDetail('${m.title.replace(/'/g, "\\'")}')"><img class="movie-poster" src="/api/image/${m.photo_id}" onerror="this.src='https://via.placeholder.com/90x120'"><div class="movie-info"><div class="movie-title-text">${m.title}</div></div></div>`).join('');
                    } else { grid.innerHTML = '<p style="text-align:center; color:#94a3b8; padding:20px;">কোনো ফেভারিট নেই!</p>'; }
                } catch(e) {}
            }

            async function loadUpcoming() {
                const grid = document.getElementById('upcomingGrid');
                grid.innerHTML = '<div class="skeleton"></div>';
                try {
                    const r = await fetch('/api/upcoming');
                    const data = await r.json();
                    if(data.length > 0) {
                        grid.innerHTML = data.map(m => `
                            <div class="upc-card">
                                <img class="upc-poster" src="/api/image/${m.photo_id}" onerror="this.src='https://via.placeholder.com/80x100'">
                                <div class="upc-info">
                                    <div style="font-weight:700; margin-bottom:5px;">${m.title}</div>
                                    <div style="font-size:12px; color:#00f260; margin-bottom:3px;"><i class="fa-solid fa-calendar"></i> ${m.release_date || 'N/A'}</div>
                                    <div style="font-size:12px; color:#94a3b8;"><i class="fa-solid fa-language"></i> ${m.language || 'N/A'}</div>
                                </div>
                            </div>
                        `).join('');
                    } else { grid.innerHTML = '<p style="text-align:center; color:#94a3b8;">কোনো আপকামিং মুভি নেই!</p>'; }
                } catch(e) {}
            }

            function loadSocialLinks() {
                if(SOCIAL_DATA.channel && SOCIAL_DATA.channel !== "#") { document.getElementById('socChannel').href = SOCIAL_DATA.channel; document.getElementById('socChannel').style.display = 'inline-flex'; }
                if(SOCIAL_DATA.fb && SOCIAL_DATA.fb !== "#") { document.getElementById('socFb').href = SOCIAL_DATA.fb; document.getElementById('socFb').style.display = 'inline-flex'; }
                if(SOCIAL_DATA.yt && SOCIAL_DATA.yt !== "#") { document.getElementById('socYt').href = SOCIAL_DATA.yt; document.getElementById('socYt').style.display = 'inline-flex'; }
                if(SOCIAL_DATA.web && SOCIAL_DATA.web !== "#") { document.getElementById('socWeb').href = SOCIAL_DATA.web; document.getElementById('socWeb').style.display = 'inline-flex'; }
                document.getElementById('profAbout').innerText = SOCIAL_DATA.about;
            }

            async function fetchUserInfo() {
                try {
                    const res = await fetch('/api/user/' + uid);
                    const data = await res.json();
                    isUserVip = data.vip;
                } catch(e) {}
            }

            fetchUserInfo(); loadMovies(1);
        </script>
    </body>
    </html>
    """
    html_code = html_code.replace("{{DIRECT_LINKS}}", dl_json).replace("{{SOCIAL_JSON}}", social_json).replace("{{TG_LINK}}", tg_url).replace("{{BOT_USER}}", BOT_USERNAME).replace("{{BKASH_NO}}", bkash_no).replace("{{NAGAD_NO}}", nagad_no).replace("{{ZONE_ID}}", zone_id).replace("{{AD_COUNT}}", str(required_ads)).replace("{{LINK_18}}", link_18)
    return html_code

# ==========================================
# 14. Main Web App APIs (Updated)
# ==========================================
@app.get("/api/user/{uid}")
async def get_user_info(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user: return {"vip": False, "is_admin": False, "refer_count": 0, "vip_expiry": None, "coins": 0, "badges": []}
    now = datetime.datetime.utcnow()
    is_vip = user.get("vip_until", now) > now
    return {"vip": is_vip, "is_admin": uid in admin_cache, "refer_count": user.get("refer_count", 0), "vip_expiry": user.get("vip_until").strftime("%d %b %Y") if is_vip else None, "coins": user.get("coins", 0)}

@app.get("/api/list")
async def list_movies(page: int = 1, q: str = "", cat: str = "All", uid: int = 0):
    if uid in banned_cache: return {"error": "banned"}
    limit = 10; skip = (page - 1) * limit
    
    match_stage = {}
    if q: match_stage["title"] = {"$regex": q, "$options": "i"}
    if cat and cat != "All": match_stage["category"] = cat

    pipeline = [
        {"$match": match_stage},
        {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "clicks": {"$sum": "$clicks"}, "created_at": {"$max": "$created_at"}, "category": {"$first": "$category"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main"]}}}}},
        {"$sort": {"created_at": -1}}, {"$skip": skip}, {"$limit": limit}
    ]
    count_pipe = [{"$match": match_stage}, {"$group": {"_id": "$title"}}, {"$count": "total"}]
    c_res = await db.movies.aggregate(count_pipe).to_list(1)
    total_groups = c_res[0]["total"] if c_res else 0
    total_pages = (total_groups + limit - 1) // limit

    movies = await db.movies.aggregate(pipeline).to_list(limit)
    return {"movies": movies, "total_pages": total_pages}

@app.get("/api/upcoming")
async def upcoming_movies():
    movies = await db.upcoming.find().sort("added_at", -1).limit(20).to_list(20)
    return [{"photo_id": m["photo_id"], "title": m.get("title", ""), "release_date": m.get("release_date", ""), "language": m.get("language", "")} for m in movies]

@app.get("/api/image/{photo_id}")
async def get_image(photo_id: str):
    try:
        cache = await db.file_cache.find_one({"photo_id": photo_id})
        now = datetime.datetime.utcnow()
        file_path = cache["file_path"] if cache and cache.get("expires_at", now) > now else (await bot.get_file(photo_id)).file_path
        if not cache or cache.get("expires_at", now) <= now: await db.file_cache.update_one({"photo_id": photo_id}, {"$set": {"file_path": file_path, "expires_at": now + datetime.timedelta(minutes=50)}}, upsert=True)
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        async def stream_image():
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url) as resp:
                    async for chunk in resp.content.iter_chunked(1024): yield chunk
        return StreamingResponse(stream_image(), media_type="image/jpeg")
    except Exception: return {"error": "not found"}

class SendRequestModel(BaseModel):
    userId: int
    movieId: str
    initData: str

@app.post("/api/send")
async def send_file(d: SendRequestModel):
    if d.userId == 0 or d.userId in banned_cache or not validate_tg_data(d.initData): return {"ok": False}
    try:
        m = await db.movies.find_one({"_id": ObjectId(d.movieId)})
        if m:
            now = datetime.datetime.utcnow(); user_data = await db.users.find_one({"user_id": d.userId})
            is_vip = user_data and user_data.get("vip_until", now) > now
            time_cfg = await db.settings.find_one({"id": "del_time"}); del_minutes = time_cfg['minutes'] if time_cfg else 60
            protect_cfg = await db.settings.find_one({"id": "protect_content"}); is_protected = protect_cfg['status'] if protect_cfg else True
            title_text = f"{m['title']} [{m.get('quality', '')}]" if m.get('quality') else m['title']
            caption = f"🎥 <b>{title_text}</b>\n\n📥 Join: https://t.me/lifetimebackup2026"
            if is_vip: caption += "\n🌟 VIP: ফাইল অটো-ডিলিট হবে না!"
            else: caption += f"\n⏳ সতর্কতা: {del_minutes} মিনিট পর অটো-ডিলিট হবে!"
            
            if m.get("file_type") == "video": sent_msg = await bot.send_video(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            else: sent_msg = await bot.send_document(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            await db.movies.update_one({"_id": ObjectId(d.movieId)}, {"$inc": {"clicks": 1}})
            
            if sent_msg and not is_vip:
                delete_at = now + datetime.timedelta(minutes=del_minutes)
                await db.auto_delete.insert_one({"chat_id": d.userId, "message_id": sent_msg.message_id, "delete_at": delete_at})
    except Exception: pass
    return {"ok": True}

# Favorites APIs
class FavModel(BaseModel):
    uid: int; title: str; photo_id: str; initData: str

@app.post("/api/fav/toggle")
async def toggle_fav(data: FavModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    existing = await db.favorites.find_one({"user_id": data.uid, "movie_title": data.title})
    if existing:
        await db.favorites.delete_one({"_id": existing["_id"]})
        return {"ok": True, "is_fav": False}
    else:
        await db.favorites.insert_one({"user_id": data.uid, "movie_title": data.title, "photo_id": data.photo_id})
        return {"ok": True, "is_fav": True}

@app.get("/api/fav/check/{uid}/{title}")
async def check_fav(uid: int, title: str):
    existing = await db.favorites.find_one({"user_id": uid, "movie_title": title})
    return {"is_fav": existing is not None}

@app.get("/api/fav/list/{uid}")
async def list_favs(uid: int):
    favs = await db.favorites.find({"user_id": uid}).to_list(100)
    return [{"title": f["movie_title"], "photo_id": f["photo_id"]} for f in favs]

# Payment, Checkin & Other APIs (Kept intact from original code)
class ReviewModel(BaseModel): uid: int; name: str; title: str; rating: int; comment: str; initData: str
@app.get("/api/reviews/{title}")
async def get_reviews(title: str): return await db.reviews.find({"movie_title": title}).sort("created_at", -1).to_list(15)
@app.post("/api/reviews")
async def add_review(data: ReviewModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    await db.reviews.insert_one({"user_id": data.uid, "name": data.name, "movie_title": data.title, "rating": data.rating, "comment": data.comment, "created_at": datetime.datetime.utcnow()})
    return {"ok": True}

class CheckinModel(BaseModel): uid: int; action: str; initData: str
@app.post("/api/checkin")
async def handle_checkin(data: CheckinModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    user = await db.users.find_one({"user_id": data.uid}); now = datetime.datetime.utcnow()
    if data.action == "claim":
        if user.get("last_checkin", now).date() >= now.date(): return {"ok": False, "msg": "ইতিমধ্যে নিয়েছেন!"}
        await db.users.update_one({"user_id": data.uid}, {"$inc": {"coins": 10}, "$set": {"last_checkin": now}}); return {"ok": True}
    elif data.action == "convert":
        if user.get("coins", 0) < 50: return {"ok": False, "msg": "৫০ কয়েন প্রয়োজন!"}
        current_vip = user.get("vip_until", now);
        if current_vip < now: current_vip = now
        await db.users.update_one({"user_id": data.uid}, {"$inc": {"coins": -50}, "$set": {"vip_until": current_vip + datetime.timedelta(days=1)}}); return {"ok": True}

class PaymentModel(BaseModel): uid: int; method: str; trx_id: str; days: int; price: int; initData: str
@app.post("/api/payment/submit")
async def submit_payment(data: PaymentModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    if await db.payments.find_one({"trx_id": data.trx_id}): return {"ok": False, "msg": "TrxID আগে ব্যবহার হয়েছে!"}
    res = await db.payments.insert_one({"user_id": data.uid, "method": data.method, "trx_id": data.trx_id, "amount": data.price, "days": data.days, "status": "pending", "created_at": datetime.datetime.utcnow()})
    try:
        builder = InlineKeyboardBuilder(); builder.button(text="✅ Approve", callback_data=f"trx_approve_{res.inserted_id}"); builder.button(text="❌ Reject", callback_data=f"trx_reject_{res.inserted_id}")
        await bot.send_message(OWNER_ID, f"💰 <b>নতুন পেমেন্ট!</b>\n👤 ID: <code>{data.uid}</code>\n🏦 {data.method.upper()}\n🧾 TrxID: <code>{data.trx_id}</code>\n💵 {data.price} BDT\n⏳ {data.days} Days VIP", parse_mode="HTML", reply_markup=builder.as_markup())
    except: pass
    return {"ok": True}

# ==========================================
# 15. Main Application Startup
# ==========================================
async def start():
    print("Initializing Database & Cache...")
    await init_db(); await load_admins(); await load_banned_users()
    port = int(os.getenv("PORT", 8000)); config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio"); server = uvicorn.Server(config)
    print("Starting Background Workers..."); asyncio.create_task(auto_delete_worker())
    print("Connecting to Telegram Bot API..."); await bot.delete_webhook(drop_pending_updates=True)
    print("Server is Running!"); await asyncio.gather(server.serve(), dp.start_polling(bot))

if __name__ == "__main__": 
    loop = asyncio.get_event_loop(); loop.run_until_complete(start())
