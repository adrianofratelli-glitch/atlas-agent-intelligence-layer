# Mapa do código — atlas-agent-intelligence-layer

> Também conhecida como: **Single Agent** (pasta antiga `singleagent`). Portas: frontend 5183 / backend 8010.
> Em uma frase: mostra o MongoDB Atlas como camada de dados **e** de orquestração de um agente de suporte, com prompt, modelo, cache semântico, memória e guardrails guardados como documentos.

Para o contexto geral, veja `docs/briefing/architecture.md`, `docs/briefing/queries.md` e `docs/briefing/ui-flows.md`. Este mapa é o atalho "onde fica o quê" e não repete o briefing.

## 1. Árvore anotada

```
backend/                      API FastAPI + o agente (Python)
  main.py                     Todas as rotas /api/*, auth, rate limit, supervisor das sessões MCP, lifespan
  agent.py                    Loop de tool-use com o LLM, allowlist/reescrita de ferramentas MCP, AREA_SCENARIOS (chips), DEMO_PLAYLIST
  agent_graph.py              O turno do agente como grafo LangGraph (nós identity -> guard -> cache -> pipeline -> finalize), checkpoint no Mongo
  policy_guardrails.py        Guardrail de entrada/saída: PII, termos banidos, denylist semântico, auditoria, fila de near-miss
  cache.py                    Cache semântico por área ($vectorSearch) + fallback exato
  cache_invalidation.py       Worker de change stream que apaga o cache de uma área quando a persona muda
  memory.py                   Memória de longo prazo: extração de fatos, retrieval híbrido ($rankFusion), supersessão em transação
  memory_backends.py          Adaptadores Atlas x Mem0 (só para o benchmark de memória)
  graph.py                    Pipeline $graphLookup da cadeia de trocas de pedido
  turn_classifier.py          Decide se o turno é "pessoal" (usa memória/não entra no cache) via $vectorSearch em turn_probes
  search_readiness.py         list_search_indexes: busca vazia só vale com índice consultável (ADR-013)
  answer_checks.py            Fidelidade da resposta: ação afirmada sem escrita, prazo sem fonte (não bloqueia)
  web_search.py               Ferramenta externa web_search (Tavily): cota, cache e auditoria no Atlas
  guidance.py                 Textos de orientação quando a busca volta vazia, escrita é negada ou o assunto é fora de escopo
  profiles.py                 Usuário -> área; perfis de área (persona) em ai_brain.area_profiles
  llm.py                      Lê ai_brain.model_config a cada chamada e chama o modelo (com fallback)
  gateway.py                  Cliente do gateway de LLM (Grove), catálogo de modelos, custo estimado
  db.py                       Conexão Atlas (AsyncMongoClient), tenant_vector_stage (filtro de tenant obrigatório), safe_query
  auth.py                     Emite/valida o JWT da identidade demo (a área vem da claim, nunca do payload)
  resilience.py               Timeout/retry/circuit breaker em volta das chamadas MCP e Atlas
  observability.py            Métricas em memória e spans; liga o tracing do pov-shared
  langfuse_tracing.py         Trace do turno no Langfuse (fail-open)
  seed.py                     Reset único: dados, denylist, cache FAQ, perfis, índices (regulares, TTL, vetoriais, BM25)
  seed_tool_registry.py       Registro de ferramentas externas (web_search) por área
  seed_turn_probes.py         Frases-sonda do classificador de turno pessoal
  calibrate_thresholds.py     Mede os thresholds de cache/guardrail contra probes rotulados
  eval_agent.py, eval_report.py   Eval do agente (ver eval/)
  chaos.py                    Injeção de falhas (CHAOS=1) para os testes de resiliência
  scripts/                    Ferramentas avulsas: preflight, load_test, chaos_suite, memory_recall_eval, qe_sensitive_profile (Queryable Encryption), measure_dilution...
  tests/                      Testes unittest do backend
frontend/                     React 18 + Vite + LeafyGreen (JS puro)
  src/App.jsx                 Abas e estado global das três telas (o estado fica aqui, não nas abas)
  src/api.js                  Todas as chamadas HTTP (objeto `api`), token, stream SSE
  src/tabs/FlexibleSchema.jsx Aba 1: schema flexível
  src/tabs/ModelSwap.jsx      Aba 2: troca de modelo e custo
  src/tabs/Agent.jsx          Aba 3: agente (a maior tela), inspetor MongoDB, demo automática
  src/components/             JsonViewer, QueryDetails, ReplacementChain (cadeia de trocas), MemoryWhy, RichText, ExecutionSummary
  tests/*.test.mjs            Testes de JS puro (stream, deadline, memória, rich text)
  vite.config.js              Porta 5183 e proxy /api -> backend (BACKEND_PORT, padrão 8010)
eval/                         dataset.json (casos), memory_recall.json, FORMAT.md, reports/ (resultados datados)
tests/e2e/demo-flow.spec.js   Playwright: roteiro da demo de ponta a ponta (na raiz)
tests/visual/tabs.spec.js     Playwright: regressão visual das 3 abas (na raiz)
playwright.config.js, playwright.e2e.config.js   Configs dos dois conjuntos Playwright
scripts/                      bootstrap-venvs.sh (cria .venv e instala o pov-shared), preflight.sh
start.sh                      Sobe backend (8010) + frontend (5183)
Dockerfile, docker/, nginx.conf   Empacotamento
docs/                         adr/ (decisões), briefing/, eval-report.md, chaos-report.md, memoria-agentica-mongodb.md
```

Observação: o MCP Server do MongoDB (`mongodb-mcp-server`, versão fixada em `backend/agent.py:942`) não está neste repositório. É lançado via `npx` por `agent.py::mcp_server_params` (linha 945) e é por ele que o agente consulta os pedidos.

## 2. "Apertei o botão X, o que roda?"

Todas as chamadas passam por `frontend/src/api.js` (objeto `api`, linha 79). A identidade (token) é obtida em `POST /api/auth/token`.

| Tela / ação | Componente | Chamada HTTP | Handler | Função de negócio | MongoDB |
|---|---|---|---|---|---|
| Abrir o app (status) | `App.jsx:34` (useEffect) | `GET /api/health` | `main.py:530` `health` | contagem por coleção | `count_documents` em várias coleções (`main.py:538-540`) |
| Aba 1: carregar documento | `FlexibleSchema.jsx:176` | `GET /api/templates/{id}` | `main.py:569` `get_template` | leitura direta | `ai_brain.prompt_templates` `find_one` (`main.py:571`) |
| Aba 1: "Adicionar variante" / "Remover" | `FlexibleSchema.jsx:190` `toggleVariant` | `POST /api/templates/{id}/variant` / `DELETE /api/templates/{id}/variant/{model}` | `main.py:583` `add_variant`, `main.py:606` `remove_variant` | `$set`/`$unset` ao vivo | `ai_brain.prompt_templates` `update_one` (`main.py:593`, `main.py:613`) |
| Aba 1: corrida (SQL x Mongo) | `FlexibleSchema.jsx:95` `FlowRace` | nenhuma | nenhum (animação no navegador) | nenhuma | nenhum |
| Aba 2: escolher modelo | `ModelSwap.jsx:72` `pick` | `POST /api/model-config/primary` | `main.py:641` `set_primary_model` | troca o modelo primário | `ai_brain.model_config` `update_one` (`main.py:651`) + auditoria `admin_audit` (`main.py:401`) |
| Aba 2: lista de modelos | `ModelSwap.jsx:69` | `GET /api/models` | `main.py:634` `available_models` | `gateway.model_catalog()` | nenhum |
| Aba 2: "Perguntar" | `ModelSwap.jsx:87` `ask` | `POST /api/chat/quick` | `main.py:707` `quick_chat` | guardrail -> `cache.lookup` -> `llm.call_with_fallback` -> `cache.store` | ver `cache.py::lookup`, `llm.py::get_active_config`, `cache.py::store` (seção 3) |
| Aba 3: escolher usuário | `Agent.jsx:435` `switchUser` | `POST /api/auth/token`, `GET /api/agent/scenarios` | `main.py:827` `auth_token`, `main.py:853` `agent_scenarios` | `auth.issue_token`; chips por área em `agent.py:654` `AREA_SCENARIOS` | `profiles.py::get_user` (`app_users`) |
| Aba 3: lista de usuários / ferramentas / roteiro | `Agent.jsx:179-181` | `GET /api/users`, `/api/agent/tools`, `/api/agent/playlist` | `main.py:845`, `main.py:878`, `main.py:872` | `profiles.list_users`, `agent.list_agent_tools` (`agent.py:955`), `DEMO_PLAYLIST` (`agent.py:884`) | `app_users`, `area_profiles`, `tool_registry` |
| Aba 3: chip de pergunta, caixa de texto + enviar, "Consolidar" | `Agent.jsx:727` / `Agent.jsx:405` `send` -> `Agent.jsx:345` `runScenario` | `POST /api/agent/run/stream` (SSE; `api.js:agentRunStream`) | `main.py:999` `agent_run_stream` -> `run_agent_resilient` (`main.py:168`) | `agent.py:1469` `run_agent` -> `agent_graph.py:686` `run_turn` (detalhe abaixo) | vários, ver abaixo |
| Aba 3: "Demo automática", pausar, continuar, encerrar | `Agent.jsx:312`, `321`, `329`, `336` | mesma `/api/agent/run/stream`, um turno por passo de `DEMO_PLAYLIST` | idem | idem | idem |
| Aba 3: "Nova conversa" | `Agent.jsx:422` | nenhuma (novo `conversation_id` no navegador) | n/a | n/a | n/a |
| Aba 3: Reset / Anterior / Próximo / play (replay do trace) | `Agent.jsx:412`, `449`, `508-522` | nenhuma | n/a | só reexibe o trace já recebido | nenhum |
| Aba 3: inspetor, abas cache / curto prazo / memória / regras / eventos | `Agent.jsx:851` `MongoInspector` (`load` linha 857) | `GET /api/cache`, `/api/memory-short/{id}`, `/api/memory/{user}`, `/api/guardrails/rules`, `/api/guardrails/events` | `main.py:1077`, `1102`, `1094`, `1139`, `1180` | `cache.recent` (`cache.py:238`), `memory.load_longterm` (`memory.py:279`), `guardrails.get_policy` (`policy_guardrails.py:112`), `guardrails.recent_events` (`policy_guardrails.py:602`) | `semantic_cache`, `agent_sessions` (`main.py:1107`), `agent_memory`, `guardrail_denylist` (`main.py:1144`), `guardrail_events` |
| Aba 3: "Limpar cache" | `Agent.jsx:877` | `DELETE /api/cache` | `main.py:1086` `cache_clear` | `cache.clear` (`cache.py:250`) | `semantic_cache` `delete_many({})` |
| Aba 3: "Esquecer cliente" | `Agent.jsx:878` | `DELETE /api/memory/{user_key}` | `main.py:1123` `memory_clear` | direto no handler | `agent_memory` `delete_many({user_key})` (`main.py:1128`) |
| Fila de near-miss (sem botão na UI; só API) | `api.js` não expõe; uso por curl | `GET /api/guardrails/candidates`, `POST /api/guardrails/candidates/{id}/review` | `main.py:1191`, `main.py:1203` | `policy_guardrails.py::list_candidates` (542), `review_candidate` (564) | `guardrail_candidates` `find`/`update_one`; aprovado entra em `guardrail_denylist` `insert_one` (`policy_guardrails.py:591`) |
| Métricas (contador de tokens economizados) | `Agent.jsx:194`, `ModelSwap.jsx:49` | `GET /api/metrics` | `main.py:478` | `observability` | nenhum |

### O que roda dentro de um turno do agente (aba 3)

O turno é o grafo de `backend/agent_graph.py` (montado em `_build_graph`, linha 658). Cada nó e o que faz:

| Nó (linha) | O que faz | Onde toca o MongoDB |
|---|---|---|
| `n_identity` (134) | Resolve usuário e área, carrega config de web_search | `profiles.get_user`, `web_search.get_config` (`ai_brain.tool_registry`) |
| `n_guard_input` (156) | `guardrails.check_input` (policy_guardrails.py:372): heurística de injeção, máscara de PII, termos banidos, denylist semântico | `$vectorSearch` em `guardrail_denylist` (`policy_guardrails.py::_denylist_top`); grava `guardrail_events` / `guardrail_candidates` |
| `n_resume_check` (171) | Decide se bloqueia, trata fora de escopo ou segue | nenhum |
| `n_blocked` (204) / `n_scope` (222) | Respostas de bloqueio e de fora de escopo (`guidance.scope_reply`) | `support_orders` `find` por dono (`guidance.py:42`) |
| `n_cache_lookup` (246) | Classifica o turno (`turn_classifier.classify`) e consulta o cache semântico (`cache.lookup`) | `$vectorSearch` em `turn_probes` e em `semantic_cache` |
| `n_cache_finish` (306) | Se houve HIT, devolve a resposta sem LLM | `update_one` de contador de hits em `semantic_cache` (`cache.py:170`) |
| `n_run_pipeline` (378) | Memória longa + extração de fato (em paralelo), histórico, loop de ferramentas MCP, guardrail de saída, escrita no curto prazo | ver tabela da seção 3 (memória, `agent_sessions`) e o MCP abaixo |
| `n_cache_store` (573) | Grava a resposta no cache só se for genérica | `semantic_cache` `insert_one` (`cache.py:234`) |
| `n_finalize` (627) | Recarrega a memória para o trace e fecha o turno | `agent_memory` `find` (`memory.load_longterm`) |

O loop de ferramentas é `agent.py::_run_tool_loop` (linha 1086). O modelo só enxerga `find`, `aggregate` e `update-many` (allowlist em `agent.py:147-151`). Antes de cada chamada, `_read_denial` (448) e `_write_denial` (390) negam ou **reescrevem** o que o modelo pediu; depois a chamada vai ao MCP por `session.call_tool` (`agent.py:1237`) e a consulta contra `support_orders` / `produtos_vector` é executada pelo MCP Server, não pelo código Python da PoV. A cadeia de trocas usa o pipeline fixo de `graph.py:20` (`$graphLookup`), injetado em `agent.py:498`. A ferramenta externa `web_search` (`agent.py:1211`) roda `web_search.run` (`web_search.py:180`).

## 3. Onde estão as queries MongoDB

Os nomes de coleção abaixo estão sem o banco. `ai_brain` guarda a config viva (só humano escreve); `POC` guarda dados e telemetria (nome pode ser trocado por `MONGODB_DB`/`MONGODB_BRAIN_DB`). Detalhes de cada índice em `docs/briefing/queries.md`.

Correção ao briefing: lá, a seção 3.2 cita `backend/guardrails.py`. Esse arquivo não existe; o guardrail está em `backend/policy_guardrails.py` (o módulo `guardrails` importado no código é do pacote pov-shared, não deste repo).

| O que faz | arquivo::função (linha) | operador / pipeline principal | collection(s) |
|---|---|---|---|
| Cache semântico, busca | `cache.py::lookup` (89), `_pipeline` (115) | `$vectorSearch` índice `semantic_cache_vs`, filtro `area`; fallback sem filtro (137) e exato (`_exact_fallback`, 191) | `semantic_cache` |
| Cache: config de threshold | `cache.py::get_config` (52) | `find_one` | `ai_brain.cache_config` |
| Cache: contar hit | `cache.py:170`, `_finish_exact` (186) | `update_one $inc` | `semantic_cache` |
| Cache: gravar / listar / limpar | `cache.py::store` (203, `insert_one` 226), `recent` (230), `clear` (242) | `insert_one`, `find`, `delete_many` | `semantic_cache` |
| Invalidação por change stream | `cache_invalidation.py::_open_stream` (194), `pipeline` (69) | `watch` na coleção de perfis, apaga o cache da área (`delete_many` 121/125) | `ai_brain.area_profiles` (vigia), `semantic_cache`, `change_stream_tokens`, `cache_invalidations` |
| Reconciliação do cache ao subir | `cache_invalidation.py::reconcile` (150) | `delete_many`, `find`, `delete_one` | `semantic_cache`, `area_profiles` |
| Denylist semântico | `policy_guardrails.py::_denylist_top` (193) | `$vectorSearch` índice `guardrail_denylist_vs` via `tenant_vector_stage` | `guardrail_denylist` |
| Política por área | `policy_guardrails.py::get_policy` (112) | `find_one` | `ai_brain.guardrail_policies` |
| Auditoria do guardrail | `policy_guardrails.py::_log` (501, `insert_one` 505) e `recent_events` (602) | `insert_one`, `find` | `guardrail_events` |
| Fila de near-miss | `policy_guardrails.py::_log_candidate` (518), `list_candidates` (542), `review_candidate` (564) | `insert_one`, `find`, `update_one`, promoção por `insert_one` | `guardrail_candidates`, `guardrail_denylist` |
| Classificador de turno pessoal | `turn_classifier.py::classify` (96) | `$vectorSearch` em `turn_probes` | `ai_brain.turn_probes`; config em `ai_brain.turn_classifier_config` (89) |
| Memória: retrieval híbrido (servidor) | `memory.py::rank_fusion_pipeline` (417), `_fused_server` (490) | `$rankFusion` com `$vectorSearch` (`_vector_leg` 365) + `$search` BM25 (`_bm25_leg` 374), `$rerank` opcional | `agent_memory` |
| Memória: fusão no app (fallback) | `memory.py::_fused_app` (498), `_vector_candidates` (402), `_bm25_candidates` (410) | duas agregações e RRF em Python | `agent_memory` |
| Memória: config de retrieval | `memory.py::_retrieval_config_doc` (356) | `find_one` | `ai_brain.memory_retrieval_config` |
| Memória: ler fatos ativos/históricos | `memory.py::_active_docs` (268), `load_longterm` (278) | `find` por `user_key`, `active` | `agent_memory` |
| Memória: orçamento do cliente | `memory.py::active_budget` (213) | `find_one` | `agent_memory` |
| Memória: gravar com supersessão | `memory.py::extract_and_store` (726; duplicado 806, anterior 819, transação 874) | `find_one`, `insert_one` + `update_one $set active:false` dentro de `start_transaction` | `agent_memory` |
| Memória curta (sessão) | `agent.py::_store_short_term` (1330, `update_one` 1341), `_load_recent_history` (1405), `run_agent` (1487) | `update_one` com `$push` + `$slice`, `find_one` | `agent_sessions` |
| Checkpoint do turno | `agent_graph.py::_build_graph` (640) | `MongoDBSaver` (langgraph), TTL = sessão | `langgraph_checkpoints`, `langgraph_checkpoint_writes` |
| Consultas do agente via MCP | `agent.py::_run_tool_loop` (1080), chamada em 1226 | `find`, `aggregate` (inclui `$vectorSearch` de catálogo e `$graphLookup`), `update-many` (só status permitido) | `support_orders`, `produtos_vector` (montadas pelo modelo e reescritas por `_read_denial`/`_write_denial`) |
| Cadeia de trocas | `graph.py::build_order_chain_pipeline` (20) | `$match` + `$graphLookup` com `restrictSearchWithMatch` por dono | `support_orders` |
| Aquecimento do MCP | `agent.py::warm_up_session` (222, chamada 236) | `aggregate` vazio via MCP | `support_orders` |
| Traces do agente | `main.py::_persist_agent_trace` (963; `insert_one` 942 e 974) | `insert_one` | `agent_traces` |
| Auditoria admin | `main.py::admin_audit` (397, `insert_one` 401) | `insert_one` | `admin_audit` |
| Modelo ativo | `llm.py::get_active_config` (28) | `find_one` (por área, com fallback) | `ai_brain.model_config` |
| Perfis e usuários | `profiles.py::list_users` (24), `get_user` (40), `get_area_profile` (69) | `find`, `find_one` | `app_users`, `ai_brain.area_profiles` |
| Pedidos do usuário (orientação) | `guidance.py::user_orders` (37) | `find` por `owner_user_key` | `support_orders` |
| web_search: config, cota, cache, auditoria | `web_search.py::get_config` (103), `_reserve_credit` (117, `find_one_and_update`), `run` (180, cache 198/225), `_audit` (133) | `find_one`, `find_one_and_update`, `update_one`, `insert_one` | `ai_brain.tool_registry`, `ai_brain.web_search_quota` (cota mensal), `POC.web_search_cache`, `external_calls` |
| Templates de prompt | `main.py::list_templates` (563), `get_template` (569), `add_variant` (583), `remove_variant` (606) | `find`, `find_one`, `update_one` | `ai_brain.prompt_templates` |
| Troca de modelo | `main.py::set_primary_model` (641) | `update_one` | `ai_brain.model_config` |
| Reset e índices | `seed.py::main` (1017), `create_vector_indexes` (896), `VECTOR_INDEXES` (839), `BM25_INDEXES` (856), TTLs (1264-1300) | `drop`/`insert_many`, `create_search_index` vetorial (autoEmbed) e BM25, `create_index` com `expireAfterSeconds` | todas as coleções acima |

Nota: `seed.py` define os índices `semantic_cache_vs`, `guardrail_denylist_vs`, `agent_memory_vs` e `agent_memory_bm25`. Os números de linha de `seed.py` no briefing (768-803) estão desatualizados; use as linhas acima.

## 4. Compartilhado × só desta PoV

**Vem de `_shared` (pacote pov-shared, instalado em modo editável por `scripts/bootstrap-venvs.sh`; sem ele o app sobe, mas degrada):**

| Módulo | Para que serve aqui | Import exato |
|---|---|---|
| `guardrails` | pontuação do denylist por cláusula, heurística de injeção determinística | `from guardrails import ascore_by_clause` (`policy_guardrails.py:142`), `ClauseBudgetExceeded, ScoreError` (154), `import guardrails as shared` (64; também `memory.py:129`) |
| `atlas_retrieval` | `CapabilityCache` e a fusão híbrida da memória; `ChangeStreamWorker` do cache | `import atlas_retrieval as _ar` (`memory.py:321`, `cache_invalidation.py:38`) |
| `tracing` | tracing distribuído com PII mascarada | `from tracing import init_tracing` (`observability.py:131`) |

`_shared/grove_client.py` **não** é usado: o gateway de LLM é o `GatewayClient` próprio em `backend/gateway.py`.

**Variáveis de ambiente.** Comuns (vêm de `PoVs/.env.shared` via `_shared/sync_env.py`): `MONGODB_URI`, `GROVE_API_KEY`, `GROVE_ANTHROPIC_BASE_URL`, `GROVE_CHAT_COMPLETIONS_URL`, `GROVE_OPENAI_MODELS`, `LLM_BLENDED_PRICES` e as chaves do Langfuse (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`). Locais da PoV: `MONGODB_DB`, `MONGODB_BRAIN_DB`, `MONGODB_CATALOG_DB` (nomes dos bancos), `MCP_SERVER_VERSION`, `MCP_POOL_SIZE` (MCP), `AUTH_REQUIRED`, `JWT_SECRET`, `ADMIN_API_KEY`, `RATE_LIMIT_PER_MINUTE` (segurança), `GUARDRAIL_*` (limites do guardrail), `TAVILY_API_KEY` (web_search), `AGENT_*` (timeouts/orçamento), `ALLOW_DEMO_DB_WRITE` (trava do seed), `CHAOS*` (testes de falha), `BACKEND_PORT`/`PORT` (portas). A lista completa está em `.env.example` (placeholders).

**Bancos e coleções.** Exclusivos desta PoV por padrão: `ai_brain` e `POC` (nomes configuráveis). Se outras PoVs usam o mesmo nome `POC` no mesmo cluster, as coleções com o mesmo nome se misturariam: não encontrei no código nenhuma declaração de que `POC` seja compartilhado; confirme antes de apontar duas PoVs ao mesmo banco. Para testes use `POC_test` e `ai_brain_test`.

**Chamadas externas (sem host/chave no código):**
- LLM via gateway Grove: `backend/gateway.py` (cliente Anthropic com `Authorization: Bearer`, linhas ~237-258) e `backend/llm.py::call_model`/`call_with_fallback` (62, 90); o loop do agente usa `agent.py::_create_with_retry` (90).
- Embeddings Voyage: **não há chamada em Python**; os índices são `autoEmbed` (voyage-4), o Atlas gera os vetores (`seed.py:867`). O `$rerank` usa modelo Voyage no servidor (`memory.py::rank_fusion_pipeline`).
- Langfuse: `backend/langfuse_tracing.py` (fail-open).
- Tavily: `backend/web_search.py::_tavily` (141).
- MongoDB MCP Server: `agent.py::mcp_server_params` (939).

## 5. "Quero mexer em..." (atalhos)

- Texto/visual de uma aba -> `frontend/src/tabs/FlexibleSchema.jsx`, `ModelSwap.jsx` ou `Agent.jsx`.
- Perguntas sugeridas (chips) -> `AREA_SCENARIOS` em `backend/agent.py:660`.
- Roteiro da demo automática -> `DEMO_PLAYLIST` em `backend/agent.py:890`.
- Prompt do agente (instruções de sistema) -> `_SYSTEM_TEMPLATE` em `backend/agent.py` (renderizado por `_render_system`, 638, em `SYSTEM`, 649). Os `prompt_templates` do Mongo (`seed.py:33`) alimentam a aba 1 e o chat da aba 2.
- Persona/política por área -> `AREA_PROFILES` (`seed.py:472`) e `GUARDRAIL_POLICY` (`seed.py:555`), depois rodar o seed.
- Frases proibidas do guardrail -> `GUARDRAIL_DENYLIST` em `backend/seed.py:619`.
- Adicionar uma rota -> `backend/main.py` (mesmo padrão de `@app.get`) e o método correspondente em `frontend/src/api.js`.
- Mudar uma query de memória -> `backend/memory.py::rank_fusion_pipeline` (417) e `_vector_leg`/`_bm25_leg`.
- Mudar o threshold do cache -> documento `ai_brain.cache_config` (`seed.py:144`), rodar `backend/calibrate_thresholds.py` antes de decidir.
- Regras de leitura/escrita do agente no MCP -> `_read_denial` / `_write_denial` em `backend/agent.py` (448 / 390).
- Trocar o modelo padrão -> aba 2 da UI, ou `ai_brain.model_config` (`seed.py:120`).
- Mudar porta -> `frontend/vite.config.js` (5183), `start.sh` (`BACKEND_PORT`, 8010).
- Mudar o seed / dados de demo -> `backend/seed.py` (`SUPPORT_ORDERS` 223, `APP_USERS` 457).

## 6. Testes

Backend (unittest, sem servidor): `cd backend && .venv/bin/python -m unittest discover -s tests -v`.

| Arquivo (`backend/tests/`) | O que cobre |
|---|---|
| `test_policies.py` | Reescrita/negação de leitura e escrita MCP, política do agente |
| `test_budget_policy.py` | Orçamento do cliente na reescrita |
| `test_mcp_contract.py` | Contrato com o MCP Server (ferramentas esperadas) |
| `test_tenant_filter.py` | `tenant_vector_stage` recusa busca sem tenant |
| `test_memory_retrieval.py`, `test_memory_extractor.py`, `test_memory_gate*.py`, `test_memory_explain_trace.py` | Retrieval híbrido, extração, portão de memória, explicação |
| `test_cache_invalidation.py` | Worker de change stream |
| `test_guardrail_dilution_adversarial.py`, `test_hardening_adversarial.py` | Casos adversariais do guardrail e do endurecimento |
| `test_turn_classifier.py`, `test_calibration.py` | Classificador de turno e calibração |
| `test_search_readiness.py` | Vazio de busca com índice ausente/em construção: denylist, cache, memória, caos `index_empty` (ADR-013) |
| `test_answer_checks.py` | `answer_checks` e as reprovações por conteúdo do eval |
| `test_web_search.py` | Ferramenta web_search (cota, cache) |
| `test_seed_indexes.py`, `test_qe_profile.py` | Índices do seed e Queryable Encryption |
| `test_chaos.py`, `test_chaos_isolation.py`, `test_gateway.py`, `test_observability.py` | Resiliência, gateway, métricas |
| `smoke.py`, `smoke_memory.py` | Smoke contra serviços reais |

Frontend (JS puro): `frontend/tests/*.test.mjs` (stream, deadline, memória, rich text).
Playwright na raiz (app rodando): `npm run test:visual` (`tests/visual/tabs.spec.js`) e `npm run test:e2e` (`tests/e2e/demo-flow.spec.js`, configuração `playwright.e2e.config.js`).
Eval: `cd backend && .venv/bin/python eval_agent.py` (offline) ou `--live`; dataset e relatórios em `eval/`.
