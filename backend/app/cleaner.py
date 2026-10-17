"""Main-content extraction: trafilatura first, BeautifulSoup fallback.

A login portal (e.g. a bare sign-in form) yields almost no trafilatura text;
the BS fallback still recovers title/meta/headings so the crawler can tell
"login-only, nothing to index" apart from "fetch failed". Returns None only
when the usable text is under ~50 chars either way.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

MIN_USABLE_CHARS = 50
FULL_ARTICLE_CHARS = 200

_STRIP_TAGS = ("script", "style", "nav", "footer", "header", "aside", "noscript")


def is_login_page(html: str) -> bool:
    """True when the page contains a password input (login portal)."""
    if not html:
        return False
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        return soup.find("input", attrs={"type": re.compile(r"^password$", re.I)}) is not None
    except Exception:
        return bool(re.search(r"<input[^>]*type=[\"']?password", html, re.I))


def _bs_fallback(html: str) -> Optional[Tuple[str, str]]:
    """Plain-text extraction: title + meta description + body text.

    Strips boilerplate (script/style/nav/footer/...) and keeps headings and
    link texts via get_text. Returns None when still under ~50 chars.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return None
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    meta = ""
    desc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if desc and desc.get("content"):
        meta = desc["content"].strip()
    body = soup.body or soup
    text = body.get_text(separator="\n", strip=True)
    # collapse blank lines
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    combined = "\n\n".join(p for p in (title, meta, text) if p)
    if len(combined) < MIN_USABLE_CHARS:
        return None
    return (title or "Untitled", combined)


def extract_main_content(html: str, url: str = "") -> Optional[Tuple[str, str]]:
    """Extract (title, text) from raw HTML. Returns None when unusable.

    Uses trafilatura's main-content extraction (tables kept, links excluded
    from noise); falls back to BeautifulSoup plain-text extraction when
    trafilatura yields nothing or under ~200 chars. Drops pages whose usable
    text is still under ~50 chars.
    """
    if not html or len(html) < 100:
        return None
    try:
        import trafilatura
    except ImportError:
        trafilatura = None  # type: ignore[assignment]
    if trafilatura is not None:
        try:
            text = trafilatura.extract(
                html,
                include_tables=True,
                include_links=False,
                include_images=False,
                favor_precision=True,
            )
            title = trafilatura.extract_title(html) if hasattr(trafilatura, "extract_title") else ""
            if text and len(text.strip()) >= FULL_ARTICLE_CHARS:
                return ((title or "").strip() or "Untitled", text.strip())
        except Exception:
            pass
    # trafilatura empty/thin -> BS fallback (also recovers title/meta/headings)
    return _bs_fallback(html)
