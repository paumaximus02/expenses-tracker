from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from expenses_tracker.db import Database
from expenses_tracker.models import MatchType


class MerchantRuleDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "test.db"
        global_db = Database(db_path)
        tenant_id = global_db.get_default_tenant_id()
        self.db = Database(db_path, tenant_id=tenant_id)
        self.bucket_id = self.db.create_bucket("Groceries").id
        self.db.upsert_merchant_rule(
            merchant_pattern="Costco",
            bucket_id=self.bucket_id,
            match_type=MatchType.EXACT,
            confirmed_by_user=True,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_delete_merchant_rule(self) -> None:
        rule = self.db.list_merchant_rules()[0]
        self.db.delete_merchant_rule(rule.id)
        self.assertEqual(self.db.list_merchant_rules(), [])

    def test_delete_missing_merchant_rule_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.db.delete_merchant_rule(9999)


if __name__ == "__main__":
    unittest.main()
