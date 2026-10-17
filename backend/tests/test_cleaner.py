"""Cleaner: trafilatura first, BeautifulSoup fallback, login detection."""
from app.cleaner import _bs_fallback, extract_main_content, is_login_page


def test_bs_fallback_recovers_title_headings_links():
    html = """<html><head><title>Dept of CSE</title>
    <meta name="description" content="CSE courses and faculty"></head>
    <body><nav>Home About</nav><h1>Computer Science</h1>
    <p>We offer B.Tech and M.Tech programs.</p>
    <a href="/faculty">Faculty list</a>
    <footer>copyright</footer>
    <script>var x = 1;</script></body></html>"""
    title, text = _bs_fallback(html)
    assert title == "Dept of CSE"
    assert "Computer Science" in text
    assert "Faculty list" in text
    assert "CSE courses and faculty" in text
    assert "copyright" not in text
    assert "var x" not in text


def test_bs_fallback_fails_when_still_tiny():
    assert _bs_fallback("<html><body><p>hi</p></body></html>") is None


def test_extract_prefers_trafilatura_but_falls_back():
    long_html = ("<html><head><title>Big</title></head><body><article>" +
                 "<p>Sentence one. Sentence two.</p>" * 60 +
                 "</article></body></html>")
    title, text = extract_main_content(long_html, "https://x.test/")
    assert len(text) >= 200
    thin = "<html><head><title>T</title></head><body><p>" + "word " * 30 + "</p></body></html>"
    title2, text2 = extract_main_content(thin, "https://x.test/")
    assert len(text2) >= 50  # BS fallback rescued it


def test_is_login_page():
    assert is_login_page('<form><input type="password" name="p"></form>')
    assert is_login_page('<INPUT TYPE="PASSWORD">')
    assert not is_login_page('<form><input type="text" name="q"></form>')
    assert not is_login_page("")
