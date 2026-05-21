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
BOT_USERNAME = "bdlatestmovie_bot" # আপনার বটের ইউজারনেম

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
    waiting_for_upc_photo = State()
    waiting_for_upc_title = State()


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
    await db.auto_delete.create_index("delete_at")
    await db.users.create_index("joined_at")
    await db.reviews.create_index("movie_title")
    await db.payments.create_index("trx_id", unique=True)
    await db.requests.create_index("movie") 


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
                            await bot.send_message(referrer_id, "🎉 <b>অভিনন্দন!</b> আপনার ৫ জন রেফার পূর্ণ হয়েছে। আপনাকে ২৪ ঘণ্টার জন্য <b>VIP</b> দেওয়া হয়েছে! এখন আপনি বিনা অ্যাডে মুভি ডাউনলোড করতে পারবেন।", parse_mode="HTML")
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
            "🔸 অ্যাডমিন প্যানেল: <code>/addadmin ID</code> | <code>/deladmin ID</code> | <code>/adminlist</code>\n"
            "🔸 ডাইরেক্ট লিংক: <code>/addlink লিংক</code> | <code>/dellink লিংক</code> | <code>/seelinks</code>\n"
            "🔸 অ্যাড জোন: <code>/setad ID</code> | অ্যাড সংখ্যা: <code>/setadcount সংখ্যা</code>\n"
            "🔸 টেলিগ্রাম: <code>/settg লিংক</code> | 18+: <code>/set18 লিংক</code>\n"
            "🔸 পেমেন্ট নাম্বার সেট: <code>/setbkash নাম্বার</code> | <code>/setnagad নাম্বার</code>\n"
            "🔸 প্রোটেকশন: <code>/protect on</code> বা <code>/protect off</code>\n"
            "🔸 অটো-ডিলিট টাইম: <code>/settime [মিনিট]</code>\n"
            "🔸 স্ট্যাটাস: <code>/stats</code> | ব্রডকাস্ট: <code>/cast</code>\n"
            "🔸 মুভি ডিলিট: <code>/delmovie মুভির নাম</code>\n"
            "🔸 ব্যান: <code>/ban ID</code> | আনব্যান: <code>/unban ID</code>\n"
            "🔸 VIP দিন: <code>/addvip ID দিন</code> | VIP বাতিল: <code>/removevip ID</code>\n"
            "🔸 আপকামিং মুভি অ্যাড: <code>/addupcoming</code>\n"
            "🔸 আপকামিং ডিলিট: <code>/delupcoming</code>\n\n"
            f"🌐 <b>ওয়েব অ্যাডমিন প্যানেল:</b> <a href='{APP_URL}/admin'>এখানে ক্লিক করুন</a>\n"
            "<i>লগিন: admin / admin123</i>\n\n"
            "📥 <b>মুভি অ্যাড করতে প্রথমে ভিডিও বা ডকুমেন্ট ফাইল পাঠান।</b>"
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
# 7. Telegram Bot Commands (Admin Settings, Manage & VIP)
# ==========================================
def format_views(n):
    if n >= 1000000: return f"{n/1000000:.1f}M".replace(".0M", "M")
    if n >= 1000: return f"{n/1000:.1f}K".replace(".0K", "K")
    return str(n)

@dp.message(Command("addlink"))
async def add_direct_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer(f"✅ ডাইরেক্ট লিংক সফলভাবে অ্যাড করা হয়েছে:\n<code>{url}</code>", parse_mode="HTML")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/addlink https://example.com</code>", parse_mode="HTML")

@dp.message(Command("dellink"))
async def del_direct_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        result = await db.settings.update_one({"id": "direct_links"}, {"$pull": {"links": url}})
        if result.modified_count > 0:
            await m.answer(f"❌ লিংকটি সফলভাবে ডিলিট করা হয়েছে:\n<code>{url}</code>", parse_mode="HTML")
        else:
            await m.answer("⚠️ লিংকটি ডাটাবেসে পাওয়া যায়নি।")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/dellink https://example.com</code>", parse_mode="HTML")

@dp.message(Command("seelinks"))
async def see_direct_links(m: types.Message):
    if m.from_user.id not in admin_cache: return
    dl_cfg = await db.settings.find_one({"id": "direct_links"})
    links = dl_cfg.get("links", []) if dl_cfg else []
    
    if not links:
        return await m.answer("⚠️ কোনো ডাইরেক্ট লিংক অ্যাড করা নেই।")
        
    text = "🔗 <b>বর্তমান ডাইরেক্ট লিংক সমূহ:</b>\n\n"
    for i, link in enumerate(links, 1):
        text += f"{i}. <code>{link}</code>\n"
        
    await m.answer(text, parse_mode="HTML", disable_web_page_preview=True)

@dp.message(Command("addadmin"))
async def add_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return await m.answer("⚠️ শুধুমাত্র মেইন Owner অ্যাডমিন অ্যাড করতে পারবে!")
    try:
        target_uid = int(m.text.split()[1])
        await db.admins.update_one({"user_id": target_uid}, {"$set": {"user_id": target_uid}}, upsert=True)
        admin_cache.add(target_uid)
        await m.answer(f"✅ ইউজার <code>{target_uid}</code> কে সফলভাবে অ্যাডমিন বানানো হয়েছে!", parse_mode="HTML")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/addadmin ইউজার_আইডি</code>", parse_mode="HTML")

@dp.message(Command("deladmin"))
async def del_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return await m.answer("⚠️ শুধুমাত্র মেইন Owner অ্যাডমিন রিমুভ করতে পারবে!")
    try:
        target_uid = int(m.text.split()[1])
        if target_uid == OWNER_ID: return await m.answer("⚠️ Main Owner কে ডিলিট করা সম্ভব নয়!")
        await db.admins.delete_one({"user_id": target_uid})
        admin_cache.discard(target_uid)
        await m.answer(f"❌ ইউজার <code>{target_uid}</code> কে অ্যাডমিন লিস্ট থেকে রিমুভ করা হয়েছে!", parse_mode="HTML")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/deladmin ইউজার_আইডি</code>", parse_mode="HTML")

@dp.message(Command("adminlist"))
async def list_admin_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    text = f"👑 <b>মেইন Owner:</b>\n▪️ <code>{OWNER_ID}</code>\n\n👮‍♂️ <b>অন্যান্য অ্যাডমিনগণ:</b>\n"
    count = 0
    async for a in db.admins.find():
        text += f"▪️ <code>{a['user_id']}</code>\n"
        count += 1
    if count == 0: text += "<i>কেউ নেই</i>"
    await m.answer(text, parse_mode="HTML")

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        title = m.text.split(" ", 1)[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0:
            await m.answer(f"✅ '<b>{title}</b>' নামের {result.deleted_count} টি ফাইল সফলভাবে ডিলিট হয়েছে!", parse_mode="HTML")
        else:
            await m.answer("⚠️ এই নামের কোনো মুভি ডাটাবেসে পাওয়া যায়নি। (নাম হুবহু মিলতে হবে)")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/delmovie মুভির সম্পূর্ণ নাম</code>", parse_mode="HTML")

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
    
    text = (f"📊 <b>অ্যাডভান্সড স্ট্যাটাস:</b>\n\n👥 মোট ইউজার: <code>{uc}</code>\n🟢 আজকের নতুন ইউজার: <code>{new_users_today}</code>\n"
            f"🎬 মোট ফাইল আপলোড: <code>{mc}</code>\n\n🔥 <b>টপ ৫ মুভি/সিরিজ:</b>\n{top_movies_text if top_movies_text else 'কোনো মুভি নেই'}")
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
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/ban ইউজার_আইডি</code>", parse_mode="HTML")

@dp.message(Command("unban"))
async def unban_user_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        target_uid = int(m.text.split()[1])
        await db.banned.delete_one({"user_id": target_uid})
        banned_cache.discard(target_uid)
        await m.answer(f"✅ ইউজার <code>{target_uid}</code> আনব্যান হয়েছে!", parse_mode="HTML")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/unban ইউজার_আইডি</code>", parse_mode="HTML")

@dp.message(Command("setadcount"))
async def set_ad_count_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        count = int(m.text.split(" ")[1])
        count = max(1, count)
        await db.settings.update_one({"id": "ad_count"}, {"$set": {"count": count}}, upsert=True)
        await m.answer(f"✅ অ্যাড দেখার সংখ্যা সেট করা হয়েছে: <b>{count} টি</b>।", parse_mode="HTML")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/setadcount 3</code>", parse_mode="HTML")

@dp.message(Command("protect"))
async def protect_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        state = m.text.split(" ")[1].lower()
        if state == "on":
            await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": True}}, upsert=True)
            await m.answer("✅ ফরোয়ার্ড প্রোটেকশন চালু করা হয়েছে।")
        elif state == "off":
            await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": False}}, upsert=True)
            await m.answer("✅ ফরোয়ার্ড প্রোটেকশন বন্ধ করা হয়েছে।")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/protect on</code> অথবা <code>/protect off</code>")

@dp.message(Command("settime"))
async def set_del_time(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        mins = int(m.text.split(" ")[1])
        await db.settings.update_one({"id": "del_time"}, {"$set": {"minutes": mins}}, upsert=True)
        await m.answer("✅ অটো-ডিলিট টাইম সেট করা হয়েছে।")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/settime 60</code> (মিনিট)", parse_mode="HTML")

@dp.message(Command("setad"))
async def set_ad(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        zone = m.text.split(" ")[1]
        await db.settings.update_one({"id": "ad_config"}, {"$set": {"zone_id": zone}}, upsert=True)
        await m.answer("✅ জোন আপডেট হয়েছে।")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/setad 1234567</code>")

@dp.message(Command("settg"))
async def set_tg_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ")[1]
        await db.settings.update_one({"id": "link_tg"}, {"$set": {"url": link}}, upsert=True)
        await m.answer(f"✅ টেলিগ্রাম লিংক সেট করা হয়েছে: <b>{link}</b>", parse_mode="HTML")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/settg https://t.me/YourChannel</code>", parse_mode="HTML")

@dp.message(Command("set18"))
async def set_18_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ")[1]
        await db.settings.update_one({"id": "link_18"}, {"$set": {"url": link}}, upsert=True)
        await m.answer(f"✅ 18+ লিংক সেট করা হয়েছে: <b>{link}</b>", parse_mode="HTML")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/set18 https://t.me/YourChannel</code>", parse_mode="HTML")

@dp.message(Command("setbkash"))
async def set_bkash(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        num = m.text.split(" ")[1]
        await db.settings.update_one({"id": "bkash_no"}, {"$set": {"number": num}}, upsert=True)
        await m.answer(f"✅ বিকাশ নাম্বার সেট করা হয়েছে: <b>{num}</b>", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/setbkash 017XXXXXXX</code>", parse_mode="HTML")

@dp.message(Command("setnagad"))
async def set_nagad(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        num = m.text.split(" ")[1]
        await db.settings.update_one({"id": "nagad_no"}, {"$set": {"number": num}}, upsert=True)
        await m.answer(f"✅ নগদ নাম্বার সেট করা হয়েছে: <b>{num}</b>", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/setnagad 017XXXXXXX</code>", parse_mode="HTML")

@dp.message(Command("addvip"))
async def add_vip_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        args = m.text.split()
        target_uid = int(args[1])
        days = int(args[2]) if len(args) > 2 else 30 
        
        now = datetime.datetime.utcnow()
        user = await db.users.find_one({"user_id": target_uid})
        if not user:
            return await m.answer("⚠️ এই ইউজারটি ডাটাবেসে নেই। তাকে আগে বট স্টার্ট করতে বলুন।")

        current_vip = user.get("vip_until", now)
        if current_vip < now:
            current_vip = now
            
        new_vip = current_vip + datetime.timedelta(days=days)
        await db.users.update_one({"user_id": target_uid}, {"$set": {"vip_until": new_vip}})
        await m.answer(f"✅ ইউজার <code>{target_uid}</code> কে সফলভাবে <b>{days} দিনের</b> VIP দেওয়া হয়েছে!", parse_mode="HTML")
        
        try:
            await bot.send_message(target_uid, f"🎉 <b>অভিনন্দন!</b> অ্যাডমিন আপনাকে <b>{days} দিনের</b> জন্য VIP মেম্বারশিপ দিয়েছেন।\n\nএখন আপনি কোনো অ্যাড ছাড়াই মুভি ডাউনলোড করতে পারবেন এবং আপনার ফাইল কখনো অটো-ডিলিট হবে কাশী হবে না!", parse_mode="HTML")
        except Exception: pass
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/addvip ইউজার_আইডি দিন</code>\nউদাহরণ: <code>/addvip 123456789 30</code>", parse_mode="HTML")

@dp.message(Command("removevip"))
async def remove_vip_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        target_uid = int(m.text.split()[1])
        now = datetime.datetime.utcnow()
        await db.users.update_one({"user_id": target_uid}, {"$set": {"vip_until": now - datetime.timedelta(days=1)}})
        await m.answer(f"❌ ইউজার <code>{target_uid}</code> এর VIP বাতিল করা হয়েছে!", parse_mode="HTML")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/removevip ইউজার_আইডি</code>", parse_mode="HTML")


# ==========================================
# 8. Admin Inline Callback (Payment Approval & Requests)
# ==========================================
@dp.callback_query(F.data.startswith("trx_"))
async def handle_trx_approval(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    action, _, pay_id = c.data.split("_")
    
    payment = await db.payments.find_one({"_id": ObjectId(pay_id)})
    if not payment or payment["status"] != "pending":
        return await c.answer("⚠️ এই পেমেন্টটি ইতিমধ্যে প্রসেস করা হয়েছে!", show_alert=True)
        
    user_id = payment["user_id"]
    days = payment["days"]
    
    if action == "approve":
        now = datetime.datetime.utcnow()
        user = await db.users.find_one({"user_id": user_id})
        current_vip = user.get("vip_until", now) if user else now
        if current_vip < now: current_vip = now
        new_vip = current_vip + datetime.timedelta(days=days)
        
        await db.users.update_one({"user_id": user_id}, {"$set": {"vip_until": new_vip}})
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "approved"}})
        
        pkg_text = f"{days} দিনের"
        if days == 30: pkg_text = "১ মাসের"
        elif days == 90: pkg_text = "৩ মাসের"
        elif days == 180: pkg_text = "৬ মাসের"
        
        await c.message.edit_text(c.message.text + f"\n\n✅ <b>পেমেন্ট অ্যাপ্রুভ করা হয়েছে! ({pkg_text})</b>", parse_mode="HTML")
        try: await bot.send_message(user_id, f"🎉 <b>পেমেন্ট সফল!</b> আপনার পেমেন্ট অ্যাপ্রুভ হয়েছে এবং আপনাকে <b>{pkg_text}</b> VIP দেওয়া হয়েছে!", parse_mode="HTML")
        except: pass
    else:
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "rejected"}})
        await c.message.edit_text(c.message.text + "\n\n❌ <b>পেমেন্ট রিজেক্ট করা হয়েছে!</b>", parse_mode="HTML")
        try: await bot.send_message(user_id, f"❌ <b>দুঃখিত!</b> আপনার পেমেন্ট (TrxID: {payment['trx_id']}) বাতিল করা হয়েছে। তথ্যে ভুল থাকলে সাপোর্ট অ্যাডমিনের সাথে যোগাযোগ করুন।", parse_mode="HTML")
        except: pass

@dp.callback_query(F.data.startswith("req_"))
async def handle_request_approval(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    action = c.data.split("_")[1] # acc or rej
    req_id = c.data.split("_")[2]
    
    req = await db.requests.find_one({"_id": ObjectId(req_id)})
    if not req:
        return await c.answer("⚠️ রিকোয়েস্টটি ইতিমধ্যে ডিলিট বা প্রসেস করা হয়েছে!", show_alert=True)
        
    movie_name = req["movie"]
    voters = req.get("voters", [])
    
    if action == "acc":
        await c.message.edit_text(c.message.text + "\n\n✅ <b>Approve করা হয়েছে! (ইউজারদের জানানো হয়েছে)</b>", parse_mode="HTML")
        for v_id in voters:
            try: await bot.send_message(v_id, f"🎉 <b>সুখবর!</b> আপনার রিকোয়েস্ট করা/ভোট দেওয়া মুভি <b>{movie_name}</b> অ্যাপে আপলোড করা হয়েছে! এখনই অ্যাপ ওপেন করে দেখে নিন।", parse_mode="HTML")
            except: pass
        await db.requests.delete_one({"_id": ObjectId(req_id)})
    elif action == "rej":
        await c.message.edit_text(c.message.text + "\n\n❌ <b>Reject করা হয়েছে!</b>", parse_mode="HTML")
        for v_id in voters:
            try: await bot.send_message(v_id, f"❌ <b>দুঃখিত!</b> আপনার রিকোয়েস্ট করা মুভি <b>{movie_name}</b> এই মুহূর্তে আপলোড করা সম্ভব হচ্ছেবিধা (হয়তো কোয়ালিটি ভালো না বা পাওয়া যায়নি)।", parse_mode="HTML")
            except: pass
        await db.requests.delete_one({"_id": ObjectId(req_id)})


# ==========================================
# 9. Movie Upload Logic 
# ==========================================
@dp.message(F.content_type.in_({'video', 'document'}), lambda m: m.from_user.id in admin_cache)
async def receive_movie_file(m: types.Message, state: FSMContext):
    fid = m.video.file_id if m.video else m.document.file_id
    ftype = "video" if m.video else "document"
    await state.set_state(AdminStates.waiting_for_photo)
    await state.update_data(file_id=fid, file_type=ftype)
    await m.answer("✅ ফাইল পেয়েছি! এবার মুভির <b>পোস্টার (Photo)</b> সেন্ড করুন।\nবাতিল করতে /start দিন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_photo, F.photo)
async def receive_movie_photo(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_title)
    await m.answer("✅ পোস্টার পেয়েছি! এবার <b>মুভি বা ওয়েব সিরিজের নাম</b> লিখে পাঠান।\n<i>(নোট: যদি ওয়েব সিরিজ হয় বা একই মুভির অন্য কোয়ালিটি অ্যাড করতে চান, তবে আগের নামটিই হুবহু দিন)</i>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer("✅ নাম সেভ হয়েছে! এবার এই ফাইলটির <b>কোয়ালিটি বা এপিসোড নাম্বার</b> দিন।\n<i>(উদাহরণ: 480p, 720p, 1080p অথবা Episode 01)</i>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    quality = m.text.strip()
    data = await state.get_data()
    await state.clear()
    
    title = data["title"]
    photo_id = data["photo_id"]
    
    await db.movies.insert_one({
        "title": title, "quality": quality, "photo_id": photo_id, 
        "file_id": data["file_id"], "file_type": data["file_type"],
        "clicks": 0, "created_at": datetime.datetime.utcnow()
    })
    
    await m.answer(f"🎉 <b>{title} [{quality}]</b> অ্যাপে সফলভাবে যুক্ত করা হয়েছে!", parse_mode="HTML")
    
    req = await db.requests.find_one({"movie": {"$regex": f"^{title}$", "$options": "i"}})
    if req:
        for v_id in req.get("voters", []):
            try:
                await bot.send_message(v_id, f"🔔 <b>কাস্টম নোটিফিকেশন:</b>\n\n🎉 সুখবর! আপনার রিকোয়েস্ট করা/ভোট দেওয়া মুভি <b>{title}</b> অ্যাপে আপলোড করা হয়েছে! এখনই অ্যাপ ওপেন করে দেখে নিন।", parse_mode="HTML")
            except Exception: pass
        await db.requests.delete_one({"_id": req["_id"]})
    
    if CHANNEL_ID and CHANNEL_ID != "-100XXXXXXXXXX":
        try:
            old_post = await db.channel_posts.find_one({"title": title})
            if old_post:
                try:
                    await bot.delete_message(chat_id=CHANNEL_ID, message_id=old_post["message_id"])
                except Exception:
                    pass
            
            bot_info = await bot.get_me()
            kb = [[types.InlineKeyboardButton(text="🎬 মুভিটি পেতে এখানে ক্লিক করুন", url=f"https://t.me/{bot_info.username}?start=new")]]
            markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
            caption = f"🎬 <b>নতুন ফাইল যুক্ত হয়েছে!</b>\n\n📌 <b>নাম:</b> {title}\n🏷 <b>কোয়ালিটি/এপিসোড:</b> {quality}\n\n👇 <i>ডাউনলোড করতে নিচের বাটনে ক্লিক করুন।</i>"
            
            sent_msg = await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_id, caption=caption, parse_mode="HTML", reply_markup=markup)
            
            await db.channel_posts.update_one(
                {"title": title},
                {"$set": {"message_id": sent_msg.message_id}},
                upsert=True
            )
        except Exception: 
            pass


# ==========================================
# 10. Upcoming Movies Logic
# ==========================================
@dp.message(Command("addupcoming"))
async def add_upc_cmd(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_upc_photo)
    await m.answer("🌟 <b>আপকামিং মুভির পোস্টার (Photo) সেন্ড করুন:</b>\nবাতিল করতে /start দিন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_photo, F.photo)
async def upc_photo_step(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_upc_title)
    await m.answer("✅ পোস্টার পেয়েছি! এবার আপকামিং মুভির <b>টাইটেল (নাম)</b> লিখে পাঠান।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_title, F.text)
async def upc_title_step(m: types.Message, state: FSMContext):
    data = await state.get_data()
    await db.upcoming.insert_one({
        "photo_id": data["photo_id"],
        "title": m.text.strip(),
        "added_at": datetime.datetime.utcnow()
    })
    await state.clear()
    await m.answer("✅ আপকামিং মুভি সফলভাবে যুক্ত করা হয়েছে!")

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
    await m.answer("📢 যে মেসেজটি ব্রডকাস্ট করতে চান সেটি পাঠান।\nবাতিল করতে /start দিন।")

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
            success += 1
            await asyncio.sleep(0.05)
        except Exception: pass
    await m.answer(f"✅ সম্পন্ন! সর্বমোট <b>{success}</b> জনকে মেসেজ পাঠানো হয়েছে।", parse_mode="HTML")

@dp.callback_query(F.data.startswith("reply_"))
async def process_reply_cb(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in admin_cache: return
    user_id = int(c.data.split("_")[1])
    await state.set_state(AdminStates.waiting_for_reply)
    await state.update_data(target_uid=user_id)
    await c.message.reply("✍️ <b>ইউজারকে কী রিপ্লাই দিতে চান তা লিখে পাঠান:</b>", parse_mode="HTML")
    await c.answer()

@dp.message(AdminStates.waiting_for_reply)
async def send_reply(m: types.Message, state: FSMContext):
    data = await state.get_data()
    target_uid = data.get("target_uid")
    await state.clear()
    try:
        if m.text: 
            await bot.send_message(target_uid, f"📩 <b>অ্যাডমিন রিপ্লাই:</b>\n\n{m.text}", parse_mode="HTML")
        else: 
            await m.copy_to(target_uid, caption=f"📩 <b>অ্যাডমিন রিপ্লাই:</b>\n\n{m.caption or ''}", parse_mode="HTML")
        await m.answer("✅ ইউজারকে সফলভাবে রিপ্লাই পাঠানো হয়েছে!")
    except Exception:
        await m.answer("⚠️ রিপ্লাই পাঠানো যায়নি! ইউজার হয়তো বট ব্লক করেছে।")


# ==========================================
# 12. Web Admin Panel API & HTML
# ==========================================
@app.get("/admin", response_class=HTMLResponse)
async def web_admin_panel(auth: bool = Depends(verify_admin)):
    html_content = """
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>MovieZone Admin Panel</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    </head>
    <body class="bg-gray-900 text-white font-sans antialiased relative">
        <div class="max-w-6xl mx-auto p-5">
            <div class="flex justify-between items-center mb-8 border-b border-gray-700 pb-4">
                <h1 class="text-3xl font-bold text-red-500"><i class="fa-solid fa-shield-halved"></i> MovieZone Admin</h1>
            </div>
            
            <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-10">
                <div class="bg-gray-800 p-6 rounded-xl shadow-lg border border-gray-700">
                    <h3 class="text-gray-400 text-sm font-bold">TOTAL USERS</h3>
                    <p class="text-4xl font-bold text-green-400 mt-2" id="statUsers">...</p>
                </div>
                <div class="bg-gray-800 p-6 rounded-xl shadow-lg border border-gray-700">
                    <h3 class="text-gray-400 text-sm font-bold">UNIQUE GROUPS</h3>
                    <p class="text-4xl font-bold text-blue-400 mt-2" id="statMovies">...</p>
                </div>
                <div class="bg-gray-800 p-6 rounded-xl shadow-lg border border-gray-700">
                    <h3 class="text-gray-400 text-sm font-bold">NEW USERS TODAY</h3>
                    <p class="text-4xl font-bold text-yellow-400 mt-2" id="statNew">...</p>
                </div>
            </div>

            <div class="bg-gray-800 rounded-xl shadow-lg border border-gray-700 p-6">
                <h2 class="text-xl font-bold mb-4 text-gray-200"><i class="fa-solid fa-film text-red-400"></i> Manage Movies & Streams</h2>
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm whitespace-nowrap">
                        <thead class="bg-gray-700 text-gray-300">
                            <tr>
                                <th class="p-4 rounded-tl-lg">Movie / Series Title</th>
                                <th class="p-4">Total Views</th>
                                <th class="p-4">Files</th>
                                <th class="p-4 rounded-tr-lg">Action</th>
                            </tr>
                        </thead>
                        <tbody id="movieTableBody">
                            <tr><td colspan="4" class="text-center p-8 text-gray-400">Loading data...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <div id="adminEditModal" class="fixed inset-0 bg-black bg-opacity-80 hidden flex items-center justify-center z-50">
            <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 w-full max-w-md relative">
                <button onclick="closeAdminEdit()" class="absolute top-3 right-4 text-gray-400 hover:text-red-500 text-2xl">&times;</button>
                <h3 class="text-xl font-bold mb-4 text-white"><i class="fa-solid fa-pen-to-square text-blue-400"></i> Edit Movie</h3>
                <input type="hidden" id="editOldTitle">
                <div class="mb-5">
                    <label class="block text-gray-400 text-sm mb-1 font-bold">Movie Title (সব ফাইলের নাম বদলাবে)</label>
                    <input type="text" id="editNewTitle" class="w-full bg-gray-900 border border-gray-600 rounded p-2 text-white outline-none focus:border-blue-500">
                </div>
                <button onclick="saveAdminEdit()" class="w-full bg-blue-600 text-white rounded p-3 font-bold hover:bg-blue-500 transition text-lg shadow-lg">Save Changes</button>
            </div>
        </div>

        <script>
            function formatViews(num) {
                if (num >= 1000000) return (num / 1000000).toFixed(1).replace('.0', '') + 'M';
                if (num >= 1000) return (num / 1000).toFixed(1).replace('.0', '') + 'K';
                return num;
            }
            async function loadStats() {
                try {
                    let r = await fetch('/api/admin/stats');
                    let d = await r.json();
                    document.getElementById('statUsers').innerText = d.total_users;
                    document.getElementById('statMovies').innerText = d.total_movies;
                    document.getElementById('statNew').innerText = d.new_today;
                } catch(e){}
            }
            async function loadMovies() {
                try {
                    let r = await fetch('/api/admin/movies');
                    let movies = await r.json();
                    let html = '';
                    if(movies.length === 0) {
                        html = '<tr><td colspan="4" class="text-center p-8 text-gray-400">No movies found.</td></tr>';
                    } else {
                        movies.forEach(m => {
                            html += `<tr class="border-b border-gray-700 hover:bg-gray-750 transition">
                                <td class="p-4 font-medium text-white">${m.title}</td>
                                <td class="p-4 text-gray-300 font-bold">${formatViews(m.total_clicks)}</td>
                                <td class="p-4 text-blue-400 font-semibold">${m.files_count} files</td>
                                <td class="p-4 flex gap-2">
                                    <button onclick="openAdminEdit('${btoa(unescape(encodeURIComponent(m.title)))}')" class="bg-blue-600 hover:bg-blue-500 text-white px-3 py-1.5 rounded text-xs font-bold transition"><i class="fa-solid fa-edit"></i> Edit</button>
                                    <button onclick="deleteMovie('${btoa(unescape(encodeURIComponent(m.title)))}')" class="bg-red-600 hover:bg-red-500 text-white px-3 py-1.5 rounded text-xs font-bold transition"><i class="fa-solid fa-trash"></i> Delete</button>
                                </td>
                            </tr>`;
                        });
                    }
                    document.getElementById('movieTableBody').innerHTML = html;
                } catch(e){}
            }
            function openAdminEdit(encodedTitle) {
                let title = decodeURIComponent(escape(atob(encodedTitle)));
                document.getElementById('editOldTitle').value = title;
                document.getElementById('editNewTitle').value = title;
                document.getElementById('adminEditModal').classList.remove('hidden');
            }
            function closeAdminEdit() {
                document.getElementById('adminEditModal').classList.add('hidden');
            }
            async function saveAdminEdit() {
                let old_title = document.getElementById('editOldTitle').value;
                let new_title = document.getElementById('editNewTitle').value.trim();
                if(!new_title) return;
                let r = await fetch('/api/admin/edit-movie', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({old_title, new_title})
                });
                let d = await r.json();
                if(d.ok) { closeAdminEdit(); loadMovies(); } else { alert(d.msg || "Error"); }
            }
            async function deleteMovie(encodedTitle) {
                let title = decodeURIComponent(escape(atob(encodedTitle)));
                if(!confirm(`Are you sure you want to delete all files under "${title}"?`)) return;
                let r = await fetch('/api/admin/delete-movie', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title})
                });
                let d = await r.json();
                if(d.ok) { loadMovies(); loadStats(); }
            }
            window.onload = function() { loadStats(); loadMovies(); };
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/api/admin/stats")
async def api_admin_stats(auth: bool = Depends(verify_admin)):
    uc = await db.users.count_documents({})
    mc = await db.movies.count_documents({})
    now = datetime.datetime.utcnow()
    today_start = datetime.datetime(now.year, now.month, now.day)
    new_today = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    return {"total_users": uc, "total_movies": mc, "new_today": new_today}

@app.get("/api/admin/movies")
async def api_admin_movies(auth: bool = Depends(verify_admin)):
    pipeline = [
        {"$group": {"_id": "$title", "total_clicks": {"$sum": "$clicks"}, "files_count": {"$sum": 1}}},
        {"$sort": {"_id": 1}}
    ]
    results = await db.movies.aggregate(pipeline).to_list(None)
    return [{"title": r["_id"], "total_clicks": r["total_clicks"], "files_count": r["files_count"]} for r in results]

class EditMovieSchema(BaseModel):
    old_title: str
    new_title: str

@app.post("/api/admin/edit-movie")
async def api_edit_movie(data: EditMovieSchema, auth: bool = Depends(verify_admin)):
    if not data.new_title.strip():
        return {"ok": False, "msg": "Title cannot be empty"}
    await db.movies.update_many({"title": data.old_title}, {"$set": {"title": data.new_title.strip()}})
    await db.channel_posts.update_many({"title": data.old_title}, {"$set": {"title": data.new_title.strip()}})
    return {"ok": True}

class DeleteMovieSchema(BaseModel):
    title: str

@app.post("/api/admin/delete-movie")
async def api_delete_movie(data: DeleteMovieSchema, auth: bool = Depends(verify_admin)):
    await db.movies.delete_many({"title": data.title})
    await db.channel_posts.delete_many({"title": data.title})
    return {"ok": True}


# ==========================================
# 13. Main Frontend Mini App Interface (User UI)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def serve_mini_app():
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>MovieBox - Premium Stream Platform</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        <style>
            body { -webkit-tap-highlight-color: transparent; }
            .no-scrollbar::-webkit-scrollbar { display: none; }
            .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
            .active-tab { color: #ef4444 !important; }
        </style>
    </head>
    <body class="bg-gray-950 text-white font-sans antialiased selection:bg-red-500 selection:text-white overflow-x-hidden">
        
        <header class="bg-gray-900/95 backdrop-blur border-b border-gray-800 sticky top-0 z-40 px-4 py-3 flex items-center justify-center">
            <div class="hidden flex items-center gap-2">
                <div class="w-8 h-8 rounded-full bg-gradient-to-tr from-red-600 to-amber-500 flex items-center justify-center font-bold text-sm text-white border border-gray-700 shadow-inner" id="userAvatar">M</div>
                <span class="text-xs font-bold tracking-wide max-w-[80px] truncate text-gray-300" id="userName">User</span>
            </div>
            
            <div class="text-center">
                <span class="text-xl font-black tracking-tighter text-transparent bg-clip-text bg-gradient-to-r from-red-500 to-rose-400">MOVIEBOX</span>
            </div>
            
            <button class="hidden text-gray-400 hover:text-white transition active:scale-90">
                <i class="fa-solid fa-bars text-lg"></i>
            </button>
        </header>

        <main class="max-w-md mx-auto px-4 pt-4 pb-24 min-h-[85vh]">
            <div id="homeView" class="tab-view">
                <div class="relative mb-6 shadow-md rounded-xl overflow-hidden group">
                    <input type="text" id="searchInput" placeholder="Search movies, series, episodes..." class="w-full bg-gray-900 border border-gray-800 rounded-xl py-3.5 pl-11 pr-10 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-red-500/50 transition">
                    <i class="fa-solid fa-magnifying-glass absolute left-4 top-4 text-gray-500 text-sm group-focus-within:text-red-500 transition"></i>
                    <button onclick="clearSearch()" id="searchClearBtn" class="absolute right-3.5 top-3.5 hidden w-5 h-5 bg-gray-800 rounded-full text-xs text-gray-400 hover:text-white flex items-center justify-center">&times;</button>
                </div>

                <div id="upcomingSection" class="mb-8 hidden">
                    <div class="flex justify-between items-center mb-3">
                        <h2 class="text-sm font-black uppercase tracking-wider text-gray-400 flex items-center gap-1.5">
                            <i class="fa-solid fa-fire text-amber-500"></i> Upcoming Releases
                        </h2>
                    </div>
                    <div id="upcomingWrapper" class="flex gap-3 overflow-x-auto no-scrollbar pb-1"></div>
                </div>

                <div>
                    <h2 class="text-sm font-black uppercase tracking-wider text-gray-400 mb-4 flex items-center gap-1.5" id="listHeader">
                        <i class="fa-solid fa-clapperboard text-red-500"></i> Latest Uploads
                    </h2>
                    <div class="grid grid-cols-2 gap-4" id="moviesGrid">
                        <div class="bg-gray-900 rounded-xl h-60 animate-pulse border border-gray-800"></div>
                        <div class="bg-gray-900 rounded-xl h-60 animate-pulse border border-gray-800"></div>
                    </div>
                </div>
            </div>

            <div id="requestView" class="tab-view hidden">
                <div class="bg-gray-900 border border-gray-800 rounded-2xl p-5 shadow-xl mb-6">
                    <h2 class="text-xl font-black text-white mb-2 flex items-center gap-2"><i class="fa-solid fa-paper-plane text-blue-400"></i> Movie Request Hub</h2>
                    <p class="text-xs text-gray-400 mb-5 leading-relaxed">Can't find your desired content? Drop the exact title below. If others vote for it, admins will prioritize upload.</p>
                    
                    <div class="space-y-4">
                        <input type="text" id="reqInput" placeholder="Enter full Movie / Series title..." class="w-full bg-gray-950 border border-gray-800 rounded-xl p-3.5 text-sm outline-none focus:border-blue-500 transition">
                        <button onclick="submitRequest()" class="w-full bg-blue-600 hover:bg-blue-500 active:scale-[0.98] transition p-3.5 rounded-xl text-sm font-bold text-white shadow-lg shadow-blue-900/30">Submit Hub Request</button>
                    </div>
                </div>

                <h3 class="text-xs font-black uppercase tracking-wider text-gray-500 mb-3 flex items-center gap-1"><i class="fa-solid fa-list-check"></i> Community Wanted List</h3>
                <div class="space-y-3" id="requestContainer"></div>
            </div>

            <div id="earnView" class="tab-view hidden">
                <div class="bg-gradient-to-b from-gray-900 to-gray-950 border border-gray-800 rounded-2xl p-6 text-center shadow-xl mb-6 relative overflow-hidden">
                    <div class="absolute -right-6 -top-6 w-24 h-24 bg-yellow-500/10 rounded-full blur-xl"></div>
                    <i class="fa-solid fa-coins text-4xl text-yellow-500 mb-2 animate-bounce"></i>
                    <h2 class="text-xs font-bold uppercase text-gray-400 tracking-widest">Your Vault Balance</h2>
                    <p class="text-4xl font-black text-yellow-400 mt-1" id="vaultBalance">0 <span class="text-xs text-gray-500 font-medium">coins</span></p>
                    <p class="text-[10px] text-gray-500 mt-2">Earn 15 coins to download files without subscription restrictions.</p>
                </div>

                <div class="space-y-4">
                    <div class="bg-gray-900 border border-gray-800 p-4 rounded-xl flex items-center justify-between shadow-md">
                        <div class="flex items-center gap-3.5">
                            <div class="w-10 h-10 bg-amber-500/10 text-amber-500 rounded-lg flex items-center justify-center text-lg"><i class="fa-solid fa-calendar-day"></i></div>
                            <div>
                                <h4 class="text-sm font-bold">Daily Attendance</h4>
                                <p class="text-[11px] text-gray-400 mt-0.5">+2 Coins instant claim</p>
                            </div>
                        </div>
                        <button id="btnCheckin" onclick="doDailyCheckin()" class="bg-amber-600 text-white font-bold text-xs px-3.5 py-2 rounded-lg hover:bg-amber-500 active:scale-95 transition shadow-md shadow-amber-900/20">Claim</button>
                    </div>

                    <div class="bg-gray-900 border border-gray-800 p-4 rounded-xl flex items-center justify-between shadow-md">
                        <div class="flex items-center gap-3.5">
                            <div class="w-10 h-10 bg-red-500/10 text-red-500 rounded-lg flex items-center justify-center text-lg"><i class="fa-solid fa-rectangle-ad"></i></div>
                            <div>
                                <h4 class="text-sm font-bold">Watch Commercial Ads</h4>
                                <p class="text-[11px] text-gray-400 mt-0.5">Need 3 views (<span id="adProgress">0</span>/3)</p>
                            </div>
                        </div>
                        <button id="btnAd" onclick="watchRewardAd()" class="bg-red-600 text-white font-bold text-xs px-3.5 py-2 rounded-lg hover:bg-red-500 active:scale-95 transition shadow-md shadow-red-900/20">Watch</button>
                    </div>

                    <div class="bg-gray-900 border border-gray-800 p-4 rounded-xl flex items-center justify-between shadow-md">
                        <div class="flex items-center gap-3.5">
                            <div class="w-10 h-10 bg-purple-500/10 text-purple-500 rounded-lg flex items-center justify-center text-lg"><i class="fa-solid fa-star-half-stroke"></i></div>
                            <div>
                                <h4 class="text-sm font-bold">Review Movie App</h4>
                                <p class="text-[11px] text-gray-400 mt-0.5">Write 2 reviews (<span id="reviewProgress">0</span>/2)</p>
                            </div>
                        </div>
                        <button id="btnReview" onclick="switchTab('home')" class="bg-purple-600 text-white font-bold text-xs px-3.5 py-2 rounded-lg hover:bg-purple-500 active:scale-95 transition shadow-md shadow-purple-900/20">Go Write</button>
                    </div>
                </div>
            </div>

            <div id="premiumView" class="tab-view hidden">
                <div id="premiumActiveHeader" class="bg-gradient-to-r from-yellow-600 to-amber-500 p-5 rounded-2xl mb-6 shadow-xl hidden">
                    <div class="flex items-center gap-3">
                        <i class="fa-solid fa-crown text-3xl text-white drop-shadow"></i>
                        <div>
                            <h2 class="text-lg font-black text-white">VIP Membership Active</h2>
                            <p class="text-xs text-white/90 mt-0.5">Expires: <span id="vipExpireDate" class="font-bold">...</span></p>
                        </div>
                    </div>
                </div>

                <div class="bg-gray-900 border border-gray-800 rounded-2xl p-5 shadow-xl mb-6">
                    <h2 class="text-xl font-black text-transparent bg-clip-text bg-gradient-to-r from-amber-400 to-yellow-300 mb-1 flex items-center gap-2"><i class="fa-solid fa-crown"></i> Unlock Ultra VIP Tier</h2>
                    <p class="text-xs text-gray-400 leading-relaxed">No commercials, direct stream file forwarding, persistent chat access, and immune to message auto-delete cycles.</p>
                    
                    <div class="grid grid-cols-3 gap-2.5 mt-5">
                        <div onclick="selectPackage(30, 20)" class="pkg-card bg-gray-950 border-2 border-gray-800 rounded-xl p-3 text-center cursor-pointer transition relative" id="pkg30">
                            <span class="text-xs font-bold text-gray-400 block">1 Month</span>
                            <span class="text-lg font-black text-amber-400 block mt-1">৳20</span>
                        </div>
                        <div onclick="selectPackage(90, 50)" class="pkg-card bg-gray-950 border-2 border-gray-800 rounded-xl p-3 text-center cursor-pointer transition relative" id="pkg90">
                            <span class="absolute -top-2 left-1/2 -translate-x-1/2 bg-red-500 text-[8px] font-black uppercase px-1.5 py-0.5 rounded-full tracking-wide">Save</span>
                            <span class="text-xs font-bold text-gray-400 block">3 Month</span>
                            <span class="text-lg font-black text-amber-400 block mt-1">৳50</span>
                        </div>
                        <div onclick="selectPackage(180, 90)" class="pkg-card bg-gray-950 border-2 border-gray-800 rounded-xl p-3 text-center cursor-pointer transition relative" id="pkg180">
                            <span class="text-xs font-bold text-gray-400 block">6 Month</span>
                            <span class="text-lg font-black text-amber-400 block mt-1">৳90</span>
                        </div>
                    </div>

                    <div id="paymentBox" class="mt-6 border-t border-gray-800 pt-5 hidden">
                        <p class="text-xs text-gray-400 mb-3">Send exact amount <b class="text-white text-sm" id="payAmountLabel">৳0</b> to any provider number using <b>Send Money</b> option:</p>
                        <div class="space-y-2 text-xs">
                            <div class="bg-gray-950 p-3 rounded-xl flex items-center justify-between border border-gray-850"><span class="font-bold text-pink-500"><i class="fa-solid fa-wallet"></i> bKash:</span> <code class="text-gray-200 font-mono select-all font-bold" id="bkashNo">...</code></div>
                            <div class="bg-gray-950 p-3 rounded-xl flex items-center justify-between border border-gray-850"><span class="font-bold text-orange-400"><i class="fa-solid fa-wallet"></i> Nagad:</span> <code class="text-gray-200 font-mono select-all font-bold" id="nagadNo">...</code></div>
                        </div>
                        <div class="mt-4 space-y-3">
                            <label class="block text-[11px] font-bold text-gray-400 uppercase tracking-wider">Provide Payment Reference TrxID:</label>
                            <input type="text" id="trxInput" placeholder="Enter the 10-digit Transaction ID" class="w-full bg-gray-950 border border-gray-800 rounded-xl p-3 text-sm font-mono text-center outline-none focus:border-amber-500 transition uppercase tracking-wide">
                            <button onclick="submitPayment()" class="w-full bg-gradient-to-r from-yellow-600 to-amber-500 hover:from-yellow-500 hover:to-amber-400 active:scale-[0.98] transition p-3.5 rounded-xl text-sm font-black text-white shadow-lg shadow-amber-900/20">Submit Verification TrxID</button>
                        </div>
                    </div>
                </div>

                <div class="bg-gray-900 border border-gray-800 rounded-2xl p-5 shadow-xl">
                    <h3 class="text-sm font-black uppercase tracking-wider text-gray-400 mb-3 flex items-center gap-1.5"><i class="fa-solid fa-user-plus text-green-400"></i> Free Refer & Earn System</h3>
                    <p class="text-xs text-gray-400 mb-4 leading-relaxed">Invite friends via link. Every 5 successfully validated joins awards you with <b>24 Hours VIP Tier</b> status dynamically.</p>
                    <div class="bg-gray-950 border border-gray-800 rounded-xl p-3 flex items-center justify-between mb-3"><code class="text-[11px] text-gray-400 font-mono truncate mr-2" id="referLink">Generating link...</code><button onclick="copyReferLink()" class="bg-gray-800 hover:bg-gray-700 text-white font-bold text-xs px-3 py-1.5 rounded-md transition whitespace-nowrap"><i class="fa-solid fa-copy"></i> Copy</button></div>
                    <div class="text-center text-xs text-gray-500 font-medium">Total Successful Referrals: <span class="text-green-400 font-bold" id="referCountLabel">0</span></div>
                </div>
            </div>
        </main>

        <nav class="fixed bottom-0 left-0 right-0 bg-gray-900/95 backdrop-blur border-t border-gray-800 py-2.5 z-40 shadow-2xl">
            <div class="max-w-md mx-auto flex justify-around items-center text-gray-500">
                <button onclick="switchTab('home')" id="tabHome" class="flex flex-col items-center gap-1 active-tab transition"><i class="fa-solid fa-house text-lg"></i><span class="text-[10px] font-bold tracking-wide">Home</span></button>
                <button onclick="switchTab('request')" id="tabRequest" class="flex flex-col items-center gap-1 transition"><i class="fa-solid fa-code-pull-request text-lg"></i><span class="text-[10px] font-bold tracking-wide">Request</span></button>
                <button onclick="switchTab('earn')" id="tabEarn" class="flex flex-col items-center gap-1 transition"><i class="fa-solid fa-coins text-lg"></i><span class="text-[10px] font-bold tracking-wide">Earn</span></button>
                <button onclick="switchTab('premium')" id="tabPremium" class="flex flex-col items-center gap-1 transition relative"><div class="absolute -top-1 right-2 w-2 h-2 bg-red-500 rounded-full"></div><i class="fa-solid fa-crown text-lg"></i><span class="text-[10px] font-bold tracking-wide">Premium</span></button>
            </div>
        </nav>

        <div id="movieModal" class="fixed inset-0 bg-black/90 backdrop-blur-sm hidden z-50 overflow-y-auto no-scrollbar">
            <div class="min-h-screen flex flex-col justify-end max-w-md mx-auto relative">
                <button onclick="closeMovieModal()" class="absolute top-4 right-4 w-9 h-9 bg-gray-900/80 hover:bg-red-600 rounded-full text-white text-xl flex items-center justify-center border border-gray-800 transition shadow-lg z-50">&times;</button>
                
                <div class="bg-gray-900 border-t border-gray-800 rounded-t-3xl overflow-hidden shadow-2xl pb-10">
                    <div class="w-full h-56 relative overflow-hidden bg-gray-950">
                        <img id="modalPoster" src="" alt="Movie Poster" class="w-full h-full object-cover">
                        <div class="absolute inset-0 bg-gradient-to-t from-gray-900 via-transparent to-black/40"></div>
                        <div class="absolute bottom-4 left-4 right-4"><h3 class="text-xl font-black text-white drop-shadow-md" id="modalTitle">...</h3></div>
                    </div>

                    <div class="p-5">
                        <div class="flex gap-2 mb-6 overflow-x-auto no-scrollbar" id="modalFilesBox"></div>

                        <div class="border-t border-gray-850 pt-5 mt-4">
                            <h4 class="text-xs font-black uppercase tracking-wider text-gray-400 mb-3 flex items-center gap-1"><i class="fa-solid fa-comments text-purple-400"></i> Reviews / Chat</h4>
                            <div class="space-y-3 max-h-40 overflow-y-auto no-scrollbar mb-4 bg-gray-950/50 p-2.5 rounded-xl border border-gray-850" id="reviewsBox"></div>
                            
                            <div class="flex gap-2">
                                <input type="text" id="reviewInput" placeholder="Type a review or message..." class="flex-1 bg-gray-950 border border-gray-850 rounded-xl px-3.5 py-2.5 text-xs outline-none focus:border-purple-500 transition">
                                <button onclick="postReview()" class="bg-purple-600 hover:bg-purple-500 text-white px-4 rounded-xl text-xs font-bold transition active:scale-95"><i class="fa-solid fa-paper-plane"></i></button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <script>
            let tg = window.Telegram.WebApp;
            tg.expand();
            tg.ready();

            let initData = tg.initData || "";
            let userObj = tg.initDataUnsafe?.user || { id: 12345, first_name: "Developer" };

            // Initialize Header attributes
            document.getElementById('userName').innerText = userObj.first_name;
            if(userObj.first_name) {
                document.getElementById('userAvatar').innerText = userObj.first_name.charAt(0).toUpperCase();
            }

            let appState = { uid: userObj.id, first_name: userObj.first_name, coins: 0, is_vip: false, vip_until: null, refer_count: 0, current_movie: null };
            let activePackage = { days: 0, amount: 0 };

            async function req(route, body={}) {
                try {
                    let r = await fetch(route, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json', 'X-TG-Data': initData},
                        body: JSON.stringify({uid: appState.uid, first_name: appState.first_name, ...body})
                    });
                    return await r.json();
                } catch(e) { return {ok:false, msg:"Network execution failed"}; }
            }

            function switchTab(tab) {
                document.querySelectorAll('.tab-view').forEach(v => v.classList.add('hidden'));
                document.querySelectorAll('nav button').forEach(b => b.classList.remove('active-tab'));
                
                if(tab === 'home') { document.getElementById('homeView').classList.remove('hidden'); document.getElementById('tabHome').classList.add('active-tab'); }
                if(tab === 'request') { document.getElementById('requestView').classList.remove('hidden'); document.getElementById('tabRequest').classList.add('active-tab'); loadRequests(); }
                if(tab === 'earn') { document.getElementById('earnView').classList.remove('hidden'); document.getElementById('tabEarn').classList.add('active-tab'); }
                if(tab === 'premium') { document.getElementById('premiumView').classList.remove('hidden'); document.getElementById('tabPremium').classList.add('active-tab'); loadPremiumInfo(); }
            }

            async function syncUser() {
                let res = await req('/api/user/sync');
                if(res.ok) {
                    appState.coins = res.user.coins;
                    appState.is_vip = res.user.is_vip;
                    appState.vip_until = res.user.vip_until;
                    appState.refer_count = res.user.refer_count;
                    
                    document.getElementById('vaultBalance').innerText = appState.coins;
                    document.getElementById('adProgress').innerText = res.tasks.ads || 0;
                    document.getElementById('reviewProgress').innerText = res.tasks.reviews || 0;
                    
                    if(appState.is_vip) {
                        document.getElementById('premiumActiveHeader').classList.remove('hidden');
                        document.getElementById('vipExpireDate').innerText = new Date(appState.vip_until).toLocaleDateString();
                    } else {
                        document.getElementById('premiumActiveHeader').classList.add('hidden');
                    }
                }
            }

            async function loadCatalog() {
                let res = await req('/api/movies/catalog');
                if(res.ok) {
                    // Load Upcoming Slider
                    if(res.upcoming && res.upcoming.length > 0) {
                        document.getElementById('upcomingSection').classList.remove('hidden');
                        let upcHtml = '';
                        res.upcoming.forEach(u => {
                            upcHtml += `<div class="min-w-[100px] max-w-[100px] flex flex-col gap-1 text-center">
                                <div class="w-full h-28 bg-gray-900 border border-gray-850 rounded-xl overflow-hidden shadow-md">
                                    <img src="/api/file/proxy?file_id=${u.photo_id}" class="w-full h-full object-cover">
                                </div>
                                <span class="text-[10px] font-bold truncate text-gray-300 px-0.5">${u.title}</span>
                            </div>`;
                        });
                        document.getElementById('upcomingWrapper').innerHTML = upcHtml;
                    }

                    // Load Latest Upload Grid
                    renderMovies(res.movies);
                }
            }

            function renderMovies(arr) {
                let html = '';
                if(arr.length === 0) {
                    html = '<div class="col-span-2 text-center py-10 text-gray-500 text-xs font-bold">No contents found matching title.</div>';
                } else {
                    arr.forEach(m => {
                        html += `<div onclick="openMovieDetail('${btoa(unescape(encodeURIComponent(m.title)))}')" class="bg-gray-900 border border-gray-850 rounded-2xl overflow-hidden shadow-lg active:scale-[0.98] transition flex flex-col group">
                            <div class="w-full h-44 bg-gray-950 relative overflow-hidden">
                                <img src="/api/file/proxy?file_id=${m.photo_id}" class="w-full h-full object-cover group-hover:scale-105 transition duration-300" loading="lazy">
                                <div class="absolute top-2 left-2 bg-black/70 backdrop-blur-sm text-[9px] font-black tracking-wide text-red-400 px-2 py-0.5 rounded-full border border-gray-800">${m.files_count} Files</div>
                            </div>
                            <div class="p-3 flex-1 flex flex-col justify-between gap-1">
                                <h3 class="text-xs font-bold text-gray-200 line-clamp-2 leading-relaxed">${m.title}</h3>
                                <div class="flex items-center justify-between mt-1 text-[10px] font-bold text-gray-500">
                                    <span><i class="fa-solid fa-eye text-gray-600"></i> ${formatViews(m.total_clicks)}</span>
                                </div>
                            </div>
                        </div>`;
                    });
                }
                document.getElementById('moviesGrid').innerHTML = html;
            }

            function formatViews(n) {
                if (n >= 1000000) return (n/1000000).toFixed(1).replace('.0','')+'M';
                if (n >= 1000) return (n/1000).toFixed(1).replace('.0','')+'K';
                return n;
            }

            // Real-time local filtering search handling
            document.getElementById('searchInput').addEventListener('input', async function(e) {
                let query = e.target.value.trim();
                if(query.length > 0) {
                    document.getElementById('searchClearBtn').classList.remove('hidden');
                    document.getElementById('listHeader').innerHTML = `<i class="fa-solid fa-square-poll-vertical text-red-500"></i> Search Results`;
                } else {
                    document.getElementById('searchClearBtn').classList.add('hidden');
                    document.getElementById('listHeader').innerHTML = `<i class="fa-solid fa-clapperboard text-red-500"></i> Latest Uploads`;
                }
                let res = await req('/api/movies/search', {query});
                if(res.ok) renderMovies(res.results);
            });

            function clearSearch() {
                document.getElementById('searchInput').value = '';
                document.getElementById('searchClearBtn').classList.add('hidden');
                document.getElementById('listHeader').innerHTML = `<i class="fa-solid fa-clapperboard text-red-500"></i> Latest Uploads`;
                loadCatalog();
            }

            async function openMovieDetail(encodedTitle) {
                let title = decodeURIComponent(escape(atob(encodedTitle)));
                let res = await req('/api/movies/details', {title});
                if(res.ok) {
                    appState.current_movie = title;
                    document.getElementById('modalTitle').innerText = title;
                    document.getElementById('modalPoster').src = `/api/file/proxy?file_id=${res.photo_id}`;
                    
                    let filesHtml = '';
                    res.files.forEach(f => {
                        filesHtml += `<button onclick="triggerFileAction('${f._id}')" class="bg-gray-950 border border-gray-800 hover:border-red-500/50 p-3 rounded-xl flex items-center justify-between min-w-[140px] text-left transition active:scale-95 shadow-md">
                            <div>
                                <span class="text-xs font-black text-gray-200 block">${f.quality}</span>
                                <span class="text-[9px] font-bold text-gray-500 block mt-0.5"><i class="fa-solid fa-eye"></i> ${formatViews(f.clicks || 0)}</span>
                            </div>
                            <i class="fa-solid fa-circle-arrow-down text-red-500 text-lg"></i>
                        </button>`;
                    });
                    document.getElementById('modalFilesBox').innerHTML = filesHtml;
                    renderReviews(res.reviews);
                    document.getElementById('movieModal').classList.remove('hidden');
                }
            }

            function closeMovieModal() {
                document.getElementById('movieModal').classList.add('hidden');
                appState.current_movie = null;
                loadCatalog();
            }

            function renderReviews(arr) {
                let html = '';
                if(!arr || arr.length === 0) {
                    html = '<div class="text-center py-4 text-[10px] text-gray-600 font-bold">No commentary yet. Leave yours below.</div>';
                } else {
                    arr.forEach(r => {
                        html += `<div class="bg-gray-900 border border-gray-850 p-2.5 rounded-lg text-xs leading-relaxed">
                            <div class="flex justify-between font-bold text-[10px] text-purple-400 mb-0.5"><span>${r.username}</span> <span class="text-gray-600">${new Date(r.timestamp).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}</span></div>
                            <p class="text-gray-200 font-medium">${r.comment}</p>
                        </div>`;
                    });
                }
                document.getElementById('reviewsBox').innerHTML = html;
                let b = document.getElementById('reviewsBox');
                b.scrollTop = b.scrollHeight;
            }

            async function postReview() {
                let inp = document.getElementById('reviewInput');
                let comment = inp.value.trim();
                if(!comment || !appState.current_movie) return;
                inp.value = '';
                let res = await req('/api/movies/add-review', {movie_title: appState.current_movie, comment});
                if(res.ok) { renderReviews(res.reviews); syncUser(); }
            }

            async function triggerFileAction(fileId) {
                let res = await req('/api/movies/claim-file', {file_id: fileId});
                if(res.ok) {
                    tg.close();
                } else {
                    alert(res.msg || "Action failed");
                    if(res.redirect === 'earn') switchTab('earn');
                }
            }

            // Tasks Subsystem
            async function doDailyCheckin() {
                let res = await req('/api/tasks/checkin');
                alert(res.msg);
                if(res.ok) syncUser();
            }

            function watchRewardAd() {
                if(typeof AdController !== 'undefined') {
                    AdController.show({
                        zoneId: 8843, 
                        onStateChange: async function(state) {
                            if(state === 'completed') {
                                let res = await req('/api/tasks/ad-watched');
                                if(res.ok) { syncUser(); alert("Ad reward accounted successfully!"); }
                            }
                        }
                    });
                } else {
                    // Ad fallback mockup mechanism for standalone testing pipeline environments
                    setTimeout(async () => {
                        let res = await req('/api/tasks/ad-watched');
                        if(res.ok) { syncUser(); alert("Commercial simulation completed! (+1 claim incremented)"); }
                    }, 1200);
                }
            }

            // Requests subsystem 
            async function loadRequests() {
                let res = await req('/api/requests/list');
                if(res.ok) {
                    let html = '';
                    if(res.requests.length === 0) {
                        html = '<div class="text-center py-8 text-xs text-gray-500 font-bold">No active requests found on database.</div>';
                    } else {
                        res.requests.forEach(r => {
                            let hasVoted = r.voters.includes(appState.uid);
                            html += `<div class="bg-gray-900 border border-gray-850 p-4 rounded-xl flex items-center justify-between shadow-md">
                                <div class="max-w-[70%]">
                                    <h4 class="text-xs font-black text-gray-200 truncate">${r.movie}</h4>
                                    <span class="text-[10px] font-bold text-gray-500 mt-1 block"><i class="fa-solid fa-thumbs-up text-blue-500/70"></i> ${r.voters.length} requests supported</span>
                                </div>
                                <button onclick="voteRequest('${r._id}')" class="${hasVoted ? 'bg-gray-800 text-gray-500 border border-gray-700 cursor-not-allowed' : 'bg-blue-600/90 text-white hover:bg-blue-500'} font-bold text-[10px] tracking-wide px-3 py-2 rounded-lg transition" ${hasVoted ? 'disabled' : ''}>${hasVoted ? 'Supported' : '<i class="fa-solid fa-plus"></i> Support'}</button>
                            </div>`;
                        });
                    }
                    document.getElementById('requestContainer').innerHTML = html;
                }
            }

            async function submitRequest() {
                let inp = document.getElementById('reqInput');
                let title = inp.value.trim();
                if(!title) return;
                inp.value = '';
                let res = await req('/api/requests/create', {movie: title});
                alert(res.msg);
                if(res.ok) loadRequests();
            }

            async function voteRequest(reqId) {
                let res = await req('/api/requests/vote', {request_id: reqId});
                if(res.ok) loadRequests();
            }

            // Premium system handling
            async function loadPremiumInfo() {
                let res = await req('/api/premium/gateways');
                if(res.ok) {
                    document.getElementById('bkashNo').innerText = res.bkash || "Not Set";
                    document.getElementById('nagadNo').innerText = res.nagad || "Not Set";
                    document.getElementById('referLink').innerText = `https://t.me/${BOT_USERNAME}?start=ref_${appState.uid}`;
                    document.getElementById('referCountLabel').innerText = appState.refer_count;
                }
            }

            function selectPackage(days, amt) {
                activePackage = { days, amount: amt };
                document.querySelectorAll('.pkg-card').forEach(c => c.classList.remove('border-amber-500', 'bg-amber-950/20'));
                document.getElementById(`pkg${days}`).classList.add('border-amber-500', 'bg-amber-950/20');
                document.getElementById('payAmountLabel').innerText = `৳${amt}`;
                document.getElementById('paymentBox').classList.remove('hidden');
            }

            async function submitPayment() {
                let trx = document.getElementById('trxInput').value.trim();
                if(!trx || activePackage.days === 0) return alert("Fill out the Transaction Identification field parameter.");
                let res = await req('/api/premium/pay-submit', {trx_id: trx, days: activePackage.days, amount: activePackage.amount});
                alert(res.msg);
                if(res.ok) {
                    document.getElementById('trxInput').value = '';
                    document.getElementById('paymentBox').classList.add('hidden');
                }
            }

            function copyReferLink() {
                let link = document.getElementById('referLink').innerText;
                navigator.clipboard.writeText(link);
                alert("Referral URL link successfully generated and written to text clipboard buffer!");
            }

            // Primary App Lifecycle initial boot sequencing triggers
            switchTab('home');
            syncUser();
            loadCatalog();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


# ==========================================
# 14. API Router Endpoints Backend Logic
# ==========================================
class UserBaseModel(BaseModel):
    uid: int
    first_name: str

@app.post("/api/user/sync")
async def api_sync_user(data: UserBaseModel, request: Request):
    if not validate_tg_data(request.headers.get("X-TG-Data", "")):
        return {"ok": False, "msg": "Telegram Integrity Authentication Broken"}
        
    now = datetime.datetime.utcnow()
    user = await db.users.find_one({"user_id": data.uid})
    if not user:
        await db.users.insert_one({
            "user_id": data.uid, "first_name": data.first_name, "joined_at": now,
            "refer_count": 0, "coins": 0, "last_checkin": now - datetime.timedelta(days=2),
            "vip_until": now - datetime.timedelta(days=1)
        })
        user = await db.users.find_one({"user_id": data.uid})
        
    tasks = await db.tasks.find_one({"user_id": data.uid})
    if tasks and tasks.get("date") != now.strftime("%Y-%m-%d"):
        await db.tasks.delete_one({"user_id": data.uid})
        tasks = None
        
    is_vip = user.get("vip_until", now) > now
    return {
        "ok": True,
        "user": {
            "coins": user.get("coins", 0),
            "is_vip": is_vip,
            "vip_until": user.get("vip_until", now).isoformat(),
            "refer_count": user.get("refer_count", 0)
        },
        "tasks": tasks or {"ads": 0, "reviews": 0}
    }

@app.post("/api/movies/catalog")
async def api_catalog(data: UserBaseModel):
    movies_cursor = db.movies.find().sort("created_at", -1)
    movie_dict = {}
    async for m in movies_cursor:
        t = m["title"]
        if t not in movie_dict:
            movie_dict[t] = {"title": t, "photo_id": m["photo_id"], "total_clicks": 0, "files_count": 0}
        movie_dict[t]["total_clicks"] += m.get("clicks", 0)
        movie_dict[t]["files_count"] += 1
        
    upcoming_cursor = db.upcoming.find().sort("added_at", -1).limit(10)
    upcoming_list = [{"photo_id": u["photo_id"], "title": u["title"]} for u in await upcoming_cursor.to_list(10)]
    
    return {"ok": True, "movies": list(movie_dict.values()), "upcoming": upcoming_list}

class QuerySchema(BaseModel):
    uid: int
    first_name: str
    query: str

@app.post("/api/movies/search")
async def api_search_movies(data: QuerySchema):
    q = data.query.strip()
    if not q:
        movies_cursor = db.movies.find().sort("created_at", -1)
    else:
        movies_cursor = db.movies.find({"$text": {"$search": q}})
        
    movie_dict = {}
    async for m in movies_cursor:
        t = m["title"]
        if t not in movie_dict:
            movie_dict[t] = {"title": t, "photo_id": m["photo_id"], "total_clicks": 0, "files_count": 0}
        movie_dict[t]["total_clicks"] += m.get("clicks", 0)
        movie_dict[t]["files_count"] += 1
        
    return {"ok": True, "results": list(movie_dict.values())}

class TitleSchema(BaseModel):
    uid: int
    first_name: str
    title: str

@app.post("/api/movies/details")
async def api_movie_details(data: TitleSchema):
    files_cursor = db.movies.find({"title": data.title}).sort("quality", 1)
    files = []
    photo_id = None
    async for f in files_cursor:
        photo_id = f["photo_id"]
        files.append({"_id": str(f["_id"]), "quality": f["quality"], "clicks": f.get("clicks", 0)})
        
    rev_cursor = db.reviews.find({"movie_title": data.title}).sort("timestamp", -1).limit(20)
    reviews = []
    async for r in rev_cursor:
        reviews.append({"username": r["username"], "comment": r["comment"], "timestamp": r["timestamp"].isoformat()})
    reviews.reverse()
    
    return {"ok": True, "photo_id": photo_id, "files": files, "reviews": reviews}

class FileActionSchema(BaseModel):
    uid: int
    first_name: str
    file_id: str

@app.post("/api/movies/claim-file")
async def api_claim_file(data: FileActionSchema, request: Request):
    if not validate_tg_data(request.headers.get("X-TG-Data", "")):
        return {"ok": False, "msg": "Verification Refused"}
        
    now = datetime.datetime.utcnow()
    user = await db.users.find_one({"user_id": data.uid})
    if not user: return {"ok": False, "msg": "User context null"}
    
    is_vip = user.get("vip_until", now) > now
    
    if not is_vip and user.get("coins", 0) < 15:
        return {"ok": False, "redirect": "earn", "msg": "VIP status or minimum 15 task coins required to access resource files."}
        
    movie_file = await db.movies.find_one({"_id": ObjectId(data.file_id)})
    if not movie_file: return {"ok": False, "msg": "File catalog missing"}
    
    await db.movies.update_one({"_id": ObjectId(data.file_id)}, {"$inc": {"clicks": 1}})
    
    try:
        cfg = await db.settings.find_one({"id": "protect_content"})
        protect = cfg.get("status", False) if cfg else False
        
        sent = await bot.copy_message(
            chat_id=data.uid,
            from_chat_id=CHANNEL_ID if (CHANNEL_ID and CHANNEL_ID != "-100XXXXXXXXXX") else data.uid,
            message_id=int(movie_file["file_id"]) if movie_file["file_id"].isdigit() else 0, 
            protect_content=protect
        )
        
        if not is_vip:
            t_cfg = await db.settings.find_one({"id": "del_time"})
            mins = t_cfg.get("minutes", 10) if t_cfg else 10
            del_at = now + datetime.timedelta(minutes=mins)
            await db.auto_delete.insert_one({"chat_id": data.uid, "message_id": sent.message_id, "delete_at": del_at})
            await db.users.update_one({"user_id": data.uid}, {"$inc": {"coins": -15}})
            
            await bot.send_message(data.uid, f"⚠️ <b>ফাইলটি অটো-ডিলিট হওয়ার পূর্বে সেভ করুন!</b>\n\nআপনি VIP মেম্বার নন, তাই নীতি অনুযায়ী ফাইলটি আগামী <b>{mins} মিনিট</b> পর স্বয়ংক্রিয়ভাবে মুছে যাবে।", parse_mode="HTML")
            
        return {"ok": True}
    except Exception as e:
        # Fallback forward dynamic direct fallback block logic link injection
        try:
            bot_info = await bot.get_me()
            link_obj = await db.settings.find_one({"id": "direct_links"})
            links = link_obj.get("links", []) if link_obj else []
            direct_url = links[0] if links else f"https://t.me/{bot_info.username}"
            
            await bot.send_message(data.uid, f"📥 <b>আপনার কাঙ্ক্ষিত মুভি ফাইলটি প্রস্তুত!</b>\n\nনিচের লিংকে ক্লিক করে সরাসরি ফাইলটি ডাউনলোড করে নিন:\n🔗 {direct_url}", disable_web_page_preview=True)
            
            if not is_vip:
                await db.users.update_one({"user_id": data.uid}, {"$inc": {"coins": -15}})
            return {"ok": True}
        except:
            return {"ok": False, "msg": "বট ইউজারকে সরাসরি বার্তা পাঠাতে অক্ষম। অনুগ্রহ করে বটটি রিস্টার্ট করুন।"}

class ReviewPostSchema(BaseModel):
    uid: int
    first_name: str
    movie_title: str
    comment: str

@app.post("/api/movies/add-review")
async def api_add_review(data: ReviewPostSchema, request: Request):
    if not validate_tg_data(request.headers.get("X-TG-Data", "")): return {"ok": False}
    c_str = data.comment.strip()
    if not c_str: return {"ok": False}
    
    now = datetime.datetime.utcnow()
    await db.reviews.insert_one({
        "user_id": data.uid, "username": data.first_name, "movie_title": data.movie_title,
        "comment": c_str, "timestamp": now
    })
    
    today = now.strftime("%Y-%m-%d")
    await db.tasks.update_one(
        {"user_id": data.uid, "date": today},
        {"$inc": {"reviews": 1}},
        upsert=True
    )
    
    return await api_movie_details(TitleSchema(uid=data.uid, first_name=data.first_name, title=data.movie_title))

@app.post("/api/requests/list")
async def api_req_list(data: UserBaseModel):
    cursor = db.requests.find().sort("voters", -1).limit(30)
    reqs = []
    async for r in cursor:
        reqs.append({"_id": str(r["_id"]), "movie": r["movie"], "voters": r.get("voters", [])})
    return {"ok": True, "requests": reqs}

class ReqCreateSchema(BaseModel):
    uid: int
    first_name: str
    movie: str

@app.post("/api/requests/create")
async def api_req_create(data: ReqCreateSchema):
    m_name = data.movie.strip()
    if not m_name: return {"ok": False, "msg": "Null title schema matching disallowed"}
    
    exists = await db.requests.find_one({"movie": {"$regex": f"^{m_name}$", "$options": "i"}})
    if exists:
        return {"ok": False, "msg": "This entry matches a listing already active on the hub pool dashboard index."}
        
    await db.requests.insert_one({"movie": m_name, "voters": [data.uid]})
    
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Approve", callback_data=f"req_acc_{str(ObjectId())}") # Real fallback handling uses explicit mappings
        builder.button(text="❌ Reject", callback_data=f"req_rej_{str(ObjectId())}")
        # Simplified dynamic reference update for routing approval workflows:
        new_req = await db.requests.find_one({"movie": m_name})
        if new_req:
            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Approve", callback_data=f"req_acc_{str(new_req['_id'])}")
            builder.button(text="❌ Reject", callback_data=f"req_rej_{str(new_req['_id'])}")
            await bot.send_message(OWNER_ID, f"🍿 <b>New Movie Requested Hub</b>:\n\nTitle: <code>{m_name}</code>\nUser: {data.first_name} ({data.uid})", parse_mode="HTML", reply_markup=builder.as_markup())
    except: pass
    
    return {"ok": True, "msg": "Request logged onto hub registry tracker successfully."}

class ReqVoteSchema(BaseModel):
    uid: int
    first_name: str
    request_id: str

@app.post("/api/requests/vote")
async def api_req_vote(data: ReqVoteSchema):
    await db.requests.update_one({"_id": ObjectId(data.request_id)}, {"$addToSet": {"voters": data.uid}})
    return {"ok": True}

@app.get("/api/file/proxy")
async def file_proxy(file_id: str):
    try:
        f_info = await bot.get_file(file_id)
        f_url = f"https://api.telegram.org/file/bot{TOKEN}/{f_info.file_path}"
        async def stream_file():
            async with aiohttp.ClientSession() as session:
                async with session.get(f_url) as resp:
                    async for chunk in resp.content.iter_chunked(4096):
                        yield chunk
        return StreamingResponse(stream_file(), media_type="image/jpeg")
    except Exception:
        # Default banner asset dynamic image binary injection
        return HTMLResponse(status_code=404)

@app.post("/api/premium/gateways")
async def api_premium_gateways(data: UserBaseModel):
    b = await db.settings.find_one({"id": "bkash_no"})
    n = await db.settings.find_one({"id": "nagad_no"})
    return {
        "ok": True, 
        "bkash": b.get("number", "017XXXXXXXX") if b else "017XXXXXXXX", 
        "nagad": n.get("number", "017XXXXXXXX") if n else "017XXXXXXXX"
    }

class PaymentSchema(BaseModel):
    uid: int
    first_name: str
    trx_id: str
    days: int
    amount: int

@app.post("/api/premium/pay-submit")
async def api_premium_submit(data: PaymentSchema, request: Request):
    if not validate_tg_data(request.headers.get("X-TG-Data", "")): return {"ok": False}
    trx = data.trx_id.strip().upper()
    if not trx: return {"ok": False, "msg": "Transaction key field missing"}
    
    dup = await db.payments.find_one({"trx_id": trx})
    if dup: return {"ok": False, "msg": "This transaction reference string ID matches a record already locked under review status verification."}
    
    res = await db.payments.insert_one({
        "user_id": data.uid, "trx_id": trx, "days": data.days, "amount": data.amount,
        "status": "pending", "timestamp": datetime.datetime.utcnow()
    })
    
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Approve", callback_data=f"trx_approve_{str(res.inserted_id)}")
        builder.button(text="❌ Reject", callback_data=f"trx_reject_{str(res.inserted_id)}")
        
        await bot.send_message(
            OWNER_ID, 
            f"💰 <b>VIP Subscription Verification Pending</b>:\n\nUser: {data.first_name} (<code>{data.uid}</code>)\nAmount: ৳{data.amount}\nPackage: {data.days} Days\nTrxID: <code>{trx}</code>",
            parse_mode="HTML", reply_markup=builder.as_markup()
        )
    except: pass
    
    return {"ok": True, "msg": "Transaction details logged under validation inspection queues successfully."}

@app.post("/api/tasks/checkin")
async def api_task_checkin(data: UserBaseModel):
    now = datetime.datetime.utcnow()
    user = await db.users.find_one({"user_id": data.uid})
    if not user: return {"ok": False, "msg": "Invalid User"}
    
    last = user.get("last_checkin", now - datetime.timedelta(days=2))
    if now - last < datetime.timedelta(days=1):
        return {"ok": False, "msg": "Daily reward checkin already locked for today cycle status."}
        
    await db.users.update_one({"user_id": data.uid}, {"$inc": {"coins": 2}, "$set": {"last_checkin": now}})
    return {"ok": True, "msg": "Daily login checkin success logged! (+2 coins added)"}

@app.post("/api/tasks/ad-watched")
async def api_ad_watched(data: UserBaseModel):
    now = datetime.datetime.utcnow()
    today = now.strftime("%Y-%m-%d")
    
    await db.tasks.update_one(
        {"user_id": data.uid, "date": today},
        {"$inc": {"ads": 1}},
        upsert=True
    )
    
    tasks = await db.tasks.find_one({"user_id": data.uid, "date": today})
    ad_limit_cfg = await db.settings.find_one({"id": "ad_count"})
    req_ads = ad_limit_cfg.get("count", 3) if ad_limit_cfg else 3
    
    if tasks.get("ads", 0) >= req_ads and not tasks.get("ads_claimed"):
        await db.tasks.update_one({"user_id": data.uid, "date": today}, {"$set": {"ads_claimed": True}})
        await db.users.update_one({"user_id": data.uid}, {"$inc": {"coins": 15}})
        return {"ok": True, "msg": "Milestone payout validated! Vault credited with 15 coins."}
        
    return {"ok": True}

class ClaimMissionSchema(BaseModel):
    uid: int
    task_type: str

@app.post("/api/tasks/claim-mission")
async def api_claim_mission(data: ClaimMissionSchema):
    now = datetime.datetime.utcnow()
    now_date = now.strftime("%Y-%m-%d")
    tasks = await db.tasks.find_one({"user_id": data.uid, "date": now_date})
    if not tasks:
        return {"ok": False, "msg": "মিশন সম্পূর্ণ হয়নি!"}
        
    if data.task_type == "ads" and tasks.get("ads", 0) >= 3 and not tasks.get("ads_claimed"):
        await db.users.update_one({"user_id": data.uid}, {"$set": {"tasks.ads_claimed": True}, "$inc": {"coins": 15}})
        return {"ok": True}
        
    if data.task_type == "reviews" and tasks.get("reviews", 0) >= 2 and not tasks.get("reviews_claimed"):
        await db.users.update_one({"user_id": data.uid}, {"$set": {"tasks.reviews_claimed": True}, "$inc": {"coins": 10}})
        return {"ok": True}
        
    return {"ok": False, "msg": "ইতিমধ্যে ক্লেইম করা হয়েছে বা মিশন সম্পূর্ণ হয়নি!"}


# ==========================================
# 15. Main Application Startup
# ==========================================
async def start():
    print("Initializing Database & Cache...")
    await init_db()
    await load_admins()
    await load_banned_users()
    
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    
    print("Starting Background Workers...")
    asyncio.create_task(auto_delete_worker())
    
    print("Connecting to Telegram Bot...")
    asyncio.create_task(dp.start_polling(bot))
    
    print(f"Serving web layout architecture engine module on port {port}...")
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(start())
    except (KeyboardInterrupt, SystemExit):
        print("Bot and Web Service stopped.")
