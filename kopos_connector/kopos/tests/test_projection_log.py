from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.kopos.services.projection.log_service import (
    create_projection_log,
    get_pending_projections,
    retry_failed_projections,
    update_projection_state,
)
from kopos_connector.kopos.tests.frappe_test_fixtures import create_open_test_shift


class TestProjectionLogService(FrappeTestCase):
    def setUp(self):
        self.cleanup_test_logs()
        self.shift = create_open_test_shift(prefix="KOPOS-PROJECTION-TEST")

    def tearDown(self):
        frappe.db.rollback()

    def cleanup_test_logs(self):
        frappe.db.delete(
            "FB Projection Log",
            {"idempotency_key": ("like", "KOPOS-PROJECTION-TEST-%")},
        )
        frappe.db.delete(
            "FB Shift",
            {"shift_code": ("like", "KOPOS-PROJECTION-TEST-%")},
        )
        frappe.db.commit()

    def create_log(
        self,
        *,
        projection_type="Sales Invoice",
        idempotency_key,
        payload_hash,
    ):
        return create_projection_log(
            source_doctype="FB Shift",
            source_name=self.shift.name,
            projection_type=projection_type,
            idempotency_key=f"KOPOS-PROJECTION-TEST-{idempotency_key}",
            payload_hash=payload_hash,
        )

    def test_create_projection_log(self):
        log_name = self.create_log(
            idempotency_key="test-key-123",
            payload_hash="abc123",
        )

        self.assertIsNotNone(log_name)

        log = frappe.get_doc("FB Projection Log", log_name)
        self.assertEqual(log.source_doctype, "FB Shift")
        self.assertEqual(log.source_name, self.shift.name)
        self.assertEqual(log.projection_type, "Sales Invoice")
        self.assertEqual(log.state, "Pending")
        self.assertEqual(log.idempotency_key, "KOPOS-PROJECTION-TEST-test-key-123")
        self.assertEqual(log.payload_hash, "abc123")

    def test_create_duplicate_log_returns_existing(self):
        idempotency_key = "duplicate-test-key"

        log1 = self.create_log(
            idempotency_key=idempotency_key,
            payload_hash="abc123",
        )

        log2 = self.create_log(
            idempotency_key=idempotency_key,
            payload_hash="abc123",
        )

        self.assertEqual(log1, log2)

    def test_update_projection_state_to_success(self):
        log_name = self.create_log(
            projection_type="FB Shift",
            idempotency_key="test-key-456",
            payload_hash="def456",
        )

        updated = update_projection_state(
            log_name=log_name,
            state="Succeeded",
            target_doctype="FB Shift",
            target_name=self.shift.name,
            error=None,
        )

        self.assertIsNotNone(updated)

        log = frappe.get_doc("FB Projection Log", log_name)
        self.assertEqual(log.state, "Succeeded")
        self.assertEqual(log.target_doctype, "FB Shift")
        self.assertEqual(log.target_name, self.shift.name)
        self.assertIsNone(log.last_error)

    def test_update_projection_state_to_failed(self):
        log_name = self.create_log(
            idempotency_key="test-key-789",
            payload_hash="ghi789",
        )

        update_projection_state(
            log_name=log_name,
            state="Failed",
            target_doctype=None,
            target_name=None,
            error="Connection timeout",
        )

        log = frappe.get_doc("FB Projection Log", log_name)
        self.assertEqual(log.state, "Failed")
        self.assertEqual(log.last_error, "Connection timeout")
        self.assertEqual(log.retry_count, 1)

    def test_update_failed_increments_retry_count(self):
        log_name = self.create_log(
            idempotency_key="test-key-retry",
            payload_hash="retry123",
        )

        update_projection_state(
            log_name=log_name,
            state="Failed",
            target_doctype=None,
            target_name=None,
            error="First failure",
        )

        update_projection_state(
            log_name=log_name,
            state="Failed",
            target_doctype=None,
            target_name=None,
            error="Second failure",
        )

        log = frappe.get_doc("FB Projection Log", log_name)
        self.assertEqual(log.retry_count, 2)

    def test_get_pending_projections(self):
        pending_one = self.create_log(
            idempotency_key="pending-1",
            payload_hash="p1",
        )

        pending_two = self.create_log(
            projection_type="Stock Issue",
            idempotency_key="pending-2",
            payload_hash="p2",
        )

        succeeded_log = self.create_log(
            projection_type="FB Shift",
            idempotency_key="success-1",
            payload_hash="s1",
        )
        update_projection_state(
            log_name=succeeded_log,
            state="Succeeded",
            target_doctype="FB Shift",
            target_name=self.shift.name,
            error=None,
        )

        pending = get_pending_projections()
        pending_names = [p["name"] for p in pending]

        self.assertIn(pending_one, pending_names)
        self.assertIn(pending_two, pending_names)
        self.assertNotIn(succeeded_log, pending_names)

    def test_retry_helper_never_relabels_unsupported_failure_as_pending(self):
        failed_log = self.create_log(
            idempotency_key="failed-1",
            payload_hash="f1",
        )
        update_projection_state(
            log_name=failed_log,
            state="Failed",
            target_doctype=None,
            target_name=None,
            error="Network error",
        )

        retried = retry_failed_projections()

        self.assertEqual(retried, [])

        log = frappe.get_doc("FB Projection Log", failed_log)
        self.assertEqual(log.state, "Failed")


class TestProjectionTransactionBoundary(FrappeTestCase):
    """Rollback-only native SQL faults, independent of the legacy shift fixture."""

    def setUp(self):
        frappe.set_user("Administrator")
        # Error Log can persist outside rollback; diagnostic storage is not
        # under test here. Keep this native transaction fixture rollback-only.
        logger = patch("kopos_connector.kopos.services.projection.log_service._log_error")
        logger.start()
        self.addCleanup(logger.stop)
        self.parent = self._sentinel()

    def tearDown(self):
        frappe.db.rollback()

    def _sentinel(self):
        return frappe.get_doc({
            "doctype": "ToDo", "description": "Synthetic transaction boundary probe",
        }).insert(ignore_permissions=True).name

    def _assert_commit_blocked(self):
        # Exercise native Database.commit and CallbackManager, but trap SQL
        # COMMIT even against regressed code: tests must never retain fixtures.
        native_sql = frappe.db.sql

        def no_commit(query, *args, **kwargs):
            if str(query).strip().lower().startswith("commit"):
                self.fail("Unsafe transaction reached SQL COMMIT")
            return native_sql(query, *args, **kwargs)

        with patch.object(frappe.db, "sql", side_effect=no_commit):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "full rollback is required"):
                    frappe.db.commit()

    def _assert_full_rollback_recovers(self):
        frappe.db.rollback()
        self.assertFalse(frappe.db.exists("ToDo", self.parent))
        # Native full rollback clears the blocker; no actual SQL commit needed.
        frappe.db.before_commit.run()
        self.assertTrue(frappe.db.exists("ToDo", self._sentinel()))

    def test_failed_savepoint_does_not_return_a_success_sentinel(self):
        from kopos_connector.utils.diagnostics import make_savepoint
        # Real MariaDB syntax rejection, not a simulated disconnection/deadlock.
        with self.assertRaises(Exception):
            make_savepoint("invalid savepoint")
        self.assertTrue(frappe.db.exists("ToDo", self.parent))
        self._assert_commit_blocked()
        self._assert_full_rollback_recovers()

    def test_empty_savepoint_preserves_parent_work_and_blocks_commit(self):
        from kopos_connector.utils.diagnostics import rollback_to_savepoint
        with self.assertRaisesRegex(ValueError, "savepoint name is required"):
            rollback_to_savepoint("", title="Synthetic rollback failed")
        self.assertTrue(frappe.db.exists("ToDo", self.parent))
        self._assert_commit_blocked()
        self._assert_full_rollback_recovers()

    def test_lost_savepoint_blocks_repeated_commit_even_after_outer_named_rollback(self):
        from kopos_connector.utils.diagnostics import make_savepoint, rollback_to_savepoint
        outer = make_savepoint("outer_probe")
        owned = make_savepoint("owned_probe")
        inner = self._sentinel()
        frappe.db.release_savepoint(owned)
        with self.assertRaises(Exception):
            rollback_to_savepoint(owned, title="Synthetic rollback failed")
        self.assertTrue(frappe.db.exists("ToDo", inner))
        self._assert_commit_blocked()
        frappe.db.rollback(save_point=outer)
        self.assertFalse(frappe.db.exists("ToDo", inner))
        self.assertTrue(frappe.db.exists("ToDo", self.parent))
        self._assert_commit_blocked()
        self._assert_full_rollback_recovers()

    def test_successful_rollback_preserves_parent_and_allows_repeated_use(self):
        from kopos_connector.utils.diagnostics import make_savepoint, rollback_to_savepoint
        owned = make_savepoint("owned_probe")
        for _ in range(2):
            inner = self._sentinel()
            rollback_to_savepoint(owned, title="Synthetic rollback failed")
            self.assertFalse(frappe.db.exists("ToDo", inner))
            self.assertTrue(frappe.db.exists("ToDo", self.parent))
        frappe.db.before_commit.run()

    def _create_log(self):
        return create_projection_log("ToDo", self.parent, "Sales Invoice", self.parent, "probe")

    def test_native_create_deduplication_and_state_updates_are_unchanged(self):
        name = self._create_log()
        self.assertEqual(self._create_log(), name)
        for count in (1, 2):
            self.assertEqual(update_projection_state(name, "Failed", error="Synthetic failure"), name)
            self.assertEqual(frappe.db.get_value("FB Projection Log", name, "retry_count"), count)
        self.assertEqual(update_projection_state(name, "Succeeded"), name)
        self.assertEqual(frappe.db.get_value("FB Projection Log", name, "state"), "Succeeded")
        frappe.db.before_commit.run()

    def test_create_cannot_recover_its_own_partial_insert_after_failed_rollback(self):
        from kopos_connector.kopos.services.projection import log_service
        from kopos_connector.kopos.doctype.fb_projection_log.fb_projection_log import FBProjectionLog
        native_insert = FBProjectionLog.insert
        inserted = []
        with patch.object(frappe.db, "savepoint", wraps=frappe.db.savepoint) as savepoint:
            def fail_after_insert(doc, *args, **kwargs):
                native_insert(doc, *args, **kwargs)
                inserted.append(doc.name)
                frappe.db.release_savepoint(savepoint.call_args.args[0])
                raise RuntimeError("Synthetic post-insert fault")

            with patch.object(FBProjectionLog, "insert", fail_after_insert), patch.object(
                log_service, "_find_existing_projection", wraps=log_service._find_existing_projection
            ) as lookup:
                with self.assertRaises(Exception):
                    self._create_log()
                self.assertEqual(lookup.call_count, 1, "No duplicate recovery after rollback failure")
        self.assertEqual(len(inserted), 1)
        self.assertTrue(frappe.db.exists("FB Projection Log", inserted[0]))
        self._assert_commit_blocked()
        self._assert_full_rollback_recovers()
        self.assertFalse(frappe.db.exists("FB Projection Log", inserted[0]))

    def test_create_failure_with_successful_rollback_retains_normal_recovery_path(self):
        from kopos_connector.kopos.services.projection import log_service
        from kopos_connector.kopos.doctype.fb_projection_log.fb_projection_log import FBProjectionLog
        native_insert = FBProjectionLog.insert
        inserted = []

        def fail_after_insert(doc, *args, **kwargs):
            native_insert(doc, *args, **kwargs)
            inserted.append(doc.name)
            raise RuntimeError("Synthetic post-insert fault")

        with patch.object(FBProjectionLog, "insert", fail_after_insert):
            with self.assertRaises(log_service.ProjectionLogError):
                self._create_log()
        self.assertEqual(len(inserted), 1)
        self.assertFalse(frappe.db.exists("FB Projection Log", inserted[0]))
        self.assertTrue(frappe.db.exists("ToDo", self.parent))
        frappe.db.before_commit.run()

    def test_update_failure_cannot_commit_its_partial_state(self):
        from kopos_connector.kopos.doctype.fb_projection_log.fb_projection_log import FBProjectionLog
        name = self._create_log()
        native_save = FBProjectionLog.save
        with patch.object(frappe.db, "savepoint", wraps=frappe.db.savepoint) as savepoint:
            def fail_after_save(doc, *args, **kwargs):
                native_save(doc, *args, **kwargs)
                frappe.db.release_savepoint(savepoint.call_args.args[0])
                raise RuntimeError("Synthetic post-save fault")

            with patch.object(FBProjectionLog, "save", fail_after_save):
                with self.assertRaises(Exception):
                    update_projection_state(name, "Succeeded")
        self.assertEqual(frappe.db.get_value("FB Projection Log", name, "state"), "Succeeded")
        self._assert_commit_blocked()
        self._assert_full_rollback_recovers()
        self.assertFalse(frappe.db.exists("FB Projection Log", name))
