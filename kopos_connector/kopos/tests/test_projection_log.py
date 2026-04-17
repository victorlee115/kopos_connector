from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.kopos.services.projection.log_service import (
    create_projection_log,
    get_pending_projections,
    retry_failed_projections,
    update_projection_state,
)


class TestProjectionLogService(FrappeTestCase):
    def setUp(self):
        self.cleanup_test_logs()

    def tearDown(self):
        self.cleanup_test_logs()

    def cleanup_test_logs(self):
        frappe.db.delete("FB Projection Log", {"source_doctype": "Test DocType"})
        frappe.db.commit()

    def test_create_projection_log(self):
        log_name = create_projection_log(
            source_doctype="Test DocType",
            source_name="TEST-001",
            projection_type="Sales Invoice",
            idempotency_key="test-key-123",
            payload_hash="abc123",
        )

        self.assertIsNotNone(log_name)

        log = frappe.get_doc("FB Projection Log", log_name)
        self.assertEqual(log.source_doctype, "Test DocType")
        self.assertEqual(log.source_name, "TEST-001")
        self.assertEqual(log.projection_type, "Sales Invoice")
        self.assertEqual(log.state, "Pending")
        self.assertEqual(log.idempotency_key, "test-key-123")
        self.assertEqual(log.payload_hash, "abc123")

    def test_create_duplicate_log_returns_existing(self):
        idempotency_key = "duplicate-test-key"

        log1 = create_projection_log(
            source_doctype="Test DocType",
            source_name="TEST-001",
            projection_type="Sales Invoice",
            idempotency_key=idempotency_key,
            payload_hash="abc123",
        )

        log2 = create_projection_log(
            source_doctype="Test DocType",
            source_name="TEST-001",
            projection_type="Sales Invoice",
            idempotency_key=idempotency_key,
            payload_hash="abc123",
        )

        self.assertEqual(log1, log2)

    def test_update_projection_state_to_success(self):
        log_name = create_projection_log(
            source_doctype="Test DocType",
            source_name="TEST-002",
            projection_type="Stock Issue",
            idempotency_key="test-key-456",
            payload_hash="def456",
        )

        updated = update_projection_state(
            log_name=log_name,
            state="Succeeded",
            target_doctype="Stock Entry",
            target_name="SE-001",
            error=None,
        )

        self.assertIsNotNone(updated)

        log = frappe.get_doc("FB Projection Log", log_name)
        self.assertEqual(log.state, "Succeeded")
        self.assertEqual(log.target_doctype, "Stock Entry")
        self.assertEqual(log.target_name, "SE-001")
        self.assertIsNone(log.last_error)

    def test_update_projection_state_to_failed(self):
        log_name = create_projection_log(
            source_doctype="Test DocType",
            source_name="TEST-003",
            projection_type="Sales Invoice",
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
        self.assertEqual(log.retry_count, 0)

    def test_update_failed_increments_retry_count(self):
        log_name = create_projection_log(
            source_doctype="Test DocType",
            source_name="TEST-004",
            projection_type="Sales Invoice",
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
        create_projection_log(
            source_doctype="Test DocType",
            source_name="PENDING-001",
            projection_type="Sales Invoice",
            idempotency_key="pending-1",
            payload_hash="p1",
        )

        create_projection_log(
            source_doctype="Test DocType",
            source_name="PENDING-002",
            projection_type="Stock Issue",
            idempotency_key="pending-2",
            payload_hash="p2",
        )

        succeeded_log = create_projection_log(
            source_doctype="Test DocType",
            source_name="SUCCEEDED-001",
            projection_type="Sales Invoice",
            idempotency_key="success-1",
            payload_hash="s1",
        )
        update_projection_state(
            log_name=succeeded_log,
            state="Succeeded",
            target_doctype="Sales Invoice",
            target_name="SI-001",
            error=None,
        )

        pending = get_pending_projections()
        pending_names = [p["source_name"] for p in pending]

        self.assertIn("PENDING-001", pending_names)
        self.assertIn("PENDING-002", pending_names)
        self.assertNotIn("SUCCEEDED-001", pending_names)

    def test_retry_failed_projections(self):
        failed_log = create_projection_log(
            source_doctype="Test DocType",
            source_name="FAILED-001",
            projection_type="Sales Invoice",
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

        self.assertTrue(len(retried) > 0)

        log = frappe.get_doc("FB Projection Log", failed_log)
        self.assertEqual(log.state, "Pending")
