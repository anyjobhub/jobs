"""
config.py — Centralised configuration & environment loading.
Reads all required env vars with safe defaults.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Adzuna ──────────────────────────────────────────────────────────────────
APP_ID: str = os.getenv("APP_ID", "")
APP_KEY: str = os.getenv("APP_KEY", "")

ADZUNA_BASE_URL = "https://api.adzuna.com/v1/api/jobs/in/search/1"

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Database ──────────────────────────────────────────────────────────────────
# Render persistent disk mounts at /var/data (paid plan required).
# Free tier uses /tmp (ephemeral — DB wiped on restart, jobs re-fetch in ≤1 hr).
# Override via DB_DIR env var in Render dashboard.
_db_dir = os.getenv("DB_DIR", "/var/data")
try:
    os.makedirs(_db_dir, exist_ok=True)
except OSError:
    # Path not writable (e.g. no persistent disk on free tier) → fall back to /tmp
    _db_dir = "/tmp"
    os.makedirs(_db_dir, exist_ok=True)
DB_PATH: str = os.path.join(_db_dir, "jobs.db")

# ── Business rules ────────────────────────────────────────────────────────────
FETCH_COOLDOWN_SECONDS: int = int(os.getenv("FETCH_COOLDOWN_SECONDS", "3600"))  # 1 hour
MAX_STORED_JOBS: int = int(os.getenv("MAX_STORED_JOBS", "500"))

# Jobs older than this many hours will NOT be sent to Telegram on restart.
# Prevents re-spamming stale jobs after Render cold-start wipes the DB.
TELEGRAM_MAX_AGE_HOURS: int = int(os.getenv("TELEGRAM_MAX_AGE_HOURS", "48"))

# ── Filtering keywords ────────────────────────────────────────────────────────
FILTER_KEYWORDS = ["walkin", "walk-in", "interview", "fresher"]
TARGET_CITY = "hyderabad"
