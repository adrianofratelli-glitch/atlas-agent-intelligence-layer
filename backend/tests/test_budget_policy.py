"""Orçamento do cliente aplicado pelo SERVIDOR na busca de catálogo (não pelo prompt)."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import agent  # noqa: E402


def _catalog_input(limit=3):
    return {"database": "POC", "collection": "produtos_vector", "connectionId": "x",
            "pipeline": [{"$vectorSearch": {"index": "produtos_vector", "path": "descricao",
                                            "query": "notebook", "numCandidates": 100,
                                            "limit": limit}}]}


def _rewrite(budget=None):
    tool_input = _catalog_input()
    denial = agent._read_denial("aggregate", "POC.produtos_vector", tool_input,
                                "conv", "user", budget_brl=budget)
    return denial, tool_input["pipeline"]


class BudgetPolicyTests(unittest.TestCase):
    def test_without_budget_pipeline_is_unchanged_shape(self):
        denial, pipeline = _rewrite(None)
        self.assertIsNone(denial)
        self.assertEqual([next(iter(s)) for s in pipeline], ["$vectorSearch", "$project"])
        self.assertEqual(pipeline[0]["$vectorSearch"]["limit"], 3)

    def test_budget_becomes_native_prefilter_on_the_vector_index(self):
        denial, pipeline = _rewrite(800)
        self.assertIsNone(denial)
        # pré-filtro NATIVO (preco é campo filter do índice): sem $match, sem janela larga
        self.assertEqual([next(iter(s)) for s in pipeline], ["$vectorSearch", "$project"])
        vector = pipeline[0]["$vectorSearch"]
        self.assertEqual(vector["filter"], {"preco": {"$lte": 800.0}})
        self.assertEqual(vector["limit"], 3)
        self.assertEqual(pipeline[1], {"$project": {"nome": 1, "preco": 1, "_id": 0}})

    def test_model_cannot_override_budget_with_its_own_filter(self):
        tool_input = _catalog_input()
        vector = tool_input["pipeline"][0]["$vectorSearch"]
        vector["filter"] = {"preco": {"$lte": 999999}}
        tool_input["pipeline"].append({"$match": {"preco": {"$lte": 999999}}})
        agent._read_denial("aggregate", "POC.produtos_vector", tool_input,
                           "conv", "user", budget_brl=500)
        pipeline = tool_input["pipeline"]
        self.assertEqual(pipeline[0]["$vectorSearch"]["filter"], {"preco": {"$lte": 500.0}})
        self.assertNotIn("$match", [next(iter(s)) for s in pipeline])

    def test_model_supplied_filter_is_dropped_without_budget(self):
        tool_input = _catalog_input()
        tool_input["pipeline"][0]["$vectorSearch"]["filter"] = {"categoria": "x"}
        agent._read_denial("aggregate", "POC.produtos_vector", tool_input, "conv", "user")
        self.assertNotIn("filter", tool_input["pipeline"][0]["$vectorSearch"])

    def test_invalid_budget_values_are_ignored(self):
        for bad in (0, -10, float("nan"), float("inf"), "800"):
            with self.subTest(bad=bad):
                _, pipeline = _rewrite(bad)
                self.assertNotIn("filter", pipeline[0]["$vectorSearch"])


if __name__ == "__main__":
    unittest.main()
