"""API: /ingest, /status, /ask SSE format, rate limiting."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import app.dependencies as deps
import app.routes.ask as ask_mod
import app.routes.ingest as ingest_mod
from app.main import app
from app.security import RateLimiter
from app.store import ChromaStore


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    store = ChromaStore(persist_directory=str(tmp_path / "chroma"))
    monkeypatch.setattr(deps, "_store", store)
    monkeypatch.setattr(deps, "_limiter", RateLimiter(per_minute=100))
    return store


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ingest_rejects_blocked(client, tmp_store):
    r = client.post("/api/ingest", json={"url": "http://127.0.0.1/"})
    assert r.status_code == 400


def test_ingest_queues_and_status_flows(client, tmp_store, monkeypatch):
    async def fake_crawl(url, max_pages=30, max_depth=2, **kw):
        return {"status": "done", "pages_crawled": 1, "pages_found": 1,
                "chunks": [{"url": url, "title": "T", "chunk_index": 0,
                            "text": ("hello world " * 60)}], "error": None}

    monkeypatch.setattr(ingest_mod, "crawl_site", fake_crawl)
    r = client.post("/api/ingest", json={"url": "https://example.com"})
    assert r.status_code == 200
    site_id = r.json()["site_id"]
    # background task runs; poll until ready
    for _ in range(100):
        s = client.get(f"/api/status/{site_id}").json()
        if s["status"] in ("ready", "failed"):
            break
        import time
        time.sleep(0.05)
    assert s["status"] == "ready"
    assert s["chunk_count"] == 1


def test_ingest_cached_returns_instantly(client, tmp_store, monkeypatch):
    import time as _t
    tmp_store.set_site("deadbeef12345678",
                       {"url": "https://example.com/", "status": "ready", "pages_crawled": 1,
                        "pages_found": 1, "chunk_count": 1, "crawled_at": _t.time(), "error": None})
    called = []

    async def fake_crawl(*a, **k):
        called.append(1)
        return {"status": "done", "pages_crawled": 0, "pages_found": 0, "chunks": [], "error": None}

    monkeypatch.setattr(ingest_mod, "crawl_site", fake_crawl)
    # compute real site id for https://example.com/
    from app.store import site_id_from_url
    sid = site_id_from_url("https://example.com/")
    tmp_store.set_site(sid, {"url": "https://example.com/", "status": "ready", "pages_crawled": 1,
                             "pages_found": 1, "chunk_count": 1, "crawled_at": _t.time(), "error": None})
    r = client.post("/api/ingest", json={"url": "https://example.com"})
    assert r.json()["status"] == "ready"
    assert not called


def test_ask_requires_ready(client, tmp_store):
    r = client.post("/api/ask", json={"site_id": "nope", "question": "hi"})
    assert r.status_code == 409


def test_ask_sse_format(client, tmp_store, monkeypatch):
    import time as _t
    from app.store import site_id_from_url
    sid = site_id_from_url("https://example.com/")
    tmp_store.set_site(sid, {"url": "https://example.com/", "status": "ready", "pages_crawled": 1,
                             "pages_found": 1, "chunk_count": 1, "crawled_at": _t.time(), "error": None})

    async def fake_stream(self, question, history=None):
        yield {"type": "status", "message": "Searching site..."}
        yield {"type": "token", "text": "Hello "}
        yield {"type": "token", "text": "world"}
        yield {"type": "sources", "sources": [{"url": "https://example.com/", "title": "T"}]}
        yield {"type": "done"}

    monkeypatch.setattr(ask_mod.SiteAskAgent, "answer_stream", fake_stream)
    r = client.post("/api/ask", json={"site_id": sid, "question": "hi"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    types = [e["type"] for e in events]
    assert types == ["status", "status", "token", "token", "sources", "done"]
    assert events[2]["text"] == "Hello "


def test_rate_limit_ask(client, tmp_store, monkeypatch):
    import time as _t
    monkeypatch.setattr(deps, "_limiter", RateLimiter(per_minute=1))
    from app.store import site_id_from_url
    sid = site_id_from_url("https://example.com/")
    tmp_store.set_site(sid, {"url": "https://example.com/", "status": "ready", "pages_crawled": 1,
                             "pages_found": 1, "chunk_count": 1, "crawled_at": _t.time(), "error": None})

    async def fake_stream(self, question, history=None):
        yield {"type": "done"}

    monkeypatch.setattr(ask_mod.SiteAskAgent, "answer_stream", fake_stream)
    assert client.post("/api/ask", json={"site_id": sid, "question": "a"}).status_code == 200
    assert client.post("/api/ask", json={"site_id": sid, "question": "b"}).status_code == 429
