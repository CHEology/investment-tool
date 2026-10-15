import argparse
import json
import sys
from pathlib import Path

from .config import ResearchConfig, load_config
from .data import import_csv, load_snapshot
from .demo import make_demo
from .providers import fetch_yahoo


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Qlib equity research assistant")
    parser.add_argument("--root", type=Path, default=Path("artifacts"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("cn-data", help="Download the pinned Qlib historical China dataset")
    cn = sub.add_parser("cn-benchmark", help="Run the official CSI300 Alpha158/LightGBM workflow")
    cn.add_argument("--threads", type=int, default=20)
    sub.add_parser("demo", help="Run real Qlib on deterministic synthetic prices")
    fetch = sub.add_parser("fetch", help="Fetch a static US research sample, including SPY")
    fetch.add_argument("--symbols", required=True, help="Comma-separated Yahoo symbols")
    fetch.add_argument("--start", required=True)
    fetch.add_argument("--end", required=True)
    ingest = sub.add_parser("import-csv", help="Import canonical US prices with raw evidence")
    ingest.add_argument("path", type=Path)
    validate = sub.add_parser("validate", help="Verify snapshot hashes and data quality")
    validate.add_argument("snapshot", type=Path)
    run = sub.add_parser("research", help="Run purged walk-forward training and simulation")
    run.add_argument("snapshot", type=Path)
    run.add_argument("--config", type=Path)
    observe = sub.add_parser("observe", help="Record today's research shortlist locally")
    observe.add_argument("run", type=Path)
    daily = sub.add_parser("daily", help="Prepare one completed session for Codex research")
    daily.add_argument("--config", type=Path, default=Path("config/us-daily.toml"))
    daily.add_argument("--snapshot", type=Path)
    daily.add_argument("--holdings", default="", help="Additional US symbols from broker holdings")
    brief = sub.add_parser("brief", help="Render a researched daily report")
    brief.add_argument("day", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "cn-data":
            from .cn import prepare_data

            path = prepare_data(args.root)
        elif args.command == "cn-benchmark":
            from .cn import benchmark

            path = benchmark(args.root, args.threads)
        elif args.command == "fetch":
            path = fetch_yahoo(
                [s.strip().upper() for s in args.symbols.split(",")],
                args.start,
                args.end,
                args.root,
            )
        elif args.command == "import-csv":
            path = import_csv(args.path, args.root)
        elif args.command == "validate":
            _, meta = load_snapshot(args.snapshot)
            print(
                json.dumps(
                    {
                        key: meta[key]
                        for key in ("status", "start", "end", "rows", "symbols", "warnings")
                    },
                    indent=2,
                )
            )
            return 0
        elif args.command == "daily":
            from .daily import prepare_daily

            path = prepare_daily(
                args.root,
                args.config,
                args.snapshot,
                [s.strip().upper() for s in args.holdings.split(",") if s.strip()],
            )
        elif args.command == "brief":
            from .brief import render_brief

            path = render_brief(args.day)
        elif args.command == "observe":
            from .watch import observe

            path = observe(args.run, args.root)
        else:
            from .pipeline import research

            if args.command == "demo":
                snapshot = make_demo(args.root)
                config = ResearchConfig(
                    train_sessions=252,
                    valid_sessions=63,
                    top_k=4,
                    max_weight=0.25,
                    num_boost_round=40,
                )
            else:
                snapshot = args.snapshot
                config = load_config(args.config)
            path = research(snapshot, args.root, config)
        print(path.resolve())
        return 0
    except (ValueError, OSError, ImportError, KeyError) as error:
        print(f"Blocked: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
