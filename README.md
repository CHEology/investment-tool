# investment-tool

Auditable, free-first investment opportunity discovery and research system for
China A-shares (SSE, SZSE, BSE) and U.S.-listed equities.

Two opportunity lanes: negative-shock overreaction (Lane A) and verified
positive step-changes in small/mid caps (Lane B), on a 3-18 month horizon.
Deterministic screening first; multi-model research only for shortlisted
candidates; every fetch leaves a manifest with SHA-256 lineage and an explicit
quality state; every published card/thesis is frozen, versioned, and tracked
forward. Zero opportunities is a valid daily result.

> Research tooling only. No trading, no broker connectivity, no position
> sizing, and no investment advice. See docs/INVARIANTS.md.

Design baseline: docs/DESIGN.md (binding). Invariants: docs/INVARIANTS.md.

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

## Operator commands

```bash
invest seed --market all   # seed universe from official identifier sources
invest resolve 300274      # A-share resolution
invest resolve AAPL        # US resolution
invest fx                  # ECB USD/CNY reference rate
invest status              # table counts + manifest quality summary
```

Lane A currently uses a free-first, provisional market-data spine. Run the
daily stages in this order (replace the date with the A-share trading date):

```bash
invest ingest-eod --date 2026-08-28
invest ingest-benchmarks
invest backfill --skip-existing       # resumable; exits non-zero after a breaker abort
invest scan --date 2026-08-28
```

`ingest-eod` stores the current Eastmoney snapshot, benchmark history comes
from Eastmoney, SSE/SZSE adjusted history comes from Tencent qfq, BSE raw
history comes from Sina, and candidate-driven announcements come from CNInfo.
All four are manifested; scan-tier price data remains visibly
`PROVISIONAL`. `--skip-existing` only skips listings that already satisfy the
configured 180-day history gate, so a partial history is retried.

Historical scans are point-in-time bounded: they ignore trigger observations
and announcements first seen after the requested scan date, and only use
market snapshots available by that date. Consequently, a newly fetched old
announcement may be useful for present research but cannot silently change an
older replay result.

Current limitations: the free providers may throttle or change response
shape; historical market-snapshot coverage is sparse; BSE returns are raw and
therefore carry corporate-action risk; and search plans are C0 human-runnable,
not autonomous web-research execution. A zero-candidate result describes the
covered sample, not the entire market when history coverage is incomplete.

Secrets live in macOS Keychain or `.env` (gitignored). The public repository
contains code, schemas, config, and synthetic fixtures only — never market
data, credentials, or generated research.
