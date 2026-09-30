"""Semantic cache over POC.semantic_cache (Atlas Vector Search, autoEmbed voyage-4).

The idea for the client: if a semantically-equivalent question was already
answered, we skip the LLM entirely and serve the stored answer from MongoDB —
cheaper and an order of magnitude faster. The match is *semantic* (not string
equality): "como peço reembolso?" hits the cache entry for "quero um estorno".

How it works, all in MongoDB:
  - lookup(): a real $vectorSearch on POC.semantic_cache. Atlas embeds the
    incoming question on the fly (autoEmbed) and returns the nearest prior Q&A
    with a similarity score. score >= hit_threshold → cache HIT.
  - store(): inserts the new Q&A; Atlas embeds `question` at write time.
  - Every HIT does an $inc on `hits` so the client can see reuse building up.

O THRESHOLD é config viva (ai_brain.cache_config), não constante. A escala do
vectorSearchScore pode mudar quando o índice/modelo é atualizado; neste cluster,
por exemplo, ela mudou de ~0.50 para ~0.59–0.83 em 2026-08. O ranking é
confiável; a escala absoluta não é. Por isso o threshold é
calibrado por medição (backend/calibrate_thresholds.py) e vive num documento
editável — recalibrar é um update_one, não um deploy. Mesma história do
model_config.

If the vector index is missing (not yet created in Atlas), lookup() degrades
gracefully to an exact normalized-text match, so the demo never crashes.
"""

import time
from datetime import datetime, timedelta, timezone

from db import MAX_TIME_MS, aggregate_list, ai_brain, poc, safe_query

CACHE_COLLECTION = "semantic_cache"
CACHE_INDEX = "semantic_cache_vs"      # Atlas Vector Search index (autoEmbed on `question`)
CACHE_PATH = "question"                # source text field for autoEmbed
CONFIG_COLLECTION = "cache_config"     # in ai_brain — live-editable threshold/TTL

# Fallback defaults quando ai_brain.cache_config não existe (seed não rodou).
# Valor medido em 2026-08 com calibrate_thresholds.py — ver docstring acima.
DEFAULT_HIT_THRESHOLD = 0.7617
DEFAULT_TTL_SECONDS = 24 * 3600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


async def get_config() -> dict:
    """Live config do cache (threshold calibrado + TTL) — lido a cada lookup."""
    doc = None
    try:
        doc = await ai_brain()[CONFIG_COLLECTION].find_one(
            {"active": True}, max_time_ms=MAX_TIME_MS
        )
    except Exception:  # noqa: BLE001 — config ausente nunca derruba o cache
        pass
    return {
        "hit_threshold": float((doc or {}).get("hit_threshold", DEFAULT_HIT_THRESHOLD)),
        "ttl_seconds": int((doc or {}).get("ttl_seconds", DEFAULT_TTL_SECONDS)),
    }


def _area_visible(doc_area: str | None, area: str) -> bool:
    """Isolation rule for the app-side postfilter path (mode "vector-postfilter").

    Mirrors the native index filter `{"area": {"$in": ["global", area]}}`.
    `None` means a seeded FAQ with no `area` field — treated as global.
    """
    return doc_area in (None, "global", area)


async def lookup(question: str, area: str = "default") -> dict:
    """Return {hit, score, threshold, answer, question, source_id, latency_ms, mode}.

    Cache isolation per AREA as a NATIVE pre-filter: `area` is a filter field in
    the vector index, so the ANN search only traverses entries visible to this
    area (its own + "global" seeded FAQs) — an answer written for Financeiro is
    never replayed to Suporte, and the top candidate is always a valid one no
    matter how large the cache grows. `mode` is "vector" (filtered search),
    "vector-postfilter" (index without the filter field yet — filtered app-side)
    or "exact" (index missing → normalized-text match). A miss still returns the
    top candidate's score, so the UI can show "how close" it was.
    """
    coll = poc()[CACHE_COLLECTION]
    cfg = await get_config()
    threshold = cfg["hit_threshold"]
    t0 = time.perf_counter()

    def _pipeline(with_filter: bool) -> list[dict]:
        stage = {
            "index": CACHE_INDEX,
            "path": CACHE_PATH,
            "query": question,
            "numCandidates": 50,
            # Sem filtro nativo o pós-filtro app-side descarta candidatos de
            # outras áreas — 25 candidatos evitam resultado vazio com cache grande.
            "limit": 1 if with_filter else 25,
        }
        if with_filter:
            stage["filter"] = {"area": {"$in": ["global", area]}}
        return [
            {"$vectorSearch": stage},
            {"$project": {"question": 1, "answer": 1, "model": 1, "area": 1,
                          "score": {"$meta": "vectorSearchScore"}}},
        ]

    try:
        docs = await aggregate_list(coll, _pipeline(True), length=1, maxTimeMS=MAX_TIME_MS)
        mode = "vector"
    except Exception:  # noqa: BLE001 — filtro não indexado → pós-filtro app-side
        try:
            docs = await aggregate_list(coll, _pipeline(False), length=25, maxTimeMS=MAX_TIME_MS)
            docs = [d for d in docs if _area_visible(d.get("area"), area)]
            mode = "vector-postfilter"
        except Exception:  # noqa: BLE001 — index not created yet → graceful fallback
            docs = await _exact_fallback(coll, question, area)
            mode = "exact"

    latency_ms = int((time.perf_counter() - t0) * 1000)
    if not docs:
        return {"hit": False, "score": 0.0, "threshold": threshold,
                "latency_ms": latency_ms, "mode": mode}

    top = docs[0]
    score = float(top.get("score", 0.0))
    hit = score >= threshold
    result = {
        "hit": hit,
        "score": round(score, 4),
        "threshold": threshold,
        "latency_ms": latency_ms,
        "mode": mode,
        "matched_question": top.get("question"),
        "matched_area": top.get("area"),  # None → entrada global (FAQ)
        "source_id": str(top.get("_id")),
    }
    if hit:
        result["answer"] = top.get("answer", "")
        result["model"] = top.get("model")
        # record the reuse so the client sees the counter climb
        await safe_query(
            coll.update_one(
                {"_id": top["_id"]},
                {"$inc": {"hits": 1}, "$set": {"last_hit_at": _utcnow()}},
            )
        )
    return result


async def _exact_fallback(coll, question: str, area: str = "default") -> list[dict]:
    """Normalized exact-match fallback when the vector index isn't available."""
    doc = await coll.find_one(
        {"question_norm": _normalize(question), "area": {"$in": ["global", area]}},
        max_time_ms=MAX_TIME_MS,
    )
    if not doc:
        return []
    doc["score"] = 1.0
    return [doc]


async def store(question: str, answer: str, model: str, scope: str = "support",
                area: str = "default") -> str:
    """Insert a new Q&A into the cache. Atlas auto-embeds `question` at write time.

    Runtime entries get `expires_at` (a real BSON date): the TTL index created by
    seed.py deletes them ttl_seconds after insertion — freshness handled by
    the database itself, not by application code.
    """
    coll = poc()[CACHE_COLLECTION]
    cfg = await get_config()
    doc = {
        "question": question,
        "question_norm": _normalize(question),  # powers the exact fallback
        "answer": answer,
        "model": model,
        "scope": scope,
        "area": area,  # cache isolation: only served back to users of this area
        "hits": 0,
        "created_at": _utcnow(),
        "last_hit_at": None,
        "expires_at": _utcnow() + timedelta(seconds=cfg["ttl_seconds"]),
    }
    res = await safe_query(coll.insert_one(doc))
    return str(res.inserted_id)


async def recent(limit: int = 20) -> list[dict]:
    """The most recently used cache entries — for the 'inspect the cache' panel."""
    coll = poc()[CACHE_COLLECTION]
    cursor = coll.find({}, {"question": 1, "answer": 1, "hits": 1, "model": 1, "area": 1,
                            "created_at": 1, "last_hit_at": 1, "expires_at": 1},
                       max_time_ms=MAX_TIME_MS).sort("created_at", -1).limit(limit)
    docs = await safe_query(cursor.to_list(length=limit))
    for d in docs:
        d["_id"] = str(d["_id"])
    return docs


async def clear() -> int:
    """Demo reset: empty the cache so the next question is a guaranteed MISS."""
    coll = poc()[CACHE_COLLECTION]
    res = await safe_query(coll.delete_many({}))
    return res.deleted_count
