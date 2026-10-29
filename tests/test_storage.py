import json
import tempfile
import threading
import unittest
from pathlib import Path

from dice_astrbot.storage import JsonStore


class JsonStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.store = JsonStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_persists_and_reloads_state(self):
        self.store.transact(lambda state: state["groups"].__setitem__("g1", {"title": "团务"}))
        reloaded = JsonStore(self.path)
        self.assertEqual(reloaded.group("g1"), {"title": "团务"})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["schema_version"], 1)

    def test_loads_legacy_file_without_shared_sessions_root(self):
        self.path.write_text(json.dumps({"schema_version": 1, "groups": {"g1": {"session": {"id": "legacy"}}}}), encoding="utf-8")
        reloaded = JsonStore(self.path)
        self.assertEqual(reloaded.group("g1")["session"]["id"], "legacy")
        self.assertEqual(reloaded.snapshot()["shared_sessions"], {})

    def test_rollback_on_mutation_exception(self):
        self.store.transact(lambda state: state["groups"].__setitem__("g1", {"count": 1}))
        before = self.path.read_bytes()
        def fail(state):
            state["groups"]["g1"]["count"] = 2
            raise ValueError("abort")
        with self.assertRaisesRegex(ValueError, "abort"):
            self.store.transact(fail)
        self.assertEqual(self.store.group("g1")["count"], 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_mutable_results_and_snapshots_do_not_leak(self):
        returned = self.store.transact(lambda state: state["groups"].setdefault("g1", {"items": []})["items"])
        returned.append("external")
        snapshot = self.store.snapshot()
        snapshot["groups"]["g1"]["items"].append("snapshot")
        group = self.store.group("g1")
        group["items"].append("group")
        self.assertEqual(self.store.group("g1")["items"], [])

    def test_concurrent_transactions_are_serialized(self):
        def increment():
            for _ in range(10):
                self.store.transact(lambda state: state["daily"].__setitem__("counter", state["daily"].get("counter", 0) + 1))
        workers = [threading.Thread(target=increment) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(self.store.snapshot()["daily"]["counter"], 40)

    def test_invalid_state_file_fails_with_context(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Dice state file is invalid"):
            JsonStore(self.path)


if __name__ == "__main__":
    unittest.main()
