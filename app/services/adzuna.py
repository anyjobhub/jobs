"""
adzuna.py — Adzuna API integration service.

Fix notes
---------
- Adzuna's `what` param uses AND logic (all words must match) → always 0 results
  for multi-word queries like "walkin interview fresher bpo software developer".
- Solution: use `what_or` param (OR logic) OR run multiple single-keyword searches.
- We run 3 targeted searches and merge + deduplicate results.

API quota awareness
-------------------
- 3 API calls per trigger (still well within free plan: 250 calls/day).
- /trigger-fetch enforces 1-hour cooldown so max 3 × 24 = 72 calls/day.
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

# Three targeted searches — each returns up to 50 results.
# Using `what_or` so Adzuna matches ANY of the words (OR logic).
SEARCH_QUERIES = [
    "fresher IT software developer",    # IT fresher jobs
    "walkin interview drive",            # walk-in jobs
    "bpo customer support fresher",      # BPO/support jobs
]

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_relevant(raw: dict) -> bool:
    """
    Return True when:
      1. location contains TARGET_CITY (hyderabad / secunderabad / telangana), OR
         the `location_name` field hints at Hyderabad, AND
      2. title or description contains at least one FILTER_KEYWORD.

    Location check is broadened because Adzuna sometimes returns:
      "Hyderabad, Telangana", "Secunderabad", "Hyderabad District" etc.
    """
    location: str = (
        raw.get("location", {}).get("display_name") or ""
    ).lower()

    # Accept Hyderabad, Secunderabad (twin city), or Telangana broadly.
    city_match = any(c in location for c in ["hyderabad", "secunderabad", "telangana"])

    # If location is missing/empty, still accept (Adzuna sometimes omits it
    # but the `where=hyderabad` param already geo-filters the results).
    if location and not city_match:
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
    Run 3 targeted Adzuna searches and merge deduplicated results.

    Why 3 searches?
    ---------------
    Adzuna `what` uses AND logic — a single multi-word query returns 0 results.
    We use `what_or` (OR logic) with focused keyword groups instead.

    Returns
    -------
    (jobs, error_message)
        jobs          : deduplicated list of clean job dicts
        error_message : string if ALL searches fail, else None
    """
    if not APP_ID or not APP_KEY:
        return [], "APP_ID or APP_KEY is not configured."

    seen_ids: set[str] = set()
    all_jobs: list[dict] = []
    errors: list[str] = []

    for query in SEARCH_QUERIES:
        params = {
            "app_id":           APP_ID,
            "app_key":          APP_KEY,
            "where":            "hyderabad",
            "what_or":          query,      # ← OR logic (any word matches)
            "results_per_page": 50,
            "sort_by":          "date",
        }

        raw_results = await _request_with_retry(params)
        if raw_results is None:
            errors.append(f"Query '{query}' failed after retry.")
            continue

        # Log first result's location for debugging zero-result runs
        if raw_results:
            sample_loc = (raw_results[0].get("location") or {}).get("display_name", "N/A")
            logger.info(
                "Query '%s' → %d raw result(s). Sample location: %s",
                query, len(raw_results), sample_loc,
            )
        else:
            logger.warning("Query '%s' → 0 raw results from Adzuna.", query)

        for r in raw_results:
            job_id = r.get("id", "")
            if job_id in seen_ids:
                continue          # skip cross-query duplicates
            if _is_relevant(r):
                seen_ids.add(job_id)
                all_jobs.append(_parse(r))

    if errors and not all_jobs:
        return [], " | ".join(errors)

    logger.info(
        "Adzuna fetch complete: %d unique job(s) passed filter across %d search(es).",
        len(all_jobs), len(SEARCH_QUERIES),
    )
    return all_jobs, None


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
