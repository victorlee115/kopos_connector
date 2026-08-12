# pyright: reportMissingImports=false

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe

from kopos_connector.kopos.api.money_contract import (
    MoneyContractValidationError,
    persisted_money_to_sen,
)

from kopos_connector.utils.diagnostics import (
    log_sanitized_error,
    make_savepoint,
    rollback_to_savepoint,
)


PROMOTION_INVOICE_FIELDS = (
    "custom_kopos_pricing_mode",
    "custom_kopos_promotion_snapshot_version",
    "custom_kopos_promotion_snapshot_hash",
    "custom_kopos_promotion_reconciliation_status",
    "custom_kopos_promotion_payload",
)
PROMOTION_ITEM_FIELD = "custom_kopos_promotion_allocation"


def create_return_sales_invoice(fb_return_event: Any) -> str | None:
    return_doc = _coerce_doc("FB Return Event", fb_return_event)
    if not return_doc:
        return None

    existing_invoice = _get_existing_reference(return_doc, "return_sales_invoice")
    if existing_invoice:
        return existing_invoice

    original_invoice_name = _value(return_doc, "original_sales_invoice")
    if not original_invoice_name:
        return None

    savepoint = _make_savepoint("fb_return_invoice")

    try:
        original_invoice = frappe.get_doc("Sales Invoice", original_invoice_name)
        return_invoice = _make_standard_return_invoice(original_invoice_name)
        return_invoice.set_posting_time = 1
        posting_dt = _resolve_posting_datetime(return_doc)
        return_invoice.posting_date = posting_dt.date().isoformat()
        return_invoice.posting_time = posting_dt.time().strftime("%H:%M:%S")

        _set_if_present(
            return_invoice,
            ["fb_return_event", "custom_fb_return_event"],
            return_doc.name,
        )

        _copy_invoice_dimensions(original_invoice, return_invoice)
        _copy_promotion_provenance(original_invoice, return_invoice)
        _copy_commercial_line_provenance(original_invoice, return_invoice)
        if hasattr(return_invoice, "custom_fb_idempotency_key"):
            return_invoice.custom_fb_idempotency_key = None
        return_invoice.is_pos = 0
        if hasattr(return_invoice, "set"):
            return_invoice.set("payments", [])
        else:
            return_invoice.payments = []
        _validate_full_standard_return_items(return_doc, return_invoice)
        _clear_optional_return_links(return_doc, return_invoice)
        if hasattr(return_invoice, "set_missing_values"):
            return_invoice.set_missing_values()
        if hasattr(return_invoice, "calculate_taxes_and_totals"):
            return_invoice.calculate_taxes_and_totals()
        return_invoice.update_stock = 0
        if hasattr(return_invoice, "paid_amount"):
            return_invoice.paid_amount = 0
        if hasattr(return_invoice, "change_amount"):
            return_invoice.change_amount = 0

        return_invoice.insert(ignore_permissions=True)
        return_invoice.submit()

        _set_source_reference(return_doc, "return_sales_invoice", return_invoice.name)

        return return_invoice.name
    except Exception:
        _rollback_savepoint(savepoint)
        _log_error("Return sales invoice creation failed")
        raise


def _make_standard_return_invoice(original_invoice_name: str):
    from erpnext.controllers.sales_and_purchase_return import make_return_doc

    return make_return_doc("Sales Invoice", original_invoice_name)


def _validate_full_standard_return_items(return_doc: Any, return_invoice: Any) -> None:
    commercial_lines = [
        line
        for line in (_value(return_doc, "lines") or [])
        if cstr(_value(line, "original_sales_invoice_item")).strip()
    ]
    if commercial_lines:
        requested = {
            cstr(_value(line, "original_sales_invoice_item")).strip():
                _quantity_decimal(
                    _value(line, "qty_returned"), "requested return quantity"
                )
            for line in commercial_lines
        }
        returned = {
            cstr(_value(item, "sales_invoice_item")).strip(): abs(
                _quantity_decimal(
                    _value(item, "qty"), "standard return quantity"
                )
            )
            for item in (_value(return_invoice, "items") or [])
            if cstr(_value(item, "sales_invoice_item")).strip()
        }
        if not returned or returned != requested:
            frappe.throw(
                "Standard Sales Invoice return does not exactly match the requested full commercial line identities",
                frappe.ValidationError,
            )
        return

    requested: dict[str, Decimal] = {}
    for line in _value(return_doc, "lines") or []:
        resolved_sale = cstr(_value(line, "original_resolved_sale")).strip()
        if resolved_sale:
            requested[resolved_sale] = requested.get(
                resolved_sale, Decimal("0")
            ) + _quantity_decimal(
                _value(line, "qty_returned"), "requested return quantity"
            )
    returned: dict[str, Decimal] = {}
    for item in _value(return_invoice, "items") or []:
        resolved_sale = cstr(_value(item, "custom_fb_resolved_sale")).strip()
        if resolved_sale:
            returned[resolved_sale] = returned.get(
                resolved_sale, Decimal("0")
            ) + abs(
                _quantity_decimal(_value(item, "qty"), "standard return quantity")
            )
    if not returned or returned != requested:
        frappe.throw(
            "Standard Sales Invoice return does not exactly match the requested full resolved-sale quantities",
            frappe.ValidationError,
        )


def _quantity_decimal(value: Any, label: str) -> Decimal:
    try:
        return Decimal(cstr(value or 0).strip() or "0")
    except (InvalidOperation, ValueError) as error:
        frappe.throw(f"Invalid {label}: {value}", frappe.ValidationError)
        raise ValueError(f"Invalid {label}: {value}") from error


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
    rollback_to_savepoint(savepoint, title="Return sales invoice rollback failed")


def _log_error(title: str) -> None:
    log_sanitized_error(title)


def _resolve_posting_datetime(doc: Any):
    created_at = _value(doc, "modified") or _value(doc, "creation")
    if created_at:
        return frappe.utils.get_datetime(created_at)
    return frappe.utils.now_datetime()


def _set_if_present(doc: Any, fieldnames: list[str], value: Any) -> None:
    if value in (None, ""):
        return

    meta = frappe.get_meta(doc.doctype)
    for fieldname in fieldnames:
        if meta.has_field(fieldname):
            setattr(doc, fieldname, value)
            return


def flt(value: Any) -> float:
    return float(frappe.utils.flt(value))


def cstr(value: Any) -> str:
    return str(frappe.utils.cstr(value))


def _copy_invoice_dimensions(original_invoice: Any, return_invoice: Any) -> None:
    for fieldname in (
        "pos_profile",
        "cost_center",
        "project",
        "remarks",
        "custom_fb_order",
        "custom_fb_shift",
        "custom_fb_device_id",
        "custom_fb_event_project",
        "custom_fb_operational_status",
    ):
        if hasattr(original_invoice, fieldname):
            setattr(return_invoice, fieldname, getattr(original_invoice, fieldname))


def _copy_promotion_provenance(
    original_invoice: Any,
    return_invoice: Any,
) -> None:
    """Restore immutable promotion evidence omitted by standard no-copy mapping."""
    for fieldname in PROMOTION_INVOICE_FIELDS:
        if hasattr(original_invoice, fieldname):
            setattr(return_invoice, fieldname, _value(original_invoice, fieldname))

    original_items = list(_value(original_invoice, "items") or [])
    return_items = list(_value(return_invoice, "items") or [])
    if len(original_items) != len(return_items):
        frappe.throw(
            "Cannot preserve promotion provenance: standard return item count "
            "differs from the original Sales Invoice",
            frappe.ValidationError,
        )

    unmatched_original_items = list(original_items)
    for return_item in return_items:
        original_item = _match_original_promotion_item(
            return_item,
            unmatched_original_items,
        )
        if original_item is None:
            frappe.throw(
                "Cannot preserve promotion provenance: return item has no unique "
                "original Sales Invoice item",
                frappe.ValidationError,
            )
        setattr(
            return_item,
            PROMOTION_ITEM_FIELD,
            _value(original_item, PROMOTION_ITEM_FIELD),
        )
        unmatched_original_items.remove(original_item)


def _copy_commercial_line_provenance(
    original_invoice: Any,
    return_invoice: Any,
) -> None:
    """Keep cashier-sale identity and modifier evidence on the credit note."""
    original_items = list(_value(original_invoice, "items") or [])
    unmatched_original_items = list(original_items)
    for return_item in list(_value(return_invoice, "items") or []):
        original_item = _match_original_promotion_item(
            return_item,
            unmatched_original_items,
        )
        if original_item is None:
            frappe.throw(
                "Cannot preserve commercial provenance: return item has no unique original Sales Invoice item",
                frappe.ValidationError,
            )
        for fieldname in (
            "custom_fb_order_line_ref",
            "custom_kopos_modifiers",
            "custom_kopos_modifier_total",
            "custom_kopos_has_modifiers",
        ):
            if hasattr(original_item, fieldname):
                setattr(return_item, fieldname, _value(original_item, fieldname))
        unmatched_original_items.remove(original_item)


def _clear_optional_return_links(return_doc: Any, return_invoice: Any) -> None:
    """Remove recipe-era Links from a commercially identified credit note."""

    commercial_lines = [
        line
        for line in (_value(return_doc, "lines") or [])
        if cstr(_value(line, "original_sales_invoice_item")).strip()
    ]
    if not commercial_lines:
        return
    for return_item in list(_value(return_invoice, "items") or []):
        # Standard ERPNext return mapping can copy this legacy Link before our
        # commercial validation runs. It is optional recipe-era metadata and a
        # deleted target must not make an exact full credit note fail Link
        # validation.
        if hasattr(return_item, "custom_fb_resolved_sale"):
            return_item.custom_fb_resolved_sale = None


def _match_original_promotion_item(
    return_item: Any,
    original_items: list[Any],
) -> Any | None:
    original_item_name = cstr(_value(return_item, "sales_invoice_item")).strip()
    if original_item_name:
        matching_by_name = [
            item
            for item in original_items
            if cstr(_value(item, "name")).strip() == original_item_name
        ]
        if len(matching_by_name) == 1:
            return matching_by_name[0]
        if len(matching_by_name) > 1:
            frappe.throw(
                "Cannot preserve promotion provenance: Sales Invoice Item reference "
                f"{original_item_name} is ambiguous",
                frappe.ValidationError,
            )

    for fieldname, label in (
        ("custom_fb_order_line_ref", "FB Order line reference"),
        # In-memory matching only for old standard return documents. The Link
        # is cleared before insert and is never dereferenced or authoritative.
        ("custom_fb_resolved_sale", "FB Resolved Sale reference"),
    ):
        reference = cstr(_value(return_item, fieldname)).strip()
        if not reference:
            continue
        matching_items = [
            item
            for item in original_items
            if cstr(_value(item, fieldname)).strip() == reference
        ]
        if len(matching_items) == 1:
            return matching_items[0]
        if len(matching_items) > 1:
            frappe.throw(
                f"Cannot preserve promotion provenance: {label} {reference} is ambiguous",
                frappe.ValidationError,
            )
    return None


def lock_fb_shift_cash_scope(shift_name: Any) -> Any:
    """Serialize every cash-affecting mutation for one FB Shift."""
    shift = cstr(shift_name).strip()
    if not shift:
        frappe.throw("FB Shift is required for cash reconciliation", frappe.ValidationError)

    rows = frappe.db.sql(
        """
        SELECT name, opening_float, counted_cash
        FROM `tabFB Shift`
        WHERE name = %s
        LIMIT 1
        FOR UPDATE
        """,
        (shift,),
        as_dict=True,
    )
    if not rows:
        frappe.throw(f"FB Shift {shift} was not found", frappe.ValidationError)
    return rows[0]


def refresh_fb_shift_cash(shift_name: Any) -> None:
    """Reconcile shift cash from current, row-locked accounting evidence."""
    shift = cstr(shift_name).strip()
    if not shift:
        return

    shift_row, cash = _calculate_locked_fb_shift_cash(shift)
    updates: dict[str, Decimal] = {
        "expected_cash": cash["expected_cash"]
    }
    if _value(shift_row, "counted_cash") is not None:
        counted_cash_sen = _money_to_sen(
            _value(shift_row, "counted_cash"), f"FB Shift {shift} counted_cash"
        )
        updates["cash_variance"] = _sen_to_amount(
            counted_cash_sen - cash["expected_cash_sen"]
        )
    frappe.db.set_value(
        "FB Shift",
        shift,
        updates,
        update_modified=False,
    )


def calculate_fb_shift_cash(shift_name: Any) -> dict[str, Decimal]:
    """Return exact, accounting-backed shift cash while holding reconciliation locks."""

    shift = cstr(shift_name).strip()
    if not shift:
        frappe.throw("FB Shift is required for cash reconciliation", frappe.ValidationError)
    _shift_row, cash = _calculate_locked_fb_shift_cash(shift)
    return {
        "opening_float": cash["opening_float"],
        "cash_sales": cash["cash_sales"],
        "cash_refunds": cash["cash_refunds"],
        "net_cash": cash["net_cash"],
        "expected_cash": cash["expected_cash"],
    }


def _calculate_locked_fb_shift_cash(
    shift: str,
) -> tuple[Any, dict[str, Any]]:
    shift_row = lock_fb_shift_cash_scope(shift)
    cash_sales_sen = _get_locked_shift_sales_cash_sen(shift)
    cash_return_adjustment_sen = _get_locked_shift_return_cash_adjustment_sen(shift)
    opening_float_sen = _money_to_sen(
        _value(shift_row, "opening_float"), f"FB Shift {shift} opening_float"
    )
    net_cash_sen = cash_sales_sen + cash_return_adjustment_sen
    expected_cash_sen = opening_float_sen + net_cash_sen
    return shift_row, {
        "opening_float": _sen_to_amount(opening_float_sen),
        "cash_sales": _sen_to_amount(cash_sales_sen),
        "cash_refunds": _sen_to_amount(abs(cash_return_adjustment_sen)),
        "net_cash": _sen_to_amount(net_cash_sen),
        "expected_cash": _sen_to_amount(expected_cash_sen),
        "expected_cash_sen": expected_cash_sen,
    }


def _get_locked_shift_sales_cash_sen(shift: str) -> int:
    rows = frappe.db.sql(
        """
        SELECT
            sales_invoice.name AS sales_invoice,
            sales_invoice.change_amount,
            payment.name AS payment_row,
            payment.mode_of_payment,
            payment.amount AS payment_amount
        FROM `tabFB Order` AS fb_order
        INNER JOIN `tabSales Invoice` AS sales_invoice
            ON sales_invoice.name = fb_order.sales_invoice
        LEFT JOIN `tabSales Invoice Payment` AS payment
            ON payment.parent = sales_invoice.name
            AND payment.parenttype = 'Sales Invoice'
            AND payment.parentfield = 'payments'
        WHERE fb_order.shift = %s
            AND fb_order.status = 'Submitted'
            AND sales_invoice.docstatus = 1
        ORDER BY sales_invoice.name, payment.idx, payment.name
        FOR UPDATE
        """,
        (shift,),
        as_dict=True,
    )
    invoice_totals: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        invoice_name = cstr(_value(row, "sales_invoice")).strip()
        if not invoice_name:
            frappe.throw(
                f"FB Shift {shift} contains an order without a Sales Invoice",
                frappe.ValidationError,
            )
        totals = invoice_totals.setdefault(
            invoice_name,
            {
                "cash_tender_sen": 0,
                "change_sen": _money_to_sen(
                    _value(row, "change_amount"),
                    f"Sales Invoice {invoice_name} change_amount",
                ),
                "payment_rows": set(),
            },
        )
        observed_change_sen = _money_to_sen(
            _value(row, "change_amount"),
            f"Sales Invoice {invoice_name} change_amount",
        )
        if observed_change_sen != totals["change_sen"]:
            frappe.throw(
                f"Sales Invoice {invoice_name} has inconsistent change evidence",
                frappe.ValidationError,
            )

        payment_row = cstr(_value(row, "payment_row")).strip()
        if not payment_row or payment_row in totals["payment_rows"]:
            continue
        totals["payment_rows"].add(payment_row)
        if cstr(_value(row, "mode_of_payment")).strip().lower() == "cash":
            totals["cash_tender_sen"] += _money_to_sen(
                _value(row, "payment_amount"),
                f"Sales Invoice {invoice_name} cash payment",
            )

    total_cash_sen = 0
    for invoice_name, totals in invoice_totals.items():
        cash_tender_sen = int(totals["cash_tender_sen"])
        change_sen = int(totals["change_sen"])
        if change_sen < 0:
            frappe.throw(
                f"Sales Invoice {invoice_name} change_amount must be non-negative",
                frappe.ValidationError,
            )
        if change_sen > cash_tender_sen:
            frappe.throw(
                f"Sales Invoice {invoice_name} change exceeds its cash tender",
                frappe.ValidationError,
            )
        total_cash_sen += cash_tender_sen - change_sen
    return total_cash_sen


def _get_locked_shift_return_cash_adjustment_sen(shift: str) -> int:
    rows = frappe.db.sql(
        """
        SELECT
            return_event.name,
            return_event.refund_method,
            return_event.settlement_doctype,
            return_event.settlement_document,
            return_event.settlement_amount,
            return_event.settlement_tenders_json,
            settlement.name AS settlement_name,
            settlement.docstatus AS settlement_docstatus
        FROM `tabFB Return Event` AS return_event
        INNER JOIN `tabFB Order` AS fb_order
            ON fb_order.sales_invoice = return_event.original_sales_invoice
        LEFT JOIN `tabJournal Entry` AS settlement
            ON settlement.name = return_event.settlement_document
        WHERE fb_order.shift = %s
            AND fb_order.status = 'Submitted'
            AND return_event.docstatus = 1
            AND return_event.settlement_status = 'Posted'
        ORDER BY return_event.name
        FOR UPDATE
        """,
        (shift,),
        as_dict=True,
    )
    total_adjustment_sen = 0
    seen_events: set[str] = set()
    for row in rows or []:
        event_name = cstr(_value(row, "name")).strip()
        if not event_name or event_name in seen_events:
            continue
        seen_events.add(event_name)
        if cstr(_value(row, "refund_method")).strip().lower() != "cash":
            continue
        total_adjustment_sen -= _validate_locked_cash_settlement_sen(row, event_name)
    return total_adjustment_sen


def _validate_locked_cash_settlement_sen(row: Any, event_name: str) -> int:
    settlement_doctype = cstr(_value(row, "settlement_doctype")).strip()
    settlement_document = cstr(_value(row, "settlement_document")).strip()
    if settlement_doctype != "Journal Entry" or not settlement_document:
        frappe.throw(
            f"FB Return Event {event_name} has no Journal Entry settlement proof",
            frappe.ValidationError,
        )
    if (
        cstr(_value(row, "settlement_name")).strip() != settlement_document
        or int(_value(row, "settlement_docstatus") or 0) != 1
    ):
        frappe.throw(
            f"FB Return Event {event_name} settlement is not submitted",
            frappe.ValidationError,
        )

    settlement_amount_sen = _money_to_sen(
        _value(row, "settlement_amount"),
        f"FB Return Event {event_name} settlement_amount",
    )
    if settlement_amount_sen <= 0:
        frappe.throw(
            f"FB Return Event {event_name} settlement_amount must be positive",
            frappe.ValidationError,
        )
    raw_evidence = _value(row, "settlement_tenders_json")
    try:
        evidence = json.loads(cstr(raw_evidence))
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        frappe.throw(
            f"FB Return Event {event_name} has invalid settlement tender evidence",
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error
    if not isinstance(evidence, Mapping):
        frappe.throw(
            f"FB Return Event {event_name} has invalid settlement tender evidence",
            frappe.ValidationError,
        )

    evidence_amount_sen = _strict_evidence_sen(
        evidence.get("settlement_amount_sen"),
        event_name,
        "settlement_amount_sen",
    )
    customer_debit_sen = _strict_evidence_sen(
        evidence.get("customer_debit_sen"), event_name, "customer_debit_sen"
    )
    return_outstanding_sen = _strict_evidence_sen(
        evidence.get("return_outstanding_sen"),
        event_name,
        "return_outstanding_sen",
    )
    if (
        cstr(evidence.get("refund_method")).strip().lower() != "cash"
        or evidence_amount_sen != settlement_amount_sen
        or customer_debit_sen != settlement_amount_sen
        or return_outstanding_sen != 0
    ):
        frappe.throw(
            f"FB Return Event {event_name} settlement tender evidence is inconsistent",
            frappe.ValidationError,
        )

    tenders = evidence.get("tenders")
    if not isinstance(tenders, list) or not tenders:
        frappe.throw(
            f"FB Return Event {event_name} has no settlement tender rows",
            frappe.ValidationError,
        )
    tender_total_sen = 0
    for index, tender in enumerate(tenders, start=1):
        if not isinstance(tender, Mapping):
            frappe.throw(
                f"FB Return Event {event_name} tender {index} is invalid",
                frappe.ValidationError,
            )
        if cstr(tender.get("refund_method")).strip().lower() != "cash":
            frappe.throw(
                f"FB Return Event {event_name} tender {index} is not cash",
                frappe.ValidationError,
            )
        amount_sen = _strict_evidence_sen(
            tender.get("amount_sen"), event_name, f"tenders[{index}].amount_sen"
        )
        if amount_sen <= 0:
            frappe.throw(
                f"FB Return Event {event_name} tender {index} must be positive",
                frappe.ValidationError,
            )
        tender_total_sen += amount_sen
    if tender_total_sen != settlement_amount_sen:
        frappe.throw(
            f"FB Return Event {event_name} tender total does not match settlement_amount",
            frappe.ValidationError,
        )
    return settlement_amount_sen


def _strict_evidence_sen(value: Any, event_name: str, fieldname: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        frappe.throw(
            f"FB Return Event {event_name} {fieldname} must be integer sen",
            frappe.ValidationError,
        )
    return value


def _get_cash_payment_total_sen(invoice: Any) -> int:
    cash_tender_sen = 0
    for payment in _value(invoice, "payments") or []:
        if cstr(_value(payment, "mode_of_payment")).strip().lower() == "cash":
            cash_tender_sen += _money_to_sen(
                _value(payment, "amount"), "Sales Invoice cash payment"
            )

    change_sen = _money_to_sen(
        _value(invoice, "change_amount"), "Sales Invoice change_amount"
    )
    if change_sen < 0:
        frappe.throw(
            "Sales Invoice change_amount must be non-negative",
            frappe.ValidationError,
        )
    if change_sen > cash_tender_sen:
        frappe.throw(
            "Sales Invoice change exceeds its cash tender",
            frappe.ValidationError,
        )
    return cash_tender_sen - change_sen


def _money_to_sen(value: Any, label: str) -> int:
    try:
        return persisted_money_to_sen(
            0 if value is None or value == "" else value,
            label,
        )
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error


def _sen_to_amount(value_sen: int) -> Decimal:
    return Decimal(value_sen) / Decimal("100")


def _set_doc_field(doc: Any, fieldname: str, value: Any) -> None:
    db_set = getattr(doc, "db_set", None)
    if callable(db_set):
        db_set(fieldname, value, update_modified=False)
        return
    setattr(doc, fieldname, value)
