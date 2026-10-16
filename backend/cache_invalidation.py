"""Invalidação do cache semântico por Change Stream (ai_brain.area_profiles → POC.semantic_cache).

O que alimenta uma resposta cacheada: o cache só grava turno SEM ferramenta de negócio e sem
memória do cliente (agent_graph.n_cache_store, main.quick_chat). Sobra o LLM + o system prompt,
cuja parte de negócio vem de `ai_brain.area_profiles.persona` — é lá que estão as políticas
("troca em até 30 dias, reembolso em até 7 dias úteis", prazos de entrega). Pedido, catálogo e
memória nunca entram numa resposta cacheada, então não são observados.

Granularidade: ÁREA. Uma mudança relevante no perfil da área X (persona, active, area) apaga:
  · as entradas de RUNTIME da área X (scope != "faq"): foram geradas com a persona antiga e são
    baratas de regenerar (TTL de 24 h de qualquer forma);
  · as FAQs semeadas cuja fonte é X (`source_area`) SÓ se o hash da persona mudou
    (`source_hash` gravado pelo seed). Trocar o `label` ou o rate limit da área não derruba FAQ.
Granularidade por entidade (pergunta a pergunta) exigiria saber que trecho da persona cada
resposta usou — o LLM não diz. Área é o menor escopo correto.

Resiliência (via `atlas_retrieval.ChangeStreamWorker`, pov-shared >= 0.3.0):
  · resume token em `ai_brain.change_stream_tokens` (+ heartbeat a cada 30 s com o
    postBatchResumeToken, para um stream ocioso não retomar de fora do oplog);
  · reconexão com backoff; invalidate/ChangeStreamHistoryLost/token inválido →
    `reconcile()`: apaga o runtime de TODAS as áreas e as FAQs cujo hash não bate, e loga;
  · um worker por processo (singleton do módulo); handler idempotente (delete_many);
  · shutdown limpo no lifespan; lag (wallTime) e contadores em `/api/health`.
Sem pov-shared o worker não sobe e o cache segue só com o TTL (comportamento anterior).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from db import MAX_TIME_MS, ai_brain, poc

try:
    import atlas_retrieval as _ar
except ImportError:  # pragma: no cover — sem pov-shared: só TTL
    _ar = None

logger = logging.getLogger("poc.cache_invalidation")

WATCHED_COLLECTION = "area_profiles"
TOKENS_COLLECTION = "change_stream_tokens"
WORKER_ID = "cache_invalidation"
CACHE_COLLECTION = "semantic_cache"
AUDIT_COLLECTION = "cache_invalidations"       # POC, TTL 30 dias (seed)
RELEVANT_FIELDS = frozenset({"persona", "active", "area"})
HEARTBEAT_SECONDS = 30
_OPS = ["insert", "update", "replace", "delete", "invalidate"]

_worker = None
_heartbeat: asyncio.Task | None = None
_stats = {"invalidations": 0, "deleted_runtime": 0, "deleted_faq": 0, "skipped": 0,
          "reconciles": 0, "last_invalidation_at": None}


def persona_hash(profile: dict | None) -> str | None:
    """Hash do que uma resposta da área herda do perfil. Mesmo cálculo no seed e aqui."""
    if not isinstance(profile, dict):
        return None
    material = {"persona": profile.get("persona"), "active": profile.get("active", True)}
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()[:16]


def pipeline() -> list[dict]:
    """$match NO SERVIDOR: só operações que podem mudar uma resposta, e só os campos usados."""
    return [
        {"$match": {"operationType": {"$in": _OPS}}},
        {"$project": {"operationType": 1, "documentKey": 1, "fullDocument": 1,
                      "updateDescription": 1, "wallTime": 1, "clusterTime": 1}},
    ]


def _area_of(change: dict) -> str | None:
    if not isinstance(change, dict):
        return None
    full = change.get("fullDocument")
    if isinstance(full, dict) and isinstance(full.get("area"), str) and full["area"]:
        return full["area"]
    doc_key = change.get("documentKey")
    key = doc_key.get("_id") if isinstance(doc_key, dict) else None
    if isinstance(key, str) and key.startswith("area_") and len(key) > 5:
        return key[5:]  # convenção do seed: _id "area_<area>" (delete não traz fullDocument)
    return None


def is_relevant(change: dict) -> bool:
    if not isinstance(change, dict):
        return False
    op = change.get("operationType")
    if op in ("insert", "replace", "delete"):
        return True
    if op != "update":
        return False
    desc = change.get("updateDescription")
    if not isinstance(desc, dict):
        return True  # update sem descrição legível: conservador, invalida
    updated = desc.get("updatedFields")
    removed = desc.get("removedFields")
    fields = (list(updated.keys()) if isinstance(updated, dict) else []) + \
        (list(removed) if isinstance(removed, list) else [])
    return any(str(f).split(".", 1)[0] in RELEVANT_FIELDS for f in fields)


async def _audit(kind: str, area: str | None, runtime: int, faq: int, reason: str) -> None:
    try:
        await poc()[AUDIT_COLLECTION].insert_one({
            "kind": kind, "area": area, "deleted_runtime": runtime, "deleted_faq": faq,
            "reason": reason[:200], "at": datetime.now(timezone.utc)})
    except Exception:  # noqa: BLE001 — auditoria nunca derruba a invalidação
        logger.warning("auditoria de invalidação falhou", exc_info=True)


async def invalidate_area(area: str, new_hash: str | None, reason: str) -> dict:
    """Apaga o runtime da área e as FAQs da área cujo hash difere. Idempotente."""
    coll = poc()[CACHE_COLLECTION]
    runtime = await coll.delete_many({"area": area, "scope": {"$ne": "faq"}})
    faq_filter = {"scope": "faq", "source_area": area}
    if new_hash is not None:
        faq_filter["source_hash"] = {"$ne": new_hash}
    faq = await coll.delete_many(faq_filter)
    _stats["invalidations"] += 1
    _stats["deleted_runtime"] += runtime.deleted_count
    _stats["deleted_faq"] += faq.deleted_count
    _stats["last_invalidation_at"] = time.time()
    logger.info("cache invalidado: área=%s runtime=%s faq=%s (%s)", area,
                runtime.deleted_count, faq.deleted_count, reason)
    await _audit("area", area, runtime.deleted_count, faq.deleted_count, reason)
    return {"area": area, "runtime": runtime.deleted_count, "faq": faq.deleted_count}


async def handle_change(change: dict) -> dict | None:
    """Handler do worker. Evento sem os campos esperados vira invalidação conservadora."""
    if not is_relevant(change):
        _stats["skipped"] += 1
        return None
    area = _area_of(change)
    if area is None:
        await reconcile("evento sem área identificável")
        return {"area": None}
    full = change.get("fullDocument") if change.get("operationType") != "delete" else None
    new_hash = persona_hash(full) if isinstance(full, dict) else None
    return await invalidate_area(area, new_hash, f"change stream {change.get('operationType')}")


async def reconcile(reason: str) -> dict:
    """Invalidação conservadora (history lost, invalidate, token inválido, evento torto):
    runtime de TODAS as áreas + FAQ cujo hash não bate com o perfil atual."""
    coll = poc()[CACHE_COLLECTION]
    runtime = await coll.delete_many({"scope": {"$ne": "faq"}})
    profiles = await ai_brain()[WATCHED_COLLECTION].find({}, max_time_ms=MAX_TIME_MS).to_list(100)
    current = {p.get("area"): persona_hash(p) for p in profiles if p.get("area")}
    faq_deleted = 0
    async for faq in coll.find({"scope": "faq"}, {"source_area": 1, "source_hash": 1}):
        src = faq.get("source_area")
        if src is None:
            continue  # FAQ sem fonte declarada não depende de perfil
        if current.get(src) != faq.get("source_hash"):
            faq_deleted += (await coll.delete_one({"_id": faq["_id"]})).deleted_count
    _stats["reconciles"] += 1
    _stats["deleted_runtime"] += runtime.deleted_count
    _stats["deleted_faq"] += faq_deleted
    _stats["last_invalidation_at"] = time.time()
    logger.warning("cache reconciliado (%s): runtime=%s faq=%s", reason,
                   runtime.deleted_count, faq_deleted)
    await _audit("reconcile", None, runtime.deleted_count, faq_deleted, reason)
    return {"runtime": runtime.deleted_count, "faq": faq_deleted}


_stream_client = None


def _stream_db():
    """Cliente PRÓPRIO do change stream, sem CSOT. O `timeoutMS=15000` do cliente do app
    (db.py) vale também para o `watch`: medido em 2026-10-16, um stream ocioso caía com
    ExecutionTimeout a cada ~18 s, e um evento no meio da reabertura se perdia."""
    global _stream_client
    import os

    from db import DB_BRAIN
    from pymongo import AsyncMongoClient

    if _stream_client is None:
        _stream_client = AsyncMongoClient(os.environ["MONGODB_URI"], appname="intelligence-layer-cs",
                                          serverSelectionTimeoutMS=MAX_TIME_MS,
                                          connectTimeoutMS=MAX_TIME_MS, maxPoolSize=2)
    return _stream_client[DB_BRAIN]


async def _open_stream(token):
    # max_await_time_ms: o getMore volta a cada 5 s mesmo sem evento, então o
    # postBatchResumeToken avança e o heartbeat salva um token recente.
    kwargs = {"full_document": "updateLookup", "max_await_time_ms": 5_000}
    if token:
        kwargs["resume_after"] = token
    return await _stream_db()[WATCHED_COLLECTION].watch(pipeline(), **kwargs)


async def _heartbeat_loop(worker, store) -> None:
    coll = ai_brain()[TOKENS_COLLECTION]
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            token = worker.current_token()
            if token is not None:
                await store.save(token)
            await coll.update_one({"_id": WORKER_ID}, {"$set": {
                "heartbeat_at": datetime.now(timezone.utc), "stats": stats()}}, upsert=True)
        except Exception:  # noqa: BLE001 — heartbeat nunca derruba o app
            logger.debug("heartbeat do worker falhou", exc_info=True)


def build_worker(*, open_stream=None, store=None, sleep=asyncio.sleep):
    if _ar is None:
        return None
    store = store or _ar.MongoResumeTokenStore(ai_brain()[TOKENS_COLLECTION], WORKER_ID)
    return _ar.ChangeStreamWorker(name=WORKER_ID, open_stream=open_stream or _open_stream,
                                  handler=handle_change, store=store,
                                  on_history_lost=reconcile, sleep=sleep)


async def start() -> bool:
    """Um worker por processo: chamadas repetidas não criam outro."""
    global _worker, _heartbeat
    if _worker is not None and _worker.running:
        return True
    _worker = build_worker()
    if _worker is None:
        logger.warning("pov-shared ausente: invalidação por change stream desligada (só TTL)")
        return False
    # Sem resume token não há como saber o que mudou enquanto nenhum worker olhava (primeira
    # partida, token apagado por history lost): reconcilia uma vez antes de assistir.
    try:
        if await _worker._store.load() is None:
            await reconcile("partida sem resume token")
    except Exception:  # noqa: BLE001 — reconcile falhando não impede o worker
        logger.warning("reconcile de partida falhou", exc_info=True)
    _worker.start()
    _heartbeat = asyncio.create_task(_heartbeat_loop(_worker, _worker._store),
                                     name="changestream:heartbeat")
    return True


async def stop() -> None:
    global _worker, _heartbeat
    if _heartbeat is not None:
        _heartbeat.cancel()
        try:
            await _heartbeat
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _heartbeat = None
    if _worker is not None:
        await _worker.stop()
    global _stream_client
    if _stream_client is not None:
        try:
            await _stream_client.close()
        except Exception:  # noqa: BLE001
            pass
        _stream_client = None


def stats() -> dict:
    out = dict(_stats)
    out["worker"] = _worker.stats.as_dict() if _worker is not None else {"state": "off"}
    return out


def heartbeat_fresh(doc: dict | None, now: datetime | None = None,
                    max_age: timedelta = timedelta(seconds=HEARTBEAT_SECONDS * 3)) -> bool:
    """Para o preflight: heartbeat recente = worker vivo em algum processo do app."""
    if not isinstance(doc, dict) or not isinstance(doc.get("heartbeat_at"), datetime):
        return False
    at = doc["heartbeat_at"]
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - at <= max_age
