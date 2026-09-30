"""Classificador de turno: limiar, sem vizinho e falha FECHADO."""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import turn_classifier as tc  # noqa: E402


def classify_with(agg_result=None, agg_error=None, threshold=0.72):
    agg = AsyncMock(side_effect=agg_error) if agg_error else AsyncMock(return_value=agg_result)
    with patch.object(tc, "get_threshold", AsyncMock(return_value=threshold)), \
         patch.object(tc, "aggregate_list", agg), \
         patch.object(tc, "ai_brain", lambda: {tc.PROBES_COLLECTION: object()}):
        return asyncio.run(tc.classify("como você me chama?"))


class TurnClassifierTests(unittest.TestCase):
    def test_score_at_or_above_threshold_is_personal(self):
        for score in (0.72, 0.8):
            with self.subTest(score=score):
                out = classify_with([{"phrase": "com qual nome você me trata?", "score": score}])
                self.assertTrue(out["personal"])
                self.assertFalse(out["error"])
                self.assertEqual(out["nearest"], "com qual nome você me trata?")

    def test_score_below_threshold_is_generic(self):
        out = classify_with([{"phrase": "x", "score": 0.6}])
        self.assertFalse(out["personal"])
        self.assertFalse(out["error"])

    def test_no_neighbour_is_generic(self):
        out = classify_with([])
        self.assertFalse(out["personal"])
        self.assertEqual(out["score"], 0.0)

    def test_search_failure_fails_closed(self):
        out = classify_with(agg_error=RuntimeError("índice fora do ar"))
        self.assertTrue(out["personal"])   # sem cache quando não dá para ter certeza
        self.assertTrue(out["error"])

    def test_missing_config_falls_back_to_documented_default(self):
        class Boom:
            def __getitem__(self, _):
                raise RuntimeError("sem config")
        with patch.object(tc, "ai_brain", lambda: Boom()):
            self.assertEqual(asyncio.run(tc.get_threshold()), tc.DEFAULT_THRESHOLD)

    def test_probes_are_unique_and_nonempty(self):
        self.assertEqual(len(tc.PERSONAL_PROBES), len(set(tc.PERSONAL_PROBES)))
        self.assertTrue(all(p.strip() for p in tc.PERSONAL_PROBES))


if __name__ == "__main__":
    unittest.main()
