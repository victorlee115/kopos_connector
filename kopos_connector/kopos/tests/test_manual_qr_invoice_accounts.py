"""Where a manually confirmed QR sale's tender lands in the ledger.

ERPNext rewrites every Sales Invoice payment row account from its Mode of Payment on
each save, so a manual QR sale only keeps its suspense account if the connector puts it
back. These run against a real site so the native save is the one under test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.kopos.api.fb_orders import submit_order_payload
from kopos_connector.kopos.api.money_contract import persisted_money_to_sen
from kopos_connector.kopos.services.accounting import sales_invoice_service
from kopos_connector.kopos.services.accounting.sales_invoice_service import create_sales_invoice
from kopos_connector.kopos.tests.frappe_test_fixtures import (
    build_sen_v1_sale_payload,
    create_open_test_shift,
)

PREFIX = "KOPOS-MANUAL-QR"


def _sen(value: Any) -> int:
    return persisted_money_to_sen(value or 0, "manual QR account test")


class TestManualQrInvoiceAccounts(FrappeTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        self.shift = create_open_test_shift(prefix=PREFIX, replenish_stock=False)
        self.company = self.shift.company
        self.suspense = self._account("Manual QR Suspense")
        self.mode_account = self._account("QR Mode Of Payment")
        mode = frappe.get_doc("Mode of Payment", "DuitNow QR")
        next(row for row in mode.accounts if row.company == self.company).default_account = self.mode_account
        mode.save(ignore_permissions=True)
        self.profile = frappe.get_doc(
            "POS Profile", frappe.db.get_value("KoPOS Device", {"device_id": self.shift.device_id}, "pos_profile")
        )
        self.profile.custom_kopos_manual_qr_suspense_account = self.suspense
        self.profile.save(ignore_permissions=True)

    def tearDown(self) -> None:
        frappe.db.rollback()

    def _account(self, label: str) -> str:
        parent = frappe.db.get_value("Account", {"company": self.company, "root_type": "Asset", "is_group": 1}, "name")
        return frappe.get_doc({
            "doctype": "Account", "account_name": f"{label} {frappe.generate_hash(length=8)}",
            "parent_account": parent, "company": self.company, "is_group": 0,
            "account_currency": frappe.db.get_value("Company", self.company, "default_currency"),
        }).insert(ignore_permissions=True).name

    def _evidence(self, suffix: str, reference: str) -> dict[str, Any]:
        captured_at = frappe.utils.now_datetime().isoformat()
        return {
            "evidence_kind": "no_receipt_acknowledgement", "captured_at": captured_at,
            "upload_status": "not_required", "reconciliation_status": "pending_reconciliation",
            "local_confirmed_at": captured_at, "local_confirmed_by": self.shift.staff_id,
            "local_confirmation_reference": reference,
            "reconciliation_idempotency_key": f"{PREFIX}-RECON-{suffix}",
            "evidence_captured_device_id": self.shift.device_id,
            "no_receipt_acknowledged": True, "no_receipt_reason_code": "receipt_unavailable",
        }

    def _issued_maybank_transaction(self, suffix: str, reference: str) -> None:
        """A QR the provider issued to this tablet, still waiting for provider settlement."""
        account = frappe.get_doc({
            "doctype": "Maybank QRPayBiz Account", "display_name": f"{PREFIX}-{suffix}",
            "username": f"{PREFIX}-{suffix}", "enabled": 0, "environment": "production",
            "encrypted_pin": "1234", "user_type": "corporate",
            "provider_device_id": frappe.generate_hash(length=32),
            "provider_device_name": "KoPOS Test Tablet", "provider_device_os": "Android 13",
        })
        account.flags.notify_update = False
        account.insert(ignore_permissions=True)
        frappe.get_doc({
            "doctype": "Maybank QR Transaction", "transaction_refno": reference, "status": "pending",
            "qr_data": f"00020101021226{suffix}", "sale_amount": 12, "sale_amount_sen": 1200,
            "device_id": self.shift.device_id, "company": self.company, "currency": "MYR",
            "outlet_id": f"{PREFIX}-OUTLET", "maybank_qrpaybiz_account": account.name,
            "pos_profile": self.profile.name, "suspense_account": self.suspense,
            "clearing_account": self.mode_account, "provider": "maybank_qr",
            "idempotency_key": f"{PREFIX}-QR-{suffix}", "request_fingerprint": f"{PREFIX}-FP-{suffix}",
            "expires_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=10),
            "created_at": frappe.utils.now_datetime(), "business_date": frappe.utils.nowdate(),
        }).insert(ignore_permissions=True)

    def _manual_qr_sale(self, channel: str) -> Any:
        """A manually confirmed QR sale of one item, as the tablet registers it."""
        suffix = frappe.generate_hash(length=10)
        reference = f"MBTEST-{suffix}" if channel == "maybank" else f"{PREFIX}-{suffix}"
        session = reference if channel == "maybank" else f"static-{suffix}"
        if channel == "maybank":
            self._issued_maybank_transaction(suffix, reference)
        payload = build_sen_v1_sale_payload(self.shift, prefix=PREFIX, quantity=1)
        payload["order"]["payments"][0].update({
            "payment_method": "DuitNow QR", "payment_channel_code": channel,
            "reference_no": reference, "external_transaction_id": session,
            "manual_confirmation_warning_acknowledged": True,
            "manual_confirmation_evidence": self._evidence(suffix, reference),
        })
        return frappe.get_doc("FB Order", submit_order_payload(payload)["fb_order"])

    def _invoice_payment_account(self, order: Any) -> tuple[str, list[dict[str, Any]]]:
        # The projection swallows its failure, so keep the reason for the assertion.
        failures: list[str] = []
        with patch.object(
            sales_invoice_service, "_log_error",
            side_effect=lambda title, error=None: failures.append(f"{title}: {error}"),
        ):
            invoice_name = create_sales_invoice(order)
        self.assertTrue(invoice_name, f"the manual QR sale produced no Sales Invoice: {failures}")
        invoice = frappe.get_doc("Sales Invoice", invoice_name)
        entries = frappe.get_all(
            "GL Entry", filters={"voucher_type": "Sales Invoice", "voucher_no": invoice.name, "is_cancelled": 0},
            fields=["account", "debit", "credit"],
        )
        return invoice.payments[0].account, entries

    def test_a_manually_confirmed_static_qr_sale_lands_in_the_suspense_account(self) -> None:
        account, entries = self._invoice_payment_account(self._manual_qr_sale("static_qr"))

        self.assertEqual(account, self.suspense)
        self.assertEqual(sum(_sen(row.debit) - _sen(row.credit) for row in entries if row.account == self.suspense), 1200)
        self.assertEqual([row for row in entries if row.account == self.mode_account], [])

    def test_a_manually_confirmed_maybank_qr_sale_lands_in_the_suspense_account(self) -> None:
        account, entries = self._invoice_payment_account(self._manual_qr_sale("maybank"))

        self.assertEqual(account, self.suspense)
        self.assertEqual(sum(_sen(row.debit) - _sen(row.credit) for row in entries if row.account == self.suspense), 1200)
        self.assertEqual([row for row in entries if row.account == self.mode_account], [])
