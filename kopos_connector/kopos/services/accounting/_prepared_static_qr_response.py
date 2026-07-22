# pyright: reportMissingImports=false
"""Response serialization for prepared-sale static QR finalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from frappe.utils import cstr

from kopos_connector.kopos.api.fb_orders import _build_submit_response


def build_secondary_claim_response(
    status: str,
    order_doc: Any,
    payment: Any,
    attempts: list[Any],
    static_claim: Any,
    winning_attempt: Any,
    *,
    confirmation_contract_version: str,
    maybank_provider: str,
    secondary_claim_role: str,
) -> dict[str, Any]:
    projection = _build_submit_response(status, order_doc)
    claim_status = cstr(_value(static_claim, "status")).strip()
    return {
        "status": status,
        "partial_failure": projection["partial_failure"],
        "confirmation_contract_version": confirmation_contract_version,
        "settlement_state": claim_status,
        "winner_channel": maybank_provider,
        "static_claim_role": secondary_claim_role,
        "static_claim_status": claim_status,
        "static_claim_finance_status": cstr(
            _value(static_claim, "finance_resolution_status")
        ).strip()
        or "pending_review",
        "static_claim_registered": True,
        "static_claim_is_sale_winner": False,
        "winning_maybank_qr_transaction": cstr(
            _value(winning_attempt, "name")
        ).strip(),
        "winning_payment_settlement_state": cstr(
            _value(payment, "settlement_status")
        ).strip(),
        "automatic_qr_state": "finalized",
        "fb_order": cstr(_value(order_doc, "name")).strip(),
        "fb_order_payment": cstr(_value(payment, "name")).strip(),
        "order_id": cstr(_value(order_doc, "order_id")).strip(),
        "idempotency_key": cstr(
            _value(order_doc, "external_idempotency_key")
        ).strip(),
        "accepted_sale_fingerprint": cstr(
            _value(order_doc, "accepted_sale_fingerprint")
        ).strip(),
        "payment_id": cstr(_value(payment, "source_payment_id")).strip(),
        "manual_qr_reconciliation": cstr(_value(static_claim, "name")).strip(),
        "reconciliation_idempotency_key": cstr(
            _value(static_claim, "reconciliation_idempotency_key")
        ).strip(),
        "sales_invoice": cstr(_value(order_doc, "sales_invoice")).strip() or None,
        "ingredient_stock_entry": cstr(
            _value(order_doc, "ingredient_stock_entry")
        ).strip()
        or None,
        "invoice_status": cstr(_value(order_doc, "invoice_status")).strip() or None,
        "stock_status": cstr(_value(order_doc, "stock_status")).strip() or None,
        "order_status": cstr(_value(order_doc, "status")).strip() or None,
        "sale_datetime": projection["sale_datetime"],
        "projection_status": projection["projection_status"],
        "failed_subsystem": projection["failed_subsystem"],
        "diagnostics": projection["diagnostics"],
        "message": projection["message"],
        "projections": projection["projections"],
        "maybank_attempt_count": len(attempts),
        "maybank_attempts_retained": True,
    }


def _value(document: Any, fieldname: str) -> Any:
    if isinstance(document, Mapping):
        return document.get(fieldname)
    getter = getattr(document, "get", None)
    if callable(getter):
        value = getter(fieldname)
        if value is not None:
            return value
    return getattr(document, fieldname, None)
