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


LOGIN_HTML = """<html><head><title>ERP Login</title></head><body>
<h1>Sign in</h1><p>Please enter your credentials to continue</p>
<form action="/login" method="post">
<input type="text" name="username" placeholder="Username">
<input type="password" name="password" placeholder="Password">
<button type="submit">Login</button></form></body></html>"""


def test_login_only_page_reports_login_required():
    """Password form + short text -> failed with LOGIN_REQUIRED, nothing indexed."""
    import asyncio
    from app.cleaner import extract_main_content, is_login_page

    assert is_login_page(LOGIN_HTML)
    assert not is_login_page("<html><body><p>Hello world</p></body></html>")
    entry = (200, "", "text/html", LOGIN_HTML)
    r = asyncio.run(crawl_site("https://erp.test/", max_pages=10, max_depth=0,
                               polite_delay=0,
                               fetch_fn=make_fetch({"https://erp.test/": entry}),
                               robots_txt="", js_fallback=None))
    assert r["status"] == "failed"
    assert r["error_code"] == "LOGIN_REQUIRED"
    assert r["pages_crawled"] == 0
    assert "login" in (r["error"] or "").lower()


def test_thin_page_triggers_playwright_fallback():
    """Extracted text under ~200 chars must attempt the JS fallback."""
    import asyncio
    thin = _page("Thin", "word " * 30, [])  # ~150 chars, no password form
    rich = _page("Rich", LONG, [])
    calls = []

    async def fake_js(url):
        calls.append(url)
        return rich[3]

    r = asyncio.run(crawl_site("https://js.test/", max_pages=10, max_depth=0,
                               polite_delay=0,
                               fetch_fn=make_fetch({"https://js.test/": thin}),
                               robots_txt="", js_fallback=fake_js))
    assert calls == ["https://js.test/"]
    assert r["status"] == "done"
    assert r["pages_crawled"] == 1
