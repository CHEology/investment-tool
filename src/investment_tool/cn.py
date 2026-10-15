"""Run the upstream CSI300 experiment with Qlib's own training and backtest."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from html import escape
from importlib.metadata import version
from pathlib import Path
from zipfile import ZipFile

QLIB_COMMIT = "54355232463878d2eebb91fe0ee5fa7fa1f5976c"
CONFIG_URL = (
    f"https://github.com/microsoft/qlib/blob/{QLIB_COMMIT}/"
    "examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml"
)
DATA_URL = (
    "https://github.com/SunsetWolf/qlib_dataset/releases/download/v2/qlib_data_cn_1d_latest.zip"
)
DATA_SHA256 = "8a323fec32e1bd0c1662c1fc753ad033f190523e450936e63c9509796ea32e41"
OFFICIAL_METRICS = {
    "IC": 0.0448,
    "ICIR": 0.3660,
    "Rank IC": 0.0469,
    "Rank ICIR": 0.3877,
    "1day.excess_return_with_cost.annualized_return": 0.0901,
    "1day.excess_return_with_cost.information_ratio": 1.0164,
    "1day.excess_return_with_cost.max_drawdown": -0.1038,
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def prepare_data(root: Path) -> Path:
    from qlib.tests.data import GetData

    folder = root.resolve() / "cn-official" / "data"
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "qlib_data_cn_1d_latest.zip"
    if not archive.exists():
        GetData().download(DATA_URL, archive)
        write_json(
            folder / "source.json",
            {
                "schema_version": 1,
                "provider": "Qlib GetData default mirror (SunsetWolf/qlib_dataset)",
                "dataset": "qlib_data_cn_1d_latest v2",
                "url": DATA_URL,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "sha256": digest(archive),
                "bytes": archive.stat().st_size,
                "archive": str(archive),
            },
        )
    if digest(archive) != DATA_SHA256:
        raise ValueError("Qlib historical data differs from the pinned reference archive")
    destination = folder / "cn_data"
    if not destination.exists():
        with ZipFile(archive) as bundle:
            bundle.extractall(destination)
    return destination


def audit_data(provider: Path) -> dict:
    import numpy as np
    import qlib
    from qlib.data import D

    qlib.init(provider_uri=str(provider), region="cn", kernels=2, joblib_backend="threading")
    calendar = D.calendar(start_time="2008-01-01", end_time="2020-08-04")
    fields = ["$open", "$high", "$low", "$close", "$volume", "$factor", "$vwap"]
    frame = D.features(
        D.instruments("csi300"), fields, start_time="2008-01-01", end_time="2020-08-04"
    )
    active = frame.loc[frame["$volume"] > 0]
    prices = active[["$open", "$high", "$low", "$close"]]
    invalid = (
        prices.isna().any(axis=1)
        | (prices <= 0).any(axis=1)
        | (prices.max(axis=1) > active["$high"] * (1 + 1e-5))
        | (prices.min(axis=1) < active["$low"] * (1 - 1e-5))
    )
    bench = D.features(["SH000300"], ["$close"], start_time="2017-01-01", end_time="2020-08-01")
    counts = frame.groupby(level="datetime").size()
    failures = []
    if frame.empty or frame.index.has_duplicates or np.isinf(frame.to_numpy()).any():
        failures.append("Empty, duplicate or infinite observations")
    if bench.empty or bench.isna().any().any() or (bench <= 0).any().any():
        failures.append("Missing or invalid CSI300 benchmark")
    expected = len(D.calendar(start_time="2017-01-01", end_time="2020-08-01"))
    if len(bench) != expected or calendar[0].year != 2008 or calendar[-1].year != 2020:
        failures.append("Incomplete benchmark period")
    quality_failures = list(failures)
    if invalid.any() or (active["$factor"].isna() | (active["$factor"] <= 0)).any():
        quality_failures.append("Invalid active-session OHLC or adjustment factor")
    return {
        "status": "failed" if quality_failures else "passed",
        "execution_failures": failures,
        "quality_failures": quality_failures,
        "purpose": "Unmodified upstream reproduction, not investment qualification",
        "rows": len(frame),
        "symbols_over_history": frame.index.get_level_values("instrument").nunique(),
        "members_per_session_min": int(counts.min()),
        "members_per_session_max": int(counts.max()),
        "missing_by_field": {key: int(value) for key, value in frame.isna().sum().items()},
        "active_rows": len(active),
        "invalid_active_ohlc_rows": int(invalid.sum()),
        "ohlc_examples": [
            {"instrument": str(index[0]), "date": str(index[1].date()), **row.to_dict()}
            for index, row in prices.loc[invalid].head(10).iterrows()
        ],
        "benchmark_sessions": len(bench),
        "calendar_end": (provider / "calendars/day.txt").read_text().splitlines()[-1],
        "membership_sha256": digest(provider / "instruments/csi300.txt"),
        "notes": [
            "使用数据包中的历史成分区间，未用今天的成分股回填历史。",
            "停牌或无成交记录保持原样，由 Qlib 原生数据处理与交易规则处理。",
            "示例数据的来源和复权方式沿用上游；本检查不等于独立核验全部行情。",
        ],
    }


def runtime_config(config_path: Path, provider: Path, threads: int) -> dict:
    from ruamel.yaml import YAML

    config = YAML(typ="safe").load(config_path.read_text())
    config["qlib_init"].update(
        provider_uri=str(provider.resolve()), kernels=2, joblib_backend="threading"
    )
    config["task"]["model"]["kwargs"]["num_threads"] = threads
    return config


def benchmark(root: Path, threads: int = 20) -> Path:
    from mlflow.tracking import MlflowClient
    from qlib.workflow.recorder import MLflowRecorder
    from ruamel.yaml import YAML

    config_path = Path("config/qlib/workflow_config_lightgbm_Alpha158.yaml")
    source = json.loads(config_path.with_name("source.json").read_text())
    if digest(config_path) != source["sha256"]:
        raise ValueError("Official benchmark configuration has been modified")
    provider = prepare_data(root)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run = root.resolve() / "cn-official" / "runs" / run_id
    run.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "qlib_commit": QLIB_COMMIT,
        "versions": {name: version(name) for name in ["pyqlib", "lightgbm", "pandas", "numpy"]},
        "data": json.loads((provider.parent / "source.json").read_text()),
        "official_config_url": CONFIG_URL,
        "official_config_sha256": digest(config_path),
        "overrides": {
            "qlib_init.provider_uri": str(provider),
            "qlib_init.kernels": 2,
            "qlib_init.joblib_backend": "threading",
            "task.model.kwargs.num_threads": threads,
        },
    }
    write_json(run / "manifest.json", manifest)
    try:
        quality = audit_data(provider)
        write_json(run / "data-quality.json", quality)
        manifest["data_quality"] = quality["status"]
        manifest["investment_qualification"] = "not_evaluated"
        if quality["quality_failures"]:
            manifest["investment_qualification"] = "blocked_by_data_quality"
        if quality["execution_failures"]:
            raise ValueError("; ".join(quality["execution_failures"]))
        config = runtime_config(config_path, provider, threads)
        workflow = run / "workflow.yaml"
        with workflow.open("w") as stream:
            YAML().dump(config, stream)
        # MLflow 3.12 rejects a file store with an ancestor named "artifacts".
        tracking = Path("mlruns/cn-official").resolve() / run_id
        manifest["tracking_uri"] = tracking.as_uri()
        command = [sys.executable, "-m", "qlib.cli.run", str(workflow), run_id, str(tracking)]
        manifest["command"] = command
        write_json(run / "manifest.json", manifest)
        print(f"Running official workflow; log: {run / 'workflow.log'}", flush=True)
        environment = dict(os.environ, MLFLOW_ALLOW_FILE_STORE="true")
        with (run / "workflow.log").open("w") as log:
            subprocess.run(
                command, stdout=log, stderr=subprocess.STDOUT, env=environment, check=True
            )
        os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"
        client = MlflowClient(tracking_uri=tracking.as_uri())
        experiment = client.get_experiment_by_name(run_id)
        result = client.search_runs([experiment.experiment_id])[0]
        if result.info.status != "FINISHED":
            raise ValueError(f"Official Qlib recorder status: {result.info.status}")
        recorder = MLflowRecorder(experiment_id=experiment.experiment_id, uri=tracking.as_uri())
        recorder.id = result.info.run_id
        summary = summarize(recorder, result.data.metrics, run)
        render_report(run, summary, quality)
        manifest.update(
            status="completed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            recorder_id=result.info.run_id,
            outputs={
                p.name: digest(p)
                for p in run.iterdir()
                if p.is_file() and p.name != "manifest.json"
            },
        )
    except Exception as error:
        manifest.update(status="failed", error=str(error))
        write_json(run / "manifest.json", manifest)
        raise
    write_json(run / "manifest.json", manifest)
    write_json(root / "cn-official" / "latest.json", {"run": str(run)})
    return run


def summarize(recorder, metrics: dict, run: Path) -> dict:
    import pandas as pd

    report = recorder.load_object("portfolio_analysis/report_normal_1day.pkl")
    report.to_csv(run / "daily-returns.csv", float_format="%.17g")
    prediction = recorder.load_object("pred.pkl")
    prediction.to_csv(run / "predictions.csv")
    ic = recorder.load_object("sig_analysis/ic.pkl")
    rank_ic = recorder.load_object("sig_analysis/ric.pkl")
    pd.concat([ic.rename("IC"), rank_ic.rename("Rank IC")], axis=1).to_csv(run / "daily-ic.csv")
    net = report["return"].astype(float) - report["cost"].astype(float)
    nav = (1 + net).cumprod()
    drawdown = nav / nav.cummax().clip(lower=1) - 1
    annual = []
    for year, group in report.groupby(report.index.year):
        annual.append(
            {
                "year": int(year),
                "sessions": len(group),
                "portfolio_return": float((1 + group["return"] - group["cost"]).prod() - 1),
                "benchmark_return": float((1 + group["bench"].astype(float)).prod() - 1),
                "mean_rank_ic": float(rank_ic.loc[str(year)].mean()),
            }
        )
    summary = {
        "start": str(report.index.min().date()),
        "end": str(report.index.max().date()),
        "sessions": len(report),
        "metrics": {key: float(metrics[key]) for key in OFFICIAL_METRICS},
        "official_reference": OFFICIAL_METRICS,
        "portfolio_total_return_with_cost": float((1 + net).prod() - 1),
        "portfolio_max_drawdown_with_cost": float(drawdown.min()),
        "benchmark_total_return": float((1 + report["bench"].astype(float)).prod() - 1),
        "annual": annual,
        "metric_definition": (
            "官方表的年化超额收益为每日(组合收益−成本−指数收益)均值×238；"
            "回撤也是该超额收益算术累计曲线的回撤，均不是账户复合年收益或账户净值回撤。"
        ),
    }
    write_json(run / "summary.json", summary)
    return summary


def render_report(run: Path, summary: dict, quality: dict) -> None:
    labels = ["IC", "ICIR", "Rank IC", "Rank ICIR", "扣费年化超额收益", "信息比率", "超额收益回撤"]
    rows = []
    for label, key in zip(labels, OFFICIAL_METRICS, strict=True):
        actual, reference = summary["metrics"][key], OFFICIAL_METRICS[key]
        percentage = key.endswith(("annualized_return", "max_drawdown"))
        fmt = ".2%" if percentage else ".4f"
        rows.append(f"<tr><td>{label}</td><td>{actual:{fmt}}</td><td>{reference:{fmt}}</td></tr>")
    years = "".join(
        f"<tr><td>{v['year']}</td><td>{v['sessions']}</td>"
        f"<td>{v['portfolio_return']:.2%}</td><td>{v['benchmark_return']:.2%}</td>"
        f"<td>{v['mean_rank_ic']:.4f}</td></tr>"
        for v in summary["annual"]
    )
    html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Qlib 沪深 300 官方基准复现</title>
<style>body{{font:17px/1.7 system-ui,sans-serif;color:#172b3a;background:#f5f7f9;
max-width:1000px;margin:40px auto;padding:0 24px}}h1{{font-size:32px}}h2{{font-size:23px}}
section{{background:white;border-radius:12px;padding:24px;margin:22px 0}}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
th,td{{text-align:right;padding:10px;border-bottom:1px solid #e4e9ee}}
th:first-child,td:first-child{{text-align:left}}a{{color:#17658c}}</style>
<h1>Qlib 沪深 300 官方基准复现</h1>
<p>测试区间 {summary["start"]} 至 {summary["end"]}，共 {summary["sessions"]} 个交易日。
使用官方原生训练、信号评价和组合回测。</p>
<p>历史复现已完成。上游示例数据包含 OHLC 异常，投资用途的数据检查未通过；
本报告保留原始输入，呈现这份数据上的官方流程结果。</p>
<section><h2>与官方基准对照</h2><table>
<tr><th>指标</th><th>本次单次运行</th><th>官方发布均值</th></tr>{"".join(rows)}</table>
<p>{summary["metric_definition"]}</p>
<p>官方表汇总了 20 次不同随机种子的运行。本次为一次原始配置复现；
依赖版本、数据快照和计算线程差异均保存在运行记录中。</p></section>
<section><h2>组合实际收益</h2>
<p>整个测试期扣费累计收益 {summary["portfolio_total_return_with_cost"]:.2%}；
沪深 300 指数同期累计收益 {summary["benchmark_total_return"]:.2%}。</p>
<p>组合账户最大回撤 {summary["portfolio_max_drawdown_with_cost"]:.2%}。
它衡量账户实际从高点下跌的幅度，与上表的超额收益回撤含义不同。</p>
<table><tr><th>年份</th><th>交易日</th><th>组合扣费收益</th><th>指数收益</th><th>Rank IC</th></tr>
{years}</table><p>每年收益按日复利计算；2020 年仅覆盖测试期内的部分年份。</p></section>
<section><h2>实验设置与数据</h2>
<p>训练：2008—2014 年；验证：2015—2016 年；测试：2017 年至 2020 年 8 月 1 日。
Alpha158 特征、LightGBM 官方参数、默认下一可交易日到再下一日的收益标签。
TopkDropout 持有 50 只股票，每日按排名尝试更换 5 只；模拟本金 1 亿元，
买入成本 0.05%、卖出成本 0.15%、最低费用 5 元，涨跌停阈值 9.5%。</p>
<p>历史股票池共涉及 {quality["symbols_over_history"]} 只股票；每个交易日成分数
{quality["members_per_session_min"]}—{quality["members_per_session_max"]}。
数据包日历截至 {quality["calendar_end"]}，用于历史复现。</p>
<p>数据检查：{escape(quality["status"])}。有 {quality["invalid_active_ohlc_rows"]} 条有效成交记录
不满足最高价／最低价关系，VWAP 缺失 {quality["missing_by_field"]["$vwap"]} 条。
本次为忠实复现而保留上游原始输入；投资用途的数据检查未通过。
停牌缺失记录未填成零。尚未验证 2020 年之后或个人账户规模下的效果。</p>
<p><a href="{CONFIG_URL}">固定版本官方配置</a> ·
<a href="https://github.com/microsoft/qlib/blob/{QLIB_COMMIT}/examples/benchmarks/README.md">
官方成绩表</a> ·
<a href="{DATA_URL}">上游下载程序使用的数据包</a> ·
<a href="summary.json">完整指标</a> · <a href="manifest.json">运行记录</a> ·
<a href="daily-returns.csv">每日收益</a> · <a href="workflow.log">训练日志</a>
</p></section></html>"""
    (run / "report.html").write_text(html)
