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


if __name__ == "__main__":
    unittest.main()
