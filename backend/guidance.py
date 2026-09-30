"""Orientação ancorada em dado real — o antídoto do beco sem saída.

Aqui a resiliência não é "responder mais bonito quando falha": é fazer com que o
próprio RESULTADO DA FERRAMENTA carregue o caminho de volta. O modelo é bom em
redigir; ele é péssimo em adivinhar qual pedido existe. Então quando uma busca
volta vazia, o app anexa ao resultado os pedidos que aquele usuário realmente tem
— consultados com o mesmo filtro de dono das queries normais.

Duas consequências, e as duas importam numa banca:
  · o modelo nunca precisa inventar um número de pedido para "ajudar";
  · a resposta que o cliente lê continua sendo 100% derivada de documento.
"""
from __future__ import annotations

import logging

from db import MAX_TIME_MS, poc

logger = logging.getLogger("agent.guidance")

# Campos seguros para orientação: mesma projeção não-PII usada pelo agente.
ORDER_HINT_FIELDS = {"_id": 0, "order_id": 1, "product_name": 1, "status": 1}
MAX_HINT_ORDERS = 5

_OUT_OF_SCOPE_PATTERNS = (
    "temperatura", "previsao do tempo", "previsão do tempo", "clima hoje",
    "placar", "resultado do jogo", "receita culinaria", "receita culinária",
    "horoscopo", "horóscopo",
)


def is_obviously_out_of_scope(message: str) -> bool:
    normalized = " ".join((message or "").lower().split())
    return any(pattern in normalized for pattern in _OUT_OF_SCOPE_PATTERNS)


async def user_orders(user_key: str) -> list[dict]:
    """Os pedidos DESTA identidade. Falha vira lista vazia — orientação é auxiliar."""
    try:
        cursor = (
            poc()["support_orders"]
            .find({"owner_user_key": user_key}, ORDER_HINT_FIELDS, max_time_ms=MAX_TIME_MS)
            .limit(MAX_HINT_ORDERS)
        )
        return await cursor.to_list(length=MAX_HINT_ORDERS)
    except Exception:  # noqa: BLE001 — nunca derruba o turno
        logger.warning("orientação: falha ao listar pedidos de %s", user_key, exc_info=True)
        return []


def _format_orders(orders: list[dict]) -> str:
    return "\n".join(
        f"- {item.get('order_id')} — {item.get('product_name', 'produto')} "
        f"(status: {item.get('status', '—')})"
        for item in orders
    )


async def scope_reply(user_key: str) -> str:
    """Useful deterministic redirect that does not depend on MCP or the LLM."""
    orders = await user_orders(user_key)
    base = (
        "Essa solicitação está fora do atendimento desta loja. Posso ajudar com status, "
        "troca ou reembolso de pedidos, busca no catálogo e preferências de atendimento."
    )
    if not orders:
        return base + " Se você tiver um número de pedido, envie-o; para produtos, descreva o que procura."
    return (
        base
        + "\n\nPedidos disponíveis para você:\n"
        + _format_orders(orders)
        + "\n\nDiga qual deles você quer tratar."
    )


async def empty_order_hint(user_key: str, *, requested: str | None = None) -> str:
    """Anexo para uma busca de pedido que voltou vazia.

    Vai como parte do tool_result, então o modelo redige em cima disso em vez de
    concluir sozinho que "o pedido não existe" e encerrar a conversa.
    """
    orders = await user_orders(user_key)
    citado = f" O pedido {requested} não pertence a esta identidade." if requested else ""
    if not orders:
        return (
            "\n\n[orientação da plataforma]"
            f"{citado} Esta identidade não possui nenhum pedido registrado. "
            "Diga isso ao cliente com clareza e ofereça ajuda com o catálogo de produtos "
            "ou com o registro de uma preferência de atendimento. Não invente números de pedido."
        )
    return (
        "\n\n[orientação da plataforma]"
        f"{citado} Estes são os pedidos reais desta identidade — ofereça-os ao cliente, "
        f"citando número e produto, e pergunte qual ele quer tratar:\n{_format_orders(orders)}"
    )


# Negações cuja causa é "a consulta era ampla demais". Nesses casos o anexo NÃO pode
# listar os pedidos: entregar a lista completa logo depois de negar uma varredura
# desfaz na prática a política que acabou de ser aplicada — e alguém assistindo a demo
# repara. Listar só faz sentido quando o agente mirou um pedido específico e errou o id.
_NEGACOES_DE_ESCOPO = ("filtro", "amplo", "massa", "específico", "especifico")


def _negacao_por_escopo(denial: str) -> bool:
    baixo = (denial or "").lower()
    return any(termo in baixo for termo in _NEGACOES_DE_ESCOPO)


async def denial_hint(user_key: str, denial: str) -> str:
    """Anexo para uma chamada barrada pela política de reescrita.

    A negação continua sendo negação — o texto dela não muda. O que o anexo faz é
    dizer ao modelo o que ele PODE fazer, para que a conversa com o cliente não
    termine num erro técnico que ele não entende nem consegue contornar.
    """
    orders = [] if _negacao_por_escopo(denial) else await user_orders(user_key)
    disponivel = (
        f"\nPedidos disponíveis para esta identidade:\n{_format_orders(orders)}"
        if orders else ""
    )
    escopo = (
        " O cliente pediu uma consulta ampla: explique que você consulta UM pedido por vez, "
        "pelo número, e peça o número — NÃO liste os pedidos dele nesta resposta."
        if _negacao_por_escopo(denial) else ""
    )
    return (
        f"{denial}\n\n[orientação da plataforma] Essa operação não é permitida ao agente.{escopo} "
        "Não tente de novo pelo mesmo caminho e NÃO diga ao cliente que houve um erro técnico. "
        "Você ainda pode: consultar um pedido específico por order_id, buscar produtos no "
        "catálogo por $vectorSearch, solicitar reembolso/troca/chamado no pedido do próprio "
        f"cliente e registrar preferências de atendimento.{disponivel}"
    )
