# Copyright (c) 2026, KoPOS
# For license information, please see license.txt

from importlib import import_module

from kopos_connector.kopos.api.money_contract import (
    MoneyContractValidationError,
    persisted_money_to_sen,
    sen_to_decimal,
)

frappe = import_module("frappe")
Document = import_module("frappe.model.document").Document
frappe_utils = import_module("frappe.utils")

cstr = frappe_utils.cstr
flt = frappe_utils.flt
now = frappe_utils.now


BLOCKING_PROJECTION_STATUSES = {"Pending", "Failed"}
BLOCKING_PROJECTION_TYPES = {"Sales Invoice", "Stock Issue", "Stock Entry", "FB Shift"}
QUERY_PAGE_SIZE = 500


def _money_sen(value: object, fieldname: str) -> int:
    try:
        return persisted_money_to_sen(value, fieldname)
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error


class FBShift(Document):
    def validate(self):
        self.calculate_variance()
        self.validate_status_transitions()

    def calculate_variance(self):
        """Calculate cash variance if counted cash is provided"""
        if self.counted_cash is not None and self.expected_cash is not None:
            counted_cash_sen = _money_sen(
                self.counted_cash,
                f"FB Shift {getattr(self, 'name', None) or 'new'} counted_cash",
            )
            expected_cash_sen = _money_sen(
                self.expected_cash,
                f"FB Shift {getattr(self, 'name', None) or 'new'} expected_cash",
            )
            self.cash_variance = sen_to_decimal(
                counted_cash_sen - expected_cash_sen
            )

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
    """Calculate exact cash from submitted ERP accounting evidence."""
    from kopos_connector.kopos.services.accounting.return_invoice_service import (
        calculate_fb_shift_cash,
    )

    return calculate_fb_shift_cash(shift_name)


def get_shift_close_projection_blockers(shift_name):
    orders = _get_all_rows(
        "FB Order",
        filters={"shift": shift_name, "status": "Submitted"},
        fields=["name", "invoice_status", "stock_status"],
        order_by="creation asc",
    )
    blockers = []
    order_names = [
        cstr(_row_value(order, "name")).strip()
        for order in orders
        if cstr(_row_value(order, "name")).strip()
    ]
    stock_required_by_order = _orders_requiring_stock_projection(order_names)

    for order in orders:
        order_name = _row_value(order, "name")

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
        projection_logs = _get_all_rows(
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
    return _orders_requiring_stock_projection([order_name]).get(order_name, True)


def _orders_requiring_stock_projection(order_names):
    """Resolve stock requirements in two bounded queries, not one query per order."""

    normalized_order_names = sorted(
        {cstr(order_name).strip() for order_name in order_names if cstr(order_name).strip()}
    )
    if not normalized_order_names:
        return {}

    required_by_order = {order_name: True for order_name in normalized_order_names}
    resolved_sales = _get_all_rows(
        "FB Resolved Sale",
        filters={"fb_order": ["in", normalized_order_names]},
        fields=["name", "fb_order", "booth_warehouse"],
        order_by="creation asc",
    )
    if not resolved_sales:
        return required_by_order

    sale_to_order = {}
    sale_to_warehouse = {}
    for sale in resolved_sales:
        sale_name = cstr(_row_value(sale, "name")).strip()
        order_name = cstr(_row_value(sale, "fb_order")).strip()
        if not sale_name or order_name not in required_by_order:
            continue
        sale_to_order[sale_name] = order_name
        sale_to_warehouse[sale_name] = cstr(
            _row_value(sale, "booth_warehouse")
        ).strip()
        required_by_order[order_name] = False

    if not sale_to_order:
        return required_by_order

    components = _get_all_rows(
        "FB Resolved Component",
        filters={
            "parent": ["in", sorted(sale_to_order)],
            "parenttype": "FB Resolved Sale",
            "parentfield": "resolved_components",
            "affects_stock": 1,
        },
        fields=["parent", "item", "warehouse", "stock_qty", "qty"],
    )
    for component in components:
        sale_name = cstr(_row_value(component, "parent")).strip()
        order_name = sale_to_order.get(sale_name)
        if not order_name:
            continue
        stock_qty = _row_value(component, "stock_qty")
        if stock_qty in (None, ""):
            stock_qty = _row_value(component, "qty")
        item = cstr(_row_value(component, "item")).strip()
        warehouse = cstr(_row_value(component, "warehouse")).strip() or sale_to_warehouse.get(
            sale_name, ""
        )
        if item and warehouse and flt(stock_qty or 0) > 0:
            required_by_order[order_name] = True

    return required_by_order


def _get_all_rows(
    doctype,
    *,
    filters,
    fields,
    order_by=None,
):
    """Read every matching row without relying on Frappe's default page limit."""

    rows = []
    start = 0
    while True:
        page = frappe.get_all(
            doctype,
            filters=filters,
            fields=fields,
            order_by=order_by,
            limit_start=start,
            limit_page_length=QUERY_PAGE_SIZE,
        )
        rows.extend(page)
        if len(page) < QUERY_PAGE_SIZE:
            return rows
        start += len(page)


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
