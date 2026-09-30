"""Escolha do limiar de menor erro quando não há separação perfeita."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calibrate_thresholds import _best_with_errors  # noqa: E402


class BestWithErrorsTests(unittest.TestCase):
    def test_outlier_positive_is_reported_not_hidden(self):
        # baixar o limiar até o outlier custaria 3 falsos positivos; ficar acima dele, 1 erro
        pos = [(0.80, "p1"), (0.78, "p2"), (0.67, "outlier")]
        neg = [(0.70, "n1"), (0.69, "n2"), (0.68, "n3")]
        thr, fn, fp = _best_with_errors(pos, neg)
        self.assertGreater(thr, 0.70)
        self.assertLess(thr, 0.78)
        self.assertEqual(fn, ["outlier"])
        self.assertEqual(fp, [])

    def test_tie_prefers_fewer_false_negatives(self):
        # limiar .575 → 1 falso positivo; limiar .80 → 1 falso negativo. Empate em erros:
        # deixar passar turno pessoal (FN) é pior que pular o cache (FP).
        pos = [(0.60, "p_low"), (0.90, "p_hi")]
        neg = [(0.55, "n_low"), (0.70, "n_mid")]
        thr, fn, fp = _best_with_errors(pos, neg)
        self.assertEqual(fn, [])
        self.assertEqual(fp, ["n_mid"])
        self.assertTrue(0.55 < thr < 0.60)

    def test_perfect_separation_has_no_errors(self):
        thr, fn, fp = _best_with_errors([(0.8, "p")], [(0.6, "n")])
        self.assertEqual((fn, fp), ([], []))
        self.assertTrue(0.6 < thr < 0.8)


if __name__ == "__main__":
    unittest.main()
