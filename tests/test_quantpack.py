"""H2 quantpack + XBRL fundamentals: point-in-time discipline, TTM assembly,
multi-class shares, guidance extraction, dilution template. Offline."""

import json

import pytest

from investment_tool import config as config_mod
from investment_tool import quantpack, research, us_fundamentals
from test_research_foundation import _mk_candidate


@pytest.fixture
def cfg():
    return config_mod.load("us_trial_v0.3")


def _facts_payload():
    """Synthetic companyfacts: 5 quarters of revenue, shares, one revision."""
    def q(start, end, val, filed, form="10-Q"):
        return {"start": start, "end": end, "val": val, "filed": filed,
                "form": form, "fy": 2026, "fp": "Q"}
    return json.dumps({
        "cik": 7000001,
        "facts": {
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2026-03-31", "val": 90_000_000, "filed": "2026-04-30"},
                {"end": "2026-06-30", "val": 100_000_000, "filed": "2026-07-30"},
                # a later refiling revises the same period end (PIT test)
                {"end": "2026-06-30", "val": 101_000_000, "filed": "2026-09-15"},
            ]}}},
            "us-gaap": {"Revenues": {"units": {"USD": [
                q("2025-07-01", "2025-09-30", 100e6, "2025-10-30"),
                q("2025-10-01", "2025-12-31", 110e6, "2026-01-30"),
                q("2026-01-01", "2026-03-31", 120e6, "2026-04-30"),
                q("2026-04-01", "2026-06-30", 130e6, "2026-07-30"),
                q("2025-01-01", "2025-12-31", 400e6, "2026-02-20", form="10-K"),
            ]}},
                "NetIncomeLoss": {"units": {"USD": [
                    q("2025-07-01", "2025-09-30", 10e6, "2025-10-30"),
                    q("2025-10-01", "2025-12-31", 11e6, "2026-01-30"),
                    q("2026-01-01", "2026-03-31", 12e6, "2026-04-30"),
                    q("2026-04-01", "2026-06-30", 13e6, "2026-07-30"),
                ]}}},
        }}).encode()


def _load_facts(conn):
    return us_fundamentals.store_companyfacts(conn, _facts_payload(), "m_test")


def test_point_in_time_shares_and_revision_isolation(conn):
    _load_facts(conn)
    # asof 2026-08-28: the 2026-09-15 revision must NOT leak backwards
    sh = us_fundamentals.shares_outstanding(conn, "7000001", "2026-08-28")
    assert sh["value"] == 100_000_000 and sh["quality"] == "OK"
    # after the revision is filed it becomes visible
    sh2 = us_fundamentals.shares_outstanding(conn, "7000001", "2026-09-16")
    assert sh2["value"] == 101_000_000


def test_ttm_revenue_prefers_four_quarters(conn):
    _load_facts(conn)
    rev = us_fundamentals.ttm_revenue(conn, "7000001", "2026-08-28")
    assert rev["quality"] == "OK"
    assert rev["value"] == pytest.approx(460e6)
    # early asof with only 2 quarters filed -> PARTIAL or annual fallback
    rev_early = us_fundamentals.ttm_revenue(conn, "7000001", "2026-02-25")
    assert rev_early["quality"] in ("PARTIAL_ANNUAL", "PARTIAL_2Q")


def test_multi_class_shares_flagged(conn):
    payload = json.dumps({
        "cik": 7000001,
        "facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {
            "shares": [
                {"end": "2026-06-30", "val": 60_000_000, "filed": "2026-07-30"},
                {"end": "2026-06-30", "val": 40_000_000, "filed": "2026-07-30"},
            ]}}}}}).encode()
    us_fundamentals.store_companyfacts(conn, payload, "m")
    # NOTE: identical (end, filed, value-key) rows collide in the PK, so this
    # exercises the flag only when values differ — which is the real case
    sh = us_fundamentals.shares_outstanding(conn, "7000001", "2026-08-28")
    assert sh["value"] == 100_000_000
    assert sh["quality"] == "APPROX_MULTI_CLASS"


def test_guidance_extractor_finds_ranges_with_context():
    text = ("For the fiscal year ending January 31, 2027, management is "
            "raising guidance and now expects revenues of $1.411 billion to "
            "$1.421 billion. Its outlook for non-GAAP EPS is $4.66 to $4.73. "
            "The building is worth $5 to $9.")  # last one lacks context words
    found = quantpack.extract_guidance(text)
    assert len(found) == 2
    assert found[0]["low"] == pytest.approx(1.411e9)
    assert found[0]["high"] == pytest.approx(1.421e9)
    assert found[1]["low"] == pytest.approx(4.66)
    assert "span" in found[0] and found[0]["quality"] == "EXTRACTED_HEURISTIC"


def test_dilution_template_recompute():
    from investment_tool import damage
    params = {
        "shares_new": {"low": 10_000_000, "high": 20_000_000, "source": "S-3"},
        "shares_out": {"value": 100_000_000, "source": "10-Q cover"},
        "mcap_pre_event": {"value": 1_000_000_000, "source": "quantpack"},
        "proceeds": {"value": 150_000_000, "source": "8-K"},
        "discount": {"low": "0.05", "high": "0.15", "source": "pricing terms"},
    }
    b = damage.run_template("dilution", params)
    assert float(b.low) == pytest.approx(7_500_000)
    assert float(b.high) == pytest.approx(1_000_000_000 * (20 / 120))
    # a param without a source fails loudly
    del params["shares_out"]["source"]
    with pytest.raises(damage.DamageParamError):
        damage.run_template("dilution", params)


def test_build_quantpack_sections_and_freeze(conn, cfg, tmp_path, monkeypatch):
    _mk_candidate(conn)
    _load_facts(conn)
    # prices for mcap/ADV: 80 real sessions
    from test_us_trial import _mk_series
    _mk_series(conn, moves={79: -0.10})
    case = research.open_case(conn, cfg, "cand_x")
    out = quantpack.build_quantpack(conn, cfg, case["case_id"])
    assert out["quantpack_version"] == 1
    pack = json.loads((research.case_dir(case["case_id"])
                       / "quantpack_latest.json").read_text())
    assert pack["fundamentals"]["market_cap"]["value"] > 0
    assert pack["fundamentals"]["ttm_revenue"]["value"] == pytest.approx(460e6)
    assert pack["fundamentals"]["adv60_usd"]["quality"] in ("OK", "PARTIAL")
    assert pack["investability"]["mcap_ok"] is True
    assert pack["damage"]["status"] == "AGENT_PARAMS_REQUIRED"
    # versioning: second build -> v2, latest updated, artifacts recorded
    out2 = quantpack.build_quantpack(conn, cfg, case["case_id"])
    assert out2["quantpack_version"] == 2
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM frozen_artifact WHERE kind='QUANT_PACK'")]
    assert len(kinds) == 2


def test_h21_hrl_duration_pattern_never_summed(conn):
    """F-A regression (actual HRL shape): a 9-month YTD row and a quarterly
    row share period_end and filed_date — alternative duration views, NOT
    share classes. The shortest current-period duration wins; summing (the
    old behavior, ~1.102B) is the bug."""
    payload = json.dumps({
        "cik": 48465,
        "facts": {"us-gaap": {"WeightedAverageNumberOfDilutedSharesOutstanding":
                  {"units": {"shares": [
                      {"start": "2025-10-27", "end": "2026-07-26",
                       "val": 550_898_000, "filed": "2026-08-27"},
                      {"start": "2026-04-27", "end": "2026-07-26",
                       "val": 551_074_000, "filed": "2026-08-27"},
                  ]}}}}}).encode()
    us_fundamentals.store_companyfacts(conn, payload, "m")
    sh = us_fundamentals.shares_outstanding(conn, "48465", "2026-08-28")
    assert sh["value"] == 551_074_000
    assert sh["grain"] == "DURATION" and sh["duration_days"] == 90
    assert sh["quality"] == "APPROX_WEIGHTED_DILUTED"


def test_h21_instant_repeats_deduplicate_but_classes_sum(conn):
    payload = json.dumps({
        "cik": 7000002,
        "facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {
            "shares": [
                # identical repeats (same value, different contexts) dedupe
                {"end": "2026-06-30", "val": 60_000_000, "filed": "2026-07-30"},
                {"end": "2026-06-30", "val": 60_000_000, "filed": "2026-07-30"},
                # a distinct simultaneous value = a genuine second class
                {"end": "2026-06-30", "val": 40_000_000, "filed": "2026-07-30"},
            ]}}}}}).encode()
    us_fundamentals.store_companyfacts(conn, payload, "m")
    sh = us_fundamentals.shares_outstanding(conn, "7000002", "2026-08-28")
    assert sh["value"] == 100_000_000
    assert sh["quality"] == "APPROX_MULTI_CLASS" and sh["grain"] == "INSTANT"


def test_h21_entry_aware_mcap_gap(conn, cfg, tmp_path, monkeypatch):
    """F-B regression: pre-event mcap uses the ACTUAL pre-event close; the
    entry analysis reports the gap remaining at the first actionable session
    with explicit dates (here: entry pending -> provisional asof residual)."""
    _mk_candidate(conn)
    _load_facts(conn)
    from test_us_trial import _mk_series
    _mk_series(conn, moves={75: -0.10, 78: 0.05})   # partial rebound after
    case = research.open_case(conn, cfg, "cand_x")
    # rewire the profile reaction to anchor at session 75
    import json as j
    row = conn.execute("SELECT profile_json FROM candidate WHERE"
                       " candidate_id='cand_x'").fetchone()
    p = j.loads(row["profile_json"])
    from investment_tool import reaction as rmod
    sess = [r["trade_date"] for r in conn.execute(
        "SELECT trade_date FROM security_day WHERE listing_id='NASDAQ:TT'"
        " ORDER BY trade_date")]
    anchors = {"event_session": sess[75], "same_session_partial": False,
               "first_actionable_session": "2027-01-05",  # not traded yet
               "precision": "TIME"}
    p["reaction"] = rmod.compute_event_reaction(conn, "NASDAQ:TT", anchors,
                                                sess[-1])
    conn.execute("UPDATE candidate SET profile_json=? WHERE candidate_id='cand_x'",
                 (j.dumps(p, default=str),))
    conn.commit()
    quantpack.build_quantpack(conn, cfg, case["case_id"])
    pack = j.loads((research.case_dir(case["case_id"])
                    / "quantpack_latest.json").read_text())
    tl = pack["event_mcap_change"]
    ent = pack["entry_analysis"]
    assert tl["pre_event"]["date"] == sess[74]
    assert tl["event_close"]["date"] == sess[75]
    # event-day abnormal change anchored on the true pre-event mcap
    assert tl["event_session_abnormal_change"] == pytest.approx(
        tl["pre_event"]["mcap"] * pack["reaction"]["mkt_adj_post_ret1"])
    assert ent["status"] == "PENDING_SESSION"
    assert ent["provisional_residual_gap_asof"] == pytest.approx(
        tl["cumulative_abnormal_change_asof"])
    # the rebound reduced the residual vs the event-day move
    assert abs(ent["provisional_residual_gap_asof"]) < abs(
        tl["event_session_abnormal_change"])
