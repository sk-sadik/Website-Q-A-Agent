"""Pydantic v2 schemas for the API."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class IngestRequest(BaseModel):
    url: str = Field(..., description="Website URL to crawl and index")
    refresh: bool = Field(default=False)

    @field_validator("url")
    @classmethod
    def _add_scheme(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("URL is required")
        return v if "://" in v else "https://" + v


class IngestResponse(BaseModel):
    site_id: str
    status: str


class StatusResponse(BaseModel):
    status: Literal["queued", "crawling", "indexing", "ready", "failed"]
    pages_crawled: int = 0
    pages_found: int = 0
    chunk_count: int = 0
    error: Optional[str] = None


class HistoryTurn(BaseModel):
    role: str
    content: str


class AskRequest(BaseModel):
    site_id: str
    question: str
    history: List[HistoryTurn] = Field(default_factory=list)

    @field_validator("question")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("Question is required")
        return v.strip()


class AnswerSource(BaseModel):
    url: str
    title: str = ""


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"


SSE_TYPES = ("status", "token", "sources", "done", "error")
