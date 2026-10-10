"""Central configuration loaded from environment. Import-safe: never raises at import time."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    OPENROUTER_API_KEY: str = ""
    LLM_MODEL: str = "apodex/apodex-1.1-mini:free"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    OPENROUTER_TIMEOUT_S: float = 60.0
    EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    MAX_PAGES: int = 30
    MAX_DEPTH: int = 2
    CACHE_TTL_HOURS: int = 24
    RATE_LIMIT_PER_MINUTE: int = 20
    CORS_ORIGINS: str = "http://localhost:3000"
    NEXT_PUBLIC_API_URL: str = "http://localhost:8000"
    CHROMA_DIR: str = "./chroma_data"

    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    def validate_startup(self) -> None:
        """Call at app startup. Fails with a clear message if key is missing."""
        if not self.OPENROUTER_API_KEY.strip():
            raise RuntimeError(
                "OPENROUTER_API_KEY is missing. Copy .env.example to .env and set "
                "OPENROUTER_API_KEY (get one at https://openrouter.ai)."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
