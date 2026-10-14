# SiteAsk

Enter any website URL → SiteAsk crawls and indexes the site → ask questions and get
streamed answers grounded **only** in the site's real content, with source citations.

- **Frontend:** Next.js 14 (App Router, TypeScript), Tailwind CSS, shadcn-style UI, light/dark mode
- **Backend:** FastAPI + httpx/trafilatura crawler (Playwright fallback), local
  `BAAI/bge-small-en-v1.5` embeddings, ChromaDB, OpenRouter tool-calling agent, SSE streaming
- **Run:** `docker compose up --build`

## Run in under 5 minutes

1. **Prereqs:** Docker + Docker Compose, and an [OpenRouter](https://openrouter.ai) API key
   (create one at https://openrouter.ai/keys — the free
   `apodex/apodex-1.1-mini:free` model works without credits).
2. **Configure:**
   ```bash
   cd siteask
   cp .env.example .env
   # edit .env and set OPENROUTER_API_KEY=sk-or-...
   ```
   The key lives **only** in `siteask/.env` (loaded by Docker Compose into the
   backend container). It is never baked into images, never sent to the browser,
   and never logged. `.env` is gitignored — only `.env.example` (empty key) is committed.
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
cd siteask/backend && python -m pytest tests/ -q   # 56 tests, all mocked (no key needed)
cd siteask/frontend && npm run typecheck && npm run build
```

Full local run with real ML deps (no Docker needed, ~1 GB downloads: torch,
BGE model, Chromium):
```bash
python -m venv .venv-real && .venv-real/Scripts/python -m pip install -r siteask/backend/requirements.txt
.venv-real/Scripts/python -m playwright install chromium
cd siteask/backend && ../../.venv-real/Scripts/python -m uvicorn app.main:app
```
Keep the venv outside the repo. Gotchas: the single uvicorn worker blocks
while CPU-embedding a big crawl, so `/api/status` may time out until indexing
finishes — just wait and retry; first BGE load downloads ~130 MB from HF Hub.

LLM smoke test (needs a real key — checks the configured model supports tool calling):
```bash
cd siteask/backend
# set OPENROUTER_API_KEY in siteask/.env or the environment first
python scripts/smoke_test_llm.py   # prints PASS (+finish_reason) or FAIL (+error)
```

## Model: Apodex 1.1 Mini (free)

Answer generation and agent reasoning run on `apodex/apodex-1.1-mini:free` via
OpenRouter's OpenAI-compatible API. The model ID is centralized in
`backend/app/config.py` (`Settings.LLM_MODEL` — the `:free` suffix is required;
the paid variant without it must not be used) and a single shared `AsyncOpenAI`
client is reused for all requests (`backend/app/llm.py`).

Apodex is reasoning-first: reasoning fields and `<think>` blocks are stripped and
**never** streamed to the browser — only final answer tokens become `token` events.
An empty generation is retried once, then surfaces a friendly error (never an
empty answer).

**Switching models:** set `LLM_MODEL` in `siteask/.env` to any OpenRouter
tool-calling model (e.g. `openai/gpt-oss-120b:free` or `poolside/laguna-m.1:free`
as free fallbacks) and restart. No code changes needed.

**Free-tier limitations:** the `:free` endpoint is rate-limited (observed:
`free-models-per-day`, limit 50/day per key, plus shared-pool per-minute 429s).
One question can use up to 5 LLM calls (4 tool rounds + final answer), so pace
live testing ~30s apart. On a 429 the API returns
"The free model's limit was reached. Please try again later." Transient
502/timeout errors are retried once; 400/401/402/403/404 are never retried and
never leak secrets or stack traces. Free models churn: on 2026-10-14
`apodex/apodex-1.1-mini:free` briefly had zero OpenRouter endpoints, so live
verification ran via `LLM_MODEL` override (`cohere/north-mini-code:free`) with
the repo default untouched — check
`https://openrouter.ai/api/v1/models` for current `:free` models with
`"tools"` in `supported_parameters` if Apodex is unavailable.

**Deploying the secret:** set `OPENROUTER_API_KEY` in your host's environment or
secrets manager (compose reads `siteask/.env`). Do not put the key in
`.env.example`, Dockerfiles, frontend code, or git.

## Configuration (.env)

| Var | Default | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | — | **required**, server refuses to start without it |
| `LLM_MODEL` | `apodex/apodex-1.1-mini:free` | OpenRouter tool-calling model; keep the `:free` suffix for the free variant |
| `OPENROUTER_TIMEOUT_S` | `60` | per-request LLM timeout |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | local, no API key; pre-downloaded in Docker build |
| `MAX_PAGES` / `MAX_DEPTH` | `30` / `2` | crawl budget |
| `CACHE_TTL_HOURS` | `24` | re-ingest within TTL returns instantly |
| `RATE_LIMIT_PER_MINUTE` | `20` | per-IP, on `/ingest` and `/ask` |
| `CORS_ORIGINS` | `http://localhost:3000` | |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | baked into frontend at build |
