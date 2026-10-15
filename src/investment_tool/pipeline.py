"""Reproducible, purged walk-forward experiments and current research rankings."""

import html
import importlib.metadata
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import metrics, simulate
from .config import ResearchConfig
from .data import DataError, load_snapshot
from .qlib_engine import export_qlib, features, folds, train_predict
from .storage import digest, new_run, now_utc, write_json


def research(snapshot: Path, root: Path, config: ResearchConfig) -> Path:
    run = new_run(root, "runs")
    manifest = {
        "schema_version": 1,
        "run_id": run.name,
        "created_at": now_utc(),
        "status": "running",
        "config": config.to_dict(),
        "snapshot": str(snapshot.resolve()),
        "artifacts": {},
    }
    try:
        prices, source = load_snapshot(snapshot)
        manifest["snapshot_manifest_sha256"] = digest(snapshot / "manifest.json")
        manifest["warnings"] = source["warnings"]
        manifest["synthetic"] = source["synthetic"]
        manifest["universe_status"] = source["universe_status"]
        manifest["versions"] = {
            name: importlib.metadata.version(name)
            for name in ("investment-tool", "pyqlib", "lightgbm", "pandas")
        }
        symbols = [s for s in source["symbols"] if s != config.benchmark]
        if config.benchmark not in source["symbols"] or len(symbols) < config.top_k:
            raise DataError("Include the benchmark and at least top_k candidate stocks")
        dates = sorted(prices.date.unique().tolist())
        windows = folds(len(dates), config)
        if not windows or windows[-1].test_end - windows[0].test_start < 2:
            raise DataError("Insufficient history for purged training, validation and testing")
        export_qlib(prices, run / "qlib")
        data = features(run / "qlib", symbols, dates, config.horizon)
        predictions, fold_records = [], []
        for index, fold in enumerate(windows):
            model_path = run / f"model-{index:03d}.txt"
            scores, best_iteration = train_predict(data, dates, fold, config, model_path)
            predictions.append(scores)
            fold_records.append(
                {key: dates[value] for key, value in asdict(fold).items()}
                | {
                    "best_iteration": best_iteration,
                    "model": model_path.name,
                    "model_sha256": digest(model_path),
                }
            )
        scores = pd.concat(predictions).sort_index()
        panel = scores.unstack("instrument").sort_index()
        panel.index = panel.index.strftime("%Y-%m-%d")
        panel.index.name = "date"
        if set(panel.columns) != set(symbols) or not np.isfinite(panel.to_numpy()).all():
            raise DataError("Predictions do not cover the full candidate universe")
        panel.to_csv(run / "scores.csv", float_format="%.12g")
        adjusted = prices.pivot(index="date", columns="symbol", values="adj_close").sort_index()
        results = {}
        for mode in ("model", "equal_weight", "momentum", "benchmark"):
            curve = simulate(adjusted, panel, config, mode=mode)
            curve.to_csv(run / f"curve-{mode}.csv", float_format="%.12g")
            results[mode] = metrics(curve)
        results["model_double_cost"] = metrics(
            simulate(adjusted, panel, config, cost_bps=config.cost_bps * 2)
        )
        # IC is descriptive: overlapping 20-session labels are not independent samples.
        forward = adjusted.shift(-(config.horizon + 1)) / adjusted.shift(-1) - 1
        ics = panel.corrwith(
            forward.reindex(index=panel.index, columns=panel.columns), axis=1, method="spearman"
        ).dropna()
        metrics_result = {
            "strategies": results,
            "mean_rank_ic": float(ics.mean()) if len(ics) else None,
            "rank_ic_dates": len(ics),
            "ic_inference": "descriptive_only_overlapping_labels",
            "folds": fold_records,
        }
        write_json(run / "evaluation.json", metrics_result)
        latest = panel.iloc[-1].sort_index().sort_values(ascending=False, kind="stable")
        candidates = [
            {
                "symbol": symbol,
                "rank": index + 1,
                "score": float(score),
                "state": "research_candidate",
                "fundamental_review": "pending",
            }
            for index, (symbol, score) in enumerate(latest.items())
        ]
        ranking = {
            "as_of": panel.index[-1],
            "horizon_sessions": config.horizon,
            "score_meaning": "relative cross-sectional rank, not a return forecast",
            "candidates": candidates,
            "live_ready": False,
            "reason": "Historical membership and prospective paper tracking not validated",
        }
        write_json(run / "ranking.json", ranking)
        write_json(
            run / "review-packet.json",
            {
                "as_of": panel.index[-1],
                "run_id": run.name,
                "snapshot_manifest_sha256": manifest["snapshot_manifest_sha256"],
                "candidates": candidates[: config.top_k],
                "required_evidence": [
                    "Latest filed financial statements and publication dates",
                    "Valuation assumptions and currency-compatible inputs",
                    "News and company announcements with source URLs",
                    "Disconfirming evidence and thesis invalidation conditions",
                ],
                "evidence_status": "pending",
                "model_explanation_status": "not_generated",
            },
        )
        write_report(run, source, ranking, results)
        for path in sorted(run.iterdir()):
            if path.is_file() and path.name != "manifest.json":
                manifest["artifacts"][path.name] = digest(path)
        manifest.update(
            status="completed", live_ready=False, evidence_end=source["end"], completed_at=now_utc()
        )
    except Exception as error:
        manifest.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        write_json(run / "manifest.json", manifest)
    return run


def write_report(run: Path, source: dict, ranking: dict, results: dict) -> None:
    def esc(value):
        return html.escape(str(value))

    label = "合成数据验证" if source["synthetic"] else "美股研究样本"
    rows = "".join(
        f"<tr><td>{esc(name)}</td><td>{value['annualized_return']:.1%}</td>"
        f"<td>{value['max_drawdown']:.1%}</td><td>{value['total_return']:.1%}</td></tr>"
        for name, value in results.items()
    )
    candidates = "".join(
        f"<tr><td>{row['rank']}</td><td>{esc(row['symbol'])}</td>"
        f"<td>{row['score']:.4f}</td><td>待基本面复核</td></tr>"
        for row in ranking["candidates"]
    )
    body = f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>美股研究</title><style>
body{{font:18px/1.65 system-ui,sans-serif;max-width:1000px;margin:48px auto;padding:0 24px;
color:#20302c;background:#fafbf8}}h1{{font-size:38px}}h2{{margin-top:36px}}
table{{border-collapse:collapse;width:100%;background:white}}th,td{{text-align:left;
padding:12px 16px;border-bottom:1px solid #dce4df}}th{{background:#eaf0eb}}
.state{{padding:18px 24px;border-left:4px solid #347456;background:#eaf0eb}}</style>
<h1>美股研究 · {esc(ranking["as_of"])}</h1><p class="state">{label}。
当前股票池未完成历史成分验证；本轮结果用于研究，不生成交易指令。</p>
<h2>滚动验证</h2><p>按信号次日收盘执行，包含双边交易成本和末日清仓成本。
各策略使用相同样本外区间；收益基于复权价格。现金收益按零计算。</p>
<table><tr><th>策略</th><th>年化收益</th><th>最大回撤</th><th>累计收益</th></tr>{rows}</table>
<h2>候选排序</h2><p>预测周期 {ranking["horizon_sessions"]} 个交易日。
分数表示相对排序，不是涨幅预测；基本面证据尚未接入。</p>
<table><tr><th>排名</th><th>股票</th><th>模型分数</th><th>状态</th></tr>{candidates}</table>
</html>"""
    (run / "report.html").write_text(body)
