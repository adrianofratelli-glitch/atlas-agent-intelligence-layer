# ADR-012: Por que a memória longa não usa o `MongoDBStore` do LangGraph

**Status:** Aceito
**Data:** 2026-10-16
**Contexto do cliente/conta:** PoV Intelligence Layer (memória de longo prazo)

## Contexto
A PoV já usa o `MongoDBSaver` (`langgraph-checkpoint-mongodb` 0.5.1) para o checkpoint do turno.
A pergunta natural é por que a memória longa (`POC.agent_memory`) não usa também o store do
LangGraph (`BaseStore`, com implementação MongoDB em `langgraph-store-mongodb`).

Contrato conferido no `langgraph` 1.2.12 instalado (`langgraph.store.base.BaseStore`):
`put(namespace, key, value, index=None, *, ttl=...)`,
`search(namespace_prefix, *, query=None, filter=None, limit=10, offset=0, refresh_ttl=None)`,
`get`, `delete`, `batch`. A implementação MongoDB não está instalada neste venv e **não foi
medida**; a decisão abaixo é sobre o contrato, que vale para qualquer backend.

## Decisão
Manter a memória longa própria (`memory.py`), porque ela entrega três garantias que o contrato
do store não expressa:

1. **Supersessão transacional.** Um fato que contradiz outro é INSERIDO e o antigo vira
   `active: false` + `superseded_by` na MESMA transação ACID. O contrato tem `put` por chave
   (sobrescreve) e `batch` de operações independentes; não há "insere este e desativa aquele
   atomicamente", nem histórico auditável do que o agente acreditava antes.
2. **Quarentena anti-poisoning na leitura.** `memory.quarantine` tira do prompt fato com formato
   de instrução gravado por qualquer caminho (import, script, base comprometida), além da recusa
   na extração. `search` devolve `SearchItem` como está; a defesa teria de ser reimplementada em
   volta de cada chamada.
3. **Guard de tenant que recusa.** `db.tenant_vector_stage` levanta `TenantFilterMissing` com
   chave vazia, e `memory.load_relevant` recusa `user_key` não-string (`{"$ne": null}` casaria
   todos os tenants); `tests/test_tenant_filter.py` falha se alguém montar `$vectorSearch`,
   `$rankFusion`, `$rerank` ou o `$search` da memória por fora. No store o isolamento é o
   `namespace_prefix` que o chamador passa — um prefixo vazio ou mais curto é uma busca válida
   e mais ampla, não um erro.

E o que esta PoV mede e mostra a mais: um único `$rankFusion` (vetor + BM25 em português, exato e
fuzzy) com `scoreDetails` no inspector, config viva por documento e `$rerank` opcional
(ADR-008, ADR-009).

## Alternativas consideradas
- **Store do LangGraph + wrappers** para transação, quarentena e guard. Sobra pouco do store:
  as três garantias ficariam no wrapper, com o store como camada de acesso.
- **Store para memória "semântica genérica"** (preferências sem contradição) e `memory.py` só
  para fatos que se supersedem. Dois caminhos de escrita e dois de leitura para o mesmo prompt.

## Consequências
- Checkpoint continua no `MongoDBSaver` (estado do grafo, onde o contrato do LangGraph é o certo).
- Reavaliar se o store ganhar escrita condicional/transacional e um modo de recusar namespace
  vazio, ou se a PoV precisar de memória compartilhada entre agentes LangGraph de terceiros.
