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

    return {"id": case["id"], "passed": not failures, "failures": failures,
            "tool_calls": tool_calls, "latency_ms": latency_ms, "tokens": tokens,
            "degraded": degraded, "llm_calls": result.get("llm_calls") or [],
            "cache_hit": bool(cache.get("hit")), "cache_leak": cache_leak,
            "blocked": blocked, "answer_chars": len(answer)}


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

    report = {"summary": summarize(rows, data, mode=mode, database=database,
                                   skipped=skipped, classifier=classifier),
              "rows": rows}
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
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
