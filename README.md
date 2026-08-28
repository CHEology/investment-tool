# investment-tool

Portfolio analytics and market data tooling. Fetches price history, tracks
holdings, and reports risk/return statistics from the command line or a notebook.

> Not investment advice. This is a calculation tool — it reports what the numbers
> did, and makes no recommendation about what to buy, sell, or hold.

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Then activate the environment:

```bash
source .venv/bin/activate
```

Optional API keys go in `.env` (copy `.env.example`). `.env` is gitignored —
this repository is public, so keep keys and real holdings out of commits.

## Usage

Fetch adjusted close prices:

```bash
invest prices VOO AAPL --start 2024-01-01
```

Report on a portfolio. Copy `examples/holdings.example.json` somewhere
gitignored (`data/` works) and edit it:

```bash
invest report data/holdings.json
```

Output covers allocation weights plus total return, CAGR, annualized
volatility, Sharpe ratio, and max drawdown. Add `--json` for machine-readable
output.

### As a library

```python
from investment_tool import Portfolio, performance_summary
from investment_tool.data import fetch_history

portfolio = Portfolio({"VOO": 40, "AAPL": 25}, cash=2500)
prices = fetch_history(portfolio.tickers, start="2024-01-01")
performance_summary(portfolio.equity_curve(prices))
```

## Layout

| Path | What's in it |
| --- | --- |
| `src/investment_tool/data.py` | Price fetching (yfinance) with an on-disk parquet cache |
| `src/investment_tool/portfolio.py` | `Portfolio` holdings model and performance metrics |
| `src/investment_tool/cli.py` | `invest` command line entry point |
| `tests/` | pytest suite, runs offline against synthetic prices |
| `notebooks/` | Exploratory analysis |
| `data/` | Local price cache and private holdings — gitignored |

## Development

```bash
pytest
ruff check .
```

Prices are cached under `data/prices/` as parquet. Pass `--no-cache` to force a
refresh.

## Caveats

- `Portfolio` assumes a static share count over the reporting window; it does
  not yet model contributions, withdrawals, or trades mid-period.
- Long-only. Short positions are rejected rather than mis-priced.
- Dividends are handled via yfinance's `auto_adjust`, so returns are total
  returns, not price returns.

## License

MIT — see [LICENSE](LICENSE).
