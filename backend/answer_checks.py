"""Checagens determinísticas da RESPOSTA do agente (não da rota).

Achado ao vivo em 2026-11-12 (turnos reais via Grove, banco `POC_test`): para
"Onde está o meu pedido PED-1003?" o agente leu o pedido e respondeu "Vou abrir um
chamado urgente… retorno em até 24 horas úteis" — nenhuma escrita aconteceu no
turno e o prazo não existe em política nem em dado. O eval aprovava (resposta
não vazia, sem degradação). Duas checagens, ambas funções puras:

  * `promessa_sem_escrita`: a resposta afirma em primeira pessoa uma ação de
    pedido (abrir chamado, registrar troca/reembolso, encaminhar/escalar) e o
    turno não teve nenhuma escrita bem-sucedida em `support_orders`;
  * `prazo_sem_fonte`: a resposta promete um prazo ("em até 24 horas", "em 3
    dias úteis") cujo número e unidade não aparecem em nada que o modelo viu no
    turno (prompt da área, contexto, resultados de ferramenta, histórico).

Não bloqueiam a resposta: viram evento no trace, `guardrail.output.answer_checks`
no resultado, auditoria em `guardrail_events` e reprovação no eval. Heurística
assumida: casa frases, não entende o texto; falso negativo é possível, e o eval
com juiz de outra família (`eval_agent.py --judge`) é a segunda opinião.
"""

from __future__ import annotations

import re
import unicodedata

# Verbos de ação em 1ª pessoa (passado/futuro) + objeto de pedido por perto.
_ACTION_VERBS = (
    r"vou abrir|abri|abrirei|ja abri|acabei de abrir|"
    r"vou registrar|registrei|ja registrei|"
    r"vou solicitar|solicitei|ja solicitei|"
    r"vou encaminhar|encaminhei|ja encaminhei|"
    r"vou escalar|escalei|"
    r"vou processar|processei|"
    r"vou atualizar|atualizei"
)
_ACTION_OBJECTS = r"chamado|reembolso|estorno|troca|solicitacao|protocolo|caso|analise|status|pedido"
_ACTION_RE = re.compile(
    rf"\b(?P<verb>{_ACTION_VERBS})\b(?P<gap>[^.!?\n]{{0,60}}?)\b(?P<obj>{_ACTION_OBJECTS})\b")

# Prazo prometido: preposição de prazo + número + unidade.
_DEADLINE_RE = re.compile(
    r"\b(?:em ate|ate|em|dentro de|no prazo de|prazo de|nas proximas|nos proximos)\s+"
    r"(?P<n>\d{1,3})\s*(?P<unit>h\b|horas?|dias?|semanas?)")


def fold(text: str) -> str:
    """minúsculas, sem acento, espaços colapsados (comparação de frases)."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    return " ".join(plain.split())


def _unit_stem(unit: str) -> str:
    return {"h": "hora", "horas": "hora", "hora": "hora", "dias": "dia", "dia": "dia",
            "semanas": "semana", "semana": "semana"}.get(unit, unit)


def _grounded(n: str, unit: str, grounding: str) -> bool:
    stem = _unit_stem(unit)
    patterns = [rf"\b{n}\s*{stem}"]
    if stem == "hora":
        patterns.append(rf"\b{n}\s*h\b")
    return any(re.search(p, grounding) for p in patterns)


def unbacked_commitments(answer: str, *, writes_ok: int) -> list[dict]:
    if writes_ok > 0:
        return []
    out = []
    for m in _ACTION_RE.finditer(fold(answer)):
        out.append({"rule": "promessa_sem_escrita", "kind": "fidelidade",
                    "detail": m.group(0)[:120]})
        break   # uma basta para reprovar; o trace não precisa da lista inteira
    return out


def ungrounded_deadlines(answer: str, *, grounding: str) -> list[dict]:
    source = fold(grounding)
    out, seen = [], set()
    for m in _DEADLINE_RE.finditer(fold(answer)):
        key = (m.group("n"), _unit_stem(m.group("unit")))
        if key in seen or _grounded(m.group("n"), m.group("unit"), source):
            continue
        seen.add(key)
        out.append({"rule": "prazo_sem_fonte", "kind": "fidelidade",
                    "detail": m.group(0)[:120]})
    return out


def check(answer: str, *, writes_ok: int, grounding: str) -> list[dict]:
    """Todas as checagens. Lista vazia = nada a apontar."""
    if not answer or not answer.strip():
        return []
    return (unbacked_commitments(answer, writes_ok=writes_ok)
            + ungrounded_deadlines(answer, grounding=grounding))
