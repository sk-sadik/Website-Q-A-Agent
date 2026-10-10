# AGENTS.md — working in this repo

## Layout
- `backend/app/`: `main.py` (app+CORS+routers), `config.py` (env, `get_settings()` —
  model ID centralized in `LLM_MODEL`, default `apodex/apodex-1.1-mini:free`),
  `llm.py` (shared AsyncOpenAI client + error classification), `models.py`,
  `security.py`, `crawler.py`, `cleaner.py`, `chunker.py`, `embeddings.py`,
  `store.py`, `agent.py` (manual tool loop, reasoning stripping, empty-answer guard),
  `prompts.py`, `dependencies.py`, `routes/{ingest,status,ask}.py`.
- `backend/scripts/smoke_test_llm.py`: live tool-calling check (needs real key).
- `backend/tests/`: pytest + pytest-asyncio (`test_llm_apodex.py` = mocked model tests).
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
