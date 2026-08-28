import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def prices() -> pd.DataFrame:
    """Deterministic two-ticker price history: AAA drifts up, BBB is flat."""
    index = pd.bdate_range("2024-01-01", periods=252)
    return pd.DataFrame(
        {
            "AAA": 100 * (1.001 ** np.arange(len(index))),
            "BBB": np.full(len(index), 50.0),
        },
        index=index,
    )
