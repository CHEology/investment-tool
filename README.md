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

## S0 commands

```bash
invest seed --market all   # seed universe from official identifier sources
invest resolve 300274      # A-share resolution
invest resolve AAPL        # US resolution
invest fx                  # ECB USD/CNY reference rate
invest status              # table counts + manifest quality summary
```

Secrets live in macOS Keychain or `.env` (gitignored). The public repository
contains code, schemas, config, and synthetic fixtures only — never market
data, credentials, or generated research.
