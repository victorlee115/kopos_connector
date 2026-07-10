# Copyright (c) 2026, KoPOS
# For license information, please see license.txt

from importlib import import_module

frappe = import_module("frappe")
Document = import_module("frappe.model.document").Document
frappe_utils = import_module("frappe.utils")

cstr = frappe_utils.cstr
flt = frappe_utils.flt
now = frappe_utils.now


BLOCKING_PROJECTION_STATUSES = {"Pending", "Failed"}
BLOCKING_PROJECTION_TYPES = {"Sales Invoice", "Stock Issue", "Stock Entry", "FB Shift"}


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
            validate_shift_can_close(self.name)

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


def get_shift_close_projection_blockers(shift_name):
    orders = frappe.get_all(
        "FB Order",
        filters={"shift": shift_name, "status": "Submitted"},
        fields=["name", "invoice_status", "stock_status"],
        order_by="creation asc",
    )
    blockers = []
    order_names = []
    stock_required_by_order = {}

    for order in orders:
        order_name = _row_value(order, "name")
        if order_name:
            order_names.append(order_name)
            stock_required_by_order[order_name] = _order_requires_stock_projection(
                order_name
            )

        invoice_status = cstr(_row_value(order, "invoice_status"))
        if invoice_status in BLOCKING_PROJECTION_STATUSES:
            blockers.append(
                {
                    "fb_order": order_name,
                    "projection_type": "Sales Invoice",
                    "state": invoice_status,
                    "reason": "invoice_status",
                }
            )

        stock_status = cstr(_row_value(order, "stock_status"))
        if stock_status == "Failed" or (
            stock_status == "Pending" and stock_required_by_order.get(order_name, True)
        ):
            blockers.append(
                {
                    "fb_order": order_name,
                    "projection_type": "Stock Issue",
                    "state": stock_status,
                    "reason": "stock_status",
                }
            )

    if order_names:
        projection_logs = frappe.get_all(
            "FB Projection Log",
            filters={
                "source_doctype": "FB Order",
                "source_name": ["in", order_names],
                "projection_type": ["in", sorted(BLOCKING_PROJECTION_TYPES)],
                "state": ["in", sorted(BLOCKING_PROJECTION_STATUSES)],
            },
            fields=["source_name", "projection_type", "state", "last_error"],
            order_by="creation asc",
        )
        for projection in projection_logs:
            projection_type = cstr(_row_value(projection, "projection_type"))
            projection_state = cstr(_row_value(projection, "state"))
            projection_order = cstr(_row_value(projection, "source_name")) or None
            if _is_noop_stock_projection(
                projection_type,
                projection_state,
                projection_order,
                stock_required_by_order,
            ):
                continue
            blockers.append(
                {
                    "fb_order": projection_order,
                    "projection_type": projection_type,
                    "state": projection_state,
                    "reason": "projection_log",
                    "last_error": cstr(_row_value(projection, "last_error")) or None,
                }
            )

    return _dedupe_close_blockers(blockers)


def validate_shift_can_close(shift_name):
    blockers = get_shift_close_projection_blockers(shift_name)
    if blockers:
        first = blockers[0]
        projection_type = first.get("projection_type") or "projection"
        state = first.get("state") or "unknown"
        fb_order = first.get("fb_order") or "unknown FB Order"
        frappe.throw(
            "FB Shift {0} cannot close while {1} projection for {2} is {3}".format(
                shift_name,
                projection_type,
                fb_order,
                state,
            ),
            frappe.ValidationError,
        )


def _row_value(row, fieldname):
    if isinstance(row, dict):
        return row.get(fieldname)
    getter = getattr(row, "get", None)
    if callable(getter):
        return getter(fieldname)
    return getattr(row, fieldname, None)


def _order_requires_stock_projection(order_name):
    if not order_name:
        return True

    resolved_sales = frappe.get_all(
        "FB Resolved Sale",
        filters={"fb_order": order_name},
        fields=["name"],
        order_by="creation asc",
    )
    if not resolved_sales:
        return True

    for resolved_sale_row in resolved_sales:
        resolved_sale_name = _row_value(resolved_sale_row, "name")
        if not resolved_sale_name:
            continue
        resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sale_name)
        for component in list(getattr(resolved_sale, "resolved_components", None) or []):
            if not int(getattr(component, "affects_stock", 0) or 0):
                continue
            item = getattr(component, "item", None)
            warehouse = getattr(component, "warehouse", None) or getattr(
                resolved_sale,
                "booth_warehouse",
                None,
            )
            qty = flt(
                getattr(component, "stock_qty", None)
                or getattr(component, "qty", None)
                or 0
            )
            if item and warehouse and qty > 0:
                return True
    return False


def _is_noop_stock_projection(
    projection_type,
    projection_state,
    projection_order,
    stock_required_by_order,
):
    if projection_type not in {"Stock Issue", "Stock Entry"}:
        return False
    if projection_state != "Pending":
        return False
    if not projection_order:
        return False
    return not stock_required_by_order.get(projection_order, True)


def _dedupe_close_blockers(blockers):
    deduped = []
    seen = set()
    for blocker in blockers:
        key = (
            blocker.get("fb_order"),
            blocker.get("projection_type"),
            blocker.get("state"),
            blocker.get("reason"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(blocker)
    return deduped
