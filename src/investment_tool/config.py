import math
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResearchConfig:
    horizon: int = 20
    train_sessions: int = 504
    valid_sessions: int = 126
    test_sessions: int = 63
    warmup_sessions: int = 60
    top_k: int = 10
    max_weight: float = 0.1
    cost_bps: float = 15.0
    benchmark: str = "SPY"
    seed: int = 42
    num_boost_round: int = 200
    # top_k may be at most this share of the candidate universe; otherwise the
    # "model" portfolio is nearly the equal-weight universe and tests nothing.
    max_selection_fraction: float = 0.3

    def __post_init__(self):
        for name in (
            "horizon",
            "train_sessions",
            "valid_sessions",
            "test_sessions",
            "warmup_sessions",
            "top_k",
            "num_boost_round",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.warmup_sessions < 60:
            raise ValueError("Alpha158 needs at least 60 warmup sessions")
        if not 0 < self.max_weight <= 1 or not 0 <= self.cost_bps < 1000:
            raise ValueError("Invalid concentration cap or transaction cost")
        if not 0 < self.max_selection_fraction <= 1:
            raise ValueError("max_selection_fraction must be in (0, 1]")
        if self.top_k * self.max_weight > 1.0000001:
            raise ValueError("top_k * max_weight must not exceed 1")

    def min_universe(self) -> int:
        """Smallest candidate universe for which top_k is a real selection."""
        return math.ceil(self.top_k / self.max_selection_fraction - 1e-9)

    def to_dict(self) -> dict:
        return asdict(self)


def load_config(path: Path | None) -> ResearchConfig:
    if path is None:
        return ResearchConfig()
    with path.open("rb") as handle:
        return ResearchConfig(**tomllib.load(handle)["research"])
