# Queries, pipelines e índices — singleagent

> Todo item aqui foi extraído do código real (grep de `.aggregate(`, `.find(`, `create_index`, `$vectorSearch`, `expireAfterSeconds`, em 2026-09-19). Nada inventado. Se o código mudar, este arquivo fica desatualizado até a próxima passada — não confie cegamente em linha exata sem checar o arquivo.

## Sumário

- 3 índices Atlas Vector Search (autoEmbed voyage-4) multi-tenant
- 1 índice Atlas Search BM25 (lexical, parte do retrieval híbrido de memória)
- 2 TTL indexes de dado operacional (curto prazo, cache) + 4 TTL indexes de auditoria (30 dias)
- 5 índices regulares (compostos + únicos)
- ~9 pipelines de agregação/find nomeados no código

---

## 1. Índices Atlas Vector Search (autoEmbed voyage-4)

Definidos em `backend/seed.py:768-803` (`VECTOR_INDEXES`, `_vector_index_definition`), criados/atualizados por `create_vector_indexes` (`seed.py:806`).

Todos usam `indexingMethod: "flat"` (não HNSW) — decisão do ADR-001, risco 1: poucos tenants, <10k vetores por área/usuário, filtro já seletivo o bastante. Reavaliar para HNSW só se algum tenant passar de ~10k vetores.

| Índice | Collection | Path (autoEmbed) | Filter fields | Por que existe |
|---|---|---|---|---|
| `semantic_cache_vs` | `POC.semantic_cache` | `question` | `area` | Cache semântico de FAQ — resposta reaproveitada mesmo com pergunta reescrita |
| `guardrail_denylist_vs` | `POC.guardrail_denylist` | `phrase` | `area` | Denylist semântico de guardrail — bloqueia intenção, não string exata |
| `agent_memory_vs` | `POC.agent_memory` | `fact` | `user_key`, `active` | Memória de longo prazo do agente — retrieval só dos fatos ATIVOS deste usuário |
| `produtos_vector` (pré-existente, fora do seed) | `POC.produtos_vector` | `descricao` | — | Catálogo de produtos para sugestão de substituto |

O `filter` é campo nativo do índice, não `.filter()` em Python — é o mecanismo central do ADR-001: o `$vectorSearch` só percorre vetores que já passam no filtro de tenant, então isolamento é garantido pelo índice, não por disciplina de código.

## 2. Índice Atlas Search BM25 (lexical)

`backend/seed.py:779-786` (`BM25_INDEXES`):

```python
{"db": "POC", "collection": "agent_memory", "name": "agent_memory_bm25",
 "definition": {"mappings": {"dynamic": False, "fields": {
     "fact": {"type": "string"},
     "user_key": {"type": "token"},
     "active": {"type": "boolean"},
 }}}}
```

Metade lexical do retrieval híbrido de memória longa (ver seção 5). Cobre o que embedding dilui: códigos, nomes próprios, termos exatos.

---

## 3. `$vectorSearch` — buscas vetoriais no código

### 3.1 Cache semântico — `backend/cache.py:92-108` (`lookup`)

Onde: `cache.py:92`, função `_pipeline`, chamada em `lookup` (linha 75).

```python
[
    {"$vectorSearch": {
        "index": "semantic_cache_vs", "path": "question", "query": question,
        "numCandidates": 50, "limit": 1,
        "filter": {"area": {"$in": ["global", area]}},
    }},
    {"$project": {"question": 1, "answer": 1, "model": 1, "area": 1,
                  "score": {"$meta": "vectorSearchScore"}}},
]
```

O que faz: acha a pergunta semanticamente mais próxima já cacheada, respeitando a área do usuário (mais FAQs globais). Score ≥ `hit_threshold` (config viva em `ai_brain.cache_config`, calibrado, default 0.7617) → HIT, resposta servida sem chamar o LLM.

Por que existe: reduzir custo/latência — pergunta reformulada ("como peço reembolso?" vs "quero um estorno") ainda bate no cache porque a busca é semântica, não string match.

Fallback em cascata se o índice não tiver o campo `filter` ainda: `with_filter=False` (25 candidatos, pós-filtro em Python) → se o índice nem existir: `_exact_fallback` (match exato por `question_norm`, `cache.py:153`).

### 3.2 Denylist semântico do guardrail — `backend/guardrails.py:125-140` (`_semantic_denylist`)

```python
[
    {"$vectorSearch": {
        "index": "guardrail_denylist_vs", "path": "phrase", "query": text,
        "numCandidates": 30, "limit": 1,
        "filter": {"area": {"$in": ["global", area]}},
    }},
    {"$project": {"phrase": 1, "category": 1, "area": 1,
                  "score": {"$meta": "vectorSearchScore"}}},
]
```

O que faz: compara a mensagem de entrada contra frases proibidas seedadas por intenção (pedir dado de outro cliente, prompt injection, conselho de investimento etc.). Score ≥ `denylist_threshold` (política por área em `ai_brain.guardrail_policies`) → bloqueia.

Por que existe: bloqueio por intenção, não por palavra-chave — a mesma pergunta reescrita ainda bate.

Efeito colateral notável: score entre `threshold - 0.05` e `threshold` vira "near-miss", gravado em `POC.guardrail_candidates` (fila de revisão humana — ADR-004), sem bloquear o turno.

### 3.3 Memória de longo prazo — retrieval vetorial — `backend/memory.py:135-153` (`_vector_candidates`)

```python
[
    {"$vectorSearch": {
        "index": "agent_memory_vs", "path": "fact", "query": query,
        "numCandidates": 100, "limit": 5,
        "filter": {"user_key": user_key, "active": True},
    }},
    {"$project": {"fact": 1, "category": 1, "created_at": 1, "active": 1,
                  "superseded_by": 1, "score": {"$meta": "vectorSearchScore"}}},
]
```

O que faz: dado o texto do turno atual, acha os 5 fatos mais relevantes deste usuário (pré-filtrados por `user_key` + `active` no próprio índice — nunca vaza fato de outro usuário nem fato desativado). Metade semântica do retrieval híbrido (ver 3.4/3.5).

Por que existe: "a memória do agente é uma query" — carregar memória vira um `$vectorSearch` pré-filtrado pela pergunta do turno, em vez de despejar tudo no prompt e estourar tokens conforme a memória cresce.

### 3.4 Memória de longo prazo — retrieval lexical (BM25) — `backend/memory.py:156-177` (`_bm25_candidates`)

```python
[
    {"$search": {
        "index": "agent_memory_bm25",
        "compound": {
            "must": [{"text": {"query": query, "path": "fact"}}],
            "filter": [
                {"equals": {"path": "user_key", "value": user_key}},
                {"equals": {"path": "active", "value": True}},
            ],
        },
    }},
    {"$limit": 5},
    {"$project": {"fact": 1, "category": 1, "created_at": 1, "active": 1,
                  "superseded_by": 1, "score": {"$meta": "searchScore"}}},
]
```

O que faz: metade lexical (BM25) do retrieval híbrido — mesmo isolamento (`user_key`+`active`) dentro do índice.

### 3.5 Fusão híbrida — `backend/memory.py:180-199` (`_rrf_fuse`)

Não é um pipeline Mongo, é pós-processamento em Python: Reciprocal Rank Fusion (`score = Σ 1/(k + posição)`, `k=60`) combinando os rankings vetorial e BM25 sem calibrar escalas de score entre os dois. Chamado por `load_relevant` (`memory.py:202`), que decide o `mode`: `all` (poucos fatos, sem busca), `hybrid` (RRF), `vector` (BM25 indisponível), `recent` (índice ausente — nunca quebra a demo).

### 3.6 Catálogo de produtos (substituição) — `backend/agent.py:357-363`

Não é chamado direto pelo modelo: o modelo pede `aggregate` em `produtos_vector` com um texto de busca, e o servidor **reescreve o pipeline inteiro** (`_read_denial`, `agent.py:326-364`) antes de repassar ao MCP:

```python
[
    {"$vectorSearch": {
        "index": "produtos_vector", "path": "descricao",
        "query": query.strip(), "numCandidates": candidates, "limit": limit,  # limit clampado 1-3
    }},
    {"$project": {"nome": 1, "preco": 1, "_id": 0}},
]
```

Por que reescrito: o modelo alterna entre `"query": "texto"` e `"query": {"text": "texto"}` — normalizado no servidor; índice/collection/campos são sempre os fixos, nunca o que o modelo mandou; `limit` clampado em 3, `numCandidates` em 100.

### 3.7 Calibração de threshold — `backend/calibrate_thresholds.py:74-88` (`top_score`)

Mesmo pipeline do 3.1/3.2 (`$vectorSearch` com o mesmo filtro de área), rodado contra pares de probe rotulados (`should_match`, texto, área) pra medir o score real em vez de escolher threshold na mão. Roda-se de novo depois de trocar modelo de embedding, tier do Atlas, índice vetorial ou dados seedados.

---

## 4. `$graphLookup` — travessia da cadeia de trocas (`backend/graph.py`)

`build_order_chain_pipeline` (`graph.py:20-47`):

```python
[
    {"$match": {"order_id": order_id, "owner_user_key": owner_user_key}},
    {"$graphLookup": {
        "from": "support_orders",
        "startWith": "$replacement_order_id",
        "connectFromField": "replacement_order_id",
        "connectToField": "order_id",
        "as": "chain",
        "maxDepth": 6,
        "depthField": "depth",
        "restrictSearchWithMatch": {"owner_user_key": owner_user_key},
    }},
    {"$project": {
        "_id": 0, "order_id": 1, "product_name": 1, "sku": 1, "status": 1,
        "chain": {"$map": {
            "input": {"$sortArray": {"input": "$chain", "sortBy": {"depth": 1}}},
            "as": "link",
            "in": {"order_id": "$$link.order_id", "product_name": "$$link.product_name",
                   "sku": "$$link.sku", "status": "$$link.status", "depth": "$$link.depth",
                   "reason": "$$link.replacement_reason"},
        }},
    }},
]
```

O que faz: um pedido trocado gera um pedido de reposição ligado ao anterior por `replacement_order_id` (ex.: `PED-1005 → PED-1006 → PED-1007`, mesmo produto reposto duas vezes). Nenhum documento sozinho responde "quantas vezes já foi reposto?" — o número de saltos não é conhecido de antemão. `$graphLookup` faz o loop inteiro dentro do servidor, numa agregação só, em vez de N idas ao banco.

Por que existe: reposição repetida do MESMO produto é sinal de defeito de lote, não azar do cliente — o agente precisa saber ANTES de prometer mais uma troca de rotina.

Isolamento: `owner_user_key` é reamarrado no `$match` inicial **e** em `restrictSearchWithMatch` a cada salto — um pedido de outro usuário nunca é alcançável, nem por um `replacement_order_id` mal preenchido.

Onde é chamado: exclusivamente via reescrita server-side em `agent.py:_read_denial` (linha ~310-325) — o modelo nunca escreve um `$graphLookup`; ele só manda um `order_id` escalar num `$match`, e o servidor descarta tudo o mais e monta o pipeline canônico.

Pós-processamento: `graph.py:summarize_order_chain` (linha 50-71) transforma a cadeia crua em sinais de negócio (`replacements`, `same_sku_count`, `recurring_defect`, `needs_quality_review` — recorrente se `same_sku_count >= 3`). O resultado cru **não** vai pro modelo — `agent.py:_summarize_chain_text` (linha 737-761) já entrega o resumo como `tool_result`, poupando contexto e evitando o modelo somar elos errado.

---

## 5. Finds nomeados (leitura direta, sem agregação)

| Onde (arquivo:linha) | Collection | Filtro | O que faz / por que |
|---|---|---|---|
| `memory.py:101-108` (`_active_docs`) | `POC.agent_memory` | `{user_key, active: True}`, sort `created_at desc`, limit | Todos os fatos ativos de um usuário — usado pelo painel de inspeção e como fallback "mode: all/recent" |
| `memory.py:124-130` | `POC.agent_memory` | `{user_key, active: False}`, sort `updated_at desc`, limit 20 | Histórico de fatos desativados (supersessão) — trilha auditável do que o agente "deixou de acreditar" |
| `memory.py:343-347` (`extract_and_store`) | `POC.agent_memory` | `{_id: {$in: fact_ids}, user_key, active: True}` | Recarrega os documentos completos dos fatos candidatos a serem substituídos, antes da extração LLM decidir `replaces` |
| `cache.py:194-196` (`recent`) | `POC.semantic_cache` | `{}`, sort `created_at desc`, limit | Painel "inspecionar cache" |
| `guardrails.py:326-330` (`list_candidates`) | `POC.guardrail_candidates` | `{status}` opcional `{area}`, sort `at desc` | Fila de near-miss para revisão humana |
| `guardrails.py:382` (`recent_events`) | `POC.guardrail_events` | `{area}` opcional, sort `at desc` | Audit log de guardrail (allow/block/mask) |
| `guidance.py:42` | `POC.support_orders` | `{owner_user_key}` | Anexo de orientação: lista pedidos reais do usuário quando uma busca volta vazia (evita o modelo inventar número de pedido) |
| `main.py:420` | `ai_brain.prompt_templates` | `{}` | Aba 1 — lista templates para a UI |
| `main.py:912` | `POC.guardrail_denylist` | — | Lista frases do denylist pro painel |
| `profiles.py:26-30` | `POC.app_users`, `ai_brain.area_profiles` | `{}` | Lista usuários/áreas da demo |
| Agente via MCP, reescrito em `agent.py:280-308` (`_read_denial`) | `POC.support_orders` | `{order_id, owner_user_key}` (remontado), projeção `ORDER_FIELDS_FOR_AGENT` (sem PII) | Único `find` que o modelo consegue disparar em pedidos — exige `order_id` escalar `PED-\d{4,12}` |
| Agente via MCP | `POC.agent_sessions` | `{session_id: conversation_id, user_key}` (remontado), projeção `{turns: 1}` | Recuperar histórico COMPLETO da sessão quando o cliente pede consolidação — a janela recente já vem no contexto |

## 6. `update-many` — a única escrita que o agente consegue disparar

`agent.py:_write_denial` (linha 230-257). Reescreve o input inteiro: exige `database.collection` == `POC.support_orders`, um `order_id` escalar (regex `PED-\d{4,12}`) no filtro, e um status de `ALLOWED_ORDER_STATUSES = {"reembolso_solicitado", "troca_solicitada", "chamado_aberto"}`. `owner_user_key` é reamarrado no filtro final — o agente só altera pedido do próprio usuário do turno.

## 7. TTL indexes (expiração nativa, zero cron)

Todos em `backend/seed.py:1076-1104`.

| Collection.campo | `expireAfterSeconds` | O que expira / por quê | ADR |
|---|---|---|---|
| `semantic_cache.expires_at` | `0` (expira na data marcada em `expires_at`) | Entradas de runtime do cache — FAQs seedadas não têm o campo, nunca expiram | — |
| `agent_sessions.updated_at` (`ttl_updated_at_24h`) | `86400` (24h) | Memória de CURTO prazo — sessão sem novo turno em 24h some sozinha; `updated_at` é tocado a cada turno, então sessão ativa nunca expira em uso | **ADR-002** |
| `guardrail_events.at` (`ttl_at_30d`) | `2592000` (30 dias) | Audit log de guardrail | **ADR-003** |
| `agent_traces.at` (`ttl_at_30d`) | `2592000` (30 dias) | Trace de cada turno do agente | **ADR-003** (mesmo padrão) |
| `guardrail_candidates.at` (`ttl_at_30d`) | `2592000` (30 dias) | Fila de near-miss pendente de revisão | **ADR-003** |
| `admin_audit.at` (`ttl_at_30d`) | `2592000` (30 dias) | Auditoria de ação administrativa (reset, review) | **ADR-003** (mesmo padrão) |

## 8. Índices regulares (não-vetoriais)

`backend/seed.py:1027-1060`:

| Collection | Índice | Por que |
|---|---|---|
| `agent_sessions` | `session_id` único | Upsert concorrente no mesmo `session_id` não pode duplicar sessão |
| `agent_memory` | `{user_key: 1, active: 1, created_at: -1}` (`user_active_created_desc`) | Cobre filtro + sort da leitura quente `_active_docs` (substitui índice legado `{user_key, active}` que deixava o sort em memória) |
| `agent_memory` | `{user_key: 1, active: 1, fact_norm: 1}` | Dedupe exato de fato (checa `fact_norm` contra a memória COMPLETA, não só os candidatos do retrieval) |
| `agent_traces` | `{conversation_id: 1, at: -1}` | Trace de um turno específico, mais recente primeiro |
| `app_users` | `user_key` único | Identidade da demo |
| `support_orders` | `order_id` único | Query quente do agente (leitura por `order_id`) nunca faz collection scan |
| `support_orders` | `{owner_user_key: 1, replacement_order_id: 1}` | `$graphLookup` casa `replacement_order_id → order_id` a cada salto — sem este índice a travessia vira collection scan por salto |
| `guardrail_candidates` | `{status: 1, at: -1}` | Fila de revisão consultada por status, ordenada por data |
| `semantic_cache` | `{question_norm: 1, area: 1}` | Fallback exato quando o índice vetorial está indisponível — sem isso vira COLLSCAN |

---

## Nota sobre a escala do score

`vectorSearchScore` não é uma constante confiável entre atualizações de índice/modelo — já mudou de ~0.50 para ~0.59–0.86 neste cluster em agosto de 2026. O ranqueamento é confiável; a escala absoluta não. Por isso nenhum threshold é fixado no código: cache e guardrail leem de `ai_brain.cache_config`/`guardrail_policies`, escritos por `calibrate_thresholds.py` contra probes rotulados — rodar de novo após qualquer mudança de embedding/índice/tier/seed.
