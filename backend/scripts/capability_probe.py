"""Sonda de capability do cluster: o que o servidor ACEITA de verdade, medido.

Roda só nos bancos de teste (isolation.use_test_databases recusa a demo). Grava três
fatos descartáveis num user_key de sonda em `<teste>.agent_memory`, espera o autoEmbed
indexar, testa `$rankFusion` (com `scoreDetails`, pesos e `fuzzy`), `$rerank` depois da
fusão e a disponibilidade de Queryable Encryption no driver, e apaga os fatos no fim.

    cd backend && .venv/bin/python scripts/capability_probe.py [--json saida.json]

Nada aqui é suposto pela documentação: cada linha do relatório é a resposta do servidor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.isolation import use_test_databases  # noqa: E402

PROBE_USER = "probe_capability_2026_10"
PROBE_FACTS = (
    "Prefere ser contatado por e-mail, nunca por telefone.",
    "Comprou o fone Quantum 910 no pedido PED-1005.",
    "Mora em Goiânia e trabalha à noite.",
)


def _rank_fusion(query: str, *, score_details: bool, fuzzy: bool, weights=None) -> dict:
    import db
    text = {"query": query, "path": "fact"}
    if fuzzy:
        text["fuzzy"] = {"maxEdits": 1}
    stage = {
        "input": {"pipelines": {
            "vector": [db.tenant_vector_stage(
                index="agent_memory_vs", path="fact", query=query,
                tenant_filter={"user_key": PROBE_USER, "active": True},
                num_candidates=50, limit=10)],
            "bm25": [
                {"$search": {"index": "agent_memory_bm25", "compound": {
                    "must": [{"text": text}],
                    "filter": [{"equals": {"path": "user_key", "value": PROBE_USER}},
                               {"equals": {"path": "active", "value": True}}]}}},
                {"$limit": 10},
            ],
        }},
        "scoreDetails": score_details,
    }
    if weights:
        stage["combination"] = {"weights": weights}
    return {"$rankFusion": stage}


async def _try(name: str, coll, pipeline, report: dict) -> list[dict] | None:
    started = time.perf_counter()
    try:
        cursor = await coll.aggregate(pipeline, maxTimeMS=20_000)
        rows = await cursor.to_list(length=20)
        report[name] = {"ok": True, "rows": len(rows),
                        "ms": round((time.perf_counter() - started) * 1000, 1),
                        "sample": _sample(rows)}
        return rows
    except Exception as exc:  # noqa: BLE001 — a sonda registra o erro do servidor
        report[name] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:400]}",
                        "code": getattr(exc, "code", None),
                        "ms": round((time.perf_counter() - started) * 1000, 1)}
        return None


def _sample(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows[:3]:
        out.append({k: v for k, v in row.items() if k != "_id"})
    return json.loads(json.dumps(out, default=str))


async def _wait_indexed(coll, timeout_s: float = 180) -> float:
    import db
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        try:
            rows = await db.aggregate_list(coll, [db.tenant_vector_stage(
                index="agent_memory_vs", path="fact", query="contato por e-mail",
                tenant_filter={"user_key": PROBE_USER, "active": True},
                num_candidates=20, limit=5)], length=5)
            if len(rows) >= len(PROBE_FACTS):
                return round(time.monotonic() - started, 1)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(3)
    return -1.0


def _qe_driver_report() -> dict:
    out: dict = {}
    try:
        import pymongocrypt  # noqa: F401
        out["pymongocrypt"] = getattr(pymongocrypt, "__version__", "instalado")
        try:
            out["libmongocrypt"] = pymongocrypt.libmongocrypt_version()
        except Exception as exc:  # noqa: BLE001
            out["libmongocrypt"] = f"indisponível: {exc}"
    except ImportError as exc:
        out["pymongocrypt"] = f"ausente: {exc}"
    try:
        from pymongo.encryption import ClientEncryption  # noqa: F401
        out["pymongo.encryption"] = "importável"
    except Exception as exc:  # noqa: BLE001
        out["pymongo.encryption"] = f"falhou: {exc}"
    import os
    out["crypt_shared_path_env"] = bool(os.getenv("CRYPT_SHARED_LIB_PATH"))
    return out


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    use_test_databases(what="sonda de capability")
    import db

    client = db.get_client()
    coll = db.poc()["agent_memory"]
    report: dict = {"database": db.DB_MAIN, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    info = await client.admin.command("buildInfo")
    report["buildInfo"] = {"version": info.get("version"), "modules": info.get("modules"),
                           "gitVersion": info.get("gitVersion")}
    try:
        params = await client.admin.command({"getParameter": 1, "featureCompatibilityVersion": 1})
        report["fcv"] = params.get("featureCompatibilityVersion")
    except Exception as exc:  # noqa: BLE001
        report["fcv"] = f"sem permissão: {type(exc).__name__}"

    await coll.delete_many({"user_key": PROBE_USER})
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    await coll.insert_many([{"user_key": PROBE_USER, "fact": f, "category": "contexto",
                             "active": True, "created_at": now, "updated_at": now}
                            for f in PROBE_FACTS])
    report["autoembed_index_seconds"] = await _wait_indexed(coll)
    try:
        q = "como prefere ser contatado"
        await _try("rankFusion_basic", coll, [_rank_fusion(q, score_details=False, fuzzy=False),
                                              {"$limit": 5},
                                              {"$project": {"fact": 1, "score": {"$meta": "score"}}}], report)
        await _try("rankFusion_weights_fuzzy", coll, [
            _rank_fusion("contatdo por emial", score_details=False, fuzzy=True,
                         weights={"vector": 1.0, "bm25": 0.5}),
            {"$limit": 5}, {"$project": {"fact": 1, "score": {"$meta": "score"}}}], report)
        await _try("rankFusion_scoreDetails_meta_scoreDetails", coll, [
            _rank_fusion(q, score_details=True, fuzzy=True), {"$limit": 5},
            {"$project": {"fact": 1, "details": {"$meta": "scoreDetails"}}}], report)
        await _try("rankFusion_scoreDetails_meta_searchScoreDetails", coll, [
            _rank_fusion(q, score_details=True, fuzzy=True), {"$limit": 5},
            {"$project": {"fact": 1, "details": {"$meta": "searchScoreDetails"}}}], report)
        await _try("rankFusion_identifier_PED", coll, [
            _rank_fusion("PED-1005", score_details=False, fuzzy=True), {"$limit": 5},
            {"$project": {"fact": 1, "score": {"$meta": "score"}}}], report)
        for model in ("rerank-2.5", "rerank-2.5-lite", "rerank-3", "rerank-3-lite"):
            await _try(f"rerank_after_rankFusion_{model}", coll, [
                _rank_fusion(q, score_details=False, fuzzy=True), {"$limit": 10},
                {"$addFields": {"fusion_score": {"$meta": "score"}}},
                {"$rerank": {"query": {"text": q}, "path": "fact",
                             "numDocsToRerank": 10, "model": model}},
                {"$project": {"fact": 1, "fusion_score": 1, "rerank_score": {"$meta": "score"}}},
            ], report)
        await _try("rerank_inside_rankFusion_input_must_fail", coll, [
            {"$rankFusion": {"input": {"pipelines": {"v": [
                db.tenant_vector_stage(index="agent_memory_vs", path="fact", query=q,
                                       tenant_filter={"user_key": PROBE_USER, "active": True},
                                       num_candidates=20, limit=5),
                {"$rerank": {"query": {"text": q}, "path": "fact", "numDocsToRerank": 5,
                             "model": "rerank-2.5"}}]}}}}], report)
        await _try("rerank_invalid_model_must_fail", coll, [
            {"$match": {"user_key": PROBE_USER}}, {"$sort": {"_id": 1}},
            {"$rerank": {"query": {"text": q}, "path": "fact", "numDocsToRerank": 5,
                         "model": "modelo-inexistente"}}], report)
    finally:
        await coll.delete_many({"user_key": PROBE_USER})
    report["queryable_encryption_driver"] = _qe_driver_report()
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
