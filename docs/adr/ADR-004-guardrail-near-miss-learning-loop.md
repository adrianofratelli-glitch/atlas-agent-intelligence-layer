# ADR-004: Guardrail near-miss learning loop com aprovação humana obrigatória

**Status:** Aceito
**Data:** 2026-07-15
**Contexto do cliente/conta:** PoV Intelligence Layer

## Contexto
O guardrail semântico (`$vectorSearch` contra `POC.guardrail_denylist`) bloqueia
quando o score de similaridade passa do `denylist_threshold` da política. Uma
mensagem com score logo ABAIXO do threshold — quase um match, mas não o
suficiente — passava sem deixar rastro: não vira violação, não é logada, não
gera nenhum sinal de que o denylist tem uma lacuna. O agente não ficava mais
apto a lidar com tentativas parecidas no futuro.

## Decisão
Quando o score do candidato mais próximo cai dentro de
`[threshold - 0.05, threshold)` ("near-miss"), a mensagem é gravada em
`POC.guardrail_candidates` com `status: "pending"` — não bloqueia o turno, só
registra. Um humano revisa via `POST /api/guardrails/candidates/{id}/review`
(`decision=approved|rejected`). `approved` insere a frase em
`POC.guardrail_denylist` (autoEmbed reindexa sozinho); `rejected` só fecha o
item. **A promoção nunca é automática.**

Esse mesmo princípio foi generalizado para toda a base `ai_brain`
(`guardrail_policies`, `prompt_templates`, `model_config`, `area_profiles`):
são documentos de POLÍTICA, editados só por humano. O agente nunca escreve em
`ai_brain` — só em `POC` (logs e filas de candidatos revisáveis).

## Alternativas consideradas
| Opção | Prós | Contras | Por que rejeitada |
|---|---|---|---|
| Auto-promoção direta ao denylist quando score near-miss | "aprendizado" instantâneo, zero fricção operacional | um usuário malicioso pode repetir frases parecidas de propósito para treinar o guardrail a bloquear falsos positivos legítimos de outro cliente (poisoning inverso); nenhuma auditoria antes do bloqueio entrar em produção | risco de segurança inaceitável para ambiente enterprise |
| Não registrar near-miss (comportamento anterior) | zero mudança, zero risco | guardrail nunca melhora sozinho; lacunas do denylist ficam invisíveis | desperdiça sinal que já está disponível no próprio `$vectorSearch` |
| Auto-promoção com contagem mínima (N ocorrências do mesmo padrão) | reduz risco de poisoning por uma única mensagem | ainda manipulável por um atacante paciente rodando N variações; adiciona lógica de agregação sem revisão de conteúdo | revisão humana sozinha já resolve com menos superfície de ataque; pode ser revisitado como sinal de PRIORIZAÇÃO da fila, não de auto-aprovação |

## Evidência
`[TBD - pendente de teste]` — sem volume real de tráfego adversarial no PoV
ainda para medir taxa de near-miss/dia ou tempo médio de revisão. Recomendado
rodar contra o denylist semântico existente (`guardrail_denylist_vs`) durante
a demo para gerar exemplos reais antes da apresentação ao cliente.

## Consequências
- Positivas: guardrail acumula sinal de tentativas que quase passaram, sem
  abrir vetor de auto-envenenamento; história auditável de toda promoção
  (`source_candidate`, `reviewed_by`, `reviewed_at`).
- Negativas / trade-offs aceitos: exige operação humana contínua (alguém
  precisa revisar a fila) — não é "aprendizado" no sentido de ML incremental
  automático; se ninguém revisar, candidatos apenas expiram (ADR-003).
- Reversibilidade: alta — `NEAR_MISS_MARGIN` e o fluxo de revisão são código
  de aplicação, ajustáveis sem migração de schema.

## Riscos e mitigação
- Risco: fila de candidatos cresce mais rápido que a capacidade de revisão
  humana. Mitigação: TTL de 30 dias (ADR-003) evita acúmulo indefinido;
  considerar dashboard de priorização por frequência/score se o volume real
  justificar.
- Risco: margem de 0.05 é heurística, não calibrada com dado real. Mitigação:
  marcado como TBD acima — recomendo calibrar com amostra de tráfego real do
  cliente antes de produção.
