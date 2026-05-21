from flask import Flask, render_template_string

app = Flask(__name__)

# --- HTML, CSS & JS Template (Single File) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Movie Box</title>
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root {
            --bg-color: #0b0f19;
            --glass-bg: rgba(255, 255, 255, 0.05);
            --glass-border: rgba(255, 255, 255, 0.1);
            --neon-cyan: #00f3ff;
            --neon-pink: #ff00de;
            --text-color: #ffffff;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Poppins', sans-serif;
        }

        body {
            background-color: var(--bg-color);
            color: var(--text-color);
            overflow-x: hidden;
            min-height: 100vh;
        }

        /* Background Neon Blobs */
        .blob {
            position: fixed;
            width: 300px;
            height: 300px;
            border-radius: 50%;
            filter: blur(100px);
            opacity: 0.4;
            z-index: -1;
        }
        .blob-1 { background: var(--neon-cyan); top: -50px; left: -50px; }
        .blob-2 { background: var(--neon-pink); bottom: 100px; right: -50px; }

        /* 1. Welcome Animation */
        #welcome-screen {
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background: var(--bg-color);
            display: flex;
            justify-content: center;
            align-items: center;
            z-index: 9999;
            transition: opacity 0.8s ease, visibility 0.8s ease;
        }
        #welcome-screen.hide {
            opacity: 0;
            visibility: hidden;
        }
        .welcome-text {
            font-size: 2.5rem;
            font-weight: 800;
            text-align: center;
            background: linear-gradient(to right, var(--neon-cyan), var(--neon-pink));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0% { transform: scale(1); opacity: 1; }
            50% { transform: scale(1.05); opacity: 0.8; }
            100% { transform: scale(1); opacity: 1; }
        }

        /* 2. Header */
        #header {
            position: fixed;
            top: 0; left: 0; width: 100%;
            height: 60px;
            background: rgba(11, 15, 25, 0.8);
            backdrop-filter: blur(15px);
            border-bottom: 1px solid var(--glass-border);
            display: flex;
            justify-content: center;
            align-items: center;
            z-index: 1000;
        }
        #header a {
            text-decoration: none;
            font-size: 1.8rem;
            font-weight: 800;
            background: linear-gradient(to right, var(--neon-cyan), var(--neon-pink));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: 2px;
            cursor: pointer;
        }

        /* Pages Container */
        .page {
            display: none;
            padding: 80px 15px 100px;
            max-width: 600px;
            margin: 0 auto;
            animation: fadeIn 0.4s ease-in;
        }
        .page.active { display: block; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

        /* 3. Search & Category */
        .search-box {
            width: 100%;
            padding: 12px 20px;
            border-radius: 25px;
            border: 1px solid var(--glass-border);
            background: var(--glass-bg);
            color: white;
            outline: none;
            margin-bottom: 15px;
            backdrop-filter: blur(10px);
        }
        .categories {
            display: flex;
            gap: 10px;
            overflow-x: auto;
            padding-bottom: 10px;
            scrollbar-width: none;
        }
        .categories::-webkit-scrollbar { display: none; }
        .cat-btn {
            padding: 8px 16px;
            border-radius: 20px;
            border: 1px solid var(--glass-border);
            background: var(--glass-bg);
            color: white;
            cursor: pointer;
            white-space: nowrap;
            transition: 0.3s;
        }
        .cat-btn:hover, .cat-btn.active {
            background: var(--neon-cyan);
            color: black;
            box-shadow: 0 0 15px var(--neon-cyan);
            border-color: var(--neon-cyan);
        }

        /* 5. Movie Card Design */
        .movie-list {
            display: flex;
            flex-direction: column;
            gap: 15px;
            margin-top: 20px;
        }
        .movie-card {
            display: flex;
            background: var(--glass-bg);
            border: 1px solid var(--glass-border);
            border-radius: 12px;
            overflow: hidden;
            cursor: pointer;
            transition: 0.3s;
            backdrop-filter: blur(10px);
        }
        .movie-card:hover {
            transform: scale(1.02);
            box-shadow: 0 0 20px rgba(0, 243, 255, 0.2);
            border-color: var(--neon-cyan);
        }
        .movie-card img {
            width: 100px;
            height: 140px;
            object-fit: cover;
        }
        .movie-info {
            padding: 15px;
            display: flex;
            flex-direction: column;
            justify-content: center;
        }
        .movie-info h3 { font-size: 1.1rem; margin-bottom: 5px; }
        .movie-info p { font-size: 0.8rem; color: #aaa; }

        /* 6, 7, 4 Modals (Glassmorphism) */
        .modal-overlay {
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.7);
            backdrop-filter: blur(8px);
            display: none;
            justify-content: center;
            align-items: center;
            z-index: 2000;
            padding: 20px;
        }
        .modal-overlay.show { display: flex; }
        .modal-content {
            background: rgba(20, 25, 40, 0.9);
            border: 1px solid var(--glass-border);
            border-radius: 15px;
            padding: 25px;
            width: 100%;
            max-width: 400px;
            text-align: center;
            box-shadow: 0 0 30px rgba(0, 243, 255, 0.1);
            animation: fadeIn 0.3s ease;
        }
        .modal-content h2 { margin-bottom: 15px; font-size: 1.5rem; }
        .modal-btn {
            padding: 10px 20px;
            border-radius: 8px;
            border: none;
            cursor: pointer;
            font-weight: 600;
            margin: 5px;
            transition: 0.3s;
        }
        .btn-primary { background: var(--neon-cyan); color: black; }
        .btn-primary:hover { box-shadow: 0 0 15px var(--neon-cyan); }
        .btn-danger { background: var(--neon-pink); color: white; }
        .btn-danger:hover { box-shadow: 0 0 15px var(--neon-pink); }
        
        /* Timer Bar */
        .timer-bar { margin: 20px 0; }
        .progress { width: 100%; background: #333; border-radius: 10px; overflow: hidden; height: 10px; }
        .progress-fill { width: 0%; height: 100%; background: var(--neon-cyan); transition: width 1s linear; }

        /* 8. Bottom Nav */
        #bottom-nav {
            position: fixed;
            bottom: 0; left: 0; width: 100%;
            height: 65px;
            background: rgba(11, 15, 25, 0.9);
            backdrop-filter: blur(15px);
            border-top: 1px solid var(--glass-border);
            display: flex;
            justify-content: space-around;
            align-items: center;
            z-index: 1000;
        }
        .nav-item {
            display: flex;
            flex-direction: column;
            align-items: center;
            color: #888;
            text-decoration: none;
            font-size: 0.75rem;
            cursor: pointer;
            transition: 0.3s;
        }
        .nav-item i { font-size: 1.2rem; margin-bottom: 3px; }
        .nav-item.active { color: var(--neon-cyan); text-shadow: 0 0 10px var(--neon-cyan); }

        /* 11. Profile Links */
        .social-link {
            display: flex;
            align-items: center;
            gap: 15px;
            padding: 15px;
            background: var(--glass-bg);
            border: 1px solid var(--glass-border);
            border-radius: 10px;
            margin-bottom: 10px;
            text-decoration: none;
            color: white;
            transition: 0.3s;
        }
        .social-link:hover { border-color: var(--neon-cyan); transform: translateX(5px); }
        .social-link i { font-size: 1.5rem; color: var(--neon-cyan); }

    </style>
</head>
<body>

    <!-- 1. Welcome Screen -->
    <div id="welcome-screen">
        <div class="welcome-text">মুভি বক্স জগতে স্বাগতম</div>
    </div>

    <!-- Background Blobs -->
    <div class="blob blob-1"></div>
    <div class="blob blob-2"></div>

    <!-- 2. Header -->
    <div id="header">
        <a onclick="navigateTo('home')">Movie Box</a>
    </div>

    <!-- Main Pages -->
    <div id="page-home" class="page active">
        <input type="text" class="search-box" placeholder="সার্চ করুন..." onkeyup="filterMovies()">
        <div class="categories" id="cat-container"></div>
        <div class="movie-list" id="movie-container"></div>
    </div>

    <div id="page-search" class="page">
        <h2 style="margin-bottom:15px;">সার্চ</h2>
        <input type="text" class="search-box" placeholder="মুভি খুঁজুন...">
    </div>

    <div id="page-favorites" class="page">
        <h2 style="margin-bottom:15px;">আমার ফেভারিট</h2>
        <div class="movie-list" id="fav-container"></div>
    </div>

    <div id="page-upcoming" class="page">
        <h2 style="margin-bottom:15px;">আসন্ন মুভি</h2>
        <div class="movie-list" id="upcoming-container"></div>
    </div>

    <div id="page-profile" class="page">
        <div style="text-align:center; margin-bottom:20px;">
            <img src="https://via.placeholder.com/100" style="border-radius:50%; border: 2px solid var(--neon-cyan);">
            <h2 style="margin-top:10px;">Admin</h2>
        </div>
        <p style="text-align:center; margin-bottom:20px; color:#aaa;">এখানে আপনার কাস্টম টেক্সট বা About Me সেকশন থাকবে।</p>
        
        <a href="#" class="social-link"><i class="fab fa-telegram"></i> Telegram Channel</a>
        <a href="#" class="social-link"><i class="fab fa-facebook"></i> Facebook Page</a>
        <a href="#" class="social-link"><i class="fab fa-youtube"></i> YouTube Channel</a>
        <a href="#" class="social-link"><i class="fas fa-globe"></i> Website Link</a>
    </div>

    <!-- 4. 18+ Age Modal -->
    <div class="modal-overlay" id="age-modal">
        <div class="modal-content">
            <h2 style="color:var(--neon-pink);">⚠️ Adult Content</h2>
            <p style="margin:15px 0;">আপনার বয়স কি ১৮ বছরের বেশি?</p>
            <button class="modal-btn btn-danger" onclick="confirmAge(true)">হ্যাঁ</button>
            <button class="modal-btn btn-primary" onclick="confirmAge(false)">না</button>
        </div>
    </div>

    <!-- 6. Movie Detail & Download Modal -->
    <div class="modal-overlay" id="detail-modal">
        <div class="modal-content">
            <h2 id="modal-movie-name">Movie Name</h2>
            <p id="modal-movie-cat" style="color:#aaa; font-size:0.9rem; margin-bottom:20px;">Category</p>
            
            <button class="modal-btn btn-primary" onclick="startDownload()" style="width:100%; margin-bottom:10px;">
                <i class="fas fa-download"></i> ডাউনলোড করুন
            </button>
            <button class="modal-btn btn-danger" id="fav-btn" onclick="toggleFavorite()" style="width:100%;">
                <i class="fas fa-heart"></i> ফেভারিট
            </button>
            
            <div class="timer-bar" id="timer-section" style="display:none;">
                <p id="timer-text">Please wait 15 seconds to unlock download</p>
                <div class="progress"><div class="progress-fill" id="progress-bar"></div></div>
            </div>
            <a href="#" id="real-download-link" style="display:none; margin-top:15px;" class="modal-btn btn-primary">
                <i class="fas fa-file-download"></i> ফাইল ডাউনলোড করুন
            </a>
            <br><br>
            <button class="modal-btn" onclick="closeModal('detail-modal')" style="background:#333; color:white;">বন্ধ করুন</button>
        </div>
    </div>

    <!-- 8. Bottom Navigation -->
    <div id="bottom-nav">
        <div class="nav-item active" onclick="navigateTo('home')"><i class="fas fa-home"></i><span>Home</span></div>
        <div class="nav-item" onclick="navigateTo('search')"><i class="fas fa-search"></i><span>Search</span></div>
        <div class="nav-item" onclick="navigateTo('favorites')"><i class="fas fa-heart"></i><span>Favorites</span></div>
        <div class="nav-item" onclick="navigateTo('upcoming')"><i class="fas fa-clock"></i><span>Upcoming</span></div>
        <div class="nav-item" onclick="navigateTo('profile')"><i class="fas fa-user"></i><span>Profile</span></div>
    </div>

    <script>
        // --- Data ---
        const categories = ['All', 'Bangla', 'Bengali Dubbed', 'Hindi', 'Hindi Dubbed', 'English', 'Web Series', 'Korean', 'Anime', '18+'];
        
        const movies = [
            { id: 1, title: "আওয়ারিয়া", cat: "Bangla", img: "https://via.placeholder.com/100x140/00f3ff/000000?text=Movie1", upcomming: false },
            { id: 2, title: "প্রিয়া রে", cat: "Bangla", img: "https://via.placeholder.com/100x140/ff00de/ffffff?text=Movie2", upcomming: false },
            { id: 3, title: "Jawan (Dubbed)", cat: "Bengali Dubbed", img: "https://via.placeholder.com/100x140/00f3ff/000000?text=Movie3", upcomming: false },
            { id: 4, title: "Animal", cat: "Hindi", img: "https://via.placeholder.com/100x140/ff00de/ffffff?text=Movie4", upcomming: false },
            { id: 5, title: "Oppenheimer", cat: "English", img: "https://via.placeholder.com/100x140/00f3ff/000000?text=Movie5", upcomming: false },
            { id: 6, title: "Squid Game S2", cat: "Korean", img: "https://via.placeholder.com/100x140/ff00de/ffffff?text=Movie6", upcomming: true, date: "2024-12-25" },
            { id: 7, title: "Demon Slayer", cat: "Anime", img: "https://via.placeholder.com/100x140/00f3ff/000000?text=Movie7", upcomming: false },
            { id: 8, title: "XXX Uncut", cat: "18+", img: "https://via.placeholder.com/100x140/ff00de/ffffff?text=18+", upcomming: false },
        ];

        let currentCat = 'All';
        let currentMovieId = null;
        let favorites = JSON.parse(localStorage.getItem('movieBoxFav')) || [];

        // --- Init ---
        window.onload = () => {
            setTimeout(() => document.getElementById('welcome-screen').classList.add('hide'), 2500);
            renderCategories();
            renderMovies();
            renderUpcoming();
        };

        // --- Navigation ---
        function navigateTo(pageId) {
            document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
            document.getElementById(`page-${pageId}`).classList.add('active');
            document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
            event.currentTarget.classList.add('active');
            
            if(pageId === 'favorites') renderFavorites();
        }

        // --- 3. Categories ---
        function renderCategories() {
            const container = document.getElementById('cat-container');
            container.innerHTML = categories.map(cat => 
                `<div class="cat-btn ${cat === currentCat ? 'active' : ''}" onclick="selectCategory('${cat}')">${cat}</div>`
            ).join('');
        }

        function selectCategory(cat) {
            if(cat === '18+') {
                document.getElementById('age-modal').classList.add('show');
            } else {
                currentCat = cat;
                renderCategories();
                renderMovies();
            }
        }

        // --- 4. Age Verification ---
        function confirmAge(isAdult) {
            document.getElementById('age-modal').classList.remove('show');
            if(isAdult) {
                currentCat = '18+';
                renderCategories();
                renderMovies();
            } else {
                currentCat = 'All';
                renderCategories();
                renderMovies();
            }
        }

        // --- 5. Movie Cards ---
        function renderMovies() {
            const container = document.getElementById('movie-container');
            const filtered = currentCat === 'All' ? movies.filter(m => !m.upcomming) : movies.filter(m => m.cat === currentCat && !m.upcomming);
            
            container.innerHTML = filtered.map(movie => `
                <div class="movie-card" onclick="openMovieModal(${movie.id})">
                    <img src="${movie.img}" alt="${movie.title}">
                    <div class="movie-info">
                        <h3>${movie.title}</h3>
                        <p>${movie.cat}</p>
                    </div>
                </div>
            `).join('');
        }

        function filterMovies() {
            // Simple search logic placeholder
        }

        // --- 6. Movie Modal ---
        function openMovieModal(id) {
            currentMovieId = id;
            const movie = movies.find(m => m.id === id);
            document.getElementById('modal-movie-name').innerText = movie.title;
            document.getElementById('modal-movie-cat').innerText = movie.cat;
            
            document.getElementById('timer-section').style.display = 'none';
            document.getElementById('real-download-link').style.display = 'none';
            document.getElementById('detail-modal').classList.add('show');
            
            updateFavBtn();
        }

        function closeModal(id) {
            document.getElementById(id).classList.remove('show');
        }

        // --- 7. Download & Adsterra Timer System ---
        function startDownload() {
            document.getElementById('timer-section').style.display = 'block';
            document.getElementById('real-download-link').style.display = 'none';
            
            // Open Adsterra Ad (Replace '#' with your actual Adsterra link)
            window.open('#', '_blank'); 

            let timeLeft = 15;
            const progressBar = document.getElementById('progress-bar');
            const timerText = document.getElementById('timer-text');
            
            const interval = setInterval(() => {
                timeLeft--;
                progressBar.style.width = ((15 - timeLeft) / 15) * 100 + '%';
                timerText.innerText = `Please wait ${timeLeft} seconds to unlock download`;
                
                if(timeLeft <= 0) {
                    clearInterval(interval);
                    timerText.innerText = "Download Unlocked!";
                    document.getElementById('real-download-link').style.display = 'inline-block';
                }
            }, 1000);
        }

        // --- 9. Favorites System ---
        function toggleFavorite() {
            if(favorites.includes(currentMovieId)) {
                favorites = favorites.filter(id => id !== currentMovieId);
            } else {
                favorites.push(currentMovieId);
            }
            localStorage.setItem('movieBoxFav', JSON.stringify(favorites));
            updateFavBtn();
        }

        function updateFavBtn() {
            const btn = document.getElementById('fav-btn');
            if(favorites.includes(currentMovieId)) {
                btn.innerHTML = '<i class="fas fa-heart"></i> ফেভারিট থেকে সরান';
            } else {
                btn.innerHTML = '<i class="far fa-heart"></i> ফেভারিট যোগ করুন';
            }
        }

        function renderFavorites() {
            const container = document.getElementById('fav-container');
            const favMovies = movies.filter(m => favorites.includes(m.id));
            if(favMovies.length === 0) {
                container.innerHTML = '<p style="text-align:center; color:#aaa;">কোনো ফেভারিট মুভি নেই।</p>';
                return;
            }
            container.innerHTML = favMovies.map(movie => `
                <div class="movie-card" onclick="openMovieModal(${movie.id})">
                    <img src="${movie.img}" alt="${movie.title}">
                    <div class="movie-info">
                        <h3>${movie.title}</h3>
                        <p>${movie.cat}</p>
                    </div>
                </div>
            `).join('');
        }

        // --- 10. Upcoming Movies ---
        function renderUpcoming() {
            const container = document.getElementById('upcoming-container');
            const upMovies = movies.filter(m => m.upcomming);
            container.innerHTML = upMovies.map(movie => `
                <div class="movie-card">
                    <img src="${movie.img}" alt="${movie.title}">
                    <div class="movie-info">
                        <h3>${movie.title}</h3>
                        <p>Release: ${movie.date}</p>
                    </div>
                </div>
            `).join('');
        }
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

if __name__ == '__main__':
    app.run(debug=True)
