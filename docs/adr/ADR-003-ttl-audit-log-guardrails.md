# ADR-003: TTL index em POC.guardrail_events e POC.guardrail_candidates

**Status:** Aceito
**Data:** 2026-07-15
**Contexto do cliente/conta:** PoV Intelligence Layer

## Contexto
`POC.guardrail_events` é o audit log de governança (todo check de guardrail,
allow/block/mask). O código já documentava a intenção ("TTL de 30 dias via
índice") mas o índice nunca tinha sido criado — a collection cresceria sem
limite. `POC.guardrail_candidates` (ver ADR-004) é a fila de near-misses
pendente de revisão humana e tem o mesmo problema de retenção.

## Decisão
Criar índice TTL em `at` para as duas collections, `expireAfterSeconds: 2592000`
(30 dias).

## Alternativas consideradas
| Opção | Prós | Contras | Por que rejeitada |
|---|---|---|---|
| Sem TTL, arquivar manualmente | retenção sob controle total | trabalho manual recorrente; risco de esquecer; volume cresce até alguém agir | não escala, e o código já assumia 30 dias por comentário |
| TTL de 90 dias | mais margem de auditoria | maior custo de storage sem ganho claro para o PoV | 30 dias é o padrão já documentado no código-fonte antes desta decisão |
| Exportar para cold storage (S3) antes de expirar | preserva histórico longo para compliance | infraestrutura extra fora do escopo do PoV | overengineering para fase de PoV; revisitar se cliente exigir retenção regulatória > 30 dias |

## Evidência
Nenhum documento existente em nenhuma das duas collections no momento da
criação (namespace inexistente) — índice criado limpo, sem necessidade de
migração. Confirmado via `getIndexes()`: `{key: {at: 1}, expireAfterSeconds: 2592000, name: "ttl_at_30d"}` em ambas.

## Consequências
- Positivas: audit log de compliance com retenção previsível e automática;
  fila de candidatos não vira lixo acumulado se ninguém revisar.
- Negativas / trade-offs aceitos: eventos de guardrail com mais de 30 dias não
  ficam disponíveis para auditoria retroativa dentro do próprio cluster.
- Reversibilidade: alta — `dropIndex` a qualquer momento; ajustar janela é
  `dropIndex` + `createIndex` novo (TTL não é alterável in-place por
  `collMod` sem recriar, mas é operação rápida e sem downtime).

## Riscos e mitigação
- Risco: requisito regulatório do cliente final exigir retenção > 30 dias
  (ex.: LGPD/setor financeiro). Mitigação: janela é parametrizável por
  política antes de produção; se necessário, mover para exportação para cold
  storage antes da expiração (ADR futuro, TBD).
