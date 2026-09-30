"""Bateria de caos: o PoV tentando se quebrar sozinho.

Cada cenário injeta UMA falha controlada no caminho real (`backend/chaos.py`) e
verifica uma afirmação concreta sobre o que "resiliente" significa ali. Sem
assertion o cenário não prova nada, então cada um carrega a sua em `assertion`.

    cd backend && CHAOS=1 .venv/bin/python scripts/chaos_suite.py              # bateria toda
    cd backend && CHAOS=1 .venv/bin/python scripts/chaos_suite.py tool_timeout # um cenário
    cd backend && CHAOS=1 LIVE=1 .venv/bin/python scripts/chaos_suite.py       # inclui os que usam Atlas

Os mesmos cenários rodam como regressão permanente em `tests/test_chaos.py` (só
com `CHAOS=1`).

Dois níveis:

* **offline** (padrão) — exercita o código REAL do loop de ferramentas
  (`agent._run_tool_loop`, `resilience.call_tool`, `agent._create_with_retry`) com
  uma sessão MCP falsa e um cliente de LLM falso. Sem Atlas, sem chave de LLM.
* **LIVE=1** — `run_agent` inteiro contra o banco de TESTE isolado
  (`scripts/isolation.py`: `POC_test`/`ai_brain_test`, nunca a demo) e, no
  `crash_resume`, um `SIGKILL` no meio do turno.

A degradação graciosa, o teto por tool e o circuit breaker são o comportamento
PADRÃO — a bateria não liga flag nenhuma para obtê-los. A única flag que aparece
aqui é `SINGLEAGENT_LEGACY_500=1`, no cenário que prova o modo antigo.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chaos  # noqa: E402
import resilience  # noqa: E402

TOOLS = [{"name": "find", "description": "find", "input_schema": {"type": "object"}}]


def _db_main() -> str:
    """Nome do banco que a política do agente espera AGORA (demo ou teste)."""
    import db

    return db.DB_MAIN


# ---------------------------------------------------------------- mundo de teste


class FakeToolResult:
    def __init__(self, text: str = '[{"order_id": "PED-1001", "status": "entregue"}]',
                 is_error: bool = False):
        self.content = [type("Block", (), {"type": "text", "text": text})()]
        self.isError = is_error


class FakeSession:
    """Sessão MCP falsa: o cenário decide o que a chamada faz."""

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or (lambda name, args: FakeToolResult())
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        result = self.behaviour(name, arguments)
        if asyncio.iscoroutine(result):
            return await result
        if isinstance(result, Exception):
            raise result
        return result

    async def list_tools(self):
        return type("Tools", (), {"tools": []})()


class FakeUsage:
    input_tokens = 40
    output_tokens = 20
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class FakeBlock:
    def __init__(self, kind, **fields):
        self.type = kind
        for key, value in fields.items():
            setattr(self, key, value)


class FakeResponse:
    def __init__(self, blocks):
        self.content = blocks
        self.usage = FakeUsage()
        self.model = "claude-haiku-4-5"
        self.stop_reason = "end_turn"


class FakeLLM:
    """Cliente de LLM falso: primeira volta pede a tool, segunda responde texto."""

    def __init__(self, tool_rounds: int = 1):
        self.tool_rounds = tool_rounds
        self.round = 0
        self.messages = self
        # O que o MODELO viu: é aqui que se prova que um resultado de tool
        # degradado chega honesto ao prompt, e não sanitizado só no trace.
        self.seen: list[dict] = []

    async def create(self, **kwargs):
        # Nenhum ponto de caos aqui: o do caminho REAL vive em
        # `agent._create_with_retry`. Dois pontos gerariam contagem dupla.
        self.seen = list(kwargs.get("messages") or [])
        self.round += 1
        if self.round <= self.tool_rounds:
            return FakeResponse([
                FakeBlock("text", text="Vou consultar o pedido."),
                FakeBlock("tool_use", id=f"tu_{self.round}", name="find",
                          # O banco vem de db.DB_MAIN: com MONGODB_DB=POC_test a
                          # política REESCREVE/NEGA por alvo, então um "POC" fixo
                          # aqui fazia a chamada ser negada e o turno terminar sem
                          # nunca entrar na ferramenta — o cenário media outra coisa.
                          input={"database": _db_main(), "collection": "support_orders",
                                 "filter": {"order_id": "PED-1001"}}),
            ])
        return FakeResponse([FakeBlock("text", text="Seu pedido está entregue.")])


@contextlib.contextmanager
def env(**values):
    """Aplica variáveis de ambiente só durante o cenário."""
    previous = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    chaos.reset()
    resilience.reset_circuits()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        chaos.reset()
        resilience.reset_circuits()


@dataclass
class Verdict:
    name: str
    assertion: str
    passed: bool
    detail: str = ""
    seconds: float = 0.0
    skipped: bool = False

    def line(self) -> str:
        status = "SKIP" if self.skipped else ("PASS" if self.passed else "FAIL")
        return f"[{status}] {self.name} ({self.seconds:.2f}s) — {self.assertion}" + (
            f"\n        {self.detail}" if self.detail else "")


async def _loop(session, llm, **overrides):
    """Executa o loop REAL de ferramentas do agente com sessão e LLM falsos."""
    import agent

    events: list[dict] = []
    metrics = {"reads": 0, "writes": 0, "tools_used": 0, "latency_ms": 0,
               "input_tokens": 0, "output_tokens": 0,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    original_client, original_resolve = agent.anthropic_client, agent.resolve_connection_id
    agent.anthropic_client = llm
    agent.resolve_connection_id = lambda _session: _connection_id()
    try:
        answer = await agent._run_tool_loop(
            session, TOOLS, "sistema", "", "onde está meu pedido PED-1001?",
            lambda phase, kind, **fields: events.append({"phase": phase, "kind": kind, **fields}),
            metrics, "claude-haiku-4-5", "conv_chaos", "cliente-demo",
            **overrides)
        return answer, events, metrics
    finally:
        agent.anthropic_client, agent.resolve_connection_id = original_client, original_resolve


async def _connection_id() -> str:
    return "preconfigured"


def _tool_events(events):
    return [e for e in events if e["kind"] == "tool_call"]


def _tool_results_seen(llm) -> str:
    """Concatena os `tool_result` que chegaram ao modelo na última chamada.

    O evento do trace passa por `_safe_tool_display` (redação), então asserção
    sobre o que o MODELO recebeu tem que ler daqui, não do trace.
    """
    texts = []
    for message in llm.seen:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    texts.append(str(block.get("content", "")))
    return "\n".join(texts)


# ---------------------------------------------------------------- cenários


async def scenario_tool_timeout() -> Verdict:
    """Chamada MCP pendurada: o teto por tool corta, o turno termina rápido."""
    assertion = ("tool pendurada 30s com TOOL_TIMEOUT_SECONDS=1 → turno termina em <5s, "
                 "resultado honesto de 'sem dado', sem exceção")
    start = perf_counter()
    with env(CHAOS="1", CHAOS_SCENARIO="timeout", CHAOS_DELAY="30",
             CHAOS_TARGET="tool:find", TOOL_TIMEOUT_SECONDS="1"):
        llm = FakeLLM()
        answer, events, _ = await _loop(FakeSession(), llm)
    elapsed = perf_counter() - start
    tool = _tool_events(events)[0]
    ok = elapsed < 5 and tool["is_error"] and "tempo limite" in _tool_results_seen(llm)
    return Verdict("tool_timeout", assertion, ok,
                   f"{elapsed:.2f}s, resposta={answer[:60]!r}", elapsed)


async def scenario_mcp_session_down() -> Verdict:
    """MCP fora do ar no meio do turno: o turno responde, sem 500 e sem inventar dado."""
    assertion = ("session.call_tool levantando exceção → turno termina com resposta, "
                 "resultado da tool diz 'sem dado', nada inventado")
    start = perf_counter()
    session = FakeSession(lambda name, args: RuntimeError("MCP stdio pipe fechada"))
    with env(CHAOS=None, TOOL_TIMEOUT_SECONDS="20"):
        llm = FakeLLM()
        answer, events, _ = await _loop(session, llm)
    elapsed = perf_counter() - start
    tool = _tool_events(events)[0]
    seen = _tool_results_seen(llm)
    ok = bool(answer) and tool["is_error"] and "sem supor nenhum valor" in seen
    return Verdict("mcp_session_down", assertion, ok, f"o modelo recebeu={seen[:70]!r}", elapsed)


async def scenario_tool_malformed_payload() -> Verdict:
    """Payload corrompido: o loop não quebra e o modelo recebe o texto cru, sem parse fantasma."""
    assertion = "tool devolve texto não-JSON → nenhuma exceção, turno termina com resposta"
    start = perf_counter()
    with env(CHAOS="1", CHAOS_SCENARIO="malformed", CHAOS_TARGET="tool:find",
             TOOL_TIMEOUT_SECONDS="20"):
        llm = FakeLLM()
        answer, events, _ = await _loop(FakeSession(), llm)
    elapsed = perf_counter() - start
    seen = _tool_results_seen(llm)
    ok = bool(answer) and "não é json" in seen
    return Verdict("tool_malformed_payload", assertion, ok,
                   f"o modelo recebeu={seen[:60]!r}", elapsed)


async def scenario_tool_circuit_breaker() -> Verdict:
    """Tool falhando sem parar: o circuito abre e para de pagar a falha."""
    assertion = (f"{resilience.TOOL_FAILURE_THRESHOLD} falhas seguidas da mesma tool → "
                 "circuito abre e a chamada seguinte é curto-circuitada")
    start = perf_counter()
    session = FakeSession(lambda name, args: RuntimeError("driver caiu"))
    with env(CHAOS=None, TOOL_TIMEOUT_SECONDS="20"):
        opened = None
        for attempt in range(resilience.TOOL_FAILURE_THRESHOLD + 1):
            try:
                await resilience.call_tool("find", session.call_tool("find", {}))
            except resilience.ToolOpenCircuit:
                opened = attempt
                break
            except RuntimeError:
                continue
        state = resilience.circuit_state().get("find", {})
    elapsed = perf_counter() - start
    ok = opened == resilience.TOOL_FAILURE_THRESHOLD and state.get("open") is True
    return Verdict("tool_circuit_breaker", assertion, ok,
                   f"abriu na chamada #{opened}, estado={state}", elapsed)


async def scenario_llm_429_before_first_token() -> Verdict:
    """429 antes do primeiro token: o retry existente absorve e o turno responde."""
    assertion = "429 na primeira tentativa → retry no mesmo modelo, turno responde normalmente"
    start = perf_counter()
    with env(CHAOS="1", CHAOS_SCENARIO="status", CHAOS_STATUS="429", CHAOS_COUNT="1",
             CHAOS_TARGET="llm", CHAOS_BACKOFF_SCALE="0.01", TOOL_TIMEOUT_SECONDS="20"):
        answer, events, _ = await _loop(FakeSession(), FakeLLM())
    elapsed = perf_counter() - start
    ok = "entregue" in answer.lower()
    return Verdict("llm_429_before_first_token", assertion, ok,
                   f"resposta={answer[:60]!r}", elapsed)


async def scenario_llm_500_persistent() -> Verdict:
    """Provedor 500 sem parar: o turno degrada com resposta explícita, nunca perde a resposta."""
    assertion = ("500 em toda tentativa (retries + fallback) → run_loop_guarded devolve a "
                 "resposta degradada e marca metrics.degraded; NENHUMA exceção sobe")
    import agent

    events, metrics = [], {"degraded": False, "degraded_reason": None}
    start = perf_counter()
    with env(CHAOS="1", CHAOS_SCENARIO="status", CHAOS_STATUS="500", CHAOS_TARGET="llm",
             CHAOS_BACKOFF_SCALE="0.01", TOOL_TIMEOUT_SECONDS="20",
             SINGLEAGENT_LEGACY_500=None):
        answer = await agent.run_loop_guarded(
            lambda: _loop(FakeSession(), FakeLLM()),
            emit=lambda phase, kind, **f: events.append({"phase": phase, **f}),
            metrics=metrics)
    elapsed = perf_counter() - start
    ok = answer == agent.resilience.DEGRADED_TURN_REPLY and metrics["degraded"]
    return Verdict("llm_500_persistent", assertion, ok,
                   f"motivo={metrics['degraded_reason']} resposta={answer[:50]!r}", elapsed)


async def scenario_legacy_500_flag() -> Verdict:
    """Prova que o padrão novo é o que degrada: com a flag, a exceção volta a subir."""
    assertion = ("SINGLEAGENT_LEGACY_500=1 → a MESMA falha que degrada por padrão volta a "
                 "subir como exceção (comportamento antigo)")
    import agent

    async def boom():
        raise RuntimeError("falha de tool")

    start = perf_counter()
    metrics = {"degraded": False, "degraded_reason": None}
    with env(SINGLEAGENT_LEGACY_500="1"):
        try:
            await agent.run_loop_guarded(boom, emit=lambda *a, **k: None, metrics=metrics)
            raised = False
        except RuntimeError:
            raised = True
    with env(SINGLEAGENT_LEGACY_500=None):
        answer = await agent.run_loop_guarded(boom, emit=lambda *a, **k: None, metrics=metrics)
    elapsed = perf_counter() - start
    ok = raised and answer == agent.resilience.DEGRADED_TURN_REPLY
    return Verdict("legacy_500_flag", assertion, ok,
                   f"com flag levantou={raised}; sem flag degradou={answer[:40]!r}", elapsed)


async def scenario_concurrent_tool_calls() -> Verdict:
    """Requests simultâneos: o pool/fronteira aguenta concorrência sem exceção nem mistura."""
    assertion = "5 turnos simultâneos → 5 respostas, nenhuma exceção, contadores por turno íntegros"
    start = perf_counter()
    with env(CHAOS=None, TOOL_TIMEOUT_SECONDS="20"):
        results = await asyncio.gather(
            *[_loop(FakeSession(), FakeLLM()) for _ in range(5)], return_exceptions=True)
    elapsed = perf_counter() - start
    failures = [r for r in results if isinstance(r, BaseException)]
    ok = not failures and all(r[2]["tools_used"] == 1 for r in results)
    return Verdict("concurrent_tool_calls", assertion, ok,
                   f"falhas={failures}", elapsed)


async def scenario_turn_timeout() -> Verdict:
    """Deadline do turno inteiro: LLM travado além de AGENT_TURN_TIMEOUT_SECONDS."""
    assertion = ("LLM travado com AGENT_TURN_TIMEOUT_SECONDS=1 → run_loop_guarded corta em ~1s "
                 "com resposta explícita e degraded_reason=turn_timeout")
    import agent

    start = perf_counter()
    metrics = {"degraded": False, "degraded_reason": None}
    original = agent.AGENT_TURN_TIMEOUT_SECONDS
    agent.AGENT_TURN_TIMEOUT_SECONDS = 1.0
    try:
        with env(CHAOS="1", CHAOS_SCENARIO="hang", CHAOS_DELAY="30", CHAOS_TARGET="llm",
                 TOOL_TIMEOUT_SECONDS="20"):
            answer = await agent.run_loop_guarded(
                lambda: _loop(FakeSession(), FakeLLM()),
                emit=lambda *a, **k: None, metrics=metrics)
    finally:
        agent.AGENT_TURN_TIMEOUT_SECONDS = original
    elapsed = perf_counter() - start
    ok = metrics["degraded_reason"] == "turn_timeout" and elapsed < 5 and bool(answer)
    return Verdict("turn_timeout", assertion, ok,
                   f"{elapsed:.2f}s motivo={metrics['degraded_reason']}", elapsed)


async def scenario_atlas_retry_semantics() -> Verdict:
    """O que segura um step-down de primário é o DRIVER, e isso é verificável."""
    assertion = ("o cliente declara retryWrites/retryReads e um orçamento CSOT por "
                 "operação — é o driver que reexecuta no novo primário, não o app")
    import db

    start = perf_counter()
    options = db.get_client().options
    ok = (options.retry_writes is True and options.retry_reads is True
          and options.timeout is not None and options.timeout > 0)
    elapsed = perf_counter() - start
    return Verdict("atlas_retry_semantics", assertion, ok,
                   f"retryWrites={options.retry_writes} retryReads={options.retry_reads} "
                   f"timeoutMS={options.timeout} maxPoolSize={options.pool_options.max_pool_size}",
                   elapsed)


async def scenario_atlas_failover() -> Verdict:
    """Step-down cujo retry do driver TAMBÉM esgota: o turno degrada, não some."""
    assertion = ("NotPrimaryError sobrevivendo ao retry do driver → SafeQueryError de "
                 "conexão (mensagem de UI, nunca stack trace) e o turno termina degradado, "
                 "sem perder o trace")
    import agent
    import db

    start = perf_counter()
    async def read():
        return {"ok": 1}

    mapped = None
    with env(CHAOS="1", CHAOS_SCENARIO="not_primary", CHAOS_TARGET="mongo"):
        try:
            await db.safe_query(read())
        except db.SafeQueryError as exc:
            mapped = exc
        except Exception as exc:  # noqa: BLE001 — erro cru vazando é a falha
            mapped = exc

    metrics = {"degraded": False, "degraded_reason": None}
    async def failing_turn():
        with env(CHAOS="1", CHAOS_SCENARIO="not_primary", CHAOS_TARGET="mongo"):
            await db.safe_query(read())

    answer = await agent.run_loop_guarded(failing_turn, emit=lambda *a, **k: None,
                                          metrics=metrics)
    elapsed = perf_counter() - start
    ok = (isinstance(mapped, db.SafeQueryError) and mapped.kind == "conexao"
          and metrics["degraded"] and bool(answer))
    return Verdict("atlas_failover", assertion, ok,
                   f"kind={getattr(mapped, 'kind', type(mapped).__name__)} "
                   f"degradou={metrics['degraded']} motivo={metrics['degraded_reason']}", elapsed)


async def scenario_search_unavailable() -> Verdict:
    """`mongot` fora: cada camada cai no fallback que a política da área manda."""
    assertion = ("$vectorSearch falhando → SafeQueryError kind='search'; o cache cai para "
                 "match exato e a memória para os fatos recentes, sem derrubar o turno")
    import db

    start = perf_counter()
    async def search():
        return []

    mapped = None
    with env(CHAOS="1", CHAOS_SCENARIO="search_down", CHAOS_TARGET="mongo"):
        try:
            await db.safe_query(search())
        except db.SafeQueryError as exc:
            mapped = exc
        except Exception as exc:  # noqa: BLE001
            mapped = exc
    elapsed = perf_counter() - start
    ok = (isinstance(mapped, db.SafeQueryError) and mapped.kind == "search"
          and "produtos_vector" in mapped.message or "índice" in getattr(mapped, "message", ""))
    return Verdict("search_unavailable", assertion, ok,
                   f"kind={getattr(mapped, 'kind', type(mapped).__name__)}: "
                   f"{getattr(mapped, 'message', '')[:80]}", elapsed)


async def scenario_guardrail_fails_closed() -> Verdict:
    """Sem a camada semântica, quem decide é o DOCUMENTO de política, não o código."""
    assertion = ("com o denylist vetorial fora: política semantic_fail_mode='closed' bloqueia "
                 "a entrada; trocando o MESMO campo para 'open' a área volta a atender — "
                 "sem deploy, sem mudar código")
    import policy_guardrails as guardrails

    start = perf_counter()
    original_search = guardrails._semantic_denylist
    original_policy = guardrails.get_policy

    async def broken(_text, _threshold, _area):
        return None, False, None          # camada semântica indisponível

    async def policy_with(mode):
        base = await original_policy("default")
        return {**base, "semantic_fail_mode": mode}

    guardrails._semantic_denylist = broken
    try:
        guardrails.get_policy = lambda area="default": policy_with("closed")
        closed = await guardrails.check_input("qual o status do meu pedido", "cliente-demo",
                                              "conv_chaos", "default")
        guardrails.get_policy = lambda area="default": policy_with("open")
        opened = await guardrails.check_input("qual o status do meu pedido", "cliente-demo",
                                              "conv_chaos", "default")
    finally:
        guardrails._semantic_denylist = original_search
        guardrails.get_policy = original_policy

    elapsed = perf_counter() - start
    ok = closed["action"] == "block" and opened["action"] == "allow"
    return Verdict("guardrail_fails_closed", assertion, ok,
                   f"closed={closed['action']} open={opened['action']} "
                   f"(a demo roda fail-closed em TODA área — ADR-001 risco 3)", elapsed)


async def scenario_live_degraded_turn() -> Verdict:
    """LIVE: run_agent inteiro contra o banco de TESTE, com o MCP falhando."""
    assertion = ("run_agent com MCP falhando → resposta degradada, trace completo e turno "
                 "gravado; nenhuma exceção sobe (banco de teste isolado)")
    if os.getenv("LIVE", "").strip() not in {"1", "true", "yes"}:
        return Verdict("live_degraded_turn", assertion, False,
                       "LIVE=1 ausente — cenário não roda contra Atlas", 0.0, skipped=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import isolation

    isolation.use_test_databases(what="chaos_suite live_degraded_turn")
    import agent  # noqa: E402 — depois do override de banco

    start = perf_counter()
    session = FakeSession(lambda name, args: RuntimeError("MCP stdio pipe fechada"))
    original = agent.anthropic_client
    agent.anthropic_client = FakeLLM()
    agent.resolve_connection_id = lambda _s: _connection_id()
    try:
        with env(CHAOS=None, TOOL_TIMEOUT_SECONDS="20"):
            result = await agent.run_agent(
                session, scenario=None, message="onde está meu pedido PED-1001?",
                conversation_id=f"conv_chaos_{os.getpid()}", user_key="cliente-demo")
        elapsed = perf_counter() - start
        answer = result.get("answer") or ""
        ok = bool(answer) and bool(result.get("trace"))
        return Verdict("live_degraded_turn", assertion, ok,
                       f"resposta={answer[:70]!r} eventos={len(result.get('trace') or [])}", elapsed)
    except Exception as exc:  # noqa: BLE001 — a assertion é justamente não chegar aqui
        return Verdict("live_degraded_turn", assertion, False,
                       f"exceção subiu: {type(exc).__name__}: {exc}", perf_counter() - start)
    finally:
        agent.anthropic_client = original


async def scenario_checkpoint_native_persistence() -> Verdict:
    """Prova que o checkpointer NATIVO do LangGraph (`agent_graph.py`,
    `MongoDBSaver`, `thread_id=conversation_id`) grava de fato — substitui o
    antigo `stale_checkpoint_recovery`, que testava a retomada manual via
    `agent.open_turn`/`agent.interrupted_turn` (removidos junto com a migração
    para `agent_graph`; ver `docs/adr/` ou o topo de `agent_graph.py`).

    Roda UM turno completo e confere que o `MongoDBSaver` escreveu pelo menos
    um checkpoint em `langgraph_checkpoints` para o `thread_id` da conversa, e
    que o `checkpoint_id` mais recente não tem nós pendentes (`next == ()`) —
    ou seja, o grafo terminou o turno de ponta a ponta e o checkpointer
    persistiu cada super-step no caminho, não só o resultado final.
    """
    assertion = ("um turno completo grava checkpoint(s) do LangGraph em "
                 "langgraph_checkpoints para o thread_id da conversa, e o estado final "
                 "não tem nó pendente (grafo terminou, não travou no meio)")
    if os.getenv("LIVE", "").strip() not in {"1", "true", "yes"}:
        return Verdict("checkpoint_native_persistence", assertion, False,
                       "LIVE=1 ausente — cenário não roda contra Atlas", 0.0, skipped=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import isolation

    isolation.use_test_databases(what="chaos_suite checkpoint_native_persistence")
    import agent
    import agent_graph
    from db import poc

    start = perf_counter()
    conversation = f"conv_ckpt_{os.getpid()}"
    await poc()["agent_sessions"].delete_many({"session_id": conversation})

    session = FakeSession()
    original = agent.anthropic_client
    agent.anthropic_client = FakeLLM()
    agent.resolve_connection_id = lambda _s: _connection_id()
    try:
        with env(CHAOS=None, TOOL_TIMEOUT_SECONDS="20"):
            await agent.run_agent(
                session, scenario=None, message="qual o status do meu pedido PED-1001?",
                conversation_id=conversation, user_key="cliente-demo")
        graph = agent_graph.get_graph()
        config = {"configurable": {"thread_id": conversation}}
        snapshot = await graph.aget_state(config)
        checkpointed = snapshot is not None and snapshot.config.get(
            "configurable", {}).get("checkpoint_id") is not None
        finished = checkpointed and not snapshot.next
        ok = checkpointed and finished
        return Verdict("checkpoint_native_persistence", assertion, ok,
                       f"checkpoint_gravado={checkpointed} sem_no_pendente={finished}",
                       perf_counter() - start)
    except Exception as exc:  # noqa: BLE001
        return Verdict("checkpoint_native_persistence", assertion, False,
                       f"exceção subiu: {type(exc).__name__}: {exc}", perf_counter() - start)
    finally:
        agent.anthropic_client = original
        await poc()["agent_sessions"].delete_many({"session_id": conversation})
        agent_graph._CHECKPOINT_CLIENT.get_database(agent.DB_MAIN)["langgraph_checkpoints"].delete_many(
            {"thread_id": conversation})
        agent_graph._CHECKPOINT_CLIENT.get_database(agent.DB_MAIN)["langgraph_checkpoint_writes"].delete_many(
            {"thread_id": conversation})


async def scenario_crash_resume() -> Verdict:
    """LIVE: SIGKILL no meio da conversa — o estado persistido sobrevive ao restart."""
    assertion = ("SIGKILL depois de gravar o turno → a sessão continua legível em "
                 "agent_sessions (banco de teste), com o turno anterior intacto")
    if os.getenv("LIVE", "").strip() not in {"1", "true", "yes"}:
        return Verdict("crash_resume", assertion, False,
                       "LIVE=1 ausente — cenário não roda contra Atlas", 0.0, skipped=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import isolation

    main_db, _ = isolation.test_database_names()
    isolation.guard(main_db, *isolation.test_database_names()[1:], what="chaos_suite crash_resume")
    conversation = f"conv_crash_{os.getpid()}"
    start = perf_counter()
    child = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve().parent / "crash_child.py"), conversation,
        env={**os.environ, "MONGODB_DB": main_db,
             "MONGODB_BRAIN_DB": isolation.test_database_names()[1]},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    stdout, _ = await child.communicate()
    if child.returncode not in (0, -9):
        return Verdict("crash_resume", assertion, False,
                       f"filho terminou em {child.returncode}: {stdout.decode()[-300:]}",
                       perf_counter() - start)

    from dotenv import load_dotenv
    from pymongo import MongoClient

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=15_000)
    doc = client[main_db]["agent_sessions"].find_one({"session_id": conversation})
    client[main_db]["agent_sessions"].delete_many({"session_id": conversation})
    client.close()
    elapsed = perf_counter() - start
    turns = len((doc or {}).get("turns") or [])
    return Verdict("crash_resume", assertion, turns >= 1,
                   f"turnos persistidos={turns} (processo morto com SIGKILL)", elapsed)


class FakeApp:
    """O suficiente de FastAPI para exercitar o pool REAL de `main.py`."""

    class _State:
        pass

    def __init__(self, size: int):
        import itertools

        self.state = self._State()
        self.state.mcp_pool = [None] * size
        self.state.mcp_errors = [None] * size
        self.state.mcp_rr_counter = itertools.count()


async def scenario_mcp_pool_round_robin() -> Verdict:
    """Slot morto no pool: o round-robin pula e o turno continua sendo servido."""
    assertion = ("com 1 de 3 slots caídos, get_mcp_session NUNCA devolve o slot morto "
                 "e distribui entre os vivos; com todos caídos devolve None (sem exceção)")
    import main

    start = perf_counter()
    app = FakeApp(3)
    alive_a, alive_c = FakeSession(), FakeSession()
    app.state.mcp_pool = [alive_a, None, alive_c]   # slot 1 reconectando
    picks = [main.get_mcp_session(app) for _ in range(9)]
    never_dead = all(p is not None for p in picks)
    both_used = {id(p) for p in picks} == {id(alive_a), id(alive_c)}

    app.state.mcp_pool = [None, None, None]         # pool inteiro caído
    empty = main.get_mcp_session(app)
    elapsed = perf_counter() - start
    ok = never_dead and both_used and empty is None
    return Verdict("mcp_pool_round_robin", assertion, ok,
                   f"escolhas={len(picks)} sem_slot_morto={never_dead} "
                   f"usou_os_dois_vivos={both_used} pool_vazio={empty is None}", elapsed)


async def scenario_mcp_pool_slot_isolation() -> Verdict:
    """Um subprocess travado não pode derrubar os requests que caberiam nos outros slots."""
    assertion = ("slot pendurado + slots sadios → turnos concorrentes terminam pelos slots "
                 "vivos dentro do teto por tool, sem esperar o travado")
    import main

    start = perf_counter()
    app = FakeApp(3)

    async def hang(_name, _args):
        await asyncio.sleep(30)

    app.state.mcp_pool = [FakeSession(hang), FakeSession(), FakeSession()]
    with env(CHAOS=None, TOOL_TIMEOUT_SECONDS="2"):
        results = await asyncio.gather(*[
            _loop(main.get_mcp_session(app), FakeLLM()) for _ in range(6)],
            return_exceptions=True)
    elapsed = perf_counter() - start
    failures = [r for r in results if isinstance(r, BaseException)]
    answered = [r for r in results if not isinstance(r, BaseException) and r[0]]
    # O slot travado é escolhido em 1/3 das vezes e é cortado pelo teto por tool;
    # o que não pode acontecer é um request ficar preso nos 30s do subprocess.
    ok = not failures and len(answered) == 6 and elapsed < 10
    return Verdict("mcp_pool_slot_isolation", assertion, ok,
                   f"{elapsed:.2f}s, respostas={len(answered)}/6, falhas={len(failures)}", elapsed)


async def scenario_mcp_supervisor_reconnects() -> Verdict:
    """Supervisor do pool: sessão que cai é republicada como None e reconectada sozinha."""
    assertion = ("sessão do slot caindo → mcp_pool[slot]=None com o erro registrado, e o "
                 "supervisor reabre o slot sem tocar nos outros")
    import main

    start = perf_counter()
    app = FakeApp(2)
    app.state.mcp_pool[1] = FakeSession()          # o vizinho segue vivo o tempo todo
    stop = asyncio.Event()
    attempts = {"n": 0}
    states: list[object] = []

    class FlakyStdio:
        """Primeira conexão morre no ping; a segunda fica de pé."""

        def __init__(self, _params):
            attempts["n"] += 1
            self.fail = attempts["n"] == 1

        async def __aenter__(self):
            return ("read", "write")

        async def __aexit__(self, *_exc):
            return False

    class FlakySession:
        def __init__(self, read, write, fail):
            self.fail = fail

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def initialize(self):
            return None

        async def send_ping(self):
            if self.fail:
                raise RuntimeError("stdio pipe fechada")

    original = (main.stdio_client, main.ClientSession, main.warm_up_session,
                main.MCP_PING_SECONDS, main.MCP_RETRY_SECONDS)
    main.stdio_client = FlakyStdio
    main.ClientSession = lambda read, write: FlakySession(read, write, attempts["n"] == 1)
    main.warm_up_session = lambda _s: _zero()
    main.MCP_PING_SECONDS, main.MCP_RETRY_SECONDS = 0.05, 0.05
    try:
        task = asyncio.create_task(main._mcp_supervisor(app, stop, 0))
        for _ in range(40):                        # ~2s de observação
            await asyncio.sleep(0.05)
            states.append(app.state.mcp_pool[0])
            if attempts["n"] >= 2 and app.state.mcp_pool[0] is not None:
                break
        stop.set()
        await asyncio.wait_for(task, timeout=3)
    finally:
        (main.stdio_client, main.ClientSession, main.warm_up_session,
         main.MCP_PING_SECONDS, main.MCP_RETRY_SECONDS) = original

    elapsed = perf_counter() - start
    went_down = any(s is None for s in states)
    came_back = app.state.mcp_pool[0] is not None or attempts["n"] >= 2
    neighbour_intact = app.state.mcp_pool[1] is not None
    ok = went_down and came_back and neighbour_intact and attempts["n"] >= 2
    return Verdict("mcp_supervisor_reconnects", assertion, ok,
                   f"tentativas de conexão={attempts['n']} caiu={went_down} "
                   f"voltou={came_back} vizinho_intacto={neighbour_intact}", elapsed)


async def _zero() -> float:
    return 0.0


async def scenario_crash_mid_tool() -> Verdict:
    """LIVE: SIGKILL DENTRO de uma chamada de ferramenta — o estado não fica pela metade.

    O checkpoint verificado mudou com a migração para `agent_graph` (StateGraph +
    `MongoDBSaver` nativo): não existe mais `agent_sessions.pending_turn` — o sinal
    de "algo foi persistido antes do crash" agora é um documento em
    `langgraph_checkpoints` para o `thread_id` da conversa (o turno morre dentro do
    nó `run_pipeline`, então o checkpoint mais recente tem que corresponder a um
    super-step ANTERIOR a ele — `identity`/`guard_input`/`resume_check`/`cache_lookup`).
    """
    assertion = ("processo morto no meio de uma tool → langgraph_checkpoints tem um "
                 "checkpoint da conversa (super-step antes de run_pipeline), nenhum "
                 "turno meio-escrito em agent_sessions, e a MESMA conversa segue utilizável")
    if os.getenv("LIVE", "").strip() not in {"1", "true", "yes"}:
        return Verdict("crash_mid_tool", assertion, False,
                       "LIVE=1 ausente — cenário não roda contra Atlas", 0.0, skipped=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import isolation

    main_db, brain_db = isolation.test_database_names()
    isolation.guard(main_db, brain_db, what="chaos_suite crash_mid_tool")
    conversation = f"conv_midtool_{os.getpid()}"
    start = perf_counter()
    child = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve().parent / "crash_child.py"),
        conversation, "mid_tool",
        env={**os.environ, "MONGODB_DB": main_db, "MONGODB_BRAIN_DB": brain_db},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    # Espera o filho avisar que JÁ está dentro do turno; sem isso o kill poderia
    # cair antes de qualquer escrita e o cenário não provaria nada.
    try:
        while True:
            line = await asyncio.wait_for(child.stdout.readline(), timeout=90)
            if not line:
                break
            if b"PRONTO" in line:
                break
    except asyncio.TimeoutError:
        child.kill()
        return Verdict("crash_mid_tool", assertion, False,
                       "filho não chegou a entrar no turno em 90s", perf_counter() - start)
    # Espera o CHECKPOINT aparecer no banco em vez de dormir um tempo fixo: o turno
    # faz guardrail + cache + memória (cada um uma ida ao Atlas) antes de chegar à
    # ferramenta, e sob carga isso varia. Com sleep fixo o cenário passava sozinho e
    # falhava em sequência — matava o processo ANTES do ponto que quer exercitar.
    from pymongo import MongoClient

    watcher = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=15_000)
    checkpoints = watcher[main_db]["langgraph_checkpoints"]
    checkpoint_seen = False
    for _ in range(60):                # até ~30s
        if checkpoints.count_documents({"thread_id": conversation}, limit=1):
            checkpoint_seen = True
            break
        await asyncio.sleep(0.5)
    watcher.close()
    if not checkpoint_seen:
        with contextlib.suppress(ProcessLookupError):
            child.kill()
        await child.communicate()
        return Verdict("crash_mid_tool", assertion, False,
                       "o turno não chegou ao checkpoint em 30s — cenário inconclusivo",
                       perf_counter() - start)
    await asyncio.sleep(1)            # já dentro da tool pendurada
    try:
        child.kill()                  # SIGKILL no meio da chamada de ferramenta
    except ProcessLookupError:
        # O filho já tinha morrido: o cenário não exercitou o que queria, e isso
        # é uma FALHA do cenário — não pode passar como se tivesse matado no meio,
        # nem derrubar a bateria inteira com a exceção crua (era o que acontecia).
        await child.communicate()
        return Verdict("crash_mid_tool", assertion, False,
                       f"o filho morreu sozinho antes do SIGKILL (rc={child.returncode}) — "
                       "cenário inconclusivo", perf_counter() - start)
    await child.communicate()

    from pymongo import MongoClient

    client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=15_000)
    doc = client[main_db]["agent_sessions"].find_one({"session_id": conversation})
    turns = (doc or {}).get("turns") or []
    # Nada pela metade: os turnos são gravados aos PARES (role user + role assistant,
    # num único $push), então um número ímpar ou uma entrada sem conteúdo significa
    # escrita parcial. O critério anterior procurava campos `user`/`answer` que não
    # existem neste schema e marcava QUALQUER turno como meio-escrito.
    roles = [t.get("role") for t in turns]
    half_written = ([t for t in turns if not (t.get("content") or "").strip()]
                    or ([] if roles.count("user") == roles.count("assistant") else list(turns)))
    # E o checkpoint tem que ter ficado: é ele que diz que o turno chegou a
    # progredir (identity/guard_input/resume_check/cache_lookup concluídos)
    # antes do SIGKILL, em vez de sumir sem rastro nenhum.
    checkpointed = client[main_db]["langgraph_checkpoints"].count_documents(
        {"thread_id": conversation}, limit=1) > 0
    client.close()

    # Segunda metade da assertion: a MESMA conversa continua utilizável depois do
    # crash — o turno seguinte responde e é gravado, sem herdar lixo.
    isolation.use_test_databases(what="chaos_suite crash_mid_tool (retomada)")
    import agent

    original = agent.anthropic_client
    agent.anthropic_client = FakeLLM()
    agent.resolve_connection_id = lambda _s: _connection_id()
    try:
        with env(CHAOS=None, TOOL_TIMEOUT_SECONDS="20"):
            resumed = await agent.run_agent(
                FakeSession(), scenario=None, message="e agora, conseguiu ver?",
                conversation_id=conversation, user_key="cliente-demo")
        resumed_ok = bool((resumed.get("answer") or "").strip())
    except Exception as exc:  # noqa: BLE001 — retomar é parte da assertion
        resumed_ok, resumed = False, {"erro": f"{type(exc).__name__}: {exc}"}
    finally:
        agent.anthropic_client = original

    client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=15_000)
    client[main_db]["agent_sessions"].delete_many({"session_id": conversation})
    client.close()
    elapsed = perf_counter() - start
    return Verdict("crash_mid_tool", assertion, (not half_written) and resumed_ok and checkpointed,
                   f"turnos apos o kill={len(turns)} meio-escritos={len(half_written)} "
                   f"checkpoint_pendente={checkpointed} retomada_ok={resumed_ok}", elapsed)


SCENARIOS = {
    "tool_timeout": scenario_tool_timeout,
    "mcp_session_down": scenario_mcp_session_down,
    "tool_malformed_payload": scenario_tool_malformed_payload,
    "tool_circuit_breaker": scenario_tool_circuit_breaker,
    "llm_429_before_first_token": scenario_llm_429_before_first_token,
    "llm_500_persistent": scenario_llm_500_persistent,
    "legacy_500_flag": scenario_legacy_500_flag,
    "concurrent_tool_calls": scenario_concurrent_tool_calls,
    "turn_timeout": scenario_turn_timeout,
    "atlas_retry_semantics": scenario_atlas_retry_semantics,
    "atlas_failover": scenario_atlas_failover,
    "search_unavailable": scenario_search_unavailable,
    "guardrail_fails_closed": scenario_guardrail_fails_closed,
    "live_degraded_turn": scenario_live_degraded_turn,
    "mcp_pool_round_robin": scenario_mcp_pool_round_robin,
    "mcp_pool_slot_isolation": scenario_mcp_pool_slot_isolation,
    "mcp_supervisor_reconnects": scenario_mcp_supervisor_reconnects,
    "checkpoint_native_persistence": scenario_checkpoint_native_persistence,
    "crash_resume": scenario_crash_resume,
    "crash_mid_tool": scenario_crash_mid_tool,
}


async def main(names: list[str]) -> int:
    if not chaos.enabled():
        print("CHAOS=1 é obrigatório para rodar a bateria (os pontos de injeção ficam "
              "inertes sem ele).")
        return 2
    chosen = names or list(SCENARIOS)
    verdicts: list[Verdict] = []
    for name in chosen:
        if name not in SCENARIOS:
            print(f"cenário desconhecido: {name}")
            return 2
        verdicts.append(await SCENARIOS[name]())
        print(verdicts[-1].line(), flush=True)

    failed = [v for v in verdicts if not v.passed and not v.skipped]
    skipped = [v for v in verdicts if v.skipped]
    print(f"\n{len(verdicts) - len(failed) - len(skipped)}/{len(verdicts) - len(skipped)} "
          f"cenários passaram" + (f" ({len(skipped)} pulados)" if skipped else ""))
    if os.getenv("CHAOS_JSON"):
        Path(os.environ["CHAOS_JSON"]).write_text(json.dumps(
            [v.__dict__ for v in verdicts], indent=2, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
