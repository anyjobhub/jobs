"""
database.py — Async SQLite handler using aiosqlite.

Schema
------
jobs  : stores each unique Adzuna job listing
meta  : key-value store for runtime state (e.g. last_fetch_at timestamp)

Design notes
------------
- `sent_to_telegram` flag prevents duplicate Telegram posts within a session.
- After a Render cold-start the DB is wiped; jobs re-fetched from Adzuna will
  be inserted fresh.  To avoid spamming old posts, the fetch route only sends
  to Telegram if a job's `created_at` is within TELEGRAM_MAX_AGE_HOURS hours.
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
    sent_to_telegram  INTEGER DEFAULT 0
);
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# ── Initialisation ────────────────────────────────────────────────────────────

async def init_db() -> None:
    """Create tables if they do not exist. Called once at startup."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE_JOBS)
        await db.execute(_CREATE_META)
        await db.commit()
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
                 url, created_at, fetched_at, sent_to_telegram)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                job["id"],
                job["title"],
                job.get("company", "N/A"),
                job.get("location", ""),
                job.get("description", "")[:500],   # cap to 500 chars
                job.get("url", ""),
                job.get("created_at", ""),
                datetime.now(timezone.utc).isoformat(),
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
            "SELECT id, title, company, location, url, created_at, fetched_at "
            "FROM jobs ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def get_walkin_jobs(limit: int = 50) -> list[dict]:
    """Return jobs whose title or description mentions walk-in keywords."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, title, company, location, url, created_at, fetched_at
            FROM   jobs
            WHERE  lower(title)       LIKE '%walkin%'
               OR  lower(title)       LIKE '%walk-in%'
               OR  lower(description) LIKE '%walkin%'
               OR  lower(description) LIKE '%walk-in%'
            ORDER  BY fetched_at DESC
            LIMIT  ?
            """,
            (limit,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def get_fresher_jobs(limit: int = 50) -> list[dict]:
    """Return jobs whose title or description mentions fresher."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, title, company, location, url, created_at, fetched_at
            FROM   jobs
            WHERE  lower(title)       LIKE '%fresher%'
               OR  lower(description) LIKE '%fresher%'
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
    return {
        "total_jobs": total,
        "new_jobs_today": today_count,
        "db_path": DB_PATH,
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
