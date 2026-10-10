"""Shared OpenRouter client (one AsyncOpenAI instance per process).

The model ID lives ONLY in settings (LLM_MODEL). Nothing else may hardcode it.
The API key lives ONLY in settings (OPENROUTER_API_KEY, from siteask/.env).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Tuple

from .config import get_settings

FREE_LIMIT_MESSAGE = "The free model's limit was reached. Please try again later."


@lru_cache(maxsize=1)
def get_llm_client():
    """Process-wide shared AsyncOpenAI client for OpenRouter."""
    from openai import AsyncOpenAI

    s = get_settings()
    return AsyncOpenAI(api_key=s.OPENROUTER_API_KEY,
                       base_url=s.OPENROUTER_BASE_URL,
                       timeout=s.OPENROUTER_TIMEOUT_S)


def classify_error(exc: BaseException) -> Tuple[str, bool]:
    """Map an OpenAI/SDK exception to (user_message, retryable).

    Never retry 400/401/402/403/404. Never retry 429 (free-tier limit).
    Retry 502/503/timeouts/network errors at most once (caller enforces).
    Never leaks secrets or stack traces.
    """
    try:
        from openai import APIConnectionError, APIStatusError, APITimeoutError
    except ImportError:  # pragma: no cover
        return ("The AI service is unavailable right now. Please try again.", True)

    if isinstance(exc, APITimeoutError):
        return ("The AI service timed out. Please try again.", True)
    if isinstance(exc, APIConnectionError):
        return ("Could not reach the AI service. Please try again.", True)
    if isinstance(exc, APIStatusError):
        code = exc.status_code or 0
        if code == 429:
            return (FREE_LIMIT_MESSAGE, False)
        if code in (502, 503, 504):
            return ("The AI service is overloaded. Please try again.", True)
        if code == 401:
            return ("The AI service is not configured correctly. Please try again later.", False)
        if code == 402:
            return ("The AI service has no credit left. Please try again later.", False)
        if code == 403:
            return ("The AI service refused the request. Please try again later.", False)
        if code == 404:
            return ("The configured AI model is unavailable. Please try again later.", False)
        if code == 400:
            return ("The AI service rejected the request. Please try rephrasing.", False)
        return ("The AI service is unavailable right now. Please try again.", False)
    return ("The AI service is unavailable right now. Please try again.", True)
