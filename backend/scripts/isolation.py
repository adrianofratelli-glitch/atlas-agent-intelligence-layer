"""Isolamento de banco para os scripts que escrevem dado REAL no Atlas.

Regra (mesma do PoV multiagente): `eval_agent.py --live`, `memory_benchmark.py` e
o cenário `crash_resume` da bateria de caos NUNCA escrevem no banco da demo. Eles
usam um par de bancos de teste no MESMO cluster, derivado do nome configurado:

    MONGODB_DB=POC            ->  MONGODB_TEST_DB=POC_test
    MONGODB_BRAIN_DB=ai_brain ->  MONGODB_TEST_BRAIN_DB=ai_brain_test

Os dois nomes podem ser sobrescritos por env. O destino é conferido ANTES de
qualquer escrita: se apontar para o banco da demo, o script RECUSA rodar, a menos
que `ALLOW_DEMO_DB_WRITE=1` seja passado explicitamente.

O banco de teste nasce vazio: `ensure_seeded()` roda o `seed.py` (com os índices
Search/Vector reais) apontado para ele e copia, em LEITURA, a configuração medida
que vive só no cluster (`cache_config`, `guardrail_policies`, `turn_classifier_config`)
e os probes do classificador (`turn_probes`) — sem isso o eval isolado mediria o
fallback, não o mesmo caminho de embedding da demo.

Uso:

    cd backend && ../.venv/bin/python scripts/isolation.py      # provisiona o banco de teste
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# O `.env` do PoV é carregado AQUI, e não só dentro de `ensure_seeded`: quem chama
# `use_test_databases` pode estar em outro venv (o do Mem0, por exemplo), onde
# `backend/db.py` ainda nem foi importado e MONGODB_URI não estaria no ambiente.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DEMO_MAIN_DB = "POC"
DEMO_BRAIN_DB = "ai_brain"

# Documentos MEDIDOS que vivem só no cluster. Copiados da demo para o teste em leitura.
BRAIN_CONFIG_COLLECTIONS = ("cache_config", "guardrail_policies", "turn_classifier_config")
# Probes do classificador de turno: sem eles `turn_classifier.classify()` não tem
# vizinho para medir e o turno cai no caminho de fallback.
BRAIN_PROBE_COLLECTIONS = ("turn_probes",)
# `produtos_vector` NÃO entra: o catálogo é somente leitura e continua no banco da
# demo (db.DB_CATALOG) — não há escrita para isolar, e copiar 500k documentos com
# índice autoEmbed a cada rodada só queimaria tempo de cluster.
INDEXED_COLLECTIONS = ("semantic_cache", "guardrail_denylist", "agent_memory")


class DemoDatabaseRefused(SystemExit):
    """Recusa explícita: o destino é o banco da demo."""


def allow_demo_write() -> bool:
    return os.getenv("ALLOW_DEMO_DB_WRITE", "").strip().lower() in {"1", "true", "yes", "on"}


def test_database_names() -> tuple[str, str]:
    """Deriva SEMPRE do nome de banco da DEMO, nunca do valor atual de MONGODB_DB.

    Bug real encontrado pela bateria de caos: como `use_test_databases` muta
    `os.environ["MONGODB_DB"]` sem restaurar, um processo de vida longa que chama
    esta função mais de uma vez (a bateria roda vários cenários no MESMO processo)
    acumulava sufixo — POC -> POC_test -> POC_test_test -> ... — e o banco fantasma
    resultante não tinha `app_users`, derrubando `crash_mid_tool` com "Identidade
    de demonstração não reconhecida" só quando rodava depois de outro cenário LIVE.
    Ler `MONGODB_TEST_DB`/`MONGODB_TEST_BRAIN_DB` (o override explícito) ou o nome
    ORIGINAL da demo (constante, nunca o que está em `os.environ` agora) é o que
    torna a função idempotente independentemente de quantas vezes é chamada.
    """
    main = os.getenv("MONGODB_TEST_DB") or f"{DEMO_MAIN_DB}_test"
    brain = os.getenv("MONGODB_TEST_BRAIN_DB") or f"{DEMO_BRAIN_DB}_test"
    return main, brain


def use_test_databases(*, what: str) -> tuple[str, str]:
    """Aponta o PROCESSO para os bancos de teste, antes de `db` ser importado.

    `db.DB_MAIN`/`DB_BRAIN` são lidos do ambiente no import; por isso isto escreve
    em `os.environ` e tem que ser chamado antes de qualquer `import db` (os
    scripts chamam na primeira linha do `main`).
    """
    main, brain = test_database_names()
    guard(main, brain, what=what)
    os.environ["MONGODB_DB"], os.environ["MONGODB_BRAIN_DB"] = main, brain
    print(f"[isolamento] {what}: banco={main} cérebro={brain}")
    return main, brain


def guard(main_db: str, brain_db: str, *, what: str) -> None:
    """Recusa rodar contra o banco da demo."""
    hits = [name for name, value in (("MONGODB_DB", main_db), ("MONGODB_BRAIN_DB", brain_db))
            if value in (DEMO_MAIN_DB, DEMO_BRAIN_DB)]
    if not hits:
        return
    if allow_demo_write():
        print(f"[isolamento] AVISO: {what} vai escrever no banco da DEMO "
              f"({main_db}/{brain_db}) — ALLOW_DEMO_DB_WRITE=1 foi passado.")
        return
    raise DemoDatabaseRefused(
        f"\n[isolamento] RECUSADO: {what} escreveria no banco da demo "
        f"({', '.join(hits)} = {main_db}/{brain_db}).\n"
        f"             Use os bancos de teste ({DEMO_MAIN_DB}_test/{DEMO_BRAIN_DB}_test) — é o\n"
        "             padrão destes scripts — ou passe ALLOW_DEMO_DB_WRITE=1 se for MESMO\n"
        "             para escrever na demo.\n")


def ensure_seeded(*, wait_indexes: bool = True) -> list[str]:
    """Semeia o banco de teste (idempotente) e copia a configuração medida da demo."""
    from pymongo import MongoClient

    uri = os.environ["MONGODB_URI"]
    client = MongoClient(uri, serverSelectionTimeoutMS=15_000)
    main_db, brain_db = os.environ["MONGODB_DB"], os.environ["MONGODB_BRAIN_DB"]
    messages: list[str] = []

    if client[main_db]["app_users"].count_documents({}) == 0:
        import seed

        seed.main()   # já lê MONGODB_DB/MONGODB_BRAIN_DB via db.DB_MAIN/DB_BRAIN
        messages.append(f"seed executado em {main_db}/{brain_db}")
    else:
        messages.append(f"seed: {main_db}.app_users já populado")

    if brain_db != DEMO_BRAIN_DB:
        source, target = client[DEMO_BRAIN_DB], client[brain_db]
        for name in BRAIN_CONFIG_COLLECTIONS:
            documents = list(source[name].find({}))      # leitura, nunca escrita na demo
            if not documents:
                continue
            target[name].delete_many({})
            target[name].insert_many(documents)
            messages.append(f"config medida copiada: {name} ({len(documents)} doc)")
        for name in BRAIN_PROBE_COLLECTIONS:
            # Probes são imutáveis na prática: copia só o que falta, senão o índice
            # autoEmbed reindexa tudo a cada execução.
            existing = {doc.get("phrase") for doc in target[name].find({}, {"phrase": 1})}
            documents = [doc for doc in source[name].find({}) if doc.get("phrase") not in existing]
            if documents:
                target[name].insert_many(documents)
            messages.append(f"probes copiados: {name} (+{len(documents)}, "
                            f"total {len(existing) + len(documents)})")

    if wait_indexes:
        messages.append(f"índices Search/Vector: {_indexes_ready(client, main_db)}")
    client.close()
    return messages


def _indexes_ready(client, main_db: str, timeout_s: float = 900.0) -> str:
    """Espera os índices Search/Vector do banco de teste ficarem READY."""
    import time

    deadline = time.monotonic() + timeout_s
    pending = list(INDEXED_COLLECTIONS)
    while pending and time.monotonic() < deadline:
        still = []
        for name in pending:
            try:
                rows = list(client[main_db][name].list_search_indexes())
            except Exception:  # noqa: BLE001 — collection ainda não existe
                rows = []
            if not rows or any(row.get("status") != "READY" for row in rows):
                still.append(name)
        if not still:
            return "todos READY"
        pending = still
        time.sleep(10)
    return f"ainda construindo: {', '.join(pending)}" if pending else "todos READY"


if __name__ == "__main__":
    use_test_databases(what="provisionamento do banco de teste")
    for line in ensure_seeded():
        print(f"[isolamento] {line}")
