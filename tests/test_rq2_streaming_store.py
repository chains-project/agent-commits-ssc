import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "hallucination"
sys.path.insert(0, str(SCRIPT_DIR))

from rq2_streaming_store import (  # noqa: E402
    REPLACE_ATTEMPTS,
    CheckpointedCsvStore,
    CsvTarget,
    OrderedCsvGroupCursor,
    atomic_write_json,
    read_json,
)


TARGETS = {
    "left": CsvTarget("left.csv", ("id", "value"), "left_rows"),
    "right": CsvTarget("right.csv", ("id", "value"), "right_rows"),
}


def read_rows(path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class StreamingStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _store(self, resume=False, seed=None):
        return CheckpointedCsvStore(
            self.root, TARGETS, "state.json", resume=resume,
            seed=seed or {"stage": "test", "input": "fixed"},
        )

    def test_atomic_write_retries_transient_permission_error(self):
        target = self.root / "state.json"
        real_replace, attempts = os.replace, []

        def replace_after_transient_lock(source, destination):
            attempts.append((source, destination))
            if len(attempts) == 1:
                raise PermissionError(5, "transient lock")
            return real_replace(source, destination)

        with patch("rq2_streaming_store.os.replace", replace_after_transient_lock):
            atomic_write_json(target, {"processed_groups": 2})
        self.assertEqual(read_json(target)["processed_groups"], 2)
        self.assertEqual(len(attempts), 2)

    def test_atomic_write_does_not_retry_other_os_errors(self):
        target = self.root / "state.json"
        with patch("rq2_streaming_store.os.replace", side_effect=OSError("disk")) as mocked:
            with self.assertRaisesRegex(OSError, "disk"):
                atomic_write_json(target, {"processed_groups": 2})
        self.assertEqual(mocked.call_count, 1)

    def test_atomic_write_bounds_persistent_permission_retries(self):
        target = self.root / "state.json"
        error = PermissionError(5, "persistent lock")
        with patch("rq2_streaming_store.os.replace", side_effect=error) as mocked:
            with patch("rq2_streaming_store.time.sleep") as sleeping:
                with self.assertRaisesRegex(PermissionError, "persistent lock"):
                    atomic_write_json(target, {"processed_groups": 2})
        self.assertEqual(mocked.call_count, REPLACE_ATTEMPTS)
        self.assertEqual(sleeping.call_count, REPLACE_ATTEMPTS - 1)

    def test_resume_truncates_uncommitted_tails_for_both_tables(self):
        store = self._store()
        store.prepare()
        state = store.commit(1, {
            "left": [{"id": "l1", "value": "ok"}],
            "right": [{"id": "r1", "value": "ok"}],
        })
        with (self.root / "left.csv").open("a", encoding="utf-8") as handle:
            handle.write("tail,bad\n")
        with (self.root / "right.csv").open("a", encoding="utf-8") as handle:
            handle.write("tail,bad\n")
        resumed = self._store(resume=True)
        resumed_state = resumed.prepare()
        self.assertEqual(resumed_state["csv_bytes"], state["csv_bytes"])
        self.assertEqual(read_rows(self.root / "left.csv"), [{"id": "l1", "value": "ok"}])
        self.assertEqual(read_rows(self.root / "right.csv"), [{"id": "r1", "value": "ok"}])

    def test_complete_resume_does_not_truncate_downstream_replacement(self):
        store = self._store()
        store.prepare()
        store.commit(1, {
            "left": [{"id": "l1", "value": "stage"}],
            "right": [{"id": "r1", "value": "stage"}],
        })
        completed = store.mark_complete()
        left = self.root / "left.csv"
        right = self.root / "right.csv"
        left.write_text(left.read_text(encoding="utf-8") + "l2,downstream\n", encoding="utf-8")
        right.write_text(right.read_text(encoding="utf-8") + "r2,downstream\n", encoding="utf-8")
        before = {path: path.read_bytes() for path in (left, right)}

        resumed = self._store(resume=True).prepare()

        self.assertEqual(resumed, completed)
        self.assertEqual({path: path.read_bytes() for path in (left, right)}, before)
        self.assertEqual(read_json(self.root / "state.json"), completed)

    def test_resume_rejects_seed_change(self):
        self._store().prepare()
        with self.assertRaisesRegex(ValueError, "seed mismatch"):
            self._store(resume=True, seed={"stage": "test", "input": "changed"}).prepare()

    def test_fresh_abort_removes_only_created_targets(self):
        keep = self.root / "keep.txt"
        keep.write_text("keep", encoding="utf-8")
        store = self._store()
        store.prepare()
        store.abort_fresh()
        self.assertTrue(keep.exists())
        self.assertFalse((self.root / "left.csv").exists())
        self.assertFalse((self.root / "state.json").exists())

    def test_ordered_group_cursor_preserves_input_groups(self):
        path = self.root / "groups.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=("commit_id", "value"))
            writer.writeheader()
            writer.writerows([
                {"commit_id": "c1", "value": "a"},
                {"commit_id": "c1", "value": "b"},
                {"commit_id": "c2", "value": "c"},
            ])
        with OrderedCsvGroupCursor(path, ("commit_id",)) as cursor:
            self.assertEqual(len(cursor.take(("c1",))), 2)
            self.assertEqual(cursor.take(("missing",)), [])
            self.assertEqual(len(cursor.take(("c2",))), 1)
            self.assertTrue(cursor.exhausted)


if __name__ == "__main__":
    unittest.main()
