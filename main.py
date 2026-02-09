import os
import httpx
import requests
import psycopg2
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

# ==================================================
# WAJIB PALING ATAS: BUAT FOLDER MEDIA
# ==================================================
os.makedirs("media", exist_ok=True)

# ================= ENV =================
SUNO_API_KEY = os.getenv("SUNO_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

BASE_URL = os.getenv(
    "BASE_URL",
    "https://musik-android.onrender.com"
)

CALLBACK_URL = f"{BASE_URL}/callback"

SUNO_BASE_API = "https://api.kie.ai/api/v1"
STYLE_GENERATE_URL = f"{SUNO_BASE_API}/style/generate"
MUSIC_GENERATE_URL = f"{SUNO_BASE_API}/generate"
STATUS_URL = f"{SUNO_BASE_API}/generate/record-info"
LYRICS_URL = f"{SUNO_BASE_API}/generate/get-timestamped-lyrics"
VIDEO_URL = f"{SUNO_BASE_API}/mp4/generate"

# ================= APP =================
app = FastAPI(
    title="AI Music Suno API Wrapper",
    version="2.0.0"
)

# ==================================================
# STATIC FILES
# ==================================================
app.mount("/media", StaticFiles(directory="media"), name="media")

# ================= REQUEST MODEL =================
class BoostStyleRequest(BaseModel):
    content: str

class GenerateMusicRequest(BaseModel):
    prompt: str
    style: Optional[str] = None
    title: Optional[str] = None
    instrumental: bool = False
    customMode: bool = False
    model: str = "V4_5"

# ================= HELPERS =================
def suno_headers():
    if not SUNO_API_KEY:
        raise HTTPException(500, "SUNO_API_KEY not set")
    return {
        "Authorization": f"Bearer {SUNO_API_KEY}",
        "Content-Type": "application/json"
    }

def normalize_model(model: str) -> str:
    if model.lower() in ["v4", "v4_5", "v45"]:
        return "V4_5"
    return model

def get_conn():
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL not set")
    return psycopg2.connect(DATABASE_URL)

def save_file(url: str, path: str):
    r = requests.get(url, timeout=120)
    with open(path, "wb") as f:
        f.write(r.content)

# ================= ROOT =================
@app.get("/")
def root():
    return {"status": "running", "service": "AI Music Suno API"}

@app.get("/health")
def health():
    return {"status": "ok"}

# ================= BOOST STYLE =================
@app.post("/boost-style")
async def boost_style(payload: BoostStyleRequest):
    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(
            STYLE_GENERATE_URL,
            headers=suno_headers(),
            json={"content": payload.content}
        )
    return res.json()

# ================= GENERATE MUSIC =================
@app.post("/generate-music")
async def generate_music(payload: GenerateMusicRequest):

    body = {
        "prompt": payload.prompt,
        "customMode": payload.customMode,
        "instrumental": payload.instrumental,
        "model": normalize_model(payload.model),
        "callBackUrl": CALLBACK_URL
    }

    if payload.style:
        body["style"] = payload.style
    if payload.title:
        body["title"] = payload.title

    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(
            MUSIC_GENERATE_URL,
            headers=suno_headers(),
            json=body
        )

    if res.status_code != 200:
        raise HTTPException(500, "Gagal generate musik")

    return res.json()

# ================= RECORD INFO =================
@app.get("/record-info/{task_id}")
async def record_info(task_id: str):
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            STATUS_URL,
            headers=suno_headers(),
            params={"taskId": task_id}
        )
    return res.json()

# ================= CALLBACK =================
@app.post("/callback")
async def callback(request: Request):
    data = await request.json()
    print("CALLBACK:", data)

    try:
        task_id = data.get("taskId") or data.get("task_id")
        items = data.get("data") or []

        if not items:
            return {"status": "ignored"}

        item = items[0]
        state = item.get("state") or item.get("status")

        if state != "succeeded":
            return {"status": "processing"}

        audio_url = (
            item.get("audioUrl")
            or item.get("audio_url")
            or item.get("streamAudioUrl")
        )

        video_url = (
            item.get("videoUrl")
            or item.get("video_url")
            or item.get("resultUrl")
        )

        # ================= AUDIO CALLBACK =================
        if audio_url:

            audio_id = item.get("audioId")
            image_url = item.get("imageUrl")
            title = item.get("title", "Untitled")

            conn = get_conn()
            cur = conn.cursor()

            cur.execute("SELECT status FROM songs WHERE task_id=%s", (task_id,))
            row = cur.fetchone()

            if row and row[0] in ["audio_done", "done"]:
                cur.close()
                conn.close()
                return {"status": "already_processed"}

            # SAVE MP3
            mp3_path = f"media/{task_id}.mp3"
            save_file(audio_url, mp3_path)
            local_audio_url = f"{BASE_URL}/media/{task_id}.mp3"

            # GET LYRICS
            async with httpx.AsyncClient(timeout=60) as client:
                lyrics_res = await client.post(
                    LYRICS_URL,
                    headers=suno_headers(),
                    json={"taskId": task_id, "audioId": audio_id}
                )

            lyrics_data = lyrics_res.json()

            # TRIGGER VIDEO
            async with httpx.AsyncClient(timeout=60) as client:
                video_res = await client.post(
                    VIDEO_URL,
                    headers=suno_headers(),
                    json={
                        "taskId": task_id,
                        "audioId": audio_id,
                        "callBackUrl": CALLBACK_URL,
                        "author": "AI Artist",
                        "domainName": BASE_URL
                    }
                )

            video_task_id = video_res.json()["data"]["taskId"]

            # UPDATE DB
            cur.execute("""
                INSERT INTO songs
                (task_id, title, audio_url, cover_url, lyrics, audio_id, video_task_id, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (task_id) DO UPDATE SET
                    audio_url=EXCLUDED.audio_url,
                    lyrics=EXCLUDED.lyrics,
                    audio_id=EXCLUDED.audio_id,
                    video_task_id=EXCLUDED.video_task_id,
                    status='audio_done'
            """, (
                task_id,
                title,
                local_audio_url,
                image_url,
                str(lyrics_data),
                audio_id,
                video_task_id,
                "audio_done"
            ))

            conn.commit()
            cur.close()
            conn.close()

            return {"status": "audio_saved_video_started"}

        # ================= VIDEO CALLBACK =================
        if video_url:

            mp4_path = f"media/{task_id}.mp4"
            save_file(video_url, mp4_path)
            local_video_url = f"{BASE_URL}/media/{task_id}.mp4"

            conn = get_conn()
            cur = conn.cursor()

            cur.execute("""
                UPDATE songs
                SET video_url=%s,
                    status='done'
                WHERE task_id=%s
            """, (local_video_url, task_id))

            conn.commit()
            cur.close()
            conn.close()

            return {"status": "video_saved"}

        return {"status": "unknown_callback"}

    except Exception as e:
        print("CALLBACK ERROR:", e)
        return {"status": "error", "error": str(e)}

# ================= DB TEST =================
@app.get("/db-all")
def db_all():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM songs;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows
