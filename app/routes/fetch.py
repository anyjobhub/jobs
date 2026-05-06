"""
routes/fetch.py — The /trigger-fetch endpoint.

Flow
----
1. Check in-memory + DB cooldown (≥ 1 hour since last fetch).
2. Call Adzuna API (via adzuna service).
3. Deduplicate against DB; insert only new jobs.
4. Cleanup DB to MAX_STORED_JOBS rows.
5. Filter Telegram-eligible jobs (new + created within TELEGRAM_MAX_AGE_HOURS).
6. Send to Telegram; mark as sent.
7. Return a JSON summary.

Anti-spam design for cold starts
---------------------------------
Render free tier wipes the ephemeral filesystem on every cold start, so the
SQLite DB is empty after each wake.  Without protection, ALL fetched jobs
would be re-sent to Telegram.

Solution: a job is only Telegram-eligible if its Adzuna `created_at`
timestamp is within TELEGRAM_MAX_AGE_HOURS (default 48 h).  Jobs posted more
than 48 h ago are stored but not re-posted.  This keeps the channel clean.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from app.config import (
    FETCH_COOLDOWN_SECONDS,
    MAX_STORED_JOBS,
    TELEGRAM_MAX_AGE_HOURS,
)
from app.database import (
    cleanup_old_jobs,
    job_exists,
    insert_job,
    mark_sent_to_telegram,
    get_meta,
    set_meta,
)
from app.services.adzuna import fetch_jobs_from_adzuna
from app.services.telegram import send_jobs_batch

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


def _is_telegram_eligible(job: dict, cutoff: datetime) -> bool:
    """
    Return True only if job.created_at is recent enough to post to Telegram.
    Jobs with unparseable dates are treated as NOT eligible (safe default).
    """
    dt = _parse_dt(job.get("created_at", ""))
    if dt is None:
        return False
    return dt >= cutoff


@router.get("/trigger-fetch", summary="Fetch jobs from Adzuna and publish new ones")
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
                    "status":                  "skipped",
                    "reason":                  "Rate limit active — 1 fetch per hour.",
                    "next_fetch_in_seconds":   remaining,
                    "next_fetch_in":           f"{remaining // 60}m {remaining % 60}s",
                    "last_fetch_at":           last_fetch_str,
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

    logger.info(
        "Fetch complete: %d fetched, %d new.", len(jobs), len(new_jobs)
    )

    # ── 4. Cleanup ────────────────────────────────────────────────────────────
    deleted = await cleanup_old_jobs(MAX_STORED_JOBS)

    # ── 5. Telegram eligibility filter ───────────────────────────────────────
    # Only post jobs created within the last TELEGRAM_MAX_AGE_HOURS.
    # This prevents spamming after a Render cold-start DB wipe.
    cutoff = now - timedelta(hours=TELEGRAM_MAX_AGE_HOURS)
    telegram_jobs = [j for j in new_jobs if _is_telegram_eligible(j, cutoff)]

    logger.info(
        "Telegram eligible: %d of %d new job(s) (cutoff: %s).",
        len(telegram_jobs), len(new_jobs), cutoff.isoformat(),
    )

    # ── 6. Send to Telegram & mark sent ──────────────────────────────────────
    sent_count = 0
    if telegram_jobs:
        sent_count = await send_jobs_batch(telegram_jobs)
        sent_ids = [j["id"] for j in telegram_jobs[:sent_count]]
        await mark_sent_to_telegram(sent_ids)

    # ── 7. Response ───────────────────────────────────────────────────────────
    return {
        "status":          "success",
        "timestamp":       now.isoformat(),
        "fetched":         len(jobs),
        "new_jobs":        len(new_jobs),
        "telegram_sent":   sent_count,
        "old_jobs_deleted": deleted,
    }
