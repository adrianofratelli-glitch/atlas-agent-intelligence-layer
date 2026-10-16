"""A5: Queryable Encryption standalone (scripts/qe_sensitive_profile.py).

Offline: mapa de campos (range só em numérico), recusa de banco da demo, chave local 0600,
resumo mascarado sem PII. LIVE (LIVE=1 + pymongocrypt): ciphertext no disco, igualdade,
faixa, escopo de tenant no servidor e entradas hostis — sempre em POC_test.
"""

import asyncio
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-import-only")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import qe_sensitive_profile as qe  # noqa: E402

try:
    import pymongocrypt  # noqa: F401
    HAS_CRYPT = True
except ImportError:
    HAS_CRYPT = False


class QEOfflineTests(unittest.TestCase):
    def test_schema_equality_on_strings_range_only_on_number(self):
        by_path = {f["path"]: f for f in qe.ENCRYPTED_FIELDS["fields"]}
        self.assertEqual(by_path["cpf"]["queries"][0]["queryType"], "equality")
        self.assertEqual(by_path["telefone"]["bsonType"], "string")
        rng = by_path["renda_mensal"]
        self.assertEqual((rng["bsonType"], rng["queries"][0]["queryType"]), ("int", "range"))
        for f in qe.ENCRYPTED_FIELDS["fields"]:
            if f["bsonType"] == "string":
                self.assertNotIn("range", [q["queryType"] for q in f.get("queries", [])])
        # o que o agente/MCP/$vectorSearch leem fica em claro
        self.assertNotIn("user_key", qe.ENCRYPTED_PATHS)
        self.assertNotIn("resumo_mascarado", qe.ENCRYPTED_PATHS)

    def test_refuses_demo_database(self):
        for name in ("POC", "ai_brain", "producao"):
            with self.assertRaises(SystemExit):
                qe.require_test_db(name)
        qe.require_test_db("POC_test")

    def test_master_key_created_0600_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets" / "k.bin"
            key = qe.load_master_key(path)
            self.assertEqual(len(key), 96)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(qe.load_master_key(path), key)
            path.write_bytes(b"curta")
            with self.assertRaises(SystemExit):
                qe.load_master_key(path)

    def test_master_key_path_is_gitignored(self):
        ignore = (Path(__file__).resolve().parents[2] / ".gitignore").read_text()
        self.assertIn("backend/secrets/", ignore)

    def test_masked_summary_never_carries_raw_pii(self):
        for p in qe.PROFILES:
            text = qe.masked_summary(p)
            self.assertNotIn(p["cpf"], text)
            self.assertNotIn(p["telefone"], text)
            self.assertNotIn(p["cpf"].replace(".", "").replace("-", ""), text)


@unittest.skipUnless(os.getenv("LIVE") in {"1", "true", "yes"} and HAS_CRYPT,
                     "LIVE=1 e pymongocrypt necessários")
class QELiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from scripts.isolation import use_test_databases

        main_db, _ = use_test_databases(what="test_qe_profile LIVE")
        cls.main_db = main_db

    def _run(self, fn):
        async def go():
            demo = qe.QEDemo(os.environ["MONGODB_URI"], self.main_db, qe.load_master_key())
            try:
                await demo.reset()
                await demo.insert_profiles()
                return await fn(demo)
            finally:
                await demo.close()
        return asyncio.run(go())

    def test_demo_report(self):
        report = self._run(qe.run_demo)
        self.assertTrue(all(report["stored_as_ciphertext"].values()))
        self.assertEqual(report["equality_cpf"], ["cliente-demo"])
        self.assertEqual(report["equality_wrong_tenant"], [])
        self.assertEqual(report["range_renda_4k_9k"], ["ana.vendas", "cliente-demo"])

    def test_hostile_and_edge_inputs(self):
        async def fn(demo):
            out = {}
            out["op"] = await demo.find_by("cpf", {"$ne": None})
            out["empty"] = await demo.find_by("cpf", "")
            out["unicode"] = await demo.find_by("telefone", "(62) 98123-4567​")
            out["huge"] = await demo.find_by("cpf", "9" * 5000)
            out["range_out"] = await demo.find_renda_between(-10**9, 10**9)
            out["range_tenant"] = await demo.find_renda_between(0, 10**6, user_key="marina.fin")
            out["range_empty"] = await demo.find_renda_between(20_000, 30_000)
            try:
                await demo.find_by("renda_mensal", "1")
                out["bad_path"] = "aceitou"
            except ValueError:
                out["bad_path"] = "recusou"
            return out
        out = self._run(fn)
        self.assertEqual(out["op"], [])
        self.assertEqual(out["empty"], [])
        self.assertEqual(out["unicode"], [])          # igualdade é exata, invisível muda o valor
        self.assertEqual(out["huge"], [])
        self.assertEqual(sorted(d["user_key"] for d in out["range_out"]),
                         ["ana.vendas", "cliente-demo", "marina.fin"])  # limites saturados
        self.assertEqual([d["user_key"] for d in out["range_tenant"]], ["marina.fin"])
        self.assertEqual(out["range_empty"], [])
        self.assertEqual(out["bad_path"], "recusou")

    def test_concurrent_equality_queries_no_cross_tenant(self):
        async def fn(demo):
            jobs = [demo.find_by("cpf", p["cpf"], user_key=p["user_key"])
                    for p in qe.PROFILES * 5]
            return await asyncio.gather(*jobs)
        results = self._run(fn)
        for p, rows in zip(qe.PROFILES * 5, results):
            self.assertEqual([r["user_key"] for r in rows], [p["user_key"]])
            self.assertEqual(rows[0]["cpf"], p["cpf"])


if __name__ == "__main__":
    unittest.main()
