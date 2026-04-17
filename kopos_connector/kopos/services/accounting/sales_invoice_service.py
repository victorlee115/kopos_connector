from __future__ import annotations

from typing import Any

import frappe

from kopos_connector.api.devices import elevate_device_api_user


def create_sales_invoice(fb_order: Any) -> str | None:
    order_doc = _coerce_doc("FB Order", fb_order)
    if not order_doc:
        return None

    existing_invoice = _get_existing_reference(order_doc, "sales_invoice")
    if existing_invoice:
        return existing_invoice

    savepoint = _make_savepoint("fb_sales_invoice")

    try:
        with elevate_device_api_user():
            invoice = frappe.new_doc("Sales Invoice")
            invoice.customer = _resolve_customer(order_doc)
            invoice.company = _value(order_doc, "company")
            invoice.currency = _resolve_currency(order_doc)
            invoice.is_pos = 1
            invoice.update_stock = 0
            invoice.set_posting_time = 1
            posting_dt = _resolve_posting_datetime(order_doc)
            invoice.posting_date = posting_dt.date().isoformat()
            invoice.posting_time = posting_dt.time().strftime("%H:%M:%S")
            invoice.due_date = invoice.posting_date
            invoice.project = _value(order_doc, "event_project") or None
            invoice.remarks = _build_invoice_remarks(order_doc)

            _set_if_present(invoice, ["custom_fb_order"], order_doc.name)
            _set_if_present(invoice, ["custom_fb_shift"], _value(order_doc, "shift"))
            _set_if_present(
                invoice,
                ["custom_fb_device_id"],
                _value(order_doc, "device_id"),
            )
            _set_if_present(
                invoice,
                ["custom_fb_event_project"],
                _value(order_doc, "event_project"),
            )
            _set_if_present(
                invoice,
                ["custom_fb_idempotency_key"],
                _value(order_doc, "external_idempotency_key"),
            )
            _set_if_present(
                invoice,
                ["custom_fb_operational_status"],
                _value(order_doc, "status") or "Submitted",
            )

            for order_item in list(_value(order_doc, "items") or []):
                item_code = _value(order_item, "item")
                if not item_code:
                    continue

                item_doc = _coerce_doc("Item", item_code)
                if not item_doc:
                    continue

                qty = float(_value(order_item, "qty") or 0)
                if qty <= 0:
                    continue

                rate = _resolve_line_rate(order_item)
                row = {
                    "item_code": item_doc.name,
                    "item_name": _value(order_item, "item_name_snapshot")
                    or _value(item_doc, "item_name")
                    or item_doc.name,
                    "description": _value(order_item, "remarks")
                    or _value(item_doc, "description")
                    or item_doc.name,
                    "qty": qty,
                    "uom": _value(order_item, "uom") or _value(item_doc, "stock_uom"),
                    "stock_uom": _value(item_doc, "stock_uom"),
                    "conversion_factor": 1,
                    "rate": rate,
                    "warehouse": _value(order_doc, "booth_warehouse") or None,
                    "custom_fb_order_line_ref": _value(order_item, "line_id") or None,
                    "custom_fb_resolved_sale": _value(order_item, "resolved_sale")
                    or None,
                    "custom_fb_recipe_snapshot_json": _value(
                        order_item, "resolved_components_snapshot"
                    )
                    or None,
                    "custom_fb_resolution_hash": _resolve_line_resolution_hash(
                        order_item
                    ),
                }
                invoice.append("items", row)

            if not invoice.items:
                raise ValueError("fb_order has no invoiceable items")

            if hasattr(invoice, "set_missing_values"):
                invoice.set_missing_values()
            _append_tax_rows(invoice, order_doc)
            if hasattr(invoice, "calculate_taxes_and_totals"):
                invoice.calculate_taxes_and_totals()
            _apply_rounding(invoice, order_doc)
            invoice.update_stock = 0

            invoice.set("payments", [])
            _append_payment_rows(invoice, order_doc)

            invoice.insert(ignore_permissions=True)
            invoice.submit()

        _set_source_reference(order_doc, "sales_invoice", invoice.name)
        _set_source_reference(order_doc, "invoice_status", "Posted")
        _link_resolved_sales(order_doc, invoice.name)

        return invoice.name
    except Exception:
        _rollback_savepoint(savepoint)
        _log_error("Sales invoice projection failed")
        return None


def _coerce_doc(doctype: str, value: Any):
    if not value:
        return None
    if getattr(value, "doctype", None) == doctype:
        return value
    try:
        return frappe.get_doc(doctype, value)
    except Exception:
        return None


def _value(doc: Any, fieldname: str) -> Any:
    if hasattr(doc, fieldname):
        return getattr(doc, fieldname)
    getter = getattr(doc, "get", None)
    if callable(getter):
        return getter(fieldname)
    return None


def _resolve_customer(order_doc: Any) -> str:
    customer = _value(order_doc, "customer")
    if customer:
        return customer

    walk_in_customer = frappe.db.exists("Customer", "Walk-in Customer")
    if walk_in_customer:
        return walk_in_customer

    raise ValueError("customer is required to create Sales Invoice")


def _resolve_currency(order_doc: Any) -> str:
    currency = _value(order_doc, "currency")
    if currency:
        return currency

    company = _value(order_doc, "company")
    default_currency = frappe.db.get_value("Company", company, "default_currency")
    if default_currency:
        return default_currency

    raise ValueError("currency is required to create Sales Invoice")


def _resolve_posting_datetime(order_doc: Any):
    created_at = _value(order_doc, "modified") or _value(order_doc, "creation")
    if created_at:
        return frappe.utils.get_datetime(created_at)
    return frappe.utils.now_datetime()


def _resolve_line_rate(order_item: Any) -> float:
    qty = float(_value(order_item, "qty") or 0)
    line_total = float(_value(order_item, "line_total") or 0)
    if qty > 0 and line_total:
        return line_total / qty
    return float(_value(order_item, "unit_price") or 0)


def _append_payment_rows(invoice: Any, order_doc: Any) -> None:
    payment_rows = list(_value(order_doc, "payments") or [])
    if not payment_rows:
        return

    try:
        from erpnext.accounts.doctype.sales_invoice.sales_invoice import (
            get_mode_of_payment_info,
        )
    except Exception:
        get_mode_of_payment_info = None

    total_paid = 0.0
    total_change = 0.0

    for index, payment in enumerate(payment_rows, start=1):
        mode_of_payment = _value(payment, "payment_method")
        amount = float(_value(payment, "amount") or 0)
        if not mode_of_payment or amount <= 0:
            continue

        payment_row = {
            "idx": index,
            "mode_of_payment": mode_of_payment,
            "amount": amount,
            "reference_no": _value(payment, "reference_no") or None,
        }

        if get_mode_of_payment_info:
            mode_info = get_mode_of_payment_info(mode_of_payment, invoice.company)
            if mode_info:
                payment_meta = mode_info[0]
                payment_row["account"] = payment_meta.get("account")
                payment_row["type"] = payment_meta.get("type")

        invoice.append("payments", payment_row)
        total_paid += amount
        total_change += float(_value(payment, "change_amount") or 0)

    if invoice.payments:
        invoice.paid_amount = total_paid
        invoice.change_amount = total_change


def _append_tax_rows(invoice: Any, order_doc: Any) -> None:
    tax_total = float(_value(order_doc, "tax_total") or 0)
    if tax_total <= 0:
        return

    account_head = _resolve_tax_account_head(_value(order_doc, "company"))
    invoice.append(
        "taxes",
        {
            "charge_type": "Actual",
            "account_head": account_head,
            "description": "KoPOS SST",
            "included_in_print_rate": 0,
            "dont_recompute_tax": 1,
            "tax_amount": tax_total,
            "base_tax_amount": tax_total,
        },
    )


def _apply_rounding(invoice: Any, order_doc: Any) -> None:
    rounding_adjustment = float(_value(order_doc, "rounding_adjustment") or 0)
    grand_total = float(_value(order_doc, "grand_total") or 0)

    if rounding_adjustment:
        invoice.disable_rounded_total = 1
        write_off_amount = _resolve_rounding_gap(invoice, grand_total)
        if write_off_amount > 0:
            invoice.write_off_amount = write_off_amount
            invoice.base_write_off_amount = write_off_amount
            write_off_defaults = _resolve_write_off_defaults(
                _value(order_doc, "company")
            )
            if write_off_defaults.get("account"):
                invoice.write_off_account = write_off_defaults["account"]
            if write_off_defaults.get("cost_center"):
                invoice.write_off_cost_center = write_off_defaults["cost_center"]
    elif grand_total > 0 and not float(getattr(invoice, "rounded_total", 0) or 0):
        invoice.rounded_total = grand_total


def _resolve_tax_account_head(company: Any) -> str:
    company_name = str(company or "").strip()
    if not company_name:
        raise ValueError("company is required to resolve tax account")

    exact_tax_account = frappe.get_all(
        "Account",
        filters={
            "company": company_name,
            "account_type": "Tax",
            "is_group": 0,
        },
        pluck="name",
        limit=1,
    )
    if exact_tax_account:
        return str(exact_tax_account[0])

    duties_account = frappe.get_all(
        "Account",
        filters={
            "company": company_name,
            "name": ["like", "Duties and Taxes%"],
            "is_group": 0,
        },
        pluck="name",
        limit=1,
    )
    if duties_account:
        return str(duties_account[0])

    raise ValueError(f"No tax account configured for company {company_name}")


def _resolve_rounding_gap(invoice: Any, target_total: float) -> float:
    current_total = float(getattr(invoice, "grand_total", 0) or 0)
    gap = round(current_total - target_total, 2)
    return gap if gap > 0 else 0.0


def _resolve_write_off_defaults(company: Any) -> dict[str, str]:
    company_name = str(company or "").strip()
    if not company_name:
        return {"account": "", "cost_center": ""}

    company_rows = frappe.get_all(
        "Company",
        filters={"name": company_name},
        fields=["write_off_account", "cost_center"],
        limit=1,
    )
    if company_rows:
        row = company_rows[0]
        account = str(row.get("write_off_account") or "").strip()
        cost_center = str(row.get("cost_center") or "").strip()
        if account:
            return {"account": account, "cost_center": cost_center}

    pos_profile_rows = frappe.get_all(
        "POS Profile",
        filters={"company": company_name},
        fields=["write_off_account", "write_off_cost_center"],
        limit=1,
    )
    if pos_profile_rows:
        row = pos_profile_rows[0]
        return {
            "account": str(row.get("write_off_account") or "").strip(),
            "cost_center": str(row.get("write_off_cost_center") or "").strip(),
        }

    return {"account": "", "cost_center": ""}


def _resolve_line_resolution_hash(order_item: Any) -> str | None:
    resolved_sale_name = _value(order_item, "resolved_sale")
    if not resolved_sale_name:
        return None
    try:
        resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sale_name)
    except Exception:
        return None
    return _value(resolved_sale, "resolution_hash")


def _link_resolved_sales(order_doc: Any, sales_invoice_name: str) -> None:
    for order_item in list(_value(order_doc, "items") or []):
        resolved_sale_name = _value(order_item, "resolved_sale")
        if not resolved_sale_name:
            continue
        try:
            resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sale_name)
            resolved_sale.db_set(
                "sales_invoice", sales_invoice_name, update_modified=False
            )
        except Exception:
            continue


def _set_if_present(doc: Any, fieldnames: list[str], value: Any) -> None:
    if value in (None, ""):
        return

    meta = frappe.get_meta(doc.doctype)
    for fieldname in fieldnames:
        if meta.has_field(fieldname):
            setattr(doc, fieldname, value)
            return


def _build_invoice_remarks(order_doc: Any) -> str:
    parts = [
        f"FB Order: {order_doc.name}",
        f"Shift: {_value(order_doc, 'shift') or ''}",
        f"Device ID: {_value(order_doc, 'device_id') or ''}",
    ]
    notes = _value(order_doc, "notes")
    if notes:
        parts.append(str(notes))
    return "\n".join(part for part in parts if part and part.split(": ")[-1] != "")


def _get_existing_reference(doc: Any, fieldname: str) -> str | None:
    value = _value(doc, fieldname)
    return str(value) if value else None


def _set_source_reference(doc: Any, fieldname: str, value: Any) -> None:
    if not hasattr(doc, fieldname):
        return
    try:
        doc.db_set(fieldname, value, update_modified=True)
    except Exception:
        setattr(doc, fieldname, value)
        doc.save(ignore_permissions=True)


def _make_savepoint(prefix: str) -> str:
    name = f"{prefix}_{frappe.generate_hash(length=8)}"
    try:
        frappe.db.savepoint(name)
    except Exception:
        return ""
    return name


def _rollback_savepoint(savepoint: str) -> None:
    try:
        if savepoint:
            frappe.db.rollback(save_point=savepoint)
        else:
            frappe.db.rollback()
    except Exception:
        pass


def _log_error(title: str) -> None:
    try:
        frappe.log_error(frappe.get_traceback(), title)
    except Exception:
        pass
