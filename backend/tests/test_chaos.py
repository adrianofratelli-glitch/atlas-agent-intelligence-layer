"""Os cenários de caos como REGRESSÃO permanente (só com CHAOS=1).

A bateria de `scripts/chaos_suite.py` é para rodar à mão e gerar o relatório; isto
aqui garante que o que foi corrigido não volte a quebrar. Sem `CHAOS=1` a classe
inteira é pulada — os pontos de injeção ficam inertes e o teste não teria o que
afirmar.

    cd backend && CHAOS=1 .venv/bin/python -m unittest tests.test_chaos -v
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import chaos  # noqa: E402

CHAOS_ON = chaos.enabled()


@unittest.skipUnless(CHAOS_ON, "requer CHAOS=1 (pontos de injeção inertes sem ele)")
class ChaosRegressionTests(unittest.TestCase):
    """Um teste por cenário offline da bateria; o veredito é o mesmo objeto."""

    def _run(self, name):
        import chaos_suite

        verdict = asyncio.run(chaos_suite.SCENARIOS[name]())
        self.assertTrue(verdict.passed, f"{name}: {verdict.assertion}\n{verdict.detail}")
        return verdict

    def test_tool_timeout_is_capped_per_call(self):
        self._run("tool_timeout")

    def test_mcp_session_down_degrades_without_inventing_data(self):
        self._run("mcp_session_down")

    def test_malformed_tool_payload_is_never_reported_as_empty_result(self):
        self._run("tool_malformed_payload")

    def test_tool_circuit_breaker_opens_after_repeated_failures(self):
        self._run("tool_circuit_breaker")

    def test_llm_429_is_absorbed_by_retry(self):
        self._run("llm_429_before_first_token")

    def test_persistent_llm_failure_degrades_the_turn(self):
        self._run("llm_500_persistent")

    def test_legacy_flag_restores_the_old_raising_behaviour(self):
        self._run("legacy_500_flag")

    def test_concurrent_turns_do_not_interfere(self):
        self._run("concurrent_tool_calls")

    def test_turn_deadline_cuts_a_hung_provider(self):
        self._run("turn_timeout")


if __name__ == "__main__":
    unittest.main()
