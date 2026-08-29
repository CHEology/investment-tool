import pytest

from investment_tool.db import connect


@pytest.fixture(autouse=True)
def isolate_data_dirs(tmp_path, monkeypatch):
    """No test may write into the real data directory: every module that
    emits audits, cards, raw payloads, params, exports, or backups is pointed
    at the test's tmp dir. Individual tests no longer need to remember to
    monkeypatch (the ones that still do are harmlessly redundant)."""
    from investment_tool import (
        cards,
        evidence_gateway,
        lane_a,
        lineage,
        peers,
        research,
        us_cli,
        us_queue,
        us_soak,
        validate,
    )

    for mod in (lineage, validate, us_cli, lane_a, us_queue, us_soak,
                research, evidence_gateway, peers):
        monkeypatch.setattr(mod, "DEFAULT_DATA_DIR", tmp_path, raising=True)
    monkeypatch.setattr(cards, "CARDS_DIR", tmp_path / "cards", raising=True)
    monkeypatch.setattr(lane_a, "PARAMS_DIR", tmp_path / "params", raising=True)


@pytest.fixture
def conn(tmp_path):
    """Isolated database + data dir per test."""
    c = connect(tmp_path)
    yield c
    c.close()
