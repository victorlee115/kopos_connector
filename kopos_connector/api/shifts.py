# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

frappe = importlib.import_module("frappe")
frappe_utils = importlib.import_module("frappe.utils")

_ = getattr(frappe, "_")
cint = frappe_utils.cint
cstr = frappe_utils.cstr
flt = frappe_utils.flt
now_datetime = frappe_utils.now_datetime

from kopos_connector.api.devices import elevate_device_api_user, get_device_doc
from kopos_connector.kopos.api.money_contract import (
    MoneyContractValidationError,
    parse_sen,
    persisted_money_to_sen,
    sen_to_decimal,
)
from kopos_connector.utils.diagnostics import log_sanitized_error, sanitized_error_message


MANAGER_APPROVAL_FIELD = "custom_kopos_approved_by_manager"


# -----------------------------------------------------------------------------
# Phase 6 - Offline Event Timestamp Validation Constants
# -----------------------------------------------------------------------------

# Past shift events are valid offline work. Only reject tablet clocks that are
# materially ahead of the ERP site clock.
MAX_FUTURE_TIMESTAMP_SKEW_SECONDS = 300

# Module logger for audit logging
_audit_logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Security Helpers for Phase 1 & 2 - Server-Side Identity & Permission Enforcement
# -----------------------------------------------------------------------------


def _resolve_device_user(device_doc: Any, staff_id: str) -> Any:
    """
    Resolve the device user row for a given device and staff_id.

    Args:
        device_doc: The KoPOS Device document
        staff_id: The ERP user email/ID

    Returns:
        The device user row from device_doc.device_users

    Raises:
        frappe.ValidationError: If staff_id is not assigned to this device
    """
    staff_id = cstr(staff_id).strip()
    if not staff_id:
        frappe.throw(_("staff_id is required"), frappe.ValidationError)

    for row in device_doc.device_users or []:
        if cstr(row.user).strip() == staff_id:
            return row

    frappe.throw(
        _("User {0} is not assigned to KoPOS Device {1}").format(
            staff_id, cstr(device_doc.device_id).strip()
        ),
        frappe.ValidationError,
    )
    return None  # Never reached, but helps type checkers


def _validate_device_user_active(device_user_row: Any, staff_id: str) -> None:
    """
    Validate that the device user row is active.

    Raises:
        frappe.ValidationError: If the device user row is inactive
    """
    if not cint(device_user_row.active):
        frappe.throw(
            _("User {0} is not active on this device").format(staff_id),
            frappe.ValidationError,
        )


def _validate_erp_user_enabled(staff_id: str) -> None:
    """
    Validate that the ERP User exists and is enabled.

    Raises:
        frappe.ValidationError: If the ERP user does not exist or is disabled
    """
    if not frappe.db.exists("User", staff_id):
        frappe.throw(
            _("User {0} not found in ERPNext").format(staff_id),
            frappe.ValidationError,
        )

    user_enabled = frappe.db.get_value("User", staff_id, "enabled")
    if not cint(user_enabled):
        frappe.throw(
            _("User {0} is disabled in ERPNext").format(staff_id),
            frappe.ValidationError,
        )


def _validate_can_open_shift(device_user_row: Any, staff_id: str) -> None:
    """
    Validate that the device user has permission to open shifts.

    Raises:
        frappe.ValidationError: If the user lacks can_open_shift permission
    """
    if not cint(device_user_row.can_open_shift):
        frappe.throw(
            _("User {0} is not authorized to open shifts on this device").format(
                staff_id
            ),
            frappe.ValidationError,
        )


def _validate_can_close_shift(device_user_row: Any, staff_id: str) -> None:
    """
    Validate that the device user has permission to close shifts.

    Raises:
        frappe.ValidationError: If the user lacks can_close_shift permission
    """
    if not cint(device_user_row.can_close_shift):
        frappe.throw(
            _("User {0} is not authorized to close shifts on this device").format(
                staff_id
            ),
            frappe.ValidationError,
        )


def resolve_and_validate_device_user(
    device_doc: Any,
    staff_id: str,
    *,
    require_open_shift: bool = False,
    require_close_shift: bool = False,
) -> Any:
    """
    Resolve the device user row and perform all security validations.

    This is the main entry point for shift operations to validate:
    1. The staff_id is assigned to the device (Phase 1)
    2. The device user row is active (Phase 1)
    3. The ERP User exists and is enabled (Phase 1)
    4. The user has the required shift permissions (Phase 2)

    Args:
        device_doc: The KoPOS Device document
        staff_id: The ERP user email/ID
        require_open_shift: If True, validate can_open_shift permission
        require_close_shift: If True, validate can_close_shift permission

    Returns:
        The resolved device user row

    Raises:
        frappe.ValidationError: If any validation fails
    """
    # Phase 1: Resolve device user mapping
    device_user_row = _resolve_device_user(device_doc, staff_id)

    # Phase 1: Validate device user row is active
    _validate_device_user_active(device_user_row, staff_id)

    # Phase 1: Validate ERP user exists and is enabled
    _validate_erp_user_enabled(staff_id)

    # Phase 2: Enforce shift permissions
    if require_open_shift:
        _validate_can_open_shift(device_user_row, staff_id)

    if require_close_shift:
        _validate_can_close_shift(device_user_row, staff_id)

    return device_user_row


# -----------------------------------------------------------------------------
# Phase 6 - Offline Event Timestamp Validation
# -----------------------------------------------------------------------------


# Module logger for audit logging
_audit_logger = logging.getLogger("kopos_connector.shift_audit")


def _normalize_offline_event_datetime(
    client_timestamp: str | datetime | None,
    field_name: str,
    max_future_skew_seconds: int = MAX_FUTURE_TIMESTAMP_SKEW_SECONDS,
) -> datetime:
    """Normalize an offline event time and reject only excessive future skew.

    A past timestamp is expected when a tablet reconnects after an outage and is
    preserved exactly. Aware timestamps are converted to the Frappe site timezone;
    naive timestamps remain site-local for compatibility with existing clients.
    """
    server_now = _coerce_to_site_local_naive(now_datetime())

    if not client_timestamp:
        return server_now

    try:
        if isinstance(client_timestamp, str):
            parsed = frappe.utils.get_datetime(client_timestamp)
        elif isinstance(client_timestamp, datetime):
            parsed = client_timestamp
        else:
            raise TypeError(f"{field_name} must be a datetime or ISO timestamp")
    except (TypeError, ValueError, OverflowError):
        frappe.throw(
            _("Invalid {0} timestamp format").format(field_name),
            frappe.ValidationError,
        )
        raise AssertionError(f"frappe.throw did not reject invalid {field_name}")

    if not isinstance(parsed, datetime):
        frappe.throw(
            _("Invalid {0} timestamp format").format(field_name),
            frappe.ValidationError,
        )
        raise AssertionError(f"frappe.throw did not reject invalid {field_name}")

    event_datetime = _coerce_to_site_local_naive(parsed)
    if event_datetime > server_now + timedelta(seconds=max_future_skew_seconds):
        frappe.throw(
            _(
                "{0} cannot be more than {1} minutes in the future relative to the Frappe site time"
            ).format(field_name, max_future_skew_seconds // 60),
            frappe.ValidationError,
        )

    return event_datetime


def _coerce_to_site_local_naive(value: datetime) -> datetime:
    """Convert a datetime to site-local naive format for Frappe DATETIME fields."""
    if not isinstance(value, datetime) or not value.tzinfo:
        return value

    timezone_getter = getattr(frappe_utils, "get_system_timezone", None)
    timezone_name = cstr(timezone_getter() if callable(timezone_getter) else None).strip()
    if not timezone_name:
        timezone_name = cstr(
            getattr(frappe.db, "get_single_value", lambda *_args, **_kwargs: None)(
                "System Settings", "time_zone"
            )
            or "UTC"
        ).strip()

    try:
        site_timezone = ZoneInfo(timezone_name or "UTC")
    except ZoneInfoNotFoundError:
        frappe.throw(
            _("Frappe site timezone {0} is not a valid IANA timezone").format(
                timezone_name
            ),
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw did not reject an invalid site timezone")

    return value.astimezone(site_timezone).replace(tzinfo=None)


def _validate_closed_at_not_before_opened_at(
    shift_doc: Any,
    closed_at: datetime,
) -> None:
    shift_name = cstr(getattr(shift_doc, "name", None)) or "unknown"
    opened_at_value = getattr(shift_doc, "opened_at", None)
    if not opened_at_value:
        frappe.throw(
            _("FB Shift {0} has no persisted opened_at timestamp").format(shift_name),
            frappe.ValidationError,
        )

    try:
        opened_at = frappe.utils.get_datetime(opened_at_value)
    except (TypeError, ValueError, OverflowError):
        frappe.throw(
            _("FB Shift {0} has an invalid opened_at timestamp").format(shift_name),
            frappe.ValidationError,
        )
        raise AssertionError(
            f"frappe.throw did not reject invalid FB Shift {shift_name} opened_at"
        )

    if not isinstance(opened_at, datetime):
        frappe.throw(
            _("FB Shift {0} has an invalid opened_at timestamp").format(shift_name),
            frappe.ValidationError,
        )
        raise AssertionError(
            f"frappe.throw did not reject invalid FB Shift {shift_name} opened_at"
        )

    normalized_opened_at = _coerce_to_site_local_naive(opened_at)
    if closed_at < normalized_opened_at:
        frappe.throw(
            _("closed_at cannot be before FB Shift {0} opened_at").format(shift_name),
            frappe.ValidationError,
        )


# -----------------------------------------------------------------------------
# Phase 7 - Audit Logging
# -----------------------------------------------------------------------------


def _log_shift_audit(
    *,
    action: str,
    device_id: str,
    staff_id: str,
    result: str,
    erp_doc_type: str | None = None,
    erp_doc_name: str | None = None,
    error_message: str | None = None,
    manager_id: str | None = None,
    ip_address: str | None = None,
) -> None:
    """
    Log a shift API action for audit purposes.

    This creates a structured audit log entry for every shift operation,
    including both successful and failed attempts.

    Args:
        action: The action being performed (open_shift, close_shift, reopen_shift)
        device_id: The KoPOS device ID
        staff_id: The requesting staff user ID
        result: "success" or "failure"
        erp_doc_type: The ERP document type created (for example, "FB Shift")
        erp_doc_name: The ERP document name/reference
        error_message: Error message if result is "failure"
        manager_id: The manager who approved the action (if applicable)
        ip_address: The source IP address (if available)
    """
    try:
        ip = ip_address
        if not ip:
            # Try to get IP from frappe.local.request if available
            try:
                request = getattr(frappe.local, "request", None)
                if request:
                    ip = getattr(request, "client_addr", None) or getattr(
                        request, "remote_addr", None
                    )
            except Exception as error:
                log_sanitized_error("KoPOS shift audit IP lookup failed", error)

        audit_entry = {
            "timestamp": now_datetime().isoformat(),
            "action": action,
            "device_id": device_id,
            "staff_id": staff_id,
            "result": result,
            "erp_doc_type": erp_doc_type,
            "erp_doc_name": erp_doc_name,
            "error_message": error_message,
            "manager_id": manager_id,
            "ip_address": ip,
            "api_user": frappe.session.user if hasattr(frappe, "session") else None,
        }

        # Log as JSON for structured logging
        _audit_logger.info(
            "KoPOS Shift Audit: %s", json.dumps(audit_entry, default=str)
        )

        # Also log to frappe's logger for production visibility
        try:
            frappe.logger("kopos_connector.audit").info(
                "Shift audit: action=%s device=%s staff=%s result=%s doc=%s",
                action,
                device_id,
                staff_id,
                result,
                f"{erp_doc_type}:{erp_doc_name}" if erp_doc_type else None,
            )
        except Exception as error:
            log_sanitized_error("KoPOS shift audit logger failed", error)

    except Exception as error:
        # Audit logging should never cause the operation to fail
        logging.getLogger(__name__).warning(
            "KoPOS shift audit failed: %s", sanitized_error_message(error)
        )


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


def _set_custom_field_value(doc: Any, fieldname: str, value: Any) -> None:
    setter = getattr(doc, "set", None)
    if callable(setter):
        setter(fieldname, value)
        return
    setattr(doc, fieldname, value)


def _shift_request_fingerprint(
    operation: str,
    request_scope: dict[str, Any],
) -> str:
    """Hash the exact canonical wire scope used by an open/close mutation."""
    message = json.dumps(
        {"operation": operation, **request_scope},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _validate_shift_retry_proof(
    shift: Any,
    *,
    operation: str,
    idempotency_key: str,
    request_fingerprint: str,
) -> None:
    if operation not in {"open", "close"}:
        raise ValueError(f"Unsupported shift retry operation: {operation}")
    persisted_key = cstr(
        _doc_value(shift, f"{operation}_idempotency_key")
    ).strip()
    persisted_fingerprint = cstr(
        _doc_value(shift, f"{operation}_request_fingerprint")
    ).strip()
    if not persisted_key or not persisted_fingerprint:
        frappe.throw(
            _(
                "Existing FB Shift has no verifiable KoPOS {0} idempotency proof"
            ).format(operation),
            frappe.ValidationError,
        )
    if persisted_key != idempotency_key:
        completed_action = "opened" if operation == "open" else "closed"
        frappe.throw(
            _(
                "FB Shift was already {0} with another idempotency_key"
            ).format(completed_action),
            frappe.ValidationError,
        )
    if persisted_fingerprint != request_fingerprint:
        frappe.throw(
            _(
                "idempotency_key was already used with a different {0} shift payload"
            ).format(operation),
            frappe.ValidationError,
        )


def _ensure_fb_shift_for_kopos_shift(
    *,
    shift_id: str,
    device_id: str,
    staff_id: str,
    company: str,
    warehouse: str,
    opening_amount: Decimal,
    opened_at: Any | None,
    open_idempotency_key: str,
    open_request_fingerprint: str,
    remarks: str | None = None,
    manager_id: str | None = None,
) -> str:
    shift_code = cstr(shift_id).strip()
    if not shift_code:
        frappe.throw(_("shift_id is required"), frappe.ValidationError)

    booth_warehouse = cstr(warehouse).strip()
    if not booth_warehouse:
        frappe.throw(
            _("A booth warehouse is required before opening a KoPOS shift"),
            frappe.ValidationError,
        )

    shift_name = frappe.db.get_value("FB Shift", {"shift_code": shift_code}, "name")
    shift_doc = (
        frappe.get_doc("FB Shift", shift_name)
        if shift_name
        else frappe.new_doc("FB Shift")
    )

    if shift_name:
        locked_shift = _lock_fb_shift(cstr(shift_name))
        if locked_shift:
            for fieldname in (
                "device_id",
                "staff_id",
                "status",
                "open_idempotency_key",
                "open_request_fingerprint",
            ):
                locked_value = _doc_value(locked_shift, fieldname)
                if locked_value is not None:
                    setattr(shift_doc, fieldname, locked_value)
        if cstr(getattr(shift_doc, "device_id", None)).strip() != cstr(
            device_id
        ).strip() or cstr(getattr(shift_doc, "staff_id", None)).strip() != cstr(
            staff_id
        ).strip():
            frappe.throw(
                _("shift_id is already bound to another device or staff user"),
                frappe.ValidationError,
            )
        if cstr(getattr(shift_doc, "status", None)).strip() != "Open":
            frappe.throw(
                _("Existing FB Shift cannot be reopened through open_shift"),
                frappe.ValidationError,
            )
        _validate_shift_retry_proof(
            shift_doc,
            operation="open",
            idempotency_key=open_idempotency_key,
            request_fingerprint=open_request_fingerprint,
        )
        return cstr(shift_doc.name)

    shift_doc.shift_code = shift_code

    shift_doc.device_id = cstr(device_id).strip()
    shift_doc.staff_id = cstr(staff_id).strip()
    shift_doc.company = cstr(company).strip()
    shift_doc.warehouse = booth_warehouse
    shift_doc.status = "Open"
    shift_doc.open_idempotency_key = open_idempotency_key
    shift_doc.open_request_fingerprint = open_request_fingerprint
    shift_doc.opening_float = opening_amount
    shift_doc.expected_cash = opening_amount
    if remarks:
        shift_doc.remarks = _append_remarks(getattr(shift_doc, "remarks", None), remarks)
    if manager_id:
        shift_doc.manager_approved_by = manager_id
    if opened_at:
        shift_doc.opened_at = opened_at

    shift_doc.insert(ignore_permissions=True)

    return cstr(shift_doc.name)


def _find_fb_shift_name(shift_id: str) -> str | None:
    shift_code = cstr(shift_id).strip()
    if not shift_code:
        return None
    try:
        return frappe.db.get_value("FB Shift", {"shift_code": shift_code}, "name")
    except Exception:
        return None


def _find_fb_shift_for_update(shift_id: str) -> Any | None:
    shift_code = cstr(shift_id).strip()
    if not shift_code:
        return None
    rows = frappe.db.sql(
        """
        SELECT name, shift_code, device_id, staff_id, status
             , open_idempotency_key, open_request_fingerprint
             , close_idempotency_key, close_request_fingerprint
        FROM `tabFB Shift`
        WHERE shift_code = %s
        LIMIT 1
        FOR UPDATE
        """,
        (shift_code,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _resolve_fb_shift_reference(value: str | None) -> str | None:
    reference = cstr(value).strip()
    if not reference:
        return None
    try:
        if frappe.db.exists("FB Shift", reference):
            return reference
        return frappe.db.get_value("FB Shift", {"shift_code": reference}, "name")
    except Exception:
        return None


def _append_remarks(existing: str | None, extra: str) -> str:
    current = cstr(existing).strip()
    if not current:
        return extra
    if extra in current:
        return current
    return f"{current}\n{extra}"


def _save_doc(doc: Any) -> None:
    save = getattr(doc, "save", None)
    if callable(save):
        save(ignore_permissions=True)


# -----------------------------------------------------------------------------
# Phase 5 - Manager Approval Token Verification
# -----------------------------------------------------------------------------


def _verify_manager_approval_token_optional(
    token: str | None,
    *,
    device_id: str,
    staff_id: str,
    action: str,
    shift_id: str | None = None,
    resource_id: str | None = None,
    amount_sen: Any = 0,
    context_hash: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any] | None:
    """
    Verify a manager approval token (optional, backward-compatible).

    This function verifies the manager approval token if provided. It is
    currently OPTIONAL to maintain backward compatibility, but logs when
    a token is missing for audit purposes.

    In a future release, the token may become REQUIRED for certain actions.

    Args:
        token: The manager approval token (may be None)
        device_id: Expected device ID
        staff_id: Expected staff ID
        action: Expected action (open_shift, close_shift, reopen_shift)
        shift_id: Expected shift ID (optional)

    Returns:
        Dict with manager_id if token was provided and valid, None otherwise

    Raises:
        frappe.ValidationError: If token is invalid, expired, tampered, or reused
    """
    from kopos_connector.utils.manager_approval import (
        verify_manager_approval_token_optional,
    )

    return verify_manager_approval_token_optional(
        token,
        device_id=device_id,
        staff_id=staff_id,
        action=action,
        shift_id=shift_id,
        resource_id=resource_id,
        amount_sen=amount_sen,
        context_hash=context_hash,
        idempotency_key=idempotency_key,
    )


def _record_manager_approval(doc: Any, manager_id: str) -> None:
    """
    Record the approving manager ID on the ERP document.

    This sets the custom_kopos_approved_by_manager field and also appends
    to the remarks for visibility.

    Args:
        doc: The ERP document to modify
        manager_id: The manager user ID who approved
    """
    # Set custom field if available
    _set_custom_field_value(doc, MANAGER_APPROVAL_FIELD, manager_id)

    # Also add to remarks for visibility (in case custom field isn't visible)
    existing_remarks = cstr(getattr(doc, "remarks", "") or "")
    if "Approved by manager:" not in existing_remarks:
        doc.remarks = f"{existing_remarks}\nApproved by manager: {manager_id}".strip()


def _lock_open_shift_scope(device_doc: Any, staff_id: str) -> None:
    device_name = cstr(getattr(device_doc, "name", None)).strip()
    if not device_name:
        frappe.throw(_("KoPOS Device name is required"), frappe.ValidationError)
    frappe.db.sql(
        "SELECT name FROM `tabKoPOS Device` WHERE name = %s FOR UPDATE",
        (device_name,),
    )
    frappe.db.sql(
        "SELECT name FROM `tabUser` WHERE name = %s FOR UPDATE",
        (cstr(staff_id).strip(),),
    )


def _lock_fb_shift(shift_name: str) -> Any | None:
    rows = frappe.db.sql(
        """
        SELECT
            name, shift_code, device_id, staff_id, status, opened_at,
            closed_at, expected_cash, opening_float,
            open_idempotency_key, open_request_fingerprint,
            close_idempotency_key, close_request_fingerprint
        FROM `tabFB Shift`
        WHERE name = %s
        FOR UPDATE
        """,
        (cstr(shift_name).strip(),),
        as_dict=True,
    )
    return rows[0] if rows else None


def _find_open_shift_conflicts_for_update(
    device_id: str, staff_id: str
) -> list[Any]:
    rows = frappe.db.sql(
        """
        SELECT name, device_id, staff_id
        FROM `tabFB Shift`
        WHERE status = 'Open'
          AND (device_id = %s OR staff_id = %s)
        ORDER BY name
        FOR UPDATE
        """,
        (cstr(device_id).strip(), cstr(staff_id).strip()),
        as_dict=True,
    )
    return list(rows or [])


def open_shift_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Open a KoPOS F&B shift in ERP.

    This function supports an optional manager_approval_token parameter
    for enhanced security. When provided, the token is verified before
    creating the ERP document, and the approving manager is recorded.

    Args:
        payload: Dict containing:
            - idempotency_key: Unique key for idempotency (required)
            - device_id: KoPOS device ID (required)
            - staff_id: ERP user ID (required)
            - shift_id: KoPOS shift ID (required)
            - opening_float_sen: Opening cash amount in sen/cents (optional)
            - opened_at: Actual POS event time. Past offline timestamps are
              preserved; aware values are converted to the Frappe site timezone;
              excessive future skew is rejected (optional, defaults to site now).
            - manager_approval_token: Manager approval token (optional, recommended)

    Returns:
        Dict with status and FB Shift name
    """
    idempotency_key = frappe.utils.cstr(payload.get("idempotency_key")).strip()
    device_id = frappe.utils.cstr(payload.get("device_id")).strip()
    staff_id = frappe.utils.cstr(payload.get("staff_id")).strip()
    shift_id = frappe.utils.cstr(payload.get("shift_id")).strip()
    opening_float_sen = _parse_non_negative_sen(
        payload.get("opening_float_sen", 0), "opening_float_sen"
    )
    opened_at = frappe.utils.cstr(payload.get("opened_at")).strip()
    manager_approval_token = payload.get("manager_approval_token")  # Optional

    if not idempotency_key:
        frappe.throw(_("idempotency_key is required"), frappe.ValidationError)
    if not device_id:
        frappe.throw(_("device_id is required"), frappe.ValidationError)
    if not staff_id:
        frappe.throw(_("staff_id is required"), frappe.ValidationError)
    if not shift_id:
        frappe.throw(_("shift_id is required"), frappe.ValidationError)
    device_doc = get_device_doc(device_id=device_id)
    if not device_doc:
        frappe.throw(
            _("KoPOS Device {0} was not found").format(device_id),
            frappe.ValidationError,
        )

    device_name = cstr(getattr(device_doc, "name", "")).strip()
    if not frappe.db.get_value("KoPOS Device", device_name, "enabled"):
        frappe.throw(
            _("KoPOS Device {0} is disabled").format(device_id),
            frappe.ValidationError,
        )

    pos_profile_name = cstr(getattr(device_doc, "pos_profile", "")).strip()
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
    warehouse = cstr(getattr(pos_profile, "warehouse", None)).strip()
    if not warehouse:
        frappe.throw(
            _("POS Profile {0} has no warehouse configured").format(pos_profile_name),
            frappe.ValidationError,
        )

    opening_amount = Decimal(opening_float_sen) / Decimal(100)
    open_request_fingerprint = _shift_request_fingerprint(
        "open",
        {
            "idempotency_key": idempotency_key,
            "device_id": device_id,
            "staff_id": staff_id,
            "shift_id": shift_id,
            "opening_float_sen": opening_float_sen,
            "opened_at": opened_at or None,
            "reason": cstr(payload.get("reason")).strip() or None,
        },
    )

    # Phase 1 & 2: Validate device user assignment, active status, ERP user enabled,
    # and can_open_shift permission
    resolve_and_validate_device_user(device_doc, staff_id, require_open_shift=True)
    _lock_open_shift_scope(device_doc, staff_id)

    existing_shift_row = _find_fb_shift_for_update(shift_id)
    if existing_shift_row:
        existing_shift = cstr(_doc_value(existing_shift_row, "name")).strip()
        if cstr(_doc_value(existing_shift_row, "device_id")).strip() != device_id:
            frappe.throw(
                _("shift_id is already used by another device"),
                frappe.ValidationError,
            )
        if cstr(_doc_value(existing_shift_row, "staff_id")).strip() != staff_id:
            frappe.throw(
                _("shift_id is already used by another staff user"),
                frappe.ValidationError,
            )
        _validate_shift_retry_proof(
            existing_shift_row,
            operation="open",
            idempotency_key=idempotency_key,
            request_fingerprint=open_request_fingerprint,
        )
        return {
            "status": "duplicate",
            "fb_shift": existing_shift,
            "shift_id": shift_id,
            "message": _("Shift already opened"),
        }

    for conflict in _find_open_shift_conflicts_for_update(device_id, staff_id):
        if cstr(_doc_value(conflict, "device_id")).strip() == device_id:
            frappe.throw(
                _("An open shift already exists on device {0}").format(device_id),
                frappe.ValidationError,
            )
        if cstr(_doc_value(conflict, "staff_id")).strip() == staff_id:
            frappe.throw(
                _("User {0} already has an open shift").format(staff_id),
                frappe.ValidationError,
            )

    from kopos_connector.utils.manager_approval import canonical_context_hash

    manager_approval = _verify_manager_approval_token_optional(
        manager_approval_token,
        device_id=device_id,
        staff_id=staff_id,
        action="open_shift",
        shift_id=shift_id,
        resource_id=shift_id,
        amount_sen=opening_float_sen,
        context_hash=canonical_context_hash(
            {"reason": cstr(payload.get("reason")).strip()}
        ),
        idempotency_key=idempotency_key,
    )

    period_start = _normalize_offline_event_datetime(opened_at, "opened_at")

    remarks = (
        f"KoPOS idempotency_key: {idempotency_key}\n"
        f"KoPOS shift_id: {shift_id}\n"
        f"KoPOS device_id: {device_id}"
    )

    fb_shift = _ensure_fb_shift_for_kopos_shift(
        shift_id=shift_id,
        device_id=device_id,
        staff_id=staff_id,
        company=company,
        warehouse=warehouse,
        opening_amount=opening_amount,
        opened_at=period_start,
        open_idempotency_key=idempotency_key,
        open_request_fingerprint=open_request_fingerprint,
        remarks=remarks,
        manager_id=manager_approval["manager_id"] if manager_approval else None,
    )

    # Phase 7: Audit logging for successful shift open
    _log_shift_audit(
        action="open_shift",
        device_id=device_id,
        staff_id=staff_id,
        result="success",
        erp_doc_type="FB Shift",
        erp_doc_name=fb_shift,
        manager_id=manager_approval.get("manager_id") if manager_approval else None,
    )

    return {
        "status": "ok",
        "fb_shift": fb_shift,
        "shift_id": shift_id,
    }


def _parse_non_negative_sen(value: Any, fieldname: str) -> int:
    try:
        amount_sen = parse_sen(0 if value is None else value, fieldname)
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if amount_sen < 0:
        frappe.throw(
            _("{0} must be non-negative").format(fieldname),
            frappe.ValidationError,
        )
    return amount_sen


def _persisted_money_sen(value: Any, fieldname: str) -> int:
    try:
        return persisted_money_to_sen(
            0 if value is None or value == "" else value,
            fieldname,
        )
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error


def close_shift_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Close a KoPOS F&B shift using the true offline event time.

    Past closed_at values are preserved after site-timezone normalization. Values
    materially in the future or before the persisted FB Shift opened_at are rejected.
    """
    idempotency_key = frappe.utils.cstr(payload.get("idempotency_key")).strip()
    device_id = frappe.utils.cstr(payload.get("device_id")).strip()
    staff_id = frappe.utils.cstr(payload.get("staff_id")).strip()
    shift_id = frappe.utils.cstr(payload.get("shift_id")).strip()
    fb_shift = frappe.utils.cstr(payload.get("fb_shift")).strip() or None
    counted_cash_value = payload.get("counted_cash_sen")
    if counted_cash_value is None:
        frappe.throw(_("counted_cash_sen is required"), frappe.ValidationError)
    counted_cash_sen = _parse_non_negative_sen(
        counted_cash_value, "counted_cash_sen"
    )
    discrepancy_note = frappe.utils.cstr(
        payload.get("discrepancy_note") or ""
    ).strip()
    closed_at = frappe.utils.cstr(payload.get("closed_at")).strip()

    if not idempotency_key:
        frappe.throw(_("idempotency_key is required"), frappe.ValidationError)
    if not device_id:
        frappe.throw(_("device_id is required"), frappe.ValidationError)
    if not staff_id:
        frappe.throw(_("staff_id is required"), frappe.ValidationError)
    if not shift_id:
        frappe.throw(_("shift_id is required"), frappe.ValidationError)
    device_doc = get_device_doc(device_id=device_id)
    if not device_doc:
        frappe.throw(
            _("KoPOS Device {0} was not found").format(device_id),
            frappe.ValidationError,
        )

    device_name = cstr(getattr(device_doc, "name", "")).strip()
    if not frappe.db.get_value("KoPOS Device", device_name, "enabled"):
        frappe.throw(
            _("KoPOS Device {0} is disabled").format(device_id),
            frappe.ValidationError,
        )

    pos_profile_name = cstr(getattr(device_doc, "pos_profile", "")).strip()
    if not pos_profile_name:
        frappe.throw(
            _("KoPOS Device {0} has no POS Profile configured").format(device_id),
            frappe.ValidationError,
        )

    # Phase 1 & 2: Validate device user assignment, active status, ERP user enabled,
    # and can_close_shift permission
    resolve_and_validate_device_user(device_doc, staff_id, require_close_shift=True)

    fb_shift = _resolve_fb_shift_reference(fb_shift) or _find_fb_shift_name(shift_id)
    if not fb_shift:
        frappe.throw(
            _("No open FB Shift found for device {0}").format(device_id),
            frappe.ValidationError,
        )
    close_request_fingerprint = _shift_request_fingerprint(
        "close",
        {
            "idempotency_key": idempotency_key,
            "device_id": device_id,
            "staff_id": staff_id,
            "shift_id": shift_id,
            "fb_shift": fb_shift,
            "counted_cash_sen": counted_cash_sen,
            "closed_at": closed_at or None,
            "discrepancy_note": discrepancy_note or None,
        },
    )

    with elevate_device_api_user():
        locked_shift = _lock_fb_shift(fb_shift)
        shift_doc = frappe.get_doc("FB Shift", fb_shift)
        if locked_shift:
            for fieldname in (
                "shift_code",
                "device_id",
                "staff_id",
                "status",
                "opened_at",
                "closed_at",
                "expected_cash",
                "opening_float",
                "open_idempotency_key",
                "open_request_fingerprint",
                "close_idempotency_key",
                "close_request_fingerprint",
            ):
                locked_value = _doc_value(locked_shift, fieldname)
                if locked_value is not None:
                    setattr(shift_doc, fieldname, locked_value)
        if frappe.utils.cstr(getattr(shift_doc, "device_id", "")) != device_id:
            frappe.throw(
                _("FB Shift {0} does not belong to device {1}").format(
                    fb_shift, device_id
                ),
                frappe.ValidationError,
            )
        if staff_id and frappe.utils.cstr(getattr(shift_doc, "staff_id", "")) != staff_id:
            frappe.throw(
                _("FB Shift {0} does not belong to user {1}").format(
                    fb_shift, staff_id
                ),
                frappe.ValidationError,
            )
        if frappe.utils.cstr(getattr(shift_doc, "shift_code", "")) != shift_id:
            frappe.throw(
                _("FB Shift {0} does not belong to shift {1}").format(
                    fb_shift, shift_id
                ),
                frappe.ValidationError,
            )
        if getattr(shift_doc, "status", None) == "Closed":
            _validate_shift_retry_proof(
                shift_doc,
                operation="close",
                idempotency_key=idempotency_key,
                request_fingerprint=close_request_fingerprint,
            )
            return {
                "status": "duplicate",
                "fb_shift": fb_shift,
                "shift_id": shift_id,
                "message": _("Shift already closed"),
            }
        if getattr(shift_doc, "status", None) != "Open":
            frappe.throw(
                _("FB Shift {0} is not open").format(fb_shift),
                frappe.ValidationError,
            )

        from kopos_connector.kopos.doctype.fb_shift.fb_shift import (
            validate_shift_can_close,
        )

        validate_shift_can_close(fb_shift)

        period_end = _normalize_offline_event_datetime(closed_at, "closed_at")
        _validate_closed_at_not_before_opened_at(shift_doc, period_end)

        counted_amount = sen_to_decimal(counted_cash_sen)

        remarks = (
            f"KoPOS idempotency_key: {idempotency_key}\n"
            f"KoPOS shift_id: {shift_id}\n"
            f"KoPOS device_id: {device_id}"
        )
        if discrepancy_note:
            remarks = f"{remarks}\n{discrepancy_note}"

        shift_doc.closed_at = period_end
        shift_doc.counted_cash = counted_amount
        shift_doc.close_idempotency_key = idempotency_key
        shift_doc.close_request_fingerprint = close_request_fingerprint
        shift_doc.remarks = _append_remarks(getattr(shift_doc, "remarks", None), remarks)
        expected_cash_sen = _persisted_money_sen(
            getattr(shift_doc, "expected_cash", 0),
            f"FB Shift {fb_shift} expected_cash",
        )
        shift_doc.cash_variance = sen_to_decimal(
            counted_cash_sen - expected_cash_sen
        )
        shift_doc.status = "Closing"
        _save_doc(shift_doc)

        shift_doc.status = "Closed"
        _save_doc(shift_doc)

        _log_shift_audit(
            action="close_shift",
            device_id=device_id,
            staff_id=staff_id,
            result="success",
            erp_doc_type="FB Shift",
            erp_doc_name=fb_shift,
        )

        return {
            "status": "ok",
            "fb_shift": fb_shift,
            "shift_id": shift_id,
        }


def get_device_open_shift_payload(device_id: str) -> dict[str, Any] | None:
    """Get the current open FB Shift for a KoPOS device.

    This endpoint allows KoPOS to discover and adopt an existing open shift
    that was created from another device or from ERPNext directly.

    Args:
        device_id: The KoPOS device ID to look up

    Returns:
        Dict with shift data if an open shift exists, None otherwise:
        - fb_shift: The ERPNext FB Shift document name
        - shift_id: The KoPOS shift ID (if stored)
        - device_id: The device ID
        - staff_id: The ERP user who opened the shift
        - opening_float_sen: Opening cash amount in sen/cents
        - opened_at: ISO timestamp when shift was opened
    """
    device_doc = get_device_doc(device_id=device_id)
    if not device_doc:
        return None

    filters: dict[str, Any] = {"device_id": device_id, "status": "Open"}
    fields = ["name", "shift_code", "device_id", "staff_id", "opening_float", "opened_at"]

    try:
        entries = frappe.get_all(
            "FB Shift",
            filters=filters,
            fields=fields,
            order_by="creation desc",
            limit=10,
        )
    except Exception:
        return None

    if not entries:
        return None

    for entry in entries:
        custom_device_id = cstr(entry.get("device_id"))
        if custom_device_id != device_id:
            continue
        return {
            "fb_shift": entry["name"],
            "shift_id": cstr(entry.get("shift_code")) or None,
            "device_id": device_id,
            "staff_id": cstr(entry.get("staff_id")),
            "opening_float_sen": int(round(flt(entry.get("opening_float", 0)) * 100)),
            "opened_at": _format_datetime_iso(entry.get("opened_at")),
        }

    return None


def _format_datetime_iso(value: Any) -> str | None:
    """Convert a datetime value to ISO format string."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)
