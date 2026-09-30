"""Anthropic wrapper.

Core rule of the demo: model_config is read from Atlas on EVERY call (zero
caching). Swapping the document in the database swaps the application's model
live, with no restart and no deploy.
"""

import os
import time

from anthropic import APIError, AsyncAnthropic

import observability
from db import MAX_TIME_MS, SafeQueryError, ai_brain, safe_query

from gateway import GatewayClient, metered_turn

client = GatewayClient(role="model_swap")

# Micro-cache opcional do model_config. Default 0 = DESLIGADO: a regra da demo
# ("o doc é lido a cada request, swap é instantâneo") continua valendo. Em
# produção, CONFIG_CACHE_SECONDS=5 corta uma leitura Mongo por chamada de LLM
# ao custo de o swap propagar em até 5s.
CONFIG_CACHE_SECONDS = float(os.getenv("CONFIG_CACHE_SECONDS", "0"))
_config_cache: dict[str, tuple[float, dict]] = {}


async def get_active_config(area: str = "default") -> dict:
    """Config de modelo com escopo por ÁREA (tenant), com fallback para o doc
    global. Um documento com {"area": "<área>", "active": true} sobrepõe a
    config default só para aquele tenant — mesma história de config viva das
    guardrail_policies, agora sem um swap global afetar todos os tenants.
    """
    if CONFIG_CACHE_SECONDS > 0:
        cached = _config_cache.get(area)
        if cached and time.monotonic() - cached[0] < CONFIG_CACHE_SECONDS:
            return cached[1]
    coll = ai_brain()["model_config"]
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
    if not doc:
        raise SafeQueryError(
            "config",
            "Nenhum documento ativo em ai_brain.model_config. Rode backend/seed.py.",
        )
    if CONFIG_CACHE_SECONDS > 0:
        _config_cache[area] = (time.monotonic(), doc)
    return doc


async def call_model(model_cfg: dict, system: str, messages: list[dict], fallback: bool = False) -> dict:
    """A single call to the model described by model_cfg ({model, temperature, max_tokens})."""
    start = time.perf_counter()
    resp = await client.messages.create(
        model=model_cfg["model"],
        _fallback=fallback,
        max_tokens=model_cfg.get("max_tokens", 1024),
        temperature=model_cfg.get("temperature", 0.3),
        system=system,
        messages=messages,
    )
    latency_ms = int((time.perf_counter() - start) * 1000)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    # Alimenta o card "Economia" de /api/metrics: custo médio por chamada LLM
    # é a base do cálculo de USD poupado por cache hit.
    observability.metrics.bump("llm_calls")
    observability.metrics.bump("llm_input_tokens", resp.usage.input_tokens)
    observability.metrics.bump("llm_output_tokens", resp.usage.output_tokens)
    return {
        "text": text,
        "model": resp.model,
        "latency_ms": latency_ms,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }


@metered_turn
async def call_with_fallback(system: str, messages: list[dict],
                             area: str = "default") -> dict:
    """Reads model_config now, tries the primary and falls back on an API error."""
    cfg = await get_active_config(area)
    try:
        result = await call_model(cfg["primary"], system, messages)
        result["route"] = "primary"
        return result
    except APIError:
        result = await call_model(cfg["fallback"], system, messages, fallback=True)
        result["route"] = "fallback"
        return result
