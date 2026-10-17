"""GET /api/status/{site_id}."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..dependencies import get_store
from ..models import StatusResponse

router = APIRouter()


@router.get("/status/{site_id}", response_model=StatusResponse)
async def status(site_id: str):
    meta = get_store().get_site(site_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Unknown site_id. Ingest a URL first.")
    return StatusResponse(
        status=meta.get("status", "failed"),
        pages_crawled=meta.get("pages_crawled", 0),
        pages_found=meta.get("pages_found", 0),
        chunk_count=meta.get("chunk_count", 0),
        error=meta.get("error"),
        error_code=meta.get("error_code"),
    )
