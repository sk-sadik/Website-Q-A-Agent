# SiteAsk

Enter any website URL → SiteAsk crawls and indexes the site → ask questions and get
streamed answers grounded **only** in the site's real content, with source citations.

- **Frontend:** Next.js 14 (App Router, TypeScript), Tailwind CSS, shadcn-style UI, light/dark mode
- **Backend:** FastAPI + httpx/trafilatura crawler (Playwright fallback), local
  `BAAI/bge-small-en-v1.5` embeddings, ChromaDB, OpenRouter tool-calling agent, SSE streaming
- **Run:** `docker compose up --build`

## Run in under 5 minutes

1. **Prereqs:** Docker + Docker Compose, and an [OpenRouter](https://openrouter.ai) API key
   (any tool-calling model, e.g. `anthropic/claude-sonnet-4.5`).
2. **Configure:**
   ```bash
   cd siteask
   cp .env.example .env
   # edit .env and set OPENROUTER_API_KEY=sk-or-...
   ```
3. **Start:**
   ```bash
   docker compose up --build
   ```
4. **Use:** open http://localhost:3000 → paste a URL (e.g. `https://example.com`) →
   watch crawl progress → ask questions. API health: http://localhost:8000/api/health.

That's it. Chroma data persists in the `chroma-data` Docker volume; re-submitting a URL
within 24h returns instantly from cache (use **Refresh site** to re-crawl).

## How it works

```
URL → /api/ingest → background BFS crawl (robots.txt, sitemap, same-domain,
      SSRF guard every hop, Playwright fallback for JS pages)
    → trafilatura main-content extract → 600-token chunks (80 overlap)
    → local BGE embeddings → Chroma collection per site (site_id = sha256(origin)[:16])
    → /api/status/{site_id} polled every 1.5s
    → /api/ask streams SSE: status → token* → sources → done
```

The agent (`backend/app/agent.py`) runs a manual tool loop (max 4 rounds):
`search_site(query)` → top-6 chunks, `fetch_page(url)` → same-domain page text.
Tool rounds are non-streaming; only the final answer streams. If the model skips
tools on round one, the agent searches anyway so answers stay grounded. Retrieved
content is wrapped in `<retrieved_content>` delimiters and treated as untrusted
(prompt-injection text in pages cannot hijack the agent).

## API

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/ingest` | `{url, refresh?}` | `{site_id, status}` (immediate; `ready` if cached) |
| GET | `/api/status/{site_id}` | — | `{status, pages_crawled, pages_found, chunk_count, error?}` |
| POST | `/api/ask` | `{site_id, question, history?}` | SSE: `status` / `token` / `sources` / `done` / `error` |
| GET | `/api/health` | — | `{status: "ok"}` |

`409` from `/api/ask` = site not ready yet. `429` = per-IP rate limit. `400` from
`/api/ingest` = invalid/blocked URL (SSRF guard blocks localhost, private,
link-local, multicast and reserved ranges on every redirect hop).

## Local development (no Docker)

Backend:
```bash
cd siteask/backend
pip install -r requirements.txt
export OPENROUTER_API_KEY=sk-or-...   # required; server fails fast without it
uvicorn app.main:app --reload
```

Frontend:
```bash
cd siteask/frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Tests:
```bash
cd siteask/backend && python -m pytest tests/ -q   # 28 tests
cd siteask/frontend && npm run typecheck && npm run build
```

## Configuration (.env)

| Var | Default | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | — | **required**, server refuses to start without it |
| `LLM_MODEL` | `anthropic/claude-sonnet-4.5` | any OpenRouter tool-calling model |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | local, no API key; pre-downloaded in Docker build |
| `MAX_PAGES` / `MAX_DEPTH` | `30` / `2` | crawl budget |
| `CACHE_TTL_HOURS` | `24` | re-ingest within TTL returns instantly |
| `RATE_LIMIT_PER_MINUTE` | `20` | per-IP, on `/ingest` and `/ask` |
| `CORS_ORIGINS` | `http://localhost:3000` | |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | baked into frontend at build |
