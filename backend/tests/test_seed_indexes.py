"""Regressão C1 (2026-10-08): em banco vazio o primeiro seed tentava criar
`turn_probes_vs` antes de a coleção existir, falhava em silêncio e saía 0."""

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-import-only")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import seed  # noqa: E402


class FakeColl:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def create_search_index(self, model):
        if self.name not in self.db.created:
            raise RuntimeError(f"Collection {self.name} does not exist")
        doc = getattr(model, "document", model)
        self.db.indexes.setdefault(self.name, set()).add(doc["name"])

    def update_search_index(self, name, definition):
        pass

    def list_search_indexes(self):
        return [{"name": n} for n in self.db.indexes.get(self.name, set())]


class FakeDB:
    def __init__(self, fail_create=False):
        self.created, self.indexes, self.fail_create = set(), {}, fail_create

    def list_collection_names(self):
        return sorted(self.created)

    def create_collection(self, name):
        if not self.fail_create:
            self.created.add(name)

    def __getitem__(self, name):
        return FakeColl(self, name)


class FakeClient:
    def __init__(self, **kw):
        self.dbs = {}
        self.kw = kw

    def __getitem__(self, name):
        return self.dbs.setdefault(name, FakeDB(**self.kw))


class SeedIndexTests(unittest.TestCase):
    def test_empty_database_gets_every_index(self):
        client = FakeClient()
        with contextlib.redirect_stdout(io.StringIO()):
            failed = seed.create_vector_indexes(client)
        self.assertEqual(failed, [])
        brain = client[seed._DB_BRAIN]
        self.assertIn("turn_probes_vs", brain.indexes["turn_probes"],
                      "a coleção é criada antes do índice")

    def test_missing_index_is_reported_not_swallowed(self):
        with contextlib.redirect_stdout(io.StringIO()):
            failed = seed.create_vector_indexes(FakeClient(fail_create=True))
        self.assertTrue(any("turn_probes_vs" in f for f in failed))
        self.assertEqual(len(failed), len(seed.VECTOR_INDEXES) + len(seed.BM25_INDEXES))


if __name__ == "__main__":
    unittest.main()
