"""Async BFS crawler: same-site only, robots-aware, sitemap-seeded, JS fallback."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .chunker import chunk_text
from .cleaner import extract_main_content
from .security import (
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    has_credentials,
    hostname_is_blocked,
    is_valid_scheme,
    normalize_url,
)

logger = logging.getLogger(__name__)

USER_AGENT = "SiteAsk/1.0 (+research crawler; contact: siteask)"
SKIP_EXT = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|zip|gz|tar|rar|7z|mp3|mp4|avi|mov|mkv|jpg|jpeg|png|gif|svg|webp|bmp|ico|css|js|mjs|json|xml|exe|dmg|woff2?|ttf|eot)$",
    re.IGNORECASE,
)
SKIP_SCHEME = re.compile(r"^(mailto:|tel:|javascript:|data:|ftp:|#)", re.IGNORECASE)

FetchResult = Tuple[int, str, str, str]  # status, final_url, content_type, text(html)
FetchFn = Callable[[str], Awaitable[Optional[FetchResult]]]


def site_root(host: str) -> str:
    """Normalize host for same-site comparison (www == non-www)."""
    h = (host or "").lower().rstrip(".")
    if h.startswith("www."):
        h = h[4:]
    return h


def is_same_site(url: str, start_url: str) -> bool:
    try:
        a = site_root(urlparse(url).hostname or "")
        b = site_root(urlparse(start_url).hostname or "")
    except Exception:
        return False
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def canonicalize(url: str) -> str:
    return normalize_url(url)


def should_skip(url: str) -> bool:
    if not url or SKIP_SCHEME.match(url):
        return True
    if not is_valid_scheme(url):
        return True
    if has_credentials(url):
        return True
    if SKIP_EXT.search(urlparse(url).path or ""):
        return True
    return False


def extract_links(html: str, base_url: str) -> List[str]:
    links: List[str] = []
    for m in re.finditer(r'<a\s[^>]*?href\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE):
        href = m.group(1).strip()
        if not href or SKIP_SCHEME.match(href):
            continue
        try:
            abs_url = canonicalize(urljoin(base_url, href))
        except Exception:
            continue
        if should_skip(abs_url):
            continue
        links.append(abs_url)
    # dedupe preserve order
    seen, out = set(), []
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def parse_sitemap_xml(content: str) -> List[str]:
    urls = re.findall(r"<loc>\s*([^<>\s]+)\s*</loc>", content or "")
    out, seen = [], set()
    for u in urls:
        u = u.strip()
        if u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def default_fetch(url: str) -> Optional[FetchResult]:
    """Fetch with manual redirect chain (max 5), SSRF re-check per hop, 3MB cap, HTML only."""
    current = url
    async with httpx.AsyncClient(
        timeout=10.0, headers={"User-Agent": USER_AGENT}, follow_redirects=False
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            host = urlparse(current).hostname or ""
            if hostname_is_blocked(host):
                logger.warning("SSRF blocked hop: %s", host)
                return None
            try:
                resp = await client.get(current)
            except Exception as e:
                logger.warning("Fetch failed %s: %s", current, e)
                return None
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    return None
                nxt = urljoin(current, loc)
                if not is_valid_scheme(nxt) or has_credentials(nxt):
                    return None
                current = canonicalize(nxt)
                continue
            if resp.status_code != 200:
                return None
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype and not (ctype.startswith("text/html") or ctype == "application/xhtml+xml"):
                return None
            # size cap: read with limit
            try:
                body = resp.content
            except Exception:
                return None
            if len(body) > MAX_RESPONSE_BYTES:
                logger.warning("Response too large: %s", current)
                return None
            try:
                html = body.decode("utf-8", errors="replace")
            except Exception:
                return None
            return (resp.status_code, current, ctype or "text/html", html)
        logger.warning("Too many redirects: %s", url)
        return None


async def playwright_fallback(url: str) -> Optional[str]:
    """Render JS page with Playwright Chromium. Returns HTML or None if unavailable."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page(user_agent=USER_AGENT)
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1500)
                html = await page.content()
                return html
            finally:
                await browser.close()
    except Exception as e:
        logger.warning("Playwright fallback failed for %s: %s", url, e)
        return None


class CrawlStats:
    def __init__(self):
        self.pages_crawled = 0
        self.pages_found = 1
        self.chunks: List[Dict] = []
        self.error: Optional[str] = None


async def crawl_site(
    start_url: str,
    max_pages: int = 30,
    max_depth: int = 2,
    concurrency: int = 4,
    polite_delay: float = 0.3,
    fetch_fn: Optional[FetchFn] = None,
    robots_txt: Optional[str] = None,
    js_fallback: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
) -> Dict:
    """BFS crawl. Never raises for per-page failures; reports start-page failure clearly."""
    fetch = fetch_fn or default_fetch
    js = js_fallback or playwright_fallback
    start = canonicalize(start_url)
    stats = CrawlStats()

    rp: Optional[RobotFileParser] = None
    if robots_txt is None:
        try:
            p = urlparse(start)
            robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
            r = await fetch(robots_url)
            robots_txt = r[3] if r else ""
        except Exception:
            robots_txt = ""
    try:
        rp = RobotFileParser()
        rp.set_url(urljoin(start, "/robots.txt"))
        rp.parse((robots_txt or "").splitlines())
    except Exception:
        rp = None

    def allowed(u: str) -> bool:
        if rp is None:
            return True
        try:
            return rp.can_fetch(USER_AGENT, u)
        except Exception:
            return True

    # sitemap seeding
    seeds: List[str] = []
    for name in ("sitemap.xml", "sitemap_index.xml"):
        try:
            p = urlparse(start)
            sm_url = f"{p.scheme}://{p.netloc}/{name}"
            r = await fetch(sm_url)
            if r and r[3]:
                for u in parse_sitemap_xml(r[3]):
                    cu = canonicalize(u)
                    if is_same_site(cu, start) and not should_skip(cu) and allowed(cu):
                        seeds.append(cu)
                if seeds:
                    break
        except Exception:
            continue

    visited: set[str] = set()
    queue: deque[Tuple[str, int]] = deque([(start, 0)])
    for s in seeds:
        if s != start:
            queue.append((s, 1))
    stats.pages_found = len(queue)
    sem = asyncio.Semaphore(max(1, min(concurrency, 5)))
    lock = asyncio.Lock()
    failed_start = False

    async def process(url: str, depth: int) -> List[Tuple[str, int]]:
        nonlocal failed_start
        async with sem:
            if polite_delay:
                await asyncio.sleep(polite_delay)
            if url in visited:
                return []
            if not allowed(url):
                async with lock:
                    visited.add(url)
                logger.info("Robots disallowed: %s", url)
                return []
            try:
                res = await fetch(url)
            except Exception as e:
                logger.warning("Page failed %s: %s", url, e)
                res = None
            if res is None:
                async with lock:
                    visited.add(url)
                    if url == start and not stats.chunks and stats.pages_crawled == 0:
                        failed_start = True
                return []
            _st, final_url, _ct, html = res
            # JS fallback when almost no text
            extracted = extract_main_content(html, final_url)
            if extracted is None and js is not None:
                try:
                    rendered = await js(final_url)
                except Exception:
                    rendered = None
                if rendered:
                    extracted = extract_main_content(rendered, final_url)
            async with lock:
                visited.add(url)
                if final_url != url:
                    visited.add(final_url)
            new_links: List[Tuple[str, int]] = []
            if extracted is not None:
                title, text = extracted
                for ch in chunk_text(text, final_url, title):
                    stats.chunks.append(ch)
                async with lock:
                    stats.pages_crawled += 1
            else:
                # page fetched but unusable — still counts as attempted, not crawled
                logger.info("No usable text: %s", final_url)
            if depth < max_depth:
                try:
                    for link in extract_links(html, final_url):
                        if link not in visited and is_same_site(link, start) and allowed(link):
                            new_links.append((link, depth + 1))
                except Exception:
                    pass
            return new_links

    # BFS in waves to respect depth
    pending: Dict[str, int] = {start: 0}
    for s in seeds:
        pending.setdefault(s, 1)
    depth = 0
    while pending and len(visited) < max_pages and depth <= max_depth:
        batch = [(u, d) for u, d in pending.items() if d == depth and u not in visited][: max_pages - len(visited)]
        pending = {u: d for u, d in pending.items() if not (d == depth and u not in visited)}
        if not batch:
            depth += 1
            continue
        results = await asyncio.gather(*[process(u, d) for u, d in batch])
        async with lock:
            stats.pages_found = len(visited) + len(pending) + sum(len(r) for r in results)
        for found in results:
            for u, d in found:
                if u not in visited and u not in pending and len(visited) + len(pending) < max_pages * 3:
                    pending[u] = d
        depth += 1
        if stats.pages_crawled >= max_pages:
            break

    if stats.pages_crawled == 0:
        stats.error = (
            f"Could not extract content from the start page ({start}). "
            "The site may be unreachable, JS-only, or blocking crawlers."
            if failed_start or True else "Crawl produced no content."
        )
        return {"status": "failed", "pages_crawled": 0, "pages_found": stats.pages_found,
                "chunks": [], "error": stats.error}
    return {"status": "done", "pages_crawled": stats.pages_crawled,
            "pages_found": stats.pages_found, "chunks": stats.chunks, "error": None}
