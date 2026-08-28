"""Nasdaq Trader official symbol directories (REFERENCE role).

nasdaqlisted.txt: Nasdaq-listed issues. otherlisted.txt: other-exchange
issues (we admit NYSE 'N' and NYSE American 'A'; Arca/BATS/IEX listings are
overwhelmingly ETFs and out of V1 scope). ETFs, test issues, obvious
warrants/rights/units/preferred are excluded from the common-equity universe.
CIK enrichment is deferred to S2 (needs the user's SEC_USER_AGENT, D3).
"""

from __future__ import annotations

import re

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

_NON_COMMON = re.compile(
    r"\b(warrant|warrants|right|rights|unit|units|preferred|preference)\b", re.I
)
_ADR = re.compile(r"american depositary|depositary sh", re.I)
_OTHER_EXCHANGES = {"N": "NYSE", "A": "AMEX"}


def _rows(text: str) -> list[list[str]]:
    lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.startswith("File Creation Time")
    ]
    return [ln.split("|") for ln in lines[1:]]  # skip header


def parse_nasdaq_listed(text: str) -> list[dict]:
    out = []
    for f in _rows(text):
        if len(f) < 8:
            continue
        symbol, name, _cat, test, _fin, _lot, etf, _nx = f[:8]
        if test == "Y" or etf == "Y" or "$" in symbol or _NON_COMMON.search(name):
            continue
        out.append(
            {"ticker": symbol, "name_en": name, "exchange": "NASDAQ",
             "is_adr": 1 if _ADR.search(name) else 0}
        )
    return out


def parse_other_listed(text: str) -> list[dict]:
    out = []
    for f in _rows(text):
        if len(f) < 8:
            continue
        symbol, name, exch, _cqs, etf, _lot, test, _nsym = f[:8]
        if exch not in _OTHER_EXCHANGES or test == "Y" or etf == "Y":
            continue
        if "$" in symbol or _NON_COMMON.search(name):
            continue
        out.append(
            {"ticker": symbol, "name_en": name, "exchange": _OTHER_EXCHANGES[exch],
             "is_adr": 1 if _ADR.search(name) else 0}
        )
    return out
