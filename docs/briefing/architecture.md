# Arquitetura — singleagent (Intelligence Layer PoV)

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
- **Observability**: Langfuse self-host, fail-open (`backend/tracing.py`) — a infra interna do Langfuse não é exposta em material de cliente (ver `../observability/README.md`).
- **Frontend**: React 18 + Vite + LeafyGreen (design system MongoDB), JavaScript puro (sem TS), sem router/estado externo. `fetch` cru embrulhado em `api.js`.

## Componentes (backend/)

| Arquivo | Responsabilidade |
|---|---|
| `agent.py` | Orquestração do loop de tool-use, allowlist/reescrita de política de ferramentas, orçamentos de contexto, chips `AREA_SCENARIOS`, `DEMO_PLAYLIST` |
| `memory.py` | Memória de longo prazo (fatos), retrieval híbrido (`$vectorSearch` + BM25 + RRF), gate de extração, dedup, supersessão transacional |
| `graph.py` | Travessia da cadeia de trocas (`$graphLookup`) — ver `queries.md` |
| `cache.py` | Cache semântico escopado por área, TTL, fallback exato |
| `guardrails.py` | Política por área, máscara de PII, denylist semântica, auditoria, fila de near-miss |
| `profiles.py` | Resolução usuário → área |
| `db.py` | Conexão Atlas (pool), `safe_query` (erros operacionais → mensagem amigável) |
| `main.py` | Superfície da API REST/SSE, propriedade do `session_id`, persistência do trace |
| `llm.py` | Leitura de `ai_brain.model_config` a cada chamada |
| `guidance.py` | Anexos de orientação quando busca volta vazia ou escrita é negada |
| `seed.py` | Reset único e idempotente de `ai_brain` + `POC`: dados, denylist, índices (regulares, TTL, vetoriais, BM25) e runtime (sessões, checkpoints, memória, auditoria). Recusa `POC`/`ai_brain` sem `ALLOW_DEMO_DB_WRITE=1`; `--keep-runtime` preserva memória e auditoria |
| `calibrate_thresholds.py` | Mede thresholds de cache/guardrail contra probes rotulados — nunca escolhido à mão |

## Fluxo de dados por turno (aba Agente)

Todo turno roda, nesta ordem, inteiramente como operações MongoDB:

```
guardrail de entrada + máscara de PII (guardrails.check_input)
  → busca no cache semântico, escopada por área (cache.lookup)
    → HIT: resposta servida do MongoDB, ZERO chamada ao LLM
    → MISS:
      → memória de longo prazo relevante (memory.load_relevant) [concorrente]
      → extração de fato com gate (memory.should_extract → memory.extract_and_store) [concorrente]
      → histórico recente da sessão (POC.agent_sessions, janela hidratada)
      → loop de ferramentas MCP (agent._run_tool_loop) — find/aggregate/update-many
      → guardrail de saída (guardrails.check_output) — redige PII
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

## Invariantes de arquitetura (não relaxar sem atualizar esta página)

1. **Superfície de ferramentas do agente**: só `find`, `aggregate`, `update-many`. Nenhum delete/drop/count/schema chega ao modelo.
2. **Toda chamada é reescrita no servidor, não só validada** — o dicionário de input é limpo e remontado inteiro (`_read_denial`/`_write_denial` em `agent.py`), então opção extra que o modelo inventar (sort, limit, collation, upsert) nunca sobrevive.
3. **Escrita com escopo de collection E de filtro**: `update-many` só em `POC.support_orders`, e só com um `order_id` escalar específico + status de uma allowlist. Memória/sessões são geridas pela plataforma, nunca pelo agente.
4. **Isolamento é a cláusula `filter` da query, aplicada como pré-filtro nativo dentro da busca ANN** (ver ADR-001). O índice não autoriza nada sozinho: o filtro é imposto por `db.tenant_vector_stage`, único construtor de `$vectorSearch` em coleção por tenant, e `tests/test_tenant_filter.py` barra query montada por fora.
5. **Memória é dado, nunca instrução** — fatos injetados entre delimitadores `<fatos_do_cliente>`, com instrução explícita de ignorar comandos embutidos.
6. **PII mascarada antes de LLM/cache/memória/trace**, redigida de novo na saída.
7. **Budgets de caractere são travas de segurança, não estimativa de billing** — a contagem real de token vem do usage do provedor.
8. **Identidade vem do seletor de UI só nesta demo** — em produção seria claim JWT/OIDC.
9. **Perfil de produção falha fechado** (`ENVIRONMENT=production` exige segredos fortes, `AUTH_REQUIRED=1`, CORS explícito).

## Onde ir a partir daqui

- Query específica, índice, TTL → `queries.md`
- Tela, fluxo de usuário, componente de UI → `ui-flows.md`
- Como o agente decide, faz checkpoint de sessão e usa memória curta+longa → `agent-behavior.md`
- Racional completo de uma decisão → `docs/adr/`
