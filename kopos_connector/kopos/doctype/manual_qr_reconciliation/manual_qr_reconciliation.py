from __future__ import annotations

import frappe
from frappe.utils import cstr
from frappe.model.document import Document


class ManualQRReconciliation(Document):
    def validate(self) -> None:
        claim_role = cstr(self.get("claim_role")).strip() or "winning_settlement"
        winning_transaction = cstr(
            self.get("winning_maybank_qr_transaction")
        ).strip()
        suspense_account = cstr(self.get("suspense_account")).strip()

        if claim_role == "secondary_possible_duplicate":
            if not winning_transaction:
                frappe.throw(
                    "Secondary static QR claim requires the winning Maybank QR transaction",
                    frappe.ValidationError,
                )
            if suspense_account:
                frappe.throw(
                    "Secondary static QR claim must not carry a suspense account",
                    frappe.ValidationError,
                )
            finance_status = cstr(
                self.get("finance_resolution_status")
            ).strip() or "pending_review"
            status = cstr(self.get("status")).strip()
            if finance_status == "pending_review" and status != "pending_reconciliation":
                frappe.throw(
                    "Pending secondary static QR claim must remain pending reconciliation",
                    frappe.ValidationError,
                )
            if finance_status == "no_second_credit" and (
                status != "reconciliation_failed"
                or cstr(self.get("finance_resolution_decision")).strip()
                != "no_second_credit"
                or not cstr(self.get("finance_resolution_key")).strip()
                or cstr(self.get("finance_liability_journal_entry")).strip()
                or cstr(self.get("finance_refund_journal_entry")).strip()
            ):
                frappe.throw(
                    "No-second-credit finance resolution evidence is incomplete",
                    frappe.ValidationError,
                )
            if finance_status == "refund_required" and (
                status != "pending_reconciliation"
                or cstr(self.get("finance_resolution_decision")).strip()
                != "independent_second_credit"
                or not cstr(self.get("finance_resolution_key")).strip()
                or not cstr(self.get("finance_liability_journal_entry")).strip()
                or cstr(self.get("finance_refund_journal_entry")).strip()
            ):
                frappe.throw(
                    "Independent second-credit liability evidence is incomplete",
                    frappe.ValidationError,
                )
            if finance_status == "refunded" and (
                status != "reconciled"
                or cstr(self.get("finance_resolution_decision")).strip()
                != "independent_second_credit"
                or not cstr(self.get("finance_liability_journal_entry")).strip()
                or not cstr(self.get("finance_refund_journal_entry")).strip()
                or not cstr(self.get("finance_resolved_by")).strip()
                or not cstr(self.get("finance_resolved_at")).strip()
            ):
                frappe.throw(
                    "Independent second-credit refund evidence is incomplete",
                    frappe.ValidationError,
                )
            if finance_status not in {
                "pending_review",
                "no_second_credit",
                "refund_required",
                "refunded",
            }:
                frappe.throw(
                    "Secondary static QR finance resolution state is invalid",
                    frappe.ValidationError,
                )
            return

        if claim_role != "winning_settlement":
            frappe.throw("Static QR claim role is invalid", frappe.ValidationError)
        if winning_transaction:
            frappe.throw(
                "Winning static QR settlement must not reference a Maybank winner",
                frappe.ValidationError,
            )
        if cstr(self.get("finance_resolution_status")).strip():
            frappe.throw(
                "Winning static QR settlement must not carry reverse-winner finance state",
                frappe.ValidationError,
            )
        if not suspense_account:
            frappe.throw(
                "Winning static QR settlement requires a suspense account",
                frappe.ValidationError,
            )
