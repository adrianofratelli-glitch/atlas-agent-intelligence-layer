# Por que MongoDB é a camada de memória de um agente

Documento de argumentação, com **números medidos nesta PoV** (22/09/2026) em vez de adjetivos.
Cada afirmação aponta para o arquivo que a implementa e para o teste/relatório que a mede.

## A tese

Um agente precisa de cinco coisas para lembrar, e a indústria costuma resolver com cinco
produtos: banco relacional para o estado, banco vetorial para a busca semântica, cache
(Redis) para respostas repetidas, um serviço de feature/flags para a configuração viva e um SaaS
de observabilidade para o trace. **Nesta PoV as cinco são o mesmo cluster MongoDB**, e a
consequência prática não é economia de licença — é que **memória, dado de negócio e política
vivem na mesma transação, no mesmo documento e na mesma consulta**.

| O que o agente precisa | Onde vive aqui | Onde vive no stack "normal" |
|---|---|---|
| Memória curta (conversa) | `POC.agent_sessions` — um documento por sessão, `turns[]` com `$slice` | Postgres + cache |
| Memória longa (fatos) | `POC.agent_memory` — `$vectorSearch` + BM25 fundidos com RRF | Banco vetorial à parte |
| Respostas repetidas | `POC.semantic_cache` — busca vetorial com TTL | Redis + embeddings à parte |
| Configuração viva (modelo, limiares, persona, política) | `ai_brain.*` — lido a cada turno | Arquivo de config + deploy |
| Trace/auditoria | `POC.agent_traces`, `POC.guardrail_events`, `observability.spans` | SaaS de observabilidade |

## Os cinco argumentos, com a medição

### 1. Isolamento multi-tenant é PRÉ-FILTRO do índice, não `WHERE` da aplicação

`user_key`, `area` e `active` são campos do tipo `filter` **dentro** dos índices vetoriais
(`agent_memory_vs`, `semantic_cache_vs`, `guardrail_denylist_vs`). O `$vectorSearch` só percorre
vetores que aquele usuário pode ver — o isolamento acontece na busca ANN, não depois dela.

Por que importa: num banco vetorial separado, o filtro por tenant é responsabilidade do código
que chama. Um `if` esquecido vira vazamento entre clientes. Aqui, esquecer é impossível: o índice
não devolve o que não é do tenant.

Medido: `backend/tests/test_policies.py` (`CacheIsolationFallbackTests`), e o eval live com
**28 casos e `cache_leaks: 0`** (`eval/reports/live-2026-09-22.json`).

### 2. Recuperação híbrida sem costurar dois sistemas

`memory.load_relevant` dispara `$vectorSearch` (semântica) e `$search` BM25 (lexical) **sobre os
mesmos documentos**, e funde os rankings com Reciprocal Rank Fusion. Um é bom em "prefere ser
contatado à noite" ≈ "não ligar durante o dia"; o outro em SKU, nome próprio e código exato.

Por que importa: com banco vetorial separado, híbrido significa manter dois índices, dois
pipelines de escrita e reconciliar identidade de documento entre eles.

Medido: recall@3 de **0,875** entre sessões, com `retrieval_modes: ["hybrid"]` no relatório
(`eval/reports/memory-atlas-2026-09-22.json`) — e o relatório **prova** qual caminho respondeu em
vez de afirmar.

### 3. O framework de memória não ganha da implementação nativa o suficiente para valer a troca

Comparação real, mesmos 8 fatos, sessões diferentes, banco isolado, perguntas que não repetem as
palavras do fato:

| | Atlas nativo | Mem0 2.1.0 + vector store MongoDB |
|---|---|---|
| Recall@3 | 0,875 | 1,000 |
| Escrita p50 / p95 | 258 ms / 2941 ms | 399 ms / 905 ms |
| Leitura p50 | 1210 ms | 1060 ms |
| Embedding | `autoEmbed` voyage-4 **no servidor** | fastembed **no cliente** |

O Mem0 ganha em recall nesta amostra — e ainda assim a recomendação é **manter o nativo**, porque
o que ele não traz está tudo do lado de cá: supersessão auditável (o fato antigo vira
`active: false` com `superseded_by`, numa transação ACID junto com o novo), deduplicação,
teto de fatos ativos, isolamento como campo de índice e a defesa contra envenenamento de memória.
Trocar significaria reimplementar isso fora do banco. Detalhe e ressalvas em
[`eval-report.md`](eval-report.md).

### 4. Memória é dado, nunca instrução — e isso é verificável

Fatos recuperados entram no prompt entre delimitadores `<fatos_do_cliente>` com instrução
explícita de ignorar comandos embutidos, e `memory.looks_like_instruction` descarta, de forma
determinística, qualquer "fato" em formato de instrução que o extrator devolva.

Por que importa: memória de agente é superfície de ataque. Quem guarda memória num store opaco
não consegue nem auditar o que foi gravado; aqui o fato é um documento com origem
(`source_session`), histórico e trilha de supersessão.

Medido: `backend/tests/test_memory_extractor.py`
(`test_instruction_shaped_facts_are_rejected_even_if_the_model_returns_them`).

### 5. O modelo documental absorve o estado do turno sem um segundo sistema

O checkpoint de turno em andamento (`agent.open_turn`) é **um `$set` no mesmo documento** da
memória curta, e é fechado pela própria gravação do turno. Sem tabela de estado à parte, sem
coordenação entre dois bancos, sem transação distribuída.

Medido: cenário de caos `crash_mid_tool` — `SIGKILL` dentro de uma chamada de ferramenta deixa
`pending_turn.status = "in_progress"` na sessão, nenhum turno meio-escrito, e a mesma conversa
segue utilizável. O turno SEGUINTE na mesma conversa detecta o checkpoint aberto, avisa no trace
("turno anterior foi interrompido... retomando normalmente") e o limpa — sem isso ficaria
`in_progress` para sempre. Cenário `stale_checkpoint_recovery`
([`chaos-report.md`](chaos-report.md)).

## O que o cache semântico faz pela conta do cliente

Cache hit é resposta servida **do MongoDB**, sem chamar o LLM: na rodada de eval, 2 de 28 turnos
foram servidos assim. O custo total dos 28 turnos foi **US$ 0,3964**, com o ledger por chamada
(`backend/gateway.py`) dizendo quanto cada uma custou — e `cost_complete: true` garantindo que
nenhuma tarifa faltante virou zero.

O gate que torna isso seguro é o que impede o cache de virar vazamento: turno que tocou dado de
pedido — na pergunta **ou** na resposta — nunca entra no cache compartilhado
(`agent.transactional_turn`). Foi um vazamento real, medido e corrigido nesta rodada
([`eval-report.md`](eval-report.md), achado 2).

## Capacidade medida (M10/M20, 22/09/2026)

`backend/scripts/load_test.py --mode data` mede a camada de dados de um turno (guardrail com
`$vectorSearch`, cache, memória híbrida, classificador e escrita de sessão) — sem LLM e sem MCP,
ou seja: mede o Atlas.

| Turnos simultâneos | p50 | p95 | Falhas |
|---|---|---|---|
| 1 | 4,7 s | 7,1 s | 0 |
| 10 | 4,6 s | 12,9 s | 0 |
| 20 | ~8 s | ~17–22 s | 0 (borda) |
| 30 | 12,0 s | 30,3 s | 1 (`maxTimeMS`, degradação correta) |

Leitura honesta: **o turno faz quatro buscas vetoriais com `autoEmbed`** (denylist, cache,
memória, classificador) e o embedding no servidor domina o tempo. O M10 sustenta ~10 turnos
simultâneos com folga; a partir de ~30 o `maxTimeMS` começa a cortar — e corta do jeito certo,
com mensagem de UI em vez de stack trace. Para demo com público ou piloto, as alavancas são,
nesta ordem: subir o tier, reduzir de quatro para duas buscas por turno, ou paralelizar as
independentes. Nenhuma delas foi aplicada nesta rodada — o número existe para a decisão ser
tomada com dado.

## Como demonstrar isso ao vivo em 4 minutos

1. **Troca de modelo** (Aba 2): `update_one` em `ai_brain.model_config`, o próximo turno já usa o
   outro modelo. Configuração é documento, não deploy.
2. **Cache hit** (Aba 3): repetir uma FAQ e mostrar o turno resolvido sem chamada de LLM, com o
   card de economia.
3. **Memória entre sessões**: dizer "meu orçamento é R$ 800", abrir nova conversa e pedir uma
   recomendação — o teto vira **pré-filtro nativo** na busca de catálogo, não instrução de prompt.
4. **Cadeia de trocas**: perguntar "quantas vezes esse pedido já foi reposto?" e mostrar o
   `$graphLookup` resolvendo a travessia dentro do servidor, numa agregação.
5. **Resiliência** (se houver tempo): `CHAOS=1 .venv/bin/python scripts/chaos_suite.py` ao vivo —
   12 cenários, incluindo MCP fora do ar e `mongot` indisponível, todos com o turno terminando.

Antes de qualquer uma delas: `./scripts/preflight.sh`.
