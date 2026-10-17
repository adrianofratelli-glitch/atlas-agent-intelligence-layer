"""Memória longa para todos (2026-10-16): fatos só entram no prompt em pergunta sobre o cliente.

Pergunta genérica sem 1ª pessoa passa pelo classificador de turno; o resto usa a memória
(falha fechado = pessoal). Garante que o roteiro "cache por área" (Ana) continua gravando.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-import-only")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_graph  # noqa: E402
import seed  # noqa: E402


def relevance(msg, personal_turn=False, verdict=None, fail=False):
    async def fake(text):
        if fail:
            raise RuntimeError("índice fora")
        return {"personal": verdict, "score": 0.5, "threshold": 0.7162}
    with mock.patch.object(agent_graph.turn_classifier, "classify", fake):
        return asyncio.run(agent_graph._memory_relevance(msg, personal_turn))


class MemoryGateTests(unittest.TestCase):
    def test_first_person_or_phrase_gate_keeps_memory_without_classifier(self):
        for msg in ("Me indica um fone bom pra eu usar no meu dia a dia?", "qual cor eu gosto?",
                    "o MEU limite mudou?"):
            self.assertIsNone(relevance(msg, verdict=False), msg)
        self.assertIsNone(relevance("Vocês entregam para todo o Brasil?", personal_turn=True))

    def test_generic_question_goes_to_classifier(self):
        self.assertEqual(relevance("Vocês entregam para todo o Brasil?", verdict=False)["personal"], False)
        self.assertEqual(relevance("Vocês entregam para todo o Brasil?", verdict=True)["personal"], True)

    def test_classifier_failure_is_fail_closed(self):
        self.assertIsNone(relevance("Qual o prazo de garantia?", fail=True))

    def test_every_demo_user_has_seeded_facts_without_pii(self):
        users = {u["user_key"] for u in seed.APP_USERS}
        self.assertEqual({f["user_key"] for f in seed.SEED_MEMORY}, users)
        ids = [f["_id"] for f in seed.SEED_MEMORY]
        self.assertEqual(len(ids), len(set(ids)))
        import guardrails
        for f in seed.SEED_MEMORY:
            self.assertEqual(guardrails.mask_pii(f["fact"]).text, f["fact"])
            self.assertFalse(agent_graph.memory.looks_like_instruction(f["fact"]), f["fact"])


if __name__ == "__main__":
    unittest.main()
