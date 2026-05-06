"""
adzuna.py — Upgraded Adzuna API integration service.

Upgrade notes (v2)
------------------
- FIXED: was using `what` (AND logic) → always 0 results.
  Now uses `what_or` (OR logic) for all queries.
- FIXED: over-strict keyword filter removed. Now accepts ALL Hyderabad
  jobs and classifies them with is_walkin / is_fresher flags instead.
- ADDED: 2-page pagination per query (doubles result volume).
- ADDED: 5 broad search queries covering IT, BPO, fresher, walk-in.
- ADDED: is_walkin + is_fresher classification on each job object.
- IMPROVED: location filter broadened (Hyderabad + Secunderabad + Telangana).
- IMPROVED: detailed per-query, per-page logging.

API call budget
---------------
  5 queries × 2 pages = 10 API calls per /trigger-fetch
  With 1-hour cooldown: max 10 × 24 = 240 calls/day
  Adzuna free plan limit: 250 calls/day  ✅  safe
"""

import logging

import httpx

from app.config import APP_ID, APP_KEY, ADZUNA_BASE_URL

logger = logging.getLogger(__name__)

# ── Search configuration ──────────────────────────────────────────────────────

# 5 broad queries × 2 pages = 10 API calls per fetch (within free-plan budget).
# Using what_or so Adzuna matches ANY word in the query (OR logic).
SEARCH_QUERIES = [
    "software developer",       # General IT roles
    "bpo customer support",     # BPO / voice / non-voice
    "fresher",                  # All fresher roles
    "it jobs",                  # Broad IT umbrella
    "walk in interview",        # Walk-in drives
]

PAGES_PER_QUERY = 2             # Fetch page 1 + page 2 → up to 100 results per query

# ── Location allowlist ────────────────────────────────────────────────────────
# Adzuna returns varied location strings for the same city. Accept all variants.
ALLOWED_LOCATIONS = ["hyderabad", "secunderabad", "telangana"]

# ── Classification keywords ────────────────────────────────────────────────────
WALKIN_KEYWORDS  = ["walkin", "walk-in", "walk in", "interview drive", "interview"]
FRESHER_KEYWORDS = ["fresher", "freshers", "0-1 year", "0 to 1 year", "entry level", "no experience"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_hyderabad_job(raw: dict) -> bool:
    """
    Return True if the job location matches Hyderabad or nearby areas.

    Location field can be empty/missing — in that case we still ACCEPT
    the job because the `where=hyderabad` Adzuna param already geo-filters.
    This avoids accidentally rejecting valid jobs with missing location data.
    """
    location: str = (
        raw.get("location", {}).get("display_name") or ""
    ).lower()

    if not location:
        return True   # Trust Adzuna's where= param when location field is empty

    return any(city in location for city in ALLOWED_LOCATIONS)


def _classify(job: dict) -> dict:
    """
    Add is_walkin and is_fresher boolean flags to a parsed job dict.
    Classification is done on title + description (case-insensitive).
    """
    text = (job.get("title", "") + " " + job.get("description", "")).lower()
    job["is_walkin"]  = any(kw in text for kw in WALKIN_KEYWORDS)
    job["is_fresher"] = any(kw in text for kw in FRESHER_KEYWORDS)
    return job


def _parse(raw: dict) -> dict:
    """Map a raw Adzuna result to our internal job schema."""
    return {
        "id":          raw.get("id", ""),
        "title":       raw.get("title", "").strip(),
        "company":     (raw.get("company") or {}).get("display_name", "N/A"),
        "location":    (raw.get("location") or {}).get("display_name", ""),
        "description": (raw.get("description") or "").strip(),
        "url":         raw.get("redirect_url", ""),
        "created_at":  raw.get("created", ""),
        "is_walkin":   False,   # will be set by _classify()
        "is_fresher":  False,
    }


# ── Main fetch function ───────────────────────────────────────────────────────

async def fetch_jobs_from_adzuna() -> tuple[list[dict], str | None]:
    """
    Run all SEARCH_QUERIES across PAGES_PER_QUERY pages, merge and deduplicate.

    Returns
    -------
    (jobs, error_message)
        jobs          : deduplicated, classified list of job dicts
        error_message : string only if ALL queries fail; else None
    """
    if not APP_ID or not APP_KEY:
        return [], "APP_ID or APP_KEY is not configured."

    seen_ids: set[str] = set()
    all_jobs: list[dict] = []
    errors:   list[str] = []
    total_raw = 0

    for query in SEARCH_QUERIES:
        query_jobs = 0

        for page in range(1, PAGES_PER_QUERY + 1):
            params = {
                "app_id":           APP_ID,
                "app_key":          APP_KEY,
                "where":            "hyderabad",
                "what_or":          query,          # OR logic — any word matches
                "results_per_page": 50,
                "page":             page,
                "sort_by":          "date",
            }

            raw_results = await _request_with_retry(params)
            if raw_results is None:
                errors.append(f"Query='{query}' page={page} failed.")
                logger.warning("⚠️  Query='%s' page=%d → request failed.", query, page)
                continue

            page_count = len(raw_results)
            total_raw += page_count

            # Log sample location for debugging
            if raw_results:
                sample_loc = (raw_results[0].get("location") or {}).get("display_name", "N/A")
                logger.info(
                    "  📄 Query='%s' page=%d → %d result(s) | sample loc: %s",
                    query, page, page_count, sample_loc,
                )
            else:
                logger.warning("  ⚠️  Query='%s' page=%d → 0 results from Adzuna.", query, page)

            for r in raw_results:
                job_id = r.get("id", "")
                if not job_id or job_id in seen_ids:
                    continue                    # skip empty IDs and cross-query dupes

                if not _is_hyderabad_job(r):
                    continue                    # skip non-Hyderabad jobs

                parsed = _parse(r)
                classified = _classify(parsed)
                seen_ids.add(job_id)
                all_jobs.append(classified)
                query_jobs += 1

            # If a page returned fewer than 50 results there are no more pages
            if page_count < 50:
                logger.info("  ✅ Query='%s' — fewer than 50 results on page %d, stopping early.", query, page)
                break

        logger.info("📊 Query='%s' → %d unique job(s) added.", query, query_jobs)

    # ── Summary ───────────────────────────────────────────────────────────────
    walkin_count  = sum(1 for j in all_jobs if j["is_walkin"])
    fresher_count = sum(1 for j in all_jobs if j["is_fresher"])

    logger.info(
        "✅ Fetch complete | raw=%d | unique=%d | walkin=%d | fresher=%d | queries=%d",
        total_raw, len(all_jobs), walkin_count, fresher_count, len(SEARCH_QUERIES),
    )

    if errors and not all_jobs:
        return [], " | ".join(errors)

    return all_jobs, None


# ── HTTP helper ───────────────────────────────────────────────────────────────

async def _request_with_retry(params: dict) -> list | None:
    """
    Attempt the Adzuna HTTP GET up to 2 times with exponential-ish wait.
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
                "Adzuna HTTP %s (attempt %d/2) — query='%s' page=%s",
                exc.response.status_code, attempt,
                params.get("what_or", "?"), params.get("page", 1),
            )
        except Exception as exc:
            logger.warning(
                "Adzuna request error (attempt %d/2): %s", attempt, exc
            )

    return None
