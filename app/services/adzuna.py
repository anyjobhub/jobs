"""
adzuna.py — Upgraded Adzuna API integration service.

Upgrade notes (v4)
------------------
- UPGRADED: `extract_apply_url()` now uses BeautifulSoup to parse the Adzuna
  HTML detail page and extract the real employer application URL from the
  "Apply for this job" button. This is more robust than regex and handles
  HTML entities, varied tag structures, and future markup changes.
- ADDED: URL extraction is applied ONLY to walk-in and fresher jobs to keep
  extra HTTP calls minimal and stay within Render free tier limits.
- FIXED: was using `what` (AND logic) → always 0 results.
  Now uses `what_or` (OR logic) for all queries.
- FIXED: over-strict keyword filter removed. Now accepts ALL Hyderabad
  jobs and classifies them with is_walkin / is_fresher flags instead.
- ADDED: 2-page pagination per query (doubles result volume).
- ADDED: 5 broad search queries covering IT, BPO, fresher, walk-in.
- ADDED: is_walkin + is_fresher classification on each job object.
- IMPROVED: location filter broadened (Hyderabad + Secunderabad + Telangana).
- IMPROVED: detailed per-query, per-page logging.

How apply URL extraction works
-------------------------------
  The Adzuna detail page (adzuna.in/details/{id}) is NOT a redirect.
  It is a full HTML page containing an "Apply for this job" <a> button.

  Step 1 — Fetch the HTML page with httpx (no Selenium needed).
  Step 2 — Parse with BeautifulSoup; find <a data-js="apply">.
  Step 3 — Follow that href (adzuna.in/land/ad/{id}?...) which IS a real
            HTTP redirect → yields the final employer career-page URL.
  Fallback — any failure returns the original Adzuna URL unchanged.

API call budget
---------------
  5 queries × 2 pages = 10 API calls per /trigger-fetch
  With 1-hour cooldown: max 10 × 24 = 240 calls/day
  Adzuna free plan limit: 250 calls/day  ✅  safe

Extra HTTP calls (URL extraction)
-----------------------------------
  Only fires for walkin/fresher jobs (typically 10–30% of results).
  Each extraction = 2 extra HTTP calls (detail page + land/ad redirect).
  1.5 s polite delay between extractions avoids 429 rate-limits.
  Failures silently fall back to Adzuna URL — system never crashes.
"""

import asyncio
import logging

import httpx
from bs4 import BeautifulSoup

from app.config import APP_ID, APP_KEY

logger = logging.getLogger(__name__)

# ── Adzuna URL ────────────────────────────────────────────────────────────────
# IMPORTANT: page number is part of the URL PATH, NOT a query parameter.
# Adzuna endpoint: /v1/api/jobs/in/search/{page}
# Sending page= as a query param returns HTTP 400.
ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/in/search/{page}"

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

# ── Apply URL extraction config ───────────────────────────────────────────────
# Seconds to wait between consecutive URL extractions.
# Adzuna's /land/ad/ endpoint rate-limits aggressive scrapers (HTTP 429).
# 1.5 s gap keeps each fetch cycle polite without meaningfully slowing things.
EXTRACT_DELAY_SECONDS = 1.5

# Browser-like headers reduce the chance of bot-detection on Adzuna pages.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


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


def _should_extract(job: dict) -> bool:
    """
    Return True if this job should have its apply URL extracted.

    Only walk-in and fresher jobs get the full URL extraction — this limits
    extra HTTP calls to high-priority listings and keeps Render free tier fast.
    """
    return bool(job.get("is_walkin") or job.get("is_fresher"))


def _parse(raw: dict) -> dict:
    """Map a raw Adzuna result to our internal job schema.

    Available Adzuna India API fields
    ----------------------------------
    id, title, description, created, redirect_url,
    company.display_name, location.display_name,
    category.label, category.tag,
    salary_min*, salary_max*  (* present only for some non-India listings),
    salary_is_predicted, latitude, longitude

    Note: contract_time, contract_type, and closing_date are NOT provided
    by the Adzuna India endpoint.
    """
    category_obj = raw.get("category") or {}
    return {
        "id":                  raw.get("id", ""),
        "title":               raw.get("title", "").strip(),
        "company":             (raw.get("company") or {}).get("display_name", "N/A"),
        "location":            (raw.get("location") or {}).get("display_name", ""),
        "description":         (raw.get("description") or "").strip(),
        # redirect_url is Adzuna's tracking URL; upgraded to the real employer
        # URL by extract_apply_url() for walk-in and fresher jobs.
        "url":                 raw.get("redirect_url", ""),
        # posted_at = when the job was posted on Adzuna (Adzuna field: "created")
        "posted_at":           raw.get("created", ""),
        # Category from Adzuna (e.g. "IT Jobs", "Engineering Jobs")
        "category":            category_obj.get("label", ""),
        # Salary — present for some jobs, null for most India results
        "salary_min":          raw.get("salary_min"),
        "salary_max":          raw.get("salary_max"),
        "salary_is_predicted": bool(int(raw.get("salary_is_predicted", 0) or 0)),
        "is_walkin":           False,   # set by _classify()
        "is_fresher":          False,
    }


# ── Apply URL extraction ──────────────────────────────────────────────────────

async def extract_apply_url(adzuna_url: str) -> str:
    """
    Extract the real employer job URL from an Adzuna detail page.

    Why not follow_redirects?
    -------------------------
    The Adzuna URL (adzuna.in/details/{id}) is NOT an HTTP redirect.
    It is a full rendered HTML page. The actual employer link only
    exists inside the HTML as the href of the "Apply for this job" button.
    follow_redirects=True would stay on the Adzuna detail page (HTTP 200).

    Two-step strategy
    -----------------
    Step 1 — Fetch adzuna.in/details/{id} HTML with httpx.
    Step 2 — Parse HTML with BeautifulSoup; locate <a data-js="apply">.
             This <a> tag's href points to adzuna.in/land/ad/{id}?... which
             IS a real HTTP 30x redirect to the employer's career page.
    Step 3 — Follow the /land/ad/ URL with follow_redirects=True; capture
             the final URL (e.g. wellsfargojobs.com/...).

    Fallback
    --------
    Any failure at any step (network error, 429, missing button, etc.)
    returns the original adzuna_url unchanged. The system never crashes.

    Parameters
    ----------
    adzuna_url : str
        The Adzuna detail URL from the API's redirect_url field.
        e.g. https://www.adzuna.in/details/12345?utm_medium=api&...

    Returns
    -------
    str
        Final employer URL if successfully extracted, else adzuna_url.
    """
    if not adzuna_url:
        return adzuna_url

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=10.0,
            headers=_BROWSER_HEADERS,
        ) as client:

            # ── Step 1: Fetch the Adzuna detail page HTML ─────────────────────
            detail_resp = await client.get(adzuna_url)
            detail_resp.raise_for_status()

            # ── Step 2: Parse HTML — find the Apply button ───────────────────
            soup = BeautifulSoup(detail_resp.text, "html.parser")

            # Primary selector: <a data-js="apply"> is the most stable attribute
            # on the Adzuna apply button across all job types.
            apply_tag = soup.find("a", attrs={"data-js": "apply"})

            # Fallback selectors in order of reliability
            if apply_tag is None:
                # rel="nofollow" + text contains "Apply"
                for tag in soup.find_all("a", rel="nofollow"):
                    if "apply" in tag.get_text(strip=True).lower():
                        apply_tag = tag
                        break

            if apply_tag is None:
                logger.debug(
                    "extract_apply_url: no Apply button found on %s",
                    adzuna_url,
                )
                return adzuna_url  # fallback: button not present in HTML

            # href may contain HTML-encoded ampersands (&amp;) — decode them
            land_url = apply_tag.get("href", "").replace("&amp;", "&")

            if not land_url or "adzuna" not in land_url:
                # Unexpected href — not the pattern we're looking for
                logger.debug(
                    "extract_apply_url: unexpected href %r for %s",
                    land_url, adzuna_url,
                )
                return adzuna_url

            # ── Step 3: Follow /land/ad/ → employer career page ───────────────
            # This endpoint returns an HTTP 30x redirect to the employer site.
            # follow_redirects=True (already set on client) will chase it.
            employer_resp = await client.get(land_url)
            final_url = str(employer_resp.url)

            # Sanity check: if we still ended up on an Adzuna domain, bail out
            if "adzuna" in final_url.lower():
                logger.debug(
                    "extract_apply_url: still on Adzuna after redirect — %s",
                    final_url,
                )
                return adzuna_url

            logger.info(
                "✅ apply URL extracted | %s → %s",
                adzuna_url.split("?")[0],   # strip UTM params for clean logs
                final_url[:80],
            )
            return final_url

    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 429:
            logger.warning(
                "extract_apply_url: rate-limited (429) for %s — "
                "keeping Adzuna URL (will retry on next fetch cycle)",
                adzuna_url,
            )
        else:
            logger.warning(
                "extract_apply_url: HTTP %d for %s — keeping Adzuna URL",
                status, adzuna_url,
            )
    except httpx.TimeoutException:
        logger.warning(
            "extract_apply_url: timed out for %s — keeping Adzuna URL",
            adzuna_url,
        )
    except Exception as exc:
        logger.warning(
            "extract_apply_url: unexpected error for %s: %s — keeping Adzuna URL",
            adzuna_url, exc,
        )

    return adzuna_url   # safe fallback on any failure


# ── Main fetch function ───────────────────────────────────────────────────────

async def fetch_jobs_from_adzuna() -> tuple[list[dict], str | None]:
    """
    Run all SEARCH_QUERIES across PAGES_PER_QUERY pages, merge and deduplicate.
    For walk-in and fresher jobs, replace the Adzuna redirect URL with the
    actual employer application URL extracted from the job detail page HTML.

    Key implementation notes
    ------------------------
    - Page number goes in the URL PATH: /search/{page} — NOT as a query param.
      Sending page= as a query param causes Adzuna to return HTTP 400.
    - Uses what_or for OR logic. Falls back to what= on 400 responses in case
      the India API endpoint does not support what_or.
    - URL extraction is applied ONLY to priority jobs (walk-in / fresher)
      to minimise extra HTTP calls on the Render free tier.

    Returns
    -------
    (jobs, error_message)
        jobs          : deduplicated, classified list of job dicts
        error_message : string only if ALL queries fail; else None
    """
    if not APP_ID or not APP_KEY:
        return [], "APP_ID or APP_KEY is not configured."

    seen_ids:      set[str]   = set()
    all_jobs:      list[dict] = []
    errors:        list[str]  = []
    total_raw      = 0
    extracted_count = 0

    for query in SEARCH_QUERIES:
        query_jobs = 0

        for page in range(1, PAGES_PER_QUERY + 1):
            # Page number goes in the URL path, not as a query param
            url = ADZUNA_URL.format(page=page)

            params = {
                "app_id":           APP_ID,
                "app_key":          APP_KEY,
                "where":            "hyderabad",
                "what_or":          query,      # OR logic: any word matches
                "results_per_page": 50,
                "sort_by":          "date",
                # NOTE: no 'page' key here — page is in the URL path above
            }

            raw_results = await _request_with_retry(url, params, query, page)
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

                # Parse → classify (sets is_walkin / is_fresher flags)
                parsed = _parse(r)
                classified = _classify(parsed)

                # ── Apply URL extraction (priority jobs only) ──────────────────
                # Walk-in and fresher jobs get their Adzuna redirect URL replaced
                # with the real employer URL extracted via BeautifulSoup.
                # All other jobs keep the Adzuna URL to avoid excess HTTP calls.
                if _should_extract(classified):
                    original_url = classified["url"]
                    real_url = await extract_apply_url(original_url)
                    classified["url"] = real_url
                    if real_url != original_url:
                        extracted_count += 1
                    # Brief pause between extractions to stay within rate limits
                    await asyncio.sleep(EXTRACT_DELAY_SECONDS)

                seen_ids.add(job_id)
                all_jobs.append(classified)
                query_jobs += 1

            # Stop early if fewer than 50 results (no more pages)
            if page_count < 50:
                logger.info(
                    "  ✅ Query='%s' page=%d — only %d results, no more pages.",
                    query, page, page_count,
                )
                break

        logger.info("📊 Query='%s' → %d unique job(s) added.", query, query_jobs)

    # ── Summary ───────────────────────────────────────────────────────────────
    walkin_count  = sum(1 for j in all_jobs if j["is_walkin"])
    fresher_count = sum(1 for j in all_jobs if j["is_fresher"])

    logger.info(
        "✅ Fetch complete | raw=%d | unique=%d | walkin=%d | fresher=%d "
        "| apply_urls_extracted=%d | queries=%d",
        total_raw, len(all_jobs), walkin_count, fresher_count,
        extracted_count, len(SEARCH_QUERIES),
    )

    if errors and not all_jobs:
        return [], " | ".join(errors)

    return all_jobs, None


# ── HTTP helper ───────────────────────────────────────────────────────────────

async def _request_with_retry(url: str, params: dict, query: str, page: int) -> list | None:
    """
    Attempt the Adzuna HTTP GET up to 2 times.

    Strategy
    --------
    - First tries with what_or= (OR logic).
    - If Adzuna returns 400 (unsupported param), automatically retries
      using what= (AND logic, but still broad for single-word queries).
    - Returns list of raw results, or None on total failure.
    """
    # Try what_or first; fall back to what= on 400
    param_variants = [
        {**params, "what_or": params.get("what_or")},      # attempt 1: OR logic
        {**{k: v for k, v in params.items() if k != "what_or"}, "what": params.get("what_or")},  # attempt 2: AND
    ]

    for attempt, p in enumerate(param_variants, start=1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, params=p)
                if resp.status_code == 400 and attempt == 1:
                    # what_or not accepted — retry with what=
                    logger.warning(
                        "Adzuna 400 with what_or= on query='%s' page=%d — retrying with what=",
                        query, page,
                    )
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data.get("results", [])
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Adzuna HTTP %s (attempt %d/2) — query='%s' page=%d",
                exc.response.status_code, attempt, query, page,
            )
        except Exception as exc:
            logger.warning(
                "Adzuna request error (attempt %d/2) — query='%s' page=%d: %s",
                attempt, query, page, exc,
            )

    return None
