"""Regression: a known fact in the index must reach the model.

Ingests one mock page containing a known fact into a real ChromaStore,
asks about it with a mocked LLM that calls search_site, and asserts the
retrieved chunk reaches the model and the final answer is NOT a refusal.
"""
import asyncio
import json

from app.agent import SiteAskAgent
from app.store import ChromaStore


class FakeFunction:
    def __init__(self, name, args):
        self.name = name
        self.arguments = json.dumps(args)


class FakeCall:
    def __init__(self, cid, name, args):
        self.id = cid
        self.function = FakeFunction(name, args)


class FakeMsg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeResp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg, "finish_reason": "stop"})()]


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        import copy
        self.calls.append(copy.deepcopy(kwargs))
        return self.script.pop(0)


class FakeClient:
    def __init__(self, script):
        self.chat = type("Chat", (), {})()
        self.chat.completions = FakeCompletions(script)


FACT = "The Riverside Library mascot is a fox named Bimble."
FACT_URL = "https://lib.test/mascot"


def test_known_fact_reaches_model_and_answer(tmp_path):
    store = ChromaStore(persist_directory=str(tmp_path / "chroma"))
    store.add_chunks("libsite", [{
        "url": FACT_URL, "title": "Mascot", "chunk_index": 0,
        "text": (FACT + " Bimble greets every visitor at the front desk. ") * 10,
    }])
    script = [
        FakeResp(FakeMsg("", [FakeCall("c1", "search_site", {"query": "library mascot name"})])),
        FakeResp(FakeMsg("The mascot is Bimble the fox.")),
    ]
    client = FakeClient(script)
    agent = SiteAskAgent(store=store, site_id="libsite", site_url="https://lib.test",
                         model="test-model", client_factory=lambda: client)

    async def go():
        return [e async for e in agent.answer_stream("What is the library mascot called?", [])]

    events = asyncio.run(go())
    # the retrieved chunk must have been fed back to the model
    tool_msgs = [m for call in client.chat.completions.calls
                 for m in call.get("messages", []) if m.get("role") == "tool"]
    assert tool_msgs, "tool result was never returned to the model"
    assert any(FACT_URL in m["content"] and "Bimble" in m["content"] for m in tool_msgs)
    # ... and the final answer must not be a refusal
    tokens = "".join(e.get("text", "") for e in events if e["type"] == "token")
    assert "Bimble" in tokens
    assert "couldn't find" not in tokens.lower()
    assert events[-1] == {"type": "done"}
