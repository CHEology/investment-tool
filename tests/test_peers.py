"""H3: judgment-built peer baskets and peer-relative residuals. Offline."""

import json

import pytest

from investment_tool import config as config_mod
from investment_tool import peers, quantpack, research
from test_research_foundation import _mk_candidate
from test_us_trial import _mk_series


@pytest.fixture
def cfg():
    return config_mod.load("us_trial_v0.4")


def _setup(conn, cfg):
    _mk_candidate(conn)
    sess = _mk_series(conn, moves={75: -0.10})               # case: -10%
    _mk_series(conn, lid="NASDAQ:P1", company="US:P1", ticker="P1",
               moves={75: -0.04})                            # peer -4%
    _mk_series(conn, lid="NASDAQ:P2", company="US:P2", ticker="P2",
               moves={75: -0.06})                            # peer -6%
    case = research.open_case(conn, cfg, "cand_x")
    # anchor the profile reaction at session 75
    from investment_tool import reaction as rmod
    row = conn.execute("SELECT profile_json FROM candidate WHERE"
                       " candidate_id='cand_x'").fetchone()
    p = json.loads(row["profile_json"])
    anchors = {"event_session": sess[75], "same_session_partial": False,
               "first_actionable_session": sess[77], "precision": "TIME"}
    p["reaction"] = rmod.compute_event_reaction(conn, "NASDAQ:TT", anchors,
                                                sess[-1])
    conn.execute("UPDATE candidate SET profile_json=? WHERE candidate_id='cand_x'",
                 (json.dumps(p, default=str),))
    conn.commit()
    return case, sess, p["reaction"]


def test_basket_records_composition_and_rationale(conn, cfg):
    case, _sess, _rx = _setup(conn, cfg)
    out = peers.set_basket(conn, cfg, case["case_id"], ["P1", "P2"],
                           etf=None, rationale="economically relevant"
                           " comparators chosen by judgment (test)",
                           set_by="pytest")
    assert out["peers"]["tickers"] == ["P1", "P2"]
    doc = json.loads(peers.peers_path(case["case_id"]).read_text())
    assert doc["rationale"].startswith("economically relevant")
    assert doc["set_by"] == "pytest" and doc["set_at_utc"]


def test_peer_relative_residuals(conn, cfg):
    case, sess, rx = _setup(conn, cfg)
    peers.set_basket(conn, cfg, case["case_id"], ["P1", "P2"],
                     rationale="r", set_by="pytest")
    pa = peers.peer_analysis(conn, case["case_id"], rx, sess[-1])
    assert pa["quality"] == "OK"
    assert pa["windows"]["event_session"] == sess[75]
    # peer median event return ~ -5% (midpoint of -4%/-6%: median picks -4%)
    assert pa["peer_median"]["event"] == pytest.approx(-0.04, abs=1e-3)
    # case -10% vs peers: residual ~ -6% (company-specific part)
    assert pa["case_vs_peers"]["event_residual"] == pytest.approx(
        rx["post_ret1"] - pa["peer_median"]["event"], abs=1e-9)
    assert pa["case_vs_peers"]["event_residual"] < -0.05


def test_quantpack_carries_peer_and_expectation_sections(conn, cfg):
    case, sess, rx = _setup(conn, cfg)
    peers.set_basket(conn, cfg, case["case_id"], ["P1", "P2"],
                     rationale="r", set_by="pytest")
    quantpack.build_quantpack(conn, cfg, case["case_id"])
    pack = json.loads((research.case_dir(case["case_id"])
                       / "quantpack_latest.json").read_text())
    assert pack["peer_analysis"]["quality"] == "OK"
    es = pack["expectation_state"]
    assert "mkt_adj_run_up_63" in es and "mkt_adj_run_up_252" in es
    assert es["event_session_mkt_adj"] == pytest.approx(
        rx["mkt_adj_post_ret1"])
    # missing horizons are listed, never silent
    assert isinstance(es["missing"], list)


def test_no_basket_is_explicit(conn, cfg):
    case, sess, rx = _setup(conn, cfg)
    pa = peers.peer_analysis(conn, case["case_id"], rx, sess[-1])
    assert pa["quality"] == "NO_BASKET"
