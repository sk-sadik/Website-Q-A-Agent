"""URL normalization/validation + SSRF guard + rate limiter."""
import socket

from app.security import (
    RateLimiter,
    has_credentials,
    hostname_is_blocked,
    is_ip_blocked,
    is_valid_scheme,
    normalize_url,
    validate_url_security,
)


def test_normalize_adds_scheme_strips_fragment_lowercases():
    assert normalize_url("example.com") == "https://example.com/"
    assert normalize_url("https://EXAMPLE.com/Page#sec") == "https://example.com/Page"
    assert normalize_url("http://example.com:80/a") == "http://example.com/a"


def test_scheme_and_credentials():
    assert is_valid_scheme("https://example.com")
    assert is_valid_scheme("http://example.com")
    assert not is_valid_scheme("ftp://example.com")
    assert not is_valid_scheme("javascript:alert(1)")
    assert has_credentials("http://user:pass@example.com/")
    assert not has_credentials("https://example.com/")


def test_ssrf_literals_blocked():
    for ip in ["127.0.0.1", "10.1.2.3", "192.168.1.1", "172.16.0.5",
               "169.254.169.254", "224.0.0.1", "::1"]:
        assert is_ip_blocked(ip), ip
        assert hostname_is_blocked(ip), ip
    assert hostname_is_blocked("localhost")
    assert hostname_is_blocked("LOCALHOST")
    assert not is_ip_blocked("93.184.216.34")  # example.com public


def test_ssrf_dns_redirect_to_private(monkeypatch):
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host == "evil.example":
            return [(socket.AF_INET, 1, 6, "", ("10.9.9.9", 0))]
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert hostname_is_blocked("evil.example")
    ok, reason = validate_url_security("http://evil.example/")
    assert not ok and "SSRF" in reason


def test_validate_rejects_bad_and_private():
    ok, _ = validate_url_security("ftp://example.com/file")
    assert not ok
    ok, _ = validate_url_security("http://user:pw@example.com/")
    assert not ok
    for u in ["http://localhost:8000/", "http://127.0.0.1/", "http://169.254.169.254/"]:
        ok, reason = validate_url_security(u)
        assert not ok, u


def test_rate_limiter():
    rl = RateLimiter(per_minute=3)
    assert all(rl.check("1.2.3.4") for _ in range(3))
    assert not rl.check("1.2.3.4")
    assert rl.check("5.6.7.8")  # per-IP
