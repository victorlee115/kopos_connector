# pyright: reportMissingImports=false

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import frappe

from kopos_connector.api.devices import (
    get_device_doc,
    get_device_pos_profile_doc,
    privileged_device_api_operation,
)
from kopos_connector.kopos.api.money_contract import (
    parse_positive_integer_quantity,
    persisted_money_to_sen,
    sen_to_decimal,
)
from kopos_connector.kopos.services.accounting.maybank_payment_service import (
    bind_qr_payment_settlement,
    normalize_qr_token,
    resolve_verified_qr_settlement_account,
)
from kopos_connector.kopos.services.orders.sale_datetime import (
    resolve_order_sale_datetime,
)
from kopos_connector.utils.diagnostics import (
    log_sanitized_error,
    make_savepoint,
    rollback_to_savepoint,
)


def _optional_money_value(value: Any) -> Any:
    return 0 if value is None or value == "" else value


def _money_sen(value: Any, fieldname: str) -> int:
    return persisted_money_to_sen(value, fieldname)


def _money_decimal(value: Any, fieldname: str) -> Decimal:
    return sen_to_decimal(_money_sen(value, fieldname))


def create_sales_invoice(fb_order: Any) -> str | None:
    order_doc = _coerce_doc("FB Order", fb_order)
    if not order_doc:
        return None

    existing_invoice = _get_existing_sales_invoice(order_doc)
    if existing_invoice:
        bind_qr_payment_settlement(order_doc, existing_invoice)
        _set_source_reference(order_doc, "sales_invoice", existing_invoice)
        _set_source_reference(order_doc, "invoice_status", "Posted")
        _link_resolved_sales(order_doc, existing_invoice)
        return existing_invoice

    pos_profile_context = _resolve_pos_profile_context(order_doc)
    savepoint = _make_savepoint("fb_sales_invoice")

    try:
        with privileged_device_api_operation("sales_invoice_projection"):
            invoice = frappe.new_doc("Sales Invoice")
            invoice.customer = _resolve_customer(order_doc)
            invoice.company = pos_profile_context["company"]
            invoice.currency = _resolve_currency(order_doc)
            invoice.is_pos = 1
            invoice.pos_profile = pos_profile_context["pos_profile"]
            # The committed POS sale is the pricing authority. ERP pricing rules
            # may have changed while an offline order was queued.
            invoice.ignore_pricing_rule = 1
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
            _set_if_present(
                invoice,
                ["custom_kopos_pricing_mode"],
                _value(order_doc, "pricing_mode"),
            )
            _set_if_present(
                invoice,
                ["custom_kopos_promotion_snapshot_version"],
                _value(order_doc, "promotion_snapshot_version"),
            )
            _set_if_present(
                invoice,
                ["custom_kopos_promotion_snapshot_hash"],
                _value(order_doc, "promotion_snapshot_hash"),
            )
            _set_if_present(
                invoice,
                ["custom_kopos_promotion_reconciliation_status"],
                _value(order_doc, "promotion_reconciliation_status"),
            )
            _set_if_present(
                invoice,
                ["custom_kopos_promotion_payload"],
                _value(order_doc, "promotion_payload_json"),
            )

            for order_item in list(_value(order_doc, "items") or []):
                item_code = _value(order_item, "item")
                if not item_code:
                    continue

                item_doc = _coerce_doc("Item", item_code)
                if not item_doc:
                    continue

                qty = parse_positive_integer_quantity(
                    _value(order_item, "qty"),
                    f"FB Order {order_doc.name} item {_value(order_item, 'line_id') or item_code} qty",
                )

                rate = _resolve_line_rate(order_item)
                line_total = _money_decimal(
                    _value(order_item, "line_total"),
                    f"FB Order {order_doc.name} item {_value(order_item, 'line_id') or item_code} line_total",
                )
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
                    "amount": line_total,
                    # ERPNext accepts a zero sales rate, but marking the row as
                    # free prevents price-list and minimum-selling-price logic
                    # from replacing or rejecting a fully discounted line.
                    "is_free_item": 1 if line_total == Decimal("0.00") else 0,
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
                invoice_item = invoice.append("items", row)
                modifier_snapshot = _build_modifier_snapshot(order_item)
                _set_if_present(
                    invoice_item,
                    ["custom_kopos_modifier_total"],
                    _money_decimal(
                        _optional_money_value(_value(order_item, "modifier_total")),
                        f"FB Order {order_doc.name} item {_value(order_item, 'line_id') or item_code} modifier_total",
                    ),
                )
                _set_if_present(
                    invoice_item,
                    ["custom_kopos_has_modifiers"],
                    1 if modifier_snapshot else 0,
                )
                _set_if_present(
                    invoice_item,
                    ["custom_kopos_modifiers"],
                    modifier_snapshot,
                )
                _set_if_present(
                    invoice_item,
                    ["custom_kopos_promotion_allocation"],
                    _value(order_item, "promotion_allocations_json") or "[]",
                )

            if not invoice.items:
                raise ValueError("fb_order has no invoiceable items")

            if hasattr(invoice, "set_missing_values"):
                invoice.set_missing_values()
            # POS Profile defaults must not add a second tax template on top of
            # the exact tax captured by the tablet.
            invoice.set("taxes", [])
            _append_tax_rows(invoice, order_doc)
            if hasattr(invoice, "calculate_taxes_and_totals"):
                invoice.calculate_taxes_and_totals()
            _apply_rounding(invoice, order_doc)
            invoice.update_stock = 0

            invoice.set("payments", [])
            _append_payment_rows(invoice, order_doc)

            invoice.insert(ignore_permissions=True)
            invoice.submit()
            if hasattr(invoice, "reload"):
                invoice.reload()
            _validate_sales_invoice_equivalence(order_doc, invoice)
            bind_qr_payment_settlement(order_doc, invoice.name)

        _set_source_reference(order_doc, "sales_invoice", invoice.name)
        _set_source_reference(order_doc, "invoice_status", "Posted")
        _link_resolved_sales(order_doc, invoice.name)

        return invoice.name
    except Exception:
        _rollback_savepoint(savepoint)
        recovered_invoice = _get_existing_sales_invoice(order_doc)
        if recovered_invoice:
            bind_qr_payment_settlement(order_doc, recovered_invoice)
            _set_source_reference(order_doc, "sales_invoice", recovered_invoice)
            _set_source_reference(order_doc, "invoice_status", "Posted")
            _link_resolved_sales(order_doc, recovered_invoice)
            return recovered_invoice
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
    getter = getattr(doc, "get", None)
    if callable(getter):
        value = getter(fieldname)
        if value is not None or (isinstance(doc, dict) and fieldname in doc):
            return value
    if hasattr(doc, fieldname):
        return getattr(doc, fieldname)
    return None


def _canonical_json_value(value: Any, *, expected_type: type) -> str:
    if value in (None, ""):
        parsed: Any = expected_type()
    elif isinstance(value, expected_type):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as error:
            raise ValueError("Persisted promotion evidence is invalid JSON") from error
    else:
        raise ValueError("Persisted promotion evidence has an invalid type")
    if not isinstance(parsed, expected_type):
        raise ValueError("Persisted promotion evidence has an invalid JSON shape")
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"))


def _resolve_customer(order_doc: Any) -> str:
    customer = _value(order_doc, "customer")
    if customer:
        return customer

    device_id = _value(order_doc, "device_id")
    if device_id:
        try:
            profile_doc = get_device_pos_profile_doc(device_id=str(device_id))
        except Exception:
            profile_doc = None

        profile_customer = _value(profile_doc, "customer") if profile_doc else None
        if profile_customer:
            return str(profile_customer)

    walk_in_customer = frappe.db.exists("Customer", "Walk-in Customer")
    if walk_in_customer:
        return walk_in_customer

    raise ValueError("customer is required to create Sales Invoice")


def _resolve_pos_profile_context(order_doc: Any) -> dict[str, str]:
    device_id = str(_value(order_doc, "device_id") or "").strip()
    if not device_id:
        frappe.throw("FB Order device_id is required to resolve POS Profile", frappe.ValidationError)

    device_doc = get_device_doc(device_id=device_id)
    pos_profile = str(_value(device_doc, "pos_profile") or "").strip()
    if not pos_profile:
        frappe.throw(
            f"KoPOS Device {device_id} has no POS Profile configured",
            frappe.ValidationError,
        )

    profile_doc = frappe.get_cached_doc("POS Profile", pos_profile)
    profile_company = str(_value(profile_doc, "company") or "").strip()
    if not profile_company:
        frappe.throw(
            f"POS Profile {pos_profile} has no company configured",
            frappe.ValidationError,
        )

    order_company = str(_value(order_doc, "company") or "").strip()
    if order_company and order_company != profile_company:
        frappe.throw(
            f"FB Order company {order_company} does not match POS Profile {pos_profile} company {profile_company}",
            frappe.ValidationError,
        )

    return {"pos_profile": pos_profile, "company": profile_company}


def _resolve_currency(order_doc: Any) -> str:
    currency = _value(order_doc, "currency")
    if currency:
        return currency

    company = _value(order_doc, "company")
    default_currency = frappe.db.get_value("Company", company, "default_currency")
    if default_currency:
        return default_currency

    raise ValueError("currency is required to create Sales Invoice")


def _get_existing_sales_invoice(order_doc: Any) -> str | None:
    source_reference = _get_existing_reference(order_doc, "sales_invoice")
    if source_reference:
        _validate_recovered_sales_invoice(order_doc, source_reference)
        return source_reference

    idempotency_key = str(_value(order_doc, "external_idempotency_key") or "").strip()
    if not idempotency_key:
        return None

    invoice_name = frappe.db.get_value(
        "Sales Invoice",
        {"custom_fb_idempotency_key": idempotency_key},
        "name",
    )
    if not invoice_name:
        return None

    resolved_invoice_name = str(invoice_name)
    _validate_recovered_sales_invoice(order_doc, resolved_invoice_name)
    return resolved_invoice_name


def _validate_recovered_sales_invoice(order_doc: Any, invoice_name: str) -> None:
    invoice = _coerce_doc("Sales Invoice", invoice_name)
    if not invoice:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} was not found",
            frappe.ValidationError,
        )
    _validate_sales_invoice_equivalence(order_doc, invoice)


def _validate_sales_invoice_equivalence(order_doc: Any, invoice: Any) -> None:
    """Prove a submitted invoice is the exact ERP projection of one FB Order."""

    invoice_name = str(_value(invoice, "name") or "(unnamed)")
    if int(_value(invoice, "docstatus") or 0) != 1:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} is not submitted",
            frappe.ValidationError,
        )
    if int(_value(invoice, "is_return") or 0):
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} is a return invoice",
            frappe.ValidationError,
        )
    if int(_value(invoice, "is_pos") or 0) != 1:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} is not a POS settlement invoice",
            frappe.ValidationError,
        )
    _validate_recovered_field(
        invoice,
        "custom_fb_order",
        order_doc.name,
        f"Sales Invoice {invoice_name} belongs to another FB Order",
    )
    _validate_recovered_field(
        invoice,
        "custom_fb_shift",
        _value(order_doc, "shift"),
        f"Sales Invoice {invoice_name} belongs to another FB Shift",
    )
    _validate_recovered_field(
        invoice,
        "custom_fb_device_id",
        _value(order_doc, "device_id"),
        f"Sales Invoice {invoice_name} belongs to another KoPOS device",
    )
    _validate_recovered_field(
        invoice,
        "custom_fb_idempotency_key",
        _value(order_doc, "external_idempotency_key"),
        f"Sales Invoice {invoice_name} has a different idempotency key",
    )
    for invoice_field, order_field in (
        ("custom_kopos_pricing_mode", "pricing_mode"),
        (
            "custom_kopos_promotion_snapshot_version",
            "promotion_snapshot_version",
        ),
        ("custom_kopos_promotion_snapshot_hash", "promotion_snapshot_hash"),
        (
            "custom_kopos_promotion_reconciliation_status",
            "promotion_reconciliation_status",
        ),
    ):
        _validate_recovered_field(
            invoice,
            invoice_field,
            _value(order_doc, order_field),
            f"Sales Invoice {invoice_name} promotion provenance differs from FB Order",
        )
    if _canonical_json_value(
        _value(invoice, "custom_kopos_promotion_payload"),
        expected_type=dict,
    ) != _canonical_json_value(
        _value(order_doc, "promotion_payload_json"),
        expected_type=dict,
    ):
        frappe.throw(
            f"Sales Invoice {invoice_name} promotion payload differs from FB Order {order_doc.name}",
            frappe.ValidationError,
        )
    _validate_recovered_field(
        invoice,
        "company",
        _value(order_doc, "company"),
        f"Sales Invoice {invoice_name} belongs to another company",
    )
    _validate_recovered_field(
        invoice,
        "currency",
        _value(order_doc, "currency"),
        f"Sales Invoice {invoice_name} uses another currency",
    )
    if int(_value(invoice, "update_stock") or 0) != 0:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} must not update finished-good stock",
            frappe.ValidationError,
        )
    if not str(_value(invoice, "pos_profile") or "").strip():
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} has no POS Profile provenance",
            frappe.ValidationError,
        )

    order_net_sen = _money_sen(
        _value(order_doc, "net_total"),
        f"FB Order {order_doc.name} net_total",
    )
    order_tax_sen = _money_sen(
        _optional_money_value(_value(order_doc, "tax_total")),
        f"FB Order {order_doc.name} tax_total",
    )
    order_rounding_sen = _money_sen(
        _optional_money_value(_value(order_doc, "rounding_adjustment")),
        f"FB Order {order_doc.name} rounding_adjustment",
    )
    order_total_sen = _money_sen(
        _value(order_doc, "grand_total"),
        f"FB Order {order_doc.name} grand_total",
    )
    expected_pre_round_total_sen = order_net_sen + order_tax_sen
    if expected_pre_round_total_sen + order_rounding_sen != order_total_sen:
        frappe.throw(
            f"FB Order {order_doc.name} totals are internally inconsistent",
            frappe.ValidationError,
        )

    invoice_net_sen = _money_sen(
        _value(invoice, "net_total"),
        f"Recovered Sales Invoice {invoice_name} net_total",
    )
    invoice_tax_sen = _money_sen(
        _optional_money_value(_value(invoice, "total_taxes_and_charges")),
        f"Recovered Sales Invoice {invoice_name} total_taxes_and_charges",
    )
    invoice_pre_round_total_sen = _money_sen(
        _value(invoice, "grand_total"),
        f"Recovered Sales Invoice {invoice_name} grand_total",
    )
    if invoice_net_sen != order_net_sen:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} net total does not match FB Order {order_doc.name}",
            frappe.ValidationError,
        )
    if invoice_tax_sen != order_tax_sen:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} tax total does not match FB Order {order_doc.name}",
            frappe.ValidationError,
        )
    if invoice_pre_round_total_sen != expected_pre_round_total_sen:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} pre-round total does not match FB Order {order_doc.name}",
            frappe.ValidationError,
        )

    write_off_sen = _money_sen(
        _optional_money_value(_value(invoice, "write_off_amount")),
        f"Recovered Sales Invoice {invoice_name} write_off_amount",
    )
    expected_write_off_sen = -order_rounding_sen
    if write_off_sen != expected_write_off_sen:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} rounding write-off does not match FB Order {order_doc.name}",
            frappe.ValidationError,
        )
    if int(_value(invoice, "disable_rounded_total") or 0) != 1:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} does not disable implicit ERP rounding",
            frappe.ValidationError,
        )
    rounded_total_sen = _money_sen(
        _optional_money_value(_value(invoice, "rounded_total")),
        f"Recovered Sales Invoice {invoice_name} rounded_total",
    )
    if rounded_total_sen != 0:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} retains an implicit rounded_total",
            frappe.ValidationError,
        )
    if invoice_pre_round_total_sen - write_off_sen != order_total_sen:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} payable total does not match FB Order {order_doc.name}",
            frappe.ValidationError,
        )

    _validate_invoice_item_equivalence(order_doc, invoice, invoice_name)
    _validate_invoice_tax_rows(order_doc, invoice, invoice_name)
    expected_paid_sen, expected_change_sen = _validate_invoice_payment_equivalence(
        order_doc,
        invoice,
        invoice_name,
    )

    paid_amount_sen = _money_sen(
        _value(invoice, "paid_amount"),
        f"Recovered Sales Invoice {invoice_name} paid_amount",
    )
    if paid_amount_sen != expected_paid_sen:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} paid amount does not match its tender rows",
            frappe.ValidationError,
        )
    change_amount_sen = _money_sen(
        _optional_money_value(_value(invoice, "change_amount")),
        f"Recovered Sales Invoice {invoice_name} change_amount",
    )
    if change_amount_sen != expected_change_sen:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} change amount does not match FB Order {order_doc.name}",
            frappe.ValidationError,
        )
    if paid_amount_sen - change_amount_sen != order_total_sen:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} net paid amount does not settle FB Order {order_doc.name}",
            frappe.ValidationError,
        )
    outstanding_sen = _money_sen(
        _optional_money_value(_value(invoice, "outstanding_amount")),
        f"Recovered Sales Invoice {invoice_name} outstanding_amount",
    )
    if outstanding_sen != 0:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} is not fully settled",
            frappe.ValidationError,
        )


def _validate_invoice_item_equivalence(
    order_doc: Any,
    invoice: Any,
    invoice_name: str,
) -> None:
    order_items = list(_value(order_doc, "items") or [])
    invoice_items = list(_value(invoice, "items") or [])
    if len(invoice_items) != len(order_items):
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} item count does not match FB Order {order_doc.name}",
            frappe.ValidationError,
        )

    order_by_line_ref = {
        str(_value(row, "line_id") or "").strip(): row for row in order_items
    }
    if "" in order_by_line_ref or len(order_by_line_ref) != len(order_items):
        frappe.throw(
            f"FB Order {order_doc.name} has invalid or duplicate line references",
            frappe.ValidationError,
        )

    seen_line_refs: set[str] = set()
    for invoice_item in invoice_items:
        line_ref = str(
            _value(invoice_item, "custom_fb_order_line_ref") or ""
        ).strip()
        order_item = order_by_line_ref.get(line_ref)
        if not order_item or line_ref in seen_line_refs:
            frappe.throw(
                f"Recovered Sales Invoice {invoice_name} has an unknown or duplicate FB Order line reference",
                frappe.ValidationError,
            )
        seen_line_refs.add(line_ref)

        if str(_value(invoice_item, "item_code") or "").strip() != str(
            _value(order_item, "item") or ""
        ).strip():
            frappe.throw(
                f"Recovered Sales Invoice {invoice_name} item {line_ref} does not match FB Order item",
                frappe.ValidationError,
            )
        invoice_qty = parse_positive_integer_quantity(
            _value(invoice_item, "qty"),
            f"Recovered Sales Invoice {invoice_name} item {line_ref} qty",
        )
        order_qty = parse_positive_integer_quantity(
            _value(order_item, "qty"),
            f"FB Order {order_doc.name} item {line_ref} qty",
        )
        if invoice_qty != order_qty:
            frappe.throw(
                f"Recovered Sales Invoice {invoice_name} item {line_ref} quantity does not match FB Order",
                frappe.ValidationError,
            )
        expected_line_sen = _money_sen(
            _value(order_item, "line_total"),
            f"FB Order {order_doc.name} item {line_ref} line_total",
        )
        for fieldname in ("amount", "net_amount"):
            actual_line_sen = _money_sen(
                _value(invoice_item, fieldname),
                f"Recovered Sales Invoice {invoice_name} item {line_ref} {fieldname}",
            )
            if actual_line_sen != expected_line_sen:
                frappe.throw(
                    f"Recovered Sales Invoice {invoice_name} item {line_ref} {fieldname} does not match FB Order",
                    frappe.ValidationError,
                )
        expected_modifier_sen = _money_sen(
            _optional_money_value(_value(order_item, "modifier_total")),
            f"FB Order {order_doc.name} item {line_ref} modifier_total",
        )
        actual_modifier_sen = _money_sen(
            _optional_money_value(
                _value(invoice_item, "custom_kopos_modifier_total")
            ),
            f"Recovered Sales Invoice {invoice_name} item {line_ref} modifier_total",
        )
        if actual_modifier_sen != expected_modifier_sen:
            frappe.throw(
                f"Recovered Sales Invoice {invoice_name} item {line_ref} modifier total does not match FB Order",
                frappe.ValidationError,
            )
        if _canonical_json_value(
            _value(invoice_item, "custom_kopos_promotion_allocation"),
            expected_type=list,
        ) != _canonical_json_value(
            _value(order_item, "promotion_allocations_json"),
            expected_type=list,
        ):
            frappe.throw(
                f"Recovered Sales Invoice {invoice_name} item {line_ref} promotion allocation does not match FB Order",
                frappe.ValidationError,
            )
        expected_warehouse = str(
            _value(order_doc, "booth_warehouse") or ""
        ).strip()
        actual_warehouse = str(_value(invoice_item, "warehouse") or "").strip()
        if expected_warehouse and actual_warehouse != expected_warehouse:
            frappe.throw(
                f"Recovered Sales Invoice {invoice_name} item {line_ref} warehouse does not match FB Order",
                frappe.ValidationError,
            )


def _validate_invoice_tax_rows(
    order_doc: Any,
    invoice: Any,
    invoice_name: str,
) -> None:
    expected_tax_sen = _money_sen(
        _optional_money_value(_value(order_doc, "tax_total")),
        f"FB Order {order_doc.name} tax_total",
    )
    tax_rows = list(_value(invoice, "taxes") or [])
    nonzero_rows: list[Any] = []
    for row in tax_rows:
        row_amount_sen = _money_sen(
            _optional_money_value(
                _value(row, "tax_amount_after_discount_amount")
                if _value(row, "tax_amount_after_discount_amount") not in (None, "")
                else _value(row, "tax_amount")
            ),
            f"Recovered Sales Invoice {invoice_name} tax row amount",
        )
        if row_amount_sen:
            nonzero_rows.append(row)
    if expected_tax_sen == 0:
        if nonzero_rows:
            frappe.throw(
                f"Recovered Sales Invoice {invoice_name} has unexpected tax rows",
                frappe.ValidationError,
            )
        return
    if len(nonzero_rows) != 1:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} must have exactly one KoPOS tax row",
            frappe.ValidationError,
        )
    row = nonzero_rows[0]
    if str(_value(row, "charge_type") or "").strip() != "Actual":
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} tax row is not an Actual charge",
            frappe.ValidationError,
        )
    if int(_value(row, "included_in_print_rate") or 0) != 0:
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} tax row must not be included in item rates",
            frappe.ValidationError,
        )
    row_amount_sen = _money_sen(
        _value(row, "tax_amount"),
        f"Recovered Sales Invoice {invoice_name} tax row amount",
    )
    row_after_discount_sen = _money_sen(
        _value(row, "tax_amount_after_discount_amount")
        if _value(row, "tax_amount_after_discount_amount") not in (None, "")
        else _value(row, "tax_amount"),
        f"Recovered Sales Invoice {invoice_name} tax row amount after discount",
    )
    if (
        row_amount_sen != expected_tax_sen
        or row_after_discount_sen != expected_tax_sen
        or not str(_value(row, "account_head") or "").strip()
    ):
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} tax row does not match FB Order",
            frappe.ValidationError,
        )


def _validate_invoice_payment_equivalence(
    order_doc: Any,
    invoice: Any,
    invoice_name: str,
) -> tuple[int, int]:
    order_payments = list(_value(order_doc, "payments") or [])
    invoice_payments = list(_value(invoice, "payments") or [])
    if len(invoice_payments) != len(order_payments):
        frappe.throw(
            f"Recovered Sales Invoice {invoice_name} payment count does not match FB Order {order_doc.name}",
            frappe.ValidationError,
        )

    total_tendered_sen = 0
    total_change_sen = 0
    for index, (order_payment, invoice_payment) in enumerate(
        zip(order_payments, invoice_payments, strict=True),
        start=1,
    ):
        expected_mode = str(_value(order_payment, "payment_method") or "").strip()
        actual_mode = str(_value(invoice_payment, "mode_of_payment") or "").strip()
        if not expected_mode or actual_mode != expected_mode:
            frappe.throw(
                f"Recovered Sales Invoice {invoice_name} payment {index} mode does not match FB Order",
                frappe.ValidationError,
            )
        expected_source_payment_id = str(
            _value(order_payment, "source_payment_id") or ""
        ).strip()
        actual_source_payment_id = str(
            _value(invoice_payment, "custom_fb_source_payment_id") or ""
        ).strip()
        if actual_source_payment_id != expected_source_payment_id:
            frappe.throw(
                f"Recovered Sales Invoice {invoice_name} payment {index} source payment ID does not match FB Order",
                frappe.ValidationError,
            )
        tendered_sen, change_sen = _resolve_payment_tender_and_change_sen(
            order_payment,
            index,
        )
        invoice_payment_sen = _money_sen(
            _value(invoice_payment, "amount"),
            f"Recovered Sales Invoice {invoice_name} payment {index} amount",
        )
        if invoice_payment_sen != tendered_sen:
            frappe.throw(
                f"Recovered Sales Invoice {invoice_name} payment {index} tender does not match FB Order",
                frappe.ValidationError,
            )
        if not str(_value(invoice_payment, "account") or "").strip():
            frappe.throw(
                f"Recovered Sales Invoice {invoice_name} payment {index} has no ledger account provenance",
                frappe.ValidationError,
            )
        total_tendered_sen += tendered_sen
        total_change_sen += change_sen
    return total_tendered_sen, total_change_sen


def _validate_recovered_field(
    invoice: Any,
    fieldname: str,
    expected_value: Any,
    message: str,
) -> None:
    actual_value = str(_value(invoice, fieldname) or "").strip()
    expected = str(expected_value or "").strip()
    if expected and actual_value != expected:
        frappe.throw(message, frappe.ValidationError)


def _resolve_posting_datetime(order_doc: Any):
    return resolve_order_sale_datetime(order_doc)


def _resolve_line_rate(order_item: Any) -> Decimal:
    qty = parse_positive_integer_quantity(
        _value(order_item, "qty"),
        "FB Order item qty",
    )
    line_total_sen = _money_sen(
        _value(order_item, "line_total"),
        "FB Order item line_total",
    )
    if line_total_sen < 0:
        raise ValueError("FB Order item line_total must be 0 or greater")
    return sen_to_decimal(line_total_sen) / Decimal(qty)


def _append_payment_rows(invoice: Any, order_doc: Any) -> None:
    payment_rows = list(_value(order_doc, "payments") or [])
    if not payment_rows:
        return

    total_applied_sen = 0
    total_tendered_sen = 0
    total_change_sen = 0

    for index, payment in enumerate(payment_rows, start=1):
        mode_of_payment = _value(payment, "payment_method")
        if not mode_of_payment:
            raise ValueError(f"FB Order payment {index} requires payment_method")
        amount_sen = _money_sen(
            _value(payment, "amount"),
            f"FB Order payment {index} amount",
        )
        if amount_sen <= 0:
            raise ValueError(f"FB Order payment {index} amount must be greater than 0")
        tendered_amount_sen, change_amount_sen = (
            _resolve_payment_tender_and_change_sen(payment, index)
        )
        tendered_amount = sen_to_decimal(tendered_amount_sen)

        payment_row = {
            "idx": index,
            "mode_of_payment": mode_of_payment,
            # ERPNext derives paid_amount and change_amount from the tender rows.
            # Storing only the net applied amount causes its validation lifecycle
            # to erase legitimate cash change.
            "amount": tendered_amount,
            "reference_no": _value(payment, "reference_no") or None,
        }
        source_payment_id = str(
            _value(payment, "source_payment_id") or ""
        ).strip()
        if source_payment_id:
            payment_row["custom_fb_source_payment_id"] = source_payment_id

        settlement_status = str(
            _value(payment, "settlement_status") or "verified"
        ).strip()
        payment_channel = normalize_qr_token(
            _value(payment, "payment_channel_code")
        )
        is_manual_confirmation = str(
            _value(payment, "is_manual_confirmation") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        if payment_channel in {"maybank", "maybank qr"}:
            if is_manual_confirmation:
                if settlement_status != "pending_reconciliation":
                    raise ValueError(
                        "Manual Maybank QR payment must remain pending_reconciliation"
                    )
                payment_meta = _resolve_mode_of_payment_context(
                    str(mode_of_payment),
                    str(invoice.company),
                )
            else:
                if settlement_status != "verified":
                    raise ValueError(
                        "Automatic Maybank QR payment must have verified settlement status"
                    )
                payment_meta = resolve_verified_qr_settlement_account(
                    str(mode_of_payment),
                    str(invoice.company),
                    str(
                        _value(invoice, "currency")
                        or _value(order_doc, "currency")
                    ),
                )
        else:
            payment_meta = _resolve_mode_of_payment_context(
                str(mode_of_payment),
                str(invoice.company),
            )
        payment_row["account"] = payment_meta["account"]
        if payment_meta.get("type"):
            payment_row["type"] = payment_meta["type"]

        if settlement_status == "pending_reconciliation":
            suspense_account = str(
                _value(payment, "suspense_account") or ""
            ).strip()
            if not suspense_account:
                raise ValueError(
                    "static_qr pending reconciliation requires a suspense account"
                )
            payment_row["account"] = suspense_account

        if not str(payment_row.get("account") or "").strip():
            raise ValueError(
                f"Mode of Payment {mode_of_payment} has no ledger account for company {invoice.company}"
            )

        invoice.append("payments", payment_row)
        total_applied_sen += amount_sen
        total_tendered_sen += tendered_amount_sen
        total_change_sen += change_amount_sen

    if invoice.payments:
        grand_total_sen = _money_sen(
            _value(order_doc, "grand_total"),
            "FB Order grand_total",
        )
        if total_applied_sen != grand_total_sen:
            raise ValueError(
                "FB Order payment total must exactly equal grand_total in sen"
            )
        if total_tendered_sen - total_change_sen != grand_total_sen:
            raise ValueError(
                "FB Order tendered total minus change must exactly equal grand_total in sen"
            )
        invoice.paid_amount = sen_to_decimal(total_tendered_sen)
        invoice.change_amount = sen_to_decimal(total_change_sen)


def _resolve_mode_of_payment_context(
    mode_of_payment: str,
    company: str,
) -> dict[str, str]:
    """Resolve an unambiguous ledger destination for a tender row."""

    try:
        from erpnext.accounts.doctype.sales_invoice.sales_invoice import (
            get_mode_of_payment_info,
        )
    except Exception as error:
        raise RuntimeError(
            "ERPNext mode-of-payment accounting resolver is unavailable"
        ) from error

    mode_info = get_mode_of_payment_info(mode_of_payment, company)
    if not mode_info:
        raise ValueError(
            f"Mode of Payment {mode_of_payment} is not configured for company {company}"
        )
    payment_meta = mode_info[0]
    # ERPNext v15 exposed this value as ``account`` in some call paths, while
    # v16's get_mode_of_payment_info() returns the child-table field name
    # ``default_account``. Accept both wire shapes so an otherwise valid POS
    # tender cannot leave its FB Order posted without a Sales Invoice.
    account = str(
        payment_meta.get("account")
        or payment_meta.get("default_account")
        or ""
    ).strip()
    if not account:
        raise ValueError(
            f"Mode of Payment {mode_of_payment} has no ledger account for company {company}"
        )
    return {
        "account": account,
        "type": str(payment_meta.get("type") or "").strip(),
    }


def _resolve_payment_tender_and_change_sen(
    payment: Any,
    index: int,
) -> tuple[int, int]:
    amount_sen = _money_sen(
        _value(payment, "amount"),
        f"FB Order payment {index} amount",
    )
    tendered_sen = _money_sen(
        _optional_money_value(_value(payment, "tendered_amount")),
        f"FB Order payment {index} tendered_amount",
    )
    change_sen = _money_sen(
        _optional_money_value(_value(payment, "change_amount")),
        f"FB Order payment {index} change_amount",
    )
    if tendered_sen < 0:
        raise ValueError(
            f"FB Order payment {index} tendered_amount must be non-negative"
        )
    if change_sen < 0:
        raise ValueError(
            f"FB Order payment {index} change_amount must be non-negative"
        )

    # Older non-cash rows persist zero in the optional tender field. Treat that
    # unambiguously as an exact tender only when there is no change.
    if tendered_sen == 0 and change_sen == 0:
        tendered_sen = amount_sen
    if tendered_sen - change_sen != amount_sen:
        raise ValueError(
            f"FB Order payment {index} tendered minus change must equal amount"
        )
    return tendered_sen, change_sen


def _append_tax_rows(invoice: Any, order_doc: Any) -> None:
    tax_total_sen = _money_sen(
        _optional_money_value(_value(order_doc, "tax_total")),
        "FB Order tax_total",
    )
    if tax_total_sen < 0:
        raise ValueError("FB Order tax_total must be non-negative")
    if tax_total_sen == 0:
        return
    tax_total = sen_to_decimal(tax_total_sen)

    account_head = _resolve_tax_account_head(
        _value(order_doc, "company"),
        device_id=_value(order_doc, "device_id"),
    )
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
    rounding_adjustment_sen = _money_sen(
        _optional_money_value(_value(order_doc, "rounding_adjustment")),
        "FB Order rounding_adjustment",
    )
    grand_total_sen = _money_sen(
        _value(order_doc, "grand_total"),
        "FB Order grand_total",
    )
    if grand_total_sen <= 0:
        raise ValueError("FB Order grand_total must be greater than 0")

    rounding_gap_sen = _resolve_rounding_gap_sen(invoice, grand_total_sen)
    expected_rounding_gap_sen = -rounding_adjustment_sen
    if rounding_gap_sen != expected_rounding_gap_sen:
        raise ValueError(
            "Sales Invoice calculated total does not exactly match the FB Order total and rounding adjustment"
        )

    # The POS-captured adjustment is the only rounding authority. Disable ERP's
    # implicit currency rounding for every sale and model the signed difference
    # as a write-off so the payable amount is grand_total - write_off_amount.
    invoice.disable_rounded_total = 1
    invoice.rounded_total = Decimal("0.00")
    if hasattr(invoice, "base_rounded_total"):
        invoice.base_rounded_total = Decimal("0.00")
    if hasattr(invoice, "rounding_adjustment"):
        invoice.rounding_adjustment = Decimal("0.00")
    if hasattr(invoice, "base_rounding_adjustment"):
        invoice.base_rounding_adjustment = Decimal("0.00")

    write_off_amount = sen_to_decimal(rounding_gap_sen)
    invoice.write_off_amount = write_off_amount
    invoice.base_write_off_amount = write_off_amount
    if rounding_gap_sen:
        write_off_defaults = _resolve_write_off_defaults(
            _value(order_doc, "company")
        )
        if write_off_defaults.get("account"):
            invoice.write_off_account = write_off_defaults["account"]
        if write_off_defaults.get("cost_center"):
            invoice.write_off_cost_center = write_off_defaults["cost_center"]


def _resolve_tax_account_head(company: Any, device_id: Any = None) -> str:
    company_name = str(company or "").strip()
    if not company_name:
        raise ValueError("company is required to resolve tax account")

    profile_tax_template = ""
    if device_id:
        try:
            profile_doc = get_device_pos_profile_doc(device_id=str(device_id))
            profile_tax_template = str(
                _value(profile_doc, "taxes_and_charges") or ""
            ).strip()
        except Exception:
            profile_tax_template = ""
    if profile_tax_template:
        configured_rows = frappe.get_all(
            "Sales Taxes and Charges",
            filters={
                "parent": profile_tax_template,
                "parenttype": "Sales Taxes and Charges Template",
                "account_head": ["is", "set"],
            },
            pluck="account_head",
            order_by="idx asc",
        )
        configured_accounts = list(dict.fromkeys(str(row) for row in configured_rows))
        if len(configured_accounts) == 1:
            return configured_accounts[0]
        if len(configured_accounts) > 1:
            raise ValueError(
                f"POS Profile tax template {profile_tax_template} has multiple tax accounts; KoPOS SST requires exactly one"
            )

    exact_tax_accounts = frappe.get_all(
        "Account",
        filters={
            "company": company_name,
            "account_type": "Tax",
            "is_group": 0,
        },
        pluck="name",
        order_by="name asc",
        limit=2,
    )
    if len(exact_tax_accounts) == 1:
        return str(exact_tax_accounts[0])
    if len(exact_tax_accounts) > 1:
        raise ValueError(
            f"Multiple tax accounts are configured for company {company_name}; configure one POS Profile tax template account"
        )

    duties_accounts = frappe.get_all(
        "Account",
        filters={
            "company": company_name,
            "name": ["like", "Duties and Taxes%"],
            "is_group": 0,
        },
        pluck="name",
        order_by="name asc",
        limit=2,
    )
    if len(duties_accounts) == 1:
        return str(duties_accounts[0])
    if len(duties_accounts) > 1:
        raise ValueError(
            f"Multiple duties and taxes accounts are configured for company {company_name}; configure one POS Profile tax template account"
        )

    raise ValueError(f"No tax account configured for company {company_name}")


def _resolve_rounding_gap_sen(invoice: Any, target_total_sen: int) -> int:
    current_total_sen = _money_sen(
        getattr(invoice, "grand_total", None),
        "Sales Invoice grand_total",
    )
    return current_total_sen - target_total_sen


def _resolve_rounding_gap(invoice: Any, target_total: Any) -> Decimal:
    """Compatibility wrapper returning an exact signed decimal rounding gap."""

    target_total_sen = _money_sen(target_total, "target_total")
    return sen_to_decimal(_resolve_rounding_gap_sen(invoice, target_total_sen))


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


def _build_modifier_snapshot(order_item: Any) -> str | None:
    modifiers = _get_order_item_modifier_rows(order_item)
    if not modifiers:
        return None

    rows = []
    for modifier_row in modifiers:
        modifier_id = _value(modifier_row, "modifier")
        if not modifier_id:
            continue
        price_adjustment_sen = _money_sen(
            _optional_money_value(_value(modifier_row, "price_adjustment")),
            f"FB Modifier {modifier_id} price_adjustment",
        )
        rows.append(
            {
                "id": modifier_id,
                "name": frappe.db.get_value("FB Modifier", modifier_id, "modifier_name")
                or modifier_id,
                "group_id": _value(modifier_row, "modifier_group"),
                "price_adjustment": format(
                    sen_to_decimal(price_adjustment_sen),
                    ".2f",
                ),
                "price_adjustment_sen": price_adjustment_sen,
            }
        )

    if not rows:
        return None
    return json.dumps({"modifiers": rows}, separators=(",", ":"))


def _get_order_item_modifier_rows(order_item: Any) -> list[Any]:
    persisted_rows = list(_value(order_item, "selected_modifiers") or [])
    if persisted_rows:
        return persisted_rows

    # ERPNext v16 cannot persist a nested child table on FB Order Line. The
    # submit path therefore carries the authenticated modifier selection on
    # the child row until FB Resolved Sale is written.
    transient_rows = getattr(order_item, "_selected_modifiers_payload", None)
    if transient_rows:
        return list(transient_rows)

    # Projection retries reload the FB Order and lose transient attributes.
    # FB Resolved Sale is the durable, canonical fallback for that path.
    resolved_sale_name = _value(order_item, "resolved_sale")
    if not resolved_sale_name:
        return []
    try:
        resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sale_name)
    except Exception as error:
        raise RuntimeError(
            "Unable to load linked FB Resolved Sale {0} for the Sales Invoice modifier audit snapshot".format(
                resolved_sale_name
            )
        ) from error
    return list(_value(resolved_sale, "selected_modifiers") or [])


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
    return make_savepoint(prefix)


def _rollback_savepoint(savepoint: str) -> None:
    rollback_to_savepoint(savepoint, title="Sales invoice projection rollback failed")


def _log_error(title: str) -> None:
    log_sanitized_error(title)
