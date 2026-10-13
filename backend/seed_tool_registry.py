"""Semeia SÓ o registro de ferramentas externas + índices da busca web — idempotente.

Não toca em nenhum outro dado da PoV (diferente de seed.py, que devolve os dados de demo
ao estado inicial) e NÃO zera a cota do mês (`ai_brain.web_search_quota`). Uso:
    python seed_tool_registry.py
Decisão em docs/adr/ADR-007.
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

import web_search

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv()

AUDIT_TTL_SECONDS = 30 * 24 * 3600

# Estado de seed do registro. Financeiro fica SEM busca web de propósito: mostra o
# controle por área (e é a área que já falha fechado na denylist).
WEB_SEARCH_TOOL = {
    "_id": web_search.TOOL_NAME,
    "kind": "external",
    "provider": web_search.PROVIDER,
    "enabled": True,
    "disabled_areas": ["financeiro"],
    "monthly_limit": web_search.DEFAULTS["monthly_limit"],
    "max_results": web_search.DEFAULTS["max_results"],
    "cache_ttl_seconds": web_search.DEFAULTS["cache_ttl_seconds"],
    "search_depth": web_search.DEFAULTS["search_depth"],
    "description": ("Busca web (Tavily) para perguntas fora do domínio da loja. "
                    "Conteúdo externo: o turno perde escrita depois de usá-la."),
}


def seed_tool_registry(brain, main) -> None:
    """Upsert do registro + índices de TTL. Recebe os dois bancos (pymongo síncrono)."""
    now = datetime.now(timezone.utc)
    brain["tool_registry"].replace_one(
        {"_id": WEB_SEARCH_TOOL["_id"]}, {**WEB_SEARCH_TOOL, "updated_at": now}, upsert=True)
    print(f"✓ {brain.name}.tool_registry: {WEB_SEARCH_TOOL['_id']} "
          f"(desligada em: {', '.join(WEB_SEARCH_TOOL['disabled_areas'])})")
    # resultado web envelhece: cada entrada carrega expires_at e o banco a remove sozinho
    main["web_search_cache"].create_index("expires_at", name="ttl_expires_at",
                                          expireAfterSeconds=0)
    # auditoria de chamada externa expira em 30 dias, como as outras trilhas da PoV
    main["external_calls"].create_index("at", name="ttl_at_30d",
                                        expireAfterSeconds=AUDIT_TTL_SECONDS)
    main["external_calls"].create_index([("area", 1), ("at", -1)])
    print(f"✓ {main.name}.web_search_cache (TTL expires_at) · "
          f"{main.name}.external_calls (TTL 30 dias, índice area+at)")


def main() -> None:
    from db import DB_BRAIN, DB_MAIN
    from seed import refuse_demo_db

    uri = os.getenv("MONGODB_URI")
    if not uri:
        sys.exit("MONGODB_URI não definida.")
    refuse_demo_db(DB_MAIN, DB_BRAIN)
    client = MongoClient(uri, serverSelectionTimeoutMS=15_000)
    seed_tool_registry(client[DB_BRAIN], client[DB_MAIN])


if __name__ == "__main__":
    main()
