"""A4: change stream area_profiles → invalidação do semantic_cache (offline, Atlas dublê).

Cobre relevância do evento, granularidade por área, FAQ só com hash diferente, evento com
campos ausentes (invalidação conservadora), history lost/invalidate via ChangeStreamWorker,
um worker por processo e heartbeat do preflight.
"""

import asyncio
import os
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-import-only")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cache_invalidation as ci  # noqa: E402


class _Res:
    def __init__(self, n):
        self.deleted_count = n


class FakeCache:
    """semantic_cache em memória com delete_many/delete_one/find que entendem os filtros usados."""

    def __init__(self, docs):
        self.docs = list(docs)
        self.filters = []

    @staticmethod
    def _match(doc, flt):
        for key, cond in flt.items():
            val = doc.get(key)
            if isinstance(cond, dict) and "$ne" in cond:
                if val == cond["$ne"]:
                    return False
            elif val != cond:
                return False
        return True

    async def delete_many(self, flt):
        self.filters.append(flt)
        keep = [d for d in self.docs if not self._match(d, flt)]
        n = len(self.docs) - len(keep)
        self.docs = keep
        return _Res(n)

    async def delete_one(self, flt):
        for i, d in enumerate(self.docs):
            if self._match(d, flt):
                del self.docs[i]
                return _Res(1)
        return _Res(0)

    def find(self, flt, projection=None, **kw):
        rows = [d for d in self.docs if self._match(d, flt)]

        class _It:
            def __aiter__(self_):
                self_.i = iter(rows)
                return self_

            async def __anext__(self_):
                try:
                    return next(self_.i)
                except StopIteration:
                    raise StopAsyncIteration
        return _It()

    async def insert_one(self, doc):
        return None


class FakeProfiles:
    def __init__(self, profiles):
        self.profiles = profiles

    def find(self, *a, **k):
        profiles = self.profiles

        class _C:
            async def to_list(self_, n):
                return list(profiles)
        return _C()


P_DEFAULT = {"_id": "area_default", "area": "default", "persona": "troca em até 30 dias"}
P_FIN = {"_id": "area_financeiro", "area": "financeiro", "persona": "estorno em 7 dias úteis"}


def cache_docs():
    h_def, h_fin = ci.persona_hash(P_DEFAULT), ci.persona_hash(P_FIN)
    return [
        {"_id": "r1", "area": "default", "scope": "support"},
        {"_id": "r2", "area": "default", "scope": "quick-chat"},
        {"_id": "r3", "area": "financeiro", "scope": "support"},
        {"_id": "faq1", "area": "global", "scope": "faq", "source_area": "default",
         "source_hash": h_def},
        {"_id": "faq2", "area": "financeiro", "scope": "faq", "source_area": "financeiro",
         "source_hash": h_fin},
        {"_id": "faq_sem_fonte", "area": "global", "scope": "faq"},
    ]


class Env:
    def __init__(self, profiles=(P_DEFAULT, P_FIN)):
        self.cache = FakeCache(cache_docs())
        self.audit = FakeCache([])
        self.profiles = FakeProfiles(list(profiles))

    def patches(self):
        poc = mock.MagicMock()
        poc.__getitem__.side_effect = lambda n: self.cache if n == ci.CACHE_COLLECTION \
            else self.audit
        brain = mock.MagicMock()
        brain.__getitem__.side_effect = lambda n: self.profiles
        return [mock.patch.object(ci, "poc", lambda: poc),
                mock.patch.object(ci, "ai_brain", lambda: brain)]

    def run(self, coro_fn):
        async def go():
            cms = self.patches()
            for cm in cms:
                cm.__enter__()
            try:
                return await coro_fn()
            finally:
                for cm in reversed(cms):
                    cm.__exit__(None, None, None)
        return asyncio.run(go())

    def ids(self):
        return sorted(d["_id"] for d in self.cache.docs)


def upd(area_id, fields, full=None):
    return {"operationType": "update", "documentKey": {"_id": area_id},
            "updateDescription": {"updatedFields": fields, "removedFields": []},
            "fullDocument": full}


class RelevanceTests(unittest.TestCase):
    def test_relevant_fields_only(self):
        self.assertTrue(ci.is_relevant(upd("area_default", {"persona": "x"})))
        self.assertTrue(ci.is_relevant(upd("area_default", {"active": False})))
        self.assertFalse(ci.is_relevant(upd("area_default", {"label": "x", "tier": "y"})))
        self.assertFalse(ci.is_relevant(upd("area_default", {"rate_limit_multiplier": 2})))
        for op in ("insert", "replace", "delete"):
            self.assertTrue(ci.is_relevant({"operationType": op}))
        self.assertFalse(ci.is_relevant({"operationType": "drop"}))

    def test_removed_and_nested_fields(self):
        ev = {"operationType": "update", "updateDescription": {"updatedFields": {},
                                                               "removedFields": ["persona"]}}
        self.assertTrue(ci.is_relevant(ev))
        self.assertTrue(ci.is_relevant(upd("area_default", {"persona.extra": 1})))

    def test_area_resolution(self):
        self.assertEqual(ci._area_of({"fullDocument": {"area": "vendas"}}), "vendas")
        self.assertEqual(ci._area_of({"documentKey": {"_id": "area_logistica"}}), "logistica")
        for bad in ({}, {"documentKey": {"_id": 3}}, {"documentKey": {"_id": "area_"}},
                    {"fullDocument": {"area": {"$ne": 1}}}, {"documentKey": None}):
            self.assertIsNone(ci._area_of(bad))

    def test_pipeline_filters_on_server(self):
        stage = ci.pipeline()[0]["$match"]["operationType"]["$in"]
        self.assertIn("update", stage)
        self.assertNotIn("drop", stage)


class InvalidationTests(unittest.TestCase):
    def test_persona_change_drops_runtime_and_faq_of_that_area_only(self):
        env = Env()
        new = dict(P_DEFAULT, persona="troca em até 15 dias")
        env.run(lambda: ci.handle_change(upd("area_default", {"persona": new["persona"]}, new)))
        self.assertEqual(env.ids(), ["faq2", "faq_sem_fonte", "r3"])

    def test_same_hash_keeps_faq(self):
        env = Env()
        env.run(lambda: ci.handle_change(upd("area_default", {"active": True}, P_DEFAULT)))
        self.assertIn("faq1", env.ids())
        self.assertNotIn("r1", env.ids())

    def test_irrelevant_change_touches_nothing(self):
        env = Env()
        out = env.run(lambda: ci.handle_change(upd("area_default", {"label": "Novo"}, P_DEFAULT)))
        self.assertIsNone(out)
        self.assertEqual(len(env.ids()), 6)

    def test_delete_event_without_full_document(self):
        env = Env()
        env.run(lambda: ci.handle_change({"operationType": "delete",
                                          "documentKey": {"_id": "area_financeiro"}}))
        self.assertEqual(env.ids(), ["faq1", "faq_sem_fonte", "r1", "r2"])

    def test_event_with_missing_fields_reconciles_conservatively(self):
        env = Env(profiles=[dict(P_DEFAULT, persona="mudou"), P_FIN])
        env.run(lambda: ci.handle_change({"operationType": "replace"}))
        # runtime de todas as áreas sai; FAQ só a de hash divergente; sem fonte fica
        self.assertEqual(env.ids(), ["faq2", "faq_sem_fonte"])

    def test_reconcile_is_idempotent(self):
        env = Env()
        env.run(lambda: ci.reconcile("teste"))
        first = env.ids()
        env.run(lambda: ci.reconcile("teste"))
        self.assertEqual(env.ids(), first)
        self.assertEqual(first, ["faq1", "faq2", "faq_sem_fonte"])

    def test_hostile_values_in_event_never_become_operators(self):
        env = Env()
        env.run(lambda: ci.handle_change(upd({"$ne": None}, {"persona": "x"},
                                             {"area": {"$gt": ""}})))
        for flt in env.cache.filters:
            for key, cond in flt.items():
                if key == "area":
                    self.fail("área hostil não pode virar filtro")

    def test_fuzz_random_events_never_raise(self):
        rng = random.Random(1610)
        atoms = [None, "", "area_default", "area_", 3, {"$ne": 1}, [], "x" * 500]
        for _ in range(300):
            env = Env()
            ev = {k: rng.choice([rng.choice(atoms), {"_id": rng.choice(atoms)},
                                 {"updatedFields": {rng.choice(["persona", "label", "a.b"]): 1}}])
                  for k in rng.sample(["operationType", "documentKey", "fullDocument",
                                       "updateDescription"], rng.randint(0, 4))}
            if rng.random() < 0.5:
                ev["operationType"] = rng.choice(["update", "replace", "insert", "delete",
                                                  "drop", None])
            env.run(lambda: ci.handle_change(ev))
            self.assertTrue(set(env.ids()) <= {d["_id"] for d in cache_docs()})


class FakeStream:
    def __init__(self, events, fail=None, token=None):
        self.events, self.fail, self.closed, self.resume_token = list(events), fail, False, token

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if self.events:
            return self.events.pop(0)
        if self.fail:
            exc, self.fail = self.fail, None
            raise exc
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


class Lost(Exception):
    code = 286


class WorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        if ci._ar is None:
            self.skipTest("pov-shared ausente")

    def test_history_lost_triggers_reconcile_and_reopens_without_token(self):
        env = Env(profiles=[dict(P_DEFAULT, persona="mudou"), P_FIN])
        store = ci._ar.MemoryResumeTokenStore()
        store.token = {"_data": "velho"}
        opened = []
        streams = iter([Lost("ChangeStreamHistoryLost"),
                        FakeStream([upd("area_financeiro", {"persona": "y"},
                                        dict(P_FIN, persona="y")) | {"_id": {"_data": "t1"}}])])
        worker = None

        async def open_stream(token):
            opened.append(token)
            nxt = next(streams, None)
            if nxt is None:
                await worker.stop()
                return FakeStream([])
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        async def go():
            nonlocal worker

            async def fast(_):
                await asyncio.sleep(0)
            worker = ci.build_worker(open_stream=open_stream, store=store, sleep=fast)
            worker.start()
            await asyncio.wait_for(worker._task, 5)
        env.run(go)
        self.assertEqual(opened[:2], [{"_data": "velho"}, None])
        self.assertEqual(env.ids(), ["faq_sem_fonte"])  # reconcile + evento do financeiro
        self.assertEqual(store.token, {"_data": "t1"})

    def test_single_worker_per_process(self):
        async def go():
            with mock.patch.object(ci, "_open_stream", lambda t: asyncio.sleep(3600)), \
                    mock.patch.object(ci, "ai_brain", mock.MagicMock()):
                ci._worker = None
                self.assertTrue(await ci.start())
                first = ci._worker
                self.assertTrue(await ci.start())
                self.assertIs(ci._worker, first)
                await ci.stop()
                self.assertFalse(first.running)
        asyncio.run(go())

    def test_heartbeat_freshness(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(ci.heartbeat_fresh({"heartbeat_at": now - timedelta(seconds=10)}, now))
        self.assertFalse(ci.heartbeat_fresh({"heartbeat_at": now - timedelta(minutes=5)}, now))
        for bad in (None, {}, {"heartbeat_at": "ontem"}):
            self.assertFalse(ci.heartbeat_fresh(bad, now))


if __name__ == "__main__":
    unittest.main()
