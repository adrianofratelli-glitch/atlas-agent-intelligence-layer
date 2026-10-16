"""Eval de retrieval da memória longa: recall@5, MRR, latência e round-trips ao Atlas.

Mede `memory.load_relevant` (o caminho do turno) contra `eval/memory_recall.json`, no
Atlas real e no banco de TESTE (scripts/isolation.py recusa a demo). Os fatos do dataset
são gravados em `<teste>.agent_memory` com user_keys próprios (`eval.mem.*`), incluindo
fatos SUPERSEDED (active=false + superseded_by), e o script espera o autoEmbed e o
índice BM25 indexarem antes de medir.

Invariantes conferidos em TODA consulta (falha = exit 1, não estatística):
  · nenhum fato de outro tenant; · nenhum fato inactive/superseded;
  · sem _id duplicado; · tamanho dentro do limite; · `mode` com valor conhecido.

    cd backend && .venv/bin/python scripts/memory_recall_eval.py --label baseline
    cd backend && .venv/bin/python scripts/memory_recall_eval.py --label final --repeat 3

`--strategy` escolhe o caminho medido (A3 compara RRF na aplicação, $rankFusion e
$rankFusion + $rerank); o padrão é o que o turno usa hoje.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.isolation import use_test_databases  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "eval" / "memory_recall.json"
REPORTS = ROOT / "eval" / "reports"
KNOWN_MODES = {"all", "hybrid", "vector", "recent", "rank_fusion", "app_rrf",
               "rank_fusion+rerank", "rerank_unavailable"}


class RoundTrips:
    """CommandListener: conta comandos enviados ao Atlas (round-trips reais)."""

    def __init__(self) -> None:
        self.count = 0
        self.by_name: dict[str, int] = {}

    def started(self, event) -> None:
        if event.command_name in {"hello", "isMaster", "ping", "endSessions"}:
            return
        self.count += 1
        self.by_name[event.command_name] = self.by_name.get(event.command_name, 0) + 1

    def succeeded(self, event) -> None:  # noqa: D401 — interface do pymongo
        pass

    def failed(self, event) -> None:
        pass


def _install_listener(listener: RoundTrips) -> None:
    """Cria o cliente do `db` com o listener, ANTES do primeiro uso."""
    import functools

    import db
    from pymongo import AsyncMongoClient, monitoring

    class _Listener(monitoring.CommandListener):
        def started(self, event):
            listener.started(event)

        def succeeded(self, event):
            listener.succeeded(event)

        def failed(self, event):
            listener.failed(event)

    db.AsyncMongoClient = functools.partial(AsyncMongoClient, event_listeners=[_Listener()])


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[idx], 1)


async def seed_fixtures(dataset: dict) -> dict[str, dict]:
    """Grava os fatos do dataset (idempotente) e devolve {ObjectId str: fato do dataset}."""
    import db
    from bson import ObjectId

    coll = db.poc()["agent_memory"]
    tenants = dataset["tenants"]
    fingerprint = json.dumps(tenants, sort_keys=True, ensure_ascii=False)
    meta = db.poc()["eval_fixture_meta"]
    current = await meta.find_one({"_id": "memory_recall"})
    keys = list(tenants)
    if current and current.get("fingerprint") == fingerprint and \
            await coll.count_documents({"user_key": {"$in": keys}}) == current.get("count"):
        mapping = current["mapping"]
        return {oid: {**_fact_by_id(tenants, fid), "user_key": uk}
                for oid, (fid, uk) in mapping.items()}

    await coll.delete_many({"user_key": {"$in": keys}})
    ids: dict[str, ObjectId] = {}
    for user_key, body in tenants.items():
        for fact in body["facts"]:
            ids[fact["id"]] = ObjectId()
    base = datetime.now(timezone.utc) - timedelta(days=30)
    docs, mapping = [], {}
    for user_key, body in tenants.items():
        for n, fact in enumerate(body["facts"]):
            created = base + timedelta(hours=n)
            doc = {"_id": ids[fact["id"]], "user_key": user_key, "fact": fact["fact"],
                   "category": fact.get("category", "contexto"),
                   "active": fact.get("active", True), "source_session": "eval",
                   "created_at": created, "updated_at": created}
            if fact.get("superseded_by"):
                doc["superseded_by"] = ids[fact["superseded_by"]]
            docs.append(doc)
            mapping[str(ids[fact["id"]])] = [fact["id"], user_key]
    if docs:
        await coll.insert_many(docs)
    await meta.replace_one({"_id": "memory_recall"},
                           {"fingerprint": fingerprint, "count": len(docs), "mapping": mapping},
                           upsert=True)
    return {oid: {**_fact_by_id(tenants, fid), "user_key": uk}
            for oid, (fid, uk) in mapping.items()}


def _fact_by_id(tenants: dict, fact_id: str) -> dict:
    for body in tenants.values():
        for fact in body["facts"]:
            if fact["id"] == fact_id:
                return fact
    raise KeyError(fact_id)


async def wait_indexed(dataset: dict, timeout_s: float = 600) -> float:
    """Espera vetor E BM25 enxergarem todos os fatos ativos de cada tenant."""
    import db

    coll = db.poc()["agent_memory"]
    started = time.monotonic()
    want = {uk: sum(1 for f in body["facts"] if f.get("active", True))
            for uk, body in dataset["tenants"].items() if body["facts"]}
    while time.monotonic() - started < timeout_s:
        ready = True
        for user_key, n in want.items():
            vec = await db.aggregate_list(coll, [db.tenant_vector_stage(
                index="agent_memory_vs", path="fact", query="cliente",
                tenant_filter={"user_key": user_key, "active": True},
                num_candidates=200, limit=100)], length=200)
            lex = await db.aggregate_list(coll, [
                {"$search": {"index": "agent_memory_bm25", "compound": {
                    "should": [{"exists": {"path": "fact"}}],
                    "filter": [{"equals": {"path": "user_key", "value": user_key}},
                               {"equals": {"path": "active", "value": True}}]}}},
                {"$limit": 200}], length=200)
            if len(vec) < n or len(lex) < n:
                ready = False
                break
        if ready:
            return round(time.monotonic() - started, 1)
        await asyncio.sleep(5)
    raise SystemExit("índices não indexaram os fatos do eval dentro do prazo")


def _strategy_fn(name: str):
    import memory

    if name == "current":
        return memory.load_relevant
    if hasattr(memory, "load_relevant_with"):
        return lambda user_key, query: memory.load_relevant_with(user_key, query, strategy=name)
    raise SystemExit(f"estratégia {name!r} não existe neste código")


async def run_cases(dataset: dict, fixtures: dict[str, dict], strategy: str, repeat: int,
                    listener: RoundTrips) -> dict:
    fn = _strategy_fn(strategy)
    limit = dataset.get("relevant_limit", 5)
    rows, violations = [], []
    lat, trips = [], []
    for case in dataset["cases"]:
        for attempt in range(repeat):
            before = listener.count
            started = time.perf_counter()
            result = await fn(case["user_key"], case["query"])
            elapsed = (time.perf_counter() - started) * 1000
            used = listener.count - before
            lat.append(elapsed)
            trips.append(used)
            if attempt:
                continue
            facts = result.get("facts", [])
            returned = [fixtures.get(f["_id"], {"id": f"externo:{f['_id']}",
                                                 "user_key": "?"}) for f in facts]
            ids = [r.get("id") for r in returned]
            problems = []
            if any(r.get("user_key") != case["user_key"] for r in returned):
                problems.append("fato de outro tenant")
            if any(r.get("active", True) is False for r in returned):
                problems.append("fato inactive/superseded")
            if len(ids) != len(set(ids)):
                problems.append("duplicata")
            # 5 do ranking + até RECENT_MERGE recentes fundidos
            if len(facts) > limit + 2:
                problems.append(f"acima do limite ({len(facts)})")
            if result.get("mode") not in KNOWN_MODES:
                problems.append(f"modo desconhecido {result.get('mode')!r}")
            if set(case.get("forbidden", [])) & set(ids):
                problems.append("superseded voltou")
            if case.get("expect_empty") and facts:
                problems.append("tenant vazio devolveu fatos")
            expected = case.get("expected", [])
            top = ids[:limit]
            rank = next((i + 1 for i, fid in enumerate(ids) if fid in expected), None)
            hit = bool(expected) and any(fid in expected for fid in top)
            rows.append({"id": case["id"], "kind": case["kind"], "query": case["query"],
                         "expected": expected, "returned": ids, "mode": result.get("mode"),
                         "hit_at_5": hit if expected else None,
                         "rank": rank, "ms": round(elapsed, 1), "round_trips": used,
                         "explain": result.get("explain"),
                         "problems": problems})
            if problems:
                violations.append({"id": case["id"], "problems": problems})
    scored = [r for r in rows if r["expected"]]
    by_kind: dict[str, dict] = {}
    for kind in sorted({r["kind"] for r in scored}):
        sub = [r for r in scored if r["kind"] == kind]
        by_kind[kind] = {"cases": len(sub),
                         "recall_at_5": round(sum(r["hit_at_5"] for r in sub) / len(sub), 3),
                         "mrr": round(sum(1 / r["rank"] if r["rank"] and r["rank"] <= limit else 0
                                          for r in sub) / len(sub), 3)}
    return {
        "summary": {
            "strategy": strategy,
            "cases": len(rows), "scored": len(scored), "repeat": repeat,
            "recall_at_5": round(sum(r["hit_at_5"] for r in scored) / len(scored), 3),
            "mrr": round(sum(1 / r["rank"] if r["rank"] and r["rank"] <= limit else 0
                             for r in scored) / len(scored), 3),
            "latency_ms": {"p50": _pct(lat, 0.50), "p95": _pct(lat, 0.95),
                           "mean": round(statistics.fmean(lat), 1)},
            "round_trips": {"mean": round(statistics.fmean(trips), 2), "max": max(trips)},
            "modes": sorted({r["mode"] for r in rows if r["mode"]}),
            "violations": len(violations),
            "by_kind": by_kind,
        },
        "violations": violations,
        "rows": rows,
    }


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="run")
    parser.add_argument("--strategy", default="current")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--config-json", help="grava ai_brain_test.memory_retrieval_config antes "
                        "de medir (só banco de teste); '{}' remove o documento")
    args = parser.parse_args(argv)
    use_test_databases(what="eval de recall da memória")
    listener = RoundTrips()
    _install_listener(listener)
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    if args.config_json is not None:
        import db
        import memory
        coll = db.ai_brain()[memory.RETRIEVAL_CONFIG_COLLECTION]
        cfg = json.loads(args.config_json)
        if cfg:
            await coll.replace_one({"_id": memory.RETRIEVAL_CONFIG_ID}, cfg, upsert=True)
        else:
            await coll.delete_one({"_id": memory.RETRIEVAL_CONFIG_ID})
    fixtures = await seed_fixtures(dataset)
    waited = await wait_indexed(dataset)
    # aquece o pool (o primeiro comando paga handshake TLS e distorceria o p95)
    await _strategy_fn(args.strategy)("eval.mem.ana", "aquecimento")
    report = await run_cases(dataset, fixtures, args.strategy, args.repeat, listener)
    import db
    report["summary"].update({"label": args.label, "database": db.DB_MAIN,
                              "config": json.loads(args.config_json) if args.config_json else None,
                              "index_wait_seconds": waited,
                              "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    out = args.out or REPORTS / (f"memory-recall-{args.label}-"
                                 f"{datetime.now().strftime('%Y-%m-%d')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n",
                   encoding="utf-8")
    s = report["summary"]
    print(json.dumps({k: s[k] for k in ("strategy", "recall_at_5", "mrr", "latency_ms",
                                        "round_trips", "modes", "violations", "by_kind")},
                     indent=2, ensure_ascii=False))
    print(f"relatório: {out}")
    for v in report["violations"]:
        print(f"VIOLAÇÃO {v['id']}: {', '.join(v['problems'])}")
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
