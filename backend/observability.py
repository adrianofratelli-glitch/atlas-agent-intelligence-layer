"""Observabilidade leve: logs JSON estruturados, request-id e métricas em processo.

Sem dependência externa: um operador consegue responder "o sistema está
degradado?" com (a) logs JSON agregáveis por qualquer coletor, (b) request_id
correlacionando log ↔ resposta, (c) GET /api/metrics com contadores e latência.
Produção pluga OpenTelemetry por cima (os pontos de corte já são os mesmos:
middleware de request + contadores por endpoint).
"""

import json
import logging
import os
import time
from collections import defaultdict

LOG_JSON = os.getenv("LOG_JSON", "0") == "1"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)[-1500:]
        rid = getattr(record, "request_id", None)
        if rid:
            entry["request_id"] = rid
        return json.dumps(entry, ensure_ascii=False)


def setup_logging() -> None:
    """LOG_JSON=1 → stdout em JSON (agregável); default: formato legível."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    if LOG_JSON:
        for handler in logging.getLogger().handlers:
            handler.setFormatter(JsonFormatter())


class Metrics:
    """Contadores e latências por endpoint, no processo (expostos em /api/metrics).

    Suficiente para a PoV e para um scrape simples; produção troca por OTel/
    Prometheus mantendo os mesmos pontos de instrumentação.
    """

    def __init__(self) -> None:
        self.started_at = time.time()
        self.requests: dict[str, int] = defaultdict(int)
        self.errors: dict[str, int] = defaultdict(int)
        self.latency_ms_sum: dict[str, float] = defaultdict(float)
        self.latency_ms_max: dict[str, float] = defaultdict(float)
        self.counters: dict[str, int] = defaultdict(int)

    def observe(self, route: str, status: int, elapsed_ms: float) -> None:
        self.requests[route] += 1
        if status >= 500:
            self.errors[route] += 1
        self.latency_ms_sum[route] += elapsed_ms
        self.latency_ms_max[route] = max(self.latency_ms_max[route], elapsed_ms)

    def bump(self, name: str, value: int = 1) -> None:
        """Contadores de negócio: cache_hit, guardrail_block, llm_fallback..."""
        self.counters[name] += value

    def snapshot(self) -> dict:
        routes = {}
        for route, count in sorted(self.requests.items()):
            routes[route] = {
                "requests": count,
                "errors_5xx": self.errors.get(route, 0),
                "avg_latency_ms": round(self.latency_ms_sum[route] / count, 1),
                "max_latency_ms": round(self.latency_ms_max[route], 1),
            }
        return {
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "routes": routes,
            "counters": dict(sorted(self.counters.items())),
        }

    def prometheus(self) -> str:
        lines = ["# TYPE pov_uptime_seconds gauge", f"pov_uptime_seconds {time.time() - self.started_at:.3f}"]
        for route, count in sorted(self.requests.items()):
            label = route.replace("\\", "\\\\").replace('"', '\\"')
            lines.extend([f'pov_http_requests_total{{route="{label}"}} {count}', f'pov_http_errors_5xx_total{{route="{label}"}} {self.errors.get(route, 0)}', f'pov_http_latency_ms_sum{{route="{label}"}} {self.latency_ms_sum[route]:.3f}', f'pov_http_latency_ms_max{{route="{label}"}} {self.latency_ms_max[route]:.3f}'])
        for name, value in sorted(self.counters.items()):
            label = name.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'pov_business_counter{{name="{label}"}} {value}')
        return "\n".join(lines) + "\n"


metrics = Metrics()


# ---------------------------------------------------------------- tracing (_shared)
# O tracing distribuído vem do pacote comum `pov-shared` (`tracing.init_tracing`),
# não de código local: os PoVs do portfólio têm que produzir spans com o MESMO
# formato para serem comparados lado a lado. O Langfuse (backend/langfuse_tracing.py)
# continua existindo — ele é a visão de PRODUTO do turno (custo/cache na Aba 3);
# isto aqui é a visão de INFRA (span por etapa, latência, erro, sink plugável).
#
# NOTA DE IMPORT: o módulo do _shared se chama `tracing`; o módulo local do
# Langfuse foi renomeado para `langfuse_tracing` justamente para não sombreá-lo,
# já que `backend/` é a raiz do sys.path deste PoV.
#
#     TRACE_SINK=off|console|phoenix|atlas   (default off — zero overhead)
#     TRACE_MASK_PII=1                       forçado aqui, SEMPRE (ver abaixo)

TRACE_SERVICE_NAME = os.getenv("TRACE_SERVICE_NAME", "singleagent")
_tracer = None
_trace_sink = "off"


def init_tracing_once() -> str:
    """Liga o tracing do _shared no startup. Fail-open: nunca derruba o backend.

    `TRACE_MASK_PII=1` é ESCRITO aqui antes de inicializar, não apenas
    recomendado: os spans carregam mensagem do cliente, resultado de tool e
    prompt. Esta PoV mascara PII antes do LLM — o span não pode
    ser a porta dos fundos por onde o dado cru sai do processo. Quem quiser
    conteúdo cru tem que mudar o código, não uma variável de ambiente.
    """
    global _tracer, _trace_sink
    os.environ["TRACE_MASK_PII"] = "1"
    try:
        from tracing import init_tracing  # pov-shared
        _trace_sink = init_tracing(TRACE_SERVICE_NAME)
        if _trace_sink != "off":
            from opentelemetry import trace as _otel
            _tracer = _otel.get_tracer(TRACE_SERVICE_NAME)
    except Exception:  # noqa: BLE001 — observability nunca derruba o processo
        logging.getLogger("poc.observability").warning(
            "tracing do _shared indisponível; seguindo sem spans", exc_info=True)
        _tracer, _trace_sink = None, "off"
    return _trace_sink


def trace_sink() -> str:
    return _trace_sink


def active() -> bool:
    return _tracer is not None


class _NullSpan:
    """Sem sink configurado, `span()` não aloca nada e não muda o caminho quente."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def set_attribute(self, *_args):
        return None


_NULL_SPAN = _NullSpan()


def span(name: str, **attributes):
    """Um passo do turno (tool, chamada de LLM, leitura/escrita de memória).

    Os atributos-base são os mesmos do PoV multiagente onde fazem sentido
    (`tool.name`, `agent.name`, `area`, `user_key` já mascarado por chave opaca),
    para os dois relatórios falarem a mesma língua.
    """
    if _tracer is None:
        return _NULL_SPAN
    return _tracer.start_as_current_span(
        name, attributes={k: v for k, v in attributes.items() if v is not None})
