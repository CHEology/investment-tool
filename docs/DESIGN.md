# DESIGN — binding baseline (approved 2026-08-28)

This file condenses the approved FINAL PRE-IMPLEMENTATION DESIGN. Where this
file and the approval conversation differ, the conversation's approved text
governs. Invariants: docs/INVARIANTS.md. Thresholds: config/thresholds/
(all EXPERIMENTAL until forward-validated).

## Architecture

```
 [PRICE_FIRST]        [EVENT_FIRST]         [SECTOR_EVENT]      [standing queries]
 EOD spines,          CNInfo/EDGAR/          policy feeds,       watchlist+sector
 peer-adj AR          procurement/           exposure links      scoped, L0 only
 triggers+shadow      certification
      \                    |                     |                   /
       observations -> EVENT objects (HYPOTHESIZED -> VERIFIED/REFUTED/STALE)
            | cause-hunt -> SearchPlan (Search Agent, bounded, web)
            v
  screening: integrity gates -> liquidity -> lane logic -> candidates (profiles,
  lexicographic ranking, full rejection log)
            v
  Evidence Builder -> frozen content-addressed EvidenceBundle vN
            v
  Constructive <-blind-> Adversarial  (bundle-only, isolated, never web)
            -> rebuttal (1 round) -> Adjudicator (claims+rebuttals only)
            -> RESEARCH_REQUEST loop (<=2 cycles)
            v
  claim pipeline: locator check -> entailment -> deterministic recompute
  UNSUPPORTED material claim => DOSSIER_BLOCKED
            v
  zh dossier -> frozen ThesisVersion -> monitoring watches -> forward validation
```

Every stage emits manifests, quality states, config version, and audit lines.

## Timestamps (approved condition 1)

Distinct where applicable: `published_at_utc` (source publication),
`source_updated_at_utc` (source revision), `first_seen_at_utc` (our first
detection; detection latency = first_seen - published; lookahead protection
keys on first_seen), `retrieved_at_utc` (this fetch, in manifests).

## Lane A (S1)

- t0 = first tradable session at-or-after disclosure timestamp.
- Primary AR: r_i - median(r, PIT sector x size cell). Triggering gates on the
  peer-adjusted CAR ONLY; the market-model CAR (beta 250d, min 120, winsorized,
  size-segmented benchmark) is a post-trigger diagnostic on cards. (S1
  clarification of the earlier "gate on the more conservative" wording.)
- Return basis: analytical returns are basis-labeled per row (EXCHANGE_PCT /
  QFQ_CONSEC / SYNTH_COMPOUND / RAW_CONSEC) with a per-listing basis_epoch;
  windows mixing raw with adjusted lineage, or mixing epochs, are BLOCKED from
  analytics. Corporate-action rewrites are detected by overlap checks and bump
  the epoch. Raw prices remain canonical where sourced (docs/audit/
  s1-adjudication.md issue 2).
- PIT peers: industry snapshot per scan date; float-mcap terciles monthly;
  min cell 8 -> broader-industry -> size-only -> market; suspended excluded;
  limit-locked included with LIMIT_CONTAMINATED flag >20%.
- Limit-locked closes pause the CAR window clock (cap +15 sessions);
  discovery_state=INCOMPLETE until first free-trade close. Board limits:
  MAIN 10%, ChiNext/STAR 20%, BSE 30%, ST 5%.
- Trigger v0: peer-adj CAR[0,+3] <= -10% AND <= -3 sigma residual, OR any
  VERIFIED negative event of listed types. Near-miss shadow log at 70%.
- Attribution mandatory: no verified cause after SearchPlan exhaustion =>
  REJECTED_NO_ATTRIBUTION (terminal, audited).
- Damage templates v0: (1) market-access/regulatory exposure, (2) earnings
  decomposition. Others reject explicitly (DAMAGE_MODEL_UNAVAILABLE).
  Output [low, high] post-tax PV with sourced parameters.
- EV<->equity reconciliation: damage is enterprise-cash-flow level; comparison
  to |peer-adj dMcap| stated under debt-unimpaired assumption (survival floor
  checks it); repricing-wedge attribution acknowledged in profiles.
- Classification PRICED_LESS | WITHIN_BRACKET | EXCESS(ratio); research
  admission: EXCESS with ratio >= 1.5 (EXPERIMENTAL).
- Survival floor (hard) + deterioration overlay (soft, never netted).
- Replay gates run under FROZEN rules; if the frozen methodology does not
  produce the expected classification, STOP AND REPORT THE DISCREPANCY —
  thresholds are never tuned to force a fixture to pass (approved condition 2).

## Lane B (S3) — summary

L0 OBSERVE -> L1 WATCH (issuer primary) -> L2 EARLY_CANDIDATE (independent
confirmation; official procurement award qualifies; issuer 中标提示 alone does
not) -> L3 CONFIRMED (binding commercial) -> L4 CONFIRMATION (never a
discovery precondition). Pre-revenue admissible. Materiality OR-gates
G1..G5 (see thresholds). L2 enters multi-model research on the scaled track
(INV-5). Downgrades are evidence-driven resets, not one-level steps.

## Evidence model

7 categorical dims (authority, independence, directness, specificity,
bindingness, reproducibility, freshness) used as RULE INPUTS, never summed.
Contradiction is a blocking STATE {UNCONTESTED, CONTESTED, SUPERSEDED,
WITHDRAWN}. An issuer filing verifies that management said X — never
independent confirmation of X (L2 requires independence >= independent third
party). Retention classes: OFFICIAL_FULL, MEDIA_EXCERPT, LINK_ONLY.

## Providers

| dataset | chain | role |
|---|---|---|
| A-share EOD/universe | tushare -> eastmoney (PROVISIONAL) | SCAN |
| A benchmarks | tushare -> eastmoney klines | SCAN |
| A announcements | cninfo (szse/sse/bj) -> exchange sites | EVIDENCE |
| US EOD scan | yfinance (PROVISIONAL, D4) | SCAN |
| US filings | edgar (SEC_USER_AGENT required, D3) | EVIDENCE |
| identifiers | cninfo mapping, edgar tickers, nasdaq dirs | REFERENCE |
| verification | eodhd (Keychain, ~20 calls/day) | VERIFY |
| discovery | gdelt(http, integrity caveat) + news RSS | DISCOVERY |
| FX | frankfurter (ECB) | SCAN |

PROVISIONAL taint propagates as verification debt; publication requires the
debt cleared by an evidence/verify-grade source.

## Runtime & integration (approved condition 3)

The system is THIS Python application + CLI. It owns all logic, prompts,
numbers, and durable state (gitignored data/). Codex Skills and Claude Code
may invoke the local CLI. ChatGPT/Deep Research integration uses exported
evidence bundles or a separately approved API/MCP layer — never direct DB
access. CI runs tests only, never production. Scheduler: launchd (S5).

## Modes & cost

C0 no paid LLM (current) -> C1 one provider (bring-up) -> C2 two providers
(standard) -> C3 three+. Caps: $100/month, $15/dossier (config, enforced
pre-call). Paid calls require explicit user approval (D1). Dossiers disclose
role/context/model/provider counts and any reduced independence.

## Slices

S0 foundations -> S1 Lane A A-share vertical -> S2 US joins Lane A (+EODHD
port; D3, D4, archive mv) -> S3 Lane B -> S4 research protocol (D1) ->
S5 monitoring/validation soak. Dashboards/VPS deferred. Frozen cards + PIT
snapshots ship in S1 so the forward-validation clock starts at first
publication.
