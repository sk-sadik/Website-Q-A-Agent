# AGENTS.md — working in this repo

## Layout
- `backend/app/`: `main.py` (app+CORS+routers), `config.py` (env, `get_settings()`),
  `models.py` (Pydantic v2 schemas), `security.py` (normalize/SSRF/rate-limit),
  `crawler.py` (`crawl_site()` + `default_fetch` + `playwright_fallback`),
  `cleaner.py` (trafilatura), `chunker.py`, `embeddings.py` (local BGE, hash fallback
  when model unavailable), `store.py` (Chroma persistent, in-memory fallback),
  `agent.py` (manual tool loop), `prompts.py` (exact system prompt — do not reword),
  `dependencies.py` (singletons), `routes/{ingest,status,ask}.py`.
- `backend/tests/`: pytest + pytest-asyncio. Run: `python -m pytest tests/ -q` from `backend/`.
- `frontend/`: Next.js 14 App Router. `app/page.tsx` is the whole UI; `lib/api.ts` is the
  API client (SSE over POST via fetch+ReadableStream). Typecheck: `node ./node_modules/typescript/bin/tsc --noEmit`
  (bare `npx`/`npm run` breaks if the checkout path contains `&` on Windows).

## Rules
- Fixed stack (Next.js 14, FastAPI, httpx+trafilatura+Playwright, bge-small-en-v1.5,
  Chroma, OpenRouter, SSE, Docker Compose). Do not substitute.
- `OPENROUTER_API_KEY` must stay server-side; frontend only uses `NEXT_PUBLIC_API_URL`.
- Never commit `.env` (only `.env.example`). Never commit `chroma_data/`.
- Crawler changes must keep: same-site only, robots respect, manual redirects (max 5)
  with per-hop SSRF re-check, 3MB cap, HTML-only, polite delay, per-page try/except.
- Agent changes must keep: max 4 tool rounds, non-streaming tool rounds, streamed final
  answer only, `<retrieved_content>` delimiters, grounding fallback search, history ≤ 6.
- Keep `SYSTEM_PROMPT` byte-identical to the spec.
- Verify by running: backend tests, frontend typecheck+build, and (with a key) a live
  ingest of `https://example.com` → `ready` → streamed answer with sources.
