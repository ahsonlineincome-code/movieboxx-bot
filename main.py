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
    asyncio.set_event_loop(asyncio.new_event_loop())\n# ==========================================

from fastapi import FastAPI, Body, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage\n
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

BOT_USERNAME = "loading..."

client = AsyncIOMotorClient(MONGO_URL) if MONGO_URL else None
db = client['tg_miniapp_db'] if client else None

# Preloaded configuration for efficiency
cached_config = {}
ADMIN_IDS = set([OWNER_ID])
BANNED_USERS = set()

# Initialize FastAPI & Aiogram
app = FastAPI(title="Telegram Mini App Backend", docs_url=None, redoc_url=None)
bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher(storage=MemoryStorage())

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBasic()

# Tasks Locking for atomicity
user_locks = {}

def get_user_lock(uid: int):
    if uid not in user_locks:
        user_locks[uid] = asyncio.Lock()
    return user_locks[uid]

# ==========================================
# 2. Database & Cache Loaders
# ==========================================
async def init_db():
    global cached_config
    if db is None:
        return
    conf = await db.config.find_one({"type": "global"})
    if not conf:
        conf = {
            "type": "global",
            "per_view_coin": 1,
            "per_review_coin": 5,
            "min_withdraw": 100,
            "withdraw_charge": 5,
            "refer_bonus": 10,
            "ad_interval": 30,
            "notice": "Welcome to our Mini App! Stay tuned for daily updates.",
            "payment_methods": ["bKash", "Nagad", "Rocket"],
            "force_channel": ""
        }
        await db.config.insert_one(conf)
    cached_config = conf

async def load_admins():
    global ADMIN_IDS
    ADMIN_IDS = set([OWNER_ID])
    if db is None: return
    async for admin in db.admins.find({}):
        ADMIN_IDS.add(admin["user_id"])

async def load_banned_users():
    global BANNED_USERS
    BANNED_USERS = set()
    if db is None: return
    async for u in db.users.find({"banned": True}):
        BANNED_USERS.add(u["user_id"])

# ==========================================
# 3. Pydantic Models for API Validation
# ==========================================
class TeleInitData(BaseModel):
    initData: str

class ClaimRewardData(BaseModel):
    uid: int
    task_type: str
    initData: str

class ManualTaskClaim(BaseModel):
    uid: int
    task_id: str
    initData: str

class WithdrawRequestData(BaseModel):
    uid: int
    method: str
    number: str
    amount: float
    initData: str

# ==========================================
# 4. Request Verification (Telegram HMAC)
# ==========================================
def verify_telegram_data(init_data: str) -> dict:
    if not TOKEN:
        # Development fallback
        return {"user": {"id": OWNER_ID, "first_name": "Developer"}}
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data))
        if "hash" not in parsed:
            return {}
        received_hash = parsed.pop("hash")
        
        # Sort lines
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        
        # HMAC Calculation
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if calculated_hash == received_hash:
            return json.loads(parsed.get("user", "{}"))
        return {}
    except Exception:
        return {}

# ==========================================
# 5. Background Workers
# ==========================================
async def auto_delete_worker():
    """Background task to delete specific tasks or reset daily status if needed"""
    while True:
        try:
            await asyncio.sleep(60)
            if db is None: continue
            now = time.time()
            # Delete expired active tasks if any auto-expiry exists
            await db.tasks.delete_many({"expiry_time": {"$lt": now}, "auto_expiry": True})
        except Exception as e:
            print(f"Worker Error: {e}")

# ==========================================
# 6. Admin Panel Authentication
# ==========================================
def authenticate_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != "admin" or credentials.password != ADMIN_PASS:
        raise HTTPException(
            status_code=status.HTTP_411_LENGTH_REQUIRED,
            detail="Incorrect admin username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True

# ==========================================
# 7. Frontend User UI (HTML + CSS + JS)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def serve_home():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Premium Rewards</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body {
            font-family: 'Poppins', sans-serif;
            background-color: #0b0f19;
            color: #ffffff;
            user-select: none;
            -webkit-user-select: none;
            overflow-x: hidden;
        }
        .nav-active {
            color: #38bdf8;
            transform: translateY(-4px);
        }
        .nav-item {
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .glass-card {
            background: rgba(17, 24, 39, 0.7);
            backdrop-filter: blur(12px);
            border: 1px solid rgba(255, 255, 255, 0.08);
        }
        .gradient-btn {
            background: linear-gradient(135deg, #38bdf8 0%, #0369a1 100%);
            box-shadow: 0 4px 15px rgba(56, 189, 248, 0.2);
        }
        .custom-scrollbar::-webkit-scrollbar {
            width: 4px;
        }
        .custom-scrollbar::-webkit-scrollbar-thumb {
            background: rgba(56, 189, 248, 0.3);
            border-radius: 10px;
        }
        /* Tab transitions */
        .page-tab {
            display: none;
            animation: fadeIn 0.4s ease-in-out forwards;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }
        /* Continuous spinning animation for loading spinner */
        @keyframes spin {
            from { transform: rotate(0deg); }
            to { transform: rotate(360deg); }
        }
        .animate-spin-custom {
            animation: spin 1s linear infinite;
        }
    </style>
</head>
<body class="flex flex-col min-h-screen antialiased">

    <div id="global-loading" class="fixed inset-0 z-50 flex flex-col items-center justify-center bg(#0b0f19)">
        <div class="relative w-16 h-16 mb-4">
            <div class="absolute inset-0 rounded-full border-4 border-sky-500/20"></div>
            <div class="absolute inset-0 rounded-full border-4 border-t-sky-500 animate-spin-custom"></div>
        </div>
        <p class="text-sky-400 font-medium tracking-wide animate-pulse">Loading Secure Environment...</p>
    </div>

    <main class="flex-1 pb-24 px-4 pt-4 max-w-md mx-auto w-full overflow-y-auto custom-scrollbar">
        
        <div class="glass-card rounded-2xl p-4 mb-5 flex items-center justify-between shadow-xl relative overflow-hidden">
            <div class="absolute -right-6 -top-6 w-24 h-24 bg-sky-500/10 rounded-full blur-xl pointer-events-none"></div>
            <div class="flex items-center space-x-3 z-10">
                <div class="w-11 h-11 rounded-xl bg-gradient-to-tr from-sky-400 to-sky-600 flex items-center justify-center text-white font-bold text-lg shadow-inner shadow-white/20" id="user-avatar">
                     U
                </div>
                <div>
                    <h2 class="font-semibold text-sm tracking-wide text-gray-200" id="user-fullname">Loading User...</h2>
                    <p class="text-xs text-emerald-400 font-medium flex items-center gap-1 mt-0.5">
                        <span class="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-ping"></span> Account Verified
                    </p>
                </div>
            </div>
        </div>

        <div id="tab-dashboard" class="page-tab block space-y-5">
            <div class="grid grid-cols-2 gap-4">
                <div class="glass-card rounded-2xl p-4 relative overflow-hidden">
                    <div class="absolute right-3 top-3 text-sky-500/20 text-3xl"><i class="fa-solid fa-coins"></i></div>
                    <p class="text-xs text-gray-400 font-medium tracking-wider uppercase">Coin Balance</p>
                    <h3 class="text-2xl font-bold text-white mt-1 tracking-tight" id="dash-coins">0.00</h3>
                    <p class="text-[10px] text-sky-400 mt-2 flex items-center gap-1"><i class="fa-solid fa-arrow-up-right-from-square"></i> Convert Anytime</p>
                </div>
                <div class="glass-card rounded-2xl p-4 relative overflow-hidden">
                    <div class="absolute right-3 top-3 text-emerald-500/20 text-3xl"><i class="fa-solid fa-wallet"></i></div>
                    <p class="text-xs text-gray-400 font-medium tracking-wider uppercase">USD Value</p>
                    <h3 class="text-2xl font-bold text-emerald-400 mt-1 tracking-tight" id="dash-usd">$0.00</h3>
                    <p class="text-[10px] text-gray-400 mt-2">Rate: 100 Coins = $1.00</p>
                </div>
            </div>

            <div class="glass-card rounded-xl px-3 py-2.5 flex items-center gap-3 text-xs bg-sky-950/20 border-sky-500/20">
                <div class="bg-sky-500/20 p-1.5 rounded-lg text-sky-400"><i class="fa-solid fa-bullhorn text-sm"></i></div>
                <div class="overflow-hidden w-full relative">
                    <div class="whitespace-nowrap inline-block animate-[marquee_25s_linear_infinite] text-gray-300 hover:[animation-play-state:paused]" id="global-notice">
                        Loading global updates from system server...
                    </div>
                </div>
            </div>
            <style>
                @keyframes marquee {
                    0% { transform: translate3d(100%, 0, 0); }
                    100% { transform: translate3d(-100%, 0, 0); }
                }
            </style>

            <div class="glass-card rounded-2xl p-5 relative overflow-hidden border-sky-500/20">
                <div class="absolute top-0 right-0 bg-sky-500/10 px-3 py-1 text-[10px] font-bold tracking-widest uppercase text-sky-400 rounded-bl-xl border-l border-b border-white/5">Auto Task</div>
                <h4 class="font-semibold text-base text-white flex items-center gap-2"><i class="fa-solid fa-play-circle text-sky-400 text-lg"></i> Sponsored Premium Ads</h4>
                <p class="text-xs text-gray-400 mt-1.5 leading-relaxed">Watch high-tier premium advertisements to fulfill your daily quota. Watch 3 full ads to unlock instant claimable coin rewards.</p>
                
                <div class="mt-4 bg-gray-900/80 rounded-full h-2 w-full p-[1px] border border-white/5">
                    <div class="bg-gradient-to-r from-sky-400 to-sky-600 h-full rounded-full transition-all duration-500" id="ad-progress-bar" style="width: 0%"></div>
                </div>
                <div class="flex justify-between items-center mt-2 text-xs">
                    <span class="text-gray-400 font-medium">Daily Complete Progress:</span>
                    <span class="text-sky-400 font-bold bg-sky-500/10 px-2 py-0.5 rounded" id="ad-progress-text">0 / 3</span>
                </div>

                <div class="grid grid-cols-2 gap-3 mt-4">
                    <button id="btn-watch-ad" class="gradient-btn py-3 rounded-xl font-semibold text-xs tracking-wider uppercase text-white flex items-center justify-center gap-2 active:scale-95 transition-transform">
                        <i class="fa-solid fa-bolt"></i> Watch Ad (<span id="ad-timer">Ready</span>)
                    </button>
                    <button id="btn-claim-ad" disabled class="bg-gray-800 text-gray-500 cursor-not-allowed py-3 rounded-xl font-semibold text-xs tracking-wider uppercase flex items-center justify-center gap-2 transition-all" onclick="claimMissionReward('ads')">
                        <i class="fa-solid fa-gift"></i> Claim 15 C
                    </button>
                </div>
            </div>

            <div class="glass-card rounded-2xl p-5 relative overflow-hidden border-indigo-500/20">
                <div class="absolute top-0 right-0 bg-indigo-500/10 px-3 py-1 text-[10px] font-bold tracking-widest uppercase text-indigo-400 rounded-bl-xl border-l border-b border-white/5">Interactive</div>
                <h4 class="font-semibold text-base text-white flex items-center gap-2"><i class="fa-solid fa-star text-indigo-400 text-lg"></i> Micro System Reviews</h4>
                <p class="text-xs text-gray-400 mt-1.5 leading-relaxed">Submit helpful ratings or micro reviews for affiliated networks. Requires at least 2 system reviews daily to unlock premium claim.</p>

                <div class="mt-4 bg-gray-900/80 rounded-full h-2 w-full p-[1px] border border-white/5">
                    <div class="bg-gradient-to-r from-indigo-400 to-indigo-600 h-full rounded-full transition-all duration-500" id="review-progress-bar" style="width: 0%"></div>
                </div>
                <div class="flex justify-between items-center mt-2 text-xs">
                    <span class="text-gray-400 font-medium">Review Milestones:</span>
                    <span class="text-indigo-400 font-bold bg-indigo-500/10 px-2 py-0.5 rounded" id="review-progress-text">0 / 2</span>
                </div>

                <div class="grid grid-cols-2 gap-3 mt-4">
                    <button class="bg-indigo-600/20 border border-indigo-500/30 text-indigo-300 hover:bg-indigo-600/30 py-3 rounded-xl font-semibold text-xs tracking-wider uppercase flex items-center justify-center gap-2 active:scale-95 transition-transform" onclick="triggerReviewSim()">
                        <i class="fa-solid fa-pen-to-square"></i> Perform Review
                    </button>
                    <button id="btn-claim-review" disabled class="bg-gray-800 text-gray-500 cursor-not-allowed py-3 rounded-xl font-semibold text-xs tracking-wider uppercase flex items-center justify-center gap-2 transition-all" onclick="claimMissionReward('reviews')">
                        <i class="fa-solid fa-gift"></i> Claim 10 C
                    </button>
                </div>
            </div>
        </div>

        <div id="tab-tasks" class="page-tab space-y-4">
            <div class="glass-card rounded-2xl p-4">
                <h3 class="font-bold text-lg text-white mb-1"><i class="fa-solid fa-layer-group text-sky-400 mr-2"></i> Mission Control Center</h3>
                <p class="text-xs text-gray-400">Complete these custom administrative tasks to harvest substantial coin payloads instantly.</p>
            </div>
            
            <div id="manual-tasks-container" class="space-y-3">
                </div>
        </div>

        <div id="tab-refer" class="page-tab space-y-5">
            <div class="glass-card rounded-2xl p-6 text-center relative overflow-hidden">
                <div class="absolute -left-10 -bottom-10 w-32 h-32 bg-sky-500/10 rounded-full blur-2xl pointer-events-none"></div>
                <div class="w-16 h-16 bg-sky-500/10 text-sky-400 rounded-2xl flex items-center justify-center text-2xl mx-auto mb-4 border border-sky-500/20 shadow-inner">
                    <i class="fa-solid fa-user-plus"></i>
                </div>
                <h3 class="text-xl font-bold text-white tracking-wide">Network Propagation</h3>
                <p class="text-xs text-gray-400 max-w-xs mx-auto mt-2 leading-relaxed">
                    Expand our network ecosystem. Invite friends and colleagues using your personalized connection gateway token below.
                </p>
                <div class="mt-4 p-3 bg-sky-950/40 rounded-xl border border-sky-500/20 flex items-center justify-between gap-2">
                    <span class="text-xs text-sky-300 font-mono truncate select-all w-full text-left" id="refer-link-text">Generating code...</span>
                    <button class="bg-sky-500 hover:bg-sky-600 text-white font-bold text-xs px-4 py-2 rounded-lg transition-colors active:scale-95" onclick="copyReferLink()">
                        Copy
                    </button>
                </div>
                <div class="mt-2 text-[11px] text-emerald-400 font-medium flex items-center justify-center gap-1">
                    <i class="fa-solid fa-circle-check"></i> Earn <span id="ref-bonus-amt">10</span> Coins per validated referral user.
                </div>
            </div>

            <div class="glass-card rounded-2xl p-4">
                <h4 class="font-semibold text-sm text-gray-300 mb-3 tracking-wider uppercase"><i class="fa-solid fa-users text-sky-400 mr-1"></i> Connected Node Peers</h4>
                <div id="referrals-list" class="space-y-2.5 max-h-60 overflow-y-auto custom-scrollbar pr-1">
                    </div>
            </div>
        </div>

        <div id="tab-wallet" class="page-tab space-y-5">
            <div class="glass-card rounded-2xl p-5 border-emerald-500/20 bg-gradient-to-b from-gray-900/90 to-emerald-950/20">
                <p class="text-xs text-gray-400 uppercase tracking-wider font-medium">Liquidation Portal</p>
                <div class="flex justify-between items-baseline mt-1.5">
                    <h3 class="text-3xl font-black text-white tracking-tight" id="wallet-coins">0.00</h3>
                    <span class="text-sm font-bold text-emerald-400" id="wallet-usd">$0.00 USD</span>
                </div>
                <div class="border-t border-white/5 my-4"></div>
                <div class="flex justify-between text-xs text-gray-400">
                    <span>Minimum Allowed Limit: <b class="text-white" id="min-withdraw-lbl">100 C</b></span>
                    <span>Administrative Fee: <b class="text-white" id="withdraw-charge-lbl">5%</b></span>
                </div>
            </div>

            <div class="glass-card rounded-2xl p-5 space-y-4">
                <h4 class="font-semibold text-sm text-gray-200 tracking-wide"><i class="fa-solid fa-building-columns text-emerald-400 mr-1"></i> Configure Payout Channels</h4>
                
                <div>
                    <label class="block text-xs text-gray-400 font-medium mb-1.5">Administrative Gateway Method</label>
                    <select id="withdraw-method" class="w-full bg-gray-950 border border-white/10 rounded-xl px-3 py-3 text-sm focus:outline-none focus:border-emerald-500/50 text-white">
                        </select>
                </div>

                <div>
                    <label class="block text-xs text-gray-400 font-medium mb-1.5">Account Identification Number / Address</label>
                    <div class="relative">
                        <span class="absolute left-3.5 top-3.5 text-gray-500 text-xs"><i class="fa-solid fa-hashtag"></i></span>
                        <input type="text" id="withdraw-number" placeholder="e.g. 017XXXXXXXX" class="w-full bg-gray-950 border border-white/10 rounded-xl pl-9 pr-3 py-3 text-sm focus:outline-none focus:border-emerald-500/50 text-white font-mono placeholder:text-gray-600">
                    </div>
                </div>

                <div>
                    <label class="block text-xs text-gray-400 font-medium mb-1.5">Liquidation Quantitative Amount (Coins)</label>
                    <div class="relative">
                        <span class="absolute left-3.5 top-3.5 text-gray-500 text-xs"><i class="fa-solid fa-coins"></i></span>
                        <input type="number" id="withdraw-amount" placeholder="Minimum required" class="w-full bg-gray-950 border border-white/10 rounded-xl pl-9 pr-3 py-3 text-sm focus:outline-none focus:border-emerald-500/50 text-white font-mono placeholder:text-gray-600">
                    </div>
                </div>

                <button class="w-full bg-gradient-to-r from-emerald-500 to-teal-700 hover:from-emerald-600 hover:to-teal-800 text-white text-xs tracking-wider uppercase font-bold py-3.5 rounded-xl shadow-lg active:scale-98 transition-all mt-2" onclick="submitWithdrawPayload()">
                    Initialize Payout Transfer
                </button>
            </div>

            <div class="glass-card rounded-2xl p-4">
                <h4 class="font-semibold text-xs text-gray-400 tracking-wider uppercase mb-3"><i class="fa-solid fa-clock-rotate-left text-emerald-400 mr-1"></i> Ledger Ledger Payout Records</h4>
                <div id="withdrawals-history-list" class="space-y-2.5 max-h-48 overflow-y-auto custom-scrollbar pr-1">
                    </div>
            </div>
        </div>

    </main>

    <nav class="fixed bottom-0 left-0 right-0 glass-card border-t border-white/5 rounded-t-3xl px-3 py-2 z-40 max-w-md mx-auto w-full flex justify-around items-center shadow-2xl">
        <button onclick="switchTab('dashboard')" id="nav-dashboard" class="nav-item flex flex-col items-center gap-1 text-gray-400 py-1.5 px-3 rounded-xl nav-active">
            <i class="fa-solid fa-chart-pie text-xl"></i>
            <span class="text-[10px] font-semibold tracking-wide">Overview</span>
        </button>
        <button onclick="switchTab('tasks')" id="nav-tasks" class="nav-item flex flex-col items-center gap-1 text-gray-400 py-1.5 px-3 rounded-xl">
            <div class="relative">
                <i class="fa-solid fa-list-check text-xl"></i>
                <span id="badge-tasks" class="hidden absolute -top-1.5 -right-2 bg-sky-500 text-white text-[8px] font-black w-4 h-4 rounded-full flex items-center justify-center animate-bounce">0</span>
            </div>
            <span class="text-[10px] font-semibold tracking-wide">Missions</span>
        </button>
        <button onclick="switchTab('refer')" id="nav-refer" class="nav-item flex flex-col items-center gap-1 text-gray-400 py-1.5 px-3 rounded-xl">
            <i class="fa-solid fa-users-rectangle text-xl"></i>
            <span class="text-[10px] font-semibold tracking-wide">Network</span>
        </button>
        <button onclick="switchTab('wallet')" id="nav-wallet" class="nav-item flex flex-col items-center gap-1 text-gray-400 py-1.5 px-3 rounded-xl">
            <i class="fa-solid fa-vault text-xl"></i>
            <span class="text-[10px] font-semibold tracking-wide">Wallet</span>
        </button>
    </nav>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();
        tg.ready();

        // Safe User Identification extraction runtime fallback
        const initDataRaw = tg.initData || "";
        let userData = { id: 0, first_name: "Anonymous User" };
        
        if (tg.initDataUnsafe && tg.initDataUnsafe.user) {
            userData = tg.initDataUnsafe.user;
        }

        let appStateData = null;
        let isAdCooldownActive = false;

        // UI Core Tab Switcher Logic
        function switchTab(tabId) {
            document.querySelectorAll('.page-tab').forEach(el => el.style.display = 'none');
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('nav-active'));
            
            const targetTab = document.getElementById(`tab-${tabId}`);
            const targetNav = document.getElementById(`nav-${tabId}`);
            if(targetTab) targetTab.style.display = 'block';
            if(targetNav) targetNav.classList.add('nav-active');
        }

        // Initialize connection with central FastAPI pipeline sync
        async function synchroniseStateCache() {
            try {
                const response = await fetch('/api/sync-user', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ initData: initDataRaw })
                });
                const resData = await response.json();
                if (resData.ok) {
                    appStateData = resData;
                    renderUIDataPayload();
                } else {
                    alert("Authentication Error: Access Denied via WebApp Security standard validation rules.");
                }
            } catch (err) {
                console.error("Pipeline synchronization error:", err);
            } finally {
                document.getElementById('global-loading').style.display = 'none';
            }
        }

        function renderUIDataPayload() {
            if (!appStateData) return;
            const u = appStateData.user;
            const cfg = appStateData.config;

            // Header profile context update
            document.getElementById('user-fullname').innerText = `${u.first_name} ${u.last_name || ''}`;
            document.getElementById('user-avatar').innerText = u.first_name.charAt(0).toUpperCase();

            // Balances tracking
            const coinVal = u.coins || 0;
            const usdCalculated = (coinVal / 100).toFixed(2);
            document.getElementById('dash-coins').innerText = coinVal.toLocaleString();
            document.getElementById('dash-usd').innerText = `$${usdCalculated}`;
            document.getElementById('wallet-coins').innerText = coinVal.toLocaleString();
            document.getElementById('wallet-usd').innerText = `$${usdCalculated} USD`;

            // Server configs updates mappings
            document.getElementById('global-notice').innerText = cfg.notice || "";
            document.getElementById('ref-bonus-amt').innerText = cfg.refer_bonus || 10;
            document.getElementById('min-withdraw-lbl').innerText = `${cfg.min_withdraw || 100} Coins`;
            document.getElementById('withdraw-charge-lbl').innerText = `${cfg.withdraw_charge || 5}%`;
            
            // Build withdraw payment configuration elements dropdown selector options
            const methodSelect = document.getElementById('withdraw-method');
            methodSelect.innerHTML = "";
            (cfg.payment_methods || ["bKash","Nagad","Rocket"]).forEach(m => {
                const opt = document.createElement('option');
                opt.value = m; opt.innerText = m;
                methodSelect.appendChild(opt);
            });

            // Referral dynamic assignment mapping string initialization links
            document.getElementById('refer-link-text').innerText = `https://t.me/${appStateData.bot_username}?start=${u.user_id}`;

            // Handle dynamic structural system tracking status updates indicators triggers
            const todayStr = new Date().toISOString().split('T')[0];
            const tasksTrack = u.tasks || {};
            const adsCount = tasksTrack.last_date === todayStr ? (tasksTrack.ads || 0) : 0;
            const revCount = tasksTrack.last_date === todayStr ? (tasksTrack.reviews || 0) : 0;

            // Updates ads modules status visually
            const adPct = Math.min((adsCount / 3) * 100, 100);
            document.getElementById('ad-progress-bar').style.width = `${adPct}%`;
            document.getElementById('ad-progress-text').innerText = `${adsCount} / 3`;
            
            const btnClaimAd = document.getElementById('btn-claim-ad');
            if (adsCount >= 3 && !tasksTrack.ads_claimed) {
                btnClaimAd.disabled = false;
                btnClaimAd.className = "bg-gradient-to-r from-amber-500 to-orange-600 text-white py-3 rounded-xl font-semibold text-xs tracking-wider uppercase text-center cursor-pointer active:scale-95 transition-all";
            } else if (tasksTrack.ads_claimed) {
                btnClaimAd.disabled = true;
                btnClaimAd.innerText = "Claimed";
                btnClaimAd.className = "bg-emerald-950 text-emerald-500 border border-emerald-500/20 py-3 rounded-xl font-semibold text-xs tracking-wider uppercase text-center cursor-not-allowed opacity-60";
            }

            // Updates micro systematic reviews layout status trackers
            const revPct = Math.min((revCount / 2) * 100, 100);
            document.getElementById('review-progress-bar').style.width = `${revPct}%`;
            document.getElementById('review-progress-text').innerText = `${revCount} / 2`;

            const btnClaimRev = document.getElementById('btn-claim-review');
            if (revCount >= 2 && !tasksTrack.reviews_claimed) {
                btnClaimRev.disabled = false;
                btnClaimRev.className = "bg-gradient-to-r from-amber-500 to-orange-600 text-white py-3 rounded-xl font-semibold text-xs tracking-wider uppercase text-center cursor-pointer active:scale-95 transition-all";
            } else if (tasksTrack.reviews_claimed) {
                btnClaimRev.disabled = true;
                btnClaimRev.innerText = "Claimed";
                btnClaimRev.className = "bg-emerald-950 text-emerald-500 border border-emerald-500/20 py-3 rounded-xl font-semibold text-xs tracking-wider uppercase text-center cursor-not-allowed opacity-60";
            }

            // Execute processing operations to render manually built mission panels lists cards layouts structures elements
            renderManualTasksListCards(appStateData.tasks, u.completed_tasks || []);
            renderReferralSubNodesList(appStateData.referrals || []);
            renderWithdrawalLedgerHistoryItemsList(appStateData.withdrawals || []);
        }

        // Simulates programmatic action watching system network premium advertisements trackers seamlessly
        document.getElementById('btn-watch-ad').addEventListener('click', async function() {
            if (isAdCooldownActive) return;
            const btn = this;
            const timerSpan = document.getElementById('ad-timer');
            isAdCooldownActive = true;
            btn.disabled = true;
            
            let remaining = appStateData.config.ad_interval || 30;
            btn.className = "bg-gray-900 border border-white/5 text-gray-500 cursor-not-allowed py-3 rounded-xl font-semibold text-xs tracking-wider uppercase flex items-center justify-center gap-2 transition-all";
            
            const handle = setInterval(() => {
                remaining--;
                if(remaining <= 0) {
                    clearInterval(handle);
                    isAdCooldownActive = false;
                    btn.disabled = false;
                    timerSpan.innerText = "Ready";
                    btn.className = "gradient-btn py-3 rounded-xl font-semibold text-xs tracking-wider uppercase text-white flex items-center justify-center gap-2 active:scale-95 transition-transform";
                } else {
                    timerSpan.innerText = `${remaining}s`;
                }
            }, 1000);

            try {
                const res = await fetch('/api/increment-ad', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ uid: userData.id, initData: initDataRaw })
                });
                const data = await res.json();
                if(data.ok) {
                    alert("System Analytics Logged: Advertisement sequence completed successfully. +1 added to dynamic milestone metric.");
                    synchroniseStateCache();
                } else {
                    alert(data.msg || "Error mapping processing request stream context.");
                }
            } catch(e) {
                console.error(e);
            }
        });

        // Simulates ratings operations trigger
        async function triggerReviewSim() {
            if(!confirm("Proceed to initialize dynamic tracking validation redirect payload stream matrix check layer?")) return;
            try {
                const res = await fetch('/api/increment-review', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ uid: userData.id, initData: initDataRaw })
                });
                const data = await res.json();
                if(data.ok) {
                    alert("System Matrix Validation: Dynamic review processing stream validated successfully.");
                    synchroniseStateCache();
                } else {
                    alert(data.msg || "Administrative restriction limit exception detected.");
                }
            } catch(e) { console.error(e); }
        }

        // Request execution for system operations micro validation parameters triggers maps
        async function claimMissionReward(type) {
            try {
                const res = await fetch('/api/claim-mission', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ uid: userData.id, task_type: type, initData: initDataRaw })
                });
                const data = await res.json();
                if(data.ok) {
                    alert("Transaction Confirmed: Coins payload credited to user secure ledger balance.");
                    synchroniseStateCache();
                } else {
                    alert(data.msg || "Transaction failed verification checklist rules.");
                }
            } catch(e) { console.error(e); }
        }

        function renderManualTasksListCards(tasks, completedIds) {
            const container = document.getElementById('manual-tasks-container');
            container.innerHTML = "";
            let availableCount = 0;

            if(!tasks || tasks.length === 0) {
                container.innerHTML = `<div class="glass-card rounded-2xl p-6 text-center text-xs text-gray-500 font-medium">No open system operational missions currently deployed by administrators. Check back later.</div>`;
                document.getElementById('badge-tasks').className = "hidden";
                return;
            }

            tasks.forEach(t => {
                const isDone = completedIds.includes(t._id);
                if(!isDone) availableCount++;

                const card = document.createElement('div');
                card.className = "glass-card rounded-xl p-4 flex items-center justify-between gap-3 relative overflow-hidden transition-all";
                
                card.innerHTML = `
                    <div class="flex items-center gap-3 w-full min-w-0">
                        <div class="w-10 h-10 rounded-xl bg-sky-500/10 border border-sky-500/20 text-sky-400 flex items-center justify-center text-sm flex-shrink-0">
                            <i class="fa-solid fa-tasks"></i>
                        </div>
                        <div class="truncate w-full">
                            <h5 class="text-xs font-semibold text-gray-200 truncate">${t.title}</h5>
                            <p class="text-[10px] text-gray-400 font-medium tracking-tight mt-0.5 flex items-center gap-1">
                                <b class="text-sky-400 font-bold bg-sky-500/10 px-1 rounded">${t.reward} Coins</b> • Action required
                            </p>
                        </div>
                    </div>
                    <div class="flex-shrink-0">
                        ${isDone ? 
                            `<span class="text-[10px] font-bold text-emerald-400 bg-emerald-500/10 px-2.5 py-1.5 rounded-lg border border-emerald-500/20 flex items-center gap-1"><i class="fa-solid fa-circle-check"></i> Completed</span>` : 
                            `<button onclick="executeManualTaskClaim('${t._id}', '${t.link}')" class="bg-gray-800 text-gray-200 hover:bg-sky-500 hover:text-white font-bold text-[10px] tracking-wider uppercase px-3 py-2 rounded-lg border border-white/5 transition-all active:scale-95">Complete</button>`
                        }
                    </div>
                `;
                container.appendChild(card);
            });

            const badge = document.getElementById('badge-tasks');
            if(availableCount > 0) {
                badge.innerText = availableCount;
                badge.className = "absolute -top-1.5 -right-2 bg-sky-500 text-white text-[8px] font-black w-4 h-4 rounded-full flex items-center justify-center animate-bounce";
            } else {
                badge.className = "hidden";
            }
        }

        async function executeManualTaskClaim(taskId, link) {
            if(link && link.trim() !== "") {
                tg.openLink(link);
            }
            alert("System Validation Process: Validating node connection context. Please allow up to several seconds for administrative processing pipeline check checks.");
            try {
                const res = await fetch('/api/claim-manual-task', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ uid: userData.id, task_id: taskId, initData: initDataRaw })
                });
                const data = await res.json();
                if(data.ok) {
                    alert(`Mission Success: +${data.reward} Coins allocation sequence processed successfully.`);
                    synchroniseStateCache();
                } else {
                    alert(data.msg || "Administrative verification rejection protocol raised.");
                }
            } catch(e) { console.error(e); }
        }

        function renderReferralSubNodesList(referrals) {
            const container = document.getElementById('referrals-list');
            container.innerHTML = "";
            if(referrals.length === 0) {
                container.innerHTML = `<p class="text-[11px] text-gray-500 text-center py-4 font-medium italic">No active direct sub-nodes propagated under this gateway identifier index.</p>`;
                return;
            }
            referrals.forEach(r => {
                const item = document.createElement('div');
                item.className = "flex items-center justify-between p-2.5 bg-gray-950/40 rounded-xl border border-white/5 text-xs text-gray-300";
                item.innerHTML = `
                    <div class="flex items-center gap-2">
                        <div class="w-2 h-2 rounded-full bg-sky-400"></div>
                        <span class="font-medium truncate max-w-[140px]">${r.first_name}</span>
                    </div>
                    <span class="text-[10px] font-mono text-gray-500">ID: ...${String(r.user_id).slice(-5)}</span>
                `;
                container.appendChild(item);
            });
        }

        function renderWithdrawalLedgerHistoryItemsList(items) {
            const container = document.getElementById('withdrawals-history-list');
            container.innerHTML = "";
            if(items.length === 0) {
                container.innerHTML = `<p class="text-[11px] text-gray-500 text-center py-4 font-medium italic">No historical payout settlement entries currently recorded in this account ledger ledger index.</p>`;
                return;
            }
            items.forEach(w => {
                let statusColor = "text-amber-400 bg-amber-500/10 border-amber-500/20";
                if(w.status === "Approved") statusColor = "text-emerald-400 bg-emerald-500/10 border-emerald-500/20";
                if(w.status === "Rejected") statusColor = "text-rose-400 bg-rose-500/10 border-rose-500/20";
                
                const item = document.createElement('div');
                item.className = "p-3 bg-gray-950/50 rounded-xl border border-white/5 text-xs space-y-1.5";
                item.innerHTML = `
                    <div class="flex justify-between items-center">
                        <span class="font-bold text-gray-200">${w.method} <span class="text-[10px] font-mono font-medium text-gray-500">(${w.number})</span></span>
                        <b class="text-white">${w.amount} C</b>
                    </div>
                    <div class="flex justify-between items-center text-[10px]">
                        <span class="text-gray-500 font-mono">${w.date || ''}</span>
                        <span class="px-2 py-0.5 rounded-md border font-semibold tracking-wide uppercase text-[8px] ${statusColor}">${w.status}</span>
                    </div>
                `;
                container.appendChild(item);
            });
        }

        async function submitWithdrawPayload() {
            const method = document.getElementById('withdraw-method').value;
            const number = document.getElementById('withdraw-number').value.trim();
            const amount = parseFloat(document.getElementById('withdraw-amount').value);

            if(!number || isNaN(amount) || amount <= 0) {
                alert("Input Validation Failure: Please fulfill all qualitative criteria inputs parameters properly before initialization.");
                return;
            }

            try {
                const res = await fetch('/api/withdraw-request', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ uid: userData.id, method: method, number: number, amount: amount, initData: initDataRaw })
                });
                const data = await res.json();
                if(data.ok) {
                    alert("Success Status: Settlement payout request registered successfully. Awaiting compliance review validation operations pipeline checklist processing.");
                    document.getElementById('withdraw-number').value = "";
                    document.getElementById('withdraw-amount').value = "";
                    synchroniseStateCache();
                } else {
                    alert(data.msg || "Execution blocked by database business validation logic layer exception.");
                }
            } catch(e) { console.error(e); }
        }

        function copyReferLink() {
            const txt = document.getElementById('refer-link-text').innerText;
            navigator.clipboard.writeText(txt).then(() => {
                alert("Success: Referral validation link token copied safely to computing clipboard system buffers.");
            }).catch(() => {
                alert("Fail to copy reference target value via script layer context runtime automation bounds.");
            });
        }

        // Self runtime starting execution loop triggers
        synchroniseStateCache();
    </script>
</body>
</html>
"""

# ==========================================
# 8. Core API System Endpoint Pipelines (Data Flow Sync Controllers)
# ==========================================
@app.post("/api/sync-user")
async def sync_user_state(payload: TeleInitData):
    if db is None:
        return {"ok": False, "msg": "Database not active"}
    
    u_info = verify_telegram_data(payload.initData)
    if not u_info and TOKEN:
        return {"ok": False, "msg": "Invalid verification token"}
        
    uid = u_info.get("id", OWNER_ID)
    first_name = u_info.get("first_name", "DevUser")
    last_name = u_info.get("last_name", "")
    username = u_info.get("username", "")
    
    if uid in BANNED_USERS:
        return {"ok": False, "msg": "Banned"}
        
    async with get_user_lock(uid):
        user = await db.users.find_one({"user_id": uid})
        if not user:
            # Parse possible referral reference parameter from global bot execution args context metadata
            ref_id = None
            user = {
                "user_id": uid,
                "first_name": first_name,
                "last_name": last_name,
                "username": username,
                "coins": 0.0,
                "referred_by": ref_id,
                "completed_tasks": [],
                "tasks": {
                    "last_date": str(datetime.date.today()),
                    "ads": 0,
                    "ads_claimed": False,
                    "reviews": 0,
                    "reviews_claimed": False
                },
                "joined_at": time.time()
            }
            await db.users.insert_one(user)
            
            # Apply network referral expansion logic bonuses payload structure safely to targets implicitly
            if ref_id:
                bonus = cached_config.get("refer_bonus", 10)
                await db.users.update_one({"user_id": ref_id}, {"$inc": {"coins": bonus}})
                # Send non-blocking update notification packet via integrated aiogram components if alive contextually
                if bot:
                    try:
                        await bot.send_message(ref_id, f"🎉 New Node Peer Connected! You received +{bonus} Coins from reference validation pipeline parameters.")
                    except Exception: pass

        # Structural data integrity check fallback verification logic
        today_str = str(datetime.date.today())
        if user.get("tasks", {}).get("last_date") != today_str:
            await db.users.update_one({"user_id": uid}, {"$set": {
                "tasks.last_date": today_str,
                "tasks.ads": 0,
                "tasks.ads_claimed": False,
                "tasks.reviews": 0,
                "tasks.reviews_claimed": False
            }})
            user = await db.users.find_one({"user_id": uid})

    # Pull contextual linked dynamic array lists maps objects criteria references seamlessly
    tasks_cursor = db.tasks.find({})
    tasks_list = []
    async for t in tasks_cursor:
        t["_id"] = str(t["_id"])
        tasks_list.append(t)
        
    referrals_cursor = db.users.find({"referred_by": uid}).limit(50)
    referrals_list = []
    async for r in referrals_cursor:
        referrals_list.append({"user_id": r["user_id"], "first_name": r["first_name"]})
        
    withdraw_cursor = db.withdrawals.find({"user_id": uid}).sort("_id", -1).limit(20)
    withdraw_list = []
    async for w in withdraw_cursor:
        w["_id"] = str(w["_id"])
        withdraw_list.append(w)

    user["_id"] = str(user["_id"])
    return {
        "ok": True,
        "user": user,
        "config": {
            "notice": cached_config.get("notice"),
            "refer_bonus": cached_config.get("refer_bonus"),
            "min_withdraw": cached_config.get("min_withdraw"),
            "withdraw_charge": cached_config.get("withdraw_charge"),
            "payment_methods": cached_config.get("payment_methods"),
            "ad_interval": cached_config.get("ad_interval")
        },
        "bot_username": BOT_USERNAME,
        "tasks": tasks_list,
        "referrals": referrals_list,
        "withdrawals": withdraw_list
    }

@app.post("/api/increment-ad")
async def increment_ad_counter(data: TeleInitData):
    # Standard security parsing execution checklist verification step bounds checks
    u_info = verify_telegram_data(data.initData)
    if not u_info and TOKEN: raise HTTPException(status_code=403, detail="Invalid Request Signature context.")
    uid = u_info.get("id", OWNER_ID)
    
    if uid in BANNED_USERS: return {"ok": False, "msg": "Banned"}
    
    async with get_user_lock(uid):
        user = await db.users.find_one({"user_id": uid})
        if not user: return {"ok": False, "msg": "User context null"}
        
        today_str = str(datetime.date.today())
        tasks = user.get("tasks", {})
        
        if tasks.get("last_date") != today_str:
            await db.users.update_one({"user_id": uid}, {"$set": {
                "tasks.last_date": today_str, "tasks.ads": 1, "tasks.ads_claimed": False
            }})
        else:
            await db.users.update_one({"user_id": uid}, {"$inc": {"tasks.ads": 1}})
            
    return {"ok": True}

@app.post("/api/increment-review")
async def increment_review_counter(data: TeleInitData):
    u_info = verify_telegram_data(data.initData)
    if not u_info and TOKEN: raise HTTPException(status_code=403, detail="Signature Check Failure")
    uid = u_info.get("id", OWNER_ID)
    
    if uid in BANNED_USERS: return {"ok": False, "msg": "Banned"}
    
    async with get_user_lock(uid):
        user = await db.users.find_one({"user_id": uid})
        if not user: return {"ok": False, "msg": "User missing"}
        
        today_str = str(datetime.date.today())
        tasks = user.get("tasks", {})
        
        if tasks.get("last_date") != today_str:
            await db.users.update_one({"user_id": uid}, {"$set": {
                "tasks.last_date": today_str, "tasks.reviews": 1, "tasks.reviews_claimed": False
            }})
        else:
            await db.users.update_one({"user_id": uid}, {"$inc": {"tasks.reviews": 1}})
            
    return {"ok": True}

@app.post("/api/claim-mission")
async def claim_mission_reward_endpoint(data: ClaimRewardData):
    u_info = verify_telegram_data(data.initData)
    if not u_info and TOKEN: raise HTTPException(status_code=403)
    
    if data.uid in BANNED_USERS: return {"ok": False, "msg": "Banned"}
    
    async with get_user_lock(data.uid):
        user = await db.users.find_one({"user_id": data.uid})
        if not user: return {"ok": False, "msg": "User object empty references index payload standard."}
        
        today_str = str(datetime.date.today())
        tasks = user.get("tasks", {})
        
        if tasks.get("last_date") != today_str:
            return {"ok": False, "msg": "মিশন সম্পূর্ণ হয়নি!"}
            
        if data.task_type == "ads" and tasks.get("ads", 0) >= 3 and not tasks.get("ads_claimed"):
            await db.users.update_one({"user_id": data.uid}, {"$set": {"tasks.ads_claimed": True}, "$inc": {"coins": 15}})
            return {"ok": True}
            
        if data.task_type == "reviews" and tasks.get("reviews", 0) >= 2 and not tasks.get("reviews_claimed"):
            await db.users.update_one({"user_id": data.uid}, {"$set": {"tasks.reviews_claimed": True}, "$inc": {"coins": 10}})
            return {"ok": True}
            
        return {"ok": False, "msg": "ইতিমধ্যে ক্লেইম করা হয়েছে বা মিশন সম্পূর্ণ হয়নি!"}

@app.post("/api/claim-manual-task")
async def claim_manual_task_endpoint(data: ManualTaskClaim):
    u_info = verify_telegram_data(data.initData)
    if not u_info and TOKEN: raise HTTPException(status_code=403)
    
    if data.uid in BANNED_USERS: return {"ok": False, "msg": "Banned"}
    
    async with get_user_lock(data.uid):
        user = await db.users.find_one({"user_id": data.uid})
        if not user: return {"ok": False, "msg": "User execution error context frame metadata map."}
        
        if data.task_id in user.get("completed_tasks", []):
            return {"ok": False, "msg": "ইতিমধ্যে মিশন সম্পূর্ণ করেছেন!"}
            
        task = await db.tasks.find_one({"_id": ObjectId(data.task_id)})
        if not task: return {"ok": False, "msg": "মিশন খুঁজে পাওয়া যায়নি!"}
        
        reward = task.get("reward", 0)
        await db.users.update_one({"user_id": data.uid}, {
            "$push": {"completed_tasks": data.task_id},
            "$inc": {"coins": reward}
        })
        
    return {"ok": True, "reward": reward}

@app.post("/api/withdraw-request")
async def create_withdraw_request_payout(data: WithdrawRequestData):
    u_info = verify_telegram_data(data.initData)
    if not u_info and TOKEN: raise HTTPException(status_code=403)
    
    if data.uid in BANNED_USERS: return {"ok": False, "msg": "Banned"}
    
    async with get_user_lock(data.uid):
        user = await db.users.find_one({"user_id": data.uid})
        if not user: return {"ok": False, "msg": "User missing"}
        
        min_w = cached_config.get("min_withdraw", 100)
        if data.amount < min_w:
            return {"ok": False, "msg": f"নূন্যতম উইথড্র সীমা {min_w} কয়েন!"}
            
        if user.get("coins", 0) < data.amount:
            return {"ok": False, "msg": "আপনার ব্যালেন্স পর্যাপ্ত নয়!"}
            
        # Deduct balances safely immediately within atomicity isolation context model standard routines checks
        await db.users.update_one({"user_id": data.uid}, {"$inc": {"coins": -data.amount}})
        
        payload = {
            "user_id": data.uid,
            "first_name": user.get("first_name", ""),
            "username": user.get("username", ""),
            "method": data.method,
            "number": data.number,
            "amount": data.amount,
            "status": "Pending",
            "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        await db.withdrawals.insert_one(payload)
        
    return {"ok": True}

# ==========================================
# 9. ADMIN PANEL FRONTEND PIPELINE LAYOUT OVERVIEW 
# ==========================================
@app.get("/admin-panel", response_class=HTMLResponse)
async def serve_admin_panel(authenticated: bool = Depends(authenticate_admin)):
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Control Terminal Matrix</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen p-6">
    <div class="max-w-5xl mx-auto space-y-6">
        <div class="flex justify-between items-center border-b border-slate-800 pb-4">
            <h1 class="text-2xl font-black tracking-wider text-sky-400 uppercase"><i class="fa-solid fa-satellite-dish mr-2"></i> Central Management Terminal Module Core</h1>
            <span class="bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 font-bold text-xs px-3 py-1 rounded-full uppercase tracking-widest animate-pulse">Secure Online Live</span>
        </div>
        
        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div class="bg-slate-900 border border-slate-800 p-5 rounded-2xl space-y-4 shadow-xl">
                <h3 class="font-bold text-sm tracking-widest text-slate-400 uppercase border-b border-slate-800 pb-2"><i class="fa-solid fa-sliders text-sky-400 mr-1"></i> System Variables Matrix</h3>
                <form action="/admin/update-config" method="POST" class="space-y-3 text-xs">
                    <div class="grid grid-cols-2 gap-3">
                        <div>
                            <label class="block text-slate-500 font-medium mb-1">Per View Reward (C)</label>
                            <input type="number" name="per_view_coin" step="0.1" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 font-mono text-white focus:outline-none focus:border-sky-500">
                        </div>
                        <div>
                            <label class="block text-slate-500 font-medium mb-1">Review Payload Reward (C)</label>
                            <input type="number" name="per_review_coin" step="0.1" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 font-mono text-white focus:outline-none focus:border-sky-500">
                        </div>
                    </div>
                    <div class="grid grid-cols-2 gap-3">
                        <div>
                            <label class="block text-slate-500 font-medium mb-1">Min Liquidation Limit</label>
                            <input type="number" name="min_withdraw" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 font-mono text-white focus:outline-none focus:border-sky-500">
                        </div>
                        <div>
                            <label class="block text-slate-500 font-medium mb-1">Compliance Processing Fee (%)</label>
                            <input type="number" name="withdraw_charge" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 font-mono text-white focus:outline-none focus:border-sky-500">
                        </div>
                    </div>
                    <div class="grid grid-cols-2 gap-3">
                        <div>
                            <label class="block text-slate-500 font-medium mb-1">Network Referral Yield (C)</label>
                            <input type="number" name="refer_bonus" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 font-mono text-white focus:outline-none focus:border-sky-500">
                        </div>
                        <div>
                            <label class="block text-slate-500 font-medium mb-1">Ad Stream Interval Cooldown (s)</label>
                            <input type="number" name="ad_interval" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 font-mono text-white focus:outline-none focus:border-sky-500">
                        </div>
                    </div>
                    <div>
                        <label class="block text-slate-500 font-medium mb-1">Broadcast Notification Ticker Text Message Notice</label>
                        <textarea name="notice" rows="2" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-white focus:outline-none focus:border-sky-500"></textarea>
                    </div>
                    <button type="submit" class="w-full bg-sky-500 hover:bg-sky-600 font-bold uppercase tracking-wider p-3 rounded-xl transition-all active:scale-98 text-slate-950">Commit Global Configurations Payload</button>
                </form>
            </div>

            <div class="bg-slate-900 border border-slate-800 p-5 rounded-2xl space-y-4 shadow-xl flex flex-col justify-between">
                <div>
                    <h3 class="font-bold text-sm tracking-widest text-slate-400 uppercase border-b border-slate-800 pb-2"><i class="fa-solid fa-plus-circle text-indigo-400 mr-1"></i> Deploy New Mission Directive</h3>
                    <form action="/admin/add-task" method="POST" class="space-y-3 text-xs mt-3">
                        <div>
                            <label class="block text-slate-500 font-medium mb-1">Mission Meta Description Title Header</label>
                            <input type="text" name="title" required placeholder="e.g. Join Official Telegram Announcement Channel Gateway Link Token" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-white focus:outline-none focus:border-indigo-500">
                        </div>
                        <div>
                            <label class="block text-slate-500 font-medium mb-1">Mission Yield Compensation Quantity Balance Allocation (Coins)</label>
                            <input type="number" name="reward" required placeholder="e.g. 50" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 font-mono text-white focus:outline-none focus:border-indigo-500">
                        </div>
                        <div>
                            <label class="block text-slate-500 font-medium mb-1">Verification Redirect Interface Target Link URL (Optional parameter)</label>
                            <input type="url" name="link" placeholder="https://t.me/example_channel" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-white focus:outline-none focus:border-indigo-500">
                        </div>
                        <button type="submit" class="w-full bg-indigo-500 hover:bg-indigo-600 font-bold uppercase tracking-wider p-3 rounded-xl transition-all text-white active:scale-98 mt-2">Deploy Mission Object Node</button>
                    </form>
                </div>
            </div>
        </div>

        <div class="bg-slate-900 border border-slate-800 p-5 rounded-2xl shadow-xl space-y-4">
            <h3 class="font-bold text-sm tracking-widest text-slate-400 uppercase border-b border-slate-800 pb-2"><i class="fa-solid fa-users-cog text-amber-500 mr-1"></i> Identity Management & Node Access Control Parameters</h3>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
                <form action="/admin/add-admin" method="POST" class="flex gap-2">
                    <input type="number" name="user_id" required placeholder="Target Global User Identification Number Index ID" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-white font-mono">
                    <button type="submit" class="bg-emerald-600 hover:bg-emerald-700 font-bold uppercase tracking-wider px-4 rounded-lg flex-shrink-0 text-white">Promote Admin</button>
                </form>
                <form action="/admin/ban-user" method="POST" class="flex gap-2">
                    <input type="number" name="user_id" required placeholder="Target Violator Node User Identification Index ID" class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-white font-mono">
                    <button type="submit" class="bg-rose-600 hover:bg-rose-700 font-bold uppercase tracking-wider px-4 rounded-lg flex-shrink-0 text-white">Quarantine Ban Node</button>
                </form>
            </div>
        </div>

        <div class="bg-slate-900 border border-slate-800 rounded-2xl shadow-xl overflow-hidden">
            <div class="p-4 bg-slate-900/50 border-b border-slate-800 flex justify-between items-center">
                <h3 class="font-bold text-sm tracking-widest text-slate-400 uppercase"><i class="fa-solid fa-receipt text-emerald-400 mr-1"></i> Liquidation Ledger Settlement Request Pipeline Stream Entries</h3>
                <button onclick="window.location.reload()" class="bg-slate-800 hover:bg-slate-700 text-slate-300 px-3 py-1 rounded text-xs font-semibold"><i class="fa-solid fa-sync mr-1"></i> Refresh Stream Logs</button>
            </div>
            <div class="overflow-x-auto text-xs">
                <table class="w-full text-left border-collapse">
                    <thead>
                        <tr class="bg-slate-950 text-slate-500 uppercase tracking-wider font-bold border-b border-slate-800 text-[10px]">
                            <th class="p-3.5">Timestamp Matrix</th>
                            <th class="p-3.5">User Identity Reference</th>
                            <th class="p-3.5">Gateway Channel</th>
                            <th class="p-3.5">Destination Address/No</th>
                            <th class="p-3.5">Quantum Volume (Coins)</th>
                            <th class="p-3.5">Validation Check Status</th>
                            <th class="p-3.5 text-right">Pipeline Interaction Controls Actions</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-800 font-medium" id="admin-withdrawals-tbody">
                        </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        async function populateAdminDashboardMetricsData() {
            try {
                // Read current existing configurations properties details maps dynamically instantly from context endpoints
                const response = await fetch('/api/sync-user', {
                    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({initData: ""})
                });
                const res = await response.json();
                if(res.ok) {
                    const cfg = res.config;
                    document.getElementsByName('per_view_coin')[0].value = cfg.per_view_coin || 1;
                    // Dynamic fill out operations definitions mapping values fields properties inside configurations views lists forms elements objects maps
                    document.getElementsByName('min_withdraw')[0].value = cfg.min_withdraw || 100;
                    document.getElementsByName('withdraw_charge')[0].value = cfg.withdraw_charge || 5;
                    document.getElementsByName('refer_bonus')[0].value = cfg.refer_bonus || 10;
                    document.getElementsByName('ad_interval')[0].value = cfg.ad_interval || 30;
                    document.getElementsByName('notice')[0].value = cfg.notice || "";
                }
                
                // Fetch internal collections data objects vectors arrays matrices dynamically
                const wRes = await fetch('/admin/api/all-withdrawals');
                const withdrawals = await wRes.json();
                
                const tbody = document.getElementById('admin-withdrawals-tbody');
                tbody.innerHTML = "";
                
                if(withdrawals.length === 0) {
                    tbody.innerHTML = `<tr><td colspan="7" class="p-8 text-center text-slate-600 font-medium italic">No structural payout ledger allocation entries records detected within database streams index.</td></tr>`;
                    return;
                }
                
                withdrawals.forEach(w => {
                    let badgeClass = "text-amber-400 bg-amber-500/10 border-amber-500/20";
                    if(w.status === "Approved") badgeClass = "text-emerald-400 bg-emerald-500/10 border-emerald-500/20";
                    if(w.status === "Rejected") badgeClass = "text-rose-400 bg-rose-500/10 border-rose-500/20";
                    
                    const tr = document.createElement('tr');
                    tr.className = "hover:bg-slate-900/40 transition-colors";
                    tr.innerHTML = `
                        <td class="p-3.5 font-mono text-slate-500 text-[11px]">${w.date || ''}</td>
                        <td class="p-3.5 font-bold text-slate-300">${w.first_name} <span class="text-slate-600 block text-[10px] font-mono font-normal">ID: ${w.user_id}</span></td>
                        <td class="p-3.5"><span class="bg-slate-800 px-2 py-1 rounded text-slate-300 font-semibold text-[11px]">${w.method}</span></td>
                        <td class="p-3.5 font-mono tracking-wide text-slate-300">${w.number}</td>
                        <td class="p-3.5 font-bold text-sky-400 text-sm">${w.amount} C</td>
                        <td class="p-3.5"><span class="border px-2.5 py-0.5 rounded-md font-bold tracking-wider uppercase text-[9px] ${badgeClass}">${w.status}</span></td>
                        <td class="p-3.5 text-right space-x-1 whitespace-nowrap">
                            \${w.status === 'Pending' ? `
                                <button onclick="processPayoutAction('\${w._id}', 'approve')" class="bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold px-2.5 py-1.5 rounded-lg uppercase tracking-wide text-[10px] transition-all">Approve</button>
                                <button onclick="processPayoutAction('\${w._id}', 'reject')" class="bg-rose-500/20 text-rose-400 hover:bg-rose-500 hover:text-slate-950 font-bold px-2.5 py-1.5 rounded-lg uppercase tracking-wide text-[10px] border border-rose-500/30 transition-all">Reject</button>
                            ` : `<span class="text-slate-600 italic text-[11px] pr-2">Settled Lock</span>`}
                        </td>
                    `;
                    tbody.appendChild(tr);
                });
                
            } catch(e) { console.error("Error drawing admin views dashboard interface canvas matrix:", e); }
        }
        
        async function processPayoutAction(id, action) {
            if(!confirm(`Are you sure you want to trigger operational deployment execution flag [ \${action.toUpperCase()} ] context parameter allocation matching identifier instance: \${id}?`)) return;
            try {
                const res = await fetch(`/admin/action-withdraw?id=\${id}&action=\${action}`, {method: 'POST'});
                const data = await res.json();
                if(data.ok) {
                    alert("Operation processed successfully within centralized database cluster context pipelines records updates trackers maps.");
                    populateAdminDashboardMetricsData();
                } else { alert(data.msg || "Error executing operations directive command framework."); }
            } catch(e) { console.error(e); }
        }
        
        // Execute bootstrap sequences instantly
        populateAdminDashboardMetricsData();
    </script>
</body>
</html>
"""

# ==========================================
# 10. Admin Administrative Panel API Endpoints Actions Handlers
# ==========================================
@app.get("/admin/api/all-withdrawals")
async def get_all_withdrawals_admin(authenticated: bool = Depends(authenticate_admin)):
    if db is None: return []
    cursor = db.withdrawals.find({}).sort("_id", -1).limit(250)
    out = []
    async for w in cursor:
        w["_id"] = str(w["_id"])
        out.append(w)
    return out

from fastapi.responses import RedirectResponse
from fastapi import Form

@app.post("/admin/update-config")
async def update_config_admin(
    per_view_coin: float = Form(...),
    per_review_coin: float = Form(...),
    min_withdraw: float = Form(...),
    withdraw_charge: float = Form(...),
    refer_bonus: float = Form(...),
    ad_interval: int = Form(...),
    notice: str = Form(...),
    authenticated: bool = Depends(authenticate_admin)
):
    global cached_config
    if db is None: return {"ok": False}
    
    await db.config.update_one({"type": "global"}, {"$set": {
        "per_view_coin": per_view_coin,
        "per_review_coin": per_review_coin,
        "min_withdraw": min_withdraw,
        "withdraw_charge": withdraw_charge,
        "refer_bonus": refer_bonus,
        "ad_interval": ad_interval,
        "notice": notice
    }})
    
    cached_config = await db.config.find_one({"type": "global"})
    return RedirectResponse(url="/admin-panel", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/add-task")
async def add_custom_task_admin(
    title: str = Form(...),
    reward: float = Form(...),
    link: str = Form(None),
    authenticated: bool = Depends(authenticate_admin)
):
    if db is None: return {"ok": False}
    payload = {
        "title": title,
        "reward": reward,
        "link": link or "",
        "created_at": time.time()
    }
    await db.tasks.insert_one(payload)
    return RedirectResponse(url="/admin-panel", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/add-admin")
async def add_admin_id_privilege(user_id: int = Form(...), authenticated: bool = Depends(authenticate_admin)):
    if db is None: return {"ok": False}
    await db.admins.update_one({"user_id": user_id}, {"$set": {"user_id": user_id, "promoted_at": time.time()}}, upsert=True)
    await load_admins()
    return RedirectResponse(url="/admin-panel", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/ban-user")
async def ban_user_node_identity(user_id: int = Form(...), authenticated: bool = Depends(authenticate_admin)):
    if db is None: return {"ok": False}
    await db.users.update_one({"user_id": user_id}, {"$set": {"banned": True}})
    await load_banned_users()
    return RedirectResponse(url="/admin-panel", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/action-withdraw")
async def process_withdraw_request_action(id: str, action: str, authenticated: bool = Depends(authenticate_admin)):
    if db is None: return {"ok": False, "msg": "Database offline"}
    
    w_req = await db.withdrawals.find_one({"_id": ObjectId(id)})
    if not w_req: return {"ok": False, "msg": "Request entry not found."}
    if w_req.get("status") != "Pending": return {"ok": False, "msg": "Request already finalized settlement lock status state parameters."}
    
    uid = w_req.get("user_id")
    amount = w_req.get("amount", 0)
    
    if action == "approve":
        await db.withdrawals.update_one({"_id": ObjectId(id)}, {"$set": {"status": "Approved"}})
        if bot:
            try:
                await bot.send_message(uid, f"✅ Withdrawal Approved! Your payment request for {amount} Coins has been successfully processed by the administration.")
            except Exception: pass
    elif action == "reject":
        await db.withdrawals.update_one({"_id": ObjectId(id)}, {"$set": {"status": "Rejected"}})
        # Refund user coins payload allocation securely
        await db.users.update_one({"user_id": uid}, {"$inc": {"coins": amount}})
        if bot:
            try:
                await bot.send_message(uid, f"❌ Withdrawal Rejected. Your payment request for {amount} Coins has been declined, and the balance has been refunded to your account.")
            except Exception: pass
            
    return {"ok": True}

# ==========================================
# 11. Telegram Aiogram Bot Router Handlers
# ==========================================
@dp.message(Command("start"))
async def telegram_start_cmd_handler(message: types.Message):
    global BOT_USERNAME
    uid = message.from_user.id
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name or ""
    username = message.from_user.username or ""
    
    # Store bot identity runtime lookup string fallback values tags implicitly
    if message.bot and hasattr(message.bot, "username") and message.bot.username:
        BOT_USERNAME = message.bot.username
        
    # Read optional deep-linking reference argument parameter metadata from injection arguments patterns strings execution context arrays
    parts = message.text.split()
    referred_by_id = None
    if len(parts) > 1:
        try:
            referred_by_id = int(parts[1])
            if referred_by_id == uid: referred_by_id = None
        except ValueError: pass

    if db is not None:
        user = await db.users.find_one({"user_id": uid})
        if not user:
            # Create user entity schema structures mappings database definitions objects vectors frames context arrays fields maps parameters
            user = {
                "user_id": uid,
                "first_name": first_name,
                "last_name": last_name,
                "username": username,
                "coins": 0.0,
                "referred_by": referred_by_id,
                "completed_tasks": [],
                "tasks": {
                    "last_date": str(datetime.date.today()),
                    "ads": 0,
                    "ads_claimed": False,
                    "reviews": 0,
                    "reviews_claimed": False
                },
                "joined_at": time.time()
            }
            await db.users.insert_one(user)
            
            if referred_by_id:
                bonus = cached_config.get("refer_bonus", 10)
                await db.users.update_one({"user_id": referred_by_id}, {"$inc": {"coins": bonus}})
                try:
                    await message.bot.send_message(referred_by_id, f"🎉 New Node Peer Connected! You received +{bonus} Coins from referral validation parameters.")
                except Exception: pass

    # Setup inline webapp execution launcher triggers parameters components keys
    builder = InlineKeyboardBuilder()
    if APP_URL:
        builder.button(text="🚀 Launch Premium Rewards Center App", web_app=types.WebAppInfo(url=APP_URL))
    builder.adjust(1)
    
    welcome_text = (
        f"Hello, {first_name}! 👋\n\n"
        f"Welcome to the Premium Rewards Platform Center Mini Ecosystem Gateway Interface.\n\n"
        f"Complete tasks, stream verified ads metrics payload channels daily loops, perform feedback validations, and swap coins balance directly out into liquid digital currency assets securely instantly!"
    )
    await message.answer(welcome_text, reply_markup=builder.as_markup())

# ==========================================
# 12. Main Application Startup
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
    if bot:
        # Extract bot username configuration directly on runtime initialize checks structures maps properties variables hooks tags boundaries loops
        try:
            b_info = await bot.get_me()
            global BOT_USERNAME
            BOT_USERNAME = b_info.username
            print(f"Bot Authenticated Context Verified Name String: @{BOT_USERNAME}")
        except Exception as e:
            print(f"Bot verification network warning handle: {e}")
            
        asyncio.create_task(dp.start_polling(bot, skip_updates=True))
        
    print(f"FastAPI Serving Central Control Matrix Pipeline running active at port instance address bounds http://0.0.0.0:{port}")
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(start())
    except (KeyboardInterrupt, SystemExit):
        print("System core processing module connection terminated successfully gracefully.")
