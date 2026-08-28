"""Provider adapter base: polite HTTP with per-provider UA/throttle, and the
discovery-vs-evidence role separation (DESIGN 5.9/7).
"""

from __future__ import annotations

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
    def __init__(self, user_agent: str, min_interval_s: float = 0.0, timeout_s: float = 30.0):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self._last = 0.0

    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, url: str, **kwargs) -> requests.Response:
        self._throttle()
        kwargs.setdefault("timeout", self.timeout_s)
        return self.session.get(url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        self._throttle()
        kwargs.setdefault("timeout", self.timeout_s)
        return self.session.post(url, **kwargs)


GENERIC_UA = "investment-tool/0.2 (personal research)"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
