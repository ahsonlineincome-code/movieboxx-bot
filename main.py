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
# 6. Telegram Bot Commands (Hardcoded Links)
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

    # Hardcoded Channel Links Provided by User
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
# 11. Main Web App UI (Fixed Clicks + UI Updates)
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
            body { background: #0f172a; font-family: 'Inter', sans-serif; color: #fff; overscroll-behavior-y: none; } 
            
            #welcomeScreen { position: fixed; top:0; left:0; width:100%; height:100%; background: #0f172a; z-index: 99999; display: flex; flex-direction: column; align-items: center; justify-content: center; transition: opacity 0.8s ease; }
            #welcomeScreen.hide { opacity: 0; visibility: hidden; }
            .ws-brand { font-size: 48px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; animation: pulse 1.5s infinite; }
            .ws-bn { font-size: 18px; color: #94a3b8; margin-top: 10px; opacity: 0; animation: fadeUp 1s 0.5s forwards; }
            @keyframes pulse { 0% { transform: scale(1); } 50% { transform: scale(1.05); } 100% { transform: scale(1); } }
            @keyframes fadeUp { to { opacity: 1; transform: translateY(-10px); } }

            header { display: flex; justify-content: center; align-items: center; padding: 15px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); z-index: 1000; cursor: pointer; }
            .logo { font-size: 24px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }

            .page-section { display: none; padding-bottom: 80px; }
            .page-section.active { display: block; }

            /* Category Styling Like Image 2 */
            .cat-row { display: flex; flex-wrap: wrap; gap: 8px; padding: 15px; }
            .cat-chip { background: #1e293b; padding: 8px 16px; border-radius: 20px; white-space: nowrap; cursor: pointer; border: 1px solid #ef4444; font-weight: 600; font-size: 12px; transition: 0.3s; color: #cbd5e1; }
            .cat-chip.active { background: linear-gradient(45deg, #ef4444, #dc2626); border-color: #ef4444; color: white; box-shadow: 0 0 12px rgba(239, 68, 68, 0.4); }

            .movie-list { padding: 0 15px; display: flex; flex-direction: column; gap: 15px; }
            .movie-card { display: flex; background: rgba(30, 41, 59, 0.6); border-radius: 16px; overflow: hidden; border: 1px solid #334155; cursor: pointer; transition: 0.3s; position: relative; }
            .movie-card:active { transform: scale(0.98); }
            .movie-card img { width: 110px; height: 160px; object-fit: cover; flex-shrink: 0; }
            .movie-info { padding: 12px; display: flex; flex-direction: column; justify-content: center; flex: 1; }
            .movie-title { font-size: 16px; font-weight: 700; margin-bottom: 5px; line-height: 1.3; }
            .movie-meta { font-size: 12px; color: #94a3b8; margin-bottom: 8px; display: flex; gap: 10px; }
            .movie-cats { display: flex; flex-wrap: wrap; gap: 5px; }
            .movie-cat-tag { background: rgba(255,255,255,0.1); padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: 600; color: #cbd5e1; }
            .fav-btn { position: absolute; top: 10px; right: 10px; background: rgba(0,0,0,0.6); border: none; width: 30px; height: 30px; border-radius: 50%; color: white; font-size: 14px; cursor: pointer; display: flex; align-items: center; justify-content: center; z-index: 10; }
            .fav-btn.active { color: #ef4444; }

            /* Floating Buttons */
            .floating-btn { position: fixed; right: 15px; width: 50px; height: 50px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 20px; z-index: 500; cursor: pointer; box-shadow: 0 4px 15px rgba(0,0,0,0.5); border: 2px solid white; text-decoration: none; color: white; }
            .btn-tg { bottom: 160px; background: linear-gradient(45deg, #24A1DE, #1b7ba8); }
            .btn-18 { bottom: 100px; background: linear-gradient(45deg, #ef4444, #b91c1c); font-weight: bold; }

            .bottom-nav { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); border-top: 1px solid #1e293b; display: flex; justify-content: space-around; padding: 10px 0; z-index: 1000; }
            .nav-item { display: flex; flex-direction: column; align-items: center; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer; border: none; background: none; }
            .nav-item i { font-size: 20px; margin-bottom: 3px; }
            .nav-item.active { color: #ef4444; }

            .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: flex-end; justify-content: center; z-index: 3000; }
            .modal-content { background: #1e293b; width: 100%; max-width: 400px; padding: 25px; border-radius: 20px 20px 0 0; max-height: 90vh; overflow-y: auto; position: relative; }
            .detail-img { width: 100%; height: 250px; object-fit: cover; border-radius: 12px; margin-bottom: 15px; }
            .detail-title { font-size: 22px; font-weight: 800; margin-bottom: 5px; }
            .detail-meta { color: #94a3b8; font-size: 14px; margin-bottom: 15px; }
            .close-icon { position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: rgba(0,0,0,0.6); color: #fff; font-size: 18px; display: flex; align-items: center; justify-content: center; cursor: pointer; border: none; }

            .dl-file-btn { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 15px; background: #0f172a; border: 1px solid #334155; color: white; font-weight: 700; border-radius: 10px; margin-bottom: 10px; cursor: pointer; }
            .dl-file-btn i { color: #ef4444; font-size: 18px; }
            .dl-file-btn.unlocked i { color: #10b981; }
            .share-btn { width: 100%; padding: 15px; border-radius: 12px; background: #334155; color: white; font-weight: 700; border: none; font-size: 16px; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 8px; margin-top: 10px; }

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
            .profile-card { background: #1e293b; margin: 15px; border-radius: 16px; padding: 20px; border: 1px solid #334155; }
            .profile-link { display: flex; align-items: center; gap: 12px; padding: 12px 0; border-bottom: 1px solid #334155; color: white; text-decoration: none; font-weight: 600; }
            .profile-link:last-child { border: none; }
            .profile-link i { width: 30px; text-align: center; font-size: 20px; color: #3b82f6; }
            
            .skeleton { background: #1e293b; border-radius: 12px; height: 160px; position: relative; overflow: hidden; }
            .skeleton::after { content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.05), transparent); animation: shimmer 1.5s infinite; }
            @keyframes shimmer { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }
        </style>
    </head>
    <body>
        <div id="welcomeScreen"><div class="ws-brand">Movie Box</div><div class="ws-bn">মুভি বক্স জগতে স্বাগতম</div></div>
        <header onclick="switchTab('home')"><div class="logo">Movie Box</div></header>

        <div id="tabHome" class="page-section active">
            <div class="search-box"><input type="text" id="searchInput" class="search-input" placeholder="🔍 খুঁজুন..."></div>
            <div class="cat-row">
                <div class="cat-chip active" onclick="filterCat('Home', this)">HOME</div>
                <div class="cat-chip" onclick="filterCat('Bangla', this)">BANGLA</div>
                <div class="cat-chip" onclick="filterCat('Hindi Dubbed', this)">HINDI DUBBED</div>
                <div class="cat-chip" onclick="filterCat('Hollywood', this)">HOLLYWOOD</div>
                <div class="cat-chip" onclick="filterCat('K-Drama', this)">K-DRAMA</div>
                <div class="cat-chip" onclick="filterCat('Horror', this)">HORROR</div>
                <div class="cat-chip" onclick="verify18(this)">ADULT CONTENT</div>
            </div>
            <div class="movie-list" id="movieListHome"><div class="skeleton"></div><div class="skeleton"></div></div>
        </div>

        <div id="tabSearch" class="page-section"><div class="search-box" style="padding-top:15px;"><input type="text" id="searchInputMain" class="search-input" placeholder="🔍 সার্চ..." oninput="searchMovies()"></div><div class="movie-list" id="movieListSearch"></div></div>
        <div id="tabFav" class="page-section"><h3 style="padding: 15px; color: #fbbf24;">❤️ ফেভারিট</h3><div class="movie-list" id="movieListFav"></div></div>
        <div id="tabUpcoming" class="page-section"><h3 style="padding: 15px; color: #38bdf8;">🌟 আপকামিং</h3><div class="movie-list" id="movieListUpcoming"></div></div>
        <div id="tabProfile" class="page-section"><div class="profile-card"><div style="text-align: center;"><h2 id="profileName">User</h2></div><div id="profileLinksContainer"></div></div></div>

        <!-- Floating Buttons Provided By User -->
        <a href="https://t.me/addlist/MwbWNafSFK4yZjhl" class="floating-btn btn-tg"><i class="fa-brands fa-telegram"></i></a>
        <a href="https://t.me/+W5V9-mn08jMyYTE1" class="floating-btn btn-18">18+</a>

        <div class="bottom-nav">
            <button class="nav-item active" onclick="switchTab('home', this)"><i class="fa-solid fa-house"></i>Home</button>
            <button class="nav-item" onclick="switchTab('search', this)"><i class="fa-solid fa-magnifying-glass"></i>Search</button>
            <button class="nav-item" onclick="switchTab('fav', this)"><i class="fa-solid fa-heart"></i>Favorites</button>
            <button class="nav-item" onclick="switchTab('upcoming', this)"><i class="fa-solid fa-clock"></i>Upcoming</button>
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
                <button class="share-btn" onclick="shareMovie()"><i class="fa-solid fa-share-nodes"></i> Share</button>
            </div>
        </div>

        <div id="adModal" class="modal">
            <div class="modal-content ad-box">
                <div class="ad-icon">⚠️</div>
                <h2 class="ad-title">সতর্কতা!</h2>
                <div class="ad-box-orange">ডাউনলোড করতে হলে অবশ্যই বিজ্ঞাপন দেখুন!</div>
                <div class="ad-box-black">ক্লিক করে কমপক্ষে <b>১৫ সেকেন্ড</b> অপেক্ষা করুন। আগে বন্ধ করলে বাতিল হবে!</div>
                <p id="adTimerText" class="ad-timer-text">অপেক্ষা করুন... <span id="timerCount">15</span>s</p>
                <button class="ad-action-btn btn-ad-open" id="adClickBtn" onclick="openAdLink()">বিজ্ঞাপন খুলুন</button>
                <button class="ad-action-btn btn-ad-tryagain" id="adTryAgainBtn" onclick="adTryAgainAction()" style="display:none;">TRY AGAIN</button>
            </div>
        </div>

        <div id="successModal" class="modal"><div class="modal-content" style="text-align: center; padding-top: 40px;"><i class="fa-solid fa-circle-check" style="font-size:70px; color:#4ade80; margin-bottom:20px;"></i><h2>ফাইল পাঠানো হয়েছে!</h2><br><button class="dl-file-btn unlocked" onclick="closeModal('successModal'); tg.close();"><i class="fa-solid fa-check"></i> বটে যান</button></div></div>

        <script>
            let tg = window.Telegram.WebApp; 
            tg.expand();
            const DIRECT_LINKS = {{DIRECT_LINKS}}; 
            const INIT_DATA = tg.initData || ""; 
            const BOT_UNAME = "{{BOT_USER}}";
            let uid = tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user.id : 0; 
            let isUserVip = false; 
            let activeCat = "Home"; 
            let userFavs = []; 
            let active18Btn = null; 
            let activeFileId = null;
            let adInterval = null; 
            let adTimeLeft = 15; 
            let adCompleted = false; 
            let adAborted = false;

            // FIX: Caching Array to prevent onclick JSON parse errors
            let currentViewMovies = [];

            setTimeout(function() { document.getElementById('welcomeScreen').classList.add('hide'); }, 2500);
            if(tg.initDataUnsafe && tg.initDataUnsafe.user) { document.getElementById('profileName').innerText = tg.initDataUnsafe.user.first_name; }

            async function fetchUserInfo() { 
                try { const res = await fetch('/api/user/' + uid); const data = await res.json(); isUserVip = data.vip; } catch(e) {} 
            }
            
            async function fetchProfileLinks() { 
                try { const res = await fetch('/api/profile'); const data = await res.json(); let html = ''; if(data.tg_link) html += '<a href="'+data.tg_link+'" class="profile-link"><i class="fa-brands fa-telegram"></i> Join Channel</a>'; document.getElementById('profileLinksContainer').innerHTML = html || '<p style="color:#64748b; text-align:center;">কোনো লিংক নেই।</p>'; } catch(e) {} 
            }

            function switchTab(tabName, btnEl) { 
                document.querySelectorAll('.page-section').forEach(function(el) { el.classList.remove('active'); }); 
                document.querySelectorAll('.nav-item').forEach(function(el) { el.classList.remove('active'); }); 
                document.getElementById('tab' + tabName.charAt(0).toUpperCase() + tabName.slice(1)).classList.add('active'); 
                if(btnEl) btnEl.classList.add('active'); 
                if(tabName === 'home') loadHomeMovies(); 
                if(tabName === 'fav') loadFavorites(); 
                if(tabName === 'upcoming') loadUpcoming(); 
                if(tabName === 'profile') fetchProfileLinks(); 
                window.scrollTo({top:0, behavior:'smooth'}); 
            }
            
            function filterCat(cat, btnEl) { activeCat = cat; document.querySelectorAll('.cat-chip').forEach(function(el) { el.classList.remove('active'); }); btnEl.classList.add('active'); loadHomeMovies(); }
            function verify18(btnEl) { active18Btn = btnEl; if(localStorage.getItem('isAdult')) { filterCat('Adult Content', btnEl); } else { document.getElementById('ageModal').style.display = 'flex'; } }
            function access18() { localStorage.setItem('isAdult', 'true'); closeModal('ageModal'); filterCat('Adult Content', active18Btn); }
            function closeModal(id) { document.getElementById(id).style.display = 'none'; }

            async function loadHomeMovies() { 
                const list = document.getElementById('movieListHome'); 
                list.innerHTML = '<div class="skeleton"></div>'; 
                try { 
                    const res = await fetch('/api/list?cat='+activeCat+'&uid='+uid); 
                    const data = await res.json(); 
                    currentViewMovies = data.movies || []; 
                    list.innerHTML = currentViewMovies.length > 0 ? currentViewMovies.map(function(m, index) { return createMovieCard(m, index); }).join('') : '<p style="text-align:center; color:#64748b; padding:30px;">কোনো মুভি পাওয়া যায়নি!</p>'; 
                } catch(e) {} 
            }
            
            async function searchMovies() { 
                const q = document.getElementById('searchInputMain').value.trim(); const list = document.getElementById('movieListSearch'); 
                if(!q) { list.innerHTML = ''; return; } 
                try { 
                    const res = await fetch('/api/list?q='+encodeURIComponent(q)+'&uid='+uid); 
                    const data = await res.json(); 
                    currentViewMovies = data.movies || []; 
                    list.innerHTML = currentViewMovies.length > 0 ? currentViewMovies.map(function(m, index) { return createMovieCard(m, index); }).join('') : '<p style="text-align:center; color:#64748b;">খুঁজে পাওয়া যায়নি!</p>'; 
                } catch(e) {} 
            }

            function createMovieCard(m, index) { 
                let isFav = userFavs.includes(m._id); 
                let catsHtml = (m.categories || []).map(function(c) { return '<span class="movie-cat-tag">'+c+'</span>'; }).join(''); 
                // FIX: Passing index instead of JSON object to prevent quote syntax errors
                return '<div class="movie-card" onclick="openDetail('+index+')"><img src="/api/image/'+m.photo_id+'" onerror="this.src=\'https://via.placeholder.com/110x160\'"><div class="movie-info"><div class="movie-title">'+m._id+'</div><div class="movie-meta"><span>'+(m.year || 'N/A')+'</span><span>'+(m.files ? m.files.length : 0)+' Files</span></div><div class="movie-cats">'+catsHtml+'</div></div><button class="fav-btn '+(isFav ? 'active' : '')+'" onclick="event.stopPropagation(); toggleFav(\''+m._id+'\', this)"><i class="fa-solid fa-heart"></i></button></div>'; 
            }

            // FIX: Using index to safely get data
            function openDetail(index) { 
                let m = currentViewMovies[index];
                if(!m) return;
                
                document.getElementById('detailImg').src = '/api/image/'+m.photo_id; 
                document.getElementById('detailTitle').innerText = m._id; 
                document.getElementById('detailMeta').innerHTML = '<span>'+(m.year || 'N/A')+'</span>'; 
                document.getElementById('detailCats').innerHTML = (m.categories || []).map(function(c) { return '<span class="movie-cat-tag">'+c+'</span>'; }).join(' '); 
                let btnsHtml = m.files.map(function(f) { 
                    let isFree = f.is_unlocked || isUserVip; 
                    return '<button class="dl-file-btn '+(isFree ? 'unlocked' : '')+'" onclick="handleFileClick(\''+f.id+'\', '+(isFree ? 'true' : 'false')+')"><span><i class="fa-solid fa-'+(isFree ? 'lock-open' : 'lock')+'"></i> Download '+f.quality+'</span></button>'; 
                }).join(''); 
                document.getElementById('fileButtonsContainer').innerHTML = btnsHtml; 
                document.getElementById('detailModal').style.display = 'flex'; 
            }

            function handleFileClick(fileId, isFree) { 
                activeFileId = fileId; 
                if(isFree) { sendFileRequest(fileId); } else { closeModal('detailModal'); resetAdModal(); document.getElementById('adModal').style.display = 'flex'; } 
            }

            function resetAdModal() { clearInterval(adInterval); adTimeLeft = 15; adCompleted = false; adAborted = false; document.getElementById('adTimerText').style.display = 'none'; document.getElementById('adClickBtn').style.display = 'block'; document.getElementById('adClickBtn').className = 'ad-action-btn btn-ad-open'; document.getElementById('adTryAgainBtn').style.display = 'none'; }
            
            function handleAppFocus() { 
                if(!adCompleted && !adAborted && adTimeLeft > 0) { 
                    clearInterval(adInterval); adAborted = true; 
                    document.getElementById('adTimerText').style.display = 'none'; document.getElementById('adClickBtn').style.display = 'none'; 
                    document.getElementById('adTryAgainBtn').style.display = 'block'; document.getElementById('adTryAgainBtn').innerText = 'TRY AGAIN'; document.getElementById('adTryAgainBtn').className = 'ad-action-btn btn-ad-tryagain'; 
                    window.removeEventListener('focus', handleAppFocus); 
                } 
            }
            
            function openAdLink() { 
                if (DIRECT_LINKS && DIRECT_LINKS.length > 0) { tg.openLink(DIRECT_LINKS[Math.floor(Math.random() * DIRECT_LINKS.length)]); } 
                document.getElementById('adClickBtn').style.display = 'none'; document.getElementById('adTimerText').style.display = 'block'; 
                window.addEventListener('focus', handleAppFocus); 
                adInterval = setInterval(function() { 
                    adTimeLeft--; document.getElementById('timerCount').innerText = adTimeLeft; 
                    if(adTimeLeft <= 0) { clearInterval(adInterval); adCompleted = true; window.removeEventListener('focus', handleAppFocus); document.getElementById('adTimerText').style.display = 'none'; document.getElementById('adTryAgainBtn').style.display = 'block'; document.getElementById('adTryAgainBtn').innerText = 'UNLOCK FILE'; document.getElementById('adTryAgainBtn').className = 'ad-action-btn btn-ad-unlock'; } 
                }, 1000); 
            }
            
            function adTryAgainAction() { if(adCompleted) { closeModal('adModal'); sendFileRequest(activeFileId); } else { resetAdModal(); } }

            async function sendFileRequest(fileId) { 
                try { const res = await fetch('/api/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({userId: uid, movieId: fileId, initData: INIT_DATA})}); const data = await res.json(); if(data.ok) { closeModal('detailModal'); document.getElementById('successModal').style.display = 'flex'; fetchUserInfo(); } else { tg.showAlert("⚠️ Failed!"); } } catch(e) {} 
            }
            
            function shareMovie() { const t = document.getElementById('detailTitle').innerText; tg.openTelegramLink('https://t.me/share/url?url=https://t.me/'+BOT_UNAME+'?start=new&text=Watch '+t); }

            async function loadFavorites() { const list = document.getElementById('movieListFav'); list.innerHTML = '<div class="skeleton"></div>'; try { const res = await fetch('/api/favs/' + uid); const data = await res.json(); userFavs = data.map(function(m) { return m._id; }); currentViewMovies = data; list.innerHTML = data.length > 0 ? data.map(function(m, index) { return createMovieCard(m, index); }).join('') : '<p style="text-align:center; color:#64748b; padding:30px;">কোনো ফেভারিট নেই!</p>'; } catch(e) {} }
            
            async function toggleFav(title, btnEl) { 
                try { const res = await fetch('/api/fav/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({uid: uid, title: title, initData: INIT_DATA})}); const data = await res.json(); if(data.isFav) { btnEl.classList.add('active'); userFavs.push(title); } else { btnEl.classList.remove('active'); userFavs = userFavs.filter(function(t) { return t !== title; }); } } catch(e) {} 
            }
            
            async function loadUpcoming() { const list = document.getElementById('movieListUpcoming'); list.innerHTML = '<div class="skeleton"></div>'; try { const res = await fetch('/api/upcoming'); const data = await res.json(); currentViewMovies = data; list.innerHTML = data.length > 0 ? data.map(function(m, index) { return '<div class="movie-card" onclick="openDetail('+index+')"><img src="/api/image/'+m.photo_id+'"><div class="movie-info"><div class="movie-title">'+m.title+'</div></div></div>'; }).join('') : '<p style="text-align:center; color:#64748b;">কোনো আপকামিং নেই!</p>'; } catch(e) {} }

            document.getElementById('searchInput').addEventListener('focus', function() { document.querySelector('.nav-item:nth-child(2)').click(); setTimeout(function() { document.getElementById('searchInputMain').focus(); }, 100); });
            fetchUserInfo(); loadHomeMovies(); loadFavorites();
        </script>
    </body>
    </html>
    """
    html_code = html_code.replace("{{DIRECT_LINKS}}", dl_json).replace("{{BOT_USER}}", BOT_USERNAME).replace("{{BKASH_NO}}", bkash_no).replace("{{NAGAD_NO}}", nagad_no)
    return html_code


# ==========================================
# 12. Main Web App APIs (Syntax Fixed)
# ==========================================
@app.get("/api/user/{uid}")
async def get_user_info(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user:
        return {"vip": False}
    now = datetime.datetime.utcnow()
    vip_until = user.get("vip_until")
    is_vip = vip_until and vip_until > now
    return {"vip": is_vip}

@app.get("/api/profile")
async def get_profile_links():
    cfg = await db.settings.find_one({"id": "profile"})
    tg_link = cfg.get("tg_link", "") if cfg else ""
    return {"tg_link": tg_link}

@app.get("/api/list")
async def list_movies(page: int = 1, q: str = "", uid: int = 0, cat: str = "Home"):
    if uid in banned_cache:
        return {"movies": []}
    limit = 20
    unlocked_ids = []
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            unlocked_ids.append(u["movie_id"])
            
    match_stage = {}
    if q:
        match_stage["title"] = {"$regex": q, "$options": "i"}
    if cat and cat != "Home":
        match_stage["categories"] = {"$in": [cat]}
        
    pipeline = [
        {"$match": match_stage}, 
        {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "clicks": {"$sum": "$clicks"}, "created_at": {"$max": "$created_at"}, "year": {"$first": "$year"}, "categories": {"$first": "$categories"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main"]}}}}}, 
        {"$sort": {"created_at": -1}}, 
        {"$skip": (page - 1) * limit}, 
        {"$limit": limit}
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
        async def stream():
            async with aiohttp.ClientSession() as s:
                async with s.get(file_url) as r:
                    async for c in r.content.iter_chunked(1024):
                        yield c
        return StreamingResponse(stream(), media_type="image/jpeg")
    except:
        return {"error": "not found"}

class SendRequestModel(BaseModel):
    userId: int
    movieId: str
    initData: str

@app.post("/api/send")
async def send_file(d: SendRequestModel):
    if d.userId == 0 or d.userId in banned_cache or not validate_tg_data(d.initData):
        return {"ok": False}
    try:
        m = await db.movies.find_one({"_id": ObjectId(d.movieId)})
        if m:
            now = datetime.datetime.utcnow()
            user_data = await db.users.find_one({"user_id": d.userId})
            is_vip = user_data and user_data.get("vip_until", now) > now
            time_cfg = await db.settings.find_one({"id": "del_time"})
            del_minutes = time_cfg['minutes'] if time_cfg else 60
            caption = f"🎥 <b>{m['title']} [{m.get('quality', '')}]</b>"
            
            if m.get("file_type") == "video":
                sent_msg = await bot.send_video(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=False)
            else:
                sent_msg = await bot.send_document(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=False)
                
            await db.movies.update_one({"_id": ObjectId(d.movieId)}, {"$inc": {"clicks": 1}})
            await db.user_unlocks.update_one({"user_id": d.userId, "movie_id": d.movieId}, {"$set": {"unlocked_at": now}}, upsert=True)
            
            if sent_msg and not is_vip:
                delete_at = now + datetime.timedelta(minutes=del_minutes)
                await db.auto_delete.insert_one({"chat_id": d.userId, "message_id": sent_msg.message_id, "delete_at": delete_at})
        return {"ok": True}
    except:
        return {"ok": False}

@app.get("/api/favs/{uid}")
async def get_favs(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user:
        return []
    fav_titles = user.get("favorites", [])
    if not fav_titles:
        return []
    pipeline = [
        {"$match": {"title": {"$in": fav_titles}}}, 
        {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "year": {"$first": "$year"}, "categories": {"$first": "$categories"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main"]}}}}}
    ]
    return await db.movies.aggregate(pipeline).to_list(len(fav_titles))

class FavModel(BaseModel):
    uid: int
    title: str
    initData: str

@app.post("/api/fav/toggle")
async def toggle_fav(data: FavModel):
    if not validate_tg_data(data.initData):
        return {"isFav": False}
    user = await db.users.find_one({"user_id": data.uid})
    favs = user.get("favorites", []) if user else []
    if data.title in favs:
        await db.users.update_one({"user_id": data.uid}, {"$pull": {"favorites": data.title}})
        return {"isFav": False}
    else:
        await db.users.update_one({"user_id": data.uid}, {"$push": {"favorites": data.title}})
        return {"isFav": True}

class PaymentModel(BaseModel):
    uid: int
    method: str
    trx_id: str
    days: int
    price: int
    initData: str

@app.post("/api/payment/submit")
async def submit_payment(data: PaymentModel):
    if not validate_tg_data(data.initData):
        return {"ok": False}
    if await db.payments.find_one({"trx_id": data.trx_id}):
        return {"ok": False, "msg": "TrxID used!"}
    res = await db.payments.insert_one({"user_id": data.uid, "method": data.method, "trx_id": data.trx_id, "amount": data.price, "days": data.days, "status": "pending"})
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Approve", callback_data=f"trx_approve_{res.inserted_id}")
        builder.button(text="❌ Reject", callback_data=f"trx_reject_{res.inserted_id}")
        await bot.send_message(OWNER_ID, f"💰 <b>Payment!</b>\n👤 <code>{data.uid}</code>\n🏦 {data.method.upper()}\n🧾 <code>{data.trx_id}</code>\n💵 {data.price} BDT", parse_mode="HTML", reply_markup=builder.as_markup())
    except:
        pass
    return {"ok": True}

# ==========================================
# 13. Main Application Startup
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
