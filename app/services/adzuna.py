"""
adzuna.py — Adzuna API integration service.

Responsibilities
----------------
1. Build the API request with correct params.
2. Execute with a single retry on failure.
3. Filter results to Hyderabad walk-in / fresher jobs only.
4. Return a clean list of job dicts ready for DB insertion.

API quota awareness
-------------------
- Only 1 page (50 results) per call.
- The /trigger-fetch route enforces a 1-hour cooldown so this function
  is never called more than once per hour.
"""
import logging

import httpx

from app.config import (
    APP_ID,
    APP_KEY,
    ADZUNA_BASE_URL,
    FILTER_KEYWORDS,
    TARGET_CITY,
)

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_relevant(raw: dict) -> bool:
    """
    Return True only when:
      1. location display_name contains TARGET_CITY (hyderabad), AND
      2. title or description contains at least one FILTER_KEYWORD.
    """
    location: str = (
        raw.get("location", {}).get("display_name") or ""
    ).lower()

    if TARGET_CITY not in location:
        return False

    title = (raw.get("title") or "").lower()
    desc = (raw.get("description") or "").lower()
    combined = title + " " + desc

    return any(kw in combined for kw in FILTER_KEYWORDS)


def _parse(raw: dict) -> dict:
    """Map a raw Adzuna result dict to our internal schema."""
    return {
        "id":          raw.get("id", ""),
        "title":       raw.get("title", "").strip(),
        "company":     (raw.get("company") or {}).get("display_name", "N/A"),
        "location":    (raw.get("location") or {}).get("display_name", ""),
        "description": (raw.get("description") or "").strip(),
        "url":         raw.get("redirect_url", ""),
        "created_at":  raw.get("created", ""),
    }


# ── Main fetch function ───────────────────────────────────────────────────────

async def fetch_jobs_from_adzuna() -> tuple[list[dict], str | None]:
    """
    Fetch one page of Adzuna results, filter, and return.

    Returns
    -------
    (jobs, error_message)
        jobs          : list of clean job dicts (may be empty)
        error_message : string if something went wrong, else None
    """
    if not APP_ID or not APP_KEY:
        return [], "APP_ID or APP_KEY is not configured."

    params = {
        "app_id":           APP_ID,
        "app_key":          APP_KEY,
        "where":            "hyderabad",
        "what":             "walkin interview fresher bpo software developer",
        "results_per_page": 50,
        "sort_by":          "date",
    }

    raw_results = await _request_with_retry(params)
    if raw_results is None:
        return [], "Adzuna API request failed after retry."

    filtered = [_parse(r) for r in raw_results if _is_relevant(r)]
    logger.info(
        "Adzuna: %d raw result(s) → %d passed filter.", len(raw_results), len(filtered)
    )
    return filtered, None


async def _request_with_retry(params: dict) -> list | None:
    """
    Attempt the Adzuna HTTP GET up to 2 times.
    Returns the list under 'results' key, or None on total failure.
    """
    for attempt in range(1, 3):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(ADZUNA_BASE_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
                return data.get("results", [])
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Adzuna HTTP error (attempt %d/2): %s %s",
                attempt, exc.response.status_code, exc.response.text[:200],
            )
        except Exception as exc:
            logger.warning("Adzuna request error (attempt %d/2): %s", attempt, exc)

    return None
