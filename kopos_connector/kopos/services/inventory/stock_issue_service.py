# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe

from kopos_connector.api.devices import privileged_device_api_operation
from kopos_connector.kopos.services.orders.sale_datetime import (
    resolve_order_sale_datetime,
)
from kopos_connector.utils.diagnostics import (
    log_sanitized_error,
    make_savepoint,
    rollback_to_savepoint,
)


def create_ingredient_stock_entry(fb_order: Any, resolved_sales: Any) -> str | None:
    order_doc = _coerce_doc("FB Order", fb_order)
    if not order_doc:
        return None

    resolved_sale_docs = _coerce_resolved_sales(resolved_sales)
    projection_id = _stock_issue_projection_id(order_doc)
    existing_entry = _get_existing_reference(order_doc, "ingredient_stock_entry")
    if not existing_entry:
        existing_entry = frappe.db.get_value(
            "Stock Entry",
            {"custom_fb_projection_id": projection_id},
            "name",
        )
    if not existing_entry:
        existing_entry = _find_legacy_stock_issue(order_doc)
    if existing_entry:
        existing_doc = _coerce_doc("Stock Entry", existing_entry)
        if not existing_doc:
            frappe.throw(
                f"Recovered Stock Entry {existing_entry} was not found",
                frappe.ValidationError,
            )
        _validate_stock_entry_equivalence(
            order_doc,
            resolved_sale_docs,
            existing_doc,
        )
        _set_source_reference(order_doc, "ingredient_stock_entry", existing_entry)
        _set_source_reference(order_doc, "stock_status", "Posted")
        return str(existing_entry)

    if not resolved_sale_docs:
        return None

    grouped_items = _build_grouped_issue_items(resolved_sale_docs)
    if not grouped_items:
        return None

    savepoint = _make_savepoint("fb_stock_issue")

    try:
        with privileged_device_api_operation("stock_projection"):
            stock_entry = frappe.new_doc("Stock Entry")
            stock_entry.stock_entry_type = "Material Issue"
            stock_entry.purpose = "Material Issue"
            stock_entry.company = _value(order_doc, "company")
            stock_entry.project = _value(order_doc, "event_project") or None
            posting_dt = _resolve_posting_datetime(order_doc)
            stock_entry.posting_date = posting_dt.date().isoformat()
            stock_entry.posting_time = posting_dt.time().strftime("%H:%M:%S")
            stock_entry.set_posting_time = 1
            stock_entry.remarks = _build_stock_entry_remarks(order_doc, resolved_sale_docs)

            _set_if_present(
                stock_entry,
                ["custom_fb_projection_id"],
                projection_id,
            )
            _set_if_present(stock_entry, ["custom_fb_order"], order_doc.name)
            _set_if_present(stock_entry, ["custom_fb_shift"], _value(order_doc, "shift"))
            _set_if_present(
                stock_entry,
                ["custom_fb_event_project"],
                _value(order_doc, "event_project"),
            )

            for item_row in grouped_items:
                stock_entry.append("items", item_row)

            stock_entry.insert(ignore_permissions=True)
            stock_entry.submit()
            if hasattr(stock_entry, "reload"):
                stock_entry.reload()
            _validate_stock_entry_equivalence(
                order_doc,
                resolved_sale_docs,
                stock_entry,
            )

        _set_source_reference(order_doc, "ingredient_stock_entry", stock_entry.name)
        _set_source_reference(order_doc, "stock_status", "Posted")

        for resolved_sale_doc in resolved_sale_docs:
            _set_source_reference(
                resolved_sale_doc, "stock_entry_issue", stock_entry.name
            )

        return stock_entry.name
    except Exception:
        _rollback_savepoint(savepoint)
        _log_error("Ingredient stock issue projection failed")
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


def _coerce_resolved_sales(resolved_sales: Any) -> list[Any]:
    documents: list[Any] = []
    for value in resolved_sales or []:
        doc = _coerce_doc("FB Resolved Sale", value)
        if doc:
            documents.append(doc)
    return documents


def _value(doc: Any, fieldname: str) -> Any:
    if hasattr(doc, fieldname):
        return getattr(doc, fieldname)
    getter = getattr(doc, "get", None)
    if callable(getter):
        return getter(fieldname)
    return None


def _build_grouped_issue_items(resolved_sale_docs: list[Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(dict)

    for resolved_sale_doc in resolved_sale_docs:
        for component in list(_value(resolved_sale_doc, "resolved_components") or []):
            if not int(_value(component, "affects_stock") or 0):
                continue

            warehouse = _value(component, "warehouse") or _value(
                resolved_sale_doc, "booth_warehouse"
            )
            item_code = _value(component, "item")
            stock_qty_value = _value(component, "stock_qty")
            if stock_qty_value in (None, ""):
                stock_qty_value = _value(component, "qty")
            qty = _positive_decimal_quantity(
                stock_qty_value,
                f"Resolved Sale {_value(resolved_sale_doc, 'name') or '(unnamed)'} stock quantity",
            )
            stock_uom = _value(component, "stock_uom") or _value(component, "uom")

            if (
                not warehouse
                or not item_code
                or not stock_uom
            ):
                raise ValueError(
                    "Stock-affecting resolved component requires item, warehouse, stock UOM, and positive stock quantity"
                )

            key = (
                warehouse,
                item_code,
                str(stock_uom or ""),
                resolved_sale_doc.company
                if hasattr(resolved_sale_doc, "company")
                else "",
            )
            current = grouped.get(key)
            if not current:
                grouped[key] = {
                    "item_code": item_code,
                    "s_warehouse": warehouse,
                    "t_warehouse": None,
                    "qty": qty,
                    "uom": stock_uom,
                    "stock_uom": stock_uom,
                    "conversion_factor": 1,
                    "description": _build_component_description(
                        resolved_sale_doc, component
                    ),
                }
                continue

            current["qty"] = Decimal(str(current.get("qty") or 0)) + qty

    return list(grouped.values())


def _positive_decimal_quantity(value: Any, fieldname: str) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{fieldname} must be a finite positive decimal")
    try:
        quantity = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as error:
        raise ValueError(
            f"{fieldname} must be a finite positive decimal"
        ) from error
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError(f"{fieldname} must be a finite positive decimal")
    return quantity


def _validate_stock_entry_equivalence(
    order_doc: Any,
    resolved_sale_docs: list[Any],
    stock_entry: Any,
) -> None:
    """Prove one submitted Material Issue exactly consumes resolved components."""

    stock_entry_name = str(_value(stock_entry, "name") or "(unnamed)")
    if int(_value(stock_entry, "docstatus") or 0) != 1:
        frappe.throw(
            f"Recovered Stock Entry {stock_entry_name} is not submitted",
            frappe.ValidationError,
        )
    if str(_value(stock_entry, "stock_entry_type") or "").strip() != "Material Issue":
        frappe.throw(
            f"Recovered Stock Entry {stock_entry_name} is not a Material Issue",
            frappe.ValidationError,
        )
    if str(_value(stock_entry, "purpose") or "").strip() != "Material Issue":
        frappe.throw(
            f"Recovered Stock Entry {stock_entry_name} has the wrong purpose",
            frappe.ValidationError,
        )
    if str(_value(stock_entry, "company") or "").strip() != str(
        _value(order_doc, "company") or ""
    ).strip():
        frappe.throw(
            f"Recovered Stock Entry {stock_entry_name} belongs to another company",
            frappe.ValidationError,
        )
    if str(_value(stock_entry, "custom_fb_order") or "").strip() != str(
        _value(order_doc, "name") or ""
    ).strip():
        frappe.throw(
            f"Recovered Stock Entry {stock_entry_name} belongs to another FB Order",
            frappe.ValidationError,
        )
    actual_projection_id = str(
        _value(stock_entry, "custom_fb_projection_id") or ""
    ).strip()
    expected_projection_id = _stock_issue_projection_id(order_doc)
    if actual_projection_id and actual_projection_id != expected_projection_id:
        frappe.throw(
            f"Recovered Stock Entry {stock_entry_name} has the wrong F&B projection identity",
            frappe.ValidationError,
        )
    if str(_value(stock_entry, "custom_fb_shift") or "").strip() != str(
        _value(order_doc, "shift") or ""
    ).strip():
        frappe.throw(
            f"Recovered Stock Entry {stock_entry_name} belongs to another FB Shift",
            frappe.ValidationError,
        )

    expected_rows = _build_grouped_issue_items(resolved_sale_docs)
    actual_rows = list(_value(stock_entry, "items") or [])
    if len(actual_rows) != len(expected_rows):
        frappe.throw(
            f"Recovered Stock Entry {stock_entry_name} item count does not match resolved components",
            frappe.ValidationError,
        )

    expected_by_key = {
        _stock_row_key(row): row
        for row in expected_rows
    }
    if len(expected_by_key) != len(expected_rows):
        frappe.throw(
            f"FB Order {_value(order_doc, 'name')} resolved components are not uniquely grouped",
            frappe.ValidationError,
        )

    seen_keys: set[tuple[str, str, str]] = set()
    for row in actual_rows:
        key = _stock_row_key(row)
        expected = expected_by_key.get(key)
        if expected is None or key in seen_keys:
            frappe.throw(
                f"Recovered Stock Entry {stock_entry_name} has an unknown or duplicate component row",
                frappe.ValidationError,
            )
        seen_keys.add(key)
        if str(_value(row, "t_warehouse") or "").strip():
            frappe.throw(
                f"Recovered Stock Entry {stock_entry_name} Material Issue row has a target warehouse",
                frappe.ValidationError,
            )
        expected_qty = _positive_decimal_quantity(
            _value(expected, "qty"),
            f"Expected Stock Entry {stock_entry_name} item quantity",
        )
        actual_qty = _positive_decimal_quantity(
            _value(row, "qty"),
            f"Recovered Stock Entry {stock_entry_name} item quantity",
        )
        transfer_qty = _positive_decimal_quantity(
            _value(row, "transfer_qty"),
            f"Recovered Stock Entry {stock_entry_name} transfer quantity",
        )
        if actual_qty != expected_qty or transfer_qty != expected_qty:
            frappe.throw(
                f"Recovered Stock Entry {stock_entry_name} component quantity does not match resolved components",
                frappe.ValidationError,
            )
        conversion_factor = _positive_decimal_quantity(
            _value(row, "conversion_factor"),
            f"Recovered Stock Entry {stock_entry_name} conversion factor",
        )
        if conversion_factor != Decimal("1"):
            frappe.throw(
                f"Recovered Stock Entry {stock_entry_name} component conversion factor must be 1",
                frappe.ValidationError,
            )


def _stock_row_key(row: Any) -> tuple[str, str, str]:
    return (
        str(_value(row, "item_code") or "").strip(),
        str(_value(row, "s_warehouse") or "").strip(),
        str(_value(row, "stock_uom") or _value(row, "uom") or "").strip(),
    )


def _build_component_description(resolved_sale_doc: Any, component: Any) -> str:
    parts = [
        f"Resolved Sale: {resolved_sale_doc.name}",
        f"Source: {_value(component, 'source_type') or ''}",
        f"Reference: {_value(component, 'source_reference') or ''}",
    ]
    remarks = _value(component, "remarks")
    if remarks:
        parts.append(str(remarks))
    return " | ".join(part for part in parts if part and part.split(": ")[-1] != "")


def _build_stock_entry_remarks(order_doc: Any, resolved_sale_docs: list[Any]) -> str:
    resolved_sale_names = ", ".join(doc.name for doc in resolved_sale_docs)
    parts = [
        f"FB Order: {order_doc.name}",
        f"Shift: {_value(order_doc, 'shift') or ''}",
        f"Device ID: {_value(order_doc, 'device_id') or ''}",
        f"Resolved Sales: {resolved_sale_names}",
    ]
    return "\n".join(part for part in parts if part and part.split(": ")[-1] != "")


def _set_if_present(doc: Any, fieldnames: list[str], value: Any) -> None:
    if value in (None, ""):
        return

    meta = frappe.get_meta(doc.doctype)
    for fieldname in fieldnames:
        if meta.has_field(fieldname):
            setattr(doc, fieldname, value)
            return


def _resolve_posting_datetime(order_doc: Any):
    return resolve_order_sale_datetime(order_doc)


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


def _stock_issue_projection_id(order_doc: Any) -> str:
    order_name = str(_value(order_doc, "name") or "").strip()
    if not order_name:
        raise ValueError("FB Order name is required for stock projection idempotency")
    readable = f"fb-order:{order_name}:stock-issue"
    if len(readable) <= 140:
        return readable
    digest = hashlib.sha256(order_name.encode("utf-8")).hexdigest()
    return f"fb-order:{digest}:stock-issue"


def _find_legacy_stock_issue(order_doc: Any) -> str | None:
    """Recover a pre-projection-id Material Issue without creating a duplicate."""

    rows = frappe.get_all(
        "Stock Entry",
        filters={
            "custom_fb_order": str(_value(order_doc, "name") or "").strip(),
            "purpose": "Material Issue",
            "docstatus": 1,
        },
        fields=["name"],
        order_by="creation asc",
        limit_page_length=2,
    )
    if len(rows) > 1:
        frappe.throw(
            f"FB Order {_value(order_doc, 'name')} has multiple submitted stock issues; manual reconciliation is required",
            frappe.ValidationError,
        )
    if not rows:
        return None
    return str(_value(rows[0], "name") or "") or None


def _make_savepoint(prefix: str) -> str:
    return make_savepoint(prefix)


def _rollback_savepoint(savepoint: str) -> None:
    rollback_to_savepoint(savepoint, title="Stock issue projection rollback failed")


def _log_error(title: str) -> None:
    log_sanitized_error(title)
