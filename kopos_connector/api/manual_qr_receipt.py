# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal, InvalidOperation
from datetime import datetime
from collections.abc import Iterable
from typing import Any, cast
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import (
    add_to_date,
    cint,
    cstr,
    get_datetime,
    get_system_timezone,
    getdate,
    now_datetime,
)

from kopos_connector.api.devices import (
    get_authenticated_device_doc,
    get_session_roles,
    lock_device_for_operational_mutation,
    require_device_context,
    require_kopos_api_access,
)
from kopos_connector.kopos.services.accounting.qr_reconciliation_service import (
    assert_qr_suspense_failure_reclassification,
    ensure_qr_suspense_failure_reclassification,
    ensure_qr_suspense_reclassification,
)
from kopos_connector.utils.diagnostics import log_sanitized_error, sanitized_error_message


DEFAULT_MAX_RECEIPT_BYTES = 5 * 1024 * 1024
JPEG_MIME_TYPES = {"image/jpeg", "image/pjpeg"}
JPEG_MAGIC = b"\xff\xd8\xff"
TRANSACTION_GRACE_SECONDS = 30
RECEIPT_UPLOAD_LOGGER = "kopos_manual_qr_receipt"
PENDING_RECONCILIATION_STATUS = "pending_reconciliation"
RECONCILED_STATUS = "reconciled"
RECONCILIATION_FAILED_STATUS = "reconciliation_failed"
RECONCILIATION_FAILED_REASONS = {
    "no_bank_transaction",
    "amount_mismatch",
    "duplicate",
    "wrong_device",
    "customer_dispute",
    "other",
}
MAYBANK_TRANSACTION_DOCTYPE = "Maybank QR Transaction"
MANUAL_QR_RECONCILIATION_DOCTYPE = "Manual QR Reconciliation"


@frappe.whitelist(methods=["GET"])
def list_pending_manual_qr_reconciliations() -> list[dict[str, Any]]:
    _require_back_office_manager()
    maybank_rows = frappe.get_all(
        MAYBANK_TRANSACTION_DOCTYPE,
        filters={"manual_reconciliation_status": PENDING_RECONCILIATION_STATUS},
        fields=[
            "name",
            "transaction_refno",
            "device_id",
            "sale_amount_sen",
            "created_at",
            "business_date",
            "provider",
            "manual_reconciliation_status",
            "receipt_file",
            "receipt_uploaded_at",
            "receipt_idempotency_key",
            "receipt_payment_id",
            "receipt_order_id",
            "receipt_amount_sen",
            "fb_order",
            "fb_order_payment",
            "sales_invoice",
            "idempotency_key",
        ],
        order_by="created_at asc",
    )
    manual_rows = frappe.get_all(
        MANUAL_QR_RECONCILIATION_DOCTYPE,
        filters={"status": PENDING_RECONCILIATION_STATUS},
        fields=[
            "name",
            "provider_session_id",
            "device_id",
            "amount_sen",
            "created_at",
            "business_date",
            "status",
            "payment_reference",
            "evidence_kind",
            "receipt_file",
            "receipt_uploaded_at",
            "receipt_idempotency_key",
            "receipt_payment_id",
            "receipt_order_id",
            "receipt_amount_sen",
            "fb_order",
            "sales_invoice",
            "reconciliation_idempotency_key",
            "evidence_json",
        ],
        order_by="created_at asc",
    )
    return [
        _manual_reconciliation_row(row)
        for row in [*maybank_rows, *manual_rows]
        if _is_static_reconciliation(row)
        or _has_manual_receipt_evidence(row)
        or bool(cstr(_row_value(row, "fb_order_payment")).strip())
    ]


@frappe.whitelist(methods=["POST"])
def fetch_manual_qr_reconciliation_status(**kwargs: Any) -> dict[str, list[dict[str, Any]]]:
    payload = _collect_status_payload(kwargs)
    device_id = _resolve_status_device_id(payload)
    requests = _extract_status_requests(payload)
    if not requests:
        return {"statuses": []}

    maybank_refnos = [
        request["transaction_refno"]
        for request in requests
        if not _is_static_reference(request["transaction_refno"])
    ]
    static_refnos = [
        request["transaction_refno"]
        for request in requests
        if _is_static_reference(request["transaction_refno"])
    ]
    rows: list[Any] = []
    filters: dict[str, Any] = {
        "transaction_refno": ["in", maybank_refnos]
    }
    if device_id:
        filters["device_id"] = device_id

    if maybank_refnos:
        rows.extend(
            frappe.get_all(
                MAYBANK_TRANSACTION_DOCTYPE,
                filters=filters,
                fields=[
                    "transaction_refno",
                    "device_id",
                    "receipt_payment_id",
                    "manual_reconciliation_status",
                    "reconciled_by",
                    "reconciled_at",
                    "reconciliation_note",
                    "reconciliation_failed_reason",
                ],
            )
        )
    if static_refnos:
        manual_filters: dict[str, Any] = {
            "provider_session_id": ["in", static_refnos]
        }
        if device_id:
            manual_filters["device_id"] = device_id
        rows.extend(
            frappe.get_all(
                MANUAL_QR_RECONCILIATION_DOCTYPE,
                filters=manual_filters,
                fields=[
                    "provider_session_id",
                    "device_id",
                    "receipt_payment_id",
                    "status",
                    "reconciled_by",
                    "reconciled_at",
                    "reconciliation_note",
                    "reconciliation_failed_reason",
                ],
            )
        )
    rows_by_refno = {
        _record_reference(row): row for row in rows
    }

    return {
        "statuses": [
            _manual_reconciliation_status_row(request, rows_by_refno.get(request["transaction_refno"]))
            for request in requests
        ]
    }


@frappe.whitelist(methods=["POST"])
def mark_manual_qr_reconciled(**kwargs: Any) -> dict[str, str]:
    try:
        return _mark_manual_qr_reconciled(**kwargs)
    except Exception:
        frappe.db.rollback()
        raise


def _mark_manual_qr_reconciled(**kwargs: Any) -> dict[str, str]:
    payload = _collect_reconciliation_payload(kwargs, required_fields=("note",))
    txn = _load_pending_reconciliation_transaction(payload["transaction_refno"])
    _validate_reconciliation_bank_match(txn, payload)
    _ensure_reconciliation_accounting_context(txn)
    accounting_evidence = ensure_qr_suspense_reclassification(txn)
    journal_entry = cstr(accounting_evidence.get("journal_entry")).strip()
    if not journal_entry:
        frappe.throw(
            _("QR reconciliation did not produce submitted accounting evidence"),
            frappe.ValidationError,
        )
    reconciled_at = now_datetime()

    frappe.db.set_value(
        _record_doctype(txn),
        txn.name,
        {
            _record_status_field(txn): RECONCILED_STATUS,
            "reconciled_by": payload["manager_id"],
            "reconciled_at": reconciled_at,
            "reconciliation_note": payload["note"],
            "reconciliation_failed_reason": None,
            "reclassification_journal_entry": journal_entry,
        },
    )
    _update_linked_payment_settlement(txn, RECONCILED_STATUS)
    _write_reconciliation_comment(
        txn,
        action=RECONCILED_STATUS,
        manager_id=payload["manager_id"],
        note=payload["note"],
        reason=None,
        reconciled_at=reconciled_at,
        reclassification_journal_entry=journal_entry,
    )
    return {
        "status": "ok",
        "manual_reconciliation_status": RECONCILED_STATUS,
        "reclassification_journal_entry": journal_entry,
    }


@frappe.whitelist(methods=["POST"])
def mark_manual_qr_reconciliation_failed(**kwargs: Any) -> dict[str, str]:
    try:
        return _mark_manual_qr_reconciliation_failed(**kwargs)
    except Exception:
        frappe.db.rollback()
        raise


def _mark_manual_qr_reconciliation_failed(**kwargs: Any) -> dict[str, str]:
    payload = _collect_reconciliation_payload(kwargs, required_fields=("reason", "note"))
    reason = payload["reason"]
    if reason not in RECONCILIATION_FAILED_REASONS:
        frappe.throw(_("reconciliation_failed_reason is invalid"), frappe.ValidationError)

    txn = _load_pending_reconciliation_transaction(
        payload["transaction_refno"],
        allowed_statuses={
            PENDING_RECONCILIATION_STATUS,
            RECONCILIATION_FAILED_STATUS,
        },
    )
    if _has_provider_paid_truth(txn):
        frappe.throw(
            _("Provider-paid Maybank QR truth cannot be marked reconciliation_failed"),
            frappe.ValidationError,
        )
    if cstr(getattr(txn, "reclassification_journal_entry", None)).strip():
        frappe.throw(
            _("QR reconciliation already has posted accounting evidence"),
            frappe.ValidationError,
        )
    _validate_reconciliation_bank_match(txn, payload)
    _ensure_reconciliation_accounting_context(txn)
    if _record_status(txn) == RECONCILIATION_FAILED_STATUS:
        return _replay_failed_reconciliation(txn, payload)

    accounting_evidence = ensure_qr_suspense_failure_reclassification(
        txn,
        reason,
    )
    journal_entry = cstr(accounting_evidence.get("journal_entry")).strip()
    if not journal_entry or cstr(
        getattr(txn, "failure_journal_entry", None)
    ).strip() != journal_entry:
        frappe.throw(
            _("QR failure disposition did not produce linked submitted accounting evidence"),
            frappe.ValidationError,
        )
    reconciled_at = now_datetime()

    frappe.db.set_value(
        _record_doctype(txn),
        txn.name,
        {
            _record_status_field(txn): RECONCILIATION_FAILED_STATUS,
            "reconciliation_failed_reason": reason,
            "reconciled_by": payload["manager_id"],
            "reconciled_at": reconciled_at,
            "reconciliation_note": payload["note"],
            "failure_journal_entry": journal_entry,
        },
    )
    _update_linked_payment_settlement(txn, RECONCILIATION_FAILED_STATUS)
    _write_reconciliation_comment(
        txn,
        action=RECONCILIATION_FAILED_STATUS,
        manager_id=payload["manager_id"],
        note=payload["note"],
        reason=reason,
        reconciled_at=reconciled_at,
        failure_journal_entry=journal_entry,
    )
    return {
        "status": "ok",
        "manual_reconciliation_status": RECONCILIATION_FAILED_STATUS,
        "failure_journal_entry": journal_entry,
    }


def _replay_failed_reconciliation(
    txn: Any,
    payload: dict[str, str],
) -> dict[str, str]:
    expected = {
        "reconciliation_failed_reason": payload["reason"],
        "reconciled_by": payload["manager_id"],
        "reconciliation_note": payload["note"],
    }
    for fieldname, expected_value in expected.items():
        if cstr(getattr(txn, fieldname, None)).strip() != expected_value:
            frappe.throw(
                _("Terminal QR reconciliation failure does not match this retry"),
                frappe.ValidationError,
            )
    evidence = assert_qr_suspense_failure_reclassification(
        txn,
        payload["reason"],
    )
    journal_entry = cstr(evidence.get("journal_entry")).strip()
    if not journal_entry or cstr(
        getattr(txn, "failure_journal_entry", None)
    ).strip() != journal_entry:
        frappe.throw(
            _("Terminal QR reconciliation failure has no linked accounting evidence"),
            frappe.ValidationError,
        )
    return {
        "status": "ok",
        "manual_reconciliation_status": RECONCILIATION_FAILED_STATUS,
        "failure_journal_entry": journal_entry,
    }


@frappe.whitelist(methods=["POST"])
def upload_manual_qr_receipt(**kwargs: Any) -> dict[str, str | None]:
    payload: dict[str, str] = {}
    transaction_name = ""
    transaction_doctype = ""

    try:
        payload = _collect_payload(kwargs)
        preflight_device = _resolve_authorized_device(payload["device_id"])
        device_authority = _capture_receipt_device_authority(preflight_device)
        uploaded_file = _get_uploaded_file()
        content, mime_type = _read_and_validate_jpeg(uploaded_file)
        file_hash = hashlib.sha256(content).hexdigest()

        # Reading and hashing a multi-megabyte JPEG must not hold the same device
        # row lock used by order submission. Reacquire the lock only for the
        # receipt mutation, then re-prove that device authority did not change
        # while the bounded upload body was being processed.
        device = _revalidate_receipt_device_authority(device_authority)
        txn = _load_and_validate_transaction(payload, device)
        transaction_name = cstr(getattr(txn, "name", None)).strip()
        transaction_doctype = _record_doctype(txn)

        existing_response = _resolve_existing_idempotency(payload, file_hash)
        if existing_response:
            _write_audit_log(
                "duplicate",
                transaction_name=transaction_name,
                transaction_doctype=transaction_doctype,
                device_id=payload["device_id"],
                idempotency_key=payload["idempotency_key"],
                message="Manual QR receipt upload returned existing File",
            )
            return existing_response

        _claim_receipt_idempotency(txn, payload, file_hash)
        file_doc = _create_private_file(txn, payload["file_name"], content)
        _attach_receipt_file(txn, file_doc, payload, file_hash)

        response = _file_response(file_doc, file_hash)
        _write_audit_log(
            "success",
            transaction_name=transaction_name,
            transaction_doctype=transaction_doctype,
            device_id=payload["device_id"],
            idempotency_key=payload["idempotency_key"],
            message="Manual QR receipt uploaded",
        )
        return response
    except Exception as exc:
        _write_audit_log(
            "failed",
            transaction_name=transaction_name,
            transaction_doctype=transaction_doctype,
            device_id=payload.get("device_id"),
            idempotency_key=payload.get("idempotency_key"),
            message=sanitized_error_message(exc),
        )
        frappe.db.rollback()
        raise


def _require_back_office_manager() -> None:
    roles = get_session_roles()
    if "System Manager" not in roles:
        frappe.throw(
            _("Only a System Manager can reconcile Manual QR payments"),
            frappe.ValidationError,
        )


def _collect_reconciliation_payload(
    kwargs: dict[str, Any], *, required_fields: tuple[str, ...]
) -> dict[str, str]:
    _require_back_office_manager()
    source = dict(kwargs)
    form_dict = getattr(getattr(frappe, "local", None), "form_dict", None)
    if isinstance(form_dict, dict):
        source.update(form_dict)

    payload = {
        "transaction_refno": cstr(source.get("transaction_refno")).strip(),
        "amount_sen": cstr(source.get("amount_sen")).strip(),
        "business_date": cstr(source.get("business_date")).strip(),
        "device_id": cstr(source.get("device_id")).strip(),
        "provider": cstr(source.get("provider")).strip(),
        "manager_id": cstr(source.get("manager_id")).strip(),
        "reason": cstr(source.get("reason")).strip(),
        "note": cstr(source.get("note")).strip(),
    }
    required = (
        "transaction_refno",
        "amount_sen",
        "business_date",
        "device_id",
        "provider",
        "manager_id",
        *required_fields,
    )
    for fieldname in required:
        if not payload[fieldname]:
            frappe.throw(_("{0} is required").format(fieldname), frappe.ValidationError)
    payload["amount_sen"] = str(_parse_positive_amount_sen(payload["amount_sen"]))
    session_user = cstr(getattr(frappe.session, "user", None)).strip()
    if payload["manager_id"] != session_user:
        frappe.throw(
            _("manager_id must match the authenticated ERPNext user"),
            frappe.ValidationError,
        )
    return payload


def _collect_status_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
    source = dict(kwargs)
    request = getattr(frappe, "request", None)
    get_json = getattr(request, "get_json", None)
    if callable(get_json):
        request_json = get_json(silent=True)
        if isinstance(request_json, dict):
            source.update(request_json)

    form_dict = getattr(getattr(frappe, "local", None), "form_dict", None)
    if isinstance(form_dict, dict):
        source.update(form_dict)

    if isinstance(source.get("payments"), str):
        parsed_payments = frappe.parse_json(source["payments"])
        if isinstance(parsed_payments, list):
            source["payments"] = parsed_payments
    if isinstance(source.get("transaction_refnos"), str):
        parsed_refnos = frappe.parse_json(source["transaction_refnos"])
        if isinstance(parsed_refnos, list):
            source["transaction_refnos"] = parsed_refnos
    return source


def _resolve_status_device_id(payload: dict[str, Any]) -> str | None:
    require_kopos_api_access()
    requested_device_id = cstr(payload.get("device_id")).strip()
    roles = get_session_roles()
    if "System Manager" in roles:
        if requested_device_id:
            device = require_device_context(device_id=requested_device_id)
            return cstr(getattr(device, "device_id", requested_device_id)).strip()
        return None

    device = get_authenticated_device_doc()
    device_id = cstr(getattr(device, "device_id", None)).strip()
    if not device_id:
        frappe.throw(_("Authenticated KoPOS Device has no device_id"), frappe.ValidationError)
    return device_id


def _extract_status_requests(payload: dict[str, Any]) -> list[dict[str, str]]:
    requests: list[dict[str, str]] = []
    seen_refnos: set[str] = set()

    payments = payload.get("payments")
    if isinstance(payments, list):
        for payment in payments:
            if not isinstance(payment, dict):
                continue
            transaction_refno = cstr(
                payment.get("transaction_refno") or payment.get("provider_session_id")
            ).strip()
            if not transaction_refno or transaction_refno in seen_refnos:
                continue
            seen_refnos.add(transaction_refno)
            requests.append(
                {
                    "transaction_refno": transaction_refno,
                    "payment_id": cstr(payment.get("payment_id")).strip(),
                }
            )

    refnos = payload.get("transaction_refnos")
    if isinstance(refnos, list):
        for refno_value in refnos:
            transaction_refno = cstr(refno_value).strip()
            if not transaction_refno or transaction_refno in seen_refnos:
                continue
            seen_refnos.add(transaction_refno)
            requests.append({"transaction_refno": transaction_refno, "payment_id": ""})

    single_refno = cstr(payload.get("transaction_refno")).strip()
    if single_refno and single_refno not in seen_refnos:
        requests.append(
            {
                "transaction_refno": single_refno,
                "payment_id": cstr(payload.get("payment_id")).strip(),
            }
        )

    return requests


def _manual_reconciliation_status_row(
    request: dict[str, str], row: Any | None
) -> dict[str, Any]:
    source = row or {}
    return {
        "payment_id": request["payment_id"]
        or cstr(_row_value(source, "receipt_payment_id")).strip()
        or None,
        "provider_session_id": request["transaction_refno"],
        "transaction_refno": request["transaction_refno"],
        "reconciliation_status": _record_status(source) or None,
        "reconciled_by": cstr(_row_value(source, "reconciled_by")).strip() or None,
        "reconciled_at": cstr(_row_value(source, "reconciled_at")).strip() or None,
        "reconciliation_note": cstr(_row_value(source, "reconciliation_note")).strip()
        or None,
        "reconciliation_failed_reason": cstr(
            _row_value(source, "reconciliation_failed_reason")
        ).strip()
        or None,
    }


def _has_manual_receipt_evidence(row: Any) -> bool:
    return bool(
        cstr(_row_value(row, "receipt_file")).strip()
        or cstr(_row_value(row, "receipt_idempotency_key")).strip()
    )


def _load_pending_reconciliation_transaction(
    transaction_refno: str,
    *,
    allowed_statuses: set[str] | None = None,
) -> Any:
    is_static = _is_static_reference(transaction_refno)
    doctype = (
        MANUAL_QR_RECONCILIATION_DOCTYPE
        if is_static
        else MAYBANK_TRANSACTION_DOCTYPE
    )
    reference_field = "provider_session_id" if is_static else "transaction_refno"
    txn_name = frappe.db.get_value(
        doctype, {reference_field: transaction_refno}, "name"
    )
    if not txn_name:
        frappe.throw(_("QR reconciliation record was not found"), frappe.ValidationError)

    frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name = %s FOR UPDATE",
        (txn_name,),
    )
    txn = frappe.get_doc(doctype, txn_name)
    status = _record_status(txn)
    expected_statuses = allowed_statuses or {PENDING_RECONCILIATION_STATUS}
    if status not in expected_statuses:
        frappe.throw(
            _("QR reconciliation record is not pending manual reconciliation"),
            frappe.ValidationError,
        )
    if is_static and not cstr(getattr(txn, "fb_order_payment", None)).strip():
        frappe.throw(
            _("Manual QR Reconciliation payment row is missing"),
            frappe.ValidationError,
        )
    if (
        not is_static
        and not _has_manual_receipt_evidence(txn)
        and not cstr(getattr(txn, "fb_order_payment", None)).strip()
    ):
        frappe.throw(
            _("Maybank QR Transaction has no manual confirmation evidence"),
            frappe.ValidationError,
        )
    return txn


def _validate_reconciliation_bank_match(txn: Any, payload: dict[str, str]) -> None:
    if _parse_positive_amount_sen(
        _transaction_amount_sen(txn),
        fieldname="QR reconciliation record amount_sen",
    ) != _parse_positive_amount_sen(payload["amount_sen"]):
        frappe.throw(
            _("amount_sen does not match QR reconciliation record"),
            frappe.ValidationError,
        )

    transaction_business_date = _business_date_string(getattr(txn, "business_date", None))
    request_business_date = _business_date_string(payload["business_date"])
    if transaction_business_date != request_business_date:
        frappe.throw(
            _("business_date does not match QR reconciliation record"),
            frappe.ValidationError,
        )

    if cstr(getattr(txn, "device_id", None)).strip() != payload["device_id"]:
        frappe.throw(
            _("device_id does not match QR reconciliation record"),
            frappe.ValidationError,
        )

    expected_provider = "static_qr" if _is_static_reconciliation(txn) else cstr(
        getattr(txn, "provider", None)
    ).strip()
    if expected_provider != payload["provider"]:
        frappe.throw(
            _("provider does not match QR reconciliation record"),
            frappe.ValidationError,
        )


def _ensure_reconciliation_accounting_context(txn: Any) -> None:
    if _is_static_reconciliation(txn):
        return

    order_name = cstr(getattr(txn, "fb_order", None)).strip()
    payment_row_name = cstr(getattr(txn, "fb_order_payment", None)).strip()
    if not order_name or not payment_row_name:
        frappe.throw(
            _("Maybank QR reconciliation is not linked to its submitted sale payment"),
            frappe.ValidationError,
        )

    order = frappe.get_doc("FB Order", order_name)
    expected_company = cstr(getattr(order, "company", None)).strip()
    expected_currency = cstr(getattr(order, "currency", None)).strip().upper()
    payment_context = frappe.db.get_value(
        "FB Order Payment",
        payment_row_name,
        ["parent", "suspense_account"],
        as_dict=True,
    )
    if not payment_context:
        frappe.throw(
            _("Maybank QR reconciliation payment row was not found"),
            frappe.ValidationError,
        )
    if cstr(_row_value(payment_context, "parent")).strip() != order_name:
        frappe.throw(
            _("Maybank QR reconciliation payment row belongs to another sale"),
            frappe.ValidationError,
        )
    expected_suspense_account = cstr(
        _row_value(payment_context, "suspense_account")
    ).strip()
    if not expected_company or not expected_currency or not expected_suspense_account:
        frappe.throw(
            _("Maybank QR reconciliation accounting context is incomplete"),
            frappe.ValidationError,
        )

    expected_values = {
        "company": expected_company,
        "currency": expected_currency,
        "suspense_account": expected_suspense_account,
    }
    updates: dict[str, str] = {}
    for fieldname, expected in expected_values.items():
        current = cstr(getattr(txn, fieldname, None)).strip()
        if fieldname == "currency":
            current = current.upper()
        if current and current != expected:
            frappe.throw(
                _("Maybank QR reconciliation {0} does not match its sale").format(
                    fieldname
                ),
                frappe.ValidationError,
            )
        if not current:
            updates[fieldname] = expected

    if updates:
        frappe.db.set_value(
            MAYBANK_TRANSACTION_DOCTYPE,
            txn.name,
            updates,
            update_modified=False,
        )


def _has_provider_paid_truth(txn: Any) -> bool:
    return not _is_static_reconciliation(txn) and (
        cstr(getattr(txn, "status", None)).strip() == "paid"
    )


def _transaction_amount_sen(txn: Any) -> Any:
    amount_sen = _row_value(txn, "amount_sen")
    if amount_sen is not None:
        return amount_sen
    return _row_value(txn, "sale_amount_sen")


def _is_static_reference(value: Any) -> bool:
    return cstr(value).strip().startswith("static-")


def _is_static_reconciliation(row: Any) -> bool:
    return (
        cstr(_row_value(row, "doctype")).strip()
        == MANUAL_QR_RECONCILIATION_DOCTYPE
        or bool(cstr(_row_value(row, "provider_session_id")).strip())
    )


def _record_doctype(row: Any) -> str:
    return (
        MANUAL_QR_RECONCILIATION_DOCTYPE
        if _is_static_reconciliation(row)
        else MAYBANK_TRANSACTION_DOCTYPE
    )


def _record_reference(row: Any) -> str:
    fieldname = (
        "provider_session_id"
        if _is_static_reconciliation(row)
        else "transaction_refno"
    )
    return cstr(_row_value(row, fieldname)).strip()


def _record_status(row: Any) -> str:
    fieldname = "status" if _is_static_reconciliation(row) else (
        "manual_reconciliation_status"
    )
    return cstr(_row_value(row, fieldname)).strip()


def _record_status_field(row: Any) -> str:
    return "status" if _is_static_reconciliation(row) else (
        "manual_reconciliation_status"
    )


def _update_linked_payment_settlement(row: Any, settlement_status: str) -> None:
    payment_row = cstr(_row_value(row, "fb_order_payment")).strip()
    if not payment_row:
        if not _is_static_reconciliation(row):
            # Compatibility for legacy receipt-only Maybank records created
            # before the exact FB Order Payment link became authoritative.
            return
        frappe.throw(
            _("Manual QR Reconciliation payment row is missing"),
            frappe.ValidationError,
        )
    frappe.db.set_value(
        "FB Order Payment",
        payment_row,
        {"settlement_status": settlement_status},
        update_modified=False,
    )


def _business_date_string(value: Any) -> str:
    if not cstr(value).strip():
        return ""
    try:
        return getdate(value).isoformat()
    except (TypeError, ValueError):
        frappe.throw(_("business_date is invalid"), frappe.ValidationError)
        return ""


def _manual_reconciliation_row(row: Any) -> dict[str, Any]:
    return {
        "transaction_refno": _record_reference(row),
        "device_id": cstr(_row_value(row, "device_id")).strip(),
        "sale_amount_sen": cint(_transaction_amount_sen(row)),
        "created_at": _row_value(row, "created_at"),
        "business_date": _business_date_string(_row_value(row, "business_date")),
        "provider": (
            "static_qr"
            if _is_static_reconciliation(row)
            else cstr(_row_value(row, "provider")).strip()
        ),
        "payment_reference": cstr(_row_value(row, "payment_reference")).strip()
        or None,
        "evidence_kind": cstr(_row_value(row, "evidence_kind")).strip() or None,
        "manual_reconciliation_status": _record_status(row),
        "receipt_file": cstr(_row_value(row, "receipt_file")).strip() or None,
        "receipt_uploaded_at": _row_value(row, "receipt_uploaded_at"),
        "receipt_idempotency_key": cstr(
            _row_value(row, "receipt_idempotency_key")
        ).strip()
        or None,
        "receipt_payment_id": cstr(_row_value(row, "receipt_payment_id")).strip()
        or None,
        "receipt_order_id": cstr(_row_value(row, "receipt_order_id")).strip() or None,
        "receipt_amount_sen": cint(_row_value(row, "receipt_amount_sen")) or None,
        "fb_order": cstr(_row_value(row, "fb_order")).strip() or None,
        "sales_invoice": cstr(_row_value(row, "sales_invoice")).strip() or None,
        "idempotency_key": cstr(
            _row_value(row, "idempotency_key")
            or _row_value(row, "reconciliation_idempotency_key")
        ).strip()
        or None,
    }


def _write_reconciliation_comment(
    txn: Any,
    *,
    action: str,
    manager_id: str,
    note: str,
    reason: str | None,
    reconciled_at: Any,
    reclassification_journal_entry: str | None = None,
    failure_journal_entry: str | None = None,
) -> None:
    payload = {
        "action": action,
        "transaction_refno": _record_reference(txn),
        "manager_id": manager_id,
        "note": note,
        "reason": reason,
        "reconciled_at": cstr(reconciled_at),
        "reclassification_journal_entry": reclassification_journal_entry,
        "failure_journal_entry": failure_journal_entry,
    }
    comment = frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Info",
            "reference_doctype": _record_doctype(txn),
            "reference_name": cstr(getattr(txn, "name", None)).strip(),
            "content": frappe.as_json(payload),
        }
    )
    comment.insert(ignore_permissions=True)


def _collect_payload(kwargs: dict[str, Any]) -> dict[str, str]:
    source = dict(kwargs)
    form_dict = getattr(getattr(frappe, "local", None), "form_dict", None)
    if isinstance(form_dict, dict):
        source.update(form_dict)

    payload = {
        "device_id": cstr(source.get("device_id")).strip(),
        "payment_id": cstr(source.get("payment_id")).strip(),
        "order_id": cstr(source.get("order_id")).strip(),
        "transaction_refno": cstr(source.get("transaction_refno")).strip(),
        "file_name": _sanitize_file_name(cstr(source.get("file_name")).strip()),
        "captured_at": cstr(source.get("captured_at")).strip(),
        "amount_sen": cstr(source.get("amount_sen")).strip(),
        "currency": cstr(source.get("currency")).strip().upper(),
        "company": cstr(source.get("company")).strip(),
        "idempotency_key": cstr(source.get("idempotency_key")).strip(),
    }

    for fieldname, value in payload.items():
        if not value:
            frappe.throw(_("{0} is required").format(fieldname), frappe.ValidationError)

    payload["amount_sen"] = str(_parse_positive_amount_sen(payload["amount_sen"]))

    if len(payload["idempotency_key"]) > 140:
        frappe.throw(_("idempotency_key is too long"), frappe.ValidationError)

    return payload


def _get_uploaded_file() -> Any:
    request = getattr(frappe, "request", None) or getattr(
        getattr(frappe, "local", None), "request", None
    )
    files = getattr(request, "files", None)
    if files is None:
        frappe.throw(_("receipt JPEG file is required"), frappe.ValidationError)
        return None

    for key in ("file", "receipt", "receipt_file"):
        try:
            return files[key]
        except (KeyError, TypeError):
            pass

    values = getattr(files, "values", None)
    if not callable(values):
        frappe.throw(_("receipt JPEG file is required"), frappe.ValidationError)
        return None
    try:
        return next(iter(cast(Iterable[Any], values())))
    except StopIteration:
        frappe.throw(_("receipt JPEG file is required"), frappe.ValidationError)
        return None


def _read_and_validate_jpeg(uploaded_file: Any) -> tuple[bytes, str]:
    mime_type = cstr(
        getattr(uploaded_file, "mimetype", None)
        or getattr(uploaded_file, "content_type", None)
    ).lower()
    if mime_type not in JPEG_MIME_TYPES:
        frappe.throw(_("receipt file must be image/jpeg"), frappe.ValidationError)

    max_bytes = _get_max_receipt_bytes()
    content = _read_file_bytes(uploaded_file, max_bytes=max_bytes)
    if len(content) > max_bytes:
        frappe.throw(_("receipt file exceeds the maximum allowed size"), frappe.ValidationError)
    if not content.startswith(JPEG_MAGIC):
        frappe.throw(_("receipt file must be a JPEG image"), frappe.ValidationError)
    return content, mime_type


def _read_file_bytes(uploaded_file: Any, *, max_bytes: int) -> bytes:
    stream = getattr(uploaded_file, "stream", None)
    read = getattr(stream, "read", None)
    if not callable(read):
        read = getattr(uploaded_file, "read", None)
    if not callable(read):
        frappe.throw(_("receipt file could not be read"), frappe.ValidationError)
        return b""

    content = read(max_bytes + 1)

    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    elif isinstance(content, bytes):
        content_bytes = content
    else:
        frappe.throw(_("receipt file could not be read"), frappe.ValidationError)
        return b""

    stream = getattr(uploaded_file, "stream", None)
    seek = getattr(stream, "seek", None)
    if callable(seek):
        seek(0)
    return content_bytes


def _get_max_receipt_bytes() -> int:
    conf = getattr(frappe, "conf", {}) or {}
    conf_get = getattr(conf, "get", None)
    configured = cint(
        conf_get("kopos_manual_qr_receipt_max_bytes") if callable(conf_get) else None
    )
    return configured if configured > 0 else DEFAULT_MAX_RECEIPT_BYTES


def _resolve_authorized_device(request_device_id: str) -> Any:
    device = require_device_context(device_id=request_device_id)
    if device is None:
        frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise")
    authorized_device_id = cstr(getattr(device, "device_id", None)).strip()
    if not authorized_device_id:
        frappe.throw(_("Authenticated KoPOS Device has no device_id"), frappe.ValidationError)
    if authorized_device_id != request_device_id:
        frappe.throw(
            _("Request device_id does not match authenticated KoPOS Device"),
            frappe.ValidationError,
        )
    if not cint(getattr(device, "enabled", 1)):
        frappe.throw(_("KoPOS Device is disabled"), frappe.ValidationError)
    return device


def _capture_receipt_device_authority(device: Any) -> dict[str, str | int]:
    return {
        "name": cstr(getattr(device, "name", None)).strip(),
        "device_id": cstr(getattr(device, "device_id", None)).strip(),
        "api_user": cstr(getattr(device, "api_user", None)).strip(),
        "config_version": cint(getattr(device, "config_version", 0)),
        "pos_profile": cstr(getattr(device, "pos_profile", None)).strip(),
    }


def _revalidate_receipt_device_authority(
    authority: dict[str, str | int],
) -> Any:
    request_device_id = cstr(authority.get("device_id")).strip()
    locked_device = lock_device_for_operational_mutation(
        device_id=request_device_id,
    )
    current_authority = _capture_receipt_device_authority(locked_device)
    if current_authority != authority:
        frappe.throw(
            _(
                "KoPOS Device authority changed while the receipt upload was in flight; "
                "authenticate again"
            ),
            frappe.ValidationError,
        )
    return locked_device


def _load_and_validate_transaction(payload: dict[str, str], device: Any) -> Any:
    is_static = _is_static_reference(payload["transaction_refno"])
    doctype = (
        MANUAL_QR_RECONCILIATION_DOCTYPE
        if is_static
        else MAYBANK_TRANSACTION_DOCTYPE
    )
    reference_field = "provider_session_id" if is_static else "transaction_refno"
    txn_name = frappe.db.get_value(
        doctype,
        {reference_field: payload["transaction_refno"]},
        "name",
    )
    if not txn_name:
        frappe.throw(_("QR reconciliation record was not found"), frappe.ValidationError)

    frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name = %s FOR UPDATE",
        (txn_name,),
    )
    txn = frappe.get_doc(doctype, txn_name)
    if _record_reference(txn) != payload["transaction_refno"]:
        frappe.throw(_("transaction_refno does not match target record"), frappe.ValidationError)
    if cstr(getattr(txn, "device_id", None)).strip() != payload["device_id"]:
        frappe.throw(_("QR reconciliation record belongs to another device"), frappe.ValidationError)
    if _parse_positive_amount_sen(
        _transaction_amount_sen(txn),
        fieldname="QR reconciliation record amount_sen",
    ) != _parse_positive_amount_sen(payload["amount_sen"]):
        frappe.throw(_("amount_sen does not match QR reconciliation record"), frappe.ValidationError)

    _validate_device_business_context(device, payload)
    if is_static:
        if cstr(getattr(txn, "company", None)).strip() != payload["company"]:
            frappe.throw(_("company does not match QR reconciliation record"), frappe.ValidationError)
        if cstr(getattr(txn, "currency", None)).strip().upper() != payload["currency"]:
            frappe.throw(_("currency does not match QR reconciliation record"), frappe.ValidationError)
        _validate_static_receipt_capture(txn, payload["captured_at"])
    else:
        _validate_provider_context(txn)
        if cstr(getattr(txn, "fb_order_payment", None)).strip():
            _validate_linked_maybank_receipt_capture(txn, payload["captured_at"])
        else:
            _validate_transaction_window(txn, payload["captured_at"])
    return txn


def _validate_static_receipt_capture(txn: Any, captured_at: str) -> None:
    if _record_status(txn) != PENDING_RECONCILIATION_STATUS:
        frappe.throw(
            _("Manual QR Reconciliation is not pending"),
            frappe.ValidationError,
        )
    raw_evidence = cstr(getattr(txn, "evidence_json", None)).strip()
    try:
        evidence = json.loads(raw_evidence)
    except (TypeError, ValueError):
        frappe.throw(
            _("Manual QR Reconciliation evidence is invalid"),
            frappe.ValidationError,
        )
        return
    if not isinstance(evidence, dict) or not cstr(evidence.get("captured_at")).strip():
        frappe.throw(
            _("Manual QR Reconciliation evidence captured_at is missing"),
            frappe.ValidationError,
        )
    expected_capture = _coerce_site_datetime(evidence["captured_at"])
    actual_capture = _coerce_site_datetime(captured_at)
    if actual_capture != expected_capture:
        frappe.throw(
            _("captured_at does not match Manual QR Reconciliation evidence"),
            frappe.ValidationError,
        )


def _validate_linked_maybank_receipt_capture(txn: Any, captured_at: str) -> None:
    if _record_status(txn) not in {
        PENDING_RECONCILIATION_STATUS,
        RECONCILED_STATUS,
        RECONCILIATION_FAILED_STATUS,
    }:
        frappe.throw(
            _("Maybank QR Transaction is not a manual reconciliation"),
            frappe.ValidationError,
        )

    payment_row = cstr(getattr(txn, "fb_order_payment", None)).strip()
    raw_evidence = cstr(
        frappe.db.get_value(
            "FB Order Payment",
            payment_row,
            "manual_confirmation_evidence_json",
        )
    ).strip()
    try:
        evidence = json.loads(raw_evidence)
    except (TypeError, ValueError):
        frappe.throw(
            _("FB Order Payment manual confirmation evidence is invalid"),
            frappe.ValidationError,
        )
        return
    if not isinstance(evidence, dict) or cstr(evidence.get("evidence_kind")).strip() != (
        "receipt_photo"
    ):
        frappe.throw(
            _("FB Order Payment does not require receipt photo upload"),
            frappe.ValidationError,
        )
    expected_captured_at = cstr(evidence.get("captured_at")).strip()
    if not expected_captured_at:
        frappe.throw(
            _("FB Order Payment receipt captured_at is missing"),
            frappe.ValidationError,
        )
    if _coerce_site_datetime(captured_at) != _coerce_site_datetime(
        expected_captured_at
    ):
        frappe.throw(
            _("captured_at does not match FB Order Payment evidence"),
            frappe.ValidationError,
        )


def _validate_device_business_context(device: Any, payload: dict[str, str]) -> None:
    pos_profile_name = cstr(getattr(device, "pos_profile", None)).strip()
    if not pos_profile_name:
        return

    pos_profile = frappe.get_cached_doc("POS Profile", pos_profile_name)
    company = cstr(getattr(pos_profile, "company", None)).strip()
    currency = cstr(getattr(pos_profile, "currency", None)).strip().upper()
    if not currency and company:
        currency = cstr(
            frappe.db.get_value("Company", company, "default_currency")
        ).strip().upper()

    if company and company != payload["company"]:
        frappe.throw(_("company does not match KoPOS Device POS Profile"), frappe.ValidationError)
    if currency and currency != payload["currency"]:
        frappe.throw(_("currency does not match KoPOS Device POS Profile"), frappe.ValidationError)


def _validate_provider_context(txn: Any) -> None:
    configured_outlet = cstr(
        getattr(frappe.db, "get_single_value", lambda *_args, **_kwargs: None)(
            "Maybank Settings", "outlet_id"
        )
    ).strip()
    txn_outlet = cstr(getattr(txn, "outlet_id", None)).strip()
    if configured_outlet and txn_outlet and txn_outlet != configured_outlet:
        frappe.throw(_("Maybank outlet context does not match settings"), frappe.ValidationError)

    if not cstr(getattr(txn, "transaction_refno", None)).strip():
        frappe.throw(_("Maybank provider transaction reference is missing"), frappe.ValidationError)


def _validate_transaction_window(txn: Any, captured_at: str) -> None:
    status = cstr(getattr(txn, "status", None)).strip()
    if status not in {"pending", "scanned"}:
        frappe.throw(_("Maybank QR Transaction is not pending manual receipt upload"), frappe.ValidationError)

    captured_dt = _coerce_site_datetime(captured_at)
    created_at = _coerce_site_datetime(getattr(txn, "created_at", None))
    expires_at = _coerce_site_datetime(getattr(txn, "expires_at", None))
    deadline = add_to_date(expires_at, seconds=TRANSACTION_GRACE_SECONDS)
    now = _coerce_site_datetime(now_datetime())

    if captured_dt.date() != created_at.date():
        frappe.throw(_("captured_at is outside the transaction business date"), frappe.ValidationError)
    if captured_dt < created_at or captured_dt > deadline:
        frappe.throw(_("captured_at is outside the transaction window"), frappe.ValidationError)
    if now > deadline:
        frappe.throw(_("Maybank QR Transaction receipt window has expired"), frappe.ValidationError)


def _coerce_site_datetime(value: Any) -> datetime:
    dt = get_datetime(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(get_system_timezone()))
    return dt


def _resolve_existing_idempotency(
    payload: dict[str, str], file_hash: str
) -> dict[str, str | None] | None:
    doctype = (
        MANUAL_QR_RECONCILIATION_DOCTYPE
        if _is_static_reference(payload["transaction_refno"])
        else MAYBANK_TRANSACTION_DOCTYPE
    )
    existing_name = frappe.db.get_value(
        doctype,
        {"receipt_idempotency_key": payload["idempotency_key"]},
        "name",
    )
    if not existing_name:
        return None

    existing = frappe.get_doc(doctype, existing_name)
    _validate_existing_receipt_claim(existing, payload, file_hash)

    receipt_file = cstr(getattr(existing, "receipt_file", None)).strip()
    if not receipt_file:
        return None

    file_row = frappe.db.get_value(
        "File",
        receipt_file,
        [
            "name",
            "file_name",
            "file_url",
            "attached_to_doctype",
            "attached_to_name",
        ],
        as_dict=True,
    )
    if not file_row:
        frappe.throw(_("Existing receipt File was not found"), frappe.ValidationError)
    return {
        "status": "ok",
        "file_name": cstr(_row_value(file_row, "file_name")),
        "file_url": cstr(_row_value(file_row, "file_url")) or None,
        "file_hash": cstr(getattr(existing, "receipt_file_hash", None)).strip(),
        "attached_to_doctype": cstr(
            _row_value(file_row, "attached_to_doctype")
        ).strip(),
        "attached_to_name": cstr(_row_value(file_row, "attached_to_name")).strip(),
    }


def _validate_existing_receipt_claim(existing: Any, payload: dict[str, str], file_hash: str) -> None:
    expected = {
        "device_id": payload["device_id"],
        "receipt_payment_id": payload["payment_id"],
        "receipt_order_id": payload["order_id"],
        "receipt_amount_sen": cstr(cint(payload["amount_sen"])),
        "receipt_file_name": payload["file_name"],
        "receipt_file_hash": file_hash,
    }
    for fieldname, expected_value in expected.items():
        actual = cstr(getattr(existing, fieldname, None)).strip()
        if actual != expected_value:
            frappe.throw(
                _("idempotency_key was already used for a different receipt upload"),
                frappe.ValidationError,
            )
    if _record_reference(existing) != payload["transaction_refno"]:
        frappe.throw(
            _("idempotency_key was already used for a different receipt upload"),
            frappe.ValidationError,
        )


def _claim_receipt_idempotency(txn: Any, payload: dict[str, str], file_hash: str) -> None:
    existing_key = cstr(getattr(txn, "receipt_idempotency_key", None)).strip()
    if existing_key and existing_key != payload["idempotency_key"]:
        frappe.throw(_("QR reconciliation record already has a receipt upload"), frappe.ValidationError)

    if existing_key:
        _validate_existing_receipt_claim(txn, payload, file_hash)
        return

    frappe.db.set_value(
        _record_doctype(txn),
        txn.name,
        {
            "receipt_idempotency_key": payload["idempotency_key"],
            "receipt_idempotency_fingerprint": _idempotency_fingerprint(payload),
            "receipt_payment_id": payload["payment_id"],
            "receipt_order_id": payload["order_id"],
            "receipt_amount_sen": cint(payload["amount_sen"]),
            "receipt_file_name": payload["file_name"],
            "receipt_file_hash": file_hash,
            "receipt_captured_at": _coerce_site_datetime(payload["captured_at"]),
        },
        update_modified=False,
    )


def _create_private_file(txn: Any, file_name: str, content: bytes) -> Any:
    doctype = _record_doctype(txn)
    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": file_name,
            "attached_to_doctype": doctype,
            "attached_to_name": txn.name,
            "is_private": 1,
            "content": content,
        }
    )
    file_doc.insert(ignore_permissions=True)
    return file_doc


def _attach_receipt_file(
    txn: Any, file_doc: Any, payload: dict[str, str], file_hash: str
) -> None:
    updates: dict[str, Any] = {
        "receipt_file": cstr(getattr(file_doc, "name", None)).strip(),
        "receipt_uploaded_at": now_datetime(),
        "receipt_idempotency_key": payload["idempotency_key"],
        "receipt_idempotency_fingerprint": _idempotency_fingerprint(payload),
        "receipt_payment_id": payload["payment_id"],
        "receipt_order_id": payload["order_id"],
        "receipt_amount_sen": cint(payload["amount_sen"]),
        "receipt_file_name": payload["file_name"],
        "receipt_file_hash": file_hash,
        "receipt_captured_at": _coerce_site_datetime(payload["captured_at"]),
    }
    if not _record_status(txn):
        updates[_record_status_field(txn)] = PENDING_RECONCILIATION_STATUS

    frappe.db.set_value(
        _record_doctype(txn),
        txn.name,
        updates,
        update_modified=False,
    )


def _file_response(file_doc: Any, file_hash: str) -> dict[str, str | None]:
    return {
        "status": "ok",
        "file_name": cstr(_row_value(file_doc, "file_name")).strip(),
        "file_url": cstr(_row_value(file_doc, "file_url")).strip() or None,
        "file_hash": file_hash,
        "attached_to_doctype": cstr(
            _row_value(file_doc, "attached_to_doctype")
        ).strip(),
        "attached_to_name": cstr(_row_value(file_doc, "attached_to_name")).strip(),
    }


def _idempotency_fingerprint(payload: dict[str, str]) -> str:
    fingerprint = "|".join(
        [
            payload["idempotency_key"],
            payload["device_id"],
            payload["payment_id"],
            payload["transaction_refno"],
        ]
    )
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _sanitize_file_name(file_name: str) -> str:
    safe_name = os.path.basename(file_name).strip()
    if not safe_name:
        return ""
    extension = os.path.splitext(safe_name)[1].lower()
    if extension not in {".jpg", ".jpeg"}:
        frappe.throw(_("file_name must use a .jpg or .jpeg extension"), frappe.ValidationError)
    return safe_name


def _parse_positive_amount_sen(value: Any, *, fieldname: str = "amount_sen") -> int:
    try:
        amount = Decimal(cstr(value).strip())
    except (InvalidOperation, ValueError):
        frappe.throw(_("{0} must be an integer").format(fieldname), frappe.ValidationError)
        return 0
    if not amount.is_finite() or amount != amount.to_integral_value():
        frappe.throw(_("{0} must be an integer").format(fieldname), frappe.ValidationError)
    amount_sen = int(amount)
    if amount_sen <= 0:
        frappe.throw(_("{0} must be greater than zero").format(fieldname), frappe.ValidationError)
    return amount_sen


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, dict):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def _write_audit_log(
    outcome: str,
    *,
    transaction_name: str,
    transaction_doctype: str,
    device_id: str | None,
    idempotency_key: str | None,
    message: str,
) -> None:
    log_payload = {
        "outcome": outcome,
        "transaction": transaction_name,
        "device_id": cstr(device_id).strip(),
        "idempotency_key": cstr(idempotency_key).strip(),
        "message": message,
    }
    try:
        frappe.logger(RECEIPT_UPLOAD_LOGGER).info(log_payload)
        if transaction_name:
            comment = frappe.get_doc(
                {
                    "doctype": "Comment",
                    "comment_type": "Info",
                    "reference_doctype": transaction_doctype
                    or MAYBANK_TRANSACTION_DOCTYPE,
                    "reference_name": transaction_name,
                    "content": frappe.as_json(log_payload),
                }
            )
            comment.insert(ignore_permissions=True)
    except Exception as error:
        log_sanitized_error("Manual QR receipt audit log failed", error)
