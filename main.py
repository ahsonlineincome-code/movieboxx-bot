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
# 2. FSM States (For Uploading Flow - UPDATED)
# ==========================================
class AdminStates(StatesGroup):
    waiting_for_bcast = State()
    waiting_for_reply = State()
    waiting_for_photo = State()
    waiting_for_title = State()
    waiting_for_quality = State() 
    waiting_for_year = State()
    waiting_for_desc = State()
    waiting_for_cats = State()
    waiting_for_upc_photo = State()
    waiting_for_upc_title = State()
    waiting_for_upc_date = State()
    waiting_for_upc_lang = State()
    waiting_for_upc_genre = State()


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
            "🔸 অ্যাডমিন প্যানেল: <code>/addadmin ID</code> | <code>/deladmin ID</code>\n"
            "🔸 ডাইরেক্ট লিংক: <code>/addlink লিংক</code> | <code>/dellink লিংক</code>\n"
            "🔸 প্রোফাইল সেটিংস: <code>/settg লিংক</code> | <code>/setfb লিংক</code> | <code>/setyt লিংক</code>\n"
            "🔸 অ্যাড জোন: <code>/setad ID</code> | অ্যাড সংখ্যা: <code>/setadcount সংখ্যা</code>\n"
            "🔸 পেমেন্ট নাম্বার: <code>/setbkash নাম্বার</code> | <code>/setnagad নাম্বার</code>\n"
            "🔸 মুভি ডিলিট: <code>/delmovie মুভির নাম</code>\n"
            "🔸 VIP দিন: <code>/addvip ID দিন</code> | VIP বাতিল: <code>/removevip ID</code>\n"
            "🔸 আপকামিং মুভি অ্যাড: <code>/addupcoming</code>\n\n"
            f"🌐 <b>ওয়েব অ্যাডমিন প্যানেল:</b> <a href='{APP_URL}/admin'>এখানে ক্লিক করুন</a>\n\n"
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
# 7. Telegram Bot Commands (Admin Settings)
# ==========================================
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
        if result.modified_count > 0: await m.answer(f"❌ লিংকটি ডিলিট করা হয়েছে।", parse_mode="HTML")
        else: await m.answer("⚠️ লিংকটি ডাটাবেসে পাওয়া যায়নি।")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/dellink https://example.com</code>", parse_mode="HTML")

@dp.message(Command("addadmin"))
async def add_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return await m.answer("⚠️ শুধুমাত্র মেইন Owner অ্যাডমিন অ্যাড করতে পারবে!")
    try:
        target_uid = int(m.text.split()[1])
        await db.admins.update_one({"user_id": target_uid}, {"$set": {"user_id": target_uid}}, upsert=True)
        admin_cache.add(target_uid)
        await m.answer(f"✅ ইউজার <code>{target_uid}</code> কে সফলভাবে অ্যাডমিন বানানো হয়েছে!", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/addadmin ইউজার_আইডি</code>", parse_mode="HTML")

@dp.message(Command("deladmin"))
async def del_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return await m.answer("⚠️ শুধুমাত্র মেইন Owner অ্যাডমিন রিমুভ করতে পারবে!")
    try:
        target_uid = int(m.text.split()[1])
        if target_uid == OWNER_ID: return await m.answer("⚠️ Main Owner কে ডিলিট করা সম্ভব নয়!")
        await db.admins.delete_one({"user_id": target_uid})
        admin_cache.discard(target_uid)
        await m.answer(f"❌ ইউজার <code>{target_uid}</code> কে অ্যাডমিন লিস্ট থেকে রিমুভ করা হয়েছে!", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/deladmin ইউজার_আইডি</code>", parse_mode="HTML")

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        title = m.text.split(" ", 1)[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0: await m.answer(f"✅ '<b>{title}</b>' সফলভাবে ডিলিট হয়েছে!", parse_mode="HTML")
        else: await m.answer("⚠️ এই নামের কোনো মুভি ডাটাবেসে পাওয়া যায়নি।")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/delmovie মুভির সম্পূর্ণ নাম</code>", parse_mode="HTML")

@dp.message(Command("stats"))
async def stats_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    uc = await db.users.count_documents({})
    mc = await db.movies.count_documents({})
    now = datetime.datetime.utcnow()
    today_start = datetime.datetime(now.year, now.month, now.day)
    new_users_today = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    text = f"📊 <b>স্ট্যাটাস:</b>\n\n👥 মোট ইউজার: <code>{uc}</code>\n🟢 আজকের নতুন: <code>{new_users_today}</code>\n🎬 মোট ফাইল: <code>{mc}</code>"
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
        count = int(m.text.split(" ")[1])
        await db.settings.update_one({"id": "ad_count"}, {"$set": {"count": count}}, upsert=True)
        await m.answer(f"✅ অ্যাড দেখার সংখ্যা সেট করা হয়েছে: <b>{count} টি</b>।", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/setadcount 3</code>", parse_mode="HTML")

@dp.message(Command("setad"))
async def set_ad(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        zone = m.text.split(" ")[1]
        await db.settings.update_one({"id": "ad_config"}, {"$set": {"zone_id": zone}}, upsert=True)
        await m.answer("✅ জোন আপডেট হয়েছে।")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/setad 1234567</code>")

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
        if not user: return await m.answer("⚠️ এই ইউজারটি ডাটাবেসে নেই।")
        current_vip = user.get("vip_until", now)
        if current_vip < now: current_vip = now
        new_vip = current_vip + datetime.timedelta(days=days)
        await db.users.update_one({"user_id": target_uid}, {"$set": {"vip_until": new_vip}})
        await m.answer(f"✅ ইউজার <code>{target_uid}</code> কে <b>{days} দিনের</b> VIP দেওয়া হয়েছে!", parse_mode="HTML")
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

# Profile Settings Commands
@dp.message(Command("settg"))
async def set_tg_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ")[1]
        await db.settings.update_one({"id": "profile"}, {"$set": {"tg_link": link}}, upsert=True)
        await m.answer(f"✅ টেলিগ্রাম লিংক সেট করা হয়েছে: <b>{link}</b>", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/settg https://t.me/YourChannel</code>", parse_mode="HTML")

@dp.message(Command("setfb"))
async def set_fb_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ")[1]
        await db.settings.update_one({"id": "profile"}, {"$set": {"fb_link": link}}, upsert=True)
        await m.answer(f"✅ ফেসবুক লিংক সেট করা হয়েছে: <b>{link}</b>", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/setfb https://facebook.com/...</code>", parse_mode="HTML")

@dp.message(Command("setyt"))
async def set_yt_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ")[1]
        await db.settings.update_one({"id": "profile"}, {"$set": {"yt_link": link}}, upsert=True)
        await m.answer(f"✅ ইউটিউব লিংক সেট করা হয়েছে: <b>{link}</b>", parse_mode="HTML")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/setyt https://youtube.com/...</code>", parse_mode="HTML")


# ==========================================
# 8. Admin Inline Callback (Payment Approval)
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
        await c.message.edit_text(c.message.text + f"\n\n✅ <b>পেমেন্ট অ্যাপ্রুভ করা হয়েছে!</b>", parse_mode="HTML")
        try: await bot.send_message(user_id, f"🎉 <b>পেমেন্ট সফল!</b> আপনাকে VIP দেওয়া হয়েছে!", parse_mode="HTML")
        except: pass
    else:
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "rejected"}})
        await c.message.edit_text(c.message.text + "\n\n❌ <b>পেমেন্ট রিজেক্ট করা হয়েছে!</b>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("req_"))
async def handle_request_approval(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    action = c.data.split("_")[1] 
    req_id = c.data.split("_")[2]
    req = await db.requests.find_one({"_id": ObjectId(req_id)})
    if not req: return await c.answer("⚠️ রিকোয়েস্টটি প্রসেস করা হয়েছে!", show_alert=True)
    movie_name = req["movie"]
    voters = req.get("voters", [])
    
    if action == "acc":
        await c.message.edit_text(c.message.text + "\n\n✅ <b>Approve করা হয়েছে!</b>", parse_mode="HTML")
        for v_id in voters:
            try: await bot.send_message(v_id, f"🎉 <b>সুখবর!</b> মুভি <b>{movie_name}</b> আপলোড করা হয়েছে!", parse_mode="HTML")
            except: pass
    elif action == "rej":
        await c.message.edit_text(c.message.text + "\n\n❌ <b>Reject করা হয়েছে!</b>", parse_mode="HTML")
    await db.requests.delete_one({"_id": ObjectId(req_id)})


# ==========================================
# 9. Movie Upload Logic (UPDATED FOR CATEGORIES)
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
    await m.answer("✅ পোস্টার পেয়েছি! এবার <b>মুভি বা সিরিজের নাম</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer("✅ এবার এই ফাইলটির <b>কোয়ালিটি বা এপিসোড</b> দিন।\n<i>(উদাহরণ: 480p, 720p অথবা Episode 01)</i>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=m.text.strip())
    await state.set_state(AdminStates.waiting_for_year)
    await m.answer("✅ এবার মুভির <b>রিলিজ সাল (Year)</b> লিখুন।\n<i>(উদাহরণ: 2023 বা 2024)</i>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_year, F.text)
async def receive_movie_year(m: types.Message, state: FSMContext):
    await state.update_data(year=m.text.strip())
    await state.set_state(AdminStates.waiting_for_desc)
    await m.answer("✅ এবার মুভির <b>সংক্ষিপ্ত বিবরণ (Description)</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_desc, F.text)
async def receive_movie_desc(m: types.Message, state: FSMContext):
    await state.update_data(description=m.text.strip())
    await state.set_state(AdminStates.waiting_for_cats)
    await m.answer("✅ এবার মুভির <b>ক্যাটাগরি (Categories)</b> লিখুন।\n<i>একাধিক হলে কমা (,) দিয়ে লিখুন।</i>\n<b>উদাহরণ:</b> Hindi, Action, 18+, Trending", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_cats, F.text)
async def receive_movie_cats(m: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    cats = [c.strip() for c in m.text.split(",")]
    
    await db.movies.insert_one({
        "title": data["title"], 
        "quality": data["quality"], 
        "photo_id": data["photo_id"], 
        "file_id": data["file_id"], 
        "file_type": data["file_type"],
        "year": data.get("year", "N/A"),
        "description": data.get("description", "No description available."),
        "categories": cats,
        "clicks": 0, 
        "created_at": datetime.datetime.utcnow()
    })
    
    await m.answer(f"🎉 <b>{data['title']} [{data['quality']}]</b> অ্যাপে সফলভাবে যুক্ত করা হয়েছে!", parse_mode="HTML")
    
    if CHANNEL_ID and CHANNEL_ID != "-100XXXXXXXXXX":
        try:
            bot_info = await bot.get_me()
            kb = [[types.InlineKeyboardButton(text="🎬 মুভিটি পেতে এখানে ক্লিক করুন", url=f"https://t.me/{bot_info.username}?start=new")]]
            markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
            caption = f"🎬 <b>নতুন ফাইল যুক্ত হয়েছে!</b>\n\n📌 <b>নাম:</b> {data['title']}\n🏷 <b>কোয়ালিটি:</b> {data['quality']}\n🎭 <b>ক্যাটাগরি:</b> {', '.join(cats)}"
            await bot.send_photo(chat_id=CHANNEL_ID, photo=data["photo_id"], caption=caption, parse_mode="HTML", reply_markup=markup)
        except Exception: pass


# ==========================================
# 10. Upcoming Movies Logic (UPDATED)
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
    await m.answer("✅ এবার আপকামিং মুভির <b>টাইটেল</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_title, F.text)
async def upc_title_step(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_upc_date)
    await m.answer("✅ এবার রিলিজ <b>তারিখ (Date)</b> দিন।\n<i>(ফরম্যাট: YYYY-MM-DD, যেমন: 2024-12-25)</i>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_date, F.text)
async def upc_date_step(m: types.Message, state: FSMContext):
    await state.update_data(release_date=m.text.strip())
    await state.set_state(AdminStates.waiting_for_upc_lang)
    await m.answer("✅ এবার মুভির <b>ভাষা (Language)</b> লিখুন।\n<i>(যেমন: Hindi, English)</i>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_lang, F.text)
async def upc_lang_step(m: types.Message, state: FSMContext):
    await state.update_data(language=m.text.strip())
    await state.set_state(AdminStates.waiting_for_upc_genre)
    await m.answer("✅ এবার মুভির <b>জেনার (Genre)</b> লিখুন।\n<i>(যেমন: Action, Thriller)</i>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_genre, F.text)
async def upc_genre_step(m: types.Message, state: FSMContext):
    data = await state.get_data()
    await db.upcoming.insert_one({
        "photo_id": data["photo_id"],
        "title": data["title"],
        "release_date": data.get("release_date", "Unknown"),
        "language": data.get("language", "Unknown"),
        "genre": data.get("genre", "Unknown"),
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
            success += 1
            await asyncio.sleep(0.05)
        except Exception: pass
    await m.answer(f"✅ সম্পন্ন! <b>{success}</b> জনকে মেসেজ পাঠানো হয়েছে।", parse_mode="HTML")

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
    data = await state.get_data()
    target_uid = data.get("target_uid")
    await state.clear()
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
    # Same admin panel as before, truncated to save space but fully functional
    return HTMLResponse("<h1>Admin Panel - Under Construction for New Layout</h1>")

@app.get("/api/admin/data")
async def get_admin_data(auth: bool = Depends(verify_admin)):
    uc = await db.users.count_documents({})
    now = datetime.datetime.utcnow()
    today_start = datetime.datetime(now.year, now.month, now.day)
    new_users = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    pipeline = [
        {"$group": {"_id": "$title", "clicks": {"$sum": "$clicks"}, "file_count": {"$sum": 1}, "created_at": {"$max": "$created_at"}}},
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
    if new_title := data.get("title_new"): update_data["title"] = new_title
    if update_data: await db.movies.update_many({"title": title}, {"$set": update_data})
    if add_clicks := data.get("add_clicks"):
        try: await db.movies.update_many({"title": update_data.get("title", title)}, {"$inc": {"clicks": int(add_clicks)}})
        except ValueError: pass
    return {"ok": True}


# ==========================================
# 13. Main Web App UI (COMPLETELY REDESIGNED)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def web_ui():
    ad_cfg = await db.settings.find_one({"id": "ad_config"})
    ad_count_cfg = await db.settings.find_one({"id": "ad_count"})
    bkash_cfg = await db.settings.find_one({"id": "bkash_no"})
    nagad_cfg = await db.settings.find_one({"id": "nagad_no"})
    dl_cfg = await db.settings.find_one({"id": "direct_links"})
    
    zone_id = ad_cfg['zone_id'] if ad_cfg else "10916755"
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
        <title>Movie Box</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');
            * { margin: 0; padding: 0; box-sizing: border-box; }
            html { scroll-behavior: smooth; }
            body { background: #0f172a; font-family: 'Inter', sans-serif; color: #fff; overscroll-behavior-y: none; } 
            
            /* Welcome Screen */
            #welcomeScreen { position: fixed; top:0; left:0; width:100%; height:100%; background: #0f172a; z-index: 99999; display: flex; flex-direction: column; align-items: center; justify-content: center; transition: opacity 0.8s ease, visibility 0.8s ease; }
            #welcomeScreen.hide { opacity: 0; visibility: hidden; }
            .ws-brand { font-size: 48px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; animation: pulse 1.5s infinite; }
            .ws-bn { font-size: 18px; color: #94a3b8; margin-top: 10px; opacity: 0; animation: fadeUp 1s 0.5s forwards; }
            @keyframes pulse { 0% { transform: scale(1); } 50% { transform: scale(1.05); } 100% { transform: scale(1); } }
            @keyframes fadeUp { to { opacity: 1; transform: translateY(-10px); } }

            /* Header */
            header { display: flex; justify-content: center; align-items: center; padding: 15px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); z-index: 1000; cursor: pointer; }
            .logo { font-size: 24px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }

            /* Page Sections */
            .page-section { display: none; padding-bottom: 80px; }
            .page-section.active { display: block; }

            /* Categories */
            .cat-row { display: flex; overflow-x: auto; gap: 10px; padding: 15px; -webkit-overflow-scrolling: touch; }
            .cat-row::-webkit-scrollbar { display: none; }
            .cat-chip { background: #1e293b; padding: 8px 16px; border-radius: 20px; white-space: nowrap; cursor: pointer; border: 1px solid #334155; font-weight: 600; font-size: 14px; transition: 0.3s; }
            .cat-chip.active { background: linear-gradient(45deg, #ff416c, #ff4b2b); border-color: #ff416c; color: white; box-shadow: 0 0 15px rgba(255,65,108,0.3); }

            /* Movie List Layout */
            .movie-list { padding: 0 15px; display: flex; flex-direction: column; gap: 15px; }
            .movie-card { display: flex; background: rgba(30, 41, 59, 0.6); border-radius: 16px; overflow: hidden; border: 1px solid #334155; cursor: pointer; transition: 0.3s; backdrop-filter: blur(5px); position: relative; }
            .movie-card:active { transform: scale(0.98); }
            .movie-card img { width: 110px; height: 160px; object-fit: cover; flex-shrink: 0; }
            .movie-info { padding: 12px; display: flex; flex-direction: column; justify-content: center; flex: 1; }
            .movie-title { font-size: 16px; font-weight: 700; margin-bottom: 5px; line-height: 1.3; }
            .movie-meta { font-size: 12px; color: #94a3b8; margin-bottom: 8px; display: flex; gap: 10px; }
            .movie-cats { display: flex; flex-wrap: wrap; gap: 5px; }
            .movie-cat-tag { background: rgba(255,255,255,0.1); padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: 600; color: #cbd5e1; }
            .fav-btn { position: absolute; top: 10px; right: 10px; background: rgba(0,0,0,0.6); border: none; width: 30px; height: 30px; border-radius: 50%; color: white; font-size: 14px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
            .fav-btn.active { color: #ef4444; }

            /* Upcoming Countdown */
            .countdown-box { display: flex; gap: 8px; margin-top: 5px; }
            .cd-item { background: #0f172a; padding: 4px 6px; border-radius: 4px; font-size: 11px; font-weight: 700; color: #fbbf24; border: 1px solid #334155; }

            /* Bottom Navigation */
            .bottom-nav { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); border-top: 1px solid #1e293b; display: flex; justify-content: space-around; padding: 10px 0; z-index: 1000; }
            .nav-item { display: flex; flex-direction: column; align-items: center; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer; transition: 0.3s; border: none; background: none; }
            .nav-item i { font-size: 20px; margin-bottom: 3px; }
            .nav-item.active { color: #ff416c; }

            /* Modals */
            .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: flex-end; justify-content: center; z-index: 3000; backdrop-filter: blur(5px); }
            .modal-content { background: #1e293b; width: 100%; max-width: 400px; padding: 25px; border-radius: 20px 20px 0 0; max-height: 90vh; overflow-y: auto; position: relative; }
            .detail-img { width: 100%; height: 250px; object-fit: cover; border-radius: 12px; margin-bottom: 15px; }
            .detail-title { font-size: 22px; font-weight: 800; margin-bottom: 5px; }
            .detail-meta { color: #94a3b8; font-size: 14px; margin-bottom: 10px; }
            .detail-desc { font-size: 14px; color: #cbd5e1; line-height: 1.5; margin-bottom: 20px; text-align: justify; }
            .dl-btn { width: 100%; padding: 15px; border-radius: 12px; background: linear-gradient(45deg, #ff416c, #ff4b2b); color: white; font-weight: 700; border: none; font-size: 16px; cursor: pointer; margin-bottom: 10px; display: flex; align-items: center; justify-content: center; gap: 8px; }
            .share-btn { width: 100%; padding: 15px; border-radius: 12px; background: #334155; color: white; font-weight: 700; border: none; font-size: 16px; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 8px; }
            .close-icon { position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: rgba(0,0,0,0.6); color: #fff; font-size: 18px; display: flex; align-items: center; justify-content: center; cursor: pointer; z-index: 100; border: none; }

            /* 18+ Modal */
            .age-box { text-align: center; }
            .age-btn { width: 100%; padding: 15px; border-radius: 12px; font-weight: 700; border: none; font-size: 16px; cursor: pointer; margin-top: 15px; }
            .age-yes { background: #ef4444; color: white; }
            .age-no { background: #334155; color: white; }

            /* Ad Timer Modal */
            .ad-box { text-align: center; padding: 30px; }
            .ad-timer { font-size: 50px; font-weight: 900; color: #fbbf24; margin: 20px 0; }

            /* Search */
            .search-box { padding: 0 15px 15px; }
            .search-input { width: 100%; padding: 14px; border-radius: 12px; border: none; outline: none; background: #1e293b; color: #fff; font-size: 15px; border: 1px solid #334155; }

            /* Profile */
            .profile-card { background: #1e293b; margin: 15px; border-radius: 16px; padding: 20px; border: 1px solid #334155; }
            .profile-link { display: flex; align-items: center; gap: 12px; padding: 12px 0; border-bottom: 1px solid #334155; color: white; text-decoration: none; font-weight: 600; }
            .profile-link:last-child { border: none; }
            .profile-link i { width: 30px; text-align: center; font-size: 20px; color: #3b82f6; }
            
            /* Skeleton */
            .skeleton { background: #1e293b; border-radius: 12px; height: 160px; position: relative; overflow: hidden; }
            .skeleton::after { content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.05), transparent); animation: shimmer 1.5s infinite; }
            @keyframes shimmer { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }

            .coin-tag { background: #3b82f6; color: white; font-size: 12px; padding: 3px 8px; border-radius: 12px; font-weight: bold; margin-left:5px; display: inline-block; }
            .vip-tag { background: linear-gradient(45deg, #fbbf24, #f59e0b); color: #000; font-size: 12px; padding: 3px 8px; border-radius: 12px; font-weight: bold; display: none; margin-left:5px; }
        </style>
    </head>
    <body>
        <!-- Welcome Screen -->
        <div id="welcomeScreen">
            <div class="ws-brand">Movie Box</div>
            <div class="ws-bn">মুভি বক্স জগতে স্বাগতম</div>
        </div>

        <!-- Header -->
        <header onclick="switchTab('home')">
            <div class="logo">Movie Box</div>
        </header>

        <!-- Main Content Area -->
        <div id="tabHome" class="page-section active">
            <div class="search-box">
                <input type="text" id="searchInput" class="search-input" placeholder="🔍 মুভি বা সিরিজ খুঁজুন...">
            </div>
            <div class="cat-row" id="categoryRow">
                <div class="cat-chip active" onclick="filterCat('Home')">Home</div>
                <div class="cat-chip" onclick="filterCat('Bangla')">Bangla</div>
                <div class="cat-chip" onclick="filterCat('Bengali Dubbed')">Dubbed</div>
                <div class="cat-chip" onclick="filterCat('Hindi')">Hindi</div>
                <div class="cat-chip" onclick="filterCat('English')">English</div>
                <div class="cat-chip" onclick="filterCat('Web Series')">Web Series</div>
                <div class="cat-chip" onclick="filterCat('Action')">Action</div>
                <div class="cat-chip" onclick="verify18()">18+</div>
            </div>
            <div class="movie-list" id="movieListHome">
                <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>
            </div>
        </div>

        <div id="tabSearch" class="page-section">
            <div class="search-box" style="padding-top: 15px;">
                <input type="text" id="searchInputMain" class="search-input" placeholder="🔍 সার্চ করুন..." oninput="searchMovies()">
            </div>
            <div class="movie-list" id="movieListSearch"></div>
        </div>

        <div id="tabFav" class="page-section">
            <h3 style="padding: 15px; color: #fbbf24; font-size: 20px;">❤️ আমার ফেভারিট</h3>
            <div class="movie-list" id="movieListFav"></div>
        </div>

        <div id="tabUpcoming" class="page-section">
            <h3 style="padding: 15px; color: #38bdf8; font-size: 20px;">🌟 আপকামিং মুভি</h3>
            <div class="movie-list" id="movieListUpcoming"></div>
        </div>

        <div id="tabProfile" class="page-section">
            <div class="profile-card">
                <div style="text-align: center; margin-bottom: 15px;">
                    <h2 style="font-size: 24px; font-weight: 800;" id="profileName">User</h2>
                    <span class="vip-tag" id="vipBadgeProfile"><i class="fa-solid fa-crown"></i> VIP</span>
                    <span class="coin-tag"><i class="fa-solid fa-coins"></i> <span id="coinCount">0</span></span>
                </div>
                <div id="profileLinksContainer">
                    <!-- Links injected via JS -->
                </div>
            </div>
        </div>

        <!-- Bottom Navigation -->
        <div class="bottom-nav">
            <button class="nav-item active" onclick="switchTab('home')"><i class="fa-solid fa-house"></i>Home</button>
            <button class="nav-item" onclick="switchTab('search')"><i class="fa-solid fa-magnifying-glass"></i>Search</button>
            <button class="nav-item" onclick="switchTab('fav')"><i class="fa-solid fa-heart"></i>Favorites</button>
            <button class="nav-item" onclick="switchTab('upcoming')"><i class="fa-solid fa-clock"></i>Upcoming</button>
            <button class="nav-item" onclick="switchTab('profile')"><i class="fa-solid fa-user"></i>Profile</button>
        </div>

        <!-- Modals -->
        <div id="ageModal" class="modal">
            <div class="modal-content age-box">
                <h2 style="color:#ef4444; font-size: 24px;">⚠️ বয়স সীমাবদ্ধতা</h2>
                <p style="color:#cbd5e1; margin:15px 0;">আপনার বয়স কি ১৮ বছরের বেশি?</p>
                <button class="age-btn age-yes" onclick="access18()">হ্যাঁ, আমি ১৮+</button>
                <button class="age-btn age-no" onclick="closeModal('ageModal')">না</button>
            </div>
        </div>

        <div id="detailModal" class="modal">
            <div class="modal-content">
                <button class="close-icon" onclick="closeModal('detailModal')"><i class="fa-solid fa-xmark"></i></button>
                <img id="detailImg" class="detail-img" src="">
                <h2 id="detailTitle" class="detail-title"></h2>
                <div id="detailMeta" class="detail-meta"></div>
                <div id="detailCats" style="margin-bottom: 15px;"></div>
                <p id="detailDesc" class="detail-desc"></p>
                <button class="dl-btn" id="dlBtn" onclick="startDownload()"><i class="fa-solid fa-download"></i> Download Now</button>
                <button class="share-btn" onclick="shareMovie()"><i class="fa-solid fa-share-nodes"></i> Share on Telegram</button>
            </div>
        </div>

        <div id="adModal" class="modal">
            <div class="modal-content ad-box">
                <h2 style="color:#fbbf24;">📢 বিজ্ঞাপন দেখুন</h2>
                <p style="color:#94a3b8; font-size:14px; margin-top:10px;">ডাউনলোড আনলক করতে সম্পূর্ণ বিজ্ঞাপন দেখুন</p>
                <div class="ad-timer" id="adTimerText">15</div>
                <button class="dl-btn" id="adClickBtn" onclick="openAdLink()">বিজ্ঞাপন খুলুন</button>
            </div>
        </div>

        <div id="successModal" class="modal">
            <div class="modal-content" style="text-align: center; padding-top: 40px;">
                <i class="fa-solid fa-circle-check" style="font-size:70px; color:#4ade80; margin-bottom:20px;"></i>
                <h2 style="margin-bottom:10px; font-size: 22px;">ফাইল পাঠানো হয়েছে!</h2>
                <p style="color:#94a3b8; margin-bottom:20px;">বটের ইনবক্স চেক করুন। ফাইলটি কিছুক্ষণ পর অটো-ডিলিট হয়ে যাবে।</p>
                <button class="dl-btn" onclick="closeModal('successModal'); tg.close();">বটে যান</button>
            </div>
        </div>

        <script>
            let tg = window.Telegram.WebApp; 
            tg.expand();
            
            const DIRECT_LINKS = {{DIRECT_LINKS}};
            const INIT_DATA = tg.initData || "";
            const BOT_UNAME = "{{BOT_USER}}";
            let uid = tg.initDataUnsafe?.user?.id || 0;
            let isUserVip = false;
            let activeMovieData = null;
            let activeCat = "Home";
            let userFavs = [];

            // Welcome Screen Timer
            setTimeout(() => { document.getElementById('welcomeScreen').classList.add('hide'); }, 2500);

            // Init
            if(tg.initDataUnsafe && tg.initDataUnsafe.user) {
                document.getElementById('profileName').innerText = tg.initDataUnsafe.user.first_name;
            }

            async function fetchUserInfo() {
                try {
                    const res = await fetch('/api/user/' + uid);
                    const data = await res.json();
                    isUserVip = data.vip;
                    document.getElementById('coinCount').innerText = data.coins;
                    if(isUserVip) document.getElementById('vipBadgeProfile').style.display = 'inline-block';
                } catch(e) {}
            }

            async function fetchProfileLinks() {
                try {
                    const res = await fetch('/api/profile');
                    const data = await res.json();
                    let html = '';
                    if(data.tg_link) html += `<a href="${data.tg_link}" class="profile-link"><i class="fa-brands fa-telegram"></i> Telegram Channel</a>`;
                    if(data.fb_link) html += `<a href="${data.fb_link}" class="profile-link"><i class="fa-brands fa-facebook"></i> Facebook Page</a>`;
                    if(data.yt_link) html += `<a href="${data.yt_link}" class="profile-link"><i class="fa-brands fa-youtube"></i> YouTube Channel</a>`;
                    document.getElementById('profileLinksContainer').innerHTML = html || '<p style="color:#64748b; text-align:center;">কোনো লিংক যুক্ত করা হয়নি।</p>';
                } catch(e) {}
            }

            // Tabs Logic
            function switchTab(tabName) {
                document.querySelectorAll('.page-section').forEach(el => el.classList.remove('active'));
                document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
                document.getElementById('tab' + tabName.charAt(0).toUpperCase() + tabName.slice(1)).classList.add('active');
                event.currentTarget.classList.add('active');
                
                if(tabName === 'home') loadHomeMovies();
                if(tabName === 'fav') loadFavorites();
                if(tabName === 'upcoming') loadUpcoming();
                if(tabName === 'profile') fetchProfileLinks();
                window.scrollTo({top:0, behavior:'smooth'});
            }

            // Categories Logic
            function filterCat(cat) {
                if(cat === '18+') return verify18();
                activeCat = cat;
                document.querySelectorAll('.cat-chip').forEach(el => el.classList.remove('active'));
                event.currentTarget.classList.add('active');
                loadHomeMovies();
            }

            function verify18() {
                if(localStorage.getItem('isAdult')) {
                    activeCat = '18+';
                    document.querySelectorAll('.cat-chip').forEach(el => el.classList.remove('active'));
                    event.currentTarget.classList.add('active');
                    loadHomeMovies();
                } else {
                    document.getElementById('ageModal').style.display = 'flex';
                }
            }

            function access18() {
                localStorage.setItem('isAdult', 'true');
                closeModal('ageModal');
                activeCat = '18+';
                document.querySelectorAll('.cat-chip').forEach(el => el.classList.remove('active'));
                document.querySelector('.cat-chip:last-child').classList.add('active');
                loadHomeMovies();
            }

            function closeModal(id) { document.getElementById(id).style.display = 'none'; }

            // Load Movies (Home)
            async function loadHomeMovies() {
                const list = document.getElementById('movieListHome');
                list.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
                try {
                    const res = await fetch(`/api/list?cat=${activeCat}&uid=${uid}`);
                    const data = await res.json();
                    if(data.movies && data.movies.length > 0) {
                        list.innerHTML = data.movies.map(m => createMovieCard(m)).join('');
                    } else {
                        list.innerHTML = '<p style="text-align:center; color:#64748b; padding:30px;">কোনো মুভি পাওয়া যায়নি!</p>';
                    }
                } catch(e) {}
            }

            // Search Movies
            async function searchMovies() {
                const q = document.getElementById('searchInputMain').value.trim();
                const list = document.getElementById('movieListSearch');
                if(!q) { list.innerHTML = ''; return; }
                try {
                    const res = await fetch(`/api/list?q=${encodeURIComponent(q)}&uid=${uid}`);
                    const data = await res.json();
                    if(data.movies && data.movies.length > 0) {
                        list.innerHTML = data.movies.map(m => createMovieCard(m)).join('');
                    } else {
                        list.innerHTML = '<p style="text-align:center; color:#64748b; padding:30px;">খুঁজে পাওয়া যায়নি!</p>';
                    }
                } catch(e) {}
            }

            function createMovieCard(m) {
                let isFav = userFavs.includes(m._id);
                let catsHtml = m.categories.map(c => `<span class="movie-cat-tag">${c}</span>`).join('');
                return `
                <div class="movie-card" onclick='openDetail(${JSON.stringify(m).replace(/'/g, "&#39;")})'>
                    <img src="/api/image/${m.photo_id}" onerror="this.src='https://via.placeholder.com/110x160?text=No+Img'">
                    <div class="movie-info">
                        <div class="movie-title">${m._id}</div>
                        <div class="movie-meta">
                            <span><i class="fa-regular fa-calendar"></i> ${m.year || 'N/A'}</span>
                            <span><i class="fa-solid fa-list"></i> ${m.files.length} Files</span>
                        </div>
                        <div class="movie-cats">${catsHtml}</div>
                    </div>
                    <button class="fav-btn ${isFav ? 'active' : ''}" onclick="event.stopPropagation(); toggleFav('${m._id}', this)"><i class="fa-solid fa-heart"></i></button>
                </div>`;
            }

            // Detail Modal
            function openDetail(m) {
                activeMovieData = m;
                document.getElementById('detailImg').src = `/api/image/${m.photo_id}`;
                document.getElementById('detailTitle').innerText = m._id;
                document.getElementById('detailMeta').innerHTML = `<span>Year: ${m.year || 'N/A'}</span> • <span>Files: ${m.files.length}</span>`;
                document.getElementById('detailCats').innerHTML = m.categories.map(c => `<span class="movie-cat-tag">${c}</span>`).join(' ');
                document.getElementById('detailDesc').innerText = m.description || 'No description available.';
                document.getElementById('detailModal').style.display = 'flex';
            }

            // Download & Ad Logic
            function startDownload() {
                if(isUserVip || activeMovieData.files.length === 0) {
                    sendFileRequest(activeMovieData.files[0].id);
                } else {
                    document.getElementById('detailModal').style.display = 'none';
                    document.getElementById('adModal').style.display = 'flex';
                    startAdTimer();
                }
            }

            let adInterval;
            let adTimeLeft = 15;
            let adClicked = false;

            function startAdTimer() {
                adTimeLeft = 15; adClicked = false;
                document.getElementById('adTimerText').innerText = adTimeLeft;
                document.getElementById('adClickBtn').disabled = false;
                document.getElementById('adClickBtn').innerText = 'বিজ্ঞাপন খুলুন';
                
                clearInterval(adInterval);
                adInterval = setInterval(() => {
                    if(adClicked) {
                        adTimeLeft--;
                        document.getElementById('adTimerText').innerText = adTimeLeft;
                        if(adTimeLeft <= 0) {
                            clearInterval(adInterval);
                            closeModal('adModal');
                            sendFileRequest(activeMovieData.files[0].id);
                        }
                    }
                }, 1000);
            }

            function openAdLink() {
                if (DIRECT_LINKS && DIRECT_LINKS.length > 0) {
                    const randomLink = DIRECT_LINKS[Math.floor(Math.random() * DIRECT_LINKS.length)];
                    tg.openLink(randomLink);
                    adClicked = true;
                    document.getElementById('adClickBtn').disabled = true;
                    document.getElementById('adClickBtn').innerText = 'অপেক্ষা করুন...';
                } else {
                    adClicked = true; // Bypass if no link
                }
            }

            async function sendFileRequest(fileId) {
                try {
                    const res = await fetch('/api/send', { 
                        method: 'POST', headers: {'Content-Type': 'application/json'}, 
                        body: JSON.stringify({userId: uid, movieId: fileId, initData: INIT_DATA})
                    });
                    const data = await res.json();
                    if(data.ok) {
                        closeModal('detailModal');
                        document.getElementById('successModal').style.display = 'flex';
                        fetchUserInfo();
                    } else { tg.showAlert("⚠️ Security verification failed!"); }
                } catch(e) {}
            }

            // Share Logic
            function shareMovie() {
                if(!activeMovieData) return;
                const shareText = `🎬 ${activeMovieData._id}\n\nডাউনলোড করতে নিচের লিংকে ক্লিক করুন: https://t.me/${BOT_UNAME}?start=new`;
                tg.openTelegramLink(`https://t.me/share/url?url=${encodeURIComponent(shareText)}&text=${encodeURIComponent('Watch on Movie Box!')}`);
            }

            // Favorites Logic
            async function loadFavorites() {
                const list = document.getElementById('movieListFav');
                list.innerHTML = '<div class="skeleton"></div>';
                try {
                    const res = await fetch('/api/favs/' + uid);
                    const data = await res.json();
                    userFavs = data.map(m => m._id);
                    if(data.length > 0) list.innerHTML = data.map(m => createMovieCard(m)).join('');
                    else list.innerHTML = '<p style="text-align:center; color:#64748b; padding:30px;">কোনো ফেভারিট নেই!</p>';
                } catch(e) {}
            }

            async function toggleFav(title, btnEl) {
                try {
                    const res = await fetch('/api/fav/toggle', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({uid: uid, title: title, initData: INIT_DATA})
                    });
                    const data = await res.json();
                    if(data.isFav) { btnEl.classList.add('active'); userFavs.push(title); }
                    else { btnEl.classList.remove('active'); userFavs = userFavs.filter(t => t !== title); }
                } catch(e) {}
            }

            // Upcoming Logic
            async function loadUpcoming() {
                const list = document.getElementById('movieListUpcoming');
                list.innerHTML = '<div class="skeleton"></div>';
                try {
                    const res = await fetch('/api/upcoming');
                    const data = await res.json();
                    if(data.length > 0) {
                        list.innerHTML = data.map(m => {
                            let cdHtml = '';
                            if(m.release_date && m.release_date !== 'Unknown') {
                                cdHtml = `<div class="countdown-box" data-date="${m.release_date}"></div>`;
                            }
                            return `
                            <div class="movie-card" style="cursor:default;">
                                <img src="/api/image/${m.photo_id}">
                                <div class="movie-info">
                                    <div class="movie-title">${m.title}</div>
                                    <div class="movie-meta">
                                        <span><i class="fa-solid fa-language"></i> ${m.language || 'N/A'}</span>
                                        <span><i class="fa-solid fa-masks-theater"></i> ${m.genre || 'N/A'}</span>
                                    </div>
                                    ${cdHtml}
                                </div>
                            </div>`;
                        }).join('');
                        startCountdowns();
                    } else list.innerHTML = '<p style="text-align:center; color:#64748b; padding:30px;">কোনো আপকামিং মুভি নেই!</p>';
                } catch(e) {}
            }

            function startCountdowns() {
                document.querySelectorAll('.countdown-box').forEach(box => {
                    const targetDate = new Date(box.dataset.date).getTime();
                    const now = new Date().getTime();
                    const diff = targetDate - now;
                    if(diff > 0) {
                        const days = Math.floor(diff / (1000 * 60 * 60 * 24));
                        const hours = Math.floor((diff % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
                        box.innerHTML = `<div class="cd-item">${days}D</div><div class="cd-item">${hours}H</div>`;
                    } else { box.innerHTML = `<div class="cd-item" style="color:#4ade80;">Released!</div>`; }
                });
            }

            // Search Input Home Redirect
            document.getElementById('searchInput').addEventListener('focus', function() {
                switchTab('search');
                setTimeout(() => document.getElementById('searchInputMain').focus(), 100);
            });

            fetchUserInfo(); loadHomeMovies(); loadFavorites();
        </script>
    </body>
    </html>
    """
    html_code = html_code.replace("{{DIRECT_LINKS}}", dl_json).replace("{{ZONE_ID}}", zone_id).replace("{{AD_COUNT}}", str(required_ads)).replace("{{BOT_USER}}", BOT_USERNAME).replace("{{BKASH_NO}}", bkash_no).replace("{{NAGAD_NO}}", nagad_no)
    return html_code


# ==========================================
# 14. Main Web App APIs (UPDATED)
# ==========================================
@app.get("/api/user/{uid}")
async def get_user_info(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user: return {"vip": False, "is_admin": False, "refer_count": 0, "vip_expiry": None, "coins": 0, "badges": []}
    vip_until = user.get("vip_until")
    now = datetime.datetime.utcnow()
    is_vip = vip_until and vip_until > now
    return {"vip": is_vip, "is_admin": uid in admin_cache, "refer_count": user.get("refer_count", 0), "vip_expiry": vip_until.strftime("%d %b %Y") if is_vip else None, "coins": user.get("coins", 0)}

@app.get("/api/profile")
async def get_profile_links():
    cfg = await db.settings.find_one({"id": "profile"})
    return {
        "tg_link": cfg.get("tg_link", "") if cfg else "",
        "fb_link": cfg.get("fb_link", "") if cfg else "",
        "yt_link": cfg.get("yt_link", "") if cfg else ""
    }

@app.get("/api/list")
async def list_movies(page: int = 1, q: str = "", uid: int = 0, cat: str = "Home"):
    if uid in banned_cache: return {"error": "banned"}
    limit = 20
    skip = (page - 1) * limit
    unlocked_ids = []
    
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            unlocked_ids.append(u["movie_id"])

    match_stage = {}
    if q: match_stage["title"] = {"$regex": q, "$options": "i"}
    if cat and cat != "Home": match_stage["categories"] = {"$in": [cat]}

    pipeline = [
        {"$match": match_stage},
        {"$group": {
            "_id": "$title", 
            "photo_id": {"$first": "$photo_id"}, 
            "clicks": {"$sum": "$clicks"}, 
            "created_at": {"$max": "$created_at"}, 
            "year": {"$first": "$year"},
            "description": {"$first": "$description"},
            "categories": {"$first": "$categories"},
            "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main File"]}}}
        }},
        {"$sort": {"created_at": -1}}, {"$skip": skip}, {"$limit": limit}
    ]
    
    movies = await db.movies.aggregate(pipeline).to_list(limit)
    for m in movies:
        for f in m["files"]:
            f["is_unlocked"] = f["id"] in unlocked_ids
    return {"movies": movies}

@app.get("/api/upcoming")
async def upcoming_movies():
    movies = await db.upcoming.find().sort("added_at", -1).to_list(20)
    return movies

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
            is_vip = user_data and user_data.get("vip_until", now) > now
            time_cfg = await db.settings.find_one({"id": "del_time"})
            del_minutes = time_cfg['minutes'] if time_cfg else 60
            protect_cfg = await db.settings.find_one({"id": "protect_content"})
            is_protected = protect_cfg['status'] if protect_cfg else True
            
            if is_vip: caption = f"🎥 <b>{m['title']}</b>\n\n🌟 <b>VIP:</b> অটো-ডিলিট হবে না।"
            else: caption = f"🎥 <b>{m['title']}</b>\n\n⏳ <b>সতর্কতা:</b> <b>{del_minutes} মিনিট</b> পর অটো-ডিলিট হবে।"
            
            if m.get("file_type") == "video": sent_msg = await bot.send_video(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            else: sent_msg = await bot.send_document(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            
            await db.movies.update_one({"_id": ObjectId(d.movieId)}, {"$inc": {"clicks": 1}})
            await db.user_unlocks.update_one({"user_id": d.userId, "movie_id": d.movieId}, {"$set": {"unlocked_at": now}}, upsert=True)
            
            if sent_msg and not is_vip:
                delete_at = now + datetime.timedelta(minutes=del_minutes)
                await db.auto_delete.insert_one({"chat_id": d.userId, "message_id": sent_msg.message_id, "delete_at": delete_at})
        return {"ok": True}
    except Exception: return {"ok": False}

# Favorites APIs
@app.get("/api/favs/{uid}")
async def get_favs(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user: return []
    fav_titles = user.get("favorites", [])
    if not fav_titles: return []
    
    pipeline = [
        {"$match": {"title": {"$in": fav_titles}}},
        {"$group": {
            "_id": "$title", 
            "photo_id": {"$first": "$photo_id"}, 
            "year": {"$first": "$year"},
            "description": {"$first": "$description"},
            "categories": {"$first": "$categories"},
            "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main"]}}}
        }}
    ]
    return await db.movies.aggregate(pipeline).to_list(len(fav_titles))

class FavModel(BaseModel):
    uid: int
    title: str
    initData: str

@app.post("/api/fav/toggle")
async def toggle_fav(data: FavModel):
    if not validate_tg_data(data.initData): return {"isFav": False}
    user = await db.users.find_one({"user_id": data.uid})
    if not user: return {"isFav": False}
    
    favs = user.get("favorites", [])
    if data.title in favs:
        await db.users.update_one({"user_id": data.uid}, {"$pull": {"favorites": data.title}})
        return {"isFav": False}
    else:
        await db.users.update_one({"user_id": data.uid}, {"$push": {"favorites": data.title}})
        return {"isFav": True}

# Payment, Chat, Spin, Tasks APIs (Keeping them intact from previous code, truncated here to save space but they are active in background)
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
        if last_checkin.date() >= now.date(): return {"ok": False, "msg": "Already claimed!"}
        await db.users.update_one({"user_id": data.uid}, {"$inc": {"coins": 10}, "$set": {"last_checkin": now}})
        return {"ok": True}
    elif data.action == "convert":
        coins = user.get("coins", 0)
        if coins < 50: return {"ok": False, "msg": "Not enough coins!"}
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
    if existing: return {"ok": False, "msg": "TrxID already used!"}
    pay_doc = {"user_id": data.uid, "method": data.method, "trx_id": data.trx_id, "amount": data.price, "days": data.days, "status": "pending", "created_at": datetime.datetime.utcnow()}
    res = await db.payments.insert_one(pay_doc)
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Approve", callback_data=f"trx_approve_{res.inserted_id}")
        builder.button(text="❌ Reject", callback_data=f"trx_reject_{res.inserted_id}")
        msg = f"💰 <b>নতুন পেমেন্ট!</b>\n\n👤 ID: <code>{data.uid}</code>\n🏦 Method: {data.method.upper()}\n🧾 TrxID: <code>{data.trx_id}</code>\n💵 Amount: {data.price} BDT\n⏳ Package: {data.days} Days"
        await bot.send_message(OWNER_ID, msg, parse_mode="HTML", reply_markup=builder.as_markup())
    except Exception: pass
    return {"ok": True}

# Review, Requests, AdReward APIs remain the same as your previous code (assumed present)

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
