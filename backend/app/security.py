"""URL normalization, validation, SSRF guard, and rate limiting."""
from __future__ import annotations

import ipaddress
import socket
import time
from collections import defaultdict, deque
from typing import Tuple
from urllib.parse import urlparse, urlunparse

MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 3 * 1024 * 1024

_BLOCKED_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),  # reserved
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
]

_BLOCKED_HOSTNAMES = {"localhost"}


def normalize_url(url: str) -> str:
    """Add https:// if missing, strip fragment, lowercase host, drop default ports."""
    url = (url or "").strip()
    if not url:
        return url
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if not host:
        return url
    # rebuild netloc without credentials, without default ports
    netloc = host
    if p.port and not ((p.scheme == "http" and p.port == 80) or (p.scheme == "https" and p.port == 443)):
        netloc = f"{host}:{p.port}"
    path = p.path or "/"
    return urlunparse((p.scheme.lower(), netloc, path, "", p.query, ""))


def is_valid_scheme(url: str) -> bool:
    return urlparse(url).scheme in ("http", "https")


def has_credentials(url: str) -> bool:
    p = urlparse(url)
    return bool(p.username or p.password)


def is_ip_blocked(ip_str: str) -> bool:
    """True if the literal IP is in a blocked range."""
    try:
        ip = ipaddress.ip_address(ip_str.strip().strip("[]"))
    except ValueError:
        return False  # not an IP literal; hostname checks happen via DNS
    return any(ip in net for net in _BLOCKED_NETS)


def resolve_ips(hostname: str) -> list[str]:
    """Resolve hostname to IP strings. Returns [] on failure."""
    host = hostname.strip().strip("[]")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    out: list[str] = []
    for _fam, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        if ip not in out:
            out.append(ip)
    return out


def hostname_is_blocked(hostname: str) -> bool:
    """Block localhost names, blocked IP literals, and anything resolving to blocked IPs."""
    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host in _BLOCKED_HOSTNAMES:
        return True
    if is_ip_blocked(host):
        return True
    for ip in resolve_ips(host):
        if is_ip_blocked(ip):
            return True
    return False


def validate_url_security(url: str) -> Tuple[bool, str]:
    """Full pre-flight check. Returns (ok, reason)."""
    url = (url or "").strip()
    if not url:
        return False, "URL is empty"
    # add scheme for scheme check so bare hosts are judged as https
    probe = url if "://" in url else "https://" + url
    if not is_valid_scheme(probe):
        return False, "Only http:// and https:// URLs are allowed"
    if has_credentials(probe):
        return False, "URLs with credentials (user:pass@host) are not allowed"
    host = urlparse(normalize_url(url)).hostname or ""
    if not host:
        return False, "Could not parse hostname from URL"
    if hostname_is_blocked(host):
        return False, f"Blocked by SSRF guard: {host} resolves to a private/loopback/link-local address"
    return True, ""


class RateLimiter:
    """Sliding-window per-IP rate limiter."""

    def __init__(self, per_minute: int = 20):
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, ip: str) -> bool:
        """True if allowed (and records the hit), False if rate limited."""
        now = time.monotonic()
        window = self._hits[ip]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return False
        window.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()
