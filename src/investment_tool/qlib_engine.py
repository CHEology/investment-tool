"""Qlib Alpha158 + LightGBM; models are saved as text, never executable pickles."""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import ResearchConfig
from .data import DataError

# A fold whose validation loss stops improving within this many trees has learned
# essentially nothing: scores collapse to a handful of leaf values per day.
MIN_LEARNED_ITERATIONS = 5


@dataclass(frozen=True)
class Fold:
    train_start: int
    train_end: int
    valid_start: int
    valid_end: int
    test_start: int
    test_end: int


def folds(size: int, config: ResearchConfig) -> list[Fold]:
    # Signal at t, entry close t+1, exit close t+1+horizon. The last label's
    # endpoint must be strictly before the next partition's first observation.
    gap = config.horizon + 1
    first = config.warmup_sessions + config.train_sessions + config.valid_sessions + 2 * gap
    result = []
    for test_start in range(first, size, config.test_sessions):
        valid_end = test_start - gap - 1
        valid_start = valid_end - config.valid_sessions + 1
        train_end = valid_start - gap - 1
        result.append(
            Fold(
                train_end - config.train_sessions + 1,
                train_end,
                valid_start,
                valid_end,
                test_start,
                min(test_start + config.test_sessions - 1, size - 1),
            )
        )
    return result


def export_qlib(frame: pd.DataFrame, destination: Path) -> None:
    """Write Qlib's documented float32 bin format into a fresh private run."""
    destination.mkdir(parents=True, exist_ok=False)
    dates = sorted(frame.date.unique())
    (destination / "calendars").mkdir()
    (destination / "calendars/day.txt").write_text("\n".join(dates) + "\n")
    (destination / "instruments").mkdir()
    names = []
    for symbol, rows in frame.groupby("symbol", sort=True):
        rows = rows.set_index("date").reindex(dates)
        factor = rows.adj_close / rows.close
        values = {key: rows[key] * factor for key in ("open", "high", "low", "close")}
        values.update(
            volume=rows.volume / factor, factor=factor, vwap=pd.Series(np.nan, index=rows.index)
        )
        folder = destination / "features" / symbol.lower()
        folder.mkdir(parents=True)
        for field, column in values.items():
            np.concatenate(([0], column.to_numpy())).astype("<f4").tofile(
                folder / f"{field}.day.bin"
            )
        names.append(f"{symbol}\t{dates[0]}\t{dates[-1]}")
    (destination / "instruments/all.txt").write_text("\n".join(names) + "\n")


def features(provider: Path, symbols: list[str], dates: list[str], horizon: int):
    import qlib
    from qlib.constant import REG_US
    from qlib.contrib.data.handler import Alpha158

    qlib.init(
        provider_uri=str(provider.resolve()),
        region=REG_US,
        kernels=1,
        expression_cache=None,
        dataset_cache=None,
    )
    handler = Alpha158(
        instruments=symbols,
        start_time=dates[0],
        end_time=dates[-1],
        infer_processors=[],
        learn_processors=[],
        label=([f"Ref($close, -{horizon + 1})/Ref($close, -1)-1"], ["LABEL0"]),
    )
    frame = handler.fetch(col_set=["feature", "label"])
    frame = frame.replace([np.inf, -np.inf], np.nan)
    if frame["feature"].shape[1] != 158:
        raise DataError("Unexpected Alpha158 feature schema")
    return frame


def train_predict(
    frame: pd.DataFrame, dates: list[str], fold: Fold, config: ResearchConfig, model_path: Path
):
    import lightgbm as lgb

    observations = frame.index.get_level_values("datetime")

    def subset(start, end):
        return frame.loc[
            (observations >= pd.Timestamp(dates[start]))
            & (observations <= pd.Timestamp(dates[end]))
        ]

    train = subset(fold.train_start, fold.train_end).dropna(subset=[("label", "LABEL0")])
    valid = subset(fold.valid_start, fold.valid_end).dropna(subset=[("label", "LABEL0")])
    test = subset(fold.test_start, fold.test_end)
    if min(len(train), len(valid), len(test)) == 0:
        raise DataError("Empty train, validation or prediction segment")

    # Cross-sectional target rank is not a percent return forecast.
    def target(rows):
        return rows["label"]["LABEL0"].groupby(level="datetime").rank(pct=True) - 0.5

    def dataset(rows):
        return lgb.Dataset(rows["feature"], label=target(rows))

    model = lgb.train(
        {
            "objective": "regression",
            "metric": "l2",
            "learning_rate": 0.04,
            "num_leaves": 15,
            "min_data_in_leaf": 50,
            "feature_fraction": 0.8,
            "seed": config.seed,
            "deterministic": True,
            "force_col_wise": True,
            "num_threads": 2,
            "verbosity": -1,
        },
        dataset(train),
        valid_sets=[dataset(valid)],
        num_boost_round=config.num_boost_round,
        callbacks=[lgb.early_stopping(20, verbose=False)],
    )
    model.save_model(str(model_path))
    scores = pd.Series(model.predict(test["feature"]), index=test.index, name="score")
    if not np.isfinite(scores).all():
        raise DataError("Non-finite model output")
    return scores, diagnostics(model, train, valid, test, scores, target)


def diagnostics(model, train, valid, test, scores: pd.Series, target) -> dict:
    """Evidence on whether a fold learned anything beyond a constant forecast."""
    valid_target = target(valid)
    constant = float(((valid_target - target(train).mean()) ** 2).mean())
    by_day = scores.groupby(level="datetime")
    best_iteration = int(model.best_iteration)
    return {
        "best_iteration": best_iteration,
        "did_not_learn": best_iteration <= MIN_LEARNED_ITERATIONS,
        "train_rows": len(train),
        "valid_rows": len(valid),
        "test_rows": len(test),
        "valid_l2": float(model.best_score["valid_0"]["l2"]),
        "valid_l2_constant": constant,
        "mean_unique_score_ratio": float((by_day.nunique() / by_day.size()).mean()),
    }
