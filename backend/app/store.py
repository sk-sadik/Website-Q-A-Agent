"""Chroma-backed vector store (one collection per site) + site registry.

Uses ChromaDB persistent client when installed; otherwise an in-memory
fallback with the same API so tests and offline dev still work.
Site metadata (status/progress/cache) is kept in a JSON registry file.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def site_id_from_url(url: str) -> str:
    p = urlparse(url.lower())
    origin = f"{p.scheme}://{p.hostname or ''}"
    if p.port and p.port not in (80, 443):
        origin += f":{p.port}"
    return hashlib.sha256(origin.encode()).hexdigest()[:16]


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class ChromaStore:
    def __init__(self, persist_directory: str = "./chroma_data"):
        self.persist_directory = persist_directory
        Path(persist_directory).mkdir(parents=True, exist_ok=True)
        self._registry_path = Path(persist_directory) / "registry.json"
        self._registry: Dict[str, Dict[str, Any]] = self._load_registry()
        self._client = None
        self._collections: Dict[str, Any] = {}
        self._memory: Dict[str, Dict[str, Any]] = {}  # fallback: site_id -> {ids, docs, metas, embs}
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            self._client = chromadb.PersistentClient(
                path=persist_directory, settings=ChromaSettings(anonymized_telemetry=False)
            )
        except Exception:
            self._client = None

    # ---- registry ----
    def _load_registry(self) -> Dict[str, Dict[str, Any]]:
        try:
            if self._registry_path.exists():
                return json.loads(self._registry_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_registry(self) -> None:
        try:
            self._registry_path.write_text(json.dumps(self._registry, indent=2), encoding="utf-8")
        except Exception:
            pass

    def get_site(self, site_id: str) -> Optional[Dict[str, Any]]:
        return self._registry.get(site_id)

    def set_site(self, site_id: str, meta: Dict[str, Any]) -> None:
        self._registry[site_id] = meta
        self._save_registry()

    def is_cache_valid(self, site_id: str, ttl_hours: int) -> bool:
        meta = self._registry.get(site_id)
        if not meta or meta.get("status") != "ready":
            return False
        crawled_at = meta.get("crawled_at") or 0
        return (time.time() - crawled_at) < ttl_hours * 3600

    # ---- vectors ----
    def _get_collection(self, site_id: str):
        if self._client is None:
            return None
        if site_id not in self._collections:
            self._collections[site_id] = self.client_get_or_create(site_id)
        return self._collections[site_id]

    def client_get_or_create(self, site_id: str):
        return self._client.get_or_create_collection(
            name=f"site_{site_id}", metadata={"description": "SiteAsk indexed content"}
        )

    def clear_site(self, site_id: str) -> None:
        self._memory.pop(site_id, None)
        if self._client is not None:
            try:
                self._client.delete_collection(f"site_{site_id}")
            except Exception:
                pass
            self._collections.pop(site_id, None)

    def add_chunks(self, site_id: str, chunks: List[Dict[str, Any]]) -> int:
        if not chunks:
            return 0
        from .embeddings import embed_texts

        ids = [f"{site_id}_{c['chunk_index']}_{i}" for i, c in enumerate(chunks)]
        docs = [c["text"] for c in chunks]
        metas = [{"url": c["url"], "title": c.get("title", ""), "chunk_index": c["chunk_index"]} for c in chunks]
        embs = embed_texts(docs)
        col = self._get_collection(site_id)
        if col is not None:
            try:
                col.delete(ids=ids)
            except Exception:
                pass
            col.add(ids=ids, documents=docs, metadatas=metas, embeddings=embs)
        else:
            mem = self._memory.setdefault(site_id, {"ids": [], "docs": [], "metas": [], "embs": []})
            # replace docs with same ids
            existing = set(mem["ids"])
            for _id, d, m, e in zip(ids, docs, metas, embs):
                if _id in existing:
                    idx = mem["ids"].index(_id)
                    mem["docs"][idx], mem["metas"][idx], mem["embs"][idx] = d, m, e
                else:
                    mem["ids"].append(_id)
                    mem["docs"].append(d)
                    mem["metas"].append(m)
                    mem["embs"].append(e)
        return len(chunks)

    def similarity_search(self, site_id: str, query: str, n_results: int = 6) -> List[Dict[str, Any]]:
        from .embeddings import embed_query

        q = embed_query(query)
        col = self._get_collection(site_id)
        if col is not None:
            try:
                res = col.query(query_embeddings=[q], n_results=n_results,
                                include=["documents", "metadatas", "distances"])
                out: List[Dict[str, Any]] = []
                docs = (res.get("documents") or [[]])[0]
                metas = (res.get("metadatas") or [[]])[0]
                dists = (res.get("distances") or [[]])[0]
                for d, m, dist in zip(docs, metas, dists):
                    out.append({"url": (m or {}).get("url", ""), "title": (m or {}).get("title", ""),
                                "text": d, "score": 1.0 / (1.0 + float(dist))})
                logger.info("search site=%s collection=site_%s q=%.60s n=%d hits=%d top_dists=%s",
                            site_id, site_id, query, n_results, len(out),
                            [round(float(x), 3) for x in dists[:3]])
                return out
            except Exception:
                logger.exception("chroma query failed site=%s", site_id)
                return []
        mem = self._memory.get(site_id)
        mem = self._memory.get(site_id)
        if not mem or not mem["docs"]:
            return []
        scored = sorted(
            zip(mem["docs"], mem["metas"], mem["embs"]),
            key=lambda t: _cosine(q, t[2]), reverse=True,
        )[:n_results]
        return [{"url": m.get("url", ""), "title": m.get("title", ""), "text": d,
                 "score": _cosine(q, e)} for d, m, e in scored]

    def count(self, site_id: str) -> int:
        col = self._get_collection(site_id)
        if col is not None:
            try:
                return col.count()
            except Exception:
                return 0
        mem = self._memory.get(site_id)
        return len(mem["docs"]) if mem else 0
