"""answer_checks: a resposta afirma o que o turno não fez? (função pura, offline)

Casos reais vieram dos turnos de 2026-11-12 no `POC_test` (Grove, Sonnet 4.5).
"""

import os
import sys
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-import-only")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import answer_checks as ac  # noqa: E402

# Resposta real (PED-1003, só `find` no turno).
STATUS_ANSWER = ("Encontrei o seu pedido PED-1003. Status atual: Em trânsito. Vou abrir um chamado "
                 "urgente para investigar o que aconteceu com a transportadora. Você deve receber um "
                 "retorno do nosso time de logística em até 24 horas úteis no e-mail cadastrado.")
GROUNDING = ("Regras da área: prazo padrão de entrega até 10 dias úteis capitais, até 15 dias úteis "
             "interior. Resultado: {order_id: PED-1003, status: em_transito} "
             "Cadeia: {path: [PED-1005, PED-1006, PED-1007], replacements: 2}")


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
                self.assertEqual(rules(ac.check(text, writes_ok=0, grounding="PED-1002")),
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


class NegationTests(unittest.TestCase):
    def test_honest_negative_report_is_not_a_claim(self):
        for text in ("Não abri nenhum chamado ainda.", "Ainda não registrei a troca.",
                     "Eu nunca encaminhei esse caso."):
            with self.subTest(text=text):
                self.assertEqual(ac.check(text, writes_ok=0, grounding=""), [])


# Resposta real do `single-005` (rodada 3 de 2026-11-12): a regra 4b fez o agente
# terminar com a oferta, mas a promessa continuou na frase anterior.
SINGLE_005 = ("O pedido PED-1005 já teve **2 reposições** (PED-1006 e PED-1007), todas com o mesmo "
              "defeito. Vou encaminhar seu caso para análise de qualidade, porque trocar de novo o "
              "mesmo item tende a repetir o problema. Posso abrir esse chamado de análise de "
              "qualidade para você agora?")


class RewriteTests(unittest.TestCase):
    """ADR-014: promessa sem escrita vira oferta antes de chegar ao cliente."""

    def test_real_single_005_answer_becomes_an_offer_and_keeps_the_facts(self):
        text, changes = ac.rewrite_unbacked(SINGLE_005, writes_ok=0)
        self.assertEqual(len(changes), 1)
        self.assertIn("Se você quiser, posso encaminhar seu caso para análise de qualidade", text)
        self.assertNotIn("Vou encaminhar", text)
        self.assertIn("**2 reposições**", text, "o resto da resposta fica intacto")
        self.assertIn("Posso abrir esse chamado", text)
        self.assertEqual(ac.check(text, writes_ok=0, grounding=GROUNDING), [], "a resposta entregue passa")
        self.assertEqual(changes[0]["action"], "rewrite")
        self.assertIn("Vou encaminhar", changes[0]["before"])

    def test_status_answer_keeps_the_deadline_flag(self):
        text, changes = ac.rewrite_unbacked(STATUS_ANSWER, writes_ok=0)
        self.assertEqual(len(changes), 1)
        self.assertIn("Se você quiser, posso abrir um chamado urgente", text)
        self.assertEqual(rules(ac.check(text, writes_ok=0, grounding=GROUNDING)), ["prazo_sem_fonte"],
                         "prazo inventado não é reescrito: segue apontado")

    def test_tenses_accents_markup_and_mid_sentence(self):
        cases = {
            "Já abri o chamado para você.": "Se você quiser, posso abrir o chamado para você.",
            "JÁ ABRI o chamado.": "Se você quiser, posso abrir o chamado.",
            "Registrei a sua solicitação de troca.": "Se você quiser, posso registrar a sua solicitação de troca.",
            "Entendo, e eu vou encaminhar seu caso.": "Entendo, e se você quiser, posso encaminhar seu caso.",
            "- **Acabei de abrir** um chamado.": "- **Se você quiser, posso abrir** um chamado.",
            "Registrei a troca e vou abrir um chamado.":
                "Se você quiser, posso registrar a troca e posso abrir um chamado.",
        }
        for before, after in cases.items():
            with self.subTest(text=before):
                text, changes = ac.rewrite_unbacked(before, writes_ok=0)
                self.assertEqual(text, after)
                self.assertTrue(changes)
                self.assertEqual(ac.check(text, writes_ok=0, grounding=""), [])

    def test_nothing_changes_when_the_write_ran_or_there_is_no_claim(self):
        self.assertEqual(ac.rewrite_unbacked(SINGLE_005, writes_ok=1), (SINGLE_005, []))
        for text in ("Posso abrir um chamado para você?", "Não abri nenhum chamado.",
                     "O status do pedido é em trânsito.\n\nPosso ajudar em algo mais?", ""):
            with self.subTest(text=text):
                self.assertEqual(ac.rewrite_unbacked(text, writes_ok=0), (text, []))

    def test_line_breaks_and_other_sentences_are_preserved(self):
        text = "Status: em trânsito.\n\nVou abrir um chamado.\n- item"
        out, _ = ac.rewrite_unbacked(text, writes_ok=0)
        self.assertEqual(out, "Status: em trânsito.\n\nSe você quiser, posso abrir um chamado.\n- item")

    def test_revert_flag(self):
        with unittest.mock.patch.dict(os.environ, {"ANSWER_REWRITE": "0"}):
            self.assertFalse(ac.rewrite_enabled())
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANSWER_REWRITE", None)
            self.assertTrue(ac.rewrite_enabled())


class UngroundedOrderTests(unittest.TestCase):
    """`pedido_sem_fonte`: número de pedido que o modelo não viu em lugar nenhum."""

    # Resposta real do `single-009` na rodada de base de 2026-11-13 (0 ferramentas no turno).
    INVENTED = ("Vejo que você tem alguns pedidos conosco:\n- **PED-2001** – Fone Bluetooth Premium\n"
                "- **PED-2002** – Mouse Ergonômico Wireless\n- **PED-2002** de novo")

    def test_invented_orders_are_flagged_once_each(self):
        flags = ac.check(self.INVENTED, writes_ok=0, grounding="prompt com PED-0000 de exemplo")
        self.assertEqual([(f["rule"], f["detail"]) for f in flags],
                         [("pedido_sem_fonte", "PED-2001"), ("pedido_sem_fonte", "PED-2002")])

    def test_orders_from_message_tool_result_or_denial_hint_are_grounded(self):
        grounding = "Onde está o ped-1003?\nPedidos disponíveis: PED-1001 (Tour One), PED-1004"
        self.assertEqual(ac.check("O PED-1003 está em trânsito; o PED-1001 e o PED-1004 também.",
                                  writes_ok=0, grounding=grounding), [])


class MemoryNoteTests(unittest.TestCase):
    def test_session_note_points_to_the_real_database(self):
        """Achado de 2026-11-13: a nota citava `POC.agent_sessions` fixo; no banco de teste a
        leitura era negada e o agente inventava o resumo da conversa (`single-022`)."""
        import agent
        note = agent._memory_note("conv_x")
        self.assertIn(f"{agent.DB_MAIN}.agent_sessions", note)
        self.assertIn(f'database "{agent.DB_MAIN}"', note)
        self.assertEqual(agent.SESSIONS_TARGET, f"{agent.DB_MAIN}.agent_sessions")
        self.assertIn("acabou de começar", note)


class DropUngroundedOrderTests(unittest.TestCase):
    # Resposta real do `single-009` na rodada 2 de 2026-11-13 (0 ferramentas no turno).
    LIVE = ("Essa pergunta está fora do escopo do meu atendimento.\n\n"
            "Agora, sobre seus pedidos na loja: você possui atualmente **4 pedidos** cadastrados:\n\n"
            "- **PED-8901** - Fone Bluetooth Premium\n- **PED-8902** - Mouse Ergonômico\n"
            "- **PED-8903** - Teclado Mecânico RGB\n- **PED-8904** - Webcam Full HD\n\n"
            "Posso ajudá-lo com algum desses pedidos ou com outra questão relacionada à loja?")

    def test_invented_list_and_its_intro_give_place_to_the_real_list(self):
        text, ids = ac.drop_ungrounded_orders(self.LIVE, grounding="",
                                              replacement="Seus pedidos registrados:\n- PED-1001 — Tour One")
        self.assertEqual(ids, ["PED-8901", "PED-8902", "PED-8903", "PED-8904"])
        self.assertNotIn("PED-89", text)
        self.assertNotIn("4 pedidos", text)
        self.assertEqual(text, "Essa pergunta está fora do escopo do meu atendimento.\n\n"
                               "Seus pedidos registrados:\n- PED-1001 — Tour One\n\n"
                               "Posso ajudá-lo com algum desses pedidos ou com outra questão "
                               "relacionada à loja?")

    def test_inline_sentence_is_dropped_and_grounded_order_stays(self):
        text, ids = ac.drop_ungrounded_orders(
            "O PED-1003 está em trânsito. Você também tem o PED-9999 em aberto. Algo mais?",
            grounding="PED-1003", replacement="R")
        self.assertEqual((text, ids), ("O PED-1003 está em trânsito. Algo mais?\nR", ["PED-9999"]))

    def test_nothing_invented_nothing_changes(self):
        self.assertEqual(ac.drop_ungrounded_orders("O PED-1003 chegou.", grounding="ped-1003",
                                                   replacement="R"), ("O PED-1003 chegou.", []))


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


class TurnWiringTests(unittest.TestCase):
    """agent_graph._answer_fidelity: o cliente recebe a oferta; trace e auditoria ficam com o original."""

    def _run(self, answer, writes_ok=0, env=None):
        import asyncio
        import agent_graph

        logged = []

        async def fake_log(text, flags, user_key, session_id, area="default", action="flag"):
            logged.append((action, text, [f["rule"] for f in flags]))

        trace, metrics, guard_out = [], {"order_writes_ok": writes_ok}, {}
        with unittest.mock.patch.object(agent_graph.guardrails, "log_answer_flags", fake_log), \
                unittest.mock.patch.dict(os.environ, env or {}):
            out = asyncio.run(agent_graph._answer_fidelity(
                {}, trace, metrics, guard_out, answer, grounding=GROUNDING,
                user_key="cliente-demo", conversation_id="c1", area="default"))
        return out, trace, metrics, guard_out, logged

    def test_promise_is_rewritten_and_audited_with_the_original(self):
        out, trace, metrics, guard_out, logged = self._run(SINGLE_005)
        self.assertNotIn("Vou encaminhar", out)
        self.assertEqual(guard_out["answer_checks"], [])
        self.assertEqual(len(guard_out["answer_rewrites"]), 1)
        self.assertEqual(metrics["answer_rewrites"], 1)
        self.assertEqual([e["action"] for e in trace], ["rewrite"])
        self.assertEqual(logged[0][0], "rewrite")
        self.assertIn("Vou encaminhar", logged[0][1], "auditoria guarda o texto do modelo")

    def test_deadline_still_flagged_after_rewrite(self):
        out, trace, _, guard_out, logged = self._run(STATUS_ANSWER)
        self.assertEqual([e["action"] for e in trace], ["rewrite", "flag"])
        self.assertEqual([f["rule"] for f in guard_out["answer_checks"]], ["prazo_sem_fonte"])
        self.assertEqual([a for a, _, _ in logged], ["rewrite", "flag"])

    def test_revert_flag_only_points(self):
        out, trace, _, guard_out, _ = self._run(SINGLE_005, env={"ANSWER_REWRITE": "0"})
        self.assertEqual(out, SINGLE_005)
        self.assertEqual(guard_out["answer_rewrites"], [])
        self.assertEqual([f["rule"] for f in guard_out["answer_checks"]], ["promessa_sem_escrita"])

    def test_invented_orders_are_replaced_by_the_owner_real_orders(self):
        import agent_graph
        import guidance

        async def orders(user_key):
            return [{"order_id": "PED-1001", "product_name": "Tour One", "status": "entregue"}]

        with unittest.mock.patch.object(guidance, "user_orders", orders):
            out, trace, metrics, guard_out, logged = self._run(DropUngroundedOrderTests.LIVE)
        self.assertNotIn("PED-8901", out)
        self.assertIn("- PED-1001 — Tour One (status: entregue)", out)
        self.assertEqual(guard_out["answer_checks"], [], "a lista real não é apontada como sem fonte")
        self.assertEqual([r["rule"] for r in guard_out["answer_rewrites"]], ["pedido_sem_fonte"])
        self.assertIn("PED-8901", logged[0][1], "auditoria guarda o texto do modelo")

    def test_broad_listing_denial_never_gets_a_list(self):
        import agent_graph
        out = asyncio_run(agent_graph._real_orders_text(
            "cliente-demo", "[orientação da plataforma] NÃO liste os pedidos dele nesta resposta"))
        self.assertNotIn("PED-1", out)

    def test_write_in_the_turn_leaves_the_answer_alone(self):
        out, trace, _, guard_out, logged = self._run(SINGLE_005, writes_ok=1)
        self.assertEqual((out, trace, logged), (SINGLE_005, [], []))


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


if __name__ == "__main__":
    unittest.main()
