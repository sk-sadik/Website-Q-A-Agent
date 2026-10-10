"""Apodex 1.1 Mini integration tests — ALL MOCKED, no real key needed.

Covers: model ID (:free suffix), base URL, key sourced from backend settings,
system prompt + history wiring, search-before-answer, tool-result round-trip,
4-round cap, SSE event order, source tracking, injection defense, reasoning
stripping, error mapping (401/402/429/502), malformed tool calls.
"""
import asyncio
import copy
import json

import httpx
import pytest

from app.agent import MAX_ROUNDS, NOT_FOUND, SiteAskAgent, ThinkStripper, message_text
from app.config import get_settings
from app.llm import FREE_LIMIT_MESSAGE, classify_error, get_llm_client
from app.prompts import SYSTEM_PROMPT


# ---------- fakes ----------

class FakeFunction:
    def __init__(self, name, args):
        self.name = name
        self.arguments = args if isinstance(args, str) else json.dumps(args)


class FakeCall:
    def __init__(self, cid, name, args):
        self.id = cid
        self.function = FakeFunction(name, args)


class FakeMsg:
    def __init__(self, content="", tool_calls=None, **extra):
        self.content = content
        self.tool_calls = tool_calls
        for k, v in extra.items():  # e.g. reasoning="..." — must be ignored
            setattr(self, k, v)


class FakeResp:
    def __init__(self, msg, finish="stop"):
        self.choices = [type("C", (), {"message": msg, "finish_reason": finish})()]


class FakeDelta:
    def __init__(self, content="", reasoning=None):
        self.content = content
        if reasoning is not None:
            self.reasoning = reasoning


class FakeStreamChunk:
    def __init__(self, content="", reasoning=None):
        self.choices = [type("C", (), {"delta": FakeDelta(content, reasoning)})()]


class FakeCompletions:
    """Script items: FakeResp, ('stream', [chunks]), or Exception to raise."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []  # captured kwargs per call (model, messages, ...)

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))  # snapshot: agent mutates the list later
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple) and item[0] == "stream":
            chunks = item[1]

            async def gen():
                for c in chunks:
                    yield c

            return gen()
        return item


class FakeClient:
    def __init__(self, script):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions(script)


class MemStore:
    def __init__(self, chunks):
        self._chunks = chunks

    def similarity_search(self, site_id, query, n_results=6):
        return self._chunks[:n_results]


def make_agent(chunks, script, **kw):
    client = FakeClient(script)
    kw.setdefault("site_id", "s")
    kw.setdefault("site_url", "https://s.test")
    agent = SiteAskAgent(store=MemStore(chunks), client_factory=lambda: client, **kw)
    return agent, client


def collect(agent, question, history=None):
    async def go():
        return [e async for e in agent.answer_stream(question, history or [])]

    return asyncio.run(go())


def status_error(code):
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    resp = httpx.Response(code, request=req, text='{"error":"x"}')
    import openai

    return openai.APIStatusError(f"status {code}", response=resp, body=None)


# ---------- config: model id, base url, key ----------

def test_model_id_has_free_suffix():
    assert get_settings().LLM_MODEL == "apodex/apodex-1.1-mini:free"
    assert get_settings().LLM_MODEL.endswith(":free")
    assert "apodex-1.1-mini" in get_settings().LLM_MODEL
    # agent defaults to the centralized setting — never hardcoded elsewhere
    agent = SiteAskAgent(store=MemStore([]), site_id="s", site_url="https://s.test")
    assert agent.model == "apodex/apodex-1.1-mini:free"


def test_base_url():
    assert get_settings().OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"


def test_shared_client_uses_settings_key_and_base(monkeypatch):
    get_llm_client.cache_clear()
    get_settings.cache_clear()
    try:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-mocked-key-123")
        monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        c = get_llm_client()
        assert c.api_key == "sk-or-mocked-key-123"  # sourced from settings, never printed
        assert c is get_llm_client()  # shared singleton, not per-request
    finally:
        get_llm_client.cache_clear()
        get_settings.cache_clear()


# ---------- prompt + history wiring ----------

def test_system_prompt_and_history_sent():
    chunks = [{"url": "https://s.test/a", "title": "A", "text": "fact", "score": 1.0}]
    script = [FakeResp(FakeMsg("done"))]
    agent, client = make_agent(chunks, script)
    collect(agent, "Q?", history=[{"role": "user", "content": "first"},
                                  {"role": "assistant", "content": "reply"}])
    sent = client.chat.completions.calls[0]["messages"]
    assert sent[0]["role"] == "system" and sent[0]["content"] == SYSTEM_PROMPT
    assert sent[1] == {"role": "user", "content": "first"}
    assert sent[2] == {"role": "assistant", "content": "reply"}
    assert sent[3] == {"role": "user", "content": "Q?"}
    assert "I couldn't find this on the website" in SYSTEM_PROMPT
    assert "untrusted data" in SYSTEM_PROMPT


def test_history_capped_at_six():
    chunks = [{"url": "https://s.test/a", "title": "A", "text": "fact", "score": 1.0}]
    script = [FakeResp(FakeMsg("done"))]
    agent, client = make_agent(chunks, script)
    hist = [{"role": "user", "content": f"q{i}"} for i in range(10)]
    collect(agent, "Q?", history=hist)
    sent = client.chat.completions.calls[0]["messages"]
    users = [m for m in sent if m["role"] == "user"]
    assert users[-7]["content"] == "q4"  # only last 6 turns + current question
    assert users[-1]["content"] == "Q?"


# ---------- tool loop ----------

def test_tool_results_returned_to_model():
    chunks = [{"url": "https://s.test/a", "title": "A", "text": "fact", "score": 1.0}]
    script = [
        FakeResp(FakeMsg("", [FakeCall("c1", "search_site", {"query": "q"})])),
        FakeResp(FakeMsg("final answer")),
    ]
    agent, client = make_agent(chunks, script)
    events = collect(agent, "Q?")
    assert events[-1] == {"type": "done"}
    second = client.chat.completions.calls[1]["messages"]
    roles = [m.get("role") for m in second]
    assert "tool" in roles  # tool result fed back in OpenAI message structure
    tool_msg = next(m for m in second if m.get("role") == "tool")
    assert "<retrieved_content>" in tool_msg["content"]


def test_four_round_cap_then_forced_answer():
    chunks = [{"url": "https://s.test/a", "title": "A", "text": "fact", "score": 1.0}]
    script = [FakeResp(FakeMsg("", [FakeCall(f"c{i}", "search_site", {"query": "x"})]))
              for i in range(MAX_ROUNDS)]
    script.append(("stream", [FakeStreamChunk("forced answer")]))
    agent, client = make_agent(chunks, script)
    events = collect(agent, "Q?")
    non_stream = [c for c in client.chat.completions.calls if not c.get("stream")]
    assert len(non_stream) == MAX_ROUNDS
    assert any(c.get("stream") for c in client.chat.completions.calls)
    assert "".join(e.get("text", "") for e in events if e["type"] == "token") == "forced answer"
    assert events[-1] == {"type": "done"}


def test_malformed_tool_args_skipped_safely():
    chunks = [{"url": "https://s.test/a", "title": "A", "text": "fact", "score": 1.0}]
    bad = FakeCall("c1", "search_site", "{not-json")
    bad2 = FakeCall("c2", "search_site", "[1,2]")  # valid JSON, not a dict
    script = [FakeResp(FakeMsg("", [bad, bad2])), FakeResp(FakeMsg("recovered"))]
    agent, _ = make_agent(chunks, script)
    events = collect(agent, "Q?")
    assert events[-1] == {"type": "done"}


def test_missing_tool_calls_attr_treated_as_no_calls():
    class BareMsg:
        def __init__(self, content="plain answer"):
            self.content = content

    class BareResp:
        def __init__(self, content="plain answer"):
            self.choices = [type("C", (), {"message": BareMsg(content),
                                           "finish_reason": "stop"})()]

    agent, _ = make_agent([{"url": "https://s.test/a", "title": "A",
                            "text": "fact", "score": 1.0}], [BareResp(), BareResp("second")])
    events = collect(agent, "Q?")  # round-0 fallback search, no crash
    assert events[-1] == {"type": "done"}


# ---------- reasoning never streams ----------

def test_reasoning_fields_and_think_tags_never_streamed():
    chunks = [{"url": "https://s.test/a", "title": "A", "text": "fact", "score": 1.0}]
    script = [FakeResp(FakeMsg("", [FakeCall("c1", "search_site", {"query": "x"})]))
              for _ in range(MAX_ROUNDS)]
    script.append(("stream", [
        FakeStreamChunk("", reasoning="SECRET-REASONING-TRACE"),
        FakeStreamChunk("<think>hidden deliberation</think>"),
        FakeStreamChunk("visible <think>more hidden</think> answer"),
        FakeStreamChunk(" tail", reasoning="more-secret"),
    ]))
    agent, _ = make_agent(chunks, script)
    events = collect(agent, "Q?")
    tokens = "".join(e.get("text", "") for e in events if e["type"] == "token")
    assert "SECRET-REASONING-TRACE" not in tokens
    assert "more-secret" not in tokens
    assert "hidden" not in tokens
    assert "<think>" not in tokens
    assert tokens == "visible  answer tail"


def test_think_stripper_split_tags():
    s = ThinkStripper()
    assert s.feed("a <th") == "a "  # partial tag held back, not leaked
    assert s.feed("ink>b</think>c") == "c"
    assert s.feed("x</think>y") == "xy"  # stray close tag stripped
    assert s.flush() == ""


def test_message_text_ignores_reasoning_field():
    m = FakeMsg("answer", reasoning="should-be-ignored")
    assert message_text(m) == "answer"
    assert message_text(FakeMsg(None)) == ""


def test_empty_generation_retries_then_errors():
    chunks = [{"url": "https://s.test/a", "title": "A", "text": "fact", "score": 1.0}]
    script = [FakeResp(FakeMsg("", [FakeCall("c1", "search_site", {"query": "x"})])),
              FakeResp(FakeMsg("")), FakeResp(FakeMsg(""))]
    agent, _ = make_agent(chunks, script)
    events = collect(agent, "Q?")
    assert events[-1]["type"] == "error"
    assert "empty" in events[-1]["message"].lower()
    assert not any(e["type"] == "token" for e in events)  # never an empty answer


# ---------- error mapping ----------

@pytest.mark.parametrize("code,fragment", [
    (401, "configured"),
    (402, "credit"),
    (403, "refused"),
    (404, "unavailable"),
    (400, "rejected"),
])
def test_no_retry_client_errors(code, fragment):
    msg, retryable = classify_error(status_error(code))
    assert not retryable
    assert fragment in msg.lower()
    assert "sk-" not in msg and "Traceback" not in msg


def test_429_friendly_no_retry():
    msg, retryable = classify_error(status_error(429))
    assert not retryable
    assert msg == FREE_LIMIT_MESSAGE


@pytest.mark.parametrize("code", [502, 503, 504])
def test_5xx_retryable(code):
    _, retryable = classify_error(status_error(code))
    assert retryable


def test_429_yields_single_call_friendly_sse():
    chunks = [{"url": "https://s.test/a", "title": "A", "text": "fact", "score": 1.0}]
    agent, client = make_agent(chunks, [status_error(429)])
    events = collect(agent, "Q?")
    assert len(client.chat.completions.calls) == 1  # no retry loop
    assert events[-1] == {"type": "error", "message": FREE_LIMIT_MESSAGE}


def test_transient_502_retried_once_then_succeeds():
    chunks = [{"url": "https://s.test/a", "title": "A", "text": "fact", "score": 1.0}]
    script = [status_error(502),
              FakeResp(FakeMsg("", [FakeCall("c1", "search_site", {"query": "q"})])),
              FakeResp(FakeMsg("recovered after retry"))]
    agent, client = make_agent(chunks, script)
    events = collect(agent, "Q?")
    assert len(client.chat.completions.calls) == 3  # fail, retry, then round-1 final
    assert events[-1] == {"type": "done"}
    assert "".join(e.get("text", "") for e in events if e["type"] == "token")


def test_401_no_retry():
    chunks = []
    agent, client = make_agent(chunks, [status_error(401)])
    events = collect(agent, "Q?")
    assert len(client.chat.completions.calls) == 1
    assert events[-1]["type"] == "error"


# ---------- sources + grounding ----------

def test_sources_tracked_from_tool_results():
    chunks = [{"url": "https://s.test/real-page", "title": "Real",
               "text": "fact", "score": 1.0}]
    script = [FakeResp(FakeMsg("", [FakeCall("c1", "search_site", {"query": "q"})])),
              FakeResp(FakeMsg("answer"))]
    agent, _ = make_agent(chunks, script)
    events = collect(agent, "Q?")
    src = next(e for e in events if e["type"] == "sources")
    assert src["sources"] == [{"url": "https://s.test/real-page", "title": "Real"}]


def test_sse_event_order():
    chunks = [{"url": "https://s.test/a", "title": "A", "text": "fact", "score": 1.0}]
    script = [FakeResp(FakeMsg("", [FakeCall("c1", "search_site", {"query": "q"})])),
              FakeResp(FakeMsg("hello world"))]
    agent, _ = make_agent(chunks, script)
    events = collect(agent, "Q?")
    types = [e["type"] for e in events]
    assert types[0] == "status"
    assert types[-1] == "done"
    assert "token" in types and "sources" in types
    assert all(set(e.keys()) <= {"type", "message", "text", "sources"} for e in events)
