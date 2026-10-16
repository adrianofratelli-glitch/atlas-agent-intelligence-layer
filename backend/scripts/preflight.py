"""Pré-voo da demo: tudo que costuma falhar AO VIVO, conferido antes da call.

Cada verificação existe porque é uma forma diferente de a demo quebrar na frente
do cliente, e todas eram descobertas no pior momento possível:

    Atlas              cluster alcançável e o IP liberado
    Índices            semantic_cache_vs, agent_memory_vs, guardrail_denylist_vs,
                       agent_memory_bm25, produtos_vector em READY (autoEmbed
                       indexa ASSÍNCRONO: índice BUILDING = cache/memória mudos)
    Config viva        model_config ativo, cache_config e guardrail_policies com
                       limiar CALIBRADO (limiar ausente = guardrail cego)
    Identidades        app_users e area_profiles semeados (sem eles o seletor
                       da UI abre vazio)
    Dados da demo      support_orders com a cadeia PED-1005→1006→1007 intacta
    Catálogo           produtos_vector com documentos (a busca de substituto)
    MCP                `npx mongodb-mcp-server@<pinado>` sobe, conecta e lista
                       as ferramentas da allowlist
    LLM                model_config aponta para um modelo que o gateway conhece
    Observabilidade    sink de tracing e Langfuse (informativo: fail-open)

Uso:

    cd backend && .venv/bin/python scripts/preflight.py           # tudo
    cd backend && .venv/bin/python scripts/preflight.py --quick   # pula MCP e LLM
    cd backend && .venv/bin/python scripts/preflight.py --json out.json

Saída: 0 = pronto para apresentar; 1 = há FALHA bloqueante; 2 = só avisos.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GREEN, YELLOW, RED, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[0m"

REQUIRED_INDEXES = [
    ("POC", "semantic_cache", "semantic_cache_vs"),
    ("POC", "agent_memory", "agent_memory_vs"),
    ("POC", "agent_memory", "agent_memory_bm25"),
    ("POC", "guardrail_denylist", "guardrail_denylist_vs"),
    ("POC", "produtos_vector", "produtos_vector"),
    ("ai_brain", "turn_probes", "turn_probes_vs"),
]
REPLACEMENT_CHAIN = ["PED-1005", "PED-1006", "PED-1007"]


class Check:
    def __init__(self, name: str):
        self.name = name
        self.status = "ok"
        self.detail = ""
        self.ms = 0.0

    def fail(self, detail: str):
        self.status, self.detail = "fail", detail
        return self

    def warn(self, detail: str):
        self.status, self.detail = "warn", detail
        return self

    def ok(self, detail: str = ""):
        self.status, self.detail = "ok", detail
        return self

    def line(self) -> str:
        color = {"ok": GREEN, "warn": YELLOW, "fail": RED}[self.status]
        mark = {"ok": "✓", "warn": "!", "fail": "✗"}[self.status]
        return (f"  {color}{mark}{RESET} {self.name:<34} {self.detail}"
                + (f" ({self.ms:.0f}ms)" if self.ms else ""))


async def _timed(check: Check, coro):
    started = time.perf_counter()
    try:
        return await coro
    finally:
        check.ms = (time.perf_counter() - started) * 1000


async def check_atlas() -> Check:
    check = Check("Atlas alcançável")
    try:
        from db import get_client

        await _timed(check, get_client().admin.command("ping"))
        return check.ok("cluster respondeu ao ping")
    except Exception as exc:  # noqa: BLE001
        return check.fail(f"{type(exc).__name__}: {str(exc)[:120]}")


async def check_indexes() -> list[Check]:
    """Índice em BUILDING é a falha silenciosa nº 1: a demo abre, mas o cache e a
    memória não acham nada e ninguém entende por quê."""
    from db import DB_BRAIN, DB_CATALOG, DB_MAIN, get_client

    client = get_client()
    resolved = {"POC": DB_MAIN, "ai_brain": DB_BRAIN}
    checks = []
    for db_alias, collection, index in REQUIRED_INDEXES:
        database = DB_CATALOG if collection == "produtos_vector" else resolved[db_alias]
        check = Check(f"índice {index}")
        try:
            cursor = await client[database][collection].list_search_indexes()
            rows = await cursor.to_list(None)
            match = next((r for r in rows if r.get("name") == index), None)
            if match is None:
                check.fail(f"NÃO EXISTE em {database}.{collection} — rode seed.py")
            elif match.get("status") != "READY":
                check.fail(f"status {match.get('status')} (autoEmbed ainda indexando)")
            else:
                check.ok(f"READY em {database}.{collection}")
        except Exception as exc:  # noqa: BLE001
            check.fail(f"{type(exc).__name__}: {str(exc)[:90]}")
        checks.append(check)
    return checks


async def check_live_config() -> list[Check]:
    from db import MAX_TIME_MS, ai_brain

    checks = []

    model = Check("model_config ativo")
    try:
        from llm import get_active_config

        cfg = await get_active_config("default")
        model.ok(f'primário {cfg["primary"]["model"]}, fallback '
                 f'{(cfg.get("fallback") or {}).get("model", "—")}')
    except Exception as exc:  # noqa: BLE001
        model.fail(f"{type(exc).__name__}: {str(exc)[:100]}")
    checks.append(model)

    cache = Check("cache_config calibrado")
    try:
        doc = await ai_brain()["cache_config"].find_one({}, max_time_ms=MAX_TIME_MS)
        threshold = (doc or {}).get("hit_threshold")
        if threshold is None:
            cache.fail("sem hit_threshold — rode calibrate_thresholds.py --apply")
        else:
            cache.ok(f"hit_threshold {threshold}")
    except Exception as exc:  # noqa: BLE001
        cache.fail(f"{type(exc).__name__}: {str(exc)[:100]}")
    checks.append(cache)

    guard = Check("guardrail_policies por área")
    try:
        import policy_guardrails as guardrails

        missing = []
        for area in ("default", "financeiro", "vendas", "logistica"):
            policy = await guardrails.get_policy(area)
            if guardrails._denylist_threshold(policy) is None:
                missing.append(area)
        if missing:
            guard.fail(f"sem denylist_threshold: {', '.join(missing)} (guardrail cego)")
        else:
            guard.ok("4 áreas com limiar calibrado")
    except Exception as exc:  # noqa: BLE001
        guard.fail(f"{type(exc).__name__}: {str(exc)[:100]}")
    checks.append(guard)

    dilution = Check("anti-diluição do denylist (pov-shared)")
    import policy_guardrails as guardrails
    if guardrails._shared_clause_scorer() is None:
        dilution.fail("pov-shared ausente: denylist pontua só o texto inteiro (evadível por "
                      "diluição) — rode scripts/bootstrap-venvs.sh --runtime")
    else:
        dilution.ok("texto inteiro + cada intenção (ascore_by_clause)")
    checks.append(dilution)

    turn = Check("turn_classifier_config")
    try:
        doc = await ai_brain()["turn_classifier_config"].find_one({}, max_time_ms=MAX_TIME_MS)
        turn.ok(f"limiar {doc.get('threshold')}") if doc else turn.warn(
            "ausente — classificador cai no default documentado")
    except Exception as exc:  # noqa: BLE001
        turn.warn(f"{type(exc).__name__}: {str(exc)[:80]}")
    checks.append(turn)
    return checks


async def check_demo_data() -> list[Check]:
    from db import DB_CATALOG, MAX_TIME_MS, get_client, poc

    checks = []

    users = Check("identidades da demo")
    try:
        total = await poc()["app_users"].count_documents({}, maxTimeMS=MAX_TIME_MS)
        areas = await poc()["app_users"].distinct("area")
        users.ok(f"{total} usuários, áreas: {', '.join(sorted(areas))}") if total else \
            users.fail("app_users VAZIO — o seletor da UI abre vazio; rode seed.py")
    except Exception as exc:  # noqa: BLE001
        users.fail(f"{type(exc).__name__}: {str(exc)[:100]}")
    checks.append(users)

    chain = Check("cadeia de trocas PED-1005→1007")
    try:
        found = await poc()["support_orders"].find(
            {"order_id": {"$in": REPLACEMENT_CHAIN}},
            {"order_id": 1, "replacement_order_id": 1, "_id": 0},
            max_time_ms=MAX_TIME_MS).to_list(None)
        by_id = {d["order_id"]: d.get("replacement_order_id") for d in found}
        broken = [o for o in REPLACEMENT_CHAIN[:-1] if by_id.get(o) is None]
        if len(found) != len(REPLACEMENT_CHAIN):
            chain.fail(f"faltam pedidos: {set(REPLACEMENT_CHAIN) - set(by_id)} — rode seed.py")
        elif broken:
            chain.fail(f"elo quebrado em {broken} ($graphLookup não tem o que percorrer)")
        else:
            chain.ok("3 elos íntegros")
    except Exception as exc:  # noqa: BLE001
        chain.fail(f"{type(exc).__name__}: {str(exc)[:100]}")
    checks.append(chain)

    catalog = Check("catálogo de produtos")
    try:
        total = await get_client()[DB_CATALOG]["produtos_vector"].estimated_document_count()
        catalog.ok(f"~{total:,} documentos") if total else catalog.fail(
            "produtos_vector VAZIO — a busca de substituto não devolve nada")
    except Exception as exc:  # noqa: BLE001
        catalog.fail(f"{type(exc).__name__}: {str(exc)[:100]}")
    checks.append(catalog)

    cache = Check("FAQs no cache semântico")
    try:
        total = await poc()["semantic_cache"].count_documents({}, maxTimeMS=MAX_TIME_MS)
        cache.ok(f"{total} entradas") if total else cache.warn(
            "cache vazio — o primeiro turno de FAQ não terá hit para mostrar")
    except Exception as exc:  # noqa: BLE001
        cache.warn(f"{type(exc).__name__}: {str(exc)[:80]}")
    checks.append(cache)
    return checks


async def check_mcp() -> Check:
    """Sobe o MCP pinado de verdade e confere a allowlist — é o que falha quando
    o `npx` não está no PATH da sessão que vai rodar a demo."""
    check = Check("MongoDB MCP Server")
    try:
        import anyio
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        import agent

        names: set[str] = set()
        started = time.perf_counter()
        try:
            async with stdio_client(agent.mcp_server_params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    names = {t.name for t in listed.tools}
        except* anyio.BrokenResourceError:
            if not names:
                raise
        check.ms = (time.perf_counter() - started) * 1000
        missing = agent.ALLOWED_TOOLS - names
        if missing:
            return check.fail(f"ferramentas ausentes: {sorted(missing)}")
        return check.ok(f"v{agent.MCP_SERVER_VERSION}, {len(names)} ferramentas expostas")
    except Exception as exc:  # noqa: BLE001
        return check.fail(f"{type(exc).__name__}: {str(exc)[:120]}")


async def check_llm() -> Check:
    """Uma chamada mínima: prova chave, rota do gateway e o modelo do model_config."""
    check = Check("LLM pelo gateway")
    try:
        from llm import call_with_fallback

        started = time.perf_counter()
        result = await call_with_fallback("Responda apenas: ok",
                                          [{"role": "user", "content": "ok?"}])
        check.ms = (time.perf_counter() - started) * 1000
        return check.ok(f'{result["model"]} respondeu ({result["route"]})')
    except Exception as exc:  # noqa: BLE001
        return check.fail(f"{type(exc).__name__}: {str(exc)[:120]}")


def check_observability() -> list[Check]:
    checks = []
    sink = Check("tracing (pov-shared)")
    configured = os.getenv("TRACE_SINK", "off")
    sink.ok(f"TRACE_SINK={configured}") if configured != "off" else sink.warn(
        "TRACE_SINK=off — sem spans; TRACE_SINK=atlas grava em observability.spans")
    checks.append(sink)

    lf = Check("Langfuse")
    if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
        try:
            import langfuse_tracing

            trace = langfuse_tracing.start_trace(name="preflight", user_id="preflight",
                                                 session_id="preflight", input_text="ping")
            lf.ok("credenciais válidas") if trace is not None else lf.warn(
                "configurado mas indisponível — o turno segue (fail-open), sem link")
        except Exception as exc:  # noqa: BLE001
            lf.warn(f"indisponível: {type(exc).__name__}")
    else:
        lf.warn("sem credenciais — card de economia segue, link do Langfuse não")
    checks.append(lf)
    return checks


async def check_atlas_capabilities() -> list[Check]:
    """Capabilities 2026-10: config do retrieval, analyzer do BM25, $rerank, FAQs com fonte
    e o worker de change stream. Tudo é fail-open no app, então aqui vira AVISO, não falha:
    a demo roda, só sem o ganho medido."""
    from db import DB_BRAIN, DB_MAIN, MAX_TIME_MS, ai_brain, get_client, poc

    import cache_invalidation
    import memory

    checks = []
    cfg = Check("memory_retrieval_config")
    try:
        doc = await ai_brain()[memory.RETRIEVAL_CONFIG_COLLECTION].find_one(
            {"_id": memory.RETRIEVAL_CONFIG_ID}, max_time_ms=MAX_TIME_MS)
        fusion, rerank, warns = memory.parse_retrieval_config(doc)
        if doc is None:
            cfg.warn("ausente: retrieval com defaults (rode seed.py)")
        elif warns:
            cfg.warn(f"config com valor inválido, usando default: {warns[0]}")
        else:
            cfg.ok(f"{fusion['per_branch_limit']} por perna, pesos {fusion['weights']}, "
                   f"rerank {'ligado' if rerank['enabled'] else 'desligado'}")
    except Exception as exc:  # noqa: BLE001
        cfg.warn(f"ilegível: {type(exc).__name__}")
    checks.append(cfg)

    analyzer = Check("BM25 da memória em português")
    try:
        cursor = await poc()[memory.MEMORY_COLLECTION].list_search_indexes()
        rows = await cursor.to_list(None)
        idx = next((r for r in rows if r.get("name") == memory.BM25_INDEX), None)
        field = ((idx or {}).get("latestDefinition") or {}).get("mappings", {}) \
            .get("fields", {}).get("fact", {})
        if field.get("analyzer") == "lucene.portuguese":
            analyzer.ok("lucene.portuguese (stopwords + stemming)")
        else:
            analyzer.warn("analyzer padrão: recall@5 0,972 em vez de 1,0 no eval — "
                          "reset controlado (seed.py) atualiza o índice")
    except Exception as exc:  # noqa: BLE001
        analyzer.warn(f"{type(exc).__name__}: {str(exc)[:80]}")
    checks.append(analyzer)

    if memory._ar is not None:
        _, rerank, _ = memory.parse_retrieval_config(
            await ai_brain()[memory.RETRIEVAL_CONFIG_COLLECTION].find_one(
                {"_id": memory.RETRIEVAL_CONFIG_ID}))
        rr = Check("$rerank (Public Preview nos rerank-3*)")
        if not rerank["enabled"]:
            rr.ok("desligado na config (decisão medida, ADR-009)")
        else:
            try:
                stages = memory._ar.build_rerank_stages(
                    "teste", "fact", memory._ar.RerankConfig(enabled=True, model=rerank["model"],
                                                             pool=1, top_n=1))
                cur = await poc()[memory.MEMORY_COLLECTION].aggregate(
                    [{"$match": {"active": True}}, {"$sort": {"_id": 1}}, {"$limit": 1}] + stages,
                    maxTimeMS=MAX_TIME_MS)
                await cur.to_list(1)
                rr.ok(f"{rerank['model']} aceito pelo servidor")
            except Exception as exc:  # noqa: BLE001
                rr.warn(f"rejeitado ({type(exc).__name__}): o turno segue sem rerank")
        checks.append(rr)

    faq = Check("FAQs com fonte da política")
    try:
        missing = await poc()["semantic_cache"].count_documents(
            {"scope": "faq", "source_hash": {"$exists": False}}, maxTimeMS=MAX_TIME_MS)
        total = await poc()["semantic_cache"].count_documents({"scope": "faq"},
                                                              maxTimeMS=MAX_TIME_MS)
        if missing:
            faq.warn(f"{missing}/{total} sem source_hash: a invalidação só as derruba no "
                     "reconcile (rode seed.py)")
        else:
            faq.ok(f"{total} FAQs com source_area/source_hash")
    except Exception as exc:  # noqa: BLE001
        faq.warn(f"{type(exc).__name__}")
    checks.append(faq)

    worker = Check("invalidação do cache (change stream)")
    try:
        doc = await ai_brain()[cache_invalidation.TOKENS_COLLECTION].find_one(
            {"_id": cache_invalidation.WORKER_ID}, max_time_ms=MAX_TIME_MS)
        if cache_invalidation.heartbeat_fresh(doc):
            st = (doc.get("stats") or {}).get("worker") or {}
            worker.ok(f"worker vivo ({st.get('state', '?')}, eventos {st.get('events', 0)}, "
                      f"lag {st.get('last_lag_seconds')}s)")
        else:
            worker.warn("sem heartbeat recente: app parado ou worker caído — o cache segue "
                        "só com TTL até ele voltar")
    except Exception as exc:  # noqa: BLE001
        worker.warn(f"{type(exc).__name__}")
    checks.append(worker)
    return checks


async def main(args) -> int:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    from db import DB_BRAIN, DB_CATALOG, DB_MAIN

    print(f"\nPré-voo da demo — bancos {DB_MAIN}/{DB_BRAIN} (catálogo em {DB_CATALOG})\n")
    checks: list[Check] = []

    print("Conectividade")
    atlas = await check_atlas()
    checks.append(atlas)
    print(atlas.line())
    if atlas.status == "fail":
        print("\nCluster inalcançável — as demais verificações não fazem sentido.\n")
        return 1

    print("\nÍndices Atlas Search / Vector Search")
    for check in await check_indexes():
        checks.append(check)
        print(check.line())

    print("\nConfiguração viva (ai_brain)")
    for check in await check_live_config():
        checks.append(check)
        print(check.line())

    print("\nCapabilities do Atlas (2026-10)")
    for check in await check_atlas_capabilities():
        checks.append(check)
        print(check.line())

    print("\nDados da demo (POC)")
    for check in await check_demo_data():
        checks.append(check)
        print(check.line())

    if not args.quick:
        print("\nCaminho do agente")
        for check in (await check_mcp(), await check_llm()):
            checks.append(check)
            print(check.line())

    print("\nObservabilidade")
    for check in check_observability():
        checks.append(check)
        print(check.line())

    failures = [c for c in checks if c.status == "fail"]
    warnings = [c for c in checks if c.status == "warn"]
    print()
    if failures:
        print(f"{RED}NÃO APRESENTE AINDA{RESET} — {len(failures)} verificação(ões) bloqueante(s):")
        for check in failures:
            print(f"    · {check.name}: {check.detail}")
    elif warnings:
        print(f"{YELLOW}PRONTO, com ressalvas{RESET} — {len(warnings)} aviso(s) não bloqueante(s).")
    else:
        print(f"{GREEN}PRONTO PARA APRESENTAR{RESET} — todas as verificações passaram.")
    print()

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"checks": [{"name": c.name, "status": c.status, "detail": c.detail,
                         "latency_ms": round(c.ms, 1)} for c in checks],
             "ready": not failures}, indent=2, ensure_ascii=False), encoding="utf-8")
    return 1 if failures else (2 if warnings else 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="pula MCP e LLM (checagem só de dados)")
    parser.add_argument("--json", help="grava o resultado neste arquivo")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
