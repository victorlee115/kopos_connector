from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.api.fb_returns import process_return_payload
from kopos_connector.kopos.api.fb_orders import submit_order_payload
from kopos_connector.kopos.api.money_contract import persisted_money_to_sen
from kopos_connector.kopos.tests.frappe_test_fixtures import (
    build_sen_v1_sale_payload,
    create_open_test_shift,
)


class TestEndToEndCommercialRefundFlow(FrappeTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        self.shift = create_open_test_shift(
            prefix="KOPOS-E2E-COMMERCIAL-RETURN",
            replenish_stock=False,
        )
        self.sale_payload = build_sen_v1_sale_payload(
            self.shift,
            prefix="KOPOS-E2E-COMMERCIAL-RETURN",
        )
        sale_result = submit_order_payload(self.sale_payload)
        self.order = frappe.get_doc("FB Order", sale_result["fb_order"])
        self.return_id = (
            "KOPOS-E2E-COMMERCIAL-RETURN-"
            + frappe.generate_hash(length=12)
        )

    def tearDown(self) -> None:
        frappe.db.rollback()

    def _payload(self, *, return_to_stock: bool) -> dict[str, object]:
        return {
            "return_id": self.return_id,
            "idempotency_key": self.return_id,
            "device_id": self.order.device_id,
            "fb_order": self.order.name,
            "original_sales_invoice": self.order.sales_invoice,
            "reason_code": "Quality Issue",
            "reason_text": "Commercial refund round-trip fixture",
            "refund_method": "cash",
            "return_to_stock": 1 if return_to_stock else 0,
        }

    def test_full_refund_round_trips_without_resolved_sale_or_stock_calls(self) -> None:
        self.assertEqual(
            frappe.get_all(
                "FB Resolved Sale",
                filters={"fb_order": self.order.name},
                pluck="name",
            ),
            [],
        )

        with patch(
            "kopos_connector.kopos.services.inventory.stock_reversal_service.create_reversal_stock_entry",
            side_effect=AssertionError("cashier refund called inventory"),
        ):
            result = process_return_payload(
                self._payload(return_to_stock=True),
                require_manager_approval=False,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["settlement_status"], "Posted")
        self.assertEqual(result["return_to_stock"], 0)
        self.assertEqual(
            result["inventory_evaluation"],
            "included_evaluated",
        )
        self.assertEqual(result["reversal_stock_entries"], [])

        return_event = frappe.get_doc("FB Return Event", result["return_event"])
        return_event.reload()
        self.assertEqual(return_event.docstatus, 1)
        self.assertEqual(return_event.return_to_stock, 0)
        self.assertEqual(len(return_event.lines), len(self.order.items))
        self.assertTrue(
            all(line.original_sales_invoice_item for line in return_event.lines)
        )
        self.assertTrue(
            all(line.original_fb_order_line_ref for line in return_event.lines)
        )
        self.assertTrue(
            all(
                not line.commercial_modifier_snapshot_json
                for line in return_event.lines
            )
        )
        self.assertTrue(
            all(not line.original_resolved_sale for line in return_event.lines)
        )

        original_invoice = frappe.get_doc(
            "Sales Invoice", self.order.sales_invoice
        )
        return_invoice = frappe.get_doc(
            "Sales Invoice", result["return_sales_invoice"]
        )
        self.assertEqual(return_invoice.docstatus, 1)
        self.assertEqual(return_invoice.is_return, 1)
        self.assertEqual(return_invoice.return_against, original_invoice.name)
        self.assertEqual(return_invoice.outstanding_amount, 0)
        self.assertEqual(
            {line.sales_invoice_item for line in return_invoice.items},
            {line.name for line in original_invoice.items},
        )
        self.assertEqual(
            {line.custom_fb_order_line_ref for line in return_invoice.items},
            {line.custom_fb_order_line_ref for line in original_invoice.items},
        )

        settlement = frappe.get_doc(
            result["settlement_doctype"], result["settlement_document"]
        )
        self.assertEqual(settlement.docstatus, 1)
        self.assertEqual(settlement.custom_fb_return_event, return_event.name)

    def test_idempotent_replay_reuses_one_credit_note_and_settlement(self) -> None:
        payload = self._payload(return_to_stock=False)
        first = process_return_payload(payload, require_manager_approval=False)
        duplicate = process_return_payload(payload, require_manager_approval=False)

        self.assertEqual(first["status"], "ok")
        self.assertEqual(duplicate["status"], "duplicate")
        for fieldname in (
            "return_event",
            "return_sales_invoice",
            "settlement_doctype",
            "settlement_document",
        ):
            self.assertEqual(duplicate[fieldname], first[fieldname])
        self.assertEqual(
            frappe.get_all(
                "FB Return Event",
                filters={"return_id": self.return_id},
                pluck="name",
            ),
            [first["return_event"]],
        )
        self.assertEqual(
            frappe.get_all(
                "Sales Invoice",
                filters={
                    "name": first["return_sales_invoice"],
                    "docstatus": 1,
                    "is_return": 1,
                },
                pluck="name",
            ),
            [first["return_sales_invoice"]],
        )
        self.assertEqual(
            frappe.get_all(
                "Journal Entry",
                filters={
                    "custom_fb_return_event": first["return_event"],
                    "docstatus": 1,
                },
                pluck="name",
            ),
            [first["settlement_document"]],
        )

    def test_qr_clearing_refund_retains_original_account_after_payment_mode_changes(self) -> None:
        """Real ERPNext ledger/credit-note proof; no provider or current-config fallback."""
        cash_invoice = frappe.get_doc("Sales Invoice", self.order.sales_invoice)
        parent = frappe.db.get_value(
            "Account", {"company": cash_invoice.company, "root_type": "Asset", "is_group": 1}, "name"
        )
        self.assertTrue(parent, "Refund fixture requires the company's native Asset group")
        suffix = frappe.generate_hash(length=10)
        accounts = []
        for label, account_type in (("Original QR Clearing", ""), ("Replacement QR Bank", "Bank")):
            account = frappe.get_doc({
                "doctype": "Account", "account_name": f"{label} {suffix}",
                "parent_account": parent, "company": cash_invoice.company,
                "account_currency": cash_invoice.currency, "is_group": 0,
                "account_type": account_type,
            }).insert(ignore_permissions=True)
            accounts.append(account.name)
        clearing, replacement = accounts
        mode = frappe.get_doc("Mode of Payment", "DuitNow QR")
        mode_account = next(row for row in mode.accounts if row.company == cash_invoice.company)
        mode_account.default_account = clearing
        mode.save(ignore_permissions=True)
        profile_name = frappe.db.get_value(
            "KoPOS Device", {"device_id": self.order.device_id}, "pos_profile"
        )
        profile = frappe.get_doc("POS Profile", profile_name)
        profile.custom_kopos_manual_qr_suspense_account = clearing
        profile.save(ignore_permissions=True)

        # Use the real sale API and immutable FB Order binding. This synthetic
        # manual confirmation exercises accounting, not live provider acceptance.
        sale = build_sen_v1_sale_payload(self.shift, prefix="KOPOS-E2E-QR-REFUND")
        payment = sale["order"]["payments"][0]
        payment_id = payment["payment_id"]
        reference = f"QR-REFUND-REFERENCE-{suffix}"
        captured_at = frappe.utils.now_datetime().isoformat()
        payment.update({
            "payment_method": "DuitNow QR", "payment_channel_code": "static_qr",
            "reference_no": reference, "external_transaction_id": f"static-{suffix}",
            "manual_confirmation_warning_acknowledged": True,
            "manual_confirmation_evidence": {
                "evidence_kind": "no_receipt_acknowledgement", "captured_at": captured_at,
                "upload_status": "not_required", "reconciliation_status": "pending_reconciliation",
                "local_confirmed_at": captured_at, "local_confirmed_by": self.shift.staff_id,
                "local_confirmation_reference": reference,
                "reconciliation_idempotency_key": f"QR-REFUND-RECONCILIATION-{suffix}",
                "evidence_captured_device_id": self.order.device_id,
                "no_receipt_acknowledged": True, "no_receipt_reason_code": "receipt_unavailable",
            },
        })
        sale_result = submit_order_payload(sale)
        qr_order = frappe.get_doc("FB Order", sale_result["fb_order"])
        original = frappe.get_doc("Sales Invoice", qr_order.sales_invoice)
        self.assertEqual(original.custom_fb_order, qr_order.name)
        self.assertEqual(original.payments[0].account, clearing)
        self.assertEqual(original.payments[0].custom_fb_source_payment_id, payment_id)
        self.assertEqual(persisted_money_to_sen(original.outstanding_amount, "original outstanding"), 0)

        # A later configuration change must not move historical refund money.
        mode.reload()
        mode_account = next(row for row in mode.accounts if row.company == cash_invoice.company)
        mode_account.default_account = replacement
        mode.save(ignore_permissions=True)
        payload = {
            **self._payload(return_to_stock=False), "fb_order": qr_order.name,
            "original_sales_invoice": original.name, "refund_method": "qr",
        }
        first = process_return_payload(payload, require_manager_approval=False)
        duplicate = process_return_payload(payload, require_manager_approval=False)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(first["settlement_document"], duplicate["settlement_document"])
        self.assertEqual(first["return_sales_invoice"], duplicate["return_sales_invoice"])
        credit = frappe.get_doc("Sales Invoice", first["return_sales_invoice"])
        self.assertEqual(credit.docstatus, 1)
        self.assertEqual(persisted_money_to_sen(credit.outstanding_amount, "credit outstanding"), 0)
        journal = frappe.get_doc("Journal Entry", first["settlement_document"])
        self.assertEqual(journal.docstatus, 1)
        gl = frappe.get_all("GL Entry", filters={
            "voucher_type": "Journal Entry", "voucher_no": journal.name, "is_cancelled": 0,
        }, fields=["account", "debit_in_account_currency", "credit_in_account_currency"])
        self.assertNotIn(replacement, {row.account for row in gl})
        tender_credit = sum(
            persisted_money_to_sen(row.credit_in_account_currency, "refund GL credit")
            - persisted_money_to_sen(row.debit_in_account_currency, "refund GL debit")
            for row in gl if row.account == clearing
        )
        self.assertEqual(tender_credit, persisted_money_to_sen(original.grand_total, "original total"))
        original.reload()
        self.assertEqual(original.payments[0].account, clearing)
        self.assertEqual(original.payments[0].custom_fb_source_payment_id, payment_id)
        mode.reload()
        self.assertEqual(
            next(row.default_account for row in mode.accounts if row.company == cash_invoice.company),
            replacement,
        )

    def test_historical_invoice_without_order_line_reference_refunds_without_optional_recipe_rows(
        self,
    ) -> None:
        original_invoice = frappe.get_doc(
            "Sales Invoice", self.order.sales_invoice
        )
        for item in original_invoice.items:
            frappe.db.set_value(
                "Sales Invoice Item",
                item.name,
                {
                    "custom_fb_order_line_ref": None,
                    "custom_fb_resolved_sale": "STALE-OPTIONAL-IDENTITY",
                    "custom_kopos_modifiers": "{corrupt optional decoration",
                },
                update_modified=False,
            )
        for item in self.order.items:
            frappe.db.set_value(
                "FB Order Line",
                item.name,
                "commercial_modifier_snapshot_json",
                "{corrupt optional decoration",
                update_modified=False,
            )

        native_sql = frappe.db.sql

        def commercial_sql(query, *args, **kwargs):
            self.assertNotIn("tabFB Resolved Sale", str(query))
            return native_sql(query, *args, **kwargs)

        # Observe the real boundary, not the name of a removed private helper.
        with patch.object(frappe.db, "sql", side_effect=commercial_sql):
            result = process_return_payload(
                self._payload(return_to_stock=False),
                require_manager_approval=False,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["settlement_status"], "Posted")
        return_event = frappe.get_doc("FB Return Event", result["return_event"])
        self.assertTrue(
            all(line.original_sales_invoice_item for line in return_event.lines)
        )
        self.assertTrue(
            all(not line.original_fb_order_line_ref for line in return_event.lines)
        )
        self.assertTrue(
            all(not line.original_resolved_sale for line in return_event.lines)
        )
