# Changelog

## 1.2.0 (2026-10-08)

- Model swap to Sonnet 5.5 (and the Sonnet 5.5 fallback) no longer fails: a per-model capability table drops `temperature`/`top_p` for models that reject them; one retry without sampling if a new model returns that 400. Quick-chat degrades with a message instead of HTTP 500, and the UI maps HTTP status to readable text.
- Anti-dilution moved to pov-shared 0.2.0: every intent is scored (no regrouping), more than 32 intents blocks, an all-NaN score marks the layer unavailable, partial scoring blocks fail-closed areas. A forbidden request with 8 or 12 benign intents appended is now blocked. Two-word fragments are no longer scored alone (false positive fixed).
- Retrieved memory that reads like an instruction is quarantined before the prompt, not only at extraction; format orders ("write exactly X at the start") count as instructions.
- Tenant isolation: every tenant-scoped `$vectorSearch` goes through `db.tenant_vector_stage`, which refuses an empty tenant key; the thesis no longer claims the index makes a missing filter impossible.
- `seed.py` creates each collection before its search index and exits non-zero if a required index is missing (first reset on an empty database used to exit 0 without `turn_probes_vs`). `scripts/preflight.sh` is executable in git.
- Live adversarial probe tolerates the benign MCP teardown race only after every check ran. Visual baseline of the model tab refreshed.

## 1.1.0 (2026-10-06)

- Semantic guardrail scores the whole message and each intent (same threshold): diluted forbidden requests 1/6 → 6/6 blocked, 0/8 false positives on composite questions (`backend/scripts/measure_dilution.py`).
- Fraud denylist phrase reworded by intent: legitimate "my order has not arrived" complaints are no longer blocked.
- Identity switch race fixed (late login no longer answers as the previous user); token/payload mismatch returns 409.
- LLM calls fail closed without the gateway (no direct-provider fallback).
- `seed.py` is the single full reset and refuses the demo databases without `ALLOW_DEMO_DB_WRITE=1`; LangGraph checkpoints expire with the session; audit TTL repaired.
- Quick-chat history and stored memory facts hardened against injection; adversarial and E2E suites added.
- UI: layout layout MongoDB 2026 "Dark Stage v4" (tokens mais escuros, Special Gothic / Source Code Pro locais, motivos de escada e grade, movimento escalonado).

## 1.0.0 (2026-09-30)

First public release.

- Repository rebuilt with a clean, single-commit history.
- English README and repository description, with screenshots captured against a real Atlas cluster.
- MIT license.
- Internal notes, presentation decks, test-output snapshots, and tooling configuration removed from the repository.
