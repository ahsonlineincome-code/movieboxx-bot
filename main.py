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
from contextlib import asynccontextmanager

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
BOT_USERNAME = ""

db = None
bot = None
dp = None

ADMINS = set([OWNER_ID])
BANNED_USERS = set()

security = HTTPBasic()

# ==========================================
# 2. Database & Cache Loaders
# ==========================================
async def init_db():
    global db
    client = AsyncIOMotorClient(MONGO_URL)
    db = client.get_database("moviedb")
    print("Database connection established!")

async def load_admins():
    global ADMINS
    ADMINS = set([OWNER_ID])
    try:
        async for admin in db.admins.find():
            ADMINS.add(admin["user_id"])
    except Exception as e:
        print("Error loading admins:", e)

async def load_banned_users():
    global BANNED_USERS
    BANNED_USERS = set()
    try:
        async for user in db.banned.find():
            BANNED_USERS.add(user["user_id"])
    except Exception as e:
        print("Error loading banned users:", e)

# ==========================================
# 3. FastAPI Lifespan (RENDER EVENT LOOP FIX)
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup Logic ---
    await init_db()
    await load_admins()
    await load_banned_users()
    
    # Background Workers
    asyncio.create_task(auto_delete_worker())
    
    # Bot Routers & Polling
    await start_bot_routers()
    
    yield
    # --- Shutdown Logic ---
    if bot:
        await bot.session.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 4. Pydantic Models for FastAPI
# ==========================================
class TelegramUser(BaseModel):
    id: int
    first_name: str
    last_name: str = None
    username: str = None
    language_code: str = None
    allows_write_to_pm: bool = None

class InitDataPayload(BaseModel):
    initData: str

class VideoUpload(BaseModel):
    title: str
    category: str
    points: int
    duration: int
    tg_file_id: str

class ClaimReward(BaseModel):
    uid: int
    vid: str

class WithdrawRequest(BaseModel):
    uid: int
    method: str
    number: str
    amount: float

class TaskClaimRequest(BaseModel):
    uid: int
    task_type: str

# ==========================================
# 5. Telegram WebApp Validation Helper
# ==========================================
def verify_telegram_init_data(init_data: str) -> dict:
    try:
        parsed = urllib.parse.parse_qs(init_data)
        auth_date = parsed.get("auth_date", [None])[0]
        hash_val = parsed.get("hash", [None])[0]
        user_str = parsed.get("user", [None])[0]
        
        if not hash_val or not auth_date or not user_str:
            return None
            
        sorted_params = []
        for k in sorted(parsed.keys()):
            if k != "hash":
                sorted_params.append(f"{k}={parsed[k][0]}")
        data_check_string = "\n".join(sorted_params)
        
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if calculated_hash == hash_val:
            return json.loads(user_str)
    except Exception as e:
        print("Validation Error:", e)
    return None

def authenticate_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username == "admin" and credentials.password == ADMIN_PASS:
        return True
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect admin credentials",
        headers={"WWW-Authenticate": "Basic"},
    )

# ==========================================
# 6. Telegram Bot Handlers & States
# ==========================================
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_ban = State()
    waiting_for_unban = State()
    waiting_for_video = State()
    waiting_for_video_details = State()

def get_admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ ভিডিও আপলোড করুন", callback_data="admin_upload_video")
    builder.button(text="📊 ইউজার পরিসংখ্যান", callback_data="admin_stats")
    builder.button(text="📢 ব্রডকাস্ট মেসেজ", callback_data="admin_broadcast")
    builder.button(text="🚫 ইউজার ব্যান করুন", callback_data="admin_ban")
    builder.button(text="🔓 ইউজার আনব্যান", callback_data="admin_unban")
    builder.button(text="💰 উইথড্র রিকোয়েস্ট", callback_data="admin_withdrawals")
    builder.adjust(1, 2, 2, 1)
    return builder.as_markup()

async def auto_delete_worker():
    while True:
        try:
            now = time.time()
            if db is not None:
                cursor = db.auto_delete_queue.find({"delete_at": {"$lte": now}})
                async for job in cursor:
                    try:
                        await bot.delete_message(chat_id=job["chat_id"], message_id=job["message_id"])
                    except Exception:
                        pass
                    await db.auto_delete_queue.delete_one({"_id": job["_id"]})
        except Exception as e:
            print("Auto Delete Worker Error:", e)
        await asyncio.sleep(5)

# ==========================================
# 7. WebApp UI Frontend
# ==========================================
index_html = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Premium Movie & Earn WebApp</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root {
            --bg-color: #0b0f17;
            --card-bg: #121824;
            --text-color: #a0aec0;
            --text-main: #ffffff;
            --accent-color: #e50914;
            --success-color: #10b981;
            --border-color: #1e293b;
            --nav-bg: #121824;
            --nav-active: #ffffff;
            --nav-inactive: #718096;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: sans-serif; -webkit-tap-highlight-color: transparent; }
        body { background-color: var(--bg-color); color: var(--text-color); padding-bottom: 85px; font-size: 14px; }
        header { background-color: var(--card-bg); padding: 15px; border-bottom: 1px solid var(--border-color); position: sticky; top: 0; z-index: 100; }
        .user-profile { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
        .user-info { display: flex; align-items: center; gap: 10px; }
        .user-avatar { width: 38px; height: 38px; background: linear-gradient(45deg, var(--accent-color), #ff4b5c); border-radius: 50%; display: flex; align-items: center; justify-content: center; color: white; font-weight: bold; }
        .balance-card { background: rgba(255, 215, 0, 0.1); border: 1px solid #ffd700; padding: 8px 14px; border-radius: 20px; }
        .balance-amount { font-size: 16px; font-weight: 700; color: #ffd700; display: flex; align-items: center; gap: 6px; }
        .categories-container { display: flex; gap: 8px; overflow-x: auto; padding: 5px 0; }
        .category-btn { background-color: #1e293b; color: var(--text-color); border: 1px solid var(--border-color); padding: 6px 14px; border-radius: 20px; font-size: 12px; cursor: pointer; white-space: nowrap; }
        .category-btn.active { background-color: var(--accent-color); color: white; border-color: var(--accent-color); }
        .content-section { padding: 15px; display: none; }
        .content-section.active { display: block; }
        .video-grid { display: grid; grid-template-columns: 1fr; gap: 15px; }
        .video-card { background-color: var(--card-bg); border: 1px solid var(--border-color); border-radius: 12px; overflow: hidden; }
        .video-thumbnail { width: 100%; height: 190px; background-color: #000; display: flex; align-items: center; justify-content: center; position: relative; }
        .video-thumbnail i { font-size: 45px; color: var(--accent-color); }
        .video-duration { position: absolute; bottom: 10px; right: 10px; background: rgba(0,0,0,0.8); padding: 3px 6px; border-radius: 4px; font-size: 11px; color: #fff; }
        .video-reward-badge { position: absolute; top: 10px; left: 10px; background: #ffd700; color: #000; padding: 4px 10px; border-radius: 6px; font-size: 11px; font-weight: bold; }
        .video-info { padding: 12px; }
        .video-title { color: var(--text-main); font-size: 14px; font-weight: 600; margin-bottom: 10px; }
        .watch-btn { background-color: var(--accent-color); color: white; border: none; padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; }
        nav { position: fixed; bottom: 0; left: 0; width: 100%; height: 68px; background-color: var(--nav-bg); border-top: 1px solid var(--border-color); display: flex; justify-content: space-around; align-items: center; z-index: 1000; }
        .nav-item { display: flex; flex-direction: column; align-items: center; color: var(--nav-inactive); text-decoration: none; font-size: 11px; cursor: pointer; flex: 1; gap: 5px; }
        .nav-item.active { color: var(--nav-active); }
        .nav-item i { font-size: 20px; }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 6px; color: white; }
        .form-control { width: 100%; background-color: #1a202c; border: 1px solid var(--border-color); padding: 12px; border-radius: 8px; color: white; }
        .submit-btn { width: 100%; background-color: var(--success-color); color: white; border: none; padding: 12px; border-radius: 8px; font-weight: 600; cursor: pointer; }
        .player-container { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background-color: #000; z-index: 2000; display: none; flex-direction: column; }
        video { width: 100%; height: 100%; }
        .countdown-overlay { position: absolute; bottom: 30px; left: 50%; transform: translateX(-50%); background: rgba(0,0,0,0.8); padding: 8px 16px; border-radius: 20px; color:#fff;}
        .task-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 10px; padding: 15px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }
    </style>
</head>
<body>
    <header id="main-header">
        <div class="user-profile">
            <div class="user-info">
                <div class="user-avatar" id="avatar-letter">U</div>
                <div class="user-name" id="display-name">Loading...</div>
            </div>
            <div class="balance-card">
                <div class="balance-amount"><i class="fas fa-coins"></i> <span id="user-coins">0</span></div>
            </div>
        </div>
        <div class="categories-container" id="categories-list">
            <button class="category-btn active" onclick="filterCategory('all')">সবগুলো</button>
            <button class="category-btn" onclick="filterCategory('movie')">মুভি লিংক</button>
            <button class="category-btn" onclick="filterCategory('income')">ইনকাম ভিডিও</button>
            <button class="category-btn" onclick="filterCategory('offer')">অফার ভিডিও</button>
        </div>
    </header>

    <div id="home-section" class="content-section active">
        <div class="video-grid" id="video-list-container"></div>
    </div>

    <div id="task-section" class="content-section">
        <h3 style="color:white; margin-bottom:15px;">📋 ডেইলি টাস্ক মিশন</h3>
        <div class="task-card">
            <div>
                <h4 style="color:white;">৩টি এডস দেখা</h4>
                <p style="font-size:12px;">আজকের প্রোগ্রেস: <span id="ads-count">0</span>/৩</p>
            </div>
            <button class="watch-btn" style="background:#10b981;" id="btn-claim-ads" onclick="claimTaskReward('ads')">Claim 15 C</button>
        </div>
        <div class="task-card">
            <div>
                <h4 style="color:white;">২টি রিভিউ দেওয়া</h4>
                <p style="font-size:12px;">আজকের প্রোগ্রেস: <span id="reviews-count">0</span>/২</p>
            </div>
            <button class="watch-btn" style="background:#10b981;" id="btn-claim-reviews" onclick="claimTaskReward('reviews')">Claim 10 C</button>
        </div>
    </div>

    <div id="profile-section" class="content-section">
        <h3 style="margin-bottom: 12px; color: #fff;">💰 উইথড্র করুন</h3>
        <div class="form-group">
            <label>মেথড</label>
            <select class="form-control" id="withdraw-method">
                <option value="Bkash">বিকাশ</option>
                <option value="Nagad">নগদ</option>
            </select>
        </div>
        <div class="form-group">
            <label>নাম্বার</label>
            <input type="number" class="form-control" id="withdraw-number" placeholder="01XXXXXXXXX">
        </div>
        <div class="form-group">
            <label>কয়েন</label>
            <input type="number" class="form-control" id="withdraw-amount" placeholder="সর্বনিম্ন ১০০ কয়েন">
        </div>
        <button class="submit-btn" onclick="submitWithdrawal()">সাবমিট করুন</button>
    </div>

    <div class="player-container" id="video-player-container">
        <video id="main-video-element" controlslist="nodownload" playsinline></video>
        <div class="countdown-overlay" id="player-countdown">অপেক্ষা করুন: 0s</div>
    </div>

    <nav>
        <div class="nav-item active" onclick="switchTab('home', this)"><i class="fas fa-home"></i>Home</div>
        <div class="nav-item" onclick="switchTab('task', this)"><i class="fas fa-tasks"></i>Tasks</div>
        <div class="nav-item" onclick="switchTab('profile', this)"><i class="fas fa-user"></i>Profile</div>
    </nav>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();
        
        let rawInitData = tg.initData;
        let userData = tg.initDataUnsafe.user || { id: 123456, first_name: "Premium User" };
        let allVideos = [];
        let currentCategory = 'all';

        document.getElementById('display-name').innerText = userData.first_name;
        document.getElementById('avatar-letter').innerText = userData.first_name.charAt(0).toUpperCase();

        async function authUser() {
            let res = await fetch('/api/auth', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ initData: rawInitData || "test_mode_enabled" })
            });
            let data = await res.json();
            if(data.ok) {
                document.getElementById('user-coins').innerText = data.user.coins;
                
                let tasks = data.user.tasks || {};
                document.getElementById('ads-count').innerText = tasks.ads || 0;
                document.getElementById('reviews-count').innerText = tasks.reviews || 0;
                
                loadVideos();
            }
        }

        async function loadVideos() {
            let res = await fetch('/api/videos');
            allVideos = await res.json();
            renderVideos();
        }

        function renderVideos() {
            let container = document.getElementById('video-list-container');
            container.innerHTML = '';
            let filtered = allVideos;
            if(currentCategory !== 'all') {
                filtered = allVideos.filter(v => v.category === currentCategory);
            }
            filtered.forEach(vid => {
                let card = document.createElement('div');
                card.className = 'video-card';
                card.innerHTML = `
                    <div class="video-thumbnail">
                        <i class="fas fa-play-circle"></i>
                        <div class="video-reward-badge">+${vid.points} Coins</div>
                        <div class="video-duration">${vid.duration}s</div>
                    </div>
                    <div class="video-info">
                        <div class="video-title">${vid.title}</div>
                        <button class="watch-btn" onclick="playVideo('${vid._id}', ${vid.duration})">প্লে করুন</button>
                    </div>
                `;
                container.appendChild(card);
            });
        }

        function filterCategory(cat) {
            currentCategory = cat;
            document.querySelectorAll('.category-btn').forEach(b => b.classList.remove('active'));
            event.target.classList.add('active');
            renderVideos();
        }

        let playbackInterval;
        function playVideo(vid, duration) {
            let container = document.getElementById('video-player-container');
            let videoElement = document.getElementById('main-video-element');
            let countdown = document.getElementById('player-countdown');
            
            videoElement.src = `/api/stream/${vid}`;
            container.style.display = 'flex';
            
            let timeLeft = duration;
            countdown.innerText = `অপেক্ষা করুন: ${timeLeft}s`;
            videoElement.play().catch(e => console.log(e));

            playbackInterval = setInterval(async () => {
                if(!videoElement.paused) {
                    timeLeft--;
                    countdown.innerText = `অপেক্ষা করুন: ${timeLeft}s`;
                    if(timeLeft <= 0) {
                        clearInterval(playbackInterval);
                        videoElement.pause();
                        container.style.display = 'none';
                        await claimReward(vid);
                    }
                }
            }, 1000);
        }

        async function claimReward(vid) {
            let res = await fetch('/api/claim', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ uid: userData.id, vid: vid })
            });
            let data = await res.json();
            if(data.ok) {
                Swal.fire('সফল!', `আপনি +${data.points} কয়েন পেয়েছেন!`, 'success');
                authUser();
            } else {
                Swal.fire('ইনফো', data.msg, 'info');
            }
        }

        async function claimTaskReward(type) {
            let res = await fetch('/api/tasks/claim', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({ uid: userData.id, task_type: type })
            });
            let data = await res.json();
            if(data.ok) {
                Swal.fire('মিশন সফল!', 'টাস্ক রিওয়ার্ড যুক্ত হয়েছে!', 'success');
                authUser();
            } else {
                Swal.fire('ব্যর্থ', data.msg || 'শর্ত পূরণ হয়নি!', 'error');
            }
        }

        async function submitWithdrawal() {
            let method = document.getElementById('withdraw-method').value;
            let number = document.getElementById('withdraw-number').value;
            let amount = parseFloat(document.getElementById('withdraw-amount').value);
            
            let res = await fetch('/api/withdraw', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({ uid: userData.id, method: method, number: number, amount: amount })
            });
            let data = await res.json();
            if(data.ok) {
                Swal.fire('সফল', 'রিকোয়েস্ট পাঠানো হয়েছে', 'success');
                authUser();
            } else {
                Swal.fire('ব্যর্থ', data.msg, 'error');
            }
        }

        function switchTab(tabId, el) {
            document.querySelectorAll('.content-section').forEach(s => s.classList.remove('active'));
            document.getElementById(tabId + '-section').classList.add('active');
            document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
            el.classList.add('active');
            document.getElementById('categories-list').style.display = (tabId === 'home') ? 'flex' : 'none';
        }

        authUser();
    </script>
</body>
</html>
"""

# ==========================================
# 8. FastAPI API Endpoints
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return HTMLResponse(content=index_html)

@app.post("/api/auth")
async def api_auth(payload: InitDataPayload):
    user_info = None
    if payload.initData == "test_mode_enabled":
        user_info = {"id": 123456, "first_name": "Premium User"}
    else:
        user_info = verify_telegram_init_data(payload.initData)
        
    if not user_info:
        raise HTTPException(status_code=400, detail="Invalid session token.")
        
    uid = user_info["id"]
    if uid in BANNED_USERS:
        raise HTTPException(status_code=403, detail="Your account has been terminated.")
        
    user_doc = await db.users.find_one({"user_id": uid})
    if not user_doc:
        user_doc = {
            "user_id": uid,
            "first_name": user_info.get("first_name", ""),
            "coins": 0,
            "watched_videos": [],
            "tasks": {"date": datetime.datetime.now().strftime("%Y-%m-%d"), "ads": 0, "reviews": 0},
            "joined_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        await db.users.insert_one(user_doc)
    else:
        user_doc["_id"] = str(user_doc["_id"])
        
    return {"ok": True, "user": user_doc}

@app.get("/api/videos")
async def get_videos():
    vids = []
    async for v in db.videos.find():
        v["_id"] = str(v["_id"])
        vids.append(v)
    return vids

@app.get("/api/stream/{vid}")
async def stream_video(vid: str):
    try:
        video = await db.videos.find_one({"_id": ObjectId(vid)})
        if not video:
            raise HTTPException(status_code=404, detail="Video missing.")
            
        file_id = video["tg_file_id"]
        file_info = await bot.get_file(file_id)
        tg_file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
        
        async def video_stream_generator():
            async with aiohttp.ClientSession() as session:
                async with session.get(tg_file_url) as response:
                    if response.status != 200: return
                    while True:
                        chunk = await response.content.read(1024 * 64)
                        if not chunk: break
                        yield chunk
                        
        return StreamingResponse(video_stream_generator(), media_type="video/mp4")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/claim")
async def claim_reward(data: ClaimReward):
    if data.uid in BANNED_USERS: return {"ok": False, "msg": "Banned"}
    video = await db.videos.find_one({"_id": ObjectId(data.vid)})
    if not video: return {"ok": False, "msg": "Video not found."}
    
    user = await db.users.find_one({"user_id": data.uid})
    if not user: return {"ok": False, "msg": "User not found."}
    if data.vid in user.get("watched_videos", []):
        return {"ok": False, "msg": "ইতিমধ্যে ক্লেইম করেছেন!"}
        
    pts = video.get("points", 5)
    v_cat = video.get("category", "movie")
    
    # টাস্ক প্রোগ্রেস আপডেট লজিক
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    tasks = user.get("tasks", {})
    if tasks.get("date") != today:
        tasks = {"date": today, "ads": 0, "reviews": 0}
        
    if v_cat == "income":
        tasks["ads"] = tasks.get("ads", 0) + 1
    elif v_cat == "offer":
        tasks["reviews"] = tasks.get("reviews", 0) + 1

    await db.users.update_one(
        {"user_id": data.uid},
        {
            "$inc": {"coins": pts},
            "$push": {"watched_videos": data.vid},
            "$set": {"tasks": tasks}
        }
    )
    return {"ok": True, "points": pts}

@app.post("/api/tasks/claim")
async def claim_task_reward(data: TaskClaimRequest):
    user = await db.users.find_one({"user_id": data.uid})
    if not user: return {"ok": False, "msg": "ইউজার পাওয়া যায়নি"}
    
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    tasks = user.get("tasks", {})
    
    if tasks.get("date") != today:
        return {"ok": False, "msg": "মিশন সম্পূর্ণ হয়নি!"}
        
    if data.task_type == "ads" and tasks.get("ads", 0) >= 3 and not tasks.get("ads_claimed"):
        await db.users.update_one({"user_id": data.uid}, {"$set": {"tasks.ads_claimed": True}, "$inc": {"coins": 15}})
        return {"ok": True}
        
    if data.task_type == "reviews" and tasks.get("reviews", 0) >= 2 and not tasks.get("reviews_claimed"):
        await db.users.update_one({"user_id": data.uid}, {"$set": {"tasks.reviews_claimed": True}, "$inc": {"coins": 10}})
        return {"ok": True}
        
    return {"ok": False, "msg": "ইতিমধ্যে ক্লেইম করা হয়েছে বা মিশন সম্পূর্ণ হয়নি!"}

@app.post("/api/withdraw")
async def init_withdrawal(data: WithdrawRequest):
    if data.uid in BANNED_USERS: return {"ok": False, "msg": "Banned"}
    user = await db.users.find_one({"user_id": data.uid})
    if not user or user.get("coins", 0) < data.amount or data.amount < 100:
        return {"ok": False, "msg": "পর্যাপ্ত কয়েন নেই বা সর্বনিম্ন সীমা লঙ্ঘন হয়েছে!"}
        
    withdrawal_doc = {
        "user_id": data.uid, "method": data.method, "number": data.number,
        "amount": data.amount, "status": "Pending", "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    await db.withdrawals.insert_one(withdrawal_doc)
    await db.users.update_one({"user_id": data.uid}, {"$inc": {"coins": -data.amount}})
    return {"ok": True}

# ==========================================
# 9. Telegram Bot Core Router Setup
# ==========================================
async def start_bot_routers():
    global bot, dp, BOT_USERNAME
    bot = Bot(token=TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    
    bot_info = await bot.get_me()
    BOT_USERNAME = bot_info.username

    @dp.message(Command("start"))
    async def cmd_start(message: types.Message):
        uid = message.from_user.id
        if uid in BANNED_USERS:
            await message.answer("🚫 অ্যাকাউন্টটি ব্লক করা রয়েছে।")
            return
            
        user_doc = await db.users.find_one({"user_id": uid})
        if not user_doc:
            user_doc = {
                "user_id": uid, "first_name": message.from_user.first_name, "coins": 0, "watched_videos": [],
                "tasks": {"date": datetime.datetime.now().strftime("%Y-%m-%d"), "ads": 0, "reviews": 0},
                "joined_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            await db.users.insert_one(user_doc)

        builder = InlineKeyboardBuilder()
        builder.button(text="🚀 ওপেন আর্ন অ্যাপ", web_app=types.WebAppInfo(url=APP_URL))
        builder.adjust(1)
        await message.answer(f"👋 স্বাগতম {message.from_user.first_name}! অ্যাপ ওপেন করে টাস্ক কমপ্লিট করুন।", reply_markup=builder.as_markup())

    # এডমিন ব্যাকগ্রাউন্ড পোলিং স্টার্ট
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    # Uvicorn সরাসরি অ্যাপ রান করবে এবং Lifespan-এর মাধ্যমে ইভেন্ট লুপ ম্যানেজ হবে
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
