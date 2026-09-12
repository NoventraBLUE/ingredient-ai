from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, Header, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import sqlite3
import json
import os
import uuid
import time
import secrets
import hmac
import hashlib
import base64
import re
import urllib.request
import urllib.parse
from collections import defaultdict
from html.parser import HTMLParser
from google import genai
from google.genai import types

app = FastAPI(title="Ingredient AI - Dynamic Photo Engine")

# 1. Environment & Secret Management
def load_env_file():
    env_path = ".env"
    if not os.path.exists(env_path):
        with open(env_path, "w", encoding="utf-8") as f:
            f.write('GEMINI_API_KEY="AQ.Ab8RN6KkkMaggtjm-BUF1IV_-G657uC1OdDFBbtRMDu075b0pA"\n')
            f.write(f'JWT_SECRET="{secrets.token_hex(32)}"\n')
            f.write(f'CSRF_SECRET="{secrets.token_hex(32)}"\n')
    
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

load_env_file()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_hex(32))
CSRF_SECRET = os.environ.get("CSRF_SECRET", secrets.token_hex(32))

client = genai.Client(api_key=GEMINI_KEY)
MODELS_TO_TRY = ["gemini-3.6-flash", "gemini-2.5-flash"]

# 2. Production Security Middleware (Force HTTPS & HSTS)
class ProductionSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        forwarded_proto = request.headers.get("x-forwarded-proto")
        host = request.headers.get("host", "")
        is_local = "localhost" in host or "127.0.0.1" in host or "0.0.0.0" in host

        if not is_local and forwarded_proto == "http":
            url = request.url.replace(scheme="https")
            return RedirectResponse(url, status_code=status.HTTP_301_MOVED_PERMANENTLY)

        response = await call_next(request)
        if not is_local or forwarded_proto == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        return response

app.add_middleware(ProductionSecurityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# 3. Dynamic Link Thumbnail & Metadata Extractor
class LinkMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = ""
        self.image = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            prop = attrs_dict.get("property", "").lower()
            name = attrs_dict.get("name", "").lower()
            content = attrs_dict.get("content", "")
            if (prop in ("og:image", "twitter:image") or name in ("image", "twitter:image")) and content.startswith("http"):
                if not self.image:
                    self.image = content
            if (prop in ("og:description", "twitter:description") or name in ("description",)) and not self.description:
                self.description = content
            if (prop in ("og:title", "twitter:title") or name in ("title",)) and not self.title:
                self.title = content

    def handle_data(self, data):
        if self.in_title and not self.title:
            self.title = data.strip()

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

def extract_thumbnail_from_link(url: str) -> str:
    # 1. YouTube Shorts or Video (extracts high quality video thumbnail)
    yt_match = re.search(r'(?:v=|youtu\.be\/|shorts\/)([a-zA-Z0-9_-]{11})', url)
    if yt_match:
        vid_id = yt_match.group(1)
        return f"https://img.youtube.com/vi/{vid_id}/hqdefault.jpg"

    # 2. Recipe Blogs / Web pages (extracts OpenGraph og:image)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            html = resp.read()[:25000].decode("utf-8", errors="ignore")
            parser = LinkMetaParser()
            parser.feed(html)
            if parser.image:
                return parser.image
    except Exception:
        pass
    return ""

def extract_link_metadata(url: str) -> str:
    yt_match = re.search(r'(?:v=|youtu\.be\/|shorts\/)([a-zA-Z0-9_-]{11})', url)
    if yt_match:
        try:
            oembed_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url)}&format=json"
            req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                return f"YouTube Title: {data.get('title', '')}"
        except Exception:
            pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            html = resp.read()[:20000].decode("utf-8", errors="ignore")
            parser = LinkMetaParser()
            parser.feed(html)
            return f"Page Title: {parser.title}\nDescription: {parser.description}"
    except Exception:
        return f"Link: {url}"

# 4. Multi-Category Authentic Food Photo Matcher
def get_dish_photo(dish_name: str) -> str:
    d = dish_name.lower()
    
    # Desi / South Asian
    if any(k in d for k in ["karahi", "kadai", "handi"]):
        return "https://images.unsplash.com/photo-1603894584373-5ac82b2ae398?w=800"
    if any(k in d for k in ["curry", "salan", "korma", "gravy", "masala", "tikka masala"]):
        return "https://images.unsplash.com/photo-1588166524941-3bf61a9c41db?w=800"
    if any(k in d for k in ["biryani", "pulao", "rice"]):
        return "https://images.unsplash.com/photo-1563379091339-03b21ab4a4f8?w=800"
    if any(k in d for k in ["nihari", "haleem", "stew", "paya"]):
        return "https://images.unsplash.com/photo-1547592166-23ac45744acd?w=800"
    if any(k in d for k in ["daal", "dal", "lentil", "chana"]):
        return "https://images.unsplash.com/photo-1546833999-b9f581a1996d?w=800"
    if any(k in d for k in ["kebab", "kabab", "tikka", "bbq", "tandoori"]):
        return "https://images.unsplash.com/photo-1555939594-58d7cb561ad1?w=800"

    # Western / Italian
    if any(k in d for k in ["pizza", "calzone"]):
        return "https://images.unsplash.com/photo-1513104890138-7c749659a591?w=800"
    if any(k in d for k in ["pasta", "fettuccine", "spaghetti", "penne", "lasagna", "alfredo"]):
        return "https://images.unsplash.com/photo-1621996346565-e3d5d6281290?w=800"
    if any(k in d for k in ["burger", "cheeseburger", "slider"]):
        return "https://images.unsplash.com/photo-1568901346375-23c9450c58cd?w=800"
    if any(k in d for k in ["steak", "beef", "ribs", "brisket"]):
        return "https://images.unsplash.com/photo-1558030006-450675393462?w=800"

    # Mexican
    if any(k in d for k in ["taco", "burrito", "fajita", "quesadilla", "mexican"]):
        return "https://images.unsplash.com/photo-1565299585323-38d6b0865b47?w=800"

    # Asian
    if any(k in d for k in ["ramen", "udon", "noodle", "soba"]):
        return "https://images.unsplash.com/photo-1569718212165-3a8278d5f624?w=800"
    if any(k in d for k in ["sushi", "sashimi"]):
        return "https://images.unsplash.com/photo-1579871494447-9811cf80d66c?w=800"
    if any(k in d for k in ["pad thai", "thai"]):
        return "https://images.unsplash.com/photo-1559314809-0d155014e29e?w=800"

    # Middle Eastern
    if any(k in d for k in ["shawarma", "hummus", "falafel"]):
        return "https://images.unsplash.com/photo-1561651823-34feb02250e4?w=800"

    # Desserts
    if any(k in d for k in ["cake", "cookie", "dessert", "brownie", "sweet", "chocolate"]):
        return "https://images.unsplash.com/photo-1578985545062-69928b1d9587?w=800"

    return "https://images.unsplash.com/photo-1546069901-ba9599a7e63c?w=800"

# 5. Rate Limiting, CSRF, Magic Bytes
class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)

    def is_allowed(self, client_ip: str) -> bool:
        now = time.time()
        window_start = now - self.window_seconds
        self.requests[client_ip] = [t for t in self.requests[client_ip] if t > window_start]
        if len(self.requests[client_ip]) >= self.max_requests:
            return False
        self.requests[client_ip].append(now)
        return True

ai_rate_limiter = SlidingWindowRateLimiter(max_requests=10, window_seconds=60)

def generate_csrf_token() -> str:
    raw = secrets.token_hex(16)
    sig = hmac.new(CSRF_SECRET.encode('utf-8'), raw.encode('utf-8'), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"

def verify_csrf_token(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        raw, sig = token.split('.')
        expected = hmac.new(CSRF_SECRET.encode('utf-8'), raw.encode('utf-8'), hashlib.sha256).hexdigest()
        return secrets.compare_digest(sig, expected)
    except Exception:
        return False

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

MAX_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".webm"}

def validate_magic_bytes(data: bytes, ext: str) -> bool:
    if len(data) < 4:
        return False
    if ext in ('.jpg', '.jpeg') and data[:3] == b'\xff\xd8\xff':
        return True
    if ext == '.png' and data[:4] == b'\x89PNG':
        return True
    if ext == '.webp' and data[:4] == b'RIFF' and len(data) >= 12 and data[8:12] == b'WEBP':
        return True
    if ext in ('.mp4', '.mov') and (len(data) >= 8 and (data[4:8] == b'ftyp' or data[:4] in (b'\x00\x00\x00\x18', b'\x00\x00\x00\x20'))):
        return True
    if ext == '.webm' and data[:4] == b'\x1a\x45\xdf\xa3':
        return True
    return False

def generate_ai_with_fallback(contents, schema):
    last_err = None
    for model_name in MODELS_TO_TRY:
        try:
            return client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.2,
                ),
            )
        except Exception as e:
            last_err = e
            continue
    raise last_err

def ask_ai_with_fallback(prompt):
    last_err = None
    for model_name in MODELS_TO_TRY:
        try:
            return client.models.generate_content(model=model_name, contents=prompt)
        except Exception as e:
            last_err = e
            continue
    raise last_err

def get_db():
    conn = sqlite3.connect("ingredient_ai.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        owner_token TEXT,
        title TEXT NOT NULL,
        photo_url TEXT NOT NULL,
        source_url TEXT,
        cuisine TEXT
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS recipes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER UNIQUE,
        title TEXT NOT NULL,
        calories INTEGER,
        min_calories INTEGER,
        max_calories INTEGER,
        protein_g REAL,
        carbs_g REAL,
        fat_g REAL,
        ingredients TEXT,
        steps TEXT,
        FOREIGN KEY (post_id) REFERENCES posts (id)
    );
    """)
    conn.commit()
    conn.close()

init_db()

class IngredientDetail(BaseModel):
    item: str
    amount: float
    unit: str
    calories: Optional[int] = 0

class ExtractedRecipe(BaseModel):
    title: str
    cuisine: str
    min_calories: int
    max_calories: int
    protein_g: float
    carbs_g: float
    fat_g: float
    ingredients: List[IngredientDetail]
    steps: List[str]

class QuestionRequest(BaseModel):
    question: str

@app.get("/api/csrf_token")
def get_csrf():
    return {"csrf_token": generate_csrf_token()}

@app.get("/api/feed")
def get_feed():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.id as post_id, p.title, p.photo_url, p.source_url, p.cuisine, p.owner_token,
               r.calories, r.min_calories, r.max_calories, r.protein_g, r.carbs_g, r.fat_g, r.ingredients, r.steps
        FROM posts p
        LEFT JOIN recipes r ON p.id = r.post_id
        ORDER BY p.id DESC
    """)
    rows = cursor.fetchall()
    feed = []
    for r in rows:
        item = dict(r)
        if item.get("ingredients"):
            item["ingredients"] = json.loads(item["ingredients"])
        if item.get("steps"):
            item["steps"] = json.loads(item["steps"])
        feed.append(item)
    conn.close()
    return {"posts": feed}

# 6. Analyze Dish (Extracts Image from URL or Dish Catalog)
@app.post("/api/analyze")
async def analyze_dish(
    request: Request,
    reel_url: Optional[str] = Form(None),
    media_file: Optional[UploadFile] = File(None),
    b_hp_url: Optional[str] = Form(None),
    x_csrf_token: Optional[str] = Header(None),
    x_owner_token: Optional[str] = Header(None)
):
    if not verify_csrf_token(x_csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token invalid or missing.")
    if b_hp_url:
        raise HTTPException(status_code=400, detail="Spam bot detected.")

    client_ip = request.client.host if request.client else "127.0.0.1"
    if not ai_rate_limiter.is_allowed(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit reached: Maximum 10 extractions per minute. Please wait.")

    clean_url = reel_url.strip() if reel_url else None
    has_file = media_file and media_file.filename

    if not has_file and not clean_url:
        raise HTTPException(status_code=400, detail="Please select a food photo/video or paste a link.")

    saved_file_path = None
    saved_photo_url = None
    uploaded_gemini_file = None

    if has_file:
        raw_name = media_file.filename or ""
        ext = os.path.splitext(raw_name.lower())
        
        if ext not in ALLOWED_IMAGE_EXTS and ext not in ALLOWED_VIDEO_EXTS:
            raise HTTPException(status_code=400, detail="Disallowed file type. Only JPG, PNG, WEBP, and MP4 are permitted.")

        file_bytes = await media_file.read()
        if len(file_bytes) > MAX_FILE_SIZE:
            raise HTTPException(status_code=413, detail="File exceeds 10MB limit.")

        if not validate_magic_bytes(file_bytes, ext):
            raise HTTPException(status_code=400, detail="Invalid media file header detected.")

        unique_name = f"{uuid.uuid4().hex}{ext}"
        saved_file_path = os.path.join(UPLOAD_DIR, unique_name)
        with open(saved_file_path, "wb") as f:
            f.write(file_bytes)
        
        if ext in ALLOWED_IMAGE_EXTS:
            saved_photo_url = f"/uploads/{unique_name}"

    link_context = ""
    link_thumbnail = ""
    if clean_url:
        link_context = extract_link_metadata(clean_url)
        link_thumbnail = extract_thumbnail_from_link(clean_url)

    contents = []

    if saved_file_path and ext in ALLOWED_VIDEO_EXTS:
        try:
            gemini_file = client.files.upload(file=saved_file_path)
            while gemini_file.state.name == "PROCESSING":
                time.sleep(1.5)
                gemini_file = client.files.get(name=gemini_file.name)
            uploaded_gemini_file = gemini_file
            contents.append(gemini_file)
        except Exception:
            pass
    elif saved_file_path:
        with open(saved_file_path, "rb") as f:
            contents.append(types.Part.from_bytes(data=f.read(), mime_type=media_file.content_type or "image/jpeg"))

    prompt = f"""
    You are Ingredient AI, an expert visual culinary recognition system.
    Look closely at the media or link and AUTOMATICALLY IDENTIFY the exact dish:
    1. Determine the EXACT dish shown (actively recognize Pakistani / Desi, Italian, Mexican, and Asian dishes; never guess French duck breast unless clearly visible).
    2. Regional Cuisine.
    3. Realistic Calorie Range: min_calories and max_calories for a standard serving.
    4. Total macro breakdown: protein (g), carbs (g), fat (g).
    5. List all ingredients with realistic amounts (grams/tbsp/cups) and calories per ingredient.
    6. 3-4 clear step-by-step cooking instructions.
    
    Link context: {clean_url or 'None'}
    {link_context}
    """
    contents.append(prompt)

    try:
        response = generate_ai_with_fallback(contents, ExtractedRecipe)
        recipe = ExtractedRecipe.model_validate_json(response.text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gemini AI error: {str(e)}")
    finally:
        if uploaded_gemini_file:
            try:
                client.files.delete(name=uploaded_gemini_file.name)
            except Exception:
                pass

    # Priority 1: User uploaded photo
    # Priority 2: Real thumbnail extracted from link (YouTube / OpenGraph)
    # Priority 3: Dynamic dish photo matching the recipe title
    if not saved_photo_url:
        saved_photo_url = link_thumbnail if link_thumbnail else get_dish_photo(recipe.title)

    avg_cal = int((recipe.min_calories + recipe.max_calories) / 2)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO posts (title, photo_url, source_url, cuisine, owner_token)
        VALUES (?, ?, ?, ?, ?)
    """, (recipe.title, saved_photo_url, clean_url, recipe.cuisine, x_owner_token))
    post_id = cursor.lastrowid

    cursor.execute("""
        INSERT INTO recipes (post_id, title, calories, min_calories, max_calories, protein_g, carbs_g, fat_g, ingredients, steps)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        post_id, recipe.title, avg_cal, recipe.min_calories, recipe.max_calories,
        recipe.protein_g, recipe.carbs_g, recipe.fat_g,
        json.dumps([i.model_dump() for i in recipe.ingredients]), json.dumps(recipe.steps)
    ))
    conn.commit()
    conn.close()

    return {"status": "success", "post_id": post_id, "title": recipe.title, "photo_url": saved_photo_url}

@app.delete("/api/posts/{post_id}")
def delete_dish(post_id: int, x_csrf_token: Optional[str] = Header(None), x_owner_token: Optional[str] = Header(None)):
    if not verify_csrf_token(x_csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token invalid or missing.")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT owner_token FROM posts WHERE id = ?", (post_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Recipe not found.")

    stored_owner = row["owner_token"]
    if stored_owner is None:
        conn.close()
        raise HTTPException(status_code=403, detail="Curated showcase recipes cannot be deleted.")

    if not x_owner_token or x_owner_token != stored_owner:
        conn.close()
        raise HTTPException(status_code=403, detail="Permission denied: You can only delete recipes you created.")

    cursor.execute("DELETE FROM recipes WHERE post_id = ?", (post_id,))
    cursor.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}

@app.post("/api/posts/{post_id}/ask")
def ask_ai(request: Request, post_id: int, req: QuestionRequest):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not ai_rate_limiter.is_allowed(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit reached. Please wait a moment.")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT p.title, p.cuisine, r.ingredients, r.steps FROM posts p JOIN recipes r ON p.id = r.post_id WHERE p.id = ?", (post_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Dish not found")

    prompt = f"Dish: {row['title']} ({row['cuisine']}). Ingredients: {row['ingredients']}. Steps: {row['steps']}. Question: {req.question}"
    response = ask_ai_with_fallback(prompt)
    return {"answer": response.text.strip()}

# 7. Frontend UI
@app.get("/", response_class=HTMLResponse)
def home():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Ingredient AI</title>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #070a12;
            --card-bg: #111827;
            --card-border: rgba(255, 255, 255, 0.08);
            --accent-green: #10b981;
            --accent-glow: rgba(16, 185, 129, 0.2);
            --accent-blue: #38bdf8;
            --accent-wa: #25d366;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; }
        body { background: var(--bg-dark); color: var(--text-main); display: flex; justify-content: center; padding: 12px; min-height: 100vh; }
        .app-wrapper { width: 100%; max-width: 460px; }

        .app-header { display: flex; justify-content: space-between; align-items: center; padding: 16px 0; }
        .brand { display: flex; align-items: center; gap: 8px; font-size: 20px; font-weight: 800; color: #fff; }
        .brand-badge { background: linear-gradient(135deg, #10b981 0%, #38bdf8 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }

        .upload-card { background: #111827; border: 1px solid var(--card-border); border-radius: 22px; padding: 16px; margin-bottom: 16px; }
        .upload-dropzone { border: 2px dashed rgba(255, 255, 255, 0.15); border-radius: 16px; padding: 20px; text-align: center; cursor: pointer; margin-bottom: 12px; background: #080c14; }
        .upload-dropzone:hover { border-color: var(--accent-green); }

        .media-preview-wrap { display: none; margin-bottom: 12px; position: relative; border-radius: 14px; overflow: hidden; height: 160px; }
        .media-preview-img { width: 100%; height: 100%; object-fit: cover; }
        .btn-clear-media { position: absolute; top: 8px; right: 8px; background: rgba(0,0,0,0.7); color: #fff; border: none; border-radius: 50%; width: 26px; height: 26px; font-size: 12px; cursor: pointer; }

        .url-field { width: 100%; background: #080c14; border: 1px solid var(--card-border); border-radius: 12px; color: #fff; padding: 11px 14px; font-size: 12px; outline: none; margin-bottom: 12px; }
        .btn-primary-action { width: 100%; background: linear-gradient(135deg, #10b981 0%, #059669 100%); color: #000; font-weight: 800; border: none; border-radius: 12px; padding: 13px; font-size: 14px; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px; }
        .bot-trap { display: none !important; position: absolute; left: -9999px; }

        .search-bar { width: 100%; background: #111827; border: 1px solid var(--card-border); border-radius: 12px; padding: 10px 14px; color: #fff; font-size: 13px; outline: none; margin-bottom: 12px; }
        .dish-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 24px; overflow: hidden; margin-bottom: 20px; }
        .dish-hero { position: relative; width: 100%; height: 220px; background: #1f293d; }
        .dish-hero img { width: 100%; height: 100%; object-fit: cover; }
        .hero-overlay { position: absolute; inset: 0; background: linear-gradient(to top, rgba(17, 24, 39, 0.95) 0%, transparent 60%); }
        .hero-badges { position: absolute; top: 12px; left: 12px; display: flex; gap: 6px; }
        .hero-tag { background: rgba(0,0,0,0.7); backdrop-filter: blur(8px); padding: 4px 10px; border-radius: 10px; font-size: 11px; font-weight: 700; color: #fff; }

        .card-content { padding: 16px; }
        .card-title-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; margin-bottom: 4px; }
        .card-dish-title { font-size: 18px; font-weight: 800; color: #fff; }
        .btn-card-del { background: none; border: none; color: #f87171; font-size: 16px; cursor: pointer; }

        .portion-tuner { display: flex; align-items: center; justify-content: space-between; background: #080c14; padding: 6px 10px; border-radius: 10px; border: 1px solid var(--card-border); margin-bottom: 12px; font-size: 11px; }
        .tuner-pills { display: flex; gap: 4px; }
        .tuner-pill { background: #1f293d; color: var(--text-muted); padding: 3px 8px; border-radius: 6px; cursor: pointer; font-weight: 700; }
        .tuner-pill.active { background: var(--accent-green); color: #000; }

        .card-tab-nav { display: flex; background: #080c14; border: 1px solid var(--card-border); border-radius: 12px; padding: 3px; margin-bottom: 12px; }
        .card-tab-btn { flex: 1; background: none; border: none; color: var(--text-muted); font-size: 11px; font-weight: 700; padding: 7px 2px; border-radius: 9px; cursor: pointer; text-align: center; }
        .card-tab-btn.active { background: #1f293d; color: #fff; }
        .card-pane { display: none; }
        .card-pane.active { display: block; }

        .checklist-row { display: flex; align-items: center; justify-content: space-between; background: #080c14; border: 1px solid var(--card-border); padding: 8px 12px; border-radius: 10px; margin-bottom: 6px; font-size: 12px; cursor: pointer; }
        .check-box { width: 18px; height: 18px; border-radius: 5px; border: 1px solid #475569; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 800; margin-right: 10px; color: transparent; }
        .checklist-row.checked { opacity: 0.45; }
        .checklist-row.checked .check-box { background: var(--accent-green); border-color: var(--accent-green); color: #000; }
        .checklist-row.checked .check-text { text-decoration: line-through; }

        .share-btn-row { display: flex; gap: 8px; margin-top: 10px; }
        .btn-wa-share { flex: 1; background: rgba(37, 211, 102, 0.15); border: 1px solid rgba(37, 211, 102, 0.4); color: #25d366; font-size: 11px; font-weight: 800; padding: 8px 10px; border-radius: 10px; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px; }
        .btn-copy-grocery { flex: 1; background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.3); color: #38bdf8; font-size: 11px; font-weight: 700; padding: 8px 10px; border-radius: 10px; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px; }

        .step-item { background: #080c14; border: 1px solid var(--card-border); border-radius: 10px; padding: 10px 12px; margin-bottom: 8px; font-size: 12px; display: flex; gap: 8px; }
        .step-badge { background: rgba(16, 185, 129, 0.2); color: #34d399; font-weight: 800; border-radius: 6px; padding: 2px 7px; font-size: 11px; height: fit-content; }
        .nutri-stat { display: flex; justify-content: space-between; background: #080c14; padding: 8px 12px; border-radius: 10px; margin-bottom: 6px; font-size: 12px; }

        .ask-bar { display: flex; gap: 6px; margin-top: 6px; }
        .ask-input-box { flex: 1; background: #080c14; border: 1px solid var(--card-border); border-radius: 8px; color: #fff; padding: 8px 10px; font-size: 12px; outline: none; }
        .btn-ask-submit { background: #38bdf8; color: #000; border: none; border-radius: 8px; padding: 0 12px; font-weight: 700; font-size: 12px; cursor: pointer; }
        .ask-reply-box { font-size: 12px; color: #cbd5e1; background: #080c14; border: 1px solid var(--card-border); border-radius: 8px; padding: 10px; margin-top: 8px; display: none; }

        .toast-notify { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: #10b981; color: #000; font-weight: 800; font-size: 12px; padding: 10px 18px; border-radius: 30px; display: none; z-index: 999; }
    </style>
</head>
<body>
    <div class="app-wrapper">
        <header class="app-header">
            <div class="brand">
                <span style="font-size:24px;">🥗</span>
                <span class="brand-badge">Ingredient AI</span>
            </div>
            <div style="font-size:11px; color:#38bdf8; font-weight:700;">Dynamic Photos 📸</div>
        </header>

        <div class="upload-card">
            <input type="file" id="mediaInput" accept="image/*,video/*" style="display: none;" onchange="handleMediaSelected(event)">

            <div class="media-preview-wrap" id="previewWrap">
                <img id="previewImg" class="media-preview-img" alt="Selected media">
                <button type="button" class="btn-clear-media" onclick="clearSelectedMedia()">✕</button>
            </div>

            <div class="upload-dropzone" id="dropzone" onclick="document.getElementById('mediaInput').click()">
                <div style="font-size:32px; margin-bottom:6px;">📷</div>
                <div style="font-size:13px; font-weight:700; color:#fff; margin-bottom:2px;" id="dropLabel">Tap to Snap Photo or Choose Video</div>
                <div style="font-size:11px; color:var(--text-muted);">AI identifies the dish, ingredients, and photos automatically</div>
            </div>

            <input type="text" id="reelUrl" class="url-field" placeholder="🔗 Or paste YouTube Short, Reel, or Recipe Link...">
            <input type="text" id="b_hp_url" class="bot-trap" tabindex="-1" autocomplete="off">

            <button type="button" class="btn-primary-action" id="btnAnalyze" onclick="executeAnalyze()">
                <span>✨</span> AI Identify & Extract Recipe
            </button>
        </div>

        <input type="text" id="searchInput" class="search-bar" placeholder="🔍 Search recipes, ingredients..." oninput="handleSearch()">
        <div id="dishesFeed">Loading dishes...</div>
    </div>

    <div id="toast" class="toast-notify">Message</div>

    <script>
        let allDishes = [];
        let selectedFile = null;
        let dishPortions = {};
        let csrfToken = null;

        async function initCSRF() {
            try {
                const res = await fetch('/api/csrf_token');
                const data = await res.json();
                csrfToken = data.csrf_token;
            } catch (_) {}
        }

        function getClientToken() {
            let token = localStorage.getItem('ingredient_ai_client_token');
            if (!token) {
                token = 'usr_' + Math.random().toString(36).substring(2) + Date.now().toString(36);
                localStorage.setItem('ingredient_ai_client_token', token);
            }
            return token;
        }

        function showToast(msg) {
            const t = document.getElementById('toast');
            t.innerText = msg;
            t.style.display = 'block';
            setTimeout(() => { t.style.display = 'none'; }, 2600);
        }

        function handleMediaSelected(event) {
            const file = event.target.files[0];
            if (!file) return;
            selectedFile = file;

            if (file.type.startsWith('image/')) {
                const previewImg = document.getElementById('previewImg');
                previewImg.src = URL.createObjectURL(file);
                document.getElementById('previewWrap').style.display = 'block';
                document.getElementById('dropzone').style.display = 'none';
            } else {
                document.getElementById('dropLabel').innerText = '🎥 Selected: ' + file.name;
            }
        }

        function clearSelectedMedia() {
            selectedFile = null;
            document.getElementById('mediaInput').value = '';
            document.getElementById('previewWrap').style.display = 'none';
            document.getElementById('dropzone').style.display = 'block';
            document.getElementById('dropLabel').innerText = 'Tap to Snap Photo or Choose Video';
        }

        async function executeAnalyze() {
            const urlInput = document.getElementById('reelUrl');
            const btn = document.getElementById('btnAnalyze');
            const cleanUrl = urlInput.value.trim();
            const botTrap = document.getElementById('b_hp_url').value;

            if (!selectedFile && !cleanUrl) {
                showToast('⚠️ Please choose a photo/video or paste a link first!');
                return;
            }

            btn.disabled = true;
            btn.innerHTML = '<span>⏳</span> AI is identifying & extracting...';

            const formData = new FormData();
            if (selectedFile) formData.append('media_file', selectedFile);
            if (cleanUrl) formData.append('reel_url', cleanUrl);
            if (botTrap) formData.append('b_hp_url', botTrap);

            try {
                const res = await fetch('/api/analyze', {
                    method: 'POST',
                    headers: {
                        'X-CSRF-Token': csrfToken || '',
                        'X-Owner-Token': getClientToken()
                    },
                    body: formData
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || 'Analysis failed');

                showToast('✅ Recipe extracted: ' + data.title);
                clearSelectedMedia();
                urlInput.value = '';
                await loadFeed();
            } catch (err) {
                showToast('❌ ' + err.message);
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<span>✨</span> AI Identify & Extract Recipe';
            }
        }

        async function loadFeed() {
            try {
                const res = await fetch('/api/feed');
                const data = await res.json();
                allDishes = data.posts || [];
                renderFeed();
            } catch (_) {}
        }

        function handleSearch() { renderFeed(); }

        function setPortion(pid, scale) {
            dishPortions[pid] = scale;
            renderFeed();
        }

        function switchCardTab(btn, paneId) {
            const card = btn.closest('.card-content');
            card.querySelectorAll('.card-tab-btn').forEach(b => b.classList.remove('active'));
            card.querySelectorAll('.card-pane').forEach(p => p.classList.remove('active'));
            btn.classList.add('active');
            card.querySelector('#' + paneId).classList.add('active');
        }

        function toggleCheckItem(el) { el.classList.toggle('checked'); }

        function copyGroceryList(pid, title) {
            const post = allDishes.find(d => d.post_id === pid);
            if (!post) return;
            const scale = dishPortions[pid] || 1.0;
            const text = "🛒 Grocery List (" + scale + "x) for " + title + ":\\n" +
                (post.ingredients || []).map(i => "- " + (Math.round(i.amount * scale * 10)/10) + " " + i.unit + " " + i.item).join("\\n");
            navigator.clipboard.writeText(text);
            showToast("📋 Copied grocery list to clipboard!");
        }

        function shareToWhatsApp(pid, title) {
            const post = allDishes.find(d => d.post_id === pid);
            if (!post) return;
            const scale = dishPortions[pid] || 1.0;
            const minCal = Math.round((post.min_calories || (post.calories * 0.9)) * scale);
            const maxCal = Math.round((post.max_calories || (post.calories * 1.1)) * scale);

            let msg = `🥗 *${title}* (${post.cuisine || 'Traditional'})\n`;
            msg += `🔥 *Calories:* ${minCal}–${maxCal} kcal (Serves ${Math.round(2 * scale)})\n\n`;
            msg += `🥕 *Ingredients Checklist:*\n`;
            (post.ingredients || []).forEach(i => {
                const amt = Math.round(i.amount * scale * 10) / 10;
                msg += `• ${amt} ${i.unit} ${i.item}\n`;
            });
            msg += `\n👩‍🍳 *Cooking Steps:*\n`;
            (post.steps || []).forEach((s, idx) => {
                msg += `${idx + 1}. ${s}\n`;
            });
            msg += `\n✨ _Shared via Ingredient AI_`;
            window.open(`https://api.whatsapp.com/send?text=${encodeURIComponent(msg)}`, '_blank');
        }

        function renderFeed() {
            const container = document.getElementById('dishesFeed');
            container.innerHTML = '';
            const q = document.getElementById('searchInput').value.toLowerCase().trim();
            let filtered = allDishes;

            if (q) {
                filtered = filtered.filter(d => (d.title || '').toLowerCase().includes(q) || (d.cuisine || '').toLowerCase().includes(q) || (d.ingredients || []).some(i => (i.item || '').toLowerCase().includes(q)));
            }

            if (filtered.length === 0) {
                container.innerHTML = '<div style="text-align:center; padding:40px; color:var(--text-muted); font-size:13px;">No dishes match your search.</div>';
                return;
            }

            const myToken = getClientToken();

            filtered.forEach(post => {
                const pid = post.post_id;
                const scale = dishPortions[pid] || 1.0;
                const card = document.createElement('div');
                card.className = 'dish-card';

                const minCal = Math.round((post.min_calories || (post.calories * 0.9)) * scale);
                const maxCal = Math.round((post.max_calories || (post.calories * 1.1)) * scale);
                const pro = Math.round((post.protein_g || 0) * scale);
                const carb = Math.round((post.carbs_g || 0) * scale);
                const fat = Math.round((post.fat_g || 0) * scale);

                const isOwner = post.owner_token && (post.owner_token === myToken);
                const deleteBtn = isOwner ? `<button class="btn-card-del" onclick="deleteDish(${pid})" title="Delete recipe">🗑️</button>` : '';

                const checklistHtml = (post.ingredients || []).map(i => {
                    const scaledAmt = Math.round(i.amount * scale * 10) / 10;
                    return `
                        <div class="checklist-row" onclick="toggleCheckItem(this)">
                            <div style="display:flex; align-items:center;">
                                <div class="check-box">✓</div>
                                <div class="check-text"><b>${scaledAmt} ${i.unit}</b> ${i.item}</div>
                            </div>
                            <span style="font-size:10px; color:var(--text-muted);">${i.calories ? '~' + Math.round(i.calories * scale) + ' kcal' : ''}</span>
                        </div>
                    `;
                }).join('');

                const stepsHtml = (post.steps || []).map((s, idx) => `
                    <div class="step-item">
                        <span class="step-badge">${idx + 1}</span>
                        <div>${s}</div>
                    </div>
                `).join('');

                card.innerHTML = `
                    <div class="dish-hero">
                        <img src="${post.photo_url}" onerror="this.src='https://images.unsplash.com/photo-1546069901-ba9599a7e63c?w=700'" alt="${post.title}">
                        <div class="hero-overlay"></div>
                        <div class="hero-badges">
                            <span class="hero-tag">${post.cuisine || 'Traditional'}</span>
                            <span class="hero-tag" style="color:#ff7b7f;">🔥 ${minCal}–${maxCal} kcal</span>
                        </div>
                    </div>

                    <div class="card-content">
                        <div class="card-title-row">
                            <h2 class="card-dish-title">${post.title}</h2>
                            ${deleteBtn}
                        </div>
                        <div style="font-size:12px; color:var(--text-muted); margin-bottom:10px;">Cuisine: ${post.cuisine || 'Traditional'} • Serves ${Math.round(2 * scale)}</div>

                        <div class="portion-tuner">
                            <span style="color:var(--text-muted); font-weight:700;">Portion:</span>
                            <div class="tuner-pills">
                                <div class="tuner-pill ${scale === 0.7 ? 'active' : ''}" onclick="setPortion(${pid}, 0.7)">Small (0.7x)</div>
                                <div class="tuner-pill ${scale === 1.0 ? 'active' : ''}" onclick="setPortion(${pid}, 1.0)">Regular (1x)</div>
                                <div class="tuner-pill ${scale === 1.4 ? 'active' : ''}" onclick="setPortion(${pid}, 1.4)">Hearty (1.4x)</div>
                            </div>
                        </div>

                        <div class="card-tab-nav">
                            <button class="card-tab-btn active" onclick="switchCardTab(this, 'pane-ing-${pid}')">🥕 Ingredients</button>
                            <button class="card-tab-btn" onclick="switchCardTab(this, 'pane-steps-${pid}')">📖 Steps</button>
                            <button class="card-tab-btn" onclick="switchCardTab(this, 'pane-nutri-${pid}')">📊 Nutrition</button>
                            <button class="card-tab-btn" onclick="switchCardTab(this, 'pane-ask-${pid}')">✨ Ask AI</button>
                        </div>

                        <div class="card-pane active" id="pane-ing-${pid}">
                            ${checklistHtml}
                            <div class="share-btn-row">
                                <button class="btn-wa-share" onclick="shareToWhatsApp(${pid}, '${post.title.replace(/'/g, "\\'")}')">
                                    💬 WhatsApp
                                </button>
                                <button class="btn-copy-grocery" onclick="copyGroceryList(${pid}, '${post.title.replace(/'/g, "\\'")}')">
                                    📋 Copy List
                                </button>
                            </div>
                        </div>

                        <div class="card-pane" id="pane-steps-${pid}">
                            ${stepsHtml}
                        </div>

                        <div class="card-pane" id="pane-nutri-${pid}">
                            <div class="nutri-stat"><span style="color:#ff7b7f; font-weight:700;">🔥 Calories</span><span>${minCal}–${maxCal} kcal</span></div>
                            <div class="nutri-stat"><span style="color:#10b981; font-weight:700;">🥩 Protein</span><span>${pro} g</span></div>
                            <div class="nutri-stat"><span style="color:#38bdf8; font-weight:700;">🍞 Carbohydrates</span><span>${carb} g</span></div>
                            <div class="nutri-stat"><span style="color:#f59e0b; font-weight:700;">🥑 Healthy Fats</span><span>${fat} g</span></div>
                        </div>

                        <div class="card-pane" id="pane-ask-${pid}">
                            <div class="ask-bar">
                                <input type="text" id="ask-in-${pid}" class="ask-input-box" placeholder="Ask cooking question or substitution..." onkeydown="if(event.key==='Enter') askDishAI(${pid})">
                                <button class="btn-ask-submit" onclick="askDishAI(${pid})">Ask</button>
                            </div>
                            <div class="ask-reply-box" id="ask-out-${pid}"></div>
                        </div>
                    </div>
                `;
                container.appendChild(card);
            });
        }

        async function deleteDish(pid) {
            if (!confirm("Delete your recipe?")) return;
            try {
                const res = await fetch('/api/posts/' + pid, {
                    method: 'DELETE',
                    headers: {
                        'X-CSRF-Token': csrfToken || '',
                        'X-Owner-Token': getClientToken()
                    }
                });
                if (!res.ok) {
                    const err = await res.json();
                    throw new Error(err.detail || 'Could not delete');
                }
                showToast('Deleted recipe.');
                loadFeed();
            } catch (err) { showToast('❌ ' + err.message); }
        }

        async function askDishAI(pid) {
            const input = document.getElementById('ask-in-' + pid);
            const box = document.getElementById('ask-out-' + pid);
            const q = input.value.trim();
            if (!q) return;

            box.style.display = 'block';
            box.innerText = 'Chef AI is thinking...';

            try {
                const res = await fetch('/api/posts/' + pid + '/ask', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ question: q })
                });
                const data = await res.json();
                box.innerHTML = '<b>Chef AI:</b> ' + data.answer;
            } catch (_) {
                box.innerText = 'Could not get answer right now.';
            }
        }

        initCSRF().then(loadFeed);
    </script>
</body>
</html>"""