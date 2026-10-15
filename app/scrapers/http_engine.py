"""HTTP transport with sane defaults, retries and structured results."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


@dataclass
class HttpResponse:
    """Transport-independent view of a response (easy to fabricate in tests)."""

    status_code: int = 0
    text: str = ""
    url: str = ""
    headers: dict = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status_code < 400

    @property
    def content_type(self) -> str:
        return (self.headers or {}).get("Content-Type", "") or ""

    def __bool__(self) -> bool:
        return not self.error


class HttpEngine:
    """Thin wrapper around :mod:`requests` with pooling, retries and timeouts."""

    def __init__(self, timeout: int = 30, user_agent: str | None = None,
                 proxy: str | None = None, retries: int = 2, verify: bool = True,
                 session: requests.Session | None = None):
        self.timeout = timeout
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        self.proxy = proxy or None
        self.retries = retries
        self.verify = verify
        self.session = session or self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=self.retries,
            connect=self.retries,
            read=self.retries,
            status=self.retries,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        return session

    def request(self, method: str, url: str, data: Any = None, json_body: Any = None,
                headers: dict | None = None, params: dict | None = None,
                timeout: int | None = None, allow_redirects: bool = True) -> HttpResponse:
        started = time.monotonic()
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        try:
            response = self.session.request(
                method=method.upper(),
                url=url,
                data=data,
                json=json_body,
                headers=headers,
                params=params,
                timeout=timeout or self.timeout,
                allow_redirects=allow_redirects,
                proxies=proxies,
                verify=self.verify,
            )
            return HttpResponse(
                status_code=response.status_code,
                text=response.text or "",
                url=response.url,
                headers=dict(response.headers),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except requests.RequestException as exc:
            logger.debug("HTTP %s %s failed: %s", method, url, exc)
            return HttpResponse(
                status_code=0,
                url=url,
                error=f"{type(exc).__name__}: {exc}"[:300],
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    def get(self, url: str, **kwargs) -> HttpResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, data: Any = None, json_body: Any = None, **kwargs) -> HttpResponse:
        return self.request("POST", url, data=data, json_body=json_body, **kwargs)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover - defensive
            pass
