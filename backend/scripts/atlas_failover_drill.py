"""Exercício de failover REAL no Atlas, com o agente rodando por cima.

O que ele prova, que nenhuma simulação prova: durante a eleição de um novo
primário, o driver reexecuta as operações sozinho (`retryWrites`/`retryReads`) e
o turno do agente **não vê nada**. É o argumento de HA do Atlas demonstrado em
vez de afirmado.

⚠️  LEIA ANTES DE RODAR — `POST /clusters/{name}/restartPrimaries` (test failover)
    é operação de CLUSTER INTEIRO, não de banco. Este cluster hospeda outras PoVs
    (`multi_agent_poc`, `finscope`, `tjgo_pdtic`, `pix`…): todas perdem o primário
    por alguns segundos junto. NÃO rode durante apresentação de ninguém, nem em
    janela compartilhada sem avisar.

Por isso há duas travas, e as duas são obrigatórias:

    ATLAS_FAILOVER_DRILL=1                  liga o script
    --cluster <nome> --confirm <nome>       você digita o nome do cluster duas vezes

Credenciais (chaves de API da Organização/Projeto, Digest Auth):

    ATLAS_PUBLIC_KEY, ATLAS_PRIVATE_KEY, ATLAS_GROUP_ID

Uso:

    cd backend && ATLAS_FAILOVER_DRILL=1 .venv/bin/python scripts/atlas_failover_drill.py \\
        --cluster meu-cluster --confirm meu-cluster

O que acontece: o script começa a escrever/ler no banco de TESTE em laço (uma
operação a cada 200ms), dispara o failover, e mede quantas operações falharam de
verdade contra quantas o driver salvou. No fim imprime a janela de indisponível.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

ATLAS_API = "https://cloud.mongodb.com/api/atlas/v2"


def guard(args) -> None:
    if os.getenv("ATLAS_FAILOVER_DRILL", "").strip() not in {"1", "true", "yes"}:
        raise SystemExit(
            "\nRECUSADO: este script derruba o primário do CLUSTER INTEIRO, que é "
            "compartilhado\ncom outras PoVs. Se é isso mesmo que você quer, e é hora de "
            "fazer isso:\n\n    ATLAS_FAILOVER_DRILL=1 ... --cluster <nome> --confirm <nome>\n")
    if args.cluster != args.confirm:
        raise SystemExit("RECUSADO: --confirm não bate com --cluster.")
    for key in ("ATLAS_PUBLIC_KEY", "ATLAS_PRIVATE_KEY", "ATLAS_GROUP_ID"):
        if not os.getenv(key):
            raise SystemExit(f"RECUSADO: {key} não definida (chave de API do Atlas).")


async def trigger_failover(cluster: str) -> int:
    """Dispara o test failover. Retorna o status HTTP da API."""
    import httpx

    url = f"{ATLAS_API}/groups/{os.environ['ATLAS_GROUP_ID']}/clusters/{cluster}/restartPrimaries"
    auth = httpx.DigestAuth(os.environ["ATLAS_PUBLIC_KEY"], os.environ["ATLAS_PRIVATE_KEY"])
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url, auth=auth,
            headers={"Accept": "application/vnd.atlas.2023-01-01+json"})
        return response.status_code


async def workload(stop: asyncio.Event, stats: dict) -> None:
    """Escreve e lê sem parar no banco de TESTE, contando o que o driver salvou."""
    import isolation

    isolation.use_test_databases(what="atlas_failover_drill")
    from db import poc

    collection = poc()["failover_drill"]
    n = 0
    while not stop.is_set():
        n += 1
        started = time.perf_counter()
        try:
            await collection.update_one({"_id": "drill"}, {"$set": {"n": n}}, upsert=True)
            await collection.find_one({"_id": "drill"})
            stats["ok"] += 1
            stats["slowest_ms"] = max(stats["slowest_ms"], (time.perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001 — falha que o retry NÃO salvou
            stats["failed"] += 1
            stats["errors"].append(f"{type(exc).__name__}: {str(exc)[:80]}")
        await asyncio.sleep(0.2)


async def main(args) -> int:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    guard(args)

    stats = {"ok": 0, "failed": 0, "slowest_ms": 0.0, "errors": []}
    stop = asyncio.Event()
    task = asyncio.create_task(workload(stop, stats))
    await asyncio.sleep(3)                     # linha de base antes do failover

    print(f"\n[drill] disparando test failover no cluster {args.cluster}...", flush=True)
    status = await trigger_failover(args.cluster)
    print(f"[drill] API respondeu {status}", flush=True)
    if status not in (200, 202):
        stop.set()
        await task
        return 1

    for remaining in range(args.seconds, 0, -10):
        print(f"[drill] observando… {remaining}s (ok={stats['ok']} falhas={stats['failed']})",
              flush=True)
        await asyncio.sleep(min(10, remaining))
    stop.set()
    await task

    print(f"\n[drill] operações bem-sucedidas: {stats['ok']}")
    print(f"[drill] operações que o retry NÃO salvou: {stats['failed']}")
    print(f"[drill] operação mais lenta: {stats['slowest_ms']:.0f}ms "
          "(a espera da reeleição aparece aqui)")
    for err in stats["errors"][:5]:
        print(f"    · {err}")
    print("\nLeitura: falhas=0 com a lenta na casa de segundos é o resultado esperado —")
    print("o driver esperou a eleição e reexecutou. Falhas > 0 significam que o retry")
    print("esgotou; aí o app degrada com mensagem de conexão (cenário atlas_failover).\n")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", required=True, help="nome do cluster no Atlas")
    parser.add_argument("--confirm", required=True, help="repita o nome do cluster")
    parser.add_argument("--seconds", type=int, default=60, help="janela de observação")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
