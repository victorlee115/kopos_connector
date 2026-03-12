from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from kopos_connector.api.catalog import get_default_pos_profile, get_tax_rate_value
from kopos_connector.api.devices import elevate_device_api_user, get_device_doc


PAYMENT_METHOD_ALIASES = {
    "cash": {"cash"},
    "qr": {"qr", "duitnow qr", "duitnow", "e wallet", "ewallet", "wallet"},
    "card": {"card", "credit card", "debit card"},
    "voucher": {"voucher", "coupon", "gift voucher"},
}

AMOUNT_TOLERANCE = 0.01

REFUND_REASON_OPTIONS = {
    "customer_changed_mind": "Customer changed mind",
    "wrong_order": "Wrong order",
    "quality_issue": "Quality issue",
    "item_damaged": "Item damaged",
    "service_issue": "Service issue",
    "pricing_error": "Pricing error",
    "other": "Other",
}

PRICING_MODES = {
    "legacy_client",
    "manual_only",
    "online_snapshot",
    "offline_snapshot",
    "server_validated",
}

PROMOTION_RECONCILIATION_STATUSES = {
    "not_applicable",
    "pending",
    "matched",
    "review_required",
}


def submit_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a POS Invoice from a KoPOS payload with idempotency handling."""
    validated = validate_submit_order_payload(payload)
    idempotency_key = validated["idempotency_key"]

    existing_invoice = frappe.db.get_value(
        "POS Invoice",
        {"custom_kopos_idempotency_key": idempotency_key},
        "name",
    )
    if existing_invoice:
        return {
            "status": "duplicate",
            "pos_invoice": existing_invoice,
            "idempotency_key": idempotency_key,
            "message": _("Order already processed"),
        }

    try:
        with elevate_device_api_user():
            pos_profile_doc = resolve_pos_profile(validated)
            invoice = build_pos_invoice(validated, pos_profile_doc)
            invoice.insert(ignore_permissions=True)
            invoice.submit()
            record_invoice_promotion_comment(invoice)
    except Exception:
        frappe.db.rollback()
        existing_invoice = frappe.db.get_value(
            "POS Invoice",
            {"custom_kopos_idempotency_key": idempotency_key},
            "name",
        )
        if existing_invoice:
            return {
                "status": "duplicate",
                "pos_invoice": existing_invoice,
                "idempotency_key": idempotency_key,
                "message": _("Order already processed"),
            }
        raise

    return {
        "status": "ok",
        "pos_invoice": invoice.name,
        "idempotency_key": idempotency_key,
    }


def validate_submit_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a KoPOS submit_order payload."""
    idempotency_key = cstr(payload.get("idempotency_key"))
    device_id = cstr(payload.get("device_id"))
    order = payload.get("order")

    if not idempotency_key:
        frappe.throw(_("idempotency_key is required"), frappe.ValidationError)
    if not device_id:
        frappe.throw(_("device_id is required"), frappe.ValidationError)
    if not isinstance(order, dict):
        frappe.throw(_("order payload must be an object"), frappe.ValidationError)

    items = order.get("items")
    payments = order.get("payments")
    if not isinstance(items, list) or not items:
        frappe.throw(_("order.items must not be empty"), frappe.ValidationError)
    if not isinstance(payments, list) or not payments:
        frappe.throw(_("order.payments must not be empty"), frappe.ValidationError)

    validated_items = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            frappe.throw(_("Each order item must be an object"), frappe.ValidationError)
        item_code = cstr(raw_item.get("item_code"))
        qty = flt(raw_item.get("qty"))
        amount = flt(raw_item.get("amount"))
        base_rate = flt(raw_item.get("base_rate") or raw_item.get("rate"))
        base_amount = flt(raw_item.get("base_amount"))
        item_discount_amount = flt(raw_item.get("discount_amount"))
        if not item_code:
            frappe.throw(
                _("order.items[].item_code is required"), frappe.ValidationError
            )
        if qty <= 0:
            frappe.throw(
                _("order.items[].qty must be greater than 0"), frappe.ValidationError
            )
        if amount < 0:
            frappe.throw(
                _("order.items[].amount must be 0 or greater"), frappe.ValidationError
            )
        if base_rate < 0:
            frappe.throw(
                _("order.items[].base_rate must be 0 or greater"),
                frappe.ValidationError,
            )
        if base_amount < 0:
            frappe.throw(
                _("order.items[].base_amount must be 0 or greater"),
                frappe.ValidationError,
            )
        if item_discount_amount < 0:
            frappe.throw(
                _("order.items[].discount_amount must be 0 or greater"),
                frappe.ValidationError,
            )

        computed_base_amount = (base_rate + flt(raw_item.get("modifier_total"))) * qty
        normalized_base_amount = base_amount or computed_base_amount
        normalized_item_discount = (
            item_discount_amount
            if raw_item.get("discount_amount") is not None
            else max(normalized_base_amount - amount, 0)
        )
        if normalized_base_amount + AMOUNT_TOLERANCE < amount:
            frappe.throw(
                _("order.items[].base_amount must be greater than or equal to amount"),
                frappe.ValidationError,
            )

        validated_items.append(
            {
                "item_code": item_code,
                "item_name": cstr(raw_item.get("item_name")),
                "qty": qty,
                "rate": flt(raw_item.get("rate")),
                "base_rate": base_rate,
                "modifier_total": flt(raw_item.get("modifier_total")),
                "base_amount": normalized_base_amount,
                "discount_amount": normalized_item_discount,
                "amount": amount,
                "modifiers": raw_item.get("modifiers")
                if isinstance(raw_item.get("modifiers"), list)
                else [],
                "promotion_allocations": validate_promotion_allocations(
                    raw_item.get("promotion_allocations")
                ),
            }
        )

    validated_payments = []
    for raw_payment in payments:
        if not isinstance(raw_payment, dict):
            frappe.throw(_("Each payment must be an object"), frappe.ValidationError)
        method = cstr(raw_payment.get("method"))
        amount = flt(raw_payment.get("amount"))
        if not method:
            frappe.throw(
                _("order.payments[].method is required"), frappe.ValidationError
            )
        if amount <= 0:
            frappe.throw(
                _("order.payments[].amount must be greater than 0"),
                frappe.ValidationError,
            )
        validated_payments.append(
            {
                "method": method,
                "amount": amount,
                "tendered": flt(raw_payment.get("tendered")),
                "change": flt(raw_payment.get("change")),
            }
        )

    subtotal = flt(order.get("subtotal"))
    tax_amount = flt(order.get("tax_amount"))
    discount_amount = flt(order.get("discount_amount"))
    rounding_adj = flt(order.get("rounding_adj"))
    total = flt(order.get("total"))

    computed_subtotal = sum(item["amount"] for item in validated_items)
    if subtotal and not amounts_match(subtotal, computed_subtotal):
        frappe.throw(
            _("order.subtotal does not match summed item amounts"),
            frappe.ValidationError,
        )

    computed_total = computed_subtotal + tax_amount - discount_amount + rounding_adj
    if total <= 0:
        frappe.throw(_("order.total must be greater than 0"), frappe.ValidationError)
    if not amounts_match(total, computed_total):
        frappe.throw(
            _("order.total does not match subtotal, tax, discount, and rounding"),
            frappe.ValidationError,
        )

    applied_payment_total = sum(payment["amount"] for payment in validated_payments)
    if not amounts_match(applied_payment_total, total):
        frappe.throw(
            _("order.payments total must equal order.total"),
            frappe.ValidationError,
        )

    pricing_context = validate_pricing_context(payload.get("pricing_context"))
    applied_promotions = validate_applied_promotions(payload.get("applied_promotions"))

    return {
        "idempotency_key": idempotency_key,
        "device_id": device_id,
        "pos_profile": cstr(payload.get("pos_profile")) or None,
        "warehouse": cstr(payload.get("warehouse")) or None,
        "company": cstr(payload.get("company")) or None,
        "currency": cstr(payload.get("currency")) or None,
        "offline_priced": bool(payload.get("offline_priced", False)),
        "pricing_context": pricing_context,
        "applied_promotions": applied_promotions,
        "order": {
            "display_number": cstr(order.get("display_number")),
            "order_type": cstr(order.get("order_type") or "takeaway"),
            "subtotal": subtotal,
            "tax_amount": tax_amount,
            "tax_rate": flt(order.get("tax_rate")),
            "discount_amount": discount_amount,
            "rounding_adj": rounding_adj,
            "total": total,
            "created_at": cstr(order.get("created_at")),
            "items": validated_items,
            "payments": validated_payments,
        },
    }


def resolve_pos_profile(payload: dict[str, Any]):
    """Resolve the POS Profile to use for an inbound KoPOS order."""
    device_id = cstr(payload.get("device_id"))
    if device_id:
        device_doc = get_device_doc(device_id=device_id)
        if not cint(device_doc.enabled):
            frappe.throw(
                _("KoPOS Device {0} is disabled").format(device_doc.device_id),
                frappe.ValidationError,
            )
        return frappe.get_cached_doc("POS Profile", device_doc.pos_profile)

    profile_name = payload.get("pos_profile")
    if profile_name:
        if not frappe.db.exists("POS Profile", profile_name):
            frappe.throw(
                _("POS Profile {0} was not found").format(profile_name),
                frappe.ValidationError,
            )
        return frappe.get_cached_doc("POS Profile", profile_name)

    default_profile = get_default_pos_profile(payload.get("company"))
    if not default_profile:
        frappe.throw(_("No enabled POS Profile was found"), frappe.ValidationError)

    return frappe.get_cached_doc("POS Profile", default_profile["name"])


def build_pos_invoice(payload: dict[str, Any], pos_profile_doc):
    """Build a draft POS Invoice document from the payload."""
    from erpnext.accounts.doctype.sales_invoice.sales_invoice import (
        get_mode_of_payment_info,
    )

    order = payload["order"]
    created_at = get_datetime(order["created_at"] or None)
    warehouse = payload.get("warehouse") or pos_profile_doc.get("warehouse")
    customer = pos_profile_doc.get("customer")

    if not customer:
        frappe.throw(
            _("POS Profile {0} must define a default Customer").format(
                pos_profile_doc.name
            ),
            frappe.ValidationError,
        )

    invoice = frappe.new_doc("POS Invoice")
    invoice.is_pos = 1
    invoice.pos_profile = pos_profile_doc.name
    invoice.company = payload.get("company") or pos_profile_doc.get("company")
    invoice.customer = customer
    invoice.currency = payload.get("currency") or pos_profile_doc.get("currency")
    invoice.posting_date = created_at.date().isoformat()
    invoice.posting_time = created_at.time().strftime("%H:%M:%S")
    invoice.set_posting_time = 1
    invoice.custom_kopos_idempotency_key = payload["idempotency_key"]
    invoice.custom_kopos_device_id = payload["device_id"]
    set_invoice_promotion_metadata(invoice, payload)
    invoice.remarks = build_invoice_remarks(payload)
    invoice.ignore_pricing_rule = 1

    for raw_item in order["items"]:
        item_doc = frappe.get_cached_doc("Item", raw_item["item_code"])
        row = {
            "item_code": item_doc.name,
            "qty": raw_item["qty"],
            "uom": item_doc.stock_uom,
            "stock_uom": item_doc.stock_uom,
            "conversion_factor": 1,
        }
        if warehouse:
            row["warehouse"] = warehouse
        invoice.append("items", row)

    invoice.set_missing_values()

    for invoice_item, raw_item in zip(invoice.items, order["items"], strict=False):
        effective_rate = raw_item["amount"] / raw_item["qty"] if raw_item["qty"] else 0
        invoice_item.item_name = raw_item["item_name"] or invoice_item.item_name
        invoice_item.description = build_item_description(
            raw_item, invoice_item.description
        )
        invoice_item.rate = effective_rate
        invoice_item.price_list_rate = (
            raw_item["base_rate"] or raw_item["rate"] or effective_rate
        )
        if hasattr(invoice_item, "custom_kopos_promotion_allocation"):
            invoice_item.custom_kopos_promotion_allocation = serialize_json_compact(
                {
                    "base_amount": raw_item.get("base_amount"),
                    "discount_amount": raw_item.get("discount_amount"),
                    "promotion_allocations": raw_item.get("promotion_allocations")
                    or [],
                }
            )
        if warehouse:
            invoice_item.warehouse = warehouse

    if order["discount_amount"] > 0:
        invoice.apply_discount_on = "Grand Total"
        invoice.discount_amount = order["discount_amount"]

    if hasattr(invoice, "calculate_taxes_and_totals"):
        invoice.calculate_taxes_and_totals()

    if order["rounding_adj"]:
        invoice.rounding_adjustment = order["rounding_adj"]
        invoice.rounded_total = order["total"]

    invoice.set("payments", [])
    default_mode = get_default_mode_of_payment(pos_profile_doc)
    for index, raw_payment in enumerate(order["payments"], start=1):
        mode_of_payment = resolve_mode_of_payment(
            raw_payment["method"], pos_profile_doc, default_mode
        )
        mode_info = get_mode_of_payment_info(mode_of_payment, invoice.company)
        if not mode_info:
            frappe.throw(
                _("Mode of Payment {0} is not configured for company {1}").format(
                    mode_of_payment, invoice.company
                ),
                frappe.ValidationError,
            )

        payment_meta = mode_info[0]
        invoice.append(
            "payments",
            {
                "idx": index,
                "mode_of_payment": mode_of_payment,
                "amount": raw_payment["amount"],
                "account": payment_meta.get("account"),
                "type": payment_meta.get("type"),
                "default": 1 if mode_of_payment == default_mode else 0,
            },
        )

    invoice.paid_amount = sum(payment["amount"] for payment in order["payments"])
    invoice.change_amount = sum(payment["change"] for payment in order["payments"])
    if invoice.change_amount and pos_profile_doc.get("account_for_change_amount"):
        invoice.account_for_change_amount = pos_profile_doc.get(
            "account_for_change_amount"
        )

    expected_total = order["total"]
    if expected_total > 0 and not invoice.rounded_total:
        invoice.rounded_total = expected_total

    return invoice


def get_default_mode_of_payment(pos_profile_doc) -> str | None:
    for payment in pos_profile_doc.get("payments") or []:
        if payment.default:
            return payment.mode_of_payment
    first = (pos_profile_doc.get("payments") or [None])[0]
    return first.mode_of_payment if first else None


def resolve_mode_of_payment(
    method: str, pos_profile_doc, default_mode: str | None
) -> str:
    """Map KoPOS payment methods to ERPNext Mode of Payment values."""
    normalized_method = normalize_token(method)
    available_modes = [
        row.mode_of_payment for row in (pos_profile_doc.get("payments") or [])
    ]
    for mode in available_modes:
        if normalize_token(mode) == normalized_method:
            return mode

    aliases = PAYMENT_METHOD_ALIASES.get(normalized_method, {normalized_method})
    for mode in available_modes:
        if normalize_token(mode) in aliases:
            return mode

    if default_mode:
        return default_mode

    frappe.throw(
        _("Could not map payment method {0} to a POS Profile payment mode").format(
            method
        ),
        frappe.ValidationError,
    )


def build_item_description(
    raw_item: dict[str, Any], fallback_description: str | None
) -> str:
    """Build an item description that preserves modifier snapshots."""
    lines = [cstr(fallback_description or raw_item.get("item_name"))]
    modifiers = raw_item.get("modifiers") or []
    if modifiers:
        lines.append("")
        lines.append(_("Modifiers:"))
        for modifier in modifiers:
            name = cstr((modifier or {}).get("name"))
            price = flt((modifier or {}).get("price"))
            if name:
                lines.append(f"- {name} (+{price:.2f})")
        return "\n".join(lines)
    return cstr(fallback_description or raw_item.get("item_name"))


def build_invoice_remarks(payload: dict[str, Any]) -> str:
    order = payload["order"]
    applied_promotions = payload.get("applied_promotions") or []
    pricing_context = payload.get("pricing_context") or {}
    tax_rate = order.get("tax_rate")
    tax_percent = (
        tax_rate * 100
        if isinstance(tax_rate, (int, float))
        else get_tax_rate_value(payload.get("pos_profile")) * 100
    )
    return "\n".join(
        [
            f"KoPOS display number: {order.get('display_number') or 'N/A'}",
            f"KoPOS device: {payload.get('device_id')}",
            f"KoPOS idempotency key: {payload.get('idempotency_key')}",
            f"KoPOS order type: {order.get('order_type')}",
            f"KoPOS tax rate: {tax_percent:.2f}%",
            f"KoPOS pricing mode: {pricing_context.get('pricing_mode') or 'legacy_client'}",
            f"KoPOS snapshot version: {pricing_context.get('snapshot_version') or 'N/A'}",
            f"KoPOS applied promotions: {len(applied_promotions)}",
        ]
    )


def set_invoice_promotion_metadata(invoice: Any, payload: dict[str, Any]) -> None:
    from kopos_connector.api.promotions import (
        append_promotion_audit_event,
        build_reconciliation_result,
        derive_review_status,
        reconcile_promotion_payload,
    )

    pricing_context = payload.get("pricing_context") or {}
    applied_promotions = payload.get("applied_promotions") or []
    pos_profile = cstr(
        payload.get("pos_profile") or getattr(invoice, "pos_profile", None)
    )
    reconciliation = (
        reconcile_promotion_payload(pos_profile, payload)
        if pos_profile and applied_promotions
        else build_reconciliation_result(
            determine_reconciliation_status(payload),
            "Promotion reconciliation deferred"
            if payload.get("applied_promotions")
            else "No applied promotions present",
        )
    )
    metadata = {
        "offline_priced": bool(payload.get("offline_priced", False)),
        "pricing_context": pricing_context,
        "applied_promotions": applied_promotions,
        "reconciliation": reconciliation,
        "items": [
            {
                "item_code": item.get("item_code"),
                "base_amount": item.get("base_amount"),
                "discount_amount": item.get("discount_amount"),
                "promotion_allocations": item.get("promotion_allocations") or [],
            }
            for item in payload.get("order", {}).get("items", [])
        ],
    }
    append_promotion_audit_event(
        metadata,
        "order.received",
        {
            "offline_priced": bool(payload.get("offline_priced", False)),
            "pricing_mode": cstr(
                pricing_context.get("pricing_mode") or "legacy_client"
            ),
            "snapshot_version": cstr(pricing_context.get("snapshot_version")),
            "applied_promotion_count": len(applied_promotions),
        },
    )
    append_promotion_audit_event(
        metadata,
        "reconciliation.{0}".format(cstr(reconciliation.get("status") or "unknown")),
        {
            "message": cstr(reconciliation.get("message")),
            "severity": cstr(reconciliation.get("severity")),
            "review_route": cstr(reconciliation.get("review_route")),
        },
    )

    if hasattr(invoice, "custom_kopos_promotion_snapshot_version"):
        invoice.custom_kopos_promotion_snapshot_version = cstr(
            pricing_context.get("snapshot_version")
        )
    if hasattr(invoice, "custom_kopos_pricing_mode"):
        invoice.custom_kopos_pricing_mode = cstr(
            pricing_context.get("pricing_mode") or "legacy_client"
        )
    if hasattr(invoice, "custom_kopos_promotion_payload"):
        invoice.custom_kopos_promotion_payload = serialize_json_compact(metadata)
    if hasattr(invoice, "custom_kopos_promotion_reconciliation_status"):
        invoice.custom_kopos_promotion_reconciliation_status = cstr(
            reconciliation.get("status") or determine_reconciliation_status(payload)
        )
    if hasattr(invoice, "custom_kopos_promotion_review_status"):
        invoice.custom_kopos_promotion_review_status = derive_review_status(
            cstr(
                reconciliation.get("status") or determine_reconciliation_status(payload)
            )
        )
    if hasattr(invoice, "custom_kopos_promotion_review_decision"):
        invoice.custom_kopos_promotion_review_decision = None
    if hasattr(invoice, "custom_kopos_promotion_reviewed_by"):
        invoice.custom_kopos_promotion_reviewed_by = None
    if hasattr(invoice, "custom_kopos_promotion_reviewed_at"):
        invoice.custom_kopos_promotion_reviewed_at = None
    if hasattr(invoice, "custom_kopos_promotion_review_notes"):
        invoice.custom_kopos_promotion_review_notes = None


def record_invoice_promotion_comment(invoice: Any) -> None:
    from kopos_connector.api.promotions import (
        add_audit_comment,
        get_invoice_promotion_metadata,
    )

    metadata = get_invoice_promotion_metadata(invoice)
    reconciliation = (
        metadata.get("reconciliation") if isinstance(metadata, dict) else {}
    )
    if not isinstance(reconciliation, dict):
        return

    status = cstr(reconciliation.get("status")) or cstr(
        getattr(invoice, "custom_kopos_promotion_reconciliation_status", None)
    )
    if status == "not_applicable":
        return

    message = (
        cstr(reconciliation.get("message")) or "Promotion reconciliation processed"
    )
    severity = cstr(reconciliation.get("severity"))
    review_route = cstr(reconciliation.get("review_route"))
    summary = "KoPOS promotion reconciliation {0}: {1}".format(status, message)
    if severity:
        summary += " (severity: {0})".format(severity)
    if review_route:
        summary += " via {0}".format(review_route)
    add_audit_comment(invoice, summary)


def determine_reconciliation_status(payload: dict[str, Any]) -> str:
    pricing_context = payload.get("pricing_context") or {}
    if payload.get("applied_promotions"):
        if pricing_context.get("restricted_mode"):
            return "review_required"
        return "pending" if payload.get("offline_priced") else "matched"
    return "not_applicable"


def validate_pricing_context(raw_context: Any) -> dict[str, Any]:
    if raw_context in (None, ""):
        return {}
    if not isinstance(raw_context, dict):
        frappe.throw(_("pricing_context must be an object"), frappe.ValidationError)

    pricing_mode = cstr(raw_context.get("pricing_mode") or "legacy_client")
    if pricing_mode not in PRICING_MODES:
        frappe.throw(
            _("pricing_context.pricing_mode is invalid"), frappe.ValidationError
        )

    return {
        "snapshot_version": cstr(raw_context.get("snapshot_version")),
        "snapshot_hash": cstr(raw_context.get("snapshot_hash")),
        "pricing_mode": pricing_mode,
        "restricted_mode": bool(raw_context.get("restricted_mode", False)),
        "priced_at": cstr(raw_context.get("priced_at")),
    }


def validate_applied_promotions(raw_promotions: Any) -> list[dict[str, Any]]:
    if raw_promotions in (None, ""):
        return []
    if not isinstance(raw_promotions, list):
        frappe.throw(_("applied_promotions must be an array"), frappe.ValidationError)

    normalized = []
    for index, raw_promotion in enumerate(raw_promotions):
        if not isinstance(raw_promotion, dict):
            frappe.throw(
                _("applied_promotions[{0}] must be an object").format(index),
                frappe.ValidationError,
            )
        promotion_id = cstr(raw_promotion.get("promotion_id"))
        if not promotion_id:
            frappe.throw(
                _("applied_promotions[{0}].promotion_id is required").format(index),
                frappe.ValidationError,
            )
        normalized.append(
            {
                "promotion_id": promotion_id,
                "promotion_name": cstr(raw_promotion.get("promotion_name")),
                "promotion_type": cstr(raw_promotion.get("promotion_type")),
                "amount": flt(raw_promotion.get("amount")),
                "scope": cstr(raw_promotion.get("scope")),
                "source": cstr(raw_promotion.get("source")),
            }
        )
    return normalized


def validate_promotion_allocations(raw_allocations: Any) -> list[dict[str, Any]]:
    if raw_allocations in (None, ""):
        return []
    if not isinstance(raw_allocations, list):
        frappe.throw(
            _("order.items[].promotion_allocations must be an array"),
            frappe.ValidationError,
        )

    normalized = []
    for index, raw_allocation in enumerate(raw_allocations):
        if not isinstance(raw_allocation, dict):
            frappe.throw(
                _("order.items[].promotion_allocations[{0}] must be an object").format(
                    index
                ),
                frappe.ValidationError,
            )
        promotion_id = cstr(raw_allocation.get("promotion_id"))
        if not promotion_id:
            frappe.throw(
                _(
                    "order.items[].promotion_allocations[{0}].promotion_id is required"
                ).format(index),
                frappe.ValidationError,
            )
        normalized.append(
            {
                "promotion_id": promotion_id,
                "amount": flt(raw_allocation.get("amount")),
                "quantity": flt(raw_allocation.get("quantity")),
                "scope": cstr(raw_allocation.get("scope")),
            }
        )
    return normalized


def serialize_json_compact(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def normalize_token(value: Any) -> str:
    return " ".join(
        cstr(value).strip().lower().replace("_", " ").replace("-", " ").split()
    )


def amounts_match(
    left: float, right: float, tolerance: float = AMOUNT_TOLERANCE
) -> bool:
    return abs(flt(left) - flt(right)) <= tolerance


def cstr(value: Any) -> str:
    return frappe.utils.cstr(value)


def flt(value: Any) -> float:
    return frappe.utils.flt(value)


REFUND_PAYMENT_MODES = {
    "cash": "Cash",
    "qr": "DuitNow QR",
    "card": "Card",
    "voucher": "Voucher",
}


def get_refund_reason_choices() -> list[dict[str, str]]:
    return [
        {"code": code, "label": label} for code, label in REFUND_REASON_OPTIONS.items()
    ]


def process_refund_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Process a refund by creating a Credit Note against a POS Invoice."""
    validated = validate_refund_payload(payload)
    idempotency_key = validated["idempotency_key"]
    original_invoice_name = validated["original_invoice"]

    # Check for duplicate refund
    existing_refund = frappe.db.get_value(
        "POS Invoice",
        {"custom_kopos_idempotency_key": idempotency_key, "is_return": 1},
        "name",
    )
    if existing_refund:
        return {
            "status": "duplicate",
            "credit_note": existing_refund,
            "idempotency_key": idempotency_key,
            "message": _("Refund already processed"),
        }

    # Get original invoice
    original_invoice = frappe.get_doc("POS Invoice", original_invoice_name)
    if original_invoice.docstatus != 1:
        frappe.throw(
            _("POS Invoice {0} is not submitted").format(original_invoice_name),
            frappe.ValidationError,
        )

    # Verify was created via KoPOS
    if not original_invoice.custom_kopos_idempotency_key:
        frappe.throw(
            _("POS Invoice {0} was not created via KoPOS").format(
                original_invoice_name
            ),
            frappe.ValidationError,
        )

    if validated["refund_type"] == "partial":
        validated["refund_amount"] = calculate_partial_refund_amount(
            original_invoice, validated["items"] or []
        )

    # Check for existing refunds
    total_refunded = (
        frappe.db.sql(
            """SELECT ABS(SUM(grand_total)) FROM `tabPOS Invoice`
        WHERE return_against = %s AND docstatus = 1 AND is_return = 1""",
            original_invoice_name,
        )[0][0]
        or 0
    )

    if total_refunded:
        max_refundable = original_invoice.grand_total - flt(total_refunded)
        if validated["refund_amount"] > max_refundable:
            frappe.throw(
                _("Refund amount {0} exceeds maximum refundable amount {1}").format(
                    validated["refund_amount"],
                    max_refundable,
                ),
                frappe.ValidationError,
            )

    # Build Credit Note using the original sale's POS configuration
    pos_profile_name = cstr(getattr(original_invoice, "pos_profile", None))
    if pos_profile_name:
        pos_profile = frappe.get_cached_doc("POS Profile", pos_profile_name)
    else:
        pos_profile = get_default_pos_profile()
        if pos_profile and isinstance(pos_profile, dict):
            pos_profile = frappe.get_cached_doc("POS Profile", pos_profile["name"])
    if not pos_profile:
        frappe.throw(_("No active POS Profile found"), frappe.ValidationError)

    try:
        with elevate_device_api_user():
            credit_note = build_credit_note(validated, original_invoice, pos_profile)

            payment_mode = resolve_refund_payment_mode(
                validated.get("payment_mode"), original_invoice
            )
            payments = credit_note.get("payments") or []
            for payment in payments:
                payment.mode_of_payment = payment_mode
                payment.amount = credit_note.grand_total / len(payments)

            if not payments:
                credit_note.append(
                    "payments",
                    {
                        "mode_of_payment": payment_mode,
                        "amount": credit_note.grand_total,
                    },
                )

            credit_note.paid_amount = sum(
                flt(payment.amount) for payment in credit_note.get("payments") or []
            )
            credit_note.change_amount = 0
            if hasattr(credit_note, "write_off_amount"):
                credit_note.write_off_amount = 0

            credit_note.insert(ignore_permissions=True)
            credit_note.submit()
    except Exception:
        frappe.db.rollback()
        existing_refund = frappe.db.get_value(
            "POS Invoice",
            {"custom_kopos_idempotency_key": idempotency_key, "is_return": 1},
            "name",
        )
        if existing_refund:
            return {
                "status": "duplicate",
                "credit_note": existing_refund,
                "idempotency_key": idempotency_key,
                "message": _("Refund already processed"),
            }
        raise

    return {
        "status": "ok",
        "credit_note": credit_note.name,
        "idempotency_key": idempotency_key,
        "refund_amount": validated["refund_amount"],
    }


def validate_refund_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize refund payload."""
    idempotency_key = cstr(payload.get("idempotency_key"))
    device_id = cstr(payload.get("device_id"))
    original_invoice = cstr(payload.get("original_invoice"))
    refund_type = cstr(payload.get("refund_type", "full"))
    refund_reason_code = normalize_token(payload.get("refund_reason_code")).replace(
        " ", "_"
    )
    refund_reason = cstr(payload.get("refund_reason"))
    refund_reason_notes = cstr(payload.get("refund_reason_notes"))

    if not refund_reason_code and refund_reason:
        normalized_reason = normalize_token(refund_reason).replace(" ", "_")
        if normalized_reason in REFUND_REASON_OPTIONS:
            refund_reason_code = normalized_reason

    if refund_reason_code and refund_reason_code not in REFUND_REASON_OPTIONS:
        frappe.throw(
            _("refund_reason_code must be one of: {0}").format(
                ", ".join(REFUND_REASON_OPTIONS.keys())
            ),
            frappe.ValidationError,
        )

    if not refund_reason and refund_reason_code:
        refund_reason = REFUND_REASON_OPTIONS[refund_reason_code]

    if not refund_reason_code and refund_reason:
        refund_reason_code = "other"

    if refund_reason_code == "other" and refund_reason_notes:
        refund_reason = refund_reason_notes

    if not idempotency_key:
        frappe.throw(_("idempotency_key is required"), frappe.ValidationError)
    if not device_id:
        frappe.throw(_("device_id is required"), frappe.ValidationError)
    if not original_invoice:
        frappe.throw(_("original_invoice is required"), frappe.ValidationError)
    if refund_type not in ("full", "partial"):
        frappe.throw(
            _("refund_type must be 'full' or 'partial'"), frappe.ValidationError
        )
    if not refund_reason:
        frappe.throw(_("refund_reason is required"), frappe.ValidationError)

    refund_amount = None
    items = None

    if refund_type == "full":
        invoice = frappe.get_doc("POS Invoice", original_invoice)
        refund_amount = invoice.grand_total
    else:
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            frappe.throw(
                _("items must be a non-empty array for partial refunds"),
                frappe.ValidationError,
            )
        refund_amount = 0
        for item in items:
            if not isinstance(item, dict):
                frappe.throw(
                    _("Each refund item must be an object"), frappe.ValidationError
                )

            item_code = cstr(item.get("item_code"))
            qty = flt(item.get("qty"))

            if not item_code:
                frappe.throw(_("items[].item_code is required"), frappe.ValidationError)
            if qty <= 0:
                frappe.throw(
                    _("items[].qty must be greater than 0"), frappe.ValidationError
                )
            if item.get("rate") is not None and flt(item.get("rate")) < 0:
                frappe.throw(
                    _("items[].rate must not be negative"), frappe.ValidationError
                )

    return {
        "idempotency_key": idempotency_key,
        "device_id": device_id,
        "original_invoice": original_invoice,
        "refund_type": refund_type,
        "refund_reason_code": refund_reason_code or "other",
        "refund_reason": refund_reason,
        "refund_reason_notes": refund_reason_notes,
        "refund_amount": refund_amount,
        "items": items,
        "return_to_stock": bool(payload.get("return_to_stock", False)),
        "payment_mode": cstr(payload.get("payment_mode")),
    }


def build_credit_note(
    validated: dict[str, Any],
    original_invoice: Any,
    pos_profile: Any,
) -> Any:
    """Build Credit Note from validated refund payload."""
    from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return

    pos_profile_name = pos_profile.name
    company = pos_profile.company

    credit_note = make_sales_return(original_invoice.name)
    original_timestamp = get_datetime(
        f"{original_invoice.posting_date} {original_invoice.posting_time}"
    )
    refund_timestamp = max(now_datetime(), add_to_date(original_timestamp, minutes=1))
    credit_note.customer = original_invoice.customer
    credit_note.company = company
    credit_note.pos_profile = pos_profile_name
    credit_note.set_posting_time = 1
    credit_note.posting_date = refund_timestamp.date().isoformat()
    credit_note.posting_time = refund_timestamp.time().strftime("%H:%M:%S")
    credit_note.custom_kopos_idempotency_key = validated["idempotency_key"]
    credit_note.custom_kopos_device_id = validated["device_id"]
    if hasattr(credit_note, "custom_kopos_refund_reason_code"):
        credit_note.custom_kopos_refund_reason_code = validated["refund_reason_code"]
    if hasattr(credit_note, "custom_kopos_refund_reason"):
        credit_note.custom_kopos_refund_reason = validated["refund_reason"]
    if hasattr(credit_note, "update_stock") and not validated["return_to_stock"]:
        credit_note.update_stock = 0
    for fieldname in (
        "custom_kopos_promotion_snapshot_version",
        "custom_kopos_pricing_mode",
        "custom_kopos_promotion_payload",
        "custom_kopos_promotion_reconciliation_status",
    ):
        if hasattr(credit_note, fieldname) and hasattr(original_invoice, fieldname):
            setattr(credit_note, fieldname, getattr(original_invoice, fieldname, None))

    if validated["refund_type"] == "full":
        for item in credit_note.items:
            original_item = next(
                (
                    source_item
                    for source_item in original_invoice.items
                    if source_item.item_code == item.item_code
                ),
                None,
            )
            if original_item:
                item.rate = get_original_refund_rate(original_item)
            if not validated["return_to_stock"]:
                item.warehouse = None
            if hasattr(item, "custom_kopos_promotion_allocation"):
                item.custom_kopos_promotion_allocation = (
                    build_refund_promotion_allocation(original_item, abs(flt(item.qty)))
                )
    else:
        requested = {item["item_code"]: item for item in validated["items"]}
        kept_items = []
        for credit_item in credit_note.items:
            refund_item = requested.get(credit_item.item_code)
            if not refund_item:
                continue

            original_item = next(
                (
                    item
                    for item in original_invoice.items
                    if item.item_code == credit_item.item_code
                ),
                None,
            )
            if not original_item:
                frappe.throw(
                    _("Item {0} not found in original invoice").format(
                        credit_item.item_code
                    ),
                    frappe.ValidationError,
                )

            qty = flt(refund_item["qty"])
            if qty > original_item.qty:
                frappe.throw(
                    _("Cannot refund {0} units of {1}; only {2} were purchased").format(
                        qty, credit_item.item_code, original_item.qty
                    ),
                    frappe.ValidationError,
                )

            credit_item.qty = -abs(qty)
            credit_item.rate = get_original_refund_rate(original_item)
            if not validated["return_to_stock"]:
                credit_item.warehouse = None
            if hasattr(credit_item, "custom_kopos_promotion_allocation"):
                credit_item.custom_kopos_promotion_allocation = (
                    build_refund_promotion_allocation(original_item, qty)
                )
            kept_items.append(credit_item)

        credit_note.set("items", kept_items)

    if hasattr(credit_note, "calculate_taxes_and_totals"):
        credit_note.calculate_taxes_and_totals()

    remarks = [
        f"KoPOS Refund Reason: {validated['refund_reason']}",
        f"KoPOS Refund Reason Code: {validated['refund_reason_code']}",
    ]
    if validated.get("refund_reason_notes"):
        remarks.append(f"KoPOS Refund Notes: {validated['refund_reason_notes']}")
    credit_note.remarks = "\n".join(remarks)

    return credit_note


def calculate_partial_refund_amount(
    original_invoice: Any, requested_items: list[dict[str, Any]]
) -> float:
    refund_amount = 0.0
    for refund_item in requested_items:
        item_code = cstr(refund_item.get("item_code"))
        qty = flt(refund_item.get("qty"))
        original_item = next(
            (
                item
                for item in original_invoice.items
                if cstr(getattr(item, "item_code", None)) == item_code
            ),
            None,
        )
        if not original_item:
            frappe.throw(
                _("Item {0} not found in original invoice").format(item_code),
                frappe.ValidationError,
            )
        refund_amount += qty * get_original_refund_rate(original_item)
    return refund_amount


def get_original_refund_rate(original_item: Any) -> float:
    original_qty = abs(flt(getattr(original_item, "qty", 0)))
    if original_qty <= 0:
        return 0
    original_amount = abs(flt(getattr(original_item, "amount", 0)))
    if original_amount > 0:
        return original_amount / original_qty
    return abs(flt(getattr(original_item, "rate", 0)))


def build_refund_promotion_allocation(
    original_item: Any, refund_qty: float
) -> str | None:
    if not original_item:
        return None

    original_payload = frappe.parse_json(
        getattr(original_item, "custom_kopos_promotion_allocation", None) or "{}"
    )
    if not isinstance(original_payload, dict) or not original_payload:
        return getattr(original_item, "custom_kopos_promotion_allocation", None)

    source_qty = abs(flt(getattr(original_item, "qty", 0)))
    if source_qty <= 0:
        return getattr(original_item, "custom_kopos_promotion_allocation", None)

    ratio = min(1.0, max(0.0, flt(refund_qty) / source_qty))
    refunded_allocations = []
    for allocation in original_payload.get("promotion_allocations") or []:
        if not isinstance(allocation, dict):
            continue
        refunded_allocations.append(
            {
                "promotion_id": cstr(allocation.get("promotion_id")),
                "amount": round(flt(allocation.get("amount")) * ratio, 6),
                "quantity": round(flt(allocation.get("quantity") or 0) * ratio, 6),
                "scope": cstr(allocation.get("scope")),
            }
        )

    refund_payload = {
        "original_sale": original_payload,
        "refund_context": {
            "allocation_mode": "proportional_from_original_line",
            "source_qty": source_qty,
            "refund_qty": flt(refund_qty),
            "refunded_base_amount": round(
                flt(original_payload.get("base_amount")) * ratio, 6
            ),
            "refunded_discount_amount": round(
                flt(original_payload.get("discount_amount")) * ratio, 6
            ),
            "refunded_promotion_allocations": refunded_allocations,
        },
    }
    return serialize_json_compact(refund_payload)


def resolve_refund_payment_mode(requested_mode: str, original_invoice: Any) -> str:
    """Resolve payment mode for refund."""
    if not requested_mode:
        # Use same payment mode as original invoice
        for payment in original_invoice.payments:
            if payment.amount:
                return payment.mode_of_payment
        return "Cash"

    # Try to match to known modes
    normalized = normalize_token(requested_mode)
    for mode, display_name in REFUND_PAYMENT_MODES.items():
        if normalized in {mode, normalize_token(display_name)}:
            return display_name

    return requested_mode if requested_mode else "Cash"
