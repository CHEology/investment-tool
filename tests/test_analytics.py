"""Scan-engine tests on a synthetic panel: 2 industries x 10 stocks x 30 days,
one engineered crash. No network, no real data."""

import numpy as np
import pandas as pd

from investment_tool import analytics


def make_panel(crash_stock="SZSE:000001", crash_days=("2026-01-20", "2026-01-21"),
               crash_ret=-0.11, n_days=45):
    dates = pd.bdate_range("2025-12-08", periods=n_days).strftime("%Y-%m-%d").tolist()
    rows = []
    rng = np.random.default_rng(7)
    for ind, n in (("光伏设备", 10), ("白酒", 10)):
        for i in range(n):
            lid = f"SZSE:{'0' if ind == '光伏设备' else '3'}{i:05d}"
            for d in dates:
                r = float(rng.normal(0, 0.004))
                if lid == crash_stock and d in crash_days:
                    r = crash_ret
                rows.append({"listing_id": lid, "trade_date": d, "pct_chg": r * 100,
                             "limit_state": "FREE", "close": "10", "amount": "50000000",
                             "exchange": "SZSE", "board": "MAIN", "ticker": lid[5:],
                             "ret": r, "amount_f": 5e7})
    df = pd.DataFrame(rows)
    cells = pd.DataFrame(
        [{"listing_id": lid, "industry": ind, "size_bucket": "MID",
          "cell": f"{ind}|MID", "is_st": 0, "name": lid}
         for ind, n in (("光伏设备", 10), ("白酒", 10))
         for lid in [f"SZSE:{'0' if ind == '光伏设备' else '3'}{i:05d}" for i in range(n)]]
    )
    return df, cells


def test_crash_triggers_and_peers_do_not():
    df, cells = make_panel()
    panel = analytics.build_ar_panel(df, cells)
    t0 = "2026-01-20"
    w = analytics.car_window(panel, "SZSE:000001", t0, k=3)
    sigma = analytics.residual_sigma(panel, "SZSE:000001", t0, window=120, min_obs=15)
    assert w["state"] == "COMPLETE"
    assert w["car"] < -0.18  # two -11% days, peer-adjusted
    verdict = analytics.evaluate_trigger(w["car"], sigma, -0.10, 3.0, 0.70)
    assert verdict == "TRIGGER"
    # an untouched peer does not trigger
    w2 = analytics.car_window(panel, "SZSE:000002", t0, k=3)
    sigma2 = analytics.residual_sigma(panel, "SZSE:000002", t0, window=120, min_obs=15)
    assert analytics.evaluate_trigger(w2["car"], sigma2, -0.10, 3.0, 0.70) == "NONE"


def test_sector_wide_crash_is_absorbed_by_peer_median():
    """If the WHOLE industry falls 11%, peer-adjusted AR ~ 0 -> no trigger.
    This is the anti-screener property (INV-3)."""
    df, cells = make_panel(crash_days=())  # sector-wide day only, no idiosyncratic crash
    mask = (df["listing_id"].str.startswith("SZSE:0")) & (df["trade_date"] == "2026-01-20")
    df.loc[mask, "ret"] = -0.11
    panel = analytics.build_ar_panel(df, cells)
    w = analytics.car_window(panel, "SZSE:000001", "2026-01-20", k=3)
    assert abs(w["car"]) < 0.02  # crash absorbed by the cell median
    sigma = analytics.residual_sigma(panel, "SZSE:000001", "2026-01-20", min_obs=15)
    assert analytics.evaluate_trigger(w["car"], sigma, -0.10, 3.0, 0.70) != "TRIGGER"


def test_limit_locked_days_pause_window_clock():
    df, cells = make_panel()
    lock_days = ["2026-01-20", "2026-01-21"]
    for d in lock_days:
        df.loc[(df["listing_id"] == "SZSE:000001") & (df["trade_date"] == d),
               "limit_state"] = "LIMIT_DOWN"
    panel = analytics.build_ar_panel(df, cells)
    w = analytics.car_window(panel, "SZSE:000001", "2026-01-20", k=3)
    # 2 locked + 4 free sessions must have elapsed
    assert w["sessions"] == 6 and w["free"] == 4 and w["state"] == "COMPLETE"


def test_shadow_band():
    verdict = analytics.evaluate_trigger(-0.08, 0.004, -0.10, 3.0, 0.70)
    assert verdict == "SHADOW"
    assert analytics.evaluate_trigger(-0.05, 0.004, -0.10, 3.0, 0.70) == "NONE"
    assert analytics.evaluate_trigger(None, 0.004, -0.10, 3.0, 0.70) == "DATA_INSUFFICIENT"
    assert analytics.evaluate_trigger(-0.2, None, -0.10, 3.0, 0.70) == "DATA_INSUFFICIENT"


def test_min_peer_fallback_to_industry_median():
    df, cells = make_panel()
    # shrink one cell below min_peers by dropping members
    keep = ~df["listing_id"].isin([f"SZSE:0{i:05d}" for i in range(3, 10)])
    panel = analytics.build_ar_panel(df[keep], cells[cells.listing_id.isin(df[keep].listing_id)],
                                     min_peers=8)
    assert "SZSE:000001" in panel.ar.columns  # still computable via fallback


def test_market_model_car_survives_degenerate_inputs():
    import pandas as pd

    df, cells = make_panel()
    panel = analytics.build_ar_panel(df, cells)
    flat_bench = pd.Series(0.0, index=panel.dates)  # zero-variance benchmark
    assert analytics.market_model_car(panel, flat_bench, "SZSE:000001",
                                      "2026-01-20", 3, min_obs=10) is None
    empty_bench = pd.Series(dtype=float)
    assert analytics.market_model_car(panel, empty_bench, "SZSE:000001",
                                      "2026-01-20", 3) is None
