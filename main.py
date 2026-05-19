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
# 3. FastAPI Lifespan (COMPLETELY FIXES RENDER LOOPS)
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup Logic ---
    await init_db()
    await load_admins()
    await load_banned_users()
    
    # Background Workers
    asyncio.create_task(auto_delete_worker())
    
    # Bot Setup & Polling Start
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
# 7. WebApp UI Frontend (4 Button Complete Layout)
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
        body { background-color: var(--bg-color); color: var(--text-color); padding-bottom: 85px; font-size: 14px; overflow-x: hidden; }
        header { background-color: var(--card-bg); padding: 15px; border-bottom: 1px solid var(--border-color); position: sticky; top: 0; z-index: 100; }
        .user-profile { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
        .user-info { display: flex; align-items: center; gap: 10px; }
        .user-avatar { width: 38px; height: 38px; background: linear-gradient(45deg, var(--accent-color), #ff4b5c); border-radius: 50%; display: flex; align-items: center; justify-content: center; color: white; font-weight: bold; }
        .user-name { color: var(--text-main); font-weight: 600; font-size: 15px; }
        .balance-card { background: rgba(255, 215, 0, 0.1); border: 1px solid #ffd700; padding: 8px 14px; border-radius: 20px; }
        .balance-amount { font-size: 16px; font-weight: 700; color: #ffd700; display: flex; align-items: center; gap: 6px; }
        .categories-container { display: flex; gap: 8px; overflow-x: auto; padding: 5px 0; width: 100%; }
        .categories-container::-webkit-scrollbar { display: none; }
        .category-btn { background-color: #1e293b; color: var(--text-color); border: 1px solid var(--border-color); padding: 6px 14px; border-radius: 20px; font-size: 12px; cursor: pointer; white-space: nowrap; }
        .category-btn.active { background-color: var(--accent-color); color: white; border-color: var(--accent-color); }
        .content-section { padding: 15px; display: none; }
        .content-section.active { display: block; }
        .search-wrapper { position: relative; margin-bottom: 20px; }
        .search-input { width: 100%; background-color: var(--card-bg); border: 1px solid var(--border-color); padding: 12px 15px 12px 40px; border-radius: 10px; color: white; }
        .search-wrapper i { position: absolute; left: 15px; top: 15px; color: #718096; }
        .video-grid { display: grid; grid-template-columns: 1fr; gap: 15px; }
        .video-card { background-color: var(--card-bg); border: 1px solid var(--border-color); border-radius: 12px; overflow: hidden; }
        .video-thumbnail { width: 100%; height: 190px; background-color: #000; display: flex; align-items: center; justify-content: center; position: relative; }
        .video-thumbnail i { font-size: 45px; color: var(--accent-color); }
        .video-duration { position: absolute; bottom: 10px; right: 10px; background: rgba(0,0,0,0.8); padding: 3px 6px; border-radius: 4px; font-size: 11px; color: #fff; }
        .video-reward-badge { position: absolute; top: 10px; left: 10px; background: #ffd700; color: #000; padding: 4px 10px; border-radius: 6px; font-size: 11px; font-weight: bold; }
        .video-info { padding: 12px; }
        .video-title { color: var(--text-main); font-size: 14px; font-weight: 600; margin-bottom: 10px; }
        .video-meta { display: flex; justify-content: space-between; align-items: center; }
        .watch-btn { background-color: var(--accent-color); color: white; border: none; padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; }
        .profile-card { background-color: var(--card-bg); padding: 20px; border-radius: 12px; border: 1px solid var(--border-color); text-align: center; margin-bottom: 20px; }
        .form-group { margin-bottom: 15px; text-align: left;}
        .form-group label { display: block; margin-bottom: 6px; color: white; font-weight:600; }
        .form-control { width: 100%; background-color: #1a202c; border: 1px solid var(--border-color); padding: 12px; border-radius: 8px; color: white; }
        .submit-btn { width: 100%; background-color: var(--success-color); color: white; border: none; padding: 12px; border-radius: 8px; font-weight: 600; cursor: pointer; }
        nav { position: fixed; bottom: 0; left: 0; width: 100%; height: 68px; background-color: var(--nav-bg); border-top: 1px solid var(--border-color); display: flex; justify-content: space-around; align-items: center; z-index: 1000; padding-bottom: env(safe-area-inset-bottom); }
        .nav-item { display: flex; flex-direction: column; align-items: center; color: var(--nav-inactive); text-decoration: none; font-size: 11px; cursor: pointer; flex: 1; gap: 5px; }
        .nav-item.active { color: var(--nav-active); }
        .nav-item i { font-size: 20px; }
        .player-container { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background-color: #000; z-index: 2000; display: none; flex-direction: column; }
        .player-header { padding: 15px; color: white; background: rgba(0,0,0,0.6); position: absolute; top:0; width:100%; z-index:10;}
        video { width: 100%; height: 100%; object-fit: contain; }
        .countdown-overlay { position: absolute; bottom: 30px; left: 50%; transform: translateX(-50%); background: rgba(0,0,0,0.8); padding: 8px 16px; border-radius: 20px; color:#fff;}
        .task-card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 10px; padding: 15px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }
        .history-item { background-color: var(--card-bg); padding: 12px; border-radius: 8px; margin-bottom: 8px; display: flex; justify-content: space-between; font-size: 13px; border: 1px solid var(--border-color); }
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

    <div id="search-section" class="content-section">
        <div class="search-wrapper">
            <i class="fas fa-search"></i>
            <input type="text" class="search-input" id="search-bar" placeholder="পছন্দের মুভি বা ভিডিও খুঁজুন..." oninput="searchVideos()">
        </div>
        <div class="video-grid" id="search-results-container">
            <p style="text-align:center; padding:20px; color:var(--text-color);">খোঁজার জন্য উপরে টাইপ করুন।</p>
        </div>
    </div>

    <div id="task-section" class="content-section">
        <h3 style="color:white; margin-bottom:15px;">📋 ডেইলি টাস্ক মিশন</h3>
        <div class="task-card">
            <div>
                <h4 style="color:white;">৩টি এডস দেখা</h4>
                <p style="font-size:12px;">আজকের প্রোগ্রেস: <span id="ads-count">0</span>/৩</p>
            </div>
            <button class="watch-btn" style="background:#10b981;" onclick="claimTaskReward('ads')">Claim 15 C</button>
        </div>
        <div class="task-card">
            <div>
                <h4 style="color:white;">২টি রিভিউ দেওয়া</h4>
                <p style="font-size:12px;">আজকের প্রোগ্রেস: <span id="reviews-count">0</span>/২</p>
            </div>
            <button class="watch-btn" style="background:#10b981;" onclick="claimTaskReward('reviews')">Claim 10 C</button>
        </div>
    </div>

    <div id="profile-section" class="content-section">
        <div class="profile-card">
            <div class="user-avatar" style="width:65px; height:65px; font-size:26px; margin: 0 auto 12px auto;" id="profile-big-avatar">U</div>
            <h3 id="profile-name" style="color:white; margin-bottom:3px;">User</h3>
            <p id="profile-id" style="font-size:12px; color:#718096; margin-bottom:15px;">ID: 0</p>
            <div style="display:flex; justify-content:space-around; background:#0b0f17; padding:12px; border-radius:8px;">
                <div>
                    <div style="font-size:16px; font-weight:bold; color:#ffd700;" id="profile-coins">0</div>
                    <div style="font-size:11px;">মোট ব্যালেন্স</div>
                </div>
                <div>
                    <div style="font-size:16px; font-weight:bold; color:var(--accent-color);" id="profile-watched">0</div>
                    <div style="font-size:11px;">দেখা ভিডিও</div>
                </div>
            </div>
        </div>

        <h3 style="margin-bottom: 12px; color: #fff;">💰 টাকা উত্তোলন (Withdraw)</h3>
        <div style="background-color: var(--card-bg); padding: 15px; border-radius:12px; border:1px solid var(--border-color); margin-bottom: 20px;">
            <div class="form-group">
                <label>পেমেন্ট মেথড</label>
                <select class="form-control" id="withdraw-method">
                    <option value="Bkash">বিকাশ (Bkash)</option>
                    <option value="Nagad">নগদ (Nagad)</option>
                </select>
            </div>
            <div class="form-group">
                <label>মোবাইল নাম্বার</label>
                <input type="number" class="form-control" id="withdraw-number" placeholder="01XXXXXXXXX">
            </div>
            <div class="form-group">
                <label>কয়েন এমাউন্ট</label>
                <input type="number" class="form-control" id="withdraw-amount" placeholder="সর্বনিম্ন ১০০ কয়েন">
            </div>
            <button class="submit-btn" onclick="submitWithdrawal()">উইথড্র রিকোয়েস্ট সাবমিট করুন</button>
        </div>
        <h3 style="margin-bottom: 12px; color: #fff;">📜 উইথড্র হিস্ট্রি</h3>
        <div id="withdraw-history"></div>
    </div>

    <div class="player-container" id="video-player-container">
        <div class="player-header"><span id="player-video-title" style="font-weight:600;">ভিডিও প্লে হচ্ছে...</span></div>
        <video id="main-video-element" controlslist="nodownload" playsinline></video>
        <div class="countdown-overlay" id="player-countdown">অপেক্ষা করুন: 0s</div>
    </div>

    <nav>
        <div class="nav-item active" onclick="switchTab('home', this)"><i class="fas fa-home"></i>Home</div>
        <div class="nav-item" onclick="switchTab('search', this)"><i class="fas fa-search"></i>Search</div>
        <div class="nav-item" onclick="switchTab('task', this)"><i class="fas fa-tasks"></i>Tasks</div>
        <div class="nav-item" onclick="switchTab('profile', this)"><i class="fas fa-user"></i>Profile</div>
    </nav>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();
        tg.ready();

        let rawInitData = tg.initData;
        let userData = tg.initDataUnsafe.user || { id: 123456, first_name: "Premium", last_name: "User" };
        let allVideos = [];
        let currentCategory = 'all';

        document.getElementById('display-name').innerText = userData.first_name;
        document.getElementById('profile-name').innerText = userData.first_name + (userData.last_name ? ' ' + userData.last_name : '');
        document.getElementById('profile-id').innerText = "ID: " + userData.id;
        document.getElementById('avatar-letter').innerText = userData.first_name.charAt(0).toUpperCase();
        document.getElementById('profile-big-avatar').innerText = userData.first_name.charAt(0).toUpperCase();

        async function authUser() {
            try {
                let res = await fetch('/api/auth', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ initData: rawInitData || "test_mode_enabled" })
                });
                let data = await res.json();
                if(data.ok) {
                    document.getElementById('user-coins').innerText = data.user.coins;
                    document.getElementById('profile-coins').innerText = data.user.coins;
                    document.getElementById('profile-watched').innerText = data.user.watched_videos ? data.user.watched_videos.length : 0;
                    
                    let tasks = data.user.tasks || {};
                    document.getElementById('ads-count').innerText = tasks.ads || 0;
                    document.getElementById('reviews-count').innerText = tasks.reviews || 0;
                    
                    loadVideos();
                    loadWithdrawHistory();
                }
            } catch(e) { console.error(e); }
        }

        async function loadVideos() {
            try {
                let res = await fetch('/api/videos');
                allVideos = await res.json();
                renderVideos('video-list-container');
            } catch(e) { console.error(e); }
        }

        function renderVideos(targetId = 'video-list-container') {
            let container = document.getElementById(targetId);
            container.innerHTML = '';
            
            let filtered = allVideos;
            if(targetId === 'video-list-container' && currentCategory !== 'all') {
                filtered = allVideos.filter(v => v.category === currentCategory);
            }
            
            if(filtered.length === 0) {
                container.innerHTML = `<div style="text-align:center; padding:30px; color:#718096; width:100%;">কোনো ভিডিও পাওয়া যায়নি!</div>`;
                return;
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
                        <div class="video-meta">
                            <span style="color:#718096;"><i class="fas fa-tag"></i> ${vid.category}</span>
                            <button class="watch-btn" onclick="playVideo('${vid._id}', '${btoa(vid.title)}', ${vid.duration})">প্লে করুন</button>
                        </div>
                    </div>
                `;
                container.appendChild(card);
            });
        }

        function filterCategory(cat) {
            currentCategory = cat;
            document.querySelectorAll('.category-btn').forEach(b => b.classList.remove('active'));
            event.target.classList.add('active');
            renderVideos('video-list-container');
        }

        function searchVideos() {
            let query = document.getElementById('search-bar').value.toLowerCase();
            if(!query) {
                document.getElementById('search-results-container').innerHTML = `<p style="text-align:center; padding:20px; color:var(--text-color);">খোঁজার জন্য উপরে টাইপ করুন।</p>`;
                return;
            }
            let results = allVideos.filter(v => v.title.toLowerCase().includes(query));
            let container = document.getElementById('search-results-container');
            container.innerHTML = '';
            results.forEach(vid => {
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
                        <div class="video-meta">
                            <span style="color:#718096;"><i class="fas fa-tag"></i> ${vid.category}</span>
                            <button class="watch-btn" onclick="playVideo('${vid._id}', '${btoa(vid.title)}', ${vid.duration})">প্লে করুন</button>
                        </div>
                    </div>
                `;
                container.appendChild(card);
            });
        }

        let playbackInterval;
        function playVideo(vid, encodedTitle, duration) {
            let title = atob(encodedTitle);
            let container = document.getElementById('video-player-container');
            let videoElement = document.getElementById('main-video-element');
            let countdown = document.getElementById('player-countdown');
            
            document.getElementById('player-video-title').innerText = title;
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
                Swal.fire('ইনফো', data.msg || 'ক্লেইম করা সম্ভব হয়নি', 'info');
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
            
            if(!number || !amount || amount < 100) {
                Swal.fire('সতর্কতা', 'সঠি তথ্য দিন, সর্বনিম্ন ১০০ কয়েন', 'warning');
                return;
            }

            let res = await fetch('/api/withdraw', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({ uid: userData.id, method: method, number: number, amount: amount })
            });
            let data = await res.json();
            if(data.ok) {
                Swal.fire('সফল', 'রিকোয়েস্ট পাঠানো হয়েছে', 'success');
                document.getElementById('withdraw-number').value = '';
                document.getElementById('withdraw-amount').value = '';
                authUser();
            } else {
                Swal.fire('ব্যর্থ', data.msg, 'error');
            }
        }

        async function loadWithdrawHistory() {
            try {
                let res = await fetch(`/api/withdraw/history/${userData.id}`);
                let list = await res.json();
                let container = document.getElementById('withdraw-history');
                container.innerHTML = '';
                if(list.length === 0) {
                    container.innerHTML = '<p style="font-size:12px; color:#718096; padding:10px 0;">কোনো হিস্ট্রি পাওয়া যায়নি।</p>';
                    return;
                }
                list.forEach(h => {
                    let div = document.createElement('div');
                    div.className = 'history-item';
                    div.innerHTML = `
                        <div><strong>${h.method}</strong> (${h.number})<br><small style="color:#718096;">${h.date}</small></div>
                        <div style="text-align:right;"><span style="font-weight:bold; color:#ffd700;">${h.amount} C</span><br><span style="color:${h.status==='Approved'?'#10b981':'#f59e0b'}">${h.status}</span></div>
                    `;
                    container.appendChild(div);
                });
            } catch(e) { console.error(e); }
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
    
    try:
        msg = f"💰 **নতুন উইথড্র রিকোয়েস্ট!**\n\n👤 ইউজার আইডি: `{data.uid}`\n💳 মেথড: {data.method}\n📞 নাম্বার: `{data.number}`\n🪙 পরিমাণ: {data.amount} Coins"
        await bot.send_message(chat_id=OWNER_ID, text=msg, parse_mode="Markdown")
    except Exception:
        pass
    return {"ok": True}

@app.get("/api/withdraw/history/{uid}")
async def withdraw_history(uid: int):
    items = []
    async for w in db.withdrawals.find({"user_id": uid}).sort("_id", -1):
        w["_id"] = str(w["_id"])
        items.append(w)
    return items

# ==========================================
# 9. Admin Dashboard Panels
# ==========================================
@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard_ui(authenticated: bool = Depends(authenticate_admin)):
    html = """
    <!DOCTYPE html>
    <html><head><title>Admin Dashboard</title><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css"></head>
    <body class="bg-light p-4"><div class="container bg-white p-4 rounded shadow-sm"><h2>Admin Panel Controller</h2><div class="row"><div class="col-md-6"><h5>Fast Video Uploader</h5><form action="/admin/upload-video-form" method="POST"><input class="form-control mb-2" name="title" placeholder="ভিডিও নাম" required><select class="form-control mb-2" name="category"><option value="movie">মুভি লিংক</option><option value="income">ইনকাম ভিডিও</option><option value="offer">অফার ভিডিও</option></select><input type="number" class="form-control mb-2" name="points" placeholder="কয়েন পরিমাণ" required><input type="number" class="form-control mb-2" name="duration" placeholder="ডিউরেশন" required><input class="form-control mb-2" name="tg_file_id" placeholder="টেলিগ্রাম ফাইল আইডি" required><button class="btn btn-primary w-100">আপলোড করুন</button></form></div><div class="col-md-6"><h5>পেন্ডিং উইথড্র</h5><div id="withdraw-list">Loading...</div></div></div></div>
    <script>async function loadW(){let r=await fetch('/admin/api/withdrawals');let d=await r.json();let c=document.getElementById('withdraw-list');c.innerHTML='';if(!d.length){c.innerHTML='No requests';return;}d.forEach(w=>{c.innerHTML+=`<div class="border p-2 mb-2">User: ${w.user_id} | ${w.amount} C<br>No: ${w.number} (${w.method})<br><button class="btn btn-success btn-sm mt-1" onclick="act('${w._id}','approve')">Approve</button> <button class="btn btn-danger btn-sm mt-1" onclick="act('${w._id}','reject')">Reject</button></div>`;});}async function act(id,t){await fetch(`/admin/api/withdraw/${id}/${t}`,{method:'POST'});loadW();}loadW();</script></body></html>
    """
    return HTMLResponse(content=html)

@app.post("/admin/upload-video-form")
async def form_upload_video(title: str = Body(...), category: str = Body(...), points: int = Body(...), duration: int = Body(...), tg_file_id: str = Body(...), authenticated: bool = Depends(authenticate_admin)):
    await db.videos.insert_one({"title": title, "category": category, "points": points, "duration": duration, "tg_file_id": tg_file_id})
    return HTMLResponse("<script>alert('সফলভাবে আপলোড হয়েছে!'); window.location='/admin/dashboard';</script>")

@app.get("/admin/api/withdrawals")
async def admin_get_withdrawals(authenticated: bool = Depends(authenticate_admin)):
    reqs = []
    async for w in db.withdrawals.find({"status": "Pending"}):
        w["_id"] = str(w["_id"])
        reqs.append(w)
    return reqs

@app.post("/admin/api/withdraw/{wid}/{action_type}")
async def admin_withdraw_action(wid: str, action_type: str, authenticated: bool = Depends(authenticate_admin)):
    status_str = "Approved" if action_type == "approve" else "Rejected"
    w_doc = await db.withdrawals.find_one({"_id": ObjectId(wid)})
    if not w_doc: return {"ok": False}
    await db.withdrawals.update_one({"_id": ObjectId(wid)}, {"$set": {"status": status_str}})
    if status_str == "Rejected":
        await db.users.update_one({"user_id": w_doc["user_id"]}, {"$inc": {"coins": w_doc["amount"]}})
    try:
        await bot.send_message(chat_id=w_doc["user_id"], text=f"🔔 **উইথড্র আপডেট!**\n\nCoins: {w_doc['amount']}\nStatus: {status_str}", parse_mode="Markdown")
    except: pass
    return {"ok": True}

# ==========================================
# 10. Telegram Bot Core Router Setup
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
        builder.button(text="📢 আমাদের চ্যানেল", url=f"https://t.me/{CHANNEL_ID.replace('-100','')}")
        builder.adjust(1)
        
        welcome_text = f"👋 **স্বাগতম {message.from_user.first_name}!**\n\nভিডিও দেখে এবং ডেলি মিশন কমপ্লিট করে ফ্রিতে বিকাশ বা নগদে পেমেন্ট নিন।"
        await message.answer(welcome_text, reply_markup=builder.as_markup(), parse_mode="Markdown")

    @dp.message(Command("panel"))
    async def cmd_panel(message: types.Message):
        if message.from_user.id not in ADMINS: return
        await message.answer("⚙️ **এডমিন কন্ট্রোল ড্যাশবোর্ড:**", reply_markup=get_admin_keyboard(), parse_mode="Markdown")

    @dp.callback_query(F.data == "admin_stats")
    async def cb_stats(callback: types.CallbackQuery):
        if callback.from_user.id not in ADMINS: return
        total_users = await db.users.count_documents({})
        total_vids = await db.videos.count_documents({})
        pending_w = await db.withdrawals.count_documents({"status": "Pending"})
        await callback.message.edit_text(f"📊 **পরিসংখ্যান:**\n\nইউজার: {total_users}\nভিডিও: {total_vids}\nপেন্ডিং উইথড্র: {pending_w}", reply_markup=get_admin_keyboard(), parse_mode="Markdown")

    @dp.callback_query(F.data == "admin_upload_video")
    async def cb_upload_init(callback: types.CallbackQuery, state: FSMContext):
        if callback.from_user.id not in ADMINS: return
        await state.set_state(AdminStates.waiting_for_video)
        await callback.message.answer("🎬 অনুগ্রহ করে আপনার কাঙ্ক্ষিত ভিডিওটি (MP4) সেন্ড করুন:")
        await callback.answer()

    @dp.message(AdminStates.waiting_for_video, F.video)
    async def process_admin_video(message: types.Message, state: FSMContext):
        await state.update_data(file_id=message.video.file_id)
        await state.set_state(AdminStates.waiting_for_video_details)
        await message.answer("📌 ফরম্যাট অনুযায়ী পাঠান:\n`টাইটেল | ক্যাটাগরি | কয়েন | ডিউরেশন`", parse_mode="Markdown")

    @dp.message(AdminStates.waiting_for_video_details)
    async def process_video_details(message: types.Message, state: FSMContext):
        try:
            parts = [p.strip() for p in message.text.split("|")]
            title, category, coins, duration = parts[0], parts[1], int(parts[2]), int(parts[3])
            state_data = await state.get_data()
            await db.videos.insert_one({"title": title, "category": category, "points": coins, "duration": duration, "tg_file_id": state_data["file_id"]})
            await state.clear()
            await message.answer("✅ ভিডিও আপলোড সম্পন্ন!", reply_markup=get_admin_keyboard())
        except Exception as e:
            await message.answer(f"❌ এরর: {str(e)}")

    @dp.callback_query(F.data == "admin_broadcast")
    async def cb_broadcast(callback: types.CallbackQuery, state: FSMContext):
        if callback.from_user.id not in ADMINS: return
        await state.set_state(AdminStates.waiting_for_broadcast)
        await callback.message.answer("📢 ব্রডকাস্ট মেসেজটি লিখুন:")
        await callback.answer()

    @dp.message(AdminStates.waiting_for_broadcast)
    async def process_broadcast(message: types.Message, state: FSMContext):
        txt = message.text
        await state.clear()
        await message.answer("⏳ ব্রডকাস্টিং শুরু হয়েছে...")
        async for u in db.users.find():
            try: await bot.send_message(chat_id=u["user_id"], text=txt)
            except: pass
        await message.answer("📢 ব্রডকাস্ট সম্পন্ন!", reply_markup=get_admin_keyboard())

    @dp.callback_query(F.data == "admin_ban")
    async def cb_ban(callback: types.CallbackQuery, state: FSMContext):
        if callback.from_user.id not in ADMINS: return
        await state.set_state(AdminStates.waiting_for_ban)
        await callback.message.answer("🚫 ব্যান করার ইউজার আইডি দিন:")
        await callback.answer()

    @dp.message(AdminStates.waiting_for_ban)
    async def process_ban(message: types.Message, state: FSMContext):
        try:
            uid = int(message.text.strip())
            await db.banned.update_one({"user_id": uid}, {"$set": {"date": datetime.datetime.now().strftime("%Y-%m-%d")}}, upsert=True)
            BANNED_USERS.add(uid)
            await state.clear()
            await message.answer(f"✅ ইউজার {uid} ব্যান করা হয়েছে।", reply_markup=get_admin_keyboard())
        except: await message.answer("❌ সঠিক আইডি দিন।")

    @dp.callback_query(F.data == "admin_unban")
    async def cb_unban(callback: types.CallbackQuery, state: FSMContext):
        if callback.from_user.id not in ADMINS: return
        await state.set_state(AdminStates.waiting_for_unban)
        await callback.message.answer("🔓 আনব্যান করার আইডি দিন:")
        await callback.answer()

    @dp.message(AdminStates.waiting_for_unban)
    async def process_unban(message: types.Message, state: FSMContext):
        try:
            uid = int(message.text.strip())
            await db.banned.delete_one({"user_id": uid})
            if uid in BANNED_USERS: BANNED_USERS.remove(uid)
            await state.clear()
            await message.answer(f"✅ ইউজার {uid} আনব্যান হয়েছে।", reply_markup=get_admin_keyboard())
        except: await message.answer("❌ সঠিক আইডি দিন।")

    @dp.callback_query(F.data == "admin_withdrawals")
    async def cb_w_list(callback: types.CallbackQuery):
        if callback.from_user.id not in ADMINS: return
        builder = InlineKeyboardBuilder()
        async for w in db.withdrawals.find({"status": "Pending"}).limit(10):
            builder.button(text=f"ID:{w['user_id']} - {w['amount']}C", callback_data=f"v_w_{w['_id']}")
        builder.adjust(2)
        await callback.message.answer("🔽 পেন্ডিং তালিকা:", reply_markup=builder.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("v_w_"))
    async def cb_view_single_w(callback: types.CallbackQuery):
        wid = callback.data.replace("v_w_", "")
        w = await db.withdrawals.find_one({"_id": ObjectId(wid)})
        if not w: return
        msg = f"💰 **উইথড্রাল ডিটেইলস:**\n\nইউজার: `{w['user_id']}`\nমেথড: {w['method']}\n📞 নাম্বার: `{w['number']}`\n🪙 পরিমাণ: {w['amount']} Coins"
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Approve", callback_data=f"act_a_{wid}")
        builder.button(text="❌ Reject", callback_data=f"act_r_{wid}")
        builder.adjust(2)
        await callback.message.answer(msg, reply_markup=builder.as_markup(), parse_mode="Markdown")

    @dp.callback_query(F.data.startswith("act_"))
    async def cb_action_execute(callback: types.CallbackQuery):
        data = callback.data.replace("act_", "")
        action_type = "approve" if data.startswith("a_") else "reject"
        wid = data.replace("a_", "").replace("r_", "")
        status_str = "Approved" if action_type == "approve" else "Rejected"
        w_doc = await db.withdrawals.find_one({"_id": ObjectId(wid)})
        if not w_doc: return
        await db.withdrawals.update_one({"_id": ObjectId(wid)}, {"$set": {"status": status_str}})
        if status_str == "Rejected":
            await db.users.update_one({"user_id": w_doc["user_id"]}, {"$inc": {"coins": w_doc["amount"]}})
        await callback.message.edit_text(f"📢 রিকোয়েস্টটি **{status_str}** করা হয়েছে।")
        try: await bot.send_message(chat_id=w_doc["user_id"], text=f"🔔 উইথড্র রিকোয়েস্ট {status_str} হয়েছে!")
        except: pass

    @dp.channel_post()
    async def auto_delete_handler(message: types.Message):
        if str(message.chat.id) == str(CHANNEL_ID):
            await db.auto_delete_queue.insert_one({"chat_id": message.chat.id, "message_id": message.message_id, "delete_at": time.time() + 60})

    # Polling Start
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
