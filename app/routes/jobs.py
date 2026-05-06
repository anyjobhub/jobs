"""
routes/jobs.py — Read-only endpoints for job listings and stats.

Endpoints
---------
GET /           — service info (root)
GET /health     — health check (keeps UptimeRobot happy & wakes Render)
GET /jobs       — latest 50 jobs
GET /walkins    — walk-in jobs only
GET /freshers   — fresher jobs only
GET /stats      — aggregate counts
"""
from fastapi import APIRouter

from app.database import get_jobs, get_walkin_jobs, get_fresher_jobs, get_stats

router = APIRouter(tags=["Jobs"])


@router.get("/jobs", summary="Latest jobs (max 50)")
async def list_jobs():
    jobs = await get_jobs(limit=50)
    return {"count": len(jobs), "jobs": jobs}


@router.get("/walkins", summary="Walk-in jobs only")
async def list_walkin_jobs():
    jobs = await get_walkin_jobs(limit=50)
    return {"count": len(jobs), "jobs": jobs}


@router.get("/freshers", summary="Fresher jobs only")
async def list_fresher_jobs():
    jobs = await get_fresher_jobs(limit=50)
    return {"count": len(jobs), "jobs": jobs}


@router.get("/stats", summary="Aggregate job counts")
async def job_stats():
    return await get_stats()
