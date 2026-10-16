"""A bateria de caos LIVE nunca pode apontar para o banco da demo (bug de 2026-10-16).

Os cenários offline importavam `db` antes do `use_test_databases` dos cenários LIVE, então
`db.DB_MAIN` ficava "POC" e live_degraded_turn/crash_mid_tool gravavam sessões e checkpoints
na demo. Agora o script isola no import (LIVE=1) e cada cenário LIVE confere os módulos já
carregados antes de escrever.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
CLEAN = {k: v for k, v in os.environ.items()
         if k not in {"MONGODB_DB", "MONGODB_BRAIN_DB", "ALLOW_DEMO_DB_WRITE",
                      "MONGODB_TEST_DB", "MONGODB_TEST_BRAIN_DB"}}


def _run(code: str, **env) -> str:
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True,
                         text=True, env={**CLEAN, "ANTHROPIC_API_KEY": "x", **env}, timeout=120)
    if out.returncode:
        raise AssertionError(out.stderr[-2000:])
    return out.stdout.strip().splitlines()[-1]


class ChaosIsolationTests(unittest.TestCase):
    def test_live_import_points_every_module_to_test_db(self):
        line = _run("import sys; sys.path.insert(0,'scripts'); import chaos_suite, db, agent; "
                    "print(db.DB_MAIN, db.DB_BRAIN, agent.DB_MAIN)", LIVE="1")
        self.assertEqual(line, "POC_test ai_brain_test POC_test")

    def test_guard_refuses_module_already_bound_to_demo(self):
        line = _run("import sys; sys.path.insert(0,'scripts'); import db; "
                    "import chaos_suite; print(chaos_suite.require_test_db('x') is not None)")
        self.assertEqual(line, "True")

    def test_guard_passes_on_test_db(self):
        line = _run("import sys; sys.path.insert(0,'scripts'); import chaos_suite; "
                    "print(chaos_suite.require_test_db('x'))", LIVE="1")
        self.assertEqual(line, "None")


if __name__ == "__main__":
    unittest.main()
