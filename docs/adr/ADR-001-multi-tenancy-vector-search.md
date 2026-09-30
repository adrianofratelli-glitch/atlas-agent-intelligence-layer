# ADR-001: Isolamento multi-tenant via filter fields em coleção compartilhada (Atlas Vector Search)

**Status:** Aceito
**Data:** 2026-07-13
**Contexto do cliente/conta:** PoV Intelligence Layer (intelligence-layer-poc)

## Contexto
PoV precisa servir múltiplos departamentos (`area`: `default` = suporte e-commerce, `financeiro`) e múltiplos usuários no mesmo agente, com garantia de que dado/memória de um departamento/usuário nunca vaze pra outro em cache semântico, guardrails e memória de longo prazo do agente. Carga é pequena (poucos departamentos, poucos usuários, dataset por área abaixo de 10k vetores). Sem requisito de isolamento físico/VPC por tenant.

## Decisão
Coleção compartilhada por caso de uso (`semantic_cache`, `guardrail_denylist`, `agent_memory`), isolamento via campos `area` e/ou `user_key` declarados como `filter` no índice Atlas Vector Search autoEmbed (voyage-4). O `$vectorSearch` aplica o filtro durante a busca ANN (pre-filtro nativo) — isolamento garantido pelo índice, não por pós-filtro em código de aplicação. Campo de filtro sempre resolvido do lado autenticado (`require_demo_user`), nunca aceito como input livre.

## Alternativas consideradas
| Opção | Prós | Contras | Por que rejeitada |
|---|---|---|---|
| Coleção por tenant | Isolamento "parece" mais forte | Zero ganho real de isolamento (auth Atlas é a nível de DB, não de coleção); carga de change streams variável por coleção; complexidade operacional de manter N coleções | Anti-pattern documentado pelo próprio MongoDB para Vector Search multi-tenant |
| Database por tenant | Isolamento forte de fato (auth a nível de DB) | Overhead operacional alto pra escala do PoV; sem requisito de compliance que justifique | Sem VPC/compliance boundary exigindo isso hoje |
| Filter field em coleção compartilhada (escolhida) | Isolamento garantido pelo índice na busca ANN; operação simples; escala até 1M tenants pequenos | Depende de filtro sempre presente e vindo de fonte autenticada | — |

## Evidência
Índices confirmados em `backend/seed.py` (linhas 365-429): `semantic_cache_vs` (filter: `area`), `guardrail_denylist_vs` (filter: `area`), `agent_memory_vs` (filter: `user_key`, `active`). Pre-filtro nativo confirmado em `cache.py:92` e `memory.py:162`. Defesa adicional em `agent.py:132` (rebind forçado de `user_key` em leitura de `agent_sessions`) e `agent.py:71` (`WRITE_SCOPE` restringe escrita do agente a `support_orders` com `order_id` validado). Nenhum load test formal de isolamento cross-tenant rodado — `[TBD - pendente de teste]`.

## Consequências
- Positivas: isolamento garantido no nível do índice (não depende de disciplina de código em todo query path); operação simples (1 coleção por caso de uso, não N); escala natural até 1M tenants pequenos sem redesenho.
- Negativas / trade-offs aceitos: sem isolamento físico/VPC entre áreas — se cliente exigir isolamento regulatório forte entre departamentos no futuro (ex: financeiro sob compliance mais rígido), modelo atual não atende sem migração.
- Reversibilidade: média. Migrar pra coleção-por-tenant depois exige reindexação e migração de dados (MongoDB fornece script de referência), mas não é trivial em produção com dado já acumulado.

## Riscos e mitigação
1. **Índice sem `indexingMethod` definido → default HNSW.** Para o volume atual (poucos tenants, <10k vetores/área), MongoDB recomenda `indexingMethod: "flat"` — mais previsível, sem overhead de grafo, filtro já seletivo o suficiente. Ação: mudar os 3 índices pra flat.
2. **Fallback de degradação sem índice pronto** (`cache.py` modos `vector-postfilter`/`exact`, `memory.py` modo `recent`) depende só de código de aplicação pra isolar, não do índice. Hoje filtra corretamente, mas é o único ponto onde a garantia "índice isola, não app" fica mais fraca. Ação: teste automatizado cobrindo esse path específico.
3. **Fallback `semantic_fail_mode: "open"` na área `default`** — se `semantic_cache_vs`/`guardrail_denylist_vs` cair sem índice pronto em produção, área default degrada silenciosamente (área `financeiro` já é fail-closed). Ação: confirmar os 3 índices sempre presentes antes de go-live, ou considerar fail-closed também pro default.
