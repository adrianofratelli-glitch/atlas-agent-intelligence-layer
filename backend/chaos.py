"""Injeção de falha controlada — DESLIGADA salvo `CHAOS=1` no ambiente.

Mesma abordagem do PoV multiagente: os pontos de falha ficam no caminho REAL
(chamada ao LLM, fronteira de tool do MCP, resultado da tool), não em mocks
espalhados pelos testes. Sem `CHAOS=1`, cada `hook()` é uma leitura de variável
de ambiente e um `return` — nenhum caminho de demo muda.

Variáveis (lidas a CADA chamada de propósito: a bateria troca o cenário entre
os casos, no mesmo processo):

    CHAOS=1                liga
    CHAOS_SCENARIO         timeout | hang | status | not_primary | search_down | none
    CHAOS_TARGET           substring casada contra "<ponto>:<nome>" ("" = todos)
    CHAOS_PHASE            fase exigida ("" = qualquer): before_first_token,
                           after_provider_response, between_tools
    CHAOS_STATUS           código HTTP simulado do provedor (default 429)
    CHAOS_DELAY            segundos de atraso para timeout/hang (default 30)
    CHAOS_COUNT            dispara só nas N primeiras vezes ("" = sempre)

`malformed` não é cenário de `hook()`: payload corrompido tem que sair de onde
o dado nasce, então é `mangle()`, aplicado no retorno da tool.
"""

from __future__ import annotations

import asyncio
import os

_fired: dict[str, int] = {}


def enabled() -> bool:
    return os.getenv("CHAOS", "").strip().lower() in {"1", "true", "yes", "on"}


def reset() -> None:
    """Zera o contador de disparos (um cenário por teste)."""
    _fired.clear()


def _armed(point: str, name: str, phase: str) -> bool:
    if not enabled():
        return False
    target = os.getenv("CHAOS_TARGET", "").strip()
    if target and target not in f"{point}:{name}":
        return False
    wanted_phase = os.getenv("CHAOS_PHASE", "").strip()
    if wanted_phase and wanted_phase != phase:
        return False
    limit = os.getenv("CHAOS_COUNT", "").strip()
    key = f"{point}:{name}:{phase}"
    count = _fired.get(key, 0)
    if limit and count >= int(limit):
        return False
    _fired[key] = count + 1
    return True


def not_primary_error():
    """Step-down do primário, com a classe REAL do driver.

    `NotPrimaryError` carrega o código 10107 e o label `RetryableWriteError`: é
    assim que o PyMongo decide reexecutar a operação depois da reeleição. Uma
    exceção caseira testaria o `except` do app; esta testa a semântica do driver
    e o mapeamento de `db.safe_query`.
    """
    from pymongo.errors import NotPrimaryError

    return NotPrimaryError("not primary (step-down simulado)",
                           {"code": 10107, "errmsg": "not primary",
                            "errorLabels": ["RetryableWriteError"]})


def search_unavailable_error():
    """`mongot` fora / índice vetorial ausente, como o servidor devolve de fato.

    `OperationFailure` com a mensagem do PlanExecutor é o que `db.safe_query`
    mapeia para a `SafeQueryError` de kind "search" — que por sua vez decide o
    fallback do cache e o fail-open/fail-closed do guardrail por área.
    """
    from pymongo.errors import OperationFailure

    return OperationFailure(
        "PlanExecutor error during aggregation :: caused by :: "
        "$vectorSearch index not found (mongot indisponível)", code=8)


class ChaosProviderError(Exception):
    """Erro de provedor simulado; carrega `status_code` como o SDK real carrega."""

    def __init__(self, status_code: int):
        super().__init__(f"chaos: provedor simulado devolveu {status_code}")
        self.status_code = status_code


async def hook(point: str, *, name: str = "", phase: str = "") -> None:
    """Ponto de injeção. Fora de CHAOS=1 não faz nada."""
    scenario = os.getenv("CHAOS_SCENARIO", "").strip().lower()
    if scenario in ("", "none") or not _armed(point, name, phase):
        return
    if scenario in ("timeout", "hang"):
        await asyncio.sleep(float(os.getenv("CHAOS_DELAY", "30")))
        return
    if scenario == "status":
        raise ChaosProviderError(int(os.getenv("CHAOS_STATUS", "429")))
    if scenario == "not_primary":
        raise not_primary_error()
    if scenario == "search_down":
        raise search_unavailable_error()


def mangle(point: str, name: str, value):
    """Corrompe o retorno de uma tool quando o cenário `malformed` está armado.

    Devolve o valor intacto em qualquer outra situação. O teste do payload
    malformado precisa do dado nascendo torto (texto que não é JSON, documento
    vazio), não de uma exceção — exceção o breaker já cobre.
    """
    if os.getenv("CHAOS_SCENARIO", "").strip().lower() != "malformed":
        return value
    if not _armed(point, name, ""):
        return value
    return os.getenv("CHAOS_MALFORMED_PAYLOAD", "{{{ não é json ]]]")
