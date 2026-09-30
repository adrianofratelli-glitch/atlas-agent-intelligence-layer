"""Resiliência da fronteira externa do turno: tools (MCP) e degradação do turno.

O que JÁ existia e não é reimplementado aqui: retry com backoff exponencial e
fallback de modelo na chamada ao LLM (`agent._create_with_retry`, SDK Anthropic
direto via `gateway.GatewayClient` — este PoV não usa LangChain, então não há
`with_retry`; o equivalente do `_shared` seria `grove_client.create_message`,
que perderia a contabilidade de custo por tentativa do `gateway.py`).

O que faltava, e está aqui:

* **teto por chamada de tool** — o deadline do turno (`AGENT_TURN_TIMEOUT_SECONDS`,
  120s) é o teto do turno INTEIRO; uma chamada MCP pendurada segurava o turno
  até lá. `TOOL_TIMEOUT_SECONDS` (default 20s) fecha a janela por chamada.
* **circuit breaker por tool** — MCP caindo sem parar não deve custar uma espera
  completa a cada iteração do loop.
* **degradação graciosa do turno** — falha de tool/LLM termina o turno com uma
  resposta explícita e o trace inteiro, nunca com a resposta perdida num erro
  HTTP. É o comportamento PADRÃO, sem flag.

Flags (só para REVERTER ao comportamento antigo, nunca para ligar o novo):

    SINGLEAGENT_LEGACY_500=1   a exceção do loop volta a subir (503 do handler)
    TOOL_TIMEOUT_SECONDS=0     desliga o teto por tool
    TOOL_BREAKER=0             desliga o circuit breaker por tool
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from time import monotonic

import chaos
import observability

TOOL_FAILURE_THRESHOLD = 4
TOOL_OPEN_SECONDS = 30.0
DEFAULT_TOOL_TIMEOUT_SECONDS = 20.0


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def tool_breaker_enabled() -> bool:
    """Padrão: ligado. Só abre depois de 4 falhas CONSECUTIVAS da mesma tool —
    não muda nada em operação normal. `TOOL_BREAKER=0` desliga."""
    return _flag("TOOL_BREAKER", default=True)


def graceful_degradation() -> bool:
    """Padrão: ligada. `SINGLEAGENT_LEGACY_500=1` devolve a falha crua ao handler."""
    return not _flag("SINGLEAGENT_LEGACY_500")


def tool_timeout_seconds() -> float:
    return float(os.getenv("TOOL_TIMEOUT_SECONDS", str(DEFAULT_TOOL_TIMEOUT_SECONDS)))


class ToolOpenCircuit(RuntimeError):
    """Tool curto-circuitada: o chamador degrada em vez de pagar mais uma falha."""


class ToolTimeout(RuntimeError):
    """A tool estourou o teto por chamada; o turno segue sem ela."""


class _ToolCircuit:
    def __init__(self) -> None:
        self.failures = 0
        self.opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if monotonic() - self.opened_at >= TOOL_OPEN_SECONDS:
            self.opened_at = None  # meio-aberto: a próxima chamada decide
            return True
        return False

    def success(self) -> None:
        self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= TOOL_FAILURE_THRESHOLD:
            self.opened_at = monotonic()


_circuits: dict[str, _ToolCircuit] = {}


def reset_circuits() -> None:
    _circuits.clear()


def circuit_state() -> dict[str, dict]:
    return {name: {"failures": circuit.failures, "open": circuit.opened_at is not None}
            for name, circuit in _circuits.items()}


class _NullGuard:
    """Sem tracing, sem breaker e sem caos, a fronteira não custa nada."""

    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc):
        return False


_NULL_GUARD = _NullGuard()


def guarded_tool(name: str, **attributes):
    if not (observability.active() or tool_breaker_enabled() or chaos.enabled()):
        return _NULL_GUARD
    return _guarded_tool(name, **attributes)


@asynccontextmanager
async def _guarded_tool(name: str, **attributes):
    circuit = _circuits.setdefault(name, _ToolCircuit())
    if tool_breaker_enabled() and not circuit.allow():
        raise ToolOpenCircuit(f"tool {name} em circuito aberto")
    with observability.span(f"tool.{name}", **{"tool.name": name, **attributes}):
        try:
            yield
        except Exception:
            circuit.failure()
            raise
        else:
            circuit.success()


async def call_tool(name: str, coro, **attributes):
    """Executa UMA tool com span, ponto de caos, circuit breaker e teto de tempo.

    O ponto de caos entra DENTRO do corpo cronometrado e dentro do try do
    breaker: falha injetada tem que contar como falha da tool e respeitar o
    teto, exatamente como a real.
    """
    limit = tool_timeout_seconds()

    async def body():
        if chaos.enabled():
            await chaos.hook("tool", name=name)
        return await coro

    try:
        async with guarded_tool(name, **attributes):
            if limit > 0:
                try:
                    return await asyncio.wait_for(body(), timeout=limit)
                except asyncio.TimeoutError as exc:
                    raise ToolTimeout(
                        f"tool {name} excedeu {limit:.0f}s e foi interrompida") from exc
            return await body()
    except BaseException:
        # A corrotina pode nunca ter sido aguardada (circuito aberto, caos):
        # sem isto, "coroutine was never awaited" polui o log a cada falha.
        coro.close()
        raise


DEGRADED_TURN_REPLY = (
    "Não consegui concluir esta consulta agora — a camada de dados demorou além do limite "
    "ou respondeu com erro, e eu preferi interromper a ficar tentando indefinidamente. "
    "Nada foi alterado no seu pedido. Pode tentar de novo em instantes ou pedir para falar "
    "com um atendente."
)


def degraded_tool_result(name: str, exc: Exception) -> str:
    """Texto devolvido AO MODELO quando uma tool falha — honesto e sem inventar dado."""
    if isinstance(exc, ToolOpenCircuit):
        return (f"Ferramenta {name} indisponível (circuito aberto após falhas seguidas). "
                "Não há dado para esta consulta; diga isso ao cliente sem supor nenhum valor.")
    if isinstance(exc, ToolTimeout):
        return (f"Ferramenta {name} excedeu o tempo limite e foi interrompida. "
                "Não há dado para esta consulta; diga isso ao cliente sem supor nenhum valor.")
    return (f"Erro na ferramenta {name}: {exc}. Não há dado para esta consulta; "
            "diga isso ao cliente sem supor nenhum valor.")
