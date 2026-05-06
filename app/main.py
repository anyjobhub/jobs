"""
main.py — FastAPI application entry point.

Startup sequence
----------------
1. Load .env (via config.py import).
2. Initialise asyncpg PostgreSQL pool + create tables (lifespan handler).
3. Mount routers.
4. Serve with uvicorn.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Config import triggers load_dotenv() early
import app.config as cfg  # noqa: F401 — side-effect import

from app.database import init_db, close_db
from app.routes import jobs as jobs_router
from app.routes import fetch as fetch_router

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("⚡ Starting Hyderabad Jobs API…")
    await init_db()         # creates asyncpg pool + tables
    logger.info("✅ PostgreSQL ready. Service is live.")
    yield
    await close_db()        # gracefully close pool on shutdown
    logger.info("🛑 Shut down complete.")


# ── Application ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Hyderabad Jobs API",
    description=(
        "Lightweight job aggregation for Hyderabad walk-in & fresher jobs. "
        "Powered by Adzuna API. Backed by PostgreSQL on Render."
    ),
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Allow cross-origin requests (useful if you add a frontend later)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(jobs_router.router)
app.include_router(fetch_router.router)


# ── Root & health ─────────────────────────────────────────────────────────────
@app.get("/", tags=["Info"], summary="Service info")
async def root():
    return {
        "service":   "Hyderabad Jobs API",
        "version":   "2.0.0",
        "database":  "PostgreSQL (Render managed)",
        "endpoints": [
            "GET /jobs          — latest 50 jobs",
            "GET /walkins       — walk-in jobs only",
            "GET /freshers      — fresher jobs only",
            "GET /stats         — aggregate counts",
            "GET /trigger-fetch — fetch & store new jobs (no Telegram)",
            "GET /health        — health check",
            "GET /docs          — interactive API docs",
        ],
    }


@app.get("/health", tags=["Info"], summary="Health check for UptimeRobot")
async def health():
    """
    Lightweight ping endpoint.
    Point UptimeRobot at /health (every 5 min) to keep Render awake.
    Point cron-job.org at /trigger-fetch every 60 min.
    """
    return {"status": "ok"}
