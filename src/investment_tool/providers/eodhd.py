"""EODHD end-of-day API — VERIFY tier for shortlisted US names.

Free-plan reality (probed live): real tickers are served, but the daily call
quota is small (~20). This adapter is therefore reserved for finalist
verification, never bulk coverage. The token comes from Keychain service
'investment-tool-eodhd'; manifests store a credential-free URL. The archived
prior implementation remains TCC-inaccessible in the user's Trash, so this is
a fresh minimal adapter against the live-verified endpoint shape.
"""

from __future__ import annotations

import subprocess

from investment_tool.providers.base import GENERIC_UA, HttpClient

BASE = "https://eodhd.com/api/eod/{ticker}"


class EodhdConfigError(RuntimeError):
    pass


def token() -> str:
    out = subprocess.run(
        ["security", "find-generic-password", "-s", "investment-tool-eodhd", "-w"],
        capture_output=True, text=True,
    )
    value = out.stdout.strip()
    if not value:
        raise EodhdConfigError("Keychain service 'investment-tool-eodhd' has no value")
    return value


def client() -> HttpClient:
    return HttpClient(user_agent=GENERIC_UA, min_interval_s=1.0)


def fetch_eod(http: HttpClient, ticker: str, start: str):
    """Returns (payload, status, credential_free_url). The token travels only
    in the request, never into manifests or logs."""
    url = BASE.format(ticker=ticker)
    resp = http.get(url, params={"api_token": token(), "fmt": "json", "from": start})
    return resp.content, resp.status_code, f"{url}?fmt=json&from={start}"
