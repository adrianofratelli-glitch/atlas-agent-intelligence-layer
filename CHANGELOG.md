# Changelog

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
