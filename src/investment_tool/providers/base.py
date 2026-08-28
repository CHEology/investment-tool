"""Provider adapter base: polite HTTP with per-provider UA/throttle, and the
discovery-vs-evidence role separation (DESIGN 5.9/7).
"""

from __future__ import annotations

import random
import time
from enum import StrEnum

import requests


class Role(StrEnum):
    SCAN = "SCAN"
    EVIDENCE = "EVIDENCE"
    DISCOVERY = "DISCOVERY"
    VERIFY = "VERIFY"
    REFERENCE = "REFERENCE"


class HttpClient:
    def __init__(self, user_agent: str, min_interval_s: float = 0.0, timeout_s: float = 30.0,
                 keep_alive: bool = True, extra_headers: dict | None = None,
                 rotate_every: int = 0):
        self._user_agent = user_agent
        self._keep_alive = keep_alive
        self._extra_headers = extra_headers or {}
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self._last = 0.0
        self._count = 0
        self._rotate_every = rotate_every
        self.session = self._new_session()

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers["User-Agent"] = self._user_agent
        session.headers.update(self._extra_headers)
        if not self._keep_alive:
            session.headers["Connection"] = "close"
        return session

    def _rebuild(self) -> None:
        try:
            self.session.close()
        finally:
            self.session = self._new_session()

    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout_s)
        last: requests.Response | None = None
        for attempt in range(3):
            self._throttle()
            self._count += 1
            if self._rotate_every and self._count % self._rotate_every == 0:
                self._rebuild()
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.RequestException:
                # poisoned keep-alive pools cause RemoteDisconnected storms:
                # rebuild the session before retrying.
                self._rebuild()
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1) + random.uniform(0, 0.5))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = resp
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = min(float(retry_after), 120.0) if retry_after else None
                except ValueError:
                    delay = None
                time.sleep(delay if delay is not None
                           else 1.5 * (attempt + 1) + random.uniform(0, 0.5))
                continue
            return resp
        return last  # type: ignore[return-value]

    def get(self, url: str, **kwargs) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self._request("POST", url, **kwargs)


GENERIC_UA = "investment-tool/0.2 (personal research)"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
