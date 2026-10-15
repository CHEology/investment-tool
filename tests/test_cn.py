import json
from pathlib import Path

import pandas as pd
import pytest

from investment_tool.cn import OFFICIAL_METRICS, digest, runtime_config, summarize


def test_official_strategy_and_learning_config_are_preserved(tmp_path):
    yaml = pytest.importorskip("ruamel.yaml").YAML(typ="safe")
    path = Path("config/qlib/workflow_config_lightgbm_Alpha158.yaml")
    source = json.loads(path.with_name("source.json").read_text())
    assert digest(path) == source["sha256"]
    original = yaml.load(path.read_text())
    actual = runtime_config(path, tmp_path, 6)
    assert actual["qlib_init"].pop("provider_uri") == str(tmp_path.resolve())
    assert actual["qlib_init"].pop("kernels") == 2
    assert actual["qlib_init"].pop("joblib_backend") == "threading"
    actual["qlib_init"]["provider_uri"] = original["qlib_init"]["provider_uri"]
    actual["task"]["model"]["kwargs"]["num_threads"] = 20
    assert actual == original
    assert actual["task"]["record"][-1]["kwargs"]["config"] == actual["port_analysis_config"]


def test_result_report_separates_official_excess_metrics_and_account_return(tmp_path):
    index = pd.date_range("2020-01-02", periods=2)
    report = pd.DataFrame(
        {"return": [0.10, -0.05], "cost": [0.01, 0.01], "bench": [0.02, -0.01]}, index=index
    )

    class Recorder:
        def load_object(self, name):
            if name == "portfolio_analysis/report_normal_1day.pkl":
                return report
            if name == "pred.pkl":
                return pd.DataFrame({"score": [1, 2]}, index=index)
            return pd.Series([0.02, 0.04], index=index)

    metrics = dict.fromkeys(OFFICIAL_METRICS, 0.5)
    summary = summarize(Recorder(), metrics, tmp_path)
    assert summary["portfolio_total_return_with_cost"] == pytest.approx(1.09 * 0.94 - 1)
    assert summary["benchmark_total_return"] == pytest.approx(1.02 * 0.99 - 1)
    assert summary["portfolio_max_drawdown_with_cost"] == pytest.approx(-0.06)
    assert summary["metrics"] == metrics
    assert summary["annual"][0]["portfolio_return"] == pytest.approx(0.0246)
