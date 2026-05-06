"""
main.py — FastAPI application entry point.

Startup sequence
----------------
1. Load .env (via config.py import).
2. Initialise SQLite tables (lifespan handler).
3. Mount routers.
4. Serve with uvicorn.

Cold-start behaviour on Render free tier
-----------------------------------------
Render spins the service down after ~15 min of inactivity and restarts it
on the next request.  The SQLite file lives on an ephemeral disk and will be
gone after a restart.  `init_db()` in the lifespan re-creates the tables on
every start, so the service is always in a valid state — jobs will re-populate
on the next /trigger-fetch call from the external cron.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Config import triggers load_dotenv() early
import app.config as cfg  # noqa: F401 — side-effect import

from app.database import init_db
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
    await init_db()
    logger.info("✅ DB ready. Service is live.")
    yield
    logger.info("🛑 Shutting down.")


# ── Application ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Hyderabad Jobs API",
    description=(
        "Lightweight job aggregation for Hyderabad walk-in & fresher jobs. "
        "Powered by Adzuna API. Optimised for Render free tier."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Allow cross-origin requests (useful if you add a simple frontend later)
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
        "version":   "1.0.0",
        "endpoints": [
            "GET /jobs          — latest 50 jobs",
            "GET /walkins       — walk-in jobs only",
            "GET /freshers      — fresher jobs only",
            "GET /stats         — aggregate counts",
            "GET /trigger-fetch — fetch & publish new jobs",
            "GET /health        — health check",
            "GET /docs          — interactive API docs",
        ],
    }


@app.get("/health", tags=["Info"], summary="Health check for UptimeRobot")
async def health():
    """
    Lightweight ping endpoint.
    Point UptimeRobot at /health (every 5 min) to keep Render awake.
    Also point cron-job.org at /trigger-fetch every 60 min.
    """
    return {"status": "ok"}
