"""Persistent, resumable research queue for the US trial (PR-A).

Every TRIGGERED event lands here with its rank; deep-read budget exhaustion
defers items instead of dropping them. Queue rows survive across runs and can
be processed later with `invest research-queue --process N` — deferred
processing reuses the reaction profile frozen at the original asof (prices
are never recomputed with later data, so the gate basis stays time-consistent
with the run that triggered it).

States: RESEARCH_PENDING | RESEARCH_IN_PROGRESS | DOC_REVIEW_COMPLETED |
FETCH_FAILED | DATA_UNAVAILABLE | REJECTED | SUPERSEDED. Terminal states
(COMPLETED/REJECTED/SUPERSEDED) are never overwritten by a re-run's enqueue.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import utc_now

PROTECTED_STATES = ("DOC_REVIEW_COMPLETED", "REJECTED", "SUPERSEDED")


def queue_id(event_id: str, listing_id: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"us_queue:{event_id}:{listing_id}").hex


def enqueue(conn: sqlite3.Connection, *, event_id: str, candidate_id: str | None,
            company_id: str, listing_id: str, ticker: str | None, asof: str,
            state: str, rank: dict | None, config_version: str,
            last_error: str | None = None) -> str:
    """Idempotent upsert. A protected (terminal) existing state is preserved;
    rank and timestamps refresh so re-ranking stays visible."""
    qid = queue_id(event_id, listing_id)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO research_queue(queue_id, event_id, candidate_id, company_id,
          listing_id, ticker, asof, state, rank_score, rank_version,
          rank_inputs_json, attempts, last_error, enqueued_at_utc,
          updated_at_utc, config_version)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)
        ON CONFLICT(queue_id) DO UPDATE SET
          candidate_id=COALESCE(excluded.candidate_id, research_queue.candidate_id),
          state=CASE WHEN research_queue.state IN ({protected})
                     THEN research_queue.state ELSE excluded.state END,
          rank_score=excluded.rank_score,
          rank_version=excluded.rank_version,
          rank_inputs_json=excluded.rank_inputs_json,
          last_error=COALESCE(excluded.last_error, research_queue.last_error),
          updated_at_utc=excluded.updated_at_utc,
          config_version=excluded.config_version
        """.format(protected=",".join("?" * len(PROTECTED_STATES))),
        (qid, event_id, candidate_id, company_id, listing_id, ticker, asof, state,
         (rank or {}).get("score"), (rank or {}).get("version"),
         json.dumps(rank, ensure_ascii=False) if rank else None,
         last_error, now, now, config_version, *PROTECTED_STATES),
    )
    return qid


def _set_state(conn, qid: str, state: str, *, bump_attempts: bool = False,
               last_error: str | None = None) -> None:
    conn.execute(
        "UPDATE research_queue SET state=?, updated_at_utc=?,"
        " attempts=attempts + CASE WHEN ? THEN 1 ELSE 0 END,"
        " last_error=COALESCE(?, last_error) WHERE queue_id=?",
        (state, utc_now(), 1 if bump_attempts else 0, last_error, qid),
    )


def pending(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT rq.queue_id, rq.state, rq.rank_score, rq.rank_version, rq.ticker,
               rq.asof, rq.attempts, rq.last_error, rq.event_id, rq.listing_id,
               rq.candidate_id, rq.company_id
        FROM research_queue rq
        WHERE rq.state IN ('RESEARCH_PENDING','FETCH_FAILED','RESEARCH_IN_PROGRESS')
        ORDER BY rq.rank_score DESC, rq.ticker, rq.event_id LIMIT ?
        """, (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def process_queue(conn: sqlite3.Connection, trial_cfg, limit: int,
                  http_factory=None) -> dict:
    """Resume deferred deep reads in rank order. For each item: fetch and
    assess the primary document, re-run the state assessment on the FROZEN
    reaction profile from the original run, upgrade the candidate row, and
    close the queue row. Failures stay visible with attempts/last_error."""
    from investment_tool import us_trial

    def _default_http_factory():
        from investment_tool.providers import sec as sec_mod
        return sec_mod.client()

    http_factory = http_factory or _default_http_factory
    http = None
    audit: dict = {"generated_at": utc_now(), "requested": limit, "processed": [],
                   "config_version": trial_cfg.id}
    rows = conn.execute(
        """
        SELECT rq.queue_id, rq.candidate_id, rq.company_id, rq.listing_id,
               rq.ticker, e.event_id, e.type, e.first_seen_at_utc,
               f.accession, f.accepted_at_utc, f.filing_date, f.items_csv,
               f.primary_doc_name, f.cik
        FROM research_queue rq
        JOIN event e ON e.event_id = rq.event_id
        LEFT JOIN sec_filing f ON f.event_id = e.event_id
        WHERE rq.state IN ('RESEARCH_PENDING','FETCH_FAILED')
        ORDER BY rq.rank_score DESC, rq.ticker, rq.event_id LIMIT ?
        """, (limit,),
    ).fetchall()
    for r in rows:
        ev = dict(r)
        _set_state(conn, ev["queue_id"], "RESEARCH_IN_PROGRESS")
        cand = conn.execute(
            "SELECT profile_json FROM candidate WHERE candidate_id=?",
            (ev["candidate_id"],),
        ).fetchone()
        if cand is None or ev["accession"] is None:
            _set_state(conn, ev["queue_id"], "DATA_UNAVAILABLE", bump_attempts=True,
                       last_error="no candidate profile or accession")
            audit["processed"].append({"queue_id": ev["queue_id"],
                                       "ticker": ev["ticker"],
                                       "outcome": "DATA_UNAVAILABLE"})
            continue
        profile = json.loads(cand["profile_json"])
        rx, hits = profile.get("reaction", {}), profile.get("trigger_legs", [])
        if http is None:
            http = http_factory()
        content, content_state, err = us_trial.review_filing_content(
            conn, trial_cfg, http, ev)
        state, new_profile = us_trial.assess_and_state(
            ev, rx, "TRIGGERED", hits, content, trial_cfg,
            content_state=content_state)
        us_trial._upsert_candidate(conn, ev["company_id"], state, new_profile,
                                   trial_cfg.id)
        if content_state == us_trial.CONTENT_REVIEWED:
            _set_state(conn, ev["queue_id"], "DOC_REVIEW_COMPLETED", bump_attempts=True)
        else:
            _set_state(conn, ev["queue_id"], "FETCH_FAILED", bump_attempts=True,
                       last_error=err or "document fetch failed")
        audit["processed"].append({"queue_id": ev["queue_id"], "ticker": ev["ticker"],
                                   "outcome": state,
                                   "category": (content or {}).get("primary")})
    conn.commit()
    out_dir = DEFAULT_DATA_DIR / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("-", "")
    (out_dir / f"us_queue_process_{stamp}.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, default=str))
    return audit
