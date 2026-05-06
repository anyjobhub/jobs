"""
database.py — Async SQLite handler using aiosqlite.

Schema v2 changes
-----------------
- Added `is_walkin`  INTEGER column (0/1 flag, set by adzuna classifier)
- Added `is_fresher` INTEGER column (0/1 flag, set by adzuna classifier)
- `get_walkin_jobs`  now queries `is_walkin = 1`  (fast indexed lookup)
- `get_fresher_jobs` now queries `is_fresher = 1` (fast indexed lookup)
- `_migrate_schema()` safely adds new columns to existing DBs via
   ALTER TABLE … so Render persistent-disk DBs upgrade without data loss.

Design notes
------------
- `sent_to_telegram` flag prevents duplicate Telegram posts within a session.
- After a Render cold-start the DB may be wiped (free tier = ephemeral /tmp).
  Jobs re-fetched from Adzuna will be inserted fresh.
- Cleanup keeps at most MAX_STORED_JOBS rows so SQLite stays tiny.
"""
import logging
from datetime import datetime, timezone

import aiosqlite

from app.config import DB_PATH

logger = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

_CREATE_JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    company           TEXT DEFAULT 'N/A',
    location          TEXT DEFAULT '',
    description       TEXT DEFAULT '',
    url               TEXT DEFAULT '',
    created_at        TEXT DEFAULT '',
    fetched_at        TEXT NOT NULL,
    sent_to_telegram  INTEGER DEFAULT 0,
    is_walkin         INTEGER DEFAULT 0,
    is_fresher        INTEGER DEFAULT 0
);
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added in schema v2 — safely migrated on existing DBs
_V2_MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN is_walkin  INTEGER DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN is_fresher INTEGER DEFAULT 0",
]


# ── Initialisation ────────────────────────────────────────────────────────────

async def init_db() -> None:
    """
    Create tables and run migrations. Called once at startup.
    Migration errors (column already exists) are silently ignored so
    a fresh DB and an upgraded existing DB both work correctly.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE_JOBS)
        await db.execute(_CREATE_META)
        await db.commit()

        # Schema v2 migrations — safe to run on any existing DB
        for sql in _V2_MIGRATIONS:
            try:
                await db.execute(sql)
                await db.commit()
            except Exception:
                pass    # Column already exists — ignore

    logger.info("Database initialised at %s", DB_PATH)


# ── Job CRUD ──────────────────────────────────────────────────────────────────

async def job_exists(job_id: str) -> bool:
    """Return True if a job with this Adzuna ID is already stored."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def insert_job(job: dict) -> None:
    """Insert a new job row; silently ignore if ID already exists."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO jobs
                (id, title, company, location, description,
                 url, created_at, fetched_at, sent_to_telegram,
                 is_walkin, is_fresher)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                job["id"],
                job["title"],
                job.get("company", "N/A"),
                job.get("location", ""),
                job.get("description", "")[:500],       # cap to 500 chars
                job.get("url", ""),
                job.get("created_at", ""),
                datetime.now(timezone.utc).isoformat(),
                1 if job.get("is_walkin")  else 0,      # boolean → int
                1 if job.get("is_fresher") else 0,
            ),
        )
        await db.commit()


async def mark_sent_to_telegram(job_ids: list[str]) -> None:
    """Mark jobs as sent so we never re-post them within the same session."""
    if not job_ids:
        return
    placeholders = ",".join("?" * len(job_ids))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE jobs SET sent_to_telegram = 1 WHERE id IN ({placeholders})",
            job_ids,
        )
        await db.commit()


async def get_jobs(limit: int = 50) -> list[dict]:
    """Return latest *limit* jobs ordered by fetch time."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, title, company, location, url,
                   created_at, fetched_at, is_walkin, is_fresher
            FROM   jobs
            ORDER  BY fetched_at DESC
            LIMIT  ?
            """,
            (limit,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def get_walkin_jobs(limit: int = 50) -> list[dict]:
    """Return jobs classified as walk-in (is_walkin = 1)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, title, company, location, url,
                   created_at, fetched_at, is_walkin, is_fresher
            FROM   jobs
            WHERE  is_walkin = 1
            ORDER  BY fetched_at DESC
            LIMIT  ?
            """,
            (limit,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def get_fresher_jobs(limit: int = 50) -> list[dict]:
    """Return jobs classified as fresher (is_fresher = 1)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, title, company, location, url,
                   created_at, fetched_at, is_walkin, is_fresher
            FROM   jobs
            WHERE  is_fresher = 1
            ORDER  BY fetched_at DESC
            LIMIT  ?
            """,
            (limit,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def get_stats() -> dict:
    """Return aggregate counts for the /stats endpoint."""
    today = datetime.now(timezone.utc).date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM jobs") as cur:
            total: int = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM jobs WHERE fetched_at LIKE ?", (f"{today}%",)
        ) as cur:
            today_count: int = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM jobs WHERE is_walkin = 1") as cur:
            walkin_total: int = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM jobs WHERE is_fresher = 1") as cur:
            fresher_total: int = (await cur.fetchone())[0]

    return {
        "total_jobs":     total,
        "new_jobs_today": today_count,
        "walkin_jobs":    walkin_total,
        "fresher_jobs":   fresher_total,
        "db_path":        DB_PATH,
    }


async def cleanup_old_jobs(max_jobs: int) -> int:
    """
    Delete rows beyond *max_jobs* (keeping the most recently fetched).
    Returns the number of rows deleted.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM jobs") as cur:
            count: int = (await cur.fetchone())[0]
        if count <= max_jobs:
            return 0
        await db.execute(
            """
            DELETE FROM jobs
            WHERE id NOT IN (
                SELECT id FROM jobs ORDER BY fetched_at DESC LIMIT ?
            )
            """,
            (max_jobs,),
        )
        await db.commit()
        deleted = count - max_jobs
    logger.info("Cleanup removed %d old job(s); %d remain.", deleted, max_jobs)
    return deleted


# ── Meta key-value store ──────────────────────────────────────────────────────

async def get_meta(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_meta(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()
