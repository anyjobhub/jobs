"""
telegram.py — Telegram Bot notification service.

Responsibilities
----------------
- Format job dicts into Markdown messages.
- POST to Telegram Bot API (sendMessage).
- Log failures without crashing the main flow.

Anti-spam design
----------------
The caller (fetch route) is responsible for passing ONLY jobs that:
  1. Are new (not previously stored in DB), AND
  2. Were created within TELEGRAM_MAX_AGE_HOURS (see config).

This prevents flooding the channel after a Render cold-start wipes the DB.
"""
import logging

import httpx

from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_TELEGRAM_SEND_URL = (
    "https://api.telegram.org/bot{token}/sendMessage"
)


def _format_message(job: dict) -> str:
    """Build the Telegram message text for a single job."""
    return (
        "🔥 *Hyderabad Job Update*\n\n"
        f"🏢 *Company:* {job.get('company', 'N/A')}\n"
        f"🎯 *Role:* {job.get('title', 'N/A')}\n"
        f"📍 *Location:* {job.get('location', 'Hyderabad')}\n"
        f"🔗 *Apply:* {job.get('url', '')}"
    )


async def send_job_to_telegram(job: dict) -> bool:
    """
    Send a single job notification.

    Returns True on success, False on any failure.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — skipping notification.")
        return False

    url = _TELEGRAM_SEND_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     _format_message(job),
        "parse_mode":               "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.error(
                    "Telegram error for job %s: %s", job.get("id"), resp.text[:200]
                )
                return False
        return True
    except Exception as exc:
        logger.error("Telegram send failed for job %s: %s", job.get("id"), exc)
        return False


async def send_jobs_batch(jobs: list[dict]) -> int:
    """
    Send a list of job notifications.

    Returns the count of successfully sent messages.
    Stops early if Telegram is not configured.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info("Telegram not configured — skipping batch of %d job(s).", len(jobs))
        return 0

    sent = 0
    for job in jobs:
        ok = await send_job_to_telegram(job)
        if ok:
            sent += 1

    logger.info("Telegram: sent %d / %d job notification(s).", sent, len(jobs))
    return sent
