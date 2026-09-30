"""Processo-filho dos cenários de crash: morre com `SIGKILL` no meio do turno.

Roda SEMPRE contra o banco de teste (o pai passa `MONGODB_DB`/`MONGODB_BRAIN_DB`
já isolados — ver `scripts/isolation.py`). O pai verifica, de fora, o que sobrou.

    python scripts/crash_child.py <conversation_id> [store|mid_tool]

`store`   — grava o turno pelo caminho real de memória curta e se mata logo depois.
            Pergunta respondida: o que já foi gravado sobrevive ao restart?
`mid_tool`— entra em `run_agent` com uma chamada de ferramenta PENDURADA e espera
            o pai matá-lo no meio dela, sem se matar sozinho. Pergunta respondida:
            um turno interrompido NO MEIO de uma tool deixa estado pela metade?
            (o teto por tool não ajuda aqui: o processo morre antes de ele expirar).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


async def run_store(conversation_id: str) -> None:
    import agent

    metrics = {"reads": 0, "writes": 0, "latency_ms": 0}
    await agent._store_short_term(
        conversation_id, "cliente-demo", "onde está meu pedido PED-1001?",
        "Seu pedido está a caminho.", lambda *a, **k: None, metrics)
    # SIGKILL em si mesmo: nada de finally, nada de flush — é o pior caso real
    # (pod morto pelo orquestrador no meio do turno).
    os.kill(os.getpid(), signal.SIGKILL)


async def run_mid_tool(conversation_id: str) -> None:
    """Turno real travado DENTRO de uma chamada de ferramenta, esperando o SIGKILL."""
    import chaos_suite

    import agent

    async def hang(_name, _args):
        await asyncio.sleep(300)      # o pai mata muito antes

    session = chaos_suite.FakeSession(hang)
    agent.anthropic_client = chaos_suite.FakeLLM()
    agent.resolve_connection_id = lambda _s: _connection_id()
    # Teto por tool alto de propósito: o cenário é o processo morrer ANTES de
    # qualquer mecanismo do app reagir.
    os.environ["TOOL_TIMEOUT_SECONDS"] = "300"
    os.environ["AGENT_TURN_TIMEOUT_SECONDS"] = "300"
    agent.AGENT_TURN_TIMEOUT_SECONDS = 300.0
    print("PRONTO", flush=True)       # sinal para o pai: já estou dentro do turno
    await agent.run_agent(session, scenario=None,
                          message="onde está meu pedido PED-1001?",
                          conversation_id=conversation_id, user_key="cliente-demo")


async def _connection_id() -> str:
    return "preconfigured"


if __name__ == "__main__":
    conversation = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "store"
    asyncio.run(run_store(conversation) if mode == "store" else run_mid_tool(conversation))
