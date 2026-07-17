# pyright: reportMissingImports=false

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import Any

frappe = import_module("frappe")
frappe_utils = import_module("frappe.utils")

cstr = frappe_utils.cstr
cint = frappe_utils.cint
get_datetime = frappe_utils.get_datetime
now_datetime = frappe_utils.now_datetime

MAYBANK_PROVIDER = "maybank_qr"
MAYBANK_PAYMENT_CHANNELS = {"maybank", "maybank qr"}
MAYBANK_MODE_OF_PAYMENT = "duitnow qr"
STATIC_QR_PAYMENT_CHANNEL = "static qr"
QR_PAYMENT_CHANNELS = MAYBANK_PAYMENT_CHANNELS | {STATIC_QR_PAYMENT_CHANNEL}
# Retained in smoke evidence for backwards-compatible observation diagnostics.
# This is not settlement authority: ``paid_at`` is the first time ERP observed
# provider-paid state and can legitimately be later after a network/ERP outage.
PAYMENT_EXPIRY_GRACE_SECONDS = 30


def normalize_qr_token(value: Any) -> str:
    """Canonicalize QR payment method/channel separators consistently."""

    return " ".join(
        cstr(value)
        .strip()
        .lower()
        .replace("_", " ")
        .replace("-", " ")
        .split()
    )


def resolve_verified_qr_settlement_account(
    mode_of_payment: Any,
    company: Any,
    currency: Any,
) -> dict[str, str]:
    """Resolve a verified QR tender only to a non-cash Asset ledger."""

    resolved_mode = cstr(mode_of_payment).strip()
    resolved_company = cstr(company).strip()
    resolved_currency = cstr(currency).strip().upper()
    if not resolved_mode or not resolved_company or not resolved_currency:
        frappe.throw(
            "Verified QR settlement requires payment method, company, and currency",
            frappe.ValidationError,
        )

    try:
        sales_invoice_module = import_module(
            "erpnext.accounts.doctype.sales_invoice.sales_invoice"
        )
        get_mode_of_payment_info = getattr(
            sales_invoice_module,
            "get_mode_of_payment_info",
        )
        mode_rows = list(
            get_mode_of_payment_info(resolved_mode, resolved_company) or []
        )
    except Exception as error:
        frappe.throw(
            "ERPNext verified QR payment-account resolver is unavailable",
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error

    if len(mode_rows) != 1:
        frappe.throw(
            f"Mode of Payment {resolved_mode} must have exactly one account for company {resolved_company}",
            frappe.ValidationError,
        )
    mode_row = mode_rows[0]
    account = cstr(
        _value(mode_row, "account") or _value(mode_row, "default_account")
    ).strip()
    mode_type = cstr(_value(mode_row, "type")).strip()
    if not account:
        frappe.throw(
            f"Mode of Payment {resolved_mode} has no ledger account for company {resolved_company}",
            frappe.ValidationError,
        )
    if mode_type.lower() != "bank":
        frappe.throw(
            f"Verified QR Mode of Payment {resolved_mode} must use type Bank",
            frappe.ValidationError,
        )

    account_row = frappe.db.get_value(
        "Account",
        account,
        [
            "name",
            "account_type",
            "root_type",
            "is_group",
            "disabled",
            "company",
            "account_currency",
        ],
        as_dict=True,
    )
    if not account_row:
        frappe.throw(
            f"Verified QR settlement account {account} was not found",
            frappe.ValidationError,
        )
    if (
        cint(_value(account_row, "is_group"))
        or cint(_value(account_row, "disabled"))
        or cstr(_value(account_row, "company")).strip() != resolved_company
        or cstr(_value(account_row, "root_type")).strip() != "Asset"
    ):
        frappe.throw(
            f"Verified QR settlement account {account} must be an enabled non-group Asset account for company {resolved_company}",
            frappe.ValidationError,
        )
    account_type = cstr(_value(account_row, "account_type")).strip()
    if account_type not in {"", "Bank"}:
        frappe.throw(
            f"Verified QR settlement account {account} must be Bank or an untyped Asset clearing account; {account_type or 'unknown'} is not allowed",
            frappe.ValidationError,
        )
    account_currency = cstr(_value(account_row, "account_currency")).strip().upper()
    if account_currency and account_currency != resolved_currency:
        frappe.throw(
            f"Verified QR settlement account {account} currency does not match {resolved_currency}",
            frappe.ValidationError,
        )
    return {"account": account, "type": mode_type}


def register_qr_payment_settlement(order_doc: Any) -> str | None:
    """Register the verified or pending-reconciliation state for one QR payment."""
    payment, channel = _single_qr_payment(order_doc)
    if payment is None:
        return None
    if channel == "maybank":
        if _is_manual_confirmation(payment):
            return _register_manual_maybank_reconciliation(order_doc, payment)
        return _claim_payment_transaction(order_doc, payment)
    return _register_manual_qr_reconciliation(order_doc, payment)


def claim_paid_maybank_transaction(order_doc: Any) -> str | None:
    """Claim the paid Maybank transaction used by an FB Order under a row lock."""
    payment = _automatic_maybank_payment(order_doc)
    if payment is None:
        return None

    return _claim_payment_transaction(order_doc, payment)


def bind_qr_payment_settlement(
    order_doc: Any, sales_invoice_name: str
) -> str | None:
    """Bind a verified Maybank claim or manual QR suspense record to an invoice."""
    payment, channel = _single_qr_payment(order_doc)
    if payment is None:
        return None
    if channel == "maybank":
        if _is_manual_confirmation(payment):
            return _bind_manual_maybank_reconciliation(
                order_doc,
                payment,
                sales_invoice_name,
            )
        return bind_claimed_maybank_transaction(order_doc, sales_invoice_name)
    return _bind_manual_qr_reconciliation(order_doc, payment, sales_invoice_name)


def _claim_payment_transaction(
    order_doc: Any,
    payment: Any,
    *,
    expected_sales_invoice: str | None = None,
) -> str:
    """Claim a validated payment, optionally recovering an existing invoice bind."""

    transaction_refno = _payment_reference(payment)
    transaction = _load_transaction_for_update(
        "transaction_refno", transaction_refno
    )
    if transaction is None:
        frappe.throw(
            "Maybank QR transaction reference was not found",
            frappe.ValidationError,
        )

    _validate_transaction_for_order(
        transaction,
        order_doc,
        payment,
        expected_sales_invoice=expected_sales_invoice,
    )

    order_name = cstr(_value(order_doc, "name")).strip()
    if not order_name:
        frappe.throw(
            "FB Order must be inserted before claiming a Maybank QR transaction",
            frappe.ValidationError,
        )

    updates: dict[str, Any] = {
        "fb_order": order_name,
        "consumption_key": order_name,
    }
    if not _value(transaction, "consumed_at"):
        updates["consumed_at"] = now_datetime()
    frappe.db.set_value("Maybank QR Transaction", _value(transaction, "name"), updates)
    _set_value(payment, "maybank_qr_transaction", _value(transaction, "name"))
    return cstr(_value(transaction, "name"))


def bind_claimed_maybank_transaction(
    order_doc: Any, sales_invoice_name: str
) -> str | None:
    """Bind an order's claimed Maybank transaction to exactly one Sales Invoice."""
    payment = _automatic_maybank_payment(order_doc)
    if payment is None:
        return None

    transaction_name = cstr(
        _value(payment, "maybank_qr_transaction")
    ).strip()
    if not transaction_name:
        transaction_name = _claim_payment_transaction(
            order_doc,
            payment,
            expected_sales_invoice=sales_invoice_name,
        )

    transaction = _load_transaction_for_update("name", transaction_name)
    if transaction is None:
        frappe.throw(
            "Claimed Maybank QR transaction was not found",
            frappe.ValidationError,
        )

    _validate_transaction_for_order(
        transaction,
        order_doc,
        payment,
        expected_sales_invoice=sales_invoice_name,
    )

    order_name = cstr(_value(order_doc, "name")).strip()
    linked_order = cstr(_value(transaction, "fb_order")).strip()
    consumption_key = cstr(_value(transaction, "consumption_key")).strip()
    if linked_order != order_name or consumption_key != order_name:
        frappe.throw(
            "Maybank QR transaction is not claimed by this FB Order",
            frappe.ValidationError,
        )

    invoice_name = cstr(sales_invoice_name).strip()
    if not invoice_name:
        frappe.throw("Sales Invoice name is required", frappe.ValidationError)

    linked_invoice = cstr(_value(transaction, "sales_invoice")).strip()
    invoice_key = cstr(_value(transaction, "invoice_consumption_key")).strip()
    if linked_invoice and linked_invoice != invoice_name:
        frappe.throw(
            "Maybank QR transaction is already bound to another Sales Invoice",
            frappe.ValidationError,
        )
    if invoice_key and invoice_key != invoice_name:
        frappe.throw(
            "Maybank QR transaction invoice claim has already been consumed",
            frappe.ValidationError,
        )

    frappe.db.set_value(
        "Maybank QR Transaction",
        _value(transaction, "name"),
        {
            "sales_invoice": invoice_name,
            "invoice_consumption_key": invoice_name,
        },
    )
    return cstr(_value(transaction, "name"))


def _register_manual_maybank_reconciliation(
    order_doc: Any,
    payment: Any,
) -> str:
    """Claim an issued Maybank QR without claiming provider-paid settlement."""

    evidence = _manual_confirmation_evidence(payment)
    order_name = cstr(_value(order_doc, "name")).strip()
    payment_row_name = cstr(_value(payment, "name")).strip()
    if not order_name or not payment_row_name:
        frappe.throw(
            "FB Order and payment row must be inserted before Maybank QR manual reconciliation",
            frappe.ValidationError,
        )

    transaction_refno = _payment_reference(payment)
    transaction = _load_transaction_for_update(
        "transaction_refno",
        transaction_refno,
    )
    if transaction is None:
        frappe.throw(
            "Issued Maybank QR transaction reference was not found",
            frappe.ValidationError,
        )

    _validate_transaction_for_order(
        transaction,
        order_doc,
        payment,
        require_provider_paid=False,
    )
    _validate_manual_evidence_identity(
        evidence,
        order_doc=order_doc,
        payment=payment,
        channel="maybank",
    )

    reconciliation_key = cstr(
        _value(payment, "reconciliation_idempotency_key")
        or evidence.get("reconciliation_idempotency_key")
    ).strip()
    if not reconciliation_key:
        frappe.throw(
            "Maybank QR reconciliation_idempotency_key is required",
            frappe.ValidationError,
        )
    existing_key = cstr(
        _value(transaction, "reconciliation_idempotency_key")
    ).strip()
    if existing_key and existing_key != reconciliation_key:
        frappe.throw(
            "Maybank QR transaction is already bound to another reconciliation attempt",
            frappe.ValidationError,
        )
    existing_payment_row = cstr(
        _value(transaction, "fb_order_payment")
    ).strip()
    if existing_payment_row and existing_payment_row != payment_row_name:
        frappe.throw(
            "Maybank QR transaction is already bound to another FB Order payment",
            frappe.ValidationError,
        )

    settlement_status = cstr(
        _value(transaction, "manual_reconciliation_status")
    ).strip()
    if settlement_status not in {
        "",
        "pending_reconciliation",
        "reconciled",
        "reconciliation_failed",
    }:
        frappe.throw(
            "Maybank QR transaction manual reconciliation state is invalid",
            frappe.ValidationError,
        )

    suspense_account = resolve_manual_qr_suspense_account(order_doc)
    updates: dict[str, Any] = {
        "fb_order": order_name,
        "consumption_key": order_name,
        "fb_order_payment": payment_row_name,
        "reconciliation_idempotency_key": reconciliation_key,
    }
    if not settlement_status:
        settlement_status = "pending_reconciliation"
        updates["manual_reconciliation_status"] = settlement_status
    if not _value(transaction, "consumed_at"):
        updates["consumed_at"] = now_datetime()
    frappe.db.set_value(
        "Maybank QR Transaction",
        _value(transaction, "name"),
        updates,
    )
    _set_value(payment, "maybank_qr_transaction", _value(transaction, "name"))
    _set_value(payment, "settlement_status", settlement_status)
    _set_value(payment, "suspense_account", suspense_account)
    return cstr(_value(transaction, "name"))


def _bind_manual_maybank_reconciliation(
    order_doc: Any,
    payment: Any,
    sales_invoice_name: str,
) -> str:
    """Bind a pending Maybank reconciliation to its one Sales Invoice."""

    invoice_name = cstr(sales_invoice_name).strip()
    if not invoice_name:
        frappe.throw("Sales Invoice name is required", frappe.ValidationError)

    transaction_name = cstr(
        _value(payment, "maybank_qr_transaction")
    ).strip()
    if not transaction_name:
        transaction_name = _register_manual_maybank_reconciliation(
            order_doc,
            payment,
        )

    transaction = _load_transaction_for_update("name", transaction_name)
    if transaction is None:
        frappe.throw(
            "Pending-reconciliation Maybank QR transaction was not found",
            frappe.ValidationError,
        )
    _validate_transaction_for_order(
        transaction,
        order_doc,
        payment,
        expected_sales_invoice=invoice_name,
        require_provider_paid=False,
    )

    order_name = cstr(_value(order_doc, "name")).strip()
    payment_row_name = cstr(_value(payment, "name")).strip()
    if (
        cstr(_value(transaction, "fb_order")).strip() != order_name
        or cstr(_value(transaction, "consumption_key")).strip() != order_name
        or cstr(_value(transaction, "fb_order_payment")).strip()
        != payment_row_name
    ):
        frappe.throw(
            "Maybank QR transaction is not bound to this FB Order payment",
            frappe.ValidationError,
        )

    linked_invoice = cstr(_value(transaction, "sales_invoice")).strip()
    invoice_key = cstr(
        _value(transaction, "invoice_consumption_key")
    ).strip()
    if linked_invoice and linked_invoice != invoice_name:
        frappe.throw(
            "Maybank QR transaction is already bound to another Sales Invoice",
            frappe.ValidationError,
        )
    if invoice_key and invoice_key != invoice_name:
        frappe.throw(
            "Maybank QR transaction invoice claim has already been consumed",
            frappe.ValidationError,
        )
    frappe.db.set_value(
        "Maybank QR Transaction",
        _value(transaction, "name"),
        {
            "sales_invoice": invoice_name,
            "invoice_consumption_key": invoice_name,
        },
    )
    return cstr(_value(transaction, "name"))


def is_automatic_maybank_payment(payment: Any) -> bool:
    mode = _normalized(_value(payment, "payment_method"))
    channel = _normalized(_value(payment, "payment_channel_code"))
    return mode == MAYBANK_MODE_OF_PAYMENT and channel in MAYBANK_PAYMENT_CHANNELS


def money_to_sen(value: Any, fieldname: str) -> int:
    """Convert an exact decimal money value to integer sen without float rounding."""
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        frappe.throw(f"{fieldname} must be a valid decimal amount", frappe.ValidationError)
        return 0

    if not amount.is_finite():
        frappe.throw(f"{fieldname} must be finite", frappe.ValidationError)
    sen = amount * Decimal("100")
    if sen != sen.to_integral_value():
        frappe.throw(
            f"{fieldname} must not contain fractional sen",
            frappe.ValidationError,
        )
    return int(sen)


def _automatic_maybank_payment(order_doc: Any) -> Any | None:
    payment, channel = _single_qr_payment(order_doc)
    return payment if channel == "maybank" else None


def _single_qr_payment(order_doc: Any) -> tuple[Any | None, str | None]:
    payments = [
        payment
        for payment in list(_value(order_doc, "payments") or [])
        if _normalized(_value(payment, "payment_method")) == MAYBANK_MODE_OF_PAYMENT
        or _normalized(_value(payment, "payment_channel_code"))
        in QR_PAYMENT_CHANNELS
    ]
    if len(payments) > 1:
        frappe.throw(
            "FB Order may contain only one DuitNow QR payment",
            frappe.ValidationError,
        )
    if not payments:
        return None, None

    payment = payments[0]
    if _normalized(_value(payment, "payment_method")) != MAYBANK_MODE_OF_PAYMENT:
        frappe.throw(
            "QR payment_channel_code requires the DuitNow QR payment method",
            frappe.ValidationError,
        )
    channel = _normalized(_value(payment, "payment_channel_code"))
    if channel in MAYBANK_PAYMENT_CHANNELS:
        return payment, "maybank"
    if channel == STATIC_QR_PAYMENT_CHANNEL:
        return payment, "static_qr"
    frappe.throw(
        "DuitNow QR payment_channel_code is unsupported or ambiguous",
        frappe.ValidationError,
    )
    return None, None


def _register_manual_qr_reconciliation(order_doc: Any, payment: Any) -> str:
    evidence = _manual_confirmation_evidence(payment)
    order_name = cstr(_value(order_doc, "name")).strip()
    payment_row_name = cstr(_value(payment, "name")).strip()
    if not order_name or not payment_row_name:
        frappe.throw(
            "FB Order and payment row must be inserted before static QR registration",
            frappe.ValidationError,
        )

    provider_session_id = cstr(
        _value(payment, "external_transaction_id")
    ).strip()
    if not provider_session_id.startswith("static-"):
        frappe.throw(
            "static_qr external_transaction_id must use the static session namespace",
            frappe.ValidationError,
        )
    payment_reference = cstr(_value(payment, "reference_no")).strip()
    if not payment_reference:
        frappe.throw("static_qr reference_no is required", frappe.ValidationError)
    _reject_maybank_reference(provider_session_id)
    _reject_maybank_reference(payment_reference)

    order_device = cstr(_value(order_doc, "device_id")).strip()
    if cstr(evidence.get("evidence_captured_device_id")).strip() != order_device:
        frappe.throw(
            "static_qr evidence device does not match FB Order",
            frappe.ValidationError,
        )
    order_staff = cstr(_value(order_doc, "staff_id")).strip()
    if cstr(evidence.get("local_confirmed_by")).strip() != order_staff:
        frappe.throw(
            "static_qr evidence confirmer does not match FB Order staff",
            frappe.ValidationError,
        )

    suspense_account = resolve_manual_qr_suspense_account(order_doc)
    amount_sen = money_to_sen(_value(payment, "amount"), "static_qr payment amount")
    idempotency_key = cstr(
        _value(payment, "reconciliation_idempotency_key")
        or evidence.get("reconciliation_idempotency_key")
    ).strip()
    if not idempotency_key:
        frappe.throw(
            "static_qr reconciliation_idempotency_key is required",
            frappe.ValidationError,
        )

    existing = _load_manual_reconciliation_for_update(
        "reconciliation_idempotency_key", idempotency_key
    )
    if existing:
        _validate_existing_manual_reconciliation(
            existing,
            order_doc=order_doc,
            payment=payment,
            amount_sen=amount_sen,
            suspense_account=suspense_account,
        )
        _apply_manual_reconciliation_to_payment(payment, existing)
        return cstr(_value(existing, "name"))

    sale_datetime = get_datetime(
        _value(order_doc, "sale_datetime") or now_datetime()
    )
    reconciliation = frappe.get_doc(
        {
            "doctype": "Manual QR Reconciliation",
            "status": "pending_reconciliation",
            "fb_order": order_name,
            "fb_order_payment": payment_row_name,
            "device_id": order_device,
            "staff_id": order_staff,
            "company": cstr(_value(order_doc, "company")).strip(),
            "currency": cstr(_value(order_doc, "currency")).strip().upper(),
            "business_date": sale_datetime.date().isoformat(),
            "amount_sen": amount_sen,
            "payment_reference": payment_reference,
            "provider_session_id": provider_session_id,
            "reconciliation_idempotency_key": idempotency_key,
            "suspense_account": suspense_account,
            "evidence_kind": cstr(evidence.get("evidence_kind")).strip(),
            "evidence_captured_at": get_datetime(evidence.get("captured_at")),
            "evidence_json": json.dumps(
                evidence, sort_keys=True, separators=(",", ":")
            ),
            "created_at": now_datetime(),
        }
    )
    try:
        reconciliation.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        existing = _load_manual_reconciliation_for_update(
            "reconciliation_idempotency_key", idempotency_key
        )
        if not existing:
            raise
        _validate_existing_manual_reconciliation(
            existing,
            order_doc=order_doc,
            payment=payment,
            amount_sen=amount_sen,
            suspense_account=suspense_account,
        )
        _apply_manual_reconciliation_to_payment(payment, existing)
        return cstr(_value(existing, "name"))

    _apply_manual_reconciliation_to_payment(payment, reconciliation)
    return cstr(_value(reconciliation, "name"))


def _bind_manual_qr_reconciliation(
    order_doc: Any, payment: Any, sales_invoice_name: str
) -> str:
    invoice_name = cstr(sales_invoice_name).strip()
    if not invoice_name:
        frappe.throw("Sales Invoice name is required", frappe.ValidationError)

    reconciliation_name = cstr(
        _value(payment, "manual_qr_reconciliation")
    ).strip()
    if not reconciliation_name:
        reconciliation_name = _register_manual_qr_reconciliation(order_doc, payment)

    reconciliation = _load_manual_reconciliation_for_update(
        "name", reconciliation_name
    )
    if not reconciliation:
        frappe.throw(
            "Manual QR Reconciliation was not found",
            frappe.ValidationError,
        )
    if cstr(_value(reconciliation, "fb_order")).strip() != cstr(
        _value(order_doc, "name")
    ).strip():
        frappe.throw(
            "Manual QR Reconciliation belongs to another FB Order",
            frappe.ValidationError,
        )
    linked_invoice = cstr(_value(reconciliation, "sales_invoice")).strip()
    if linked_invoice and linked_invoice != invoice_name:
        frappe.throw(
            "Manual QR Reconciliation is already bound to another Sales Invoice",
            frappe.ValidationError,
        )
    frappe.db.set_value(
        "Manual QR Reconciliation",
        _value(reconciliation, "name"),
        "sales_invoice",
        invoice_name,
    )
    return cstr(_value(reconciliation, "name"))


def _manual_confirmation_evidence(payment: Any) -> dict[str, Any]:
    channel_label = (
        "maybank"
        if is_automatic_maybank_payment(payment)
        else "static_qr"
    )
    if not _is_manual_confirmation(payment):
        frappe.throw(
            f"{channel_label} must carry server-validated manual confirmation evidence",
            frappe.ValidationError,
        )
    raw_evidence = _value(payment, "manual_confirmation_evidence_json")
    try:
        evidence = json.loads(cstr(raw_evidence))
    except (TypeError, ValueError):
        frappe.throw(
            f"{channel_label} manual confirmation evidence is invalid",
            frappe.ValidationError,
        )
        return {}
    if not isinstance(evidence, Mapping):
        frappe.throw(
            f"{channel_label} manual confirmation evidence must be an object",
            frappe.ValidationError,
        )
    if cstr(evidence.get("reconciliation_status")).strip() != (
        "pending_reconciliation"
    ):
        frappe.throw(
            f"{channel_label} evidence must remain pending reconciliation",
            frappe.ValidationError,
        )
    required_fields = (
        "evidence_kind",
        "captured_at",
        "upload_status",
        "local_confirmed_at",
        "local_confirmed_by",
        "local_confirmation_reference",
        "reconciliation_idempotency_key",
        "evidence_captured_device_id",
    )
    for fieldname in required_fields:
        if not cstr(evidence.get(fieldname)).strip():
            frappe.throw(
                f"{channel_label} evidence {fieldname} is required",
                frappe.ValidationError,
            )
    for fieldname in ("captured_at", "local_confirmed_at"):
        try:
            get_datetime(evidence.get(fieldname))
        except Exception:
            frappe.throw(
                f"{channel_label} evidence {fieldname} is invalid",
                frappe.ValidationError,
            )
    if cstr(evidence.get("local_confirmation_reference")).strip() != cstr(
        _value(payment, "reference_no")
    ).strip():
        frappe.throw(
            f"{channel_label} evidence reference does not match payment",
            frappe.ValidationError,
        )
    if cstr(evidence.get("reconciliation_idempotency_key")).strip() != cstr(
        _value(payment, "reconciliation_idempotency_key")
    ).strip():
        frappe.throw(
            f"{channel_label} evidence idempotency key does not match payment",
            frappe.ValidationError,
        )
    evidence_kind = cstr(evidence.get("evidence_kind")).strip()
    upload_status = cstr(evidence.get("upload_status")).strip()
    if evidence_kind == "receipt_photo":
        local_uri = cstr(evidence.get("local_uri")).strip()
        if not local_uri or local_uri.lower().startswith("data:"):
            frappe.throw(
                f"{channel_label} receipt evidence local_uri is invalid",
                frappe.ValidationError,
            )
        if cstr(evidence.get("mime_type")).strip().lower() not in {
            "image/jpeg",
            "image/pjpeg",
        }:
            frappe.throw(
                f"{channel_label} receipt evidence mime_type is invalid",
                frappe.ValidationError,
            )
        if upload_status not in {
            "upload_pending",
            "uploading",
            "uploaded",
            "upload_failed",
        }:
            frappe.throw(
                f"{channel_label} receipt evidence upload_status is invalid",
                frappe.ValidationError,
            )
    elif evidence_kind == "no_receipt_acknowledgement":
        if evidence.get("no_receipt_acknowledged") is not True or not cstr(
            evidence.get("no_receipt_reason_code")
        ).strip():
            frappe.throw(
                f"{channel_label} no-receipt evidence is invalid",
                frappe.ValidationError,
            )
        if upload_status != "not_required":
            frappe.throw(
                f"{channel_label} no-receipt upload_status must be not_required",
                frappe.ValidationError,
            )
    elif evidence_kind == "reference":
        if upload_status not in {"not_required", "uploaded"}:
            frappe.throw(
                f"{channel_label} reference evidence upload_status is invalid",
                frappe.ValidationError,
            )
    else:
        frappe.throw(
            f"{channel_label} evidence_kind is invalid",
            frappe.ValidationError,
        )
    return dict(evidence)


def _validate_manual_evidence_identity(
    evidence: Mapping[str, Any],
    *,
    order_doc: Any,
    payment: Any,
    channel: str,
) -> None:
    order_device = cstr(_value(order_doc, "device_id")).strip()
    if cstr(evidence.get("evidence_captured_device_id")).strip() != order_device:
        frappe.throw(
            f"{channel} evidence device does not match FB Order",
            frappe.ValidationError,
        )
    order_staff = cstr(_value(order_doc, "staff_id")).strip()
    if cstr(evidence.get("local_confirmed_by")).strip() != order_staff:
        frappe.throw(
            f"{channel} evidence confirmer does not match FB Order staff",
            frappe.ValidationError,
        )
    if cstr(evidence.get("local_confirmation_reference")).strip() != cstr(
        _value(payment, "reference_no")
    ).strip():
        frappe.throw(
            f"{channel} evidence reference does not match payment",
            frappe.ValidationError,
        )


def resolve_manual_qr_suspense_account(order_doc: Any) -> str:
    """Return the configured QR suspense ledger after exact order-bound checks."""

    account = cstr(
        frappe.db.get_single_value(
            "Maybank Settings", "manual_qr_suspense_account"
        )
    ).strip()
    if not account:
        frappe.throw(
            "Maybank Settings manual_qr_suspense_account is required for manual QR confirmation",
            frappe.ValidationError,
        )
    account_row = frappe.db.get_value(
        "Account",
        account,
        ["company", "is_group", "disabled", "root_type", "account_currency"],
        as_dict=True,
    )
    if not account_row:
        frappe.throw("manual QR suspense account was not found", frappe.ValidationError)
    order_company = cstr(_value(order_doc, "company")).strip()
    if (
        not order_company
        or cstr(_value(account_row, "company")).strip() != order_company
    ):
        frappe.throw(
            "manual QR suspense account belongs to another company",
            frappe.ValidationError,
        )
    is_group = _strict_account_flag(_value(account_row, "is_group"))
    disabled = _strict_account_flag(_value(account_row, "disabled"))
    if is_group is None or disabled is None or is_group or disabled:
        frappe.throw(
            "manual QR suspense account must be an enabled ledger account",
            frappe.ValidationError,
        )
    if cstr(_value(account_row, "root_type")).strip() != "Asset":
        frappe.throw(
            "manual QR suspense account must be an Asset account",
            frappe.ValidationError,
        )
    account_currency = cstr(_value(account_row, "account_currency")).strip().upper()
    order_currency = cstr(_value(order_doc, "currency")).strip().upper()
    if not order_currency or account_currency != order_currency:
        frappe.throw(
            "manual QR suspense account currency does not match FB Order",
            frappe.ValidationError,
        )
    return account


def _strict_account_flag(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in {0, 1}:
        return value
    if isinstance(value, str) and value.strip() in {"0", "1"}:
        return int(value.strip())
    return None


def _reject_maybank_reference(reference: str) -> None:
    if frappe.db.exists(
        "Maybank QR Transaction", {"transaction_refno": reference}
    ):
        frappe.throw(
            "static_qr must not use a Maybank transaction reference",
            frappe.ValidationError,
        )


def _load_manual_reconciliation_for_update(
    fieldname: str, value: str
) -> Any | None:
    if fieldname not in {"name", "reconciliation_idempotency_key"}:
        raise ValueError("unsupported Manual QR Reconciliation lookup field")
    rows = frappe.db.sql(
        f"""
        SELECT
            name, status, fb_order, sales_invoice, fb_order_payment,
            device_id, company, currency, amount_sen, payment_reference,
            provider_session_id, reconciliation_idempotency_key,
            suspense_account
        FROM `tabManual QR Reconciliation`
        WHERE `{fieldname}` = %s
        LIMIT 1
        FOR UPDATE
        """,
        (value,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _validate_existing_manual_reconciliation(
    reconciliation: Any,
    *,
    order_doc: Any,
    payment: Any,
    amount_sen: int,
    suspense_account: str,
) -> None:
    expected = {
        "fb_order": cstr(_value(order_doc, "name")).strip(),
        "fb_order_payment": cstr(_value(payment, "name")).strip(),
        "device_id": cstr(_value(order_doc, "device_id")).strip(),
        "company": cstr(_value(order_doc, "company")).strip(),
        "currency": cstr(_value(order_doc, "currency")).strip().upper(),
        "payment_reference": cstr(_value(payment, "reference_no")).strip(),
        "provider_session_id": cstr(
            _value(payment, "external_transaction_id")
        ).strip(),
        "suspense_account": suspense_account,
    }
    for fieldname, expected_value in expected.items():
        actual = cstr(_value(reconciliation, fieldname)).strip()
        if fieldname == "currency":
            actual = actual.upper()
        if actual != expected_value:
            frappe.throw(
                f"Manual QR Reconciliation {fieldname} does not match",
                frappe.ValidationError,
            )
    if _strict_integer_sen(
        _value(reconciliation, "amount_sen"),
        "Manual QR Reconciliation amount_sen",
    ) != amount_sen:
        frappe.throw(
            "Manual QR Reconciliation amount does not match",
            frappe.ValidationError,
        )


def _apply_manual_reconciliation_to_payment(
    payment: Any, reconciliation: Any
) -> None:
    _set_value(payment, "manual_qr_reconciliation", _value(reconciliation, "name"))
    _set_value(payment, "settlement_status", "pending_reconciliation")
    _set_value(payment, "suspense_account", _value(reconciliation, "suspense_account"))


def _payment_reference(payment: Any) -> str:
    external_reference = cstr(
        _value(payment, "external_transaction_id")
    ).strip()
    receipt_reference = cstr(_value(payment, "reference_no")).strip()
    if not external_reference:
        frappe.throw(
            "Maybank QR payment external_transaction_id is required",
            frappe.ValidationError,
        )
    if receipt_reference and receipt_reference != external_reference:
        frappe.throw(
            "Maybank QR payment references do not match",
            frappe.ValidationError,
        )
    return external_reference


def _load_transaction_for_update(fieldname: str, value: str) -> Any | None:
    if fieldname not in {"name", "transaction_refno"}:
        raise ValueError("unsupported Maybank transaction lookup field")

    rows = frappe.db.sql(
        f"""
        SELECT
            name, transaction_refno, status, maybank_status, sale_amount_sen,
            device_id, outlet_id, company, currency, provider,
            expires_at, paid_at, fb_order, sales_invoice,
            consumption_key, invoice_consumption_key, consumed_at,
            qr_data, fb_order_payment, reconciliation_idempotency_key,
            manual_reconciliation_status
        FROM `tabMaybank QR Transaction`
        WHERE `{fieldname}` = %s
        LIMIT 1
        FOR UPDATE
        """,
        (value,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _validate_transaction_for_order(
    transaction: Any,
    order_doc: Any,
    payment: Any,
    *,
    expected_sales_invoice: str | None = None,
    require_provider_paid: bool = True,
) -> None:
    expected_reference = _payment_reference(payment)
    if cstr(_value(transaction, "transaction_refno")).strip() != expected_reference:
        frappe.throw(
            "Maybank QR transaction reference does not match payment",
            frappe.ValidationError,
        )

    transaction_status = cstr(_value(transaction, "status")).strip().lower()
    if require_provider_paid:
        if transaction_status != "paid":
            frappe.throw(
                "Maybank QR transaction is not paid",
                frappe.ValidationError,
            )
        if cstr(_value(transaction, "maybank_status")).strip() != "1":
            frappe.throw(
                "Maybank QR transaction lacks provider-paid status evidence",
                frappe.ValidationError,
            )
    else:
        if transaction_status not in {
            "creating",
            "pending",
            "scanned",
            "paid",
            "failed",
            "timeout",
            "unknown",
        } or not cstr(_value(transaction, "qr_data")).strip():
            frappe.throw(
                "Maybank QR transaction was not issued to the POS",
                frappe.ValidationError,
            )

    payment_amount_sen = money_to_sen(
        _value(payment, "amount"), "Maybank QR payment amount"
    )
    transaction_amount_sen = _strict_integer_sen(
        _value(transaction, "sale_amount_sen"),
        "Maybank QR transaction sale_amount_sen",
    )
    if transaction_amount_sen != payment_amount_sen:
        frappe.throw(
            "Maybank QR transaction amount does not match payment",
            frappe.ValidationError,
        )

    order_device = cstr(_value(order_doc, "device_id")).strip()
    transaction_device = cstr(_value(transaction, "device_id")).strip()
    if not order_device or transaction_device != order_device:
        frappe.throw(
            "Maybank QR transaction device does not match FB Order",
            frappe.ValidationError,
        )

    order_name = cstr(_value(order_doc, "name")).strip()
    order_company = cstr(_value(order_doc, "company")).strip()
    transaction_company = cstr(_value(transaction, "company")).strip()
    linked_order = cstr(_value(transaction, "fb_order")).strip()
    consumption_key = cstr(_value(transaction, "consumption_key")).strip()
    legacy_submitted_claim = bool(
        not transaction_company
        and order_company
        and cint(_value(order_doc, "docstatus")) == 1
        and linked_order == order_name
        and consumption_key == order_name
    )
    if (
        not order_company
        or (not transaction_company and not legacy_submitted_claim)
        or (transaction_company and transaction_company != order_company)
    ):
        frappe.throw(
            "Maybank QR transaction company does not match FB Order",
            frappe.ValidationError,
        )

    # The transaction reservation captures the provider outlet before the QR
    # network call, and provider status responses are verified against that
    # immutable value.  Comparing a historical payment with today's mutable
    # Maybank Settings would reject legitimate paid sales after an outlet
    # rotation.  New reservations still capture the current configured outlet.
    if not cstr(_value(transaction, "outlet_id")).strip():
        frappe.throw(
            "Maybank QR transaction outlet metadata is missing",
            frappe.ValidationError,
        )

    order_currency = cstr(_value(order_doc, "currency")).strip().upper()
    transaction_currency = cstr(_value(transaction, "currency")).strip().upper()
    if not order_currency or transaction_currency != order_currency:
        frappe.throw(
            "Maybank QR transaction currency does not match FB Order",
            frappe.ValidationError,
        )
    if transaction_currency != "MYR":
        frappe.throw(
            "Maybank QR transaction and FB Order currency must be MYR",
            frappe.ValidationError,
        )

    if cstr(_value(transaction, "provider")).strip().lower() != MAYBANK_PROVIDER:
        frappe.throw(
            "Maybank QR transaction provider is invalid",
            frappe.ValidationError,
        )

    expires_at_value = _value(transaction, "expires_at")
    paid_at_value = _value(transaction, "paid_at")
    if not expires_at_value or (require_provider_paid and not paid_at_value):
        frappe.throw(
            "Maybank QR transaction required timestamp is missing",
            frappe.ValidationError,
        )
    try:
        get_datetime(expires_at_value)
        if require_provider_paid:
            get_datetime(paid_at_value)
    except Exception as error:
        frappe.throw(
            "Maybank QR transaction required timestamp is invalid",
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error

    # A provider-authenticated ``paid`` result is monetary truth. ``paid_at``
    # records when ERP first observed that result, so comparing it with QR
    # display expiry would reject genuine payments whenever polling was delayed
    # by bad internet or an ERP outage. Preserve both timestamps for audit, but
    # never synthesize a local non-payment decision from observation latency.

    if linked_order and linked_order != order_name:
        frappe.throw(
            "Maybank QR transaction is already used by another FB Order",
            frappe.ValidationError,
        )
    if consumption_key and consumption_key != order_name:
        frappe.throw(
            "Maybank QR transaction claim has already been consumed",
            frappe.ValidationError,
        )

    expected_invoice = cstr(expected_sales_invoice).strip()
    linked_invoice = cstr(_value(transaction, "sales_invoice")).strip()
    invoice_key = cstr(_value(transaction, "invoice_consumption_key")).strip()
    if linked_invoice and linked_invoice != expected_invoice:
        frappe.throw(
            "Maybank QR transaction is already used by another Sales Invoice",
            frappe.ValidationError,
        )
    if invoice_key and invoice_key != expected_invoice:
        frappe.throw(
            "Maybank QR transaction invoice claim has already been consumed",
            frappe.ValidationError,
        )


def _strict_integer_sen(value: Any, fieldname: str) -> int:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        frappe.throw(f"{fieldname} must be an integer", frappe.ValidationError)
        return 0
    if not amount.is_finite() or amount != amount.to_integral_value():
        frappe.throw(f"{fieldname} must be an integer", frappe.ValidationError)
    return int(amount)


def _is_manual_confirmation(payment: Any) -> bool:
    value = _value(payment, "is_manual_confirmation")
    if isinstance(value, bool):
        return value
    return cstr(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalized(value: Any) -> str:
    return normalize_qr_token(value)


def _value(doc: Any, fieldname: str) -> Any:
    if isinstance(doc, dict):
        return doc.get(fieldname)
    getter = getattr(doc, "get", None)
    if callable(getter):
        value = getter(fieldname)
        if value is not None:
            return value
    return getattr(doc, fieldname, None)


def _set_value(doc: Any, fieldname: str, value: Any) -> None:
    if isinstance(doc, dict):
        doc[fieldname] = value
        return
    setattr(doc, fieldname, value)
