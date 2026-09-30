"""Identidade por usuário e personalização por área — tudo em documentos.

POC.app_users            → quem é o usuário: user_key, nome e a ÁREA a que pertence.
ai_brain.area_profiles   → o perfil da área: persona/regras de negócio injetadas no
                           system prompt do agente. Personalizar uma área é um
                           update_one, não um deploy — mesma história do model_config.

A área do usuário decide três isolamentos no pipeline:
  1. Persona / regras de negócio → area_profiles.persona (vai para o system prompt)
  2. Guardrails                  → guardrail_policies com area=<área> (fallback "default")
  3. Cache semântico             → entradas gravadas com a área do turno; um HIT só
                                   vale para a mesma área (ou entradas globais/FAQ)

A memória (curto e longo prazo) já é isolada por user_key: cada usuário só vê a sua.
"""

from db import MAX_TIME_MS, SafeQueryError, ai_brain, poc, safe_query

USERS_COLLECTION = "app_users"          # em POC
PROFILES_COLLECTION = "area_profiles"   # em ai_brain
DEFAULT_AREA = "default"


async def list_users() -> list[dict]:
    """Usuários cadastrados, com o rótulo da área resolvido de area_profiles."""
    cursor = poc()[USERS_COLLECTION].find({}, max_time_ms=MAX_TIME_MS).sort("name", 1)
    users = await safe_query(cursor.to_list(length=50))
    prof_cursor = ai_brain()[PROFILES_COLLECTION].find(
        {}, {"area": 1, "label": 1}, max_time_ms=MAX_TIME_MS
    )
    profiles = await safe_query(prof_cursor.to_list(length=50))
    labels = {p["area"]: p.get("label", p["area"]) for p in profiles}
    for u in users:
        u["_id"] = str(u["_id"])
        area = u.get("area", DEFAULT_AREA)
        u["area_label"] = labels.get(area, area)
    return users


async def get_user(user_key: str) -> dict:
    """O documento do usuário. Usuário fora do cadastro cai na área default."""
    doc = await safe_query(
        poc()[USERS_COLLECTION].find_one({"user_key": user_key}, max_time_ms=MAX_TIME_MS)
    )
    if not doc:
        return {"user_key": user_key, "name": user_key, "area": DEFAULT_AREA}
    doc["_id"] = str(doc["_id"])
    doc.setdefault("area", DEFAULT_AREA)
    return doc


async def require_demo_user(user_key: str) -> dict:
    """Resolve only registered identities in the demo boundary.

    Production replaces this boundary with a JWT/OIDC principal. Accepting an
    arbitrary user_key here would let a caller create a new memory namespace and
    weaken the isolation demonstrated by the POC.
    """
    doc = await safe_query(
        poc()[USERS_COLLECTION].find_one({"user_key": user_key}, max_time_ms=MAX_TIME_MS)
    )
    if not doc:
        raise SafeQueryError("identidade", "Identidade de demonstração não reconhecida.")
    doc["_id"] = str(doc["_id"])
    doc.setdefault("area", DEFAULT_AREA)
    return doc


async def get_area_profile(area: str) -> dict:
    """O perfil ativo da área, com fallback para o perfil default."""
    coll = ai_brain()[PROFILES_COLLECTION]
    doc = None
    if area and area != DEFAULT_AREA:
        doc = await safe_query(
            coll.find_one({"area": area, "active": True}, max_time_ms=MAX_TIME_MS)
        )
    if not doc:
        doc = await safe_query(
            coll.find_one({"area": DEFAULT_AREA, "active": True}, max_time_ms=MAX_TIME_MS)
        )
    return doc or {"area": DEFAULT_AREA, "label": "Padrão", "persona": ""}
