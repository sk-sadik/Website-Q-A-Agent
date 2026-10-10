"""Local embeddings: BGE-small-en-v1.5 when available, hash fallback otherwise.

Uses the BGE query-instruction prefix for queries as recommended:
https://huggingface.co/BAAI/bge-small-en-v1.5
"""
from __future__ import annotations

import hashlib
import math
import os
from typing import List, Optional

QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
DIM = 384  # bge-small-en-v1.5 dimension; hash fallback matches it

_model = None
_model_name: Optional[str] = None


def get_model():
    """Load sentence-transformers model once. Raises ImportError/RuntimeError if unavailable."""
    global _model, _model_name
    if _model is not None:
        return _model
    name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    from sentence_transformers import SentenceTransformer

    _model = SentenceTransformer(name, device="cpu")
    _model_name = name
    return _model


def _hash_embed(texts: List[str]) -> List[List[float]]:
    """Deterministic offline fallback: hashed bag-of-words, L2-normalized."""
    vecs: List[List[float]] = []
    for t in texts:
        v = [0.0] * DIM
        for word in t.lower().split():
            h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
            v[h % DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        vecs.append([x / n for x in v])
    return vecs


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Batch-embed documents. Falls back to hash embeddings if model unavailable."""
    if not texts:
        return []
    try:
        model = get_model()
        vecs = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]
    except Exception:
        return _hash_embed(texts)


def embed_query(query: str) -> List[float]:
    """Embed a single query with the BGE instruction prefix."""
    return embed_texts([QUERY_PREFIX + query])[0]
