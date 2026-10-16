"""O isolamento por tenant é a cláusula `filter` da query, não o índice.

Sonda do juiz (2026-10-08): `$vectorSearch` sem `filter` em `agent_memory_vs` é
aceito pelo Atlas e devolve documentos — o campo `filter` do índice só torna o
pré-filtro eficiente. Por isso toda busca vetorial em coleção por tenant nasce em
`db.tenant_vector_stage`, que recusa montar a query sem a chave do tenant, e este
teste falha se algum módulo do runtime montar `$vectorSearch` por fora dela.
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-import-only")
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import db  # noqa: E402

# Módulos que podem montar `$vectorSearch` à mão, com o motivo:
ALLOWED_RAW = {
    "db.py": "o próprio construtor",
    "agent.py": "catálogo de produtos (dado compartilhado, sem tenant); teto de preço é filtro de servidor",
    "calibrate_thresholds.py": "ferramenta offline de calibração, fora do caminho de request",
    "turn_classifier.py": "probes globais do classificador de turno, sem tenant",
}


class TenantFilterTests(unittest.TestCase):
    def test_stage_refuses_missing_tenant_key(self):
        for bad in (None, {}, {"user_key": None}, {"user_key": ""}, {"user_key": "u", "active": None}):
            with self.subTest(bad=bad), self.assertRaises(db.TenantFilterMissing):
                db.tenant_vector_stage(index="agent_memory_vs", path="fact", query="q",
                                       tenant_filter=bad, num_candidates=10, limit=1)

    def test_stage_carries_the_filter(self):
        st = db.tenant_vector_stage(index="agent_memory_vs", path="fact", query="q",
                                    tenant_filter={"user_key": "u", "active": True},
                                    num_candidates=10, limit=1)["$vectorSearch"]
        self.assertEqual(st["filter"], {"user_key": "u", "active": True})

    def test_no_runtime_module_builds_raw_vector_search(self):
        offenders = []
        for path in sorted(BACKEND.glob("*.py")):
            if path.name in ALLOWED_RAW:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if '"$vectorSearch":' in code or "'$vectorSearch':" in code:
                    offenders.append(f"{path.name}:{n}")
        self.assertEqual(offenders, [], "use db.tenant_vector_stage para coleções por tenant")


class HybridTenantGuardTests(unittest.TestCase):
    """$rankFusion e $rerank (2026-10) não podem fugir do guard: as DUAS pernas levam o filtro
    de tenant no servidor e o $rerank só reordena o que a fusão filtrada devolveu."""

    def setUp(self):
        import memory
        if memory._ar is None:
            self.skipTest("pov-shared ausente")
        self.memory = memory
        self.fusion, self.rerank, _ = memory.parse_retrieval_config(None)

    def _pipeline(self, user_key, rerank=None):
        return self.memory.rank_fusion_pipeline(user_key, "q", self.fusion, rerank)

    def test_both_legs_carry_tenant_filter(self):
        stage = self._pipeline("u")[0]["$rankFusion"]
        legs = stage["input"]["pipelines"]
        self.assertEqual(set(legs), {"vector", "bm25"})
        self.assertEqual(legs["vector"][0]["$vectorSearch"]["filter"],
                         {"user_key": "u", "active": True})
        flt = legs["bm25"][0]["$search"]["compound"]["filter"]
        self.assertIn({"equals": {"path": "user_key", "value": "u"}}, flt)
        self.assertIn({"equals": {"path": "active", "value": True}}, flt)

    def test_empty_tenant_refused_in_every_leg(self):
        for bad in ("", None):
            with self.subTest(bad=bad), self.assertRaises(db.TenantFilterMissing):
                self._pipeline(bad)
            with self.subTest(bad=bad, leg="bm25"), self.assertRaises(db.TenantFilterMissing):
                self.memory._bm25_leg(bad, "q", self.fusion)

    def test_rerank_only_after_filtered_fusion(self):
        rr = dict(self.rerank, enabled=True)
        pipeline = self._pipeline("u", rr)
        ops = [next(iter(st)) for st in pipeline]
        self.assertEqual(ops[0], "$rankFusion")
        self.assertIn("$rerank", ops)
        self.assertLess(ops.index("$rankFusion"), ops.index("$rerank"))
        for leg in pipeline[0]["$rankFusion"]["input"]["pipelines"].values():
            self.assertNotIn("$rerank", [next(iter(st)) for st in leg])

    def test_no_runtime_module_builds_raw_fusion_or_rerank(self):
        offenders = []
        for path in sorted(BACKEND.glob("*.py")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                for op in ("$rankFusion", "$rerank"):
                    if f'"{op}":' in code or f"'{op}':" in code:
                        offenders.append(f"{path.name}:{n}:{op}")
        self.assertEqual(offenders, [], "use os builders do atlas_retrieval (pov-shared)")

    def test_lexical_search_on_memory_only_in_guarded_leg(self):
        hits = [n for n, line in enumerate(
            (BACKEND / "memory.py").read_text(encoding="utf-8").splitlines(), 1)
            if '"$search":' in line.split("#", 1)[0]]
        self.assertEqual(len(hits), 1, "$search em agent_memory só via memory._bm25_leg")


if __name__ == "__main__":
    unittest.main()
