# Arquitetura — singleagent (Intelligence Layer PoV)

> Números de linha podem ficar defasados após mudanças: a fonte atualizada é [MAPA_DO_CODIGO.md](../MAPA_DO_CODIGO.md).

> Este briefing existe pra responder rápido "onde está X" quando o gestor pergunta. Se você quer a tese completa e o roteiro de apresentação, releia o `README.md` — ele tem mais autoridade e é mantido atualizado. Aqui vai o mapa técnico: stack, componentes, fluxo de dados por turno, e pra onde ir quando algo quebra.

## O que é

PoV/demo ao vivo mostrando o MongoDB Atlas como camada de dados **e** de orquestração de um agente de IA de suporte. A tese central: configuração de prompt, configuração de modelo, cache semântico e memória do agente vivem todos como **documentos MongoDB**, não como config de aplicação nem como um vector DB separado ao lado. UI em português, usada ao vivo com clientes brasileiros.

Três abas de demonstração:

| Aba | Componente | O que mostra |
|---|---|---|
| 1 — Schema flexível | `frontend/src/tabs/FlexibleSchema.jsx` | templates de prompt como documentos polimórficos em `ai_brain.prompt_templates`; `$set` ao vivo sem migração |
| 2 — Troca de modelo e custo | `frontend/src/tabs/ModelSwap.jsx` | `ai_brain.model_config` lido a cada chamada de LLM (`backend/llm.py`); Sonnet 4.5↔Sonnet 5.5 é um `update_one` |
| 3 — Agente | `frontend/src/tabs/Agent.jsx` | agente de suporte autônomo (`backend/agent.py`) rodando tool-use real contra MongoDB **via MongoDB MCP Server** |

A aba 3 é o core do produto e o assunto deste documento (e de `agent-behavior.md`).

## Stack

- **Backend**: Python 3.12+, FastAPI (`backend/main.py`), driver `pymongo` Async (`AsyncMongoClient`, não Motor — deprecado). Dois databases no mesmo cluster Atlas: `ai_brain` (config viva, só humano escreve) e `POC` (dados de demo + telemetria).
- **LLM**: Anthropic (Claude Sonnet 4.5 default, fallback configurável), acessado via um gateway HTTP interno (`backend/gateway.py`, `GatewayClient`) — não direto ao SDK.
- **Ferramentas do agente**: MongoDB MCP Server (`npx mongodb-mcp-server@2.1.0`, pinado — ver `backend/agent.py:mcp_server_params`), o mesmo protocolo que uma IDE usaria, rodando via stdio.
- **Observability**: Langfuse self-host, fail-open (`backend/langfuse_tracing.py`) — a infra interna do Langfuse não é exposta em material de cliente (ver `../observability/README.md`).
- **Frontend**: React 18 + Vite + LeafyGreen (design system MongoDB), JavaScript puro (sem TS), sem router/estado externo. `fetch` cru embrulhado em `api.js`.

## Componentes (backend/)

| Arquivo | Responsabilidade |
|---|---|
| `agent.py` | Orquestração do loop de tool-use, allowlist/reescrita de política de ferramentas, orçamentos de contexto, chips `AREA_SCENARIOS`, `DEMO_PLAYLIST` |
| `memory.py` | Memória de longo prazo (fatos), retrieval híbrido num `$rankFusion` (vetor + BM25 pt) com fallback em cascata, gate de extração, dedup, supersessão transacional |
| `graph.py` | Travessia da cadeia de trocas (`$graphLookup`) — ver `queries.md` |
| `cache.py` | Cache semântico escopado por área, TTL, fallback exato |
| `cache_invalidation.py` | Worker de change stream (lifespan) que invalida o cache por área quando a persona muda |
| `policy_guardrails.py` | Política por área, máscara de PII, denylist semântica, auditoria, fila de near-miss |
| `profiles.py` | Resolução usuário → área |
| `db.py` | Conexão Atlas (pool), `safe_query` (erros operacionais → mensagem amigável) |
| `main.py` | Superfície da API REST/SSE, propriedade do `session_id`, persistência do trace |
| `llm.py` | Leitura de `ai_brain.model_config` a cada chamada |
| `guidance.py` | Anexos de orientação quando busca volta vazia ou escrita é negada |
| `seed.py` | Reset único e idempotente de `ai_brain` + `POC`: dados, denylist, índices (regulares, TTL, vetoriais, BM25) e runtime (sessões, checkpoints, memória, auditoria). Recusa `POC`/`ai_brain` sem `ALLOW_DEMO_DB_WRITE=1`; `--keep-runtime` preserva memória e auditoria |
| `agent_graph.py` | Grafo LangGraph do agente (nós, checkpoints `MongoDBSaver` com TTL) |
| `turn_classifier.py` | Classificador de turno pessoal (`$vectorSearch` em `turn_probes`), usado no cache |
| `web_search.py` | Ferramenta de busca web com cache, cota mensal e auditoria de chamadas externas |
| `resilience.py` | Resiliência (retry/breaker) nas chamadas externas |
| `observability.py` | Observabilidade do agente (métricas/tracing fail-open) |
| `calibrate_thresholds.py` | Mede thresholds de cache/guardrail contra probes rotulados — nunca escolhido à mão |

## Fluxo de dados por turno (aba Agente)

Todo turno roda, nesta ordem, inteiramente como operações MongoDB:

```
guardrail de entrada + máscara de PII (policy_guardrails.check_input)
  → busca no cache semântico, escopada por área (cache.lookup)
    → HIT: resposta servida do MongoDB, ZERO chamada ao LLM
    → MISS:
      → memória de longo prazo relevante (memory.load_relevant) [concorrente]
      → extração de fato com gate (memory.should_extract → memory.extract_and_store) [concorrente]
      → histórico recente da sessão (POC.agent_sessions, janela hidratada)
      → loop de ferramentas MCP (agent._run_tool_loop) — find/aggregate/update-many
      → guardrail de saída (policy_guardrails.check_output) — redige PII
      → escrita de curto prazo (agent._store_short_term → POC.agent_sessions)
      → insert/supersessão de longo prazo (memory.extract_and_store)
      → escrita no cache (só se resposta genérica, sem personalização por memória)
```

Memória de longo prazo e extração de fato rodam **concorrentemente** — são independentes e cada uma custa latência sentida pelo usuário.

## Decisões de arquitetura já registradas (ADRs — não duplicar aqui)

Ver `docs/adr/`. Para o "porquê" de cada uma, leia o ADR; aqui só o resumo de uma linha e a queda prática:

- **ADR-001 — Isolamento multi-tenant via filter fields em coleção compartilhada (Vector Search)**: `area`/`user_key` são campos `filter` nos índices `$vectorSearch` (não pós-filtro em Python). Aplica-se a `semantic_cache_vs`, `guardrail_denylist_vs`, `agent_memory_vs`.
- **ADR-002 — TTL em `POC.agent_sessions` (memória de curto prazo)**: `updated_at` expira em 24h de inatividade.
- **ADR-003 — TTL em `POC.guardrail_events` e `POC.guardrail_candidates`**: `at` expira em 30 dias (audit log).
- **ADR-004 — Guardrail near-miss learning loop com aprovação humana**: score "quase bloqueado" vira candidato revisável, nunca promoção automática ao denylist.
- **ADR-005 — Quick-chat com gate de cache e histórico de sessão.**
- **ADR-008 — Memória longa com `$rankFusion` no servidor, `scoreDetails` e config viva** (`ai_brain.memory_retrieval_config`).
- **ADR-009 — `$rerank` na memória**: implementado e medido, desligado por padrão.
- **ADR-010 — Invalidação do cache semântico por Change Stream** de `ai_brain.area_profiles` (`backend/cache_invalidation.py`).
- **ADR-011 — Queryable Encryption** para PII do perfil, standalone (`backend/scripts/qe_sensitive_profile.py`).
- **ADR-012 — Por que não o `MongoDBStore` do LangGraph** na memória longa.
- **ADR-014 — Promessa sem escrita vira oferta**: `answer_checks.rewrite_unbacked` troca, na frase, a ação afirmada sem `update-many` bem-sucedido ("Vou encaminhar seu caso") pela oferta ("Se você quiser, posso encaminhar seu caso") antes da entrega; original no trace e em `guardrail_events`; `ANSWER_REWRITE=0` reverte.
- **ADR-013 — Busca vazia exige índice consultável**: `$vectorSearch`/`$search` em índice ausente ou em construção devolve `[]` sem erro; `backend/search_readiness.py` confere `list_search_indexes` (queryable/READY) antes de aceitar o vazio. Denylist e classificador falham fechado, memória troca de perna com o nome verdadeiro, cache marca `unavailable`.
- **ADR-007 — Ferramenta externa de busca web, governada por documento**: `web_search` (Tavily) fora do MCP, ligada por área em `ai_brain.tool_registry`, taint que tira a escrita do resto do turno, cota/cache/auditoria no Atlas, relógio no prompt e `date_diff` para contagem de dias.

## Invariantes de arquitetura (não relaxar sem atualizar esta página)

1. **Superfície de ferramentas do agente**: pelo MCP, só `find`, `aggregate`, `update-many`. Nenhum delete/drop/count/schema chega ao modelo. Fora do MCP, só `date_diff` (pura) e `web_search` (registro por área + taint, ADR-007).
10. **Conteúdo web contamina o turno**: depois de `web_search`, nenhuma escrita no mesmo turno (`agent.TAINTED_WRITE_DENIAL`).
2. **Toda chamada é reescrita no servidor, não só validada** — o dicionário de input é limpo e remontado inteiro (`_read_denial`/`_write_denial` em `agent.py`), então opção extra que o modelo inventar (sort, limit, collation, upsert) nunca sobrevive.
3. **Escrita com escopo de collection E de filtro**: `update-many` só em `POC.support_orders`, e só com um `order_id` escalar específico + status de uma allowlist. Memória/sessões são geridas pela plataforma, nunca pelo agente.
4. **Isolamento é a cláusula `filter` da query, aplicada como pré-filtro nativo dentro da busca ANN** (ver ADR-001). O índice não autoriza nada sozinho: o filtro é imposto por `db.tenant_vector_stage`, único construtor de `$vectorSearch` em coleção por tenant, e `tests/test_tenant_filter.py` barra query montada por fora.
5. **Memória é dado, nunca instrução** — fatos injetados entre delimitadores `<fatos_do_cliente>`, com instrução explícita de ignorar comandos embutidos.
6. **PII mascarada antes de LLM/cache/memória/trace**, redigida de novo na saída.
7. **Budgets de caractere são travas de segurança, não estimativa de billing** — a contagem real de token vem do usage do provedor.
8. **Identidade vem do seletor de UI só nesta demo** — em produção seria claim JWT/OIDC.
9. **Perfil de produção falha fechado** (`ENVIRONMENT=production` exige segredos fortes, `AUTH_REQUIRED=1`, CORS explícito).
11. **Vazio de busca não é resposta** sem índice consultável (ADR-013): toda camada que decide por "nada parecido" passa por `search_readiness`.

## Decisões de desenho

Justificativas de implementação que não têm ADR próprio. O racional das decisões maiores está em `docs/adr/`.

- **Versão do MCP Server pinada** (`MCP_SERVER_VERSION`, sobrescrevível por env): sem pin, a mesma PoV roda com pacotes diferentes em dev, demo e apresentação, com comportamento diferente e nenhum aviso. `backend/tests/test_mcp_contract.py` conecta ao binário pinado e falha se `ALLOWED_TOOLS` deixar de bater com `session.list_tools()`; o teste captura `list_tools()` antes do teardown e só trata como benigno o `anyio.BrokenResourceError` do encerramento do subprocesso.
- **Cadeia de trocas resumida antes do modelo:** `agent.py:_summarize_chain_text` converte a saída do `$graphLookup` nos sinais `replacements`, `same_sku_count`, `recurring_defect`, `needs_quality_review`. Poupa contexto e evita o modelo somar elos errado; se o parse falhar, o texto original passa intacto (formato inesperado do MCP não derruba o turno). Com `needs_quality_review: true` o prompt (regra 4b) manda abrir chamado em vez de nova troca.
- **Bancos por constante, não por literal:** `db.py` expõe `DB_MAIN`/`DB_BRAIN`/`DB_CATALOG` e a política de ferramentas deriva os alvos delas. Com literais, o app leria um banco e o MCP escreveria em outro quando os scripts trocam para os bancos `*_test`. O catálogo vetorial é só leitura e fica no banco da demo.
- **CSOT acima do `maxTimeMS`:** o cliente declara `retryWrites`/`retryReads` (quem cobre troca de primário é o driver) e `timeoutMS` (padrão 15 s) cobrindo seleção de servidor, handshake e retries, acima do `maxTimeMS` de 10 s para que o erro específico da consulta vença primeiro. O worker de change stream usa cliente próprio sem CSOT (ADR-010).
- **Pool de conexões explícito:** `maxPoolSize=50`, `minPoolSize=5`, `maxIdleTimeMS=30000` como número de referência para dimensionamento (tier de referência M10/M20), em vez dos defaults implícitos do driver.
- **Chaves privadas do estado do grafo** (`_area_profile`, `_used_business_tools`) precisam estar declaradas em `agent_graph.TurnState`: o `StateGraph` descarta em silêncio chave de retorno que não é campo do schema, e o erro só aparece como `KeyError` no nó seguinte.
- **Cache e transação:** `agent.mentions_order` marca como transacional qualquer turno cuja pergunta ou resposta cite um `PED-…`, mesmo sem ferramenta. Uma pergunta genérica recebia a lista de pedidos do cliente via `guidance.scope_reply` e era gravada no cache compartilhado da área (`docs/eval-report.md`, achado 2). Pergunta com `PED-…` também não lê o cache.
- **Cache por capacidade:** toda entrada carrega o carimbo `web_enabled` (`cache._capability_ok`); resposta gerada sem busca web não é servida a turno que tem a ferramenta, senão ligar a ferramenta no registro não mudaria nada até o TTL. Resposta que cita a data de hoje (`agent.uses_current_date`) não vai para o cache.
- **Aba 2, acerto imediato por texto normalizado** (`cache.lookup`, modo `exact-recent`): o autoEmbed leva de segundos a minutos para indexar a entrada nova. `no_cache` ("Comparar modelos") ignora o cache, senão repetir a pergunta após trocar o modelo devolveria a resposta do modelo anterior. O provider vem do catálogo, nunca do payload.
- **Guardrail por intenção, não pela mensagem inteira:** o embedding da mensagem inteira é diluído por uma segunda intenção anexada e cai abaixo dos negativos legítimos, então baixar o limiar não resolve. A denylist pontua cada cláusula (`ascore_by_clause` do `pov-shared`); acima de `GUARDRAIL_MAX_CLAUSES` a mensagem é bloqueada, nunca reagrupada nem truncada (reagrupar diluía a intenção proibida). A heurística determinística de injeção roda antes e é complementar ao embedding.
- **Fidelidade da resposta** (`backend/answer_checks.py`, desde 2026-11-12; reescrita desde 2026-11-13, ADR-014): depois do guardrail de saída, `agent_graph._answer_fidelity` primeiro reescreve a promessa sem escrita como oferta (evento `rewrite`, `guardrail.output.answer_rewrites`, contador `answer_rewrites`) e depois confere o texto entregue. `pedido_sem_fonte` = `PED-…` citado que não aparece em nada que o modelo viu no turno; sai da resposta e entra o pedido do número (`ORDER_FALLBACK_TEXT`). `promessa_sem_escrita` = ação de pedido afirmada em 1ª pessoa ("vou abrir um chamado", "registrei a troca") sem `update-many` bem-sucedido que tenha casado um pedido (`metrics.order_writes_ok`, `agent.write_took_effect`); `prazo_sem_fonte` = prazo prometido ("em até 24 horas") cujo número e unidade não aparecem no prompt da área, no contexto, no histórico nem nos resultados de ferramenta. Vira evento `guardrail`/`flag` no trace, `guardrail.output.answer_checks`, registro em `guardrail_events` (texto mascarado) e o contador `answer_flags` em `/api/metrics`. Não bloqueia: é heurística de frase. A prevenção é a regra 4a do prompt; a correção da promessa é a reescrita (o prazo inventado só é apontado, não há frase segura para pôr no lugar).
- **Capacidade medida** (`backend/scripts/load_test.py`, M10/M20, camada de dados do turno): 10 simultâneos p50 4,6 s / p95 12,9 s sem falha; 30 simultâneos p50 12 s / p95 30 s com `maxTimeMS` cortando. O turno faz quatro `$vectorSearch` com autoEmbed, e esse é o gargalo.

## Onde ir a partir daqui

- Query específica, índice, TTL → `queries.md`
- Tela, fluxo de usuário, componente de UI → `ui-flows.md`
- Como o agente decide, faz checkpoint de sessão e usa memória curta+longa → `agent-behavior.md`
- Racional completo de uma decisão → `docs/adr/`
