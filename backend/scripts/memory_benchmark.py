"""Benchmark de memória: nativa (Atlas) x Mem0 (vector store MongoDB).

Mede DUAS coisas, contra o Atlas real e no banco de TESTE isolado:

* **Recall entre sessões** — os fatos são gravados numa "sessão" e consultados em
  outra, com a pergunta escrita como o cliente escreveria (não com o texto do
  fato). Recall@3 = fração das consultas cujo fato esperado aparece no top-3.
* **Latência de escrita e de leitura** — p50/p95 medidos, nunca estimados.

    # nativa (venv do PoV)
    cd backend && .venv/bin/python scripts/memory_benchmark.py --backend atlas

    # Mem0 (venv separado: mem0ai troca jiter/protobuf e puxa openai+qdrant)
    cd backend && ../.venv-memory/bin/python scripts/memory_benchmark.py --backend mem0

    # relatório comparando os dois arquivos
    cd backend && .venv/bin/python scripts/memory_benchmark.py --compare atlas.json mem0.json

Isolamento: o benchmark ESCREVE, então roda nos bancos de teste
(`scripts/isolation.py`) e recusa o banco da demo sem `ALLOW_DEMO_DB_WRITE=1`.
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

USER_KEY = "bench.memoria"

# Fatos escritos na "sessão 1" e as perguntas da "sessão 2". A pergunta NUNCA
# repete as palavras do fato — recall com a mesma frase mediria string matching.
CASES = [
    ("Meu orçamento máximo para substituições é de 800 reais.",
     "até quanto eu disse que posso gastar?"),
    ("Sou alérgico a látex.",
     "tem algum material que eu não posso usar?"),
    ("Costumo comprar sempre a versão preta dos produtos.",
     "qual cor eu prefiro?"),
    ("Prefiro ser contatado à noite, depois das 19h.",
     "qual o melhor horário para falar comigo?"),
    ("Moro em apartamento e não tenho portaria 24 horas.",
     "tem alguma restrição para a entrega no meu endereço?"),
    ("Uso os fones principalmente para reuniões de trabalho.",
     "para que eu uso o produto no dia a dia?"),
    ("Já tive problema com entrega atrasada no mês passado.",
     "tive algum problema anterior com vocês?"),
    ("Prefiro receber a nota fiscal por e-mail.",
     "como eu quero receber os documentos?"),
]


def percentiles(values: list[float]) -> dict:
    if not values:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "mean_ms": 0.0}
    ordered = sorted(values)
    return {
        "p50_ms": round(ordered[max(0, math.ceil(0.50 * len(ordered)) - 1)], 1),
        "p95_ms": round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 1),
        "mean_ms": round(statistics.fmean(ordered), 1),
    }


async def build_backend(name: str):
    import memory_backends

    if name == "atlas":
        return memory_backends.AtlasMemory(), {}
    import os

    backend = memory_backends.Mem0Memory(db_name=os.environ["MONGODB_DB"])
    return backend, {"mem0_version": backend.package_version,
                     "embedder": "fastembed BAAI/bge-small-en-v1.5",
                     "vector_store": "mongodb (mem0.vector_stores.mongodb)"}


async def run(args) -> int:
    import isolation

    isolation.use_test_databases(what=f"memory_benchmark --backend {args.backend}")
    backend, extra = await build_backend(args.backend)
    user_key = f"{USER_KEY}.{args.backend}"

    # --- sessão 1: escrita -------------------------------------------------
    write_ms: list[float] = []
    for fact, _ in CASES:
        started = time.perf_counter()
        await backend.write(user_key, fact)
        write_ms.append((time.perf_counter() - started) * 1000)

    if args.index_wait:
        # O índice vetorial (autoEmbed no Atlas, ou o do Mem0) indexa de forma
        # ASSÍNCRONA: consultar antes disso mediria o fallback, não a busca.
        print(f"[benchmark] aguardando {args.index_wait:.0f}s de indexação...", flush=True)
        await asyncio.sleep(args.index_wait)

    # --- sessão 2 (outra sessão, mesmo usuário): recall ---------------------
    read_ms: list[float] = []
    hits, rows = 0, []
    for fact, question in CASES:
        started = time.perf_counter()
        found = await backend.search(user_key, question, limit=3)
        read_ms.append((time.perf_counter() - started) * 1000)
        top = [row["fact"] for row in found]
        hit = any(fact.strip().lower()[:40] in (candidate or "").strip().lower()
                  for candidate in top)
        hits += hit
        rows.append({"question": question, "expected": fact, "hit": hit,
                     "returned": top[:3],
                     # `mode` (só a nativa) diz QUAL caminho respondeu: "hybrid" é a
                     # busca de verdade; "recent"/"all" significa que o índice ainda
                     # não tinha os fatos e o número mede atraso de indexação, não
                     # qualidade de recuperação.
                     "mode": (found[0].get("mode") if found else None)})

    summary = {
        "backend": backend.name,
        "database": __import__("os").environ["MONGODB_DB"],
        "cases": len(CASES),
        "recall_at_3": round(hits / len(CASES), 4),
        "write": percentiles(write_ms),
        "read": percentiles(read_ms),
        "index_wait_seconds": args.index_wait,
        "retrieval_modes": sorted({row.get("mode") for row in rows if row.get("mode")}),
        **extra,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "rows": rows},
                                              indent=2, ensure_ascii=False))
    if args.cleanup:
        await cleanup(args.backend, user_key)
    return 0


async def cleanup(backend_name: str, user_key: str) -> None:
    """Apaga o que o benchmark escreveu (o banco é de teste, mas não é lixeira)."""
    import os

    from pymongo import MongoClient

    client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=15_000)
    database = client[os.environ["MONGODB_DB"]]
    collection = "agent_memory" if backend_name == "atlas" else "mem0_facts"
    removed = database[collection].delete_many({
        "user_key": user_key} if backend_name == "atlas" else {"user_id": user_key})
    print(f"[benchmark] limpeza: {removed.deleted_count} documento(s) em {collection}")
    client.close()


def compare(paths: list[str]) -> None:
    reports = [json.loads(Path(p).read_text())["summary"] for p in paths]
    keys = ["backend", "recall_at_3", "write", "read", "database"]
    for key in keys:
        print(f"{key}: " + " | ".join(str(report.get(key)) for report in reports))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["atlas", "mem0"], default="atlas")
    parser.add_argument("--json", help="grava {summary, rows}")
    parser.add_argument("--compare", nargs="+", help="compara relatórios já gravados")
    parser.add_argument("--index-wait", type=float, default=30.0,
                        help="segundos de espera pela indexação assíncrona (0 desliga)")
    parser.add_argument("--cleanup", action="store_true", default=True)
    parser.add_argument("--no-cleanup", dest="cleanup", action="store_false")
    args = parser.parse_args()
    if args.compare:
        compare(args.compare)
        return 0
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
