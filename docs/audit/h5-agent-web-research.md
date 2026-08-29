# H5 Agent web-research and blind-pair gate — 2026-08-29

## Result

- Codex CLI `0.150.0-alpha.8` launched a fresh ephemeral Agent and performed
  real Web Search against the official OpenAI Web Search guide.
- Probe session `01a04eb2-e233-7893-8fc2-2013437b5c87` also executed an HTTPS
  shell request inside the same `workspace-write` sandbox and received HTTP
  200, proving that hosted search and the local evidence-fetch subprocess are
  both reachable with the production adapter settings.
- The isolated live integration gate passed:
  `RUN_CODEX_LIVE=1 pytest
  tests/test_agent_runner.py::test_codex_live_web_search_capture_and_import`
  completed again in 29.60 seconds. The Agent searched, captured a public page through
  `invest research fetch`, read the stored text, returned its `evd_*` ID and an
  exact quote, and the normal importer accepted the claim.
- Offline suite: 215 passed, 1 explicit live-only test skipped; ruff clean.

## Independence semantics

Logical independence is two distinct Agent contexts over two separately
snapshotted blind work orders tied to one frozen bundle. Provider and model may
be identical. Both work orders exist before the first analyst output. A missing
or reused context ID, changed input snapshot, stale bundle, or incomplete pair
prevents adjudication. Provider/model counts remain disclosure metadata only.

## Search boundary

The Search Agent may follow any public source or newly discovered entity; its
coverage list is not a whitelist. Search results and snippets are discovery
leads. A claim becomes eligible only after its URL is fetched by the evidence
gateway, stored with manifest/hash/timestamps, cited by local evidence ID and
exact quote, and accepted by the existing temporal and claim validators.

Official capability references:

- <https://developers.openai.com/api/docs/guides/tools-web-search>
- <https://developers.openai.com/codex/config-reference>
