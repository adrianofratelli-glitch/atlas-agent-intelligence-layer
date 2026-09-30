"""Langfuse tracing for the agent tool-use loop (Aba 3).

Fail-open by design: sem LANGFUSE_PUBLIC_KEY/SECRET_KEY configurados, todo
método vira no-op — a demo nunca quebra por causa de observability. O client
é lazy-instanciado uma vez por processo.
"""

import logging
import os

logger = logging.getLogger("poc.tracing")

_client = None
_enabled = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _get_client():
    global _client
    if not _enabled:
        return None
    if _client is None:
        try:
            from langfuse import Langfuse
            candidate = Langfuse()
            # auth_check é síncrono e rápido (uma chamada), mas SÓ roda uma vez por
            # processo (client é cacheado). Sem isso, um Langfuse fora do ar não
            # impede a criação da trace (o SDK é fire-and-forget) — o turno segue
            # normal, mas a UI mostraria um link "Ver no Langfuse" que dá 404 no
            # meio de uma demo ao vivo. Aqui a feature inteira fica invisível em
            # vez de visível-e-quebrada.
            if not candidate.auth_check():
                raise RuntimeError("Langfuse auth_check falhou")
            _client = candidate
        except Exception:  # noqa: BLE001 — tracing nunca derruba o turno
            logger.warning("Langfuse indisponível/mal configurado; tracing desligado para este processo")
            _client = False
    return _client or None


def start_trace(*, name: str, user_id: str, session_id: str, input_text: str,
                metadata: dict | None = None):
    """Uma trace por turno do agente. Retorna None se Langfuse não configurado."""
    client = _get_client()
    if client is None:
        return None
    try:
        return client.trace(name=name, user_id=user_id, session_id=session_id,
                            input=input_text, metadata=metadata or {})
    except Exception:  # noqa: BLE001
        logger.exception("falha ao criar trace Langfuse")
        return None


def log_generation(trace, *, name: str, model: str, input_text: str,
                   output_text: str, usage: dict, latency_ms: int):
    """Uma chamada ao LLM (reasoning step do loop) como generation."""
    if trace is None:
        return
    try:
        trace.span(name=name, input=input_text, output=output_text,
                   metadata={"model": model, "latency_ms": latency_ms, "kind": "reasoning_text"})
    except Exception:  # noqa: BLE001
        logger.exception("falha ao logar generation no Langfuse")


def log_span(trace, *, name: str, input_data=None, output_data=None,
            metadata: dict | None = None):
    """Uma tool call (MongoDB MCP) ou passo não-LLM do turno como span."""
    if trace is None:
        return
    try:
        trace.span(name=name, input=input_data, output=output_data,
                  metadata=metadata or {})
    except Exception:  # noqa: BLE001
        logger.exception("falha ao logar span no Langfuse")


def finish_trace(trace, *, output_text: str | None = None):
    if trace is None:
        return
    try:
        from gateway import _calls
        for call in _calls.get() or []:
            trace.generation(name=call['agent'], model=call['model'],
                             metadata=call,
                             usage={"input": call.get('input_tokens', 0) + call.get('cache_read_tokens', 0) + call.get('cache_write_tokens', 0),
                                    "output": call.get('output_tokens', 0), "unit": "TOKENS"} if call.get('usage_known') else None)
        if output_text is not None:
            trace.update(output=output_text)
    except Exception:  # noqa: BLE001
        logger.exception("falha ao finalizar trace Langfuse")


def trace_url(trace) -> str | None:
    if trace is None:
        return None
    try:
        return trace.get_trace_url()
    except Exception:  # noqa: BLE001
        return None
