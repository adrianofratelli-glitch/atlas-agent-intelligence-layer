# Relatório de caos — `feat/resilience-observability` (22/09/2026)

A bateria está em `backend/scripts/chaos_suite.py` e roda de verdade; os mesmos cenários são
regressão permanente em `backend/tests/test_chaos.py` (só com `CHAOS=1`).

```bash
cd backend && CHAOS=1 .venv/bin/python scripts/chaos_suite.py                  # bateria offline
cd backend && CHAOS=1 LIVE=1 .venv/bin/python scripts/chaos_suite.py           # inclui Atlas real
cd backend && CHAOS=1 .venv/bin/python -m unittest tests.test_chaos -v          # como regressão
```

A degradação graciosa do turno, o teto por chamada de ferramenta e o circuit breaker por tool são
o comportamento **PADRÃO** — a bateria não liga flag nenhuma para obtê-los. A única flag que
aparece aqui é `SINGLEAGENT_LEGACY_500=1`, no cenário que prova o modo antigo.

A injeção fica em `backend/chaos.py`, atrás de `CHAOS=1`: pontos de falha no caminho REAL
(`agent._create_with_retry` para o LLM, `resilience.call_tool` para a fronteira de tool, e o
retorno da tool para payload corrompido), não mocks espalhados pelos testes. Sem a variável, cada
ponto é uma leitura de ambiente e um `return`.

Os cenários offline usam sessão MCP falsa e cliente de LLM falso, mas executam o loop REAL
(`agent._run_tool_loop`, `agent.run_loop_guarded`). Os cenários `LIVE=1` rodam contra o Atlas, no
banco de TESTE isolado (`POC_test`/`ai_brain_test`, `backend/scripts/isolation.py`) — nunca o da
demo, que é recusado sem `ALLOW_DEMO_DB_WRITE=1`.

## Resultado (última execução: 20/20)

| Cenário | O que injeta | Assertion | Resultado |
|---|---|---|---|
| `tool_timeout` | chamada MCP pendurada 30s, `TOOL_TIMEOUT_SECONDS=1` | turno termina em <5s, resultado honesto de "sem dado" | PASS (1,83s) |
| `mcp_session_down` | `session.call_tool` levanta exceção | turno responde; o modelo recebe "não há dado… sem supor nenhum valor" | PASS (0,00s) |
| `tool_malformed_payload` | tool devolve texto que não é JSON | nenhuma exceção, e o payload torto **não** vira "nenhum documento" | PASS (0,00s) |
| `tool_circuit_breaker` | tool falhando sem parar | abre em 4 falhas e curto-circuita a 5ª | PASS (0,00s) |
| `llm_429_before_first_token` | 429 na primeira tentativa | retry no mesmo modelo absorve; turno responde normal | PASS (3,00s) |
| `llm_500_persistent` | 500 em toda tentativa (retries + fallback) | `run_loop_guarded` devolve resposta degradada; nenhuma exceção sobe | PASS (3,01s) |
| `legacy_500_flag` | a MESMA falha com `SINGLEAGENT_LEGACY_500=1` | a exceção volta a subir (prova que o default novo é o que degrada) | PASS (0,00s) |
| `concurrent_tool_calls` | 5 turnos simultâneos | 5 respostas, nenhuma exceção, contadores por turno íntegros | PASS (0,00s) |
| `turn_timeout` | LLM travado, `AGENT_TURN_TIMEOUT_SECONDS=1` | corta em ~1s com `degraded_reason=turn_timeout` | PASS (1,00s) |
| `live_degraded_turn` | `run_agent` inteiro com o MCP falhando (Atlas real) | resposta degradada + trace completo + turno gravado, sem exceção | PASS (7,94s) |
| `crash_resume` | `SIGKILL` no processo depois de gravar o turno | a sessão continua legível em `agent_sessions` (2 turnos persistidos) | PASS (6,61s) |
| `crash_mid_tool` | `SIGKILL` **dentro** de uma chamada de ferramenta pendurada | checkpoint `pending_turn` fica na sessão, nenhum turno meio-escrito, conversa segue utilizável | PASS (25,7s) |
| `atlas_retry_semantics` | — (inspeção do cliente) | `retryWrites`/`retryReads` declarados + orçamento CSOT por operação: quem cobre o step-down é o DRIVER | PASS |
| `atlas_failover` | `NotPrimaryError` (código 10107 + label `RetryableWriteError`) sobrevivendo ao retry | vira `SafeQueryError` de conexão e o turno degrada com trace inteiro | PASS |
| `search_unavailable` | `mongot` fora / índice vetorial ausente | `SafeQueryError` kind `search`; cache cai para match exato, memória para fatos recentes | PASS |
| `guardrail_fails_closed` | denylist vetorial indisponível | `semantic_fail_mode` do DOCUMENTO decide: `closed` bloqueia, `open` atende — sem deploy | PASS (4,9s) |
| `mcp_pool_round_robin` | 1 de 3 slots do pool caído | round-robin nunca devolve o slot morto; pool inteiro caído devolve `None`, sem exceção | PASS |
| `mcp_pool_slot_isolation` | 1 slot pendurado + 2 sadios, 6 turnos simultâneos | os 6 terminam pelos slots vivos dentro do teto por tool (2,0s), sem esperar o travado | PASS (2,0s) |
| `mcp_supervisor_reconnects` | sessão do slot caindo no ping | slot vira `None` com o erro registrado e o supervisor reabre sozinho; vizinho intacto | PASS (0,15s) |
| `stale_checkpoint_recovery` | checkpoint `pending_turn` deixado por um crash anterior na mesma conversa | o próximo turno detecta, anuncia a retomada no trace e limpa o checkpoint | PASS (5,9s) |

## Modos de falha do Atlas que a bateria cobre

Três cenários novos existem porque esta PoV **vende o Atlas**, e resiliência de banco não pode
ficar só no slide:

* **Step-down de primário.** O que segura não é código de aplicação, é o driver: `retryWrites` e
  `retryReads` estão declarados explicitamente em `db.py` (embora sejam default) e verificados por
  `atlas_retry_semantics`. Durante a eleição, a operação é reexecutada no novo primário e o turno
  não vê nada. `atlas_failover` cobre o caso em que o retry TAMBÉM esgota: vira mensagem de UI e
  turno degradado, nunca stack trace.
  O exercício com failover REAL fica em `scripts/atlas_failover_drill.py`, atrás de duas travas —
  `restartPrimaries` é operação de CLUSTER INTEIRO e este cluster é compartilhado com outras PoVs.
* **`mongot` indisponível.** `search_unavailable` injeta o `OperationFailure` real do PlanExecutor
  e confere o mapeamento para `SafeQueryError` kind `search` — que é o que dispara o fallback de
  match exato no cache e o modo `recent` na memória.
* **Guardrail sem camada semântica.** `guardrail_fails_closed` prova que quem decide é o
  documento de política: o mesmo campo `semantic_fail_mode` bloqueia ou libera, com um
  `update_one` e sem deploy. (Esta demo roda fail-closed em TODA área — ADR-001, risco 3.)

## Bugs REAIS revelados e corrigidos

0. **Nome de banco de teste acumulava sufixo entre cenários — a bateria achava a própria
   bateria.** `scripts/isolation.test_database_names()` derivava do valor ATUAL de
   `os.environ["MONGODB_DB"]`, mas `use_test_databases()` muta esse mesmo env var sem
   restaurar. Num processo de vida longa como a bateria completa (vários cenários no MESMO
   processo Python), a segunda chamada via `POC` → `POC_test` → `POC_test_test` → …
   O banco fantasma `POC_test_test` não tinha `app_users`, então `crash_mid_tool` falhava com
   *"Identidade de demonstração não reconhecida"* — mas SÓ quando rodava depois de outro
   cenário `LIVE` no mesmo processo, nunca sozinho. Foi reproduzido isolando a sequência exata
   (`live_degraded_turn` → cenários do pool → `crash_mid_tool`) e confirmado direto no cluster:
   `POC_test_test` existia, `POC_test` (o real) não foi afetado. Correção: os nomes de teste
   agora derivam da constante `DEMO_MAIN_DB`/`DEMO_BRAIN_DB` ("POC"/"ai_brain"), nunca do
   env var mutável — a mesma filosofia que `guard()` já usava. Regressão em
   `tests/test_policies.py:IsolationDatabaseNamingTests`.

1. **Payload corrompido do MCP era reportado ao cliente como "pedido não encontrado".**
   `agent._is_empty_order_read` decidia "resultado vazio" por uma única condição: não haver
   nenhum `PED-…` no texto de retorno. Um payload truncado, uma mensagem de erro do servidor MCP
   ou qualquer formato inesperado também não têm id — e o turno então trocava o resultado por
   *"Busca concluída: nenhum documento corresponde a esse filtro"*, ou seja, o agente **afirmava
   a inexistência do pedido a partir de um retorno que não entendeu**. Não é queda, é resposta
   errada com cara de certa — o pior tipo para uma demo de dados.
   Correção: vazio agora tem que ser reconhecível — JSON que é mesmo lista/objeto vazio, ou um
   marcador textual conhecido do MCP (`EMPTY_RESULT_MARKERS`); qualquer outra coisa segue como
   erro de ferramenta, honesto. Coberto por `tool_malformed_payload` na regressão.

2. **Nenhum teto por chamada de ferramenta.** O único limite era o deadline do turno inteiro
   (`AGENT_TURN_TIMEOUT_SECONDS`, 120s): uma chamada MCP pendurada segurava o turno por até dois
   minutos, e o cliente via um spinner. Correção: `resilience.call_tool` com
   `TOOL_TIMEOUT_SECONDS` (default **20s**, resiliente por padrão; `0` desliga). Medido: 1,83s no
   cenário com teto de 1s, contra os 30s da falha injetada.

3. **Falha do loop perdia a resposta do turno.** Qualquer exceção no loop (provedor esgotado,
   MCP morto, circuito aberto) subia para o endpoint e virava um erro HTTP: o trace inteiro, o
   raciocínio e a memória curta daquele turno iam embora junto. Correção: `agent.run_loop_guarded`
   degrada por padrão — resposta explícita, `metrics.degraded`/`degraded_reason`, trace inteiro
   preservado e turno gravado. `SINGLEAGENT_LEGACY_500=1` restaura o comportamento antigo, e o
   cenário `legacy_500_flag` existe justamente para provar que o novo é o default.

4. **Os spans do tracing não eram exportados — em silêncio.** Não é caos, foi descoberto ao ligar
   o `pov-shared`: `backend/tracing.py` e `backend/guardrails.py` sombreavam os módulos homônimos
   do `_shared` (`backend/` é `sys.path[0]`), e o exporter de mascaramento chamava a `mask_pii`
   **async** do PoV, quebrando dentro do `BatchSpanProcessor` sem derrubar nada. Correção: os
   módulos do PoV passaram a se chamar `langfuse_tracing.py` e `policy_guardrails.py` (a
   convenção que o próprio `_shared` documenta).

Nenhum dos quatro exigiu mudança de arquitetura.

## Limitações conhecidas (o que este relatório NÃO afirma)

* **Os cenários offline não exercitam o MCP real nem o provedor real.** Eles exercitam o loop
  real com as bordas falsas; quem cobre o MCP de verdade é `tests/test_mcp_contract.py` (binário
  pinado) e os cenários `LIVE=1`.
* **Crash no meio de uma tool: o estado não fica pela metade, e a conversa continua usável — mas
  o turno interrompido é PERDIDO.** Medido por `crash_mid_tool`: depois do `SIGKILL`, `agent_sessions`
  tinha 0 turnos meio-escritos (a escrita de curto prazo só acontece DEPOIS do loop, então ou o
  turno existe completo, ou não existe) e o turno seguinte na mesma conversa respondeu e gravou
  normalmente. O que esta PoV **não** tem é checkpoint por passo: a chamada de ferramenta em voo
  não é retomada, o cliente precisa repetir a pergunta. Isso é decisão de arquitetura (nenhum
  checkpointer transacional por passo), agora com o comportamento medido em vez de suposto.
* **Não há streaming do provedor neste loop**, então "falha no meio do stream" foi implementada
  como a falha equivalente: o provedor falha em todas as tentativas depois de já ter sido
  chamado (`llm_500_persistent`).
* `concurrent_tool_calls` mede concorrência no loop, não o pool de sessões MCP
  (`main.py:_mcp_supervisor`), que continua coberto só por inspeção e pelo uso ao vivo.
