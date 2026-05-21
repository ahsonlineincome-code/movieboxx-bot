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

CATEGORIES = ["Bangla", "Hindi Dubbed", "Hollywood", "K-Drama", "Horror", "Action", "Web Series", "Adult Content"]

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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Incorrect", 
            headers={"WWW-Authenticate": "Basic"}
        )
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
                        if current_vip < now:
                            current_vip = now
                        await db.users.update_one({"user_id": referrer_id}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=1)}})
                        try:
                            await bot.send_message(referrer_id, "🎉 ৫ জন রেফারের জন্য ২৪ ঘণ্টা VIP!", parse_mode="HTML")
                        except:
                            pass
            except:
                pass

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

    tg_link = "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"

    kb = [
        [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))],
        [
            types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link),
            types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)
        ]
    ]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    text = f"👋 <b>স্বাগতম {message.from_user.first_name}!</b>\n\n🎬 Movie Box জগতে আপনাকে স্বাগতম। নিচের বাটনে ক্লিক করে মুভি উপভোগ করুন।"
    if uid in admin_cache:
        text += "\n\n⚙️ <b>অ্যাডমিন মোড অন.</b>"
    await message.answer(text, reply_markup=markup, parse_mode="HTML")

# Admin Stats Command
@dp.message(Command("stats"))
async def bot_stats(m: types.Message):
    if m.from_user.id not in admin_cache:
        return
    total_users = await db.users.count_documents({})
    total_movies = await db.movies.count_documents({})
    total_upcoming = await db.upcoming.count_documents({})
    vip_users = await db.users.count_documents({"vip_until": {"$gt": datetime.datetime.utcnow()}})
    
    text = (
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total Users: <b>{total_users}</b>\n"
        f"💎 VIP Users: <b>{vip_users}</b>\n"
        f"🎬 Total Movies: <b>{total_movies}</b>\n"
        f"🌟 Upcoming: <b>{total_upcoming}</b>"
    )
    await m.answer(text, parse_mode="HTML")

@dp.message(lambda m: m.chat.type == "private" and m.from_user.id not in admin_cache)
async def forward_to_admin(m: types.Message):
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ রিপ্লাই", callback_data=f"reply_{m.from_user.id}")
        await bot.send_message(OWNER_ID, f"📩 <a href='tg://user?id={m.from_user.id}'>{m.from_user.first_name}</a>:\n\n{m.text or 'Media'}", parse_mode="HTML", reply_markup=builder.as_markup())
    except:
        pass

# ==========================================
# 7. Admin Commands & Settings
# ==========================================
@dp.message(Command("addlink"))
async def add_direct_link(m: types.Message):
    if m.from_user.id not in admin_cache:
        return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer(f"✅ লিংক অ্যাড হয়েছে।", parse_mode="HTML")
    except:
        await m.answer("⚠️ /addlink url", parse_mode="HTML")

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache:
        return
    try:
        title = m.text.split(" ", 1)[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0:
            await m.answer(f"✅ '<b>{title}</b>' ডিলিট হয়েছে!", parse_mode="HTML")
        else:
            await m.answer("⚠️ পাওয়া যায়নি")
    except:
        await m.answer("⚠️ /delmovie নাম", parse_mode="HTML")

@dp.message(Command("addvip"))
async def add_vip_cmd(m: types.Message):
    if m.from_user.id not in admin_cache:
        return
    try:
        args = m.text.split()
        target_uid = int(args[1])
        days = int(args[2]) if len(args) > 2 else 30 
        now = datetime.datetime.utcnow()
        user = await db.users.find_one({"user_id": target_uid})
        if not user:
            return await m.answer("⚠️ ইউজার নেই।")
        current_vip = user.get("vip_until", now)
        if current_vip < now:
            current_vip = now
        await db.users.update_one({"user_id": target_uid}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=days)}})
        await m.answer(f"✅ <code>{target_uid}</code> কে {days} দিনের VIP দেওয়া হয়েছে!", parse_mode="HTML")
    except:
        await m.answer("⚠️ /addvip ID দিন", parse_mode="HTML")

# ==========================================
# 8. Movie Upload Logic & Category Buttons
# ==========================================
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
    await m.answer("✅ এবার <b>এপিসোড বা কোয়ালিটি</b> লিখুন।\n<i>(যেমন: Ep 01 বা 720p)</i>", parse_mode="HTML")

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
    for cat in CATEGORIES:
        builder.button(text=cat, callback_data=f"selcat_{cat}")
    builder.button(text="✅ Done", callback_data="cats_done")
    builder.adjust(2)
    await m.answer("✅ এবার <b>ক্যাটাগরি সিলেক্ট</b> করুন।", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(AdminStates.waiting_for_cats, F.data.startswith("selcat_"))
async def process_category_selection(c: types.CallbackQuery, state: FSMContext):
    cat = c.data.split("_")[1]
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    
    if cat in selected_cats:
        selected_cats.remove(cat)
    else:
        selected_cats.append(cat)
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
    if not selected_cats:
        return await c.answer("⚠️ অন্তত ১টি সিলেক্ট করুন!", show_alert=True)
    
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
    await c.message.edit_text(f"🎉 <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!", parse_mode="HTML")

# ==========================================
# 9. Upcoming & Broadcast
# ==========================================
@dp.message(Command("addupcoming"))
async def add_upc_cmd(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache:
        return
    await state.set_state(AdminStates.waiting_for_upc_photo)
    await m.answer("🌟 <b>আপকামিং পোস্টার</b> পাঠান।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_photo, F.photo)
async def upc_photo_step(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_upc_title)
    await m.answer("✅ এবার <b>টাইটেল</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_title, F.text)
async def upc_title_step(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_upc_date)
    await m.answer("✅ এবার <b>তারিখ</b> দিন (YYYY-MM-DD)।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_date, F.text)
async def upc_date_step(m: types.Message, state: FSMContext):
    data = await state.get_data()
    await db.upcoming.insert_one({
        "photo_id": data["photo_id"], 
        "title": data["title"], 
        "release_date": m.text.strip(), 
        "language": "N/A", 
        "genre": "N/A"
    })
    await state.clear()
    await m.answer("✅ আপকামিং যুক্ত হয়েছে!")

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache:
        return
    await state.set_state(AdminStates.waiting_for_bcast)
    await m.answer("📢 ব্রডকাস্ট মেসেজ পাঠান।")

@dp.message(AdminStates.waiting_for_bcast)
async def execute_broadcast(m: types.Message, state: FSMContext):
    await state.clear()
    success = 0
    async for u in db.users.find():
        try:
            await m.copy_to(chat_id=u['user_id'])
            success += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await m.answer(f"✅ {success} জনকে পাঠানো হয়েছে।", parse_mode="HTML")

@dp.callback_query(F.data.startswith("trx_"))
async def handle_trx_approval(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache:
        return
    action = c.data.split("_")[1]
    pay_id = c.data.split("_")[2]
    payment = await db.payments.find_one({"_id": ObjectId(pay_id)})
    if not payment or payment["status"] != "pending":
        return await c.answer("⚠️ প্রসেস করা হয়েছে!", show_alert=True)
        
    user_id = payment["user_id"]
    days = payment["days"]
    
    if action == "approve":
        now = datetime.datetime.utcnow()
        user = await db.users.find_one({"user_id": user_id})
        current_vip = user.get("vip_until", now) if user else now
        if current_vip < now:
            current_vip = now
        await db.users.update_one({"user_id": user_id}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=days)}})
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "approved"}})
        await c.message.edit_text(c.message.text + "\n\n✅ <b>অ্যাপ্রুভ!</b>", parse_mode="HTML")
    else:
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "rejected"}})
        await c.message.edit_text(c.message.text + "\n\n❌ <b>রিজেক্ট!</b>", parse_mode="HTML")

# ==========================================
# 10. Web Admin Panel API
# ==========================================
@app.get("/admin", response_class=HTMLResponse)
async def web_admin_panel(auth: bool = Depends(verify_admin)):
    return HTMLResponse("<h1>Admin Panel</h1>")

# ==========================================
# 11. Main Web App UI (Updated with Requests)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def web_ui():
    dl_cfg = await db.settings.find_one({"id": "direct_links"})
    bkash_cfg = await db.settings.find_one({"id": "bkash_no"})
    nagad_cfg = await db.settings.find_one({"id": "nagad_no"})
    
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
            
            /* Profile Buttons Styles */
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
        </style>
