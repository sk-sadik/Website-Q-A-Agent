"""Crawler on a local mock site: limits, same-domain, robots, broken pages."""
import pytest

from app.crawler import crawl_site, extract_links, is_same_site, playwright_fallback


def _page(title, body, links=()):
    anchors = "".join(f'<a href="{l}">x</a>' for l in links)
    html = (f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>"
            f"<p>{body}</p>{anchors}</article></body></html>")
    return (200, "", "text/html", html)


LONG = "word " * 300


def make_fetch(pages, record=None):
    async def fetch(url):
        (record or []).append(url)
        if url in pages:
            entry = pages[url]
            if isinstance(entry, Exception):
                raise entry
            if entry is None:
                return None
            status, final, ctype, html = entry
            return (status, final or url, ctype, html)
        return None
    return fetch


def test_depth_and_page_limits():
    pages = {
        "https://site.test/": _page("Home", LONG, ["/a", "/b"]),
        "https://site.test/a": _page("A", LONG, ["/a1"]),
        "https://site.test/b": _page("B", LONG, []),
        "https://site.test/a1": _page("A1", LONG, []),
    }
    # depth 1: home + a + b, not a1
    import asyncio
    r = asyncio.run(crawl_site("https://site.test/", max_pages=30, max_depth=1,
                               polite_delay=0, fetch_fn=make_fetch(pages), robots_txt=""))
    assert r["status"] == "done"
    assert r["pages_crawled"] == 3
    urls = {c["url"] for c in r["chunks"]}
    assert "https://site.test/a1" not in urls


def test_same_domain_filtering():
    import asyncio
    pages = {
        "https://site.test/": _page("Home", LONG, ["/in", "https://evil.test/steal"]),
        "https://site.test/in": _page("In", LONG, []),
        "https://evil.test/steal": _page("Steal", LONG, []),
    }
    r = asyncio.run(crawl_site("https://site.test/", max_pages=30, max_depth=1,
                               polite_delay=0, fetch_fn=make_fetch(pages), robots_txt=""))
    urls = {c["url"] for c in r["chunks"]}
    assert "https://site.test/in" in urls
    assert "https://evil.test/steal" not in urls
    assert is_same_site("https://www.site.test/x", "https://site.test/")
    assert not is_same_site("https://other.test/", "https://site.test/")


def test_robots_disallow_and_broken_page_survives():
    import asyncio
    pages = {
        "https://site.test/": _page("Home", LONG, ["/blocked", "/broken", "/ok"]),
        "https://site.test/ok": _page("OK", LONG, []),
        "https://site.test/broken": RuntimeError("boom"),
    }
    robots = "User-agent: *\nDisallow: /blocked\n"
    r = asyncio.run(crawl_site("https://site.test/", max_pages=30, max_depth=1,
                               polite_delay=0, fetch_fn=make_fetch(pages), robots_txt=robots))
    assert r["status"] == "done"
    urls = {c["url"] for c in r["chunks"]}
    assert "https://site.test/ok" in urls
    assert not any(u.startswith("https://site.test/blocked") for u in urls)
    assert r["pages_crawled"] == 2  # home + ok; broken page logged, crawl continues


def test_start_page_failure_is_clear():
    import asyncio
    r = asyncio.run(crawl_site("https://site.test/", max_pages=10, max_depth=1,
                               polite_delay=0, fetch_fn=make_fetch({}), robots_txt=""))
    assert r["status"] == "failed"
    assert r["error"] and "start page" in r["error"].lower()


def test_extract_links_skips_junk():
    html = ('<a href="/ok">a</a><a href="mailto:x@y">b</a>'
            '<a href="javascript:void(0)">c</a><a href="/f.pdf">d</a>')
    assert extract_links(html, "https://site.test/") == ["https://site.test/ok"]


def test_playwright_fallback_enforces_ssrf_guard():
    """Blocked hosts are rejected before any browser launches (no network)."""
    import asyncio
    assert asyncio.run(playwright_fallback("http://127.0.0.1/")) is None
    assert asyncio.run(playwright_fallback("http://localhost:8000/")) is None
    assert asyncio.run(playwright_fallback("http://169.254.169.254/")) is None
