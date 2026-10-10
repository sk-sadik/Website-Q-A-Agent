"""Tool-calling agent loop (implemented manually on top of OpenAI SDK + OpenRouter)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator, Callable, Dict, List, Optional
from urllib.parse import urlparse

from .llm import classify_error, get_llm_client
from .prompts import SYSTEM_PROMPT
from .security import has_credentials, hostname_is_blocked, is_valid_scheme, normalize_url

logger = logging.getLogger(__name__)

MAX_ROUNDS = 4
FETCH_TRUNCATE = 6000
NOT_FOUND = "I couldn't find this on the website"
THINK_OPEN = re.compile(r"<think\s*>", re.IGNORECASE)
THINK_CLOSE = re.compile(r"</think\s*>", re.IGNORECASE)

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


class ThinkStripper:
    """Stateful stripper for <think>...</think> blocks, tolerant of chunk splits."""

    def __init__(self):
        self.in_think = False
        self._hold = ""

    def feed(self, text: str) -> str:
        rest = self._hold + (text or "")
        self._hold = ""
        out: List[str] = []
        while rest:
            if self.in_think:
                m = THINK_CLOSE.search(rest)
                if not m:
                    return "".join(out)  # drop remainder, still thinking
                rest = rest[m.end():]
                self.in_think = False
            else:
                mo = THINK_OPEN.search(rest)
                mc = THINK_CLOSE.search(rest)
                if mc and (not mo or mc.start() < mo.start()):
                    rest = rest[:mc.start()] + rest[mc.end():]  # stray close: drop
                    continue
                if not mo:
                    # hold back a possible split-tag tail ("<th" + "ink>...")
                    k = rest.rfind("<")
                    if k != -1 and ">" not in rest[k:]:
                        out.append(rest[:k])
                        self._hold = rest[k:]
                    else:
                        out.append(rest)
                    break
                out.append(rest[:mo.start()])
                rest = rest[mo.end():]
                self.in_think = True
        return "".join(out)

    def flush(self) -> str:
        """Emit any held text at end of stream (dropped if inside unclosed think)."""
        hold, self._hold = self._hold, ""
        return "" if self.in_think else hold


def message_text(msg: Any) -> str:
    """Assistant content only. Reasoning/thinking fields are never treated as answer text."""
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # some providers return content blocks
        return "".join(getattr(b, "text", "") or "" for b in content
                       if getattr(b, "type", "text") in ("text", "output_text"))
    return ""


class SiteAskAgent:
    def __init__(self, store, site_id: str, site_url: str, model: Optional[str] = None,
                 api_key: Optional[str] = None, base_url: Optional[str] = None,
                 client_factory: Optional[Callable[[], Any]] = None):
        # Model ID is centralized in settings; an explicit model only overrides (tests).
        if model is None:
            from .config import get_settings

            model = get_settings().LLM_MODEL
        self.store = store
        self.site_id = site_id
        self.site_url = site_url
        self.model = model
        self._client_factory = client_factory

    def _client_or_create(self):
        if self._client_factory is not None:
            return self._client_factory()
        return get_llm_client()  # shared process-wide client

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

    async def _chat(self, messages: List[Dict], tools=None, stream: bool = False):
        """One LLM call with at most one retry for transient errors only."""
        client = self._client_or_create()
        kwargs: Dict[str, Any] = {"model": self.model, "messages": messages}
        if tools is not None:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            if stream:
                return await client.chat.completions.create(stream=True, **kwargs)
            return await client.chat.completions.create(**kwargs)
        except Exception as e:
            user_msg, retryable = classify_error(e)
            logger.warning("LLM call failed (%s, retryable=%s): %s",
                           type(e).__name__, retryable, _short_error(e))
            if not retryable:
                raise _AgentError(user_msg)
            await asyncio.sleep(1)
            try:
                if stream:
                    return await client.chat.completions.create(stream=True, **kwargs)
                return await client.chat.completions.create(**kwargs)
            except Exception as e2:
                user_msg2, _ = classify_error(e2)
                logger.warning("LLM retry failed (%s)", type(e2).__name__)
                raise _AgentError(user_msg2)

    def _extract_tool_calls(self, message) -> List[Dict[str, Any]]:
        """Safely parse tool calls; malformed args or unknown shapes are skipped."""
        calls = getattr(message, "tool_calls", None) or []
        if not isinstance(calls, (list, tuple)):
            return []
        out = []
        for c in calls:
            try:
                fn = getattr(getattr(c, "function", None), "name", "")
                raw = getattr(getattr(c, "function", None), "arguments", "") or "{}"
                args = json.loads(raw)
                if not isinstance(args, dict):
                    continue
                if not fn:
                    continue
            except Exception:
                continue
            out.append({"id": getattr(c, "id", "") or "", "name": fn, "args": args})
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
                except _AgentError as e:
                    yield {"type": "error", "message": str(e)}
                    return
                try:
                    msg = resp.choices[0].message
                except Exception:
                    yield {"type": "error",
                           "message": "The AI service returned a bad response. Please try again."}
                    return
                calls = self._extract_tool_calls(msg)
                # Grounding fallback: first round with no tool call -> run search ourselves
                if not calls and round_no == 0:
                    chunks = self.search_site(question)
                    yield {"type": "status", "message": f"Found {len(chunks)} relevant passages"}
                    tool_text = self._format_chunks(chunks, question)
                    for c in chunks:
                        used.setdefault(c.get("url", ""), c.get("title", ""))
                    messages.append({"role": "assistant", "content": message_text(msg)})
                    messages.append({"role": "user", "content": tool_text})
                    continue
                if not calls:
                    text = message_text(msg).strip()
                    if not text:
                        # Empty generation (e.g. reasoning-only pass): retry once.
                        try:
                            resp = await self._chat(messages, tools=TOOLS)
                            msg = resp.choices[0].message
                            text = message_text(msg).strip()
                        except _AgentError as e:
                            yield {"type": "error", "message": str(e)}
                            return
                        except Exception:
                            text = ""
                        if not text:
                            yield {"type": "error", "message":
                                   "The AI service returned an empty response. Please try again."}
                            return
                    for tok in _split_tokens(text):
                        yield {"type": "token", "text": tok}
                    yield {"type": "sources", "sources": [
                        {"url": u, "title": t} for u, t in used.items() if u]}
                    yield {"type": "done"}
                    return
                # execute tools
                messages.append({"role": "assistant", "content": message_text(msg),
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
            streamed_ok, final_events = await self._stream_final(messages)
            for ev in final_events:
                yield ev
            if not streamed_ok:  # error already yielded
                return
            yield {"type": "sources", "sources": [{"url": u, "title": t} for u, t in used.items() if u]}
            yield {"type": "done"}
        except Exception:
            logger.exception("agent failure")
            yield {"type": "error", "message": "Something went wrong. Please try again."}

    async def _stream_final(self, messages: List[Dict]) -> tuple[bool, List[Dict[str, Any]]]:
        """Stream ONLY final answer content as token events. Returns (ok, events).
        Reasoning fields/tags are never forwarded. Never returns an empty answer:
        an empty generation is retried once, then becomes a friendly error event."""
        for attempt in (0, 1):
            try:
                stream_resp = await self._chat(messages, stream=True)
            except _AgentError as e:
                return False, [{"type": "error", "message": str(e)}]
            stripper = ThinkStripper()
            events: List[Dict[str, Any]] = []
            try:
                async for chunk in stream_resp:
                    try:
                        delta = getattr(chunk.choices[0].delta, "content", None) or ""
                    except Exception:
                        delta = ""
                    # NOTE: delta.reasoning / reasoning_content deliberately ignored.
                    text = stripper.feed(delta)
                    if text:
                        events.append({"type": "token", "text": text})
            except _AgentError as e:
                return False, [{"type": "error", "message": str(e)}]
            except Exception:
                logger.exception("stream read failed")
                return False, [{"type": "error",
                                "message": "Streaming failed. Please try again."}]
            tail = stripper.flush()
            if tail:
                events.append({"type": "token", "text": tail})
            if events:
                return True, events
            logger.warning("empty streamed answer (attempt %d)", attempt)
            # empty generation: retry once, then friendly error — never an empty answer
        return False, [{"type": "error",
                        "message": "The AI service returned an empty response. Please try again."}]

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


class _AgentError(Exception):
    """User-safe, pre-classified LLM failure (message is safe to stream to clients)."""


def _short_error(e: BaseException) -> str:
    s = str(e)
    return s[:300]


def _split_tokens(text: str) -> List[str]:
    # word-wise tokens for non-streaming fallback path
    return [w + " " for w in text.split(" ")]
