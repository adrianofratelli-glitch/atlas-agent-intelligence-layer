# ADR-005: Quick-chat com gate de cache e histórico de sessão

**Status:** Aceito
**Data:** 2026-09-21
**Contexto do cliente/conta:** PoV Intelligence Layer (aba Modelo e custo)

## Contexto
O `/api/chat/quick` (mini-chat da aba Modelo e custo) era stateless e gravava
toda resposta no cache semântico compartilhado da área. Numa sessão de teste,
"quais foram as perguntas que eu fiz até agora?" foi respondida sem contexto
("você fez apenas uma pergunta"), gravada no cache e servida depois para
"…nessa sessão?" — resposta errada, reutilizada entre sessões.

## Decisão
1. **Gate de cache.** O turno pula lookup e store do cache semântico quando é
   pessoal (`memory.should_extract` ou `turn_classifier.classify`, que falha
   fechado) ou fala da própria conversa (`memory.references_conversation`,
   match de frases sem regex).
2. **Histórico.** O front envia até 10 turnos anteriores; o backend mascara PII
   nos turnos do usuário (`guardrails.mask_pii`), limita a 6.000 caracteres e
   normaliza a alternância user/assistant. Turnos bloqueados pelo guardrail não
   entram.

## Consequências
- Perguntas dependentes de contexto nunca contaminam o cache compartilhado.
- **Limite conhecido:** continuação sem gatilho ("e o da Colômbia?") ainda pode
  ler/gravar o cache sem contexto. Fechar isso reduziria os hits da demo.
- O índice `$vectorSearch` com autoEmbed é eventualmente consistente: repetir
  uma pergunta segundos após a primeira pode dar MISS (~40s observados).
  Em demo ao vivo, esperar ~60s ou usar as FAQs semeadas.
