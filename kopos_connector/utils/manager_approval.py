"""
Manager approval token utilities for shift operations.

This module provides short-lived, HMAC-signed tokens that authorize
privileged shift actions (open_shift, close_shift, reopen_shift).

Security properties:
- Tokens are signed with HMAC-SHA256 using a server-side secret
- Tokens expire after a configurable time (default: 5 minutes)
- Used tokens are tracked briefly to prevent replay attacks
- Tokens are tied to specific device_id, staff_id, and action
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

# Token validity duration in seconds (default: 5 minutes)
DEFAULT_TOKEN_TTL_SECONDS = 300

# How long to keep used tokens in the replay cache (should be >= TTL)
REPLAY_CACHE_TTL_SECONDS = 600


def _get_signing_secret() -> str:
    """
    Get or generate the server-side secret for signing approval tokens.

    The secret is stored in the site config and persists across restarts.
    """
    cache_key = "kopos_manager_approval_secret"

    # Try cache first
    cache = frappe.cache()
    cached_secret = cache.get_value(cache_key)
    if cached_secret:
        return cstr(cached_secret)

    # Try site config
    secret = cstr(frappe.conf.get("kopos_manager_approval_secret") or "")
    if not secret:
        # Generate a new secret if none exists
        secret = frappe.generate_hash(length=64)
        # Note: In production, this should be set in site_config.json
        # For now, we generate and cache it

    cache.set_value(cache_key, secret)
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


def _is_token_reused(token_id: str) -> bool:
    """Check if a token has already been used (replay attack prevention)."""
    cache_key = f"kopos_used_approval_token:{token_id}"
    cache = frappe.cache()
    return bool(cache.get_value(cache_key))


def _mark_token_used(token_id: str) -> None:
    """Mark a token as used to prevent replay attacks."""
    cache_key = f"kopos_used_approval_token:{token_id}"
    cache = frappe.cache()
    cache.set_value(cache_key, "1", expires_in_sec=REPLAY_CACHE_TTL_SECONDS)


def generate_manager_approval_token(
    *,
    device_id: str,
    staff_id: str,
    action: str,
    manager_id: str,
    shift_id: str | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """
    Generate a short-lived manager approval token for shift operations.

    Args:
        device_id: The KoPOS device ID
        staff_id: The staff user ID performing the action
        action: The action being authorized (open_shift, close_shift, reopen_shift)
        manager_id: The manager user ID approving the action
        shift_id: Optional shift ID for close/reopen operations
        ttl_seconds: Token validity duration (default: 300 seconds / 5 minutes)

    Returns:
        Dict with token, expires_at, and issued_at fields
    """
    valid_actions = ("open_shift", "close_shift", "reopen_shift")
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

    ttl = ttl_seconds or DEFAULT_TOKEN_TTL_SECONDS
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
        "issued_at": issued_at,
        "expires_at": expires_at,
        "token_id": token_id,
    }

    signature = _create_token_signature(payload)
    token = _encode_token(payload, signature)

    return {
        "token": token,
        "token_id": token_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "issued_at_iso": now.isoformat(),
        "expires_at_iso": frappe.utils.add_to_date(now, seconds=ttl).isoformat(),
    }


class ManagerApprovalTokenVerificationError(frappe.ValidationError):
    """Raised when manager approval token verification fails."""

    pass


def verify_manager_approval_token(
    token: str,
    *,
    device_id: str,
    staff_id: str,
    action: str,
    shift_id: str | None = None,
) -> dict[str, Any]:
    """
    Verify a manager approval token.

    Args:
        token: The approval token to verify
        device_id: Expected device ID
        staff_id: Expected staff ID
        action: Expected action
        shift_id: Expected shift ID (optional)

    Returns:
        Dict with verification result and manager_id if successful

    Raises:
        ManagerApprovalTokenVerificationError: If verification fails
    """
    if not token:
        raise ManagerApprovalTokenVerificationError(
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
    expires_at = payload.get("expires_at", 0)
    if now > expires_at:
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token has expired")
        )

    # Check for replay attacks
    token_id = payload.get("token_id", "")
    if _is_token_reused(token_id):
        raise ManagerApprovalTokenVerificationError(
            _("Manager approval token has already been used")
        )

    # Verify the token matches the expected parameters
    token_device_id = cstr(payload.get("device_id", "")).strip()
    token_staff_id = cstr(payload.get("staff_id", "")).strip()
    token_action = cstr(payload.get("action", "")).strip()
    token_shift_id = cstr(payload.get("shift_id", "")).strip() or None
    manager_id = cstr(payload.get("manager_id", "")).strip()

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
        # Only fail if the token has a shift_id that doesn't match
        # (tokens without shift_id can be used for any shift)
        if token_shift_id is not None:
            raise ManagerApprovalTokenVerificationError(
                _("Manager approval token shift_id mismatch")
            )

    # Mark the token as used
    _mark_token_used(token_id)

    return {
        "valid": True,
        "manager_id": manager_id,
        "token_id": token_id,
        "issued_at": payload.get("issued_at"),
        "expires_at": expires_at,
    }


def verify_manager_approval_token_optional(
    token: str | None,
    *,
    device_id: str,
    staff_id: str,
    action: str,
    shift_id: str | None = None,
) -> dict[str, Any] | None:
    """
    Optionally verify a manager approval token.

    This is a backward-compatible version that logs when token is missing
    but does not fail. Use this during the transition period.

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
        )
    except frappe.ValidationError as e:
        # Re-raise validation errors (invalid/tampered/expired tokens should fail)
        raise
