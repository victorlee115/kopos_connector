from __future__ import annotations

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
