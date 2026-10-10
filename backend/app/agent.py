"""Tool-calling agent loop (implemented manually on top of OpenAI SDK + OpenRouter)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator, Callable, Dict, List, Optional
from urllib.parse import urlparse

from .prompts import SYSTEM_PROMPT
from .security import has_credentials, hostname_is_blocked, is_valid_scheme, normalize_url

logger = logging.getLogger(__name__)

MAX_ROUNDS = 4
FETCH_TRUNCATE = 6000
NOT_FOUND = "I couldn't find this on the website"

TOOLS = [
    {"type": "function", "function": {
        "name": "search_site",
        "description": "Search indexed pages of this site for content relevant to the query.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search query"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_page",
        "description": "Fetch one page from the SAME site and return its cleaned text (max ~6000 chars).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Full page URL"}}, "required": ["url"]}}},
]


class SiteAskAgent:
    def __init__(self, store, site_id: str, site_url: str, api_key: str,
                 model: str, base_url: str = "https://openrouter.ai/api/v1",
                 client_factory: Optional[Callable[[], Any]] = None):
        self.store = store
        self.site_id = site_id
        self.site_url = site_url
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self._client_factory = client_factory
        self._client = None

    def _client_or_create(self):
        if self._client_factory is not None:
            return self._client_factory()
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    # ---- tools ----
    def search_site(self, query: str) -> List[Dict[str, Any]]:
        try:
            return self.store.similarity_search(self.site_id, query, n_results=6)
        except Exception as e:
            logger.warning("search_site failed: %s", e)
            return []

    async def fetch_page(self, url: str, fetch_fn=None) -> str:
        """Fetch + clean one page. Same-domain + SSRF enforced. Returns text or error string."""
        url = normalize_url(url)
        if not is_valid_scheme(url) or has_credentials(url):
            return "fetch_page error: URL must be http(s) without credentials."
        if hostname_is_blocked(urlparse(url).hostname or ""):
            return "fetch_page error: blocked by SSRF guard."
        # same-domain only
        try:
            a = (urlparse(url).hostname or "").lower().lstrip("www.")
            b = (urlparse(self.site_url).hostname or "").lower().lstrip("www.")
            if not a or not b or not (a == b or a.endswith("." + b) or b.endswith("." + a)):
                return "fetch_page error: URL is not on the same site."
        except Exception:
            return "fetch_page error: invalid URL."
        try:
            if fetch_fn is None:
                from .crawler import default_fetch

                fetch_fn = default_fetch
            res = await fetch_fn(url)
            if res is None:
                return "fetch_page error: could not fetch page."
            _st, final, _ct, html = res
            from .cleaner import extract_main_content

            ex = extract_main_content(html, final)
            if ex is None:
                return "fetch_page error: no usable text on page."
            title, text = ex
            return f"URL: {final}\nTitle: {title}\n{text[:FETCH_TRUNCATE]}"
        except Exception as e:
            return f"fetch_page error: {e}"

    def _history_messages(self, history: List[Dict[str, str]]) -> List[Dict[str, str]]:
        msgs = []
        for turn in (history or [])[-6:]:
            role = turn.get("role", "user")
            if role not in ("user", "assistant"):
                role = "user"
            content = str(turn.get("content", ""))[:4000]
            msgs.append({"role": role, "content": content})
        return msgs

    async def _chat(self, messages: List[Dict], tools=None, stream: bool = False, retry: bool = True):
        client = self._client_or_create()
        kwargs: Dict[str, Any] = {"model": self.model, "messages": messages}
        if tools is not None:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if not stream:
            kwargs["temperature"] = 0.2
        try:
            if stream:
                return await client.chat.completions.create(stream=True, **kwargs)
            return await client.chat.completions.create(**kwargs)
        except Exception as e:
            if retry:
                await asyncio.sleep(1)
                try:
                    if stream:
                        return await client.chat.completions.create(stream=True, **kwargs)
                    return await client.chat.completions.create(**kwargs)
                except Exception as e2:
                    raise e2
            raise e

    def _extract_tool_calls(self, message) -> List[Dict[str, Any]]:
        calls = getattr(message, "tool_calls", None) or []
        out = []
        for c in calls:
            try:
                fn = c.function.name
                args = json.loads(c.function.arguments or "{}")
            except Exception:
                continue
            out.append({"id": getattr(c, "id", ""), "name": fn, "args": args})
        return out

    async def answer_stream(
        self, question: str, history: Optional[List[Dict[str, str]]] = None
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yield SSE-ready event dicts: status/token/sources/done/error."""
        used: Dict[str, str] = {}
        messages: List[Dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages += self._history_messages(history or [])
        messages.append({"role": "user", "content": question})
        yield {"type": "status", "message": "Searching site..."}
        try:
            for round_no in range(MAX_ROUNDS):
                try:
                    resp = await self._chat(messages, tools=TOOLS)
                except Exception as e:
                    logger.error("LLM error: %s", e)
                    yield {"type": "error", "message": "The AI service is unavailable right now. Please try again."}
                    return
                msg = resp.choices[0].message
                calls = self._extract_tool_calls(msg)
                # Grounding fallback: first round with no tool call -> run search ourselves
                if not calls and round_no == 0:
                    chunks = self.search_site(question)
                    yield {"type": "status", "message": f"Found {len(chunks)} relevant passages"}
                    tool_text = self._format_chunks(chunks, question)
                    for c in chunks:
                        used.setdefault(c.get("url", ""), c.get("title", ""))
                    messages.append({"role": "assistant", "content": msg.content or ""})
                    messages.append({"role": "user", "content": tool_text})
                    continue
                if not calls:
                    # final answer available (non-stream path fallback)
                    text = (msg.content or "").strip() or NOT_FOUND
                    for tok in _split_tokens(text):
                        yield {"type": "token", "text": tok}
                    yield {"type": "sources", "sources": [
                        {"url": u, "title": t} for u, t in used.items() if u]}
                    yield {"type": "done"}
                    return
                # execute tools
                messages.append({"role": "assistant", "content": msg.content or "",
                                 "tool_calls": [{"id": c["id"], "type": "function",
                                                 "function": {"name": c["name"],
                                                              "arguments": json.dumps(c["args"])}} for c in calls]})
                for c in calls[:4]:
                    if c["name"] == "search_site":
                        q = str(c["args"].get("query", question))[:500]
                        chunks = self.search_site(q)
                        yield {"type": "status", "message": f"Searching for: {q[:80]}"}
                        for ch in chunks:
                            used.setdefault(ch.get("url", ""), ch.get("title", ""))
                        result = self._format_chunks(chunks, q)
                    elif c["name"] == "fetch_page":
                        u = str(c["args"].get("url", ""))[:1000]
                        yield {"type": "status", "message": "Reading a page..."}
                        text = await self.fetch_page(u)
                        m = re.search(r"URL: (https?://\S+)", text)
                        if m:
                            used.setdefault(m.group(1), "")
                        result = f"<retrieved_content>\n{text[:FETCH_TRUNCATE]}\n</retrieved_content>"
                    else:
                        result = f"<retrieved_content>\nUnknown tool {c['name']}\n</retrieved_content>"
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
            # max rounds: force final streamed answer
            messages.append({"role": "user", "content": (
                "Answer the original question now using ONLY the retrieved content above. "
                "If it is not there, say exactly: \"I couldn't find this on the website\" "
                "and suggest which page might have it.")})
            try:
                stream_resp = await self._chat(messages, stream=True)
                async for chunk in stream_resp:
                    try:
                        delta = chunk.choices[0].delta.content or ""
                    except Exception:
                        delta = ""
                    if delta:
                        yield {"type": "token", "text": delta}
            except Exception as e:
                logger.error("Stream error: %s", e)
                yield {"type": "error", "message": "Streaming failed. Please try again."}
                return
            yield {"type": "sources", "sources": [{"url": u, "title": t} for u, t in used.items() if u]}
            yield {"type": "done"}
        except Exception as e:
            logger.exception("agent failure")
            yield {"type": "error", "message": "Something went wrong. Please try again."}

    def _format_chunks(self, chunks: List[Dict[str, Any]], query: str) -> str:
        if not chunks:
            return (f"<retrieved_content>\nSearch query: {query}\nNo relevant passages found "
                    "on this site.\n</retrieved_content>")
        parts = [f"<retrieved_content>\nSearch query: {query}"]
        for c in chunks:
            parts.append(f"\n---\nURL: {c.get('url','')}\nTitle: {c.get('title','')}\n"
                         f"{(c.get('text','') or '')[:1500]}")
        parts.append("\n</retrieved_content>")
        return "\n".join(parts)


def _split_tokens(text: str) -> List[str]:
    # word-wise tokens for non-streaming fallback path
    return [w + " " for w in text.split(" ")]
