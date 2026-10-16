# ADR-010: Invalidação do cache semântico por Change Stream (`area_profiles` → `semantic_cache`)

**Status:** Aceito
**Data:** 2026-10-16
**Contexto do cliente/conta:** PoV Intelligence Layer (cache semântico, aba Agente e aba Modelo)

## Contexto
O cache semântico só expirava por TTL (24 h). Uma regra de negócio alterada ao vivo
(`update_one` na persona de uma área, a tese "config é documento") continuava respondida pelo
cache com a regra antiga até o TTL vencer — exatamente o tipo de inconsistência que a demo
promete não ter.

**O que alimenta uma resposta cacheada** (lido no código): o cache só grava turno SEM ferramenta
de negócio e SEM memória do cliente (`agent_graph.n_cache_store`, `main.quick_chat`). Sobra o LLM
e o system prompt, cuja parte de negócio é `ai_brain.area_profiles.persona` — onde estão as
políticas ("troca em até 30 dias, reembolso em até 7 dias úteis", prazos de entrega). As FAQs
semeadas repetem essas políticas. Pedidos, catálogo e memória nunca entram numa resposta
cacheada; `model_config` muda quem responde, não o que é verdade (a aba 2 já tem "Comparar
modelos", que ignora o cache); `tool_registry` já é tratado pelo carimbo `web_enabled`.

## Decisão
1. **Observar `ai_brain.area_profiles`.** `$match` NO SERVIDOR por `operationType`
   (insert/update/replace/delete/invalidate) e `$project` dos campos usados.
   `full_document="updateLookup"`; `delete` não traz documento e a área sai do `_id`
   (`area_<area>`, convenção do seed).
2. **Granularidade: área.** Update relevante (campo `persona`, `active` ou `area`) apaga as
   entradas de runtime da área (`scope != "faq"`) e as FAQs cuja fonte é a área
   (`source_area`) **somente se** o hash da persona mudou (`source_hash`, gravado pelo seed).
   Trocar `label`, `tier` ou rate limit não derruba nada. Granularidade por entidade (pergunta a
   pergunta) exigiria saber que trecho da persona cada resposta usou; o LLM não informa. Área é o
   menor escopo correto.
3. **Resiliência** (`atlas_retrieval.ChangeStreamWorker`, pov-shared 0.3.0):
   - resume token em `ai_brain.change_stream_tokens` (`_id: "cache_invalidation"`), salvo depois
     de cada evento (at-least-once; o handler é idempotente, `delete_many`), e pelo heartbeat a
     cada 30 s com o `postBatchResumeToken` do cursor;
   - reconexão com backoff exponencial e jitter; antes de reabrir, salva o token do cursor;
   - `invalidate`, ChangeStreamHistoryLost (286), Fatal (280), InvalidResumeToken (260) e
     qualquer recusa não transitória ao abrir COM token (medido: token malformado volta code
     15) → **reconcile**: apaga o runtime de todas as áreas e as FAQs cujo hash não bate com o
     perfil atual, registra em `POC.cache_invalidations` (TTL 30 dias) e reabre sem token;
   - partida sem token (primeira vez, ou depois de history lost) também reconcilia: não há como
     saber o que mudou enquanto nenhum worker olhava;
   - um worker por processo (singleton do módulo, `start()` idempotente); vários processos
     processam o mesmo evento sem efeito colateral (idempotente);
   - **cliente próprio sem CSOT.** O `timeoutMS=15000` do cliente do app vale para o `watch`:
     medido, um stream ocioso caía com `ExecutionTimeout` a cada ~18 s e um evento no meio da
     reabertura se perdia. O worker usa um `AsyncMongoClient` dedicado (pool 2) e
     `max_await_time_ms=5000`;
   - lifespan do FastAPI sobe e para o worker (shutdown fecha cursor e cliente); `/api/health`
     expõe eventos, reconexões, history lost, lag (wallTime) e p95; o preflight confere o
     heartbeat.
4. **Fail-open.** Sem pov-shared ou com o `watch` recusado, o app sobe e o cache segue só com o
   TTL, como antes.

## Alternativas consideradas
- **Gatilho do Atlas (Database Trigger).** Serviço fora do app, outra superfície de deploy e de
  segredo; o worker no lifespan mostra o mesmo mecanismo (change stream) dentro da PoV.
- **Pre-images (`changeStreamPreAndPostImages`).** Daria a área num `delete` sem convenção de
  `_id`, mas é opção de collection que muda o custo do oplog; a convenção do seed basta e o
  evento sem área identificável cai no reconcile.
- **Invalidar tudo a cada mudança.** Correto, mas apaga FAQs e o cache das outras áreas por uma
  troca de rótulo; o hash por área evita isso.
- **Encurtar o TTL.** Reduz a janela, não a fecha, e derruba a taxa de HIT.

## Consequências
- Medido no POC_test: invalidação em ~1,0 s após o `update_one` (lag do worker 0,9–0,95 s);
  área vizinha preservada; auditoria por evento.
- Caos LIVE (`scripts/chaos_suite.py cs_*`): invalidação, conexão derrubada no meio do stream,
  resume token inválido, ChangeStreamHistoryLost e SIGKILL do worker com retomada pelo token —
  5/5.
- **Demo pendente:** as FAQs da demo só ganham `source_area`/`source_hash` num reset controlado
  (`seed.py`); até lá o reconcile não as toca (FAQ sem fonte não depende de perfil) e o preflight
  avisa. A primeira partida do app na demo apaga o cache de runtime uma vez (reconcile de
  partida sem token); FAQs ficam.
