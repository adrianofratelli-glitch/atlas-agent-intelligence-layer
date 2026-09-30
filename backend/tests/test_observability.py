"""Observabilidade nunca derruba nem contamina o turno.

Dois contratos que a demo depende e que só apareciam como "funcionou na minha
máquina": (1) sem Langfuse configurado — ou com ele FORA DO AR, que foi o caso
neste ambiente — todo método de `langfuse_tracing` é no-op e o turno segue;
(2) o tracing distribuído do `pov-shared` sobe com `TRACE_MASK_PII=1` ESCRITO
pelo código, não apenas recomendado.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import langfuse_tracing  # noqa: E402
import observability  # noqa: E402


class LangfuseFailOpenTests(unittest.TestCase):
    """Langfuse ausente ou indisponível → no-op silencioso, nunca exceção."""

    def setUp(self):
        self._enabled = langfuse_tracing._enabled
        self._client = langfuse_tracing._client

    def tearDown(self):
        langfuse_tracing._enabled = self._enabled
        langfuse_tracing._client = self._client

    def test_without_credentials_start_trace_returns_none(self):
        langfuse_tracing._enabled = False
        langfuse_tracing._client = None
        self.assertIsNone(langfuse_tracing.start_trace(
            name="t", user_id="u", session_id="s", input_text="oi"))

    def test_every_call_on_a_none_trace_is_a_noop(self):
        """É assim que o turno chama quando não há trace: sem if espalhado pelo agent."""
        langfuse_tracing.log_generation(None, name="r", model="m", input_text=None,
                                        output_text="o", usage={}, latency_ms=1)
        langfuse_tracing.log_span(None, name="tool", input_data={}, output_data="")
        langfuse_tracing.finish_trace(None, output_text="fim")
        self.assertIsNone(langfuse_tracing.trace_url(None))

    def test_broken_client_does_not_raise(self):
        """Langfuse no ar mas quebrado: o erro fica no log, o turno continua."""
        class Boom:
            def trace(self, **_kwargs):
                raise RuntimeError("langfuse fora do ar")

        langfuse_tracing._enabled = True
        langfuse_tracing._client = Boom()
        self.assertIsNone(langfuse_tracing.start_trace(
            name="t", user_id="u", session_id="s", input_text="oi"))


class SharedTracingTests(unittest.TestCase):
    """O sink do pov-shared é opt-in, e o mascaramento de PII não é opcional."""

    def setUp(self):
        self._sink = os.environ.get("TRACE_SINK")
        self._mask = os.environ.get("TRACE_MASK_PII")

    def tearDown(self):
        for key, value in (("TRACE_SINK", self._sink), ("TRACE_MASK_PII", self._mask)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_is_off_and_span_is_a_cheap_noop(self):
        os.environ.pop("TRACE_SINK", None)
        self.assertEqual(observability.init_tracing_once(), "off")
        with observability.span("tool.find", **{"tool.name": "find"}) as handle:
            handle.set_attribute("x", 1)   # não pode explodir no caminho quente

    def test_pii_masking_is_forced_by_code_not_by_env(self):
        """Alguém que rode com TRACE_MASK_PII=0 não consegue vazar PII nos spans."""
        os.environ["TRACE_MASK_PII"] = "0"
        os.environ.pop("TRACE_SINK", None)
        observability.init_tracing_once()
        self.assertEqual(os.environ["TRACE_MASK_PII"], "1")


if __name__ == "__main__":
    unittest.main()
