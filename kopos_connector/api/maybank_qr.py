from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import (
    add_to_date,
    cint,
    cstr,
    flt,
    get_datetime,
    get_system_timezone,
    now_datetime,
)

from kopos_connector.services.maybank.client import MaybankClient

STATUS_MAP = {
    "0": "failed",
    "1": "paid",
    "2": "pending",
    "3": "scanned",
    "4": "failed",
    "6": "timeout",
}
MAX_AMOUNT_SEN = 10_000_000
MAX_QR_PER_MINUTE = 10
DEFAULT_QR_TTL_SECONDS = 60
GRACE_SECONDS = 30
PAID_TRANSACTION_MESSAGE = "payment already completed for this order"
REUSABLE_STATUSES = ("pending", "scanned")
UNKNOWN_STATUS = "unknown"


def _scoped_idempotency_key(device_id: str, idempotency_key: str) -> str:
    return f"{device_id}:{idempotency_key}"


def _serialize_site_datetime(value: Any) -> str:
    dt = get_datetime(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(get_system_timezone()))
    return dt.isoformat()


def _coerce_site_datetime(value: Any) -> Any:
    dt = get_datetime(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(get_system_timezone()))
    return dt


def _load_existing_txn(device_id: str, idempotency_key: str) -> Any:
    fields = [
        "name",
        "transaction_refno",
        "status",
        "qr_data",
        "sale_amount",
        "sale_amount_sen",
        "expires_at",
        "device_id",
    ]
    existing = frappe.db.get_value(
        "Maybank QR Transaction",
        {"idempotency_scope_key": _scoped_idempotency_key(device_id, idempotency_key)},
        fields,
        as_dict=True,
    )
    if existing:
        return existing
    return frappe.db.get_value(
        "Maybank QR Transaction",
        {"idempotency_key": idempotency_key, "device_id": device_id},
        fields,
        as_dict=True,
    )


def _build_existing_txn_response(existing: Any) -> dict[str, Any]:
    return {
        "status": "ok",
        "qr_data": cstr(_existing_value(existing, "qr_data")),
        "transaction_refno": cstr(_existing_value(existing, "transaction_refno")),
        "sale_amount": cstr(_existing_value(existing, "sale_amount")),
        "expires_at": _serialize_site_datetime(_existing_value(existing, "expires_at")),
    }


def _existing_value(existing: Any, fieldname: str) -> Any:
    if isinstance(existing, dict):
        return existing.get(fieldname)
    return getattr(existing, fieldname, None)


def _delete_existing_txn(name: str) -> None:
    frappe.db.sql(
        "DELETE FROM `tabMaybank QR Transaction` WHERE name = %s",
        (name,),
    )
    frappe.db.commit()


def _resolve_existing_txn(
    device_id: str, idempotency_key: str, amount_sen: int, now: Any
) -> dict[str, Any] | None:
    existing = _load_existing_txn(device_id, idempotency_key)
    if not existing:
        return None

    existing_device_id = cstr(_existing_value(existing, "device_id"))
    if existing_device_id and existing_device_id != device_id:
        frappe.throw("existing transaction belongs to another device")

    existing_amount_sen = cint(_existing_value(existing, "sale_amount_sen"))
    if existing_amount_sen and existing_amount_sen != amount_sen:
        frappe.throw("existing transaction amount does not match idempotency key")

    status = cstr(_existing_value(existing, "status"))
    if status in REUSABLE_STATUSES:
        expires_at = _coerce_site_datetime(_existing_value(existing, "expires_at"))
        if expires_at <= now:
            txn = frappe.get_doc(
                "Maybank QR Transaction", _existing_value(existing, "name")
            )
            try:
                _poll_txn_status(txn)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "Maybank existing transaction refresh failed",
                )
            existing = _load_existing_txn(device_id, idempotency_key)
            if not existing:
                return None
            status = cstr(_existing_value(existing, "status"))
            expires_at = _coerce_site_datetime(_existing_value(existing, "expires_at"))

        if (
            status in REUSABLE_STATUSES
            and add_to_date(expires_at, seconds=GRACE_SECONDS) > now
        ):
            return _build_existing_txn_response(existing)

    if status == "paid":
        frappe.throw(PAID_TRANSACTION_MESSAGE)

    _delete_existing_txn(cstr(_existing_value(existing, "name")))
    return None


def _extract_status_entry(result: dict[str, Any]) -> dict[str, Any] | None:
    data = result.get("data")
    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    return None


def _poll_txn_status(txn: Any) -> None:
    client = MaybankClient.from_settings()
    result = client.check_status(txn.transaction_refno)
    entry = _extract_status_entry(result)
    if entry is None:
        frappe.log_error(
            f"Maybank empty response for {txn.transaction_refno}",
            "Maybank on-demand poll: empty data",
        )
        return

    raw_status = entry.get("status", "")
    new_status = STATUS_MAP.get(str(raw_status), UNKNOWN_STATUS)
    if new_status != txn.status:
        _update_txn_status(txn.name, new_status, cint(raw_status), result)
        frappe.db.commit()
        txn.reload()
        return

    frappe.db.sql(
        "UPDATE `tabMaybank QR Transaction` SET last_polled_at = %s, poll_count = poll_count + 1, raw_response = %s WHERE name = %s",
        (now_datetime(), frappe.as_json(result), txn.name),
    )
    frappe.db.commit()


def _extract_expiry_seconds(qr_entry: dict[str, Any]) -> int:
    raw_expiry = (
        qr_entry.get("expires_in_seconds")
        or qr_entry.get("expiresInSeconds")
        or qr_entry.get("ttl_seconds")
        or qr_entry.get("ttlSeconds")
    )
    expires_in_seconds = (
        cint(raw_expiry) if raw_expiry is not None else DEFAULT_QR_TTL_SECONDS
    )
    return expires_in_seconds if expires_in_seconds > 0 else DEFAULT_QR_TTL_SECONDS


def _generate_qr_payload(
    client: MaybankClient, amount_rm: str, now: Any
) -> tuple[dict[str, Any], str, str, Any]:
    result = client.generate_qr(amount_rm)

    if result.get("status") != "QR000":
        frappe.throw(
            f"Maybank QR generation failed: {result.get('text', 'Unknown error')}"
        )

    data = result.get("data")
    if not data or not isinstance(data, list) or len(data) == 0:
        frappe.throw("Maybank returned empty data for QR generation")

    qr_entry = data[0]
    if not isinstance(qr_entry, dict):
        frappe.throw("Maybank returned invalid QR data")

    refno = cstr(qr_entry.get("transaction_refno", ""))
    qr_data = cstr(
        qr_entry.get("qr_data", qr_entry.get("qr_code", qr_entry.get("qrString", "")))
    )

    if not refno:
        frappe.throw("Maybank returned empty transaction reference")
    if not qr_data:
        frappe.throw("Maybank returned empty QR data")

    expires_at = add_to_date(now, seconds=_extract_expiry_seconds(qr_entry))
    return result, refno, qr_data, expires_at


def generate_maybank_qr_payload(payload: dict[str, Any]) -> dict[str, Any]:
    amount_sen = cint(payload.get("amount_sen", 0))
    device_id = cstr(payload.get("device_id"))
    idempotency_key = cstr(payload.get("idempotency_key"))

    if amount_sen <= 0 or amount_sen > MAX_AMOUNT_SEN:
        frappe.throw("amount_sen must be between 1 and 10000000")
    if not idempotency_key:
        frappe.throw("idempotency_key is required")
    if not device_id:
        frappe.throw("device_id is required")

    now = now_datetime()
    existing_response = _resolve_existing_txn(
        device_id, idempotency_key, amount_sen, _coerce_site_datetime(now)
    )
    if existing_response:
        return existing_response

    _check_rate_limit(device_id)

    client = MaybankClient.from_settings()
    amount_rm = f"{amount_sen / 100:.2f}"
    for _ in range(2):
        current_now = now_datetime()
        result, refno, qr_data, expires_at = _generate_qr_payload(
            client, amount_rm, current_now
        )

        txn = frappe.get_doc(
            {
                "doctype": "Maybank QR Transaction",
                "transaction_refno": refno,
                "outlet_id": client.outlet_id,
                "sale_amount": flt(amount_rm, 2),
                "sale_amount_sen": amount_sen,
                "qr_data": qr_data,
                "status": "pending",
                "maybank_status": 2,
                "device_id": device_id,
                "idempotency_key": idempotency_key,
                "idempotency_scope_key": _scoped_idempotency_key(
                    device_id, idempotency_key
                ),
                "round_number": 1,
                "created_at": current_now,
                "expires_at": expires_at,
                "raw_response": frappe.as_json(result),
            }
        )
        try:
            txn.insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            frappe.db.rollback()
            existing_response = _resolve_existing_txn(
                device_id,
                idempotency_key,
                amount_sen,
                _coerce_site_datetime(now_datetime()),
            )
            if existing_response:
                return existing_response
            continue

        return {
            "status": "ok",
            "qr_data": qr_data,
            "transaction_refno": refno,
            "sale_amount": amount_rm,
            "expires_at": _serialize_site_datetime(expires_at),
        }

    existing_response = _resolve_existing_txn(
        device_id, idempotency_key, amount_sen, _coerce_site_datetime(now_datetime())
    )
    if existing_response:
        return existing_response
    raise frappe.DuplicateEntryError


def check_maybank_payment_payload(
    transaction_refno: str, device_id: str | None = None
) -> dict[str, Any]:
    if not transaction_refno:
        frappe.throw("transaction_refno is required")

    filters: dict[str, Any] = {"transaction_refno": transaction_refno}
    if device_id:
        filters["device_id"] = device_id

    txn_name = frappe.db.get_value("Maybank QR Transaction", filters, "name")
    if not txn_name:
        frappe.throw("Transaction not found", frappe.ValidationError)

    txn = frappe.get_doc("Maybank QR Transaction", txn_name)

    if txn.status in ("pending", "scanned"):
        last_poll = txn.last_polled_at or txn.created_at
        if (now_datetime() - last_poll).total_seconds() > 2:
            try:
                _poll_txn_status(txn)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(), "Maybank on-demand poll failed"
                )

    return {
        "status": txn.status,
        "transaction_refno": txn.transaction_refno,
        "sale_amount": cstr(txn.sale_amount),
        "paid_at": _serialize_site_datetime(txn.paid_at) if txn.paid_at else None,
    }


def _update_txn_status(
    name: str, status: str, raw_status: int, raw_response: dict[str, Any]
) -> None:
    updates: dict[str, Any] = {
        "status": status,
        "maybank_status": raw_status,
        "last_polled_at": now_datetime(),
        "poll_count": cint(
            frappe.db.get_value("Maybank QR Transaction", name, "poll_count")
        )
        + 1,
        "raw_response": frappe.as_json(raw_response),
    }
    if status == "scanned" and not frappe.db.get_value(
        "Maybank QR Transaction", name, "scanned_at"
    ):
        updates["scanned_at"] = now_datetime()
    elif status == "paid":
        updates["paid_at"] = now_datetime()

    frappe.db.set_value("Maybank QR Transaction", name, updates)

    txn = frappe.get_doc("Maybank QR Transaction", name)
    frappe.publish_realtime(
        "maybank_payment_status",
        {
            "transaction_refno": txn.transaction_refno,
            "status": status,
        },
        user=txn.owner,
    )


def _check_rate_limit(device_id: str) -> None:
    recent = frappe.db.count(
        "Maybank QR Transaction",
        filters={
            "device_id": device_id,
            "created_at": [">", add_to_date(now_datetime(), minutes=-1)],
        },
    )
    if recent >= MAX_QR_PER_MINUTE:
        frappe.throw("QR generation rate limit exceeded. Try again shortly.")
