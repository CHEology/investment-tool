"""Command line entry point: `invest <command>`."""

from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from investment_tool import data as data_mod
from investment_tool.portfolio import Portfolio, performance_summary


def _add_range_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", help="start date, YYYY-MM-DD (default: 3 years ago)")
    parser.add_argument("--end", help="end date, YYYY-MM-DD (default: today)")
    parser.add_argument("--no-cache", action="store_true", help="bypass the on-disk price cache")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invest", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prices = sub.add_parser("prices", help="fetch and print adjusted close prices")
    prices.add_argument("tickers", nargs="+")
    _add_range_args(prices)
    prices.add_argument("--tail", type=int, default=10, help="rows to display (default: 10)")

    report = sub.add_parser("report", help="performance summary for a holdings file")
    report.add_argument("holdings", help="path to a holdings JSON file")
    _add_range_args(report)
    report.add_argument("--json", action="store_true", help="emit JSON instead of a table")

    return parser


def cmd_prices(args: argparse.Namespace) -> int:
    frame = data_mod.fetch_history(
        args.tickers, start=args.start, end=args.end, use_cache=not args.no_cache
    )
    print(frame.tail(args.tail).to_string(float_format=lambda v: f"{v:,.2f}"))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    portfolio = Portfolio.from_json(args.holdings)
    prices = data_mod.fetch_history(
        portfolio.tickers, start=args.start, end=args.end, use_cache=not args.no_cache
    )
    equity = portfolio.equity_curve(prices).dropna()
    summary = performance_summary(equity)
    weights = portfolio.weights(prices.iloc[-1])

    if args.json:
        print(json.dumps({"summary": summary, "weights": weights.to_dict()}, indent=2))
        return 0

    print(f"{portfolio.name}  ({equity.index[0].date()} to {equity.index[-1].date()})\n")
    print("Allocation")
    for ticker, weight in weights.items():
        print(f"  {ticker:<8} {weight:>7.2%}")
    print("\nPerformance")
    print(f"  Value        {summary['end_value']:>12,.2f}")
    print(f"  Total return {summary['total_return']:>12.2%}")
    print(f"  CAGR         {summary['cagr']:>12.2%}")
    print(f"  Volatility   {summary['volatility']:>12.2%}")
    print(f"  Sharpe       {summary['sharpe']:>12.2f}")
    print(f"  Max drawdown {summary['max_drawdown']:>12.2%}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"prices": cmd_prices, "report": cmd_report}
    try:
        return handlers[args.command](args)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    pd.set_option("display.max_rows", 200)
    raise SystemExit(main())
