# Product invariants (user-approved; change control applies)

- INV-1 Markets: China A-shares (SSE, SZSE, BSE) and U.S.-listed common equities
  plus eligible ADRs. Hong Kong out of scope for V1.
- INV-2 Horizon ~3-18 months. No intraday or high-frequency trading.
- INV-3 Lane A: major negative events or price dislocations where the market may
  have priced materially more permanent damage than conservative evidence
  supports. A large price decline alone is never evidence.
- INV-4 Lane B: verified positive step-changes in small/mid caps, discovered
  BEFORE final delivery or full revenue recognition.
- INV-5 Evidence maturity L0-L4; L2 is eligible for serious multi-model analysis
  with distinct confidence labels and haircuts.
- INV-6 Daily post-close scans plus event-triggered ingestion. Zero
  opportunities is a valid result and must explain the full funnel.
- INV-7 Free/official-first data. Aggregators may discover events but cannot
  become final evidence without primary verification. Missing/stale/conflicting
  data remain explicit quality states. No paid or trial-only dependency without
  explicit approval.
- INV-8 Deterministic/cheap methods for broad screening; frontier models only
  for shortlisted candidates; >=2 blind analyses from distinct Agent contexts
  plus adjudication. The two Agents may use the same provider and model;
  provider/model diversity is disclosed but is not an independence gate.
  Agreement is not proof; material claims need exact source grounding and
  deterministic recomputation.
- INV-9 Single-user V1. Chinese primary output. No trading, no broker write
  access, no automatic position sizing, no "definitely unpriced" claims.
  Conditional valuation/observation zones allowed.
- INV-10 Freeze and version every published thesis; track outcomes
  prospectively 6-12 months; no lookahead, survivorship bias, retroactive
  threshold tuning, or deletion of failed candidates.

Change control: any code change touching gate logic must state which invariants
it touches. Scope changes require an explicit PROPOSED PRODUCT-SCOPE CHANGE
approved by the user.
