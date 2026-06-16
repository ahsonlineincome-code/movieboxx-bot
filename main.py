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

try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

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

TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))
APP_URL = os.getenv("APP_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1003904328439")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
BOT_USERNAME = os.getenv("BOT_USERNAME", "bdlatestmovie_bot")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "-1003708048942")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()
security = HTTPBasic()

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

client = AsyncIOMotorClient(MONGO_URL)
db = client['movie_database']

admin_cache = set([OWNER_ID])
banned_cache = set()
CATEGORIES = ["Bangla", "Bangla Dubbed", "Hindi Dubbed", "Hollywood", "K-Drama", "Anime", "Horror", "Web Series", "Adult Content"]
broadcast_queue = asyncio.Queue()

file_path_cache = {}
aiohttp_session = None

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
    waiting_for_upload_movie = State()
    waiting_for_upload_quality = State()
    waiting_for_upload_file = State()

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

def validate_tg_data(init_data: str) -> bool:
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        hash_val = parsed_data.pop('hash', None)
        auth_date = int(parsed_data.get('auth_date', 0))
        if not hash_val or time.time() - auth_date > 86400: return False
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return calculated_hash == hash_val
    except: return False

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (correct_username and correct_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect", headers={"WWW-Authenticate": "Basic"})
    return True

async def auto_delete_worker():
    while True:
        try:
            now = datetime.datetime.utcnow()
            async for msg in db.auto_delete.find({"delete_at": {"$lte": now}}):
                try: await bot.delete_message(chat_id=msg["chat_id"], message_id=msg["message_id"])
                except: pass
                await db.auto_delete.delete_one({"_id": msg["_id"]})
                await asyncio.sleep(0.5)
        except: pass
        await asyncio.sleep(60)

async def auto_lock_worker():
    while True:
        try:
            expire_time = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
            result = await db.user_unlocks.delete_many({"unlocked_at": {"$lte": expire_time}})
        except Exception as e: print(f"Auto-lock worker error: {e}")
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
    global aiohttp_session
    aiohttp_session = aiohttp.ClientSession()
    await init_db()
    await load_admins()
    await load_banned_users()
    asyncio.create_task(auto_delete_worker())
    asyncio.create_task(broadcast_queue_worker())
    asyncio.create_task(auto_lock_worker())

@app.on_event("shutdown")
async def on_shutdown():
    global aiohttp_session
    if aiohttp_session: await aiohttp_session.close()

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if uid in banned_cache: return await message.answer("🚫 আপনাকে ব্যান করা হয়েছে", parse_mode="HTML")
    await state.clear()
    
    args = message.text.split()
    if len(args) > 1 and args[1] == "addupload" and uid in admin_cache:
        return await add_upload_movie_start(message, state)

    now = datetime.datetime.utcnow()
    user = await db.users.find_one({"user_id": uid})
    if not user:
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
    kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))], [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link), types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)]]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    text = f"👋 <b>স্বাগতম {message.from_user.first_name}!</b>\n\n🎬 Movie Box জগতে আপনাকে স্বাগতম"
    if uid in admin_cache: text += "\n\n⚙️ <b>অ্যাডমিন মোড অন</b>"
    await message.answer(text, reply_markup=markup, parse_mode="HTML")

@dp.message(Command("stats"))
async def bot_stats(m: types.Message):
    if m.from_user.id not in admin_cache: return
    total_users = await db.users.count_documents({})
    total_movies = await db.movies.count_documents({})
    vip_users = await db.users.count_documents({"vip_until": {"$gt": datetime.datetime.utcnow()}})
    await m.answer(f"📊 Bot Statistics\n\n👥 Users: {total_users}\n💎 VIP: {vip_users}\n🎬 Movies: {total_movies}", parse_mode="HTML")

@dp.message(Command("ban"))
async def ban_user(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        uid = int(m.text.split()[1])
        await db.banned.update_one({"user_id": uid}, {"$set": {"user_id": uid}}, upsert=True)
        banned_cache.add(uid)
        await m.answer(f"🚫 Banned {uid}", parse_mode="HTML")
    except: pass

@dp.message(Command("unban"))
async def unban_user(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        uid = int(m.text.split()[1])
        await db.banned.delete_one({"user_id": uid})
        banned_cache.discard(uid)
        await m.answer(f"✅ Unbanned {uid}", parse_mode="HTML")
    except: pass

@dp.message(Command("cancel"))
async def cancel_cmd(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.clear()
    await m.answer("❌ Process Cancelled!", parse_mode="HTML")

@dp.message(Command("addvip"))
async def add_vip_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        args = m.text.split()
        target_uid = int(args[1])
        days = int(args[2]) if len(args) > 2 else 30
        now = datetime.datetime.utcnow()
        user = await db.users.find_one({"user_id": target_uid})
        if not user: return await m.answer("⚠️ ইউজার নেই", parse_mode="HTML")
        current_vip = user.get("vip_until", now)
        if current_vip < now: current_vip = now
        await db.users.update_one({"user_id": target_uid}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=days)}})
        await m.answer(f"✅ <code>{target_uid}</code> কে {days} দিনের VIP দেওয়া হয়েছে!", parse_mode="HTML")
    except: await m.answer("⚠️ /addvip ID DAYS", parse_mode="HTML")

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        title = m.text.split(" ", 1)[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0: await m.answer(f"✅ '<b>{title}</b>' ডিলিট হয়েছে!", parse_mode="HTML")
        else: await m.answer("⚠️ পাওয়া যায়নি")
    except: await m.answer("⚠️ /delmovie মুভির নাম", parse_mode="HTML")

@dp.message(Command("adduploadmovie"))
async def add_upload_movie_start(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_upload_movie)
    await m.answer("📤 আপলোড মুভি অ্যাড করতে প্রথমে <b>মুভির নাম</b> লিখুন:", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upload_movie, F.text)
async def receive_upload_movie_title(m: types.Message, state: FSMContext):
    title = m.text.strip()
    movie = await db.movies.find_one({"title": title})
    if not movie:
        await m.answer("❌ এই নামের কোনো মুভি ডেটাবেসে নেই", parse_mode="HTML")
        await state.clear()
        return
    await state.update_data(title=title, photo_id=movie["photo_id"], year=movie.get("year", "N/A"), categories=movie.get("categories", []))
    await state.set_state(AdminStates.waiting_for_upload_quality)
    await m.answer("✅ এবার <b>নতুন কোয়ালিটি</b> লিখুন (যেমন: 720p):", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upload_quality, F.text)
async def receive_upload_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=m.text.strip())
    await state.set_state(AdminStates.waiting_for_upload_file)
    await m.answer("✅ এবার <b>ভিডিও/ফাইল</b> পাঠান:", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upload_file, F.content_type.in_({'video', 'document'}))
async def receive_upload_file(m: types.Message, state: FSMContext):
    data = await state.get_data()
    fid = m.video.file_id if m.video else m.document.file_id
    ftype = "video" if m.video else "document"
    await state.clear()
    await db.movies.insert_one({"title": data["title"], "quality": data["quality"], "photo_id": data["photo_id"], "file_id": fid, "file_type": ftype, "year": data.get("year", "N/A"), "categories": data.get("categories", []), "clicks": 0, "created_at": datetime.datetime.utcnow()})
    await m.answer(f"✅ <b>{data['title']} [{data['quality']}]</b> Added! \n\n⚠️ No Broadcast sent (Quality Update)", parse_mode="HTML")

@dp.message(F.content_type.in_({'video', 'document'}), lambda m: m.from_user.id in admin_cache)
async def receive_movie_file(m: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None: return
    fid = m.video.file_id if m.video else m.document.file_id
    ftype = "video" if m.video else "document"
    await state.set_state(AdminStates.waiting_for_photo)
    await state.update_data(file_id=fid, file_type=ftype, categories=[])
    await m.answer("✅ File received! Send the <b>Poster</b>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_photo, F.photo)
async def receive_movie_photo(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_title)
    await m.answer("✅ Send <b>Movie Title</b>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer("✅ Send <b>Quality</b> (e.g. 720p)", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=m.text.strip())
    await state.set_state(AdminStates.waiting_for_year)
    await m.answer("✅ Send <b>Release Year</b>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_year, F.text)
async def receive_movie_year(m: types.Message, state: FSMContext):
    await state.update_data(year=m.text.strip())
    await state.set_state(AdminStates.waiting_for_cats)
    builder = InlineKeyboardBuilder()
    for i, cat in enumerate(CATEGORIES): builder.button(text=cat, callback_data=f"selcat_{i}")
    builder.button(text="✅ Done", callback_data="cats_done")
    builder.adjust(2)
    await m.answer("✅ Select <b>Categories</b>:", reply_markup=builder.as_markup(), parse_mode="HTML")

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
    if not selected_cats: return await c.answer("⚠️ Select at least 1!", show_alert=True)
    await state.clear()
    await db.movies.insert_one({"title": data["title"], "quality": data["quality"], "photo_id": data["photo_id"], "file_id": data["file_id"], "file_type": data["file_type"], "year": data.get("year", "N/A"), "categories": selected_cats, "clicks": 0, "created_at": datetime.datetime.utcnow()})
    await c.message.edit_text(f"🎉 <b>{data['title']} [{data['quality']}]</b> Added!\n⏳ Broadcast queued...", parse_mode="HTML")
    await broadcast_queue.put({"data": data, "selected_cats": selected_cats, "admin_id": c.from_user.id})
    await c.answer()

async def run_movie_broadcast(data, selected_cats, admin_id):
    bcast_success = 0
    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"
    bcast_kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))], [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link)], [types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)]]
    bcast_markup = types.InlineKeyboardMarkup(inline_keyboard=bcast_kb)
    bcast_text = f"🆕 <b>New Movie!</b>\n\n🎬 <b>{data['title']}</b>\n📺 {data['quality']}\n\n👇 Watch Now!"
    delete_at = datetime.datetime.utcnow() + datetime.timedelta(days=1)
    async for u in db.users.find():
        try:
            sent_msg = await bot.send_photo(u['user_id'], photo=data["photo_id"], caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
            await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at})
            bcast_success += 1
            await asyncio.sleep(0.05)
        except: pass
    try: await bot.send_message(admin_id, f"✅ Broadcast Done: {bcast_success}", parse_mode="HTML")
    except: pass

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_bcast)
    await m.answer("📢 ব্রডকাস্ট মেসেজ পাঠান:\n\n⚠️ বাতিল করতে /cancel লিখুন", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_bcast)
async def execute_broadcast(m: types.Message, state: FSMContext):
    if m.text and m.text.startswith("/"):
        await state.clear(); await m.answer("⚠️ বাতিল হয়েছে", parse_mode="HTML"); return
    await state.clear()
    prog_msg = await m.answer("⏳ <b>Broadcast started...</b>", parse_mode="HTML")
    asyncio.create_task(run_manual_broadcast(m, prog_msg, m.from_user.id))

async def run_manual_broadcast(m, prog_msg, admin_id):
    total_users = await db.users.count_documents({}); success = 0; blocked = 0
    async for u in db.users.find():
        try: await m.copy_to(chat_id=u['user_id']); success += 1; await asyncio.sleep(0.05)
        except: blocked += 1
    try: await prog_msg.edit_text(f"✅ Broadcast Complete!\n👥 Total: {total_users}\n✅ Success: {success}\n🚫 Blocked: {blocked}", parse_mode="HTML")
    except: pass

@app.get("/img/{file_id}")
async def serve_image(file_id: str):
    if file_id in file_path_cache: return RedirectResponse(url=file_path_cache[file_id])
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/getFile?file_id={file_id}"
        async with aiohttp_session.get(url) as resp:
            data = await resp.json()
            if data.get("ok"):
                img_url = f"https://api.telegram.org/file/bot{TOKEN}/{data['result']['file_path']}"
                file_path_cache[file_id] = img_url
                return RedirectResponse(url=img_url)
    except: pass
    return RedirectResponse(url="https://via.placeholder.com/320x180?text=No+Poster")

@app.get("/dl/{file_id}")
async def download_file(file_id: str):
    if file_id in file_path_cache: return RedirectResponse(url=file_path_cache[file_id])
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/getFile?file_id={file_id}"
        async with aiohttp_session.get(url) as resp:
            data = await resp.json()
            if data.get("ok"):
                dl_url = f"https://api.telegram.org/file/bot{TOKEN}/{data['result']['file_path']}"
                file_path_cache[file_id] = dl_url
                return RedirectResponse(url=dl_url)
    except: pass
    raise HTTPException(status_code=404)

@app.get("/api/trending")
async def get_trending_movies():
    seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    pipeline = [{"$match": {"unlocked_at": {"$gte": seven_days_ago}}}, {"$group": {"_id": "$title", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 5}]
    trending_titles = await db.user_unlocks.aggregate(pipeline).to_list(5)
    movies = []
    for t in trending_titles:
        movie = await db.movies.find_one({"title": t["_id"]})
        if movie: movie["_id"] = str(movie["_id"]); movies.append(movie)
    return movies

@app.get("/api/movies")
async def get_movies(category: str = "All", search: str = "", page: int = 1):
    query = {}
    if category != "All": query["categories"] = category
    if search: query["$or"] = [{"title": {"$regex": search, "$options": "i"}}, {"year": {"$regex": search, "$options": "i"}}]
    per_page = 10; total = await db.movies.count_documents(query)
    total_pages = math.ceil(total / per_page) if total > 0 else 1
    movies = await db.movies.find(query).sort("created_at", -1).skip((page - 1) * per_page).limit(per_page).to_list(per_page)
    for m in movies: m["_id"] = str(m["_id"])
    return {"movies": movies, "total_pages": total_pages, "current_page": page}

@app.get("/api/movie/{title}")
async def get_movie_detail(title: str):
    movies = await db.movies.find({"title": title}).to_list(10)
    if not movies: raise HTTPException(status_code=404)
    base = movies[0]
    qualities = [{"quality": m.get("quality", "Main"), "file_id": m["file_id"], "file_type": m["file_type"], "_id": str(m["_id"])} for m in movies]
    return {"title": base["title"], "photo_id": base["photo_id"], "year": base.get("year", "N/A"), "categories": base.get("categories", []), "clicks": base.get("clicks", 0), "qualities": qualities}

@app.post("/api/movie/click/{title}")
async def increment_click(title: str):
    await db.movies.update_one({"title": title}, {"$inc": {"clicks": 1}})

@app.post("/api/unlock")
async def save_unlock(request: Request):
    data = await request.json()
    await db.user_unlocks.update_one({"user_id": data["user_id"], "title": data["title"]}, {"$set": {"unlocked_at": datetime.datetime.utcnow()}}, upsert=True)

@app.get("/panel", response_class=HTMLResponse)
async def admin_panel_ui(auth: bool = Depends(verify_admin)):
    return HTMLResponse('<h1>Admin Panel</h1><p>Use bot to upload movies.</p>')

@app.get("/", response_class=HTMLResponse)
async def web_ui():
    dl_cfg = await db.settings.find_one({"id": "direct_links"}); dl_json = json.dumps(dl_cfg.get('links', []) if dl_cfg else [])
    adl_cfg = await db.settings.find_one({"id": "adult_direct_links"}); adl_json = json.dumps(adl_cfg.get('links', []) if adl_cfg else [])

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
            body { background: #0f172a; font-family: system-ui, sans-serif; color: #fff; } 
            #welcomeScreen { position: fixed; top:0; left:0; width:100%; height:100%; background: #0f172a; z-index: 99999; display: flex; flex-direction: column; align-items: center; justify-content: center; transition: opacity 0.8s ease; }
            #welcomeScreen.hide { opacity: 0; visibility: hidden; }
            .ws-brand { font-size: 48px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            header { display: flex; justify-content: center; align-items: center; padding: 15px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: rgba(15, 23, 42, 0.95); z-index: 1000; cursor: pointer; }
            .logo { font-size: 24px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .page-section { display: none; padding-bottom: 80px; }
            .page-section.active { display: block; }
            .cat-row { display: flex; flex-wrap: wrap; gap: 8px; padding: 15px; }
            .cat-chip { background: #1e293b; padding: 8px 16px; border-radius: 20px; cursor: pointer; border: 1px solid #ef4444; font-weight: 600; font-size: 12px; color: #cbd5e1; }
            .cat-chip.active { background: linear-gradient(45deg, #ef4444, #dc2626); border-color: #ef4444; color: white; }
            .trending-section { padding: 15px; }
            .section-title { font-size: 18px; font-weight: 800; margin-bottom: 10px; }
            .trending-slider { display: flex; overflow-x: auto; scroll-snap-type: x mandatory; gap: 10px; scrollbar-width: none; }
            .trending-slider::-webkit-scrollbar { display: none; }
            .trending-card { min-width: 85%; scroll-snap-align: start; border-radius: 12px; overflow: hidden; position: relative; cursor: pointer; }
            .trending-card img { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }
            .trending-overlay { position: absolute; bottom: 0; left: 0; width: 100%; background: linear-gradient(transparent, rgba(0,0,0,0.9)); padding: 20px 15px 15px; }
            .movie-list { padding: 0 15px; display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
            .movie-card { background: #1e293b; border-radius: 12px; overflow: hidden; cursor: pointer; }
            .movie-card img { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }
            .movie-info { padding: 8px 10px; }
            .movie-title { font-size: 13px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
            .movie-meta { font-size: 10px; color: #94a3b8; display: flex; gap: 5px; }
            .bottom-nav { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); border-top: 1px solid #1e293b; display: flex; justify-content: space-around; padding: 10px 0; z-index: 1000; }
            .nav-item { display: flex; flex-direction: column; align-items: center; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer; border: none; background: none; }
            .nav-item i { font-size: 20px; margin-bottom: 3px; }
            .nav-item.active { color: #ef4444; }
            .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: flex-end; justify-content: center; z-index: 3000; }
            .modal-content { background: #1e293b; width: 100%; max-width: 400px; padding: 25px; border-radius: 20px 20px 0 0; max-height: 90vh; overflow-y: auto; position: relative; }
            .detail-img { width: 100%; aspect-ratio: 16/9; object-fit: cover; border-radius: 12px; margin-bottom: 15px; }
            .detail-title { font-size: 22px; font-weight: 800; margin-bottom: 5px; }
            .close-icon { position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: rgba(0,0,0,0.6); color: #fff; font-size: 18px; display: flex; align-items: center; justify-content: center; cursor: pointer; border: none; }
            .dl-file-btn { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 15px; background: #0f172a; border: 1px solid #334155; color: white; font-weight: 700; border-radius: 10px; margin-bottom: 10px; cursor: pointer; }
            .search-box { padding: 0 15px 15px; }
            .search-input { width: 100%; padding: 14px; border-radius: 12px; border: none; outline: none; background: #1e293b; color: #fff; font-size: 15px; }
            .pagination-container { display: flex; justify-content: center; gap: 8px; padding: 20px 15px 80px 15px; }
            .page-btn { background: #1e293b; color: #cbd5e1; border: 1px solid #334155; padding: 10px 15px; border-radius: 10px; cursor: pointer; }
            .page-btn.active { background: #ef4444; color: white; border-color: #ef4444; }
            .ad-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.9); z-index: 5000; justify-content: center; align-items: center; flex-direction: column; padding: 20px; }
            .ad-content { background: #1e293b; padding: 20px; border-radius: 12px; text-align: center; max-width: 350px; width: 100%; }
            .ad-icon { font-size: 50px; color: #fbbf24; margin-bottom: 10px; }
            .ad-btn { width: 100%; padding: 15px; border-radius: 10px; border: none; font-weight: 700; font-size: 16px; cursor: pointer; margin-top: 10px; }
            .ad-btn-wait { background: #334155; color: #94a3b8; }
            .ad-btn-download { background: #10b981; color: white; display: none; }
            .ad-link-btn { display: block; width: 100%; padding: 15px; border-radius: 10px; background: #ea580c; color: white; font-weight: 700; text-decoration: none; margin-bottom: 10px; }
        </style>
    </head>
    <body>
        <div id="welcomeScreen"><div class="ws-brand">Movie Box</div></div>
        <header onclick="switchTab('home')"><div class="logo">Movie Box</div></header>

        <div id="tabHome" class="page-section active">
            <div class="search-box"><input type="text" id="searchInput" class="search-input" placeholder="Search..." oninput="searchMovies()"></div>
            <div class="cat-row" id="catRow"></div>
            <div class="trending-section" id="trendingSection" style="display:none;">
                <div class="section-title"><i class="fa-solid fa-fire" style="color:#ef4444"></i> Trending Now</div>
                <div class="trending-slider" id="trendingSlider"></div>
            </div>
            <div class="movie-list" id="movieList"></div>
            <div class="pagination-container" id="pagination"></div>
        </div>

        <div class="bottom-nav">
            <button class="nav-item active" onclick="switchTab('home')"><i class="fa-solid fa-house"></i>Home</button>
            <button class="nav-item" onclick="switchTab('profile')"><i class="fa-solid fa-user"></i>Profile</button>
        </div>

        <div class="modal" id="detailModal">
            <div class="modal-content">
                <button class="close-icon" onclick="closeModal()"><i class="fa-solid fa-xmark"></i></button>
                <img id="detailImg" class="detail-img" src="">
                <h2 id="detailTitle" class="detail-title"></h2>
                <p id="detailMeta" style="color:#94a3b8; margin-bottom:15px;"></p>
                <div id="detailQualities"></div>
            </div>
        </div>

        <div class="ad-overlay" id="adModal">
            <div class="ad-content">
                <div class="ad-icon"><i class="fa-solid fa-ad"></i></div>
                <div style="font-size:20px;font-weight:800;color:#fbbf24;margin-bottom:15px">Ad</div>
                <div id="adTimerText" style="margin-bottom:15px">Please wait 10 seconds...</div>
                <a id="adVisitLink" href="#" class="ad-link-btn" target="_blank">Visit Ad</a>
                <button id="adWaitBtn" class="ad-btn ad-btn-wait" onclick="alert('Please watch the ad for 10 seconds before downloading!')">10 seconds wait</button>
                <button id="adDownloadBtn" class="ad-btn ad-btn-download" onclick="finalDownload()">Download</button>
            </div>
        </div>

        <script>
            const tg = window.Telegram.WebApp; tg.ready(); tg.expand();
            const DIRECT_LINKS = __DL_JSON__;
            const ADULT_LINKS = __ADL_JSON__;
            let currentCat = "All", currentPage = 1, searchQuery = "", currentFileUrl = "", adInterval = null, adSecondsLeft = 10;
            let currentUser = tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user : {id: 0};

            setTimeout(() => { document.getElementById('welcomeScreen').classList.add('hide'); }, 1500);

            function switchTab(tab) {
                document.querySelectorAll('.page-section').forEach(s => s.classList.remove('active'));
                document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
                document.getElementById('tabHome').classList.add('active');
            }

            async function loadCategories() {
                const cats = ["All", "Bangla", "Bangla Dubbed", "Hindi Dubbed", "Hollywood", "K-Drama", "Anime", "Horror", "Web Series", "Adult Content"];
                document.getElementById('catRow').innerHTML = cats.map(c => `<div class="cat-chip ${c === currentCat ? 'active' : ''}" onclick="selectCat('${c}')">${c}</div>`).join('');
            }

            function selectCat(cat) { currentCat = cat; currentPage = 1; loadCategories(); loadMovies(); }

            async function loadTrending() {
                try {
                    const res = await fetch('/api/trending'); const movies = await res.json();
                    if(movies.length > 0) {
                        document.getElementById('trendingSection').style.display = 'block';
                        document.getElementById('trendingSlider').innerHTML = movies.map(m => `<div class="trending-card" onclick="openMovie('${m.title}')"><img src="/img/${m.photo_id}"><div class="trending-overlay"><div style="font-weight:800">${m.title}</div></div></div>`).join('');
                        setInterval(() => { const s = document.getElementById('trendingSlider'); if(s.scrollLeft + s.clientWidth >= s.scrollWidth - 10) s.scrollTo({left: 0, behavior: 'smooth'}); else s.scrollBy({left: s.clientWidth, behavior: 'smooth'}); }, 3000);
                    }
                } catch(e) {}
            }

            async function loadMovies() {
                try {
                    const res = await fetch(`/api/movies?category=${currentCat}&search=${searchQuery}&page=${currentPage}`);
                    const data = await res.json();
                    document.getElementById('movieList').innerHTML = data.movies.map(m => `<div class="movie-card" onclick="openMovie('${m.title}')"><img src="/img/${m.photo_id}"><div class="movie-info"><div class="movie-title">${m.title}</div><div class="movie-meta"><span>${m.year || 'N/A'}</span><span>${m.quality || ''}</span></div></div></div>`).join('');
                    renderPagination(data.total_pages, data.current_page);
                } catch(e) {}
            }

            function searchMovies() { searchQuery = document.getElementById('searchInput').value; currentPage = 1; loadMovies(); }

            function renderPagination(tp, c) {
                const pag = document.getElementById('pagination'); if(tp <= 1) { pag.innerHTML = ''; return; }
                let h = `<button class="page-btn" onclick="goToPage(${c-1})" ${c===1?'disabled':''}><</button>`;
                for(let i=1; i<=tp; i++) h += `<button class="page-btn ${i===c?'active':''}" onclick="goToPage(${i})">${i}</button>`;
                h += `<button class="page-btn" onclick="goToPage(${c+1})" ${c===tp?'disabled':''}>></button>`;
                pag.innerHTML = h;
            }

            function goToPage(p) { currentPage = p; loadMovies(); window.scrollTo(0,0); }

            async function openMovie(title) {
                try {
                    const res = await fetch(`/api/movie/${encodeURIComponent(title)}`);
                    const m = await res.json(); fetch(`/api/movie/click/${encodeURIComponent(title)}`);
                    document.getElementById('detailImg').src = `/img/${m.photo_id}`;
                    document.getElementById('detailTitle').innerText = m.title;
                    document.getElementById('detailMeta').innerText = `${m.year} | Views: ${m.clicks}`;
                    document.getElementById('detailQualities').innerHTML = m.qualities.map(q => `<button class="dl-file-btn" onclick="initDownload('${q.file_id}', ${m.categories.includes('Adult Content')})"><span><i class="fa-solid fa-download"></i> ${q.quality}</span><i class="fa-solid fa-arrow-right"></i></button>`).join('');
                    document.getElementById('detailModal').style.display = 'flex';
                    if(currentUser.id) fetch('/api/unlock', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({user_id: currentUser.id, title: m.title}) });
                } catch(e) { alert('Error!'); }
            }

            function closeModal() { document.getElementById('detailModal').style.display = 'none'; }

            function initDownload(fileId, isAdult) {
                currentFileUrl = `/dl/${fileId}`;
                const links = isAdult ? ADULT_LINKS : DIRECT_LINKS;
                document.getElementById('adVisitLink').href = links.length > 0 ? links[Math.floor(Math.random() * links.length)] : "#";
                document.getElementById('adDownloadBtn').style.display = 'none';
                document.getElementById('adWaitBtn').style.display = 'block';
                document.getElementById('adTimerText').innerText = "Please wait 10 seconds...";
                document.getElementById('adModal').style.display = 'flex';
                adSecondsLeft = 10;
                if(adInterval) clearInterval(adInterval);
                adInterval = setInterval(() => {
                    adSecondsLeft--;
                    if(adSecondsLeft > 0) {
                        document.getElementById('adTimerText').innerText = `Please wait ${adSecondsLeft} seconds...`;
                        document.getElementById('adWaitBtn').innerText = `${adSecondsLeft} seconds wait`;
                    } else {
                        clearInterval(adInterval);
                        document.getElementById('adTimerText').innerText = "You can download now!";
                        document.getElementById('adWaitBtn').style.display = 'none';
                        document.getElementById('adDownloadBtn').style.display = 'block';
                    }
                }, 1000);
            }

            function finalDownload() {
                if(adSecondsLeft > 0) { alert("Please watch the ad for 10 seconds before downloading!"); return; }
                if(currentFileUrl) window.open(currentFileUrl, '_blank');
                document.getElementById('adModal').style.display = 'none';
            }

            loadCategories(); loadMovies(); loadTrending();
        </script>
    </body></html>'''
    
    html_code = html_code.replace("__DL_JSON__", dl_json).replace("__ADL_JSON__", adl_json)
    return HTMLResponse(html_code)

async def main():
    global bot, dp
    try: await bot.delete_webhook(drop_pending_updates=True)
    except: pass
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)), log_level="info")
    server = uvicorn.Server(config)
    asyncio.create_task(dp.start_polling(bot))
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
