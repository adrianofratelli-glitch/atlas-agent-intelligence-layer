# Changelog

## Unreleased

- Long-term memory retrieval is one server-side `$rankFusion` (vector + Portuguese BM25 with fuzzy), tenant-filtered on both legs, with live config in `ai_brain.memory_retrieval_config` and a fallback cascade (app RRF in parallel → one leg → newest). recall@5 1.0, MRR 0.859 → 0.918, p50 1,267 → 740 ms on `eval/memory_recall.json`.
- Inspector: "why these facts were recalled" from `scoreDetails` (rank, weight, contribution per leg), also stored in `agent_traces`.
- `$rerank` wired in and measured (pool 20/40/80, three models); off by default (ADR-009).
- Semantic cache invalidated by a Change Stream on `ai_brain.area_profiles` (resume token, backoff, reconcile on history lost); stats in `/api/health`, checked by the preflight.
- Queryable Encryption demo script for CPF/phone/income in a test-only collection (ADR-011).
- Fix: the full LIVE chaos suite wrote sessions and LangGraph checkpoints to the demo database (`db` was imported before the test-database switch); it now isolates at import and every LIVE scenario refuses a demo-bound module.
- `seed.py` updates an existing BM25 index (it used to only print "already exists"); requires `pov-shared` >= 0.3.0 for the new retrieval (the app still starts without it).

- Atualiza dependências compatíveis e pisos de segurança; alinha metadados de versão entre aplicação e frontend.

## 1.3.1 (2026-10-13)

- A price cap stated in the message ("até 300 reais", "no máximo R$ 1.200,00") becomes a server-side catalog pre-filter, like the budget stored in memory; the lower of the two wins. A number without a currency marker ("até 2 fones", "até 30 dias") is ignored.
- Latency: the identity node reads model config, area profile and tool registry in parallel (~1.3 s → ~0.8 s per turn), the cache lookup reads its config and the exact match in parallel, and the backend warms the Atlas connection pool at startup (first turn after a restart ~12 s → ~9 s; warm turns ~5 s).
- Chat answers render `**bold**` and clickable links (tabs 2 and 3) without `innerHTML`; a web call is labeled as a call, not as MCP.
- Semantic cache for web results measured and rejected: with `voyage-4`, different questions ("first" vs "second round", "today" vs "tomorrow") score higher than the weakest paraphrase of the same question, so no threshold is safe. Details in ADR-007.
- README screenshots of the agent tab recaptured with the current UI (web search call, cache hit).

## 1.3.0 (2026-10-13)

- The agent answers questions outside the store's domain through a `web_search` tool (Tavily), dispatched server-side outside MCP and governed per area by `ai_brain.tool_registry` (fails closed without the document or the key; Finance is off in the seed). The LLM gateway rejects the provider's native web search tools (400), hence our own tool.
- Taint: after a web search, every write in the same turn is denied on the server; the agent asks the customer to confirm in a new message. Web results reach the model between `<resultados_web_nao_confiaveis>` delimiters.
- Monthly quota (`ai_brain.web_search_quota`, atomic conditional `$inc`, 900 < the 1,000 free credits), result cache with TTL (`POC.web_search_cache`) and audit (`POC.external_calls`, 30-day TTL). `seed_tool_registry.py` seeds only this, without resetting the demo.
- Current date, time and timezone in the system prompt (tabs 2 and 3); answers that quote today's date are not written to the shared cache. Day counts come from a pure `date_diff` tool.
- Semantic cache: a question that quotes an order (`PED-…`) no longer reads the cache (a FAQ used to swallow a refund request), and each entry records `web_enabled`, so an answer written without web search is not served to a turn that has it.
- Behavior battery: 38 live turns across tabs 2 and 3; 4 failures found and fixed, 1 source-quality limit documented in ADR-007.

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
