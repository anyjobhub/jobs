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

# ── PostgreSQL ────────────────────────────────────────────────────────────────
# Render automatically injects DATABASE_URL when a Postgres database is
# attached to the service. Locally, set it in your .env file.
# Format: postgresql://user:password@host:port/dbname
DATABASE_URL: str = os.getenv("DATABASE_URL", "")

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Business rules ────────────────────────────────────────────────────────────
FETCH_COOLDOWN_SECONDS: int = int(os.getenv("FETCH_COOLDOWN_SECONDS", "3600"))  # 1 hour
MAX_STORED_JOBS: int = int(os.getenv("MAX_STORED_JOBS", "500"))

# ── Filtering keywords ────────────────────────────────────────────────────────
FILTER_KEYWORDS = ["walkin", "walk-in", "interview", "fresher"]
TARGET_CITY = "hyderabad"
