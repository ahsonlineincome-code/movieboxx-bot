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

        <!-- Custom Admin Edit Modal -->
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
                if (num >= 1000000) return (num / 1000000).toFixed(1).replace(/\\.0$/, '') + 'M';
                if (num >= 1000) return (num / 1000).toFixed(1).replace(/\\.0$/, '') + 'K';
                return num.toString();
            }

            async function loadAdminData() {
                try {
                    const res = await fetch('/api/admin/data');
                    const data = await res.json();
                    document.getElementById('statUsers').innerText = data.total_users;
                    document.getElementById('statMovies').innerText = data.total_groups;
                    document.getElementById('statNew').innerText = data.new_users_today;
                    let html = '';
                    data.movies.forEach(m => {
                        html += `<tr class="border-b border-gray-700 hover:bg-gray-750 transition">
                            <td class="p-4 font-medium text-base">` + m._id + `</td>
                            <td class="p-4 text-gray-400 font-bold"><i class="fa-solid fa-eye text-gray-500"></i> ` + formatViews(m.clicks) + `</td>
                            <td class="p-4 text-green-400 font-bold">` + m.file_count + `</td>
                            <td class="p-4 flex gap-2">
                                <button onclick="addViews('`+encodeURIComponent(m._id)+`')" class="text-yellow-400 bg-yellow-900 bg-opacity-30 px-3 py-1 rounded"><i class="fa-solid fa-fire"></i> Boost</button>
                                <button onclick="openAdminEdit('`+encodeURIComponent(m._id)+`', '`+m._id.replace(/'/g, "\\'")+`')" class="text-blue-400 bg-blue-900 bg-opacity-30 px-3 py-1 rounded">Edit</button>
                                <button onclick="deleteMovie('`+encodeURIComponent(m._id)+`')" class="text-red-400 bg-red-900 bg-opacity-30 px-3 py-1 rounded">Delete</button>
                            </td>
                        </tr>`;
                    });
                    document.getElementById('movieTableBody').innerHTML = html;
                } catch (e) { alert("Error loading data from the server!"); }
            }
            
            function openAdminEdit(encodedTitle, oldTitle) {
                document.getElementById('editOldTitle').value = encodedTitle;
                document.getElementById('editNewTitle').value = oldTitle;
                document.getElementById('adminEditModal').classList.remove('hidden');
            }
            
            function closeAdminEdit() {
                document.getElementById('adminEditModal').classList.add('hidden');
            }
            
            async function saveAdminEdit() {
                let encodedTitle = document.getElementById('editOldTitle').value;
                let newTitle = document.getElementById('editNewTitle').value.trim();
                
                await fetch('/api/admin/movie/' + encodedTitle, {
                    method: 'PUT', 
                    headers: {'Content-Type': 'application/json'}, 
                    body: JSON.stringify({title_new: newTitle})
                });
                closeAdminEdit();
                loadAdminData();
            }

            async function deleteMovie(encodedTitle) {
                if(!confirm('Are you absolutely sure you want to delete ALL files for this movie?')) return;
                await fetch('/api/admin/movie/' + encodedTitle, {method: 'DELETE'});
                loadAdminData();
            }

            async function addViews(encodedTitle) {
                let amount = prompt("এই মুভির ভিউ কত বাড়াতে চান? (যেমন: 1000 বা 5000):", "1000");
                if(amount && amount.trim() !== "" && !isNaN(amount)) {
                    await fetch('/api/admin/movie/' + encodedTitle, {
                        method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({add_clicks: parseInt(amount)})
                    });
                    loadAdminData();
                }
            }
            loadAdminData();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/api/admin/data")
async def get_admin_data(auth: bool = Depends(verify_admin)):
    uc = await db.users.count_documents({})
    now = datetime.datetime.utcnow()
    today_start = datetime.datetime(now.year, now.month, now.day)
    new_users = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    pipeline = [
        {"$group": {
            "_id": "$title", 
            "clicks": {"$sum": "$clicks"}, 
            "file_count": {"$sum": 1}, 
            "created_at": {"$max": "$created_at"}
        }},
        {"$sort": {"created_at": -1}}, {"$limit": 50}
    ]
    movies = await db.movies.aggregate(pipeline).to_list(50)
    return {"total_users": uc, "total_groups": len(movies), "new_users_today": new_users, "movies": movies}

@app.delete("/api/admin/movie/{title}")
async def delete_movie_api(title: str, auth: bool = Depends(verify_admin)):
    await db.movies.delete_many({"title": title})
    return {"ok": True}

@app.put("/api/admin/movie/{title}")
async def edit_movie_api(title: str, data: dict = Body(...), auth: bool = Depends(verify_admin)):
    update_data = {}
    
    if new_title := data.get("title_new"): 
        update_data["title"] = new_title
        
    if update_data:
        await db.movies.update_many({"title": title}, {"$set": update_data})
        
    if add_clicks := data.get("add_clicks"):
        try:
            clicks_to_add = int(add_clicks)
            await db.movies.update_many({"title": update_data.get("title", title)}, {"$inc": {"clicks": clicks_to_add}})
        except ValueError: pass
            
    return {"ok": True}


# ==========================================
# 13. Main Web App UI (Frontend with Full Features)
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
    
    zone_id = ad_cfg['zone_id'] if ad_cfg else "10916755"
    tg_url = tg_cfg['url'] if tg_cfg else "https://t.me/MovieeBD"
    link_18 = b18_cfg['url'] if b18_cfg else "https://t.me/MovieeBD"
    required_ads = ad_count_cfg['count'] if ad_count_cfg else 1
    
    bkash_no = bkash_cfg['number'] if bkash_cfg else "Not Set"
    nagad_no = nagad_cfg['number'] if nagad_cfg else "Not Set"
    
    direct_links = dl_cfg.get('links', []) if dl_cfg else []
    dl_json = json.dumps(direct_links)

    html_code = r"""
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>MovieZone BD</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            html { scroll-behavior: smooth; }
            body { background: #0f172a; font-family: sans-serif; color: #fff; -webkit-font-smoothing: antialiased; overscroll-behavior-y: none; } 
            
            header { display: flex; justify-content: space-between; align-items: center; padding: 15px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); z-index: 1000; }
            .logo { font-size: 24px; font-weight: bold; }
            .logo span { background: red; color: #fff; padding: 2px 6px; border-radius: 5px; margin-left: 5px; font-size: 16px; }
            .header-right { display: flex; align-items: center; gap: 10px; }
            .user-info { display: flex; align-items: center; gap: 8px; background: #1e293b; padding: 6px 14px; border-radius: 25px; font-weight: bold; font-size: 14px; border: 1px solid #334155; }
            .user-info img { width: 28px; height: 28px; border-radius: 50%; object-fit: cover; }
            
            .menu-btn { background: #1e293b; border: 1px solid #334155; padding: 8px 12px; border-radius: 8px; cursor: pointer; color: white; font-size: 18px; transition: 0.3s; }
            .menu-btn:active { transform: scale(0.9); }
            
            .dropdown-menu { display: none; position: absolute; top: 65px; right: 15px; background: #1e293b; border: 1px solid #334155; border-radius: 12px; overflow: hidden; box-shadow: 0 5px 20px rgba(0,0,0,0.5); z-index: 2000; width: 220px; }
            .dropdown-menu a { display: block; padding: 12px 15px; color: white; text-decoration: none; font-weight: bold; font-size: 15px; border-bottom: 1px solid #334155; cursor: pointer; transition: 0.2s; }
            .dropdown-menu a:hover { background: #334155; }
            .dropdown-menu a:last-child { border-bottom: none; }
            .dropdown-menu i { width: 20px; text-align: center; margin-right: 8px; }

            .search-box { padding: 15px; }
            .search-input { width: 100%; padding: 16px; border-radius: 25px; border: none; outline: none; text-align: center; background: #1e293b; color: #fff; font-size: 18px; font-weight: bold; transition: 0.3s; box-shadow: inset 0 2px 5px rgba(0,0,0,0.3); }
            .search-input::placeholder { color: #94a3b8; font-weight: 500; font-size: 16px; }
            .search-input:focus { box-shadow: 0 0 15px rgba(248,113,113,0.7); }
            
            .section-title { padding: 5px 15px 15px; font-size: 22px; font-weight: 900; display: flex; align-items: center; gap: 8px; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; text-shadow: 0px 4px 15px rgba(255, 75, 43, 0.4); }
            .section-title i { -webkit-text-fill-color: #ff416c; }
            
            .trending-container, .upcoming-container { display: flex; overflow-x: auto; gap: 15px; padding: 0 15px 20px; scroll-behavior: smooth; -webkit-overflow-scrolling: touch; }
            .trending-container::-webkit-scrollbar, .upcoming-container::-webkit-scrollbar { display: none; }
            .trending-card, .upcoming-card { min-width: 140px; max-width: 140px; background: #1e293b; border-radius: 12px; overflow: hidden; cursor: pointer; flex-shrink: 0; position: relative; transition: transform 0.2s; }
            .trending-card:active, .upcoming-card:active { transform: scale(0.95); }
            .trending-card img, .upcoming-card img { height: 200px; object-fit: cover; width: 100%; border-radius: 10px; display: block; }
            
            .grid { padding: 0 15px 20px; display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; }
            .card { background: #1e293b; border-radius: 12px; overflow: hidden; cursor: pointer; transition: transform 0.2s, box-shadow 0.2s; }
            .card:active { transform: scale(0.95); }
            
            .post-content { position: relative; padding: 3px; border-radius: 12px; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); background-size: 400%; animation: glowing 8s linear infinite; }
            @keyframes glowing { 0% { background-position: 0 0; } 50% { background-position: 400% 0; } 100% { background-position: 0 0; } }
            .post-content img { width: 100%; height: 230px; object-fit: cover; display: block; border-radius: 10px; }
            
            .top-badge { position: absolute; top: 10px; left: 10px; background: linear-gradient(45deg, #ff0000, #cc0000); color: white; padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; z-index: 10; }
            .view-badge { position: absolute; bottom: 10px; left: 10px; background: rgba(0,0,0,0.75); color: #fff; padding: 4px 8px; border-radius: 6px; font-size: 12px; font-weight: bold; display: flex; align-items: center; gap: 5px; }
            .ep-badge { position: absolute; top: 10px; right: 10px; background: #10b981; color: white; padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; z-index: 10; }

            .card-footer { padding: 12px; font-size: 14px; font-weight: bold; text-align: center; color: #f8fafc; line-height: 1.4; white-space: normal; word-wrap: break-word; display: block; }
            
            .skeleton { background: #1e293b; border-radius: 12px; height: 260px; overflow: hidden; position: relative; }
            .skeleton::after { content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.05), transparent); animation: shimmer 1.5s infinite; }
            @keyframes shimmer { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }

            .pagination { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 10px 15px 120px; flex-wrap: wrap; }
            .page-btn { background: #1e293b; color: #fff; border: 1px solid #334155; padding: 10px 16px; border-radius: 8px; cursor: pointer; font-weight: bold; transition: 0.3s; outline: none; }
            .page-btn.active { background: #f87171; border-color: #f87171; color: white; box-shadow: 0 0 10px rgba(248,113,113,0.4); }

            .floating-btn { position: fixed; right: 20px; color: white; width: 50px; height: 50px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 22px; z-index: 500; cursor: pointer; box-shadow: 0 4px 15px rgba(0,0,0,0.5); transition: 0.3s; }
            .floating-btn:active { transform: scale(0.9); }
            .btn-18 { bottom: 155px; background: linear-gradient(45deg, #ff0000, #990000); border: 2px solid #fff; font-weight: bold; font-size: 18px; }
            .btn-tg { bottom: 95px; background: linear-gradient(45deg, #24A1DE, #1b7ba8); }
            .btn-req { bottom: 35px; background: linear-gradient(45deg, #10b981, #059669); }

            .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: center; justify-content: center; z-index: 3000; backdrop-filter: blur(5px); }
            .modal-content { background: #1e293b; width: 92%; max-width: 400px; padding: 25px; border-radius: 20px; text-align: center; border: 1px solid #334155; max-height: 85vh; overflow-y: auto; position: relative; }
            
            .close-icon { position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: #334155; color: #fff; font-size: 18px; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: 0.2s; z-index: 100; border: 1px solid #475569; }
            .close-icon:hover { background: #ef4444; color: white; }
            .close-icon:active { transform: scale(0.9); }

            .instruction-text { color: #fbbf24; font-size: 15.5px; font-weight: bold; margin-bottom: 20px; line-height: 1.5; }
            
            .rgb-border { position: relative; border: none; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); background-size: 400%; animation: glowing 8s linear infinite; padding: 3px; border-radius: 14px; margin-bottom: 12px; cursor: pointer; transition: 0.3s; width: 100%; box-shadow: 0 0 15px rgba(255,0,0,0.3); }
            .rgb-border:active { transform: scale(0.98); }
            .rgb-inner { display: flex; justify-content: space-between; align-items: center; background: #0f172a; padding: 16px; border-radius: 12px; width: 100%; color: white; font-weight: bold; font-size: 16px; }

            .close-btn { background: #334155; color: white; padding: 12px 20px; border-radius: 12px; margin-top: 15px; border: none; width: 100%; font-weight: bold; font-size: 16px; cursor: pointer; }
            .req-input { width: 100%; padding: 16px; margin: 20px 0; border-radius: 12px; border: 2px solid #334155; background: #0f172a; color: white; outline: none; font-size: 16px; font-weight: bold; }
            .btn-submit { background: linear-gradient(45deg, #10b981, #059669); color: white; border: none; padding: 15px 20px; border-radius: 12px; font-weight: bold; width: 100%; font-size: 18px; cursor: pointer; transition: 0.3s; }
            .btn-submit:active { transform: scale(0.95); }
            .notice-box { background: linear-gradient(135deg, rgba(248,113,113,0.15), rgba(220,38,38,0.25)); border-left: 5px solid #ef4444; padding: 15px; text-align: left; margin: 25px 0; border-radius: 8px; }
            .notice-box p { color: #fecaca; font-size: 16.5px; font-weight: bold; margin: 0; line-height: 1.6; text-shadow: 0 1px 3px rgba(0,0,0,0.5); }
            .refer-box { background: #0f172a; padding: 15px; border-radius: 10px; border: 1px dashed #3b82f6; margin: 15px 0; font-size: 14px; word-break: break-all; color: #93c5fd; }

            /* Direct Link RGB Modal Custom CSS */
            .dl-rgb-wrap { position: relative; border: none; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); background-size: 400%; animation: glowing 8s linear infinite; padding: 4px; border-radius: 16px; width: 100%; max-width: 350px; margin: auto; }
            .dl-inner-box { background: rgba(15, 23, 42, 0.98); border-radius: 12px; padding: 30px 20px; display: flex; flex-direction: column; align-items: center; gap: 15px; }

            .vip-tag { background: linear-gradient(45deg, #fbbf24, #f59e0b); color: #000; font-size: 12px; padding: 3px 8px; border-radius: 12px; font-weight: bold; display: none; margin-left:5px; box-shadow: 0 0 10px rgba(251,191,36,0.5); }
            
            /* CSS FOR FEATURES */
            .coin-tag { background: #3b82f6; color: white; font-size: 12px; padding: 3px 8px; border-radius: 12px; font-weight: bold; margin-left:5px; display: inline-block; }
            .badge-tag { background: #6366f1; color: white; font-size: 11px; padding: 2px 6px; border-radius: 8px; font-weight: bold; margin-left:4px; display: inline-block; margin-top: 4px; border:1px solid #818cf8;}
            .lb-item { display: flex; justify-content: space-between; background: #0f172a; padding: 12px; border-radius: 8px; margin-bottom: 8px; border: 1px solid #334155; align-items: center;}
            .lb-rank { font-size: 18px; font-weight: 900; color: #fbbf24; width: 30px;}
            .req-item { background: #0f172a; padding: 12px; border-radius: 8px; margin-bottom: 8px; border: 1px solid #334155; display: flex; justify-content: space-between; align-items: center;}
            .vote-btn { background: #3b82f6; color: white; border: none; padding: 6px 12px; border-radius: 6px; font-weight: bold; cursor: pointer; transition: 0.2s;}
            .vote-btn:disabled { background: #475569; cursor: not-allowed; color:#94a3b8;}
            
            /* Review UI */
            .review-section { margin-top: 25px; padding-top: 20px; border-top: 1px solid #334155; text-align: left; }
            .stars { color: #fbbf24; font-size: 24px; cursor: pointer; letter-spacing: 5px; text-align: center; margin: 15px 0; }
            .review-input { width: 100%; background: #0f172a; border: 1px solid #334155; color: white; padding: 12px; border-radius: 8px; outline: none; margin-bottom: 12px; font-family: inherit; }
            .review-item { background: #0f172a; padding: 12px; border-radius: 8px; margin-bottom: 10px; font-size: 14px; border-left: 3px solid #38bdf8; }
            .review-item span { color: #fbbf24; font-weight: bold; }

            /* VIP Payment Packages UI */
            .method-btn { padding: 12px; width: 48%; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; color: white; font-size: 16px; transition: 0.3s; }
            .pay-box { background: #0f172a; border: 1px solid #334155; padding: 15px; border-radius: 10px; margin-top:15px; text-align: left; font-size: 14.5px; color:#cbd5e1; display:none; }
            .pay-number { font-size: 24px; color: #4ade80; font-weight: 900; text-align: center; letter-spacing: 2px; margin: 15px 0; }
            
            .pkg-options { margin: 15px 0; }
            .pkg-label { display: block; background: #1e293b; padding: 12px; border-radius: 8px; margin-bottom: 8px; cursor: pointer; border: 1px solid #334155; font-weight: bold; transition: 0.3s; }
            .pkg-label:hover { background: #334155; }
            .pkg-label input { margin-right: 10px; transform: scale(1.2); }

            /* Chat CSS */
            .chat-container { height: 350px; overflow-y: auto; background: #0f172a; border-radius: 10px; border: 1px solid #334155; padding: 10px; margin-top: 15px; text-align: left; display: flex; flex-direction: column; gap: 8px; }
            .chat-msg { background: #1e293b; padding: 8px 12px; border-radius: 12px; font-size: 14px; width: fit-content; max-width: 85%; word-break: break-word; position: relative; }
            .chat-msg.mine { background: #3b82f6; color: white; align-self: flex-end; border-bottom-right-radius: 2px; }
            .chat-msg.others { border-bottom-left-radius: 2px; border: 1px solid #334155; }
            .chat-name { font-size: 11px; color: #fbbf24; font-weight: bold; margin-bottom: 3px; }
            .chat-reply-btn { font-size: 12px; color: #94a3b8; cursor: pointer; margin-left: 10px; float: right; padding: 2px 5px; }
            .chat-reply-btn:hover { color: #fbbf24; }
            
            /* Spin CSS (FIXED FOR 8 SLICES) */
            .spin-wrapper { position: relative; width: 250px; height: 250px; margin: 20px auto; border-radius: 50%; border: 8px solid #334155; overflow: hidden; box-shadow: 0 0 30px rgba(251,191,36,0.3); }
            .wheel { 
                position: relative; width: 100%; height: 100%; border-radius: 50%; 
                transition: transform 4s cubic-bezier(0.1, 0.7, 0.1, 1); 
                background: conic-gradient(
                    #ff9a9e 0deg 45deg,
                    #fecfef 45deg 90deg,
                    #a18cd1 90deg 135deg,
                    #fbc2eb 135deg 180deg,
                    #84fab0 180deg 225deg,
                    #8fd3f4 225deg 270deg,
                    #fccb90 270deg 315deg,
                    #d57eeb 315deg 360deg
                ); 
            }
            .pointer { position: absolute; top: -15px; left: 50%; transform: translateX(-50%); width: 0; height: 0; border-left: 15px solid transparent; border-right: 15px solid transparent; border-top: 30px solid #ef4444; z-index: 10; filter: drop-shadow(0 2px 2px rgba(0,0,0,0.5)); }
            .spin-center { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 50px; height: 50px; background: #1e293b; border-radius: 50%; z-index: 5; border: 3px solid #fbbf24; display: flex; align-items: center; justify-content: center; font-weight: bold; color: white; }

            /* Tasks CSS */
            .task-box { background: #0f172a; padding: 15px; border-radius: 12px; border: 1px solid #334155; text-align: left; margin-bottom: 15px; }
            .task-title { font-size: 16px; font-weight: bold; color: white; margin-bottom: 8px; display: flex; justify-content: space-between; }
            .progress-bg { width: 100%; height: 10px; background: #334155; border-radius: 5px; overflow: hidden; margin-bottom: 10px; }
            .progress-fill { height: 100%; background: linear-gradient(45deg, #10b981, #059669); width: 0%; transition: width 0.5s; }
            .task-btn { padding: 8px 15px; border-radius: 8px; border: none; font-weight: bold; color: white; width: 100%; cursor: pointer; background: #3b82f6; transition: 0.2s; }
            .task-btn:disabled { background: #475569; color: #94a3b8; cursor: not-allowed; }
            .task-btn.claimed { background: #10b981; }
        </style>
    </head>
    <body onclick="closeMenu(event)">
        <header>
            <div class="logo">Latest Movie <span>BD</span></div>
            <div class="header-right">
                <div style="display: flex; flex-direction: column; align-items: flex-end;">
                    <div class="user-info">
                        <span id="uName">Guest</span>
                        <span id="vipBadge" class="vip-tag"><i class="fa-solid fa-crown"></i> VIP</span>
                        <span class="coin-tag"><i class="fa-solid fa-coins"></i> <span id="coinCount">0</span></span>
                        <img id="uPic" src="https://cdn-icons-png.flaticon.com/512/3135/3135715.png">
                    </div>
                    <div id="badgesContainer"></div>
                </div>
                <div class="menu-btn" onclick="toggleMenu(event)"><i class="fa-solid fa-bars"></i></div>
            </div>
        </header>
        
        <div id="dropdownMenu" class="dropdown-menu">
            <a onclick="goHome()"><i class="fa-solid fa-house text-green-400"></i> হোম পেইজ</a>
            <a onclick="openCheckinModal()"><i class="fa-solid fa-gift text-pink-400"></i> ডেইলি চেক-ইন 🪙</a>
            <a onclick="openTasksModal()"><i class="fa-solid fa-bullseye text-red-400"></i> ডেইলি মিশন 🎯</a>
            <a onclick="openSpinModal()"><i class="fa-solid fa-dharmachakra text-yellow-400"></i> লাকি স্পিন 🎡</a>
            <a onclick="openChatModal()"><i class="fa-solid fa-comments text-blue-400"></i> গ্লোবাল চ্যাট 💬</a>
            <a onclick="openVipModal()"><i class="fa-solid fa-crown text-yellow-400"></i> VIP প্যাকেজ কিনুন</a>
            <a onclick="openReferModal()"><i class="fa-solid fa-share-nodes text-blue-400"></i> রেফার ও ইনকাম</a>
            <a onclick="openLeaderboard()"><i class="fa-solid fa-trophy text-yellow-400"></i> লিডারবোর্ড 🏆</a>
            <a onclick="openReqModal()"><i class="fa-solid fa-code-pull-request text-green-400"></i> রিকোয়েস্ট ও ভোট 🗳️</a>
        </div>

        <div class="search-box">
            <input type="text" id="searchInput" class="search-input" placeholder="🔍 মুভি বা ওয়েব সিরিজ খুঁজুন...">
        </div>

        <div id="trendingWrapper">
            <div class="section-title"><i class="fa-solid fa-fire"></i> ট্রেন্ডিং মুভি</div>
            <div class="trending-container" id="trendingGrid">
                <div class="skeleton" style="min-width:140px; height:240px;"></div>
                <div class="skeleton" style="min-width:140px; height:240px;"></div>
                <div class="skeleton" style="min-width:140px; height:240px;"></div>
            </div>
        </div>

        <div id="upcomingWrapper" style="display: none;">
            <div class="section-title"><i class="fa-solid fa-clock-rotate-left"></i> আপকামিং মুভি</div>
            <div class="upcoming-container" id="upcomingGrid"></div>
        </div>

        <div class="section-title"><i class="fa-solid fa-film"></i> নতুন সব মুভি</div>
        <div class="grid" id="movieGrid"></div>
        <div class="pagination" id="paginationBox"></div>

        <div class="floating-btn btn-18" onclick="window.open('{{LINK_18}}')">18+</div>
        <div class="floating-btn btn-tg" onclick="window.open('{{TG_LINK}}')"><i class="fa-brands fa-telegram"></i></div>
        <div class="floating-btn btn-req" onclick="openReqModal()"><i class="fa-solid fa-code-pull-request"></i></div>

        <!-- Download & Review Modal -->
        <div id="qualityModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="closeQualityModal()"><i class="fa-solid fa-xmark"></i></div>
                <h2 id="modalTitle" style="color:#38bdf8; margin-bottom: 8px; font-size: 22px; font-weight:900;">Movie Title</h2>
                <p class="instruction-text">👇 আপনি কোনটি ডাউনলোড করতে চান তা নির্বাচন করুন:</p>
                
                <div id="qualityList"></div>
                
                <!-- Rating & Review Section -->
                <div class="review-section">
                    <h3 style="color:white; font-size:18px;"><i class="fa-solid fa-star text-yellow-400"></i> রেটিং ও কমেন্ট</h3>
                    <div class="stars" id="starRating">
                        <i class="fa-regular fa-star" onclick="setRating(1)"></i>
                        <i class="fa-regular fa-star" onclick="setRating(2)"></i>
                        <i class="fa-regular fa-star" onclick="setRating(3)"></i>
                        <i class="fa-regular fa-star" onclick="setRating(4)"></i>
                        <i class="fa-regular fa-star" onclick="setRating(5)"></i>
                    </div>
                    <textarea id="reviewText" class="review-input" rows="2" placeholder="মুভিটি কেমন লাগলো? কমেন্ট করে জানান..."></textarea>
                    <button class="btn-submit" style="padding:10px; font-size:16px;" onclick="submitReview()">সাবমিট করুন</button>
                    
                    <div id="reviewList" style="margin-top:20px; max-height:150px; overflow-y:auto; padding-right:5px;">
                        <!-- Reviews will load here -->
                    </div>
                </div>
            </div>
        </div>

        <!-- Direct Link RGB Modal -->
        <div id="directLinkModal" class="modal">
            <div class="modal-content" style="background: transparent; border: none; padding: 0;">
                <div class="close-icon" onclick="document.getElementById('directLinkModal').style.display='none'" style="top: -15px; right: 5px; z-index: 1000;"><i class="fa-solid fa-xmark"></i></div>
                <div class="dl-rgb-wrap">
                    <div class="dl-inner-box">
                        <h2 style="color: #4ade80; font-size: 24px; font-weight: 900; margin-bottom: 5px;"><i class="fa-solid fa-unlock-keyhole"></i> আনলক করুন</h2>
                        <p id="dlDescText" style="color: #cbd5e1; font-size: 15px; line-height: 1.6; font-weight: 600;">
                            <!-- Text injected via JS -->
                        </p>
                        <button id="dlClickBtn" class="btn-submit" style="background: linear-gradient(45deg, #ef4444, #f97316); box-shadow: 0 0 15px rgba(239,68,68,0.5); font-size: 18px; padding: 15px; margin-top: 10px;" onclick="executeDirectLink()">🔗 Click Here (Open Link)</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Daily Check-in Modal -->
        <div id="checkinModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('checkinModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <i class="fa-solid fa-gift" style="font-size:70px; color:#ec4899; text-shadow: 0 0 20px rgba(236,72,153,0.5);"></i>
                <h2 style="margin:15px 0 10px; color:white; font-size: 24px;">ডেইলি রিওয়ার্ড</h2>
                <p style="color:#cbd5e1; font-size:15px; line-height: 1.5;">প্রতিদিন লগিন করে ফ্রী কয়েন সংগ্রহ করুন। ৫০ কয়েন জমিয়ে ১ দিনের VIP কেনা যাবে!</p>
                
                <div style="background:#0f172a; border: 2px dashed #fbbf24; border-radius:15px; padding: 20px; margin: 20px 0;">
                    <h1 style="color:#fbbf24; font-size:45px; margin: 0;"><i class="fa-solid fa-coins"></i> <span id="modalCoinCount">0</span></h1>
                    <p style="color:gray; font-size:13px; margin-top:5px;">আপনার বর্তমান ব্যালেন্স</p>
                </div>
                
                <button class="btn-submit" style="background: linear-gradient(45deg, #3b82f6, #2563eb); margin-bottom: 10px;" onclick="claimCheckin()">আজকের ১০ কয়েন সংগ্রহ করুন</button>
                <button class="btn-submit" style="background: linear-gradient(45deg, #8b5cf6, #6d28d9); margin-bottom: 10px;" onclick="watchAdForCoins()"><i class="fa-solid fa-link"></i> ডাইরেক্ট লিংক ভিজিট করে ৫ কয়েন আয় করুন</button>
                <button class="btn-submit" style="background: linear-gradient(45deg, #f59e0b, #d97706); color:black;" onclick="convertCoins()"><i class="fa-solid fa-crown"></i> কয়েন দিয়ে VIP কিনুন (50)</button>
            </div>
        </div>

        <!-- VIP Automated Payment Modal with Packages -->
        <div id="vipModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('vipModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 style="color:#fbbf24; font-size: 24px; margin-bottom:10px;"><i class="fa-solid fa-crown"></i> VIP প্যাকেজ কিনুন</h2>
                <p style="color:#cbd5e1; font-size:14.5px; margin-bottom:15px; line-height: 1.4;">পেমেন্ট করে VIP প্যাকেজ কিনুন। ফাইল অটো-ডিলিট হবে না এবং কোনো অ্যাড দেখতে হবে না!</p>
                
                <div style="display:flex; justify-content:space-between; margin-bottom: 15px;">
                    <button class="method-btn" style="background:#e11471; box-shadow: 0 4px 10px rgba(225,20,113,0.4);" onclick="selectPayment('bkash')">bKash</button>
                    <button class="method-btn" style="background:#f97316; box-shadow: 0 4px 10px rgba(249,115,22,0.4);" onclick="selectPayment('nagad')">Nagad</button>
                </div>

                <div id="payBox" class="pay-box">
                    <p style="color:#38bdf8; font-weight:bold; font-size: 16px; margin-bottom:10px;">👇 প্যাকেজ সিলেক্ট করুন:</p>
                    <div class="pkg-options">
                        <label class="pkg-label"><input type="radio" name="vip_pkg" value="7" data-price="10" checked> ৭ দিন - ১০ টাকা</label>
                        <label class="pkg-label"><input type="radio" name="vip_pkg" value="15" data-price="20"> ১৫ দিন - ২০ টাকা</label>
                        <label class="pkg-label"><input type="radio" name="vip_pkg" value="30" data-price="30"> ১ মাস (৩০ দিন) - ৩০ টাকা</label>
                        <label class="pkg-label"><input type="radio" name="vip_pkg" value="90" data-price="80"> ৩ মাস (৯০ দিন) - ৮০ টাকা</label>
                        <label class="pkg-label"><input type="radio" name="vip_pkg" value="180" data-price="150"> ৬ মাস (১৮০ দিন) - ১৫০ টাকা</label>
                    </div>

                    <p><b>১.</b> নিচের নাম্বারে আপনার নির্বাচিত প্যাকেজের টাকা <b>Send Money</b> করুন:</p>
                    <div class="pay-number" id="payNumberText">...</div>
                    <p><b>২.</b> টাকা পাঠানোর পর ফিরতি মেসেজে থাকা <b>TrxID</b> নিচে সাবমিট করুন:</p>
                    <input type="text" id="trxIdInput" class="search-input" style="margin-top:10px; background:#1e293b; padding:15px; font-size:16px;" placeholder="যেমন: 8JD8XXXXX">
                    <button class="btn-submit" onclick="submitPayment()">পেমেন্ট ভেরিফাই করুন</button>
                </div>
            </div>
        </div>

        <!-- Refer Modal -->
        <div id="referModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('referModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <i class="fa-solid fa-share-nodes" style="font-size:60px; color:#38bdf8;"></i>
                <h2 style="margin:15px 0 10px; color:white; font-size: 24px;">রেফার করুন</h2>
                <p style="color:#cbd5e1; font-size:15px; margin-bottom:15px;">প্রতি ৫ জন রেফার করলেই পাবেন ২৪ ঘণ্টার VIP একদম ফ্রি!</p>
                <h3 style="color:#4ade80; font-size:18px;">মোট রেফার: <span id="refCountNum" style="font-size:24px; font-weight:900;">0</span> জন</h3>
                <div class="refer-box" id="refLinkText">Loading link...</div>
                <button class="btn-submit" style="background: linear-gradient(45deg, #3b82f6, #1d4ed8);" onclick="copyReferLink()"><i class="fa-regular fa-copy"></i> লিংক কপি করুন</button>
            </div>
        </div>
        
        <!-- Leaderboard Modal -->
        <div id="leaderboardModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('leaderboardModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 style="color:#fbbf24; font-size: 24px; margin-bottom:15px;"><i class="fa-solid fa-trophy"></i> টপ লিডারবোর্ড</h2>
                <div id="lbList" style="max-height: 50vh; overflow-y:auto; text-align:left; padding-right:5px;">
                    <!-- Loading leaderboard -->
                </div>
            </div>
        </div>

        <!-- Request Board & Vote Modal -->
        <div id="reqModal" class="modal">
            <div class="modal-content" style="max-height: 90vh;">
                <div class="close-icon" onclick="document.getElementById('reqModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 style="color:white; font-size: 24px;">মুভি রিকোয়েস্ট ও ভোট 🗳️</h2>
                <div style="display:flex; gap:10px; margin-top:15px;">
                    <input type="text" id="reqText" class="req-input" style="margin:0;" placeholder="নতুন মুভির নাম...">
                    <button class="btn-submit" style="width:auto; padding:0 20px;" onclick="sendReq()"><i class="fa-solid fa-plus"></i></button>
                </div>
                <p style="text-align:left; color:#94a3b8; font-size:14px; margin-top:15px; font-weight:bold;">ট্রেন্ডিং রিকোয়েস্ট:</p>
                <div id="reqList" style="max-height: 40vh; overflow-y:auto; margin-top:10px; text-align:left; padding-right:5px;">
                    <!-- Loading requests -->
                </div>
            </div>
        </div>

        <!-- Global Chat Modal -->
        <div id="chatModal" class="modal">
            <div class="modal-content" style="max-height: 90vh; display:flex; flex-direction:column;">
                <div class="close-icon" onclick="closeChat()"><i class="fa-solid fa-xmark"></i></div>
                <h2 style="color:white; font-size: 22px; margin-bottom:5px;"><i class="fa-solid fa-comments text-blue-400"></i> গ্লোবাল চ্যাট</h2>
                <p style="color:#94a3b8; font-size:13px; margin-bottom:5px;">সবার সাথে কথা বলুন, মুভির নাম শেয়ার করুন!</p>
                <div id="chatBox" class="chat-container"></div>
                <div style="display:flex; gap:8px; margin-top:15px;">
                    <input type="text" id="chatInput" class="req-input" style="margin:0; padding:12px; font-size:14px;" placeholder="মেসেজ লিখুন...">
                    <button class="btn-submit" style="width:auto; padding:0 20px; font-size:16px;" onclick="sendChatMessage()"><i class="fa-solid fa-paper-plane"></i></button>
                </div>
            </div>
        </div>

        <!-- Spin to Win Modal -->
        <div id="spinModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('spinModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 style="color:#fbbf24; font-size: 24px; margin-bottom:5px;"><i class="fa-solid fa-dharmachakra"></i> লাকি স্পিন</h2>
                <p style="color:#cbd5e1; font-size:14px;">প্রতিদিন ৩ বার স্পিন করে ফ্রী কয়েন জিতে নিন! স্পিন করতে একটি লিংক ভিজিট করতে হবে.</p>
                
                <div class="spin-wrapper">
                    <div class="pointer"></div>
                    <div class="spin-center">SPIN</div>
                    <div class="wheel" id="spinWheel">
                        <!-- Slices will be rendered via JS -->
                    </div>
                </div>
                
                <p style="color:#4ade80; font-weight:bold; margin-bottom:15px;">আজকের স্পিন বাকি: <span id="spinsLeftText">...</span></p>
                <button class="btn-submit" id="spinBtn" onclick="startSpin()" style="background: linear-gradient(45deg, #f59e0b, #d97706);"><i class="fa-solid fa-play"></i> স্পিন করুন</button>
            </div>
        </div>

        <!-- Daily Missions Modal -->
        <div id="tasksModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('tasksModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 style="color:#f87171; font-size: 24px; margin-bottom:5px;"><i class="fa-solid fa-bullseye"></i> ডেইলি মিশন</h2>
                <p style="color:#cbd5e1; font-size:14px; margin-bottom:15px;">প্রতিদিনের কাজগুলো সম্পূর্ণ করে এক্সট্রা কয়েন কালেক্ট করুন!</p>
                
                <div class="task-box">
                    <div class="task-title"><span><i class="fa-solid fa-link text-pink-400"></i> ৩টি ডাইরেক্ট লিংক দেখুন</span> <span id="adTaskProgress">0/3</span></div>
                    <div class="progress-bg"><div class="progress-fill" id="adTaskBar"></div></div>
                    <button class="task-btn" id="adTaskBtn" disabled onclick="claimTaskReward('ads')">15 Coins Claim করুন</button>
                </div>
                
                <div class="task-box">
                    <div class="task-title"><span><i class="fa-solid fa-star text-yellow-400"></i> ২টি মুভি রিভিউ দিন</span> <span id="revTaskProgress">0/2</span></div>
                    <div class="progress-bg"><div class="progress-fill" id="revTaskBar"></div></div>
                    <button class="task-btn" id="revTaskBtn" disabled onclick="claimTaskReward('reviews')">10 Coins Claim করুন</button>
                </div>
            </div>
        </div>

        <div id="successModal" class="modal">
            <div class="modal-content">
                <i class="fa-solid fa-circle-check" style="font-size:80px; color:#4ade80;"></i>
                <h2 style="margin:20px 0 10px; color:white; font-size: 26px;">সম্পন্ন হয়েছে!</h2>
                <p style="color: #4ade80; font-size: 17px; font-weight: bold;">✅ ফাইলটি বটের ইনবক্সে পাঠানো হয়েছে।</p>
                <div class="notice-box" id="successNoticeBox">
                    <p><i class="fa-solid fa-triangle-exclamation" style="color: #fbbf24;"></i> <b>সতর্কতা:</b> কপিরাইট এড়াতে মুভিটি কিছুক্ষণ পর অটোমেটিক ডিলিট হয়ে যাবে। এখনই বট থেকে সেভ করে নিন!</p>
                </div>
                <button class="btn-submit" onclick="tg.close()">বটে ফিরে যান</button>
            </div>
        </div>

        <script>
            let tg = window.Telegram.WebApp; 
            tg.expand();
            
            const DIRECT_LINKS = {{DIRECT_LINKS}};
            let onAdCompleteCallback = null;

            const ZONE_ID = "{{ZONE_ID}}";
            const REQUIRED_ADS = parseInt("{{AD_COUNT}}");
            const INIT_DATA = tg.initData || "";
            const BOT_UNAME = "{{BOT_USER}}";
            let currentPage = 1; let isLoading = false; let searchQuery = "";
            let uid = tg.initDataUnsafe?.user?.id || 0;
            let currentAdStep = 1; let activeFileId = null; let autoScrollInterval; let isTouching = false; let abortController = null;
            let isRewardAd = false;
            let isSpinAd = false;
            
            const BKASH_NO = "{{BKASH_NO}}";
            const NAGAD_NO = "{{NAGAD_NO}}";
            
            let loadedMovies = {}; 
            let isUserVip = false;
            let isAdmin = false;
            let userReferCount = 0;
            let currentRating = 0;
            let currentMovieTitle = "";
            let selectedPayMethod = "";

            function formatViews(num) {
                if (num >= 1000000) return (num / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
                if (num >= 1000) return (num / 1000).toFixed(1).replace(/\.0$/, '') + 'K';
                return num.toString();
            }

            if(tg.initDataUnsafe && tg.initDataUnsafe.user) {
                document.getElementById('uName').innerText = tg.initDataUnsafe.user.first_name;
                if(tg.initDataUnsafe.user.photo_url) document.getElementById('uPic').src = tg.initDataUnsafe.user.photo_url;
            }

            async function fetchUserInfo() {
                try {
                    const res = await fetch('/api/user/' + uid);
                    const data = await res.json();
                    isUserVip = data.vip;
                    isAdmin = data.is_admin;
                    userReferCount = data.refer_count;
                    document.getElementById('coinCount').innerText = data.coins;
                    document.getElementById('modalCoinCount').innerText = data.coins;
                    
                    if(isUserVip) {
                        document.getElementById('vipBadge').style.display = 'inline-block';
                        if(data.vip_expiry) {
                            if(!document.getElementById('vipExpiryText')) {
                                document.getElementById('dropdownMenu').insertAdjacentHTML('afterbegin', `<div id="vipExpiryText" style="padding: 10px 15px; color: #4ade80; font-size: 13px; font-weight: bold; border-bottom: 1px solid #334155; text-align: center;"><i class="fa-regular fa-clock"></i> মেয়াদ: ${data.vip_expiry}</div>`);
                            }
                        }
                    }
                    document.getElementById('refCountNum').innerText = userReferCount;
                    document.getElementById('refLinkText').innerText = `https://t.me/${BOT_UNAME}?start=ref_${uid}`;
                    
                    if(data.badges && data.badges.length > 0) {
                        document.getElementById('badgesContainer').innerHTML = data.badges.map(b => '<span class="badge-tag">'+b+'</span>').join('');
                    }
                } catch(e) {}
            }

            function toggleMenu(e) { e.stopPropagation(); const menu = document.getElementById('dropdownMenu'); menu.style.display = (menu.style.display === 'block') ? 'none' : 'block'; }
            function closeMenu() { document.getElementById('dropdownMenu').style.display = 'none'; }
            
            function goHome() {
                document.getElementById('searchInput').value = ""; searchQuery = "";
                document.getElementById('trendingWrapper').style.display = 'block';
                loadUpcoming(); loadTrending(); loadMovies(1); closeMenu();
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }
            
            function openReferModal() { document.getElementById('referModal').style.display = 'flex'; closeMenu(); }
            function copyReferLink() {
                navigator.clipboard.writeText(document.getElementById('refLinkText').innerText).then(() => { tg.showAlert("✅ রেফার লিংক কপি হয়েছে!"); });
            }
            
            async function openLeaderboard() {
                closeMenu();
                document.getElementById('leaderboardModal').style.display = 'flex';
                const lbList = document.getElementById('lbList');
                lbList.innerHTML = "<p style='color:gray; text-align:center;'>Loading...</p>";
                try {
                    const res = await fetch('/api/leaderboard');
                    const data = await res.json();
                    if(data.length===0) return lbList.innerHTML = "<p style='color:gray; text-align:center;'>কোনো ডাটা পাওয়া যায়নি।</p>";
                    lbList.innerHTML = data.map((u, i) => `
                        <div class="lb-item">
                            <div style="display:flex; align-items:center; gap:10px;">
                                <span class="lb-rank">#${i+1}</span>
                                <span style="color:white; font-weight:bold;">${u.name}</span>
                            </div>
                            <span style="color:#4ade80; font-weight:bold;"><i class="fa-solid fa-users"></i> ${u.refers}</span>
                        </div>
                    `).join('');
                } catch(e) {}
            }

            // --- REVIEWS LOGIC ---
            function setRating(val) {
                currentRating = val;
                let stars = document.getElementById('starRating').children;
                for(let i=0; i<5; i++) stars[i].className = i < val ? "fa-solid fa-star" : "fa-regular fa-star";
            }

            async function loadReviews(title) {
                const list = document.getElementById('reviewList');
                list.innerHTML = "<p style='color:gray; text-align:center; padding:10px;'>Loading...</p>";
                try {
                    const res = await fetch('/api/reviews/' + encodeURIComponent(title));
                    const data = await res.json();
                    if(data.length === 0) list.innerHTML = "<p style='color:gray; text-align:center; padding:10px;'>এখনো কেউ কমেন্ট করেনি। আপনি প্রথম হতে পারেন!</p>";
                    else list.innerHTML = data.map(r => `<div class="review-item"><span>${'★'.repeat(r.rating)}${'☆'.repeat(5-r.rating)}</span> <b style="color:#e2e8f0;">${r.name}</b>: <br><span style="color:#94a3b8; font-weight:normal;">${r.comment}</span></div>`).join('');
                } catch(e) {}
            }

            async function submitReview() {
                if(currentRating === 0) return tg.showAlert("অনুগ্রহ করে স্টার রেটিং দিন!");
                let text = document.getElementById('reviewText').value;
                if(!text) return tg.showAlert("কিছু কমেন্ট লিখুন!");
                
                try {
                    await fetch('/api/reviews', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({uid: uid, name: document.getElementById('uName').innerText, title: currentMovieTitle, rating: currentRating, comment: text, initData: INIT_DATA})
                    });
                    document.getElementById('reviewText').value = ""; setRating(0);
                    loadReviews(currentMovieTitle); tg.showAlert("✅ ধন্যবাদ! আপনার রিভিউ যুক্ত হয়েছে।");
                } catch(e) {}
            }

            // --- CHECK-IN LOGIC ---
            function openCheckinModal() { document.getElementById('checkinModal').style.display = 'flex'; closeMenu(); }
            async function claimCheckin() {
                try {
                    const res = await fetch('/api/checkin', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({uid: uid, action: "claim", initData: INIT_DATA})
                    });
                    const data = await res.json();
                    if(data.ok) { tg.showAlert("🎉 অভিনন্দন! আপনি 10 Coins পেয়েছেন।"); fetchUserInfo(); }
                    else tg.showAlert(data.msg || "আপনি ইতিমধ্যে আজকের রিওয়ার্ড নিয়েছেন!");
                } catch(e) {}
            }
            async function convertCoins() {
                try {
                    const res = await fetch('/api/checkin', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({uid: uid, action: "convert", initData: INIT_DATA})
                    });
                    const data = await res.json();
                    if(data.ok) { tg.showAlert("✅ সফল! ৫০ কয়েন কেটে নেওয়া হয়েছে এবং আপনার ১ দিনের VIP চালু হয়েছে।"); fetchUserInfo(); }
                    else tg.showAlert(data.msg || "আপনার পর্যাপ্ত কয়েন নেই! (৫০ প্রয়োজন)");
                } catch(e) {}
            }
            
            // --- VIP PAYMENT LOGIC ---
            function openVipModal() { document.getElementById('vipModal').style.display = 'flex'; document.getElementById('payBox').style.display='none'; closeMenu(); }
            function selectPayment(method) {
                selectedPayMethod = method;
                document.getElementById('payBox').style.display = 'block';
                document.getElementById('payNumberText').innerText = method === 'bkash' ? BKASH_NO : NAGAD_NO;
            }
            async function submitPayment() {
                const trxId = document.getElementById('trxIdInput').value.trim();
                if(trxId.length < 5) return tg.showAlert("সঠিক TrxID দিন!");
                
                let selectedRadio = document.querySelector('input[name="vip_pkg"]:checked');
                let days = parseInt(selectedRadio.value);
                let price = parseInt(selectedRadio.getAttribute('data-price'));
                
                try {
                    const res = await fetch('/api/payment/submit', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({uid: uid, method: selectedPayMethod, trx_id: trxId, days: days, price: price, initData: INIT_DATA})
                    });
                    const data = await res.json();
                    if(data.ok) {
                        tg.showAlert("✅ পেমেন্ট রিকোয়েস্ট সফলভাবে পাঠানো হয়েছে! অ্যাডমিন যাচাই করার পর আপনার VIP চালু করে দেবে।");
                        document.getElementById('vipModal').style.display = 'none';
                        document.getElementById('trxIdInput').value = '';
                    } else { tg.showAlert(data.msg || "TrxID আগে ব্যবহার করা হয়েছে অথবা ভুল!"); }
                } catch(e) {}
            }


            // --- MOVIE LOADING & UI LOGIC ---
            function drawSkeletons(count) { return Array(count).fill('<div class="skeleton"></div>').join(''); }
            function startAutoScroll() {
                if(autoScrollInterval) clearInterval(autoScrollInterval);
                autoScrollInterval = setInterval(() => {
                    if(isTouching) return; 
                    let grid = document.getElementById('trendingGrid');
                    if(grid) {
                        if (grid.scrollLeft >= (grid.scrollWidth - grid.clientWidth - 10)) grid.scrollTo({ left: 0, behavior: 'smooth' });
                        else grid.scrollBy({ left: 155, behavior: 'smooth' });
                    }
                }, 3500);
            }

            async function loadTrending() {
                try {
                    const r = await fetch(`/api/trending?uid=${uid}`);
                    const data = await r.json();
                    if(data.error === "banned") return document.body.innerHTML = `<h2 style='color:#ef4444; text-align:center; margin-top:80px;'>🚫 You are permanently Banned!</h2>`;
                    const grid = document.getElementById('trendingGrid');
                    if(data.length === 0) return document.getElementById('trendingWrapper').style.display = 'none';
                    grid.innerHTML = data.map(m => {
                        loadedMovies[m._id] = m;
                        return `<div class="trending-card" onclick="openQualityModal('${m._id.replace(/'/g, "\\'")}')">
                            <div class="post-content">
                                <div class="top-badge">🔥 TOP</div>
                                <img src="/api/image/${m.photo_id}" loading="lazy" onerror="this.src='https://via.placeholder.com/400x240?text=No+Image'">
                                <div class="ep-badge"><i class="fa-solid fa-list"></i> ${m.files.length}</div>
                                <div class="view-badge"><i class="fa-solid fa-eye"></i> ${formatViews(m.clicks)}</div>
                            </div>
                            <div class="card-footer">${m._id}</div>
                        </div>`;
                    }).join('');
                    grid.addEventListener('touchstart', () => isTouching = true, {passive: true});
                    grid.addEventListener('touchend', () => setTimeout(() => isTouching = false, 1000), {passive: true});
                    setTimeout(startAutoScroll, 2000);
                } catch(e) {}
            }

            async function loadUpcoming() {
                try {
                    const r = await fetch(`/api/upcoming`);
                    const data = await r.json();
                    const grid = document.getElementById('upcomingGrid');
                    const wrapper = document.getElementById('upcomingWrapper');
                    if(data.length > 0) {
                        wrapper.style.display = 'block';
                        grid.innerHTML = data.map(m => `<div class="upcoming-card"><img src="/api/image/${m.photo_id}"><div class="card-footer">${m.title}</div></div>`).join('');
                    } else { wrapper.style.display = 'none'; }
                } catch(e) {}
            }

            async function loadMovies(page = 1, signal = null) {
                if(isLoading) return; isLoading = true; currentPage = page;
                const grid = document.getElementById('movieGrid');
                const pBox = document.getElementById('paginationBox');
                grid.innerHTML = drawSkeletons(16); pBox.innerHTML = "";

                try {
                    const r = await fetch(`/api/list?page=${currentPage}&q=${encodeURIComponent(searchQuery)}&uid=${uid}`, { signal });
                    const data = await r.json();
                    if(data.error === "banned") return;

                    if(data.movies && data.movies.length === 0) {
                        grid.innerHTML = `<p style='grid-column: span 2; text-align:center; color:#fbbf24; font-size: 18px; padding:40px;'>🚫 কোনো মুভি পাওয়া যায়নি!</p>`;
                    } else if (data.movies) {
                        grid.innerHTML = data.movies.map(m => {
                            loadedMovies[m._id] = m; 
                            return `<div class="card" onclick="openQualityModal('${m._id.replace(/'/g, "\\'")}')">
                                <div class="post-content">
                                    <img src="/api/image/${m.photo_id}" loading="lazy" onerror="this.src='https://via.placeholder.com/400x240?text=No+Image'">
                                    <div class="ep-badge"><i class="fa-solid fa-list"></i> ${m.files.length}</div>
                                    <div class="view-badge"><i class="fa-solid fa-eye"></i> ${formatViews(m.clicks)}</div>
                                </div>
                                <div class="card-footer">${m._id}</div>
                            </div>`;
                        }).join('');
                        renderPagination(data.total_pages);
                    }
                } catch(e) {}
                isLoading = false;
            }

            function renderPagination(totalPages) {
                if (totalPages <= 1) return;
                let html = `<button class="page-btn" ${currentPage === 1 ? 'disabled' : ''} onclick="goToPage(${currentPage - 1})"><i class="fa-solid fa-angle-left"></i></button>`;
                let start = Math.max(1, currentPage - 1); let end = Math.min(totalPages, currentPage + 1);
                if (start > 1) { html += `<button class="page-btn" onclick="goToPage(1)">1</button>`; if (start > 2) html += `<span style="color:gray;">...</span>`; }
                for (let i = start; i <= end; i++) html += `<button class="page-btn ${i === currentPage ? 'active' : ''}" onclick="goToPage(${i})">${i}</button>`; 
                if (end < totalPages) { if (end < totalPages - 1) html += `<span style="color:gray;">...</span>`; html += `<button class="page-btn" onclick="goToPage(${totalPages})">${totalPages}</button>`; }
                html += `<button class="page-btn" ${currentPage === totalPages ? 'disabled' : ''} onclick="goToPage(${currentPage + 1})"><i class="fa-solid fa-angle-right"></i></button>`;
                document.getElementById('paginationBox').innerHTML = html;
            }

            function goToPage(p) { if (p < 1) return; loadMovies(p); window.scrollTo({ top: document.getElementById('movieGrid').offsetTop - 100, behavior: 'smooth' }); }

            let timeout = null;
            document.getElementById('searchInput').addEventListener('input', function(e) {
                clearTimeout(timeout); searchQuery = e.target.value.trim();
                if(searchQuery !== "") { document.getElementById('trendingWrapper').style.display = 'none'; document.getElementById('upcomingWrapper').style.display = 'none'; isTouching = true; } 
                else { document.getElementById('trendingWrapper').style.display = 'block'; loadUpcoming(); isTouching = false; loadTrending(); }
                timeout = setTimeout(() => { 
                    if(abortController) abortController.abort();
                    abortController = new AbortController();
                    loadMovies(1, abortController.signal); 
                }, 500); 
            });


            // ==========================================
            // DIRECT LINK AD LOGIC (REPLACED MONETAG)
            // ==========================================
            function showDirectLinkModal(descText) {
                document.getElementById('dlDescText').innerHTML = descText;
                const btn = document.getElementById('dlClickBtn');
                btn.innerText = "🔗 Click Here (Open Link)";
                btn.disabled = false;
                btn.style.background = "linear-gradient(45deg, #ef4444, #f97316)";
                document.getElementById('directLinkModal').style.display = 'flex';
            }

            function executeDirectLink() {
                if (!DIRECT_LINKS || DIRECT_LINKS.length === 0) {
                    document.getElementById('directLinkModal').style.display = 'none';
                    if (onAdCompleteCallback) onAdCompleteCallback();
                    return;
                }
                
                const randomLink = DIRECT_LINKS[Math.floor(Math.random() * DIRECT_LINKS.length)];
                tg.openLink(randomLink);

                const btn = document.getElementById('dlClickBtn');
                btn.disabled = true;
                let timeLeft = 15;
                btn.innerText = `⏳ অপেক্ষা করুন... (${timeLeft}s)`;
                btn.style.background = "#475569";

                let dlTimer = setInterval(() => {
                    timeLeft--;
                    btn.innerText = `⏳ অপেক্ষা করুন... (${timeLeft}s)`;
                    if (timeLeft <= 0) {
                        clearInterval(dlTimer);
                        document.getElementById('directLinkModal').style.display = 'none';
                        if (onAdCompleteCallback) onAdCompleteCallback();
                    }
                }, 1000);
            }

            function openQualityModal(title) {
                const movie = loadedMovies[title];
                if(!movie) return;
                currentMovieTitle = title;
                document.getElementById('modalTitle').innerText = title;
                
                let listHtml = movie.files.map(f => {
                    let isFree = f.is_unlocked || isUserVip;
                    let icon = isFree ? '<i class="fa-solid fa-paper-plane text-green-400" style="font-size:18px;"></i>' : '<i class="fa-solid fa-lock text-red-400" style="font-size:18px;"></i>';
                    let cls = isFree ? 'border-left: 5px solid #10b981;' : 'border-left: 5px solid #ef4444;';
                    return `
                    <div class="rgb-border" onclick="handleQualityClick('${f.id}', ${f.is_unlocked})">
                        <div class="rgb-inner" style="${cls}"><span><i class="fa-solid fa-download"></i> ${f.quality}</span> ${icon}</div>
                    </div>`;
                }).join('');
                
                document.getElementById('qualityList').innerHTML = listHtml;
                document.getElementById('qualityModal').style.display = 'flex';
                
                setRating(0);
                loadReviews(title);
            }
            
            function closeQualityModal() { document.getElementById('qualityModal').style.display = 'none'; }

            function handleQualityClick(fileId, isUnlocked) {
                closeQualityModal();
                if(isUnlocked || isUserVip) { 
                    sendFile(fileId); 
                } else { 
                    activeFileId = fileId; 
                    onAdCompleteCallback = () => sendFile(activeFileId);
                    showDirectLinkModal("এই ভিডিও বা মুভিটি আনলক করতে নিচের বাটনে ক্লিক করুন। একটি নতুন পেইজ ওপেন হবে, সেখানে <b>১৫ সেকেন্ড</b> অপেক্ষা করুন। এরপর অটোমেটিক আপনার বটের ইনবক্সে মুভি চলে যাবে!");
                }
            }
            
            function watchAdForCoins() {
                document.getElementById('checkinModal').style.display = 'none';
                onAdCompleteCallback = () => claimAdReward();
                showDirectLinkModal("৫ কয়েন ফ্রী পেতে নিচের বাটনে ক্লিক করুন। একটি নতুন পেইজ ওপেন হবে, সেখানে <b>১৫ সেকেন্ড</b> অপেক্ষা করুন। এরপর আপনার ব্যালেন্সে কয়েন যোগ হয়ে যাবে!");
            }
            
            async function claimAdReward() {
                try {
                    const res = await fetch('/api/reward_ad', { 
                        method: 'POST', headers: {'Content-Type': 'application/json'}, 
                        body: JSON.stringify({uid: uid, initData: INIT_DATA})
                    });
                    const data = await res.json();
                    if(data.ok) { tg.showAlert("🎉 অভিনন্দন! আপনি লিংক ভিজিট করে ৫ কয়েন পেয়েছেন!"); fetchUserInfo(); loadTasks(); }
                } catch(e) {}
            }

            async function sendFile(id) {
                try {
                    const res = await fetch('/api/send', { 
                        method: 'POST', headers: {'Content-Type': 'application/json'}, 
                        body: JSON.stringify({userId: uid, movieId: id, initData: INIT_DATA})
                    });
                    const responseData = await res.json();
                    if(!responseData.ok) return alert("⚠️ Security verification failed!");
                    
                    if (isUserVip) {
                        document.getElementById('successNoticeBox').innerHTML = `<p style="color:#4ade80;"><i class="fa-solid fa-crown" style="color: #fbbf24;"></i> <b>VIP সুবিধা:</b> এই ফাইলটি আপনার ইনবক্স থেকে কখনো অটো-ডিলিট হবে না। সারাজীবন সেভ থাকবে!</p>`;
                        document.getElementById('successNoticeBox').style.background = "linear-gradient(135deg, rgba(74,222,128,0.1), rgba(34,197,94,0.15))";
                        document.getElementById('successNoticeBox').style.borderLeftColor = "#4ade80";
                    }

                    document.getElementById('successModal').style.display = 'flex';
                    setTimeout(() => { loadTrending(); loadMovies(currentPage); }, 1000); 
                    fetchUserInfo(); 
                } catch (e) {}
            }
            
            // --- REQUEST & UPVOTE LOGIC ---
            function openReqModal() { 
                closeMenu();
                document.getElementById('reqModal').style.display = 'flex'; 
                document.getElementById('reqText').focus(); 
                loadRequests();
            }
            
            async function loadRequests() {
                const reqList = document.getElementById('reqList');
                reqList.innerHTML = "<p style='color:gray; text-align:center;'>Loading...</p>";
                try {
                    const res = await fetch('/api/requests');
                    const data = await res.json();
                    if(data.length===0) return reqList.innerHTML = "<p style='color:gray; text-align:center;'>কোনো রিকোয়েস্ট নেই।</p>";
                    reqList.innerHTML = data.map(r => {
                        let hasVoted = r.voters.includes(uid);
                        let btnCls = hasVoted ? "background:#475569;" : "background:#3b82f6;";
                        let delBtn = isAdmin ? `<button onclick="deleteReq('${r.id}')" style="background:#ef4444; border:none; padding:6px 10px; border-radius:6px; color:white; cursor:pointer; margin-left:5px;"><i class="fa-solid fa-trash"></i></button>` : '';
                        return `
                        <div class="req-item">
                            <span style="color:white; font-weight:bold; flex:1;">${r.movie}</span>
                            <div>
                                <button class="vote-btn" style="${btnCls}" ${hasVoted?'disabled':''} onclick="voteRequest('${r.id}')">
                                    <i class="fa-solid fa-caret-up"></i> ${r.votes}
                                </button>
                                ${delBtn}
                            </div>
                        </div>
                        `;
                    }).join('');
                } catch(e) {}
            }
            
            async function voteRequest(id) {
                try {
                    await fetch('/api/requests/vote', { 
                        method: 'POST', headers: {'Content-Type': 'application/json'}, 
                        body: JSON.stringify({uid: uid, req_id: id, initData: INIT_DATA})
                    });
                    loadRequests();
                } catch (e) {}
            }
            
            async function deleteReq(id) {
                if(!confirm('আপনি কি নিশ্চিত যে এই রিকোয়েস্টটি ডিলিট করতে চান?')) return;
                try {
                    await fetch('/api/requests/' + id, { method: 'DELETE' });
                    loadRequests();
                } catch(e) {}
            }
            
            async function sendReq() {
                const text = document.getElementById('reqText').value;
                if(!text) return alert('মুভির নাম লিখুন!');
                try {
                    await fetch('/api/request', { 
                        method: 'POST', headers: {'Content-Type': 'application/json'}, 
                        body: JSON.stringify({uid: uid, uname: tg.initDataUnsafe.user?.first_name || 'Guest', movie: text, initData: INIT_DATA})
                    });
                    document.getElementById('reqText').value = '';
                    tg.showAlert('রিকোয়েস্ট বা ভোট সফলভাবে যোগ করা হয়েছে!');
                    loadRequests();
                } catch (e) {}
            }

            // --- Chat Logic ---
            let chatInterval = null;

            async function fetchChatMessages() {
                try {
                    const res = await fetch('/api/chat');
                    const data = await res.json();
                    const chatBox = document.getElementById('chatBox');
                    
                    let html = "";
                    data.forEach(msg => {
                        const isMine = msg.uid === uid;
                        html += `
                            <div class="chat-msg ${isMine ? 'mine' : 'others'}">
                                ${!isMine ? `<div class="chat-name">${msg.name}</div>` : ''}
                                ${msg.text}
                                ${!isMine ? `<span class="chat-reply-btn" onclick="replyToChat('${msg.name}')"><i class="fa-solid fa-reply"></i></span>` : ''}
                            </div>
                        `;
                    });
                    
                    const isScrolledToBottom = chatBox.scrollHeight - chatBox.clientHeight <= chatBox.scrollTop + 10;
                    chatBox.innerHTML = html;
                    if(isScrolledToBottom) chatBox.scrollTop = chatBox.scrollHeight;
                    
                } catch(e) {}
            }
            
            function replyToChat(name) {
                const input = document.getElementById('chatInput');
                input.value = `@${name} ` + input.value;
                input.focus();
            }

            function openChatModal() {
                closeMenu();
                document.getElementById('chatModal').style.display = 'flex';
                document.getElementById('chatBox').innerHTML = "<p style='color:gray; text-align:center;'>Loading chat...</p>";
                fetchChatMessages();
                chatInterval = setInterval(fetchChatMessages, 3000);
            }
            
            function closeChat() {
                document.getElementById('chatModal').style.display='none';
                if(chatInterval) { clearInterval(chatInterval); chatInterval = null; }
            }

            async function sendChatMessage() {
                const input = document.getElementById('chatInput');
                const text = input.value.trim();
                if(!text) return;
                
                input.value = "";
                const chatBox = document.getElementById('chatBox');
                
                const div = document.createElement('div');
                div.className = `chat-msg mine`;
                div.innerHTML = text;
                chatBox.appendChild(div);
                chatBox.scrollTop = chatBox.scrollHeight;

                try {
                    await fetch('/api/chat', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({uid: uid, name: document.getElementById('uName').innerText, text: text, initData: INIT_DATA})
                    });
                    fetchChatMessages();
                } catch(e) {}
            }

            // --- Spin To Win Logic ---
            const spinRewards = [10, 50, 0, 100, 20, 0, 500, 5];
            let currentRotation = 0;
            let spinsLeftToday = 3;

            function renderWheel() {
                const wheel = document.getElementById('spinWheel');
                wheel.innerHTML = '';
                const sliceAngle = 360 / 8;
                for(let i=0; i<8; i++) {
                    const textDiv = document.createElement('div');
                    textDiv.style.position = 'absolute';
                    textDiv.style.width = '40px'; 
                    textDiv.style.height = '50%'; 
                    textDiv.style.top = '0';
                    textDiv.style.left = '50%';
                    textDiv.style.marginLeft = '-20px';
                    textDiv.style.transformOrigin = '50% 100%';
                    textDiv.style.transform = `rotate(${i * sliceAngle + (sliceAngle/2)}deg)`;
                    textDiv.style.fontWeight = '900';
                    textDiv.style.fontSize = '18px';
                    textDiv.style.color = '#000';
                    textDiv.style.textAlign = 'center';
                    textDiv.style.paddingTop = '15px'; 
                    textDiv.innerHTML = spinRewards[i] ? spinRewards[i]+'🪙' : 'Oops!';
                    wheel.appendChild(textDiv);
                }
            }
            renderWheel();

            async function openSpinModal() {
                closeMenu();
                document.getElementById('spinModal').style.display = 'flex';
                try {
                    const res = await fetch('/api/spin/status/' + uid);
                    const data = await res.json();
                    spinsLeftToday = data.spins_left;
                    document.getElementById('spinsLeftText').innerText = spinsLeftToday;
                    document.getElementById('spinBtn').disabled = spinsLeftToday <= 0;
                } catch(e) {}
            }

            function startSpin() {
                if(spinsLeftToday <= 0) return tg.showAlert("আপনি আজকের স্পিন লিমিট শেষ করেছেন!");
                document.getElementById('spinModal').style.display = 'none';
                
                onAdCompleteCallback = () => executeSpin();
                showDirectLinkModal("স্পিন আনলক করতে নিচের বাটনে ক্লিক করুন। একটি নতুন পেইজ ওপেন হবে, সেখানে <b>১৫ সেকেন্ড</b> অপেক্ষা করুন। এরপর অটোমেটিক স্পিন হুইল চালু হয়ে যাবে!");
            }

            async function executeSpin() {
                document.getElementById('spinModal').style.display = 'flex';
                document.getElementById('spinBtn').disabled = true;
                
                const winIndex = Math.floor(Math.random() * 8);
                const reward = spinRewards[winIndex];
                
                const sliceAngle = 360 / 8;
                const targetAngle = 360 - (winIndex * sliceAngle + (sliceAngle / 2));
                
                let base = Math.floor(currentRotation / 360) * 360;
                currentRotation = base + (360 * 5) + targetAngle;
                
                document.getElementById('spinWheel').style.transform = `rotate(${currentRotation}deg)`;
                
                setTimeout(async () => {
                    try {
                        const res = await fetch('/api/spin', {
                            method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({uid: uid, reward: reward, initData: INIT_DATA})
                        });
                        const data = await res.json();
                        if(data.ok) {
                            spinsLeftToday = data.spins_left;
                            document.getElementById('spinsLeftText').innerText = spinsLeftToday;
                            if(reward > 0) { tg.showAlert(`🎉 অভিনন্দন! আপনি ${reward} কয়েন জিতেছেন!`); fetchUserInfo(); }
                            else { tg.showAlert(`😔 Better luck next time!`); }
                        } else { tg.showAlert(data.msg); }
                    } catch(e) {}
                    document.getElementById('spinBtn').disabled = spinsLeftToday <= 0;
                }, 4100);
            }

            // --- Daily Tasks Logic ---
            async function openTasksModal() {
                closeMenu();
                document.getElementById('tasksModal').style.display = 'flex';
                loadTasks();
            }

            async function loadTasks() {
                try {
                    const res = await fetch('/api/tasks/' + uid);
                    const data = await res.json();
                    
                    let adCount = Math.min(data.ads, 3);
                    let revCount = Math.min(data.reviews, 2);
                    
                    document.getElementById('adTaskProgress').innerText = `${adCount}/3`;
                    document.getElementById('adTaskBar').style.width = `${(adCount/3)*100}%`;
                    const adBtn = document.getElementById('adTaskBtn');
                    
                    if(data.ads_claimed) { adBtn.innerText = "Claimed ✅"; adBtn.className = "task-btn claimed"; adBtn.disabled = true; }
                    else if(adCount >= 3) { adBtn.innerText = "Claim 15 Coins!"; adBtn.className = "task-btn"; adBtn.disabled = false; }
                    else { adBtn.innerText = "15 Coins Claim করুন"; adBtn.className = "task-btn"; adBtn.disabled = true; }
                    
                    document.getElementById('revTaskProgress').innerText = `${revCount}/2`;
                    document.getElementById('revTaskBar').style.width = `${(revCount/2)*100}%`;
                    const revBtn = document.getElementById('revTaskBtn');
                    
                    if(data.reviews_claimed) { revBtn.innerText = "Claimed ✅"; revBtn.className = "task-btn claimed"; revBtn.disabled = true; }
                    else if(revCount >= 2) { revBtn.innerText = "Claim 10 Coins!"; revBtn.className = "task-btn"; revBtn.disabled = false; }
                    else { revBtn.innerText = "10 Coins Claim করুন"; revBtn.className = "task-btn"; revBtn.disabled = true; }
                    
                } catch(e) {}
            }

            fetchUserInfo(); loadTrending(); loadUpcoming(); loadMovies(1); 
        </script>
    </body>
    </html>
    """
    html_code = html_code.replace("{{DIRECT_LINKS}}", dl_json).replace("{{ZONE_ID}}", zone_id).replace("{{TG_LINK}}", tg_url).replace("{{LINK_18}}", link_18).replace("{{AD_COUNT}}", str(required_ads)).replace("{{BOT_USER}}", BOT_USERNAME).replace("{{BKASH_NO}}", bkash_no).replace("{{NAGAD_NO}}", nagad_no)
    return html_code


# ==========================================
# 14. Main Web App APIs
# ==========================================
@app.get("/api/user/{uid}")
async def get_user_info(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user: return {"vip": False, "is_admin": False, "refer_count": 0, "vip_expiry": None, "coins": 0, "badges": []}
    
    vip_until = user.get("vip_until")
    now = datetime.datetime.utcnow()
    is_vip = False
    vip_expiry_str = None
    
    if vip_until and vip_until > now:
        is_vip = True
        vip_expiry_str = vip_until.strftime("%d %b %Y")
        
    badges = []
    refer_count = user.get("refer_count", 0)
    unlocks = await db.user_unlocks.count_documents({"user_id": uid})
    reviews_count = await db.reviews.count_documents({"user_id": uid})
    
    if refer_count >= 5: badges.append("🤝 Top Referrer")
    if unlocks >= 5: badges.append("🍿 Binge Watcher")
    if reviews_count >= 3: badges.append("✍️ Top Critic")
        
    return {
        "vip": is_vip,
        "is_admin": uid in admin_cache,
        "refer_count": refer_count,
        "vip_expiry": vip_expiry_str,
        "coins": user.get("coins", 0),
        "badges": badges
    }

async def update_daily_task(uid: int, task_type: str):
    now_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    user = await db.users.find_one({"user_id": uid})
    if not user: return
    
    tasks = user.get("tasks", {})
    if tasks.get("date") != now_date:
        tasks = {"date": now_date, "ads": 0, "reviews": 0, "ads_claimed": False, "reviews_claimed": False}
    
    if task_type in tasks:
        tasks[task_type] += 1
        await db.users.update_one({"user_id": uid}, {"$set": {"tasks": tasks}})

class ReviewModel(BaseModel):
    uid: int
    name: str
    title: str
    rating: int
    comment: str
    initData: str

@app.get("/api/reviews/{title}")
async def get_reviews(title: str):
    reviews = await db.reviews.find({"movie_title": title}).sort("created_at", -1).to_list(15)
    return [{"name": r["name"], "rating": r["rating"], "comment": r["comment"]} for r in reviews]

@app.post("/api/reviews")
async def add_review(data: ReviewModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    await db.reviews.insert_one({
        "user_id": data.uid, "name": data.name, "movie_title": data.title,
        "rating": data.rating, "comment": data.comment, "created_at": datetime.datetime.utcnow()
    })
    await update_daily_task(data.uid, "reviews")
    return {"ok": True}

class CheckinModel(BaseModel):
    uid: int
    action: str
    initData: str

@app.post("/api/checkin")
async def handle_checkin(data: CheckinModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    user = await db.users.find_one({"user_id": data.uid})
    if not user: return {"ok": False}
    
    now = datetime.datetime.utcnow()
    
    if data.action == "claim":
        last_checkin = user.get("last_checkin", now - datetime.timedelta(days=2))
        if last_checkin.date() >= now.date():
            return {"ok": False, "msg": "আপনি ইতিমধ্যে আজকের রিওয়ার্ড নিয়ে নিয়েছেন!"}
        await db.users.update_one({"user_id": data.uid}, {"$inc": {"coins": 10}, "$set": {"last_checkin": now}})
        return {"ok": True}
        
    elif data.action == "convert":
        coins = user.get("coins", 0)
        if coins < 50: return {"ok": False, "msg": "আপনার কমপক্ষে ৫০ কয়েন প্রয়োজন!"}
        
        current_vip = user.get("vip_until", now)
        if current_vip < now: current_vip = now
        new_vip = current_vip + datetime.timedelta(days=1)
        
        await db.users.update_one({"user_id": data.uid}, {"$inc": {"coins": -50}, "$set": {"vip_until": new_vip}})
        return {"ok": True}

class PaymentModel(BaseModel):
    uid: int
    method: str
    trx_id: str
    days: int
    price: int
    initData: str

@app.post("/api/payment/submit")
async def submit_payment(data: PaymentModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    
    existing = await db.payments.find_one({"trx_id": data.trx_id})
    if existing: return {"ok": False, "msg": "এই TrxID টি ইতিমধ্যে ব্যবহার করা হয়েছে!"}
    
    pay_doc = {
        "user_id": data.uid,
        "method": data.method,
        "trx_id": data.trx_id,
        "amount": data.price,
        "days": data.days,
        "status": "pending",
        "created_at": datetime.datetime.utcnow()
    }
    res = await db.payments.insert_one(pay_doc)
    
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Approve", callback_data=f"trx_approve_{res.inserted_id}")
        builder.button(text="❌ Reject", callback_data=f"trx_reject_{res.inserted_id}")
        
        pkg_name = f"{data.days} দিনের"
        if data.days == 30: pkg_name = "১ মাসের"
        elif data.days == 90: pkg_name = "৩ মাসের"
        elif data.days == 180: pkg_name = "৬ মাসের"
        
        msg = f"💰 <b>নতুন পেমেন্ট রিকোয়েস্ট!</b>\n\n👤 ইউজার ID: <code>{data.uid}</code>\n🏦 মেথড: {data.method.upper()}\n🧾 TrxID: <code>{data.trx_id}</code>\n💵 পরিমাণ: {data.price} BDT\n⏳ প্যাকেজ: {pkg_name} VIP"
        await bot.send_message(OWNER_ID, msg, parse_mode="HTML", reply_markup=builder.as_markup())
    except Exception: pass
    
    return {"ok": True}

@app.get("/api/trending")
async def trending_movies(uid: int = 0):
    if uid in banned_cache: return {"error": "banned"}
    unlocked_movie_ids = []
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            unlocked_movie_ids.append(u["movie_id"])

    pipeline = [
        {"$group": {
            "_id": "$title", 
            "photo_id": {"$first": "$photo_id"}, 
            "clicks": {"$sum": "$clicks"}, 
            "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main File"]}}}
        }},
        {"$sort": {"clicks": -1}}, {"$limit": 10}
    ]
    movies = await db.movies.aggregate(pipeline).to_list(10)
    for m in movies:
        for f in m["files"]:
            f["is_unlocked"] = f["id"] in unlocked_movie_ids
    return movies

@app.get("/api/upcoming")
async def upcoming_movies():
    pipeline = [{"$sort": {"added_at": -1}}, {"$limit": 10}]
    movies = await db.upcoming.aggregate(pipeline).to_list(10)
    return [{"photo_id": m["photo_id"], "title": m.get("title", "")} for m in movies]

@app.get("/api/list")
async def list_movies(page: int = 1, q: str = "", uid: int = 0):
    if uid in banned_cache: return {"error": "banned"}
    limit = 16
    skip = (page - 1) * limit
    unlocked_ids = []
    
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            unlocked_ids.append(u["movie_id"])

    match_stage = {"title": {"$regex": q, "$options": "i"}} if q else {}
    pipeline = [
        {"$match": match_stage},
        {"$group": {
            "_id": "$title", 
            "photo_id": {"$first": "$photo_id"}, 
            "clicks": {"$sum": "$clicks"}, 
            "created_at": {"$max": "$created_at"}, 
            "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main File"]}}}
        }},
        {"$sort": {"created_at": -1}}, {"$skip": skip}, {"$limit": limit}
    ]
    count_pipe = [{"$match": match_stage}, {"$group": {"_id": "$title"}}, {"$count": "total"}]
    c_res = await db.movies.aggregate(count_pipe).to_list(1)
    total_groups = c_res[0]["total"] if c_res else 0
    total_pages = (total_groups + limit - 1) // limit

    movies = await db.movies.aggregate(pipeline).to_list(limit)
    for m in movies:
        for f in m["files"]:
            f["is_unlocked"] = f["id"] in unlocked_ids
    return {"movies": movies, "total_pages": total_pages}

@app.get("/api/image/{photo_id}")
async def get_image(photo_id: str):
    try:
        cache = await db.file_cache.find_one({"photo_id": photo_id})
        now = datetime.datetime.utcnow()
        if cache and cache.get("expires_at", now) > now:
            file_path = cache["file_path"]
        else:
            file_info = await bot.get_file(photo_id)
            file_path = file_info.file_path
            await db.file_cache.update_one({"photo_id": photo_id}, {"$set": {"file_path": file_path, "expires_at": now + datetime.timedelta(minutes=50)}}, upsert=True)
            
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
            now = datetime.datetime.utcnow()
            user_data = await db.users.find_one({"user_id": d.userId})
            is_vip = False
            if user_data and user_data.get("vip_until", now) > now: is_vip = True

            time_cfg = await db.settings.find_one({"id": "del_time"})
            del_minutes = time_cfg['minutes'] if time_cfg else 60
            protect_cfg = await db.settings.find_one({"id": "protect_content"})
            is_protected = protect_cfg['status'] if protect_cfg else True
            q_text = m.get("quality", "")
            title_text = f"{m['title']} [{q_text}]" if q_text else m['title']
            
            if is_vip:
                caption = (f"🎥 <b>{title_text}</b>\n\n🌟 <b>VIP সুবিধা:</b> এই ফাইলটি আপনার ইনবক্স থেকে কখনো অটো-ডিলিট হবে না।\n\n📥 Join: https://t.me/lifetimebackup2026")
            else:
                caption = (f"🎥 <b>{title_text}</b>\n\n⏳ <b>সতর্কতা:</b> কপিরাইট এড়াতে মুভিটি <b>{del_minutes} মিনিট</b> পর অটো-ডিলিট হয়ে যাবে। "
                           f"দয়া করে এখনই ফরওয়ার্ড বা সেভ করে নিন!\n\n📥 Join: https://t.me/lifetimebackup2026")
            
            if m.get("file_type") == "video": sent_msg = await bot.send_video(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            else: sent_msg = await bot.send_document(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            
            await db.movies.update_one({"_id": ObjectId(d.movieId)}, {"$inc": {"clicks": 1}})
            await db.user_unlocks.update_one({"user_id": d.userId, "movie_id": d.movieId}, {"$set": {"unlocked_at": now}}, upsert=True)
            
            if sent_msg and not is_vip:
                delete_at = now + datetime.timedelta(minutes=del_minutes)
                await db.auto_delete.insert_one({"chat_id": d.userId, "message_id": sent_msg.message_id, "delete_at": delete_at})
    except Exception: pass
    return {"ok": True}

@app.get("/api/leaderboard")
async def get_leaderboard():
    users = await db.users.find().sort("refer_count", -1).limit(10).to_list(10)
    return [{"name": u.get("first_name", "User"), "refers": u.get("refer_count", 0)} for u in users]

class AdRewardModel(BaseModel):
    uid: int
    initData: str

@app.post("/api/reward_ad")
async def reward_ad(data: AdRewardModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    await db.users.update_one({"user_id": data.uid}, {"$inc": {"coins": 5}})
    await update_daily_task(data.uid, "ads")
    return {"ok": True}

@app.get("/api/requests")
async def get_requests():
    reqs = await db.requests.find().sort("votes", -1).limit(20).to_list(20)
    return [{"id": str(r["_id"]), "movie": r["movie"], "votes": r["votes"], "voters": r.get("voters", [])} for r in reqs]

class VoteModel(BaseModel):
    uid: int
    req_id: str
    initData: str

@app.post("/api/requests/vote")
async def vote_request(data: VoteModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    req = await db.requests.find_one({"_id": ObjectId(data.req_id)})
    if not req: return {"ok": False}
    if data.uid in req.get("voters", []): return {"ok": False, "msg": "Already voted"}
    
    await db.requests.update_one({"_id": ObjectId(data.req_id)}, {"$inc": {"votes": 1}, "$push": {"voters": data.uid}})
    return {"ok": True}

@app.delete("/api/requests/{req_id}")
async def delete_request(req_id: str):
    await db.requests.delete_one({"_id": ObjectId(req_id)})
    return {"ok": True}

class ReqModel(BaseModel):
    uid: int
    uname: str
    movie: str
    initData: str

@app.post("/api/request")
async def handle_request(data: ReqModel):
    if data.uid in banned_cache or not validate_tg_data(data.initData): return {"ok": False}
    
    existing = await db.requests.find_one({"movie": {"$regex": f"^{data.movie}$", "$options": "i"}})
    if existing:
        if data.uid not in existing.get("voters", []):
            await db.requests.update_one({"_id": existing["_id"]}, {"$inc": {"votes": 1}, "$push": {"voters": data.uid}})
    else:
        res = await db.requests.insert_one({
            "user_id": data.uid, "uname": data.uname, "movie": data.movie,
            "votes": 1, "voters": [data.uid], "created_at": datetime.datetime.utcnow()
        })
        req_id = str(res.inserted_id)
        
        try: 
            now = datetime.datetime.utcnow()
            user_data = await db.users.find_one({"user_id": data.uid})
            is_vip = False
            if user_data and user_data.get("vip_until", now) > now: is_vip = True
                
            vip_text = "🌟 <b>[VIP Member]</b>" if is_vip else "👤 [Free User]"
            
            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Approve", callback_data=f"req_acc_{req_id}")
            builder.button(text="❌ Reject", callback_data=f"req_rej_{req_id}")
            builder.button(text="✍️ রিপ্লাই", callback_data=f"reply_{data.uid}")
            
            await bot.send_message(OWNER_ID, f"🔔 <b>নতুন মুভি রিকোয়েস্ট!</b>\n\n{vip_text}\nইউজার: {data.uname} (<code>{data.uid}</code>)\n🎬 মুভির নাম: <b>{data.movie}</b>", parse_mode="HTML", reply_markup=builder.as_markup())
        except Exception: pass
    
    return {"ok": True}

class ChatMsgModel(BaseModel):
    uid: int
    name: str
    text: str
    initData: str

@app.get("/api/chat")
async def get_chat():
    msgs = await db.chat.find().sort("timestamp", -1).limit(50).to_list(50)
    msgs.reverse()
    return [{"uid": m["uid"], "name": m.get("name", "User"), "text": m["text"]} for m in msgs]

@app.post("/api/chat")
async def post_chat(data: ChatMsgModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    await db.chat.insert_one({
        "uid": data.uid,
        "name": data.name,
        "text": data.text,
        "timestamp": datetime.datetime.utcnow()
    })
    return {"ok": True}

class SpinModel(BaseModel):
    uid: int
    reward: int
    initData: str

@app.get("/api/spin/status/{uid}")
async def get_spin_status(uid: int):
    now_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    user = await db.users.find_one({"user_id": uid})
    if not user: return {"spins_left": 0}
    
    spin_data = user.get("spin", {"date": "", "count": 0})
    if spin_data["date"] != now_date:
        return {"spins_left": 3}
    return {"spins_left": max(0, 3 - spin_data["count"])}

@app.post("/api/spin")
async def handle_spin(data: SpinModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    now_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    user = await db.users.find_one({"user_id": data.uid})
    
    spin_data = user.get("spin", {"date": "", "count": 0})
    if spin_data["date"] != now_date:
        spin_data = {"date": now_date, "count": 0}
        
    if spin_data["count"] >= 3:
        return {"ok": False, "msg": "আপনি আজকের স্পিন লিমিট শেষ করেছেন!"}
        
    spin_data["count"] += 1
    await db.users.update_one(
        {"user_id": data.uid}, 
        {"$set": {"spin": spin_data}, "$inc": {"coins": data.reward}}
    )
    return {"ok": True, "spins_left": max(0, 3 - spin_data["count"])}

class TaskClaimModel(BaseModel):
    uid: int
    task_type: str
    initData: str

@app.get("/api/tasks/{uid}")
async def get_tasks(uid: int):
    now_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    user = await db.users.find_one({"user_id": uid})
    if not user: return {"ads": 0, "reviews": 0, "ads_claimed": False, "reviews_claimed": False}
    
    tasks = user.get("tasks", {})
    if tasks.get("date") != now_date:
        return {"ads": 0, "reviews": 0, "ads_claimed": False, "reviews_claimed": False}
    return tasks

@app.post("/api/tasks/claim")
async def claim_task(data: TaskClaimModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    now_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    user = await db.users.find_one({"user_id": data.uid})
    
    tasks = user.get("tasks", {})
    if tasks.get("date") != now_date:
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
    
    print("Connecting to Telegram Bot API...")
    await bot.delete_webhook(drop_pending_updates=True)
    
    print("Server is Running!")
    await asyncio.gather(server.serve(), dp.start_polling(bot))

if __name__ == "__main__": 
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start())
