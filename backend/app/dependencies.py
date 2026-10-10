"""Shared singletons: store, rate limiter, background crawl tasks."""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from .config import get_settings
from .security import RateLimiter
from .store import ChromaStore

_store: ChromaStore | None = None
_limiter: RateLimiter | None = None
_tasks: Dict[str, Dict[str, Any]] = {}
_task_locks: Dict[str, asyncio.Lock] = {}


def get_store() -> ChromaStore:
    global _store
    if _store is None:
        _store = ChromaStore(persist_directory=get_settings().CHROMA_DIR)
    return _store


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(per_minute=get_settings().RATE_LIMIT_PER_MINUTE)
    return _limiter


def task_lock(site_id: str) -> asyncio.Lock:
    if site_id not in _task_locks:
        _task_locks[site_id] = asyncio.Lock()
    return _task_locks[site_id]


def tasks() -> Dict[str, Dict[str, Any]]:
    return _tasks
