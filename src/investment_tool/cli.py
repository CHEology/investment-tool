"""`invest` CLI — the single operator surface. Agent environments call this;
they never touch the database directly (approved integration boundary)."""

from __future__ import annotations

import argparse
import sys

from investment_tool import config as config_mod
from investment_tool import entities
from investment_tool.db import connect
from investment_tool.lineage import record_fetch
from investment_tool.quality import Quality, QualityState


def _cfg(conn):
    cfg = config_mod.load("v0")
    config_mod.register(conn, cfg, changelog="initial v0 thresholds (all EXPERIMENTAL)")
    return cfg


def cmd_seed(args) -> int:
    conn = connect()
    cfg = _cfg(conn)
    market = args.market

    if market in ("a", "all"):
        from investment_tool.providers import cninfo

        http = cninfo.client()
        payload, status = cninfo.fetch_security_mapping(http)
        state = QualityState.OK if status == 200 else QualityState.ERROR
        quality = Quality(state, f"http={status}")
        m = record_fetch(
            conn, provider="cninfo", dataset="security_mapping", params={},
            source_url=cninfo.MAPPING_URL, payload=payload, http_status=status,
            quality=quality, config_version=cfg.id,
        )
        if not quality.usable_for_scan:
            print(f"cninfo mapping fetch failed: http={status} (manifest {m.manifest_id})")
            return 1
        rows = cninfo.parse_security_mapping(payload)
        n = entities.seed_a_share(conn, rows)
        by_ex = {}
        for r in rows:
            by_ex[r["exchange"]] = by_ex.get(r["exchange"], 0) + 1
        print(f"A-share seed: {n} listings {by_ex} (manifest {m.manifest_id})")

    if market in ("us", "all"):
        from investment_tool.providers import nasdaq
        from investment_tool.providers.base import GENERIC_UA, HttpClient

        http = HttpClient(user_agent=GENERIC_UA, min_interval_s=1.0)
        total = 0
        for url, dataset, parser in (
            (nasdaq.NASDAQ_LISTED_URL, "nasdaqlisted", nasdaq.parse_nasdaq_listed),
            (nasdaq.OTHER_LISTED_URL, "otherlisted", nasdaq.parse_other_listed),
        ):
            resp = http.get(url)
            quality = Quality(
                QualityState.OK if resp.status_code == 200 else QualityState.ERROR,
                f"http={resp.status_code}",
            )
            m = record_fetch(
                conn, provider="nasdaq_trader", dataset=dataset, params={},
                source_url=url, payload=resp.content, http_status=resp.status_code,
                quality=quality, config_version=cfg.id,
            )
            if not quality.usable_for_scan:
                print(f"{dataset} fetch failed: http={resp.status_code}")
                return 1
            rows = parser(resp.content.decode("utf-8", errors="replace"))
            total += entities.seed_us(conn, rows)
            print(f"US seed [{dataset}]: {len(rows)} rows (manifest {m.manifest_id})")
        print(f"US seed total listings inserted-or-present: {total}")
        print("note: CIK enrichment deferred to S2 (requires SEC_USER_AGENT, decision D3)")

    return 0


def cmd_resolve(args) -> int:
    conn = connect()
    rows = entities.resolve(conn, args.ticker)
    if not rows:
        print(f"no listing found for {args.ticker!r}")
        return 1
    for r in rows:
        name = r["name_zh"] or r["name_en"] or ""
        print(
            f"{r['listing_id']}  company={r['company_id']}  {name}  board={r['board'] or '-'}"
            f"  ccy={r['currency']}  adr={r['is_adr']}  cik={r['cik'] or 'pending-D3'}"
        )
    return 0


def cmd_fx(args) -> int:
    conn = connect()
    cfg = _cfg(conn)
    from investment_tool.providers import frankfurter as fx

    http = fx.client()
    payload, status, url = fx.fetch_rate(http, args.date)
    quality = Quality(QualityState.OK if status == 200 else QualityState.ERROR, f"http={status}")
    m = record_fetch(
        conn, provider="frankfurter", dataset="usd_cny", params={"date": args.date or "latest"},
        source_url=url, payload=payload, http_status=status, quality=quality,
        config_version=cfg.id,
    )
    if not quality.usable_for_scan:
        print(f"fx fetch failed http={status}")
        return 1
    date, rate = fx.parse_rate(payload)
    conn.execute(
        "INSERT OR REPLACE INTO fx_day(pair, date, rate, source, manifest_id) VALUES(?,?,?,?,?)",
        ("USDCNY", date, rate, "frankfurter_ecb", m.manifest_id),
    )
    conn.commit()
    print(f"USDCNY {date} = {rate} (manifest {m.manifest_id})")
    return 0


def cmd_status(args) -> int:
    conn = connect()
    for table in ("company", "listing", "manifest", "security_day", "announcement",
                  "event", "candidate", "frozen_artifact"):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]  # noqa: S608
        print(f"{table:16s} {n}")
    q = conn.execute(
        "SELECT quality_state, COUNT(*) AS n FROM manifest GROUP BY quality_state"
    ).fetchall()
    if q:
        print("manifest quality:", {r["quality_state"]: r["n"] for r in q})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="invest", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("seed", help="seed the security universe from official identifier sources")
    s.add_argument("--market", choices=["a", "us", "all"], default="all")
    s.set_defaults(func=cmd_seed)

    r = sub.add_parser("resolve", help="resolve a ticker/code to company and listings")
    r.add_argument("ticker")
    r.set_defaults(func=cmd_resolve)

    f = sub.add_parser("fx", help="fetch and store the ECB USD/CNY reference rate")
    f.add_argument("--date", default=None, help="YYYY-MM-DD (default: latest)")
    f.set_defaults(func=cmd_fx)

    st = sub.add_parser("status", help="table counts and manifest quality summary")
    st.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
