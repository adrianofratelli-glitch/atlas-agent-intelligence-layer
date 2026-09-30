# ADR-002: TTL index em POC.agent_sessions (memória de curto prazo)

**Status:** Aceito
**Data:** 2026-07-15
**Contexto do cliente/conta:** PoV Intelligence Layer

## Contexto
`POC.agent_sessions` guarda o array `turns[]` da conversa ativa (memória de curto
prazo / working context). Sem mecanismo de expiração, toda sessão iniciada
permanece na collection para sempre, mesmo após o cliente encerrar a conversa —
memória de curto prazo se comportando como memória de longo prazo, inflando a
collection indefinidamente e mantendo dados conversacionais além do necessário
para compliance de retenção.

## Decisão
Criar índice TTL em `agent_sessions.updated_at` com `expireAfterSeconds: 86400`
(24 horas de inatividade — revisado em 2026-07-23, era 3600/1h no desenho
original). `updated_at` é atualizado a cada novo turno (`$set` em
`update_one`), então uma sessão ativa nunca expira em uso — só expira 24h após
o último turno. A janela de 1h expirava sessões rápido demais para o caso de
uso real (cliente fecha a aba, sai para almoço ou reunião e volta no mesmo
dia esperando o mesmo contexto); 24h também alinha com o TTL da PoV irmã
(MultiAgent `short_term_memory`), então as duas demos contam a mesma história
de continuidade de sessão.

## Alternativas consideradas
| Opção | Prós | Contras | Por que rejeitada |
|---|---|---|---|
| TTL em `created_at` | mais simples | expira sessão em uso após a janela mesmo com cliente ainda conversando | quebra UX de sessão longa |
| Cron job de limpeza (app-side) | controle fino de lógica | mais um componente para manter/monitorar; atraso entre execuções; falha silenciosa se o job cair | TTL nativo do MongoDB já resolve sem infraestrutura extra |
| Sem expiração (manual reset via DELETE) | zero mudança | collection cresce sem limite; dado conversacional retido indefinidamente | não é sustentável para produção multi-tenant |
| Manter 1h | menor retenção | expira sessão rápido demais se o cliente cai e volta no mesmo dia | rejeitada nesta revisão em favor de 24h |

## Evidência
`agent_sessions` estava vazia (0 documentos) no momento da criação do índice —
sem migração de dados retroativa necessária. Índice original confirmado via
`getIndexes()`: `{key: {updated_at: 1}, expireAfterSeconds: 3600}`; revisado
em 2026-07-23 para `expireAfterSeconds: 86400` (índice recriado com novo nome
`ttl_updated_at_24h` em `seed.py`, o antigo `ttl_updated_at_1h` é dropado antes).

## Consequências
- Positivas: memória de curto prazo se comporta como short-term de fato; sem
  job externo; sem custo de storage crescendo sem limite; sessão sobrevive a
  uma queda de conexão/pausa longa do cliente no mesmo dia.
- Negativas / trade-offs aceitos: TTL monitor do MongoDB roda a cada ~60s, não
  é expiração instantânea — aceitável para este dado (não é informação
  sensível com deadline exato); sessões abandonadas ficam retidas por mais
  tempo (24h em vez de 1h) antes de expirar.
- Reversibilidade: alta — `dropIndex` remove a expiração a qualquer momento
  sem perda de dado além do já expirado.

## Riscos e mitigação
- Risco: cliente espera retomar sessão após pausa longa (>24h) e ela já expirou.
  Mitigação: 24h é o padrão atual do PoV; ajustável por política (mesmo padrão
  de config viva usado em `guardrail_policies`/`model_config`) se o caso de
  uso do cliente pedir janela ainda maior.
