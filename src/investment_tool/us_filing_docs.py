"""Selective SEC filing-document retrieval and text extraction.

Only filings that survive the price/event filter get their primary document
fetched (never all 1,640). Documents are manifested like every external
download; extracted text lives in the data dir (never in Git). Lookahead
honesty: the document itself was public at acceptance time, but the system's
knowledge is governed by sec_filing.first_seen_at_utc — evaluation cutoffs
must filter on that, and the trial does.
"""

from __future__ import annotations

import html
import re
import sqlite3
from pathlib import Path

from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import record_fetch
from investment_tool.quality import Quality, QualityState

DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"


def doc_url(cik: str, accession: str, doc: str) -> str:
    return DOC_URL.format(cik=int(cik), acc_nodash=accession.replace("-", ""), doc=doc)


def text_path(accession: str) -> Path:
    return DEFAULT_DATA_DIR / "research" / "filings" / f"{accession}.txt"


def extract_text(payload: bytes, limit: int = 40000) -> str:
    raw = payload.decode("utf-8", errors="replace")
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t\xa0]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n", raw)
    return raw.strip()[:limit]


def fetch_primary_document(conn: sqlite3.Connection, cfg, http, accession: str) -> dict:
    f = conn.execute("SELECT * FROM sec_filing WHERE accession=?", (accession,)).fetchone()
    if f is None:
        return {"state": "NO_FILING"}
    if f["primary_doc_name"] is None:
        return {"state": "NO_PRIMARY_DOC_NAME"}
    existing = conn.execute(
        "SELECT 1 FROM sec_filing_document WHERE accession=? AND filename=?",
        (accession, f["primary_doc_name"]),
    ).fetchone()
    tp = text_path(accession)
    if existing and tp.exists():
        return {"state": "CACHED", "text_path": str(tp)}
    url = doc_url(f["cik"], accession, f["primary_doc_name"])
    resp = http.get(url)
    quality = Quality(QualityState.OK if resp.status_code == 200 else QualityState.ERROR,
                      f"http={resp.status_code}")
    m = record_fetch(conn, provider="sec", dataset="filing_document",
                     params={"accession": accession}, source_url=url,
                     payload=resp.content, http_status=resp.status_code,
                     quality=quality, config_version=cfg.id)
    if resp.status_code != 200:
        return {"state": f"ERROR http={resp.status_code}"}
    conn.execute(
        "INSERT OR REPLACE INTO sec_filing_document(accession, filename, doc_type, url,"
        " sha256, manifest_id) VALUES(?,?,?,?,?,?)",
        (accession, f["primary_doc_name"], "primary", url, m.raw_sha256, m.manifest_id),
    )
    conn.execute(
        "UPDATE sec_filing SET primary_doc_url=? WHERE accession=? AND primary_doc_url IS NULL",
        (url, accession),
    )
    conn.commit()
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(extract_text(resp.content))
    return {"state": "FETCHED", "text_path": str(tp), "bytes": len(resp.content)}


# ---- deterministic content assessment (trial tier; keyword rules, versioned) ----

CONTENT_RULES = [
    ("non_reliance_restatement",
     r"non-reliance|should no longer be relied|"
     r"(?:restate(?:d|ment|ments|ing)?\s+(?:our\s+|the\s+)?"
     r"(?:previously issued\s+)?financial statements?|"
     r"financial statements?.{0,80}(?:restate|restatement))"),
    ("bankruptcy_distress", r"chapter 11|chapter 7|bankruptcy|going concern"),
    ("delisting_compliance",
     r"delist|listing (qualification|standard|compliance)|continued listing"),
    ("late_filing", r"unable to file|notification of late|extension of time"),
    ("management_change", r"resign(ation|ed|s)?|termination of (the )?(chief|principal)"
                          r"|appoint(ment|ed).{0,40}(chief|principal)|departure of director"),
    ("auditor_change", r"dismiss(al|ed).{0,30}(auditor|accountant)|new independent registered"),
    ("financing_dilution", r"offering|private placement|convertible note|at-the-market|warrant"
                           r"|registered direct"),
    ("acquisition_disposition", r"merger agreement|acquisition of|disposition|divestiture"
                                r"|purchase agreement"),
    ("earnings_guidance", r"financial results|results of operations|guidance|outlook"
                          r"|preliminary (unaudited )?results"),
]

CONTENT_VERSION = "us_trial_content_v1"


def assess_content(text: str, items_csv: str | None) -> dict:
    lowered = text.lower()
    flags = [name for name, pattern in CONTENT_RULES if re.search(pattern, lowered)]
    items = [i.strip() for i in (items_csv or "").split(",") if i.strip()]
    # item codes take precedence for the primary category
    if "4.02" in items:
        primary = "non_reliance_restatement"
    elif "1.03" in items:
        primary = "bankruptcy_distress"
    elif "3.01" in items:
        primary = "delisting_compliance"
    elif "5.02" in items or "management_change" in flags:
        primary = "management_change"
    elif "2.02" in items or "earnings_guidance" in flags:
        primary = "earnings_guidance"
    elif "1.01" in items and "acquisition_disposition" in flags:
        primary = "acquisition_disposition"
    elif flags:
        primary = flags[0]
    else:
        primary = "neutral_or_unclassified"
    return {"primary": primary, "flags": flags, "content_version": CONTENT_VERSION}
