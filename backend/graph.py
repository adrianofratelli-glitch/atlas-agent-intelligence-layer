"""Travessia da cadeia de trocas em `POC.support_orders`, via $graphLookup.

Um pedido trocado gera um pedido de reposição, ligado ao anterior por `replacement_order_id`.
Nenhum documento sozinho responde "este item já foi reposto quantas vezes?" — é preciso seguir
a cadeia, e o número de saltos não é conhecido de antemão. `$graphLookup` faz esse loop dentro
do servidor, em uma agregação, em vez de N idas ao banco a partir da aplicação.

O sinal: reposição repetida do MESMO produto é defeito de lote, não azar do cliente. Trocar
pela quarta vez reproduz o problema — o agente precisa saber disso ANTES de prometer a troca.

Como todo o resto da superfície de ferramentas deste agente, o pipeline é montado inteiro pelo
servidor a partir de um único order_id escalar. O modelo nunca escreve um `$graphLookup`.
"""

GRAPH_MAX_DEPTH = 6
RECURRENCE_THRESHOLD = 3
GRAPH_PROJECT_FIELDS = ("order_id", "product_name", "sku", "status")


def build_order_chain_pipeline(order_id: str, owner_user_key: str) -> list[dict]:
    """Pipeline canônico da cadeia. `restrictSearchWithMatch` repete o filtro de dono a cada
    salto: nem um pedido de outro usuário é alcançável por um campo mal preenchido."""
    return [
        {"$match": {"order_id": order_id, "owner_user_key": owner_user_key}},
        {"$graphLookup": {
            "from": "support_orders",
            "startWith": "$replacement_order_id",
            "connectFromField": "replacement_order_id",
            "connectToField": "order_id",
            "as": "chain",
            "maxDepth": GRAPH_MAX_DEPTH,
            "depthField": "depth",
            "restrictSearchWithMatch": {"owner_user_key": owner_user_key},
        }},
        {"$project": {
            "_id": 0, "order_id": 1, "product_name": 1, "sku": 1, "status": 1,
            "chain": {"$map": {
                "input": {"$sortArray": {"input": "$chain", "sortBy": {"depth": 1}}},
                "as": "link",
                # Projeção explícita também dentro do array: sem isso, customer_name e os
                # demais campos de identidade voltariam nos elos da cadeia.
                "in": {"order_id": "$$link.order_id", "product_name": "$$link.product_name",
                       "sku": "$$link.sku", "status": "$$link.status", "depth": "$$link.depth",
                       "reason": "$$link.replacement_reason"},
            }},
        }},
    ]


def summarize_order_chain(document: dict | None) -> dict:
    """Sinais de negócio a partir da cadeia crua. Aritmética sobre o array, sem LLM."""
    document = document or {}
    chain = document.get("chain") or []
    root_sku = document.get("sku")
    skus = [root_sku] + [link.get("sku") for link in chain]
    same_sku = [item for item in skus if item and item == root_sku]
    recurring = len(same_sku) >= RECURRENCE_THRESHOLD
    return {
        "order_id": document.get("order_id"),
        "product_name": document.get("product_name"),
        "sku": root_sku,
        "replacements": len(chain),
        "same_sku_count": len(same_sku),
        "recurring_defect": recurring,
        "path": ([document["order_id"]] if document.get("order_id") else [])
                + [link.get("order_id") for link in chain],
        "reasons": [link.get("reason") for link in chain if link.get("reason")],
        # Quarta reposição do mesmo SKU não é atendimento, é qualidade: o agente deve
        # dizer isso em vez de abrir mais uma troca automática.
        "needs_quality_review": recurring,
    }
