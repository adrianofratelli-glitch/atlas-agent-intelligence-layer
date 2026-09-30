"""Teste de contrato: valida que o MongoDB MCP Server pinado ainda expõe as
ferramentas que `agent.ALLOWED_TOOLS` espera, pelo nome exato.

`ALLOWED_TOOLS` é uma constante hardcoded (agent.py) — se uma nova versão do
MCP Server renomear/remover uma ferramenta, nada mais no repo pegaria isso
automaticamente. Este teste conecta de verdade ao processo `npx
mongodb-mcp-server@<MCP_SERVER_VERSION>` (mesmos StdioServerParameters do
supervisor em produção) e compara `session.list_tools()` contra o allowlist.

Requer MONGODB_URI (mesma env var do resto do backend) e rede/npx disponíveis
— em CI sem esses dois, o teste é pulado (não falha o suite inteiro por causa
de infraestrutura ausente), com uma mensagem clara do porquê.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-import-only")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402

CONNECT_TIMEOUT_SECONDS = 30


class McpToolContractTest(unittest.TestCase):
    """Conecta ao MCP Server real (não mockado) — é exatamente o contrato que
    importa: o binário publicado no npm, na versão pinada, com as ferramentas
    que o resto do app assume que existem."""

    def test_allowed_tools_are_exposed_by_the_pinned_mcp_server(self):
        if not os.getenv("MONGODB_URI"):
            self.skipTest("MONGODB_URI não definida — teste de contrato do MCP pulado "
                          "(precisa de um cluster real para subir o servidor).")
        try:
            listed_names = asyncio.run(
                asyncio.wait_for(_list_tool_names(), timeout=CONNECT_TIMEOUT_SECONDS)
            )
        except FileNotFoundError:
            self.skipTest("npx não encontrado — teste de contrato do MCP pulado.")
        except asyncio.TimeoutError as exc:
            self.skipTest(f"MCP Server não respondeu a tempo: {exc}")

        missing = agent.ALLOWED_TOOLS - listed_names
        self.assertFalse(
            missing,
            f"mongodb-mcp-server@{agent.MCP_SERVER_VERSION} não expõe mais as "
            f"ferramentas {missing}, que agent.ALLOWED_TOOLS espera. Atualize "
            "ALLOWED_TOOLS/WRITE_TOOLS/READ_TOOLS (e a reescrita de política em "
            "agent.py) para a nova superfície antes de subir a versão.",
        )


async def _list_tool_names() -> set[str]:
    """Conecta ao processo MCP real via stdio.

    O encerramento do subprocess `npx` corre em paralelo com o reader de
    stdout do SDK `mcp`; é uma condição de corrida benigna e conhecida do
    `anyio` (o processo já saiu quando o reader tenta escrever o último
    frame) que aparece como `anyio.BrokenResourceError` no `__aexit__` do
    context manager — depois que já lemos a resposta com sucesso. Sem essa
    guarda, o erro de teardown mascarava um contrato que na verdade passou
    (o teste virava skip em vez de rodar a asserção real).
    """
    import anyio
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    result: set[str] = set()
    try:
        async with stdio_client(agent.mcp_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                result = {t.name for t in listed.tools}
    except* anyio.BrokenResourceError:
        if not result:
            raise
    return result


if __name__ == "__main__":
    unittest.main()
