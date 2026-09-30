"""Semeia SÓ o classificador de turno (probes + índice + config) — idempotente.

Não toca em nenhum outro dado da PoV (diferente de seed.py, que devolve os dados
de demo ao estado inicial). Uso:  python seed_turn_probes.py
Depois rode  python calibrate_thresholds.py --apply  para medir o limiar.
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.operations import SearchIndexModel

import turn_classifier as tc

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv()


def seed_probes_and_config(db) -> None:
    """Upsert idempotente dos probes e do documento de config (sem tocar em mais nada)."""
    now = datetime.now(timezone.utc)
    probes = db[tc.PROBES_COLLECTION]
    new = 0
    for phrase in tc.PERSONAL_PROBES:
        res = probes.update_one(
            {"phrase": phrase},
            {"$setOnInsert": {"phrase": phrase, "label": "personal", "created_at": now}},
            upsert=True)
        new += bool(res.upserted_id)
    print(f"✓ {tc.PROBES_COLLECTION}: {len(tc.PERSONAL_PROBES)} probes ({new} novos)")
    db[tc.CONFIG_COLLECTION].update_one(
        {"active": True},
        {"$setOnInsert": {"active": True, "threshold": tc.DEFAULT_THRESHOLD,
                          "created_at": now}}, upsert=True)
    print(f"✓ {tc.CONFIG_COLLECTION}: config ativa (recalibre com "
          f"calibrate_thresholds.py --apply)")


def main() -> None:
    from seed import _vector_index_definition

    uri = os.getenv("MONGODB_URI")
    if not uri:
        sys.exit("MONGODB_URI não definida.")
    db = MongoClient(uri, serverSelectionTimeoutMS=15_000)["ai_brain"]
    probes = db[tc.PROBES_COLLECTION]
    seed_probes_and_config(db)

    definition = _vector_index_definition(tc.PROBES_PATH, [])
    try:
        probes.create_search_index(SearchIndexModel(
            definition=definition, name=tc.PROBES_INDEX, type="vectorSearch"))
        print(f"✓ índice vetorial '{tc.PROBES_INDEX}' criado (aguarde ficar READY)")
    except Exception as exc:  # noqa: BLE001
        if "already" in str(exc).lower():
            print(f"✓ índice '{tc.PROBES_INDEX}' já existe")
        else:
            print(f"⚠ não criei o índice: {str(exc)[:160]}\n  definição: {definition}")


if __name__ == "__main__":
    main()
