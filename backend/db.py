"""Atlas connection + safe_query helper.

Driver: PyMongo Async (AsyncMongoClient) — o driver assíncrono oficial que
substituiu o Motor (deprecado). Diferença relevante de API: `aggregate()` é uma
corrotina (retorna o cursor após await) — por isso o helper aggregate_list.

Every read goes through maxTimeMS=10s. Operational errors become a SafeQueryError
with a user-friendly message — the frontend renders it in a Banner, never a stack trace.
"""

import os
import logging
from pathlib import Path

from dotenv import load_dotenv
from pymongo import AsyncMongoClient
from pymongo.errors import (
    ConnectionFailure,
    ExecutionTimeout,
    NetworkTimeout,
    OperationFailure,
    ServerSelectionTimeoutError,
    WTimeoutError,
)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

MAX_TIME_MS = 10_000
# Shared with seed.py (creates the TTL index) and main.py (surfaces the
# deadline to the inspector) so the two never drift apart.
# 24h, not 1h: a customer who drops off (closed tab, lunch, meeting) and comes
# back later should still land in the same short-term context instead of a
# cold session — matches the semantic_cache/short_term_memory TTL on the
# MultiAgent PoV, so both demos tell the same story about session continuity.
SESSION_IDLE_SECONDS = 86400
logger = logging.getLogger("poc.db")

# Nomes dos bancos: constantes, não literais espalhados. O default é o da demo;
# os scripts que escrevem dado REAL (eval --live, benchmark de memória,
# crash_resume do chaos) apontam para `<nome>_test` via env e assim não tocam a
# demo — ver backend/scripts/isolation.py. A política de ferramentas do agente
# (agent.py) deriva o alvo daqui, senão o app leria um banco e o MCP escreveria
# no outro.
DB_MAIN = os.getenv("MONGODB_DB", "POC")
DB_BRAIN = os.getenv("MONGODB_BRAIN_DB", "ai_brain")
# O catálogo (500k documentos + índice autoEmbed) é SOMENTE LEITURA: nenhum
# caminho do agente escreve nele. Por isso ele continua no banco da demo mesmo
# quando o resto aponta para o banco de teste — copiar meio milhão de documentos
# e reconstruir o índice vetorial a cada rodada de eval não compraria isolamento
# nenhum, já que não há escrita para isolar.
DB_CATALOG = os.getenv("MONGODB_CATALOG_DB", "POC")

_client: AsyncMongoClient | None = None


def get_client() -> AsyncMongoClient:
    global _client
    if _client is None:
        uri = os.getenv("MONGODB_URI")
        if not uri:
            raise SafeQueryError(
                "config",
                "MONGODB_URI não definida. Copie .env.example para .env e preencha a URI do cluster.",
            )
        _client = AsyncMongoClient(
            uri,
            serverSelectionTimeoutMS=MAX_TIME_MS,
            connectTimeoutMS=MAX_TIME_MS,
            appname="intelligence-layer-poc",
            # Explícito em vez de default do driver: número real de referência
            # para dimensionamento. Tier de referência desta PoV
            # é M10/M20 — maxPoolSize=50 cobre concorrência de demo/apresentação
            # com folga sem pressionar o connection limit do cluster;
            # minPoolSize evita reabrir handshake TLS a cada rajada após um
            # período ocioso; maxIdleTimeMS libera conexões extras entre demos.
            maxPoolSize=int(os.getenv("MONGODB_MAX_POOL_SIZE", "50")),
            minPoolSize=int(os.getenv("MONGODB_MIN_POOL_SIZE", "5")),
            maxIdleTimeMS=int(os.getenv("MONGODB_MAX_IDLE_TIME_MS", "30000")),
            # EXPLÍCITO, embora seja o default do driver: é a resposta à pergunta
            # "e quando o primário cair?". Num step-down (eleição de ~2-10s num
            # replica set do Atlas) o driver reexecuta a operação no novo primário
            # sozinho — o turno do agente não vê nada. O app só enxerga a falha
            # se o retry TAMBÉM esgotar, e aí degrada com mensagem de conexão
            # (ver `safe_query` e `agent.run_loop_guarded`). Deixar implícito faria
            # a resiliência parecer acidental; aqui ela é declarada e verificável
            # (`scripts/preflight.py` confere, `chaos_suite.py atlas_failover` mede).
            retryWrites=True,
            retryReads=True,
            # CSOT (Client-Side Operation Timeout): UM orçamento por operação que
            # cobre seleção de servidor, handshake, envio e resposta — inclusive os
            # retries acima. Antes havia só maxTimeMS por consulta (tempo de
            # execução NO servidor) e serverSelectionTimeoutMS, cada um por conta
            # própria: uma operação podia gastar os dois em sequência. Default 15s,
            # acima do maxTimeMS de 10s para o erro específico da consulta vencer.
            timeoutMS=int(os.getenv("MONGODB_TIMEOUT_MS", "15000")),
        )
    return _client


def ai_brain():
    return get_client()[DB_BRAIN]


def poc():
    return get_client()[DB_MAIN]


async def aggregate_list(coll, pipeline, *, length: int, **kwargs) -> list[dict]:
    """PyMongo Async: aggregate() é corrotina → await duas vezes (cursor, depois lista)."""
    cursor = await coll.aggregate(pipeline, **kwargs)
    return await cursor.to_list(length=length)


def _chaos_enabled() -> bool:
    return os.getenv("CHAOS", "").strip().lower() in {"1", "true", "yes", "on"}


async def _chaos_hook() -> None:
    import chaos

    await chaos.hook("mongo", name="safe_query")


class SafeQueryError(Exception):
    """Operational error carrying a UI-ready message."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        self.message = message
        super().__init__(message)


async def safe_query(awaitable):
    """Awaits a PyMongo Async operation, mapping failures to user-friendly messages.

    maxTimeMS is passed on each call (find/aggregate); here we handle what
    slips through: timeouts, missing search index, mongot restarting, network.

    É também a FRONTEIRA única do MongoDB no app, então é aqui que a bateria de
    caos injeta os modos de falha próprios do Atlas (step-down de primário,
    `mongot` fora). Sem `CHAOS=1` isto é uma leitura de variável de ambiente.
    """
    try:
        if _chaos_enabled():
            try:
                await _chaos_hook()
            except BaseException:
                # A corrotina do chamador nunca chegou a ser aguardada — sem isto,
                # cada cenário de caos que injeta aqui deixa um "coroutine was
                # never awaited" no log, poluindo a saída da bateria (medido:
                # 3 avisos numa execução completa). Mesmo padrão de
                # `resilience.call_tool`.
                awaitable.close()
                raise
        return await awaitable
    except (ExecutionTimeout, NetworkTimeout, WTimeoutError):
        raise SafeQueryError(
            "timeout",
            "A consulta excedeu 10 segundos (maxTimeMS). O cluster pode estar sob carga — tente novamente.",
        )
    except ServerSelectionTimeoutError:
        raise SafeQueryError(
            "conexao",
            "Não foi possível alcançar o cluster Atlas. Verifique a MONGODB_URI e o IP Access List.",
        )
    except OperationFailure as e:
        msg = str(e).lower()
        if "mongot" in msg or "search index" in msg or "$vectorsearch" in msg:
            raise SafeQueryError(
                "search",
                "O Atlas Search (mongot) está indisponível ou o índice vetorial não foi encontrado. "
                "Confira o índice 'produtos_vector' em POC.produtos_vector.",
            )
        if "index not found" in msg or "no such index" in msg:
            raise SafeQueryError(
                "indice",
                "Índice necessário não encontrado nesta collection.",
            )
        logger.warning("MongoDB operation rejected code=%s", e.code, exc_info=True)
        raise SafeQueryError(
            "operacao",
            "Operação rejeitada pelo MongoDB. Consulte o request-id nos logs do backend.",
        ) from e
    except ConnectionFailure:
        raise SafeQueryError(
            "conexao",
            "Conexão com o cluster perdida. Tente novamente em alguns segundos.",
        )
