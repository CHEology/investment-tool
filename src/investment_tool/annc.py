"""CNInfo announcement ingestion and rule-based classification (EVIDENCE tier).

S1 uses candidate-driven per-stock queries (cause-hunt) plus optional day
listings. Announcement time is the source publication time (published_at_utc);
first_seen_at_utc is our detection time — their gap is the detection-latency
metric (approved condition 1).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime

from investment_tool.lineage import record_fetch, utc_now
from investment_tool.providers.base import BROWSER_UA, HttpClient
from investment_tool.providers.cninfo import QUERY_URL
from investment_tool.quality import Quality, QualityState

COLUMN_BY_EXCHANGE = {"SZSE": ("szse", "sz"), "SSE": ("sse", "sh"), "BSE": ("bj", "bj")}

# Rule-based classifier: (regex on title, event_type, lane, relevance).
# Relevance semantics (review issue 4):
#   HARD_NEGATIVE           adverse on its face; satisfies attribution if
#                           temporally eligible.
#   CONTENT_REVIEW_REQUIRED content decides sign/materiality (e.g. a periodic
#                           report); it can satisfy attribution ONLY after an
#                           operator content-review confirmation (C0: recorded
#                           in the damage-params file) AND temporal eligibility.
#   POSITIVE / NEUTRAL      never satisfy Lane A attribution.
HARD_NEGATIVE = "HARD_NEGATIVE"
CONTENT_REVIEW = "CONTENT_REVIEW_REQUIRED"
POSITIVE = "POSITIVE"
NEUTRAL = "NEUTRAL"

RULES: list[tuple[str, str, str, str]] = [
    (r"立案|调查通知", "REGULATORY_INVESTIGATION", "A", HARD_NEGATIVE),
    (r"行政处罚|监管函|警示函|纪律处分", "REGULATORY_PENALTY", "A", HARD_NEGATIVE),
    (r"退市风险|其他风险警示", "DELISTING_RISK", "A", HARD_NEGATIVE),
    (r"业绩预告|业绩快报", "EARNINGS_PREANNOUNCE", "A", CONTENT_REVIEW),
    (r"年度报告|半年度报告|季度报告", "PERIODIC_REPORT", "A", CONTENT_REVIEW),
    (r"诉讼|仲裁", "LITIGATION", "A", CONTENT_REVIEW),
    (r"澄清", "CLARIFICATION", "A", CONTENT_REVIEW),
    (r"异常波动|异动", "PRICE_ANOMALY_NOTICE", "A", CONTENT_REVIEW),
    (r"停牌", "SUSPENSION", "A", CONTENT_REVIEW),
    (r"减持", "HOLDER_SELLDOWN", "A", HARD_NEGATIVE),
    (r"回购", "BUYBACK", "A", POSITIVE),
    (r"增持", "HOLDER_INCREASE", "A", POSITIVE),
    (r"中标|预中标", "CONTRACT_AWARD_NOTICE", "B", POSITIVE),
    (r"合同|框架协议", "MATERIAL_CONTRACT", "B", POSITIVE),
    (r"取得.*(专利|证书|批准|注册|许可)|获得.*(认证|批件)",
     "CERTIFICATION_APPROVAL", "B", POSITIVE),
]


def classify(title: str) -> tuple[str, str, str]:
    for pattern, etype, lane, relevance in RULES:
        if re.search(pattern, title):
            return etype, lane, relevance
    return "OTHER", "-", NEUTRAL


def eligible_from(published_at_utc: str | None) -> str | None:
    """First calendar date whose SESSION a date-precision announcement can
    causally explain. CNInfo stamps are date-granular (midnight Beijing =
    16:00Z previous UTC day): an announcement stamped Beijing date D is first
    publicly available on D, so it can explain sessions with trade_date >= D."""
    if not published_at_utc:
        return None
    from datetime import datetime, timedelta

    dt = datetime.strptime(published_at_utc, "%Y-%m-%dT%H:%M:%SZ")
    beijing = dt + timedelta(hours=8)
    return beijing.strftime("%Y-%m-%d")


def _client() -> HttpClient:
    http = HttpClient(user_agent=BROWSER_UA, min_interval_s=1.5)
    # Cookie preflight (endpoint expects a session).
    try:
        http.get("https://www.cninfo.com.cn/new/disclosure")
    except Exception:  # noqa: BLE001 - preflight is best-effort
        pass
    return http


def fetch_stock_announcements(http: HttpClient, exchange: str, code: str, org_id: str | None,
                              se_start: str, se_end: str, page: int = 1):
    column, plate = COLUMN_BY_EXCHANGE[exchange]
    stock = f"{code},{org_id}" if org_id else code
    data = {
        "pageNum": page, "pageSize": 30, "column": column, "tabName": "fulltext",
        "plate": plate, "stock": stock, "searchkey": "", "secid": "", "category": "",
        "trade": "", "seDate": f"{se_start}~{se_end}", "sortName": "time",
        "sortType": "desc", "isHLtitle": "true",
    }
    resp = http.post(
        QUERY_URL, data=data,
        headers={"X-Requested-With": "XMLHttpRequest",
                 "Referer": "https://www.cninfo.com.cn/new/disclosure"},
    )
    return resp.content, resp.status_code, QUERY_URL, data


def parse_announcements(payload: bytes) -> list[dict]:
    data = json.loads(payload)
    out = []
    for a in data.get("announcements") or []:
        ts = a.get("announcementTime")
        published = (
            datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if isinstance(ts, (int, float)) else None
        )
        title = re.sub(r"<[^>]+>", "", a.get("announcementTitle") or "")
        out.append(
            {
                "ann_id": str(a.get("announcementId")),
                "sec_code": a.get("secCode"),
                "org_id": a.get("orgId"),
                "title": title,
                "adjunct_url": ("https://static.cninfo.com.cn/" + a["adjunctUrl"])
                if a.get("adjunctUrl") else None,
                "published_at_utc": published,
            }
        )
    return out


def ingest_for_listing(conn: sqlite3.Connection, config_version: str, listing_row,
                       se_start: str, se_end: str,
                       http: HttpClient | None = None) -> list[dict] | None:
    """Fetch + store announcements for one listing over a date window.
    Returns the stored rows (with classification)."""
    http = http or _client()
    payload, status, url, params = fetch_stock_announcements(
        http, listing_row["exchange"], listing_row["ticker"], listing_row["cninfo_org_id"],
        se_start, se_end,
    )
    quality = Quality(QualityState.OK if status == 200 else QualityState.ERROR, f"http={status}")
    m = record_fetch(
        conn, provider="cninfo", dataset="announcements",
        params={k: v for k, v in params.items() if k in ("stock", "seDate", "column")},
        source_url=url, payload=payload, http_status=status, quality=quality,
        config_version=config_version,
    )
    if status != 200:
        return None  # fetch degraded: caller must not treat as "no announcements"
    rows = parse_announcements(payload)
    now = utc_now()
    stored = []
    for r in rows:
        etype, lane, relevance = classify(r["title"])
        pub = r["published_at_utc"]
        precision = "DATE" if (pub or "").endswith("T16:00:00Z") else "TIME"
        anomaly = "FIRST_SEEN_BEFORE_PUBLISHED" if (pub and now < pub) else None
        conn.execute(
            "INSERT OR IGNORE INTO announcement(ann_id, exchange_column, sec_code, org_id, title,"
            " adjunct_url, published_at_utc, ts_precision, ts_anomaly, first_seen_at_utc,"
            " category, relevance, manifest_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["ann_id"], listing_row["exchange"], r["sec_code"], r["org_id"], r["title"],
                r["adjunct_url"], pub, precision, anomaly, now, etype, relevance, m.manifest_id,
            ),
        )
        stored.append({**r, "event_type": etype, "lane": lane, "relevance": relevance,
                       "ts_precision": precision, "ts_anomaly": anomaly,
                       "eligible_from": eligible_from(pub)})
    conn.commit()
    return stored


def create_event_from_announcement(conn: sqlite3.Connection, company_id: str, ann: dict) -> str:
    """Issuer-primary announcement on the designated platform => VERIFIED event
    (authority=issuer filing, directness=primary). Independence stays 0: this
    verifies that the company said X, never X itself (DESIGN 7)."""
    event_id = "ev_" + hashlib.sha256(
        f"{company_id}|{ann['event_type']}|{ann['ann_id']}".encode()
    ).hexdigest()[:16]
    now = utc_now()
    conn.execute(
        "INSERT OR IGNORE INTO event(event_id, scope, type, published_at_utc, first_seen_at_utc,"
        " state, lane_relevance) VALUES(?,?,?,?,?,?,?)",
        (event_id, "COMPANY", ann["event_type"], ann["published_at_utc"], now, "VERIFIED",
         ann["lane"]),
    )
    conn.execute(
        "INSERT OR IGNORE INTO event_company(event_id, company_id) VALUES(?,?)",
        (event_id, company_id),
    )
    dims = {"authority": 2, "independence": 0, "directness": 3, "specificity": 2,
            "bindingness": 0, "reproducibility": 1, "freshness": 2}
    conn.execute(
        "INSERT OR IGNORE INTO evidence(evidence_id, event_id, source_url, publisher_domain,"
        " published_at_utc, retrieved_at_utc, first_seen_at_utc, sha256, retention_class,"
        " excerpt, dims_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "evd_" + ann["ann_id"], event_id, ann["adjunct_url"] or "cninfo:" + ann["ann_id"],
            "cninfo.com.cn", ann["published_at_utc"], now, now,
            hashlib.sha256(
                json.dumps(ann, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            "LINK_ONLY", ann["title"], json.dumps(dims),
        ),
    )
    conn.execute("UPDATE announcement SET event_id=? WHERE ann_id=?", (event_id, ann["ann_id"]))
    conn.commit()
    return event_id
