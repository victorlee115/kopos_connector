from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.kopos.api.fb_orders import _retry_projection_log, submit_order_payload
from kopos_connector.kopos.services.projection.retry_service import (
    COMMERCIAL_ORDER_PROJECTION_TYPES,
)
from kopos_connector.kopos.tests.frappe_test_fixtures import (
    build_sen_v1_sale_payload,
    create_open_test_shift,
)
from kopos_connector.patches import requeue_stranded_commercial_projections


PREFIX = "KOPOS-STRANDED-SALE"


class TestStrandedCommercialProjectionRecovery(FrappeTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        shift = create_open_test_shift(prefix=PREFIX, replenish_stock=False)
        with patch(
            "kopos_connector.kopos.doctype.fb_order.fb_order.create_sales_invoice",
            side_effect=RuntimeError("ERP outage while posting"),
        ):
            result = submit_order_payload(build_sen_v1_sale_payload(shift, prefix=PREFIX))
        self.order = frappe.get_doc("FB Order", result["fb_order"])
        self.log_name = frappe.db.get_value(
            "FB Projection Log",
            {
                "source_doctype": "FB Order",
                "source_name": self.order.name,
                "projection_type": "Sales Invoice",
            },
            "name",
        )

    def tearDown(self) -> None:
        frappe.db.rollback()

    def _retry_candidates(self) -> list[str]:
        # The selection shared by the scheduler worker and the cashier retry.
        return frappe.get_all(
            "FB Projection Log",
            filters={
                "source_doctype": "FB Order",
                "state": "Failed",
                "projection_type": ("in", COMMERCIAL_ORDER_PROJECTION_TYPES),
            },
            pluck="name",
        )

    def _invoices(self) -> list[str]:
        return frappe.get_all(
            "Sales Invoice",
            filters={"custom_fb_idempotency_key": self.order.external_idempotency_key},
            pluck="name",
        )

    def test_stranded_pending_sale_is_requeued_and_posts_exactly_one_invoice(self) -> None:
        self.assertEqual(self.order.invoice_status, "Failed")
        self.assertEqual(self._invoices(), [])
        # The production row: a failed sale later rewritten to Pending, error kept.
        frappe.db.set_value(
            "FB Projection Log",
            self.log_name,
            {"state": "Pending", "next_retry_at": None},
            update_modified=False,
        )
        self.assertNotIn(self.log_name, self._retry_candidates())
        with self.assertRaisesRegex(frappe.ValidationError, "cannot be retried from state Pending"):
            _retry_projection_log(self.log_name)

        requeue_stranded_commercial_projections.execute()
        requeue_stranded_commercial_projections.execute()

        log = frappe.get_doc("FB Projection Log", self.log_name)
        self.assertEqual(log.state, "Failed")
        self.assertEqual(
            log.last_error,
            f"{requeue_stranded_commercial_projections.REQUEUE_MESSAGE} Previous error: ERP outage while posting",
        )
        self.assertIn(self.log_name, self._retry_candidates())

        result = _retry_projection_log(self.log_name)

        self.assertEqual(result["state"], "Succeeded")
        self.order.reload()
        self.assertEqual(self.order.invoice_status, "Posted")
        self.assertEqual(self._invoices(), [result["target_name"]])
        self.assertEqual(frappe.db.get_value("Sales Invoice", result["target_name"], "docstatus"), 1)
