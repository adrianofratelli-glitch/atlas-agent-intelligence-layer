"""Teste de carga do turno do agente — para a PoV ter UM número, não uma opinião.

A PoV afirma capacidade em três lugares (`maxPoolSize=50`, pool de sessões MCP,
tier M10/M20) e não tinha nenhuma medição. Isto responde à pergunta que o cliente
faz: "quantos atendimentos simultâneos isso aguenta, e com que latência?".

Dois modos, e a diferença importa:

    --mode data   (default) exercita a CAMADA DE DADOS do turno: guardrail com
                  $vectorSearch, cache semântico, memória híbrida (vector+BM25 com
                  RRF), perfil de área e escrita de sessão. Sem LLM e sem MCP, ou
                  seja: mede o Atlas, que é o que esta PoV vende. Custo zero em
                  tokens, seguro de repetir.
    --mode full   turno inteiro (LLM + MCP). Mede a experiência real ponta a ponta,
                  mas o gargalo passa a ser o provedor — e gasta tokens de verdade.

Sempre no banco de TESTE isolado (`scripts/isolation.py`), nunca no da demo.

    cd backend && .venv/bin/python scripts/load_test.py --users 10 --turns 3
    cd backend && .venv/bin/python scripts/load_test.py --mode full --users 4 --turns 1
    cd backend && .venv/bin/python scripts/load_test.py --users 20 --json ../eval/reports/load.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

MESSAGES = [
    "qual o status do meu pedido?",
    "quero trocar um produto com defeito",
    "qual o prazo de reembolso?",
    "meu pedido chegou danificado, e agora?",
    "vocês têm um fone parecido para substituir?",
]
IDENTITIES = ["cliente-demo", "ana.vendas", "marina.fin", "carlos.log"]


def percentiles(values: list[float]) -> dict:
    if not values:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0}
    ordered = sorted(values)
    pick = lambda q: ordered[max(0, math.ceil(q * len(ordered)) - 1)]  # noqa: E731
    return {"p50_ms": round(pick(0.50), 1), "p95_ms": round(pick(0.95), 1),
            "p99_ms": round(pick(0.99), 1), "max_ms": round(ordered[-1], 1)}


async def data_turn(user_key: str, message: str, session_id: str) -> dict:
    """As operações de Atlas de UM turno, na mesma ordem do pipeline real."""
    import cache
    import memory
    import policy_guardrails as guardrails
    import profiles
    import turn_classifier
    from db import MAX_TIME_MS, poc

    started = time.perf_counter()
    user = await profiles.require_demo_user(user_key)
    area = user.get("area", profiles.DEFAULT_AREA)
    await profiles.get_area_profile(area)
    guard = await guardrails.check_input(message, user_key, session_id, area)
    masked = guard.get("masked_text") or message
    await cache.lookup(masked, area)
    await memory.load_relevant(user_key, masked)
    await turn_classifier.classify(masked)
    await poc()["agent_sessions"].update_one(
        {"session_id": session_id, "user_key": user_key},
        {"$push": {"turns": {"$each": [{"role": "user", "content": masked}], "$slice": -50}},
         "$set": {"updated_at": time.time()}},
        upsert=True)
    return {"ms": (time.perf_counter() - started) * 1000, "area": area,
            "blocked": guard["action"] == "block"}


async def full_turn(session, user_key: str, message: str, session_id: str) -> dict:
    import agent

    started = time.perf_counter()
    result = await agent.run_agent(session, scenario=None, message=message,
                                   conversation_id=session_id, user_key=user_key)
    metrics = result.get("metrics") or {}
    return {"ms": (time.perf_counter() - started) * 1000,
            "area": (result.get("profile") or {}).get("area"),
            "blocked": False, "degraded": bool(metrics.get("degraded")),
            "tools": metrics.get("tools_used", 0)}


async def run(args) -> int:
    import isolation

    isolation.use_test_databases(what=f"load_test --mode {args.mode}")
    import db

    options = db.get_client().options
    print(f"[carga] modo={args.mode} usuários={args.users} turnos={args.turns} "
          f"(total {args.users * args.turns})")
    print(f"[carga] maxPoolSize={options.pool_options.max_pool_size} "
          f"minPoolSize={options.pool_options.min_pool_size} "
          f"timeoutMS={options.timeout} retryWrites={options.retry_writes}")

    session = None
    stack = None
    if args.mode == "full":
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        import agent

        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(agent.mcp_server_params()))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

    latencies: list[float] = []
    errors: list[str] = []
    results: list[dict] = []

    conversation_ids: list[str] = []

    async def worker(index: int) -> None:
        user_key = IDENTITIES[index % len(IDENTITIES)]
        for turn in range(args.turns):
            message = MESSAGES[(index + turn) % len(MESSAGES)]
            conversation = f"conv_load_{index}_{turn}_{int(time.time() * 1000)}"
            conversation_ids.append(conversation)
            try:
                row = (await data_turn(user_key, message, conversation) if args.mode == "data"
                       else await full_turn(session, user_key, message, conversation))
                latencies.append(row["ms"])
                results.append(row)
            except Exception as exc:  # noqa: BLE001 — erro sob carga É o resultado
                errors.append(f"{type(exc).__name__}: {str(exc)[:100]}")

    wall_started = time.perf_counter()
    await asyncio.gather(*[worker(i) for i in range(args.users)])
    wall = time.perf_counter() - wall_started
    if stack is not None:
        import anyio

        try:
            await stack.aclose()
        except* anyio.BrokenResourceError:
            pass

    total = len(latencies) + len(errors)
    summary = {
        "mode": args.mode,
        "database": db.DB_MAIN,
        "concurrent_users": args.users,
        "turns_per_user": args.turns,
        "total_turns": total,
        "succeeded": len(latencies),
        "failed": len(errors),
        "wall_seconds": round(wall, 2),
        "throughput_turns_per_second": round(len(latencies) / wall, 2) if wall else 0.0,
        "latency": percentiles(latencies),
        "mean_ms": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        "pool": {"max": options.pool_options.max_pool_size,
                 "min": options.pool_options.min_pool_size,
                 "timeout_ms": options.timeout},
        "errors": errors[:5],
        "measurement_scope": ("modo 'data' mede a camada de dados do turno (guardrail, cache, "
                              "memória híbrida, sessão) sem LLM nem MCP; 'full' inclui os dois"),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "rows": results},
                                              indent=2, ensure_ascii=False), encoding="utf-8")

    from pymongo import MongoClient
    import os

    client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=15_000)
    removed = client[db.DB_MAIN]["agent_sessions"].delete_many(
        {"session_id": {"$in": conversation_ids}}) if conversation_ids else None
    print(f"[carga] limpeza: {getattr(removed, 'deleted_count', 0)} sessão(ões) removida(s)")
    client.close()
    return 1 if errors else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["data", "full"], default="data")
    parser.add_argument("--users", type=int, default=10, help="turnos simultâneos")
    parser.add_argument("--turns", type=int, default=3, help="turnos por usuário")
    parser.add_argument("--json", help="grava {summary, rows}")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
