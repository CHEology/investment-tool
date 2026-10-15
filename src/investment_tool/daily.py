"""One completed US session: prices, a reusable Qlib model, and a research packet."""

import tomllib
from pathlib import Path

import pandas as pd

from .backtest import select_weights
from .config import load_config
from .data import DataError, load_snapshot
from .providers import fetch_yahoo
from .qlib_engine import Fold, export_qlib, features, train_predict
from .storage import digest, new_run, now_utc, read_json, write_json
from .watch import expected_session, locked


def latest_fold(size, config):
    test = size - 1
    gap = config.horizon + 1
    valid_end = test - gap - 1
    valid_start = valid_end - config.valid_sessions + 1
    train_end = valid_start - gap - 1
    train_start = train_end - config.train_sessions + 1
    if train_start < config.warmup_sessions:
        raise DataError("Not enough history for daily training")
    return Fold(train_start, train_end, valid_start, valid_end, test, test)


def model_due(metadata, key, dates, interval):
    if metadata is None or metadata["key"] != key:
        return True
    trained = metadata["trained_as_of"]
    return trained > dates[-1] or sum(day > trained for day in dates) >= interval


def price_summary(prices):
    result = {}
    for symbol, rows in prices.groupby("symbol"):
        rows = rows.sort_values("date")
        adjusted = rows.adj_close
        last = adjusted.iloc[-1]
        result[symbol] = {
            "close": float(rows.close.iloc[-1]),
            **{f"return_{n}d": float(last / adjusted.iloc[-n - 1] - 1) for n in (1, 5, 20)},
            **{f"vs_ma{n}": float(last / adjusted.iloc[-n:].mean() - 1) for n in (50, 200)},
        }
    return result


def rank_candidates(scores, config, previous=None):
    signal = scores.droplevel("datetime").sort_index()
    weights = select_weights(signal, config.top_k, config.max_weight)
    ranks = signal.rank(ascending=False, method="min")
    old = {row["symbol"]: row["rank"] for row in previous["candidates"]} if previous else {}
    return [
        {
            "symbol": symbol,
            "rank": int(ranks[symbol]),
            "score": float(value),
            "selected": bool(weights.get(symbol, 0) > 0),
            "previous_rank": old.get(symbol),
        }
        for symbol, value in signal.sort_values(ascending=False, kind="stable").items()
    ]


def prepare_daily(root, config_path, snapshot=None, holdings=(), now=None):
    with locked(root / ".daily.lock"):
        return _prepare(root, config_path, snapshot, holdings, now)


def _prepare(root, config_path, snapshot, holdings, now):
    settings = tomllib.loads(config_path.read_text())
    daily = settings["daily"]
    config = load_config(config_path)
    symbols = sorted(set(daily["symbols"]))
    if len(symbols) < config.min_universe():
        raise DataError("Daily candidate universe is too small")
    all_symbols = sorted(set(symbols + daily["market_symbols"] + list(holdings)))
    key = {"config": config.to_dict(), "symbols": symbols, "features": "Alpha158-v1"}
    request = {"settings": settings, "price_symbols": all_symbols}
    as_of = expected_session(now)
    day = root / "days" / as_of
    manifest_path = day / "manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if manifest["request"] != request:
            raise DataError("This session already uses different inputs; use a separate --root")
        if digest(day / "packet.json") != manifest["packet_sha256"]:
            raise DataError("Daily packet hash mismatch")
        return day

    snapshot = snapshot or fetch_yahoo(all_symbols, daily["start"], as_of, root)
    prices, source = load_snapshot(snapshot)
    if source["synthetic"] or source["end"] != as_of:
        raise DataError(f"Daily research requires real prices through {as_of}")
    if not set(all_symbols) <= set(source["symbols"]):
        raise DataError("Snapshot is missing configured market or holding symbols")
    dates = sorted(prices.date.unique())
    fold = latest_fold(len(dates), config)
    work = new_run(root, "calculations")
    provider = work / "qlib"
    export_qlib(prices[prices.symbol.isin(symbols)], provider)
    frame = features(provider, symbols, dates, config.horizon)

    pointer = root / "model-current.json"
    metadata = read_json(pointer) if pointer.exists() else None
    retrained = model_due(metadata, key, dates, daily["retrain_sessions"])
    if retrained:
        model_dir = new_run(root, "models")
        model_path = model_dir / "model.txt"
        scores, diagnostics = train_predict(frame, dates, fold, config, model_path)
        metadata = {
            "key": key,
            "trained_as_of": as_of,
            "path": str(model_path.resolve()),
            "sha256": digest(model_path),
            "feature_names": list(frame["feature"].columns),
            "diagnostics": diagnostics,
            "windows": {
                "train": [dates[fold.train_start], dates[fold.train_end]],
                "valid": [dates[fold.valid_start], dates[fold.valid_end]],
            },
        }
        write_json(model_dir / "metadata.json", metadata)
        write_json(pointer, metadata)
    else:
        import lightgbm as lgb

        model_path = Path(metadata["path"])
        if digest(model_path) != metadata["sha256"]:
            raise DataError("Saved model hash mismatch")
        latest = frame["feature"].loc[[pd.Timestamp(as_of)]]
        if list(latest.columns) != metadata["feature_names"]:
            raise DataError("Saved model feature schema changed")
        model = lgb.Booster(model_file=str(model_path))
        scores = pd.Series(model.predict(latest), index=latest.index, name="score")

    earlier = sorted(p for p in (root / "days").glob("*/manifest.json") if p.parent.name < as_of)
    previous = None
    if earlier:
        prior_manifest = read_json(earlier[-1])
        prior_path = earlier[-1].parent / "packet.json"
        if digest(prior_path) != prior_manifest["packet_sha256"]:
            raise DataError("Previous daily packet hash mismatch")
        prior = read_json(prior_path)
        # Portfolio changes do not reset the stock model's observation history.
        if prior["model"]["key"] == key:
            previous = prior
    candidates = rank_candidates(scores, config, previous)
    selected = {row["symbol"] for row in candidates if row["selected"]}
    old = (
        {row["symbol"] for row in previous["candidates"] if row["selected"]} if previous else set()
    )
    market = price_summary(prices[prices.symbol.isin(all_symbols)])
    packet = {
        "as_of": as_of,
        "created_at": now_utc(),
        "snapshot": str(snapshot.resolve()),
        "snapshot_manifest_sha256": digest(snapshot / "manifest.json"),
        "model": metadata,
        "retrained": retrained,
        "score_kind": "relative_rank_not_return_forecast",
        "market_symbols": daily["market_symbols"],
        "holding_symbols": list(holdings),
        "prices": market,
        "breadth": {
            "universe": len(symbols),
            "advancing": sum(market[s]["return_1d"] > 0 for s in symbols),
            "above_ma200": sum(market[s]["vs_ma200"] > 0 for s in symbols),
        },
        "candidates": candidates,
        "unique_scores": int(scores.nunique()),
        "changes": {
            "baseline": previous is None,
            "previous_as_of": previous["as_of"] if previous else None,
            "model_changed": previous is not None
            and previous["model"]["sha256"] != metadata["sha256"],
            "entered": sorted(selected - old) if previous else [],
            "left": sorted(old - selected),
        },
    }
    write_json(day / "packet.json", packet)
    write_json(
        manifest_path,
        {
            "status": "awaiting_research",
            "as_of": as_of,
            "request": request,
            "packet_sha256": digest(day / "packet.json"),
        },
    )
    return day
