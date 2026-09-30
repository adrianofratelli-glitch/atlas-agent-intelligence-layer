"""Classificador de turno "depende da memória do usuário?" — feito no MongoDB.

Problema: uma pergunta que só faz sentido com a memória DESTE usuário ("como você
me chama?", "qual é o meu limite?") nunca pode ser respondida pelo cache semântico
compartilhado da área, nem gravada nele. O portão de frases de `memory.py` pega os
casos óbvios sem custo; este classificador cobre o resto — paráfrases que nenhuma
lista de frases antecipa.

Como funciona (mesmo padrão do cache e da denylist de guardrail):
  - `ai_brain.turn_probes` guarda frases-exemplo de turnos PESSOAIS; o índice
    `turn_probes_vs` (autoEmbed voyage-4) as vetoriza no Atlas.
  - classify(): `$vectorSearch` com a mensagem; se o vizinho mais próximo passa do
    limiar, o turno é pessoal.
  - O limiar vive em `ai_brain.turn_classifier_config` e é MEDIDO por
    `calibrate_thresholds.py` contra probes rotulados — nunca escolhido na mão.

Só roda quando importa: num HIT de cache (antes de servir a resposta) e antes de
gravar no cache. Falha FECHADO — índice fora do ar ⇒ o turno é tratado como
pessoal (sem cache); custa uma chamada ao LLM, nunca um vazamento.
"""

import time

from db import MAX_TIME_MS, ai_brain, aggregate_list, safe_query

PROBES_COLLECTION = "turn_probes"
CONFIG_COLLECTION = "turn_classifier_config"
PROBES_INDEX = "turn_probes_vs"        # autoEmbed vector index on `phrase`
PROBES_PATH = "phrase"

# Usado só quando ai_brain.turn_classifier_config não existe (seed não rodou).
# Valor medido com calibrate_thresholds.py — ver o documento de config vivo.
DEFAULT_THRESHOLD = 0.7162

# Frases de turnos que dependem da memória do usuário (recall, preferência,
# atualização/esquecimento). Semeadas em ai_brain.turn_probes.
PERSONAL_PROBES = [
    "qual é o meu nome?",
    "você sabe como eu gosto de ser chamado?",
    "o que você lembra a meu respeito?",
    "me diga o que tem salvo no meu cadastro",
    "qual é o meu orçamento máximo?",
    "você guardou as minhas preferências?",
    "como eu prefiro ser contatado?",
    "lembra do que te pedi antes?",
    "qual é o meu limite de gasto?",
    "o que eu já te contei sobre mim?",
    "com qual nome você me trata?",
    "você lembra do meu canal preferido de contato?",
    "já te disse qual é o meu apelido?",
    "que informações minhas você tem guardadas?",
    "considerando o que você sabe de mim, o que me indica?",
    "me sugira algo dentro do meu limite de preço",
    "recomende algo que combine com o meu gosto",
    "com base nas minhas preferências, o que devo comprar?",
    "tenho preferência por contato no whatsapp",
    "só quero ser avisado por e-mail",
    "mude a forma como você me trata",
    "atualize o meu apelido",
    "esqueça o que eu disse sobre o meu orçamento",
    "anote isso sobre mim para as próximas conversas",
    "guarde essa informação para os próximos atendimentos",
    "como você me chama mesmo?",
    "qual apelido eu te passei?",
    "você tem o meu perfil de compras salvo?",
    "o que você registrou das minhas preferências?",
    "lembra qual era o meu teto de preço?",
    "pode repetir o que eu pedi para você anotar?",
    "a partir de agora fale comigo por whatsapp",
    "prefiro ser atendido por telefone",
    "não me ligue, só mensagem",
    "me trate pelo primeiro nome",
    "não quero receber promoções",
    "me avise quando o preço baixar",
    "ajuste as recomendações ao meu perfil",
    "mostre o que você sabe do meu histórico",
    "apague o que você guardou sobre mim",
    "me lembra o que a gente combinou antes",
    "qual valor a gente tinha combinado?",
    "o que ficou combinado entre nós sobre o meu orçamento?",
    "o que ficou registrado sobre as minhas preferências?",
]


async def get_threshold() -> float:
    doc = None
    try:
        doc = await ai_brain()[CONFIG_COLLECTION].find_one(
            {"active": True}, max_time_ms=MAX_TIME_MS)
    except Exception:  # noqa: BLE001 — config ausente nunca derruba o turno
        pass
    return float((doc or {}).get("threshold", DEFAULT_THRESHOLD))


async def classify(text: str) -> dict:
    """Return {personal, score, threshold, latency_ms, error}.

    `error` é True quando o índice/consulta falhou — quem chama trata como
    pessoal (fail-closed)."""
    started = time.perf_counter()
    threshold = await get_threshold()
    try:
        docs = await safe_query(aggregate_list(
            ai_brain()[PROBES_COLLECTION],
            [
                {"$vectorSearch": {"index": PROBES_INDEX, "path": PROBES_PATH,
                                   "query": text, "numCandidates": 50, "limit": 1}},
                {"$project": {"phrase": 1, "_id": 0,
                              "score": {"$meta": "vectorSearchScore"}}},
            ],
            length=1, maxTimeMS=MAX_TIME_MS,
        ))
    except Exception:  # noqa: BLE001 — fail-closed
        return {"personal": True, "score": 0.0, "threshold": threshold,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error": True}
    score = float(docs[0]["score"]) if docs else 0.0
    return {"personal": score >= threshold, "score": round(score, 4),
            "threshold": threshold, "nearest": docs[0]["phrase"] if docs else None,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "error": False}
