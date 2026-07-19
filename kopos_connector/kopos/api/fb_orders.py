from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

frappe = importlib.import_module("frappe")
frappe_utils = importlib.import_module("frappe.utils")

from kopos_connector.kopos.api.money_contract import (
    LEGACY_DECIMAL_MONEY_CONTRACT_VERSION,
    MAX_SAFE_INTEGER,
    MoneyContractValidationError,
    parse_positive_integer_quantity,
    parse_wire_money_sen,
    persisted_money_to_sen,
    require_money_contract_version,
    sen_to_decimal,
)
from kopos_connector.kopos.services.accounting.maybank_payment_service import (
    normalize_qr_token,
)
from kopos_connector.kopos.services.orders.sale_datetime import (
    normalize_site_datetime,
    validate_submit_sale_datetime,
    validate_submit_sale_datetime_bounds,
)


def cint(value: Any) -> int:
    return int(frappe_utils.cint(value))


def cstr(value: Any) -> str:
    return str(frappe_utils.cstr(value))


def flt(value: Any) -> float:
    return float(frappe_utils.flt(value))


def now_datetime() -> Any:
    return frappe_utils.now_datetime()


ORDER_PROJECTION_CONFIG = (
    {
        "projection_type": "Sales Invoice",
        "target_field": "sales_invoice",
        "target_doctype": "Sales Invoice",
    },
    {
        "projection_type": "Stock Issue",
        "target_field": "ingredient_stock_entry",
        "target_doctype": "Stock Entry",
    },
)

ORDER_RETRY_PROJECTION_CONFIG = ORDER_PROJECTION_CONFIG + (
    {
        "projection_type": "FB Shift",
        "target_field": "shift",
        "target_doctype": "FB Shift",
    },
)

RETURN_PROJECTION_CONFIG = (
    {
        "projection_type": "Sales Return",
        "target_field": "return_sales_invoice",
        "target_doctype": "Sales Invoice",
    },
    {
        "projection_type": "Stock Reversal",
        "target_field": "return_sales_invoice",
        "target_doctype": "Sales Invoice",
        "enabled_field": "return_to_stock",
    },
)

REMAKE_PROJECTION_CONFIG = (
    {
        "projection_type": "Stock Issue",
        "target_field": "replacement_stock_entry",
        "target_doctype": "Stock Entry",
    },
)

ACCEPTABLE_STOCK_STATUSES = {"Pending", "Posted"}

PROJECTION_SUBSYSTEMS = {
    "Sales Invoice": "sales_invoice",
    "Stock Issue": "stock",
    "Stock Entry": "stock",
    "FB Shift": "shift",
}

PROMOTION_PRICING_MODES = {
    "legacy_client",
    "manual_only",
    "online_snapshot",
    "offline_snapshot",
    "server_validated",
}
PROMOTION_SOURCE_SNAPSHOT = "snapshot"
LOCALLY_SUPPORTED_PROMOTION_TYPES = {
    "buy_x_get_y",
    "happy_hour",
    "item_discount",
    "nth_item_discount",
    "order_discount",
}


def submit_order() -> dict[str, Any]:
    payload = _get_request_payload()
    return submit_order_payload(payload)


def submit_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_submit_order_payload(payload)
    existing_name = _get_existing_fb_order_name(normalized["external_idempotency_key"])
    if existing_name:
        order_doc = frappe.get_doc("FB Order", existing_name)
        if cstr(getattr(order_doc, "accepted_sale_fingerprint", None)).strip():
            _validate_existing_accepted_sale_fingerprint(normalized, order_doc)
            return _finalize_prepared_automatic_qr_order(normalized, order_doc)
        _validate_existing_order_fingerprint(normalized, order_doc)
        return _build_submit_response("duplicate", order_doc)

    validated = _validate_new_submit_order_state(normalized)
    try:
        _validate_submit_shift(
            shift_name=validated["shift"],
            device_id=validated["device_id"],
            staff_id=validated["staff_id"],
            lock=True,
        )
        order_doc = _build_fb_order(validated)
        order_doc.insert(ignore_permissions=True)
        order_doc.submit()
    except Exception:
        frappe.db.rollback()
        existing_name = _get_existing_fb_order_name(
            validated["external_idempotency_key"]
        )
        if existing_name:
            order_doc = frappe.get_doc("FB Order", existing_name)
            _validate_existing_order_fingerprint(normalized, order_doc)
            return _build_submit_response("duplicate", order_doc)
        raise

    return _build_submit_response("ok", order_doc)


def _finalize_prepared_automatic_qr_order(
    normalized: dict[str, Any],
    order_doc: Any,
) -> dict[str, Any]:
    """Submit the preaccepted order once payment truth is available."""

    frappe.db.sql(
        "SELECT name FROM `tabFB Order` WHERE name = %s LIMIT 1 FOR UPDATE",
        (order_doc.name,),
    )
    order_doc.reload()
    _validate_existing_accepted_sale_fingerprint(normalized, order_doc)
    order_docstatus = cint(getattr(order_doc, "docstatus", 0))
    if order_docstatus not in {0, 1}:
        frappe.throw(
            "Prepared Automatic QR sale is not eligible for finalization",
            frappe.ValidationError,
        )

    incoming_payments = list(normalized["payments"])
    stored_payments = list(order_doc.get("payments") or [])
    if len(incoming_payments) != 1 or len(stored_payments) != 1:
        frappe.throw(
            "Prepared Automatic QR sale must retain exactly one payment",
            frappe.ValidationError,
        )
    incoming = incoming_payments[0]
    stored = stored_payments[0]
    stored_payment_name = cstr(getattr(stored, "name", None)).strip()
    if stored_payment_name != cstr(
        getattr(order_doc, "automatic_qr_payment", None)
    ).strip():
        frappe.throw(
            "Prepared Automatic QR payment identity does not match",
            frappe.ValidationError,
        )
    if cstr(incoming.get("payment_id")).strip() != cstr(
        getattr(stored, "source_payment_id", None)
    ).strip():
        frappe.throw(
            "Prepared Automatic QR local payment identity does not match",
            frappe.ValidationError,
        )
    if _persisted_money_sen(
        getattr(stored, "amount", None),
        "Prepared Automatic QR payment amount",
    ) != incoming["amount_sen"]:
        frappe.throw(
            "Prepared Automatic QR payment amount does not match",
            frappe.ValidationError,
        )

    transaction_refno = cstr(incoming.get("external_transaction_id")).strip()
    if not transaction_refno or cstr(incoming.get("reference_no")).strip() != (
        transaction_refno
    ):
        frappe.throw(
            "Prepared Automatic QR finalization requires one exact provider reference",
            frappe.ValidationError,
        )
    transaction = frappe.db.get_value(
        "Maybank QR Transaction",
        {"transaction_refno": transaction_refno},
        [
            "name",
            "transaction_refno",
            "status",
            "maybank_status",
            "paid_at",
            "expires_at",
            "provider",
            "qr_data",
            "outlet_id",
            "device_id",
            "currency",
            "sale_amount_sen",
            "fb_order",
            "fb_order_payment",
        ],
        as_dict=True,
    )
    if not transaction:
        frappe.throw(
            "Prepared Automatic QR transaction was not found",
            frappe.ValidationError,
        )
    if (
        cstr(transaction.get("fb_order")).strip() != cstr(order_doc.name)
        or cstr(transaction.get("fb_order_payment")).strip()
        != stored_payment_name
    ):
        frappe.throw(
            "Prepared Automatic QR transaction binding does not match",
            frappe.ValidationError,
        )

    has_manual_evidence = bool(incoming.get("manual_confirmation_evidence_json"))

    if order_docstatus == 1:
        stored_transaction_name = cstr(
            getattr(stored, "maybank_qr_transaction", None)
        ).strip()
        stored_reference = cstr(
            getattr(stored, "external_transaction_id", None)
        ).strip()
        if (
            stored_transaction_name != cstr(transaction.get("name")).strip()
            or stored_reference != transaction_refno
        ):
            frappe.throw(
                "Prepared Automatic QR sale was finalized with a different provider transaction",
                frappe.ValidationError,
            )
        _validate_exact_prepared_qr_settlement_replay(incoming, stored)
        if not cint(getattr(stored, "is_manual_confirmation", 0)):
            provider_paid = _has_exact_authenticated_provider_paid_evidence(
                transaction,
                order_doc=order_doc,
                payment=stored,
                transaction_refno=transaction_refno,
            )
            if not provider_paid:
                frappe.throw(
                    "Prepared Automatic QR payment has no exact authenticated provider settlement",
                    frappe.ValidationError,
                )
        return _build_submit_response("duplicate", order_doc)

    provider_paid = _has_exact_authenticated_provider_paid_evidence(
        transaction,
        order_doc=order_doc,
        payment=stored,
        transaction_refno=transaction_refno,
    )
    if not provider_paid and not has_manual_evidence:
        frappe.throw(
            "Prepared Automatic QR payment is not yet confirmed",
            frappe.ValidationError,
        )

    resolved = _resolve_order_payment(incoming, 1)
    mutable_fields = (
        "reference_no",
        "external_transaction_id",
        "manual_confirmation_evidence_json",
        "reconciliation_idempotency_key",
    )
    for fieldname in mutable_fields:
        setattr(stored, fieldname, resolved.get(fieldname))
    if provider_paid:
        stored.is_manual_confirmation = 0
        stored.settlement_status = "verified"
    else:
        stored.is_manual_confirmation = 1
        stored.settlement_status = "pending_reconciliation"

    order_doc.automatic_qr_state = (
        "provider_paid" if provider_paid else "manual_pending_reconciliation"
    )
    order_doc.save(ignore_permissions=True)
    order_doc.submit()
    frappe.db.set_value(
        "FB Order",
        order_doc.name,
        "automatic_qr_state",
        "finalized",
        update_modified=False,
    )
    order_doc.automatic_qr_state = "finalized"
    return _build_submit_response("ok", order_doc)


def _validate_exact_prepared_qr_settlement_replay(
    incoming: Mapping[str, Any],
    stored: Any,
) -> None:
    expected = {
        "reference_no": cstr(incoming.get("reference_no")).strip(),
        "manual_confirmation_evidence_json": cstr(
            incoming.get("manual_confirmation_evidence_json")
        ).strip(),
        "reconciliation_idempotency_key": cstr(
            incoming.get("reconciliation_idempotency_key")
        ).strip(),
    }
    for fieldname, expected_value in expected.items():
        if cstr(getattr(stored, fieldname, None)).strip() != expected_value:
            frappe.throw(
                "Prepared Automatic QR sale was finalized with different settlement evidence",
                frappe.ValidationError,
            )


def _has_exact_authenticated_provider_paid_evidence(
    transaction: Mapping[str, Any],
    *,
    order_doc: Any,
    payment: Any,
    transaction_refno: str,
) -> bool:
    provider_status_paid = cstr(transaction.get("status")).strip().lower() == "paid"
    provider_flag_paid = cstr(transaction.get("maybank_status")).strip() == "1"
    if not provider_status_paid or not provider_flag_paid:
        return False

    exact_values = {
        "transaction_refno": transaction_refno,
        "provider": "maybank_qr",
        "device_id": cstr(getattr(order_doc, "device_id", None)).strip(),
        "currency": "MYR",
    }
    for fieldname, expected_value in exact_values.items():
        actual_value = cstr(transaction.get(fieldname)).strip()
        if fieldname in {"provider", "currency"}:
            actual_value = (
                actual_value.lower()
                if fieldname == "provider"
                else actual_value.upper()
            )
            expected_value = (
                expected_value.lower()
                if fieldname == "provider"
                else expected_value.upper()
            )
        if not expected_value or actual_value != expected_value:
            return False

    if cstr(getattr(order_doc, "currency", None)).strip().upper() != "MYR":
        return False
    for fieldname in ("paid_at", "expires_at", "qr_data", "outlet_id"):
        if not cstr(transaction.get(fieldname)).strip():
            return False

    raw_transaction_amount_sen = transaction.get("sale_amount_sen")
    if isinstance(raw_transaction_amount_sen, bool):
        return False
    try:
        transaction_amount_decimal = Decimal(str(raw_transaction_amount_sen))
    except (InvalidOperation, ValueError):
        return False
    if (
        not transaction_amount_decimal.is_finite()
        or transaction_amount_decimal != transaction_amount_decimal.to_integral_value()
    ):
        return False
    transaction_amount_sen = int(transaction_amount_decimal)
    payment_amount_sen = _persisted_money_sen(
        getattr(payment, "amount", None),
        "Prepared Automatic QR payment amount",
    )
    if transaction_amount_sen != payment_amount_sen:
        return False
    return True


def prepare_automatic_qr_sale_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Durably accept one immutable sale before any Maybank provider request."""

    normalized = _normalize_submit_order_payload(payload)
    _validate_automatic_qr_prepare_payment(normalized)
    existing_name = _get_existing_fb_order_name(normalized["external_idempotency_key"])
    if existing_name:
        order_doc = frappe.get_doc("FB Order", existing_name)
        _validate_existing_accepted_sale_fingerprint(normalized, order_doc)
        return _build_automatic_qr_prepare_response("duplicate", order_doc)

    validated = _validate_new_submit_order_state(normalized)
    try:
        _validate_submit_shift(
            shift_name=validated["shift"],
            device_id=validated["device_id"],
            staff_id=validated["staff_id"],
            lock=True,
        )
        order_doc = _build_fb_order(validated)
        # Resolve server-default recipe identity before the first insert. Once
        # accepted_sale_fingerprint is persisted, recipe fields are part of the
        # immutable prepared-sale snapshot and must never be enriched afterward.
        line_resolutions = order_doc.build_line_resolutions()
        order_doc.accepted_sale_fingerprint = validated[
            "accepted_sale_fingerprint"
        ]
        order_doc.automatic_qr_state = "prepared"
        order_doc.automatic_qr_accepted_at = now_datetime()
        order_doc.insert(ignore_permissions=True)

        order_doc.validate_stock_availability(line_resolutions)
        order_doc.create_resolved_sales(line_resolutions)
        payment_rows = list(order_doc.get("payments") or [])
        if len(payment_rows) != 1 or not cstr(
            getattr(payment_rows[0], "name", None)
        ).strip():
            frappe.throw(
                "Prepared Automatic QR sale has no durable payment row",
                frappe.ValidationError,
            )
        payment_rows[0].settlement_status = "awaiting_provider"
        order_doc.automatic_qr_payment = payment_rows[0].name
        order_doc.save(ignore_permissions=True)
    except Exception:
        frappe.db.rollback()
        existing_name = _get_existing_fb_order_name(
            validated["external_idempotency_key"]
        )
        if existing_name:
            order_doc = frappe.get_doc("FB Order", existing_name)
            _validate_existing_accepted_sale_fingerprint(normalized, order_doc)
            return _build_automatic_qr_prepare_response("duplicate", order_doc)
        raise

    return _build_automatic_qr_prepare_response("ok", order_doc)


def _validate_automatic_qr_prepare_payment(normalized: dict[str, Any]) -> None:
    payments = list(normalized["payments"])
    if len(payments) != 1:
        frappe.throw(
            "Automatic QR preparation requires exactly one payment",
            frappe.ValidationError,
        )
    payment = payments[0]
    if _normalize_token(payment.get("payment_channel_code")) not in {
        "maybank",
        "maybank qr",
    }:
        frappe.throw(
            "Automatic QR preparation requires the Maybank payment channel",
            frappe.ValidationError,
        )
    if not cstr(payment.get("payment_id")).strip():
        frappe.throw(
            "Automatic QR preparation requires payment_id",
            frappe.ValidationError,
        )
    if (
        cstr(payment.get("reference_no")).strip()
        or cstr(payment.get("external_transaction_id")).strip()
        or payment.get("manual_confirmation_evidence_json")
        or payment.get("is_manual_confirmation")
    ):
        frappe.throw(
            "Automatic QR preparation must occur before provider or manual settlement evidence exists",
            frappe.ValidationError,
        )


def _build_automatic_qr_prepare_response(
    result_status: str,
    order_doc: Any,
) -> dict[str, Any]:
    payment_row = cstr(
        getattr(order_doc, "automatic_qr_payment", None)
    ).strip()
    if not payment_row:
        payments = list(order_doc.get("payments") or [])
        payment_row = cstr(getattr(payments[0], "name", None)).strip() if payments else ""
    if not payment_row:
        frappe.throw(
            "Prepared Automatic QR sale payment row is missing",
            frappe.ValidationError,
        )
    return {
        "status": result_status,
        "fb_order": order_doc.name,
        "fb_order_payment": payment_row,
        "order_id": cstr(order_doc.order_id),
        "idempotency_key": cstr(order_doc.external_idempotency_key),
        "accepted_sale_fingerprint": cstr(
            getattr(order_doc, "accepted_sale_fingerprint", None)
        ),
        "automatic_qr_state": cstr(
            getattr(order_doc, "automatic_qr_state", None)
        ),
    }


def get_order_status(fb_order_name: str) -> dict[str, Any]:
    if not cstr(fb_order_name):
        frappe.throw("fb_order_name is required", frappe.ValidationError)

    order_doc = frappe.get_doc("FB Order", fb_order_name)
    projection_statuses = _get_projection_statuses("FB Order", order_doc.name)
    return {
        "status": "ok",
        "fb_order": order_doc.name,
        "order_id": cstr(order_doc.order_id),
        "sale_datetime": cstr(getattr(order_doc, "sale_datetime", None)) or None,
        "shift_id": cstr(order_doc.shift),
        "staff_id": cstr(order_doc.staff_id),
        "device_id": cstr(order_doc.device_id),
        "event_project": cstr(order_doc.event_project) or None,
        "order_status": cstr(order_doc.status),
        "sales_invoice": cstr(order_doc.sales_invoice) or None,
        "ingredient_stock_entry": cstr(order_doc.ingredient_stock_entry) or None,
        "invoice_status": cstr(order_doc.invoice_status),
        "stock_status": cstr(order_doc.stock_status),
        "projections": projection_statuses,
    }


def retry_failed_projections(fb_order_name: str) -> dict[str, Any]:
    if not cstr(fb_order_name):
        frappe.throw("fb_order_name is required", frappe.ValidationError)

    order_doc = frappe.get_doc("FB Order", fb_order_name)
    failed_logs = frappe.get_all(
        "FB Projection Log",
        filters={
            "source_doctype": "FB Order",
            "source_name": order_doc.name,
            "state": "Failed",
        },
        fields=["name"],
        order_by="creation asc",
    )

    retried = []
    for row in failed_logs:
        retried.append(_retry_projection_log(row.name))

    order_doc.reload()
    return {
        "status": "ok",
        "fb_order": order_doc.name,
        "order_id": cstr(order_doc.order_id),
        "sale_datetime": cstr(getattr(order_doc, "sale_datetime", None)) or None,
        "shift_id": cstr(order_doc.shift),
        "staff_id": cstr(order_doc.staff_id),
        "device_id": cstr(order_doc.device_id),
        "event_project": cstr(order_doc.event_project) or None,
        "order_status": cstr(order_doc.status),
        "sales_invoice": cstr(order_doc.sales_invoice) or None,
        "ingredient_stock_entry": cstr(order_doc.ingredient_stock_entry) or None,
        "invoice_status": cstr(order_doc.invoice_status),
        "stock_status": cstr(order_doc.stock_status),
        "retried": retried,
        "projections": _get_projection_statuses("FB Order", order_doc.name),
    }


def validate_fb_order(doc, method: str | None = None) -> None:
    _validate_fb_order_doc(doc)
    _set_default_order_statuses(doc)


def before_submit_fb_order(doc, method: str | None = None) -> None:
    _validate_fb_order_doc(doc)
    _validate_submit_shift(
        shift_name=cstr(doc.shift),
        device_id=cstr(doc.device_id),
        staff_id=cstr(doc.staff_id),
        lock=True,
    )
    doc.status = "Submitted"
    _set_default_order_statuses(doc)


def on_submit_fb_order(doc, method: str | None = None) -> None:
    doc.status = "Submitted"
    _process_projection_bundle(doc, "FB Order", ORDER_PROJECTION_CONFIG)


def validate_fb_return_event(doc, method: str | None = None) -> None:
    _validate_return_event_doc(doc)


def on_submit_fb_return_event(doc, method: str | None = None) -> None:
    doc.status = "Submitted"
    _process_projection_bundle(doc, "FB Return Event", RETURN_PROJECTION_CONFIG)


def validate_fb_remake_event(doc, method: str | None = None) -> None:
    _validate_remake_event_doc(doc)


def on_submit_fb_remake_event(doc, method: str | None = None) -> None:
    doc.status = "Submitted"
    _process_projection_bundle(doc, "FB Remake Event", REMAKE_PROJECTION_CONFIG)


def _get_request_payload() -> dict[str, Any]:
    if not getattr(frappe, "request", None):
        return _coerce_mapping(getattr(frappe.local, "form_dict", None))

    body = frappe.request.get_data(as_text=True) or ""
    if body:
        payload: Any = None
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            frappe.throw(f"invalid JSON payload: {exc.msg}", frappe.ValidationError)
        return _coerce_mapping(payload)

    return _coerce_mapping(getattr(frappe.local, "form_dict", None))


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    frappe.throw("request payload must be a JSON object", frappe.ValidationError)
    return {}


def _require_money_contract_version(payload: Mapping[str, Any]) -> str:
    try:
        return require_money_contract_version(payload)
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
    raise AssertionError("unreachable")


def _parse_wire_money_sen(
    row: Mapping[str, Any],
    *,
    version: str,
    sen_field: str,
    legacy_fields: tuple[str, ...],
    required: bool = True,
    default: int = 0,
) -> int:
    try:
        return parse_wire_money_sen(
            row,
            version=version,
            sen_field=sen_field,
            legacy_fields=legacy_fields,
            required=required,
            default=default,
        )
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
    raise AssertionError("unreachable")


def _parse_positive_integer_quantity(value: Any, fieldname: str) -> int:
    try:
        return parse_positive_integer_quantity(value, fieldname)
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
    raise AssertionError("unreachable")


def _require_safe_sen(value: int, fieldname: str) -> int:
    if abs(value) > MAX_SAFE_INTEGER:
        frappe.throw(f"{fieldname} exceeds the safe integer range", frappe.ValidationError)
    return value


def _checked_sen_sum(values: Any, fieldname: str) -> int:
    return _require_safe_sen(sum(values), fieldname)


def _normalize_optional_identifier(
    value: Any,
    fieldname: str,
    *,
    present: bool,
) -> str | None:
    if not present:
        return None
    if not isinstance(value, str):
        frappe.throw(
            f"{fieldname} must be a non-empty trimmed string",
            frappe.ValidationError,
        )
    normalized = value.strip()
    if not normalized or normalized != value:
        frappe.throw(
            f"{fieldname} must be a non-empty trimmed string",
            frappe.ValidationError,
        )
    if len(normalized) > 140:
        frappe.throw(f"{fieldname} is too long", frappe.ValidationError)
    return normalized


def _normalize_optional_tax_rate(
    value: Any,
    *,
    present: bool,
) -> Decimal | None:
    if not present:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        frappe.throw(
            "order.tax_rate must be a finite decimal rate between 0 and 1",
            frappe.ValidationError,
        )
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError):
        frappe.throw(
            "order.tax_rate must be a finite decimal rate between 0 and 1",
            frappe.ValidationError,
        )
    if not rate.is_finite() or rate < 0 or rate > 1:
        frappe.throw(
            "order.tax_rate must be a finite decimal rate between 0 and 1",
            frappe.ValidationError,
        )
    canonical = rate.normalize()
    if canonical == 0:
        canonical = Decimal("0")
    if canonical.as_tuple().exponent < -6:
        frappe.throw(
            "order.tax_rate supports at most 6 decimal places",
            frappe.ValidationError,
        )
    return canonical


def _normalize_optional_promotion_text(
    value: Any,
    fieldname: str,
    *,
    required: bool = False,
    maximum_length: int = 140,
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        frappe.throw(f"{fieldname} must be a trimmed string", frappe.ValidationError)
    normalized = value.strip()
    if required and not normalized:
        frappe.throw(f"{fieldname} is required", frappe.ValidationError)
    if normalized != value or len(normalized) > maximum_length:
        frappe.throw(
            f"{fieldname} must be a trimmed string no longer than {maximum_length} characters",
            frappe.ValidationError,
        )
    return normalized or None


def _normalize_pricing_context(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        frappe.throw("pricing_context must be an object", frappe.ValidationError)

    pricing_mode = _normalize_optional_promotion_text(
        value.get("pricing_mode"),
        "pricing_context.pricing_mode",
    )
    if pricing_mode and pricing_mode not in PROMOTION_PRICING_MODES:
        frappe.throw(
            "pricing_context.pricing_mode is invalid",
            frappe.ValidationError,
        )
    snapshot_version = _normalize_optional_promotion_text(
        value.get("snapshot_version"),
        "pricing_context.snapshot_version",
    )
    snapshot_hash = _normalize_optional_promotion_text(
        value.get("snapshot_hash"),
        "pricing_context.snapshot_hash",
        maximum_length=64,
    )
    if snapshot_hash and (
        len(snapshot_hash) != 64
        or any(character not in "0123456789abcdef" for character in snapshot_hash)
    ):
        frappe.throw(
            "pricing_context.snapshot_hash must be a lowercase SHA-256 digest",
            frappe.ValidationError,
        )
    if bool(snapshot_version) != bool(snapshot_hash):
        frappe.throw(
            "pricing_context snapshot_version and snapshot_hash must be provided together",
            frappe.ValidationError,
        )
    if pricing_mode in {"online_snapshot", "offline_snapshot"} and not snapshot_version:
        frappe.throw(
            "snapshot pricing modes require pricing_context snapshot version and hash",
            frappe.ValidationError,
        )

    restricted_mode = value.get("restricted_mode", False)
    if not isinstance(restricted_mode, bool):
        frappe.throw(
            "pricing_context.restricted_mode must be a boolean",
            frappe.ValidationError,
        )
    if restricted_mode and pricing_mode != "manual_only":
        frappe.throw(
            "restricted promotion pricing must use manual_only pricing_mode",
            frappe.ValidationError,
        )
    raw_offline_ids = value.get("offline_applied_promotion_ids", [])
    if not isinstance(raw_offline_ids, list):
        frappe.throw(
            "pricing_context.offline_applied_promotion_ids must be an array",
            frappe.ValidationError,
        )
    offline_ids = [
        _normalize_optional_promotion_text(
            promotion_id,
            f"pricing_context.offline_applied_promotion_ids[{index}]",
            required=True,
        )
        for index, promotion_id in enumerate(raw_offline_ids)
    ]
    if len(offline_ids) != len(set(offline_ids)):
        frappe.throw(
            "pricing_context.offline_applied_promotion_ids must be unique",
            frappe.ValidationError,
        )

    raw_expiry = value.get("promotion_expiry_by_id", {})
    if not isinstance(raw_expiry, Mapping):
        frappe.throw(
            "pricing_context.promotion_expiry_by_id must be an object",
            frappe.ValidationError,
        )
    promotion_expiry_by_id: dict[str, str | None] = {}
    for promotion_id, expiry in raw_expiry.items():
        normalized_id = _normalize_optional_promotion_text(
            promotion_id,
            "pricing_context.promotion_expiry_by_id key",
            required=True,
        )
        assert normalized_id is not None
        promotion_expiry_by_id[normalized_id] = _normalize_optional_promotion_text(
            expiry,
            f"pricing_context.promotion_expiry_by_id.{normalized_id}",
            maximum_length=64,
        )

    return {
        "snapshot_version": snapshot_version,
        "snapshot_hash": snapshot_hash,
        "snapshot_downloaded_at": _normalize_optional_promotion_text(
            value.get("snapshot_downloaded_at"),
            "pricing_context.snapshot_downloaded_at",
            maximum_length=64,
        ),
        "snapshot_published_at": _normalize_optional_promotion_text(
            value.get("snapshot_published_at"),
            "pricing_context.snapshot_published_at",
            maximum_length=64,
        ),
        "snapshot_effective_from": _normalize_optional_promotion_text(
            value.get("snapshot_effective_from"),
            "pricing_context.snapshot_effective_from",
            maximum_length=64,
        ),
        "pricing_mode": pricing_mode,
        "restricted_mode": restricted_mode,
        "priced_at": _normalize_optional_promotion_text(
            value.get("priced_at"),
            "pricing_context.priced_at",
            maximum_length=64,
        ),
        "offline_applied_promotion_ids": offline_ids,
        "promotion_expiry_by_id": promotion_expiry_by_id,
    }


def _normalize_applied_promotions(
    value: Any,
    money_contract_version: str,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        frappe.throw("applied_promotions must be an array", frappe.ValidationError)

    promotions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_promotion in enumerate(value):
        field_prefix = f"applied_promotions[{index}]"
        if not isinstance(raw_promotion, Mapping):
            frappe.throw(f"{field_prefix} must be an object", frappe.ValidationError)
        promotion_id = _normalize_optional_promotion_text(
            raw_promotion.get("promotion_id"),
            f"{field_prefix}.promotion_id",
            required=True,
        )
        assert promotion_id is not None
        if promotion_id in seen_ids:
            frappe.throw(
                "applied_promotions promotion_id values must be unique",
                frappe.ValidationError,
            )
        seen_ids.add(promotion_id)
        amount_sen = _parse_wire_money_sen(
            raw_promotion,
            version=money_contract_version,
            sen_field="amount_sen",
            legacy_fields=("amount",),
        )
        if amount_sen <= 0:
            frappe.throw(
                f"{field_prefix}.amount_sen must be greater than 0",
                frappe.ValidationError,
            )
        offline_applied = raw_promotion.get("offline_applied", False)
        if not isinstance(offline_applied, bool):
            frappe.throw(
                f"{field_prefix}.offline_applied must be a boolean",
                frappe.ValidationError,
            )
        source = _normalize_optional_promotion_text(
            raw_promotion.get("source"),
            f"{field_prefix}.source",
            required=True,
        )
        if source != PROMOTION_SOURCE_SNAPSHOT:
            frappe.throw(
                f"{field_prefix}.source must be {PROMOTION_SOURCE_SNAPSHOT}",
                frappe.ValidationError,
            )
        promotions.append(
            {
                "promotion_id": promotion_id,
                "promotion_name": _normalize_optional_promotion_text(
                    raw_promotion.get("promotion_name"),
                    f"{field_prefix}.promotion_name",
                ),
                "promotion_type": _normalize_optional_promotion_text(
                    raw_promotion.get("promotion_type"),
                    f"{field_prefix}.promotion_type",
                ),
                "amount_sen": amount_sen,
                "scope": _normalize_optional_promotion_text(
                    raw_promotion.get("scope"),
                    f"{field_prefix}.scope",
                ),
                "source": source,
                "snapshot_version": _normalize_optional_promotion_text(
                    raw_promotion.get("snapshot_version"),
                    f"{field_prefix}.snapshot_version",
                    required=True,
                ),
                "snapshot_hash": _normalize_optional_promotion_text(
                    raw_promotion.get("snapshot_hash"),
                    f"{field_prefix}.snapshot_hash",
                    required=True,
                    maximum_length=64,
                ),
                "valid_from": _normalize_optional_promotion_text(
                    raw_promotion.get("valid_from"),
                    f"{field_prefix}.valid_from",
                    maximum_length=64,
                ),
                "valid_upto": _normalize_optional_promotion_text(
                    raw_promotion.get("valid_upto"),
                    f"{field_prefix}.valid_upto",
                    maximum_length=64,
                ),
                "offline_applied": offline_applied,
            }
        )
    return promotions


def _normalize_promotion_allocations(
    value: Any,
    *,
    item_index: int,
    money_contract_version: str,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        frappe.throw(
            f"items[{item_index}].promotion_allocations must be an array",
            frappe.ValidationError,
        )
    allocations: list[dict[str, Any]] = []
    for allocation_index, raw_allocation in enumerate(value):
        field_prefix = (
            f"items[{item_index}].promotion_allocations[{allocation_index}]"
        )
        if not isinstance(raw_allocation, Mapping):
            frappe.throw(f"{field_prefix} must be an object", frappe.ValidationError)
        promotion_id = _normalize_optional_promotion_text(
            raw_allocation.get("promotion_id"),
            f"{field_prefix}.promotion_id",
            required=True,
        )
        assert promotion_id is not None
        amount_sen = _parse_wire_money_sen(
            raw_allocation,
            version=money_contract_version,
            sen_field="amount_sen",
            legacy_fields=("amount",),
        )
        if amount_sen <= 0:
            frappe.throw(
                f"{field_prefix}.amount_sen must be greater than 0",
                frappe.ValidationError,
            )
        quantity_value = raw_allocation.get("quantity")
        quantity = (
            _parse_positive_integer_quantity(
                quantity_value,
                f"{field_prefix}.quantity",
            )
            if quantity_value is not None
            else None
        )
        allocations.append(
            {
                "promotion_id": promotion_id,
                "amount_sen": amount_sen,
                "quantity": quantity,
                "scope": _normalize_optional_promotion_text(
                    raw_allocation.get("scope"),
                    f"{field_prefix}.scope",
                ),
            }
        )
    return allocations


def _validate_normalized_promotion_evidence(
    pricing_context: dict[str, Any],
    applied_promotions: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> str:
    allocations = [
        allocation
        for item in items
        for allocation in item["promotion_allocations"]
    ]
    if not applied_promotions:
        if allocations:
            frappe.throw(
                "promotion allocations require applied_promotions",
                frappe.ValidationError,
            )
        if pricing_context.get("offline_applied_promotion_ids"):
            frappe.throw(
                "offline promotion ids require applied_promotions",
                frappe.ValidationError,
            )
        if pricing_context.get("promotion_expiry_by_id"):
            frappe.throw(
                "promotion expiry evidence requires applied_promotions",
                frappe.ValidationError,
            )
        return "not_applicable"

    snapshot_version = pricing_context.get("snapshot_version")
    snapshot_hash = pricing_context.get("snapshot_hash")
    pricing_mode = pricing_context.get("pricing_mode")
    if not snapshot_version or not snapshot_hash:
        frappe.throw(
            "applied_promotions require promotion snapshot version and hash",
            frappe.ValidationError,
        )
    if pricing_mode not in {"online_snapshot", "offline_snapshot", "server_validated"}:
        frappe.throw(
            "applied_promotions require a snapshot pricing mode",
            frappe.ValidationError,
        )
    if pricing_context.get("restricted_mode"):
        frappe.throw(
            "applied_promotions are forbidden in restricted pricing mode",
            frappe.ValidationError,
        )

    applied_totals = {
        promotion["promotion_id"]: promotion["amount_sen"]
        for promotion in applied_promotions
    }
    allocation_totals: dict[str, int] = {}
    for item in items:
        line_allocation_total = _checked_sen_sum(
            (
                allocation["amount_sen"]
                for allocation in item["promotion_allocations"]
            ),
            "promotion allocation total",
        )
        if line_allocation_total > item["discount_amount_sen"]:
            frappe.throw(
                "line promotion allocations cannot exceed the line discount",
                frappe.ValidationError,
            )
        for allocation in item["promotion_allocations"]:
            promotion_id = allocation["promotion_id"]
            if promotion_id not in applied_totals:
                frappe.throw(
                    "line promotion allocation references an unapplied promotion",
                    frappe.ValidationError,
                )
            allocation_totals[promotion_id] = _checked_sen_sum(
                (
                    allocation_totals.get(promotion_id, 0),
                    allocation["amount_sen"],
                ),
                "promotion allocation total",
            )
    if applied_totals != allocation_totals:
        frappe.throw(
            "applied promotion totals must exactly match line allocations",
            frappe.ValidationError,
        )

    offline_ids = {
        promotion["promotion_id"]
        for promotion in applied_promotions
        if promotion["offline_applied"]
    }
    if offline_ids != set(pricing_context["offline_applied_promotion_ids"]):
        frappe.throw(
            "offline promotion ids do not match applied promotion evidence",
            frappe.ValidationError,
        )
    if pricing_mode == "online_snapshot" and offline_ids:
        frappe.throw(
            "online snapshot pricing cannot mark promotions as offline applied",
            frappe.ValidationError,
        )
    for promotion in applied_promotions:
        if (
            promotion["snapshot_version"] != snapshot_version
            or promotion["snapshot_hash"] != snapshot_hash
        ):
            frappe.throw(
                "applied promotion snapshot identity does not match pricing_context",
                frappe.ValidationError,
            )
    return "matched"


def _validate_offline_pricing_consistency(
    offline_priced: bool,
    pricing_context: dict[str, Any],
) -> None:
    pricing_mode = pricing_context.get("pricing_mode")
    if pricing_mode == "offline_snapshot" and not offline_priced:
        frappe.throw(
            "offline_priced must be true for offline_snapshot pricing",
            frappe.ValidationError,
        )
    if pricing_mode in {"online_snapshot", "server_validated"} and offline_priced:
        frappe.throw(
            "offline_priced must be false for online or server-validated pricing",
            frappe.ValidationError,
        )


def _persisted_money_sen(value: Any, fieldname: str) -> int:
    try:
        return persisted_money_to_sen(value, fieldname)
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
    raise AssertionError("unreachable")


def _validate_submit_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a new order, including mutable ERP references.

    The public submit path intentionally normalizes and fingerprints the request
    before calling this stateful phase so an accepted request can be reconciled
    after a timeout even when its shift has subsequently closed.
    """

    return _validate_new_submit_order_state(_normalize_submit_order_payload(payload))


def _normalize_submit_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    order_value = payload.get("order")
    if not isinstance(order_value, Mapping):
        frappe.throw("order must be a JSON object", frappe.ValidationError)
    order_payload = dict(order_value)
    money_contract_version = _require_money_contract_version(payload)
    order_id = cstr(payload.get("order_id") or order_payload.get("id"))
    idempotency_key = cstr(payload.get("idempotency_key"))
    source = cstr(payload.get("source") or "API")
    device_id = cstr(payload.get("device_id"))
    shift = cstr(payload.get("shift") or payload.get("shift_id"))
    staff_id = cstr(payload.get("staff_id"))
    booth_warehouse = cstr(payload.get("booth_warehouse") or payload.get("warehouse"))
    company = cstr(payload.get("company"))
    currency = cstr(payload.get("currency"))
    customer = cstr(payload.get("customer")) or None
    event_project = cstr(payload.get("event_project")) or None
    notes = cstr(payload.get("notes") or order_payload.get("notes")) or None
    catalog_version = _normalize_optional_identifier(
        payload.get("catalog_version"),
        "catalog_version",
        present="catalog_version" in payload,
    )
    display_number = cstr(order_payload.get("display_number"))
    order_type = cstr(order_payload.get("order_type"))
    tax_rate = _normalize_optional_tax_rate(
        order_payload.get("tax_rate"),
        present="tax_rate" in order_payload,
    )
    sale_datetime_value = order_payload.get("created_at")
    items = order_payload.get("items")
    payments = order_payload.get("payments")

    if not order_id:
        frappe.throw("order_id is required", frappe.ValidationError)
    if not idempotency_key:
        frappe.throw("idempotency_key is required", frappe.ValidationError)
    if not device_id:
        frappe.throw("device_id is required", frappe.ValidationError)
    if not shift:
        frappe.throw("shift is required", frappe.ValidationError)
    if not staff_id:
        frappe.throw("staff_id is required", frappe.ValidationError)
    if not booth_warehouse:
        frappe.throw("booth_warehouse is required", frappe.ValidationError)
    if not company:
        frappe.throw("company is required", frappe.ValidationError)
    if not currency:
        frappe.throw("currency is required", frappe.ValidationError)
    if not display_number:
        frappe.throw("order.display_number is required", frappe.ValidationError)
    if not order_type:
        frappe.throw("order.order_type is required", frappe.ValidationError)
    if not isinstance(items, list) or not items:
        frappe.throw("order.items must contain at least one row", frappe.ValidationError)
    if not isinstance(payments, list) or not payments:
        frappe.throw(
            "order.payments must contain at least one row", frappe.ValidationError
        )

    sale_datetime = validate_submit_sale_datetime(sale_datetime_value)

    item_rows = items if isinstance(items, list) else []
    payment_rows = payments if isinstance(payments, list) else []

    validated_items = [
        _normalize_order_item(row, index, money_contract_version)
        for index, row in enumerate(item_rows, start=1)
    ]
    validated_payments = [
        _normalize_order_payment(row, index, money_contract_version)
        for index, row in enumerate(payment_rows, start=1)
    ]
    payment_ids = [
        row["payment_id"] for row in validated_payments if row["payment_id"]
    ]
    if len(payment_ids) != len(set(payment_ids)):
        frappe.throw(
            "order.payments payment_id values must be unique within the order",
            frappe.ValidationError,
        )

    pricing_context = _normalize_pricing_context(payload.get("pricing_context"))
    applied_promotions = _normalize_applied_promotions(
        payload.get("applied_promotions"),
        money_contract_version,
    )
    promotion_reconciliation_status = _validate_normalized_promotion_evidence(
        pricing_context,
        applied_promotions,
        validated_items,
    )
    offline_priced = payload.get("offline_priced", False)
    if not isinstance(offline_priced, bool):
        frappe.throw("offline_priced must be a boolean", frappe.ValidationError)
    _validate_offline_pricing_consistency(offline_priced, pricing_context)

    totals_payload = dict(order_payload)
    for fieldname in (
        "subtotal",
        "net_total",
        "tax_amount",
        "tax_total",
        "rounding_adjustment",
        "rounding_adj",
        "total",
        "grand_total",
    ):
        if fieldname in payload:
            totals_payload[fieldname] = payload[fieldname]

    net_total_sen = _parse_wire_money_sen(
        totals_payload,
        version=money_contract_version,
        sen_field="subtotal_sen",
        legacy_fields=("subtotal", "net_total"),
    )
    tax_total_sen = _parse_wire_money_sen(
        totals_payload,
        version=money_contract_version,
        sen_field="tax_amount_sen",
        legacy_fields=("tax_amount", "tax_total"),
    )
    rounding_adjustment_sen = _parse_wire_money_sen(
        totals_payload,
        version=money_contract_version,
        sen_field="rounding_adjustment_sen",
        legacy_fields=("rounding_adjustment", "rounding_adj"),
        required=False,
        default=0,
    )
    grand_total_sen = _parse_wire_money_sen(
        totals_payload,
        version=money_contract_version,
        sen_field="total_sen",
        legacy_fields=("total", "grand_total"),
    )

    summed_line_total_sen = _checked_sen_sum(
        (row["line_total_sen"] for row in validated_items),
        "order item total",
    )
    if net_total_sen < 0:
        frappe.throw("order.subtotal_sen must be 0 or greater", frappe.ValidationError)
    if tax_total_sen < 0:
        frappe.throw("order.tax_amount_sen must be 0 or greater", frappe.ValidationError)
    if tax_rate is not None:
        expected_tax_total_sen = int(
            (Decimal(net_total_sen) * tax_rate).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
        if expected_tax_total_sen != tax_total_sen:
            frappe.throw(
                "order.tax_amount_sen must equal subtotal_sen multiplied by order.tax_rate, rounded to sen",
                frappe.ValidationError,
            )
    if grand_total_sen <= 0:
        frappe.throw("order.total_sen must be greater than 0", frappe.ValidationError)
    if summed_line_total_sen != net_total_sen:
        frappe.throw(
            "order.subtotal_sen must equal summed line totals",
            frappe.ValidationError,
        )
    expected_total_sen = _checked_sen_sum(
        (net_total_sen, tax_total_sen, rounding_adjustment_sen),
        "order calculated total",
    )
    if expected_total_sen != grand_total_sen:
        frappe.throw(
            "order.total_sen must equal subtotal_sen plus tax_amount_sen plus rounding_adjustment_sen",
            frappe.ValidationError,
        )

    paid_total_sen = _checked_sen_sum(
        (row["amount_sen"] for row in validated_payments),
        "order payment total",
    )
    if paid_total_sen != grand_total_sen:
        frappe.throw("payments total must equal order.total_sen", frappe.ValidationError)

    normalized = {
        "money_contract_version": money_contract_version,
        "order_id": order_id,
        "external_idempotency_key": idempotency_key,
        "source": source,
        "device_id": device_id,
        "shift": shift,
        "staff_id": staff_id,
        "booth_warehouse": booth_warehouse,
        "company": company,
        "currency": currency,
        "customer": customer,
        "event_project": event_project,
        "notes": notes,
        "catalog_version": catalog_version,
        "display_number": display_number,
        "order_type": order_type,
        "sale_datetime": sale_datetime,
        "net_total_sen": net_total_sen,
        "tax_total_sen": tax_total_sen,
        "tax_rate": tax_rate,
        "rounding_adjustment_sen": rounding_adjustment_sen,
        "grand_total_sen": grand_total_sen,
        "items": validated_items,
        "payments": validated_payments,
        "offline_priced": offline_priced,
        "pricing_context": pricing_context,
        "applied_promotions": applied_promotions,
        "promotion_reconciliation_status": promotion_reconciliation_status,
    }
    normalized["accepted_sale_fingerprint"] = (
        _canonical_accepted_sale_fingerprint(normalized)
    )
    normalized["request_fingerprint"] = _canonical_order_request_fingerprint(normalized)
    return normalized


def _validate_new_submit_order_state(normalized: dict[str, Any]) -> dict[str, Any]:
    shift_name = _resolve_fb_shift_name(normalized["shift"])
    if not shift_name:
        frappe.throw(f"shift {normalized['shift']} was not found", frappe.ValidationError)
    assert shift_name is not None
    shift_doc = _validate_submit_shift(
        shift_name=shift_name,
        device_id=normalized["device_id"],
        staff_id=normalized["staff_id"],
    )
    validate_submit_sale_datetime_bounds(
        normalized["sale_datetime"],
        shift_name=shift_name,
        shift_opened_at=getattr(shift_doc, "opened_at", None),
    )
    _require_doc("Warehouse", normalized["booth_warehouse"], "booth_warehouse")
    _require_doc("Company", normalized["company"], "company")
    _require_doc("User", normalized["staff_id"], "staff_id")
    if normalized["customer"]:
        _require_doc("Customer", normalized["customer"], "customer")
    if normalized["event_project"]:
        _require_doc("Project", normalized["event_project"], "event_project")

    validated = dict(normalized)
    validated["promotion_evidence"] = _validate_published_promotion_snapshot(
        normalized
    )
    validated["shift"] = shift_name
    validated["net_total"] = sen_to_decimal(normalized["net_total_sen"])
    validated["tax_total"] = sen_to_decimal(normalized["tax_total_sen"])
    validated["rounding_adjustment"] = sen_to_decimal(
        normalized["rounding_adjustment_sen"]
    )
    validated["grand_total"] = sen_to_decimal(normalized["grand_total_sen"])
    validated["items"] = [
        _resolve_order_item(row, index)
        for index, row in enumerate(normalized["items"], start=1)
    ]
    validated["payments"] = [
        _resolve_order_payment(row, index)
        for index, row in enumerate(normalized["payments"], start=1)
    ]
    return validated


def _snapshot_rule_decimal(value: Any, fieldname: str) -> Decimal:
    if isinstance(value, bool):
        frappe.throw(f"{fieldname} must be a finite decimal", frappe.ValidationError)
    try:
        result = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError) as error:
        frappe.throw(f"{fieldname} must be a finite decimal", frappe.ValidationError)
        raise ValueError(f"{fieldname} must be a finite decimal") from error
    if not result.is_finite():
        frappe.throw(f"{fieldname} must be a finite decimal", frappe.ValidationError)
    return result


def _snapshot_rule_integer(value: Any, fieldname: str) -> int:
    decimal_value = _snapshot_rule_decimal(value, fieldname)
    if decimal_value != decimal_value.to_integral_value():
        frappe.throw(f"{fieldname} must be an integer", frappe.ValidationError)
    result = int(decimal_value)
    if result < 0:
        frappe.throw(f"{fieldname} must not be negative", frappe.ValidationError)
    return result


def _snapshot_rule_money_sen(value: Any, fieldname: str) -> int:
    decimal_value = _snapshot_rule_decimal(value, fieldname)
    if decimal_value < 0:
        frappe.throw(f"{fieldname} must not be negative", frappe.ValidationError)
    return int(
        (decimal_value * Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _snapshot_unit_discount_sen(
    rule: dict[str, Any],
    unit_price_sen: int,
) -> int:
    discount_type = cstr(rule.get("discount_type") or "percentage")
    discount_value = _snapshot_rule_decimal(
        rule.get("discount_value"),
        f"promotion {cstr(rule.get('promotion_id'))} discount_value",
    )
    if discount_value < 0:
        frappe.throw("Promotion discount_value must not be negative", frappe.ValidationError)
    if discount_type == "fixed_amount":
        amount_sen = int(
            (discount_value * Decimal("100")).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        return max(0, min(unit_price_sen, amount_sen))
    if discount_type == "fixed_price":
        target_sen = int(
            (discount_value * Decimal("100")).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        return max(0, unit_price_sen - target_sen)
    if discount_type == "free_item":
        return unit_price_sen
    if discount_type != "percentage":
        frappe.throw(
            f"Promotion discount_type {discount_type or '<blank>'} is unsupported",
            frappe.ValidationError,
        )
    percentage_sen = int(
        (Decimal(unit_price_sen) * discount_value / Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    return max(0, min(unit_price_sen, percentage_sen))


def _snapshot_rule_timestamp(value: Any, fieldname: str) -> Any | None:
    if value in (None, ""):
        return None
    try:
        return normalize_site_datetime(value, fieldname=fieldname)
    except (TypeError, ValueError, OverflowError) as error:
        frappe.throw(f"{fieldname} is not a valid timestamp", frappe.ValidationError)
        raise ValueError(f"{fieldname} is not a valid timestamp") from error


def _snapshot_rule_is_available(
    rule: dict[str, Any],
    *,
    sale_datetime: Any,
    pos_profile: str,
) -> bool:
    promotion_type = cstr(rule.get("promotion_type"))
    if not bool(rule.get("offline_allowed")):
        return False
    if promotion_type not in LOCALLY_SUPPORTED_PROMOTION_TYPES:
        return False
    rule_profile = cstr(rule.get("pos_profile"))
    if rule_profile and rule_profile != pos_profile:
        return False
    selected_profiles = {
        cstr(value)
        for value in (rule.get("selected_pos_profiles") or [])
        if cstr(value)
    }
    if selected_profiles and pos_profile not in selected_profiles:
        return False
    valid_from = _snapshot_rule_timestamp(
        rule.get("valid_from"),
        f"promotion {cstr(rule.get('promotion_id'))} valid_from",
    )
    valid_upto = _snapshot_rule_timestamp(
        rule.get("valid_upto"),
        f"promotion {cstr(rule.get('promotion_id'))} valid_upto",
    )
    if valid_from is not None and sale_datetime < valid_from:
        return False
    if valid_upto is not None and sale_datetime > valid_upto:
        return False
    return True


def _snapshot_line_is_eligible(
    rule: dict[str, Any],
    line: dict[str, Any],
) -> bool:
    eligible_items = {
        cstr(value) for value in (rule.get("eligible_items") or []) if cstr(value)
    }
    if eligible_items and line["item_code"] not in eligible_items:
        return False
    eligible_groups = {
        cstr(value)
        for value in (rule.get("eligible_item_groups") or [])
        if cstr(value)
    }
    if not eligible_groups:
        return True
    if line.get("item_group") is None:
        line["item_group"] = cstr(
            frappe.db.get_value("Item", line["item_code"], "item_group")
        )
    return line["item_group"] in eligible_groups


def _snapshot_rule_minimums_met(
    rule: dict[str, Any],
    units: list[dict[str, Any]],
) -> bool:
    min_qty = _snapshot_rule_integer(
        rule.get("min_qty") or 0,
        f"promotion {cstr(rule.get('promotion_id'))} min_qty",
    )
    min_amount_sen = _snapshot_rule_money_sen(
        rule.get("min_amount") or 0,
        f"promotion {cstr(rule.get('promotion_id'))} min_amount",
    )
    if min_qty > 0 and len(units) < min_qty:
        return False
    if min_amount_sen > 0 and sum(unit["base_price_sen"] for unit in units) < min_amount_sen:
        return False
    return True


def _snapshot_eligible_units(
    rule: dict[str, Any],
    lines: list[dict[str, Any]],
    *,
    respect_stacking: bool = True,
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for line in lines:
        if not _snapshot_line_is_eligible(rule, line):
            continue
        if (
            respect_stacking
            and cstr(rule.get("stacking_policy") or "exclusive") != "stackable"
            and line["promotion_discount_sen"] > 0
        ):
            continue
        for unit_index in range(line["quantity"]):
            units.append(
                {
                    "line_index": line["index"],
                    "item_code": line["item_code"],
                    "base_price_sen": line["unit_price_sen"],
                    "cart_order": line["index"],
                    "unit_order": unit_index,
                }
            )
    return units


def _append_expected_promotion_allocation(
    line: dict[str, Any],
    promotion_id: str,
    amount_sen: int,
    quantity: int,
) -> None:
    line["expected_allocations"].append(
        {
            "promotion_id": promotion_id,
            "amount_sen": amount_sen,
            "quantity": quantity,
            "scope": "line",
        }
    )
    line["promotion_discount_sen"] = _checked_sen_sum(
        (line["promotion_discount_sen"], amount_sen),
        "calculated promotion discount",
    )


def _calculate_expected_snapshot_promotions(
    normalized: dict[str, Any],
    snapshot_promotions: list[dict[str, Any]],
    pos_profile: str,
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    sale_datetime = normalize_site_datetime(
        normalized["sale_datetime"],
        fieldname="order.sale_datetime",
    )
    selected_ids = {
        promotion["promotion_id"] for promotion in normalized["applied_promotions"]
    }
    lines = [
        {
            "index": index,
            "item_code": item["item_code"],
            "item_group": None,
            "quantity": item["qty"],
            "unit_price_sen": item["unit_price_sen"],
            "promotion_discount_sen": 0,
            "expected_allocations": [],
        }
        for index, item in enumerate(normalized["items"])
    ]
    ordered_rules = sorted(
        snapshot_promotions,
        key=lambda rule: (
            _snapshot_rule_integer(
                rule.get("priority") or 0,
                f"promotion {cstr(rule.get('promotion_id'))} priority",
            ),
            cstr(rule.get("promotion_id")),
        ),
    )
    expected_promotions: list[dict[str, Any]] = []
    for rule in ordered_rules:
        promotion_id = cstr(rule.get("promotion_id"))
        if not promotion_id or not _snapshot_rule_is_available(
            rule,
            sale_datetime=sale_datetime,
            pos_profile=pos_profile,
        ):
            continue
        if cstr(rule.get("activation_mode") or "automatic") == "manual_selectable" and promotion_id not in selected_ids:
            continue

        promotion_type = cstr(rule.get("promotion_type"))
        total_discount_sen = 0
        if promotion_type in {"item_discount", "happy_hour", "order_discount"}:
            # The tablet evaluates item-discount minimums across every eligible
            # unit, then applies an exclusive rule only to lines not already
            # discounted. Preserve that ordering exactly; nth-item rules use
            # the stacking-filtered unit pool below.
            eligible_units = _snapshot_eligible_units(
                rule,
                lines,
                respect_stacking=False,
            )
            if not _snapshot_rule_minimums_met(rule, eligible_units):
                continue
            for line in lines:
                if not _snapshot_line_is_eligible(rule, line):
                    continue
                if cstr(rule.get("stacking_policy") or "exclusive") != "stackable" and line[
                    "promotion_discount_sen"
                ] > 0:
                    continue
                amount_per_unit_sen = _snapshot_unit_discount_sen(
                    rule, line["unit_price_sen"]
                )
                amount_sen = amount_per_unit_sen * line["quantity"]
                if amount_sen <= 0:
                    continue
                _append_expected_promotion_allocation(
                    line,
                    promotion_id,
                    amount_sen,
                    line["quantity"],
                )
                total_discount_sen = _checked_sen_sum(
                    (total_discount_sen, amount_sen),
                    "calculated applied promotion total",
                )
        elif promotion_type in {"nth_item_discount", "buy_x_get_y"}:
            eligible_units = _snapshot_eligible_units(rule, lines)
            if not eligible_units or not _snapshot_rule_minimums_met(
                rule, eligible_units
            ):
                continue
            buy_qty = max(
                1,
                _snapshot_rule_integer(
                    rule.get("buy_qty") or 1,
                    f"promotion {promotion_id} buy_qty",
                ),
            )
            discount_qty = max(
                1,
                _snapshot_rule_integer(
                    rule.get("discount_qty") or 1,
                    f"promotion {promotion_id} discount_qty",
                ),
            )
            cycle_size = buy_qty + discount_qty
            pools: dict[str, list[dict[str, Any]]] = {}
            same_item = cstr(rule.get("eligible_scope_mode") or "eligible_pool") == "same_item"
            for unit in eligible_units:
                pool_key = unit["item_code"] if same_item else "__all__"
                pools.setdefault(pool_key, []).append(unit)
            for units in pools.values():
                ordered_units = sorted(
                    units,
                    key=lambda unit: (unit["cart_order"], unit["unit_order"]),
                )
                cycles = (
                    len(ordered_units) // cycle_size
                    if cstr(rule.get("repeat_mode") or "once") == "repeat"
                    else (1 if len(ordered_units) >= cycle_size else 0)
                )
                for cycle_index in range(cycles):
                    cycle_units = ordered_units[
                        cycle_index * cycle_size : (cycle_index + 1) * cycle_size
                    ]
                    discounted_units = sorted(
                        cycle_units,
                        key=lambda unit: (
                            unit["base_price_sen"],
                            -unit["cart_order"],
                            -unit["unit_order"],
                        ),
                    )[:discount_qty]
                    for unit in discounted_units:
                        amount_sen = _snapshot_unit_discount_sen(
                            rule, unit["base_price_sen"]
                        )
                        if amount_sen <= 0:
                            continue
                        line = lines[unit["line_index"]]
                        _append_expected_promotion_allocation(
                            line, promotion_id, amount_sen, 1
                        )
                        total_discount_sen = _checked_sen_sum(
                            (total_discount_sen, amount_sen),
                            "calculated applied promotion total",
                        )
        if total_discount_sen > 0:
            expected_promotions.append(
                {
                    "promotion_id": promotion_id,
                    "promotion_name": cstr(rule.get("promotion_name")),
                    "promotion_type": promotion_type,
                    "amount_sen": total_discount_sen,
                    "rule": rule,
                }
            )
    return expected_promotions, [line["expected_allocations"] for line in lines]


def _canonical_promotion_allocations(value: list[dict[str, Any]]) -> str:
    normalized = [
        {
            "promotion_id": allocation.get("promotion_id"),
            "amount_sen": allocation.get("amount_sen"),
            "quantity": allocation.get("quantity"),
            "scope": allocation.get("scope"),
        }
        for allocation in value
    ]
    normalized.sort(
        key=lambda allocation: (
            cstr(allocation.get("promotion_id")),
            int(allocation.get("amount_sen") or 0),
            int(allocation.get("quantity") or 0),
            cstr(allocation.get("scope")),
        )
    )
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _validate_snapshot_promotion_pricing(
    normalized: dict[str, Any],
    snapshot_promotions: list[dict[str, Any]],
    pos_profile: str,
) -> None:
    expected_promotions, expected_allocations = (
        _calculate_expected_snapshot_promotions(
            normalized, snapshot_promotions, pos_profile
        )
    )
    actual_by_id = {
        promotion["promotion_id"]: promotion
        for promotion in normalized["applied_promotions"]
    }
    expected_by_id = {
        promotion["promotion_id"]: promotion for promotion in expected_promotions
    }
    if set(actual_by_id) != set(expected_by_id):
        frappe.throw(
            "Applied promotion ids do not exactly match server-recalculated snapshot pricing",
            frappe.ValidationError,
        )
    expected_expiry_by_id = {
        promotion_id: (
            cstr(expected["rule"].get("valid_upto")) or None
        )
        for promotion_id, expected in expected_by_id.items()
    }
    if normalized["pricing_context"].get("promotion_expiry_by_id", {}) != (
        expected_expiry_by_id
    ):
        frappe.throw(
            "pricing_context.promotion_expiry_by_id does not match the promotion snapshot",
            frappe.ValidationError,
        )
    for promotion_id, expected in expected_by_id.items():
        actual = actual_by_id[promotion_id]
        rule = expected["rule"]
        if (
            actual["amount_sen"] != expected["amount_sen"]
            or (
                actual.get("promotion_name")
                and actual["promotion_name"] != expected["promotion_name"]
            )
            or (
                actual.get("promotion_type")
                and actual["promotion_type"] != expected["promotion_type"]
            )
            or (actual.get("scope") and actual["scope"] != "order")
            or actual["offline_applied"] != normalized["offline_priced"]
            or (
                actual.get("valid_from")
                and cstr(actual["valid_from"]) != cstr(rule.get("valid_from"))
            )
            or (
                actual.get("valid_upto")
                and cstr(actual["valid_upto"]) != cstr(rule.get("valid_upto"))
            )
        ):
            frappe.throw(
                f"Applied promotion {promotion_id} does not match server-recalculated snapshot pricing",
                frappe.ValidationError,
            )
    for index, item in enumerate(normalized["items"]):
        if _canonical_promotion_allocations(item["promotion_allocations"]) != (
            _canonical_promotion_allocations(expected_allocations[index])
        ):
            frappe.throw(
                f"items[{index + 1}].promotion_allocations do not match server-recalculated snapshot pricing",
                frappe.ValidationError,
            )


def _validate_published_promotion_snapshot(
    normalized: dict[str, Any],
) -> dict[str, Any]:
    pricing_context = normalized["pricing_context"]
    applied_promotions = normalized["applied_promotions"]
    items = normalized["items"]
    evidence: dict[str, Any] = {
        "offline_priced": normalized["offline_priced"],
        "pricing_context": pricing_context,
        "applied_promotions": applied_promotions,
        "items": [
            {
                "line_id": item["line_id"],
                "item_code": item["item_code"],
                "promotion_allocations": item["promotion_allocations"],
            }
            for item in items
        ],
        "reconciliation": {
            "status": normalized["promotion_reconciliation_status"],
            "source": "none",
        },
        "snapshot": None,
    }
    snapshot_version = pricing_context.get("snapshot_version")
    snapshot_hash = pricing_context.get("snapshot_hash")
    if not snapshot_version and not snapshot_hash:
        return evidence

    from kopos_connector.api.promotions import (
        build_snapshot_version_from_hash,
        compute_snapshot_content_hash,
        get_snapshot_by_version,
        resolve_snapshot_pos_profile,
    )

    pos_profile = resolve_snapshot_pos_profile(
        None,
        device_id=normalized["device_id"],
    )
    snapshot = get_snapshot_by_version(pos_profile, snapshot_version)
    if snapshot is None:
        frappe.throw(
            "Referenced promotion snapshot was not found for the device POS Profile",
            frappe.ValidationError,
        )
    persisted_hash = cstr(getattr(snapshot, "snapshot_hash", None)).strip()
    persisted_status = cstr(getattr(snapshot, "status", None)).strip()
    persisted_profile = cstr(getattr(snapshot, "pos_profile", None)).strip()
    persisted_version = cstr(getattr(snapshot, "snapshot_version", None)).strip()
    if persisted_hash != snapshot_hash:
        frappe.throw("Promotion snapshot hash mismatch", frappe.ValidationError)
    if persisted_profile != pos_profile or persisted_version != snapshot_version:
        frappe.throw(
            "Promotion snapshot document identity is inconsistent",
            frappe.ValidationError,
        )
    if persisted_status not in {"Published", "Superseded"}:
        frappe.throw(
            "Promotion snapshot is not a published pricing authority",
            frappe.ValidationError,
        )

    try:
        snapshot_payload = json.loads(
            cstr(getattr(snapshot, "snapshot_payload", None)) or "{}"
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Promotion snapshot payload is invalid JSON") from error
    if not isinstance(snapshot_payload, dict):
        frappe.throw("Promotion snapshot payload is invalid", frappe.ValidationError)
    raw_snapshot_promotions = snapshot_payload.get("promotions")
    if not isinstance(raw_snapshot_promotions, list) or any(
        not isinstance(promotion, dict) for promotion in raw_snapshot_promotions
    ):
        frappe.throw(
            "Promotion snapshot promotions payload is invalid",
            frappe.ValidationError,
        )
    recomputed_hash = compute_snapshot_content_hash(
        {
            "pos_profile": snapshot_payload.get("pos_profile"),
            "promotions": raw_snapshot_promotions,
        }
    )
    if (
        cstr(snapshot_payload.get("snapshot_version")) != snapshot_version
        or cstr(snapshot_payload.get("snapshot_hash")) != snapshot_hash
        or cstr(snapshot_payload.get("pos_profile")) != pos_profile
        or recomputed_hash != snapshot_hash
        or build_snapshot_version_from_hash(recomputed_hash) != snapshot_version
        or int(getattr(snapshot, "promotion_count", -1))
        != len(raw_snapshot_promotions)
    ):
        frappe.throw(
            "Promotion snapshot persisted identity is inconsistent",
            frappe.ValidationError,
        )
    snapshot_promotions = {
        cstr(promotion.get("promotion_id")): promotion
        for promotion in raw_snapshot_promotions
        if cstr(promotion.get("promotion_id"))
    }
    if len(snapshot_promotions) != len(raw_snapshot_promotions):
        frappe.throw(
            "Promotion snapshot contains missing or duplicate promotion ids",
            frappe.ValidationError,
        )
    for context_field, snapshot_field in (
        ("snapshot_published_at", "published_at"),
        ("snapshot_effective_from", "effective_from"),
    ):
        context_value = pricing_context.get(context_field)
        snapshot_value = snapshot_payload.get(snapshot_field)
        if context_value and snapshot_value and normalize_site_datetime(
            context_value,
            fieldname=f"pricing_context.{context_field}",
        ) != normalize_site_datetime(
            snapshot_value,
            fieldname=f"promotion snapshot {snapshot_field}",
        ):
            frappe.throw(
                f"pricing_context.{context_field} does not match the promotion snapshot",
                frappe.ValidationError,
            )
    if pricing_context.get("priced_at") and normalize_site_datetime(
        pricing_context["priced_at"],
        fieldname="pricing_context.priced_at",
    ) != normalize_site_datetime(
        normalized["sale_datetime"],
        fieldname="order.sale_datetime",
    ):
        frappe.throw(
            "pricing_context.priced_at must equal the immutable sale_datetime",
            frappe.ValidationError,
        )
    for promotion in applied_promotions:
        snapshot_promotion = snapshot_promotions.get(promotion["promotion_id"])
        if snapshot_promotion is None:
            frappe.throw(
                "Applied promotion id does not exist in the referenced snapshot",
                frappe.ValidationError,
            )
        if promotion["promotion_name"] and promotion["promotion_name"] != cstr(
            snapshot_promotion.get("promotion_name")
        ):
            frappe.throw(
                "Applied promotion name does not match the referenced snapshot",
                frappe.ValidationError,
            )
        if promotion["promotion_type"] and promotion["promotion_type"] != cstr(
            snapshot_promotion.get("promotion_type")
        ):
            frappe.throw(
                "Applied promotion type does not match the referenced snapshot",
                frappe.ValidationError,
            )

    for item in items:
        for allocation in item["promotion_allocations"]:
            snapshot_promotion = snapshot_promotions[allocation["promotion_id"]]
            eligible_items = {
                cstr(item_code)
                for item_code in snapshot_promotion.get("eligible_items", [])
                if cstr(item_code)
            }
            if eligible_items and item["item_code"] not in eligible_items:
                frappe.throw(
                    "Promotion allocation item is not eligible in the referenced snapshot",
                    frappe.ValidationError,
                )

    if pricing_context.get("pricing_mode") in {
        "online_snapshot",
        "offline_snapshot",
        "server_validated",
    }:
        _validate_snapshot_promotion_pricing(
            normalized,
            raw_snapshot_promotions,
            pos_profile,
        )

    evidence["snapshot"] = {
        "snapshot_version": snapshot_version,
        "snapshot_hash": snapshot_hash,
        "pos_profile": pos_profile,
        "status": persisted_status,
    }
    evidence["reconciliation"] = {
        "status": "matched" if applied_promotions else "not_applicable",
        "source": "published_snapshot",
    }
    return evidence


def _normalize_order_item(
    value: Any,
    index: int,
    money_contract_version: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        frappe.throw(f"items[{index}] must be an object", frappe.ValidationError)

    line_id = cstr(value.get("line_id") or value.get("backend_line_uuid"))
    item_code = cstr(value.get("item") or value.get("item_code"))
    qty = _parse_positive_integer_quantity(value.get("qty"), f"items[{index}].qty")
    uom = cstr(value.get("uom"))
    unit_price_sen = _parse_wire_money_sen(
        value,
        version=money_contract_version,
        sen_field="unit_price_sen",
        legacy_fields=("unit_price", "rate"),
    )
    modifier_total_sen = _parse_wire_money_sen(
        value,
        version=money_contract_version,
        sen_field="modifier_total_sen",
        legacy_fields=("modifier_total",),
        required=False,
        default=0,
    )
    discount_amount_sen = _parse_wire_money_sen(
        value,
        version=money_contract_version,
        sen_field="discount_amount_sen",
        legacy_fields=("discount_amount",),
        required=False,
        default=0,
    )
    line_total_sen = _parse_wire_money_sen(
        value,
        version=money_contract_version,
        sen_field="line_total_sen",
        legacy_fields=("line_total", "amount"),
    )
    remarks = cstr(value.get("remarks")) or None
    recipe = cstr(value.get("recipe")) or None
    raw_recipe_version = value.get("recipe_version")
    recipe_version = (
        _parse_positive_integer_quantity(
            raw_recipe_version,
            f"items[{index}].recipe_version",
        )
        if raw_recipe_version not in (None, "")
        else None
    )
    backend_line_uuid = cstr(value.get("backend_line_uuid")) or None
    modifiers = value.get("modifiers", value.get("selected_modifiers", []))
    promotion_allocations = _normalize_promotion_allocations(
        value.get("promotion_allocations"),
        item_index=index,
        money_contract_version=money_contract_version,
    )

    if not line_id:
        frappe.throw(f"items[{index}].line_id is required", frappe.ValidationError)
    if not item_code:
        frappe.throw(f"items[{index}].item_code is required", frappe.ValidationError)
    if bool(recipe) != (recipe_version is not None):
        frappe.throw(
            f"items[{index}].recipe and recipe_version must be provided together",
            frappe.ValidationError,
        )
    if unit_price_sen < 0:
        frappe.throw(
            f"items[{index}].unit_price_sen must be 0 or greater",
            frappe.ValidationError,
        )
    if discount_amount_sen < 0:
        frappe.throw(
            f"items[{index}].discount_amount_sen must be 0 or greater",
            frappe.ValidationError,
        )
    if line_total_sen < 0:
        frappe.throw(
            f"items[{index}].line_total_sen must be 0 or greater",
            frappe.ValidationError,
        )
    if not isinstance(modifiers, list):
        frappe.throw(f"items[{index}].modifiers must be an array", frappe.ValidationError)

    validated_modifiers = [
        _normalize_selected_modifier(
            row,
            index,
            modifier_index,
            money_contract_version,
        )
        for modifier_index, row in enumerate(modifiers, start=1)
    ]
    resolved_modifier_total_sen = _checked_sen_sum(
        (row["price_adjustment_sen"] for row in validated_modifiers),
        f"items[{index}] modifier total",
    )
    if modifier_total_sen != resolved_modifier_total_sen:
        frappe.throw(
            f"items[{index}].modifier_total_sen must equal summed modifier price adjustments",
            frappe.ValidationError,
        )

    expected_total_sen = (unit_price_sen + modifier_total_sen) * qty
    expected_total_sen -= discount_amount_sen
    _require_safe_sen(expected_total_sen, f"items[{index}] calculated total")
    if expected_total_sen != line_total_sen:
        frappe.throw(
            f"items[{index}].line_total_sen does not match qty, pricing, modifiers, and discount",
            frappe.ValidationError,
        )

    return {
        "line_id": line_id,
        "backend_line_uuid": backend_line_uuid,
        "item_code": item_code,
        "submitted_item_name": cstr(value.get("item_name")) or None,
        "qty": qty,
        "uom": uom or None,
        "unit_price_sen": unit_price_sen,
        "modifier_total_sen": resolved_modifier_total_sen,
        "discount_amount_sen": discount_amount_sen,
        "line_total_sen": line_total_sen,
        "recipe": recipe,
        "recipe_version": recipe_version,
        "remarks": remarks,
        "selected_modifiers": validated_modifiers,
        "promotion_allocations": promotion_allocations,
    }


def _normalize_selected_modifier(
    value: Any,
    item_index: int,
    modifier_index: int,
    money_contract_version: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        frappe.throw(
            f"items[{item_index}].selected_modifiers[{modifier_index}] must be an object",
            frappe.ValidationError,
        )

    modifier_group = cstr(value.get("modifier_group"))
    modifier = cstr(value.get("modifier"))
    price_adjustment_sen = _parse_wire_money_sen(
        value,
        version=money_contract_version,
        sen_field="price_adjustment_sen",
        legacy_fields=("price_adjustment", "price", "base_price"),
    )
    instruction_text = cstr(value.get("instruction_text")) or None
    sort_order = cint(value.get("sort_order"))

    if not modifier_group:
        frappe.throw(
            f"items[{item_index}].selected_modifiers[{modifier_index}].modifier_group is required",
            frappe.ValidationError,
        )
    if not modifier:
        frappe.throw(
            f"items[{item_index}].selected_modifiers[{modifier_index}].modifier is required",
            frappe.ValidationError,
        )

    return {
        "modifier_group": modifier_group,
        "modifier": modifier,
        "price_adjustment_sen": price_adjustment_sen,
        "instruction_text": instruction_text,
        "sort_order": sort_order,
    }


def _normalize_order_payment(
    value: Any,
    index: int,
    money_contract_version: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        frappe.throw(f"payments[{index}] must be an object", frappe.ValidationError)

    payment_id = _normalize_optional_identifier(
        value.get("payment_id"),
        f"payments[{index}].payment_id",
        present="payment_id" in value,
    )
    payment_method = cstr(value.get("payment_method") or value.get("method"))
    amount_sen = _parse_wire_money_sen(
        value,
        version=money_contract_version,
        sen_field="amount_sen",
        legacy_fields=("amount",),
    )
    tendered_amount_sen = _parse_wire_money_sen(
        value,
        version=money_contract_version,
        sen_field="tendered_amount_sen",
        legacy_fields=("tendered_amount",),
        required=False,
        default=0,
    )
    change_amount_sen = _parse_wire_money_sen(
        value,
        version=money_contract_version,
        sen_field="change_amount_sen",
        legacy_fields=("change_amount",),
        required=False,
        default=0,
    )
    payment_channel_code = cstr(value.get("payment_channel_code")) or None
    reference_no = cstr(value.get("reference_no")) or None
    external_transaction_id = cstr(value.get("external_transaction_id")) or None
    manual_confirmation_evidence = value.get("manual_confirmation_evidence")
    normalized_channel = cstr(payment_channel_code).strip().lower()
    normalized_channel_token = _normalize_token(payment_channel_code)
    is_static_qr = normalized_channel == "static_qr"
    is_maybank_qr = normalized_channel_token in {"maybank", "maybank qr"}
    manual_evidence = None

    if not payment_method:
        frappe.throw(
            f"payments[{index}].payment_method is required", frappe.ValidationError
        )
    if amount_sen <= 0:
        frappe.throw(
            f"payments[{index}].amount_sen must be greater than 0",
            frappe.ValidationError,
        )
    if tendered_amount_sen < 0:
        frappe.throw(
            f"payments[{index}].tendered_amount_sen must be 0 or greater",
            frappe.ValidationError,
        )
    if change_amount_sen < 0:
        frappe.throw(
            f"payments[{index}].change_amount_sen must be 0 or greater",
            frappe.ValidationError,
        )
    tendered_present = "tendered_amount_sen" in value or "tendered_amount" in value
    change_present = "change_amount_sen" in value or "change_amount" in value
    if (
        tendered_present
        and change_present
        and tendered_amount_sen - change_amount_sen != amount_sen
    ):
        frappe.throw(
            f"payments[{index}] tendered minus change must equal amount_sen",
            frappe.ValidationError,
        )

    _validate_payment_channel_binding(payment_method, payment_channel_code, index)
    if normalized_channel_token == "static qr" and normalized_channel != "static_qr":
        frappe.throw(
            f"payments[{index}].payment_channel_code must be static_qr",
            frappe.ValidationError,
        )
    if is_static_qr or (
        is_maybank_qr and manual_confirmation_evidence is not None
    ):
        manual_evidence = _validate_manual_qr_evidence(
            manual_confirmation_evidence,
            index=index,
            channel="static_qr" if is_static_qr else "maybank",
            reference_no=reference_no,
            external_transaction_id=external_transaction_id,
        )
    elif manual_confirmation_evidence is not None:
        frappe.throw(
            f"payments[{index}].manual_confirmation_evidence is supported only for QR payments",
            frappe.ValidationError,
        )

    return {
        "payment_id": payment_id,
        "payment_method": payment_method,
        "payment_channel_code": payment_channel_code,
        "amount_sen": amount_sen,
        "tendered_amount_sen": tendered_amount_sen,
        "change_amount_sen": change_amount_sen,
        "reference_no": reference_no,
        "external_transaction_id": external_transaction_id,
        "is_manual_confirmation": 1 if manual_evidence else 0,
        "manual_confirmation_evidence_json": (
            json.dumps(manual_evidence, sort_keys=True, separators=(",", ":"))
            if manual_evidence
            else None
        ),
        "reconciliation_idempotency_key": (
            cstr(manual_evidence.get("reconciliation_idempotency_key"))
            if manual_evidence
            else None
        ),
        "settlement_status": (
            "pending_reconciliation" if manual_evidence else "verified"
        ),
    }


def _resolve_order_item(value: dict[str, Any], index: int) -> dict[str, Any]:
    item_doc = frappe.get_doc("Item", value["item_code"])
    resolved_uom = value["uom"] or cstr(getattr(item_doc, "stock_uom", None))
    if not resolved_uom:
        frappe.throw(f"items[{index}].uom is required", frappe.ValidationError)
    if not frappe.db.exists("UOM", resolved_uom):
        frappe.throw(f"UOM {resolved_uom} was not found", frappe.ValidationError)
    if value["recipe"] and not frappe.db.exists("FB Recipe", value["recipe"]):
        frappe.throw(
            f"FB Recipe {value['recipe']} was not found", frappe.ValidationError
        )

    resolved_modifiers = [
        _resolve_selected_modifier(row, index, modifier_index)
        for modifier_index, row in enumerate(value["selected_modifiers"], start=1)
    ]
    return {
        "line_id": value["line_id"],
        "backend_line_uuid": value["backend_line_uuid"],
        "item": item_doc.name,
        "item_name_snapshot": value["submitted_item_name"]
        or cstr(getattr(item_doc, "item_name", None))
        or item_doc.name,
        "qty": value["qty"],
        "uom": resolved_uom,
        "unit_price": sen_to_decimal(value["unit_price_sen"]),
        "modifier_total": sen_to_decimal(value["modifier_total_sen"]),
        "discount_amount": sen_to_decimal(value["discount_amount_sen"]),
        "line_total": sen_to_decimal(value["line_total_sen"]),
        "recipe": value["recipe"],
        "recipe_version": value["recipe_version"],
        "is_recipe_managed": 1 if value["recipe"] else 0,
        "remarks": value["remarks"],
        "selected_modifiers": resolved_modifiers,
        "promotion_allocations": value["promotion_allocations"],
    }


def _resolve_selected_modifier(
    value: dict[str, Any], item_index: int, modifier_index: int
) -> dict[str, Any]:
    field_prefix = f"items[{item_index}].modifiers[{modifier_index}]"
    _get_required_fb_modifier_doc(
        "FB Modifier Group",
        value["modifier_group"],
        f"{field_prefix}.modifier_group",
    )
    modifier_doc = _get_required_fb_modifier_doc(
        "FB Modifier",
        value["modifier"],
        f"{field_prefix}.modifier",
    )
    if cstr(getattr(modifier_doc, "modifier_group", None)) != value["modifier_group"]:
        frappe.throw(
            f"{field_prefix}.modifier {value['modifier']} does not belong to FB Modifier Group {value['modifier_group']}",
            frappe.ValidationError,
        )

    return {
        "modifier_group": value["modifier_group"],
        "modifier": value["modifier"],
        # Preserve the authenticated sale snapshot. Current catalog pricing may
        # legitimately differ by the time an offline order reaches ERP.
        "price_adjustment": sen_to_decimal(value["price_adjustment_sen"]),
        "instruction_text": value["instruction_text"]
        or cstr(getattr(modifier_doc, "instruction_text", None))
        or None,
        "sort_order": value["sort_order"]
        or cint(getattr(modifier_doc, "display_order", 0)),
        "affects_stock": 1
        if cint(getattr(modifier_doc, "affects_stock", 0))
        else 0,
        "affects_recipe": 1
        if cint(getattr(modifier_doc, "affects_recipe", 0))
        else 0,
    }


def _resolve_order_payment(value: dict[str, Any], index: int) -> dict[str, Any]:
    payment_method = _resolve_mode_of_payment_name(value["payment_method"])
    _validate_payment_channel_binding(
        payment_method,
        value["payment_channel_code"],
        index,
    )
    _require_doc("Mode of Payment", payment_method, "payment_method")
    resolved = dict(value)
    resolved["source_payment_id"] = resolved.pop("payment_id", None)
    resolved["payment_method"] = payment_method
    resolved["amount"] = sen_to_decimal(value["amount_sen"])
    resolved["tendered_amount"] = sen_to_decimal(value["tendered_amount_sen"])
    resolved["change_amount"] = sen_to_decimal(value["change_amount_sen"])
    resolved.pop("amount_sen", None)
    resolved.pop("tendered_amount_sen", None)
    resolved.pop("change_amount_sen", None)
    return resolved


def _validate_payment_channel_binding(
    payment_method: str,
    payment_channel_code: str | None,
    index: int,
) -> None:
    normalized_payment_method = _normalize_token(payment_method)
    normalized_channel_token = _normalize_token(payment_channel_code)
    supported_qr_channels = {"maybank", "maybank qr", "static qr"}
    if (
        normalized_channel_token in supported_qr_channels
        and normalized_payment_method != "duitnow qr"
    ):
        frappe.throw(
            f"payments[{index}].payment_channel_code requires DuitNow QR",
            frappe.ValidationError,
        )
    if (
        normalized_payment_method == "duitnow qr"
        and normalized_channel_token not in supported_qr_channels
    ):
        frappe.throw(
            f"payments[{index}].payment_channel_code is required for DuitNow QR",
            frappe.ValidationError,
        )


# Compatibility helpers for internal callers and focused tests. Public requests
# must still declare their money contract at the payload root.
def _validate_order_item(value: Any, index: int) -> dict[str, Any]:
    normalized = _normalize_order_item(
        value,
        index,
        LEGACY_DECIMAL_MONEY_CONTRACT_VERSION,
    )
    return _resolve_order_item(normalized, index)


def _validate_selected_modifier(
    value: Any, item_index: int, modifier_index: int
) -> dict[str, Any]:
    normalized = _normalize_selected_modifier(
        value,
        item_index,
        modifier_index,
        LEGACY_DECIMAL_MONEY_CONTRACT_VERSION,
    )
    return _resolve_selected_modifier(normalized, item_index, modifier_index)


def _validate_order_payment(value: Any, index: int) -> dict[str, Any]:
    normalized = _normalize_order_payment(
        value,
        index,
        LEGACY_DECIMAL_MONEY_CONTRACT_VERSION,
    )
    return _resolve_order_payment(normalized, index)


def _validate_manual_qr_evidence(
    value: Any,
    *,
    index: int,
    channel: str,
    reference_no: str | None,
    external_transaction_id: str | None,
) -> dict[str, Any]:
    prefix = f"payments[{index}].manual_confirmation_evidence"
    if not isinstance(value, Mapping) or not value:
        frappe.throw(
            f"{prefix} is required for {channel}",
            frappe.ValidationError,
        )

    evidence = dict(value)
    required_fields = (
        "evidence_kind",
        "captured_at",
        "upload_status",
        "reconciliation_status",
        "local_confirmed_at",
        "local_confirmed_by",
        "local_confirmation_reference",
        "reconciliation_idempotency_key",
        "evidence_captured_device_id",
    )
    for fieldname in required_fields:
        if not cstr(evidence.get(fieldname)).strip():
            frappe.throw(
                f"{prefix}.{fieldname} is required",
                frappe.ValidationError,
            )

    reconciliation_status = cstr(evidence.get("reconciliation_status")).strip()
    if reconciliation_status not in {
        "pending_reconciliation",
        "erp_reconciliation_pending",
    }:
        frappe.throw(
            f"{prefix}.reconciliation_status must be pending reconciliation",
            frappe.ValidationError,
        )

    evidence_kind = cstr(evidence.get("evidence_kind")).strip()
    if evidence_kind not in {
        "receipt_photo",
        "reference",
        "no_receipt_acknowledgement",
    }:
        frappe.throw(
            f"{prefix}.evidence_kind is invalid",
            frappe.ValidationError,
        )

    for fieldname in ("captured_at", "local_confirmed_at"):
        try:
            frappe_utils.get_datetime(evidence.get(fieldname))
        except Exception:
            frappe.throw(
                f"{prefix}.{fieldname} is invalid",
                frappe.ValidationError,
            )

    idempotency_key = cstr(evidence.get("reconciliation_idempotency_key")).strip()
    if len(idempotency_key) > 140:
        frappe.throw(
            f"{prefix}.reconciliation_idempotency_key is too long",
            frappe.ValidationError,
        )

    provider_session = cstr(external_transaction_id).strip()
    payment_reference = cstr(reference_no).strip()
    if channel == "static_qr":
        if not provider_session.startswith("static-"):
            frappe.throw(
                f"payments[{index}].external_transaction_id must be a static_qr session",
                frappe.ValidationError,
            )
    else:
        if not provider_session or provider_session.startswith("static-"):
            frappe.throw(
                f"payments[{index}].external_transaction_id must identify the issued Maybank QR transaction",
                frappe.ValidationError,
            )
        if not payment_reference or payment_reference != provider_session:
            frappe.throw(
                f"payments[{index}] Maybank QR references must identify the same issued transaction",
                frappe.ValidationError,
            )

    evidence_reference = cstr(evidence.get("local_confirmation_reference")).strip()
    if payment_reference and evidence_reference != payment_reference:
        frappe.throw(
            f"{prefix}.local_confirmation_reference does not match reference_no",
            frappe.ValidationError,
        )

    upload_status = cstr(evidence.get("upload_status")).strip()
    if evidence_kind == "receipt_photo":
        if not cstr(evidence.get("local_uri")).strip():
            frappe.throw(f"{prefix}.local_uri is required", frappe.ValidationError)
        if cstr(evidence.get("local_uri")).strip().lower().startswith("data:"):
            frappe.throw(
                f"{prefix}.local_uri must not contain inline image data",
                frappe.ValidationError,
            )
        if cstr(evidence.get("mime_type")).strip().lower() not in {
            "image/jpeg",
            "image/pjpeg",
        }:
            frappe.throw(
                f"{prefix}.mime_type must be image/jpeg",
                frappe.ValidationError,
            )
        if upload_status not in {
            "upload_pending",
            "uploading",
            "uploaded",
            "upload_failed",
        }:
            frappe.throw(
                f"{prefix}.upload_status is invalid for receipt evidence",
                frappe.ValidationError,
            )
    elif evidence_kind == "no_receipt_acknowledgement":
        if evidence.get("no_receipt_acknowledged") is not True:
            frappe.throw(
                f"{prefix}.no_receipt_acknowledged must be true",
                frappe.ValidationError,
            )
        if not cstr(evidence.get("no_receipt_reason_code")).strip():
            frappe.throw(
                f"{prefix}.no_receipt_reason_code is required",
                frappe.ValidationError,
            )
        if upload_status != "not_required":
            frappe.throw(
                f"{prefix}.upload_status must be not_required",
                frappe.ValidationError,
            )
    elif upload_status not in {"not_required", "uploaded"}:
        frappe.throw(
            f"{prefix}.upload_status is invalid for reference evidence",
            frappe.ValidationError,
        )

    evidence["reconciliation_idempotency_key"] = idempotency_key
    evidence["reconciliation_status"] = "pending_reconciliation"
    return evidence


PAYMENT_METHOD_ALIASES = {
    "cash": {"cash"},
    "qr": {"duitnow qr"},
    "card": {"card", "credit card", "debit card"},
    "voucher": {"voucher", "coupon", "gift voucher"},
}

PAYMENT_METHOD_CANONICAL_NAMES = {
    "cash": "Cash",
    "qr": "DuitNow QR",
    "card": "Card",
    "voucher": "Voucher",
}


def _normalize_token(value: Any) -> str:
    return normalize_qr_token(value)


def _resolve_mode_of_payment_name(requested_mode: str) -> str:
    requested = cstr(requested_mode).strip()
    if not requested:
        return requested

    normalized_requested = _normalize_token(requested)
    if normalized_requested == "duitnow qr" and frappe.db.exists(
        "Mode of Payment", "DuitNow QR"
    ):
        return "DuitNow QR"

    if normalized_requested in {
        "qr",
        "maybank qr",
        "duitnow",
        "e wallet",
        "ewallet",
        "wallet",
    }:
        frappe.throw(
            "payment_method must be DuitNow QR",
            frappe.ValidationError,
        )

    aliases = PAYMENT_METHOD_ALIASES.get(normalized_requested, {normalized_requested})

    canonical_name = PAYMENT_METHOD_CANONICAL_NAMES.get(normalized_requested)
    if canonical_name and frappe.db.exists("Mode of Payment", canonical_name):
        return canonical_name

    if frappe.db.exists("Mode of Payment", requested):
        return requested

    available_modes = frappe.get_all("Mode of Payment", pluck="name") or []
    for mode in available_modes:
        if _normalize_token(mode) == normalized_requested:
            return cstr(mode)
    for mode in available_modes:
        if _normalize_token(mode) in aliases:
            return cstr(mode)

    return requested


def _build_fb_order(validated: dict[str, Any]):
    pricing_context_value = validated.get("pricing_context")
    pricing_context = (
        dict(pricing_context_value)
        if isinstance(pricing_context_value, Mapping)
        else {}
    )
    promotion_evidence_value = validated.get("promotion_evidence")
    promotion_reconciliation_status = cstr(
        validated.get("promotion_reconciliation_status") or "not_applicable"
    )
    promotion_evidence = (
        dict(promotion_evidence_value)
        if isinstance(promotion_evidence_value, Mapping)
        else {
            "offline_priced": bool(validated.get("offline_priced", False)),
            "pricing_context": pricing_context,
            "applied_promotions": [],
            "items": [
                {
                    "line_id": item["line_id"],
                    "item_code": item["item"],
                    "promotion_allocations": item.get(
                        "promotion_allocations", []
                    ),
                }
                for item in validated["items"]
            ],
            "reconciliation": {
                "status": promotion_reconciliation_status,
                "source": "none",
            },
            "snapshot": None,
        }
    )
    order_doc = frappe.new_doc("FB Order")
    order_doc.order_id = validated["order_id"]
    order_doc.display_number = validated["display_number"]
    order_doc.order_type = validated["order_type"]
    order_doc.catalog_version = validated["catalog_version"]
    order_doc.external_idempotency_key = validated["external_idempotency_key"]
    order_doc.request_fingerprint = validated["request_fingerprint"]
    order_doc.source = validated["source"]
    order_doc.sale_datetime = validated["sale_datetime"]
    order_doc.device_id = validated["device_id"]
    order_doc.shift = validated["shift"]
    order_doc.staff_id = validated["staff_id"]
    order_doc.event_project = validated["event_project"]
    order_doc.booth_warehouse = validated["booth_warehouse"]
    order_doc.company = validated["company"]
    order_doc.currency = validated["currency"]
    order_doc.customer = validated["customer"]
    order_doc.status = "Draft"
    order_doc.invoice_status = "Pending"
    order_doc.stock_status = "Pending"
    order_doc.net_total = validated["net_total"]
    order_doc.tax_total = validated["tax_total"]
    order_doc.tax_rate = validated["tax_rate"]
    if hasattr(order_doc, "rounding_adjustment"):
        order_doc.rounding_adjustment = validated["rounding_adjustment"]
    order_doc.grand_total = validated["grand_total"]
    order_doc.pricing_mode = pricing_context.get("pricing_mode")
    order_doc.promotion_snapshot_version = pricing_context.get(
        "snapshot_version"
    )
    order_doc.promotion_snapshot_hash = pricing_context.get(
        "snapshot_hash"
    )
    order_doc.promotion_reconciliation_status = promotion_reconciliation_status
    order_doc.promotion_payload_json = json.dumps(
        promotion_evidence,
        sort_keys=True,
        separators=(",", ":"),
    )
    order_doc.notes = validated["notes"]

    for item in validated["items"]:
        row = order_doc.append(
            "items",
            {
                "line_id": item["line_id"],
                "backend_line_uuid": item["backend_line_uuid"],
                "item": item["item"],
                "item_name_snapshot": item["item_name_snapshot"],
                "qty": item["qty"],
                "uom": item["uom"],
                "unit_price": item["unit_price"],
                "modifier_total": item["modifier_total"],
                "discount_amount": item["discount_amount"],
                "line_total": item["line_total"],
                "recipe": item["recipe"],
                "recipe_version": item["recipe_version"],
                "is_recipe_managed": item["is_recipe_managed"],
                "promotion_allocations_json": json.dumps(
                    item.get("promotion_allocations", []),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "remarks": item["remarks"],
            },
        )
        _set_selected_modifiers_payload(row, item["selected_modifiers"])

    for payment in validated["payments"]:
        order_doc.append("payments", payment)

    return order_doc


def _canonical_order_request_fingerprint(validated: dict[str, Any]) -> str:
    canonical = {
        key: value
        for key, value in validated.items()
        if key
        not in {
            "money_contract_version",
            "request_fingerprint",
            "accepted_sale_fingerprint",
        }
    }
    message = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        default=_canonical_json_value,
    )
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _canonical_accepted_sale_fingerprint(validated: dict[str, Any]) -> str:
    """Fingerprint immutable sale facts separately from mutable QR settlement."""

    payment_identity_fields = (
        "payment_id",
        "payment_method",
        "payment_channel_code",
        "amount_sen",
        "tendered_amount_sen",
        "change_amount_sen",
    )
    canonical = {
        key: value
        for key, value in validated.items()
        if key
        not in {
            "money_contract_version",
            "request_fingerprint",
            "accepted_sale_fingerprint",
            "payments",
        }
    }
    canonical["payments"] = [
        {
            fieldname: payment.get(fieldname)
            for fieldname in payment_identity_fields
        }
        for payment in validated["payments"]
    ]
    message = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        default=_canonical_json_value,
    )
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _canonical_json_value(value: Any) -> str:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return cstr(isoformat())
    return cstr(value)


def _validate_existing_order_fingerprint(
    validated: dict[str, Any], order_doc: Any
) -> None:
    existing_fingerprint = cstr(
        getattr(order_doc, "request_fingerprint", None)
    ).strip()
    if not existing_fingerprint:
        frappe.throw(
            "Existing FB Order has no request fingerprint; idempotent retry cannot be verified",
            frappe.ValidationError,
        )
    if existing_fingerprint != validated["request_fingerprint"]:
        frappe.throw(
            "idempotency_key was already used with a different canonical FB Order payload",
            frappe.ValidationError,
        )


def _validate_existing_accepted_sale_fingerprint(
    normalized: dict[str, Any],
    order_doc: Any,
) -> None:
    existing_fingerprint = cstr(
        getattr(order_doc, "accepted_sale_fingerprint", None)
    ).strip()
    existing_state = cstr(
        getattr(order_doc, "automatic_qr_state", None)
    ).strip()
    if not existing_fingerprint or not existing_state:
        frappe.throw(
            "idempotency_key is already used by a sale that was not prepared for Automatic QR",
            frappe.ValidationError,
        )
    if existing_fingerprint != normalized["accepted_sale_fingerprint"]:
        frappe.throw(
            "idempotency_key was already used with a different accepted sale",
            frappe.ValidationError,
        )


def _set_selected_modifiers_payload(
    line: Any, modifiers: list[dict[str, Any]]
) -> None:
    table_fieldnames = getattr(line, "_table_fieldnames", {})
    append = getattr(line, "append", None)
    if (
        isinstance(table_fieldnames, Mapping)
        and table_fieldnames.get("selected_modifiers")
        and callable(append)
    ):
        for modifier in modifiers:
            append("selected_modifiers", modifier)
        return

    # Frappe v16 intentionally assigns an empty child-table map to rows inside
    # another child table. FB Order Line therefore exposes Document.append(),
    # but cannot use it for its nested selected_modifiers field. Keep the
    # authenticated sale snapshot transient until FBOrder.before_submit writes
    # the durable rows to FB Resolved Sale.
    setattr(
        line,
        "_selected_modifiers_payload",
        [frappe._dict(modifier) for modifier in modifiers],
    )


def _get_existing_fb_order_name(idempotency_key: str) -> str | None:
    if not idempotency_key:
        return None
    existing = frappe.db.get_value(
        "FB Order",
        {"external_idempotency_key": idempotency_key},
        "name",
    )
    return cstr(existing) or None


def _build_submit_response(result_status: str, order_doc) -> dict[str, Any]:
    projections = _get_projection_statuses("FB Order", order_doc.name)
    projection_status = _get_submit_projection_status(order_doc, projections)
    response_status = (
        "partial_failure" if projection_status["diagnostics"] else result_status
    )
    first_diagnostic = (
        projection_status["diagnostics"][0]
        if projection_status["diagnostics"]
        else None
    )
    return {
        "status": response_status,
        "partial_failure": bool(projection_status["diagnostics"]),
        "fb_order": order_doc.name,
        "order_id": cstr(order_doc.order_id),
        "idempotency_key": cstr(order_doc.external_idempotency_key),
        "sale_datetime": cstr(getattr(order_doc, "sale_datetime", None)) or None,
        "sales_invoice": cstr(order_doc.sales_invoice) or None,
        "ingredient_stock_entry": cstr(order_doc.ingredient_stock_entry) or None,
        "order_status": cstr(order_doc.status),
        "invoice_status": cstr(order_doc.invoice_status),
        "stock_status": cstr(order_doc.stock_status),
        "projection_status": projection_status["projection_status"],
        "failed_subsystem": first_diagnostic["failed_subsystem"]
        if first_diagnostic
        else None,
        "diagnostics": projection_status["diagnostics"],
        "message": first_diagnostic["error_message"] if first_diagnostic else None,
        "projections": projections,
    }


def _get_submit_projection_status(
    order_doc,
    projections: list[dict[str, Any]],
) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    fb_order = cstr(order_doc.name)
    idempotency_key = cstr(getattr(order_doc, "external_idempotency_key", None))
    order_status = cstr(getattr(order_doc, "status", None))
    invoice_status = cstr(getattr(order_doc, "invoice_status", None))
    stock_status = cstr(getattr(order_doc, "stock_status", None))

    if order_status != "Submitted":
        diagnostics.append(
            _build_projection_diagnostic(
                fb_order=fb_order,
                idempotency_key=idempotency_key,
                projection_status="failed",
                failed_subsystem="fb_order",
                error_message=f"FB Order status is {order_status or 'missing'}; expected Submitted",
            )
        )

    if invoice_status != "Posted":
        diagnostics.append(
            _build_projection_diagnostic(
                fb_order=fb_order,
                idempotency_key=idempotency_key,
                projection_status="failed",
                failed_subsystem="sales_invoice",
                error_message=f"Sales Invoice projection status is {invoice_status or 'missing'}; expected Posted",
            )
        )

    if stock_status not in ACCEPTABLE_STOCK_STATUSES:
        diagnostics.append(
            _build_projection_diagnostic(
                fb_order=fb_order,
                idempotency_key=idempotency_key,
                projection_status="failed",
                failed_subsystem="stock",
                error_message=f"Stock projection status is {stock_status or 'missing'}; expected Pending or Posted",
            )
        )

    for projection in projections:
        if cstr(projection.get("state")) != "Failed":
            continue
        projection_type = cstr(projection.get("projection_type"))
        failed_subsystem = PROJECTION_SUBSYSTEMS.get(
            projection_type,
            projection_type.lower().replace(" ", "_") or "projection",
        )
        diagnostics.append(
            _build_projection_diagnostic(
                fb_order=fb_order,
                idempotency_key=idempotency_key,
                projection_status="failed",
                failed_subsystem=failed_subsystem,
                error_message=cstr(projection.get("last_error"))
                or f"{projection_type or 'Projection'} failed",
            )
        )

    return {
        "projection_status": "failed" if diagnostics else "posted",
        "diagnostics": _dedupe_projection_diagnostics(diagnostics),
    }


def _build_projection_diagnostic(
    *,
    fb_order: str,
    idempotency_key: str,
    projection_status: str,
    failed_subsystem: str,
    error_message: str,
) -> dict[str, Any]:
    return {
        "fb_order": fb_order,
        "projection_status": projection_status,
        "failed_subsystem": failed_subsystem,
        "error_message": error_message,
        "idempotency_key": idempotency_key,
    }


def _dedupe_projection_diagnostics(
    diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    deduped = []
    seen = set()
    for diagnostic in diagnostics:
        key = (
            diagnostic["fb_order"],
            diagnostic["failed_subsystem"],
            diagnostic["error_message"],
            diagnostic["idempotency_key"],
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(diagnostic)
    return deduped


def _validate_fb_order_doc(doc) -> None:
    if not cstr(getattr(doc, "order_id", None)):
        frappe.throw("FB Order requires order_id", frappe.ValidationError)
    if not cstr(getattr(doc, "external_idempotency_key", None)):
        frappe.throw(
            "FB Order requires external_idempotency_key", frappe.ValidationError
        )
    if not cstr(getattr(doc, "device_id", None)):
        frappe.throw("FB Order requires device_id", frappe.ValidationError)
    if not cstr(getattr(doc, "shift", None)):
        frappe.throw("FB Order requires shift", frappe.ValidationError)
    if not cstr(getattr(doc, "staff_id", None)):
        frappe.throw("FB Order requires staff_id", frappe.ValidationError)
    if not cstr(getattr(doc, "booth_warehouse", None)):
        frappe.throw("FB Order requires booth_warehouse", frappe.ValidationError)
    if not cstr(getattr(doc, "company", None)):
        frappe.throw("FB Order requires company", frappe.ValidationError)
    if not cstr(getattr(doc, "currency", None)):
        frappe.throw("FB Order requires currency", frappe.ValidationError)
    if not doc.get("items"):
        frappe.throw("FB Order requires at least one item", frappe.ValidationError)
    if not doc.get("payments"):
        frappe.throw("FB Order requires at least one payment", frappe.ValidationError)

    _require_doc("FB Shift", cstr(doc.shift), "shift")
    _require_doc("Warehouse", cstr(doc.booth_warehouse), "booth_warehouse")
    _require_doc("Company", cstr(doc.company), "company")
    _require_doc("User", cstr(doc.staff_id), "staff_id")
    if cstr(getattr(doc, "customer", None)):
        _require_doc("Customer", cstr(doc.customer), "customer")
    if cstr(getattr(doc, "event_project", None)):
        _require_doc("Project", cstr(doc.event_project), "event_project")

    if doc.name:
        duplicate_name = frappe.db.get_value(
            "FB Order",
            {
                "external_idempotency_key": cstr(doc.external_idempotency_key),
                "name": ["!=", doc.name],
            },
            "name",
        )
    else:
        duplicate_name = _get_existing_fb_order_name(cstr(doc.external_idempotency_key))
    if duplicate_name:
        frappe.throw(
            f"FB Order already exists for idempotency key {doc.external_idempotency_key}",
            frappe.ValidationError,
        )

    line_total_values_sen: list[int] = []
    for index, row in enumerate(doc.get("items") or [], start=1):
        if not cstr(getattr(row, "line_id", None)):
            frappe.throw(
                f"FB Order item {index} requires line_id", frappe.ValidationError
            )
        if not cstr(getattr(row, "item", None)):
            frappe.throw(f"FB Order item {index} requires item", frappe.ValidationError)
        _parse_positive_integer_quantity(
            getattr(row, "qty", None), f"FB Order item {index} qty"
        )
        line_total_sen = _persisted_money_sen(
            getattr(row, "line_total", None),
            f"FB Order item {index} line_total",
        )
        if line_total_sen < 0:
            frappe.throw(
                f"FB Order item {index} requires line_total 0 or greater",
                frappe.ValidationError,
            )
        line_total_values_sen.append(line_total_sen)

    payment_values_sen: list[int] = []
    source_payment_ids: set[str] = set()
    for index, row in enumerate(doc.get("payments") or [], start=1):
        if not cstr(getattr(row, "payment_method", None)):
            frappe.throw(
                f"FB Order payment {index} requires payment_method",
                frappe.ValidationError,
            )
        amount_sen = _persisted_money_sen(
            getattr(row, "amount", None),
            f"FB Order payment {index} amount",
        )
        if amount_sen <= 0:
            frappe.throw(
                f"FB Order payment {index} requires amount greater than 0",
                frappe.ValidationError,
            )
        source_payment_id = str(
            getattr(row, "source_payment_id", None) or ""
        ).strip()
        if source_payment_id:
            if len(source_payment_id) > 140:
                frappe.throw(
                    f"FB Order payment {index} source_payment_id is too long",
                    frappe.ValidationError,
                )
            if source_payment_id in source_payment_ids:
                frappe.throw(
                    "FB Order payment source_payment_id values must be unique within the order",
                    frappe.ValidationError,
                )
            source_payment_ids.add(source_payment_id)
        payment_values_sen.append(amount_sen)

    line_total_sum_sen = _checked_sen_sum(
        line_total_values_sen, "FB Order line total"
    )
    payment_total_sen = _checked_sen_sum(
        payment_values_sen, "FB Order payment total"
    )
    net_total_sen = _persisted_money_sen(doc.net_total, "FB Order net_total")
    tax_total_sen = _persisted_money_sen(doc.tax_total, "FB Order tax_total")
    rounding_adjustment_sen = _persisted_money_sen(
        getattr(doc, "rounding_adjustment", 0) or 0,
        "FB Order rounding_adjustment",
    )
    grand_total_sen = _persisted_money_sen(
        doc.grand_total, "FB Order grand_total"
    )

    if line_total_sum_sen != net_total_sen:
        frappe.throw(
            "FB Order net_total must equal summed line totals", frappe.ValidationError
        )
    calculated_total_sen = _checked_sen_sum(
        (net_total_sen, tax_total_sen, rounding_adjustment_sen),
        "FB Order calculated total",
    )
    if calculated_total_sen != grand_total_sen:
        frappe.throw(
            "FB Order grand_total must equal net_total plus tax_total plus rounding_adjustment",
            frappe.ValidationError,
        )
    if payment_total_sen != grand_total_sen:
        frappe.throw(
            "FB Order payments total must equal grand_total",
            frappe.ValidationError,
        )
    _validate_persisted_fb_order_promotion_evidence(doc)


def _parse_persisted_promotion_value(
    value: Any,
    *,
    expected_type: type,
    fieldname: str,
) -> Any:
    if value in (None, ""):
        return expected_type()
    if isinstance(value, expected_type):
        return value
    if not isinstance(value, str):
        frappe.throw(f"{fieldname} has an invalid type", frappe.ValidationError)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        frappe.throw(f"{fieldname} is invalid JSON", frappe.ValidationError)
        raise ValueError(f"{fieldname} is invalid JSON") from error
    if not isinstance(parsed, expected_type):
        frappe.throw(f"{fieldname} has an invalid JSON shape", frappe.ValidationError)
    return parsed


def _validate_persisted_fb_order_promotion_evidence(doc: Any) -> None:
    raw_payload = getattr(doc, "promotion_payload_json", None)
    raw_allocations = [
        _parse_persisted_promotion_value(
            getattr(item, "promotion_allocations_json", None),
            expected_type=list,
            fieldname=f"FB Order item {cstr(getattr(item, 'line_id', None))} promotion_allocations_json",
        )
        for item in (doc.get("items") or [])
    ]
    if not raw_payload:
        if any(raw_allocations) or any(
            cstr(getattr(doc, fieldname, None))
            for fieldname in (
                "pricing_mode",
                "promotion_snapshot_version",
                "promotion_snapshot_hash",
            )
        ):
            frappe.throw(
                "FB Order promotion provenance requires promotion_payload_json",
                frappe.ValidationError,
            )
        payload = {
            "offline_priced": False,
            "pricing_context": {},
            "applied_promotions": [],
            "items": [
                {
                    "line_id": cstr(getattr(item, "line_id", None)),
                    "item_code": cstr(getattr(item, "item", None)),
                    "promotion_allocations": [],
                }
                for item in (doc.get("items") or [])
            ],
            "reconciliation": {
                "status": "not_applicable",
                "source": "none",
            },
            "snapshot": None,
        }
        doc.promotion_reconciliation_status = "not_applicable"
        doc.promotion_payload_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )
    else:
        payload = _parse_persisted_promotion_value(
            raw_payload,
            expected_type=dict,
            fieldname="FB Order promotion_payload_json",
        )

    raw_context = payload.get("pricing_context", {})
    if not isinstance(raw_context, Mapping):
        frappe.throw(
            "FB Order promotion pricing_context must be an object",
            frappe.ValidationError,
        )
    pricing_context = (
        {} if not raw_context else _normalize_pricing_context(raw_context)
    )
    applied_promotions = _normalize_applied_promotions(
        payload.get("applied_promotions"),
        "sen_v1",
    )
    offline_priced = payload.get("offline_priced", False)
    if not isinstance(offline_priced, bool):
        frappe.throw(
            "FB Order promotion offline_priced must be a boolean",
            frappe.ValidationError,
        )
    normalized_items: list[dict[str, Any]] = []
    for index, item in enumerate(doc.get("items") or [], start=1):
        allocations = _normalize_promotion_allocations(
            raw_allocations[index - 1],
            item_index=index,
            money_contract_version="sen_v1",
        )
        normalized_items.append(
            {
                "line_id": cstr(getattr(item, "line_id", None)),
                "item_code": cstr(getattr(item, "item", None)),
                "qty": _parse_positive_integer_quantity(
                    getattr(item, "qty", None), f"FB Order item {index} qty"
                ),
                "unit_price_sen": _persisted_money_sen(
                    getattr(item, "unit_price", None),
                    f"FB Order item {index} unit_price",
                ),
                "discount_amount_sen": _persisted_money_sen(
                    getattr(item, "discount_amount", None) or 0,
                    f"FB Order item {index} discount_amount",
                ),
                "promotion_allocations": allocations,
            }
        )
    reconciliation_status = _validate_normalized_promotion_evidence(
        pricing_context,
        applied_promotions,
        normalized_items,
    )
    _validate_offline_pricing_consistency(offline_priced, pricing_context)
    normalized = {
        "device_id": cstr(getattr(doc, "device_id", None)),
        "sale_datetime": getattr(doc, "sale_datetime", None),
        "offline_priced": offline_priced,
        "pricing_context": pricing_context,
        "applied_promotions": applied_promotions,
        "promotion_reconciliation_status": reconciliation_status,
        "items": normalized_items,
    }
    expected_payload = _validate_published_promotion_snapshot(normalized)
    persisted_snapshot = payload.get("snapshot")
    if (
        isinstance(persisted_snapshot, Mapping)
        and isinstance(expected_payload.get("snapshot"), Mapping)
        and cstr(persisted_snapshot.get("status")) in {"Published", "Superseded"}
    ):
        expected_payload["snapshot"]["status"] = persisted_snapshot.get("status")
    if json.dumps(payload, sort_keys=True, separators=(",", ":")) != json.dumps(
        expected_payload, sort_keys=True, separators=(",", ":")
    ):
        frappe.throw(
            "FB Order promotion payload does not match its canonical snapshot evidence",
            frappe.ValidationError,
        )
    if (
        cstr(getattr(doc, "pricing_mode", None))
        != cstr(pricing_context.get("pricing_mode"))
        or cstr(getattr(doc, "promotion_snapshot_version", None))
        != cstr(pricing_context.get("snapshot_version"))
        or cstr(getattr(doc, "promotion_snapshot_hash", None))
        != cstr(pricing_context.get("snapshot_hash"))
        or cstr(getattr(doc, "promotion_reconciliation_status", None))
        != reconciliation_status
    ):
        frappe.throw(
            "FB Order promotion headers do not match promotion_payload_json",
            frappe.ValidationError,
        )


def _set_default_order_statuses(doc) -> None:
    if not cstr(getattr(doc, "status", None)):
        doc.status = "Draft"
    if not cstr(getattr(doc, "invoice_status", None)):
        doc.invoice_status = "Pending"
    if not cstr(getattr(doc, "stock_status", None)):
        doc.stock_status = "Pending"


def _validate_return_event_doc(doc) -> None:
    if not cstr(getattr(doc, "return_id", None)):
        frappe.throw("FB Return Event requires return_id", frappe.ValidationError)
    if not cstr(getattr(doc, "status", None)):
        doc.status = "Draft"
    if not cstr(getattr(doc, "fb_order", None)) and not cstr(
        getattr(doc, "original_sales_invoice", None)
    ):
        frappe.throw(
            "FB Return Event requires fb_order or original_sales_invoice",
            frappe.ValidationError,
        )
    if cstr(getattr(doc, "fb_order", None)):
        _require_doc("FB Order", cstr(doc.fb_order), "fb_order")
    if cstr(getattr(doc, "original_sales_invoice", None)):
        _require_doc(
            "Sales Invoice",
            cstr(doc.original_sales_invoice),
            "original_sales_invoice",
        )


def _validate_remake_event_doc(doc) -> None:
    if not cstr(getattr(doc, "remake_id", None)):
        frappe.throw("FB Remake Event requires remake_id", frappe.ValidationError)
    if not cstr(getattr(doc, "status", None)):
        doc.status = "Draft"
    if not cstr(getattr(doc, "original_order", None)):
        frappe.throw("FB Remake Event requires original_order", frappe.ValidationError)
    _require_doc("FB Order", cstr(doc.original_order), "original_order")
    if cstr(getattr(doc, "original_order_line", None)):
        _require_doc(
            "FB Order Line", cstr(doc.original_order_line), "original_order_line"
        )
    if cstr(getattr(doc, "original_resolved_sale", None)):
        _require_doc(
            "FB Resolved Sale",
            cstr(doc.original_resolved_sale),
            "original_resolved_sale",
        )


def _process_projection_bundle(
    doc, source_doctype: str, config: tuple[dict[str, str], ...]
) -> None:
    results = []
    for entry in config:
        if entry.get("enabled_field") and not cint(
            getattr(doc, entry["enabled_field"], 0)
        ):
            continue
        results.append(_sync_projection(doc, source_doctype, entry))

    if source_doctype == "FB Order":
        doc.invoice_status = _resolve_order_status(results, "Sales Invoice")
        doc.stock_status = _resolve_order_status(results, "Stock Issue")
        doc.db_set("status", "Submitted", update_modified=False)
        doc.db_set("invoice_status", doc.invoice_status, update_modified=False)
        doc.db_set("stock_status", doc.stock_status, update_modified=False)


def _sync_projection(
    doc, source_doctype: str, config: dict[str, str]
) -> dict[str, Any]:
    log = _get_or_create_projection_log(doc, source_doctype, config)
    target_name = cstr(getattr(doc, config["target_field"], None))
    target_exists = bool(
        target_name and frappe.db.exists(config["target_doctype"], target_name)
    )
    state = "Succeeded" if target_exists else "Pending"
    last_error = (
        None if target_exists else cstr(getattr(log, "last_error", None)) or None
    )
    _update_projection_log(
        log, state, target_name if target_exists else None, last_error
    )
    return {
        "projection_type": config["projection_type"],
        "state": state,
        "target_name": target_name if target_exists else None,
        "log": log.name,
    }


def _get_or_create_projection_log(doc, source_doctype: str, config: dict[str, str]):
    existing_name = frappe.db.get_value(
        "FB Projection Log",
        {
            "source_doctype": source_doctype,
            "source_name": doc.name,
            "projection_type": config["projection_type"],
        },
        "name",
    )
    if existing_name:
        return frappe.get_doc("FB Projection Log", existing_name)

    log = frappe.new_doc("FB Projection Log")
    log.projection_id = _make_projection_id(
        source_doctype, doc.name, config["projection_type"]
    )
    log.source_doctype = source_doctype
    log.source_name = doc.name
    log.source_event_type = "submit"
    log.projection_type = config["projection_type"]
    log.idempotency_key = _make_projection_id(
        source_doctype, doc.name, config["projection_type"]
    )
    log.payload_hash = _build_payload_hash(doc, config["projection_type"])
    log.target_doctype = config["target_doctype"]
    log.state = "Pending"
    log.retry_count = 0
    log.created_at = now_datetime()
    log.last_attempt_at = now_datetime()
    log.insert(ignore_permissions=True)
    return log


def _update_projection_log(
    log, state: str, target_name: str | None, last_error: str | None
) -> None:
    log.state = state
    log.target_name = target_name
    log.last_error = last_error
    log.last_attempt_at = now_datetime()
    if state == "Succeeded":
        # A supported manual retry may recover a row after automatic retries
        # exhausted.  Clear obsolete scheduler/dead-letter evidence together
        # with the successful state so support views cannot report a recovered
        # projection as still requiring intervention.
        log.next_retry_at = None
        log.lease_token = None
        log.lease_expires_at = None
        log.dead_lettered_at = None
    log.save(ignore_permissions=True)


def _retry_projection_log(log_name: str) -> dict[str, Any]:
    # Serialize tablet, scheduler, and support retries for the same projection.
    # Target services are independently idempotent, but the row lock also keeps
    # retry counters and terminal evidence monotonic.
    frappe.db.sql(
        "SELECT name FROM `tabFB Projection Log` WHERE name = %s FOR UPDATE",
        (log_name,),
    )
    log = frappe.get_doc("FB Projection Log", log_name)
    config = _get_projection_config(cstr(log.source_doctype), cstr(log.projection_type))
    if config is None:
        frappe.throw(
            f"No projection handler is configured for {log.source_doctype} {log.projection_type}",
            frappe.ValidationError,
        )
        raise AssertionError("unreachable")

    current_state = cstr(getattr(log, "state", None))
    if current_state == "Succeeded":
        return {
            "projection_log": log.name,
            "projection_type": cstr(log.projection_type),
            "state": "Succeeded",
            "target_name": cstr(getattr(log, "target_name", None)) or None,
        }
    if current_state != "Failed":
        frappe.throw(
            f"Projection {log.name} cannot be retried from state {current_state or 'missing'}",
            frappe.ValidationError,
        )
        raise AssertionError("unreachable")

    source_doc = frappe.get_doc(cstr(log.source_doctype), cstr(log.source_name))

    log.retry_count = cint(log.retry_count) + 1
    log.last_attempt_at = now_datetime()
    log.save(ignore_permissions=True)

    result = _retry_projection_target(source_doc, cstr(log.source_doctype), config, log)
    reload_doc = getattr(source_doc, "reload", None)
    if callable(reload_doc):
        reload_doc()
    if cstr(log.source_doctype) == "FB Order":
        source_doc.invoice_status = _derive_projection_field_status(
            source_doc, "Sales Invoice"
        )
        source_doc.stock_status = _derive_projection_field_status(
            source_doc, "Stock Issue"
        )
        source_doc.db_set(
            "invoice_status", source_doc.invoice_status, update_modified=False
        )
        source_doc.db_set(
            "stock_status", source_doc.stock_status, update_modified=False
        )

    return {
        "projection_log": log.name,
        "projection_type": result["projection_type"],
        "state": result["state"],
        "target_name": result["target_name"],
    }


def _retry_projection_target(
    source_doc,
    source_doctype: str,
    config: dict[str, str],
    log,
) -> dict[str, Any]:
    projection_type = config["projection_type"]
    try:
        target_name = _run_projection_handler(source_doc, source_doctype, projection_type)
    except Exception as error:
        _update_projection_log(log, "Failed", None, str(error))
        return {
            "projection_type": projection_type,
            "state": "Failed",
            "target_name": None,
            "log": log.name,
        }

    if target_name:
        _update_projection_log(log, "Succeeded", target_name, None)
        return {
            "projection_type": projection_type,
            "state": "Succeeded",
            "target_name": target_name,
            "log": log.name,
        }

    if _projection_can_remain_pending(source_doc, source_doctype, projection_type):
        # There is no stock target to create; this is a completed no-op.
        _update_projection_log(log, "Succeeded", None, None)
        return {
            "projection_type": projection_type,
            "state": "Succeeded",
            "target_name": None,
            "log": log.name,
        }

    error_message = f"{projection_type} projection retry did not create a target document"
    _update_projection_log(log, "Failed", None, error_message)
    return {
        "projection_type": projection_type,
        "state": "Failed",
        "target_name": None,
        "log": log.name,
    }


def _run_projection_handler(
    source_doc,
    source_doctype: str,
    projection_type: str,
) -> str | None:
    if source_doctype == "FB Order" and projection_type == "Sales Invoice":
        return _create_sales_invoice_projection(source_doc)
    if source_doctype == "FB Order" and projection_type == "Stock Issue":
        resolved_sales = _get_order_resolved_sales(source_doc)
        if not _order_requires_stock_projection(source_doc, resolved_sales):
            return None
        return _create_stock_issue_projection(source_doc, resolved_sales)
    if source_doctype == "FB Order" and projection_type == "FB Shift":
        _refresh_order_shift_projection(source_doc)
        return cstr(getattr(source_doc, "shift", None)) or None
    return _get_existing_projection_target(source_doc, projection_type)


def _create_sales_invoice_projection(source_doc) -> str | None:
    service_module = importlib.import_module(
        "kopos_connector.kopos.services.accounting.sales_invoice_service"
    )

    return cstr(service_module.create_sales_invoice(source_doc)) or None


def _create_stock_issue_projection(source_doc, resolved_sales: list[Any]) -> str | None:
    service_module = importlib.import_module(
        "kopos_connector.kopos.services.inventory.stock_issue_service"
    )

    return cstr(
        service_module.create_ingredient_stock_entry(source_doc, resolved_sales)
    ) or None


def _get_order_resolved_sales(source_doc) -> list[Any]:
    get_resolved_sales = getattr(source_doc, "get_resolved_sales", None)
    if callable(get_resolved_sales):
        resolved_sales = get_resolved_sales()
        if isinstance(resolved_sales, list):
            return resolved_sales
        return []

    resolved_sales = []
    for line in list(getattr(source_doc, "items", None) or []):
        resolved_sale_name = cstr(getattr(line, "resolved_sale", None))
        if not resolved_sale_name:
            continue
        resolved_sales.append(frappe.get_doc("FB Resolved Sale", resolved_sale_name))
    return resolved_sales


def _order_requires_stock_projection(source_doc, resolved_sales: list[Any]) -> bool:
    requires_stock_projection = getattr(source_doc, "requires_stock_projection", None)
    if callable(requires_stock_projection):
        return bool(requires_stock_projection(resolved_sales))
    return any(
        _resolved_sale_affects_stock(resolved_sale) for resolved_sale in resolved_sales
    )


def _resolved_sale_affects_stock(resolved_sale: Any) -> bool:
    for component in list(getattr(resolved_sale, "resolved_components", None) or []):
        if not cint(getattr(component, "affects_stock", 0)):
            continue
        item = cstr(getattr(component, "item", None))
        warehouse = cstr(
            getattr(component, "warehouse", None)
            or getattr(resolved_sale, "booth_warehouse", None)
        )
        qty = flt(
            getattr(component, "stock_qty", None) or getattr(component, "qty", None)
        )
        if item and warehouse and qty > 0:
            return True
    return False


def _refresh_order_shift_projection(source_doc) -> None:
    update_shift_expected_cash = getattr(source_doc, "update_shift_expected_cash", None)
    if callable(update_shift_expected_cash):
        update_shift_expected_cash()
        return
    shift = cstr(getattr(source_doc, "shift", None))
    if not shift:
        frappe.throw("FB Order has no shift to refresh", frappe.ValidationError)
    frappe.get_doc("FB Shift", shift)


def _get_existing_projection_target(source_doc, projection_type: str) -> str | None:
    config = _get_projection_config(
        cstr(getattr(source_doc, "doctype", None)), projection_type
    )
    if not config:
        return None
    target_name = cstr(getattr(source_doc, config["target_field"], None))
    if target_name and frappe.db.exists(config["target_doctype"], target_name):
        return target_name
    return None


def _projection_can_remain_pending(
    source_doc,
    source_doctype: str,
    projection_type: str,
) -> bool:
    if source_doctype != "FB Order" or projection_type != "Stock Issue":
        return False
    return not _order_requires_stock_projection(
        source_doc, _get_order_resolved_sales(source_doc)
    )


def _get_projection_config(
    source_doctype: str, projection_type: str
) -> dict[str, str] | None:
    config_map = {
        "FB Order": ORDER_RETRY_PROJECTION_CONFIG,
        "FB Return Event": RETURN_PROJECTION_CONFIG,
        "FB Remake Event": REMAKE_PROJECTION_CONFIG,
    }
    for entry in config_map.get(source_doctype, ()):
        if entry["projection_type"] == projection_type:
            return entry
    return None


def _get_projection_statuses(
    source_doctype: str, source_name: str
) -> list[dict[str, Any]]:
    rows = frappe.get_all(
        "FB Projection Log",
        filters={"source_doctype": source_doctype, "source_name": source_name},
        fields=[
            "name",
            "projection_type",
            "state",
            "target_doctype",
            "target_name",
            "idempotency_key",
            "retry_count",
            "last_error",
            "last_attempt_at",
        ],
        order_by="creation asc",
    )
    return [
        {
            "projection_log": cstr(row.name),
            "projection_type": cstr(row.projection_type),
            "state": cstr(row.state),
            "target_doctype": cstr(row.target_doctype) or None,
            "target_name": cstr(row.target_name) or None,
            "idempotency_key": cstr(row.idempotency_key) or None,
            "retry_count": cint(row.retry_count),
            "last_error": cstr(row.last_error) or None,
            "last_attempt_at": row.last_attempt_at.isoformat()
            if getattr(row, "last_attempt_at", None)
            else None,
        }
        for row in rows
    ]


def _resolve_order_status(results: list[dict[str, Any]], projection_type: str) -> str:
    relevant = [row for row in results if row["projection_type"] == projection_type]
    if not relevant:
        return "Pending"
    states = {row["state"] for row in relevant}
    if "Failed" in states:
        return "Failed"
    if states == {"Succeeded"}:
        return "Posted"
    return "Pending"


def _derive_projection_field_status(doc, projection_type: str) -> str:
    rows = frappe.get_all(
        "FB Projection Log",
        filters={
            "source_doctype": "FB Order",
            "source_name": doc.name,
            "projection_type": projection_type,
        },
        fields=["state"],
    )
    states = {cstr(row.state) for row in rows}
    if "Failed" in states:
        return "Failed"
    if states == {"Succeeded"} and states:
        return "Posted"
    return "Pending"


def _make_projection_id(
    source_doctype: str, source_name: str, projection_type: str
) -> str:
    source_prefix = source_doctype.upper().replace(" ", "-")
    projection_prefix = projection_type.upper().replace(" ", "-")
    return f"{source_prefix}-{source_name}-{projection_prefix}"


def _build_payload_hash(doc, projection_type: str) -> str:
    payload = json.dumps(
        {
            "source_doctype": doc.doctype,
            "source_name": doc.name,
            "projection_type": projection_type,
            "status": cstr(getattr(doc, "status", None)),
        },
        sort_keys=True,
        default=str,
    )
    return frappe.generate_hash(payload, 16)


def _require_doc(doctype: str, name: str, field_label: str) -> None:
    if not frappe.db.exists(doctype, name):
        frappe.throw(f"{field_label} {name} was not found", frappe.ValidationError)


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def _get_required_fb_modifier_doc(doctype: str, name: str, field_label: str):
    identifier = cstr(name)
    if _looks_like_legacy_kopos_modifier_identifier(identifier):
        frappe.throw(
            f"{field_label} {identifier} is a legacy KoPOS modifier id; submit FB-only modifier ids",
            frappe.ValidationError,
        )
    if not frappe.db.exists(doctype, identifier):
        frappe.throw(
            f"{field_label} {identifier} was not found in {doctype}; submit FB-only modifier ids because legacy KoPOS modifier ids are not supported",
            frappe.ValidationError,
        )
    return frappe.get_cached_doc(doctype, identifier)


def _looks_like_legacy_kopos_modifier_identifier(value: str) -> bool:
    normalized_value = cstr(value).strip().upper().replace("_", "-")
    return normalized_value.startswith("KOPOS-")


def _resolve_fb_shift_name(value: str) -> str | None:
    if frappe.db.exists("FB Shift", value):
        return value
    return frappe.db.get_value("FB Shift", {"shift_code": value}, "name")


def _validate_submit_shift(
    *, shift_name: str, device_id: str, staff_id: str, lock: bool = False
) -> Any:
    locked_rows: list[Any] = []
    if lock:
        locked_rows = frappe.db.sql(
            """
            SELECT name, device_id, staff_id, status, opened_at, closed_at
            FROM `tabFB Shift`
            WHERE name = %s
            FOR UPDATE
            """,
            (shift_name,),
            as_dict=True,
        )
    shift_doc = frappe.get_doc("FB Shift", shift_name)
    if locked_rows:
        for fieldname in (
            "device_id",
            "staff_id",
            "status",
            "opened_at",
            "closed_at",
        ):
            locked_value = _row_value(locked_rows[0], fieldname)
            if locked_value is not None:
                setattr(shift_doc, fieldname, locked_value)
    shift_device_id = cstr(getattr(shift_doc, "device_id", None))
    shift_staff_id = cstr(getattr(shift_doc, "staff_id", None))
    shift_status = cstr(getattr(shift_doc, "status", None))

    if shift_device_id != device_id:
        frappe.throw(
            f"shift {shift_name} does not belong to device {device_id}",
            frappe.ValidationError,
        )
    if shift_staff_id != staff_id:
        frappe.throw(
            f"shift {shift_name} does not belong to staff {staff_id}",
            frappe.ValidationError,
        )
    if shift_status != "Open":
        frappe.throw(
            f"shift {shift_name} is {shift_status or 'missing'}; new FB Orders require an Open FB Shift",
            frappe.ValidationError,
        )
    return shift_doc
