# ADR-007: Ferramenta externa de busca web, governada por documento

**Status:** Aceito
**Data:** 2026-10-13
**Contexto do cliente/conta:** PoV Intelligence Layer (aba Agente)

## Contexto
O agente só enxergava o próprio Atlas (`find`, `aggregate`, `update-many` via MCP).
Qualquer pergunta fora do domínio da loja ("que dia é hoje?", "quando é o segundo
turno?", "como está o tempo em São Paulo?") virava "não tenho acesso" ou o
redirecionamento fixo de escopo (`guidance.is_obviously_out_of_scope`, lista de
palavras). Um agente que não alcança nada fora do próprio banco é um RAG com passos.

Três fatos medidos guiaram o desenho:
- O gateway Grove **não aceita** as ferramentas server-side da Anthropic
  (`web_search_*`, `web_fetch_*` → 400 "tool type ... is not supported for this
  model", Sonnet 5.5 e 4.5, 2026-10-13). A busca precisa ser ferramenta nossa.
- Nenhum prompt recebia a data atual: o modelo não sabia que dia era, nem julgava
  se um resultado de busca estava velho.
- "Que dia é hoje?" era resposta genérica e podia ir para o cache semântico
  compartilhado — servida errada no dia seguinte.

## Decisão
1. **Relógio do turno.** `agent.clock_block()` (data, hora, fuso
   `America/Sao_Paulo`) vai no bloco DINÂMICO do system (Aba 3) e no system do
   mini-chat (Aba 2). Data não é ferramenta.
2. **Cache por origem, não por lista de palavras.** Resposta que cita a data de
   hoje (`agent.uses_current_date`) não vai para o cache compartilhado; resposta
   de turno que usou qualquer ferramenta (inclusive a web) também não, pela regra
   que já existia (`used_business_tools`).
3. **`web_search` (Tavily) despachada no servidor, fora do MCP.** Não entra em
   `ALLOWED_TOOLS` (o contrato com o MongoDB MCP Server não muda); é anexada à
   lista de ferramentas só quando o registro a liga para a área.
4. **Registro como documento.** `ai_brain.tool_registry` (`_id: "web_search"`):
   `enabled`, `disabled_areas`, `monthly_limit`, `max_results`,
   `cache_ttl_seconds`, `search_depth`. O registro só restringe; documento
   ausente, ilegível ou sem `TAVILY_API_KEY` = ferramenta desligada (falha fechado).
   No seed, Financeiro fica sem busca web.
5. **Taint do turno.** A primeira chamada a `web_search` contamina o turno: toda
   escrita depois dela é negada no servidor (`agent.TAINTED_WRITE_DENIAL`), antes
   do MCP. O conteúdo volta ao modelo entre `<resultados_web_nao_confiaveis>`, com
   instrução de ignorar comandos embutidos.
6. **Cota, cache e auditoria no Atlas.** `ai_brain.web_search_quota`
   (`_id: "AAAA-MM"`, `$inc` condicional atômico; teto 900 < 1.000 créditos do
   plano gratuito), `POC.web_search_cache` (consulta normalizada, TTL em
   `expires_at`, 6 h) e `POC.external_calls` (consulta mascarada, URLs, latência,
   cache hit, cota; TTL 30 dias).
7. **PII.** A consulta é mascarada de novo (`guardrails.mask_pii`) antes de sair
   do processo rumo ao provedor.

8. **Contagem de dias é código.** `date_diff` (função pura, sem rede) responde
   "quantos dias faltam"; o modelo errava a subtração (medido: "72 dias" até o
   Natal em 13/10, três vezes; são 73).
9. **Carimbo de capacidade no cache semântico.** Cada entrada grava `web_enabled`;
   no acerto, carimbo diferente do turno atual = miss (`cache._capability_ok`).
   FAQ semeada não tem carimbo e vale sempre. Sem isso, a recusa "fora do escopo"
   gravada com a web desligada continuava servida depois de ligar a web no
   registro (medido no Financeiro), e a recusa "não tenho dado em tempo real" da
   Aba 2 era servida à Aba 3.
10. **Pergunta que cita `PED-…` não lê o cache** (a gravação já recusava): medido,
    "…e já solicite o reembolso do meu PED-4002" era servida por uma FAQ e o
    reembolso nunca acontecia.

## Alternativas consideradas
- **Busca nativa do provedor do LLM** (`web_search` da Anthropic): mais simples,
  mas indisponível pelo Grove.
- **Brave Search API:** exige cartão até no plano gratuito, e os termos não incluem
  direito de armazenar resultado (conflita com o cache).
- **DuckDuckGo / SearXNG:** sem API oficial, raspam HTML de terceiros; quebram sem
  aviso — risco alto em demo ao vivo.
- **Sem taint, só delimitadores:** o agente tem dado privado + escrita + conteúdo
  não confiável no mesmo contexto. A reescrita de política não pega uma ordem
  embutida numa página que pede um status PERMITIDO no pedido CERTO do cliente.

## Consequências
- Perguntas de conhecimento geral e atualidade são respondidas com fonte (medido
  ao vivo: segundo turno → 25/10/2026 com URL do TSE; clima de SP com divergência
  entre fontes declarada).
- Um pedido de ação no MESMO turno de uma busca não é executado: o agente pede
  confirmação numa nova mensagem (medido ao vivo: `update-many` no PED-4002 negado).
- O pitch ganha três pontos de MongoDB: ferramenta ligada/desligada por
  `update_one`, crédito do provedor poupado pelo cache com TTL, trilha de auditoria
  consultável por agregação.
- **Limite conhecido:** o cache web é por match exato da consulta normalizada. O
  modelo reescreve a consulta entre turnos ("data Brasil TSE" vs "data oficial
  TSE") e o cache não casa. Cache semântico (`$vectorSearch`) exige limiar
  calibrado (`calibrate_thresholds.py`), não escolhido na mão — próximo passo.
- **Limite conhecido:** o modelo nem sempre confronta a fonte com o relógio
  (medido: "noite sem nuvens" às 11 h, vindo de uma página de clima desatualizada),
  mesmo com a instrução explícita. É julgamento sobre o dado da fonte, não regra
  aplicável no servidor.
- **Limite conhecido:** o backend passa a depender de saída para a internet. Rede
  bloqueada vira resultado degradado honesto (`resilience.call_tool`: teto,
  circuit breaker).
- Busca web gasta latência (~3–5 s por chamada medida) e crédito; a cota é
  ajustável ao vivo no documento.
