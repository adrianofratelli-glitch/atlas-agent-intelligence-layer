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
    turno (prompt da área, contexto, resultados de ferramenta, histórico);
  * `pedido_sem_fonte` (2026-11-13): a resposta cita um `PED-…` que não está em
    nada que o modelo viu no turno (pedido inventado).

Não bloqueiam a resposta: viram evento no trace, `guardrail.output.answer_checks`
no resultado, auditoria em `guardrail_events` e reprovação no eval. Heurística
assumida: casa frases, não entende o texto; falso negativo é possível, e o eval
com juiz de outra família (`eval_agent.py --judge`) é a segunda opinião.

Desde 2026-11-13 (ADR-014) a promessa sem escrita também é CORRIGIDA antes de
chegar ao cliente: `rewrite_unbacked` troca, na frase apontada, a ação afirmada
("Vou encaminhar seu caso…", "Já abri um chamado…") pela oferta ("Se você quiser,
posso encaminhar seu caso…"). Motivo: a regra 4b do prompt reduziu a frase mas não
a eliminou (2 de 3 respostas do `single-005` ao vivo em 2026-11-12), e uma promessa
falsa entregue ao cliente é pior do que um aviso no trace. O texto original fica no
trace e na auditoria; `ANSWER_REWRITE=0` volta a só apontar. O prazo sem fonte não
é reescrito: não há frase segura para pôr no lugar de um número inventado.
"""

from __future__ import annotations

import os
import re
import unicodedata


def rewrite_enabled() -> bool:
    """Ligada por padrão; `ANSWER_REWRITE=0` volta a só apontar (flag de reversão)."""
    return os.getenv("ANSWER_REWRITE", "1").strip().lower() not in {"0", "false", "no", "off"}

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


# "não abri chamado", "nunca registrei" são relato honesto, não promessa.
_NEGATION_BEFORE_RE = re.compile(r"\b(?:nao|nunca|ainda nao)\s+(?:eu\s+)?$")


def _claims(folded: str):
    for m in _ACTION_RE.finditer(folded):
        if _NEGATION_BEFORE_RE.search(folded[max(0, m.start() - 16):m.start()]):
            continue
        yield m


def unbacked_commitments(answer: str, *, writes_ok: int) -> list[dict]:
    if writes_ok > 0:
        return []
    out = []
    for m in _claims(fold(answer)):
        out.append({"rule": "promessa_sem_escrita", "kind": "fidelidade",
                    "detail": m.group(0)[:120]})
        break   # uma basta para reprovar; o trace não precisa da lista inteira
    return out


# Verbo afirmado (forma dobrada, como em _ACTION_VERBS) -> infinitivo da oferta.
_INFINITIVE = {
    "vou abrir": "abrir", "abri": "abrir", "abrirei": "abrir", "ja abri": "abrir",
    "acabei de abrir": "abrir",
    "vou registrar": "registrar", "registrei": "registrar", "ja registrei": "registrar",
    "vou solicitar": "solicitar", "solicitei": "solicitar", "ja solicitei": "solicitar",
    "vou encaminhar": "encaminhar", "encaminhei": "encaminhar", "ja encaminhei": "encaminhar",
    "vou escalar": "escalar", "escalei": "escalar",
    "vou processar": "processar", "processei": "processar",
    "vou atualizar": "atualizar", "atualizei": "atualizar",
}
# Frase = trecho até . ! ? ou quebra de linha (o delimitador fica com a frase).
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]*|\n+")
# Início de frase depois de marcação de lista/ênfase do markdown.
_LEADING_MARKUP_RE = re.compile(r"^[\s*_>#\-•\d.)]*$")


def _verb_pattern(folded_verb: str) -> re.Pattern:
    """Verbo dobrado -> regex no texto ORIGINAL (acento e caixa livres; "eu" opcional)."""
    words = [("j[aá]" if w == "ja" else re.escape(w)) for w in folded_verb.split()]
    return re.compile(r"\b(?:eu\s+)?" + r"\s+".join(words) + r"\b", re.IGNORECASE)


def _rewrite_sentence(sentence: str, *, again: bool = False) -> tuple[str, str | None]:
    """Reescreve a 1ª ação afirmada da frase como oferta. (frase, verbo) ou (frase, None).

    `again`: a frase já ganhou um "se você quiser"; a 2ª ação vira só "posso <verbo>"."""
    for m in _claims(fold(sentence)):
        verb = m.group("verb")
        hit = _verb_pattern(verb).search(sentence)
        if not hit:     # dobra mudou o texto de um jeito que o padrão não acha: não arrisca
            return sentence, None
        at_start = bool(_LEADING_MARKUP_RE.match(sentence[:hit.start()]))
        offer = (f"posso {_INFINITIVE[verb]}" if again
                 else f"{'Se' if at_start else 'se'} você quiser, posso {_INFINITIVE[verb]}")
        return sentence[:hit.start()] + offer + sentence[hit.end():], verb
    return sentence, None


def rewrite_unbacked(answer: str, *, writes_ok: int) -> tuple[str, list[dict]]:
    """Troca cada ação afirmada sem escrita por uma oferta. Devolve (texto, trocas).

    Determinístico e local à frase: o resto da resposta (status, contagem de
    reposições, datas) fica intacto. Com escrita bem-sucedida no turno não mexe em
    nada, igual a `unbacked_commitments`.
    """
    if writes_ok > 0 or not answer:
        return answer, []
    out, changes = [], []
    for part in _SENTENCE_RE.findall(answer):
        # uma frase pode ter duas promessas ("vou abrir… e vou registrar…"): repete
        for attempt in range(3):
            new, verb = _rewrite_sentence(part, again=attempt > 0)
            if verb is None:
                break
            changes.append({"rule": "promessa_sem_escrita", "kind": "fidelidade",
                            "action": "rewrite", "before": part.strip()[:200],
                            "after": new.strip()[:200]})
            part = new
        out.append(part)
    return "".join(out), changes


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


_ORDER_ID_RE = re.compile(r"\bPED-\d{3,6}\b", re.IGNORECASE)


def ungrounded_orders(answer: str, *, grounding: str) -> list[dict]:
    """Número de pedido citado na resposta que não veio de nada que o modelo viu.

    Achado da rodada de base de 2026-11-13 (`single-009`): numa pergunta fora de
    escopo, sem nenhuma ferramenta no turno, o agente listou "PED-2001 – Fone
    Bluetooth Premium, PED-2002 – Mouse Ergonômico…", pedidos e produtos que o
    cliente não tem. Os pedidos reais chegam ao modelo pela mensagem, pelo histórico
    ou pelos resultados de ferramenta (inclusive a lista anexada a uma negação);
    fora disso, o número foi inventado."""
    source = {m.upper() for m in _ORDER_ID_RE.findall(grounding or "")}
    out, seen = [], set()
    for m in _ORDER_ID_RE.findall(answer):
        oid = m.upper()
        if oid in source or oid in seen:
            continue
        seen.add(oid)
        out.append({"rule": "pedido_sem_fonte", "kind": "fidelidade", "detail": oid})
    return out


def check(answer: str, *, writes_ok: int, grounding: str) -> list[dict]:
    """Todas as checagens. Lista vazia = nada a apontar."""
    if not answer or not answer.strip():
        return []
    return (unbacked_commitments(answer, writes_ok=writes_ok)
            + ungrounded_deadlines(answer, grounding=grounding)
            + ungrounded_orders(answer, grounding=grounding))
