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
BOT_USERNAME = ""

db = None
bot = None
dp = None
app = FastAPI()

ADMINS = set([OWNER_ID])
BANNED_USERS = set()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBasic()

# ==========================================
# 2. Database Connection
# ==========================================
async def init_db():
    global db
    client = AsyncIOMotorClient(MONGO_URL)
    db = client.get_database("moviedb")
    print("Database connection established!")

async def load_admins():
    global ADMINS
    ADMINS = set([OWNER_ID])
    async for admin in db.admins.find():
        ADMINS.add(admin["user_id"])

async def load_banned_users():
    global BANNED_USERS
    BANNED_USERS = set()
    async for user in db.banned.find():
        BANNED_USERS.add(user["user_id"])

# ==========================================
# 3. Pydantic Models for FastAPI
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

class DailyTaskClaim(BaseModel):
    uid: int
    task_type: str

# ==========================================
# 4. Telegram WebApp Validation Helper
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
# 5. Telegram Bot Handlers & States
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
# 6. WebApp UI with New 5 Button Navigation
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
            --accent-color: #e50914; /* Netflix Red Premium Style */
            --success-color: #10b981;
            --border-color: #1e293b;
            --nav-bg: #121824;
            --nav-active: #ffffff;
            --nav-inactive: #718096;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            -webkit-tap-highlight-color: transparent;
        }

        body {
            background-color: var(--bg-color);
            color: var(--text-color);
            padding-bottom: 85px;
            font-size: 14px;
            overflow-x: hidden;
        }

        header {
            background-color: var(--card-bg);
            padding: 15px;
            border-bottom: 1px solid var(--border-color);
            position: sticky;
            top: 0;
            z-index: 100;
        }

        .user-profile {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
        }

        .user-info {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .user-avatar {
            width: 38px;
            height: 38px;
            background: linear-gradient(45deg, var(--accent-color), #ff4b5c);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: bold;
            font-size: 16px;
        }

        .user-name {
            color: var(--text-main);
            font-weight: 600;
            font-size: 15px;
        }

        .balance-card {
            background: rgba(255, 215, 0, 0.1);
            border: 1px solid #ffd700;
            padding: 8px 14px;
            border-radius: 20px;
        }

        .balance-amount {
            font-size: 16px;
            font-weight: 700;
            color: #ffd700;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .categories-container {
            display: flex;
            gap: 8px;
            overflow-x: auto;
            padding: 5px 0;
            width: 100%;
        }
        .categories-container::-webkit-scrollbar { display: none; }

        .category-btn {
            background-color: #1e293b;
            color: var(--text-color);
            border: 1px solid var(--border-color);
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
            white-space: nowrap;
        }

        .category-btn.active {
            background-color: var(--accent-color);
            color: white;
            border-color: var(--accent-color);
        }

        /* Content Sections */
        .content-section {
            padding: 15px;
            display: none;
        }

        .content-section.active {
            display: block;
        }

        /* Search Section UI */
        .search-wrapper {
            position: relative;
            margin-bottom: 20px;
        }
        .search-input {
            width: 100%;
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            padding: 12px 15px 12px 40px;
            border-radius: 10px;
            color: white;
            font-size: 14px;
        }
        .search-wrapper i {
            position: absolute;
            left: 15px;
            top: 15px;
            color: #718096;
        }

        /* Video / Movie Grid UI */
        .video-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 15px;
        }

        .video-card {
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            overflow: hidden;
        }

        .video-thumbnail {
            width: 100%;
            height: 190px;
            background-color: #000;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
        }

        .video-thumbnail i {
            font-size: 45px;
            color: var(--accent-color);
            opacity: 0.9;
        }

        .video-duration {
            position: absolute;
            bottom: 10px;
            right: 10px;
            background-color: rgba(0,0,0,0.8);
            padding: 3px 6px;
            border-radius: 4px;
            font-size: 11px;
            color: #fff;
        }

        .video-reward-badge {
            position: absolute;
            top: 10px;
            left: 10px;
            background-color: #ffd700;
            color: #000;
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 11px;
            font-weight: bold;
        }

        .video-info {
            padding: 12px;
        }

        .video-title {
            color: var(--text-main);
            font-size: 14px;
            font-weight: 600;
            margin-bottom: 10px;
        }

        .video-meta {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .watch-btn {
            background-color: var(--accent-color);
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            font-weight: 600;
            cursor: pointer;
        }

        /* Missions UI */
        .task-card {
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            padding: 15px;
            border-radius: 12px;
            margin-bottom: 12px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .task-details h4 { color: var(--text-main); margin-bottom: 4px; }
        .task-details p { font-size: 12px; color: #ffd700; }

        .task-btn {
            background-color: var(--success-color);
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            font-weight: 600;
        }
        .task-btn:disabled { background-color: #2d3748; color: #718096; }

        /* Profile & Withdrawal Form */
        .profile-card {
            background-color: var(--card-bg);
            padding: 20px;
            border-radius: 12px;
            border: 1px solid var(--border-color);
            text-align: center;
            margin-bottom: 20px;
        }
        .form-group { margin-bottom: 15px; text-align: left;}
        .form-group label { display: block; margin-bottom: 6px; font-weight: 600; color: white;}
        .form-control {
            width: 100%; background-color: #1a202c; border: 1px solid var(--border-color);
            padding: 12px; border-radius: 8px; color: white;
        }
        .submit-btn {
            width: 100%; background-color: var(--success-color); color: white;
            border: none; padding: 12px; border-radius: 8px; font-weight: 600; cursor: pointer;
        }

        /* 🆕 PREMIUM BOTTOM NAVIGATION BAR (MATCHED WITH 2ND SCREENSHOT) */
        nav {
            position: fixed;
            bottom: 0;
            left: 0;
            width: 100%;
            height: 68px;
            background-color: var(--nav-bg);
            border-top: 1px solid var(--border-color);
            display: flex;
            justify-content: space-around;
            align-items: center;
            z-index: 1000;
            padding-bottom: env(safe-area-inset-bottom);
        }

        .nav-item {
            display: flex;
            flex-direction: column;
            align-items: center;
            color: var(--nav-inactive);
            text-decoration: none;
            font-size: 11px;
            font-weight: 500;
            gap: 5px;
            cursor: pointer;
            transition: color 0.2s ease;
            flex: 1;
        }

        .nav-item i {
            font-size: 20px;
        }

        .nav-item.active {
            color: var(--nav-active);
        }

        /* Video Player Layer */
        .player-container {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background-color: #000; z-index: 2000; display: none; flex-direction: column;
        }
        .player-header { padding: 15px; color: white; background: rgba(0,0,0,0.6); position: absolute; top:0; width:100%; z-index:10;}
        video { width: 100%; height: 100%; object-fit: contain; }
        .countdown-overlay { position: absolute; bottom: 30px; left: 50%; transform: translateX(-50%); background: rgba(0,0,0,0.8); padding: 8px 16px; border-radius: 20px; color:#fff;}

        .history-item {
            background-color: var(--card-bg); padding: 12px; border-radius: 8px;
            margin-bottom: 8px; display: flex; justify-content: space-between; font-size: 13px;
            border: 1px solid var(--border-color);
        }
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

    <div id="upcoming-section" class="content-section">
        <h3 style="color: #fff; margin-bottom: 15px;">🍿 আসন্ন মুভি ও ট্রেলার</h3>
        <div style="background-color: var(--card-bg); border: 1px solid var(--border-color); border-radius: 12px; padding: 40px 15px; text-align: center;">
            <i class="fas fa-film" style="font-size: 40px; color: var(--accent-color); margin-bottom: 15px;"></i>
            <h4 style="color: white; margin-bottom: 5px;">নতুন ধামাকা আসছে খুব শীঘ্রই!</h4>
            <p style="font-size: 13px;">সবচেয়ে লেটেস্ট রিলিজ এবং ট্রেলার দেখতে চোখ রাখুন আমাদের এই ট্যাবে।</p>
        </div>
    </div>

    <div id="missions-section" class="content-section">
        <h3 style="margin-bottom: 15px; color: #fff;">🎯 দৈনিক মিশন সমুহ</h3>
        <div id="tasks-container"></div>
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
        <div class="player-header">
            <span id="player-video-title" style="font-weight:600;">মুভি/ভিডিও প্লে হচ্ছে...</span>
        </div>
        <video id="main-video-element" controlslist="nodownload" playsinline></video>
        <div class="countdown-overlay" id="player-countdown">অপেক্ষা করুন: 0s</div>
    </div>

    <nav>
        <div class="nav-item active" onclick="switchTab('home', this)"><i class="fas fa-home"></i>Home</div>
        <div class="nav-item" onclick="switchTab('search', this)"><i class="fas fa-search"></i>Search</div>
        <div class="nav-item" onclick="switchTab('upcoming', this)"><i class="fas fa-film"></i>Upcoming</div>
        <div class="nav-item" onclick="switchTab('missions', this)"><i class="fas fa-tasks"></i>Missions</div>
        <div class="nav-item" onclick="switchTab('profile', this)"><i class="fas fa-user"></i>Profile</div>
    </nav>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();
        tg.ready();

        let rawInitData = tg.initData;
        let userData = tg.initDataUnsafe.user || { id: 123456, first_name: "Premium", last_name: "User", username: "premium_user" };
        
        let currentCategory = 'all';
        let allVideos = [];
        let userCoinsCount = 0;

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
                    userCoinsCount = data.user.coins;
                    document.getElementById('user-coins').innerText = userCoinsCount;
                    document.getElementById('profile-coins').innerText = userCoinsCount;
                    document.getElementById('profile-watched').innerText = data.user.watched_videos ? data.user.watched_videos.length : 0;
                    loadVideos();
                    loadTasks();
                    loadWithdrawHistory();
                }
            } catch(e) { console.error(e); }
        }

        async function loadVideos() {
            try {
                let res = await fetch('/api/videos');
                allVideos = await res.json();
                renderVideos(allVideos, 'video-list-container');
            } catch(e) { console.error(e); }
        }

        function renderVideos(videoArray, targetContainerId) {
            let container = document.getElementById(targetContainerId);
            container.innerHTML = '';
            
            let filtered = videoArray;
            if (targetContainerId === 'video-list-container' && currentCategory !== 'all') {
                filtered = videoArray.filter(v => v.category === currentCategory);
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
                        <div class="video-reward-badge"><i class="fas fa-coins"></i> +${vid.points}</div>
                        <div class="video-duration">${vid.duration} সেকেন্ড</div>
                    </div>
                    <div class="video-info">
                        <div class="video-title">${vid.title}</div>
                        <div class="video-meta">
                            <span style="color:#718096;"><i class="fas fa-tag"></i> ${vid.category}</span>
                            <button class="watch-btn" onclick="startVideoPlayback('${vid._id}', '${btoa(vid.title)}', ${vid.duration})">প্লে করুন</button>
                        </div>
                    </div>
                `;
                container.appendChild(card);
            });
        }

        function filterCategory(cat) {
            currentCategory = cat;
            let btns = document.querySelectorAll('.category-btn');
            btns.forEach(b => b.classList.remove('active'));
            event.target.classList.add('active');
            renderVideos(allVideos, 'video-list-container');
        }

        function searchVideos() {
            let query = document.getElementById('search-bar').value.toLowerCase();
            if(!query) {
                document.getElementById('search-results-container').innerHTML = `<p style="text-align:center; padding:20px; color:var(--text-color);">খোঁজার জন্য উপরে টাইপ করুন।</p>`;
                return;
            }
            let results = allVideos.filter(v => v.title.toLowerCase().includes(query));
            renderVideos(results, 'search-results-container');
        }

        let countdownInterval;
        function startVideoPlayback(vid, encodedTitle, duration) {
            let title = atob(encodedTitle);
            let container = document.getElementById('video-player-container');
            let videoElement = document.getElementById('main-video-element');
            let countdownOverlay = document.getElementById('player-countdown');
            
            document.getElementById('player-video-title').innerText = title;
            videoElement.src = `/api/stream/${vid}`;
            container.style.display = 'flex';
            
            let timeLeft = duration;
            countdownOverlay.innerText = `অপেক্ষা করুন: ${timeLeft}s`;
            videoElement.play().catch(e => console.log(e));

            countdownInterval = setInterval(async () => {
                if (!videoElement.paused) {
                    timeLeft--;
                    countdownOverlay.innerText = `অপেক্ষা করুন: ${timeLeft}s`;
                    if(timeLeft <= 0) {
                        clearInterval(countdownInterval);
                        videoElement.pause();
                        container.style.display = 'none';
                        await claimVideoReward(vid);
                    }
                }
            }, 1000);
        }

        async function claimVideoReward(vid) {
            try {
                let res = await fetch('/api/claim', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ uid: userData.id, vid: vid })
                });
                let data = await res.json();
                if(data.ok) {
                    Swal.fire('অভিনন্দন!', `আপনি সফলভাবে ${data.points} কয়েন পেয়েছেন!`, 'success');
                    authUser();
                } else {
                    Swal.fire('ইনফো', data.msg || 'ক্লেইম করা সম্ভব হয়নি', 'info');
                }
            } catch(e) { console.error(e); }
        }

        async function loadTasks() {
            try {
                let res = await fetch(`/api/tasks/${userData.id}`);
                let data = await res.json();
                let container = document.getElementById('tasks-container');
                container.innerHTML = `
                    <div class="task-card">
                        <div class="task-details">
                            <h4>৩টি ইনকাম বিজ্ঞাপন দেখুন</h4>
                            <p>প্রোগ্রেস: ${data.ads}/3 (পুরস্কার: ১৫ কয়েন)</p>
                        </div>
                        <button class="task-btn" ${ (data.ads >= 3 && !data.ads_claimed) ? '' : 'disabled' } onclick="claimDailyMission('ads')">
                            ${data.ads_claimed ? 'ক্লেইমড' : 'ক্লেইম'}
                        </button>
                    </div>
                    <div class="task-card">
                        <div class="task-details">
                            <h4>২টি অ্যাপ রিভিউ দিন</h4>
                            <p>প্রোগ্রেস: ${data.reviews}/2 (পুরস্কার: ১০ কয়েন)</p>
                        </div>
                        <button class="task-btn" ${ (data.reviews >= 2 && !data.reviews_claimed) ? '' : 'disabled' } onclick="claimDailyMission('reviews')">
                            ${data.reviews_claimed ? 'ক্লেইমড' : 'ক্লেইম'}
                        </button>
                    </div>
                `;
            } catch(e) { console.error(e); }
        }

        async function claimDailyMission(type) {
            try {
                let res = await fetch('/api/tasks/claim', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ uid: userData.id, task_type: type })
                });
                let data = await res.json();
                if(data.ok) {
                    Swal.fire('সফল!', 'মিশন বোনাস অ্যাড করা হয়েছে!', 'success');
                    authUser();
                } else {
                    Swal.fire('ব্যর্থ', data.msg, 'error');
                }
            } catch(e) { console.error(e); }
        }

        async function submitWithdrawal() {
            let method = document.getElementById('withdraw-method').value;
            let number = document.getElementById('withdraw-number').value;
            let amount = parseFloat(document.getElementById('withdraw-amount').value);

            if(!number || !amount || amount < 100) {
                Swal.fire('সতর্কতা', 'সঠিক তথ্য দিন, সর্বনিম্ন উত্তোলন ১০০ কয়েন!', 'warning');
                return;
            }

            try {
                let res = await fetch('/api/withdraw', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ uid: userData.id, method: method, number: number, amount: amount })
                });
                let data = await res.json();
                if(data.ok) {
                    Swal.fire('সফল', 'অনুরোধটি সফলভাবে এডমিনের কাছে পাঠানো হয়েছে!', 'success');
                    document.getElementById('withdraw-number').value = '';
                    document.getElementById('withdraw-amount').value = '';
                    authUser();
                } else {
                    Swal.fire('ব্যর্থ', data.msg, 'error');
                }
            } catch(e) { console.error(e); }
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
                        <div>
                            <strong>${h.method}</strong> (${h.number})<br>
                            <small style="color:#718096;">${h.date}</small>
                        </div>
                        <div style="text-align:right;">
                            <span style="font-weight:bold; color:#ffd700;">${h.amount} C</span><br>
                            <span style="color:${h.status==='Approved'?'#10b981':'#f59e0b'}">${h.status}</span>
                        </div>
                    `;
                    container.appendChild(div);
                });
            } catch(e) { console.error(e); }
        }

        // 🆕 5 BUTTON NAVIGATION TAB SWITCHING HANDLER
        function switchTab(tabId, el) {
            let sections = document.querySelectorAll('.content-section');
            sections.forEach(s => s.classList.remove('active'));
            document.getElementById(tabId + '-section').classList.add('active');
            
            let navs = document.querySelectorAll('.nav-item');
            navs.forEach(n => n.classList.remove('active'));
            el.classList.add('active');

            // Hide main header bar category selection for non-home pages to match UI layout cleanly
            let header = document.getElementById('categories-list');
            if (tabId === 'home') {
                header.style.display = 'flex';
            } else {
                header.style.display = 'none';
            }
        }

        authUser();
    </script>
</body>
</html>
"""

# ==========================================
# 7. FastAPI Business Logic Routes
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return HTMLResponse(content=index_html)

@app.post("/api/auth")
async def api_auth(payload: InitDataPayload):
    user_info = None
    if payload.initData == "test_mode_enabled":
        user_info = {"id": 123456, "first_name": "Premium", "last_name": "User", "username": "premium_user"}
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
            "last_name": user_info.get("last_name", ""),
            "username": user_info.get("username", ""),
            "coins": 0,
            "watched_videos": [],
            "tasks": {"ads": 0, "reviews": 0, "ads_claimed": False, "reviews_claimed": False},
            "joined_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        await db.users.insert_one(user_doc)
    else:
        if "tasks" not in user_doc:
            await db.users.update_one({"user_id": uid}, {"$set": {"tasks": {"ads": 0, "reviews": 0, "ads_claimed": False, "reviews_claimed": False}}})
            user_doc["tasks"] = {"ads": 0, "reviews": 0, "ads_claimed": False, "reviews_claimed": False}
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
            raise HTTPException(status_code=404, detail="Video metadata missing.")
            
        file_id = video["tg_file_id"]
        file_info = await bot.get_file(file_id)
        tg_file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
        
        async def video_stream_generator():
            timeout = aiohttp.ClientTimeout(total=3600)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(tg_file_url) as response:
                    if response.status != 200:
                        return
                    while True:
                        chunk = await response.content.read(1024 * 64)
                        if not chunk:
                            break
                        yield chunk
                        
        return StreamingResponse(video_stream_generator(), media_type="video/mp4")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/claim")
async def claim_reward(data: ClaimReward):
    if data.uid in BANNED_USERS:
        return {"ok": False, "msg": "Banned"}
        
    video = await db.videos.find_one({"_id": ObjectId(data.vid)})
    if not video:
        return {"ok": False, "msg": "Video parameters not found."}
        
    user = await db.users.find_one({"user_id": data.uid})
    if not user:
        return {"ok": False, "msg": "User context null."}
        
    if data.vid in user.get("watched_videos", []):
        return {"ok": False, "msg": "আপনি ইতিমধ্যে এই ভিডিওর পুরস্কার ক্লেইম করেছেন!"}
        
    pts = video.get("points", 5)
    now_date = datetime.datetime.now().strftime("%Y-%m-%d")
    
    await db.daily_missions.update_one(
        {"user_id": data.uid, "date": now_date},
        {"$set": {"completed": True}},
        upsert=True
    )
    
    if video.get("category") == "income" or video.get("category") == "offer":
        await db.users.update_one(
            {"user_id": data.uid},
            {
                "$inc": {"coins": pts, "tasks.ads": 1},
                "$push": {"watched_videos": data.vid}
            }
        )
    else:
        await db.users.update_one(
            {"user_id": data.uid},
            {
                "$inc": {"coins": pts},
                "$push": {"watched_videos": data.vid}
            }
        )
        
    return {"ok": True, "points": pts}

@app.post("/api/withdraw")
async def init_withdrawal(data: WithdrawRequest):
    if data.uid in BANNED_USERS:
        return {"ok": False, "msg": "Banned"}
        
    user = await db.users.find_one({"user_id": data.uid})
    if not user or user.get("coins", 0) < data.amount or data.amount < 100:
        return {"ok": False, "msg": "পর্যাপ্ত কয়েন নেই বা সর্বনিম্ন সীমা লঙ্ঘন হয়েছে!"}
        
    withdrawal_doc = {
        "user_id": data.uid,
        "method": data.method,
        "number": data.number,
        "amount": data.amount,
        "status": "Pending",
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

@app.get("/api/tasks/{uid}")
async def get_user_tasks(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user:
        return {"ads": 0, "reviews": 0, "ads_claimed": False, "reviews_claimed": False}
    tasks = user.get("tasks", {})
    return {
        "ads": tasks.get("ads", 0),
        "reviews": tasks.get("reviews", 0),
        "ads_claimed": tasks.get("ads_claimed", False),
        "reviews_claimed": tasks.get("reviews_claimed", False)
    }

@app.post("/api/tasks/claim")
async def claim_task_reward(data: DailyTaskClaim):
    if data.uid in BANNED_USERS:
        return {"ok": False, "msg": "Banned"}
        
    user = await db.users.find_one({"user_id": data.uid})
    if not user:
        return {"ok": False, "msg": "ইউজার পাওয়া যায়নি!"}
        
    tasks = user.get("tasks", {})
    now_date = datetime.datetime.now().strftime("%Y-%m-%d")
    
    mission = await db.daily_missions.find_one({"user_id": data.uid, "date": now_date})
    if not mission:
        return {"ok": False, "msg": "মিশন সম্পূর্ণ হয়নি!"}
        
    if data.task_type == "ads" and tasks.get("ads", 0) >= 3 and not tasks.get("ads_claimed"):
        await db.users.update_one({"user_id": data.uid}, {"$set": {"tasks.ads_claimed": True}, "$inc": {"coins": 15}})
        return {"ok": True}
        
    if data.task_type == "reviews" and tasks.get("reviews", 0) >= 2 and not tasks.get("reviews_claimed"):
        await db.users.update_one({"user_id": data.uid}, {"$set": {"tasks.reviews_claimed": True}, "$inc": {"coins": 10}})
        return {"ok": True}
        
    return {"ok": False, "msg": "ইতিমধ্যে ক্লেইম করা হয়েছে বা মিশন সম্পূর্ণ হয়নি!"}

# ==========================================
# 8. Control Dashboard Sub-Pages (Admin View)
# ==========================================
@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard_ui(authenticated: bool = Depends(authenticate_admin)):
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Admin Dashboard Control</title>
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    </head>
    <body class="bg-light p-4">
        <div class="container bg-white p-4 rounded shadow-sm">
            <h2 class="mb-4">Earning WebApp Admin Controller Panel</h2>
            <div class="row">
                <div class="col-md-6 mb-3">
                    <div class="card p-3">
                        <h5>Fast Video Engine Uploader</h5>
                        <form action="/admin/upload-video-form" method="POST" class="mt-2">
                            <div class="mb-2"><input class="form-control" name="title" placeholder="ভিডিও টাইটেল বা মুভি নাম" required></div>
                            <div class="mb-2">
                                <select class="form-control" name="category">
                                    <option value="movie">মুভি লিংক</option>
                                    <option value="income">ইনকাম ভিডিও</option>
                                    <option value="offer">অফার ভিডিও</option>
                                </select>
                            </div>
                            <div class="mb-2"><input type="number" class="form-control" name="points" placeholder="কয়েন রিওয়ার্ড পরিমাণ" required></div>
                            <div class="mb-2"><input type="number" class="form-control" name="duration" placeholder="ভিডিও ডিউরেশন (সেকেন্ড)" required></div>
                            <div class="mb-2"><input class="form-control" name="tg_file_id" placeholder="টেলিগ্রাম ফাইল আইডি (File ID)" required></div>
                            <button class="btn btn-primary w-100">আপলোড নিশ্চিত করুন</button>
                        </form>
                    </div>
                </div>
                <div class="col-md-6 mb-3">
                    <div class="card p-3">
                        <h5>পেন্ডিং উইথড্র রিকোয়েস্ট সমুহ</h5>
                        <div id="withdraw-list" class="mt-2">Loading requests...</div>
                    </div>
                </div>
            </div>
        </div>
        <script>
            async function fetchWithdrawals() {
                let res = await fetch('/admin/api/withdrawals');
                let data = await res.json();
                let container = document.getElementById('withdraw-list');
                container.innerHTML = '';
                if(data.length === 0) { container.innerHTML = 'কোনো পেন্ডিং রিকোয়েস্ট নেই'; return; }
                data.forEach(w => {
                    container.innerHTML += `
                        <div class="border p-2 rounded mb-2">
                            User: ${w.user_id} | Amount: ${w.amount} Coins<br>
                            Method: ${w.method} | No: ${w.number}<br>
                            <button class="btn btn-success btn-sm mt-1" onclick="action('${w._id}', 'approve')">Approve</button>
                            <button class="btn btn-danger btn-sm mt-1" onclick="action('${w._id}', 'reject')">Reject</button>
                        </div>
                    `;
                });
            }
            async function action(id, type) {
                await fetch(`/admin/api/withdraw/${id}/${type}`, {method: 'POST'});
                fetchWithdrawals();
            }
            fetchWithdrawals();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

@app.post("/admin/upload-video-form")
async def form_upload_video(title: str = Body(...), category: str = Body(...), points: int = Body(...), duration: int = Body(...), tg_file_id: str = Body(...), authenticated: bool = Depends(authenticate_admin)):
    doc = {"title": title, "category": category, "points": points, "duration": duration, "tg_file_id": tg_file_id}
    await db.videos.insert_one(doc)
    return HTMLResponse("<script>alert('সফলভাবে ভিডিও আপলোড হয়েছে!'); window.location='/admin/dashboard';</script>")

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
    if not w_doc:
        return {"ok": False}
        
    await db.withdrawals.update_one({"_id": ObjectId(wid)}, {"$set": {"status": status_str}})
    if status_str == "Rejected":
        await db.users.update_one({"user_id": w_doc["user_id"]}, {"$inc": {"coins": w_doc["amount"]}})
        
    try:
        msg = f"🔔 **আপনার উইথড্র রিকোয়েস্ট আপডেট!**\n\n💰 পরিমাণ: {w_doc['amount']} Coins\n📌 স্ট্যাটাস: {status_str}"
        await bot.send_message(chat_id=w_doc["user_id"], text=msg, parse_mode="Markdown")
    except Exception:
        pass
        
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
            await message.answer("🚫 দুঃখিত, আপনার অ্যাকাউন্টটি আমাদের সিস্টেমে ব্লক করা রয়েছে।")
            return
            
        user_doc = await db.users.find_one({"user_id": uid})
        if not user_doc:
            user_doc = {
                "user_id": uid,
                "first_name": message.from_user.first_name,
                "last_name": message.from_user.last_name,
                "username": message.from_user.username,
                "coins": 0,
                "watched_videos": [],
                "tasks": {"ads": 0, "reviews": 0, "ads_claimed": False, "reviews_claimed": False},
                "joined_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            await db.users.insert_one(user_doc)

        builder = InlineKeyboardBuilder()
        builder.button(text="🚀 ওপেন আর্ন অ্যাপ", web_app=types.WebAppInfo(url=APP_URL))
        builder.button(text="📢 আমাদের চ্যানেল", url=f"https://t.me/{CHANNEL_ID.replace('-100','')}")
        builder.adjust(1)
        
        welcome_text = (
            f"👋 **আসসালামু আলাইকুম, {message.from_user.first_name}!**\n\n"
            f"আমাদের ভিডিও আর্নিং অ্যাপের অফিসিয়াল বোটে আপনাকে স্বাগতম। এখানে আপনি প্রতিদিন বিভিন্ন শর্ট ভিডিও, "
            f"মুভি ক্লিপস এবং টাস্ক কমপ্রীট করে ফ্রিতে আনলিমিটেড কয়েন ইনকাম করতে পারবেন।\n\n"
            f"💰 ১০০% ট্রাস্টেড পেমেন্ট বিকাশ এবং নগদের মাধ্যমে সরাসরি দেওয়া হয়।"
        )
        await message.answer(welcome_text, reply_markup=builder.as_markup(), parse_mode="Markdown")

    @dp.message(Command("panel"))
    async def cmd_panel(message: types.Message):
        if message.from_user.id not in ADMINS:
            return
        await message.answer("⚙️ **ওয়েলকাম এডমিন কন্ট্রোল ড্যাশবোর্ড:**", reply_markup=get_admin_keyboard(), parse_mode="Markdown")

    @dp.callback_query(F.data == "admin_stats")
    async def cb_stats(callback: types.CallbackQuery):
        if callback.from_user.id not in ADMINS: return
        total_users = await db.users.count_documents({})
        total_vids = await db.videos.count_documents({})
        pending_w = await db.withdrawals.count_documents({"status": "Pending"})
        
        stat_msg = (
            f"📊 **টোটাল সিস্টেম স্ট্যাটিস্টিক্স:**\n\n"
            f"👥 মোট রেজিস্টার্ড ইউজার: {total_users} জন\n"
            f"🎥 মোট আপলোডকৃত ভিডিও: {total_vids} টি\n"
            f"⏳ পেন্ডিং উইথড্র রিকোয়েস্ট: {pending_w} টি"
        )
        await callback.message.edit_text(stat_msg, reply_markup=get_admin_keyboard(), parse_mode="Markdown")

    @dp.callback_query(F.data == "admin_upload_video")
    async def cb_upload_init(callback: types.CallbackQuery, state: FSMContext):
        if callback.from_user.id not in ADMINS: return
        await state.set_state(AdminStates.waiting_for_video)
        await callback.message.answer("🎬 অনুগ্রহ করে আপনার কাঙ্ক্ষিত ভিডিওটি (MP4 Format) এখানে সেন্ড করুন:")
        await callback.answer()

    @dp.message(AdminStates.waiting_for_video, F.video)
    async def process_admin_video(message: types.Message, state: FSMContext):
        file_id = message.video.file_id
        await state.update_data(file_id=file_id)
        await state.set_state(AdminStates.waiting_for_video_details)
        
        info_txt = (
            "📌 **ভিডিও রিসিভড!**\n\n"
            "এখন নিচের ফরম্যাটে ভিডিওর ডিটেইলস লিখে পাঠান:\n"
            "`টাইটেল | ক্যাটাগরি | কয়েন | ডিউরেশন` \n\n"
            "💡 উদাহরণ: `নতুন মুভি ২০২৬ | movie | 10 | 30`\n"
            "*(ক্যাটাগরি অবশ্যই movie, income অথবা offer হতে হবে)*"
        )
        await message.answer(info_txt, parse_mode="Markdown")

    @dp.message(AdminStates.waiting_for_video_details)
    async def process_video_details(message: types.Message, state: FSMContext):
        try:
            parts = [p.strip() for p in message.text.split("|")]
            if len(parts) < 4:
                await message.answer("❌ ফরম্যাট ভুল হয়েছে! আবার চেষ্টা করুন।")
                return
                
            title, category, coins, duration = parts[0], parts[1], int(parts[2]), int(parts[3])
            state_data = await state.get_data()
            
            video_doc = {
                "title": title,
                "category": category,
                "points": coins,
                "duration": duration,
                "tg_file_id": state_data["file_id"]
            }
            await db.videos.insert_one(video_doc)
            await state.clear()
            await message.answer("✅ ভিডিওটি সফলভাবে সিস্টেমে আপলোড করা হয়েছে!", reply_markup=get_admin_keyboard())
        except Exception as e:
            await message.answer(f"❌ এরর ঘটেছে: {str(e)}")

    @dp.callback_query(F.data == "admin_broadcast")
    async def cb_broadcast(callback: types.CallbackQuery, state: FSMContext):
        if callback.from_user.id not in ADMINS: return
        await state.set_state(AdminStates.waiting_for_broadcast)
        await callback.message.answer("📢 সকল ইউজারের কাছে পাঠানোর জন্য ব্রডকাস্ট মেসেজটি লিখুন:")
        await callback.answer()

    @dp.message(AdminStates.waiting_for_broadcast)
    async def process_broadcast(message: types.Message, state: FSMContext):
        text_to_send = message.text
        await state.clear()
        await message.answer("⏳ ব্রডকাস্টিং শুরু হয়েছে...")
        
        success, fail = 0, 0
        async for user in db.users.find():
            try:
                await bot.send_message(chat_id=user["user_id"], text=text_to_send)
                success += 1
                await asyncio.sleep(0.05)
            except Exception:
                fail += 1
                
        await message.answer(f"📢 **ব্রডকাস্ট সম্পন্ন!**\n\n✅ সফল: {success} জন\n❌ ব্যর্থ: {fail} জন", reply_markup=get_admin_keyboard(), parse_mode="Markdown")

    @dp.callback_query(F.data == "admin_ban")
    async def cb_ban(callback: types.CallbackQuery, state: FSMContext):
        if callback.from_user.id not in ADMINS: return
        await state.set_state(AdminStates.waiting_for_ban)
        await callback.message.answer("🚫 যে ইউজারকে ব্যান করতে চান তার টেলিগ্রাম ইউজার আইডি (User ID) দিন:")
        await callback.answer()

    @dp.message(AdminStates.waiting_for_ban)
    async def process_ban(message: types.Message, state: FSMContext):
        try:
            target_uid = int(message.text.strip())
            await db.banned.update_one({"user_id": target_uid}, {"$set": {"date": datetime.datetime.now().strftime("%Y-%m-%d")}}, upsert=True)
            BANNED_USERS.add(target_uid)
            await state.clear()
            await message.answer(f"✅ ইউজার `{target_uid}` কে সফলভাবে ব্যান করা হয়েছে।", reply_markup=get_admin_keyboard(), parse_mode="Markdown")
        except ValueError:
            await message.answer("❌ সঠিক সংখ্যা বা ইউজার আইডি দিন।")

    @dp.callback_query(F.data == "admin_unban")
    async def cb_unban(callback: types.CallbackQuery, state: FSMContext):
        if callback.from_user.id not in ADMINS: return
        await state.set_state(AdminStates.waiting_for_unban)
        await callback.message.answer("🔓 যে ইউজারকে আনব্যান করতে চান তার আইডি দিন:")
        await callback.answer()

    @dp.message(AdminStates.waiting_for_unban)
    async def process_unban(message: types.Message, state: FSMContext):
        try:
            target_uid = int(message.text.strip())
            await db.banned.delete_one({"user_id": target_uid})
            if target_uid in BANNED_USERS:
                BANNED_USERS.remove(target_uid)
            await state.clear()
            await message.answer(f"✅ ইউজার `{target_uid}` সফলভাবে আনব্যান হয়েছে।", reply_markup=get_admin_keyboard(), parse_mode="Markdown")
        except ValueError:
            await message.answer("❌ সঠিক সংখ্যা বা ইউজার আইডি দিন।")

    @dp.callback_query(F.data == "admin_withdrawals")
    async def cb_admin_w_list(callback: types.CallbackQuery):
        if callback.from_user.id not in ADMINS: return
        builder = InlineKeyboardBuilder()
        
        count = 0
        async for w in db.withdrawals.find({"status": "Pending"}).limit(10):
            count += 1
            builder.button(text=f"ID:{w['user_id']} - {w['amount']}C", callback_data=f"v_w_{w['_id']}")
            
        builder.adjust(2)
        if count == 0:
            await callback.message.answer("কোনো পেন্ডিং উইথড্র রিকোয়েস্ট নেই।")
        else:
            await callback.message.answer("🔽 পেন্ডিং রিকোয়েস্টের তালিকা (যেকোনো একটিতে ক্লিক করুন):", reply_markup=builder.as_markup())
        await callback.answer()

    @dp.callback_query(F.data.startswith("v_w_"))
    async def cb_view_single_w(callback: types.CallbackQuery):
        if callback.from_user.id not in ADMINS: return
        wid = callback.data.replace("v_w_", "")
        w = await db.withdrawals.find_one({"_id": ObjectId(wid)})
        if not w:
            await callback.answer("রিকোয়েস্টটি পাওয়া যায়নি।")
            return
            
        msg = f"💰 **উইথড্রাল ডিটেইলস:**\n\n👤 ইউজার: `{w['user_id']}`\n💳 মেথড: {w['method']}\n📞 নাম্বার: `{w['number']}`\n🪙 পরিমাণ: {w['amount']} Coins"
        
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Approve", callback_data=f"act_a_{wid}")
        builder.button(text="❌ Reject", callback_data=f"act_r_{wid}")
        builder.adjust(2)
        
        await callback.message.answer(msg, reply_markup=builder.as_markup(), parse_mode="Markdown")
        await callback.answer()

    @dp.callback_query(F.data.startswith("act_"))
    async def cb_action_execute(callback: types.CallbackQuery):
        if callback.from_user.id not in ADMINS: return
        action_data = callback.data.replace("act_", "")
        action_type = "approve" if action_data.startswith("a_") else "reject"
        wid = action_data.replace("a_", "").replace("r_", "")
        
        status_str = "Approved" if action_type == "approve" else "Rejected"
        w_doc = await db.withdrawals.find_one({"_id": ObjectId(wid)})
        if not w_doc: return
        
        await db.withdrawals.update_one({"_id": ObjectId(wid)}, {"$set": {"status": status_str}})
        if status_str == "Rejected":
            await db.users.update_one({"user_id": w_doc["user_id"]}, {"$inc": {"coins": w_doc["amount"]}})
            
        await callback.message.edit_text(f"📢 রিকোয়েস্টটি সফলভাবে **{status_str}** করা হয়েছে।", parse_mode="Markdown")
        
        try:
            msg = f"🔔 **আপনার উইথড্র রিকোয়েস্ট আপডেট!**\n\n💰 পরিমাণ: {w_doc['amount']} Coins\n📌 স্ট্যাটাস: {status_str}"
            await bot.send_message(chat_id=w_doc["user_id"], text=msg, parse_mode="Markdown")
        except Exception:
            pass

    asyncio.create_task(dp.start_polling(bot))

@dp.channel_post()
async def auto_delete_channel_post_handler(message: types.Message):
    if str(message.chat.id) == str(CHANNEL_ID):
        delete_time = time.time() + 60
        job = {
            "chat_id": message.chat.id,
            "message_id": message.message_id,
            "delete_at": delete_time
        }
        await db.auto_delete_queue.insert_one(job)

# ==========================================
# 10. Main Application Startup
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
    await start_bot_routers()
    
    print("Starting Starlette Web Server Engine...")
    await server.serve()

if __name__ == "__main__":
    asyncio.run(start())
