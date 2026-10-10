"""FastAPI app: CORS, routers, startup validation, rate limiting on ingest/ask."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .dependencies import get_limiter
from .routes import ask as ask_routes
from .routes import ingest as ingest_routes
from .routes import status as status_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("siteask")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    try:
        s.validate_startup()
    except RuntimeError as e:
        logger.error(str(e))
        raise
    # warm-up check only; embeddings load lazily on first crawl
    logger.info("SiteAsk backend starting (model=%s)", s.LLM_MODEL)
    yield
    logger.info("SiteAsk backend shutting down")


app = FastAPI(title="SiteAsk", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _rate_limit_ingest(request: Request, call_next):
    if request.url.path == "/api/ingest" and request.method == "POST":
        ip = request.client.host if request.client else "unknown"
        if not get_limiter().check(ip):
            return JSONResponse(status_code=429,
                                content={"detail": "Rate limit exceeded. Try again in a minute."})
    return await call_next(request)


@app.api_route("/api/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/health")
async def health_root():
    return {"status": "ok", "version": "1.0.0"}


app.include_router(ingest_routes.router, prefix="/api", tags=["ingest"])
app.include_router(status_routes.router, prefix="/api", tags=["status"])
app.include_router(ask_routes.router, prefix="/api", tags=["ask"])


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    logger.exception("unhandled error")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
