"""Small HTTP layer: stdlib-only GET requests with retries and an on-disk TTL cache.

Every data source talks to the network through an object that satisfies the
``HttpClient`` protocol, so tests and demo mode can swap in canned responses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib import error, request
from urllib.parse import urlencode

from mlb_prop_predictor import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"mlb-prop-predictor/{__version__} (+https://github.com/JoshuaDHaber/MLB-Prop-Predictor)"

# Query parameters that must never be written into cache keys or logs.
_SECRET_PARAMS = {"apiKey", "api_key", "apikey", "token"}


class HttpError(RuntimeError):
    """Raised when a request fails after all retries."""


@dataclass
class Response:
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False

    def json(self) -> Any:
        return json.loads(self.text)


class HttpClient(Protocol):
    def get(self, url: str, params: dict[str, Any] | None = None, ttl: int = 0) -> Response: ...


def build_url(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    clean = {k: v for k, v in params.items() if v is not None}
    return f"{url}?{urlencode(clean, safe=',()[]')}"


def redact(url: str, params: dict[str, Any] | None) -> str:
    """URL with secret query parameters masked; safe for logs and cache keys."""
    safe = {k: ("***" if k in _SECRET_PARAMS else v) for k, v in (params or {}).items()}
    return build_url(url, safe)


class CachedHttpClient:
    """GET client with retry/backoff and a JSON-on-disk cache keyed by redacted URL.

    ``ttl`` is per call, in seconds; ``0`` disables caching for that call.
    """

    def __init__(
        self,
        cache_dir: Path | None,
        timeout: float = 20.0,
        retries: int = 3,
        backoff: float = 1.5,
    ) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)

    # -- cache -------------------------------------------------------------
    def _cache_path(self, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, key: str, ttl: int) -> Response | None:
        path = self._cache_path(key)
        if path is None or ttl <= 0 or not path.exists():
            return None
        if time.time() - path.stat().st_mtime > ttl:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return Response(text=payload["text"], headers=payload.get("headers", {}), from_cache=True)
        except (OSError, ValueError, KeyError):
            return None

    def _write_cache(self, key: str, response: Response) -> None:
        path = self._cache_path(key)
        if path is None:
            return
        try:
            path.write_text(json.dumps({"url": key, "text": response.text, "headers": response.headers}))
        except OSError as exc:  # cache problems should never break a run
            log.debug("cache write failed for %s: %s", key, exc)

    # -- network -----------------------------------------------------------
    def get(self, url: str, params: dict[str, Any] | None = None, ttl: int = 0) -> Response:
        key = redact(url, params)
        cached = self._read_cache(key, ttl)
        if cached is not None:
            log.debug("cache hit %s", key)
            return cached

        full_url = build_url(url, params)
        req = request.Request(full_url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                log.debug("GET %s (attempt %d)", key, attempt)
                with request.urlopen(req, timeout=self.timeout) as resp:
                    text = resp.read().decode("utf-8-sig", errors="replace")
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                response = Response(text=text, headers=headers)
                if ttl > 0:
                    self._write_cache(key, response)
                return response
            except error.HTTPError as exc:
                last_exc = exc
                # 4xx (other than rate limiting) will not succeed on retry.
                if 400 <= exc.code < 500 and exc.code != 429:
                    break
            except (error.URLError, TimeoutError, ConnectionError) as exc:
                last_exc = exc
            if attempt < self.retries:
                time.sleep(self.backoff**attempt)
        raise HttpError(f"GET {key} failed: {last_exc}") from last_exc
