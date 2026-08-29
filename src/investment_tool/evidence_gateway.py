"""Open-world evidence gateway (H1): the ONE road from the web into a
research case.

This is a provenance and capture boundary, NOT a search censorship
mechanism: any publicly accessible URL may be captured — unknown domains are
classified, never rejected. What the gateway enforces is that nothing
becomes citable evidence without a manifest, a content hash, stored text,
and the three timestamps (published / first_seen / retrieved). An agent may
explore the web freely; it may not present an uncaptured source as verified
evidence.

Source classes (descending default authority — a class is a prior, not a
verdict; corroboration rules live in the claim validator):
  PRIMARY_REGULATORY   SEC/exchange/regulator/court/government
  ISSUER               the company's own IR pages, releases, decks
  INDEPENDENT_REPORTING reputable financial journalism
  SPECIALIST           trade/industry publications and datasets
  SECONDARY_ANALYSIS   aggregators, commentary, sell-side summaries
  DISCOVERY_LEAD       weak/unverified leads (may guide search, weakest cite)
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from investment_tool import us_filing_docs
from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import record_fetch, utc_now
from investment_tool.quality import Quality, QualityState

SOURCE_CLASSES = ("PRIMARY_REGULATORY", "ISSUER", "INDEPENDENT_REPORTING",
                  "SPECIALIST", "SECONDARY_ANALYSIS", "DISCOVERY_LEAD")

# default classification PRIORS by domain pattern — everything else is
# DISCOVERY_LEAD until reclassified; nothing is refused for being unknown
_CLASS_PRIORS = (
    (r"(\.|^)(sec|justice|ftc|fda|cftc|treasury|federalreserve|courtlistener)"
     r"\.(gov|org)$|\.gov$", "PRIMARY_REGULATORY"),
    (r"(\.|^)(nasdaq|nyse|finra)\.(com|org)$", "PRIMARY_REGULATORY"),
    (r"(\.|^)(globenewswire|businesswire|prnewswire)\.com$", "ISSUER"),
    (r"(\.|^)(reuters|bloomberg|wsj|ft|barrons|cnbc|marketwatch|axios)\.com$",
     "INDEPENDENT_REPORTING"),
    (r"(\.|^)(retaildive|supermarketnews|foodbusinessnews|chainstoreage|"
     r"fiercehealthcare|healthcaredive)\.com$", "SPECIALIST"),
    (r"(\.|^)(seekingalpha|fool|zacks|benzinga|investing|stocktitan|"
     r"stockanalysis|tipranks)\.com$", "SECONDARY_ANALYSIS"),
)

_UA = ("Mozilla/5.0 (Macintosh) investment-tool research gateway"
       " (personal research; contact via repository)")


def classify_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for pattern, cls in _CLASS_PRIORS:
        if re.search(pattern, host):
            return cls
    return "DISCOVERY_LEAD"


def _client():
    from investment_tool.providers.base import HttpClient
    return HttpClient(user_agent=_UA, min_interval_s=1.0)


def evidence_dir(case_id: str):
    d = DEFAULT_DATA_DIR / "research" / "cases" / case_id / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    return d


def capture(conn: sqlite3.Connection, cfg, case_id: str, url: str, *,
            published_at_utc: str | None = None, title: str | None = None,
            source_class: str | None = None, note: str | None = None,
            http=None) -> dict:
    """Fetch one URL, manifest it, store extracted text, insert an evidence
    row linked to the case. Returns {evidence_id, decision_eligible, ...}.
    Failures are recorded (manifested) and reported, never silent."""
    case = conn.execute("SELECT * FROM research_case WHERE case_id=?",
                        (case_id,)).fetchone()
    if case is None:
        return {"error": f"no research case {case_id}"}
    if source_class is not None and source_class not in SOURCE_CLASSES:
        return {"error": f"unknown source_class {source_class};"
                         f" one of {SOURCE_CLASSES}"}
    http = http or _client()
    try:
        resp = http.get(url)
        status, payload = resp.status_code, resp.content
    except Exception as exc:
        status, payload = 0, repr(exc).encode()
    quality = Quality(QualityState.OK if status == 200 else QualityState.ERROR,
                      f"http={status}")
    m = record_fetch(conn, provider="web", dataset="evidence_page",
                     params={"case_id": case_id, "url": url}, source_url=url,
                     payload=payload, http_status=status or None,
                     quality=quality, config_version=cfg.id)
    if status != 200:
        return {"error": f"fetch failed http={status}", "url": url,
                "manifest": m.manifest_id,
                "hint": "record the channel as BLOCKED in coverage if this"
                        " source matters and no mirror exists"}
    text = us_filing_docs.extract_text(payload, limit=250000)
    sha = hashlib.sha256(payload).hexdigest()
    evidence_id = f"evd_{sha[:16]}"
    cls = source_class or classify_domain(url)
    now = utc_now()
    existing = conn.execute("SELECT evidence_id, content_path FROM evidence"
                            " WHERE evidence_id=?", (evidence_id,)).fetchone()
    if existing:
        # globally content-addressed and IMMUTABLE: the same content captured
        # again (any case) only gains a case association — the original row,
        # its first_seen_at_utc, and its stored text are never rewritten
        path = existing["content_path"]
        if path and not Path(path).exists():
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text)
    else:
        path = evidence_dir(case_id) / f"{evidence_id}.txt"
        path.write_text(text)
        conn.execute(
            "INSERT INTO evidence(evidence_id, event_id, case_id,"
            " source_url, publisher_domain, published_at_utc, retrieved_at_utc,"
            " first_seen_at_utc, sha256, retention_class, excerpt, dims_json,"
            " title, source_class, content_path, access_note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (evidence_id, None, case_id, url, urlparse(url).hostname,
             published_at_utc, now, now, sha,
             "OFFICIAL_FULL" if cls in ("PRIMARY_REGULATORY", "ISSUER")
             else "MEDIA_EXCERPT",
             text[:500], '{"gateway": "h1"}', title, cls, str(path), note),
        )
    conn.execute("INSERT OR IGNORE INTO case_evidence(case_id, evidence_id,"
                 " added_at_utc) VALUES(?,?,?)", (case_id, evidence_id, now))
    conn.commit()
    eligible = None
    if published_at_utc:
        eligible = published_at_utc <= case["decision_cutoff_utc"]
    return {"evidence_id": evidence_id, "source_class": cls,
            "published_at_utc": published_at_utc,
            "decision_eligible": eligible,
            "decision_cutoff_utc": case["decision_cutoff_utc"],
            "chars": len(text), "content_path": str(path),
            "manifest": m.manifest_id,
            "note": "undated capture: decision eligibility unknown — set"
                    " --published-at or the claim validator treats it as"
                    " HINDSIGHT-only" if not published_at_utc else None}
