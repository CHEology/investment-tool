"""SEC EDGAR access layer: identity gate, ONE global rate limiter shared by
every SEC adapter, and pure offline parsers.

Fair-access compliance (verified from SEC's guidance page): max 10 req/s and a
declared User-Agent of the form "Name contact@domain". We target 4 req/s with
burst 8. The UA value is runtime configuration (env SEC_USER_AGENT or Keychain
service 'investment-tool-sec-ua'); it is never written to code, git, fixtures,
logs, manifests, or source URLs. Live fetching REFUSES to start without a
credible non-placeholder value — offline parsers need none.
"""

from __future__ import annotations

import json
import re
import threading
import time

from investment_tool.providers.base import HttpClient

MAX_TARGET_RPS = 4.0
BURST = 8

TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{q}/master.{ymd}.idx"
GETCURRENT_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&company="
    "&owner=include&count=100&output=atom"
)
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"

_PLACEHOLDER = re.compile(r"example\.(com|org)|placeholder|your[-_ ]?(name|email)|contact@example",
                          re.I)


class SecConfigError(RuntimeError):
    pass


def require_user_agent() -> str:
    import os

    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        try:
            import getpass

            import keyring

            ua = (keyring.get_password("investment-tool-sec-ua", getpass.getuser()) or "").strip()
        except Exception:  # noqa: BLE001 - keyring absence is a normal condition
            ua = ""
    if not ua or "@" not in ua or _PLACEHOLDER.search(ua):
        raise SecConfigError(
            "SEC_USER_AGENT missing or placeholder. Live SEC access requires a declared"
            " identity 'Name contact@domain' (SEC fair-access policy). Set the env var or"
            " store it via Keychain service 'investment-tool-sec-ua'. Offline fixtures"
            " need no identity."
        )
    return ua


class GlobalRateLimiter:
    """Token bucket shared across every SEC adapter in this process."""

    def __init__(self, rate: float = MAX_TARGET_RPS, burst: int = BURST,
                 clock=time.monotonic, sleeper=time.sleep):
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last = clock()
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Blocks until a token is available; returns seconds waited."""
        waited = 0.0
        with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                need = (1.0 - self._tokens) / self.rate
                self._sleep(need)
                waited += need


_LIMITER = GlobalRateLimiter()


def client() -> HttpClient:
    http = HttpClient(user_agent=require_user_agent(), min_interval_s=0.0, timeout_s=30.0)
    original = http._request

    def limited(method, url, **kwargs):
        _LIMITER.acquire()
        return original(method, url, **kwargs)

    http._request = limited  # type: ignore[method-assign]
    return http


# ----------------------------- offline parsers -----------------------------

def parse_company_tickers_exchange(payload: bytes) -> list[dict]:
    data = json.loads(payload.decode("utf-8"))
    fields = data["fields"]
    out = []
    for row in data["data"]:
        rec = dict(zip(fields, row, strict=True))
        out.append(
            {
                "cik": str(rec["cik"]),
                "name": rec.get("name"),
                "ticker": (rec.get("ticker") or "").upper(),
                "exchange": rec.get("exchange"),
            }
        )
    return [r for r in out if r["ticker"]]


def accession_from_path(path: str) -> str | None:
    m = re.search(r"(\d{10}-\d{2}-\d{6})", path.replace("/", "-"))
    if m:
        return m.group(1)
    m = re.search(r"/(\d{18})[./]", path)
    if m:
        d = m.group(1)
        return f"{d[:10]}-{d[10:12]}-{d[12:]}"
    return None


def parse_master_idx(payload: bytes) -> list[dict]:
    text = payload.decode("latin-1")
    rows = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) != 5 or parts[0] in ("CIK", "") or not parts[0].strip().isdigit():
            continue
        cik, name, form, date_filed, path = (p.strip() for p in parts)
        accession = accession_from_path(path)
        if accession is None:
            continue
        rows.append(
            {
                "cik": cik, "company_name": name, "form": form,
                "filing_date": f"{date_filed[:4]}-{date_filed[4:6]}-{date_filed[6:]}",
                "path": path, "accession": accession,
            }
        )
    return rows


def parse_getcurrent_atom(payload: bytes) -> list[dict]:
    """Best-effort entries from the (demonstrably lossy) latest-filings feed."""
    text = payload.decode("utf-8", errors="replace")
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", text, re.S):
        title = re.search(r"<title>([^<]*)</title>", entry)
        acc = re.search(r"AccNo:&lt;/b&gt;\s*(\d{10}-\d{2}-\d{6})", entry)
        updated = re.search(r"<updated>([^<]*)</updated>", entry)
        if not (title and acc):
            continue
        m = re.match(r"\s*([A-Z0-9/\- ]+?) - .*\((\d{10})\)\s*\((Filer|Filed by)\)", title.group(1))
        out.append(
            {
                "form": m.group(1).strip() if m else None,
                "cik": str(int(m.group(2))) if m else None,
                "role_hint": m.group(3) if m else None,
                "accession": acc.group(1),
                "updated": updated.group(1) if updated else None,
            }
        )
    return out


def parse_efts_items(payload: bytes) -> dict[str, dict]:
    data = json.loads(payload.decode("utf-8"))
    out: dict[str, dict] = {}
    for hit in data.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        acc = src.get("adsh") or (hit.get("_id", "").split(":", 1)[0] or None)
        if not acc:
            continue
        rec = out.setdefault(acc, {"items": [], "ciks": []})
        for it in src.get("items") or []:
            if it not in rec["items"]:
                rec["items"].append(it)
        for c in src.get("ciks") or []:
            c = str(int(c))
            if c not in rec["ciks"]:
                rec["ciks"].append(c)
    return out


def parse_submissions_recent(payload: bytes) -> list[dict]:
    data = json.loads(payload.decode("utf-8"))
    r = data.get("filings", {}).get("recent", {})
    keys = ("accessionNumber", "form", "filingDate", "reportDate", "acceptanceDateTime",
            "items", "primaryDocument")
    n = len(r.get("accessionNumber", []))
    out = []
    for i in range(n):
        out.append({k: (r.get(k) or [None] * n)[i] for k in keys})
    return out
