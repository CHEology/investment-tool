"""Trading calendars, derived from observed bars and provider calendars.

S0 ships the storage and derivation utility; population happens with the
first price-spine ingestion (S1). Missing calendar coverage is an explicit
condition callers must handle, never an implicit weekday assumption.
"""

from __future__ import annotations

import sqlite3


def mark_trading_days(
    conn: sqlite3.Connection, exchange: str, dates: list[str], source: str
) -> int:
    rows = [(exchange, d, 1, source) for d in dates]
    conn.executemany(
        "INSERT OR REPLACE INTO calendar_day(exchange, date, is_trading, source) VALUES(?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def trading_days(conn: sqlite3.Connection, exchange: str, start: str, end: str) -> list[str]:
    cur = conn.execute(
        "SELECT date FROM calendar_day WHERE exchange=? AND is_trading=1 AND date>=? AND date<=?"
        " ORDER BY date",
        (exchange, start, end),
    )
    return [r["date"] for r in cur.fetchall()]
