# UI — telas, fluxos e componentes

> Front em `frontend/` (React 18 + Vite + LeafyGreen, JS puro, sem router/estado externo). `fetch` cru embrulhado em `frontend/src/api.js`. Proxy `/api` → backend `:8010`. Frontend sobe em `:5183`.

## Regra que governa toda a UI

**Nada de valor decidido no cliente.** Área, política, modelo, escopo de cache — tudo vem do backend, derivado do token de auth. O React só exibe.

## Estado global (`App.jsx`)

O estado das três abas vive em `App.jsx` e desce por props — as abas não são donas do próprio estado. Motivo: a demo volta e avança entre abas o tempo todo (mostra schema, troca modelo, roda agente, volta ao schema); se cada aba desmontasse ao trocar, a narrativa quebraria a cada retorno.

## As três telas

### Aba 1 — Schema flexível (`tabs/FlexibleSchema.jsx`)

Screenshot: `docs/img/tab1-schema-flexivel.png`.

Mostra o documento **antes e depois** de um `$set` em `ai_brain.prompt_templates` — templates de prompt como documentos polimórficos, formatos diferentes convivendo na mesma coleção. Adicionar uma variante de modelo é um `$set` ao vivo, sem migração/ALTER TABLE. Tem um flash visual marcando o instante em que o documento muda no projetor — sem isso o `$set` passa despercebido.

### Aba 2 — Troca de modelo e custo (`tabs/ModelSwap.jsx`)

Screenshot: `docs/img/tab2-model-swap.png`.

Faz uma pergunta, mostra o custo. Troca `ai_brain.model_config` com um `update_one` (Sonnet ↔ Haiku), refaz a pergunta — o custo muda, nenhum deploy aconteceu. `backend/llm.py` lê esse documento a cada chamada.

### Aba 3 — Agente (`tabs/Agent.jsx`, 976 linhas — a maior do projeto)

Screenshots: `docs/img/tab3-agent.png`, `docs/img/tab3-cache-hit.png`.

É onde o pipeline inteiro do turno vira visível: guardrail, máscara de PII, cache, memória, chamadas de ferramenta MCP (com a versão pedida pelo modelo e a versão **reescrita** lado a lado), e o trace.

Componentes de apoio:

| Componente | Arquivo | O que faz |
|---|---|---|
| `JsonViewer` | `components/JsonViewer.jsx` | Documento cru formatado — sustenta "polimórfico" como afirmação verificável |
| `PipelineSteps` | `components/PipelineSteps.jsx` | Etapas do turno em ordem (Perceive → Retrieve → Reason → Act → Store → Loop), o pipeline virando imagem |
| `QueryDetails` | `components/QueryDetails.jsx` | Detalhe de uma chamada de ferramenta específica |
| `ReplacementChain` | `components/ReplacementChain.jsx` (84 linhas) | Renderiza a cadeia de trocas quando o `$graphLookup` rodou no turno — procura no trace a chamada `aggregate` cujo pipeline contém `$graphLookup` e desenha `PED-1005 → PED-1006 → PED-1007`, com contadores e veredito |

Detalhe de UX relevante: o `ReplacementChain` lê o estado `visible` do replay (não `events` bruto), então respeita tanto o replay passo-a-passo quanto o Tour guiado — só aparece no momento exato em que a travessia acontece, não antes.

Badge da coluna MongoDB mostra o **estágio** (`$vectorSearch` / `$graphLookup`), não o nome cru da ferramenta MCP (`aggregate` cobre os dois casos e ficariam indistinguíveis na tela) — o `opLabel` deriva o rótulo do pipeline que o servidor efetivamente montou, então se a reescrita mudar, o rótulo muda junto em vez de mentir.

Estados de UI relevantes em `Agent.jsx` (via `useState`): `scenarios` (chips por área), `tools` (ferramentas MCP realmente disponíveis nesta sessão), `users`, `liveStatus` (passo em andamento durante streaming, em vez de spinner genérico), `playlist`/`demo` (auto-demo), `tokensSaved` (contador de tokens evitados por cache hit), `showInspector` (painel de inspeção cache/memória/guardrail).

## Fluxo do usuário na aba Agente

1. Escolhe um usuário no seletor de identidade — isso chama `POST /api/auth/token`; a partir daí toda requisição carrega o token, e a área do usuário sai da **claim do token**, nunca do payload. É o que garante que isolamento de cache/política por área é real (não um filtro que dá pra mudar no DevTools).
2. Clica um chip de pergunta sugerida — **ninguém digita pergunta ao vivo na demo**. Os chips vêm filtrados pela área do usuário e referenciam pedidos do próprio usuário (senão o primeiro clique já bate no isolamento e devolve vazio). Catálogo de chips: `AREA_SCENARIOS` em `backend/agent.py`.
3. `POST /api/agent/run/stream` roda o turno e entrega cada evento de `emit()` via Server-Sent Events assim que é gerado — a UI mostra o passo em andamento ("chamando ferramenta X...") em vez de esperar o trace inteiro (até `AGENT_TURN_TIMEOUT_SECONDS`, 120s). Consumido via `fetch` + `ReadableStream` em `api.js:agentRunStream` (não `EventSource`, que não suporta POST/headers de auth).
4. `POST /api/agent/run` (sem streaming) segue existindo para scripts/smoke test.
5. Botão "▶ Demo automática" roda `DEMO_PLAYLIST` (`backend/agent.py`) — script narrativo que troca de identidade ao vivo, demonstrando memória por usuário e persona/guardrail/cache por área na sequência.

## Endpoints de inspeção e reset (essenciais pra demo repetível)

Listados em `backend/main.py`. Os principais, por o que resolvem:

| Endpoint | Resolve |
|---|---|
| `GET/DELETE /api/cache` | Inspecionar / limpar cache semântico |
| `GET/DELETE /api/memory/{user_key}` | Inspecionar / limpar memória de longo prazo |
| `GET /api/memory-short/{conversation_id}` | Inspecionar memória de curto prazo (sessão) |
| `GET /api/guardrails/policy`, `/rules`, `/events`, `/candidates` | Política ativa, regras, audit log, fila de near-miss |
| `POST /api/guardrails/candidates/{id}/review` | Aprovar/rejeitar promoção de near-miss ao denylist |
| `GET /api/agent/scenarios` | Chips por área |
| `GET /api/agent/playlist` | Roteiro da auto-demo |
| `GET /api/agent/tools` | Ferramentas MCP realmente disponíveis na sessão ativa |

Os botões de limpar cache/memória são o que permite repetir a mesma demo do zero na frente do próximo cliente — sem eles, o segundo turno bate no cache e o efeito (miss → LLM real) some. Toda ação administrativa grava documento de auditoria (`admin_audit`, TTL 30 dias) com IP e detalhes, e tem rate limit por identidade.

## Roteiro de apresentação (o que precisa acontecer, na ordem)

1. Aba 1 — dois templates de prompt convivendo na mesma coleção; `$set` ao vivo.
2. Aba 2 — pergunta, custo, `update_one` trocando modelo, repete pergunta, custo muda.
3. Aba 3, primeira pergunta — loop MCP roda, trace mostra query pedida vs. reescrita lado a lado.
4. Repete a mesma pergunta — cache HIT, zero chamada de LLM.
5. Pergunta personalizada, puxa memória de longo prazo — mostra que essa resposta NÃO entra no cache.
6. Informa preferência, contradiz no turno seguinte — mostra supersessão (fato antigo com `superseded_by`, não apagado).
7. Troca de identidade — a mesma pergunta não recupera a memória do usuário anterior (pré-filtro no índice).
8. Pede pedido de outro usuário — volta vazio, não "negado" (não vaza existência do documento).
9. Tenta induzir query ampla / escrita na própria memória — negação acontece ANTES do MCP, no reescritor.
10. Troca pra área Financeiro — threshold mais rígido, comportamento fail-closed.

Passos 5–9 são os que ganham a conversa contra "isso eu faço com uma extensão vetorial no meu banco atual" — não é sobre o vetor, é sobre onde a política vive e quem consegue auditar.

## Antes de apresentar (checklist operacional)

- `python seed.py` se pedidos foram mexidos em ensaio (restaura status **e invalida cache de runtime**) — nunca rodar durante apresentação ao vivo.
- Cache e memória limpos pelos endpoints de reset entre um cliente e outro.
- Uma pergunta de catálogo de aquecimento (exercita `$vectorSearch` via MCP — caminho com mais peças no meio, primeira agregação de uma sessão MCP custa ~5s vs. ~650ms nas seguintes; ver `agent.py:warm_up_session`).
- `calibrate_thresholds.py` rodado depois de qualquer mudança de índice/embedding.
- Conferir a listagem de ferramentas MCP disponíveis (`GET /api/agent/tools`) antes de começar — sessão MCP viva.
