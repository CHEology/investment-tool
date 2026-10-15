import json

import pandas as pd
import pytest

from investment_tool.data import DataError
from investment_tool.storage import digest, write_json
from investment_tool.watch import expected_session, observe


@pytest.mark.parametrize(
    "now,expected",
    [
        ("2024-01-08T21:00:00Z", "2024-01-05"),
        ("2024-01-08T23:01:00Z", "2024-01-08"),
        ("2024-01-07T23:01:00Z", "2024-01-05"),
        ("2024-07-04T23:01:00Z", "2024-07-03"),
        ("2024-11-29T20:01:00Z", "2024-11-29"),
    ],
)
def test_completed_market_sessions(now, expected):
    assert expected_session(pd.Timestamp(now)) == expected


def fixture_run(tmp_path, synthetic=False, date="2024-01-08", candidates=None):
    run = tmp_path / "run"
    run.mkdir()
    candidates = candidates or [{"symbol": "AAA"}]
    write_json(run / "ranking.json", {"as_of": date, "candidates": candidates})
    write_json(run / "evaluation.json", {})
    write_json(
        run / "manifest.json",
        {
            "status": "completed",
            "synthetic": synthetic,
            "config": {"top_k": 1},
            "artifacts": {name: digest(run / name) for name in ["ranking.json", "evaluation.json"]},
        },
    )
    return run


def test_observation_is_idempotent(tmp_path):
    run = fixture_run(tmp_path)
    now = pd.Timestamp("2024-01-08T23:01:00Z")
    path = observe(run, tmp_path, now)
    before = path.read_bytes()
    assert observe(run, tmp_path, now) == path
    assert path.read_bytes() == before
    journal = json.loads(before)
    assert len(journal["observations"]) == 1
    assert journal["observations"][0]["baseline"]


def test_stale_and_synthetic_observations_block(tmp_path):
    run = fixture_run(tmp_path, date="2024-01-05")
    with pytest.raises(DataError, match="requires session"):
        observe(run, tmp_path, pd.Timestamp("2024-01-08T23:01:00Z"))
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["synthetic"] = True
    write_json(run / "manifest.json", manifest)
    with pytest.raises(DataError, match="non-synthetic"):
        observe(run, tmp_path)


def test_tied_cutoff_names_all_enter_the_journal(tmp_path):
    candidates = [
        {"symbol": "AAA", "selected": True},
        {"symbol": "BBB", "selected": True},
        {"symbol": "CCC", "selected": False},
    ]
    run = fixture_run(tmp_path, candidates=candidates)
    path = observe(run, tmp_path, pd.Timestamp("2024-01-08T23:01:00Z"))
    assert json.loads(path.read_text())["observations"][0]["top"] == ["AAA", "BBB"]
