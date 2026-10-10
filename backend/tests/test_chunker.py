"""Chunker: size, overlap, metadata."""
from app.chunker import CHUNK_CHARS, OVERLAP_CHARS, chunk_text, count_tokens


def _words(n):
    return ("lorem ipsum dolor sit amet " * n).strip()


def test_small_text_single_chunk_with_metadata():
    chunks = chunk_text(_words(20), "https://ex.com/a", "Title A")
    assert len(chunks) == 1
    c = chunks[0]
    assert c["url"] == "https://ex.com/a"
    assert c["title"] == "Title A"
    assert c["chunk_index"] == 0
    assert c["token_count"] == count_tokens(c["text"])


def test_short_text_dropped():
    assert chunk_text("too short", "https://ex.com/", "T") == []


def test_large_text_splits_with_overlap_and_indices():
    text = _words(1200)  # ~30k chars -> multiple chunks
    chunks = chunk_text(text, "https://ex.com/big", "Big")
    assert len(chunks) >= 3
    for i, c in enumerate(chunks):
        assert c["chunk_index"] == i
        assert c["url"] == "https://ex.com/big"
        assert count_tokens(c["text"]) <= 700  # target 600 + 80 overlap
    # overlap: tail of chunk 0 reappears at start of chunk 1
    assert chunks[0]["text"][-100:] in chunks[1]["text"] or \
        len(set(chunks[0]["text"].split()) & set(chunks[1]["text"].split())) >= 5
