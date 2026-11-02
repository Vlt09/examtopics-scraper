import threading
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .cache import HtmlCache
from .parsers import raise_if_blocked
from .settings import (
    MAX_HTTP_POOL_SIZE,
    MIN_REQUEST_INTERVAL_SECONDS,
    RATE_LIMIT_BACKOFF_SECONDS,
    RATE_LIMIT_MAX_BACKOFF_SECONDS,
    REQUEST_HEADERS,
)

RATE_LIMIT_STATUSES = (429, 503)


class RateLimiter:
    """Shared across threads: spaces requests out and pauses everyone after a 429."""

    def __init__(self, min_interval: float = MIN_REQUEST_INTERVAL_SECONDS):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_allowed)
            self._next_allowed = start + self.min_interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)

    def pause(self, seconds: float):
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)


def _retry_after_seconds(response: requests.Response) -> Optional[float]:
    value = response.headers.get("Retry-After", "").strip()
    if value.isdigit():
        return float(value)
    return None


class SessionFactory:
    def __init__(self, pool_size: int = MAX_HTTP_POOL_SIZE):
        self.pool_size = pool_size
        self._thread_local = threading.local()

    def get(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is not None:
            return session

        retry = Retry(
            total=0,
            connect=0,
            read=0,
            redirect=2,
            status=0,
            allowed_methods=frozenset(["GET"]),
        )
        adapter = HTTPAdapter(
            pool_connections=self.pool_size,
            pool_maxsize=self.pool_size,
            max_retries=retry,
        )
        session = requests.Session()
        session.headers.update(REQUEST_HEADERS)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        self._thread_local.session = session
        return session


class HttpFetcher:
    def __init__(
        self,
        *,
        timeout: int,
        retries: int,
        cache: Optional[HtmlCache],
        refresh_cache: bool = False,
        session_factory: Optional[SessionFactory] = None,
        rate_limiter: Optional[RateLimiter] = None,
    ):
        self.timeout = timeout
        self.retries = retries
        self.cache = cache
        self.refresh_cache = refresh_cache
        self.session_factory = session_factory or SessionFactory()
        self.rate_limiter = rate_limiter or RateLimiter()

    def fetch_html(self, url: str) -> str:
        if self.cache is not None and not self.refresh_cache:
            cached_html = self.cache.read(url)
            if cached_html is not None:
                return cached_html

        last_error = None
        rate_limit_hits = 0
        for attempt in range(1, self.retries + 1):
            try:
                self.rate_limiter.wait()
                response = self.session_factory.get().get(url, timeout=self.timeout)
                if response.status_code in RATE_LIMIT_STATUSES:
                    rate_limit_hits += 1
                    wait = _retry_after_seconds(response) or min(
                        RATE_LIMIT_BACKOFF_SECONDS * (2 ** (rate_limit_hits - 1)),
                        RATE_LIMIT_MAX_BACKOFF_SECONDS,
                    )
                    last_error = RuntimeError(f"HTTP {response.status_code} (rate limited)")
                    if attempt < self.retries:
                        print(f"\nRate limited (HTTP {response.status_code}), waiting {wait:.0f}s...")
                        self.rate_limiter.pause(wait)
                    continue
                response.raise_for_status()
                raise_if_blocked(response.text)
                if self.cache is not None:
                    self.cache.write(url, response.text)
                return response.text
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(1.0 * attempt)

        raise RuntimeError(f"Failed to load {url}: {last_error}")

