"""Guardrails whose POLICY and AUDIT layers live in MongoDB.

Honest framing for the client: MongoDB is not a toxicity/PII classifier. What is
a genuine MongoDB story — and matches the rest of this POC ("the AI layer lives
in documents") — is:

  1. Policy as a document   → ai_brain.guardrail_policies
     Regex patterns for PII (CPF, cartão), banned terms, and the semantic
     denylist threshold all live in ONE editable document. Tightening a rule is
     an update_one, not a redeploy — the same live-config story as model_config.
     `semantic_fail_mode` também é política: "open" (indisponibilidade do índice
     não bloqueia — default) ou "closed" (área crítica bloqueia se a camada
     semântica cair — ex.: Financeiro).

  2. Semantic denylist      → POC.guardrail_denylist  (Atlas Vector Search)
     Prohibited example utterances are stored WITH embeddings (autoEmbed). An
     incoming message is $vectorSearch-ed against them: if it's semantically
     close to a forbidden intent (leak another customer's data, prompt-injection,
     guaranteed-return advice), it's blocked — even if it's phrased differently.

  3. Audit log              → POC.guardrail_events
     Every check (allowed or blocked, input and output) is appended, queryable
     during the PoV to show governance/compliance evidence. Sempre com o texto
     JÁ MASCARADO — o log de governança nunca é ele próprio um vazamento.

Enforcement itself (running the regex, comparing the score) is app logic; Mongo
is the policy store, the semantic matcher, and the system of record.
"""

import logging
import os
import re
from datetime import datetime, timezone

from db import MAX_TIME_MS, aggregate_list, ai_brain, poc, safe_query

logger = logging.getLogger("poc.guardrails")

POLICY_COLLECTION = "guardrail_policies"      # in ai_brain
DENYLIST_COLLECTION = "guardrail_denylist"    # in POC (vector search)
EVENTS_COLLECTION = "guardrail_events"        # in POC (audit log)
CANDIDATES_COLLECTION = "guardrail_candidates"  # in POC (near-miss review queue)
DENYLIST_INDEX = "guardrail_denylist_vs"
DENYLIST_PATH = "phrase"
NEAR_MISS_MARGIN = 0.05                       # score dentro de [threshold-margem, threshold) vira candidato


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _injection_heuristic_enabled() -> bool:
    """Ligada por padrão. `GUARDRAIL_INJECTION_HEURISTIC=0` volta ao comportamento
    anterior (só denylist vetorial) — a flag existe para REVERTER, não para ativar."""
    return os.getenv("GUARDRAIL_INJECTION_HEURISTIC", "1").strip().lower() not in {"0", "false", "no", "off"}


def _deterministic_injection(text: str) -> str | None:
    """Retorna o padrão que casou, ou None. Falha ABERTA: se o helper comum não
    estiver instalado, a denylist vetorial segue sendo a camada que decide."""
    try:
        import guardrails as shared          # pov-shared (núcleo); NUNCA este módulo
    except ImportError:
        return None
    try:
        result = shared.check_injection(text, use_llm=False)
    except Exception:  # noqa: BLE001 — camada extra nunca derruba o turno
        logger.warning("heurística de injeção indisponível; seguindo só com a denylist")
        return None
    if result.ok:
        return None
    findings = getattr(result, "findings", ()) or ()
    return (findings[0][:40] if findings else result.reason)


def _denylist_threshold(policy: dict) -> float | None:
    """Resolve the live threshold, including the pre-migration field name.

    Some already-seeded environments still store the calibrated value as
    ``vector_threshold``. Ignoring it used the old 0.505 fallback, which belongs
    to a previous Atlas score scale and consequently blocked almost every input.
    ``denylist_threshold`` is canonical; the legacy key is read only so a rolling
    deploy stays safe until the calibration migration writes the canonical key.
    """
    raw = policy.get("denylist_threshold")
    if raw is None:
        raw = policy.get("vector_threshold")
    if raw is None:
        logger.critical(
            "política %s sem denylist_threshold", policy.get("_id", "<desconhecida>")
        )
        return None
    try:
        threshold = float(raw)
    except (TypeError, ValueError):
        logger.critical(
            "threshold inválido na política %s: %r",
            policy.get("_id", "<desconhecida>"), raw,
        )
        return None
    if not 0.0 <= threshold <= 1.0:
        logger.critical(
            "threshold fora da faixa na política %s: %r",
            policy.get("_id", "<desconhecida>"), raw,
        )
        return None
    return threshold


async def get_policy(area: str = "default") -> dict:
    """The active guardrail policy for an AREA, falling back to the default one.

    One active policy document per area: tightening the Financeiro rules touches
    only that area's document — the other areas keep their own policy untouched.
    """
    coll = ai_brain()[POLICY_COLLECTION]
    doc = None
    if area and area != "default":
        doc = await safe_query(
            coll.find_one({"active": True, "area": area}, max_time_ms=MAX_TIME_MS)
        )
    if not doc:
        doc = await safe_query(
            coll.find_one(
                {"active": True,
                 "$or": [{"area": "default"}, {"area": {"$exists": False}}]},
                max_time_ms=MAX_TIME_MS,
            )
        )
    return doc or {}


async def _semantic_denylist(
    text: str, threshold: float, area: str
) -> tuple[dict | None, bool, dict | None]:
    """$vectorSearch the message against forbidden example utterances.

    Returns (match | None, available, near_miss | None). `available=False`
    significa que a camada semântica não pôde rodar (índice ausente) — quem
    decide se isso bloqueia é a política da área (`semantic_fail_mode`), não
    este helper. `near_miss` é o melhor candidato quando o score fica LOGO
    ABAIXO do threshold (dentro de NEAR_MISS_MARGIN) — não bloqueia, mas é
    sinal de possível tentativa que o denylist ainda não cobre.

    Entries with area "global" apply everywhere; entries with a specific area only
    there. The scoping is a NATIVE pre-filter: `area` is a filter field in the
    vector index, so the ANN search only traverses applicable entries — the top
    match is always valid, no matter how large the denylist grows.
    """
    def _pipeline(with_filter: bool) -> list[dict]:
        stage = {
            "index": DENYLIST_INDEX,
            "path": DENYLIST_PATH,
            "query": text,
            "numCandidates": 30,
            "limit": 1 if with_filter else 5,
        }
        if with_filter:
            stage["filter"] = {"area": {"$in": ["global", area]}}
        return [
            {"$vectorSearch": stage},
            {"$project": {"phrase": 1, "category": 1, "area": 1,
                          "score": {"$meta": "vectorSearchScore"}}},
        ]

    coll = poc()[DENYLIST_COLLECTION]
    try:
        docs = await aggregate_list(coll, _pipeline(True), length=1, maxTimeMS=MAX_TIME_MS)
    except Exception:  # noqa: BLE001 — filtro não indexado → pós-filtro app-side
        try:
            docs = await aggregate_list(coll, _pipeline(False), length=5, maxTimeMS=MAX_TIME_MS)
            docs = [d for d in docs if d.get("area") in (None, "global", area)]
        except Exception as exc:  # noqa: BLE001 — índice ausente → camada indisponível
            logger.warning("denylist semântico indisponível (área=%s): %s", area, exc)
            return None, False, None
    if not docs:
        return None, True, None
    top_score = round(float(docs[0].get("score", 0)), 4)
    if top_score >= threshold:
        return {"phrase": docs[0].get("phrase"), "category": docs[0].get("category"),
                "score": top_score}, True, None
    if top_score >= threshold - NEAR_MISS_MARGIN:
        return None, True, {
            "closest_phrase": docs[0].get("phrase"), "category": docs[0].get("category"),
            "score": top_score, "threshold": threshold,
        }
    return None, True, None


def _regex_hits(text: str, patterns: list[dict]) -> list[dict]:
    """Return [{name, match}] for every configured regex that fires."""
    hits = []
    for p in patterns:
        try:
            m = re.search(p["pattern"], text)
        except re.error:
            continue
        if m:
            hits.append({"name": p.get("name", "regex"), "match": m.group(0)})
    return hits


def _mask(text: str, patterns: list[dict]) -> tuple[str, list[str]]:
    """Redact every PII pattern in `text`. Returns (masked_text, [rule names])."""
    masked = text
    fired = []
    for p in patterns:
        try:
            new = re.sub(p["pattern"], p.get("mask", "«removido»"), masked)
        except re.error:
            continue
        if new != masked:
            fired.append(p.get("name", "pii"))
            masked = new
    return masked, fired


async def mask_pii(text: str, area: str = "default") -> str:
    """Só o mascaramento de PII da política da área — sem denylist nem auditoria.
    Usado em histórico de conversa já validado turno a turno por `check_input`."""
    policy = await get_policy(area)
    return _mask(text, policy.get("pii_patterns", []))[0]


async def check_input(text: str, user_key: str, session_id: str,
                      area: str = "default") -> dict:
    """Guardrail on the incoming message. Blocks and logs when a rule fires.

    The policy and the denylist scope come from the user's AREA, so each area
    enforces its own rules. Returns {allowed, action, violations, block_message,
    masked_text}. `masked_text` é a mensagem com PII redigida — é ESSA versão que
    segue para o LLM, a memória e o trace (PII não sai do guardrail em claro).
    `action` is "allow" or "block". A blocked message never reaches the LLM.
    """
    policy = await get_policy(area)
    violations: list[dict] = []

    # 0) injeção de instrução, camada DETERMINÍSTICA (pov-shared, sem LLM, sem rede)
    # Complementar — não substituta — da denylist vetorial, e a medição diz por quê:
    # contra as sondas rotuladas de calibrate_thresholds.py, a heurística deu 0 falso
    # positivo em 19 frases legítimas e pegou o caso que o embedding PERDE (frase
    # proibida diluída com uma segunda intenção: score cai de 0,9284 para 0,6799,
    # abaixo de qualquer pergunta legítima — ver docs/eval-report.md, achado 1).
    # Em troca, ela NÃO pega os maliciosos parafraseados ("posso alegar que não
    # recebi…"), que são justamente os que a busca vetorial pega com 0,79–0,86.
    # Uma cobre o buraco da outra; por isso somam, e não se substituem.
    if _injection_heuristic_enabled():
        hit = _deterministic_injection(text)
        if hit:
            violations.append({
                "rule": "injecao_deterministica", "kind": "prompt_injection",
                "detail": f"padrão de injeção de instrução detectado ({hit})",
            })

    # 1) semantic denylist (MongoDB Vector Search), scoped to the area
    threshold = _denylist_threshold(policy)
    if threshold is None:
        match, semantic_available, near_miss = None, False, None
    else:
        match, semantic_available, near_miss = await _semantic_denylist(text, threshold, area)
    if near_miss:
        await _log_candidate(text, near_miss, user_key, session_id, area)
    if match:
        violations.append({
            "rule": "denylist_semantico", "kind": "topico_proibido",
            "detail": f'próximo de "{match["phrase"]}" ({match["category"]})',
            "score": match["score"],
        })
    elif not semantic_available and policy.get("semantic_fail_mode", "open") == "closed":
        # área crítica com fail-closed: sem camada semântica → não passa
        violations.append({
            "rule": "denylist_indisponivel", "kind": "fail_closed",
            "detail": "camada semântica indisponível e a política da área é fail-closed",
        })

    # 2) banned terms (regex from the policy document)
    for hit in _regex_hits(text, policy.get("banned_terms", [])):
        violations.append({"rule": "termo_proibido", "kind": "conteudo",
                           "detail": hit["match"]})

    # 3) PII na entrada: mascarada ANTES de seguir adiante (LLM, memória, trace).
    # O valor detectado NUNCA sai em claro: nem na violation, nem no audit log.
    masked_text, pii_fired = _mask(text, policy.get("pii_patterns", []))
    pii_flags = [{"rule": "pii_entrada", "kind": name,
                  "detail": f"{name} detectado (valor mascarado antes do LLM)"}
                 for name in pii_fired]

    blocking = violations  # denylist + banned terms block; PII in input only warns
    allowed = not blocking
    action = "allow" if allowed else "block"
    all_violations = violations + pii_flags

    # audit log recebe a amostra JÁ MASCARADA — o log de governança não pode ser
    # ele próprio um vazamento de PII
    await _log("input", masked_text, action, all_violations, user_key, session_id, area)

    return {
        "allowed": allowed,
        "action": action,
        "violations": all_violations,
        "masked_text": masked_text,
        "pii_masked": bool(pii_fired),
        "block_message": policy.get(
            "block_message",
            "Desculpe, não posso ajudar com esse pedido. Ele fere as políticas de uso.",
        ) if not allowed else None,
        "policy_id": str(policy.get("_id")) if policy else None,
    }


async def check_output(text: str, user_key: str, session_id: str,
                       area: str = "default") -> dict:
    """Guardrail on the agent's answer: redact any PII before it reaches the user."""
    policy = await get_policy(area)
    masked, fired = _mask(text, policy.get("pii_patterns", []))
    violations = [{"rule": "pii_saida", "kind": name, "detail": "mascarado"} for name in fired]
    action = "mask" if fired else "allow"
    if fired:
        # loga a versão mascarada — nunca a resposta com a PII em claro
        await _log("output", masked, action, violations, user_key, session_id, area)
    return {"text": masked, "masked": bool(fired), "action": action, "violations": violations}


async def _log(stage: str, text: str, action: str, violations: list[dict],
               user_key: str, session_id: str, area: str = "default") -> None:
    """Append an audit record to POC.guardrail_events (TTL de 30 dias via índice)."""
    await safe_query(
        poc()[EVENTS_COLLECTION].insert_one({
            "stage": stage,               # "input" | "output"
            "action": action,             # "allow" | "block" | "mask"
            "text_sample": text[:280],
            "violations": violations,
            "user_key": user_key,
            "session_id": session_id,
            "area": area,
            "at": _utcnow(),
        })
    )


async def _log_candidate(text: str, near_miss: dict, user_key: str,
                         session_id: str, area: str) -> None:
    """Append a near-miss to POC.guardrail_candidates — a REVIEW QUEUE, not an
    auto-updating denylist. Um usuário mal-intencionado poderia repetir a mesma
    frase de propósito para 'treinar' o guardrail a bloquear algo legítimo de
    outro cliente; por isso a promoção para o denylist exige aprovação humana
    (review_candidate), nunca acontece sozinha.
    """
    await safe_query(
        poc()[CANDIDATES_COLLECTION].insert_one({
            "text_sample": text[:280],
            "closest_phrase": near_miss["closest_phrase"],
            "category": near_miss.get("category"),
            "score": near_miss["score"],
            "threshold": near_miss["threshold"],
            "user_key": user_key,
            "session_id": session_id,
            "area": area,
            "status": "pending",   # pending | approved | rejected
            "at": _utcnow(),
        })
    )


async def list_candidates(status: str = "pending", limit: int = 50,
                          area: str | None = None) -> list[dict]:
    """Fila de near-misses para revisão humana — powers the guardrails panel.

    `area` escopa a fila ao tenant: sem ela (visão de operador/admin) vêm todas
    as áreas; com ela, um tenant nunca vê candidato de outro.
    """
    query: dict = {} if status == "all" else {"status": status}
    if area is not None:
        query["area"] = area
    cursor = (
        poc()[CANDIDATES_COLLECTION]
        .find(query, max_time_ms=MAX_TIME_MS)
        .sort("at", -1)
        .limit(limit)
    )
    docs = await safe_query(cursor.to_list(length=limit))
    for d in docs:
        d["_id"] = str(d["_id"])
    return docs


async def review_candidate(candidate_id: str, decision: str, reviewer: str = "") -> dict:
    """Human-in-the-loop promotion: approve inserts the phrase into the live
    denylist (autoEmbed indexa sozinho); reject só fecha o item. Nunca é
    automático — é a política deliberada contra auto-envenenamento do denylist.
    """
    from bson import ObjectId

    if decision not in ("approved", "rejected"):
        raise ValueError("decision deve ser 'approved' ou 'rejected'")
    if not ObjectId.is_valid(candidate_id):
        raise ValueError("candidate_id inválido")
    coll = poc()[CANDIDATES_COLLECTION]
    cand = await safe_query(coll.find_one({"_id": ObjectId(candidate_id)}, max_time_ms=MAX_TIME_MS))
    if not cand:
        raise ValueError("candidato não encontrado")

    now = _utcnow()
    # condicionado a status=pending: revisão dupla não re-promove nem duplica
    res = await safe_query(coll.update_one(
        {"_id": cand["_id"], "status": "pending"},
        {"$set": {"status": decision, "reviewed_by": reviewer[:64], "reviewed_at": now}},
    ))
    if res.modified_count == 0:
        raise ValueError(f"candidato já revisado (status atual: {cand.get('status')})")

    promoted = False
    if decision == "approved":
        await safe_query(poc()[DENYLIST_COLLECTION].insert_one({
            "phrase": cand["text_sample"],
            "category": cand.get("category", "aprendido_por_revisao"),
            "area": cand.get("area", "global"),
            "source_candidate": cand["_id"],
            "at": now,
        }))
        promoted = True
    return {"status": decision, "promoted": promoted}


async def recent_events(limit: int = 20, area: str | None = None) -> list[dict]:
    """Latest audit records — powers the guardrails panel.

    `area` escopa o log ao tenant; sem ela é a visão de operador (todas as áreas).
    """
    query = {"area": area} if area is not None else {}
    cursor = poc()[EVENTS_COLLECTION].find(query, max_time_ms=MAX_TIME_MS).sort("at", -1).limit(limit)
    docs = await safe_query(cursor.to_list(length=limit))
    for d in docs:
        d["_id"] = str(d["_id"])
    return docs
