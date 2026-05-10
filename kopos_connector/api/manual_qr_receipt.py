# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from collections.abc import Iterable
from typing import Any, cast
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, cstr, get_datetime, get_system_timezone, now_datetime

from kopos_connector.api.devices import get_authenticated_device_doc, get_session_roles


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


@frappe.whitelist(methods=["GET"])
def list_pending_manual_qr_reconciliations() -> list[dict[str, Any]]:
    _require_back_office_manager()
    rows = frappe.get_all(
        "Maybank QR Transaction",
        filters={"manual_reconciliation_status": PENDING_RECONCILIATION_STATUS},
        fields=[
            "name",
            "transaction_refno",
            "device_id",
            "sale_amount_sen",
            "created_at",
            "manual_reconciliation_status",
            "receipt_file",
            "receipt_uploaded_at",
            "receipt_idempotency_key",
            "receipt_payment_id",
            "receipt_order_id",
            "receipt_amount_sen",
            "fb_order",
            "sales_invoice",
            "idempotency_key",
        ],
        order_by="created_at asc",
    )
    return [_manual_reconciliation_row(row) for row in rows]


@frappe.whitelist(methods=["POST"])
def mark_manual_qr_reconciled(**kwargs: Any) -> dict[str, str]:
    payload = _collect_reconciliation_payload(kwargs, required_fields=("note",))
    txn = _load_pending_reconciliation_transaction(payload["transaction_refno"])
    reconciled_at = now_datetime()

    frappe.db.set_value(
        "Maybank QR Transaction",
        txn.name,
        {
            "manual_reconciliation_status": RECONCILED_STATUS,
            "reconciled_by": payload["manager_id"],
            "reconciled_at": reconciled_at,
            "reconciliation_note": payload["note"],
            "reconciliation_failed_reason": None,
        },
    )
    _write_reconciliation_comment(
        txn,
        action=RECONCILED_STATUS,
        manager_id=payload["manager_id"],
        note=payload["note"],
        reason=None,
        reconciled_at=reconciled_at,
    )
    return {"status": "ok", "manual_reconciliation_status": RECONCILED_STATUS}


@frappe.whitelist(methods=["POST"])
def mark_manual_qr_reconciliation_failed(**kwargs: Any) -> dict[str, str]:
    payload = _collect_reconciliation_payload(kwargs, required_fields=("reason", "note"))
    reason = payload["reason"]
    if reason not in RECONCILIATION_FAILED_REASONS:
        frappe.throw(_("reconciliation_failed_reason is invalid"), frappe.ValidationError)

    txn = _load_pending_reconciliation_transaction(payload["transaction_refno"])
    reconciled_at = now_datetime()

    frappe.db.set_value(
        "Maybank QR Transaction",
        txn.name,
        {
            "manual_reconciliation_status": RECONCILIATION_FAILED_STATUS,
            "reconciliation_failed_reason": reason,
            "reconciled_by": payload["manager_id"],
            "reconciled_at": reconciled_at,
            "reconciliation_note": payload["note"],
        },
    )
    _write_reconciliation_comment(
        txn,
        action=RECONCILIATION_FAILED_STATUS,
        manager_id=payload["manager_id"],
        note=payload["note"],
        reason=reason,
        reconciled_at=reconciled_at,
    )
    return {"status": "ok", "manual_reconciliation_status": RECONCILIATION_FAILED_STATUS}


@frappe.whitelist(methods=["POST"])
def upload_manual_qr_receipt(**kwargs: Any) -> dict[str, str | None]:
    payload: dict[str, str] = {}
    transaction_name = ""

    try:
        payload = _collect_payload(kwargs)
        uploaded_file = _get_uploaded_file()
        content, mime_type = _read_and_validate_jpeg(uploaded_file)
        file_hash = hashlib.sha256(content).hexdigest()

        device = _resolve_authorized_device(payload["device_id"])
        txn = _load_and_validate_transaction(payload, device)
        transaction_name = cstr(getattr(txn, "name", None)).strip()

        existing_response = _resolve_existing_idempotency(payload, file_hash)
        if existing_response:
            _write_audit_log(
                "duplicate",
                transaction_name=transaction_name,
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
            device_id=payload["device_id"],
            idempotency_key=payload["idempotency_key"],
            message="Manual QR receipt uploaded",
        )
        return response
    except Exception as exc:
        _write_audit_log(
            "failed",
            transaction_name=transaction_name,
            device_id=payload.get("device_id"),
            idempotency_key=payload.get("idempotency_key"),
            message=str(exc),
        )
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
        "manager_id": cstr(source.get("manager_id")).strip(),
        "reason": cstr(source.get("reason")).strip(),
        "note": cstr(source.get("note")).strip(),
    }
    required = ("transaction_refno", "manager_id", *required_fields)
    for fieldname in required:
        if not payload[fieldname]:
            frappe.throw(_("{0} is required").format(fieldname), frappe.ValidationError)
    return payload


def _load_pending_reconciliation_transaction(transaction_refno: str) -> Any:
    txn_name = frappe.db.get_value(
        "Maybank QR Transaction",
        {"transaction_refno": transaction_refno},
        "name",
    )
    if not txn_name:
        frappe.throw(_("Maybank QR Transaction was not found"), frappe.ValidationError)

    txn = frappe.get_doc("Maybank QR Transaction", txn_name)
    status = cstr(getattr(txn, "manual_reconciliation_status", None)).strip()
    if status != PENDING_RECONCILIATION_STATUS:
        frappe.throw(
            _("Maybank QR Transaction is not pending manual reconciliation"),
            frappe.ValidationError,
        )
    return txn


def _manual_reconciliation_row(row: Any) -> dict[str, Any]:
    return {
        "transaction_refno": cstr(_row_value(row, "transaction_refno")).strip(),
        "device_id": cstr(_row_value(row, "device_id")).strip(),
        "sale_amount_sen": cint(_row_value(row, "sale_amount_sen")),
        "created_at": _row_value(row, "created_at"),
        "manual_reconciliation_status": cstr(
            _row_value(row, "manual_reconciliation_status")
        ).strip(),
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
        "idempotency_key": cstr(_row_value(row, "idempotency_key")).strip() or None,
    }


def _write_reconciliation_comment(
    txn: Any,
    *,
    action: str,
    manager_id: str,
    note: str,
    reason: str | None,
    reconciled_at: Any,
) -> None:
    payload = {
        "action": action,
        "transaction_refno": cstr(getattr(txn, "transaction_refno", None)).strip(),
        "manager_id": manager_id,
        "note": note,
        "reason": reason,
        "reconciled_at": cstr(reconciled_at),
    }
    comment = frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Info",
            "reference_doctype": "Maybank QR Transaction",
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

    if cint(payload["amount_sen"]) <= 0:
        frappe.throw(_("amount_sen must be greater than zero"), frappe.ValidationError)

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

    content = _read_file_bytes(uploaded_file)
    max_bytes = _get_max_receipt_bytes()
    if len(content) > max_bytes:
        frappe.throw(_("receipt file exceeds the maximum allowed size"), frappe.ValidationError)
    if not content.startswith(JPEG_MAGIC):
        frappe.throw(_("receipt file must be a JPEG image"), frappe.ValidationError)
    return content, mime_type


def _read_file_bytes(uploaded_file: Any) -> bytes:
    read = getattr(uploaded_file, "read", None)
    if callable(read):
        content = read()
    else:
        stream = getattr(uploaded_file, "stream", None)
        stream_read = getattr(stream, "read", None)
        if not callable(stream_read):
            frappe.throw(_("receipt file could not be read"), frappe.ValidationError)
            return b""
        content = stream_read()

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
    device = get_authenticated_device_doc()
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


def _load_and_validate_transaction(payload: dict[str, str], device: Any) -> Any:
    txn_name = frappe.db.get_value(
        "Maybank QR Transaction",
        {"transaction_refno": payload["transaction_refno"]},
        "name",
    )
    if not txn_name:
        frappe.throw(_("Maybank QR Transaction was not found"), frappe.ValidationError)

    txn = frappe.get_doc("Maybank QR Transaction", txn_name)
    if cstr(getattr(txn, "transaction_refno", None)).strip() != payload["transaction_refno"]:
        frappe.throw(_("transaction_refno does not match target transaction"), frappe.ValidationError)
    if cstr(getattr(txn, "device_id", None)).strip() != payload["device_id"]:
        frappe.throw(_("Maybank QR Transaction belongs to another device"), frappe.ValidationError)
    if cint(getattr(txn, "sale_amount_sen", 0)) != cint(payload["amount_sen"]):
        frappe.throw(_("amount_sen does not match Maybank QR Transaction"), frappe.ValidationError)

    _validate_device_business_context(device, payload)
    _validate_provider_context(txn)
    _validate_transaction_window(txn, payload["captured_at"])
    return txn


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
    existing_name = frappe.db.get_value(
        "Maybank QR Transaction",
        {"receipt_idempotency_key": payload["idempotency_key"]},
        "name",
    )
    if not existing_name:
        return None

    existing = frappe.get_doc("Maybank QR Transaction", existing_name)
    _validate_existing_receipt_claim(existing, payload, file_hash)

    receipt_file = cstr(getattr(existing, "receipt_file", None)).strip()
    if not receipt_file:
        return None

    file_row = frappe.db.get_value(
        "File",
        receipt_file,
        ["name", "file_name", "file_url"],
        as_dict=True,
    )
    if not file_row:
        frappe.throw(_("Existing receipt File was not found"), frappe.ValidationError)
    return {
        "status": "ok",
        "file_name": cstr(_row_value(file_row, "file_name")),
        "file_url": cstr(_row_value(file_row, "file_url")) or None,
        "file_hash": cstr(getattr(existing, "receipt_file_hash", None)).strip(),
    }


def _validate_existing_receipt_claim(existing: Any, payload: dict[str, str], file_hash: str) -> None:
    expected = {
        "device_id": payload["device_id"],
        "transaction_refno": payload["transaction_refno"],
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


def _claim_receipt_idempotency(txn: Any, payload: dict[str, str], file_hash: str) -> None:
    existing_key = cstr(getattr(txn, "receipt_idempotency_key", None)).strip()
    if existing_key and existing_key != payload["idempotency_key"]:
        frappe.throw(_("Maybank QR Transaction already has a receipt upload"), frappe.ValidationError)

    if existing_key:
        _validate_existing_receipt_claim(txn, payload, file_hash)
        return

    frappe.db.set_value(
        "Maybank QR Transaction",
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
    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": file_name,
            "attached_to_doctype": "Maybank QR Transaction",
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
    frappe.db.set_value(
        "Maybank QR Transaction",
        txn.name,
        {
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
        },
        update_modified=False,
    )


def _file_response(file_doc: Any, file_hash: str) -> dict[str, str | None]:
    return {
        "status": "ok",
        "file_name": cstr(getattr(file_doc, "file_name", None)).strip(),
        "file_url": cstr(getattr(file_doc, "file_url", None)).strip() or None,
        "file_hash": file_hash,
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


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, dict):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def _write_audit_log(
    outcome: str,
    *,
    transaction_name: str,
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
                    "reference_doctype": "Maybank QR Transaction",
                    "reference_name": transaction_name,
                    "content": frappe.as_json(log_payload),
                }
            )
            comment.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Manual QR receipt audit log failed")
