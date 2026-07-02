from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from expenses_tracker.db import Database


class SyncLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "test.db"
        global_db = Database(db_path)
        tenant_id = global_db.get_default_tenant_id()
        self.db = Database(db_path, tenant_id=tenant_id)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_lock_round_trip(self) -> None:
        self.assertFalse(self.db.is_sync_in_progress())
        self.db.set_sync_in_progress(True)
        self.assertTrue(self.db.is_sync_in_progress())
        self.db.set_sync_in_progress(False)
        self.assertFalse(self.db.is_sync_in_progress())

    def test_stale_lock_is_ignored(self) -> None:
        stale = datetime.now(timezone.utc) - Database.SYNC_LOCK_MAX_AGE - timedelta(minutes=1)
        self.db.set_sync_value("sync_in_progress", stale.isoformat())
        self.assertFalse(self.db.is_sync_in_progress())

    def test_recent_lock_is_honored(self) -> None:
        recent = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.db.set_sync_value("sync_in_progress", recent.isoformat())
        self.assertTrue(self.db.is_sync_in_progress())

    def test_legacy_true_marker_is_treated_as_expired(self) -> None:
        self.db.set_sync_value("sync_in_progress", "true")
        self.assertFalse(self.db.is_sync_in_progress())


if __name__ == "__main__":
    unittest.main()
