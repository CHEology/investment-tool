import json

from investment_tool import cards


def _mk_candidate(conn):
    conn.execute(
        "INSERT INTO company(company_id, name_zh, created_asof)"
        " VALUES('CN:300274','阳光电源','2026-08-28T00:00:00Z')")
    conn.execute(
        "INSERT INTO listing(listing_id, company_id, ticker, exchange, board, currency)"
        " VALUES('SZSE:300274','CN:300274','300274','SZSE','CHINEXT','CNY')")
    profile = {
        "t0": "2026-08-27", "car_peer": -0.062, "car_mm": -0.121, "window_state": "PARTIAL",
        "sigma": 0.018,
        "liquidity": {"class": "NORMAL", "adv60_usd": 8.4e8},
        "announcements": [{"title": "回购公告", "type": "BUYBACK",
                           "published": "2026-08-27T12:00:00Z", "negative": False}],
        "damage": {"classification": "WITHIN_BRACKET", "excess_ratio": None,
                   "dmcap_abnormal": "1.4e10", "mcap_t0_minus_1": "2.3e11",
                   "damage_low": "2.9e9", "damage_high": "3.8e10",
                   "template": "market_access", "admitted": False},
        "verification_debt": ["eastmoney PROVISIONAL spine"],
    }
    conn.execute(
        "INSERT INTO candidate(candidate_id, company_id, lane, state, profile_json, gates_json,"
        " detected_at_utc, config_version) VALUES(?,?,?,?,?,?,?,?)",
        ("cand1", "CN:300274", "A", "NOT_ADMITTED_WITHIN_BRACKET",
         json.dumps(profile, ensure_ascii=False), "{}", "2026-08-28T16:00:00Z", "v0"),
    )
    conn.commit()
    return conn.execute("SELECT * FROM candidate WHERE candidate_id='cand1'").fetchone()


def _mk_us_lead(conn, state="US_TRIAL_LEAD"):
    conn.execute(
        "INSERT INTO company(company_id, name_en, cik, created_asof)"
        " VALUES('US:TT','Test Corp','7000001','2026-01-01T00:00:00Z')")
    conn.execute(
        "INSERT INTO listing(listing_id, company_id, ticker, exchange, currency)"
        " VALUES('NASDAQ:TT','US:TT','TT','NASDAQ','USD')")
    profile = {
        "event_id": "ev_us_x", "event_type": "ISSUER_8K", "ticker": "TT",
        "accession": "acc-x", "accepted_at_utc": "2026-08-27T12:02:52Z",
        "first_seen_at_utc": "2026-08-28T21:18:28Z",
        "reaction": {"state": "OK", "post_ret1": -0.10, "mkt_adj_post_ret1": -0.11,
                     "post_cum": -0.08, "mkt_adj_post_cum": -0.08, "ret21": -0.30,
                     "volume_ratio": 5.2, "t0_session": "2026-08-27"},
        "gate": "TRIGGERED", "trigger_legs": ["post1", "volume"],
        "content": {"primary": "earnings_guidance", "flags": ["earnings_guidance"],
                    "content_version": "us_trial_content_v0"},
        # legacy directional text as stored by the 2026-08-28 run — the
        # renderer must NOT resurface it
        "excess_rationale": "业绩/指引类下调需区分一次性因素与结构性恶化；反应可能超出经常性影响",
        "unresolved_questions": ["事件的现金流影响是否有界"],
        "config_version": "us_trial_v0",
    }
    conn.execute(
        "INSERT INTO candidate(candidate_id, company_id, lane, state, profile_json, gates_json,"
        " detected_at_utc, config_version) VALUES(?,?,?,?,?,?,?,?)",
        ("uscand1", "US:TT", "A", state,
         json.dumps(profile, ensure_ascii=False), "{}", "2026-08-28T23:00:00Z",
         "us_trial_v0"),
    )
    conn.commit()
    return conn.execute("SELECT * FROM candidate WHERE candidate_id='uscand1'").fetchone()


def test_us_card_has_no_unsupported_conclusions(conn):
    """Review F3: cards must not auto-assert bounded/temporary damage, must not
    resurface legacy directional rationale, and must label trailing windows as
    non-event-anchored diagnostics. Applies to legacy-state rows too."""
    row = _mk_us_lead(conn, state="US_TRIAL_CANDIDATE")
    content = cards.render_us_card_zh(conn, row)
    assert "倾向暂时性" not in content
    assert "反应可能超出经常性影响" not in content  # legacy stored text ignored
    assert "试验线索" in content            # legacy state renders as lead
    assert "非事件锚定" in content          # trailing windows labeled honestly
    assert "不构成任何投资建议" in content


def test_us_card_correction_note_renders(conn):
    row = _mk_us_lead(conn)
    content = cards.render_us_card_zh(conn, row, correction_note=["定价判定：UNRESOLVED"])
    assert "v2 更正说明" in content and "UNRESOLVED" in content


def test_card_renders_zh_and_freezes(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "CARDS_DIR", tmp_path / "cards")
    row = _mk_candidate(conn)
    content = cards.render_card_zh(conn, row)
    assert "阳光电源" in content and "WITHIN_BRACKET" in content
    assert "不构成任何投资建议" in content
    frozen = cards.freeze_card(conn, row, content)
    stored = conn.execute(
        "SELECT * FROM frozen_artifact WHERE artifact_id=?", (frozen["artifact_id"],)
    ).fetchone()
    assert stored["content_sha256"] == frozen["sha256"]
    # re-freeze -> new version, immutable prior artifact
    frozen2 = cards.freeze_card(conn, row, content + "\nupdate")
    assert frozen2["version"] == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM frozen_artifact").fetchone()["n"] == 2
    states = conn.execute(
        "SELECT version, status FROM frozen_artifact ORDER BY version"
    ).fetchall()
    assert [(r["version"], r["status"]) for r in states] == [
        (1, "SUPERSEDED"),
        (2, "VALID"),
    ]
