"""Token-aware chunking (~600 tokens/chunk, 80 token overlap)."""
from __future__ import annotations

import re
from typing import Any, Dict, List

CHARS_PER_TOKEN = 4
TARGET_TOKENS = 600
OVERLAP_TOKENS = 80
CHUNK_CHARS = TARGET_TOKENS * CHARS_PER_TOKEN
OVERLAP_CHARS = OVERLAP_TOKENS * CHARS_PER_TOKEN


def count_tokens(text: str) -> int:
    return len(text or "") // CHARS_PER_TOKEN


def _sentences(para: str) -> List[str]:
    parts = re.split(r"(?<=[.!?])\s+", para.strip())
    return [s.strip() for s in parts if s.strip()]


def chunk_text(text: str, url: str, title: str) -> List[Dict[str, Any]]:
    """Split text into chunks keeping {url, title, chunk_index, text, token_count}."""
    if not text or len(text.strip()) < 50:
        return []
    text = text.strip()
    if count_tokens(text) <= TARGET_TOKENS:
        return [{"url": url, "title": title or "Untitled", "chunk_index": 0,
                 "text": text, "token_count": count_tokens(text)}]

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    # fall back to sentence splitting if no paragraph breaks
    if len(paragraphs) <= 1:
        flat = _sentences(text)
        paragraphs, cur = [], ""
        for s in flat:
            if count_tokens(cur + " " + s) > TARGET_TOKENS // 2 and cur:
                paragraphs.append(cur)
                cur = s
            else:
                cur = (cur + " " + s).strip()
        if cur:
            paragraphs.append(cur)

    chunks: List[Dict[str, Any]] = []
    buf: List[str] = []
    buf_tokens = 0

    def flush() -> None:
        nonlocal buf, buf_tokens
        if not buf:
            return
        joined = "\n\n".join(buf).strip()
        if len(joined) >= 100:
            chunks.append({"url": url, "title": title or "Untitled",
                           "chunk_index": len(chunks), "text": joined,
                           "token_count": count_tokens(joined)})
        buf, buf_tokens = [], 0

    for para in paragraphs:
        pt = count_tokens(para)
        if pt > TARGET_TOKENS:
            flush()
            for s in _sentences(para):
                # hard-split overlong sentences by words so we always make progress
                pieces = [s]
                if count_tokens(s) > TARGET_TOKENS:
                    words = s.split()
                    pieces, cur = [], ""
                    for w in words:
                        if count_tokens(cur + " " + w) > TARGET_TOKENS and cur:
                            pieces.append(cur)
                            cur = w
                        else:
                            cur = (cur + " " + w).strip()
                    if cur:
                        pieces.append(cur)
                for s in pieces:
                    st = count_tokens(s)
                    if buf_tokens + st > TARGET_TOKENS and buf:
                        flush()
                        # overlap: carry tail of previous chunk
                        prev = chunks[-1]["text"][-OVERLAP_CHARS:] if chunks else ""
                        if prev:
                            buf, buf_tokens = [prev], count_tokens(prev)
                    buf.append(s)
                    buf_tokens += st
            flush()
            continue
        if buf_tokens + pt > TARGET_TOKENS and buf:
            flush()
            prev = chunks[-1]["text"][-OVERLAP_CHARS:] if chunks else ""
            if prev:
                buf, buf_tokens = [prev], count_tokens(prev)
        buf.append(para)
        buf_tokens += pt
    flush()
    # fix indices
    for i, c in enumerate(chunks):
        c["chunk_index"] = i
    return chunks
