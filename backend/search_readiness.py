"""Lista vazia de `$vectorSearch`/`$search` não prova que a camada está de pé.

Medido em 2026-11-12 neste cluster: um `$vectorSearch` contra um índice que não
existe (ou que ainda está em construção pela primeira vez) devolve **zero
documentos, sem erro**. Todo `except` que decide "camada indisponível" nunca
dispara nesse caso, e o chamador lê o vazio como "nada parecido":

  * denylist semântica: "sem match" → a política `semantic_fail_mode: closed`
    da área era ignorada e a mensagem passava;
  * classificador de turno: score 0 → "não pessoal" (o contrário do fail-closed
    declarado), turno pessoal elegível ao cache compartilhado;
  * memória longa: uma perna muda dentro do `$rankFusion` aparecia como fusão;
  * cache semântico: MISS indistinguível de índice fora.

A regra (ADR-013): quando a busca volta vazia, quem decide se o vazio é
verdadeiro é `list_search_indexes` — `queryable: true` (ou `status: READY` nas
versões que não trazem o campo). O resultado fica em cache curto: pronto por
60 s, não pronto por 5 s (para perceber logo que o índice ficou pronto).
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("poc.search_readiness")

READY_TTL_S = 60.0
NOT_READY_TTL_S = 5.0

# (db, collection, índice) -> (expira_em, estado)
_cache: dict[tuple[str, str, str], tuple[float, bool | None]] = {}
_clock = time.monotonic   # relógio próprio: o teste avança sem mexer no event loop


def reset() -> None:
    """Zera o cache (testes e bateria de caos)."""
    _cache.clear()


def _key(coll, index: str) -> tuple[str, str, str]:
    db_name = getattr(getattr(coll, "database", None), "name", "?")
    return (str(db_name), str(getattr(coll, "name", "?")), index)


def status_is_queryable(row: dict | None) -> bool:
    """Linha de `list_search_indexes` → consultável? Função pura (testável)."""
    if not row:
        return False
    if "queryable" in row:
        return bool(row.get("queryable"))
    return row.get("status") == "READY"


async def _list(coll, index: str) -> list[dict]:
    import db as _db

    if _db._chaos_enabled():
        import chaos

        forced = chaos.index_readiness(f"{getattr(coll, 'name', '?')}:{index}")
        if forced is not None:
            return [{"name": index, "status": "READY" if forced else "BUILDING",
                     "queryable": forced}]
    cursor = await coll.list_search_indexes(index)
    return await cursor.to_list(length=None)


async def queryable(coll, index: str) -> bool | None:
    """True/False pelo `list_search_indexes`; None se nem isso respondeu.

    None (sem permissão, rede) fica para o chamador decidir: a denylist e o
    classificador tratam como indisponível (falham fechado), a memória e o cache
    seguem como antes.
    """
    key = _key(coll, index)
    now = _clock()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        rows = await _list(coll, index)
        state: bool | None = status_is_queryable(next(
            (r for r in rows if r.get("name") == index), None))
    except Exception as exc:  # noqa: BLE001 — sem resposta ≠ pronto
        logger.warning("list_search_indexes falhou para %s.%s/%s: %s",
                       key[0], key[1], index, exc)
        state = None
    ttl = READY_TTL_S if state else NOT_READY_TTL_S
    _cache[key] = (now + ttl, state)
    if state is False:
        logger.warning("índice %s em %s.%s não está consultável (ausente ou em construção): "
                       "lista vazia não vale como 'sem resultado'", index, key[0], key[1])
    return state


async def empty_is_trustworthy(coll, index: str) -> bool:
    """Busca voltou vazia: o vazio é real só se o índice está consultável."""
    return (await queryable(coll, index)) is True
