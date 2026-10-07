"""Orquestração do turno da Aba 3 como um StateGraph do LangGraph.

Migração do loop monolítico (uma função com múltiplos `return` antecipados)
para um grafo real: nós = fases do turno (Perceive/Retrieve/Reason·Act/Store),
arestas condicionais = os três desvios (bloqueado pelo guardrail, fora de
escopo, cache hit) e o caminho completo. Checkpoint por turno usa o
`MongoDBSaver` NATIVO do LangGraph (`langgraph-checkpoint-mongodb`), com
`thread_id = conversation_id` — o próprio framework persiste o estado do
grafo a cada super-step em `agent_sessions.langgraph_checkpoints`/`_writes`.

Isto SUBSTITUI o checkpoint manual (`agent.open_turn`/`agent.interrupted_turn`,
documento `pending_turn`) como mecanismo de recuperação: com o checkpointer
nativo, invocar `graph.ainvoke` de novo com o MESMO `thread_id` depois de um
crash retoma do último super-step concluído, sem UM documento próprio para
isso. `open_turn`/`interrupted_turn` foram REMOVIDOS de `agent.py` — o
cenário de caos `crash_mid_tool` foi reescrito para verificar retomada pelo
checkpointer (ver `scripts/chaos_suite.py`), não mais pelo campo
`pending_turn`.

Nenhuma lógica de política (guardrails, reescrita de tool, higiene de cache,
orçamento, PII) foi tocada — os nós chamam as MESMAS funções de `agent.py`,
`memory.py`, `cache.py`, `policy_guardrails.py`, apenas reorganizadas em
fases do grafo em vez de uma função linear.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional, TypedDict

from langgraph.checkpoint.mongodb import MongoDBSaver
from langgraph.graph import END, StateGraph
from pymongo import MongoClient as SyncMongoClient

import agent
import cache
import langfuse_tracing as tracing
import memory
import observability
import policy_guardrails as guardrails
import profiles
import turn_classifier
from db import DB_MAIN, MAX_TIME_MS, SESSION_IDLE_SECONDS, poc
from guidance import is_obviously_out_of_scope, scope_reply

logger = logging.getLogger("poc.agent_graph")


class TurnState(TypedDict, total=False):
    scenario: Optional[str]
    user_msg: str
    conversation_id: str
    user_key: str
    area: str
    agent_model: str
    agent_fallback_model: Optional[str]
    profile_info: dict
    guard_in: dict
    guard_out: Optional[dict]
    personal_turn: bool
    cache_res: dict
    ltm: dict
    history: list
    history_summary: Optional[str]
    tools: list
    budget_brl: Optional[float]
    system_static: str
    system_dynamic: str
    final_answer: str
    metrics: dict
    trace: list
    turn_count: int
    new_facts: list
    superseded: list
    mem_tx: bool
    cache_stored: bool
    output: dict
    # Chaves "privadas" repassadas entre nós desta implementação (não fazem
    # parte do envelope público de `_result`) — precisam estar declaradas
    # aqui: uma chave de retorno de nó que não é um campo do TypedDict é
    # descartada silenciosamente pelo StateGraph, não vira KeyError na hora
    # do `return` (isso já derrubou `checkpoint_native_persistence` e
    # `live_degraded_turn` em produção — ver chaos-report).
    _area_profile: dict
    _used_business_tools: bool


def _emit(ctx: dict, trace: list, phase: str, kind: str, **fields) -> None:
    """Mesma semântica do `emit()` original de `agent.run_agent`: acumula no
    trace do turno, repassa ao streaming SSE (`on_event`) e espelha no
    Langfuse — só que agora vive fora do estado do grafo (`ctx` nunca é
    serializado pelo checkpointer, ao contrário de `trace`/`metrics`)."""
    event = {"phase": phase, "kind": kind, **fields}
    trace.append(event)
    on_event = ctx.get("on_event")
    if on_event is not None:
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 — streaming nunca derruba o turno
            logger.exception("on_event falhou (streaming) — turno continua")
    lf_trace = ctx.get("lf_trace")
    if kind == "reasoning":
        tracing.log_generation(
            lf_trace, name=f"{phase}.reasoning", model=fields.get("model"),
            input_text=None, output_text=fields.get("text"),
            usage={k: fields.get(k, 0) for k in (
                "input_tokens", "output_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens")},
            latency_ms=fields.get("latency_ms", 0),
        )
    elif kind == "tool_call":
        tracing.log_span(
            lf_trace, name=f"{phase}.{fields.get('tool', 'tool')}",
            input_data=fields.get("args"), output_data=fields.get("result"),
            metadata={"is_error": fields.get("is_error", False),
                      "latency_ms": fields.get("latency_ms")},
        )


def _ctx(config) -> dict:
    return config["configurable"]["ctx"]


# ---------------------------------------------------------------------------
# Nós
# ---------------------------------------------------------------------------

async def n_identity(state: TurnState, config) -> dict:
    user_key = state["user_key"]
    metrics = state["metrics"]
    user = await profiles.require_demo_user(user_key)
    area = user.get("area", profiles.DEFAULT_AREA)
    agent_model, agent_fallback_model = await agent._resolve_agent_model(area)
    area_profile = await profiles.get_area_profile(area)
    metrics["reads"] += 2
    profile_info = {"area": area, "label": area_profile.get("label", area),
                     "user_key": user_key, "user_name": user.get("name", user_key)}
    return {"area": area, "agent_model": agent_model,
            "agent_fallback_model": agent_fallback_model,
            "profile_info": profile_info, "_area_profile": area_profile,
            "metrics": metrics}


async def n_guard_input(state: TurnState, config) -> dict:
    ctx = _ctx(config)
    user_key, conversation_id = state["user_key"], state["conversation_id"]
    metrics = state["metrics"]
    guard_in = await guardrails.check_input(state["user_msg"], user_key,
                                             conversation_id, state["area"])
    metrics["reads"] += 1
    user_msg = guard_in.get("masked_text") or state["user_msg"]
    ctx["lf_trace"] = tracing.start_trace(
        name="singleagent.turn", user_id=user_key, session_id=conversation_id,
        input_text=user_msg, metadata={"scenario": state.get("scenario"), "area": state["area"]},
    )
    return {"user_msg": user_msg, "guard_in": guard_in, "metrics": metrics}


async def n_resume_check(state: TurnState, config) -> dict:
    """Sem checkpoint manual: a única coisa que sobra aqui é o log de
    perceive (identidade + guardrail) que já existia no turno original."""
    ctx = _ctx(config)
    trace = state["trace"]
    metrics = state["metrics"]
    profile_info = state["profile_info"]
    guard_in = state["guard_in"]
    _emit(ctx, trace, "perceive", "message", actor="user", text=state["user_msg"])
    _emit(ctx, trace, "perceive", "tool_call", actor="mongodb",
          tool="find (app_users → area_profiles)",
          args={"database": f"{DB_MAIN}/ai_brain", "filter": {"user_key": state["user_key"]}},
          result=(f'Usuário "{profile_info["user_name"]}" → área "{profile_info["label"]}". '
                  "Persona, guardrails e cache deste turno seguem o perfil da área."),
          reads=metrics["reads"], writes=metrics["writes"])
    _emit(ctx, trace, "perceive", "guardrail", actor="guardrail", stage="input",
          action=guard_in["action"], violations=guard_in["violations"],
          result=("Bloqueado pela política de guardrails."
                  if not guard_in["allowed"] else
                  "Entrada aprovada pelos guardrails."
                  + (" PII detectada foi mascarada antes do LLM."
                     if guard_in.get("pii_masked") else "")))
    return {"trace": trace, "metrics": metrics}


def _route_after_resume(state: TurnState) -> str:
    if not state["guard_in"]["allowed"]:
        return "blocked"
    if is_obviously_out_of_scope(state["user_msg"]):
        return "scope"
    return "cache_lookup"


async def n_blocked(state: TurnState, config) -> dict:
    ctx = _ctx(config)
    trace, metrics = state["trace"], state["metrics"]
    final_answer = state["guard_in"]["block_message"]
    turn_count = await agent._store_short_term(
        state["conversation_id"], state["user_key"], state["user_msg"],
        final_answer, lambda *a, **k: _emit(ctx, trace, *a, **k), metrics)
    _emit(ctx, trace, "act", "message", actor="agent", text=final_answer)
    _emit(ctx, trace, "loop", "message", actor="agent", text="Turno encerrado pelo guardrail.")
    tracing.finish_trace(ctx.get("lf_trace"), output_text=final_answer)
    output = agent._result(
        state.get("scenario"), state["user_msg"], final_answer, state["conversation_id"],
        turn_count, trace, metrics, state["guard_in"], {"hit": False, "blocked": True},
        None, None, state["agent_model"], state["profile_info"],
        lf_trace_url=tracing.trace_url(ctx.get("lf_trace")))
    return {"output": output, "trace": trace, "metrics": metrics}


async def n_scope(state: TurnState, config) -> dict:
    ctx = _ctx(config)
    trace, metrics = state["trace"], state["metrics"]
    final_answer = await scope_reply(state["user_key"])
    metrics["reads"] += 1
    metrics["memory_extraction_skipped"] = True
    _emit(ctx, trace, "retrieve", "tool_call", actor="mongodb", tool="find (support_orders)",
          args={"database": DB_MAIN, "collection": "support_orders",
                "filter": {"owner_user_key": state["user_key"]}},
          result="Solicitação fora de escopo redirecionada com os pedidos reais desta identidade.",
          reads=metrics["reads"], writes=metrics["writes"])
    turn_count = await agent._store_short_term(
        state["conversation_id"], state["user_key"], state["user_msg"],
        final_answer, lambda *a, **k: _emit(ctx, trace, *a, **k), metrics)
    _emit(ctx, trace, "act", "message", actor="agent", text=final_answer)
    tracing.finish_trace(ctx.get("lf_trace"), output_text=final_answer)
    output = agent._result(
        state.get("scenario"), state["user_msg"], final_answer, state["conversation_id"],
        turn_count, trace, metrics, state["guard_in"], {"hit": False, "scope_redirect": True},
        None, None, state["agent_model"], state["profile_info"],
        lf_trace_url=tracing.trace_url(ctx.get("lf_trace")))
    return {"output": output, "trace": trace, "metrics": metrics}


async def n_cache_lookup(state: TurnState, config) -> dict:
    ctx = _ctx(config)
    trace, metrics = state["trace"], state["metrics"]
    user_msg, area = state["user_msg"], state["area"]
    personal_turn = memory.should_extract(user_msg)
    if personal_turn:
        cache_res = {"hit": False, "score": 0.0, "threshold": None, "answer": None,
                     "question": None, "source_id": None, "latency_ms": 0, "mode": "bypass"}
        _emit(ctx, trace, "retrieve", "message", actor="agent",
              text="Cache semântico ignorado: a mensagem é pessoal (preferência/"
                   "tratamento) e depende da memória de longo prazo do usuário.")
    else:
        with observability.span("cache.lookup", **{"area": area, "step": "semantic_cache"}):
            cache_res = await cache.lookup(user_msg, area)
        metrics["reads"] += 1
        _emit(ctx, trace, "retrieve", "tool_call", actor="mongodb",
              tool="$vectorSearch (semantic_cache)",
              args={"database": DB_MAIN, "collection": "semantic_cache", "query": user_msg,
                    "filter": {"area": {"$in": ["global", area]}}},
              result=(f"CACHE HIT — score {cache_res['score']} ≥ {cache_res['threshold']}. "
                      "Resposta servida do MongoDB, sem LLM."
                      if cache_res["hit"] else
                      f"CACHE MISS — melhor score {cache_res['score']} < {cache_res['threshold']}."),
              reads=metrics["reads"], writes=metrics["writes"], latency_ms=cache_res["latency_ms"])

    if cache_res["hit"] and not personal_turn:
        turn_cls = await turn_classifier.classify(user_msg)
        metrics["reads"] += 1
        _emit(ctx, trace, "retrieve", "tool_call", actor="mongodb",
              tool="$vectorSearch (turn_probes)",
              args={"database": "ai_brain", "collection": "turn_probes", "query": user_msg},
              result=("Classificador indisponível — por segurança o cache é ignorado."
                      if turn_cls["error"] else
                      f"Turno {'PESSOAL' if turn_cls['personal'] else 'genérico'} — "
                      f"score {turn_cls['score']} vs limiar {turn_cls['threshold']}."),
              reads=metrics["reads"], writes=metrics["writes"], latency_ms=turn_cls["latency_ms"])
        if turn_cls["personal"]:
            personal_turn = True
            cache_res = {"hit": False, "score": cache_res["score"],
                         "threshold": cache_res["threshold"], "answer": None,
                         "question": None, "source_id": None,
                         "latency_ms": cache_res["latency_ms"], "mode": "bypass"}

    return {"cache_res": cache_res, "personal_turn": personal_turn,
            "trace": trace, "metrics": metrics}


def _route_after_cache(state: TurnState) -> str:
    return "cache_finish" if state["cache_res"]["hit"] else "gather"


async def n_cache_finish(state: TurnState, config) -> dict:
    ctx = _ctx(config)
    trace, metrics, cache_res = state["trace"], state["metrics"], state["cache_res"]
    final_answer = cache_res["answer"]
    cache_res["tokens_economizados"] = agent.estimate_tokens(final_answer)
    turn_count = await agent._store_short_term(
        state["conversation_id"], state["user_key"], state["user_msg"],
        final_answer, lambda *a, **k: _emit(ctx, trace, *a, **k), metrics)
    _emit(ctx, trace, "act", "message", actor="agent", text=final_answer)
    _emit(ctx, trace, "loop", "message", actor="agent",
          text="Respondido pelo cache semântico — próximo turno.")
    tracing.finish_trace(ctx.get("lf_trace"), output_text=final_answer)
    output = agent._result(
        state.get("scenario"), state["user_msg"], final_answer, state["conversation_id"],
        turn_count, trace, metrics, state["guard_in"], cache_res, None, None,
        state["agent_model"], state["profile_info"],
        lf_trace_url=tracing.trace_url(ctx.get("lf_trace")))
    return {"output": output, "trace": trace, "metrics": metrics, "cache_res": cache_res}


async def n_run_pipeline(state: TurnState, config) -> dict:
    """Caminho completo: memória longa + histórico + tools + orçamento em
    paralelo, monta o prompt, roda o loop de ferramentas (com extração de
    memória em background, exatamente como o `run_agent` original — o
    `asyncio.create_task` nasce aqui e é aguardado só depois do
    `_store_short_term`, então os dois passam pela MESMA janela de
    concorrência do código anterior) e fecha guardrail de saída + memória
    curta."""
    ctx = _ctx(config)
    session = config["configurable"]["session"]
    trace, metrics = state["trace"], state["metrics"]
    user_msg, user_key = state["user_msg"], state["user_key"]
    conversation_id = state["conversation_id"]

    ltm, (history, history_summary), tools, budget_brl = await asyncio.gather(
        memory.load_relevant(user_key, user_msg),
        agent._load_recent_history(conversation_id, user_key),
        agent.list_agent_tools(session),
        memory.active_budget(user_key),
    )
    metrics["reads"] += 1
    if budget_brl:
        _emit(ctx, trace, "retrieve", "message", actor="mongodb",
              text=f"Orçamento do cliente (memória de longo prazo): R$ {budget_brl:,.2f}. "
                   "O servidor aplica esse teto como $match no catálogo — "
                   "o modelo não consegue ignorá-lo.")
    if ltm.get("facts"):
        mode = ltm.get("mode")
        semantic = mode in ("vector", "hybrid")
        tool = ("$vectorSearch + $search RRF (agent_memory)" if mode == "hybrid"
                else "$vectorSearch (agent_memory)" if mode == "vector"
                else "find (agent_memory)")
        detail = (
            f"Memória longo prazo: {len(ltm['facts'])} fato(s) relevantes à pergunta, "
            f"de {ltm.get('total_active', 0)} ativos — "
            + ("retrieval híbrido (semântico + lexical, fusão RRF)."
               if mode == "hybrid" else "retrieval semântico.")
            if semantic else
            f"Memória longo prazo: {len(ltm['facts'])} fato(s) sobre o cliente."
        )
        _emit(ctx, trace, "retrieve", "tool_call", actor="mongodb", tool=tool,
              args={"database": DB_MAIN, "collection": "agent_memory",
                    "query": user_msg if semantic else None,
                    "filter": {"user_key": user_key, "active": True}},
              result=detail, reads=metrics["reads"], writes=metrics["writes"])

    mem_task = None
    if memory.should_extract(user_msg):
        mem_task = asyncio.create_task(
            memory.extract_and_store(user_key, user_msg, conversation_id, relevant=ltm))
    else:
        metrics["memory_extraction_skipped"] = True

    if history:
        metrics["reads"] += 1
        _emit(ctx, trace, "retrieve", "tool_call", actor="mongodb", tool="find (agent_sessions)",
              args={"database": DB_MAIN, "collection": "agent_sessions",
                    "filter": {"session_id": conversation_id},
                    "projection": {"turns": {"$slice": -agent.HISTORY_TURNS}}},
              result=f"Memória curta: últimos {len(history)} turno(s) hidratados no contexto.",
              reads=metrics["reads"], writes=metrics["writes"])
    if history_summary:
        _emit(ctx, trace, "retrieve", "message", actor="agent",
              text="Turnos mais antigos desta sessão foram resumidos (Sonnet) "
                   "em vez de descartados/cortados crus — contexto extra sem "
                   "estourar o budget.")

    profile_info = state["profile_info"]
    area_profile = state["_area_profile"]
    persona = (area_profile.get("persona") or "").strip()
    persona_block = (
        f"\n\nRegras da área \"{profile_info['label']}\" (carregadas de "
        f"ai_brain.area_profiles):\n{persona}" if persona else "")
    summary_block = (
        f"\n\nResumo do início desta conversa (turnos mais antigos, já fora da "
        f"janela recente): {history_summary}" if history_summary else "")
    system_static = agent.SYSTEM + persona_block
    budget_block = (
        f"\n\nOrçamento do cliente: R$ {budget_brl:,.2f}. A busca de catálogo já "
        "devolve somente itens dentro desse teto (filtro aplicado pelo sistema). Os "
        "itens são os mais próximos do pedido DENTRO do orçamento, então confira se "
        "de fato correspondem ao que o cliente pediu: se voltar vazia ou só trouxer "
        "itens de outra categoria, diga que não há opção dentro do orçamento — "
        "nunca que o produto está indisponível — e ofereça alternativas próximas."
        if budget_brl else "")
    system_dynamic = (agent._memory_note(conversation_id) + memory.format_for_prompt(ltm)
                       + budget_block + summary_block)

    emit_fn = lambda *a, **k: _emit(ctx, trace, *a, **k)  # noqa: E731
    final_answer = await agent.run_loop_guarded(
        lambda: agent._run_tool_loop(
            session, tools, system_static, system_dynamic, user_msg, emit_fn,
            metrics, state["agent_model"], conversation_id, user_key, history=history,
            fallback_model=state["agent_fallback_model"], budget_brl=budget_brl,
        ),
        emit=emit_fn, metrics=metrics,
    )

    guard_out = await guardrails.check_output(final_answer, user_key, conversation_id, state["area"])
    if guard_out["masked"]:
        final_answer = guard_out["text"]
        metrics["writes"] += 1
        _emit(ctx, trace, "act", "guardrail", actor="guardrail", stage="output",
              action="mask", violations=guard_out["violations"],
              result="PII mascarada na resposta antes de enviar ao cliente.")

    turn_count = await agent._store_short_term(conversation_id, user_key, user_msg,
                                                final_answer, emit_fn, metrics)

    used_business_tools = metrics["tools_used"] > 0
    new_facts, superseded, mem_tx = [], [], False
    if mem_task is None:
        mem_write = {"new": [], "superseded": [], "transaction": False,
                     "usage": {"input_tokens": 0, "output_tokens": 0}}
    else:
        try:
            mem_write = await mem_task
        except Exception:  # noqa: BLE001 — falha na extração nunca derruba a resposta
            logger.exception("extração de memória falhou (user_key=%s)", user_key)
            mem_write = {"new": [], "superseded": [], "transaction": False,
                         "usage": {"input_tokens": 0, "output_tokens": 0}}

    extractor_usage = mem_write.get("usage") or {}
    metrics["memory_extractor_input_tokens"] = int(extractor_usage.get("input_tokens", 0))
    metrics["memory_extractor_output_tokens"] = int(extractor_usage.get("output_tokens", 0))

    if final_answer:
        new_facts = mem_write["new"]
        superseded = mem_write["superseded"]
        mem_tx = mem_write["transaction"]
        if new_facts:
            metrics["writes"] += 1
            detail = f"{len(new_facts)} novo(s) fato(s) na memória de longo prazo."
            if superseded:
                detail += (
                    f" {len(superseded)} fato antigo(s) SUPERSEDIDO(s) "
                    f"(ex.: \"{superseded[0]['fact']}\")"
                    + (" — insert + update numa transação ACID." if mem_tx else "."))
            _emit(ctx, trace, "store", "tool_call", actor="mongodb",
                  tool="insert-one + update-one (agent_memory)" if superseded
                       else "insert-one (agent_memory)",
                  args={"database": DB_MAIN, "collection": "agent_memory",
                        "filter": {"user_key": user_key}, "transaction": mem_tx or None},
                  result=detail, reads=metrics["reads"], writes=metrics["writes"])

    return {
        "final_answer": final_answer, "guard_out": guard_out, "turn_count": turn_count,
        "ltm": ltm, "budget_brl": budget_brl, "new_facts": new_facts,
        "superseded": superseded, "mem_tx": mem_tx,
        "_used_business_tools": used_business_tools,
        "trace": trace, "metrics": metrics,
    }


async def n_cache_store(state: TurnState, config) -> dict:
    ctx = _ctx(config)
    trace, metrics = state["trace"], state["metrics"]
    user_msg, final_answer = state["user_msg"], state["final_answer"]
    used_business_tools = state["_used_business_tools"]
    new_facts, superseded = state["new_facts"], state["superseded"]
    ltm, personal_turn = state["ltm"], state["personal_turn"]
    cache_res, agent_model, area = state["cache_res"], state["agent_model"], state["area"]
    cache_stored = False

    if not used_business_tools and final_answer:
        mem_task_fired = bool(new_facts) or bool(superseded) or state["mem_tx"] \
            or metrics.get("memory_extractor_input_tokens", 0) > 0 \
            or not metrics.get("memory_extraction_skipped", True)
        personalized = (bool(new_facts) or bool(superseded)
                        or bool(ltm.get("facts")) or mem_task_fired
                        or personal_turn
                        or agent.transactional_turn(user_msg, final_answer, used_business_tools))
        if not personalized:
            store_cls = await turn_classifier.classify(user_msg)
            metrics["reads"] += 1
            personalized = store_cls["personal"]
        if not personalized:
            with observability.span("cache.store", **{"area": area, "step": "semantic_cache"}):
                await cache.store(user_msg, final_answer, agent_model, area=area)
            metrics["writes"] += 1
            cache_stored = True
            _emit(ctx, trace, "store", "tool_call", actor="mongodb",
                  tool="insert-one (semantic_cache)",
                  args={"database": DB_MAIN, "collection": "semantic_cache"},
                  result="Resposta gravada no cache semântico para reuso futuro (com TTL).",
                  reads=metrics["reads"], writes=metrics["writes"])
        else:
            _emit(ctx, trace, "store", "message", actor="agent",
                  text="Resposta personalizada — não vai para o cache compartilhado "
                       "(higiene de cache: respostas com dados do cliente não são reusadas).")

    cache_res["stored"] = cache_stored
    return {"cache_res": cache_res, "cache_stored": cache_stored, "trace": trace, "metrics": metrics}


async def n_finalize(state: TurnState, config) -> dict:
    ctx = _ctx(config)
    trace, metrics = state["trace"], state["metrics"]
    final_answer = state["final_answer"]
    _emit(ctx, trace, "act", "message", actor="agent", text=final_answer or "(sem resposta)")
    _emit(ctx, trace, "loop", "message", actor="agent", text="Pronto para o próximo turno.")
    with observability.span("memory.load_longterm", **{"step": "long_term_memory"}):
        ltm_after = await memory.load_longterm(state["user_key"])
    tracing.finish_trace(ctx.get("lf_trace"), output_text=final_answer)
    output = agent._result(
        state.get("scenario"), state["user_msg"], final_answer, state["conversation_id"],
        state["turn_count"], trace, metrics, state["guard_in"], state["cache_res"],
        {"new_facts": state["new_facts"], "superseded": state["superseded"],
         "transaction": state["mem_tx"], "longterm": ltm_after,
         "extraction": {
             "skipped": metrics["memory_extraction_skipped"],
             "input_tokens": metrics["memory_extractor_input_tokens"],
             "output_tokens": metrics["memory_extractor_output_tokens"]}},
        state["guard_out"], state["agent_model"], state["profile_info"],
        lf_trace_url=tracing.trace_url(ctx.get("lf_trace")))
    return {"output": output, "trace": trace, "metrics": metrics}


# ---------------------------------------------------------------------------
# Compilação (uma vez por processo)
# ---------------------------------------------------------------------------

_GRAPH = None
_CHECKPOINT_CLIENT: SyncMongoClient | None = None


def _build_graph():
    builder = StateGraph(TurnState)
    builder.add_node("identity", n_identity)
    builder.add_node("guard_input", n_guard_input)
    builder.add_node("resume_check", n_resume_check)
    builder.add_node("blocked", n_blocked)
    builder.add_node("scope", n_scope)
    builder.add_node("cache_lookup", n_cache_lookup)
    builder.add_node("cache_finish", n_cache_finish)
    builder.add_node("run_pipeline", n_run_pipeline)
    builder.add_node("cache_store", n_cache_store)
    builder.add_node("finalize", n_finalize)

    builder.set_entry_point("identity")
    builder.add_edge("identity", "guard_input")
    builder.add_edge("guard_input", "resume_check")
    builder.add_conditional_edges("resume_check", _route_after_resume, {
        "blocked": "blocked", "scope": "scope", "cache_lookup": "cache_lookup"})
    builder.add_conditional_edges("cache_lookup", _route_after_cache, {
        "cache_finish": "cache_finish", "gather": "run_pipeline"})
    builder.add_edge("run_pipeline", "cache_store")
    builder.add_edge("cache_store", "finalize")
    for terminal in ("blocked", "scope", "cache_finish", "finalize"):
        builder.add_edge(terminal, END)

    global _CHECKPOINT_CLIENT
    _CHECKPOINT_CLIENT = SyncMongoClient(os.environ["MONGODB_URI"])
    checkpointer = MongoDBSaver(
        _CHECKPOINT_CLIENT, db_name=DB_MAIN,
        checkpoint_collection_name="langgraph_checkpoints",
        writes_collection_name="langgraph_checkpoint_writes",
        # Checkpoints expiram junto com a sessão (mesmo TTL de inatividade de
        # agent_sessions). Sem isso eles ficavam órfãos para sempre: medido em
        # 2026-10-06, 13 threads em POC.langgraph_checkpoints sem nenhuma sessão.
        ttl=SESSION_IDLE_SECONDS,
    )
    return builder.compile(checkpointer=checkpointer)


def get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _build_graph()
    return _GRAPH


async def run_turn(session, *, scenario, user_msg, conversation_id, user_key,
                    on_event=None) -> dict:
    """Ponto de entrada chamado por `agent.run_agent`. Um super-step do
    LangGraph por turno de conversa; `thread_id=conversation_id` é a chave
    de checkpoint — um crash no meio de `run_pipeline` deixa o checkpointer
    no último nó CONCLUÍDO (`cache_lookup`), e a próxima invocação com o
    mesmo `thread_id` recomeça dali, não do zero da conversa inteira."""
    graph = get_graph()
    ctx: dict[str, Any] = {"on_event": on_event, "lf_trace": None}
    initial: TurnState = {
        "scenario": scenario, "user_msg": user_msg, "conversation_id": conversation_id,
        "user_key": user_key, "trace": [],
        "metrics": {
            "reads": 0, "writes": 0, "tools_used": 0, "latency_ms": 0,
            "degraded": False, "degraded_reason": None,
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "memory_extractor_input_tokens": 0, "memory_extractor_output_tokens": 0,
            "memory_extraction_skipped": False,
            "context_budget": {
                "history_chars": agent.MAX_HISTORY_CHARS,
                "memory_chars": memory.MAX_PROMPT_MEMORY_CHARS,
                "tool_result_chars": agent.MAX_TOOL_RESULT_CHARS,
                "estimated_chars_per_token": agent.CHARS_PER_TOKEN_ESTIMATE,
            },
        },
    }
    config = {"configurable": {"thread_id": conversation_id, "session": session, "ctx": ctx}}
    final_state = await graph.ainvoke(initial, config=config)
    return final_state["output"]
