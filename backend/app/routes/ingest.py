"""POST /api/ingest — validate, cache-check, launch background crawl."""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ..config import get_settings
from ..crawler import crawl_site
from ..dependencies import get_store, task_lock, tasks
from ..models import IngestRequest, IngestResponse
from ..security import normalize_url, validate_url_security
from ..store import site_id_from_url

logger = logging.getLogger(__name__)
router = APIRouter()


async def _run_crawl(site_id: str, url: str) -> None:
    store = get_store()
    s = get_settings()
    meta = store.get_site(site_id) or {}
    meta.update({"status": "crawling", "pages_crawled": 0, "pages_found": 1,
                 "chunk_count": 0, "error": None, "url": url})
    store.set_site(site_id, meta)
    try:
        result = await crawl_site(url, max_pages=s.MAX_PAGES, max_depth=s.MAX_DEPTH)
        if result["status"] == "done":
            meta.update({"status": "indexing"})
            store.set_site(site_id, meta)
            store.clear_site(site_id)
            store.add_chunks(site_id, result["chunks"])
            meta.update({"status": "ready", "pages_crawled": result["pages_crawled"],
                         "pages_found": result["pages_found"],
                         "chunk_count": store.count(site_id), "crawled_at": time.time(),
                         "error": None})
        else:
            meta.update({"status": "failed", "pages_crawled": result.get("pages_crawled", 0),
                         "pages_found": result.get("pages_found", 0),
                         "error": result.get("error") or "Crawl failed"})
        store.set_site(site_id, meta)
    except Exception as e:
        logger.exception("crawl crashed")
        meta.update({"status": "failed", "error": f"Crawl crashed: {e}"})
        store.set_site(site_id, meta)
    finally:
        tasks().pop(site_id, None)


@router.post("/ingest", response_model=IngestResponse)
async def ingest(body: IngestRequest, request: Request, background: BackgroundTasks):
    s = get_settings()
    ok, reason = validate_url_security(body.url)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    url = normalize_url(body.url)
    site_id = site_id_from_url(url)
    store = get_store()

    if not body.refresh and store.is_cache_valid(site_id, s.CACHE_TTL_HOURS):
        return IngestResponse(site_id=site_id, status="ready")

    existing = store.get_site(site_id)
    if existing and existing.get("status") in ("queued", "crawling", "indexing") and not body.refresh:
        return IngestResponse(site_id=site_id, status=existing["status"])
    if site_id in tasks() and not body.refresh:
        return IngestResponse(site_id=site_id, status="crawling")

    store.set_site(site_id, {"url": url, "status": "queued", "pages_crawled": 0,
                             "pages_found": 1, "chunk_count": 0,
                             "crawled_at": (existing or {}).get("crawled_at", 0),
                             "error": None})

    async def runner():
        async with task_lock(site_id):
            await _run_crawl(site_id, url)

    # schedule without awaiting
    task = asyncio.create_task(runner())
    tasks()[site_id] = {"task": task, "url": url}
    background.add_task(lambda: None)  # keep BackgroundTasks param meaningful
    return IngestResponse(site_id=site_id, status="queued")
