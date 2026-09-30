"""Memória plugável: duas implementações por trás da mesma interface.

O PoV usa a NATIVA por padrão — a pergunta que o benchmark responde é se vale a
pena trocar por um framework de memória, não qual está "ligado".

* `AtlasMemory` — a implementação desta PoV (`backend/memory.py`): fatos em
  `POC.agent_memory`, recuperação híbrida ($vectorSearch pré-filtrado por
  `user_key`+`active` + BM25 fundidos com RRF), supersessão e deduplicação. Tudo
  documento no MESMO cluster que os dados de negócio.
* `Mem0Memory` — adaptador para o Mem0 com o vector store MongoDB
  (`mem0.vector_stores.mongodb`). Suporte confirmado na doc oficial
  (https://docs.mem0.ai/components/vectordbs/dbs/mongodb) e na assinatura do
  pacote instalado; **versão testada: mem0ai 2.1.0**, com embedder `fastembed`
  (local, sem chave) e LLM Anthropic.

Dependência pesada: o `mem0ai` troca `jiter`/`protobuf` e puxa openai +
qdrant-client, então NÃO entra no venv do PoV. Ele vive em `.venv-memory`
(`uv venv --python 3.12 .venv-memory && uv pip install --python .venv-memory/bin/python
"mem0ai==2.1.0" fastembed "pymongo>=4.17,<5" python-dotenv "anthropic>=0.109"`),
que é o venv usado por `scripts/memory_benchmark.py`. `Mem0Memory` importa o
pacote só quando é instanciado — no venv do PoV, a classe existe e falha ao ser
usada, em vez de quebrar o import de quem só quer a nativa.

Interface (a mínima que o agente realmente usa):

    await backend.write(user_key, fact)            -> id do fato
    await backend.search(user_key, query, limit)   -> [{"fact": str, "score": float}]
    await backend.all(user_key)                    -> [{"fact": str}]
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Protocol


class MemoryBackend(Protocol):
    name: str

    async def write(self, user_key: str, fact: str) -> str: ...

    async def search(self, user_key: str, query: str, limit: int = 3) -> list[dict]: ...

    async def all(self, user_key: str) -> list[dict]: ...


class AtlasMemory:
    """A memória nativa da PoV, sobre `POC.agent_memory`.

    `write` grava o documento no MESMO formato e na MESMA collection que
    `memory.extract_and_store` grava, mas SEM passar pelo extrator por LLM — de
    propósito: o benchmark compara a camada de armazenamento/recuperação dos dois
    backends, e do lado do Mem0 o equivalente é `add(..., infer=False)`. Medir um
    lado com extrator e o outro sem daria um número que não significa nada.
    """

    name = "atlas_native"

    async def write(self, user_key: str, fact: str) -> str:
        import memory
        from db import poc

        now = memory._utcnow()
        result = await poc()[memory.MEMORY_COLLECTION].insert_one({
            "user_key": user_key, "fact": fact, "fact_norm": memory._norm(fact),
            "category": "contexto", "active": True, "source_session": "benchmark",
            "created_at": now, "updated_at": now, "superseded_by": None,
        })
        return str(result.inserted_id)

    async def search(self, user_key: str, query: str, limit: int = 3) -> list[dict]:
        import memory

        result = await memory.load_relevant(user_key, query)
        return [{"fact": row.get("fact", ""), "score": row.get("score", 0.0),
                 "mode": result.get("mode")}
                for row in (result.get("facts") or [])[:limit]]

    async def all(self, user_key: str) -> list[dict]:
        import memory

        result = await memory.load_longterm(user_key)
        return [{"fact": row.get("fact", "")} for row in result.get("facts") or []]


class Mem0Memory:
    """Adaptador Mem0 com vector store MongoDB (mesmo cluster, outra collection).

    O Mem0 é síncrono; cada chamada roda num thread para não bloquear o event loop
    — é assim que ele entraria no `run_agent` de verdade.
    """

    name = "mem0_mongodb"
    TESTED_VERSION = "2.1.0"

    def __init__(self, *, db_name: str, collection_name: str = "mem0_facts",
                 mongo_uri: str | None = None, model: str | None = None):
        from mem0 import Memory  # ImportError aqui = venv errado, e a mensagem diz isso

        import mem0

        self.package_version = getattr(mem0, "__version__", "desconhecida")
        config = {
            "vector_store": {"provider": "mongodb", "config": {
                "db_name": db_name,
                "collection_name": collection_name,
                "mongo_uri": mongo_uri or os.environ["MONGODB_URI"],
                # fastembed BAAI/bge-small-en-v1.5 = 384 dimensões.
                "embedding_model_dims": 384,
            }},
            # Embedder LOCAL: o benchmark não pode depender de uma chave OpenAI que
            # este PoV não tem, e o autoEmbed do Atlas (voyage-4) é do índice
            # vetorial nativo, não algo que o Mem0 saiba usar.
            "embedder": {"provider": "fastembed",
                         "config": {"model": "BAAI/bge-small-en-v1.5"}},
            "llm": {"provider": "anthropic", "config": {
                "model": model or os.getenv("MEM0_MODEL", "claude-haiku-4-5"),
                "api_key": os.getenv("ANTHROPIC_API_KEY"),
            }},
        }
        self.memory = Memory.from_config(config)

    async def write(self, user_key: str, fact: str) -> str:
        result = await asyncio.to_thread(
            self.memory.add, fact, user_id=user_key, infer=False)
        results = (result or {}).get("results") or []
        return str(results[0].get("id", "")) if results else ""

    async def search(self, user_key: str, query: str, limit: int = 3) -> list[dict]:
        # mem0 2.1.0 recusa `user_id` como argumento de topo em search()/get_all():
        # "Top-level entity parameters ... are not supported ... Use filters=".
        # `add()` ainda aceita — a API não é simétrica entre escrita e leitura.
        result = await asyncio.to_thread(
            self.memory.search, query=query, filters={"user_id": user_key}, limit=limit)
        return [{"fact": row.get("memory", ""), "score": row.get("score", 0.0)}
                for row in (result or {}).get("results") or []]

    async def all(self, user_key: str) -> list[dict]:
        result = await asyncio.to_thread(self.memory.get_all, filters={"user_id": user_key})
        return [{"fact": row.get("memory", "")}
                for row in (result or {}).get("results") or []]


async def timed(coro) -> tuple[object, float]:
    """(resultado, milissegundos) — o benchmark mede latência de verdade, não estima."""
    started = time.perf_counter()
    result = await coro
    return result, (time.perf_counter() - started) * 1000
