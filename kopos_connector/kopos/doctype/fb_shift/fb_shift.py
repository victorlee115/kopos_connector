# Copyright (c) 2026, KoPOS
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now


class FBShift(Document):
    def validate(self):
        self.calculate_variance()
        self.validate_status_transitions()

    def calculate_variance(self):
        """Calculate cash variance if counted cash is provided"""
        if self.counted_cash is not None and self.expected_cash is not None:
            self.cash_variance = self.counted_cash - self.expected_cash

    def validate_status_transitions(self):
        """Validate status transitions"""
        valid_transitions = {
            "Open": ["Closing", "Cancelled"],
            "Closing": ["Closed", "Exception", "Open"],
            "Closed": [],
            "Exception": ["Closing", "Open"],
            "Cancelled": [],
        }

        if self.is_new():
            return

        old_status = frappe.db.get_value("FB Shift", self.name, "status")
        if old_status and self.status != old_status:
            if self.status not in valid_transitions.get(old_status, []):
                frappe.throw(
                    f"Invalid status transition from {old_status} to {self.status}"
                )

    def before_submit(self):
        """Validate before submitting"""
        if self.status != "Open":
            frappe.throw("Shift must be Open to submit")

    def on_submit(self):
        """Handle shift submission"""
        pass

    def before_update_after_submit(self):
        """Validate updates after submit"""
        if self.status == "Closed":
            # Check for any pending projections
            pending_orders = frappe.get_all(
                "FB Order",
                filters={
                    "shift": self.name,
                    "status": "Submitted",
                    "invoice_status": ["in", ["Pending", "Failed"]],
                },
                limit=1,
            )
            if pending_orders:
                self.status = "Exception"
                self.close_blocked_reason = "Pending order projections exist"
                frappe.msgprint(
                    "Shift moved to Exception: Pending order projections exist"
                )

    def on_update(self):
        """Handle shift updates"""
        if self.status == "Closed" and not self.closed_at:
            self.closed_at = now()
            self.db_set("closed_at", self.closed_at)


@frappe.whitelist()
def get_shift_expected_cash(shift_name):
    """Calculate expected cash for a shift based on orders"""
    shift = frappe.get_doc("FB Shift", shift_name)

    # Get all orders for this shift
    orders = frappe.get_all(
        "FB Order",
        filters={"shift": shift_name, "status": "Submitted"},
        fields=["name", "grand_total", "sales_invoice"],
    )

    total_cash = 0
    for order in orders:
        if order.sales_invoice:
            # Get payment details from Sales Invoice
            si = frappe.get_doc("Sales Invoice", order.sales_invoice)
            for payment in si.payments:
                if payment.mode_of_payment == "Cash":
                    total_cash += payment.amount

    return {
        "opening_float": shift.opening_float,
        "cash_sales": total_cash,
        "expected_cash": shift.opening_float + total_cash,
    }
