# ADR-014: Promessa de ação sem escrita vira oferta antes de chegar ao cliente

**Status:** Aceito
**Data:** 2026-11-13
**Contexto do cliente/conta:** PoV Intelligence Layer (fidelidade da resposta do agente, `answer_checks`)

## Contexto
Desde 2026-11-12, `backend/answer_checks.py` aponta `promessa_sem_escrita`: a resposta afirma
em primeira pessoa uma ação no pedido ("Vou encaminhar seu caso", "Já abri um chamado") e o
turno não teve nenhum `update-many` que tenha casado um pedido. A checagem só apontava. Ia para
o trace, `guardrail_events`, `/api/metrics` e o card da UI, mas o cliente recebia a promessa
mesmo assim.

A correção pelo prompt (regras 4a e 4b) foi medida e é parcial. Para "O PED-1005 já foi reposto
quantas vezes?" (`single-005`, que proíbe escrita), o agente passou a terminar com a oferta
("Posso abrir esse chamado de análise de qualidade para você agora?"). Mesmo assim, em 2 de 3
respostas da rodada de 12/11 e de novo na rodada de base de 13/11, a frase anterior ainda dizia
"Vou encaminhar seu caso para análise de qualidade". O eval ficou em 27/28 nas três rodadas
seguintes pelo mesmo motivo.

Uma regra que importa não pode depender de o modelo obedecer ao texto do prompt.

## Decisão
1. `answer_checks.rewrite_unbacked(texto, writes_ok)` (função pura). Sem escrita bem-sucedida no
   turno, cada frase com ação afirmada ganha, no lugar do verbo, a oferta "Se você quiser, posso
   <infinitivo>". Exemplos: "Vou encaminhar seu caso…" vira "Se você quiser, posso encaminhar seu
   caso…", e "Já abri o chamado" vira "Se você quiser, posso abrir o chamado". O resto da
   resposta fica intacto: status, contagem de reposições, datas e markdown. A frase é localizada
   no texto dobrado (sem acento e em minúsculas), e o verbo é trocado no texto original.
2. O verbo afirmado logo depois de "não", "nunca" ou "ainda não" deixa de contar como promessa,
   na checagem e na reescrita. "Não abri nenhum chamado" é um relato honesto.
3. Em `agent_graph._answer_fidelity`, depois da máscara de PII e antes da memória curta e do
   cache, o turno reescreve primeiro e confere depois:
   * `guard_out["answer_rewrites"]`: lista com {antes, depois}. O evento `[output] REWRITE` vai
     para o trace e para `guardrail_events`, este com o texto ORIGINAL do modelo já mascarado.
     O contador `answer_rewrites` aparece em `/api/metrics`;
   * `guard_out["answer_checks"]`: checagem sobre o texto ENTREGUE. O prazo sem fonte continua
     apontado e não é reescrito, porque não há frase segura para pôr no lugar de um número
     inventado.
4. **Pedido sem fonte, a mesma lógica.** `pedido_sem_fonte` aponta um `PED-…` da resposta que não
   aparece na mensagem, no histórico, no prompt nem nos resultados de ferramenta. Na rodada 2 de 13/11,
   com a regra 7b já dizendo "sem pedido informado, não cite número", o agente inventou outra lista
   ("PED-8901 – Fone Bluetooth Premium…"). `answer_checks.drop_ungrounded_orders` tira da resposta o
   item de lista ou a frase com o pedido inventado. Também tira a linha que só apresentava a lista,
   como "você possui 4 pedidos:". No lugar entra o pedido do número
   (`agent_graph.ORDER_FALLBACK_TEXT`). A lista real do dono chegou a ser testada nessa posição.
   Foi descartada porque, numa rodada final de 13/11, o pedido inventado apareceu também em "me manda a
   lista de todos os pedidos" (`single-028`, sem ferramenta no turno). Listar ali desfaria a
   política de um pedido por vez.
5. O card da aba Agente mostra "Guardrails · promessa virou oferta" com a frase antes e depois.
6. O eval julga a resposta entregue e expõe `answer_rewritten_turns` no resumo e
   `answer_rewrites` por caso: a taxa de 28/28 não esconde que o modelo ainda escreveu a promessa.
7. `ANSWER_REWRITE=0` volta ao comportamento anterior, em que a checagem só aponta. A flag existe
   só para reverter.

## Alternativas descartadas
* **Disparar a escrita prometida**: quando o cliente só perguntou, abrir chamado cria trabalho
  que ninguém pediu, e o `single-005` proíbe a escrita. A promessa é que está errada, não a
  ausência da escrita.
* **Remover a frase**: perde fatos que muitas vezes estão na mesma frase, como "já foi reposto 2
  vezes e vou encaminhar…".
* **Segunda chamada de LLM para reescrever**: custo e latência em todo turno apontado, e o
  resultado também é não determinístico.
* **Só endurecer o prompt**: medido, reduz a frequência sem eliminar.

## Consequências
* O cliente não recebe mais uma promessa de ação que não aconteceu. A memória curta grava o texto
  entregue, então o próximo turno não herda a promessa.
* Uma frase com duas ações vira "Se você quiser, posso registrar a troca e posso abrir um
  chamado". É menos fluente, mas é verdadeira.
* A heurística é a mesma da checagem: casa frases e não entende o texto. Uma promessa fora da
  lista de verbos passa como antes. O juiz de outra família (`eval_agent.py --judge`) segue como
  segunda opinião.
* O evento `reasoning` do trace mantém o texto do modelo, para que a demo mostre o que foi
  corrigido.

## Evidência
* `backend/tests/test_answer_checks.py`: `RewriteTests` usa a resposta real do `single-005`,
  `TurnWiringTests` cobre trace, auditoria, flag de reversão e turno com escrita, e
  `NegationTests` cobre os relatos negativos. `UngroundedOrderTests` e
  `DropUngroundedOrderTests` usam a resposta real do `single-009`.
* Eval ao vivo em `POC_test`: ver `docs/eval-report.md`, rodadas de 13/11.
