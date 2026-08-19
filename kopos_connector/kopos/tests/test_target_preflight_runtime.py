from __future__ import annotations

import hashlib

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.acceptance.target_preflight_machine import (
    _database_round_trip,
    _index_check,
    _redis_round_trip,
    _scheduler_check,
    _schema_check,
)


class TestTargetPreflightRuntime(FrappeTestCase):
    """Exercise the target probes against real Frappe, MariaDB, and Redis."""

    def tearDown(self) -> None:
        frappe.db.rollback()

    def test_frappe_and_mariadb_probe_round_trips_then_removes_its_row(self) -> None:
        proof = _database_round_trip("1" * 32)

        self.assertTrue(proof["frappeSaveReadRoundTrip"]["passed"])
        self.assertTrue(proof["mariaDbSaveReadRoundTrip"]["passed"])
        self.assertEqual(proof["frappeSaveReadRoundTrip"]["residualRows"], 0)
        self.assertEqual(proof["mariaDbSaveReadRoundTrip"]["residualRows"], 0)

    def test_redis_probe_proves_exclusive_owner_safe_lock_and_cleanup(self) -> None:
        site = str(getattr(frappe.local, "site", "test.localhost"))
        proof = _redis_round_trip(
            "2" * 32,
            hashlib.sha256(site.encode("utf-8")).hexdigest(),
        )

        self.assertTrue(proof["passed"])
        self.assertTrue(proof["exclusiveAcquire"])
        self.assertTrue(proof["wrongOwnerReleaseRejected"])
        self.assertTrue(proof["ownerReleaseSucceeded"])
        self.assertEqual(proof["residualKeys"], 0)

    def test_reviewed_schema_indexes_and_scheduler_match_the_real_site(self) -> None:
        schema = _schema_check()
        indexes = _index_check()
        # Restored production-derived data is intentionally run with the
        # scheduler paused.  Keep validating every job row and cron timing
        # without weakening the default production preflight rejection.
        scheduler = _scheduler_check(allow_paused_scheduler=True)

        self.assertTrue(schema["passed"])
        self.assertEqual(schema["missing"], [])
        self.assertEqual(schema["mismatched"], [])
        self.assertTrue(indexes["passed"])
        self.assertEqual(indexes["missing"], [])
        self.assertTrue(scheduler["passed"])
        self.assertEqual(scheduler["missing"], [])
        self.assertEqual(scheduler["obsolete"], [])
