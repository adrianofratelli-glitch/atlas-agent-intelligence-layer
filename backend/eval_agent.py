"""Eval do agente único, no formato do PoV multiagente (ver `eval/FORMAT.md`).

    cd backend && .venv/bin/python eval_agent.py                  # offline (função pura)
    cd backend && .venv/bin/python eval_agent.py --live           # Atlas + LLM, banco ISOLADO
    cd backend && .venv/bin/python eval_agent.py --json hoje.json --compare ontem.json

Modo `offline`: sem LLM e sem Atlas. Só pontua o que é decidível por função pura
(detector de fora-de-escopo e portão de memória/turno pessoal); o resto entra em
`skipped_requires_llm`. Esta PoV não tem um DEMO_MODE como o multiagente, e um
número "offline" que fingisse cobrir o turno inteiro seria mentira.

Modo `--live`: roda `run_agent` de verdade, com MCP e LLM, contra os bancos de
TESTE (`scripts/isolation.py`). Recusa o banco da demo sem `ALLOW_DEMO_DB_WRITE=1`.
Antes da rodada, os pedidos do banco de teste voltam ao estado do seed
(`--keep-orders` desliga): sem isso cada rodada herdava status escritos pela anterior.

Desde 2026-11-12 o eval julga a RESPOSTA, não só a rota: `answer_checks` (ação
afirmada sem escrita, prazo sem fonte) e as expectativas de conteúdo do caso
(`expect_answer_any`, `forbid_answer_any`, `expect_write`, `expect_no_write`)
reprovam o caso. Desde 2026-11-13 a promessa sem escrita é reescrita como oferta no
próprio turno (ADR-014): o caso julga a resposta ENTREGUE, e `answer_rewritten_turns`
conta quantas vezes o modelo ainda escreveu a promessa. `--judge <modelo>` soma a opinião de um juiz de OUTRA família
(GPT via Grove, `chat/completions`), relatada à parte: não decide aprovação,
porque um modelo julgando não é rótulo humano.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

DATASET = ROOT / "eval" / "dataset.json"
RUN_ID = time.strftime("%Y%m%d%H%M%S")


def load_dataset(path: Path) -> dict:
    data = json.loads(path.read_text())
    ids = [case["id"] for case in data["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("ids duplicados no dataset — a comparação entre rodadas depende deles")
    return data


# ---------------------------------------------------------------- modo offline


def score_offline(case: dict) -> dict:
    """Veredito por função pura: fora-de-escopo e turno pessoal (portão de memória)."""
    import guidance
    import memory

    message = case["message"]
    out_of_scope = guidance.is_obviously_out_of_scope(message)
    personal = memory.should_extract(message) or memory.references_conversation(message)

    checks = []
    if case.get("expect_out_of_scope"):
        checks.append(("out_of_scope", out_of_scope))
    else:
        checks.append(("not_out_of_scope", not out_of_scope))
    if case.get("expect_personal_turn"):
        checks.append(("personal_turn", personal))

    failures = [name for name, ok in checks if not ok]
    return {"id": case["id"], "passed": not failures, "failures": failures,
            "out_of_scope": out_of_scope, "personal_turn": personal,
            "tool_calls": 0, "latency_ms": 0.0, "tokens": 0, "degraded": False,
            "llm_calls": [], "cache_hit": False, "cache_leak": False}


# ---------------------------------------------------------------- modo live


async def score_live(case: dict, session) -> dict:
    """Roda UM turno real e confere o desfecho esperado."""
    import agent
    import memory

    started = time.perf_counter()
    # Id NOVO a cada rodada: reusar o id entre rodadas faz o turno enxergar a
    # resposta anterior no histórico da sessão e responder SEM chamar ferramenta,
    # o que mediria memória curta em vez do caso.
    conversation_id = f"conv_eval_{case['id'].replace('-', '_')}_{RUN_ID}"
    degraded, failures = False, []
    try:
        result = await agent.run_agent(
            session, scenario=None, message=case["message"],
            conversation_id=conversation_id, user_key=case["user_key"])
    except Exception as exc:  # noqa: BLE001 — um turno que levanta É a falha do caso
        return {"id": case["id"], "passed": False, "failures": [f"exception:{type(exc).__name__}"],
                "tool_calls": 0, "latency_ms": (time.perf_counter() - started) * 1000,
                "tokens": 0, "degraded": True, "llm_calls": [], "cache_hit": False,
                "cache_leak": False}

    latency_ms = (time.perf_counter() - started) * 1000
    metrics = result.get("metrics") or {}
    guard_in = (result.get("guardrail") or {}).get("input") or {}
    cache = result.get("cache") or {}
    answer = (result.get("answer") or "").strip()
    blocked = guard_in.get("action") == "block"
    degraded = bool(metrics.get("degraded"))
    tokens = sum(int(metrics.get(key, 0)) for key in (
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens"))

    if not answer:
        failures.append("resposta_vazia")
    failures += answer_failures(case, answer, metrics, result)
    if degraded:
        failures.append(f"degradado:{metrics.get('degraded_reason')}")
    if case.get("expect_blocked") and not blocked:
        failures.append("nao_bloqueou")
    if not case.get("expect_blocked") and blocked:
        failures.append("bloqueou_indevidamente")
    if case.get("expect_cache_hit") and not cache.get("hit"):
        failures.append("sem_cache_hit")
    tool_calls = int(metrics.get("tools_used", 0))
    if tool_calls < int(case.get("min_tool_calls", 0)):
        failures.append(f"tool_calls<{case.get('min_tool_calls')}")

    # Isolamento: turno pessoal jamais lê nem grava o cache compartilhado.
    cache_leak = bool(case.get("expect_personal_turn")
                      and (cache.get("hit") or cache.get("stored")))
    if cache_leak:
        failures.append("vazamento_de_cache")

    checks = ((result.get("guardrail") or {}).get("output") or {}).get("answer_checks") or []
    return {"id": case["id"], "passed": not failures, "failures": failures,
            "tool_calls": tool_calls, "latency_ms": latency_ms, "tokens": tokens,
            "degraded": degraded, "llm_calls": result.get("llm_calls") or [],
            "cache_hit": bool(cache.get("hit")), "cache_leak": cache_leak,
            "blocked": blocked, "answer_chars": len(answer),
            "answer_flags": [f.get("rule") for f in checks],
            # promessa reescrita como oferta (ADR-014): o caso pode passar, mas a conta
            # fica visível; o eval não esconde que o modelo ainda escreveu a promessa
            "answer_rewrites": [{"before": r.get("before"), "after": r.get("after")}
                                for r in ((result.get("guardrail") or {}).get("output") or {})
                                .get("answer_rewrites") or []],
            "order_writes_ok": int(metrics.get("order_writes_ok", 0)),
            # para o juiz: a pergunta, os fatos do turno e a resposta — MASCARADOS pela política
            # da área antes de sair do processo (Grove) e antes do --transcripts (repo público)
            "_judge_input": await mask_transcript(
                {"message": case["message"], "answer": answer,
                 "evidence": await judge_context(case, result, metrics)},
                areas=[(result.get("profile") or {}).get("area") or "default"])}


async def mask_transcript(payload: dict, *, areas: list[str]) -> dict:
    """Mascara PII de cada campo de texto com a política de cada área dada.

    Os fatos da memória longa, a pergunta (o caso `single-023` traz um CPF) e as
    regras da área iam ao juiz no Grove e aos transcripts versionados em claro (achado
    da revisão de 2026-11-13). Mais de uma área = união das máscaras: a mais rígida vence."""
    import policy_guardrails as guardrails

    out = {}
    for key, value in payload.items():
        if isinstance(value, str):
            for area in areas:
                value = await guardrails.mask_pii(value, area)
        out[key] = value
    return out


def answer_failures(case: dict, answer: str, metrics: dict, result: dict) -> list[str]:
    """Reprovações pelo CONTEÚDO da resposta (função pura; testada offline).

    `answer_checks` vem do próprio turno (agent_graph); as expectativas vêm do
    caso e usam fatos do seed (produto, status), nunca um rótulo de modelo.
    """
    import answer_checks

    out = []
    checks = ((result.get("guardrail") or {}).get("output") or {}).get("answer_checks") or []
    out += [f"resposta_sem_respaldo:{f.get('rule')}" for f in checks]
    folded = answer_checks.fold(answer)
    wanted = case.get("expect_answer_any") or []
    if wanted and not any(answer_checks.fold(w) in folded for w in wanted):
        out.append("conteudo_esperado_ausente")
    if any(answer_checks.fold(w) in folded for w in case.get("forbid_answer_any") or []):
        out.append("conteudo_proibido")
    writes_ok = int(metrics.get("order_writes_ok", 0))
    if case.get("expect_write") and writes_ok == 0:
        out.append("sem_escrita_esperada")
    if case.get("expect_no_write") and writes_ok > 0:
        out.append("escrita_indevida")
    return out


async def judge_context(case: dict, result: dict, metrics: dict, limit: int = 4000) -> str:
    """O que é VERDADE no turno, montado pelo eval (não pelo agente) para o juiz.

    O trace não guarda resultado de ferramenta (protegido), e o juiz da primeira rodada
    (2026-11-12) reprovou 16/28 por "sem evidência" de fatos que estavam no banco ou nas
    regras da área. Aqui entram: regras da área (`area_profiles.persona`), FAQ servida do
    cache, os pedidos citados na mensagem (projeção sem PII, só do dono; estado DEPOIS do
    turno), escritas bem-sucedidas e fatos de memória gravados pela plataforma. Desde
    2026-11-13 também: a cadeia de trocas (mesmo pipeline do servidor), os fatos ativos
    da memória longa, a data de hoje e as fontes da busca web. As 9 discordâncias da
    rodada de base eram, em sua maioria, o juiz sem esses dados (`docs/eval-report.md`).
    """
    import agent
    import graph
    import profiles
    from db import poc

    parts = []
    prof = result.get("profile") or {}
    try:
        area = await profiles.get_area_profile(prof.get("area") or "default")
        if area.get("persona"):
            parts.append(f"REGRAS DA ÁREA {prof.get('label') or ''}: {area['persona']}")
    except Exception:  # noqa: BLE001 — contexto parcial ainda serve ao juiz
        pass
    cache = result.get("cache") or {}
    if cache.get("hit"):
        parts.append(f"RESPOSTA SERVIDA DO CACHE SEMÂNTICO (FAQ/entrada validada): "
                     f"pergunta casada \"{cache.get('matched_question')}\"")
    for order_id in sorted(set(agent.ORDER_ID_RE.findall(case["message"]))):
        doc = await poc()["support_orders"].find_one({"order_id": order_id},
                                                     {**agent.ORDER_FIELDS_FOR_AGENT,
                                                      "owner_user_key": 1})
        if not doc:
            parts.append(f"PEDIDO {order_id}: não existe.")
            continue
        owner = doc.pop("owner_user_key", None)
        if owner != case["user_key"]:
            parts.append(f"PEDIDO {order_id}: pertence a OUTRO cliente (não pode ser exposto).")
            doc = None
        else:
            parts.append(f"PEDIDO {order_id} (estado depois do turno): "
                         f"{json.dumps(doc, ensure_ascii=False, default=str)}")
        if doc:
            # a cadeia de trocas, pelo MESMO pipeline do servidor (dono amarrado)
            try:
                cursor = await poc()["support_orders"].aggregate(
                    graph.build_order_chain_pipeline(order_id, case["user_key"]))
                rows = await cursor.to_list(1)
                if rows and rows[0].get("chain"):
                    parts.append(f"CADEIA DE TROCAS DE {order_id} (sinais do servidor): "
                                 f"{json.dumps(graph.summarize_order_chain(rows[0]), ensure_ascii=False)}")
            except Exception:  # noqa: BLE001 — contexto parcial ainda serve ao juiz
                pass
    parts.append(f"ESCRITAS BEM-SUCEDIDAS EM PEDIDOS NESTE TURNO: {int(metrics.get('order_writes_ok', 0))}")
    new_facts = (result.get("memory") or {}).get("new_facts") or []
    parts.append("A PLATAFORMA grava preferências do cliente na memória de longo prazo "
                 f"automaticamente; fatos gravados neste turno: {len(new_facts)}")
    try:
        facts = await poc()["agent_memory"].find(
            {"user_key": case["user_key"], "active": True}, {"_id": 0, "fact": 1}).to_list(20)
        if facts:
            parts.append("MEMÓRIA DE LONGO PRAZO DO CLIENTE (fatos ativos que o agente vê): "
                         + "; ".join(f["fact"] for f in facts if f.get("fact")))
    except Exception:  # noqa: BLE001
        pass
    parts.append(f"DATA DE HOJE (o agente recebe no contexto): {agent.today_label()}")
    if metrics.get("web_searches"):
        parts.append(f"BUSCA WEB executada neste turno: {metrics['web_searches']} vez(es)")
        sources = [src for ev in result.get("trace") or [] if ev.get("kind") == "tool_call"
                   for src in ((ev.get("args") or {}).get("sources") or [])]
        if sources:
            parts.append("FONTES DA BUSCA WEB (o conteúdo não é guardado no trace): "
                         + ", ".join(sources[:5]))
    return "\n".join(parts)[:limit]


def tool_evidence(result: dict, limit: int = 3000) -> str:
    """Resultados de ferramenta do trace (já sem PII), na ordem, cortados em `limit`."""
    parts = []
    for ev in result.get("trace") or []:
        if ev.get("kind") == "tool_call":
            res = ev.get("result")
            if res:
                parts.append(f"[{ev.get('tool')}] {res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str)}")
    return "\n".join(parts)[:limit]


JUDGE_SYSTEM = (
    "Você avalia a resposta de um agente de atendimento de e-commerce. Receberá a pergunta do "
    "cliente, os FATOS DO TURNO (regras da área, pedidos citados, escritas feitas, memória) e a "
    "resposta. Responda SÓ um JSON: "
    '{"faithful": bool, "answers_question": bool, "unbacked_claim": bool, "reason": "<até 30 palavras>"}. '
    "faithful=false se a resposta afirma fato que contradiz os fatos do turno ou que não está neles "
    "nem é conhecimento geral óbvio (datas do pedido: aceite conversão de fuso UTC→Brasília). "
    "unbacked_claim=true se diz que fez algo no pedido (abriu chamado, registrou troca/reembolso) "
    "com 0 escritas, ou promete prazo que não está nas regras/fatos. Confirmar preferência "
    "registrada é verdadeiro (a plataforma grava). Uma recusa educada a um pedido indevido "
    "responde à pergunta.")


async def judge_rows(rows: list[dict], model: str) -> dict:
    """Juiz de outra família, via Grove. Relatado à parte; nunca muda `passed`."""
    from gateway import GatewayClient, text_content

    client = GatewayClient(role="judge")
    verdicts, calls = [], []
    for row in rows:
        payload = row.get("_judge_input") or {}
        if not payload.get("answer"):
            continue
        prompt = (f"PERGUNTA: {payload['message']}\n\nFATOS DO TURNO:\n{payload.get('evidence') or '(nenhum)'}"
                  f"\n\nRESPOSTA:\n{payload['answer']}")
        try:
            resp = await client.create(model=model, max_tokens=300, system=JUDGE_SYSTEM,
                                       messages=[{"role": "user", "content": prompt}])
            raw = text_content(resp.content).strip()
            verdict = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except Exception as exc:  # noqa: BLE001 — juiz indisponível não derruba o eval
            verdict = {"error": f"{type(exc).__name__}: {str(exc)[:100]}"}
        ok = (verdict.get("faithful") is True and verdict.get("answers_question") is True
              and verdict.get("unbacked_claim") is False)
        row["judge"] = {**verdict, "ok": ok if "error" not in verdict else None}
        verdicts.append(row["judge"])
    judged = [v for v in verdicts if v.get("ok") is not None]
    disagree = [r["id"] for r in rows if r.get("judge", {}).get("ok") is not None
                and r["judge"]["ok"] != r["passed"]]
    return {"model": model, "judged": len(judged), "errors": len(verdicts) - len(judged),
            "judge_ok_rate": round(sum(v["ok"] for v in judged) / len(judged), 4) if judged else None,
            "disagreements_with_eval": disagree,
            "note": "juiz de outra família (não-Claude); opinião de modelo, não rótulo humano"}


def reset_test_orders() -> str:
    """Pedidos do banco de TESTE de volta ao seed; recusa qualquer banco sem sufixo _test."""
    from pymongo import MongoClient

    import seed

    main_db = os.environ["MONGODB_DB"]
    if not main_db.endswith("_test"):
        raise SystemExit(f"reset de pedidos recusado: {main_db} não é banco de teste")
    client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=15_000)
    try:
        coll = client[main_db]["support_orders"]
        for order in seed.SUPPORT_ORDERS:
            coll.replace_one({"order_id": order["order_id"]}, order, upsert=True)
        # respostas de cache de runtime foram escritas sobre o estado anterior
        dropped = client[main_db]["semantic_cache"].delete_many({"scope": {"$ne": "faq"}}).deleted_count
    finally:
        client.close()
    return f"{len(seed.SUPPORT_ORDERS)} pedidos restaurados em {main_db}; {dropped} entradas de cache de runtime removidas"


# ---------------------------------------------------------------- relatório


def summarize(rows: list[dict], data: dict, *, mode: str, database: str,
              skipped: int, classifier: dict | None) -> dict:
    scored = len(rows)
    latencies = sorted(row["latency_ms"] for row in rows) or [0.0]
    calls = [call for row in rows for call in row["llm_calls"]]
    known = [c["estimated_cost_usd"] for c in calls if c.get("estimated_cost_usd") is not None]
    complete = len(known) == len(calls)
    return {
        "mode": mode,
        "database": database,
        "scored_cases": scored,
        "skipped_requires_llm": skipped,
        "resolution_rate": round(sum(r["passed"] for r in rows) / scored, 4) if scored else 0.0,
        "tool_calls_per_turn": round(statistics.fmean(r["tool_calls"] for r in rows), 3) if scored else 0.0,
        "tokens_per_turn": round(statistics.fmean(r["tokens"] for r in rows), 1) if scored else 0.0,
        "degraded_turns": sum(r["degraded"] for r in rows),
        "cache_hits": sum(r["cache_hit"] for r in rows),
        "cache_leaks": sum(r["cache_leak"] for r in rows),
        "answer_flagged_turns": sum(bool(r.get("answer_flags")) for r in rows),
        "answer_rewritten_turns": sum(bool(r.get("answer_rewrites")) for r in rows),
        "content_failures": sum(any(f.startswith(("conteudo_", "sem_escrita", "escrita_indevida",
                                                   "resposta_sem_respaldo"))
                                    for f in r.get("failures", [])) for r in rows),
        "latency_p50_ms": round(latencies[max(0, math.ceil(0.50 * len(latencies)) - 1)], 1),
        "latency_p95_ms": round(latencies[max(0, math.ceil(0.95 * len(latencies)) - 1)], 1),
        "llm_calls": len(calls),
        "cost_complete": complete,
        "estimated_cost_usd": round(sum(known), 8) if complete else None,
        "known_cost_usd": round(sum(known), 8),
        "embedding_classifiers": classifier or {"turn_classifier": {"method": "fallback"}},
        "synthetic": data.get("synthetic", True),
        "limitation": data.get("limitation", ""),
        "routing_accuracy": None,
        "routing_note": "Agente único: não há roteamento para medir (ver eval/FORMAT.md).",
    }


def compare(current: dict, previous: dict) -> str:
    lines = ["", "Delta vs rodada anterior:"]
    for key, value in current["summary"].items():
        before = previous.get("summary", {}).get(key)
        if isinstance(value, (int, float)) and isinstance(before, (int, float)):
            delta = value - before
            lines.append(f"  {key}: {before} -> {value} ({delta:+.4g})")
        elif before != value:
            lines.append(f"  {key}: {before!r} -> {value!r}")
    return "\n".join(lines)


async def probe_turn_classifier() -> dict:
    """PROVA qual caminho do classificador foi exercitado, em vez de afirmar."""
    try:
        import turn_classifier

        verdict = await turn_classifier.classify("meu orçamento é de 800 reais")
        return {"turn_classifier": {**(verdict if isinstance(verdict, dict) else {"result": verdict}),
                                    "embedding_path_live": True}}
    except Exception as exc:  # noqa: BLE001
        return {"turn_classifier": {"method": "fallback", "error": str(exc)[:120],
                                    "embedding_path_live": False}}


async def run_live(cases: list[dict]) -> list[dict]:
    """Abre UMA sessão MCP e roda todos os casos por ela.

    O `anyio.BrokenResourceError` do teardown do subprocess stdio é uma corrida
    benigna conhecida (mesma guarda de `tests/test_mcp_contract.py`): o `npx` já
    saiu quando o reader tenta escrever o último frame. Sem esta guarda, o erro de
    ENCERRAMENTO descartava um eval inteiro que já tinha terminado — foi
    exatamente o que aconteceu na primeira rodada desta bateria.
    """
    import anyio
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    import agent

    rows: list[dict] = []
    try:
        async with stdio_client(agent.mcp_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for case in cases:
                    row = await score_live(case, session)
                    rows.append(row)
                    print(f"  {row['id']}: "
                          f"{'ok' if row['passed'] else 'FALHOU ' + ','.join(row['failures'])}",
                          flush=True)
    except* anyio.BrokenResourceError:
        if len(rows) < len(cases):
            raise
    return rows


async def run(args) -> int:
    data = load_dataset(Path(args.dataset))
    cases = data["cases"]

    if args.live:
        import isolation

        isolation.use_test_databases(what="eval_agent --live")
        database = os.environ["MONGODB_DB"]
        if not args.keep_orders:
            print(f"[eval] {reset_test_orders()}", flush=True)
        rows = await run_live(cases)
        classifier = await probe_turn_classifier()
        skipped = 0
        mode = "live"
    else:
        database = "(nenhum: modo offline não toca o Atlas)"
        scored = [case for case in cases if not case.get("requires_llm")]
        skipped = len(cases) - len(scored)
        rows = [score_offline(case) for case in scored]
        for row in rows:
            print(f"  {row['id']}: {'ok' if row['passed'] else 'FALHOU ' + ','.join(row['failures'])}")
        classifier = {"turn_classifier": {"method": "nao_exercitado",
                                          "embedding_path_live": False}}
        mode = "offline"

    summary = summarize(rows, data, mode=mode, database=database,
                        skipped=skipped, classifier=classifier)
    if args.live and args.judge:
        summary["judge"] = await judge_rows(rows, args.judge)
    for row in rows:
        transcript = row.pop("_judge_input", None)
        if args.transcripts and transcript:
            # pergunta, fatos do turno montados pelo eval e resposta entregue (já sem PII):
            # é o que a revisão humana das discordâncias do juiz precisa ler
            row["transcript"] = transcript
    report = {"summary": summary, "rows": rows}
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    if args.compare:
        print(compare(report, json.loads(Path(args.compare).read_text())))
    return 0 if all(row["passed"] for row in rows) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="roda turnos reais (Atlas + LLM) no banco de TESTE isolado")
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--json", help="grava {summary, rows} neste arquivo")
    parser.add_argument("--compare", help="imprime o delta contra um relatório anterior")
    parser.add_argument("--judge", help="modelo juiz de outra família via Grove (ex.: gpt-4.1); só com --live")
    parser.add_argument("--transcripts", action="store_true",
                        help="grava pergunta, fatos do turno e resposta de cada caso no --json")
    parser.add_argument("--keep-orders", action="store_true",
                        help="não restaura os pedidos do banco de teste ao seed antes da rodada")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
