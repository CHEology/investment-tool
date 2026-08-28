import pytest

from investment_tool import config as config_mod


def test_v0_loads_and_all_entries_experimental():
    cfg = config_mod.load("v0")
    assert cfg.data["meta"]["status"] == "EXPERIMENTAL"
    assert cfg.value("lane_a.trigger.peer_adj_car_0_3_max") == "-0.10"
    assert cfg.value("peer_cells.min_peer_count") == 8
    assert cfg.value("boards.price_limits.BSE") == "0.30"


def test_register_is_idempotent_and_forward_only(conn):
    cfg = config_mod.load("v0")
    assert config_mod.register(conn, cfg) == "v0"
    assert config_mod.register(conn, cfg) == "v0"  # same content: fine

    tampered = config_mod.Config("v0", cfg.data, "deadbeef")
    with pytest.raises(RuntimeError, match="forward-only"):
        config_mod.register(conn, tampered)


def test_missing_key_raises_not_defaults():
    cfg = config_mod.load("v0")
    with pytest.raises(KeyError):
        cfg.get("lane_a.nonexistent.threshold")
