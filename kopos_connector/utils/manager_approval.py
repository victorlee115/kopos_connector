"""
Manager approval utilities for privileged POS operations.

This module provides short-lived, HMAC-signed tokens that authorize
privileged shift, void, refund, stock, and device actions.

Security properties:
- Tokens are signed with HMAC-SHA256 using a server-side secret
- Tokens expire after a configurable time (default: 5 minutes)
- Every issued token has a durable database record and is consumed under row lock
- Tokens are tied to an exact device, staff, action, shift, resource, amount, and context
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, cstr, get_datetime, now_datetime

from kopos_connector.kopos.api.money_contract import (
    MAX_SAFE_INTEGER,
    MoneyContractValidationError,
    parse_sen,
)
from kopos_connector.utils.diagnostics import log_sanitized_error
from kopos_connector.utils.pin import hash_pin, pin_hash_needs_upgrade, verify_pin

# Token validity duration in seconds (default: 5 minutes)
DEFAULT_TOKEN_TTL_SECONDS = 300
MIN_TOKEN_TTL_SECONDS = 30
MAX_TOKEN_TTL_SECONDS = 300

MAX_PIN_FAILURES = 5
PIN_FAILURE_WINDOW_SECONDS = 15 * 60
PIN_LOCKOUT_SECONDS = 15 * 60
PIN_RATE_LIMIT_KEY_PREFIX = "kopos:manager-pin-rate"
PIN_RATE_LIMIT_CHECK_SCRIPT = """
local locked_until = tonumber(redis.call('HGET', KEYS[1], 'locked_until') or '0')
local now_epoch = tonumber(ARGV[1])
if locked_until > now_epoch then
    return {1, locked_until}
end
return {0, locked_until}
"""
PIN_RATE_LIMIT_FAILURE_SCRIPT = """
local now_epoch = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local max_failures = tonumber(ARGV[3])
local lockout_seconds = tonumber(ARGV[4])
local window_started = tonumber(redis.call('HGET', KEYS[1], 'window_started') or '0')
local failures = tonumber(redis.call('HGET', KEYS[1], 'failures') or '0')
local locked_until = tonumber(redis.call('HGET', KEYS[1], 'locked_until') or '0')

if locked_until > now_epoch then
    redis.call('EXPIRE', KEYS[1], lockout_seconds)
    return {failures, locked_until}
end
if window_started == 0 or now_epoch - window_started >= window_seconds then
    window_started = now_epoch
    failures = 0
end
failures = failures + 1
locked_until = 0
if failures >= max_failures then
    locked_until = now_epoch + lockout_seconds
end
redis.call(
    'HSET', KEYS[1],
    'window_started', window_started,
    'failures', failures,
    'locked_until', locked_until
)
redis.call('EXPIRE', KEYS[1], math.max(window_seconds, lockout_seconds) + 60)
return {failures, locked_until}
"""
PIN_RATE_LIMIT_CLEAR_SCRIPT = "return redis.call('DEL', KEYS[1])"
DUMMY_PIN_HASH = (
    "scrypt$256$d00df00dd00df00dd00df00dd00df00d$"
    "970c84b0c7a95c6c8f80550ea302c4c2dc682396d317432e7348bcedf9e83df5"
)
APPROVAL_DOCTYPE = "KoPOS Manager Approval"
ACTION_PERMISSION_FIELDS = {
    "void_order": "can_void",
    "refund_order": "can_refund",
}
AUTHORIZATION_MODES = {"device_manager", "system_manager"}


def _get_signing_secret() -> str:
    """
    Get or generate the server-side secret for signing approval tokens.

    The secret is stored in the site config and persists across restarts.
    """
    conf = getattr(frappe, "conf", {}) or {}
    conf_get = getattr(conf, "get", None)
    secret = cstr(
        conf_get("kopos_manager_approval_secret") if callable(conf_get) else ""
    )
    if len(secret) < 32:
        frappe.throw(
            _("kopos_manager_approval_secret must be configured with at least 32 characters"),
            frappe.ValidationError,
        )

    return secret


def _create_token_signature(payload: dict[str, Any]) -> str:
    """Create an HMAC-SHA256 signature for the token payload."""
    secret = _get_signing_secret()
    # Sort keys for deterministic serialization
    message = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return signature


def _encode_token(payload: dict[str, Any], signature: str) -> str:
    """Encode payload and signature into a token string."""
    message = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    # Use hex encoding to avoid URL encoding issues
    encoded_message = message.encode("utf-8").hex()
    return f"v1.{encoded_message}.{signature}"


def _decode_token(token: str) -> tuple[dict[str, Any], str] | None:
    """
    Decode a token string into payload and signature.

    Returns None if the token format is invalid.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            return None

        encoded_message, signature = parts[1], parts[2]
        message = bytes.fromhex(encoded_message).decode("utf-8")
        payload = json.loads(message)
        return payload, signature
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def canonical_context_hash(context: Mapping[str, Any] | None) -> str:
    message = json.dumps(
        dict(context or {}),
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


def parse_integer_sen(value: Any, fieldname: str = "amount_sen") -> int:
    try:
        return parse_sen(value, fieldname)
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error


def parse_persisted_integer_sen(
    value: Any,
    fieldname: str = "amount_sen",
) -> int:
    """Read a Frappe Int value that may materialize as an integral Decimal."""
    if isinstance(value, bool) or value is None:
        frappe.throw(
            _("{0} must be an integer number of sen").format(fieldname),
            frappe.ValidationError,
        )
    try:
        amount = Decimal(cstr(value).strip())
    except (InvalidOperation, ValueError) as error:
        frappe.throw(
            _("{0} must be an integer number of sen").format(fieldname),
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error
    if (
        not amount.is_finite()
        or amount != amount.to_integral_value()
        or abs(amount) > MAX_SAFE_INTEGER
    ):
        frappe.throw(
            _("{0} must be a safe integer number of sen").format(fieldname),
            frappe.ValidationError,
        )
    return int(amount)


def money_to_sen(value: Any, fieldname: str) -> int:
    try:
        amount = Decimal(cstr(value).strip())
    except (InvalidOperation, ValueError):
        frappe.throw(
            _("{0} must be a valid decimal amount").format(fieldname),
            frappe.ValidationError,
        )
        return 0
    if not amount.is_finite():
        frappe.throw(
            _("{0} must be finite").format(fieldname),
            frappe.ValidationError,
        )
    amount_sen = amount * Decimal("100")
    if amount_sen != amount_sen.to_integral_value():
        frappe.throw(
            _("{0} contains fractional sen").format(fieldname),
            frappe.ValidationError,
        )
    return int(amount_sen)


def authorize_manager_for_device(
    device_doc: Any,
    *,
    manager_id: str | None,
    manager_pin: str | None,
    admin_approval: bool = False,
    action: str | None = None,
) -> str:
    """Authorize an explicit System Manager path or a PIN-backed device manager."""
    session_user = cstr(getattr(frappe.session, "user", None)).strip()
    if not session_user or session_user == "Guest":
        frappe.throw(_("Authentication required for manager approval"), frappe.ValidationError)

    from kopos_connector.api.devices import KOPOS_DEVICE_API_ROLE, get_session_roles

    roles = get_session_roles(session_user)
    if admin_approval:
        if KOPOS_DEVICE_API_ROLE in roles or "System Manager" not in roles:
            frappe.throw(
                _(
                    "Explicit admin approval requires a non-device System Manager session"
                ),
                frappe.ValidationError,
            )
        _validate_enabled_erp_user(session_user)
        return session_user

    resolved_manager = cstr(manager_id).strip()
    raw_pin = cstr(manager_pin).strip()
    device_name = cstr(getattr(device_doc, "name", None)).strip()
    if not device_name or not cint(getattr(device_doc, "enabled", 0)):
        frappe.throw(_("KoPOS Device is disabled or invalid"), frappe.ValidationError)

    limiter_identity = resolved_manager or "<missing-manager>"
    _assert_manager_pin_rate_limit(device_name, limiter_identity)
    if not resolved_manager or not raw_pin:
        verify_pin(raw_pin, DUMMY_PIN_HASH)
        _record_manager_pin_rate_limit_failure(device_name, limiter_identity)
        frappe.throw(_("Manager credentials are invalid"), frappe.ValidationError)
    manager_row = _load_manager_row_for_update(device_name, resolved_manager)
    if not manager_row:
        verify_pin(raw_pin, DUMMY_PIN_HASH)
        _record_manager_pin_rate_limit_failure(device_name, resolved_manager)
        frappe.throw(_("Manager credentials are invalid"), frappe.ValidationError)

    if not cint(_row_value(manager_row, "active")) or not cint(
        _row_value(manager_row, "can_manager_override")
    ):
        verify_pin(raw_pin, cstr(_row_value(manager_row, "pin_hash")) or DUMMY_PIN_HASH)
        _record_manager_pin_rate_limit_failure(device_name, resolved_manager)
        frappe.throw(_("Manager credentials are invalid"), frappe.ValidationError)

    now = now_datetime()
    locked_until = _optional_datetime(_row_value(manager_row, "pin_locked_until"))
    if locked_until and locked_until > now:
        frappe.throw(
            _("Manager PIN is temporarily locked; try again later"),
            frappe.ValidationError,
        )

    pin_hash = cstr(_row_value(manager_row, "pin_hash")).strip()
    if not verify_pin(raw_pin, pin_hash):
        _record_manager_pin_rate_limit_failure(device_name, resolved_manager)
        _record_failed_pin_attempt(manager_row, now)
        frappe.throw(_("Manager credentials are invalid"), frappe.ValidationError)

    _validate_enabled_erp_user(resolved_manager)
    _clear_manager_pin_rate_limit(device_name, resolved_manager)
    _validate_device_manager_action_permission(manager_row, action)

    updates: dict[str, Any] = {
        "pin_failed_attempts": 0,
        "pin_last_failed_at": None,
        "pin_locked_until": None,
    }
    if pin_hash_needs_upgrade(pin_hash):
        updates["pin_hash"] = hash_pin(raw_pin)
    _persist_successful_manager_pin_verification(device_doc, manager_row, updates)
    return resolved_manager


def _persist_successful_manager_pin_verification(
    device_doc: Any,
    manager_row: Any,
    updates: dict[str, Any],
) -> None:
    """Persist PIN state and version any verifier change in one transaction."""
    frappe.db.set_value(
        "KoPOS Device User",
        _row_value(manager_row, "name"),
        updates,
        update_modified=False,
    )
    if "pin_hash" not in updates:
        return

    device_name = cstr(getattr(device_doc, "name", None)).strip()
    if not device_name:
        frappe.throw(
            _("KoPOS Device is required for PIN verifier rotation"),
            frappe.ValidationError,
        )
    locked_version = _row_value(manager_row, "device_config_version")
    current_version = cint(
        locked_version
        if locked_version is not None
        else getattr(device_doc, "config_version", 0)
    )
    next_version = max(1, current_version) + 1
    frappe.db.set_value(
        "KoPOS Device",
        device_name,
        {"config_version": next_version},
        update_modified=True,
    )
    device_doc.config_version = next_version


def _load_manager_row_for_update(device_name: str, manager_id: str) -> Any | None:
    rows = frappe.db.sql(
        """
        SELECT
            device_user.name, device_user.user, device_user.active,
            device_user.can_manager_override, device_user.pin_hash,
            device_user.pin_failed_attempts, device_user.pin_last_failed_at,
            device_user.pin_locked_until, device_user.can_void,
            device_user.can_refund,
            device.config_version AS device_config_version
        FROM `tabKoPOS Device User` AS device_user
        INNER JOIN `tabKoPOS Device` AS device
            ON device.name = device_user.parent
        WHERE device_user.parent = %s
          AND device_user.parenttype = 'KoPOS Device'
          AND device_user.parentfield = 'device_users'
          AND device_user.user = %s
        ORDER BY device_user.name
        LIMIT 2
        FOR UPDATE
        """,
        (device_name, manager_id),
        as_dict=True,
    )
    return rows[0] if len(rows or []) == 1 else None


def _validate_device_manager_action_permission(
    manager_row: Any,
    action: str | None,
) -> None:
    required_field = ACTION_PERMISSION_FIELDS.get(cstr(action).strip())
    if required_field and not cint(_row_value(manager_row, required_field)):
        frappe.throw(
            _("Manager is not authorized for {0}").format(
                "voids" if required_field == "can_void" else "refunds"
            ),
            frappe.ValidationError,
        )


def _validate_enabled_erp_user(user_id: str) -> None:
    enabled = frappe.db.get_value("User", user_id, "enabled")
    if not cint(enabled):
        frappe.throw(_("Manager credentials are invalid"), frappe.ValidationError)


def _record_failed_pin_attempt(manager_row: Any, now: Any) -> None:
    """Mirror Redis security state for Desk visibility.

    Redis is authoritative because endpoint error handling rolls this database write
    back. The atomic Redis limiter above deliberately survives that rollback.
    """
    attempts = cint(_row_value(manager_row, "pin_failed_attempts"))
    last_failed_at = _optional_datetime(_row_value(manager_row, "pin_last_failed_at"))
    if not last_failed_at or (now - last_failed_at).total_seconds() >= (
        PIN_FAILURE_WINDOW_SECONDS
    ):
        attempts = 0
    attempts += 1
    locked_until = (
        add_to_date(now, seconds=PIN_LOCKOUT_SECONDS)
        if attempts >= MAX_PIN_FAILURES
        else None
    )
    frappe.db.set_value(
        "KoPOS Device User",
        _row_value(manager_row, "name"),
        {
            "pin_failed_attempts": attempts,
            "pin_last_failed_at": now,
            "pin_locked_until": locked_until,
        },
        update_modified=False,
    )


def _manager_pin_rate_limit_key(device_name: str, manager_id: str) -> str:
    site = cstr(getattr(getattr(frappe, "local", None), "site", None)).strip()
    identity = f"{site}\0{device_name}\0{manager_id}".encode("utf-8")
    return f"{PIN_RATE_LIMIT_KEY_PREFIX}:{hashlib.sha256(identity).hexdigest()}"


def _atomic_manager_pin_rate_limit_eval(
    script: str,
    device_name: str,
    manager_id: str,
    *arguments: int,
) -> Any:
    try:
        cache = frappe.cache()
        make_key = getattr(cache, "make_key", None)
        eval_script = getattr(cache, "eval", None)
        if not callable(make_key) or not callable(eval_script):
            raise RuntimeError("Redis atomic scripting is unavailable")
        key = make_key(_manager_pin_rate_limit_key(device_name, manager_id))
        return eval_script(script, 1, key, *arguments)
    except Exception as error:
        log_sanitized_error("KoPOS manager PIN rate limiter unavailable", error)
        frappe.throw(
            _("Manager PIN verification is temporarily unavailable"),
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error


def _parse_rate_limit_result(result: Any, operation: str) -> tuple[int, int]:
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        frappe.throw(
            _("Manager PIN verification is temporarily unavailable"),
            frappe.ValidationError,
        )
    try:
        first = int(result[0])
        second = int(result[1])
    except (TypeError, ValueError) as error:
        log_sanitized_error(
            f"KoPOS manager PIN {operation} returned invalid Redis state",
            error,
        )
        frappe.throw(
            _("Manager PIN verification is temporarily unavailable"),
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error
    return first, second


def _assert_manager_pin_rate_limit(device_name: str, manager_id: str) -> None:
    now_epoch = int(time.time())
    locked, locked_until = _parse_rate_limit_result(
        _atomic_manager_pin_rate_limit_eval(
            PIN_RATE_LIMIT_CHECK_SCRIPT,
            device_name,
            manager_id,
            now_epoch,
        ),
        "check",
    )
    if locked or locked_until > now_epoch:
        frappe.throw(
            _("Manager PIN is temporarily locked; try again later"),
            frappe.ValidationError,
        )


def _record_manager_pin_rate_limit_failure(
    device_name: str,
    manager_id: str,
) -> None:
    _parse_rate_limit_result(
        _atomic_manager_pin_rate_limit_eval(
            PIN_RATE_LIMIT_FAILURE_SCRIPT,
            device_name,
            manager_id,
            int(time.time()),
            PIN_FAILURE_WINDOW_SECONDS,
            MAX_PIN_FAILURES,
            PIN_LOCKOUT_SECONDS,
        ),
        "failure update",
    )


def _clear_manager_pin_rate_limit(device_name: str, manager_id: str) -> None:
    result = _atomic_manager_pin_rate_limit_eval(
        PIN_RATE_LIMIT_CLEAR_SCRIPT,
        device_name,
        manager_id,
    )
    try:
        int(result)
    except (TypeError, ValueError) as error:
        log_sanitized_error(
            "KoPOS manager PIN limiter clear returned invalid Redis state",
            error,
        )
        frappe.throw(
            _("Manager PIN verification is temporarily unavailable"),
            frappe.ValidationError,
        )


def _optional_datetime(value: Any) -> Any | None:
    if not cstr(value).strip():
        return None
    return get_datetime(value)


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def build_sales_invoice_approval_scope(
    invoice: Any,
    *,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    invoice_doc = (
        invoice
        if getattr(invoice, "doctype", None) == "Sales Invoice"
        else frappe.get_doc("Sales Invoice", cstr(invoice).strip())
    )
    invoice_name = cstr(getattr(invoice_doc, "name", None)).strip()
    fb_order_name = cstr(getattr(invoice_doc, "custom_fb_order", None)).strip()
    if not invoice_name or not fb_order_name:
        frappe.throw(
            _("Sales Invoice is not bound to an FB Order"),
            frappe.ValidationError,
        )
    order_doc = frappe.get_doc("FB Order", fb_order_name)
    device_id = cstr(
        getattr(invoice_doc, "custom_fb_device_id", None)
        or getattr(order_doc, "device_id", None)
    ).strip()
    staff_id = cstr(getattr(order_doc, "staff_id", None)).strip()
    shift_id = cstr(
        getattr(order_doc, "shift", None)
        or getattr(invoice_doc, "custom_fb_shift", None)
    ).strip()
    if not device_id or not staff_id or not shift_id:
        frappe.throw(
            _("Sales Invoice approval scope is incomplete"),
            frappe.ValidationError,
        )
    amount_sen = abs(
        money_to_sen(
            getattr(invoice_doc, "grand_total", None),
            "Sales Invoice grand_total",
        )
    )
    return {
        "device_id": device_id,
        "staff_id": staff_id,
        "shift_id": shift_id,
        "resource_id": invoice_name,
        "amount_sen": amount_sen,
        "context_hash": canonical_context_hash(context),
        "fb_order": fb_order_name,
    }


def validate_requested_scope(
    payload: Mapping[str, Any], scope: Mapping[str, Any]
) -> None:
    for fieldname in ("device_id", "staff_id", "shift_id", "resource_id"):
        submitted = cstr(payload.get(fieldname)).strip()
        if submitted and submitted != cstr(scope.get(fieldname)).strip():
            frappe.throw(
                _("{0} does not match the ERP approval scope").format(fieldname),
                frappe.ValidationError,
            )
    submitted_amount = payload.get("amount_sen")
    if submitted_amount is not None and cstr(submitted_amount).strip():
        if parse_integer_sen(submitted_amount) != parse_integer_sen(
            scope.get("amount_sen")
        ):
            frappe.throw(
                _("amount_sen does not match the ERP approval scope"),
                frappe.ValidationError,
            )


def generate_manager_approval_token(
    *,
    device_id: str,
    staff_id: str,
    action: str,
    manager_id: str,
    shift_id: str | None = None,
    resource_id: str | None = None,
    amount_sen: Any = 0,
    context_hash: str | None = None,
    ttl_seconds: int | None = None,
    authorization_mode: str = "device_manager",
) -> dict[str, Any]:
    """
    Generate and persist a short-lived, exact-scope manager approval token.

    Args:
        device_id: The KoPOS device ID
        staff_id: The staff user ID performing the action
        action: The privileged action being authorized
        manager_id: The manager user ID approving the action
        shift_id: The exact FB Shift scope
        ttl_seconds: Token validity duration (default: 300 seconds / 5 minutes)

    Returns:
        Token metadata plus the exact server-derived scope bound into the token
    """
    valid_actions = (
        "open_shift",
        "close_shift",
        "reopen_shift",
        "void_order",
        "refund_order",
        "stock_refill",
        "stock_waste",
        "stock_remake",
        "manual_qr_override",
        "device_config_change",
        "inventory_count_reconciliation",
    )
    if action not in valid_actions:
        frappe.throw(
            _("Invalid action '{0}'. Must be one of: {1}").format(
                action, ", ".join(valid_actions)
            ),
            frappe.ValidationError,
        )

    if not device_id:
        frappe.throw(_("device_id is required"), frappe.ValidationError)
    if not staff_id:
        frappe.throw(_("staff_id is required"), frappe.ValidationError)
    if not manager_id:
        frappe.throw(_("manager_id is required"), frappe.ValidationError)
    if not cstr(shift_id).strip():
        frappe.throw(_("shift_id is required"), frappe.ValidationError)
    if not cstr(resource_id).strip():
        frappe.throw(_("resource_id is required"), frappe.ValidationError)
    resolved_amount_sen = parse_integer_sen(amount_sen)
    if resolved_amount_sen < 0:
        frappe.throw(_("amount_sen must be non-negative"), frappe.ValidationError)
    resolved_context_hash = cstr(context_hash).strip().lower()
    if len(resolved_context_hash) != 64 or any(
        character not in "0123456789abcdef" for character in resolved_context_hash
    ):
        frappe.throw(_("context_hash must be a SHA-256 hex digest"), frappe.ValidationError)
    resolved_authorization_mode = cstr(authorization_mode).strip()
    if resolved_authorization_mode not in AUTHORIZATION_MODES:
        frappe.throw(
            _("authorization_mode is invalid"),
            frappe.ValidationError,
        )

    ttl = cint(ttl_seconds or DEFAULT_TOKEN_TTL_SECONDS)
    if ttl < MIN_TOKEN_TTL_SECONDS or ttl > MAX_TOKEN_TTL_SECONDS:
        frappe.throw(
            _("ttl_seconds must be between {0} and {1}").format(
                MIN_TOKEN_TTL_SECONDS, MAX_TOKEN_TTL_SECONDS
            ),
            frappe.ValidationError,
        )
    now = now_datetime()
    issued_at = int(time.time())
    expires_at = issued_at + ttl

    # Generate a unique token ID for replay prevention
    token_id = frappe.generate_hash(length=16)

    payload = {
        "device_id": cstr(device_id).strip(),
        "staff_id": cstr(staff_id).strip(),
        "action": action,
        "manager_id": cstr(manager_id).strip(),
        "shift_id": cstr(shift_id).strip() if shift_id else None,
        "resource_id": cstr(resource_id).strip(),
        "amount_sen": resolved_amount_sen,
        "context_hash": resolved_context_hash,
        "authorization_mode": resolved_authorization_mode,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "token_id": token_id,
    }

    signature = _create_token_signature(payload)
    token = _encode_token(payload, signature)
    expires_datetime = add_to_date(now, seconds=ttl)
    approval_doc = frappe.get_doc(
        {
            "doctype": APPROVAL_DOCTYPE,
            "token_id": token_id,
            "status": "issued",
            "token_digest": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "device_id": payload["device_id"],
            "staff_id": payload["staff_id"],
            "manager_id": payload["manager_id"],
            "action": payload["action"],
            "shift_id": payload["shift_id"],
            "resource_id": payload["resource_id"],
            "amount_sen": payload["amount_sen"],
            "context_hash": payload["context_hash"],
            "authorization_mode": payload["authorization_mode"],
            "issued_at": now,
            "expires_at": expires_datetime,
            "issued_by_api_user": cstr(
                getattr(frappe.session, "user", None)
            ).strip(),
        }
    )
    approval_doc.insert(ignore_permissions=True)

    return {
        "token": token,
        "token_id": token_id,
        "action": payload["action"],
        "device_id": payload["device_id"],
        "staff_id": payload["staff_id"],
        "shift_id": payload["shift_id"],
        "resource_id": payload["resource_id"],
        "amount_sen": payload["amount_sen"],
        "context_hash": payload["context_hash"],
        "authorization_mode": payload["authorization_mode"],
        "issued_at": issued_at,
        "expires_at": expires_at,
        "issued_at_iso": now.isoformat(),
        "expires_at_iso": expires_datetime.isoformat(),
    }


class ManagerApprovalTokenVerificationError(frappe.ValidationError):
    """Raised when manager approval token verification fails."""

    error_code = "manager_approval_invalid"


class ManagerApprovalRequiredError(ManagerApprovalTokenVerificationError):
    """Raised only when an active privileged mutation has no approval token."""

    error_code = "manager_approval_required"


class ManagerApprovalExpiredError(ManagerApprovalTokenVerificationError):
    """Raised when a previously valid approval token has expired."""

    error_code = "manager_approval_expired"


def verify_manager_approval_token(
    token: str,
    *,
    device_id: str,
    staff_id: str,
    action: str,
    shift_id: str | None = None,
    resource_id: str | None = None,
    amount_sen: Any = 0,
    context_hash: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """
    Verify a manager approval token.

    Args:
        token: The approval token to verify
        device_id: Expected device ID
        staff_id: Expected staff ID
        action: Expected action
        shift_id: Expected shift ID

    Returns:
        Dict with verification result and manager_id if successful

    Raises:
        ManagerApprovalTokenVerificationError: If verification fails
    """
    if not token:
        raise ManagerApprovalRequiredError(
            _("Manager approval token is required")
        )

    # Decode the token
    decoded = _decode_token(token)
    if decoded is None:
        raise ManagerApprovalTokenVerificationError(
            _("Invalid manager approval token format")
        )

    payload, provided_signature = decoded

    # Verify the signature
    expected_signature = _create_token_signature(payload)
    if not hmac.compare_digest(provided_signature, expected_signature):
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token signature is invalid (tampered)")
        )

    # Check expiration
    now = int(time.time())
    try:
        expires_at = int(payload.get("expires_at", 0))
    except (TypeError, ValueError):
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token expiry is invalid")
        )
    if now > expires_at:
        raise ManagerApprovalExpiredError(
            _("Manager approval token has expired")
        )

    # Verify the token matches the expected parameters
    token_device_id = cstr(payload.get("device_id", "")).strip()
    token_staff_id = cstr(payload.get("staff_id", "")).strip()
    token_action = cstr(payload.get("action", "")).strip()
    token_shift_id = cstr(payload.get("shift_id", "")).strip() or None
    token_resource_id = cstr(payload.get("resource_id", "")).strip()
    token_amount_sen = parse_integer_sen(payload.get("amount_sen"), "token amount_sen")
    token_context_hash = cstr(payload.get("context_hash", "")).strip().lower()
    authorization_mode = cstr(payload.get("authorization_mode", "")).strip()
    manager_id = cstr(payload.get("manager_id", "")).strip()
    token_id = cstr(payload.get("token_id", "")).strip()

    if token_device_id != cstr(device_id).strip():
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token device_id mismatch")
        )

    if token_staff_id != cstr(staff_id).strip():
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token staff_id mismatch")
        )

    if token_action != cstr(action).strip():
        raise ManagerApprovalTokenVerificationError(
            _(
                "Manager approval token action mismatch (expected '{0}', got '{1}')"
            ).format(action, token_action)
        )

    expected_shift_id = cstr(shift_id).strip() or None
    if token_shift_id != expected_shift_id:
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token shift_id mismatch")
        )

    if token_resource_id != cstr(resource_id).strip():
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token resource_id mismatch")
        )

    expected_amount_sen = parse_integer_sen(amount_sen)
    if token_amount_sen != expected_amount_sen:
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token amount_sen mismatch")
        )

    expected_context_hash = cstr(context_hash).strip().lower()
    if token_context_hash != expected_context_hash:
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token context_hash mismatch")
        )

    if not token_id or not manager_id:
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token identity is invalid")
        )

    approval = _load_approval_for_update(token_id)
    if approval is None:
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token was not issued by this site")
        )
    _validate_persisted_approval(approval, payload, token)
    if cstr(_row_value(approval, "status")).strip() != "issued":
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token has already been used")
        )
    _validate_manager_action_authorization_at_use(
        device_id=token_device_id,
        manager_id=manager_id,
        action=token_action,
        authorization_mode=authorization_mode,
    )

    frappe.db.set_value(
        APPROVAL_DOCTYPE,
        _row_value(approval, "name"),
        {
            "status": "consumed",
            "consumed_at": now_datetime(),
            "consumed_idempotency_key": cstr(idempotency_key).strip() or None,
        },
        update_modified=False,
    )

    return {
        "valid": True,
        "manager_id": manager_id,
        "token_id": token_id,
        "issued_at": payload.get("issued_at"),
        "expires_at": expires_at,
    }


def _load_approval_for_update(token_id: str) -> Any | None:
    rows = frappe.db.sql(
        """
        SELECT
            name, token_id, status, token_digest, device_id, staff_id,
            manager_id, action, shift_id, resource_id, amount_sen,
            context_hash, authorization_mode, issued_at, expires_at,
            consumed_idempotency_key
        FROM `tabKoPOS Manager Approval`
        WHERE token_id = %s
        LIMIT 1
        FOR UPDATE
        """,
        (token_id,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _validate_persisted_approval(
    approval: Any, payload: dict[str, Any], token: str
) -> None:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(
        cstr(_row_value(approval, "token_digest")).strip(), digest
    ):
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token does not match its issuance record")
        )

    expected = {
        "token_id": cstr(payload.get("token_id")).strip(),
        "device_id": cstr(payload.get("device_id")).strip(),
        "staff_id": cstr(payload.get("staff_id")).strip(),
        "manager_id": cstr(payload.get("manager_id")).strip(),
        "action": cstr(payload.get("action")).strip(),
        "shift_id": cstr(payload.get("shift_id")).strip(),
        "resource_id": cstr(payload.get("resource_id")).strip(),
        "context_hash": cstr(payload.get("context_hash")).strip().lower(),
    }
    authorization_mode = cstr(payload.get("authorization_mode")).strip()
    if authorization_mode:
        expected["authorization_mode"] = authorization_mode
    for fieldname, expected_value in expected.items():
        actual = cstr(_row_value(approval, fieldname)).strip()
        if fieldname == "context_hash":
            actual = actual.lower()
        if actual != expected_value:
            raise ManagerApprovalTokenVerificationError(
                _("Manager approval token issuance record mismatch")
            )
    if parse_persisted_integer_sen(
        _row_value(approval, "amount_sen"), "approval amount_sen"
    ) != parse_integer_sen(payload.get("amount_sen"), "token amount_sen"):
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token issuance amount mismatch")
        )


def _validate_manager_action_authorization_at_use(
    *,
    device_id: str,
    manager_id: str,
    action: str,
    authorization_mode: str,
) -> None:
    required_field = ACTION_PERMISSION_FIELDS.get(action)
    if not required_field:
        return
    if authorization_mode not in AUTHORIZATION_MODES:
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval authorization mode is invalid")
        )

    from kopos_connector.api.devices import KOPOS_DEVICE_API_ROLE, get_session_roles

    if authorization_mode == "system_manager":
        roles = get_session_roles(manager_id)
        if "System Manager" not in roles or KOPOS_DEVICE_API_ROLE in roles:
            raise ManagerApprovalTokenVerificationError(
                _("Approving System Manager is no longer authorized")
            )
        try:
            _validate_enabled_erp_user(manager_id)
        except frappe.ValidationError as error:
            raise ManagerApprovalTokenVerificationError(
                _("Approving System Manager is no longer authorized")
            ) from error
        return

    device_rows = frappe.db.sql(
        """
        SELECT name, enabled
        FROM `tabKoPOS Device`
        WHERE device_id = %s
        LIMIT 2
        FOR UPDATE
        """,
        (device_id,),
        as_dict=True,
    )
    if len(device_rows or []) != 1 or not cint(
        _row_value(device_rows[0], "enabled")
    ):
        raise ManagerApprovalTokenVerificationError(
            _("Approving manager is no longer authorized for this device")
        )
    manager_row = _load_manager_row_for_update(
        cstr(_row_value(device_rows[0], "name")).strip(),
        manager_id,
    )
    if (
        not manager_row
        or not cint(_row_value(manager_row, "active"))
        or not cint(_row_value(manager_row, "can_manager_override"))
        or not cint(_row_value(manager_row, required_field))
    ):
        raise ManagerApprovalTokenVerificationError(
            _("Approving manager is no longer authorized for this action")
        )
    try:
        _validate_enabled_erp_user(manager_id)
    except frappe.ValidationError as error:
        raise ManagerApprovalTokenVerificationError(
            _("Approving manager is no longer authorized for this action")
        ) from error


def load_consumed_manager_approval_proof(
    *,
    approval_token_id: str,
    approval_manager_id: str,
    action: str,
    idempotency_key: str,
    resource_id: str,
) -> dict[str, str]:
    """Load immutable proof for an already-committed privileged mutation."""
    token_id = cstr(approval_token_id).strip()
    manager_id = cstr(approval_manager_id).strip()
    expected_action = cstr(action).strip()
    expected_idempotency_key = cstr(idempotency_key).strip()
    expected_resource_id = cstr(resource_id).strip()
    if (
        not token_id
        or not manager_id
        or not expected_action
        or not expected_idempotency_key
        or not expected_resource_id
    ):
        frappe.throw(
            _("Committed manager approval proof is incomplete"),
            frappe.ValidationError,
        )

    rows = frappe.db.sql(
        """
        SELECT
            token_id, status, manager_id, action, resource_id, context_hash,
            consumed_idempotency_key
        FROM `tabKoPOS Manager Approval`
        WHERE token_id = %s
        LIMIT 1
        """,
        (token_id,),
        as_dict=True,
    )
    if len(rows or []) != 1:
        frappe.throw(
            _("Committed manager approval record was not found"),
            frappe.ValidationError,
        )
    row = rows[0]
    persisted_token_id = cstr(_row_value(row, "token_id")).strip()
    persisted_manager_id = cstr(_row_value(row, "manager_id")).strip()
    persisted_action = cstr(_row_value(row, "action")).strip()
    persisted_status = cstr(_row_value(row, "status")).strip()
    persisted_resource_id = cstr(_row_value(row, "resource_id")).strip()
    persisted_idempotency_key = cstr(
        _row_value(row, "consumed_idempotency_key")
    ).strip()
    context_hash = cstr(_row_value(row, "context_hash")).strip().lower()
    if (
        persisted_token_id != token_id
        or persisted_manager_id != manager_id
        or persisted_action != expected_action
        or persisted_status != "consumed"
        or persisted_idempotency_key != expected_idempotency_key
        or persisted_resource_id != expected_resource_id
    ):
        frappe.throw(
            _("Committed manager approval record does not match the mutation"),
            frappe.ValidationError,
        )
    if len(context_hash) != 64 or any(
        character not in "0123456789abcdef" for character in context_hash
    ):
        frappe.throw(
            _("Committed manager approval context hash is invalid"),
            frappe.ValidationError,
        )
    return {
        "approval_manager_id": persisted_manager_id,
        "approval_token_id": persisted_token_id,
        "approval_context_hash": context_hash,
    }


def verify_manager_approval_token_optional(
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
    Optionally verify a manager approval token.

    This remains optional only for legacy shift operations. Active void and
    refund routes call the strict verifier directly.

    Returns:
        Dict with manager_id if token was provided and valid, None otherwise
    """
    if not token:
        # Log missing token for audit purposes
        frappe.logger("kopos").info(
            "Manager approval token not provided for %s action on device %s by staff %s",
            action,
            device_id,
            staff_id,
        )
        return None

    try:
        return verify_manager_approval_token(
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
    except frappe.ValidationError as e:
        # Re-raise validation errors (invalid/tampered/expired tokens should fail)
        raise
