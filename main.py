import os, asyncio, datetime, uvicorn
import aiohttp
from fastapi import FastAPI, Body
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from pydantic import BaseModel

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))
APP_URL = os.getenv("APP_URL")

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

client = AsyncIOMotorClient(MONGO_URL)
db = client["movie_database"]

admin_temp = {}
admin_cache = set([OWNER_ID])

# =========================
# LOAD ADMINS
# =========================

async def load_admins():
    admin_cache.clear()
    admin_cache.add(OWNER_ID)

    async for admin in db.admins.find():
        admin_cache.add(admin["user_id"])

# =========================
# START
# =========================

@dp.message(Command("start"))
async def start_cmd(message: types.Message):

    await db.users.update_one(
        {"user_id": message.from_user.id},
        {"$set": {"first_name": message.from_user.first_name}},
        upsert=True
    )

    kb = [[
        types.InlineKeyboardButton(
            text="🎬 OPEN MOVIE APP",
            web_app=types.WebAppInfo(url=APP_URL)
        )
    ]]

    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)

    text = """
🎬 Movie Upload Bot

ভিডিও / ডকুমেন্ট পাঠান
তারপর পোস্টার দিন
তারপর ক্যাটাগরি সিলেক্ট করুন
তারপর নাম দিন
"""

    await message.answer(text, reply_markup=markup)

# =========================
# UPLOAD
# =========================

@dp.callback_query(F.data.startswith("cat_"))
async def select_category(c: types.CallbackQuery):

    uid = c.from_user.id

    if uid not in admin_cache:
        return

    if admin_temp.get(uid, {}).get("step") != "category":
        return

    category = c.data.replace("cat_", "")

    admin_temp[uid]["category"] = category
    admin_temp[uid]["step"] = "title"

    await c.message.edit_text(
        f"✅ Category Selected: {category}\n\nএখন মুভির নাম দিন"
    )

    await c.answer()

# =========================
# MESSAGE HANDLER
# =========================

@dp.message(F.content_type.in_({
    'text',
    'photo',
    'video',
    'document'
}))
async def catch_all_inputs(m: types.Message):

    uid = m.from_user.id

    # =========================
    # VIDEO
    # =========================

    if uid in admin_cache and (m.video or m.document):

        fid = m.video.file_id if m.video else m.document.file_id
        ftype = "video" if m.video else "document"

        admin_temp[uid] = {
            "step": "photo",
            "file_id": fid,
            "type": ftype
        }

        await m.answer(
            "✅ ভিডিও পেয়েছি\n\nএখন পোস্টার দিন"
        )

        return

    # =========================
    # PHOTO
    # =========================

    if uid in admin_cache and m.photo and admin_temp.get(uid, {}).get("step") == "photo":

        admin_temp[uid]["photo_id"] = m.photo[-1].file_id

        admin_temp[uid]["step"] = "category"

        category_buttons = [
            ["HOME", "ADULT CONTENT", "BANGLA"],
            ["HINDI DUBBED", "WEB SERIES", "K DRAMA"],
            ["BANGLA DUBBED", "HINDI", "ENGLISH", "WWE"],
            ["HORROR"]
        ]

        kb = InlineKeyboardBuilder()

        for row in category_buttons:
            for cat in row:
                kb.button(
                    text=cat,
                    callback_data=f"cat_{cat}"
                )

        kb.adjust(3,3,4,1)

        await m.answer(
            "✅ পোস্টার পেয়েছি\n\nক্যাটাগরি সিলেক্ট করুন",
            reply_markup=kb.as_markup()
        )

        return

    # =========================
    # TITLE
    # =========================

    if uid in admin_cache and m.text and not str(m.text).startswith("/"):

        if admin_temp.get(uid, {}).get("step") == "title":

            title = m.text.strip()

            await db.movies.insert_one({
                "title": title,
                "category": admin_temp[uid]["category"],
                "photo_id": admin_temp[uid]["photo_id"],
                "file_id": admin_temp[uid]["file_id"],
                "file_type": admin_temp[uid]["type"],
                "clicks": 0,
                "created_at": datetime.datetime.utcnow()
            })

            del admin_temp[uid]

            await m.answer(
                f"🎉 {title} Added Successfully"
            )

# =========================
# WEB UI
# =========================

@app.get("/", response_class=HTMLResponse)
async def web_ui():

    html_code = r"""
<!DOCTYPE html>
<html lang="en">

<head>

<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">

<title>Movie App</title>

<link rel="stylesheet"
href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">

<style>

*{
margin:0;
padding:0;
box-sizing:border-box;
}

body{
background:#050816;
font-family:sans-serif;
color:#fff;
}

header{
padding:15px;
text-align:center;
font-size:22px;
font-weight:bold;
background:#0f172a;
border-bottom:1px solid #1e293b;
letter-spacing: 1px;
}

.category-wrapper{
padding:12px 10px;
width: 100%;
overflow-x: auto;
-webkit-overflow-scrolling: touch;
}

/* হাইড স্ক্রলবার */
.category-wrapper::-webkit-scrollbar {
display: none;
}

.category-scroll{
display:flex;
gap:8px;
width: max-content;
padding-bottom: 2px;
}

.cat-btn{
border:none;
outline:none;
cursor:pointer;
padding:8px 14px;
border-radius:20px;
background:#111827;
color:#ccc;
font-size:13px;
font-weight:600;
border:1px solid rgba(255, 91, 0, 0.4);
box-shadow: 0 0 3px rgba(255, 91, 0, 0.2);
transition:.2s ease-in-out;
white-space:nowrap;
}

.cat-btn:hover{
color: #fff;
border-color: #ff5b00;
}

.cat-btn.active{
background:linear-gradient(45deg,#ff5b00,#ff7300);
color: #fff;
font-weight: 700;
border-color: #ff7300;
box-shadow: 0 0 8px #ff5b00;
}

.search-box{
padding:10px 15px;
}

.search-input{
width:100%;
padding:12px 20px;
border:none;
outline:none;
border-radius:30px;
background:#111827;
color:#fff;
font-size:15px;
border: 1px solid #1e293b;
}

.search-input:focus{
border-color: #ff5b00;
}

.grid{
padding:15px;
display:grid;
grid-template-columns:repeat(2,1fr);
gap:15px;
}

.card{
background:#111827;
border-radius:12px;
overflow:hidden;
border: 1px solid #1e293b;
}

.card img{
width:100%;
height:200px;
object-fit:cover;
}

.card-footer{
padding:10px;
text-align:center;
font-weight:600;
font-size:13px;
color: #e2e8f0;
}

.view{
font-size:11px;
opacity:.7;
margin-top:5px;
color: #94a3b8;
}

@media(max-width:500px){
.card img{
height:170px;
}
.grid {
gap: 10px;
padding: 10px;
}
}

</style>

</head>

<body>

<header>
🎬 MOVIE APP
</header>

<div class="category-wrapper">

<div class="category-scroll">

<button class="cat-btn active"
onclick="filterCategory('', event)">
HOME
</button>

<button class="cat-btn"
onclick="filterCategory('ADULT CONTENT', event)">
ADULT CONTENT
</button>

<button class="cat-btn"
onclick="filterCategory('BANGLA', event)">
BANGLA
</button>

<button class="cat-btn"
onclick="filterCategory('HINDI DUBBED', event)">
HINDI DUBBED
</button>

<button class="cat-btn"
onclick="filterCategory('WEB SERIES', event)">
WEB SERIES
</button>

<button class="cat-btn"
onclick="filterCategory('K DRAMA', event)">
K DRAMA
</button>

<button class="cat-btn"
onclick="filterCategory('BANGLA DUBBED', event)">
BANGLA DUBBED
</button>

<button class="cat-btn"
onclick="filterCategory('HINDI', event)">
HINDI
</button>

<button class="cat-btn"
onclick="filterCategory('ENGLISH', event)">
ENGLISH
</button>

<button class="cat-btn"
onclick="filterCategory('WWE', event)">
WWE
</button>

<button class="cat-btn"
onclick="filterCategory('HORROR', event)">
HORROR
</button>

</div>
</div>

<div class="search-box">

<input
type="text"
id="searchInput"
class="search-input"
placeholder="Search Movie..."
>

</div>

<div class="grid" id="movieGrid"></div>

<script>

let currentCategory = "";
let searchQuery = "";

async function loadMovies(){

const r = await fetch(
`/api/list?q=${searchQuery}&category=${currentCategory}`
);

const data = await r.json();

const grid = document.getElementById("movieGrid");

if(data.movies.length === 0){

grid.innerHTML = `
<h2 style="padding:20px; text-align:center; width:100%; grid-column: span 2; font-size:16px; color:#64748b;">
No Movies Found
</h2>
`;

return;
}

grid.innerHTML = data.movies.map(m => `

<div class="card">

<img src="/api/image/${m.photo_id}">

<div class="card-footer">

${m.title}

<div class="view">
👁 ${m.clicks}
</div>

</div>

</div>

`).join('');

}

function filterCategory(category, event){

currentCategory = category;

document.querySelectorAll('.cat-btn').forEach(btn=>{
btn.classList.remove('active');
});

event.target.classList.add('active');

loadMovies();

}

document.getElementById("searchInput")
.addEventListener("input", function(e){

searchQuery = e.target.value;

loadMovies();

});

loadMovies();

</script>

</body>
</html>
"""

    return html_code

# =========================
# LIST API
# =========================

@app.get("/api/list")
async def list_movies(
    q: str = "",
    category: str = ""
):

    query = {}

    if q:
        query["title"] = {
            "$regex": q,
            "$options": "i"
        }

    if category and category != "":
        query["category"] = category

    movies = []

    async for m in db.movies.find(query).sort("created_at", -1):

        m["_id"] = str(m["_id"])
        m["clicks"] = m.get("clicks", 0)

        movies.append(m)

    return {
        "movies": movies
    }

# =========================
# IMAGE
# =========================

@app.get("/api/image/{photo_id}")
async def get_image(photo_id: str):

    try:

        file_info = await bot.get_file(photo_id)

        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"

        async def stream_image():

            async with aiohttp.ClientSession() as session:

                async with session.get(file_url) as resp:

                    async for chunk in resp.content.iter_chunked(1024):
                        yield chunk

        return StreamingResponse(
            stream_image(),
            media_type="image/jpeg"
        )

    except:

        return {"error": "not found"}

# =========================
# START SERVER
# =========================

async def start():

    await load_admins()

    port = int(os.getenv("PORT", 8000))

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        loop="asyncio"
    )

    server = uvicorn.Server(config)

    await bot.delete_webhook(drop_pending_updates=True)

    await asyncio.gather(
        server.serve(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(start())
