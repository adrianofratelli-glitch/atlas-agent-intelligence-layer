"""FastAPI — MongoDB Intelligence Layer POC.

Tabs:
  1. Flexible schema → /api/templates, /api/templates/{id}/variant
  2. Model swap      → /api/model-config, /api/model-config/swap, /api/chat/quick
  3. Agent           → /api/agent/scenarios | run  (autonomous loop via MongoDB MCP Server)

A sessão com o MongoDB MCP Server é gerida por um SUPERVISOR em background:
uma task própria abre a sessão stdio, faz ping periódico e reconecta com
backoff se o processo morrer — o agente se recupera sozinho, sem reiniciar o
backend. (Enter/exit dos context managers ficam na MESMA task, exigência dos
cancel scopes do anyio usados pelo cliente MCP.)
"""

import asyncio
import itertools
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from bson import ObjectId
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field

import auth
import cache
import policy_guardrails as guardrails
import guidance
import memory
import observability
import profiles
import turn_classifier
from agent import (
    DEFAULT_USER_KEY,
    AREA_SCENARIOS,
    DEMO_PLAYLIST,
    WRITE_TOOLS,
    forget_connection_id,
    list_agent_tools,
    mcp_server_params,
    run_agent,
    warm_up_session,
)
from db import MAX_TIME_MS, SESSION_IDLE_SECONDS, SafeQueryError, ai_brain, get_client, poc, safe_query
from llm import call_with_fallback, get_active_config

observability.setup_logging()
logger = logging.getLogger("poc.main")

MCP_PING_SECONDS = 30      # intervalo do health-check de cada sessão MCP
MCP_RETRY_SECONDS = 5      # backoff entre tentativas de reconexão de UM slot

# Pool de sessões MCP: uma sessão stdio única serializava TODO o backend — toda
# call_tool concorrente esperava na mesma pipe, e um subprocess travado derrubava
# TODOS os requests em voo de uma vez. N subprocessos independentes, cada um com
# seu próprio supervisor de reconexão, distribuídos por round-robin.
MCP_POOL_SIZE = max(1, int(os.getenv("MCP_POOL_SIZE", "3")))


async def _mcp_supervisor(app: FastAPI, stop: asyncio.Event, slot: int) -> None:
    """Dona do ciclo de vida de UMA sessão MCP do pool (índice `slot`): abre,
    monitora (ping), reconecta — sem afetar as outras sessões do pool.

    Tudo acontece nesta task — o stdio_client usa cancel scopes do anyio que
    não podem ser abertos numa task e fechados em outra.
    """
    while not stop.is_set():
        session = None
        try:
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(mcp_server_params()))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                app.state.mcp_pool[slot] = session
                app.state.mcp_errors[slot] = None
                # Antes de anunciar a sessão como pronta: aquece o caminho de aggregate,
                # senão o primeiro turno da demo que usar catálogo ou cadeia de trocas
                # paga ~5 s de carga sob demanda dentro do MCP Server.
                warm_ms = await warm_up_session(session)
                logger.info("sessão MongoDB MCP Server [slot %d] estabelecida (aggregate aquecido em %d ms)",
                            slot, int(warm_ms))
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=MCP_PING_SECONDS)
                    except asyncio.TimeoutError:
                        await session.send_ping()  # falhou → reconecta lá fora
        except Exception as exc:  # noqa: BLE001 — sessão caiu → reconectar só ESTE slot
            app.state.mcp_pool[slot] = None
            app.state.mcp_errors[slot] = str(exc)
            if not stop.is_set():
                logger.warning("sessão MCP [slot %d] indisponível (%s) — reconectando em %ss",
                               slot, str(exc)[:200], MCP_RETRY_SECONDS)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=MCP_RETRY_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            app.state.mcp_pool[slot] = None
            if session is not None:
                forget_connection_id(session)  # limpa _CONNECTION_IDS — não cresce sem teto


def get_mcp_session(app: FastAPI) -> ClientSession | None:
    """Round-robin simples sobre o pool: escolhe a próxima sessão viva.

    Se o slot sorteado estiver caído (reconectando), tenta os outros antes de
    desistir — um subprocess travado não derruba requests que caberiam nos
    outros slots do pool.
    """
    pool = getattr(app.state, "mcp_pool", None) or []
    if not pool:
        return None
    n = len(pool)
    start = next(app.state.mcp_rr_counter) % n
    for offset in range(n):
        session = pool[(start + offset) % n]
        if session is not None:
            return session
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Sobe o supervisor da sessão MCP. Se o MCP não subir (npx ausente, Atlas
    inacessível), o app continua no ar — só o endpoint do agente reporta o erro,
    via Banner amigável — e o supervisor segue tentando reconectar."""
    validate_runtime_security(
        ENVIRONMENT,
        ADMIN_API_KEY,
        os.getenv("JWT_SECRET", ""),
        auth.AUTH_REQUIRED,
        DEMO_TOKEN_ISSUANCE_ENABLED,
        CORS_ORIGINS,
    )
    # Tracing distribuído (pov-shared): sink por env, PII sempre mascarada.
    logger.info("tracing: sink=%s", observability.init_tracing_once())
    app.state.mcp_pool = [None] * MCP_POOL_SIZE
    app.state.mcp_errors = ["pool MCP ainda inicializando"] * MCP_POOL_SIZE
    app.state.mcp_rr_counter = itertools.count()
    stop = asyncio.Event()
    pool_tasks = [
        asyncio.create_task(_mcp_supervisor(app, stop, slot))
        for slot in range(MCP_POOL_SIZE)
    ]
    janitor_task = asyncio.create_task(_rate_windows_janitor(stop))
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(*pool_tasks, janitor_task)


app = FastAPI(title="MongoDB Intelligence Layer", lifespan=lifespan)

# Origens permitidas configuráveis por ambiente (CSV) — atrás de um domínio real
# basta setar CORS_ORIGINS, sem alteração de código.
CORS_ORIGINS = [
    o.strip() for o in os.getenv(
        "CORS_ORIGINS", "http://localhost:5183,http://127.0.0.1:5183"
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Admin boundary + rate limiting ----------

# Endpoints administrativos (config global, resets, aprovação de denylist) exigem
# X-Admin-Key quando ADMIN_API_KEY está definida. Sem a env var o PoV roda em
# modo demo aberto — com warning explícito no log, nunca silencioso.
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
DEMO_TOKEN_ISSUANCE_ENABLED = os.getenv("DEMO_TOKEN_ISSUANCE_ENABLED", "1") == "1"
if not ADMIN_API_KEY:
    logger.warning(
        "ADMIN_API_KEY não definida — endpoints administrativos abertos (modo demo). "
        "Defina a env var para exigir X-Admin-Key."
    )


def require_admin(request: Request) -> None:
    if not ADMIN_API_KEY:
        return  # modo demo
    supplied = request.headers.get("x-admin-key", "")
    # compare em bytes: header não-ASCII não pode virar TypeError/500
    if not secrets.compare_digest(supplied.encode(), ADMIN_API_KEY.encode()):
        raise HTTPException(status_code=401, detail="X-Admin-Key ausente ou inválida.")


def validate_runtime_security(
    environment: str,
    admin_api_key: str,
    jwt_secret: str,
    auth_required: bool,
    demo_token_issuance_enabled: bool,
    cors_origins: list[str],
) -> None:
    if environment in {"development", "dev", "local"}:
        return
    if len(admin_api_key) < 24 or len(jwt_secret) < 32:
        raise RuntimeError("ADMIN_API_KEY/JWT_SECRET inseguros fora de development")
    if not auth_required:
        raise RuntimeError("AUTH_REQUIRED deve permanecer ligado fora de development")
    if demo_token_issuance_enabled:
        raise RuntimeError("DEMO_TOKEN_ISSUANCE_ENABLED deve estar desligado fora de development")
    if "*" in cors_origins:
        raise RuntimeError("CORS_ORIGINS='*' é recusado fora de development")


# Rate limit por identidade (sliding window em memória, por processo). Protege o
# fair use entre tenants na demo; produção usa gateway/API management na frente.
# A chave inclui o IP do chamador: rotacionar user_key no payload não ganha
# janela nova, e janelas vazias são removidas (sem crescimento sem teto).
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
_rate_windows: dict[str, deque] = defaultdict(deque)


# X-Forwarded-For é spoofável quando o backend está exposto direto (sem proxy
# na frente): qualquer cliente ganharia janela nova de rate limit forjando o
# header. Só confie nele com TRUST_PROXY=1 (deploy atrás de nginx/gateway).
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def admin_audit(action: str, request: Request, **details) -> None:
    """Best-effort: toda ação administrativa vira documento em POC.admin_audit
    (quem — IP —, o quê, quando). Falha no log nunca derruba a ação."""
    try:
        await poc()["admin_audit"].insert_one({
            "action": action,
            "ip": client_ip(request),
            "details": details,
            "at": datetime.now(timezone.utc),
        })
    except Exception:  # noqa: BLE001
        logger.exception("falha ao gravar admin_audit (best-effort)")


def enforce_rate_limit(identity: str, multiplier: float = 1.0) -> None:
    """`multiplier` vem da claim de tier do token (área com tier "priority"
    ganha mais requisições/minuto sem tocar código — é dado de area_profiles,
    não uma constante por área espalhada aqui).

    Limpeza PASSIVA: só a janela DESTA identidade é podada aqui — O(1)
    amortizado por request, nunca um scan de todas as identidades vistas no
    último minuto. Identidades totalmente inativas (sem request algum) são
    varridas do dict por uma tarefa periódica em background
    (`_rate_windows_janitor`), não a cada chamada."""
    limit = max(1, round(RATE_LIMIT_PER_MINUTE * multiplier))
    now = time.monotonic()
    window = _rate_windows[identity]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Limite de {limit} requisições/minuto atingido "
                   "para esta identidade. Aguarde e tente novamente.",
        )
    window.append(now)


RATE_WINDOWS_JANITOR_SECONDS = 60


async def _rate_windows_janitor(stop: asyncio.Event) -> None:
    """Remove do dict identidades sem nenhum timestamp recente (janela vazia).

    Só existe para não deixar `_rate_windows` crescer sem teto com identidades
    que nunca mais fazem outro request — a poda por request (`enforce_rate_limit`)
    já mantém CADA janela viva em O(1), esta tarefa só libera memória das mortas.
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=RATE_WINDOWS_JANITOR_SECONDS)
        except asyncio.TimeoutError:
            now = time.monotonic()
            dead = []
            for key, window in list(_rate_windows.items()):
                while window and now - window[0] > 60:
                    window.popleft()
                if not window:
                    dead.append(key)
            for key in dead:
                _rate_windows.pop(key, None)


@app.middleware("http")
async def request_observability(request: Request, call_next):
    """request_id em toda resposta + latência/erros por rota em /api/metrics."""
    request_id = request.headers.get("x-request-id") or uuid4().hex[:16]
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        observability.metrics.observe(request.url.path, 500,
                                      (time.perf_counter() - start) * 1000)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    observability.metrics.observe(request.url.path, response.status_code, elapsed_ms)
    response.headers["X-Request-Id"] = request_id
    return response


@app.get("/api/metrics")
async def api_metrics(request: Request):
    """Contadores em processo: requests/erros/latência por rota + contadores de
    negócio (cache hits, bloqueios). Produção pluga OTel/Prometheus por cima."""
    auth.resolve_user_key(request, None)
    snap = observability.metrics.snapshot()
    # Heuristic savings uses complete measured turn costs, never a fixed tariff.
    c = snap["counters"]
    turns = c.get("priced_turns", 0)
    avg = c.get("estimated_cost_nanousd", 0) / 1e9 / turns if turns and not c.get("unpriced_turns", 0) else None
    snap["savings"] = {"cache_hits": c.get("cache_hits", 0),
        "avg_llm_call_usd": avg,
        "estimated_saved_usd": c.get("cache_hits", 0) * avg if avg is not None else None,
        "basis": "heuristic: cache hits times mean observed turn cost; not invoice savings"}
    return snap


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    require_admin(request)
    return Response(observability.metrics.prometheus(), media_type="text/plain; version=0.0.4")


@app.exception_handler(SafeQueryError)
async def safe_query_handler(_: Request, exc: SafeQueryError):
    return JSONResponse(status_code=503, content={"error": {"kind": exc.kind, "message": exc.message}})


def clean(doc):
    """ObjectId/datetime → string for JSON (datas sempre como ISO-8601 UTC "Z")."""
    if isinstance(doc, list):
        return [clean(d) for d in doc]
    if isinstance(doc, dict):
        return {k: clean(v) for k, v in doc.items()}
    if isinstance(doc, datetime):
        if doc.tzinfo is not None:
            doc = doc.astimezone(timezone.utc)
        return doc.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(doc, ObjectId):
        return str(doc)
    return doc


# ---------- Sidebar / health ----------

AI_BRAIN_COLLECTIONS = ["prompt_templates", "model_config", "cache_config",
                        "guardrail_policies", "area_profiles"]
# POC collections that power the agent + the intelligence features
POC_COLLECTIONS = ["support_orders", "agent_sessions", "agent_memory",
                   "semantic_cache", "guardrail_denylist", "guardrail_events",
                   "app_users", "agent_traces"]


@app.get("/api/health")
async def health():
    db = ai_brain()
    await safe_query(get_client().admin.command("ping"))
    # 13 counts sequenciais viravam soma de RTTs; em paralelo o health responde
    # no tempo do count mais lento.
    names = AI_BRAIN_COLLECTIONS + POC_COLLECTIONS
    results = await asyncio.gather(
        *[safe_query(db[c].count_documents({}, maxTimeMS=MAX_TIME_MS))
          for c in AI_BRAIN_COLLECTIONS],
        *[safe_query(poc()[c].count_documents({}, maxTimeMS=MAX_TIME_MS))
          for c in POC_COLLECTIONS],
        get_active_config(),
    )
    counts = dict(zip(names, results[:-1]))
    cfg = results[-1]
    return {
        "ping": "ok",
        "counts": counts,
        "primary_model": cfg["primary"]["model"],
        "fallback_model": cfg["fallback"]["model"],
    }


@app.get("/health/live")
async def liveness():
    return {"status": "alive"}


# ---------- Tab 1: Flexible schema ----------

@app.get("/api/templates")
async def list_templates():
    cursor = ai_brain()["prompt_templates"].find({}, max_time_ms=MAX_TIME_MS)
    return clean(await safe_query(cursor.to_list(length=50)))


@app.get("/api/templates/{template_id}")
async def get_template(template_id: str):
    doc = await safe_query(
        ai_brain()["prompt_templates"].find_one({"_id": template_id}, max_time_ms=MAX_TIME_MS)
    )
    return clean(doc)


MODEL_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$"


class VariantBody(BaseModel):
    model_name: str = Field("gemini-3-pro", pattern=MODEL_NAME_PATTERN)


@app.post("/api/templates/{template_id}/variant")
async def add_variant(template_id: str, body: VariantBody, request: Request):
    """Real $set on Atlas: adds a new variant to the document — zero migration."""
    require_admin(request)
    variant = {
        "system": f"Você é um assistente de catálogo otimizado para {body.model_name}.",
        "user_template": "Contexto: {{rag_chunks}}\n\nPergunta: {{question}}",
        "added_live_by": "schema-war-demo",
    }
    await safe_query(
        ai_brain()["prompt_templates"].update_one(
            {"_id": template_id},
            {
                "$set": {
                    f"variants.{body.model_name}": variant,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
    )
    return await get_template(template_id)


@app.delete("/api/templates/{template_id}/variant/{model_name}")
async def remove_variant(template_id: str, model_name: str, request: Request):
    """Demo reset: $unset the variant added live."""
    require_admin(request)
    if not re.fullmatch(MODEL_NAME_PATTERN, model_name):
        raise HTTPException(status_code=422, detail="model_name inválido")
    await safe_query(
        ai_brain()["prompt_templates"].update_one(
            {"_id": template_id},
            {"$unset": {f"variants.{model_name}": ""}},
        )
    )
    return await get_template(template_id)


# ---------- Tab 2: Model Swap ----------

@app.get("/api/model-config")
async def model_config():
    return clean(await get_active_config())


@app.post("/api/model-config/swap")
async def swap_models(request: Request, area: str = "default"):
    """Real update_one: swaps primary ↔ fallback. The backend reads the doc on every request.

    `area` limita o swap ao documento de config daquela área quando existir
    (fallback: doc global) — a troca deixa de ser cegamente global."""
    require_admin(request)
    cfg = await get_active_config(area)
    if not cfg.get("fallback") or not cfg.get("primary"):
        raise HTTPException(status_code=400,
                            detail="Config sem primary/fallback — swap indisponível.")
    await admin_audit("model_swap", request, area=area,
                      new_primary=cfg["fallback"].get("model"))
    await safe_query(
        ai_brain()["model_config"].update_one(
            {"_id": cfg["_id"]},
            {
                "$set": {
                    "primary": cfg["fallback"],
                    "fallback": cfg["primary"],
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
    )
    return clean(await get_active_config(area))


class QuickChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=4_000)


QUICK_CHAT_HISTORY_TURNS = 10
QUICK_CHAT_HISTORY_CHARS = 6_000


class QuickChatBody(BaseModel):
    question: str = Field(min_length=1, max_length=4_000)
    # Turnos anteriores da sessão do mini-chat (o estado vive no navegador).
    history: list[QuickChatTurn] = Field(default_factory=list, max_length=40)
    user_key: str | None = Field(default=None, max_length=128)


async def _quick_chat_history(turns: list[QuickChatTurn], area: str) -> list[dict]:
    """Histórico limitado e com PII mascarada, alternando user/assistant (a API
    exige começar em user). Teto de turnos e de caracteres, como no agente."""
    picked, used = [], 0
    for t in reversed(turns[-QUICK_CHAT_HISTORY_TURNS:]):
        text = t.text.strip()
        if not text:
            continue
        used += len(text)
        if used > QUICK_CHAT_HISTORY_CHARS:
            break
        picked.append({"role": t.role,
                       "content": await guardrails.mask_pii(text, area) if t.role == "user" else text})
    picked.reverse()
    while picked and picked[0]["role"] != "user":
        picked.pop(0)
    merged: list[dict] = []
    for m in picked:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(dict(m))
    if merged and merged[-1]["role"] == "user":
        merged.pop()  # user órfão (sem resposta): a pergunta atual segue em user
    return merged


@app.post("/api/chat/quick")
async def quick_chat(body: QuickChatBody, request: Request):
    """Chat do Model Swap. Guardrail de entrada TAMBÉM aqui: proteção é
    transversal — nenhum endpoint que chama o LLM fica fora da política.
    Com user_key, área do usuário governa política, config de modelo e trilha
    de auditoria; sem ele, área default (demo)."""
    enforce_rate_limit(f"quick:{client_ip(request)}")
    area = "default"
    identity = "model-swap-demo"
    resolved = auth.resolve_user_key(request, body.user_key)
    if resolved:
        user = await profiles.require_demo_user(resolved)
        area = user.get("area", "default")
        identity = resolved
    guard = await guardrails.check_input(body.question, identity, "quick-chat", area)
    if not guard["allowed"]:
        return {
            "text": guard["block_message"],
            "model": "guardrail",
            "route": "blocked",
            "latency_ms": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
    masked = guard.get("masked_text") or body.question
    if guidance.is_obviously_out_of_scope(masked):
        return {
            "text": await guidance.scope_reply(identity),
            "model": "deterministic_scope_router",
            "route": "scope_redirect",
            "latency_ms": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
    # Cache semântico também aqui: mesma pergunta (semanticamente) já respondida
    # → serve do MongoDB sem tocar o LLM. Área do usuário escopa a visibilidade.
    # Pergunta pessoal ou sobre a própria conversa depende de quem/quando pergunta:
    # não lê nem grava o cache compartilhado (mesmo gate do agente; falha fechado).
    bypass_cache = memory.should_extract(masked) or memory.references_conversation(masked)
    if not bypass_cache:
        bypass_cache = (await turn_classifier.classify(masked))["personal"]
    cached = {"hit": False, "bypass": True} if bypass_cache else await cache.lookup(masked, area=area)
    if cached.get("hit"):
        observability.metrics.bump("cache_hits")
        return {
            "text": cached["answer"],
            "model": cached.get("model") or "cache",
            "route": "cache",
            "latency_ms": cached["latency_ms"],
            "input_tokens": 0,
            "output_tokens": 0,
            "cache": cached,
        }
    history = await _quick_chat_history(body.history, area)
    result = await call_with_fallback(
        system="Você é um assistente de e-commerce. Responda em português, em poucas frases.",
        messages=history + [{"role": "user", "content": masked}],
        area=area,
    )
    # PII na saída mascarada aqui também — mesma regra do agente
    guard_out = await guardrails.check_output(result["text"], identity, "quick-chat", area)
    result["text"] = guard_out["text"]
    # Sem memória/tools neste endpoint → resposta é genérica por construção,
    # segura para o cache compartilhado da área.
    if not bypass_cache:
        try:
            await cache.store(masked, result["text"], result.get("model", "?"),
                              scope="quick-chat", area=area)
        except Exception:  # noqa: BLE001 — falha de cache nunca degrada a resposta
            logger.exception("cache store falhou no quick-chat")
    result["cache"] = cached
    return result


# ---------- Auth (JWT demo-issuer; produção troca a emissão pelo IdP) ----------

class TokenBody(BaseModel):
    user_key: str = Field(min_length=1, max_length=128)


@app.post("/api/auth/token")
async def auth_token(body: TokenBody):
    """Troca uma identidade demo cadastrada por um JWT. O switcher do frontend
    é o 'login' da demo; produção substitui esta emissão por OIDC/JWKS."""
    if not DEMO_TOKEN_ISSUANCE_ENABLED:
        raise HTTPException(status_code=404, detail="emissão de token demo desabilitada")
    user = await profiles.require_demo_user(body.user_key)
    area = user.get("area", profiles.DEFAULT_AREA)
    area_profile = await profiles.get_area_profile(area)
    return auth.issue_token(
        user["user_key"], area, user.get("name", ""),
        tier=area_profile.get("tier", "standard"),
        rate_limit_multiplier=area_profile.get("rate_limit_multiplier", 1.0),
    )


# ---------- Tab 3: Agent (autonomous loop via MongoDB MCP Server) ----------

@app.get("/api/users")
async def list_users():
    """Registered users (POC.app_users) with their area resolved — powers the
    identity switcher. In production the user_key comes from real auth (JWT/OIDC),
    never from the client payload; the switcher stands in for a login."""
    return clean({"users": await profiles.list_users()})


@app.get("/api/agent/scenarios")
async def agent_scenarios(user_key: str | None = None):
    """Sugestões de perguntas DA ÁREA do usuário — cada departamento vê chips
    contextuais que referenciam os pedidos do próprio usuário. Sem user_key
    (ou área sem entrada própria), cai na área default."""
    area = profiles.DEFAULT_AREA
    if user_key:
        user = await profiles.get_user(user_key)
        area = user.get("area", profiles.DEFAULT_AREA)
    scenarios = AREA_SCENARIOS.get(area) or AREA_SCENARIOS["default"]
    return {
        "area": area,
        "scenarios": [
            {"key": key, "label": s["label"], "message": s["message"]}
            for key, s in scenarios.items()
        ],
    }


@app.get("/api/agent/playlist")
async def agent_playlist():
    """Curated auto-demo scripts for the ▶ Demo automática button."""
    return {"playlist": DEMO_PLAYLIST}


@app.get("/api/agent/tools")
async def agent_tools(request: Request):
    """The MongoDB tools the agent has available through the MCP Server."""
    session = get_mcp_session(request.app)
    if session is None:
        return {"tools": []}
    tools = await list_agent_tools(session)
    return {
        "tools": [
            {"name": t["name"], "kind": "write" if t["name"] in WRITE_TOOLS else "read"}
            for t in tools
        ]
    }


class AgentRunBody(BaseModel):
    scenario: str | None = None
    message: str | None = Field(default=None, max_length=4_000)
    conversation_id: str | None = Field(default=None, max_length=64)
    user_key: str | None = Field(default=None, max_length=128)


@app.post("/api/agent/run")
async def agent_run(request: Request, body: AgentRunBody):
    session = get_mcp_session(request.app)
    if session is None:
        errors = getattr(request.app.state, "mcp_errors", None) or []
        detail = next((e for e in errors if e), "")
        raise SafeQueryError(
            "mcp",
            "O MongoDB MCP Server não está disponível (o supervisor está tentando "
            "reconectar). Confira se o Node/npx está instalado e o cluster acessível. "
            + detail,
        )
    # Identidade: claim `sub` do JWT vence o payload; payload é fallback demo
    user_key = auth.resolve_user_key(request, body.user_key) or DEFAULT_USER_KEY
    # uuid4: id de conversa não-adivinhável (timestamp em segundos colide entre
    # usuários e é enumerável)
    # IP compõe a chave: rotacionar user_key não escapa do limite
    enforce_rate_limit(f"agent:{client_ip(request)}:{user_key}",
                       multiplier=auth.resolve_rate_limit_multiplier(request))
    conversation_id = body.conversation_id or f"conv_{uuid4().hex[:16]}"
    try:
        result = await run_agent(
            session,
            scenario=body.scenario,
            message=body.message,
            conversation_id=conversation_id,
            user_key=user_key,
        )
    except SafeQueryError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise SafeQueryError("agente", f"Falha ao executar o agente: {exc}")

    # Contadores de negócio para /api/metrics
    observability.metrics.bump("agent_turns")
    if (result.get("cache") or {}).get("hit"):
        observability.metrics.bump("cache_hits")
        observability.metrics.bump("tokens_economizados", (result.get("cache") or {}).get("tokens_economizados", 0))
    if ((result.get("guardrail") or {}).get("input") or {}).get("action") == "block":
        observability.metrics.bump("guardrail_blocks")

    # Observabilidade: o trace replayável também é um documento (POC.agent_traces).
    # PII: user_message/answer/trace chegam aqui já mascarados pelos guardrails.
    # Best-effort: falha em gravar o trace nunca derruba a resposta ao usuário.
    try:
        await poc()["agent_traces"].insert_one({
            "conversation_id": result.get("conversation_id"),
            "user_key": user_key,
            "area": (result.get("profile") or {}).get("area"),
            "scenario": result.get("scenario"),
            "user_message": result.get("user_message"),
            "answer": result.get("answer"),
            "model": result.get("model"),
            "metrics": result.get("metrics"),
            "llm_calls": result.get("llm_calls", []),
            "economics": result.get("economics", {}),
            "cache_hit": (result.get("cache") or {}).get("hit"),
            "guardrail_action": ((result.get("guardrail") or {}).get("input") or {}).get("action"),
            "trace": result.get("trace"),
            "at": datetime.now(timezone.utc),
        })
    except Exception:  # noqa: BLE001
        logger.exception("falha ao gravar agent_trace (best-effort)")
    return clean(result)


async def _persist_agent_trace(result: dict, user_key: str) -> None:
    """Mesma escrita best-effort de POC.agent_traces usada por /api/agent/run,
    fatorada para ser reaproveitada pelo endpoint de streaming sem duplicar
    a lógica de observabilidade/contadores."""
    observability.metrics.bump("agent_turns")
    if (result.get("cache") or {}).get("hit"):
        observability.metrics.bump("cache_hits")
        observability.metrics.bump("tokens_economizados", (result.get("cache") or {}).get("tokens_economizados", 0))
    if ((result.get("guardrail") or {}).get("input") or {}).get("action") == "block":
        observability.metrics.bump("guardrail_blocks")
    try:
        await poc()["agent_traces"].insert_one({
            "conversation_id": result.get("conversation_id"),
            "user_key": user_key,
            "area": (result.get("profile") or {}).get("area"),
            "scenario": result.get("scenario"),
            "user_message": result.get("user_message"),
            "answer": result.get("answer"),
            "model": result.get("model"),
            "metrics": result.get("metrics"),
            "llm_calls": result.get("llm_calls", []),
            "economics": result.get("economics", {}),
            "cache_hit": (result.get("cache") or {}).get("hit"),
            "guardrail_action": ((result.get("guardrail") or {}).get("input") or {}).get("action"),
            "trace": result.get("trace"),
            "at": datetime.now(timezone.utc),
        })
    except Exception:  # noqa: BLE001
        logger.exception("falha ao gravar agent_trace (best-effort)")


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(clean(data), ensure_ascii=False)}\n\n"


@app.post("/api/agent/run/stream")
async def agent_run_stream(request: Request, body: AgentRunBody):
    """Mesmo turno de /api/agent/run, mas via Server-Sent Events: cada evento de
    `emit()` (Perceive/Retrieve/Reason/Act/Store/Loop) chega ao cliente assim
    que é gerado, em vez do trace inteiro só no final — o turno pode levar até
    AGENT_TURN_TIMEOUT_SECONDS (120s) sem feedback incremental era o problema.
    Endpoint novo em vez de mudar o antigo: mantém /api/agent/run compatível
    para quem ainda consome request/response simples (smoke test, scripts)."""
    session = get_mcp_session(request.app)
    if session is None:
        errors = getattr(request.app.state, "mcp_errors", None) or []
        detail = next((e for e in errors if e), "")
        raise SafeQueryError(
            "mcp",
            "O MongoDB MCP Server não está disponível (o supervisor está tentando "
            "reconectar). Confira se o Node/npx está instalado e o cluster acessível. "
            + detail,
        )
    user_key = auth.resolve_user_key(request, body.user_key) or DEFAULT_USER_KEY
    enforce_rate_limit(f"agent:{client_ip(request)}:{user_key}",
                       multiplier=auth.resolve_rate_limit_multiplier(request))
    conversation_id = body.conversation_id or f"conv_{uuid4().hex[:16]}"

    async def event_stream():
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()

        def on_event(event: dict) -> None:
            # emit() roda de forma síncrona dentro da task do agente — repassa
            # pra fila sem bloquear (thread-safe pois estamos na mesma loop).
            loop.call_soon_threadsafe(queue.put_nowait, event)

        async def run() -> None:
            try:
                result = await run_agent(
                    session, scenario=body.scenario, message=body.message,
                    conversation_id=conversation_id, user_key=user_key,
                    on_event=on_event,
                )
                await queue.put(("result", result))
            except SafeQueryError as exc:
                await queue.put(("error", {"kind": exc.kind, "message": exc.message}))
            except ValueError as exc:
                await queue.put(("error", {"kind": "validacao", "message": str(exc)}))
            except Exception as exc:  # noqa: BLE001
                await queue.put(("error", {"kind": "agente", "message": f"Falha ao executar o agente: {exc}"}))
            finally:
                await queue.put(("__done__", SENTINEL))

        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if isinstance(item, dict):  # evento de trace (emitido via on_event)
                    yield _sse("trace", item)
                    continue
                kind, payload = item
                if kind == "__done__":
                    break
                if kind == "result":
                    await _persist_agent_trace(payload, user_key)
                    yield _sse("result", payload)
                elif kind == "error":
                    yield _sse("error", payload)
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- Intelligence features: cache, memory, guardrails (inspect/reset) ----------

@app.get("/api/cache")
async def cache_inspect():
    """The semantic cache contents — question, answer, reuse count."""
    cfg = await cache.get_config()
    return clean({"entries": await cache.recent(), "threshold": cfg["hit_threshold"],
                  "collection": f"POC.{cache.CACHE_COLLECTION}",
                  "index": cache.CACHE_INDEX})


@app.delete("/api/cache")
async def cache_clear(request: Request):
    """Demo reset: empty the cache so the next question is a guaranteed MISS."""
    require_admin(request)
    await admin_audit("cache_clear", request)
    return {"deleted": await cache.clear()}


@app.get("/api/memory/{user_key}")
async def memory_inspect(user_key: str, request: Request):
    """Long-term memory for a user: active facts + superseded history (audit)."""
    user_key = auth.resolve_user_key(request, user_key) or user_key
    await profiles.require_demo_user(user_key)
    return clean(await memory.load_longterm(user_key, include_history=True))


@app.get("/api/memory-short/{conversation_id}")
async def memory_short_inspect(conversation_id: str, user_key: str, request: Request):
    """Short-term memory (the current conversation's turns), from POC.agent_sessions."""
    user_key = auth.resolve_user_key(request, user_key) or user_key
    await profiles.require_demo_user(user_key)
    doc = await safe_query(
        poc()["agent_sessions"].find_one(
            {"session_id": conversation_id, "user_key": user_key}, max_time_ms=MAX_TIME_MS
        )
    )
    if doc is None:
        return clean({"session_id": conversation_id, "turns": [],
                      "collection": "POC.agent_sessions"})
    # TTL (ADR-002): a session's `updated_at` drives a MongoDB TTL index — no
    # cron, no invalidation code. Surfaced here so the inspector shows it
    # instead of looking like the session lives forever.
    doc["ttl_expires_at"] = doc["updated_at"] + timedelta(seconds=SESSION_IDLE_SECONDS)
    doc["ttl_idle_seconds"] = SESSION_IDLE_SECONDS
    return clean(doc)


@app.delete("/api/memory/{user_key}")
async def memory_clear(user_key: str, request: Request):
    """Demo reset: forget everything about this user (long-term memory)."""
    require_admin(request)
    await profiles.require_demo_user(user_key)
    await admin_audit("memory_clear", request, user_key=user_key)
    res = await safe_query(poc()["agent_memory"].delete_many({"user_key": user_key}))
    return {"deleted": res.deleted_count}


@app.get("/api/guardrails/policy")
async def guardrails_policy():
    """The live-editable guardrail policy document from ai_brain.guardrail_policies."""
    return clean(await guardrails.get_policy())


@app.get("/api/guardrails/rules")
async def guardrails_rules(area: str = "default"):
    """The active guardrails for an AREA: its policy (PII, banned terms, threshold)
    + the denylist phrases that apply to it (global ones + the area's own).
    This is 'which guardrails do we have', not the log."""
    policy = await guardrails.get_policy(area)
    cursor = poc()[guardrails.DENYLIST_COLLECTION].find(
        {"$or": [{"area": {"$exists": False}}, {"area": None}, {"area": area}]},
        {"phrase": 1, "category": 1, "area": 1}, max_time_ms=MAX_TIME_MS
    )
    denylist = await safe_query(cursor.to_list(length=100))
    return clean({
        "area": area,
        "policy": policy,
        "denylist": denylist,
        "denylist_collection": f"POC.{guardrails.DENYLIST_COLLECTION}",
        "policy_collection": f"ai_brain.{guardrails.POLICY_COLLECTION}",
    })


async def _scoped_area(request: Request, user_key: str | None,
                       area: str | None) -> str | None:
    """Resolve o escopo de área de um endpoint de auditoria.

    A área NUNCA é auto-declarada: vem do documento do usuário (mesma fronteira
    demo dos endpoints de memória) ou, para visão de operador (`area=all` ou
    área explícita), da admin key. Retorna None = todas as áreas (operador).
    """
    if area == "all":
        require_admin(request)
        return None
    user_key = auth.resolve_user_key(request, user_key)
    if user_key:
        user = await profiles.require_demo_user(user_key)
        return user.get("area", "default")
    if area:  # área explícita sem identidade = operador
        require_admin(request)
        return area
    return "default"


@app.get("/api/guardrails/events")
async def guardrails_events(request: Request, user_key: str | None = None,
                            area: str | None = None):
    """Latest guardrail audit records from POC.guardrail_events, SCOPED ao
    tenant do user_key informado. Visão de outra área ou de todas exige admin."""
    scoped = await _scoped_area(request, user_key, area)
    return clean({"events": await guardrails.recent_events(area=scoped),
                  "area": scoped or "all",
                  "collection": f"POC.{guardrails.EVENTS_COLLECTION}"})


@app.get("/api/guardrails/candidates")
async def guardrails_candidates(request: Request, status: str = "pending",
                                user_key: str | None = None,
                                area: str | None = None):
    """Near-misses aguardando revisão humana antes de virarem denylist.
    Escopado ao tenant do user_key; outra área ou fila completa exige admin."""
    scoped = await _scoped_area(request, user_key, area)
    return clean({"candidates": await guardrails.list_candidates(status, area=scoped),
                  "area": scoped or "all",
                  "collection": f"POC.{guardrails.CANDIDATES_COLLECTION}"})


@app.post("/api/guardrails/candidates/{candidate_id}/review")
async def guardrails_review_candidate(candidate_id: str, decision: str,
                                      request: Request, reviewer: str = ""):
    """Aprova (promove ao denylist) ou rejeita um candidato near-miss.
    Promoção muda a política viva → operação administrativa."""
    require_admin(request)
    await admin_audit("guardrail_candidate_review", request,
                      candidate_id=candidate_id, decision=decision,
                      reviewer=reviewer[:64])
    try:
        return clean(await guardrails.review_candidate(candidate_id, decision, reviewer))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
