"""
database.py — Async PostgreSQL handler using asyncpg.

Migrated from SQLite/aiosqlite → PostgreSQL/asyncpg.

Key differences from SQLite version
-------------------------------------
- Connection pool (asyncpg.Pool) replaces per-call aiosqlite.connect().
  The pool is created once at startup (init_db) and closed at shutdown.
- Placeholders are $1, $2, … (PostgreSQL style) instead of ?.
- INSERT OR IGNORE → INSERT … ON CONFLICT (id) DO NOTHING.
- Schema migrations use IF NOT EXISTS / IF NOT EXISTS column checks via
  DO $$ … $$ anonymous blocks — no ALTER TABLE … ADD COLUMN IF NOT EXISTS
  needed (Postgres 9.6+ supports it natively).
- All public functions receive the pool as a module-level singleton via
  get_pool() — callers don't need to manage connections.

Pool lifecycle
--------------
  init_db()  → called in FastAPI lifespan startup  → creates pool + tables
  close_db() → called in FastAPI lifespan shutdown → closes pool cleanly
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from app.config import DATABASE_URL, MAX_STORED_JOBS

logger = logging.getLogger(__name__)

# ── Module-level pool singleton ───────────────────────────────────────────────
_pool: Optional[asyncpg.Pool] = None


def get_pool() -> asyncpg.Pool:
    """Return the active connection pool. Raises if init_db() was not called."""
    if _pool is None:
        raise RuntimeError("Database pool is not initialised. Call init_db() first.")
    return _pool


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


# ── Initialisation ────────────────────────────────────────────────────────────

async def init_db() -> None:
    """
    Create the asyncpg connection pool and ensure tables exist.
    Called once at application startup via FastAPI lifespan.
    """
    global _pool

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Attach a PostgreSQL database to your Render service and ensure "
            "DATABASE_URL is injected as an environment variable."
        )

    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,         # conservative for Render free tier
        command_timeout=30,
        # Render Postgres uses SSL; asyncpg handles it automatically
        # when the URL contains ?sslmode=require (Render adds this).
    )

    async with _pool.acquire() as conn:
        await conn.execute(_CREATE_JOBS)
        await conn.execute(_CREATE_META)

    logger.info("✅ PostgreSQL pool ready (%s)", DATABASE_URL.split("@")[-1])


async def close_db() -> None:
    """Close the connection pool. Called at application shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("🛑 PostgreSQL pool closed.")


# ── Job CRUD ──────────────────────────────────────────────────────────────────

async def job_exists(job_id: str) -> bool:
    """Return True if a job with this Adzuna ID is already stored."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM jobs WHERE id = $1", job_id
        )
        return row is not None


async def insert_job(job: dict) -> None:
    """Insert a new job row; silently ignore if the ID already exists."""
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO jobs
                (id, title, company, location, description,
                 url, created_at, fetched_at, sent_to_telegram,
                 is_walkin, is_fresher)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0, $9, $10)
            ON CONFLICT (id) DO NOTHING
            """,
            job["id"],
            job["title"],
            job.get("company", "N/A"),
            job.get("location", ""),
            (job.get("description") or "")[:500],   # cap to 500 chars
            job.get("url", ""),
            job.get("created_at", ""),
            datetime.now(timezone.utc).isoformat(),
            1 if job.get("is_walkin")  else 0,
            1 if job.get("is_fresher") else 0,
        )


async def mark_sent_to_telegram(job_ids: list[str]) -> None:
    """Mark jobs as sent so they are never re-posted within the same session."""
    if not job_ids:
        return
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET sent_to_telegram = 1 WHERE id = ANY($1::text[])",
            job_ids,
        )


async def get_jobs(limit: int = 50) -> list[dict]:
    """Return the latest *limit* jobs ordered by fetch time (newest first)."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, company, location, url,
                   created_at, fetched_at, is_walkin, is_fresher
            FROM   jobs
            ORDER  BY fetched_at DESC
            LIMIT  $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


async def get_walkin_jobs(limit: int = 50) -> list[dict]:
    """Return jobs classified as walk-in (is_walkin = 1)."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, company, location, url,
                   created_at, fetched_at, is_walkin, is_fresher
            FROM   jobs
            WHERE  is_walkin = 1
            ORDER  BY fetched_at DESC
            LIMIT  $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


async def get_fresher_jobs(limit: int = 50) -> list[dict]:
    """Return jobs classified as fresher (is_fresher = 1)."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, company, location, url,
                   created_at, fetched_at, is_walkin, is_fresher
            FROM   jobs
            WHERE  is_fresher = 1
            ORDER  BY fetched_at DESC
            LIMIT  $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


async def get_stats() -> dict:
    """Return aggregate counts for the /stats endpoint."""
    today = datetime.now(timezone.utc).date().isoformat()
    async with get_pool().acquire() as conn:
        total: int = await conn.fetchval("SELECT COUNT(*) FROM jobs")
        today_count: int = await conn.fetchval(
            "SELECT COUNT(*) FROM jobs WHERE fetched_at LIKE $1",
            f"{today}%",
        )
        walkin_total: int = await conn.fetchval(
            "SELECT COUNT(*) FROM jobs WHERE is_walkin = 1"
        )
        fresher_total: int = await conn.fetchval(
            "SELECT COUNT(*) FROM jobs WHERE is_fresher = 1"
        )

    return {
        "total_jobs":     total,
        "new_jobs_today": today_count,
        "walkin_jobs":    walkin_total,
        "fresher_jobs":   fresher_total,
    }


async def cleanup_old_jobs(max_jobs: int) -> int:
    """
    Delete rows beyond *max_jobs* (keeping the most recently fetched).
    Returns the number of rows deleted.
    """
    async with get_pool().acquire() as conn:
        count: int = await conn.fetchval("SELECT COUNT(*) FROM jobs")
        if count <= max_jobs:
            return 0
        await conn.execute(
            """
            DELETE FROM jobs
            WHERE id NOT IN (
                SELECT id FROM jobs ORDER BY fetched_at DESC LIMIT $1
            )
            """,
            max_jobs,
        )
        deleted = count - max_jobs

    logger.info("Cleanup removed %d old job(s); %d remain.", deleted, max_jobs)
    return deleted


# ── Meta key-value store ──────────────────────────────────────────────────────

async def get_meta(key: str) -> str | None:
    """Read a value from the meta table."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM meta WHERE key = $1", key
        )
        return row["value"] if row else None


async def set_meta(key: str, value: str) -> None:
    """Upsert a key-value pair in the meta table."""
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO meta (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            key, value,
        )
