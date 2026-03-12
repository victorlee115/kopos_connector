from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, nowdate

from kopos_connector.api.devices import get_device_doc


def _set_custom_field_value(doc: Any, fieldname: str, value: str) -> None:
    try:
        if hasattr(doc, fieldname):
            setattr(doc, fieldname, value)
    except Exception:
        pass


def _find_by_idempotency(doctype: str, idempotency_key: str) -> str | None:
    for fieldname in ("custom_kopos_idempotency_key",):
        try:
            existing = frappe.db.get_value(
                doctype, {fieldname: idempotency_key, "docstatus": 1}, "name"
            )
            if existing:
                return existing
        except Exception:
            continue

    try:
        matches = frappe.get_all(
            doctype,
            filters={
                "remarks": ["like", f"KoPOS idempotency_key: {idempotency_key}%"],
                "docstatus": 1,
            },
            pluck="name",
            limit=1,
        )
        return matches[0] if matches else None
    except Exception:
        return None


def _get_cash_mode_of_payment(pos_profile: Any) -> str:
    payments = pos_profile.get("payments") or []
    for payment in payments:
        mode = frappe.utils.cstr(getattr(payment, "mode_of_payment", ""))
        if mode.strip().lower() == "cash":
            return mode

    default_mode = next(
        (
            frappe.utils.cstr(getattr(payment, "mode_of_payment", ""))
            for payment in payments
            if getattr(payment, "default", 0)
        ),
        "",
    )
    if default_mode:
        return default_mode

    first_mode = next(
        (
            frappe.utils.cstr(getattr(payment, "mode_of_payment", ""))
            for payment in payments
            if frappe.utils.cstr(getattr(payment, "mode_of_payment", ""))
        ),
        "",
    )
    if first_mode:
        return first_mode

    frappe.throw(
        _("POS Profile {0} must define at least one payment mode").format(
            pos_profile.name
        ),
        frappe.ValidationError,
    )
    return ""


def _doc_value(doc: Any, fieldname: str) -> Any:
    if hasattr(doc, fieldname):
        return getattr(doc, fieldname)
    getter = getattr(doc, "get", None)
    if callable(getter):
        return getter(fieldname)
    return None


def _find_opening_entry_name(
    pos_profile_name: str,
    staff_id: str,
    device_id: str,
    shift_id: str | None = None,
    require_open: bool = False,
    allow_device_fallback: bool = True,
) -> str | None:
    filters: dict[str, Any] = {
        "pos_profile": pos_profile_name,
        "user": staff_id,
        "docstatus": 1,
    }
    if require_open:
        filters["status"] = "Open"

    if shift_id:
        try:
            existing = frappe.db.get_value(
                "POS Opening Entry",
                {**filters, "custom_kopos_shift_id": shift_id},
                "name",
            )
            if existing:
                return existing
        except Exception:
            pass

    if allow_device_fallback:
        try:
            existing = frappe.db.get_value(
                "POS Opening Entry",
                {**filters, "custom_kopos_device_id": device_id},
                "name",
            )
            if existing:
                return existing
        except Exception:
            pass

    if not shift_id:
        return None

    matches = frappe.get_all(
        "POS Opening Entry",
        filters=filters,
        fields=["name", "remarks"],
        limit=20,
    )
    shift_marker = f"KoPOS shift_id: {shift_id}"
    device_marker = f"KoPOS device_id: {device_id}"
    for row in matches:
        remarks = frappe.utils.cstr(row.get("remarks"))
        if shift_marker in remarks and device_marker in remarks:
            return row.get("name")
    return None


def _find_closing_entry_name(shift_id: str) -> str | None:
    try:
        return frappe.db.get_value(
            "POS Closing Entry",
            {"custom_kopos_shift_id": shift_id, "docstatus": 1},
            "name",
        )
    except Exception:
        return None


def open_shift_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a POS Opening Entry for a KoPOS shift."""
    idempotency_key = frappe.utils.cstr(payload.get("idempotency_key"))
    device_id = frappe.utils.cstr(payload.get("device_id"))
    staff_id = frappe.utils.cstr(payload.get("staff_id"))
    shift_id = frappe.utils.cstr(payload.get("shift_id"))
    opening_float_sen = flt(payload.get("opening_float_sen", 0))
    opened_at = frappe.utils.cstr(payload.get("opened_at"))

    if not idempotency_key:
        frappe.throw(_("idempotency_key is required"), frappe.ValidationError)
    if not device_id:
        frappe.throw(_("device_id is required"), frappe.ValidationError)
    if not staff_id:
        frappe.throw(_("staff_id is required"), frappe.ValidationError)
    if not shift_id:
        frappe.throw(_("shift_id is required"), frappe.ValidationError)
    if opening_float_sen < 0:
        frappe.throw(
            _("opening_float_sen must be non-negative"), frappe.ValidationError
        )

    device_doc = get_device_doc(device_id=device_id)
    if not frappe.db.get_value("KoPOS Device", device_doc.name, "enabled"):
        frappe.throw(
            _("KoPOS Device {0} is disabled").format(device_id),
            frappe.ValidationError,
        )

    pos_profile_name = device_doc.pos_profile
    if not pos_profile_name:
        frappe.throw(
            _("KoPOS Device {0} has no POS Profile configured").format(device_id),
            frappe.ValidationError,
        )

    pos_profile = frappe.get_cached_doc("POS Profile", pos_profile_name)
    company = pos_profile.company
    if not company:
        frappe.throw(
            _("POS Profile {0} has no company configured").format(pos_profile_name),
            frappe.ValidationError,
        )

    if not frappe.db.exists("User", staff_id):
        frappe.throw(
            _("User {0} not found in ERPNext").format(staff_id),
            frappe.ValidationError,
        )

    existing_by_idempotency = _find_by_idempotency("POS Opening Entry", idempotency_key)
    if existing_by_idempotency:
        return {
            "status": "duplicate",
            "pos_opening_entry": existing_by_idempotency,
            "message": _("Shift already opened"),
        }

    existing_by_shift = _find_opening_entry_name(
        pos_profile_name=pos_profile_name,
        staff_id=staff_id,
        device_id=device_id,
        shift_id=shift_id,
        require_open=False,
        allow_device_fallback=False,
    )
    if existing_by_shift:
        return {
            "status": "duplicate",
            "pos_opening_entry": existing_by_shift,
            "message": _("Shift already opened"),
        }

    existing_open = _find_opening_entry_name(
        pos_profile_name=pos_profile_name,
        staff_id=staff_id,
        device_id=device_id,
        require_open=True,
    )
    if existing_open:
        frappe.throw(
            _("An open shift already exists for user {0} on device {1}").format(
                staff_id, device_id
            ),
            frappe.ValidationError,
        )

    period_start = frappe.utils.get_datetime(opened_at) if opened_at else now_datetime()
    posting_date = period_start.date() if hasattr(period_start, "date") else nowdate()

    opening_amount = flt(opening_float_sen) / 100

    remarks = (
        f"KoPOS idempotency_key: {idempotency_key}\n"
        f"KoPOS shift_id: {shift_id}\n"
        f"KoPOS device_id: {device_id}"
    )

    cash_mode = _get_cash_mode_of_payment(pos_profile)

    doc = frappe.get_doc(
        {
            "doctype": "POS Opening Entry",
            "pos_profile": pos_profile_name,
            "company": company,
            "user": staff_id,
            "period_start_date": period_start,
            "posting_date": posting_date,
            "remarks": remarks,
            "balance_details": [
                {"mode_of_payment": cash_mode, "opening_amount": opening_amount}
            ],
        }
    )

    _set_custom_field_value(doc, "custom_kopos_idempotency_key", idempotency_key)
    _set_custom_field_value(doc, "custom_kopos_shift_id", shift_id)
    _set_custom_field_value(doc, "custom_kopos_device_id", device_id)

    doc.insert(ignore_permissions=True)
    doc.submit()

    return {
        "status": "ok",
        "pos_opening_entry": doc.name,
    }


def close_shift_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a POS Closing Entry for a KoPOS shift."""
    idempotency_key = frappe.utils.cstr(payload.get("idempotency_key"))
    device_id = frappe.utils.cstr(payload.get("device_id"))
    staff_id = frappe.utils.cstr(payload.get("staff_id"))
    shift_id = frappe.utils.cstr(payload.get("shift_id"))
    pos_opening_entry = frappe.utils.cstr(payload.get("pos_opening_entry")) or None
    counted_cash_sen = flt(payload.get("counted_cash_sen", 0))
    discrepancy_note = frappe.utils.cstr(payload.get("discrepancy_note") or "")
    closed_at = frappe.utils.cstr(payload.get("closed_at"))

    if not idempotency_key:
        frappe.throw(_("idempotency_key is required"), frappe.ValidationError)
    if not device_id:
        frappe.throw(_("device_id is required"), frappe.ValidationError)
    if not staff_id:
        frappe.throw(_("staff_id is required"), frappe.ValidationError)
    if not shift_id:
        frappe.throw(_("shift_id is required"), frappe.ValidationError)
    if counted_cash_sen < 0:
        frappe.throw(_("counted_cash_sen must be non-negative"), frappe.ValidationError)

    device_doc = get_device_doc(device_id=device_id)
    if not frappe.db.get_value("KoPOS Device", device_doc.name, "enabled"):
        frappe.throw(
            _("KoPOS Device {0} is disabled").format(device_id),
            frappe.ValidationError,
        )

    pos_profile_name = device_doc.pos_profile
    if not pos_profile_name:
        frappe.throw(
            _("KoPOS Device {0} has no POS Profile configured").format(device_id),
            frappe.ValidationError,
        )

    existing_by_idempotency = _find_by_idempotency("POS Closing Entry", idempotency_key)
    if existing_by_idempotency:
        return {
            "status": "duplicate",
            "pos_closing_entry": existing_by_idempotency,
            "message": _("Shift already closed"),
        }

    existing_by_shift = _find_closing_entry_name(shift_id)
    if existing_by_shift:
        return {
            "status": "duplicate",
            "pos_closing_entry": existing_by_shift,
            "message": _("Shift already closed"),
        }

    if not pos_opening_entry:
        pos_opening_entry = _find_opening_entry_name(
            pos_profile_name=pos_profile_name,
            staff_id=staff_id,
            device_id=device_id,
            shift_id=shift_id,
            require_open=True,
        )

    if not pos_opening_entry:
        pos_opening_entry = _find_opening_entry_name(
            pos_profile_name=pos_profile_name,
            staff_id=staff_id,
            device_id=device_id,
            require_open=True,
        )

    if not pos_opening_entry:
        frappe.throw(
            _("No open POS Opening Entry found for device {0}").format(device_id),
            frappe.ValidationError,
        )

    opening_entry = frappe.get_doc("POS Opening Entry", pos_opening_entry)
    if opening_entry.docstatus != 1:
        frappe.throw(
            _("POS Opening Entry {0} is not submitted").format(pos_opening_entry),
            frappe.ValidationError,
        )
    if opening_entry.status != "Open":
        frappe.throw(
            _("POS Opening Entry {0} is not open").format(pos_opening_entry),
            frappe.ValidationError,
        )
    if frappe.utils.cstr(opening_entry.pos_profile) != pos_profile_name:
        frappe.throw(
            _("POS Opening Entry {0} does not belong to POS Profile {1}").format(
                pos_opening_entry, pos_profile_name
            ),
            frappe.ValidationError,
        )
    if staff_id and frappe.utils.cstr(opening_entry.user) != staff_id:
        frappe.throw(
            _("POS Opening Entry {0} does not belong to user {1}").format(
                pos_opening_entry, staff_id
            ),
            frappe.ValidationError,
        )
    opening_device_id = frappe.utils.cstr(
        _doc_value(opening_entry, "custom_kopos_device_id")
    )
    if opening_device_id and opening_device_id != device_id:
        frappe.throw(
            _("POS Opening Entry {0} does not belong to device {1}").format(
                pos_opening_entry, device_id
            ),
            frappe.ValidationError,
        )
    opening_shift_id = frappe.utils.cstr(
        _doc_value(opening_entry, "custom_kopos_shift_id")
    )
    if opening_shift_id and opening_shift_id != shift_id:
        frappe.throw(
            _("POS Opening Entry {0} does not belong to shift {1}").format(
                pos_opening_entry, shift_id
            ),
            frappe.ValidationError,
        )

    existing_close = frappe.db.exists(
        "POS Closing Entry",
        {"pos_opening_entry": pos_opening_entry, "docstatus": 1},
    )
    if existing_close:
        return {
            "status": "duplicate",
            "pos_closing_entry": existing_close,
            "message": _("Shift already closed"),
        }

    period_end = frappe.utils.get_datetime(closed_at) if closed_at else now_datetime()
    posting_date = period_end.date() if hasattr(period_end, "date") else nowdate()

    counted_amount = flt(counted_cash_sen) / 100
    cash_mode = _get_cash_mode_of_payment(
        frappe.get_cached_doc("POS Profile", opening_entry.pos_profile)
    )

    balance_details = []
    for row in opening_entry.balance_details:
        mode = row.mode_of_payment
        opening_amt = flt(row.opening_amount)
        if frappe.utils.cstr(mode) == cash_mode:
            balance_details.append(
                {
                    "mode_of_payment": mode,
                    "opening_amount": opening_amt,
                    "closing_amount": counted_amount,
                }
            )
        else:
            balance_details.append(
                {
                    "mode_of_payment": mode,
                    "opening_amount": opening_amt,
                    "closing_amount": opening_amt,
                }
            )

    remarks = (
        f"KoPOS idempotency_key: {idempotency_key}\n"
        f"KoPOS shift_id: {shift_id}\n"
        f"KoPOS device_id: {device_id}"
    )
    if discrepancy_note:
        remarks = f"{remarks}\n{discrepancy_note}"

    closing_doc = frappe.get_doc(
        {
            "doctype": "POS Closing Entry",
            "pos_opening_entry": pos_opening_entry,
            "pos_profile": opening_entry.pos_profile,
            "company": opening_entry.company,
            "user": opening_entry.user,
            "period_end_date": period_end,
            "posting_date": posting_date,
            "remarks": remarks,
            "balance_details": balance_details,
        }
    )

    _set_custom_field_value(
        closing_doc, "custom_kopos_idempotency_key", idempotency_key
    )
    _set_custom_field_value(closing_doc, "custom_kopos_shift_id", shift_id)
    _set_custom_field_value(closing_doc, "custom_kopos_device_id", device_id)

    closing_doc.insert(ignore_permissions=True)
    closing_doc.submit()

    return {
        "status": "ok",
        "pos_closing_entry": closing_doc.name,
        "pos_opening_entry": pos_opening_entry,
    }
