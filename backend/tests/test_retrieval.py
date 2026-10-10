"""Retrieval returns the right chunk for a known question (offline hash embeddings)."""
from app.store import ChromaStore


def test_retrieval(tmp_path):
    store = ChromaStore(persist_directory=str(tmp_path / "chroma"))
    store.add_chunks("abc123", [
        {"url": "https://s.test/pricing", "title": "Pricing", "chunk_index": 0,
         "text": "Our pricing is simple. The Pro plan costs 49 dollars per month. " * 8},
        {"url": "https://s.test/about", "title": "About", "chunk_index": 0,
         "text": "We are a team of gardeners who love roses and tulips in spring. " * 8},
    ])
    hits = store.similarity_search("abc123", "How much does the Pro plan cost?", n_results=2)
    assert len(hits) == 2
    assert hits[0]["url"] == "https://s.test/pricing"
    assert "49 dollars" in hits[0]["text"]
    assert store.count("abc123") == 2
