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
ADMINS = set()
BANNED_USERS = set()

app = FastAPI(title="Movie Box Web App API")
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

# ==========================================
# 2. Database & Models
# ==========================================
async def init_db():
    global db
    client = AsyncIOMotorClient(MONGO_URL)
    db = client.get_default_database()
    print("MongoDB Connected Successfully.")

async def load_admins():
    global ADMINS
    ADMINS.add(OWNER_ID)
    async for admin in db.admins.find():
        ADMINS.add(admin["user_id"])

async def load_banned_users():
    global BANNED_USERS
    async for user in db.banned.find():
        BANNED_USERS.add(user["user_id"])

class MovieData(BaseModel):
    title: str
    caption: str
    file_id: str
    category: str = "All"
    is_upcoming: bool = False
    release_date: str = ""
    language: str = "Bangla"
    poster_url: str = "https://placehold.co/600x400/1a1a1a/fff?text=No+Poster"

class ClaimData(BaseModel):
    uid: int
    task_type: str

# ==========================================
# 3. HTML/UI Interface (All Features Combined)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def index_page():
    return """
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Movie Box</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <link href="https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@400;600;700&family=Poppins:wght@400;600;700&display=swap" rel="stylesheet">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            :root {
                --primary: #ff0055;
                --primary-glow: #ff005588;
                --bg: #0a0512;
                --card-bg: rgba(255, 255, 255, 0.03);
                --border: rgba(255, 255, 255, 0.08);
                --text: #ffffff;
                --text-muted: #b3b3b3;
            }

            * {
                box-sizing: border-box;
                margin: 0;
                padding: 0;
                user-select: none;
                -webkit-tap-highlight-color: transparent;
            }

            body {
                font-family: 'Poppins', 'Hind Siliguri', sans-serif;
                background-color: var(--bg);
                color: var(--text);
                overflow-x: hidden;
                padding-bottom: 90px;
                background-image: radial-gradient(circle at 50% 0%, #1e0b36 0%, var(--bg) 70%);
            }

            /* 1. Welcome Animation Screen */
            #welcome-screen {
                position: fixed;
                top: 0; left: 0; width: 100%; height: 100%;
                background: var(--bg);
                z-index: 9999;
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                transition: opacity 0.5s ease-out, transform 0.5s ease-out;
            }
            .welcome-logo {
                font-size: 2.5rem;
                font-weight: 700;
                color: #fff;
                text-shadow: 0 0 20px var(--primary-glow);
                animation: pulse 1.5s infinite alternate;
                margin-bottom: 15px;
            }
            .welcome-text {
                font-family: 'Hind Siliguri', sans-serif;
                font-size: 1.2rem;
                color: var(--text-muted);
                letter-spacing: 1px;
            }
            @keyframes pulse {
                0% { transform: scale(0.95); text-shadow: 0 0 10px var(--primary-glow); }
                100% { transform: scale(1.05); text-shadow: 0 0 30px var(--primary); }
            }

            /* 2. Clean Header Design */
            header {
                height: 60px;
                display: flex;
                align-items: center;
                justify-content: center;
                border-bottom: 1px solid var(--border);
                backdrop-filter: blur(20px);
                position: sticky;
                top: 0;
                z-index: 100;
                background: rgba(10, 5, 18, 0.6);
            }
            .header-title {
                font-size: 1.5rem;
                font-weight: 700;
                letter-spacing: 1.5px;
                color: #fff;
                text-shadow: 0 0 10px var(--primary-glow);
                cursor: pointer;
                text-decoration: none;
            }

            /* Container */
            .container {
                padding: 15px;
            }

            /* 3. Search & Category System */
            .search-box {
                display: flex;
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid var(--border);
                border-radius: 12px;
                padding: 12px;
                margin-bottom: 20px;
                align-items: center;
                box-shadow: inset 0 1px 1px rgba(255,255,255,0.05);
            }
            .search-box i { color: var(--text-muted); margin-right: 10px; }
            .search-box input {
                background: none; border: none; color: #fff; width: 100%; outline: none; font-size: 1rem;
            }

            .categories {
                display: flex;
                overflow-x: auto;
                gap: 10px;
                margin-bottom: 20px;
                padding-bottom: 5px;
            }
            .categories::-webkit-scrollbar { display: none; }
            .category-btn {
                background: var(--card-bg);
                border: 1px solid var(--border);
                color: var(--text-muted);
                padding: 8px 16px;
                border-radius: 20px;
                white-space: nowrap;
                font-size: 0.9rem;
                cursor: pointer;
                transition: all 0.3s;
            }
            .category-btn.active, .category-btn:hover {
                background: var(--primary);
                color: #fff;
                border-color: var(--primary);
                box-shadow: 0 0 15px var(--primary-glow);
            }

            /* Filter Bar for Upcoming */
            .filter-bar {
                display: none;
                gap: 10px;
                margin-bottom: 15px;
            }

            /* 5. Movie Card Design (Single Row Style Layout) */
            .section-title {
                font-size: 1.2rem;
                margin-bottom: 15px;
                font-weight: 600;
                display: flex;
                align-items: center;
                justify-content: space-between;
            }
            .movie-grid {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
                gap: 15px;
            }
            .movie-card {
                background: var(--card-bg);
                border: 1px solid var(--border);
                border-radius: 14px;
                overflow: hidden;
                cursor: pointer;
                transition: all 0.4s cubic-bezier(0.165, 0.84, 0.44, 1);
                position: relative;
            }
            .movie-card:hover {
                transform: translateY(-5px);
                border-color: var(--primary);
                box-shadow: 0 5px 20px rgba(255, 0, 85, 0.2);
            }
            .poster-wrapper {
                position: relative;
                width: 100%;
                padding-top: 145%;
                background: #15101e;
            }
            .movie-poster {
                position: absolute;
                top: 0; left: 0; width: 100%; height: 100%;
                object-fit: cover;
                transition: transform 0.4s;
            }
            .movie-card:hover .movie-poster {
                transform: scale(1.05);
            }
            .movie-info {
                padding: 8px;
            }
            .movie-title {
                font-size: 0.85rem;
                font-weight: 600;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
                color: #eaeaea;
            }
            .movie-meta {
                font-size: 0.75rem;
                color: var(--text-muted);
                margin-top: 2px;
            }

            /* 4. 18+ Content Protection System & 6. Modal Popup UI */
            .modal {
                position: fixed;
                top: 0; left: 0; width: 100%; height: 100%;
                background: rgba(5, 2, 10, 0.8);
                backdrop-filter: blur(20px);
                z-index: 2000;
                display: flex;
                align-items: center;
                justify-content: center;
                opacity: 0; pointer-events: none;
                transition: opacity 0.3s ease;
                padding: 20px;
            }
            .modal.active { opacity: 1; pointer-events: auto; }
            .modal-content {
                background: rgba(25, 15, 40, 0.75);
                border: 1px solid var(--border);
                border-radius: 24px;
                width: 100%;
                max-width: 400px;
                padding: 25px;
                box-shadow: 0 10px 40px rgba(0,0,0,0.5), inset 0 1px 1px rgba(255,255,255,0.1);
                text-align: center;
                transform: scale(0.9);
                transition: transform 0.3s ease;
            }
            .modal.active .modal-content { transform: scale(1); }

            .btn {
                background: var(--primary);
                color: #fff;
                border: none;
                padding: 12px 24px;
                border-radius: 12px;
                font-size: 1rem;
                font-weight: 600;
                cursor: pointer;
                width: 100%;
                margin-top: 15px;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 8px;
                box-shadow: 0 4px 15px var(--primary-glow);
                transition: all 0.3s;
            }
            .btn:active { transform: scale(0.98); }
            .btn-secondary {
                background: rgba(255,255,255,0.08);
                box-shadow: none;
                border: 1px solid var(--border);
            }

            /* 8. Bottom Navigation Bar */
            .bottom-nav {
                position: fixed;
                bottom: 0; left: 0; width: 100%;
                height: 70px;
                background: rgba(15, 8, 25, 0.7);
                backdrop-filter: blur(25px);
                border-top: 1px solid var(--border);
                display: flex;
                justify-content: space-around;
                align-items: center;
                z-index: 1000;
            }
            .nav-item {
                display: flex;
                flex-direction: column;
                align-items: center;
                color: var(--text-muted);
                text-decoration: none;
                font-size: 0.75rem;
                gap: 5px;
                cursor: pointer;
                transition: color 0.3s;
                width: 20%;
            }
            .nav-item i { font-size: 1.25rem; transition: transform 0.3s; }
            .nav-item.active { color: var(--primary); }
            .nav-item.active i { transform: translateY(-2px); text-shadow: 0 0 10px var(--primary-glow); }

            /* 11. Profile Section UI */
            .profile-card {
                background: var(--card-bg);
                border: 1px solid var(--border);
                border-radius: 20px;
                padding: 20px;
                text-align: center;
                margin-bottom: 20px;
            }
            .profile-avatar {
                width: 80px; height: 80px; border-radius: 50%;
                background: linear-gradient(45deg, var(--primary), #8800ff);
                margin: 0 auto 15px; display: flex; align-items: center; justify-content: center;
                font-size: 2rem; font-weight: bold; box-shadow: 0 0 20px rgba(136,0,255,0.4);
            }
            .social-grid {
                display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 15px;
            }
            .social-btn {
                padding: 10px; border-radius: 10px; border: 1px solid var(--border);
                color: #fff; text-decoration: none; font-size: 0.85rem; display: flex;
                align-items: center; justify-content: center; gap: 8px; background: rgba(255,255,255,0.02);
            }

            /* Dynamic Pages View Status */
            .page { display: none; }
            .page.active { display: block; }
        </style>
    </head>
    <body>

        <div id="welcome-screen">
            <div class="welcome-logo">Movie Box</div>
            <div class="welcome-text">মুভি বক্স জগতে স্বাগতম</div>
        </div>

        <header>
            <div class="header-title" onclick="switchPage('home')">Movie Box</div>
        </header>

        <div class="container">
            
            <div id="page-home" class="page active">
                <div class="search-box">
                    <i class="fa-solid fa-magnifying-glass"></i>
                    <input type="text" id="search-input" placeholder="মুভি খুঁজুন..." oninput="searchMovies()">
                </div>

                <div class="categories" id="category-bar">
                    <div class="category-btn active" onclick="filterCategory('All', this)">All</div>
                    <div class="category-btn" onclick="filterCategory('Bangla', this)">Bangla</div>
                    <div class="category-btn" onclick="filterCategory('Bengali Dubbed', this)">Bengali Dubbed</div>
                    <div class="category-btn" onclick="filterCategory('Hindi', this)">Hindi</div>
                    <div class="category-btn" onclick="filterCategory('Hindi Dubbed', this)">Hindi Dubbed</div>
                    <div class="category-btn" onclick="filterCategory('English', this)">English</div>
                    <div class="category-btn" onclick="filterCategory('Web Series', this)">Web Series</div>
                    <div class="category-btn" onclick="filterCategory('Korean', this)">Korean</div>
                    <div class="category-btn" onclick="filterCategory('Anime', this)">Anime</div>
                    <div class="category-btn" onclick="filterCategory('18+', this)">18+</div>
                </div>

                <div class="section-title"><span id="section-title-text">সব মুভি</span></div>
                <div class="movie-grid" id="movie-container"></div>
            </div>

            <div id="page-search" class="page">
                <div class="search-box">
                    <i class="fa-solid fa-magnifying-glass"></i>
                    <input type="text" id="global-search-input" placeholder="সার্চ ইঞ্জিন..." oninput="globalSearch()">
                </div>
                <div class="movie-grid" id="search-container"></div>
            </div>

            <div id="page-favorites" class="page">
                <div class="section-title">আমার ফেভারিটস</div>
                <div class="movie-grid" id="favorites-container"></div>
            </div>

            <div id="page-upcoming" class="page">
                <div class="section-title">আসন্ন মুভি সমূহ (Upcoming)</div>
                <div class="categories">
                    <div class="category-btn active" onclick="filterUpcomingLang('All', this)">All</div>
                    <div class="category-btn" onclick="filterUpcomingLang('Bangla', this)">Bangla</div>
                    <div class="category-btn" onclick="filterUpcomingLang('Hindi', this)">Hindi</div>
                    <div class="category-btn" onclick="filterUpcomingLang('English', this)">English</div>
                </div>
                <div class="movie-grid" id="upcoming-container"></div>
            </div>

            <div id="page-profile" class="page">
                <div class="profile-card">
                    <div class="profile-avatar" id="prof-avatar">M</div>
                    <h3 id="prof-name">User Profile</h3>
                    <p style="color: var(--text-muted); font-size: 0.85rem; margin-top:5px;">মুভি বক্স মেম্বার</p>
                </div>
                <div class="section-title">কমিউনিটি ও সোশ্যাল লিঙ্ক</div>
                <div class="social-grid">
                    <a href="https://t.me/your_channel" target="_blank" class="social-btn" id="link-tg"><i class="fa-brands fa-telegram" style="color:#26a5e4;"></i> Telegram Channel</a>
                    <a href="#" target="_blank" class="social-btn" id="link-fb"><i class="fa-brands fa-facebook" style="color:#1877f2;"></i> Facebook Page</a>
                    <a href="#" target="_blank" class="social-btn" id="link-yt"><i class="fa-brands fa-youtube" style="color:#ff0000;"></i> YouTube Channel</a>
                    <a href="#" target="_blank" class="social-btn" id="link-web"><i class="fa-solid fa-globe" style="color:#00ffcc;"></i> Website</a>
                </div>
                <div class="profile-card" style="margin-top: 15px; text-align: left;">
                    <h4>About Me</h4>
                    <p style="color:var(--text-muted); font-size:0.9rem; margin-top:8px;" id="about-text">স্বাগতম মুভি বক্সে। এখানে আপনি পাবেন লেটেস্ট সব প্রিমিয়াম মুভি কালেকশন।</p>
                </div>
            </div>

        </div>

        <div class="modal" id="modal-nsfw">
            <div class="modal-content">
                <i class="fa-solid fa-triangle-exclamation" style="font-size: 3rem; color: var(--primary); margin-bottom: 15px;"></i>
                <h2>Adult Warning!</h2>
                <p style="color: var(--text-muted); margin: 10px 0 20px; font-size: 0.95rem;">আপনার বয়স কি ১৮ বছরের বেশি? এই সেকশনে অ্যাডাল্ট কন্টেন্ট রয়েছে।</p>
                <button class="btn" onclick="verifyNsfw(true)">হ্যাঁ, আমার বয়স ১৮+</button>
                <button class="btn btn-secondary" onclick="verifyNsfw(false)">না, ফিরে যান</button>
            </div>
        </div>

        <div class="modal" id="modal-details">
            <div class="modal-content" style="text-align: left;">
                <div style="position: relative; border-radius: 14px; overflow:hidden; margin-bottom: 15px;">
                    <img id="modal-movie-poster" src="" style="width:100%; max-height:220px; object-fit:cover;">
                </div>
                <h3 id="modal-movie-title" style="margin-bottom: 5px;">Movie Title</h3>
                <p id="modal-movie-lang" style="font-size: 0.8rem; color: var(--primary); margin-bottom: 10px;">Language</p>
                <p id="modal-movie-desc" style="color: var(--text-muted); font-size: 0.9rem; max-height: 100px; overflow-y: auto; margin-bottom: 20px;">Caption/Description</p>
                
                <div style="display:flex; gap:10px;">
                    <button class="btn btn-secondary" id="fav-toggle-btn" style="width:50px; margin-top:0;" onclick="toggleFavoriteCurrent()"><i class="fa-regular fa-heart"></i></button>
                    <button class="btn" id="download-trigger-btn" style="margin-top:0; flex-grow:1;" onclick="startDownloadProcess()"><i class="fa-solid fa-download"></i> ডাউনলোড করুন</button>
                </div>

                <div id="timer-wrapper" style="display:none; margin-top:15px; text-align:center; background:rgba(0,0,0,0.2); padding:10px; border-radius:10px; border:1px solid var(--border);">
                    <p style="font-size:0.9rem;" id="timer-text">Please wait 15 seconds to unlock download</p>
                </div>
                
                <button class="btn btn-secondary" style="margin-top:15px; width:100%;" onclick="closeModal('modal-details')">বন্ধ করুন</button>
            </div>
        </div>

        <div class="bottom-nav">
            <div class="nav-item active" id="nav-home" onclick="switchPage('home')">
                <i class="fa-solid fa-house"></i>Home
            </div>
            <div class="nav-item" id="nav-search" onclick="switchPage('search')">
                <i class="fa-solid fa-magnifying-glass"></i>Search
            </div>
            <div class="nav-item" id="nav-favorites" onclick="switchPage('favorites')">
                <i class="fa-solid fa-heart"></i>Favorites
            </div>
            <div class="nav-item" id="nav-upcoming" onclick="switchPage('upcoming')">
                <i class="fa-solid fa-calendar-days"></i>Upcoming
            </div>
            <div class="nav-item" id="nav-profile" onclick="switchPage('profile')">
                <i class="fa-solid fa-user"></i>Profile
            </div>
        </div>

        <script>
            let tg = window.Telegram.WebApp;
            tg.expand();
            
            let allMovies = [];
            let favorites = JSON.parse(localStorage.getItem('mb_favs')) || [];
            let currentSelectedMovie = null;
            let currentCategory = 'All';
            let currentUpcomingLang = 'All';

            // Hide Welcome Screen
            window.addEventListener('DOMContentLoaded', () => {
                setTimeout(() => {
                    const ws = document.getElementById('welcome-screen');
                    ws.style.opacity = '0';
                    ws.style.transform = 'scale(1.1)';
                    setTimeout(() => ws.style.display = 'none', 500);
                }, 2000);
                
                // Load User info if available via TG
                if(tg.initDataUnsafe && tg.initDataUnsafe.user) {
                    document.getElementById('prof-name').innerText = tg.initDataUnsafe.user.first_name;
                    document.getElementById('prof-avatar').innerText = tg.initDataUnsafe.user.first_name[0].toUpperCase();
                }
                
                fetchMovies();
            });

            async function fetchMovies() {
                try {
                    let r = await fetch('/api/movies');
                    allMovies = await r.json();
                    renderHome();
                    renderUpcoming();
                } catch(e) { console.error("Error loading movies", e); }
            }

            function renderHome() {
                let container = document.getElementById('movie-container');
                container.innerHTML = '';
                let filtered = allMovies.filter(m => !m.is_upcoming);
                
                if(currentCategory !== 'All') {
                    filtered = filtered.filter(m => m.category === currentCategory);
                }
                
                if(filtered.length === 0) {
                    container.innerHTML = '<p style="grid-column:1/-1; text-align:center; color:var(--text-muted);">কোনো মুভি পাওয়া যায়নি।</p>';
                    return;
                }

                filtered.forEach(m => {
                    container.appendChild(createMovieCard(m));
                });
            }

            function renderUpcoming() {
                let container = document.getElementById('upcoming-container');
                container.innerHTML = '';
                let filtered = allMovies.filter(m => m.is_upcoming);
                
                if(currentUpcomingLang !== 'All') {
                    filtered = filtered.filter(m => m.language === currentUpcomingLang);
                }

                if(filtered.length === 0) {
                    container.innerHTML = '<p style="grid-column:1/-1; text-align:center; color:var(--text-muted);">কোনো আপকামিং মুভি নেই।</p>';
                    return;
                }

                filtered.forEach(m => {
                    container.appendChild(createMovieCard(m, true));
                });
            }

            function createMovieCard(m, isUpcoming=false) {
                let card = document.createElement('div');
                card.className = 'movie-card';
                card.onclick = () => openMovieDetails(m);
                
                card.innerHTML = `
                    <div class="poster-wrapper">
                        <img class="movie-poster" src="${m.poster_url || 'https://placehold.co/600x400/1a1a1a/fff?text=Movie+Box'}" alt="">
                    </div>
                    <div class="movie-info">
                        <div class="movie-title">${m.title}</div>
                        <div class="movie-meta">${isUpcoming ? (m.release_date || 'Coming Soon') : (m.language || 'Bangla')}</div>
                    </div>
                `;
                return card;
            }

            function filterCategory(cat, btn) {
                if(cat === '18+') {
                    document.getElementById('modal-nsfw').classList.add('active');
                    window.pendingCategoryBtn = btn;
                    return;
                }
                executeCategoryChange(cat, btn);
            }

            function executeCategoryChange(cat, btn) {
                currentCategory = cat;
                document.querySelectorAll('#category-bar .category-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                document.getElementById('section-title-text').innerText = cat === 'All' ? 'সব মুভি' : cat;
                renderHome();
            }

            function verifyNsfw(status) {
                document.getElementById('modal-nsfw').classList.remove('active');
                if(status) {
                    executeCategoryChange('18+', window.pendingCategoryBtn);
                } else {
                    executeCategoryChange('All', document.querySelectorAll('#category-bar .category-btn')[0]);
                }
            }

            function filterUpcomingLang(lang, btn) {
                currentUpcomingLang = lang;
                btn.parentNode.querySelectorAll('.category-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                renderUpcoming();
            }

            // 6. Movie Details Modal Popup UI Engine
            function openMovieDetails(m) {
                currentSelectedMovie = m;
                document.getElementById('modal-movie-title').innerText = m.title;
                document.getElementById('modal-movie-lang').innerText = m.category + " | " + (m.language || "");
                document.getElementById('modal-movie-desc').innerText = m.caption || "কোনো বিবরণী নেই।";
                document.getElementById('modal-movie-poster').src = m.poster_url || "https://placehold.co/600x400/1a1a1a/fff?text=Movie+Box";
                
                let isFav = favorites.includes(m._id);
                let favIcon = document.getElementById('fav-toggle-btn').querySelector('i');
                if(isFav) {
                    favIcon.className = "fa-solid fa-heart";
                    favIcon.style.color = "var(--primary)";
                } else {
                    favIcon.className = "fa-regular fa-heart";
                    favIcon.style.color = "#fff";
                }

                document.getElementById('timer-wrapper').style.display = 'none';
                document.getElementById('download-trigger-btn').style.display = 'inline-flex';
                
                document.getElementById('modal-details').classList.add('active');
            }

            function closeModal(id) {
                document.getElementById(id).classList.remove('active');
            }

            // 7. Download Protection & adsterra Ads Timer System
            function startDownloadProcess() {
                if(!currentSelectedMovie) return;
                
                // Open Adsterra or Ad provider page link safely
                window.open("https://www.highratecpm.com/your-adsterra-direct-link", "_blank");
                
                let dlBtn = document.getElementById('download-trigger-btn');
                let tw = document.getElementById('timer-wrapper');
                let tt = document.getElementById('timer-text');
                
                dlBtn.style.display = 'none';
                tw.style.display = 'block';
                
                let timeLeft = 15;
                tt.innerText = `Please wait ${timeLeft} seconds to unlock download`;
                
                let timer = setInterval(() => {
                    timeLeft--;
                    if(timeLeft <= 0) {
                        clearInterval(timer);
                        tw.style.display = 'none';
                        dlBtn.style.display = 'inline-flex';
                        
                        // Deep link redirect to telegram to forward/get file securely
                        if(tg.initDataUnsafe && tg.initDataUnsafe.user) {
                            window.location.href = `https://t.me/${tg.bot_username}?start=file_${currentSelectedMovie._id}`;
                        } else {
                            alert("ফাইলটি প্রসেস করা হয়েছে! টেলিগ্রাম বট অপশনে ফাইলটি চেক করুন।");
                        }
                    } else {
                        tt.innerText = `Please wait ${timeLeft} seconds to unlock download`;
                    }
                }, 1000);
            }

            // 9. Favorites System Architecture
            function toggleFavoriteCurrent() {
                if(!currentSelectedMovie) return;
                let id = currentSelectedMovie._id;
                if(favorites.includes(id)) {
                    favorites = favorites.filter(f => f !== id);
                } else {
                    favorites.push(id);
                }
                localStorage.setItem('mb_favs', JSON.stringify(favorites));
                openMovieDetails(currentSelectedMovie); // Refresh UI
                renderFavorites();
            }

            function renderFavorites() {
                let container = document.getElementById('favorites-container');
                container.innerHTML = '';
                let favList = allMovies.filter(m => favorites.includes(m._id));
                if(favList.length === 0) {
                    container.innerHTML = '<p style="text-align:center; color:var(--text-muted); grid-column:1/-1;">ফেভারিট লিস্ট ফাকা!</p>';
                    return;
                }
                favList.forEach(m => {
                    container.appendChild(createMovieCard(m));
                });
            }

            // 8. Bottom Navigation & Navigation Engine
            function switchPage(pageId) {
                document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
                
                document.getElementById(`page-${pageId}`).classList.add('active');
                document.getElementById(`nav-${pageId}`).classList.add('active');
                
                if(pageId === 'favorites') renderFavorites();
            }

            function searchMovies() {
                let val = document.getElementById('search-input').value.toLowerCase();
                let container = document.getElementById('movie-container');
                container.innerHTML = '';
                let filtered = allMovies.filter(m => !m.is_upcoming && m.title.toLowerCase().includes(val));
                filtered.forEach(m => container.appendChild(createMovieCard(m)));
            }

            function globalSearch() {
                let val = document.getElementById('global-search-input').value.toLowerCase();
                let container = document.getElementById('search-container');
                container.innerHTML = '';
                let filtered = allMovies.filter(m => m.title.toLowerCase().includes(val));
                filtered.forEach(m => container.appendChild(createMovieCard(m)));
            }
        </script>
    </body>
    </html>
    """

# ==========================================
# 4. REST API Endpoint Providers
# ==========================================
@app.get("/api/movies")
async def get_movies_api():
    movies = []
    async for movie in db.movies.find():
        movie["_id"] = str(movie["_id"])
        movies.append(movie)
    return movies

# ==========================================
# 5. Admin Panel (Protected Core Component)
# ==========================================
@app.post("/admin/add-movie")
async def add_movie(data: MovieData, credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != "admin" or credentials.password != ADMIN_PASS:
        raise HTTPException(status_code=status.HTTP_41__UNAUTHORIZED, detail="Invalid Credentials")
    
    res = await db.movies.insert_one(data.dict())
    return {"ok": True, "id": str(res.inserted_id)}

@app.delete("/admin/delete-movie/{movie_id}")
async def delete_movie(movie_id: str, credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != "admin" or credentials.password != ADMIN_PASS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Credentials")
    
    await db.movies.delete_one({"_id": ObjectId(movie_id)})
    return {"ok": True}

# ==========================================
# 6. Telegram Bot Command Core Layer
# ==========================================
@dp.message(Command("start"))
async def start_cmd(msg: types.Message):
    uid = msg.from_user.id
    if uid in BANNED_USERS:
        return await msg.answer("দুঃখিত, আপনাকে এই বট থেকে ব্যান করা হয়েছে।")
        
    await db.users.update_one(
        {"user_id": uid},
        {"$set": {"username": msg.from_user.username, "last_seen": datetime.datetime.now()}},
        upsert=True
    )
    
    args = msg.text.split()
    if len(args) > 1 and args[1].startswith("file_"):
        movie_id = args[1].replace("file_", "")
        movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
        if movie:
            return await bot.send_document(
                chat_id=msg.chat.id,
                document=movie["file_id"],
                caption=f"🎬 <b>{movie['title']}</b>\n\n{movie['caption']}\n\nDownloaded via Movie Box App.",
                parse_mode="HTML"
            )

    builder = InlineKeyboardBuilder()
    builder.button(text="🎬 Open Movie Box Web App", web_app=types.WebAppInfo(url=APP_URL))
    builder.adjust(1)
    
    await msg.answer(
        f"👋 স্বাগতম <b>{msg.from_user.first_name}</b>!\n\nমুভি বক্স জগতে আপনাকে স্বাগতম। অ্যাপ ওপেন করে সরাসরি প্রিমিয়াম মুভি স্ট্রিম বা ডাউনলোড করতে নিচের বাটনে ক্লিক করুন।",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

# ==========================================
# 7. Background System Worker Setup
# ==========================================
async def auto_delete_worker():
    while True:
        await asyncio.sleep(60)

# ==========================================
# 8. Main Application Startup Engine
# ==========================================
async def start():
    print("Initializing Database & Core Systems...")
    await init_db()
    await load_admins()
    await load_banned_users()
    
    global BOT_USERNAME
    if bot:
        bot_info = await bot.get_me()
        BOT_USERNAME = bot_info.username
        print(f"Connected to Telegram Bot: @{BOT_USERNAME}")
        asyncio.create_task(dp.start_polling(bot))
    
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    
    print("Starting Auto Background Workers...")
    asyncio.create_task(auto_delete_worker())
    
    print(f"Starting Web Server UI on Port {port}...")
    await server.serve()

if __name__ == "__main__":
    asyncio.run(start())
