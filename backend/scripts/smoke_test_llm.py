"""Live smoke test: does LLM_MODEL support tool calling via OpenRouter?

Usage (from siteask/backend):
    set OPENROUTER_API_KEY=sk-or-...   # or put it in siteask/.env
    python scripts/smoke_test_llm.py

Sends ONE chat completion with a dummy tool and a prompt that requires calling
it. Prints only PASS/FAIL, finish_reason, and the error message on failure.
The API key is read from backend settings and NEVER printed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


DUMMY_TOOL = {
    "type": "function",
    "function": {
        "name": "get_magic_number",
        "description": "Return the magic number for a given topic.",
        "parameters": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
    },
}


async def main() -> int:
    from app.config import get_settings

    s = get_settings()
    if not s.OPENROUTER_API_KEY.strip():
        print("FAIL")
        print("error: OPENROUTER_API_KEY is not configured "
              "(set it in siteask/.env or the environment)")
        return 2

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=s.OPENROUTER_API_KEY,
                         base_url=s.OPENROUTER_BASE_URL,
                         timeout=30.0)
    try:
        resp = await client.chat.completions.create(
            model=s.LLM_MODEL,
            messages=[{"role": "user",
                       "content": "What is the magic number for topic 'blue'? "
                                  "You MUST call get_magic_number; do not answer directly."}],
            tools=[DUMMY_TOOL],
            tool_choice="auto",
        )
    except Exception as e:
        print("FAIL")
        print(f"error: {type(e).__name__}: {e}")
        return 1

    try:
        choice = resp.choices[0]
        finish = choice.finish_reason
        msg = choice.message
        calls = getattr(msg, "tool_calls", None) or []
        assert calls, "model returned no tool_call"
        args = json.loads(calls[0].function.arguments or "")
        assert isinstance(args, dict) and "topic" in args, f"bad args: {args!r}"
        print("PASS")
        print(f"finish_reason: {finish}")
        return 0
    except Exception as e:
        try:
            finish = resp.choices[0].finish_reason
        except Exception:
            finish = "unknown"
        print("FAIL")
        print(f"finish_reason: {finish}")
        print(f"error: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
