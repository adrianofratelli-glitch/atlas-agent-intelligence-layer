# Formato do eval do agente único (compatível com o do multiagente)

Este arquivo é o par de `../multiagente-atendimento/eval/FORMAT.md`. O objetivo é medir a MESMA
coisa nas duas PoVs para comparar single vs multi com números, não com impressão. O que muda aqui
está listado em **Diferenças** — nada de silencioso.

## Dataset — `eval/dataset.json`

```json
{
  "name": "...", "version": 1,
  "synthetic": true,
  "generated_by": "modelo/autor e data",
  "limitation": "frase honesta sobre o que este dataset NÃO mede",
  "cases": [
    {
      "id": "single-001",
      "user_key": "cliente-demo",
      "message": "onde está meu pedido PED-1003?",
      "expected_outcome": "resolved",
      "min_tool_calls": 1,
      "requires_llm": true,
      "expect_out_of_scope": false,
      "expect_blocked": false,
      "expect_personal_turn": false,
      "notes": "por que este caso existe"
    }
  ]
}
```

| Campo | Obrigatório | Significado |
|---|---|---|
| `id` | sim | estável; é a chave de comparação entre rodadas |
| `user_key` | sim | identidade real do seed (`POC.app_users`); decide área, persona, política e escopo de cache |
| `message` | sim | a frase do cliente, como ele escreveria |
| `expected_outcome` | sim | `resolved` \| `blocked` \| `out_of_scope` — o desfecho esperado do turno |
| `min_tool_calls` | não (0) | piso de chamadas de ferramenta: cobre a consulta que deveria existir e sumiu |
| `requires_llm` | não (false) | caso sem veredito honesto offline: entra na contagem, fica fora das métricas no modo `offline` |
| `expect_out_of_scope` | não | resolvido = orientação de escopo, sem loop de ferramentas |
| `expect_blocked` | não | resolvido = bloqueio do guardrail de entrada |
| `expect_personal_turn` | não | o turno é pessoal: NÃO pode ler nem gravar o cache compartilhado |
| `expect_cache_hit` | não | o caso deve ser servido do cache semântico (sem chamada ao LLM) |
| `notes` | não | por que a expectativa é essa |

`synthetic: true` é obrigatório porque as frases foram escritas por um modelo. Dataset e agente
saem da MESMA família de modelo (Claude); isso está em `limitation` e é repetido no relatório.

## Métricas — `backend/eval_agent.py`

* **Taxa de resolução** — o turno entregou o desfecho esperado: resposta não vazia, não degradada,
  e (conforme o caso) bloqueada/orientada como esperado.
* **Chamadas de ferramenta por turno (média)** — o análogo de "handoffs por turno" do multiagente:
  é o custo de coordenação do agente único, e é o número comparável entre as duas PoVs.
* **Tokens por turno (média)** e **turnos degradados** — custo e resiliência na mesma tabela.
* **Latência p50/p95 por turno** e **custo estimado** (do ledger real de `gateway.py`, com
  `cost_complete` dizendo se TODAS as chamadas tinham tarifa conhecida — preço faltando nunca
  vira zero).
* **Isolamento de cache/memória** — `cache_leaks`: turnos pessoais que leram ou gravaram o cache
  compartilhado. Qualquer valor > 0 é falha, não estatística.

## Relatório

`--json out.json` grava `{"summary": {...}, "rows": [...]}`; `--compare antes.json` imprime o
delta campo a campo. `summary` carrega `mode` (`offline`/`live`), `database`, `scored_cases`,
`skipped_requires_llm`, as métricas acima, `synthetic` e `limitation`.

```bash
cd backend && .venv/bin/python eval_agent.py                      # offline (sem LLM, sem Atlas)
cd backend && .venv/bin/python eval_agent.py --live               # Atlas + LLM reais, banco ISOLADO
cd backend && .venv/bin/python eval_agent.py --json hoje.json --compare ontem.json
```

`--live` grava dado real (sessão, memória, cache) e por isso roda nos bancos de TESTE
(`POC_test`/`ai_brain_test`, via `backend/scripts/isolation.py`), nunca no da demo — ele RECUSA o
banco da demo sem `ALLOW_DEMO_DB_WRITE=1`. O `summary` carrega `database` para o número nunca
ficar órfão de onde foi medido.

## Diferenças em relação ao formato do multiagente

| Multiagente | Aqui | Por quê |
|---|---|---|
| `expected_agent` | removido | há UM agente; a pergunta "roteou certo?" não existe |
| `min_handoffs`, handoffs/turno | `min_tool_calls`, tool calls/turno | o custo de coordenação do agente único é a chamada de ferramenta, não o handoff |
| `expected_route_source` | removido | não há roteador |
| `customer` | `user_key` | é o nome do campo de identidade nesta PoV (`POC.app_users`) |
| modo `demo` (store em memória) | modo `offline` | esta PoV não tem DEMO_MODE: offline avalia só o que é função pura (escopo, portão de memória); o resto é `skipped_requires_llm` |
| `embedding_classifiers` no summary | idem, com `turn_classifier` apenas | só há um classificador por embedding aqui (não existe `scope_classifier`) |

Acurácia de roteamento (100% nas duas rodadas do multiagente) simplesmente não tem equivalente
aqui, e o comparativo em `docs/eval-report.md` diz isso em vez de preencher a célula com 100%.
