"""Reproducible, purged walk-forward experiments and current research rankings."""

import html
import importlib.metadata
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from . import evaluation
from .backtest import MODES, metrics, select_weights, simulate
from .config import ResearchConfig
from .data import DataError, load_snapshot
from .qlib_engine import MIN_LEARNED_ITERATIONS, export_qlib, features, folds, train_predict
from .storage import digest, new_run, now_utc, write_json


def check_universe(symbols: list[str], config: ResearchConfig) -> None:
    if config.benchmark in symbols:
        raise DataError("Benchmark must be excluded from candidates")
    if len(symbols) < config.min_universe():
        raise DataError(
            f"top_k={config.top_k} needs at least {config.min_universe()} candidates "
            f"(max_selection_fraction={config.max_selection_fraction}); got {len(symbols)}. "
            "Selecting most of the universe is indistinguishable from equal weight."
        )


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
        if config.benchmark not in source["symbols"]:
            raise DataError("Include the benchmark in the snapshot")
        check_universe(symbols, config)
        dates = sorted(prices.date.unique().tolist())
        windows = folds(len(dates), config)
        if not windows or windows[-1].test_end - windows[0].test_start < 2:
            raise DataError("Insufficient history for purged training, validation and testing")
        export_qlib(prices, run / "qlib")
        data = features(run / "qlib", symbols, dates, config.horizon)
        predictions, fold_records = [], []
        for index, fold in enumerate(windows):
            model_path = run / f"model-{index:03d}.txt"
            scores, diagnostics = train_predict(data, dates, fold, config, model_path)
            predictions.append(scores)
            fold_records.append(
                {key: dates[value] for key, value in asdict(fold).items()}
                | diagnostics
                | {
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
        results, curves = {}, {}
        for mode in MODES:
            curves[mode] = simulate(adjusted, panel, config, mode=mode)
            curves[mode].to_csv(run / f"curve-{mode}.csv", float_format="%.12g")
            results[mode] = metrics(curves[mode])
        results["model_double_cost"] = metrics(
            simulate(adjusted, panel, config, cost_bps=config.cost_bps * 2)
        )
        relative = {
            mode: evaluation.relative(curves[mode], curves["equal_weight"])
            for mode in ("model", "momentum", "momentum_12_1")
        }
        ics = evaluation.rank_ics(panel, adjusted, config.horizon)
        for record in fold_records:
            record["rank_ic"] = evaluation.summarize(
                ics.loc[record["test_start"] : record["test_end"]]
            )
        sparse = evaluation.non_overlapping(ics, panel.index, config.horizon)
        unlearned = sum(record["did_not_learn"] for record in fold_records)
        metrics_result = {
            "strategies": results,
            "relative_to_equal_weight": relative,
            "mean_rank_ic": float(ics.mean()) if len(ics) else None,
            "rank_ic_dates": len(ics),
            # Overlapping labels: the daily series is descriptive only. Inference uses
            # every horizon-th signal date, whose label windows do not overlap.
            "ic_inference": "descriptive_only_overlapping_labels",
            "rank_ic_non_overlapping": evaluation.summarize(sparse)
            | {"step_sessions": config.horizon},
            "rank_ic_by_year": evaluation.by_year(ics),
            "fold_diagnostics": {
                "folds": len(fold_records),
                "did_not_learn": unlearned,
                "did_not_learn_rule": f"best_iteration <= {MIN_LEARNED_ITERATIONS}",
            },
            "tie_rule": "tied cutoff scores share the remaining top_k budget equally",
            "folds": fold_records,
        }
        write_json(run / "evaluation.json", metrics_result)
        latest = panel.iloc[-1]
        selected = select_weights(latest, config.top_k, config.max_weight)
        # Competition ranking: equal scores share a rank; list order within a tie is
        # alphabetical for display only and carries no meaning.
        ranks = latest.rank(method="min", ascending=False).astype(int)
        order = sorted(latest.index, key=lambda symbol: (ranks[symbol], symbol))
        candidates = [
            {
                "symbol": symbol,
                "rank": int(ranks[symbol]),
                "score": float(latest[symbol]),
                "tied": bool((latest == latest[symbol]).sum() > 1),
                "selected": bool(selected[symbol] > 0),
                "target_weight": float(selected[symbol]),
                "state": "research_candidate",
                "fundamental_review": "pending",
            }
            for symbol in order
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
                "candidates": [c for c in candidates if c["selected"]],
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
        write_report(run, source, ranking, results, metrics_result)
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


def write_report(run: Path, source: dict, ranking: dict, results: dict, evaluated: dict) -> None:
    def esc(value):
        return html.escape(str(value))

    def num(value, spec=".3f"):
        return "—" if value is None else format(value, spec)

    def ic_cells(stats):
        return (
            f"<td>{num(stats['mean'])}</td><td>{num(stats['t_stat'], '.2f')}</td>"
            f"<td>{num(stats['positive_share'], '.0%')}</td><td>{stats['dates']}</td>"
        )

    label = "合成数据验证" if source["synthetic"] else "美股研究样本"
    names = {
        "model": "模型",
        "equal_weight": "等权股票池",
        "momentum": "60 日动量",
        "momentum_12_1": "12-1 月动量",
        "benchmark": "SPY",
        "model_double_cost": "模型（双倍成本）",
    }
    rows = "".join(
        f"<tr><td>{esc(names.get(name, name))}</td><td>{value['annualized_return']:.1%}</td>"
        f"<td>{value['max_drawdown']:.1%}</td><td>{value['total_return']:.1%}</td></tr>"
        for name, value in results.items()
    )
    relative = "".join(
        f"<tr><td>{esc(names[name])}</td><td>{num(value['annualized_active_return'], '.1%')}</td>"
        f"<td>{num(value['tracking_error'], '.1%')}</td>"
        f"<td>{num(value['information_ratio'], '.2f')}</td>"
        f"<td>{num(value['active_t_stat'], '.2f')}</td></tr>"
        for name, value in evaluated["relative_to_equal_weight"].items()
    )
    sparse = evaluated["rank_ic_non_overlapping"]
    years = "".join(
        f"<tr><td>{esc(year)}</td>{ic_cells(stats)}</tr>"
        for year, stats in evaluated["rank_ic_by_year"].items()
    )
    folds = "".join(
        f"<tr{' class=flag' if row['did_not_learn'] else ''}><td>{index}</td>"
        f"<td>{esc(row['test_start'])} – {esc(row['test_end'])}</td>"
        f"<td>{row['best_iteration']}{' ⚠' if row['did_not_learn'] else ''}</td>"
        f"<td>{row['valid_l2']:.4f} / {row['valid_l2_constant']:.4f}</td>"
        f"<td>{row['mean_unique_score_ratio']:.0%}</td>{ic_cells(row['rank_ic'])}</tr>"
        for index, row in enumerate(evaluated["folds"])
    )
    diagnostics = evaluated["fold_diagnostics"]
    warning = (
        f"<p class=warn>{diagnostics['did_not_learn']} / {diagnostics['folds']} 个窗口在 "
        f"{esc(diagnostics['did_not_learn_rule'])} 时提前停止，模型在这些窗口几乎没有学到信号，"
        "分数退化为少数几个取值。</p>"
        if diagnostics["did_not_learn"]
        else ""
    )
    candidates = "".join(
        f"<tr><td>{row['rank']}{' =' if row['tied'] else ''}</td><td>{esc(row['symbol'])}</td>"
        f"<td>{row['score']:.4f}</td><td>{row['target_weight']:.1%}</td><td>待基本面复核</td></tr>"
        for row in ranking["candidates"]
    )
    body = f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>美股研究</title><style>
body{{font:18px/1.65 system-ui,sans-serif;max-width:1000px;margin:48px auto;padding:0 24px;
color:#20302c;background:#fafbf8}}h1{{font-size:38px}}h2{{margin-top:36px}}
table{{border-collapse:collapse;width:100%;background:white}}th,td{{text-align:left;
padding:12px 16px;border-bottom:1px solid #dce4df}}th{{background:#eaf0eb}}
.state{{padding:18px 24px;border-left:4px solid #347456;background:#eaf0eb}}
.warn{{padding:18px 24px;border-left:4px solid #b4572e;background:#f7ebe4}}
tr.flag td{{background:#fbf1ec}}</style>
<h1>美股研究 · {esc(ranking["as_of"])}</h1><p class="state">{label}。
当前股票池未完成历史成分验证；本轮结果用于研究，不生成交易指令。</p>{warning}
<h2>滚动验证</h2><p>按信号次日收盘执行，包含双边交易成本和末日清仓成本。
各策略使用相同样本外区间；收益基于复权价格。现金收益按零计算。</p>
<table><tr><th>策略</th><th>年化收益</th><th>最大回撤</th><th>累计收益</th></tr>{rows}</table>
<h2>相对等权股票池</h2><p>主动收益为日收益差的年化均值；t 值按日收益计算，
未校正序列相关，偏乐观。</p>
<table><tr><th>策略</th><th>年化主动收益</th><th>跟踪误差</th><th>信息比率</th><th>t 值</th></tr>
{relative}</table>
<h2>Rank IC</h2><p>全部交易日均值 {num(evaluated["mean_rank_ic"])}
（{evaluated["rank_ic_dates"]} 日，标签重叠，仅作描述）。
每 {sparse["step_sessions"]} 个交易日取一次、标签不重叠：均值 {num(sparse["mean"])}，
t 值 {num(sparse["t_stat"], ".2f")}，样本 {sparse["dates"]} 个。</p>
<table><tr><th>年份</th><th>均值</th><th>t 值（重叠）</th><th>为正比例</th><th>日数</th></tr>
{years}</table>
<h2>训练窗口</h2><p>验证误差与常数预测误差对比；唯一分数比例为每日不同分数个数 / 股票数。
标 ⚠ 的窗口视为未学到信号。窗口内 t 值基于重叠标签，仅作描述。</p>
<table><tr><th>#</th><th>测试区间</th><th>最佳迭代</th><th>验证 L2 / 常数</th>
<th>唯一分数</th><th>IC 均值</th><th>t 值</th><th>为正</th><th>日数</th></tr>{folds}</table>
<h2>候选排序</h2><p>预测周期 {ranking["horizon_sessions"]} 个交易日。
分数表示相对排序，不是涨幅预测；基本面证据尚未接入。
同分（标 =）共享名次；处在入选边界的同分股票平分剩余仓位，不按代码字母排序取舍。</p>
<table><tr><th>排名</th><th>股票</th><th>模型分数</th><th>目标权重</th><th>状态</th></tr>
{candidates}</table>
</html>"""
    (run / "report.html").write_text(body)
