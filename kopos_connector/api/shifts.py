from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, now_datetime, nowdate

from kopos_connector.api.devices import elevate_device_api_user, get_device_doc


# -----------------------------------------------------------------------------
# Phase 6 - Timestamp Skew Validation Constants
# -----------------------------------------------------------------------------

# Maximum allowed clock skew between client and server (in seconds)
# 10 minutes is generous for mobile devices that may have slight clock drift
MAX_TIMESTAMP_SKEW_SECONDS = 600  # 10 minutes

# Module logger for audit logging
_audit_logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Phase 5 - Security-Critical Custom Field Constants
# -----------------------------------------------------------------------------

# Security-critical custom fields that must never fail silently.
# These fields are essential for replay protection and ownership verification.
SECURITY_CRITICAL_FIELDS = frozenset(
    {
        "custom_kopos_idempotency_key",
        "custom_kopos_shift_id",
        "custom_kopos_device_id",
    }
)

# Manager approval custom field for audit trail
MANAGER_APPROVAL_FIELD = "custom_kopos_approved_by_manager"

# Module logger for deprecation warnings
_logger = logging.getLogger(__name__)


class SecurityFieldError(frappe.ValidationError):
    """Raised when a security-critical custom field operation fails.

    This error indicates that the KoPOS custom fields may not be properly
    installed or that a security-critical metadata field is unavailable.
    """

    pass


def _log_deprecation_warning(message: str) -> None:
    """Log a deprecation warning for legacy remarks-based lookups.

    This should only be called when falling back to the legacy remarks-based
    path, which is being phased out in favor of direct custom field lookups.

    The remarks-based fallback exists ONLY for compatibility with legacy
    records created before custom fields were installed. It should NEVER
    be used as the primary lookup path for new records.
    """
    _logger.warning(
        "[DEPRECATION] %s - Remarks-based lookup is deprecated and will be "
        "removed in a future version. Ensure KoPOS custom fields are properly "
        "installed by running the install hooks.",
        message,
    )
    # Also log to frappe's logger if available (for production visibility)
    try:
        frappe.logger("kopos_connector.security").warning("[DEPRECATION] %s", message)
    except Exception:
        pass  # Don't fail if frappe logger is unavailable


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
# Phase 6 - Timestamp Skew Validation
# -----------------------------------------------------------------------------


# Module logger for audit logging
_audit_logger = logging.getLogger("kopos_connector.shift_audit")


def _validate_timestamp_skew(
    client_timestamp: str | datetime | None,
    field_name: str,
    max_skew_seconds: int = MAX_TIMESTAMP_SKEW_SECONDS,
) -> datetime:
    """
    Validate that a client-provided timestamp is within acceptable skew of server time.

    Args:
        client_timestamp: The timestamp provided by the client (ISO string or datetime)
        field_name: The field name for error messages (e.g., "opened_at", "closed_at")
        max_skew_seconds: Maximum allowed difference in seconds (default: 600 = 10 minutes)

    Returns:
        The parsed and validated datetime object

    Raises:
        frappe.ValidationError: If timestamp is too far in past or future
    """
    server_now = now_datetime()

    if not client_timestamp:
        return server_now

    # Parse timestamp if string
    parsed: datetime = server_now  # default fallback
    if isinstance(client_timestamp, str):
        try:
            parsed = frappe.utils.get_datetime(client_timestamp)
        except Exception:
            frappe.throw(
                _("Invalid {0} timestamp format").format(field_name),
                frappe.ValidationError,
            )
    elif isinstance(client_timestamp, datetime):
        parsed = client_timestamp
    else:
        parsed = server_now

    parsed_for_compare = parsed
    server_for_compare = server_now

    if parsed.tzinfo and not server_now.tzinfo:
        timezone_name = cstr(
            getattr(frappe.db, "get_single_value", lambda *_args, **_kwargs: None)(
                "System Settings", "time_zone"
            )
            or ""
        ).strip()
        site_tz = ZoneInfo(timezone_name) if timezone_name else None
        if site_tz:
            parsed_for_compare = parsed.astimezone(site_tz)
            server_for_compare = server_now.replace(tzinfo=site_tz)
        else:
            parsed_for_compare = parsed.replace(tzinfo=None)
    elif server_now.tzinfo and not parsed.tzinfo:
        parsed_for_compare = parsed.replace(tzinfo=server_now.tzinfo)

    skew = abs((parsed_for_compare - server_for_compare).total_seconds())

    if skew > max_skew_seconds:
        direction = (
            "in the future"
            if parsed_for_compare > server_for_compare
            else "in the past"
        )
        frappe.throw(
            _(
                "{0} timestamp is too far {1} (skew: {2} seconds, max allowed: {3})"
            ).format(field_name, direction, int(skew), max_skew_seconds),
            frappe.ValidationError,
        )

    return parsed


def _coerce_to_site_local_naive(value: datetime) -> datetime:
    """Convert a datetime to site-local naive format for Frappe DATETIME fields."""
    if not isinstance(value, datetime) or not value.tzinfo:
        return value

    timezone_name = cstr(
        getattr(frappe.db, "get_single_value", lambda *_args, **_kwargs: None)(
            "System Settings", "time_zone"
        )
        or ""
    ).strip()
    if not timezone_name:
        return value.replace(tzinfo=None)

    return value.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)


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
        erp_doc_type: The ERP document type created (e.g., "POS Opening Entry")
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
            except Exception:
                pass

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
        except Exception:
            pass  # Don't fail if frappe logger is unavailable

    except Exception:
        # Audit logging should never cause the operation to fail
        pass


def _set_custom_field_value(doc: Any, fieldname: str, value: str) -> None:
    """Set a custom field value on a document.

    For security-critical fields (idempotency_key, shift_id, device_id),
    this function fails closed - raising SecurityFieldError if the field
    cannot be set. For non-critical fields, it silently ignores failures
    for backward compatibility.

    Args:
        doc: The ERPNext document to modify
        fieldname: The custom field name to set
        value: The value to set

    Raises:
        SecurityFieldError: If a security-critical field cannot be set
    """
    is_security_critical = fieldname in SECURITY_CRITICAL_FIELDS

    if not hasattr(doc, fieldname):
        if is_security_critical:
            frappe.throw(
                _(
                    "Security-critical field {0} is not available on document. "
                    "Ensure KoPOS custom fields are installed correctly."
                ).format(fieldname),
                SecurityFieldError,
            )
        # Non-critical field: silently skip
        return

    try:
        setattr(doc, fieldname, value)
    except Exception as e:
        if is_security_critical:
            frappe.throw(
                _("Failed to set security-critical field {0}: {1}").format(
                    fieldname, str(e)
                ),
                SecurityFieldError,
            )
        # Non-critical field: silently ignore
        pass


def _find_by_idempotency(doctype: str, idempotency_key: str) -> str | None:
    """Find an existing document by idempotency key.

    PRIMARY PATH: Uses custom_kopos_idempotency_key custom field.
    FALLBACK PATH (DEPRECATED): Uses remarks field for legacy records.

    The remarks fallback exists ONLY for compatibility with records created
    before custom fields were installed. It logs a deprecation warning and
    should not be relied upon for new records.

    Args:
        doctype: The ERPNext DocType to search
        idempotency_key: The idempotency key to search for

    Returns:
        The document name if found, None otherwise
    """
    # PRIMARY: Try custom field lookup first
    custom_field_failed = False
    for fieldname in ("custom_kopos_idempotency_key",):
        try:
            existing = frappe.db.get_value(
                doctype, {fieldname: idempotency_key, "docstatus": 1}, "name"
            )
            if existing:
                return existing
        except Exception:
            custom_field_failed = True
            continue

    # FALLBACK (DEPRECATED): Only for legacy records without custom fields
    # This path should rarely be hit for properly installed systems
    if custom_field_failed:
        _log_deprecation_warning(
            f"Custom field lookup failed for {doctype} idempotency check. "
            f"Falling back to remarks-based lookup for idempotency_key={idempotency_key}. "
            f"Please ensure KoPOS custom fields are installed."
        )

    try:
        if not frappe.db.has_column(doctype, "remarks"):
            return None
        matches = frappe.get_all(
            doctype,
            filters={
                "remarks": ["like", f"KoPOS idempotency_key: {idempotency_key}%"],
                "docstatus": 1,
            },
            pluck="name",
            limit=1,
        )
        if matches:
            # Log when we find a legacy record via remarks
            _log_deprecation_warning(
                f"Found {doctype} {matches[0]} via legacy remarks-based lookup. "
                f"This record may have been created before custom fields were installed."
            )
            return matches[0]
    except Exception:
        pass

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

    if not frappe.db.has_column("POS Opening Entry", "remarks"):
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


def _ensure_fb_shift_for_kopos_shift(
    *,
    shift_id: str,
    device_id: str,
    staff_id: str,
    company: str,
    warehouse: str,
    opening_float: float,
    opened_at: Any | None,
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

    if not shift_name:
        shift_doc.shift_code = shift_code

    shift_doc.device_id = cstr(device_id).strip()
    shift_doc.staff_id = cstr(staff_id).strip()
    shift_doc.company = cstr(company).strip()
    shift_doc.warehouse = booth_warehouse
    shift_doc.status = "Open"
    shift_doc.opening_float = flt(opening_float)
    if opened_at:
        shift_doc.opened_at = opened_at

    if shift_name:
        shift_doc.save(ignore_permissions=True)
    else:
        shift_doc.insert(ignore_permissions=True)

    return cstr(shift_doc.name)


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


def open_shift_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a POS Opening Entry for a KoPOS shift.

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
            - opened_at: ISO timestamp for shift open time (optional)
            - manager_approval_token: Manager approval token (optional, recommended)

    Returns:
        Dict with status and pos_opening_entry name
    """
    idempotency_key = frappe.utils.cstr(payload.get("idempotency_key"))
    device_id = frappe.utils.cstr(payload.get("device_id"))
    staff_id = frappe.utils.cstr(payload.get("staff_id"))
    shift_id = frappe.utils.cstr(payload.get("shift_id"))
    opening_float_sen = flt(payload.get("opening_float_sen", 0))
    opened_at = frappe.utils.cstr(payload.get("opened_at"))
    manager_approval_token = payload.get("manager_approval_token")  # Optional

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
    warehouse = cstr(getattr(pos_profile, "warehouse", None)).strip()
    if not warehouse:
        frappe.throw(
            _("POS Profile {0} has no warehouse configured").format(pos_profile_name),
            frappe.ValidationError,
        )

    opening_amount = flt(opening_float_sen) / 100

    # Phase 1 & 2: Validate device user assignment, active status, ERP user enabled,
    # and can_open_shift permission
    resolve_and_validate_device_user(device_doc, staff_id, require_open_shift=True)

    # Phase 5: Verify manager approval token if provided
    # Currently OPTIONAL for backward compatibility, but logs when missing
    manager_approval = _verify_manager_approval_token_optional(
        manager_approval_token,
        device_id=device_id,
        staff_id=staff_id,
        action="open_shift",
        shift_id=shift_id,
    )

    existing_by_idempotency = _find_by_idempotency("POS Opening Entry", idempotency_key)
    if existing_by_idempotency:
        _ensure_fb_shift_for_kopos_shift(
            shift_id=shift_id,
            device_id=device_id,
            staff_id=staff_id,
            company=company,
            warehouse=warehouse,
            opening_float=opening_amount,
            opened_at=opened_at or None,
        )
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
        _ensure_fb_shift_for_kopos_shift(
            shift_id=shift_id,
            device_id=device_id,
            staff_id=staff_id,
            company=company,
            warehouse=warehouse,
            opening_float=opening_amount,
            opened_at=opened_at or None,
        )
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

    period_start = _coerce_to_site_local_naive(
        _validate_timestamp_skew(opened_at, "opened_at")
    )
    posting_date = period_start.date() if hasattr(period_start, "date") else nowdate()

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

    # Phase 5: Record manager approval if provided
    if manager_approval:
        _record_manager_approval(doc, manager_approval["manager_id"])

    doc.insert(ignore_permissions=True)
    doc.submit()
    _ensure_fb_shift_for_kopos_shift(
        shift_id=shift_id,
        device_id=device_id,
        staff_id=staff_id,
        company=company,
        warehouse=warehouse,
        opening_float=opening_amount,
        opened_at=period_start,
    )

    # Phase 7: Audit logging for successful shift open
    _log_shift_audit(
        action="open_shift",
        device_id=device_id,
        staff_id=staff_id,
        result="success",
        erp_doc_type="POS Opening Entry",
        erp_doc_name=doc.name,
        manager_id=manager_approval.get("manager_id") if manager_approval else None,
    )

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

    # Phase 1 & 2: Validate device user assignment, active status, ERP user enabled,
    # and can_close_shift permission
    resolve_and_validate_device_user(device_doc, staff_id, require_close_shift=True)

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

    with elevate_device_api_user():
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

        period_end = _coerce_to_site_local_naive(
            _validate_timestamp_skew(closed_at, "closed_at")
        )
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

        _log_shift_audit(
            action="close_shift",
            device_id=device_id,
            staff_id=staff_id,
            result="success",
            erp_doc_type="POS Closing Entry",
            erp_doc_name=closing_doc.name,
        )

        return {
            "status": "ok",
            "pos_closing_entry": closing_doc.name,
            "pos_opening_entry": pos_opening_entry,
        }


def get_device_open_shift_payload(device_id: str) -> dict[str, Any] | None:
    """Get the current open shift for a KoPOS device.

    This endpoint allows KoPOS to discover and adopt an existing open shift
    that was created from another device or from ERPNext directly.

    Args:
        device_id: The KoPOS device ID to look up

    Returns:
        Dict with shift data if an open shift exists, None otherwise:
        - pos_opening_entry: The ERPNext document name
        - shift_id: The KoPOS shift ID (if stored)
        - device_id: The device ID
        - staff_id: The ERP user who opened the shift
        - opening_float_sen: Opening cash amount in sen/cents
        - opened_at: ISO timestamp when shift was opened
    """
    device_doc = get_device_doc(device_id=device_id)
    pos_profile_name = device_doc.pos_profile
    if not pos_profile_name:
        return None

    filters: dict[str, Any] = {
        "pos_profile": pos_profile_name,
        "docstatus": 1,
        "status": "Open",
    }

    fields = [
        "name",
        "user",
        "period_start_date",
        "custom_kopos_shift_id",
        "custom_kopos_device_id",
    ]

    try:
        entries = frappe.get_all(
            "POS Opening Entry",
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
        custom_device_id = cstr(entry.get("custom_kopos_device_id"))
        if custom_device_id == device_id:
            opening_float_sen = _get_opening_float_sen(entry["name"])
            return {
                "pos_opening_entry": entry["name"],
                "shift_id": cstr(entry.get("custom_kopos_shift_id")) or None,
                "device_id": device_id,
                "staff_id": cstr(entry.get("user")),
                "opening_float_sen": opening_float_sen,
                "opened_at": _format_datetime_iso(entry.get("period_start_date")),
            }

    return None


def _get_opening_float_sen(pos_opening_entry: str) -> int:
    """Get the opening float amount in sen from a POS Opening Entry."""
    try:
        balance_details = frappe.get_all(
            "POS Opening Entry Balance Detail",
            filters={"parent": pos_opening_entry, "parentfield": "balance_details"},
            fields=["opening_amount"],
        )
        total = sum(flt(row.get("opening_amount", 0)) for row in balance_details)
        return int(round(total * 100))
    except Exception:
        return 0


def _format_datetime_iso(value: Any) -> str | None:
    """Convert a datetime value to ISO format string."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)
