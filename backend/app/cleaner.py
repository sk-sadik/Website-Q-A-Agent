"""Main-content extraction with trafilatura."""
from __future__ import annotations

from typing import Optional, Tuple


MIN_USABLE_CHARS = 50


def extract_main_content(html: str, url: str = "") -> Optional[Tuple[str, str]]:
    """Extract (title, text) from raw HTML. Returns None when unusable.

    Uses trafilatura's main-content extraction (tables kept, links excluded
    from noise). Drops pages with no usable text (< 50 chars). The crawler
    retries pages under ~200 chars with Playwright before giving up.
    """
    if not html or len(html) < 100:
        return None
    try:
        import trafilatura
    except ImportError:
        return None
    try:
        text = trafilatura.extract(
            html,
            include_tables=True,
            include_links=False,
            include_images=False,
            favor_precision=True,
        )
        title = trafilatura.extract_title(html) if hasattr(trafilatura, "extract_title") else ""
    except Exception:
        return None
    if not text or len(text.strip()) < MIN_USABLE_CHARS:
        return None
    title = (title or "").strip() or "Untitled"
    return (title, text.strip())
