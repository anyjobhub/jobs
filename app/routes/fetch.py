"""
routes/fetch.py — The /trigger-fetch endpoint.

Flow
----
1. Check cooldown (≥ 1 hour since last fetch).
2. Fetch from Adzuna API.
3. Deduplicate against DB; insert only new jobs.
4. Cleanup DB to MAX_STORED_JOBS rows.
5. Return a JSON summary.

Telegram
--------
Telegram notifications are NOT sent here.
This endpoint is a pure data pipeline: fetch → store → done.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter

from app.config import FETCH_COOLDOWN_SECONDS, MAX_STORED_JOBS
from app.database import cleanup_old_jobs, job_exists, insert_job, get_meta, set_meta
from app.services.adzuna import fetch_jobs_from_adzuna

router = APIRouter(tags=["Fetch"])
logger = logging.getLogger(__name__)


def _parse_dt(iso: str) -> datetime | None:
    """Parse an ISO-8601 datetime string, returning None on failure."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


@router.get("/trigger-fetch", summary="Fetch jobs from Adzuna and store in PostgreSQL")
async def trigger_fetch():
    now = datetime.now(timezone.utc)

    # ── 1. Cooldown check ─────────────────────────────────────────────────────
    last_fetch_str = await get_meta("last_fetch_at")
    if last_fetch_str:
        last_fetch_dt = _parse_dt(last_fetch_str)
        if last_fetch_dt:
            elapsed = (now - last_fetch_dt).total_seconds()
            if elapsed < FETCH_COOLDOWN_SECONDS:
                remaining = int(FETCH_COOLDOWN_SECONDS - elapsed)
                return {
                    "status":               "skipped",
                    "reason":               "Rate limit active — 1 fetch per hour.",
                    "next_fetch_in_seconds": remaining,
                    "next_fetch_in":         f"{remaining // 60}m {remaining % 60}s",
                    "last_fetch_at":         last_fetch_str,
                }

    # ── 2. Fetch from Adzuna ──────────────────────────────────────────────────
    logger.info("Triggering Adzuna fetch at %s", now.isoformat())
    jobs, error = await fetch_jobs_from_adzuna()

    if error:
        logger.error("Adzuna fetch error: %s", error)
        return {"status": "error", "message": error}

    # Record successful fetch timestamp immediately (even if 0 results)
    await set_meta("last_fetch_at", now.isoformat())

    # ── 3. Deduplicate & insert ───────────────────────────────────────────────
    new_jobs: list[dict] = []
    for job in jobs:
        if not job.get("id"):
            continue
        if not await job_exists(job["id"]):
            await insert_job(job)
            new_jobs.append(job)

    logger.info("Fetch complete: %d fetched, %d new.", len(jobs), len(new_jobs))

    # ── 4. Cleanup — keep DB size bounded ─────────────────────────────────────
    deleted = await cleanup_old_jobs(MAX_STORED_JOBS)

    # ── 5. Response ───────────────────────────────────────────────────────────
    return {
        "status":            "success",
        "timestamp":         now.isoformat(),
        "fetched":           len(jobs),
        "new_jobs":          len(new_jobs),
        "old_jobs_deleted":  deleted,
    }
