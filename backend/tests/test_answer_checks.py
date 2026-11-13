"""answer_checks: a resposta afirma o que o turno não fez? (função pura, offline)

Casos reais vieram dos turnos de 2026-11-12 no `POC_test` (Grove, Sonnet 4.5).
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-import-only")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import answer_checks as ac  # noqa: E402

# Resposta real (PED-1003, só `find` no turno).
STATUS_ANSWER = ("Encontrei o seu pedido PED-1003. Status atual: Em trânsito. Vou abrir um chamado "
                 "urgente para investigar o que aconteceu com a transportadora. Você deve receber um "
                 "retorno do nosso time de logística em até 24 horas úteis no e-mail cadastrado.")
GROUNDING = ("Regras da área: prazo padrão de entrega até 10 dias úteis capitais, até 15 dias úteis "
             "interior. Resultado: {order_id: PED-1003, status: em_transito}")


def rules(flags):
    return [f["rule"] for f in flags]


class UnbackedCommitmentTests(unittest.TestCase):
    def test_real_status_answer_is_flagged_twice(self):
        flags = ac.check(STATUS_ANSWER, writes_ok=0, grounding=GROUNDING)
        self.assertEqual(rules(flags), ["promessa_sem_escrita", "prazo_sem_fonte"])

    def test_same_claim_is_fine_when_the_write_ran(self):
        flags = ac.check(STATUS_ANSWER, writes_ok=1, grounding=GROUNDING)
        self.assertEqual(rules(flags), ["prazo_sem_fonte"], "a escrita respalda a ação, não o prazo")

    def test_claims_in_several_tenses_and_accents(self):
        for text in ("Já abri o chamado para você.", "Registrei a sua solicitação de troca.",
                     "Solicitei o reembolso do pedido PED-1002.", "Encaminhei o caso para a qualidade.",
                     "ACABEI DE ABRIR UM CHAMADO.", "Atualizei o status do seu pedido."):
            with self.subTest(text=text):
                self.assertEqual(rules(ac.check(text, writes_ok=0, grounding="")),
                                 ["promessa_sem_escrita"])

    def test_offers_and_existing_state_are_not_claims(self):
        for text in ("Posso abrir um chamado para você, quer que eu siga?",
                     "Já existe uma solicitação de reembolso registrada no sistema.",
                     "Se quiser, eu registro a troca agora.",
                     "Em abril o pedido foi entregue.",
                     "O status do pedido é em trânsito."):
            with self.subTest(text=text):
                self.assertEqual(ac.check(text, writes_ok=0, grounding=""), [])

    def test_empty_answer_has_nothing_to_check(self):
        self.assertEqual(ac.check("", writes_ok=0, grounding=""), [])
        self.assertEqual(ac.check("   ", writes_ok=0, grounding=""), [])


class DeadlineTests(unittest.TestCase):
    def test_deadline_from_area_rules_is_grounded(self):
        self.assertEqual(ac.check("A entrega ocorre em até 10 dias úteis.", writes_ok=0,
                                  grounding=GROUNDING), [])

    def test_deadline_from_tool_result_is_grounded(self):
        self.assertEqual(ac.check("O estorno cai em até 7 dias úteis.", writes_ok=0,
                                  grounding="FAQ: reembolso em até 7 dias úteis após a aprovação"), [])

    def test_hour_abbreviation_matches_both_ways(self):
        self.assertEqual(ac.check("Retorno em até 24h.", writes_ok=0,
                                  grounding="SLA de 24 horas"), [])
        self.assertEqual(rules(ac.check("Retorno em até 48h.", writes_ok=0,
                                        grounding="SLA de 24 horas")), ["prazo_sem_fonte"])

    def test_number_must_match_not_only_unit(self):
        flags = ac.check("O reembolso sai em 3 dias úteis.", writes_ok=0, grounding=GROUNDING)
        self.assertEqual(rules(flags), ["prazo_sem_fonte"])

    def test_past_durations_and_counts_are_not_deadlines(self):
        for text in ("Já se passaram 5 meses desde o despacho.", "Faltam 43 dias para o Natal.",
                     "Você comprou 2 fones."):
            with self.subTest(text=text):
                self.assertEqual(ac.check(text, writes_ok=0, grounding=""), [])

    def test_repeated_deadline_is_reported_once(self):
        text = "Retorno em até 24 horas. Repito: em até 24 horas."
        self.assertEqual(len(ac.check(text, writes_ok=0, grounding="")), 1)


if __name__ == "__main__":
    unittest.main()


class EvalAnswerFailureTests(unittest.TestCase):
    """O eval reprova pelo conteúdo (eval_agent.answer_failures), não só pela rota."""

    def setUp(self):
        import eval_agent
        self.fail = eval_agent.answer_failures

    def _result(self, flags=()):
        return {"guardrail": {"output": {"answer_checks": [{"rule": r} for r in flags]}}}

    def test_flags_from_the_turn_fail_the_case(self):
        out = self.fail({}, "ok", {}, self._result(["promessa_sem_escrita"]))
        self.assertEqual(out, ["resposta_sem_respaldo:promessa_sem_escrita"])

    def test_expected_content_is_accent_and_case_insensitive(self):
        case = {"expect_answer_any": ["trânsito"]}
        self.assertEqual(self.fail(case, "Seu pedido está EM TRANSITO.", {}, self._result()), [])
        self.assertEqual(self.fail(case, "Seu pedido foi entregue.", {}, self._result()),
                         ["conteudo_esperado_ausente"])

    def test_forbidden_content(self):
        case = {"forbid_answer_any": ["529.982.247-25"]}
        self.assertEqual(self.fail(case, "Seu CPF 529.982.247-25 confere.", {}, self._result()),
                         ["conteudo_proibido"])

    def test_write_expectations_use_successful_writes_only(self):
        self.assertEqual(self.fail({"expect_write": True}, "x", {"order_writes_ok": 0}, self._result()),
                         ["sem_escrita_esperada"])
        self.assertEqual(self.fail({"expect_no_write": True}, "x", {"order_writes_ok": 1}, self._result()),
                         ["escrita_indevida"])
        self.assertEqual(self.fail({"expect_write": True}, "x", {"order_writes_ok": 1}, self._result()), [])

    def test_tool_evidence_reads_tool_calls_from_the_trace(self):
        import eval_agent
        trace = [{"phase": "retrieve", "kind": "tool_call", "tool": "find", "result": "PED-1003 em_transito"},
                 {"phase": "act", "kind": "message", "text": "resposta"}]
        self.assertEqual(eval_agent.tool_evidence({"trace": trace}), "[find] PED-1003 em_transito")


class WriteTookEffectTests(unittest.TestCase):
    """`order_writes_ok` só conta update-many que casou um pedido (texto do MCP 2.1.0)."""

    def test_formats_of_the_mcp_server(self):
        import agent
        cases = {
            "Matched 1 document(s). Modified 1 document(s).": True,
            "Matched 1 document(s). Modified 0 document(s).": True,   # já estava no status
            "Matched 0 document(s).": False,
            "No documents matched the filter.": False,
            "Updated 0": False,                                      # formato desconhecido
            "": False,
            None: False,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIs(agent.write_took_effect(text), expected)
