# Sonda de capability do cluster — 2026-10-16

Gerada por `backend/scripts/capability_probe.py` no banco de teste `POC_test` (nunca na demo).
Cada linha é a resposta do servidor, não a leitura da documentação. Reexecute depois de qualquer
mudança de versão do cluster:

```bash
cd backend && .venv/bin/python scripts/capability_probe.py --json /tmp/probe.json
```

## Servidor

| Item | Valor |
|---|---|
| `buildInfo.version` | 9.0.4 (`enterprise`) |
| FCV | 9.0 |
| autoEmbed (`voyage-4`) indexou 3 fatos novos em | 14,4 s |

## `$rankFusion`

Documentação: https://www.mongodb.com/docs/manual/reference/operator/aggregation/rankfusion/

| Sonda | Resultado | Latência (cliente → Atlas) |
|---|---|---|
| vector (`tenant_vector_stage`) + `$search` com `compound.filter` (user_key, active) | aceito | 477 ms |
| `combination.weights` (vector 1.0, bm25 0.5) + `fuzzy.maxEdits=1` com typo ("contatdo por emial") | aceito, fato certo em 1º | 494 ms |
| `scoreDetails: true` + `{$meta: "scoreDetails"}` | aceito, detalhe por perna | 467 ms |
| `scoreDetails: true` + `{$meta: "searchScoreDetails"}` (nome do exemplo Node.js da doc) | aceito, **campo vazio** | 456 ms |
| identificador exato `PED-1005` | fato certo em 1º (score 1/61 + 1/61) | 473 ms |

Formato real do `scoreDetails` (a doc avisa que não é garantido):

```json
{"value": 0.0328, "description": "value output by reciprocal rank fusion algorithm, ...",
 "details": [
   {"inputPipelineName": "bm25", "rank": 1, "weight": 1.0, "value": 1.2951, "details": []},
   {"inputPipelineName": "vector", "rank": 1, "weight": 1.0, "value": 0.8521, "details": []}]}
```

Perna sem o documento: `{"inputPipelineName": "bm25", "rank": "NA"}`, sem `weight`/`value`
(a doc diz `N/A`). O parser aceita os dois e qualquer outra coisa vira "detalhe indisponível".

## `$rerank` (Public Preview para `rerank-3`/`rerank-3-lite`)

Documentação: https://www.mongodb.com/docs/vector-search/query/aggregation-stages/rerank/

| Sonda | Resultado | Latência |
|---|---|---|
| `$rerank` depois de `$rankFusion` + `$limit 10`, `rerank-2.5` | aceito | 810 ms |
| idem, `rerank-2.5-lite` | aceito | 650 ms |
| idem, `rerank-3` (Preview) | aceito | 1.085 ms |
| idem, `rerank-3-lite` (Preview) | aceito | 636 ms |
| `$rerank` DENTRO de um input pipeline do `$rankFusion` | **rejeitado**, code 12108704: "input pipelines must not contain $rerank" | 258 ms |
| modelo inexistente | **rejeitado**, code 65169 (Voyage 400, lista os 6 modelos suportados) | 422 ms |

Consequências de projeto:
- `$rerank` vem DEPOIS da fusão, nunca dentro dela (a doc e o servidor concordam).
- `$rerank` substitui `$meta: "score"`: o score da fusão precisa ser copiado para um campo
  (`$addFields`) antes do estágio.
- `model` é obrigatório; a doc não define default.
- A latência medida acima inclui o RTT cliente → cluster (~250 ms nesta máquina).

## Queryable Encryption (driver)

| Item | Valor |
|---|---|
| `pymongo.encryption` | importável |
| `pymongocrypt` | **ausente** no venv do runtime |
| `crypt_shared` (`CRYPT_SHARED_LIB_PATH`) | não configurado |

A5 decide se instala `pymongo[encryption]` num venv próprio (explicit encryption não exige
`crypt_shared`). Ver `docs/loop-log.md`.
