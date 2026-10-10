"""POST /api/ask — SSE stream of agent events."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..agent import SiteAskAgent
from ..config import get_settings
from ..dependencies import get_limiter, get_store
from ..models import AskRequest

logger = logging.getLogger(__name__)
router = APIRouter()


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


@router.post("/ask")
async def ask(body: AskRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not get_limiter().check(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in a minute.")
    store = get_store()
    meta = store.get_site(body.site_id)
    if not meta or meta.get("status") != "ready":
        raise HTTPException(status_code=409,
                            detail="Site is not ready yet. Wait for crawling/indexing to finish.")
    s = get_settings()
    agent = SiteAskAgent(store=store, site_id=body.site_id, site_url=meta.get("url", ""),
                         api_key=s.OPENROUTER_API_KEY, model=s.LLM_MODEL,
                         base_url=s.OPENROUTER_BASE_URL)
    history = [{"role": t.role, "content": t.content} for t in body.history]

    async def gen():
        try:
            yield _sse({"type": "status", "message": "Thinking..."})
            async for ev in agent.answer_stream(body.question, history):
                yield _sse(ev)
                if await request.is_disconnected():
                    break
        except Exception as e:
            logger.exception("ask stream failed")
            yield _sse({"type": "error", "message": "Something went wrong. Please try again."})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})
