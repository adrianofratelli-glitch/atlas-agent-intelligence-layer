"""A2: o evento de memória do trace carrega "por que este fato foi lembrado".

O bloco `memory_retrieval` vai para POC.agent_traces e para o inspector; formato inesperado
do scoreDetails vira `explain: None` (detalhe indisponível), nunca erro.
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-import-only")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_graph  # noqa: E402
import memory  # noqa: E402


def _ltm(mode, facts, retrieval=None):
    return {"user_key": "u", "total_active": 30, "mode": mode, "facts": facts,
            "retrieval": retrieval or {"strategy": mode}}


class MemoryExplainTraceTests(unittest.TestCase):
    def test_block_carries_strategy_config_and_per_leg_explain(self):
        if memory._ar is None:
            self.skipTest("pov-shared ausente")
        doc = {"_id": "a", "fact": "Prefere e-mail", "fusion_score": 0.0328,
               "score_details": {"value": 0.0328, "details": [
                   {"inputPipelineName": "vector", "rank": 1, "weight": 1.0, "value": 0.8},
                   {"inputPipelineName": "bm25", "rank": "NA"}]}}
        fact = memory._fact_with_score(doc)
        block = agent_graph._memory_explain(
            _ltm("rank_fusion", [fact], {"strategy": "rank_fusion",
                                         "fusion": {"weights": {"vector": 1, "bm25": 1}},
                                         "rerank": {"enabled": False, "status": "off"},
                                         "fallbacks": [], "search_ms": 412.0}),
            {"strategy": "rank_fusion", "fusion": {"weights": {"vector": 1, "bm25": 1}},
             "rerank": {"enabled": False, "status": "off"}, "fallbacks": [], "search_ms": 412.0})
        self.assertEqual(block["strategy"], "rank_fusion")
        legs = block["facts"][0]["explain"]["legs"]
        self.assertEqual([(l["name"], l["rank"]) for l in legs], [("vector", 1), ("bm25", None)])
        self.assertAlmostEqual(legs[0]["contribution"], 1 / 61, places=6)
        self.assertEqual(block["facts"][0]["score"], 0.0328)

    def test_unexpected_details_and_fallback_modes_never_raise(self):
        for raw in ("texto", {"details": "x"}, None, {"details": [{"rank": 1}]}):
            fact = memory._fact_with_score({"_id": "b", "fact": "x", "score_details": raw,
                                            "fusion_score": float("nan")})
            self.assertIsNone(fact["explain"])
            block = agent_graph._memory_explain(_ltm("app_rrf", [fact]), {})
            self.assertEqual(block["strategy"], "app_rrf")
            self.assertIsNone(block["facts"][0]["explain"])

    def test_fact_text_is_capped_and_labels_cover_every_mode(self):
        fact = {"_id": "c", "fact": "y" * 1000}
        block = agent_graph._memory_explain(_ltm("recent", [fact] * 12), {})
        self.assertEqual(len(block["facts"]), 8)
        self.assertEqual(len(block["facts"][0]["fact"]), 120)
        for mode in memory.MODES:
            if mode not in ("all", "recent"):
                self.assertIn(mode, agent_graph._MEMORY_TOOL_LABEL)
                self.assertIn(mode, agent_graph._MEMORY_MODE_TEXT)


if __name__ == "__main__":
    unittest.main()
