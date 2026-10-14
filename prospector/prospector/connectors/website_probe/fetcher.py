"""HTTP fetching under the crawler conduct rules (R4).

- identified user agent, robots.txt honoured (Protego), one request per second per host,
- size and time caps, no authentication, no cookies persistence, no proxies, no retries that
  hammer a host, and an allow-list of passive paths so the probe can only request pages a
  visitor would open.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from protego import Protego

from ...config import Settings, get_settings

# Paths a probe may request. Everything else is refused before any network activity.
PASSIVE_PATHS = [
    re.compile(r"^/$"),
    re.compile(r"^/index\.(php|html?)$", re.IGNORECASE),
    re.compile(r"^/robots\.txt$"),
    re.compile(r"^/sitemap(_index)?\.xml$"),
    re.compile(
        r"^(/[a-z]{2}(-[a-z]{2})?)?/(contact|contacts|contact-us|contactus|get-in-touch|reach-us|"
        r"about|about-us|aboutus|who-we-are|our-story|company|privacy|privacy-policy|terms|"
        r"terms-of-service|terms-and-conditions|imprint|impressum|legal|services|portfolio|"
        r"our-work|work|case-studies|projects)[a-z0-9-]*/?(\.html?|\.php)?$",
        re.IGNORECASE,
    ),
]
ACTIVE_PATHS = [re.compile(r"^/readme\.html$", re.IGNORECASE), re.compile(r"^/wp-json/?$", re.IGNORECASE)]


def host_is_public(host: str) -> bool | None:
    """SSRF guard: True when every resolved address is globally routable, False when any is
    private/loopback/link-local/reserved, None when the name does not resolve (the HTTP client
    will then report its own DNS failure)."""
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    host = host.strip("[]")
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError, OSError):
            return None
        addresses = set()
        for info in infos:
            try:
                addresses.add(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
        if not addresses:
            return None
    return all(addr.is_global and not addr.is_multicast for addr in addresses)


class HostRateLimiter:
    def __init__(self, min_interval_s: float = 1.0) -> None:
        self.min_interval = min_interval_s
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host)
            delay = 0.0 if last is None else max(0.0, self.min_interval - (now - last))
            self._last[host] = now + delay
        if delay > 0:
            time.sleep(delay)


@dataclass
class FetchResult:
    requested_url: str
    final_url: str | None
    status: int | None
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    bytes_len: int = 0
    redirect_chain: list[str] = field(default_factory=list)
    error: str | None = None  # dns_fail | tls_invalid | timeout | connection | too_large | refused
    robots_blocked: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None


class PathNotAllowed(ValueError):
    pass


class Fetcher:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        rate_limiter: HostRateLimiter | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.rate_limiter = rate_limiter or HostRateLimiter(self.settings.crawler_min_interval_s)
        self._client = client
        self._robots: dict[str, Protego | None] = {}
        self.requests_made = 0

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers={"User-Agent": self.settings.crawler_user_agent, "Accept": "text/html,*/*;q=0.5"},
                timeout=self.settings.crawler_timeout_s,
                follow_redirects=False,
                trust_env=False,  # no proxies from the environment
            )
        return self._client

    # --- allow-list ----------------------------------------------------------------------
    def path_allowed(self, url: str) -> bool:
        path = urlsplit(url).path or "/"
        if any(p.match(path) for p in PASSIVE_PATHS):
            return True
        return bool(self.settings.crawler_active_probes and any(p.match(path) for p in ACTIVE_PATHS))

    def _host_allowed(self, url: str) -> bool:
        if self.settings.crawler_allow_private_hosts:
            return True
        return host_is_public(urlsplit(url).hostname or "") is not False

    # --- robots --------------------------------------------------------------------------
    def _robots_for(self, url: str) -> Protego | None:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base in self._robots:
            return self._robots[base]
        robots_url = base + "/robots.txt"
        rp: Protego | None = None
        try:
            self.rate_limiter.wait(parts.netloc)
            resp = self.client.get(robots_url, follow_redirects=True)
            self.requests_made += 1
            if resp.status_code == 200 and len(resp.content) < 512_000:
                rp = Protego.parse(resp.text)
            elif 400 <= resp.status_code < 500:
                rp = None  # no robots file: everything allowed
            else:
                rp = Protego.parse("User-agent: *\nDisallow: /\n")  # 5xx: be conservative
        except httpx.HTTPError:
            rp = None
        self._robots[base] = rp
        return rp

    def can_fetch(self, url: str) -> bool:
        rp = self._robots_for(url)
        if rp is None:
            return True
        agent = self.settings.crawler_user_agent.split("/")[0]
        return bool(rp.can_fetch(url, agent))

    # --- fetch ---------------------------------------------------------------------------
    def get(self, url: str, *, max_redirects: int = 5, check_robots: bool = True) -> FetchResult:
        if not self.path_allowed(url):
            raise PathNotAllowed(f"refusing non-passive path: {url}")
        result = FetchResult(requested_url=url, final_url=None, status=None)
        if not self._host_allowed(url):
            result.error = "blocked_private_address"
            return result
        if check_robots and not self.can_fetch(url):
            result.robots_blocked = True
            result.error = "robots_disallow"
            return result
        current = url
        for _ in range(max_redirects + 1):
            host = urlsplit(current).netloc
            self.rate_limiter.wait(host)
            try:
                with self.client.stream("GET", current) as resp:
                    self.requests_made += 1
                    status = resp.status_code
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    if status in (301, 302, 303, 307, 308) and headers.get("location"):
                        nxt = urljoin(current, headers["location"])
                        result.redirect_chain.append(nxt)
                        if urlsplit(nxt).netloc != host and (not self.path_allowed(nxt) or not self._host_allowed(nxt)):
                            result.error = "refused"
                            result.status = status
                            return result
                        if check_robots and not self.can_fetch(nxt):
                            result.robots_blocked = True
                            result.error = "robots_disallow"
                            return result
                        current = nxt
                        continue
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > self.settings.crawler_max_bytes:
                            result.error = "too_large"
                            break
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    encoding = resp.encoding or "utf-8"
                    try:
                        text = body.decode(encoding, errors="replace")
                    except LookupError:
                        text = body.decode("utf-8", errors="replace")
                    result.final_url = current
                    result.status = status
                    result.headers = headers
                    result.text = text
                    result.bytes_len = total
                    if result.error == "too_large":
                        result.error = None  # truncated but usable
                    return result
            except httpx.ConnectError as exc:
                msg = str(exc).lower()
                if "certificate" in msg or "ssl" in msg or "tls" in msg:
                    result.error = "tls_invalid"
                elif "name or service not known" in msg or "nodename" in msg or "getaddrinfo" in msg \
                        or "temporary failure in name resolution" in msg or "no address" in msg:
                    result.error = "dns_fail"
                else:
                    result.error = "connection"
                return result
            except httpx.TimeoutException:
                result.error = "timeout"
                return result
            except httpx.HTTPError as exc:
                result.error = f"http_error:{type(exc).__name__}"
                return result
        result.error = "too_many_redirects"
        return result
