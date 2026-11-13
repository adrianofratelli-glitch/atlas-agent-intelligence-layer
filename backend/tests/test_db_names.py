"""Nome de banco nunca fixo no que o modelo, a UI ou o operador leem (achado de 2026-11-13).

`_memory_note` citava `POC.agent_sessions` literal: com `MONGODB_DB=POC_test` a leitura era
negada e o agente inventava o resumo da conversa. A revisão achou a mesma classe em outros
pontos (prompt, memória, rótulos da API, calibração). Este teste varre o código de runtime.
"""

import ast
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-import-only")
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_LITERAL = re.compile(r'\bPOC\.[a-z_]|\bai_brain\.[a-z_]|"POC"|\'POC\'|"ai_brain"')
# O template do prompt cita "POC" de propósito: `agent._render_system` troca pelo banco real
# (coberto em `test_rendered_prompt_names_only_the_configured_databases`).
_ALLOWED = {("agent.py", "_SYSTEM_TEMPLATE"), ("agent.py", "_render_system")}


def _owner(tree):
    owners = {}
    for node in tree.body:
        name = (node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                else node.targets[0].id if isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Name) else None)
        for sub in ast.walk(node):
            owners[id(sub)] = name
    return owners


def literal_db_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    # string solta como instrução (docstring ou comentário em bloco) não chega a ninguém
    prose = {id(n.value) for n in ast.walk(tree)
             if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    owners = _owner(tree)
    hits = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in prose
                and _LITERAL.search(n.value)
                and (path.name, owners.get(id(n))) not in _ALLOWED):
            hits.append(f"{path.name}:{n.lineno}: {n.value[:80]!r}")
    return hits


class LiteralDatabaseNameTests(unittest.TestCase):
    def test_runtime_code_has_no_hardcoded_database_name(self):
        hits = [h for p in sorted(BACKEND.glob("*.py")) if p.name != "seed.py"
                for h in literal_db_names(p)]
        self.assertEqual(hits, [], "use db.DB_MAIN / DB_BRAIN / DB_CATALOG")

    def test_scanner_catches_the_original_bug(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "x.py"
            bad.write_text('def note(c):\n    return f"find em POC.agent_sessions ({c})"\n')
            self.assertEqual(len(literal_db_names(bad)), 1)


class RenderedNamesTests(unittest.TestCase):
    def test_rendered_prompt_names_only_the_configured_databases(self):
        import agent
        with mock.patch.object(agent, "DB_MAIN", "POC_test"), \
                mock.patch.object(agent, "DB_CATALOG", "POC"):
            rendered = agent._render_system(agent._SYSTEM_TEMPLATE)
        self.assertNotIn("POC.", rendered)
        self.assertIn('collection "support_orders" do database "POC_test"', rendered)
        self.assertIn('Catálogo de produtos para substituições: database "POC"', rendered)

    def test_memory_prompt_and_inspector_use_the_real_database(self):
        import memory
        with mock.patch.object(memory, "DB_MAIN", "POC_test"):
            text = memory.format_for_prompt({"user_key": "u", "facts": [{"fact": "Prefere e-mail."}],
                                             "mode": "all"})
        self.assertIn("recuperados de POC_test.agent_memory", text)


class CalibrationGuardTests(unittest.TestCase):
    def test_apply_refuses_the_demo_brain_without_the_explicit_flag(self):
        import calibrate_thresholds as ct
        self.assertIsNotNone(ct.apply_refusal("ai_brain", {}))
        self.assertIsNone(ct.apply_refusal("ai_brain", {"ALLOW_DEMO_DB_WRITE": "1"}))
        self.assertIsNone(ct.apply_refusal("ai_brain_test", {}))


if __name__ == "__main__":
    unittest.main()
