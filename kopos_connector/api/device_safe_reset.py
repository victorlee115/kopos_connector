# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from base64 import urlsafe_b64decode
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.twofactor import get_qr_svg_code
from frappe.utils import cint, cstr, get_datetime, now_datetime
from frappe.utils.password import get_decrypted_password, set_encrypted_password

from kopos_connector.api.devices import (
    ensure_unique_device_api_user,
    get_device_doc,
    privileged_device_api_operation,
    require_device_context,
    require_system_manager,
    serialize_device_config,
)
from kopos_connector.kopos.services.orders.sale_datetime import (
    normalize_site_datetime,
)
from kopos_connector.utils.diagnostics import (
    log_sanitized_error,
    make_savepoint,
    rollback_to_savepoint,
)


SAFE_RESET_DOCTYPE = "KoPOS Device Safe Reset"
PROVISIONING_MODE_SAFE_RESET = "safe_reset"
PROVISIONING_MODE_SAFE_RESET_APPROVAL = "safe_reset_approval"
SAFE_RESET_PROTOCOL_VERSION = 2
REQUEST_TTL_SECONDS = 24 * 60 * 60
DEFAULT_APPROVAL_TTL_SECONDS = 15 * 60
MIN_APPROVAL_TTL_SECONDS = 60
MAX_APPROVAL_TTL_SECONDS = 15 * 60
REDEEMED_RECOVERY_TTL_SECONDS = 24 * 60 * 60
CANCEL_CONFIRMATION_PREFIX = "CANCEL SAFE RESET"
EXPORT_EVIDENCE_MAX_AGE_SECONDS = 30 * 60
EXPORT_EVIDENCE_MAX_FUTURE_SKEW_SECONDS = 5 * 60
MAX_SUPPORT_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024 + 64 * 1024 * 1024
MAX_PROVIDER_REJECTED_DRAFTS_PER_SAFE_RESET = 64
MAX_REFUNDED_DUPLICATE_PAYMENTS_PER_SAFE_RESET = 64
MAX_TERMINAL_SECONDARY_STATIC_CLAIMS_PER_SAFE_RESET = 64
MAX_REFUND_EVIDENCE_WORKLOAD_BYTES = 64 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
# The reset proof is the existing Android 32-byte value encoded as lowercase
# hex. Keep this distinct from the independent 43-character base64url
# idempotency and approval secrets so pending tablet journals remain redeemable.
RESET_PROOF_NONCE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CHALLENGE_ID_PATTERN = re.compile(r"^KSAC-[0-9a-f]{64}$")
SECRET_256_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
SAFE_RESET_RATE_LIMIT_WINDOW_SECONDS = 60
SAFE_RESET_RATE_LIMIT_MAX_ATTEMPTS = 12
SAFE_RESET_RATE_LIMIT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""
QUEUE_EVIDENCE_KEYS = (
    "pending_count",
    "failed_count",
    "syncing_count",
    "dead_letter_count",
)
MIGRATION_RECOVERY_EVIDENCE_FIELDS = (
    "migration_recovery_point_count",
    "migration_recovery_valid_point_count",
    "migration_recovery_invalid_point_count",
    "migration_recovery_captured_pending_total",
    "migration_recovery_review_required",
)
MAX_MIGRATION_RECOVERY_COUNT = 2_147_483_647
REQUEST_ORIGIN_DEVICE = "device_authenticated"
REQUEST_ORIGIN_CREDENTIAL_RECOVERY = "credential_recovery"
CANCELLATION_ORIGIN_DEVICE = "device_authenticated"
CANCELLATION_ORIGIN_SYSTEM_MANAGER = "system_manager"
ABANDONMENT_CANCELLATION_REASON = (
    "Device abandoned an unacknowledged safe reset request"
)


def request_device_safe_reset(
    *,
    safe_reset_protocol_version: Any,
    device_id: str | None,
    reason: str | None,
    export_sha256: str | None,
    export_content_sha256: str | None,
    export_byte_length: Any,
    exported_at: Any,
    drained_row_count: Any,
    queue_evidence: Mapping[str, Any] | str | None,
    migration_recovery_point_count: Any,
    migration_recovery_valid_point_count: Any,
    migration_recovery_invalid_point_count: Any,
    migration_recovery_captured_pending_total: Any,
    migration_recovery_review_required: Any,
    previous_config_version: Any,
    reset_proof_sha256: str | None,
    erp_base_url: str | None,
    company: str | None,
    currency: str | None,
    pos_profile: str | None,
    warehouse: str | None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Persist immutable drain/export evidence from the currently authenticated device."""
    device_id_value = cstr(device_id).strip()
    device_doc = require_device_context(device_id=device_id_value)
    if device_doc is None:
        frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)
        raise ValueError("KoPOS Device is required")

    api_user = cstr(getattr(device_doc, "api_user", None)).strip()
    session_user = cstr(getattr(frappe.session, "user", None)).strip()
    if not api_user or not hmac.compare_digest(api_user, session_user):
        frappe.throw(
            _("Safe reset must be requested by the device's dedicated API user"),
            frappe.ValidationError,
        )
    ensure_unique_device_api_user(
        api_user,
        current_device_name=cstr(getattr(device_doc, "name", None)).strip() or None,
    )
    return _register_safe_reset_request(
        device_doc=device_doc,
        api_user_override=None,
        request_origin=REQUEST_ORIGIN_DEVICE,
        registered_by_system_manager=None,
        credential_recovery_confirmed_at=None,
        safe_reset_protocol_version=safe_reset_protocol_version,
        request_id=request_id,
        device_id=device_id_value,
        reason=reason,
        export_sha256=export_sha256,
        export_content_sha256=export_content_sha256,
        export_byte_length=export_byte_length,
        exported_at=exported_at,
        drained_row_count=drained_row_count,
        queue_evidence=queue_evidence,
        migration_recovery_point_count=migration_recovery_point_count,
        migration_recovery_valid_point_count=migration_recovery_valid_point_count,
        migration_recovery_invalid_point_count=migration_recovery_invalid_point_count,
        migration_recovery_captured_pending_total=(
            migration_recovery_captured_pending_total
        ),
        migration_recovery_review_required=migration_recovery_review_required,
        previous_config_version=previous_config_version,
        reset_proof_sha256=reset_proof_sha256,
        erp_base_url=erp_base_url,
        company=company,
        currency=currency,
        pos_profile=pos_profile,
        warehouse=warehouse,
        allow_stale_export=False,
        stale_export_override_reason=None,
        validate_business_state=True,
    )


def abandon_unregistered_device_safe_reset_request(
    *,
    safe_reset_protocol_version: Any,
    device_id: str | None,
    request_id: str | None,
    reason: str | None,
    export_sha256: str | None,
    export_content_sha256: str | None,
    export_byte_length: Any,
    exported_at: Any,
    drained_row_count: Any,
    queue_evidence: Mapping[str, Any] | str | None,
    migration_recovery_point_count: Any,
    migration_recovery_valid_point_count: Any,
    migration_recovery_invalid_point_count: Any,
    migration_recovery_captured_pending_total: Any,
    migration_recovery_review_required: Any,
    previous_config_version: Any,
    reset_proof_sha256: str | None,
    erp_base_url: str | None,
    company: str | None,
    currency: str | None,
    pos_profile: str | None,
    warehouse: str | None,
    cancellation_idempotency_key: str | None,
) -> dict[str, Any]:
    """Fence an unacknowledged request against any delayed registration."""
    device_id_value = cstr(device_id).strip()
    request_id_value = _require_request_id(request_id)
    cancellation_key = _require_256_bit_secret(
        cancellation_idempotency_key,
        "cancellation_idempotency_key",
    )
    cancellation_digest = _sha256_text(cancellation_key)

    device_doc = require_device_context(device_id=device_id_value)
    if device_doc is None:
        frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)
        raise ValueError("KoPOS Device is required")
    api_user = cstr(getattr(device_doc, "api_user", None)).strip()
    session_user = cstr(getattr(frappe.session, "user", None)).strip()
    if not api_user or not hmac.compare_digest(api_user, session_user):
        frappe.throw(
            _(
                "Safe reset abandonment requires the device's dedicated API "
                "user"
            ),
            frappe.ValidationError,
        )
    device_name = cstr(getattr(device_doc, "name", None)).strip()
    ensure_unique_device_api_user(
        api_user,
        current_device_name=device_name or None,
    )

    prepared = _prepare_safe_reset_request_evidence(
        device_doc=device_doc,
        api_user_override=None,
        request_origin=REQUEST_ORIGIN_DEVICE,
        safe_reset_protocol_version=safe_reset_protocol_version,
        request_id=request_id_value,
        device_id=device_id_value,
        reason=reason,
        export_sha256=export_sha256,
        export_content_sha256=export_content_sha256,
        export_byte_length=export_byte_length,
        exported_at=exported_at,
        drained_row_count=drained_row_count,
        queue_evidence=queue_evidence,
        migration_recovery_point_count=migration_recovery_point_count,
        migration_recovery_valid_point_count=(
            migration_recovery_valid_point_count
        ),
        migration_recovery_invalid_point_count=(
            migration_recovery_invalid_point_count
        ),
        migration_recovery_captured_pending_total=(
            migration_recovery_captured_pending_total
        ),
        migration_recovery_review_required=(
            migration_recovery_review_required
        ),
        previous_config_version=previous_config_version,
        reset_proof_sha256=reset_proof_sha256,
        erp_base_url=erp_base_url,
        company=company,
        currency=currency,
        pos_profile=pos_profile,
        warehouse=warehouse,
        allow_stale_export=False,
        stale_export_override_reason=None,
    )

    _lock_device_for_update(device_name)
    locked_device = get_device_doc(name=device_name)
    locked_api_user = cstr(getattr(locked_device, "api_user", None)).strip()
    if not hmac.compare_digest(
        device_id_value,
        cstr(getattr(locked_device, "device_id", None)).strip(),
    ) or not hmac.compare_digest(api_user, locked_api_user):
        frappe.throw(
            _("Safe reset abandonment device binding changed"),
            frappe.ValidationError,
        )
    ensure_unique_device_api_user(
        locked_api_user,
        current_device_name=device_name or None,
    )
    scope = prepared["scope"]
    _validate_reset_scope(
        locked_device,
        erp_base_url=scope["erp_base_url"],
        company=scope["company"],
        currency=scope["currency"],
        pos_profile=scope["pos_profile"],
        warehouse=scope["warehouse"],
    )

    existing = _find_matching_reset_for_update(
        device_id=device_id_value,
        request_id=request_id_value,
        reset_proof_sha256=prepared["reset_proof_sha256"],
    )
    if existing is not None:
        _validate_reset_device_binding(existing, locked_device)
        _validate_existing_prepared_request_evidence(
            existing,
            prepared,
            device_id=device_id_value,
            api_user=locked_api_user,
        )
        if cstr(getattr(existing, "status", None)).strip().lower() == "cancelled":
            if not hmac.compare_digest(
                cancellation_digest,
                cstr(
                    getattr(
                        existing,
                        "cancellation_idempotency_sha256",
                        None,
                    )
                ).strip(),
            ):
                frappe.throw(
                    _(
                        "Safe reset abandonment idempotency key was already used "
                        "differently"
                    ),
                    frappe.ValidationError,
                )
            _validate_stored_cancellation_result(existing)
            return _abandonment_fence_ack(existing)
        return _request_ack(existing)

    reused_cancellation = _find_cancellation_idempotency_for_update(
        device_id=device_id_value,
        cancellation_idempotency_sha256=cancellation_digest,
    )
    if reused_cancellation is not None:
        frappe.throw(
            _(
                "Safe reset abandonment idempotency key was already used for a "
                "different request"
            ),
            frappe.ValidationError,
        )

    requested_at = now_datetime()
    cancelled_at = requested_at
    reset_doc = frappe.get_doc(
        {
            "doctype": SAFE_RESET_DOCTYPE,
            "reset_id": f"KSR-{cstr(frappe.generate_hash(length=24)).strip()}",
            "safe_reset_protocol_version": prepared["protocol_version"],
            "request_id": request_id_value,
            "status": "cancelled",
            "device": device_name,
            "device_id": device_id_value,
            "api_user": locked_api_user,
            "reason": prepared["reason"],
            "request_origin": REQUEST_ORIGIN_DEVICE,
            "registered_by_system_manager": None,
            "credential_recovery_confirmed_at": None,
            "stale_export_override": 0,
            "stale_export_override_reason": "",
            "export_sha256": prepared["export_sha256"],
            "export_content_sha256": prepared["export_content_sha256"],
            "export_byte_length": prepared["export_byte_length"],
            "exported_at": prepared["exported_at"],
            "drained_row_count": prepared["drained_row_count"],
            "queue_pending_count": prepared["queue_evidence"]["pending_count"],
            "queue_failed_count": prepared["queue_evidence"]["failed_count"],
            "queue_syncing_count": prepared["queue_evidence"]["syncing_count"],
            "queue_dead_letter_count": prepared["queue_evidence"][
                "dead_letter_count"
            ],
            **prepared["migration_recovery"],
            "previous_config_version": prepared["previous_config_version"],
            "reset_proof_sha256": prepared["reset_proof_sha256"],
            "evidence_fingerprint": prepared["evidence_fingerprint"],
            "request_fingerprint": prepared["request_fingerprint"],
            "requested_by_api_user": locked_api_user,
            "requested_at": requested_at,
            "request_expires_at": requested_at
            + timedelta(seconds=REQUEST_TTL_SECONDS),
            "erp_base_url": scope["erp_base_url"],
            "company": scope["company"],
            "currency": scope["currency"],
            "pos_profile": scope["pos_profile"],
            "warehouse": scope["warehouse"],
            "cancellation_idempotency_sha256": cancellation_digest,
            "cancellation_reason": ABANDONMENT_CANCELLATION_REASON,
            "cancellation_origin": CANCELLATION_ORIGIN_DEVICE,
            "cancelled_by_user": locked_api_user,
            "cancelled_by_api_user": locked_api_user,
            "cancelled_at": cancelled_at,
        }
    )
    reset_doc.cancellation_result_fingerprint = _cancellation_result_fingerprint(
        reset_doc,
        cancellation_idempotency_sha256=cancellation_digest,
        cancellation_reason=ABANDONMENT_CANCELLATION_REASON,
        cancellation_origin=CANCELLATION_ORIGIN_DEVICE,
        cancelled_by_user=locked_api_user,
        cancelled_by_api_user=locked_api_user,
        cancelled_at=cancelled_at,
    )
    with privileged_device_api_operation("device_safe_reset"):
        reset_doc.insert(ignore_permissions=True)
    response = _abandonment_fence_ack(reset_doc)
    frappe.db.commit()
    return response


def _abandonment_fence_ack(reset_doc: Any) -> dict[str, Any]:
    response = _request_ack(reset_doc)
    response.update(
        {
            "status": "cancelled",
            "lifecycle_status": "cancelled",
            "abandonment_status": "fenced",
            "local_release_authorized": True,
            "cancellation_idempotency_sha256": cstr(
                getattr(reset_doc, "cancellation_idempotency_sha256", None)
            ).strip(),
            "cancelled_at": _iso_utc(
                getattr(reset_doc, "cancelled_at", None)
            ),
        }
    )
    return response


def resolve_device_safe_reset_request(
    *,
    safe_reset_protocol_version: Any,
    device_id: str | None,
    request_id: str | None,
    reset_proof_sha256: str | None,
    export_sha256: str | None,
    export_content_sha256: str | None,
    export_byte_length: Any,
    previous_config_version: Any,
) -> dict[str, Any]:
    """Resolve an unacknowledged request without registering or changing it."""
    protocol_version = _require_safe_reset_protocol_version(
        safe_reset_protocol_version
    )
    request_id_value = _require_request_id(request_id)
    reset_proof_digest = _require_sha256(
        reset_proof_sha256,
        "reset_proof_sha256",
    )
    export_digest = _require_sha256(export_sha256, "export_sha256")
    export_content_digest = _require_sha256(
        export_content_sha256,
        "export_content_sha256",
    )
    export_length = _require_archive_byte_length(export_byte_length)
    previous_version = _require_positive_int(
        previous_config_version,
        "previous_config_version",
    )

    device_id_value = cstr(device_id).strip()
    device_doc = require_device_context(device_id=device_id_value)
    if device_doc is None:
        frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)
        raise ValueError("KoPOS Device is required")
    api_user = cstr(getattr(device_doc, "api_user", None)).strip()
    session_user = cstr(getattr(frappe.session, "user", None)).strip()
    if not api_user or not hmac.compare_digest(api_user, session_user):
        frappe.throw(
            _(
                "Safe reset request resolution requires the device's dedicated "
                "API user"
            ),
            frappe.ValidationError,
        )
    ensure_unique_device_api_user(
        api_user,
        current_device_name=cstr(getattr(device_doc, "name", None)).strip()
        or None,
    )

    device_name = cstr(getattr(device_doc, "name", None)).strip()
    _lock_device_for_update(device_name)
    locked_device = get_device_doc(name=device_name)
    if not hmac.compare_digest(
        device_id_value,
        cstr(getattr(locked_device, "device_id", None)).strip(),
    ):
        frappe.throw(
            _("Safe reset resolution device binding changed"),
            frappe.ValidationError,
        )
    locked_api_user = cstr(getattr(locked_device, "api_user", None)).strip()
    if not hmac.compare_digest(api_user, locked_api_user):
        frappe.throw(
            _("Safe reset resolution API user binding changed"),
            frappe.ValidationError,
        )
    ensure_unique_device_api_user(
        locked_api_user,
        current_device_name=device_name or None,
    )

    reset_doc = _find_matching_reset_for_update(
        device_id=device_id_value,
        request_id=request_id_value,
        reset_proof_sha256=reset_proof_digest,
    )
    if reset_doc is None:
        checked_at = _iso_utc(now_datetime())
        if checked_at is None:
            raise RuntimeError("Safe reset resolution timestamp is unavailable")
        return {
            "status": "not_registered",
            "request_registration_status": "not_found",
            "request_committed": False,
            "local_release_authorized": False,
            "recovery_action": (
                "abandon_unregistered_device_safe_reset_request"
            ),
            "safe_reset_protocol_version": protocol_version,
            "request_id": request_id_value,
            "device_id": device_id_value,
            "previous_config_version": previous_version,
            "reset_proof_sha256": reset_proof_digest,
            "export_sha256": export_digest,
            "export_content_sha256": export_content_digest,
            "export_byte_length": export_length,
            "checked_at": checked_at,
        }

    _validate_reset_device_binding(reset_doc, locked_device)
    _validate_resolved_request_identity(
        reset_doc,
        protocol_version=protocol_version,
        request_id=request_id_value,
        device_id=device_id_value,
        reset_proof_sha256=reset_proof_digest,
        export_sha256=export_digest,
        export_content_sha256=export_content_digest,
        export_byte_length=export_length,
        previous_config_version=previous_version,
    )
    return _request_ack(reset_doc)


def classify_device_safe_reset_request_registration(
    *,
    safe_reset_protocol_version: Any,
    device_id: str | None,
    request_id: str | None,
    reset_proof_sha256: str | None,
    export_sha256: str | None,
    export_content_sha256: str | None,
    export_byte_length: Any,
    previous_config_version: Any,
) -> dict[str, str]:
    """Classify a rolled-back registration without mutating ERP state.

    A caller may only treat ``not_found`` as authoritative. Existing matching
    evidence and an unrelated active reset deliberately require the dedicated
    resolution endpoint so a rejected request can never release local evidence
    based on an ambiguous conflict.
    """
    resolution = resolve_device_safe_reset_request(
        safe_reset_protocol_version=safe_reset_protocol_version,
        device_id=device_id,
        request_id=request_id,
        reset_proof_sha256=reset_proof_sha256,
        export_sha256=export_sha256,
        export_content_sha256=export_content_sha256,
        export_byte_length=export_byte_length,
        previous_config_version=previous_config_version,
    )
    if resolution.get("status") != "not_registered":
        return {"request_registration_status": "matching_request_exists"}

    device_id_value = cstr(device_id).strip()
    if _find_active_reset_for_update(device_id_value) is not None:
        return {"request_registration_status": "active_reset_conflict"}

    checked_at = cstr(resolution.get("checked_at")).strip()
    if not checked_at:
        raise RuntimeError("Safe reset registration check timestamp is unavailable")
    return {
        "request_registration_status": "not_found",
        "checked_at": checked_at,
    }


def register_device_credential_recovery(
    *,
    safe_reset_protocol_version: Any,
    confirmation: str | None,
    request_id: str | None,
    device_id: str | None,
    reason: str | None,
    export_sha256: str | None,
    export_content_sha256: str | None,
    export_byte_length: Any,
    exported_at: Any,
    drained_row_count: Any,
    queue_evidence: Mapping[str, Any] | str | None,
    migration_recovery_point_count: Any,
    migration_recovery_valid_point_count: Any,
    migration_recovery_invalid_point_count: Any,
    migration_recovery_captured_pending_total: Any,
    migration_recovery_review_required: Any,
    previous_config_version: Any,
    reset_proof_sha256: str | None,
    erp_base_url: str | None,
    company: str | None,
    currency: str | None,
    pos_profile: str | None,
    warehouse: str | None,
    allow_stale_export: Any = False,
    stale_export_override_reason: str | None = None,
) -> dict[str, Any]:
    """Register tablet-held evidence when the old device credential is unavailable."""
    require_system_manager()
    device_id_value = cstr(device_id).strip()
    expected_confirmation = f"RECOVER {device_id_value}"
    if not device_id_value or not hmac.compare_digest(
        cstr(confirmation), expected_confirmation
    ):
        frappe.throw(
            _("Type RECOVER followed by the exact device ID to confirm credential recovery"),
            frappe.ValidationError,
        )
    device_doc = get_device_doc(device_id=device_id_value)
    if device_doc is None or not cint(getattr(device_doc, "enabled", 0)):
        frappe.throw(
            _("Credential recovery requires an enabled registered KoPOS Device"),
            frappe.ValidationError,
        )
    api_user = _credential_recovery_target_api_user(device_doc)
    ensure_unique_device_api_user(
        api_user,
        current_device_name=cstr(getattr(device_doc, "name", None)).strip() or None,
    )
    registered_by = cstr(getattr(frappe.session, "user", None)).strip()
    return _register_safe_reset_request(
        device_doc=device_doc,
        api_user_override=api_user,
        request_origin=REQUEST_ORIGIN_CREDENTIAL_RECOVERY,
        registered_by_system_manager=registered_by,
        credential_recovery_confirmed_at=now_datetime(),
        safe_reset_protocol_version=safe_reset_protocol_version,
        request_id=request_id,
        device_id=device_id_value,
        reason=reason,
        export_sha256=export_sha256,
        export_content_sha256=export_content_sha256,
        export_byte_length=export_byte_length,
        exported_at=exported_at,
        drained_row_count=drained_row_count,
        queue_evidence=queue_evidence,
        migration_recovery_point_count=migration_recovery_point_count,
        migration_recovery_valid_point_count=migration_recovery_valid_point_count,
        migration_recovery_invalid_point_count=migration_recovery_invalid_point_count,
        migration_recovery_captured_pending_total=(
            migration_recovery_captured_pending_total
        ),
        migration_recovery_review_required=migration_recovery_review_required,
        previous_config_version=previous_config_version,
        reset_proof_sha256=reset_proof_sha256,
        erp_base_url=erp_base_url,
        company=company,
        currency=currency,
        pos_profile=pos_profile,
        warehouse=warehouse,
        allow_stale_export=allow_stale_export,
        stale_export_override_reason=stale_export_override_reason,
        validate_business_state=True,
    )


def _prepare_safe_reset_request_evidence(
    *,
    device_doc: Any,
    api_user_override: str | None,
    request_origin: str,
    safe_reset_protocol_version: Any,
    request_id: str | None,
    device_id: str,
    reason: str | None,
    export_sha256: str | None,
    export_content_sha256: str | None,
    export_byte_length: Any,
    exported_at: Any,
    drained_row_count: Any,
    queue_evidence: Mapping[str, Any] | str | None,
    migration_recovery_point_count: Any,
    migration_recovery_valid_point_count: Any,
    migration_recovery_invalid_point_count: Any,
    migration_recovery_captured_pending_total: Any,
    migration_recovery_review_required: Any,
    previous_config_version: Any,
    reset_proof_sha256: str | None,
    erp_base_url: str | None,
    company: str | None,
    currency: str | None,
    pos_profile: str | None,
    warehouse: str | None,
    allow_stale_export: Any,
    stale_export_override_reason: str | None,
) -> dict[str, Any]:
    """Normalize immutable evidence once for registration and abandonment."""
    protocol_version = _require_safe_reset_protocol_version(
        safe_reset_protocol_version
    )
    reason_value = _require_reason(reason)
    export_digest = _require_sha256(export_sha256, "export_sha256")
    export_content_digest = _require_sha256(
        export_content_sha256,
        "export_content_sha256",
    )
    export_length = _require_archive_byte_length(export_byte_length)
    export_timestamp, stale_override, stale_override_reason = (
        _validate_export_timestamp(
            exported_at,
            allow_stale_export=allow_stale_export,
            stale_export_override_reason=stale_export_override_reason,
            credential_recovery=(
                request_origin == REQUEST_ORIGIN_CREDENTIAL_RECOVERY
            ),
        )
    )
    reset_proof_digest = _require_sha256(
        reset_proof_sha256,
        "reset_proof_sha256",
    )
    drained_count = _require_nonnegative_int(
        drained_row_count,
        "drained_row_count",
    )
    queue_counts = _normalize_queue_evidence(queue_evidence)
    if any(queue_counts.values()):
        frappe.throw(
            _(
                "Safe reset requires a fully drained queue with no pending, failed, "
                "syncing, or dead-letter rows"
            ),
            frappe.ValidationError,
        )
    migration_recovery = _normalize_migration_recovery_evidence(
        migration_recovery_point_count=migration_recovery_point_count,
        migration_recovery_valid_point_count=migration_recovery_valid_point_count,
        migration_recovery_invalid_point_count=migration_recovery_invalid_point_count,
        migration_recovery_captured_pending_total=(
            migration_recovery_captured_pending_total
        ),
        migration_recovery_review_required=migration_recovery_review_required,
    )
    previous_version = _require_positive_int(
        previous_config_version,
        "previous_config_version",
    )
    scope = _validate_reset_scope(
        device_doc,
        erp_base_url=erp_base_url,
        company=company,
        currency=currency,
        pos_profile=pos_profile,
        warehouse=warehouse,
    )
    api_user = cstr(api_user_override).strip() or cstr(
        getattr(device_doc, "api_user", None)
    ).strip()
    if not api_user:
        frappe.throw(
            _("KoPOS Device has no dedicated API user"),
            frappe.ValidationError,
        )

    stored_request_id, fingerprint_request_id = _normalize_request_id(request_id)
    evidence_fingerprint = _request_fingerprint(
        {
            "request_id": fingerprint_request_id,
            "safe_reset_protocol_version": protocol_version,
            "device_id": device_id,
            "api_user": api_user,
            "reason": reason_value,
            "export_sha256": export_digest,
            "export_content_sha256": export_content_digest,
            "export_byte_length": export_length,
            "exported_at": export_timestamp.isoformat(),
            "drained_row_count": drained_count,
            "queue_evidence": queue_counts,
            **migration_recovery,
            "previous_config_version": previous_version,
            "reset_proof_sha256": reset_proof_digest,
            "scope": scope,
        }
    )
    request_fingerprint = _request_fingerprint(
        {
            "request_origin": request_origin,
            "evidence_fingerprint": evidence_fingerprint,
            "stale_export_override": stale_override,
            "stale_export_override_reason": stale_override_reason,
        }
    )
    return {
        "protocol_version": protocol_version,
        "reason": reason_value,
        "export_sha256": export_digest,
        "export_content_sha256": export_content_digest,
        "export_byte_length": export_length,
        "exported_at": export_timestamp,
        "stale_export_override": stale_override,
        "stale_export_override_reason": stale_override_reason,
        "reset_proof_sha256": reset_proof_digest,
        "drained_row_count": drained_count,
        "queue_evidence": queue_counts,
        "migration_recovery": migration_recovery,
        "previous_config_version": previous_version,
        "scope": scope,
        "api_user": api_user,
        "request_id": stored_request_id,
        "fingerprint_request_id": fingerprint_request_id,
        "evidence_fingerprint": evidence_fingerprint,
        "request_fingerprint": request_fingerprint,
    }


def _register_safe_reset_request(
    *,
    device_doc: Any,
    api_user_override: str | None,
    request_origin: str,
    registered_by_system_manager: str | None,
    credential_recovery_confirmed_at: datetime | None,
    safe_reset_protocol_version: Any,
    request_id: str | None,
    device_id: str,
    reason: str | None,
    export_sha256: str | None,
    export_content_sha256: str | None,
    export_byte_length: Any,
    exported_at: Any,
    drained_row_count: Any,
    queue_evidence: Mapping[str, Any] | str | None,
    migration_recovery_point_count: Any,
    migration_recovery_valid_point_count: Any,
    migration_recovery_invalid_point_count: Any,
    migration_recovery_captured_pending_total: Any,
    migration_recovery_review_required: Any,
    previous_config_version: Any,
    reset_proof_sha256: str | None,
    erp_base_url: str | None,
    company: str | None,
    currency: str | None,
    pos_profile: str | None,
    warehouse: str | None,
    allow_stale_export: Any,
    stale_export_override_reason: str | None,
    validate_business_state: bool,
) -> dict[str, Any]:
    prepared = _prepare_safe_reset_request_evidence(
        device_doc=device_doc,
        api_user_override=api_user_override,
        request_origin=request_origin,
        safe_reset_protocol_version=safe_reset_protocol_version,
        request_id=request_id,
        device_id=device_id,
        reason=reason,
        export_sha256=export_sha256,
        export_content_sha256=export_content_sha256,
        export_byte_length=export_byte_length,
        exported_at=exported_at,
        drained_row_count=drained_row_count,
        queue_evidence=queue_evidence,
        migration_recovery_point_count=migration_recovery_point_count,
        migration_recovery_valid_point_count=(
            migration_recovery_valid_point_count
        ),
        migration_recovery_invalid_point_count=(
            migration_recovery_invalid_point_count
        ),
        migration_recovery_captured_pending_total=(
            migration_recovery_captured_pending_total
        ),
        migration_recovery_review_required=(
            migration_recovery_review_required
        ),
        previous_config_version=previous_config_version,
        reset_proof_sha256=reset_proof_sha256,
        erp_base_url=erp_base_url,
        company=company,
        currency=currency,
        pos_profile=pos_profile,
        warehouse=warehouse,
        allow_stale_export=allow_stale_export,
        stale_export_override_reason=stale_export_override_reason,
    )
    protocol_version = prepared["protocol_version"]
    reason_value = prepared["reason"]
    export_digest = prepared["export_sha256"]
    export_content_digest = prepared["export_content_sha256"]
    export_length = prepared["export_byte_length"]
    export_timestamp = prepared["exported_at"]
    stale_override = prepared["stale_export_override"]
    stale_override_reason = prepared["stale_export_override_reason"]
    reset_proof_digest = prepared["reset_proof_sha256"]
    drained_count = prepared["drained_row_count"]
    queue_counts = prepared["queue_evidence"]
    migration_recovery = prepared["migration_recovery"]
    previous_version = prepared["previous_config_version"]
    scope = prepared["scope"]
    api_user = prepared["api_user"]
    stored_request_id = prepared["request_id"]
    fingerprint_request_id = prepared["fingerprint_request_id"]
    evidence_fingerprint = prepared["evidence_fingerprint"]
    fingerprint = prepared["request_fingerprint"]

    _lock_device_for_update(cstr(getattr(device_doc, "name", None)).strip())
    locked_device = get_device_doc(name=cstr(getattr(device_doc, "name", None)).strip())
    _validate_reset_scope(
        locked_device,
        erp_base_url=scope["erp_base_url"],
        company=scope["company"],
        currency=scope["currency"],
        pos_profile=scope["pos_profile"],
        warehouse=scope["warehouse"],
    )

    existing = _find_matching_reset_for_update(
        device_id=device_id,
        request_id=stored_request_id if fingerprint_request_id else None,
        reset_proof_sha256=reset_proof_digest,
    )
    if existing is not None:
        if hmac.compare_digest(
            cstr(getattr(existing, "request_fingerprint", None)).strip(),
            fingerprint,
        ):
            return _request_ack(existing)
        if (
            request_origin == REQUEST_ORIGIN_CREDENTIAL_RECOVERY
            and cstr(getattr(existing, "request_origin", None)).strip()
            == REQUEST_ORIGIN_DEVICE
            and hmac.compare_digest(
                cstr(getattr(existing, "evidence_fingerprint", None)).strip(),
                evidence_fingerprint,
            )
        ):
            response = _request_ack(existing)
            response["registration_resolution"] = "existing_device_authenticated"
            return response
        else:
            frappe.throw(
                _("Safe reset request_id or reset proof was reused with different evidence"),
                frappe.ValidationError,
            )

    _enforce_export_timestamp_freshness(
        export_timestamp,
        stale_override=stale_override,
        stale_override_reason=stale_override_reason,
        credential_recovery=(request_origin == REQUEST_ORIGIN_CREDENTIAL_RECOVERY),
    )
    if cint(getattr(locked_device, "config_version", 0)) != previous_version:
        frappe.throw(
            _("Device configuration changed before safe reset evidence was accepted"),
            frappe.ValidationError,
        )
    if validate_business_state:
        _assert_no_open_shift_or_unresolved_projection(device_id)

    active = _find_active_reset_for_update(device_id)
    if active is not None and _expire_active_reset_if_needed(active):
        active = None
    if active is not None:
        frappe.throw(
            _("KoPOS Device already has an active safe reset request"),
            frappe.ValidationError,
        )

    requested_at = now_datetime()
    reset_id_value = f"KSR-{cstr(frappe.generate_hash(length=24)).strip()}"
    request_expires_at = requested_at + timedelta(seconds=REQUEST_TTL_SECONDS)
    reset_doc = frappe.get_doc(
        {
            "doctype": SAFE_RESET_DOCTYPE,
            "reset_id": reset_id_value,
            "safe_reset_protocol_version": protocol_version,
            "request_id": stored_request_id,
            "status": "requested",
            "device": cstr(getattr(locked_device, "name", None)).strip(),
            "device_id": device_id,
            "api_user": api_user,
            "reason": reason_value,
            "request_origin": request_origin,
            "registered_by_system_manager": registered_by_system_manager,
            "credential_recovery_confirmed_at": credential_recovery_confirmed_at,
            "stale_export_override": 1 if stale_override else 0,
            "stale_export_override_reason": stale_override_reason,
            "export_sha256": export_digest,
            "export_content_sha256": export_content_digest,
            "export_byte_length": export_length,
            "exported_at": export_timestamp,
            "drained_row_count": drained_count,
            "queue_pending_count": queue_counts["pending_count"],
            "queue_failed_count": queue_counts["failed_count"],
            "queue_syncing_count": queue_counts["syncing_count"],
            "queue_dead_letter_count": queue_counts["dead_letter_count"],
            **migration_recovery,
            "previous_config_version": previous_version,
            "reset_proof_sha256": reset_proof_digest,
            "evidence_fingerprint": evidence_fingerprint,
            "request_fingerprint": fingerprint,
            "requested_by_api_user": (
                api_user if request_origin == REQUEST_ORIGIN_DEVICE else None
            ),
            "requested_at": requested_at,
            "request_expires_at": request_expires_at,
            "erp_base_url": scope["erp_base_url"],
            "company": scope["company"],
            "currency": scope["currency"],
            "pos_profile": scope["pos_profile"],
            "warehouse": scope["warehouse"],
        }
    )
    with privileged_device_api_operation("device_safe_reset"):
        reset_doc.insert(ignore_permissions=True)
    response = _request_ack(reset_doc)
    frappe.db.commit()
    return response


def _migration_recovery_evidence_from_doc(
    reset_doc: Any,
) -> dict[str, int | bool]:
    return _normalize_migration_recovery_evidence(
        migration_recovery_point_count=getattr(
            reset_doc, "migration_recovery_point_count", 0
        ),
        migration_recovery_valid_point_count=getattr(
            reset_doc, "migration_recovery_valid_point_count", 0
        ),
        migration_recovery_invalid_point_count=getattr(
            reset_doc, "migration_recovery_invalid_point_count", 0
        ),
        migration_recovery_captured_pending_total=getattr(
            reset_doc, "migration_recovery_captured_pending_total", 0
        ),
        migration_recovery_review_required=getattr(
            reset_doc, "migration_recovery_review_required", 0
        ),
    )


def _migration_recovery_ack_fingerprint(
    reset_doc: Any,
    *,
    acknowledged_by: str,
    acknowledged_at: Any,
    acknowledgement_reason: str,
) -> str:
    return _request_fingerprint(
        {
            "reset_id": cstr(getattr(reset_doc, "reset_id", None)).strip()
            or cstr(getattr(reset_doc, "name", None)).strip(),
            "safe_reset_protocol_version": cint(
                getattr(reset_doc, "safe_reset_protocol_version", 0)
            ),
            "export_sha256": cstr(
                getattr(reset_doc, "export_sha256", None)
            ).strip(),
            "export_content_sha256": cstr(
                getattr(reset_doc, "export_content_sha256", None)
            ).strip(),
            "export_byte_length": cint(
                getattr(reset_doc, "export_byte_length", 0)
            ),
            **_migration_recovery_evidence_from_doc(reset_doc),
            "acknowledged_by": acknowledged_by,
            "acknowledged_at": _iso(acknowledged_at),
            "acknowledgement_reason": acknowledgement_reason,
        }
    )


def _migration_recovery_authorization_updates(
    reset_doc: Any,
    *,
    first_authorization: bool,
    confirmation: str | None,
    acknowledgement_reason: str | None,
) -> dict[str, Any]:
    evidence = _migration_recovery_evidence_from_doc(reset_doc)
    raw_confirmation = cstr(confirmation)
    raw_reason = cstr(acknowledgement_reason)
    reset_id = cstr(getattr(reset_doc, "reset_id", None)).strip() or cstr(
        getattr(reset_doc, "name", None)
    ).strip()
    export_sha256 = _require_sha256(
        cstr(getattr(reset_doc, "export_sha256", None)),
        "export_sha256",
    )
    expected_confirmation = f"ACK RECOVERY {reset_id} {export_sha256}"

    if not evidence["migration_recovery_review_required"]:
        if raw_confirmation or raw_reason:
            frappe.throw(
                _(
                    "Migration recovery acknowledgement is only valid when recovery points exist"
                ),
                frappe.ValidationError,
            )
        for fieldname in (
            "migration_recovery_acknowledged_by",
            "migration_recovery_acknowledged_at",
            "migration_recovery_acknowledgement_reason",
            "migration_recovery_ack_fingerprint",
        ):
            if cstr(getattr(reset_doc, fieldname, None)).strip():
                frappe.throw(
                    _("Unexpected stored migration recovery acknowledgement"),
                    frappe.ValidationError,
                )
        return {}

    if first_authorization:
        if not hmac.compare_digest(raw_confirmation, expected_confirmation):
            frappe.throw(
                _(
                    "Type the exact ACK RECOVERY confirmation for this reset and export digest"
                ),
                frappe.ValidationError,
            )
        reason = raw_reason.strip()
        if raw_reason != reason or len(reason) < 20 or len(reason) > 500:
            frappe.throw(
                _(
                    "Migration recovery acknowledgement reason must contain 20-500 characters without padding"
                ),
                frappe.ValidationError,
            )
        acknowledged_by = cstr(getattr(frappe.session, "user", None)).strip()
        acknowledged_at = now_datetime()
        return {
            "migration_recovery_acknowledged_by": acknowledged_by,
            "migration_recovery_acknowledged_at": acknowledged_at,
            "migration_recovery_acknowledgement_reason": reason,
            "migration_recovery_ack_fingerprint": _migration_recovery_ack_fingerprint(
                reset_doc,
                acknowledged_by=acknowledged_by,
                acknowledged_at=acknowledged_at,
                acknowledgement_reason=reason,
            ),
        }

    acknowledged_by = cstr(
        getattr(reset_doc, "migration_recovery_acknowledged_by", None)
    ).strip()
    acknowledged_at = getattr(
        reset_doc, "migration_recovery_acknowledged_at", None
    )
    reason = cstr(
        getattr(reset_doc, "migration_recovery_acknowledgement_reason", None)
    ).strip()
    fingerprint = cstr(
        getattr(reset_doc, "migration_recovery_ack_fingerprint", None)
    ).strip()
    if (
        not acknowledged_by
        or not acknowledged_at
        or len(reason) < 20
        or len(reason) > 500
        or not SHA256_PATTERN.fullmatch(fingerprint)
        or not hmac.compare_digest(
            fingerprint,
            _migration_recovery_ack_fingerprint(
                reset_doc,
                acknowledged_by=acknowledged_by,
                acknowledged_at=acknowledged_at,
                acknowledgement_reason=reason,
            ),
        )
        or not hmac.compare_digest(
            acknowledged_by,
            cstr(getattr(reset_doc, "authorized_by", None)).strip(),
        )
    ):
        frappe.throw(
            _("Stored migration recovery acknowledgement is incomplete or invalid"),
            frappe.ValidationError,
        )
    if raw_confirmation and not hmac.compare_digest(
        raw_confirmation, expected_confirmation
    ):
        frappe.throw(
            _("Migration recovery reissue confirmation does not match"),
            frappe.ValidationError,
        )
    if raw_reason and not hmac.compare_digest(raw_reason, reason):
        frappe.throw(
            _("Migration recovery reissue reason cannot change"),
            frappe.ValidationError,
        )
    return {}


def authorize_device_safe_reset(
    *,
    reset_id: str | None,
    expires_in_seconds: Any = None,
    erpnext_url: str | None = None,
    migration_recovery_confirmation: str | None = None,
    migration_recovery_acknowledgement_reason: str | None = None,
) -> dict[str, Any]:
    """Issue a short-lived approval challenge without changing device authority."""
    require_system_manager()
    reset_id_value = _require_reset_id(reset_id)
    ttl_seconds = _safe_reset_approval_ttl(expires_in_seconds)
    savepoint = make_savepoint("kopos_safe_reset_authorize")
    response: dict[str, Any] = {}

    try:
        reset_doc = _get_reset_with_device_lock(reset_id_value)
        _require_safe_reset_protocol_version(
            getattr(reset_doc, "safe_reset_protocol_version", None)
        )
        lifecycle_status = cstr(getattr(reset_doc, "status", None)).strip().lower()
        if lifecycle_status not in {"requested", "authorized", "redeemed"}:
            frappe.throw(
                _(
                    "Safe reset can only be authorized or reissued before it is "
                    "completed or expired"
                ),
                frappe.ValidationError,
            )
        first_authorization = lifecycle_status == "requested"
        if first_authorization and _is_expired(
            getattr(reset_doc, "request_expires_at", None)
        ):
            frappe.throw(
                _("Safe reset request expired; request a new reset from the device"),
                frappe.ValidationError,
            )
        migration_ack_updates = _migration_recovery_authorization_updates(
            reset_doc,
            first_authorization=first_authorization,
            confirmation=migration_recovery_confirmation,
            acknowledgement_reason=migration_recovery_acknowledgement_reason,
        )

        device_name = cstr(getattr(reset_doc, "device", None)).strip()
        device_doc = get_device_doc(name=device_name)
        request_origin = cstr(
            getattr(reset_doc, "request_origin", None)
        ).strip()
        if request_origin == REQUEST_ORIGIN_CREDENTIAL_RECOVERY:
            _validate_recovery_approval_binding(reset_doc, device_doc)
        else:
            _validate_reset_device_binding(reset_doc, device_doc)
        _validate_reset_scope(
            device_doc,
            erp_base_url=cstr(getattr(reset_doc, "erp_base_url", None)).strip(),
            company=cstr(getattr(reset_doc, "company", None)).strip(),
            currency=cstr(getattr(reset_doc, "currency", None)).strip(),
            pos_profile=cstr(getattr(reset_doc, "pos_profile", None)).strip(),
            warehouse=cstr(getattr(reset_doc, "warehouse", None)).strip(),
        )

        current_version = cint(getattr(device_doc, "config_version", 0))
        expected_version = cint(
            getattr(
                reset_doc,
                "new_config_version"
                if lifecycle_status == "redeemed"
                else "previous_config_version",
                0,
            )
        )
        if current_version <= 0 or current_version != expected_version:
            frappe.throw(
                _("Device configuration changed after safe reset evidence was recorded"),
                frappe.ValidationError,
            )

        _assert_stored_queue_evidence_is_drained(reset_doc)
        _assert_no_open_shift_or_unresolved_projection(
            cstr(getattr(reset_doc, "device_id", None)).strip()
        )

        authorizing_manager = cstr(getattr(frappe.session, "user", None)).strip()
        issued_at = now_datetime()
        next_authorization_count = cint(
            getattr(reset_doc, "authorization_count", 0)
        ) + 1
        approval_generation = cint(
            getattr(reset_doc, "approval_generation", 0)
        ) + 1
        approval_base_url = _matching_provisioning_erp_base_url(
            erpnext_url,
            cstr(getattr(reset_doc, "erp_base_url", None)).strip(),
        )
        _require_safe_reset_https(approval_base_url)
        approval_token = _new_256_bit_secret()
        approval_challenge_id = f"KSAC-{secrets.token_hex(32)}"
        approval_expires_at = issued_at + timedelta(seconds=ttl_seconds)
        approval_token_digest = _sha256_text(approval_token)
        approval_fingerprint = _approval_challenge_fingerprint(
            reset_doc,
            approval_challenge_id=approval_challenge_id,
            approval_generation=approval_generation,
            approval_expires_at=approval_expires_at,
            approval_token_sha256=approval_token_digest,
            approval_erpnext_url=approval_base_url,
        )
        approval_link = _approval_link(
            erpnext_url=approval_base_url,
            reset_id=reset_id_value,
            request_id=cstr(getattr(reset_doc, "request_id", None)).strip(),
            approval_challenge_id=approval_challenge_id,
            approval_generation=approval_generation,
            approval_expires_at=approval_expires_at,
            approval_token=approval_token,
        )
        qr_value = get_qr_svg_code(approval_link)
        approval_qr_svg = (
            qr_value.decode("utf-8")
            if isinstance(qr_value, (bytes, bytearray, memoryview))
            else cstr(qr_value)
        )
        if not approval_qr_svg:
            raise RuntimeError("Safe reset approval QR could not be rendered")

        _apply_reset_transition(
            reset_doc,
            {
                "status": "authorized" if first_authorization else lifecycle_status,
                "authorized_by": authorizing_manager
                if first_authorization
                else getattr(reset_doc, "authorized_by", None),
                "authorized_at": issued_at
                if first_authorization
                else getattr(reset_doc, "authorized_at", None),
                **migration_ack_updates,
                "authorization_count": next_authorization_count,
                "approval_challenge_id": approval_challenge_id,
                "approval_token_sha256": approval_token_digest,
                "approval_generation": approval_generation,
                "approval_issued_by": authorizing_manager,
                "approval_issued_at": issued_at,
                "approval_expires_at": approval_expires_at,
                "approval_fingerprint": approval_fingerprint,
                "approval_erpnext_url": approval_base_url,
                "current_redemption_idempotency_sha256": None,
                "current_redemption_result_fingerprint": None,
            },
        )
        response = _approval_response(
            reset_doc,
            approval_link=approval_link,
            approval_qr_svg=approval_qr_svg,
        )
        frappe.db.commit()
    except frappe.ValidationError:
        rollback_to_savepoint(
            savepoint,
            title="KoPOS safe reset authorization rollback failed",
        )
        raise
    except Exception as error:
        rollback_to_savepoint(
            savepoint,
            title="KoPOS safe reset authorization rollback failed",
        )
        log_sanitized_error("KoPOS safe reset authorization failed", error)
        frappe.throw(
            _("Safe reset authorization failed before a usable approval QR was issued"),
            frappe.ValidationError,
        )
    return response


def _approval_challenge_fingerprint(
    reset_doc: Any,
    *,
    approval_challenge_id: str,
    approval_generation: int,
    approval_expires_at: Any,
    approval_token_sha256: str,
    approval_erpnext_url: str,
) -> str:
    return _request_fingerprint(
        {
            "safe_reset_protocol_version": cint(
                getattr(reset_doc, "safe_reset_protocol_version", 0)
            ),
            "reset_id": cstr(getattr(reset_doc, "reset_id", None)).strip()
            or cstr(getattr(reset_doc, "name", None)).strip(),
            "request_id": cstr(getattr(reset_doc, "request_id", None)).strip(),
            "device_id": cstr(getattr(reset_doc, "device_id", None)).strip(),
            "request_origin": cstr(
                getattr(reset_doc, "request_origin", None)
            ).strip(),
            "evidence_fingerprint": cstr(
                getattr(reset_doc, "evidence_fingerprint", None)
            ).strip(),
            "request_fingerprint": cstr(
                getattr(reset_doc, "request_fingerprint", None)
            ).strip(),
            "previous_config_version": cint(
                getattr(reset_doc, "previous_config_version", 0)
            ),
            "export_sha256": cstr(
                getattr(reset_doc, "export_sha256", None)
            ).strip(),
            "export_content_sha256": cstr(
                getattr(reset_doc, "export_content_sha256", None)
            ).strip(),
            "export_byte_length": cint(
                getattr(reset_doc, "export_byte_length", 0)
            ),
            **_migration_recovery_evidence_from_doc(reset_doc),
            "erp_base_url": _normalize_erp_base_url(
                approval_erpnext_url,
                "approval ERP base URL",
            ),
            "approval_challenge_id": approval_challenge_id,
            "approval_generation": approval_generation,
            "approval_expires_at": _iso_utc(approval_expires_at),
            "approval_token_sha256": approval_token_sha256,
        }
    )


def _new_256_bit_secret() -> str:
    for _attempt in range(3):
        candidate = secrets.token_urlsafe(32)
        if SECRET_256_PATTERN.fullmatch(candidate):
            return candidate
    raise RuntimeError("Could not generate a 256-bit safe reset secret")


def _approval_link(
    *,
    erpnext_url: str,
    reset_id: str,
    request_id: str,
    approval_challenge_id: str,
    approval_generation: int,
    approval_expires_at: Any,
    approval_token: str,
) -> str:
    parameters = (
        ("base_url", erpnext_url),
        ("provisioning_mode", PROVISIONING_MODE_SAFE_RESET_APPROVAL),
        ("safe_reset_protocol_version", str(SAFE_RESET_PROTOCOL_VERSION)),
        ("reset_id", reset_id),
        ("request_id", request_id),
        ("approval_challenge_id", approval_challenge_id),
        ("approval_generation", str(approval_generation)),
        ("approval_expires_at", cstr(_iso_utc(approval_expires_at))),
        ("token", approval_token),
    )
    query = "&".join(
        f"{quote(name, safe='')}={quote(value, safe='')}"
        for name, value in parameters
    )
    return f"kopos://provision?{query}"


def _approval_response(
    reset_doc: Any,
    *,
    approval_link: str,
    approval_qr_svg: str,
) -> dict[str, Any]:
    response = {
        "status": "authorized",
        "lifecycle_status": cstr(
            getattr(reset_doc, "status", None)
        ).strip().lower(),
        "provisioning_mode": PROVISIONING_MODE_SAFE_RESET_APPROVAL,
        "approval_challenge_id": cstr(
            getattr(reset_doc, "approval_challenge_id", None)
        ).strip(),
        "approval_generation": cint(
            getattr(reset_doc, "approval_generation", 0)
        ),
        "approval_issued_at": _iso_utc(
            getattr(reset_doc, "approval_issued_at", None)
        ),
        "approval_expires_at": _iso_utc(
            getattr(reset_doc, "approval_expires_at", None)
        ),
        "approval_link": approval_link,
        "approval_qr_svg": approval_qr_svg,
    }
    response.update(_safe_reset_response_tags(reset_doc))
    response["provisioning_mode"] = PROVISIONING_MODE_SAFE_RESET_APPROVAL
    return response


def _require_safe_reset_https(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme == "https":
        return
    developer_mode = bool(
        cint(getattr(getattr(frappe, "conf", None), "developer_mode", 0))
    )
    if developer_mode and cstr(parsed.hostname).lower() in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        return
    frappe.throw(
        _("Safe reset approval requires an HTTPS ERP origin"),
        frappe.ValidationError,
    )


def _validate_recovery_approval_binding(reset_doc: Any, device_doc: Any) -> None:
    if device_doc is None or not cint(getattr(device_doc, "enabled", 0)):
        frappe.throw(
            _("Safe reset requires an enabled registered KoPOS Device"),
            frappe.ValidationError,
        )
    if not hmac.compare_digest(
        cstr(getattr(reset_doc, "device_id", None)).strip(),
        cstr(getattr(device_doc, "device_id", None)).strip(),
    ):
        frappe.throw(_("Safe reset device binding changed"), frappe.ValidationError)
    expected_api_user = _credential_recovery_target_api_user(device_doc)
    audited_api_user = cstr(getattr(reset_doc, "api_user", None)).strip()
    if not hmac.compare_digest(expected_api_user, audited_api_user):
        frappe.throw(
            _("Safe reset API user binding changed after recovery registration"),
            frappe.ValidationError,
        )
    ensure_unique_device_api_user(
        audited_api_user,
        current_device_name=cstr(getattr(device_doc, "name", None)).strip() or None,
    )


def _require_request_id(value: str | None) -> str:
    request_id = cstr(value)
    if request_id != request_id.strip() or not REQUEST_ID_PATTERN.fullmatch(
        request_id
    ):
        frappe.throw(_("Valid safe reset request_id is required"), frappe.ValidationError)
    return request_id


def _enforce_safe_reset_redemption_rate_limit(reset_id: str) -> None:
    request = getattr(getattr(frappe, "local", None), "request", None) or getattr(
        frappe,
        "request",
        None,
    )
    remote_address = cstr(getattr(request, "remote_addr", None)).strip() or "unknown"
    bucket_digest = _sha256_text(f"{remote_address}\n{reset_id}")
    cache_key = f"kopos:safe-reset-redeem-rate:{bucket_digest}"
    try:
        cache = frappe.cache()
        make_key = getattr(cache, "make_key", None)
        eval_script = getattr(cache, "eval", None)
        if not callable(make_key) or not callable(eval_script):
            raise RuntimeError("Redis does not support atomic safe reset rate limiting")
        raw_count = eval_script(
            SAFE_RESET_RATE_LIMIT_SCRIPT,
            1,
            make_key(cache_key),
            SAFE_RESET_RATE_LIMIT_WINDOW_SECONDS,
        )
        if isinstance(raw_count, (bytes, bytearray, memoryview)):
            raw_count = bytes(raw_count).decode("ascii")
        if isinstance(raw_count, bool) or not re.fullmatch(r"[0-9]+", cstr(raw_count)):
            raise RuntimeError("Redis returned an invalid safe reset rate-limit count")
        attempt_count = int(raw_count)
    except Exception as error:
        log_sanitized_error("KoPOS safe reset rate limiter unavailable", error)
        frappe.throw(
            _("Safe reset redemption is temporarily unavailable"),
            frappe.ValidationError,
        )
        raise
    if attempt_count > SAFE_RESET_RATE_LIMIT_MAX_ATTEMPTS:
        frappe.throw(
            _("Too many safe reset redemption attempts; retry shortly"),
            frappe.ValidationError,
        )


def _validate_redeem_request_identity(
    reset_doc: Any,
    *,
    protocol_version: int,
    request_id: str,
    reset_proof_nonce: str,
    export_sha256: str,
    export_content_sha256: str,
    export_byte_length: int,
) -> None:
    stored_protocol = _require_safe_reset_protocol_version(
        getattr(reset_doc, "safe_reset_protocol_version", None)
    )
    if stored_protocol != protocol_version or not hmac.compare_digest(
        cstr(getattr(reset_doc, "request_id", None)).strip(),
        request_id,
    ):
        frappe.throw(
            _("Safe reset request identity does not match"),
            frappe.ValidationError,
        )
    _validate_reset_proof(reset_doc, reset_proof_nonce)
    _validate_archive_evidence_matches(
        reset_doc,
        export_sha256=export_sha256,
        export_content_sha256=export_content_sha256,
        export_byte_length=export_byte_length,
        context="redemption",
    )


def _validate_archive_evidence_matches(
    reset_doc: Any,
    *,
    export_sha256: str,
    export_content_sha256: str,
    export_byte_length: int,
    context: str,
) -> None:
    digest_matches = hmac.compare_digest(
        cstr(getattr(reset_doc, "export_sha256", None)).strip(),
        export_sha256,
    ) and hmac.compare_digest(
        cstr(getattr(reset_doc, "export_content_sha256", None)).strip(),
        export_content_sha256,
    )
    length_matches = (
        cint(getattr(reset_doc, "export_byte_length", 0)) == export_byte_length
    )
    if not digest_matches or not length_matches:
        frappe.throw(
            _("Safe reset {0} retained-archive evidence does not match").format(
                context
            ),
            frappe.ValidationError,
        )


def _match_redemption_challenge(
    reset_doc: Any,
    *,
    challenge_id: str,
    generation: int,
    token_sha256: str,
) -> str:
    current_matches = (
        hmac.compare_digest(
            cstr(getattr(reset_doc, "approval_challenge_id", None)).strip(),
            challenge_id,
        )
        and cint(getattr(reset_doc, "approval_generation", 0)) == generation
        and hmac.compare_digest(
            cstr(getattr(reset_doc, "approval_token_sha256", None)).strip(),
            token_sha256,
        )
    )
    committed_matches = (
        hmac.compare_digest(
            cstr(
                getattr(reset_doc, "redeemed_approval_challenge_id", None)
            ).strip(),
            challenge_id,
        )
        and cint(getattr(reset_doc, "redeemed_approval_generation", 0))
        == generation
        and hmac.compare_digest(
            cstr(
                getattr(reset_doc, "redeemed_approval_token_sha256", None)
            ).strip(),
            token_sha256,
        )
    )
    if committed_matches:
        return "committed"
    if current_matches:
        return "current"
    frappe.throw(
        _("Safe reset approval token, challenge, or generation is stale"),
        frappe.ValidationError,
    )
    raise ValueError("Safe reset approval challenge is stale")


def _validate_current_approval_fingerprint(reset_doc: Any) -> None:
    stored = cstr(getattr(reset_doc, "approval_fingerprint", None)).strip()
    expected = _approval_challenge_fingerprint(
        reset_doc,
        approval_challenge_id=_require_challenge_id(
            cstr(getattr(reset_doc, "approval_challenge_id", None))
        ),
        approval_generation=_require_approval_generation(
            getattr(reset_doc, "approval_generation", None)
        ),
        approval_expires_at=getattr(reset_doc, "approval_expires_at", None),
        approval_token_sha256=_require_sha256(
            cstr(getattr(reset_doc, "approval_token_sha256", None)),
            "approval_token_sha256",
        ),
        approval_erpnext_url=cstr(
            getattr(reset_doc, "approval_erpnext_url", None)
        ).strip(),
    )
    if not hmac.compare_digest(stored, expected):
        frappe.throw(
            _("Safe reset approval audit fingerprint is invalid"),
            frappe.ValidationError,
        )


def _retry_redeemed_result(
    reset_doc: Any,
    *,
    challenge_kind: str,
    idempotency_sha256: str,
    export_sha256: str,
    export_content_sha256: str,
    export_byte_length: int,
) -> dict[str, Any]:
    if (
        challenge_kind == "committed"
        and _is_expired(getattr(reset_doc, "redeemed_recovery_expires_at", None))
    ):
        frappe.throw(
            _(
                "Safe reset redemption response recovery expired; ask a System "
                "Manager to reissue an approval"
            ),
            frappe.ValidationError,
        )
    _validate_stored_redemption_result(reset_doc)
    _validate_archive_evidence_matches(
        reset_doc,
        export_sha256=export_sha256,
        export_content_sha256=export_content_sha256,
        export_byte_length=export_byte_length,
        context="redemption retry",
    )
    committed_idempotency = cstr(
        getattr(reset_doc, "redemption_idempotency_sha256", None)
    ).strip()
    if not hmac.compare_digest(committed_idempotency, idempotency_sha256):
        frappe.throw(
            _("Safe reset redemption idempotency key was already used differently"),
            frappe.ValidationError,
        )
    should_bind_current_challenge = False
    if challenge_kind == "current":
        _validate_current_approval_fingerprint(reset_doc)
        current_idempotency = cstr(
            getattr(reset_doc, "current_redemption_idempotency_sha256", None)
        ).strip()
        if not current_idempotency:
            if _is_expired(getattr(reset_doc, "approval_expires_at", None)):
                frappe.throw(
                    _("Safe reset approval challenge expired"),
                    frappe.ValidationError,
                )
            should_bind_current_challenge = True
        elif not hmac.compare_digest(current_idempotency, committed_idempotency):
            frappe.throw(
                _("Safe reset reissued approval is bound to a different redemption"),
                frappe.ValidationError,
            )
    setup = _read_redeemed_setup(reset_doc)
    response = _redemption_response(
        reset_doc,
        setup=setup,
        recovery_path=(
            "reissued_current_challenge"
            if challenge_kind == "current"
            else "initial_redemption"
        ),
    )
    if should_bind_current_challenge:
        _apply_reset_transition(
            reset_doc,
            {
                "current_redemption_idempotency_sha256": idempotency_sha256,
                "current_redemption_result_fingerprint": cstr(
                    getattr(reset_doc, "redemption_result_fingerprint", None)
                ).strip(),
                "last_redeemed_at": now_datetime(),
                "redemption_count": cint(getattr(reset_doc, "redemption_count", 0))
                + 1,
            },
        )
    return response


def _build_redeemed_setup(
    reset_doc: Any,
    *,
    device_doc: Any,
    credentials: Mapping[str, str],
    new_config_version: int,
) -> dict[str, Any]:
    setup = serialize_device_config(
        device_doc,
        include_secrets=True,
        api_key=credentials["api_key"],
        api_secret=credentials["api_secret"],
    )
    if not isinstance(setup, dict):
        raise RuntimeError("Safe reset device setup could not be serialized")
    setup.update(
        {
            "erpnext_url": cstr(
                getattr(reset_doc, "approval_erpnext_url", None)
            ).strip(),
            "provisioning_user": cstr(
                getattr(reset_doc, "api_user", None)
            ).strip(),
            "provisioning_mode": PROVISIONING_MODE_SAFE_RESET,
            "safe_reset_protocol_version": SAFE_RESET_PROTOCOL_VERSION,
            "request_origin": cstr(
                getattr(reset_doc, "request_origin", None)
            ).strip(),
            "request_id": cstr(getattr(reset_doc, "request_id", None)).strip(),
            "reset_id": cstr(getattr(reset_doc, "reset_id", None)).strip()
            or cstr(getattr(reset_doc, "name", None)).strip(),
            "approval_challenge_id": cstr(
                getattr(reset_doc, "approval_challenge_id", None)
            ).strip(),
            "approval_generation": cint(
                getattr(reset_doc, "approval_generation", 0)
            ),
            "previous_config_version": cint(
                getattr(reset_doc, "previous_config_version", 0)
            ),
            "new_config_version": new_config_version,
            "export_sha256": cstr(
                getattr(reset_doc, "export_sha256", None)
            ).strip(),
            "export_content_sha256": cstr(
                getattr(reset_doc, "export_content_sha256", None)
            ).strip(),
            "export_byte_length": cint(
                getattr(reset_doc, "export_byte_length", 0)
            ),
            **_migration_recovery_evidence_from_doc(reset_doc),
        }
    )
    if cint(setup.get("config_version")) != new_config_version:
        raise RuntimeError("Safe reset setup config version is stale")
    return setup


def _nonsecret_setup_snapshot(setup: Mapping[str, Any]) -> dict[str, Any]:
    forbidden = {"api_key", "api_secret"}
    snapshot = {str(key): value for key, value in setup.items() if key not in forbidden}
    if forbidden.intersection(snapshot):
        raise RuntimeError("Safe reset snapshot contains credentials")
    return snapshot


def _canonical_response_setup(setup: Mapping[str, Any]) -> dict[str, Any]:
    api_key = cstr(setup.get("api_key")).strip()
    api_secret = cstr(setup.get("api_secret")).strip()
    if not api_key or not api_secret:
        raise RuntimeError("Safe reset setup credentials are incomplete")
    canonical_snapshot = json.dumps(
        _nonsecret_setup_snapshot(setup),
        sort_keys=True,
        separators=(",", ":"),
    )
    normalized = json.loads(canonical_snapshot)
    if not isinstance(normalized, dict):
        raise RuntimeError("Safe reset setup snapshot is invalid")
    normalized["api_key"] = api_key
    normalized["api_secret"] = api_secret
    return normalized


def _store_redeemed_setup(reset_id: str, setup: Mapping[str, Any]) -> str:
    snapshot = _nonsecret_setup_snapshot(setup)
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    setup_sha256 = _sha256_text(canonical)
    set_encrypted_password(
        SAFE_RESET_DOCTYPE,
        reset_id,
        canonical,
        "redemption_setup_snapshot",
    )
    persisted = get_decrypted_password(
        SAFE_RESET_DOCTYPE,
        reset_id,
        "redemption_setup_snapshot",
        raise_exception=False,
    )
    if not isinstance(persisted, str) or not hmac.compare_digest(
        persisted,
        canonical,
    ):
        raise RuntimeError("Safe reset setup snapshot could not be verified")
    return setup_sha256


def _read_redeemed_setup(reset_doc: Any) -> dict[str, Any]:
    reset_id = cstr(getattr(reset_doc, "reset_id", None)).strip() or cstr(
        getattr(reset_doc, "name", None)
    ).strip()
    canonical = get_decrypted_password(
        SAFE_RESET_DOCTYPE,
        reset_id,
        "redemption_setup_snapshot",
        raise_exception=False,
    )
    if not isinstance(canonical, str) or not hmac.compare_digest(
        _sha256_text(canonical),
        cstr(getattr(reset_doc, "redemption_setup_sha256", None)).strip(),
    ):
        frappe.throw(
            _("Safe reset recovery setup is unavailable or corrupted"),
            frappe.ValidationError,
        )
    try:
        snapshot = json.loads(canonical)
    except json.JSONDecodeError as error:
        raise frappe.ValidationError(
            "Safe reset recovery setup is unavailable or corrupted"
        ) from error
    if not isinstance(snapshot, dict) or {"api_key", "api_secret"}.intersection(
        snapshot
    ):
        frappe.throw(
            _("Safe reset recovery setup is unavailable or corrupted"),
            frappe.ValidationError,
        )
    device_doc = get_device_doc(
        name=cstr(getattr(reset_doc, "device", None)).strip()
    )
    _validate_reset_device_binding(reset_doc, device_doc)
    if cint(getattr(device_doc, "config_version", 0)) != cint(
        getattr(reset_doc, "new_config_version", 0)
    ):
        frappe.throw(
            _("Safe reset recovery setup no longer matches ERP authority"),
            frappe.ValidationError,
        )
    credentials = _read_current_device_api_credentials(device_doc)
    if not hmac.compare_digest(
        _sha256_text(credentials["api_key"]),
        cstr(getattr(reset_doc, "issued_api_key_sha256", None)).strip(),
    ) or not hmac.compare_digest(
        _sha256_text(credentials["api_secret"]),
        cstr(getattr(reset_doc, "issued_api_secret_sha256", None)).strip(),
    ):
        frappe.throw(
            _("Safe reset recovery credentials no longer match the issued result"),
            frappe.ValidationError,
        )
    snapshot["api_key"] = credentials["api_key"]
    snapshot["api_secret"] = credentials["api_secret"]
    return snapshot


def _redemption_result_fingerprint(
    reset_doc: Any,
    *,
    redemption_idempotency_sha256: str,
    export_sha256: str,
    export_content_sha256: str,
    export_byte_length: int,
    setup_sha256: str,
    issued_api_key_sha256: str,
    issued_api_secret_sha256: str,
    redeemed_at: Any,
    recovery_expires_at: Any,
) -> str:
    return _request_fingerprint(
        {
            "safe_reset_protocol_version": cint(
                getattr(reset_doc, "safe_reset_protocol_version", 0)
            ),
            "reset_id": cstr(getattr(reset_doc, "reset_id", None)).strip(),
            "request_id": cstr(getattr(reset_doc, "request_id", None)).strip(),
            "approval_challenge_id": cstr(
                getattr(reset_doc, "approval_challenge_id", None)
            ).strip(),
            "approval_generation": cint(
                getattr(reset_doc, "approval_generation", 0)
            ),
            "approval_token_sha256": cstr(
                getattr(reset_doc, "approval_token_sha256", None)
            ).strip(),
            "approval_fingerprint": cstr(
                getattr(reset_doc, "approval_fingerprint", None)
            ).strip(),
            "approval_expires_at": _iso_utc(
                getattr(reset_doc, "approval_expires_at", None)
            ),
            "redemption_idempotency_sha256": redemption_idempotency_sha256,
            "export_sha256": export_sha256,
            "export_content_sha256": export_content_sha256,
            "export_byte_length": export_byte_length,
            "redemption_setup_sha256": setup_sha256,
            "issued_api_key_sha256": issued_api_key_sha256,
            "issued_api_secret_sha256": issued_api_secret_sha256,
            "redeemed_at": _iso_utc(redeemed_at),
            "redeemed_recovery_expires_at": _iso_utc(recovery_expires_at),
        }
    )


def _validate_stored_redemption_result(reset_doc: Any) -> None:
    expected = _request_fingerprint(
        {
            "safe_reset_protocol_version": cint(
                getattr(reset_doc, "safe_reset_protocol_version", 0)
            ),
            "reset_id": cstr(getattr(reset_doc, "reset_id", None)).strip(),
            "request_id": cstr(getattr(reset_doc, "request_id", None)).strip(),
            "approval_challenge_id": cstr(
                getattr(reset_doc, "redeemed_approval_challenge_id", None)
            ).strip(),
            "approval_generation": cint(
                getattr(reset_doc, "redeemed_approval_generation", 0)
            ),
            "approval_token_sha256": cstr(
                getattr(reset_doc, "redeemed_approval_token_sha256", None)
            ).strip(),
            "approval_fingerprint": cstr(
                getattr(reset_doc, "redeemed_approval_fingerprint", None)
            ).strip(),
            "approval_expires_at": _iso_utc(
                getattr(reset_doc, "redeemed_approval_expires_at", None)
            ),
            "redemption_idempotency_sha256": cstr(
                getattr(reset_doc, "redemption_idempotency_sha256", None)
            ).strip(),
            "export_sha256": cstr(
                getattr(reset_doc, "redemption_export_sha256", None)
            ).strip(),
            "export_content_sha256": cstr(
                getattr(reset_doc, "redemption_export_content_sha256", None)
            ).strip(),
            "export_byte_length": cint(
                getattr(reset_doc, "redemption_export_byte_length", 0)
            ),
            "redemption_setup_sha256": cstr(
                getattr(reset_doc, "redemption_setup_sha256", None)
            ).strip(),
            "issued_api_key_sha256": cstr(
                getattr(reset_doc, "issued_api_key_sha256", None)
            ).strip(),
            "issued_api_secret_sha256": cstr(
                getattr(reset_doc, "issued_api_secret_sha256", None)
            ).strip(),
            "redeemed_at": _iso_utc(
                getattr(reset_doc, "redemption_issued_at", None)
            ),
            "redeemed_recovery_expires_at": _iso_utc(
                getattr(reset_doc, "redeemed_recovery_expires_at", None)
            ),
        }
    )
    if not hmac.compare_digest(
        expected,
        cstr(getattr(reset_doc, "redemption_result_fingerprint", None)).strip(),
    ):
        frappe.throw(
            _("Safe reset redemption audit result is invalid"),
            frappe.ValidationError,
        )


def _redemption_response(
    reset_doc: Any,
    *,
    setup: dict[str, Any],
    recovery_path: str,
) -> dict[str, Any]:
    committed_result = {
        "approval_challenge_id": cstr(
            getattr(reset_doc, "redeemed_approval_challenge_id", None)
        ).strip(),
        "approval_generation": cint(
            getattr(reset_doc, "redeemed_approval_generation", 0)
        ),
        "approval_expires_at": _iso_utc(
            getattr(reset_doc, "redeemed_approval_expires_at", None)
        ),
        "issued_at": _iso_utc(
            getattr(reset_doc, "redemption_issued_at", None)
        ),
        "result_recovery_expires_at": _iso_utc(
            getattr(reset_doc, "redeemed_recovery_expires_at", None)
        ),
    }
    current_approval = {
        "approval_challenge_id": cstr(
            getattr(reset_doc, "approval_challenge_id", None)
        ).strip(),
        "approval_generation": cint(
            getattr(reset_doc, "approval_generation", 0)
        ),
        "approval_expires_at": _iso_utc(
            getattr(reset_doc, "approval_expires_at", None)
        ),
    }
    response = {
        "status": "ok",
        "recovery_path": recovery_path,
        "redemption_idempotency_sha256": cstr(
            getattr(reset_doc, "redemption_idempotency_sha256", None)
        ).strip(),
        "issued_at": committed_result["issued_at"],
        "expires_at": committed_result["result_recovery_expires_at"],
        "committed_result": committed_result,
        "current_approval": current_approval,
        "setup": setup,
    }
    response.update(_safe_reset_response_tags(reset_doc))
    response.update(
        {
            "approval_challenge_id": cstr(
                getattr(reset_doc, "redeemed_approval_challenge_id", None)
            ).strip(),
            "approval_generation": cint(
                getattr(reset_doc, "redeemed_approval_generation", 0)
            ),
            "approval_expires_at": _iso_utc(
                getattr(reset_doc, "redeemed_approval_expires_at", None)
            ),
        }
    )
    return response


def _validate_stored_completion_result(reset_doc: Any) -> None:
    expected = _request_fingerprint(
        {
            "safe_reset_protocol_version": cint(
                getattr(reset_doc, "safe_reset_protocol_version", 0)
            ),
            "reset_id": cstr(getattr(reset_doc, "reset_id", None)).strip(),
            "device_id": cstr(getattr(reset_doc, "device_id", None)).strip(),
            "new_config_version": cint(
                getattr(reset_doc, "new_config_version", 0)
            ),
            "export_sha256": cstr(
                getattr(reset_doc, "completion_export_sha256", None)
            ).strip(),
            "export_content_sha256": cstr(
                getattr(reset_doc, "completion_export_content_sha256", None)
            ).strip(),
            "export_byte_length": cint(
                getattr(reset_doc, "completion_export_byte_length", 0)
            ),
            "completion_idempotency_sha256": cstr(
                getattr(reset_doc, "completion_idempotency_sha256", None)
            ).strip(),
            "completed_by_api_user": cstr(
                getattr(reset_doc, "completed_by_api_user", None)
            ).strip(),
            "completed_at": _iso_utc(
                getattr(reset_doc, "completed_at", None)
            ),
        }
    )
    if not hmac.compare_digest(
        expected,
        cstr(getattr(reset_doc, "completion_result_fingerprint", None)).strip(),
    ):
        frappe.throw(
            _("Safe reset completion audit result is invalid"),
            frappe.ValidationError,
        )


def redeem_device_safe_reset_approval(
    *,
    safe_reset_protocol_version: Any,
    token: str | None,
    reset_id: str | None,
    request_id: str | None,
    approval_challenge_id: str | None,
    approval_generation: Any,
    reset_proof_nonce: str | None,
    redemption_idempotency_key: str | None,
    export_sha256: str | None,
    export_content_sha256: str | None,
    export_byte_length: Any,
) -> dict[str, Any]:
    """Redeem an approval only after the trusted app rehashes its retained archive.

    The archive fields are a trusted-app rehash assertion. They are not described as
    cryptographic proof that the tablet possesses the archive.
    """
    protocol_version = _require_safe_reset_protocol_version(
        safe_reset_protocol_version
    )
    reset_id_value = _require_reset_id(reset_id)
    request_id_value = _require_request_id(request_id)
    challenge_id = _require_challenge_id(approval_challenge_id)
    generation = _require_approval_generation(approval_generation)
    token_value = _require_256_bit_secret(token, "token")
    proof_nonce = _require_reset_proof_nonce(reset_proof_nonce)
    idempotency_key = _require_256_bit_secret(
        redemption_idempotency_key,
        "redemption_idempotency_key",
    )
    export_digest = _require_sha256(export_sha256, "export_sha256")
    content_digest = _require_sha256(
        export_content_sha256,
        "export_content_sha256",
    )
    byte_length = _require_archive_byte_length(export_byte_length)
    _enforce_safe_reset_redemption_rate_limit(reset_id_value)

    savepoint = make_savepoint("kopos_safe_reset_redeem")
    try:
        reset_doc = _get_reset_with_device_lock(reset_id_value)
        _validate_redeem_request_identity(
            reset_doc,
            protocol_version=protocol_version,
            request_id=request_id_value,
            reset_proof_nonce=proof_nonce,
            export_sha256=export_digest,
            export_content_sha256=content_digest,
            export_byte_length=byte_length,
        )
        lifecycle_status = cstr(getattr(reset_doc, "status", None)).strip().lower()
        if lifecycle_status not in {"authorized", "redeemed"}:
            frappe.throw(
                _("Safe reset approval is not redeemable"),
                frappe.ValidationError,
            )

        token_digest = _sha256_text(token_value)
        idempotency_digest = _sha256_text(idempotency_key)
        challenge_kind = _match_redemption_challenge(
            reset_doc,
            challenge_id=challenge_id,
            generation=generation,
            token_sha256=token_digest,
        )
        if lifecycle_status == "redeemed":
            response = _retry_redeemed_result(
                reset_doc,
                challenge_kind=challenge_kind,
                idempotency_sha256=idempotency_digest,
                export_sha256=export_digest,
                export_content_sha256=content_digest,
                export_byte_length=byte_length,
            )
            frappe.db.commit()
            return response
        if challenge_kind != "current":
            frappe.throw(
                _("Safe reset approval challenge is stale"),
                frappe.ValidationError,
            )
        _validate_current_approval_fingerprint(reset_doc)
        if _is_expired(getattr(reset_doc, "approval_expires_at", None)):
            frappe.throw(
                _("Safe reset approval challenge expired; ask a System Manager to reissue it"),
                frappe.ValidationError,
            )

        device_name = cstr(getattr(reset_doc, "device", None)).strip()
        device_doc = get_device_doc(name=device_name)
        previous_identity_state = "complete"
        request_origin = cstr(
            getattr(reset_doc, "request_origin", None)
        ).strip()
        if request_origin == REQUEST_ORIGIN_CREDENTIAL_RECOVERY:
            previous_identity_state = _restore_recovery_device_api_identity(
                reset_doc,
                device_doc,
            )
        _validate_reset_device_binding(reset_doc, device_doc)
        _validate_reset_scope(
            device_doc,
            erp_base_url=cstr(getattr(reset_doc, "erp_base_url", None)).strip(),
            company=cstr(getattr(reset_doc, "company", None)).strip(),
            currency=cstr(getattr(reset_doc, "currency", None)).strip(),
            pos_profile=cstr(getattr(reset_doc, "pos_profile", None)).strip(),
            warehouse=cstr(getattr(reset_doc, "warehouse", None)).strip(),
        )
        previous_config_version = cint(
            getattr(reset_doc, "previous_config_version", 0)
        )
        if previous_config_version <= 0 or cint(
            getattr(device_doc, "config_version", 0)
        ) != previous_config_version:
            frappe.throw(
                _("Device configuration changed before safe reset redemption"),
                frappe.ValidationError,
            )
        _assert_stored_queue_evidence_is_drained(reset_doc)
        _assert_no_open_shift_or_unresolved_projection(
            cstr(getattr(reset_doc, "device_id", None)).strip()
        )

        credentials = _rotate_device_api_credentials(
            device_doc,
            allow_incomplete_previous=(
                request_origin == REQUEST_ORIGIN_CREDENTIAL_RECOVERY
            ),
        )
        new_config_version = previous_config_version + 1
        frappe.db.set_value(
            "KoPOS Device",
            device_name,
            "config_version",
            new_config_version,
            update_modified=False,
        )
        setattr(device_doc, "config_version", new_config_version)
        redeemed_at = now_datetime()
        recovery_expires_at = redeemed_at + timedelta(
            seconds=REDEEMED_RECOVERY_TTL_SECONDS
        )
        setup = _build_redeemed_setup(
            reset_doc,
            device_doc=device_doc,
            credentials=credentials,
            new_config_version=new_config_version,
        )
        setup = _canonical_response_setup(setup)
        setup_sha256 = _store_redeemed_setup(reset_id_value, setup)
        previous_credential_state = (
            previous_identity_state
            if previous_identity_state != "complete"
            else credentials["previous_credential_state"]
        )
        result_fingerprint = _redemption_result_fingerprint(
            reset_doc,
            redemption_idempotency_sha256=idempotency_digest,
            export_sha256=export_digest,
            export_content_sha256=content_digest,
            export_byte_length=byte_length,
            setup_sha256=setup_sha256,
            issued_api_key_sha256=credentials["issued_api_key_sha256"],
            issued_api_secret_sha256=credentials["issued_api_secret_sha256"],
            redeemed_at=redeemed_at,
            recovery_expires_at=recovery_expires_at,
        )
        _apply_reset_transition(
            reset_doc,
            {
                "status": "redeemed",
                "credential_rotated_at": redeemed_at,
                "new_config_version": new_config_version,
                "previous_credential_state": previous_credential_state,
                "revoked_api_key_sha256": credentials["revoked_api_key_sha256"],
                "issued_api_key_sha256": credentials["issued_api_key_sha256"],
                "issued_api_secret_sha256": credentials[
                    "issued_api_secret_sha256"
                ],
                "redeemed_approval_challenge_id": challenge_id,
                "redeemed_approval_generation": generation,
                "redeemed_approval_token_sha256": token_digest,
                "redeemed_approval_fingerprint": cstr(
                    getattr(reset_doc, "approval_fingerprint", None)
                ).strip(),
                "redeemed_approval_expires_at": getattr(
                    reset_doc,
                    "approval_expires_at",
                    None,
                ),
                "redemption_idempotency_sha256": idempotency_digest,
                "redemption_export_sha256": export_digest,
                "redemption_export_content_sha256": content_digest,
                "redemption_export_byte_length": byte_length,
                "redemption_setup_sha256": setup_sha256,
                "redemption_result_fingerprint": result_fingerprint,
                "redemption_issued_at": redeemed_at,
                "redeemed_at": redeemed_at,
                "last_redeemed_at": redeemed_at,
                "redeemed_recovery_expires_at": recovery_expires_at,
                "redemption_count": 1,
            },
        )
        response = _redemption_response(
            reset_doc,
            setup=setup,
            recovery_path="initial_redemption",
        )
        frappe.db.commit()
        return response
    except frappe.ValidationError:
        rollback_to_savepoint(
            savepoint,
            title="KoPOS safe reset redemption rollback failed",
        )
        raise
    except Exception as error:
        rollback_to_savepoint(
            savepoint,
            title="KoPOS safe reset redemption rollback failed",
        )
        log_sanitized_error("KoPOS safe reset redemption failed", error)
        frappe.throw(
            _("Safe reset redemption failed before credentials were safely issued"),
            frappe.ValidationError,
        )


def _cancellation_result_fingerprint(
    reset_doc: Any,
    *,
    cancellation_idempotency_sha256: str,
    cancellation_reason: str,
    cancellation_origin: str,
    cancelled_by_user: str,
    cancelled_by_api_user: str,
    cancelled_at: Any,
) -> str:
    return _request_fingerprint(
        {
            "safe_reset_protocol_version": cint(
                getattr(reset_doc, "safe_reset_protocol_version", 0)
            ),
            "reset_id": cstr(getattr(reset_doc, "reset_id", None)).strip(),
            "request_id": cstr(getattr(reset_doc, "request_id", None)).strip(),
            "device_id": cstr(getattr(reset_doc, "device_id", None)).strip(),
            "previous_config_version": cint(
                getattr(reset_doc, "previous_config_version", 0)
            ),
            "reset_proof_sha256": cstr(
                getattr(reset_doc, "reset_proof_sha256", None)
            ).strip(),
            "evidence_fingerprint": cstr(
                getattr(reset_doc, "evidence_fingerprint", None)
            ).strip(),
            "request_fingerprint": cstr(
                getattr(reset_doc, "request_fingerprint", None)
            ).strip(),
            "cancellation_idempotency_sha256": cancellation_idempotency_sha256,
            "cancellation_reason": cancellation_reason,
            "cancellation_origin": cancellation_origin,
            "cancelled_by_user": cancelled_by_user,
            "cancelled_by_api_user": cancelled_by_api_user,
            "cancelled_at": _iso_utc(cancelled_at),
        }
    )


def _validate_stored_cancellation_result(reset_doc: Any) -> None:
    expected = _cancellation_result_fingerprint(
        reset_doc,
        cancellation_idempotency_sha256=cstr(
            getattr(reset_doc, "cancellation_idempotency_sha256", None)
        ).strip(),
        cancellation_reason=cstr(
            getattr(reset_doc, "cancellation_reason", None)
        ),
        cancellation_origin=cstr(
            getattr(reset_doc, "cancellation_origin", None)
        ).strip(),
        cancelled_by_user=cstr(
            getattr(reset_doc, "cancelled_by_user", None)
        ).strip(),
        cancelled_by_api_user=cstr(
            getattr(reset_doc, "cancelled_by_api_user", None)
        ).strip(),
        cancelled_at=getattr(reset_doc, "cancelled_at", None),
    )
    if not hmac.compare_digest(
        expected,
        cstr(
            getattr(reset_doc, "cancellation_result_fingerprint", None)
        ).strip(),
    ):
        frappe.throw(
            _("Safe reset cancellation audit result is invalid"),
            frappe.ValidationError,
        )


def _cancellation_response(reset_doc: Any, *, replay: bool) -> dict[str, Any]:
    response = {
        "status": "already_cancelled" if replay else "cancelled",
        "lifecycle_status": "cancelled",
        "device_id": cstr(getattr(reset_doc, "device_id", None)).strip(),
        "cancellation_idempotency_sha256": cstr(
            getattr(reset_doc, "cancellation_idempotency_sha256", None)
        ).strip(),
        "cancellation_reason": cstr(
            getattr(reset_doc, "cancellation_reason", None)
        ),
        "cancellation_origin": cstr(
            getattr(reset_doc, "cancellation_origin", None)
        ).strip(),
        "cancelled_by_user": cstr(
            getattr(reset_doc, "cancelled_by_user", None)
        ).strip(),
        "cancelled_by_api_user": cstr(
            getattr(reset_doc, "cancelled_by_api_user", None)
        ).strip(),
        "cancelled_at": _iso_utc(getattr(reset_doc, "cancelled_at", None)),
        "credentials_rotated": False,
    }
    response.update(_safe_reset_response_tags(reset_doc))
    response["status"] = "already_cancelled" if replay else "cancelled"
    response["lifecycle_status"] = "cancelled"
    return response


def _commit_pre_rotation_cancellation(
    reset_doc: Any,
    *,
    cancellation_digest: str,
    cancellation_reason: str,
    cancellation_origin: str,
    cancelled_by_user: str,
    cancelled_by_api_user: str,
) -> dict[str, Any]:
    """Commit or replay one immutable cancellation while holding device/reset locks."""
    status = cstr(getattr(reset_doc, "status", None)).strip().lower()
    if status == "cancelled":
        if not hmac.compare_digest(
            cancellation_digest,
            cstr(
                getattr(reset_doc, "cancellation_idempotency_sha256", None)
            ).strip(),
        ) or not hmac.compare_digest(
            cancellation_reason,
            cstr(getattr(reset_doc, "cancellation_reason", None)),
        ):
            frappe.throw(
                _(
                    "Safe reset cancellation idempotency key was already used "
                    "differently"
                ),
                frappe.ValidationError,
            )
        _validate_stored_cancellation_result(reset_doc)
        return _cancellation_response(reset_doc, replay=True)
    if status not in {"requested", "authorized"}:
        frappe.throw(
            _("Safe reset can be cancelled only before credential rotation"),
            frappe.ValidationError,
        )
    if cint(getattr(reset_doc, "new_config_version", 0)) > 0 or getattr(
        reset_doc, "credential_rotated_at", None
    ):
        frappe.throw(
            _("Safe reset cancellation is forbidden after credential rotation"),
            frappe.ValidationError,
        )

    cancelled_at = now_datetime()
    result_fingerprint = _cancellation_result_fingerprint(
        reset_doc,
        cancellation_idempotency_sha256=cancellation_digest,
        cancellation_reason=cancellation_reason,
        cancellation_origin=cancellation_origin,
        cancelled_by_user=cancelled_by_user,
        cancelled_by_api_user=cancelled_by_api_user,
        cancelled_at=cancelled_at,
    )
    _apply_reset_transition(
        reset_doc,
        {
            "status": "cancelled",
            "cancellation_idempotency_sha256": cancellation_digest,
            "cancellation_reason": cancellation_reason,
            "cancellation_origin": cancellation_origin,
            "cancelled_by_user": cancelled_by_user,
            "cancelled_by_api_user": cancelled_by_api_user or None,
            "cancelled_at": cancelled_at,
            "cancellation_result_fingerprint": result_fingerprint,
        },
    )
    response = _cancellation_response(reset_doc, replay=False)
    frappe.db.commit()
    return response


def cancel_device_safe_reset(
    *,
    safe_reset_protocol_version: Any,
    confirmation: str | None,
    request_id: str | None,
    reset_id: str | None,
    device_id: str | None,
    reason: str | None,
    idempotency_key: str | None,
    previous_config_version: Any,
    reset_proof_sha256: str | None,
) -> dict[str, Any]:
    """Cancel an authenticated safe reset before any credential rotation."""
    _require_safe_reset_protocol_version(safe_reset_protocol_version)
    reset_id_value = _require_reset_id(reset_id)
    request_id_value = _require_request_id(request_id)
    device_id_value = cstr(device_id).strip()
    cancellation_reason = _require_reason(reason)
    cancellation_key = _require_256_bit_secret(
        idempotency_key,
        "idempotency_key",
    )
    cancellation_digest = _sha256_text(cancellation_key)
    supplied_version = _require_positive_int(
        previous_config_version,
        "previous_config_version",
    )
    supplied_proof_digest = _require_sha256(
        reset_proof_sha256,
        "reset_proof_sha256",
    )
    expected_confirmation = f"{CANCEL_CONFIRMATION_PREFIX} {reset_id_value}"
    if not hmac.compare_digest(cstr(confirmation), expected_confirmation):
        frappe.throw(
            _("Safe reset cancellation confirmation is invalid"),
            frappe.ValidationError,
        )

    device_doc = require_device_context(device_id=device_id_value)
    if device_doc is None:
        frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)
        raise ValueError("KoPOS Device is required")
    device_name = cstr(getattr(device_doc, "name", None)).strip()
    reset_doc = _get_reset_with_device_lock(
        reset_id_value,
        expected_device_name=device_name,
    )
    device_doc = get_device_doc(name=device_name)
    _validate_reset_device_binding(reset_doc, device_doc)
    _require_safe_reset_protocol_version(
        getattr(reset_doc, "safe_reset_protocol_version", None)
    )

    session_user = cstr(getattr(frappe.session, "user", None)).strip()
    if not hmac.compare_digest(
        session_user,
        cstr(getattr(reset_doc, "api_user", None)).strip(),
    ):
        frappe.throw(
            _("Safe reset must be cancelled by its authenticated device API user"),
            frappe.ValidationError,
        )
    if not hmac.compare_digest(
        request_id_value,
        cstr(getattr(reset_doc, "request_id", None)).strip(),
    ) or not hmac.compare_digest(
        supplied_proof_digest,
        cstr(getattr(reset_doc, "reset_proof_sha256", None)).strip(),
    ) or supplied_version != cint(
        getattr(reset_doc, "previous_config_version", 0)
    ):
        frappe.throw(
            _("Safe reset cancellation request identity does not match the audit"),
            frappe.ValidationError,
        )

    cancellation_status = cstr(
        getattr(reset_doc, "status", None)
    ).strip().lower()
    if cancellation_status == "cancelled":
        return _commit_pre_rotation_cancellation(
            reset_doc,
            cancellation_digest=cancellation_digest,
            cancellation_reason=cancellation_reason,
            cancellation_origin=CANCELLATION_ORIGIN_DEVICE,
            cancelled_by_user=session_user,
            cancelled_by_api_user=session_user,
        )
    if cancellation_status not in {"requested", "authorized"}:
        frappe.throw(
            _("Safe reset can be cancelled only before credential rotation"),
            frappe.ValidationError,
        )

    if cint(getattr(device_doc, "config_version", 0)) != supplied_version or cint(
        getattr(reset_doc, "new_config_version", 0)
    ) > 0 or getattr(reset_doc, "credential_rotated_at", None):
        frappe.throw(
            _("Safe reset cancellation is forbidden after credential rotation"),
            frappe.ValidationError,
        )

    return _commit_pre_rotation_cancellation(
        reset_doc,
        cancellation_digest=cancellation_digest,
        cancellation_reason=cancellation_reason,
        cancellation_origin=CANCELLATION_ORIGIN_DEVICE,
        cancelled_by_user=session_user,
        cancelled_by_api_user=session_user,
    )


def cancel_device_safe_reset_as_system_manager(
    *,
    safe_reset_protocol_version: Any,
    confirmation: str | None,
    reset_id: str | None,
    reason: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Abandon a pre-rotation reset when the old tablet credential is unavailable."""
    require_system_manager()
    _require_safe_reset_protocol_version(safe_reset_protocol_version)
    reset_id_value = _require_reset_id(reset_id)
    expected_confirmation = f"{CANCEL_CONFIRMATION_PREFIX} {reset_id_value}"
    if not hmac.compare_digest(cstr(confirmation), expected_confirmation):
        frappe.throw(
            _("Safe reset cancellation confirmation is invalid"),
            frappe.ValidationError,
        )
    cancellation_reason = _require_reason(reason)
    cancellation_key = _require_256_bit_secret(
        idempotency_key,
        "idempotency_key",
    )
    cancellation_digest = _sha256_text(cancellation_key)
    reset_doc = _get_reset_with_device_lock(reset_id_value)
    _require_safe_reset_protocol_version(
        getattr(reset_doc, "safe_reset_protocol_version", None)
    )
    manager_user = cstr(getattr(frappe.session, "user", None)).strip()
    if not manager_user or manager_user == "Guest":
        frappe.throw(_("Authentication required"), frappe.ValidationError)
    return _commit_pre_rotation_cancellation(
        reset_doc,
        cancellation_digest=cancellation_digest,
        cancellation_reason=cancellation_reason,
        cancellation_origin=CANCELLATION_ORIGIN_SYSTEM_MANAGER,
        cancelled_by_user=manager_user,
        cancelled_by_api_user="",
    )


def complete_device_safe_reset(
    *,
    safe_reset_protocol_version: Any,
    device_id: str | None,
    reset_id: str | None,
    new_config_version: Any,
    export_sha256: str | None,
    export_content_sha256: str | None,
    export_byte_length: Any,
    completion_idempotency_key: str | None,
) -> dict[str, Any]:
    """Record activation after a second trusted-app retained-archive rehash."""
    protocol_version = _require_safe_reset_protocol_version(
        safe_reset_protocol_version
    )
    device_id_value = cstr(device_id).strip()
    device_doc = require_device_context(device_id=device_id_value)
    if device_doc is None:
        frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)
        raise ValueError("KoPOS Device is required")
    device_name = cstr(getattr(device_doc, "name", None)).strip()
    reset_doc = _get_reset_with_device_lock(
        _require_reset_id(reset_id),
        expected_device_name=device_name,
    )
    device_doc = get_device_doc(name=device_name)
    _validate_reset_device_binding(reset_doc, device_doc)
    _require_safe_reset_protocol_version(
        getattr(reset_doc, "safe_reset_protocol_version", None)
    )

    expected_version = cint(getattr(reset_doc, "new_config_version", 0))
    supplied_version = _require_positive_int(
        new_config_version,
        "new_config_version",
    )
    if supplied_version != expected_version or cint(
        getattr(device_doc, "config_version", 0)
    ) != expected_version:
        frappe.throw(
            _("Safe reset completion config version does not match ERP authority"),
            frappe.ValidationError,
        )
    completion_export_sha256 = _require_sha256(export_sha256, "export_sha256")
    completion_content_sha256 = _require_sha256(
        export_content_sha256,
        "export_content_sha256",
    )
    completion_byte_length = _require_archive_byte_length(export_byte_length)
    _validate_archive_evidence_matches(
        reset_doc,
        export_sha256=completion_export_sha256,
        export_content_sha256=completion_content_sha256,
        export_byte_length=completion_byte_length,
        context="completion",
    )
    completion_key = _require_256_bit_secret(
        completion_idempotency_key,
        "completion_idempotency_key",
    )
    completion_digest = _sha256_text(completion_key)
    redemption_digest = _require_sha256(
        cstr(getattr(reset_doc, "redemption_idempotency_sha256", None)),
        "redemption_idempotency_sha256",
    )
    if hmac.compare_digest(completion_digest, redemption_digest):
        frappe.throw(
            _(
                "Safe reset completion requires a distinct idempotency key from "
                "redemption"
            ),
            frappe.ValidationError,
        )

    session_user = cstr(getattr(frappe.session, "user", None)).strip()
    if not hmac.compare_digest(
        session_user,
        cstr(getattr(reset_doc, "api_user", None)).strip(),
    ):
        frappe.throw(
            _("Safe reset must be completed by the rotated device API user"),
            frappe.ValidationError,
        )
    status = cstr(getattr(reset_doc, "status", None)).strip().lower()
    if status == "completed":
        if not hmac.compare_digest(
            cstr(
                getattr(reset_doc, "completion_idempotency_sha256", None)
            ).strip(),
            completion_digest,
        ):
            frappe.throw(
                _("Safe reset completion idempotency key was already used differently"),
                frappe.ValidationError,
            )
        _validate_stored_completion_result(reset_doc)
        return _completion_response(reset_doc)
    if status != "redeemed":
        frappe.throw(
            _("Safe reset must be redeemed before completion"),
            frappe.ValidationError,
        )

    completed_at = now_datetime()
    result_fingerprint = _request_fingerprint(
        {
            "safe_reset_protocol_version": protocol_version,
            "reset_id": cstr(getattr(reset_doc, "reset_id", None)).strip(),
            "device_id": device_id_value,
            "new_config_version": expected_version,
            "export_sha256": completion_export_sha256,
            "export_content_sha256": completion_content_sha256,
            "export_byte_length": completion_byte_length,
            "completion_idempotency_sha256": completion_digest,
            "completed_by_api_user": session_user,
            "completed_at": _iso_utc(completed_at),
        }
    )
    _apply_reset_transition(
        reset_doc,
        {
            "status": "completed",
            "completion_idempotency_sha256": completion_digest,
            "completion_export_sha256": completion_export_sha256,
            "completion_export_content_sha256": completion_content_sha256,
            "completion_export_byte_length": completion_byte_length,
            "completion_result_fingerprint": result_fingerprint,
            "completed_by_api_user": session_user,
            "completed_at": completed_at,
        },
    )
    response = _completion_response(reset_doc)
    frappe.db.commit()
    return response


def _normalize_queue_evidence(
    value: Mapping[str, Any] | str | None,
) -> dict[str, int]:
    parsed: object = value
    if isinstance(value, str):
        try:
            parsed = frappe.parse_json(value)
        except Exception as error:
            raise frappe.ValidationError("queue_evidence must be valid JSON") from error
    if not isinstance(parsed, Mapping):
        frappe.throw(_("queue_evidence must be an object"), frappe.ValidationError)
        raise ValueError("queue_evidence must be an object")
    unexpected = sorted(set(str(key) for key in parsed.keys()) - set(QUEUE_EVIDENCE_KEYS))
    if unexpected:
        frappe.throw(
            _("queue_evidence contains unsupported fields"),
            frappe.ValidationError,
        )
    return {
        key: _require_nonnegative_int(parsed.get(key), f"queue_evidence.{key}")
        for key in QUEUE_EVIDENCE_KEYS
    }


def _normalize_migration_recovery_evidence(
    *,
    migration_recovery_point_count: Any,
    migration_recovery_valid_point_count: Any,
    migration_recovery_invalid_point_count: Any,
    migration_recovery_captured_pending_total: Any,
    migration_recovery_review_required: Any,
) -> dict[str, int | bool]:
    normalized: dict[str, int | bool] = {
        fieldname: _require_bounded_migration_recovery_count(value, fieldname)
        for fieldname, value in (
            (
                "migration_recovery_point_count",
                migration_recovery_point_count,
            ),
            (
                "migration_recovery_valid_point_count",
                migration_recovery_valid_point_count,
            ),
            (
                "migration_recovery_invalid_point_count",
                migration_recovery_invalid_point_count,
            ),
            (
                "migration_recovery_captured_pending_total",
                migration_recovery_captured_pending_total,
            ),
        )
    }
    normalized["migration_recovery_review_required"] = (
        _require_migration_recovery_review_required(
            migration_recovery_review_required
        )
    )
    point_count = int(normalized["migration_recovery_point_count"])
    valid_point_count = int(normalized["migration_recovery_valid_point_count"])
    invalid_point_count = int(
        normalized["migration_recovery_invalid_point_count"]
    )
    captured_pending_total = int(
        normalized["migration_recovery_captured_pending_total"]
    )
    review_required = bool(normalized["migration_recovery_review_required"])
    if valid_point_count + invalid_point_count != point_count:
        frappe.throw(
            _(
                "Migration recovery valid and invalid point counts must equal "
                "migration_recovery_point_count"
            ),
            frappe.ValidationError,
        )
    if review_required != (point_count > 0):
        frappe.throw(
            _(
                "migration_recovery_review_required must equal whether recovery "
                "points exist"
            ),
            frappe.ValidationError,
        )
    if point_count == 0 and captured_pending_total != 0:
        frappe.throw(
            _(
                "migration_recovery_captured_pending_total must be zero when "
                "no migration recovery points exist"
            ),
            frappe.ValidationError,
        )
    return normalized


def _normalize_strict_migration_recovery_evidence(
    *,
    migration_recovery_point_count: Any,
    migration_recovery_valid_point_count: Any,
    migration_recovery_invalid_point_count: Any,
    migration_recovery_captured_pending_total: Any,
    migration_recovery_review_required: Any,
) -> dict[str, int | bool]:
    """Validate evidence that has already crossed a typed JSON boundary."""
    count_values = (
        (
            "migration_recovery_point_count",
            migration_recovery_point_count,
        ),
        (
            "migration_recovery_valid_point_count",
            migration_recovery_valid_point_count,
        ),
        (
            "migration_recovery_invalid_point_count",
            migration_recovery_invalid_point_count,
        ),
        (
            "migration_recovery_captured_pending_total",
            migration_recovery_captured_pending_total,
        ),
    )
    for fieldname, value in count_values:
        if isinstance(value, bool) or not isinstance(value, int):
            frappe.throw(
                _("{0} must be a JSON integer").format(fieldname),
                frappe.ValidationError,
            )
        if value < 0 or value > MAX_MIGRATION_RECOVERY_COUNT:
            frappe.throw(
                _("{0} exceeds the supported count range").format(fieldname),
                frappe.ValidationError,
            )
    if not isinstance(migration_recovery_review_required, bool):
        frappe.throw(
            _("migration_recovery_review_required must be a JSON boolean"),
            frappe.ValidationError,
        )
    return _normalize_migration_recovery_evidence(
        migration_recovery_point_count=migration_recovery_point_count,
        migration_recovery_valid_point_count=migration_recovery_valid_point_count,
        migration_recovery_invalid_point_count=migration_recovery_invalid_point_count,
        migration_recovery_captured_pending_total=(
            migration_recovery_captured_pending_total
        ),
        migration_recovery_review_required=migration_recovery_review_required,
    )


def _require_bounded_migration_recovery_count(value: Any, fieldname: str) -> int:
    parsed = _require_nonnegative_int(value, fieldname)
    if parsed > MAX_MIGRATION_RECOVERY_COUNT:
        frappe.throw(
            _("{0} exceeds the supported count range").format(fieldname),
            frappe.ValidationError,
        )
    return parsed


def _require_migration_recovery_review_required(value: Any) -> bool:
    fieldname = "migration_recovery_review_required"
    if value is None or (isinstance(value, str) and not value.strip()):
        frappe.throw(_("{0} is required").format(fieldname), frappe.ValidationError)
    return _require_explicit_bool(value, fieldname)


def _validate_export_timestamp(
    value: Any,
    *,
    allow_stale_export: Any,
    stale_export_override_reason: str | None,
    credential_recovery: bool,
) -> tuple[datetime, bool, str]:
    if value is None or not cstr(value).strip():
        frappe.throw(_("exported_at is required"), frappe.ValidationError)
    try:
        parsed = frappe.utils.get_datetime(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise frappe.ValidationError("exported_at must be a valid datetime") from error
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        frappe.throw(
            _("exported_at must include an explicit timezone offset"),
            frappe.ValidationError,
        )
    try:
        exported_at = normalize_site_datetime(parsed, fieldname="exported_at")
    except ValueError as error:
        raise frappe.ValidationError(str(error)) from error

    override_requested = _require_explicit_bool(
        allow_stale_export,
        "allow_stale_export",
    )
    override_reason = cstr(stale_export_override_reason).strip()
    if override_requested:
        if not credential_recovery:
            frappe.throw(
                _("Only credential recovery may override stale export evidence"),
                frappe.ValidationError,
            )
        if len(override_reason) < 20 or len(override_reason) > 500:
            frappe.throw(
                _(
                    "stale_export_override_reason must contain 20-500 characters"
                ),
                frappe.ValidationError,
            )
        return exported_at, True, override_reason
    if override_reason:
        frappe.throw(
            _("Stale export override reason requires allow_stale_export=true"),
            frappe.ValidationError,
        )
    return exported_at, False, ""


def _enforce_export_timestamp_freshness(
    exported_at: datetime,
    *,
    stale_override: bool,
    stale_override_reason: str,
    credential_recovery: bool,
) -> None:
    current_time = normalize_site_datetime(
        now_datetime(),
        fieldname="current site datetime",
    )
    if exported_at > current_time + timedelta(
        seconds=EXPORT_EVIDENCE_MAX_FUTURE_SKEW_SECONDS
    ):
        frappe.throw(
            _("exported_at cannot be more than 5 minutes in the future"),
            frappe.ValidationError,
        )
    is_stale = exported_at < current_time - timedelta(
        seconds=EXPORT_EVIDENCE_MAX_AGE_SECONDS
    )
    if is_stale and not (
        credential_recovery and stale_override and stale_override_reason
    ):
        frappe.throw(
            _(
                "exported_at is older than the 30-minute safe-reset evidence window"
            ),
            frappe.ValidationError,
        )
    if not is_stale and stale_override:
        frappe.throw(
            _("Stale export override is only valid for stale recovery evidence"),
            frappe.ValidationError,
        )


def _require_explicit_bool(value: Any, fieldname: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in {0, 1}:
            return bool(value)
    elif isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"", "false", "0"}:
            return False
    elif value is None:
        return False
    frappe.throw(_("{0} must be true or false").format(fieldname), frappe.ValidationError)
    raise ValueError(f"{fieldname} must be true or false")


def _validate_reset_scope(
    device_doc: Any,
    *,
    erp_base_url: str | None,
    company: str | None,
    currency: str | None,
    pos_profile: str | None,
    warehouse: str | None,
) -> dict[str, str]:
    if device_doc is None or not cint(getattr(device_doc, "enabled", 0)):
        frappe.throw(
            _("Safe reset requires an enabled registered KoPOS Device"),
            frappe.ValidationError,
        )

    submitted_profile = cstr(pos_profile).strip()
    device_profile = cstr(getattr(device_doc, "pos_profile", None)).strip()
    if not submitted_profile or not hmac.compare_digest(
        submitted_profile, device_profile
    ):
        frappe.throw(
            _("Safe reset POS Profile does not match ERP device scope"),
            frappe.ValidationError,
        )
    profile_doc = frappe.get_cached_doc("POS Profile", device_profile)
    profile_company = cstr(getattr(profile_doc, "company", None)).strip()
    profile_warehouse = cstr(getattr(profile_doc, "warehouse", None)).strip()
    profile_currency = cstr(getattr(profile_doc, "currency", None)).strip()
    if not profile_currency and profile_company:
        profile_currency = cstr(
            frappe.db.get_value("Company", profile_company, "default_currency")
        ).strip()

    submitted_company = cstr(company).strip()
    submitted_currency = cstr(currency).strip()
    submitted_warehouse = cstr(warehouse).strip()
    if not submitted_company or not hmac.compare_digest(
        submitted_company, profile_company
    ):
        frappe.throw(
            _("Safe reset company does not match ERP device scope"),
            frappe.ValidationError,
        )
    if not submitted_currency or not hmac.compare_digest(
        submitted_currency, profile_currency
    ):
        frappe.throw(
            _("Safe reset currency does not match ERP device scope"),
            frappe.ValidationError,
        )
    if not submitted_warehouse or not hmac.compare_digest(
        submitted_warehouse, profile_warehouse
    ):
        frappe.throw(
            _("Safe reset warehouse does not match ERP device scope"),
            frappe.ValidationError,
        )

    submitted_base_url = _normalize_erp_base_url(erp_base_url, "erp_base_url")
    current_base_url = _normalize_erp_base_url(
        frappe.utils.get_url(),
        "ERP server base URL",
    )
    if not hmac.compare_digest(submitted_base_url, current_base_url):
        frappe.throw(
            _("Safe reset ERP base URL does not match this ERP site"),
            frappe.ValidationError,
        )
    return {
        "erp_base_url": submitted_base_url,
        "company": profile_company,
        "currency": profile_currency,
        "pos_profile": device_profile,
        "warehouse": profile_warehouse,
    }


def _matching_provisioning_erp_base_url(
    requested_value: str | None,
    audited_value: str,
) -> str:
    audited_base_url = _normalize_erp_base_url(audited_value, "audited ERP base URL")
    requested_base_url = (
        _normalize_erp_base_url(requested_value, "erpnext_url")
        if cstr(requested_value).strip()
        else audited_base_url
    )
    if not hmac.compare_digest(requested_base_url, audited_base_url):
        frappe.throw(
            _("Safe reset provisioning URL does not match audited ERP scope"),
            frappe.ValidationError,
        )
    return audited_base_url


def _normalize_erp_base_url(value: str | None, fieldname: str) -> str:
    raw_value = cstr(value).strip()
    if not raw_value or any(character.isspace() for character in raw_value):
        frappe.throw(_("{0} is invalid").format(fieldname), frappe.ValidationError)
    try:
        parsed = urlsplit(raw_value)
        port = parsed.port
    except ValueError:
        frappe.throw(_("{0} is invalid").format(fieldname), frappe.ValidationError)
        raise
    scheme = parsed.scheme.lower()
    hostname = cstr(parsed.hostname).strip().lower()
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        frappe.throw(_("{0} is invalid").format(fieldname), frappe.ValidationError)

    decoded_segments = unquote(parsed.path).split("/")
    if (
        "\\" in parsed.path
        or "//" in parsed.path
        or any(segment in {".", ".."} for segment in decoded_segments)
    ):
        frappe.throw(_("{0} has an unsafe path").format(fieldname), frappe.ValidationError)
    path = parsed.path.rstrip("/")
    normalized_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    authority = normalized_host if port is None or default_port else f"{normalized_host}:{port}"
    return f"{scheme}://{authority}{path}"


def _request_fingerprint(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return _sha256_text(canonical)


def _normalize_request_id(value: str | None) -> tuple[str, str]:
    supplied = cstr(value).strip()
    if supplied:
        if not REQUEST_ID_PATTERN.fullmatch(supplied):
            frappe.throw(
                _("request_id must be 8-128 URL-safe characters"),
                frappe.ValidationError,
            )
        return supplied, supplied
    return f"KSRQ-{cstr(frappe.generate_hash(length=24)).strip()}", ""


def _require_reason(value: str | None) -> str:
    reason = cstr(value).strip()
    if not reason:
        frappe.throw(_("Safe reset reason is required"), frappe.ValidationError)
    if len(reason) > 500:
        frappe.throw(
            _("Safe reset reason must not exceed 500 characters"),
            frappe.ValidationError,
        )
    return reason


def _require_sha256(value: str | None, fieldname: str) -> str:
    digest = cstr(value).strip().lower()
    if not SHA256_PATTERN.fullmatch(digest):
        frappe.throw(
            _("{0} must be a SHA-256 digest").format(fieldname),
            frappe.ValidationError,
        )
    return digest


def _require_safe_reset_protocol_version(value: Any) -> int:
    version = _require_nonnegative_int(value, "safe_reset_protocol_version")
    if version != SAFE_RESET_PROTOCOL_VERSION:
        frappe.throw(
            _(
                "Safe reset protocol version 2 is required; legacy or mixed-version "
                "safe reset requests are rejected"
            ),
            frappe.ValidationError,
        )
    return version


def _require_archive_byte_length(value: Any) -> int:
    byte_length = _require_positive_int(value, "export_byte_length")
    if byte_length > MAX_SUPPORT_ARCHIVE_BYTES:
        frappe.throw(
            _("export_byte_length exceeds the supported support-archive size"),
            frappe.ValidationError,
        )
    return byte_length


def _require_approval_generation(value: Any) -> int:
    return _require_positive_int(value, "approval_generation")


def _require_challenge_id(value: str | None) -> str:
    challenge_id = cstr(value)
    if challenge_id != challenge_id.strip() or not CHALLENGE_ID_PATTERN.fullmatch(
        challenge_id
    ):
        frappe.throw(_("Safe reset approval challenge is invalid"), frappe.ValidationError)
    return challenge_id


def _require_256_bit_secret(value: str | None, fieldname: str) -> str:
    secret = cstr(value)
    if secret != secret.strip() or not SECRET_256_PATTERN.fullmatch(secret):
        frappe.throw(
            _("{0} must be a 256-bit base64url secret").format(fieldname),
            frappe.ValidationError,
        )
    try:
        decoded = urlsafe_b64decode(f"{secret}=")
    except (TypeError, ValueError) as error:
        raise frappe.ValidationError(
            f"{fieldname} must be a 256-bit base64url secret"
        ) from error
    if len(decoded) != 32:
        frappe.throw(
            _("{0} must be a 256-bit base64url secret").format(fieldname),
            frappe.ValidationError,
        )
    return secret


def _require_reset_proof_nonce(value: str | None) -> str:
    nonce = cstr(value)
    if nonce != nonce.strip() or not RESET_PROOF_NONCE_PATTERN.fullmatch(nonce):
        frappe.throw(
            _("Safe reset proof nonce is invalid"),
            frappe.ValidationError,
        )
    return nonce


def _require_nonnegative_int(value: Any, fieldname: str) -> int:
    if isinstance(value, bool):
        frappe.throw(_("{0} must be an integer").format(fieldname), frappe.ValidationError)
    text = cstr(value).strip()
    if not re.fullmatch(r"[0-9]+", text):
        frappe.throw(
            _("{0} must be a non-negative integer").format(fieldname),
            frappe.ValidationError,
        )
    return int(text)


def _require_positive_int(value: Any, fieldname: str) -> int:
    parsed = _require_nonnegative_int(value, fieldname)
    if parsed <= 0:
        frappe.throw(
            _("{0} must be a positive integer").format(fieldname),
            frappe.ValidationError,
        )
    return parsed


def _safe_reset_approval_ttl(value: Any) -> int:
    if value is None or not cstr(value).strip():
        return DEFAULT_APPROVAL_TTL_SECONDS
    requested = _require_positive_int(value, "expires_in_seconds")
    return max(
        MIN_APPROVAL_TTL_SECONDS,
        min(MAX_APPROVAL_TTL_SECONDS, requested),
    )


def _require_reset_id(value: str | None) -> str:
    reset_id = cstr(value).strip()
    if not re.fullmatch(r"KSR-[A-Za-z0-9._:-]{8,128}", reset_id):
        frappe.throw(_("Valid safe reset_id is required"), frappe.ValidationError)
    return reset_id


def _lock_device_for_update(device_name: str) -> None:
    if not device_name:
        frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)
    rows = frappe.db.sql(
        "SELECT name FROM `tabKoPOS Device` WHERE name = %s FOR UPDATE",
        (device_name,),
    )
    if rows == [] and not frappe.db.exists("KoPOS Device", device_name):
        frappe.throw(_("KoPOS Device was not found"), frappe.ValidationError)


def _get_reset_with_device_lock(
    reset_id: str,
    *,
    expected_device_name: str | None = None,
) -> Any:
    """Lock the device before its safe-reset row and revalidate the binding."""
    device_name = cstr(expected_device_name).strip()
    if not device_name:
        device_name = cstr(
            frappe.db.get_value(SAFE_RESET_DOCTYPE, reset_id, "device")
        ).strip()
        if not device_name:
            frappe.throw(
                _("KoPOS device safe reset was not found"),
                frappe.ValidationError,
            )

    _lock_device_for_update(device_name)
    reset_doc = _get_reset_for_update(reset_id)
    locked_device_name = cstr(getattr(reset_doc, "device", None)).strip()
    if not locked_device_name or not hmac.compare_digest(
        locked_device_name,
        device_name,
    ):
        frappe.throw(
            _(
                "Safe reset device binding changed while transaction locks were acquired"
            ),
            frappe.ValidationError,
        )
    return reset_doc


def _get_reset_for_update(reset_id: str):
    """Lock a reset row after its device row is already locked."""
    rows = frappe.db.sql(
        "SELECT name FROM `tabKoPOS Device Safe Reset` WHERE name = %s FOR UPDATE",
        (reset_id,),
        as_dict=True,
    )
    if not rows:
        frappe.throw(_("KoPOS device safe reset was not found"), frappe.ValidationError)
    return frappe.get_doc(SAFE_RESET_DOCTYPE, reset_id)


def _find_matching_reset_for_update(
    *,
    device_id: str,
    request_id: str | None,
    reset_proof_sha256: str,
):
    request_clause = "request_id = %s OR " if request_id else ""
    params: list[Any] = [device_id]
    if request_id:
        params.append(request_id)
    params.append(reset_proof_sha256)
    rows = frappe.db.sql(
        f"""
        SELECT name
        FROM `tabKoPOS Device Safe Reset`
        WHERE device_id = %s
          AND ({request_clause}reset_proof_sha256 = %s)
        ORDER BY creation DESC, name DESC
        LIMIT 1
        FOR UPDATE
        """,
        tuple(params),
        as_dict=True,
    )
    if not rows:
        return None
    return frappe.get_doc(SAFE_RESET_DOCTYPE, _row_value(rows[0], "name"))


def _find_cancellation_idempotency_for_update(
    *,
    device_id: str,
    cancellation_idempotency_sha256: str,
):
    rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabKoPOS Device Safe Reset`
        WHERE device_id = %s
          AND cancellation_idempotency_sha256 = %s
        ORDER BY creation DESC, name DESC
        LIMIT 1
        FOR UPDATE
        """,
        (device_id, cancellation_idempotency_sha256),
        as_dict=True,
    )
    if not rows:
        return None
    return frappe.get_doc(SAFE_RESET_DOCTYPE, _row_value(rows[0], "name"))


def _find_active_reset_for_update(device_id: str):
    rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabKoPOS Device Safe Reset`
        WHERE device_id = %s
          AND (
            status IS NULL
            OR status NOT IN ('completed', 'cancelled', 'expired')
          )
        ORDER BY creation DESC, name DESC
        LIMIT 1
        FOR UPDATE
        """,
        (device_id,),
        as_dict=True,
    )
    if not rows:
        return None
    return frappe.get_doc(SAFE_RESET_DOCTYPE, _row_value(rows[0], "name"))


def _expire_active_reset_if_needed(reset_doc: Any) -> bool:
    if cstr(getattr(reset_doc, "status", None)).strip().lower() != "requested":
        return False
    if not _is_expired(getattr(reset_doc, "request_expires_at", None)):
        return False
    _apply_reset_transition(reset_doc, {"status": "expired"})
    return True


def _request_ack(reset_doc: Any) -> dict[str, Any]:
    return {
        "status": "requested",
        "safe_reset_protocol_version": cint(
            getattr(reset_doc, "safe_reset_protocol_version", 0)
        ),
        "lifecycle_status": cstr(getattr(reset_doc, "status", None)).strip().lower(),
        "reset_id": cstr(getattr(reset_doc, "reset_id", None)).strip()
        or cstr(getattr(reset_doc, "name", None)).strip(),
        "request_id": cstr(getattr(reset_doc, "request_id", None)).strip(),
        "request_origin": cstr(
            getattr(reset_doc, "request_origin", None)
        ).strip(),
        "device_id": cstr(getattr(reset_doc, "device_id", None)).strip(),
        "previous_config_version": cint(
            getattr(reset_doc, "previous_config_version", 0)
        ),
        "export_sha256": cstr(getattr(reset_doc, "export_sha256", None)).strip(),
        "export_content_sha256": cstr(
            getattr(reset_doc, "export_content_sha256", None)
        ).strip(),
        "export_byte_length": cint(
            getattr(reset_doc, "export_byte_length", 0)
        ),
        "drained_row_count": cint(
            getattr(reset_doc, "drained_row_count", 0)
        ),
        "queue_evidence": {
            "pending_count": cint(
                getattr(reset_doc, "queue_pending_count", 0)
            ),
            "failed_count": cint(
                getattr(reset_doc, "queue_failed_count", 0)
            ),
            "syncing_count": cint(
                getattr(reset_doc, "queue_syncing_count", 0)
            ),
            "dead_letter_count": cint(
                getattr(reset_doc, "queue_dead_letter_count", 0)
            ),
        },
        **_migration_recovery_evidence_from_doc(reset_doc),
        "reset_proof_sha256": cstr(
            getattr(reset_doc, "reset_proof_sha256", None)
        ).strip(),
        "exported_at": _iso_utc(getattr(reset_doc, "exported_at", None)),
        "erp_base_url": cstr(
            getattr(reset_doc, "erp_base_url", None)
        ).strip(),
        "company": cstr(getattr(reset_doc, "company", None)).strip(),
        "currency": cstr(getattr(reset_doc, "currency", None)).strip(),
        "pos_profile": cstr(getattr(reset_doc, "pos_profile", None)).strip(),
        "warehouse": cstr(getattr(reset_doc, "warehouse", None)).strip(),
        "stale_export_override": bool(
            cint(getattr(reset_doc, "stale_export_override", 0))
        ),
        "requested_at": _iso_utc(getattr(reset_doc, "requested_at", None)),
        "request_expires_at": _iso_utc(
            getattr(reset_doc, "request_expires_at", None)
        ),
    }


def _validate_resolved_request_identity(
    reset_doc: Any,
    *,
    protocol_version: int,
    request_id: str,
    device_id: str,
    reset_proof_sha256: str,
    export_sha256: str,
    export_content_sha256: str,
    export_byte_length: int,
    previous_config_version: int,
) -> None:
    stored_protocol_version = _require_safe_reset_protocol_version(
        getattr(reset_doc, "safe_reset_protocol_version", None)
    )
    string_bindings = (
        (
            cstr(getattr(reset_doc, "request_id", None)).strip(),
            request_id,
        ),
        (
            cstr(getattr(reset_doc, "device_id", None)).strip(),
            device_id,
        ),
        (
            cstr(getattr(reset_doc, "reset_proof_sha256", None)).strip(),
            reset_proof_sha256,
        ),
        (
            cstr(getattr(reset_doc, "export_sha256", None)).strip(),
            export_sha256,
        ),
        (
            cstr(getattr(reset_doc, "export_content_sha256", None)).strip(),
            export_content_sha256,
        ),
    )
    if (
        stored_protocol_version != protocol_version
        or any(
            not hmac.compare_digest(stored_value, submitted_value)
            for stored_value, submitted_value in string_bindings
        )
        or cint(getattr(reset_doc, "export_byte_length", 0))
        != export_byte_length
        or cint(getattr(reset_doc, "previous_config_version", 0))
        != previous_config_version
    ):
        frappe.throw(
            _(
                "Safe reset resolution identity or retained-archive evidence "
                "does not match"
            ),
            frappe.ValidationError,
        )

    request_origin = cstr(
        getattr(reset_doc, "request_origin", None)
    ).strip()
    audited_api_user = cstr(getattr(reset_doc, "api_user", None)).strip()
    requested_by_api_user = cstr(
        getattr(reset_doc, "requested_by_api_user", None)
    ).strip()
    if (
        not hmac.compare_digest(request_origin, REQUEST_ORIGIN_DEVICE)
        or not audited_api_user
        or not hmac.compare_digest(requested_by_api_user, audited_api_user)
    ):
        frappe.throw(
            _(
                "Safe reset resolution requires an authenticated-device request "
                "origin"
            ),
            frappe.ValidationError,
        )


def _validate_existing_prepared_request_evidence(
    reset_doc: Any,
    prepared: Mapping[str, Any],
    *,
    device_id: str,
    api_user: str,
) -> None:
    """Validate every submitted immutable field before accepting an existing row."""
    try:
        stored_exported_at = normalize_site_datetime(
            getattr(reset_doc, "exported_at", None),
            fieldname="stored safe reset exported_at",
        ).isoformat()
    except ValueError as error:
        raise frappe.ValidationError(str(error)) from error

    expected_scope = prepared["scope"]
    string_bindings = (
        (cstr(getattr(reset_doc, "request_id", None)), prepared["request_id"]),
        (cstr(getattr(reset_doc, "device_id", None)).strip(), device_id),
        (cstr(getattr(reset_doc, "api_user", None)).strip(), api_user),
        (cstr(getattr(reset_doc, "reason", None)), prepared["reason"]),
        (
            cstr(getattr(reset_doc, "request_origin", None)).strip(),
            REQUEST_ORIGIN_DEVICE,
        ),
        (
            cstr(getattr(reset_doc, "requested_by_api_user", None)).strip(),
            api_user,
        ),
        (
            cstr(getattr(reset_doc, "export_sha256", None)).strip(),
            prepared["export_sha256"],
        ),
        (
            cstr(getattr(reset_doc, "export_content_sha256", None)).strip(),
            prepared["export_content_sha256"],
        ),
        (
            cstr(getattr(reset_doc, "reset_proof_sha256", None)).strip(),
            prepared["reset_proof_sha256"],
        ),
        (
            cstr(getattr(reset_doc, "evidence_fingerprint", None)).strip(),
            prepared["evidence_fingerprint"],
        ),
        (
            cstr(getattr(reset_doc, "request_fingerprint", None)).strip(),
            prepared["request_fingerprint"],
        ),
        (
            cstr(getattr(reset_doc, "erp_base_url", None)).strip(),
            expected_scope["erp_base_url"],
        ),
        (
            cstr(getattr(reset_doc, "company", None)).strip(),
            expected_scope["company"],
        ),
        (
            cstr(getattr(reset_doc, "currency", None)).strip(),
            expected_scope["currency"],
        ),
        (
            cstr(getattr(reset_doc, "pos_profile", None)).strip(),
            expected_scope["pos_profile"],
        ),
        (
            cstr(getattr(reset_doc, "warehouse", None)).strip(),
            expected_scope["warehouse"],
        ),
        (
            cstr(getattr(reset_doc, "stale_export_override_reason", None)),
            prepared["stale_export_override_reason"],
        ),
        (stored_exported_at, prepared["exported_at"].isoformat()),
    )
    stored_queue_evidence = {
        "pending_count": cint(getattr(reset_doc, "queue_pending_count", 0)),
        "failed_count": cint(getattr(reset_doc, "queue_failed_count", 0)),
        "syncing_count": cint(getattr(reset_doc, "queue_syncing_count", 0)),
        "dead_letter_count": cint(
            getattr(reset_doc, "queue_dead_letter_count", 0)
        ),
    }
    if (
        any(
            not hmac.compare_digest(stored_value, expected_value)
            for stored_value, expected_value in string_bindings
        )
        or cint(getattr(reset_doc, "safe_reset_protocol_version", 0))
        != prepared["protocol_version"]
        or cint(getattr(reset_doc, "export_byte_length", 0))
        != prepared["export_byte_length"]
        or cint(getattr(reset_doc, "drained_row_count", 0))
        != prepared["drained_row_count"]
        or cint(getattr(reset_doc, "previous_config_version", 0))
        != prepared["previous_config_version"]
        or bool(cint(getattr(reset_doc, "stale_export_override", 0)))
        != prepared["stale_export_override"]
        or stored_queue_evidence != prepared["queue_evidence"]
        or _migration_recovery_evidence_from_doc(reset_doc)
        != prepared["migration_recovery"]
        or bool(
            cstr(
                getattr(reset_doc, "registered_by_system_manager", None)
            ).strip()
        )
        or getattr(reset_doc, "credential_recovery_confirmed_at", None)
    ):
        frappe.throw(
            _(
                "Safe reset abandonment evidence does not match the existing "
                "request"
            ),
            frappe.ValidationError,
        )


def _validate_reset_device_binding(reset_doc: Any, device_doc: Any) -> None:
    if not cint(getattr(device_doc, "enabled", 0)):
        frappe.throw(_("KoPOS Device is disabled"), frappe.ValidationError)
    if not hmac.compare_digest(
        cstr(getattr(reset_doc, "device_id", None)).strip(),
        cstr(getattr(device_doc, "device_id", None)).strip(),
    ):
        frappe.throw(_("Safe reset device binding changed"), frappe.ValidationError)
    if not hmac.compare_digest(
        cstr(getattr(reset_doc, "api_user", None)).strip(),
        cstr(getattr(device_doc, "api_user", None)).strip(),
    ):
        frappe.throw(_("Safe reset API user binding changed"), frappe.ValidationError)
    ensure_unique_device_api_user(
        cstr(getattr(device_doc, "api_user", None)).strip(),
        current_device_name=cstr(getattr(device_doc, "name", None)).strip() or None,
    )


def _assert_stored_queue_evidence_is_drained(reset_doc: Any) -> None:
    counts = (
        cint(getattr(reset_doc, "queue_pending_count", 0)),
        cint(getattr(reset_doc, "queue_failed_count", 0)),
        cint(getattr(reset_doc, "queue_syncing_count", 0)),
        cint(getattr(reset_doc, "queue_dead_letter_count", 0)),
    )
    if any(counts):
        frappe.throw(
            _("Safe reset queue evidence is not fully drained"),
            frappe.ValidationError,
        )


def _assert_no_open_shift_or_unresolved_projection(device_id: str) -> None:
    active_shift_rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabFB Shift`
        WHERE device_id = %s
          AND (status IS NULL OR status NOT IN ('Closed', 'Cancelled'))
        ORDER BY name
        LIMIT 1
        FOR UPDATE
        """,
        (device_id,),
        as_dict=True,
    )
    if active_shift_rows:
        frappe.throw(
            _("Safe reset requires every FB Shift for the device to be closed"),
            frappe.ValidationError,
        )

    unresolved_prepared_qr_rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabFB Order`
        WHERE device_id = %s
          AND docstatus = 0
          AND COALESCE(accepted_sale_fingerprint, '') != ''
          AND COALESCE(automatic_qr_state, '') != 'provider_rejected'
        ORDER BY name
        LIMIT 1
        FOR UPDATE
        """,
        (device_id,),
        as_dict=True,
    )
    if unresolved_prepared_qr_rows:
        frappe.throw(
            _(
                "Safe reset requires every prepared Automatic QR sale for the "
                "device to be finalized"
            ),
            frappe.ValidationError,
        )

    provider_rejected_drafts = frappe.db.sql(
        """
        SELECT name
        FROM `tabFB Order`
        WHERE device_id = %s
          AND docstatus = 0
          AND automatic_qr_state = 'provider_rejected'
        ORDER BY name
        LIMIT 65
        """,
        (device_id,),
        as_dict=True,
    )
    if len(provider_rejected_drafts or []) > MAX_PROVIDER_REJECTED_DRAFTS_PER_SAFE_RESET:
        frappe.throw(
            _(
                "Safe reset provider-rejected Automatic QR evidence exceeds the "
                "bounded verification limit"
            ),
            frappe.ValidationError,
        )
    if provider_rejected_drafts:
        from kopos_connector.api.automatic_qr import (
            has_durable_no_provider_release_fence,
        )

        for row in provider_rejected_drafts:
            order_name = cstr(_row_value(row, "name")).strip()
            if not order_name or not has_durable_no_provider_release_fence(order_name):
                frappe.throw(
                    _(
                        "Safe reset requires durable no-provider evidence for "
                        "every provider-rejected Automatic QR sale"
                    ),
                    frappe.ValidationError,
                )

    unresolved_projection_queries = (
        """
        SELECT projection.name
        FROM `tabFB Projection Log` AS projection
        INNER JOIN `tabFB Order` AS source
          ON source.name = projection.source_name
        WHERE projection.source_doctype = 'FB Order'
          AND projection.state IN ('Pending', 'Failed')
          AND source.device_id = %s
        ORDER BY projection.name
        LIMIT 1
        """,
        """
        SELECT projection.name
        FROM `tabFB Projection Log` AS projection
        INNER JOIN `tabFB Shift` AS source
          ON source.name = projection.source_name
        WHERE projection.source_doctype = 'FB Shift'
          AND projection.state IN ('Pending', 'Failed')
          AND source.device_id = %s
        ORDER BY projection.name
        LIMIT 1
        """,
        """
        SELECT projection.name
        FROM `tabFB Projection Log` AS projection
        INNER JOIN `tabFB Return Event` AS return_event
          ON return_event.name = projection.source_name
        INNER JOIN `tabFB Order` AS source
          ON source.name = return_event.fb_order
        WHERE projection.source_doctype = 'FB Return Event'
          AND projection.state IN ('Pending', 'Failed')
          AND source.device_id = %s
        ORDER BY projection.name
        LIMIT 1
        """,
        """
        SELECT projection.name
        FROM `tabFB Projection Log` AS projection
        INNER JOIN `tabFB Waste Event` AS waste_event
          ON waste_event.name = projection.source_name
        INNER JOIN `tabFB Shift` AS source
          ON source.name = waste_event.shift
        WHERE projection.source_doctype = 'FB Waste Event'
          AND projection.state IN ('Pending', 'Failed')
          AND source.device_id = %s
        ORDER BY projection.name
        LIMIT 1
        """,
    )
    for query in unresolved_projection_queries:
        projection_rows = frappe.db.sql(
            query,
            (device_id,),
            as_dict=True,
        )
        if projection_rows:
            frappe.throw(
                _(
                    "Safe reset requires all device projections to succeed or be "
                    "reversed"
                ),
                frappe.ValidationError,
            )

    unresolved_maybank_rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabMaybank QR Transaction`
        WHERE device_id = %s
          AND (
            status IS NULL
            OR status NOT IN ('paid', 'failed', 'timeout')
            OR (
              status = 'paid'
              AND (
                COALESCE(maybank_status, 0) != 1
                OR COALESCE(provider, '') != 'maybank_qr'
                OR COALESCE(transaction_refno, '') = ''
                OR COALESCE(sale_amount_sen, 0) <= 0
                OR COALESCE(currency, '') != 'MYR'
              )
            )
            OR COALESCE(duplicate_payment_status, '') NOT IN
              ('', 'possible_duplicate', 'accounting_pending',
               'refund_required', 'refunded', 'settled_existing_sale')
            OR COALESCE(duplicate_payment_status, '') IN
              ('possible_duplicate', 'accounting_pending', 'refund_required')
            OR (
              status = 'paid'
              AND COALESCE(duplicate_payment_status, '') != 'refunded'
              AND (
                consumed_at IS NULL
                OR fb_order IS NULL
                OR sales_invoice IS NULL
              )
            )
            OR (
              COALESCE(duplicate_payment_status, '') = 'refunded'
              AND status != 'paid'
            )
            OR COALESCE(manual_reconciliation_status, '') NOT IN
              ('', 'reconciled', 'reconciliation_failed')
          )
        ORDER BY name
        LIMIT 1
        """,
        (device_id,),
        as_dict=True,
    )
    if unresolved_maybank_rows:
        frappe.throw(
            _(
                "Safe reset requires every Maybank QR payment for the device to "
                "reach a terminal consumed or exactly refunded state"
            ),
            frappe.ValidationError,
        )

    refunded_duplicate_rows = frappe.db.sql(
        """
        SELECT
          txn.name,
          txn.fb_order,
          evidence.file_size AS evidence_file_size
        FROM `tabMaybank QR Transaction` AS txn
        LEFT JOIN `tabFile` AS evidence
          ON evidence.name = txn.duplicate_refund_evidence_file
        WHERE txn.device_id = %s
          AND txn.status = 'paid'
          AND txn.duplicate_payment_status = 'refunded'
        ORDER BY txn.name
        LIMIT 65
        """,
        (device_id,),
        as_dict=True,
    )
    if len(refunded_duplicate_rows or []) > MAX_REFUNDED_DUPLICATE_PAYMENTS_PER_SAFE_RESET:
        frappe.throw(
            _(
                "Safe reset refunded duplicate-payment evidence exceeds the "
                "bounded verification limit"
            ),
            frappe.ValidationError,
        )

    declared_evidence_workload = 0
    for row in refunded_duplicate_rows or []:
        declared_bytes = cint(_row_value(row, "evidence_file_size"))
        if (
            declared_bytes <= 0
            or declared_evidence_workload
            > MAX_REFUND_EVIDENCE_WORKLOAD_BYTES - declared_bytes
        ):
            frappe.throw(
                _(
                    "Safe reset refunded duplicate-payment evidence exceeds the "
                    "bounded verification workload"
                ),
                frappe.ValidationError,
            )
        declared_evidence_workload += declared_bytes

    verified_evidence_workload = 0
    if refunded_duplicate_rows:
        from kopos_connector.kopos.services.accounting.duplicate_qr_payment_service import (
            lock_and_assert_duplicate_refund_terminal_evidence,
        )

        for row in refunded_duplicate_rows:
            proof = lock_and_assert_duplicate_refund_terminal_evidence(
                cstr(_row_value(row, "name")).strip(),
                expected_order_name=cstr(_row_value(row, "fb_order")).strip(),
                expected_device_id=device_id,
            )
            verified_bytes = cint(proof.get("provider_evidence_byte_length"))
            if (
                verified_bytes <= 0
                or verified_evidence_workload
                > MAX_REFUND_EVIDENCE_WORKLOAD_BYTES - verified_bytes
            ):
                frappe.throw(
                    _(
                        "Safe reset refunded duplicate-payment evidence exceeds "
                        "the bounded verification workload"
                    ),
                    frappe.ValidationError,
                )
            verified_evidence_workload += verified_bytes

    terminal_secondary_claim_rows = frappe.db.sql(
        """
        SELECT
          claim.name,
          claim.fb_order,
          credit_evidence.file_size AS credit_evidence_file_size,
          refund_evidence.file_size AS refund_evidence_file_size
        FROM `tabManual QR Reconciliation` AS claim
        LEFT JOIN `tabFile` AS credit_evidence
          ON credit_evidence.name = claim.finance_credit_evidence_file
        LEFT JOIN `tabFile` AS refund_evidence
          ON refund_evidence.name = claim.finance_refund_evidence_file
        WHERE claim.device_id = %s
          AND claim.claim_role = 'secondary_possible_duplicate'
          AND claim.finance_resolution_status IN
            ('no_second_credit', 'refunded')
        ORDER BY claim.name
        LIMIT 65
        """,
        (device_id,),
        as_dict=True,
    )
    if (
        len(terminal_secondary_claim_rows or [])
        > MAX_TERMINAL_SECONDARY_STATIC_CLAIMS_PER_SAFE_RESET
    ):
        frappe.throw(
            _(
                "Safe reset terminal secondary static QR evidence exceeds the "
                "bounded verification limit"
            ),
            frappe.ValidationError,
        )
    for row in terminal_secondary_claim_rows or []:
        for fieldname in (
            "credit_evidence_file_size",
            "refund_evidence_file_size",
        ):
            declared_bytes = cint(_row_value(row, fieldname))
            if not declared_bytes:
                continue
            if (
                declared_bytes < 0
                or declared_evidence_workload
                > MAX_REFUND_EVIDENCE_WORKLOAD_BYTES - declared_bytes
            ):
                frappe.throw(
                    _(
                        "Safe reset QR finance evidence exceeds the bounded "
                        "verification workload"
                    ),
                    frappe.ValidationError,
                )
            declared_evidence_workload += declared_bytes

    if terminal_secondary_claim_rows:
        from kopos_connector.kopos.services.accounting.secondary_static_claim_resolution import (
            lock_and_assert_secondary_static_claim_terminal,
        )

        for row in terminal_secondary_claim_rows:
            proof = lock_and_assert_secondary_static_claim_terminal(
                cstr(_row_value(row, "name")).strip(),
                expected_order_name=cstr(
                    _row_value(row, "fb_order")
                ).strip(),
                expected_device_id=device_id,
            )
            verified_bytes = cint(proof.get("evidence_byte_length")) + cint(
                proof.get("credit_evidence_byte_length")
            ) + cint(proof.get("refund_evidence_byte_length"))
            if (
                verified_bytes <= 0
                or verified_evidence_workload
                > MAX_REFUND_EVIDENCE_WORKLOAD_BYTES - verified_bytes
            ):
                frappe.throw(
                    _(
                        "Safe reset QR finance evidence exceeds the bounded "
                        "verification workload"
                    ),
                    frappe.ValidationError,
                )
            verified_evidence_workload += verified_bytes

    unresolved_manual_qr_rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabManual QR Reconciliation`
        WHERE device_id = %s
          AND (
            status IS NULL
            OR status NOT IN ('reconciled', 'reconciliation_failed')
            OR (
              claim_role = 'secondary_possible_duplicate'
              AND (
                COALESCE(finance_resolution_status, '') NOT IN
                  ('no_second_credit', 'refunded')
                OR (
                  finance_resolution_status = 'no_second_credit'
                  AND status != 'reconciliation_failed'
                )
                OR (
                  finance_resolution_status = 'refunded'
                  AND status != 'reconciled'
                )
              )
            )
          )
        ORDER BY name
        LIMIT 1
        """,
        (device_id,),
        as_dict=True,
    )
    if unresolved_manual_qr_rows:
        frappe.throw(
            _(
                "Safe reset requires every manual QR reconciliation for the device "
                "to be resolved"
            ),
            frappe.ValidationError,
        )


def _credential_recovery_target_api_user(device_doc: Any) -> str:
    from kopos_connector.api.provisioning import _device_api_user_email

    current_binding = cstr(getattr(device_doc, "api_user", None)).strip()
    if current_binding and frappe.db.exists("User", current_binding):
        return current_binding
    target = _device_api_user_email(device_doc)
    if not target:
        frappe.throw(
            _("Could not derive a dedicated API user for KoPOS Device"),
            frappe.ValidationError,
        )
    return target


def _restore_recovery_device_api_identity(reset_doc: Any, device_doc: Any) -> str:
    """Recreate/re-enable only the audited dedicated user during manager recovery."""
    from kopos_connector.api.provisioning import _ensure_device_api_user

    target_api_user = cstr(getattr(reset_doc, "api_user", None)).strip()
    current_binding = cstr(getattr(device_doc, "api_user", None)).strip()
    if not target_api_user:
        frappe.throw(
            _("Credential recovery audit has no dedicated API user binding"),
            frappe.ValidationError,
        )
    ensure_unique_device_api_user(
        target_api_user,
        current_device_name=cstr(getattr(device_doc, "name", None)).strip() or None,
    )

    current_user_exists = bool(
        current_binding and frappe.db.exists("User", current_binding)
    )
    if not current_binding:
        previous_state = "missing_binding"
    elif not current_user_exists:
        previous_state = "missing_user"
    elif not cint(frappe.db.get_value("User", current_binding, "enabled")):
        previous_state = "disabled_user"
    else:
        previous_state = "complete"

    if current_binding and current_user_exists and not hmac.compare_digest(
        current_binding,
        target_api_user,
    ):
        frappe.throw(
            _("KoPOS Device API user binding changed after recovery registration"),
            frappe.ValidationError,
        )

    setattr(device_doc, "api_user", target_api_user)
    resolved_user = _ensure_device_api_user(device_doc)
    if not hmac.compare_digest(resolved_user, target_api_user):
        raise RuntimeError("Credential recovery restored an unexpected API user")
    if current_binding != target_api_user:
        frappe.db.set_value(
            "KoPOS Device",
            cstr(getattr(device_doc, "name", None)).strip(),
            "api_user",
            target_api_user,
            update_modified=False,
        )
    if not cint(frappe.db.get_value("User", target_api_user, "enabled")):
        raise RuntimeError("Credential recovery dedicated API user was not enabled")
    return previous_state


def _rotate_device_api_credentials(
    device_doc: Any,
    *,
    allow_incomplete_previous: bool,
) -> dict[str, str]:
    api_user = cstr(getattr(device_doc, "api_user", None)).strip()
    if not api_user:
        frappe.throw(
            _("KoPOS Device has no dedicated API user"),
            frappe.ValidationError,
        )
    ensure_unique_device_api_user(
        api_user,
        current_device_name=cstr(getattr(device_doc, "name", None)).strip() or None,
    )
    if not frappe.db.exists("User", api_user):
        frappe.throw(
            _("KoPOS Device dedicated API user is missing"),
            frappe.ValidationError,
        )
    old_api_key = cstr(frappe.db.get_value("User", api_user, "api_key")).strip()
    old_api_secret = _read_api_secret(api_user)
    previous_credential_state = _previous_credential_state(
        api_key=old_api_key,
        api_secret=old_api_secret,
    )
    if previous_credential_state != "complete" and not allow_incomplete_previous:
        frappe.throw(
            _("Existing device credentials are incomplete; use credential recovery support"),
            frappe.ValidationError,
        )

    new_api_key = _generate_distinct_secret(old_api_key, length=15)
    new_api_secret = _generate_distinct_secret(old_api_secret, length=32)
    frappe.db.set_value(
        "User",
        api_user,
        "api_key",
        new_api_key,
        update_modified=False,
    )
    set_encrypted_password("User", api_user, new_api_secret, "api_secret")
    persisted_key = cstr(frappe.db.get_value("User", api_user, "api_key")).strip()
    persisted_secret = _read_api_secret(api_user)
    if not hmac.compare_digest(persisted_key, new_api_key) or not hmac.compare_digest(
        persisted_secret,
        new_api_secret,
    ):
        raise RuntimeError("Rotated device credentials could not be verified")
    return {
        "user": api_user,
        "api_key": new_api_key,
        "api_secret": new_api_secret,
        "previous_credential_state": previous_credential_state,
        "revoked_api_key_sha256": (
            _sha256_text(old_api_key) if old_api_key else ""
        ),
        "issued_api_key_sha256": _sha256_text(new_api_key),
        "issued_api_secret_sha256": _sha256_text(new_api_secret),
    }


def _previous_credential_state(*, api_key: str, api_secret: str) -> str:
    if api_key and api_secret:
        return "complete"
    if not api_key and not api_secret:
        return "missing_both"
    if not api_key:
        return "missing_api_key"
    return "missing_api_secret"


def _read_current_device_api_credentials(device_doc: Any) -> dict[str, str]:
    api_user = cstr(getattr(device_doc, "api_user", None)).strip()
    if not api_user:
        frappe.throw(
            _("KoPOS Device has no dedicated API user"),
            frappe.ValidationError,
        )
    ensure_unique_device_api_user(
        api_user,
        current_device_name=cstr(getattr(device_doc, "name", None)).strip() or None,
    )
    api_key = cstr(frappe.db.get_value("User", api_user, "api_key")).strip()
    api_secret = _read_api_secret(api_user)
    if not api_key or not api_secret:
        frappe.throw(
            _("Rotated device credentials are unavailable; contact ERP support"),
            frappe.ValidationError,
        )
    return {
        "user": api_user,
        "api_key": api_key,
        "api_secret": api_secret,
    }


def _generate_distinct_secret(previous: str, *, length: int) -> str:
    for _attempt in range(3):
        candidate = cstr(frappe.generate_hash(length=length)).strip()
        if candidate and not hmac.compare_digest(candidate, previous):
            return candidate
    raise RuntimeError("Unique replacement credential could not be generated")


def _read_api_secret(api_user: str) -> str:
    try:
        return cstr(
            get_decrypted_password(
                "User",
                api_user,
                "api_secret",
                raise_exception=False,
            )
            or ""
        ).strip()
    except Exception as error:
        log_sanitized_error("KoPOS safe reset credential read failed", error)
        return ""


def _validate_reset_proof(reset_doc: Any, nonce: str) -> None:
    expected = cstr(getattr(reset_doc, "reset_proof_sha256", None)).strip()
    actual = _sha256_text(nonce)
    if not hmac.compare_digest(expected, actual):
        frappe.throw(_("Safe reset proof did not match"), frappe.ValidationError)


def _apply_reset_transition(reset_doc: Any, updates: Mapping[str, Any]) -> None:
    for fieldname, value in updates.items():
        setattr(reset_doc, fieldname, value)
    reset_doc.save(ignore_permissions=True)


def _completion_response(reset_doc: Any) -> dict[str, Any]:
    return {
        "status": "completed",
        "safe_reset_protocol_version": cint(
            getattr(reset_doc, "safe_reset_protocol_version", 0)
        ),
        "reset_id": cstr(getattr(reset_doc, "reset_id", None)).strip()
        or cstr(getattr(reset_doc, "name", None)).strip(),
        "device_id": cstr(getattr(reset_doc, "device_id", None)).strip(),
        "new_config_version": cint(getattr(reset_doc, "new_config_version", 0)),
        "completion_idempotency_sha256": cstr(
            getattr(reset_doc, "completion_idempotency_sha256", None)
        ).strip(),
        "export_sha256": cstr(
            getattr(reset_doc, "completion_export_sha256", None)
        ).strip(),
        "export_content_sha256": cstr(
            getattr(reset_doc, "completion_export_content_sha256", None)
        ).strip(),
        "export_byte_length": cint(
            getattr(reset_doc, "completion_export_byte_length", 0)
        ),
        **_migration_recovery_evidence_from_doc(reset_doc),
        "completed_at": _iso_utc(getattr(reset_doc, "completed_at", None)),
    }


def _safe_reset_response_tags(reset_doc: Any) -> dict[str, Any]:
    return {
        "provisioning_mode": PROVISIONING_MODE_SAFE_RESET,
        "safe_reset_protocol_version": cint(
            getattr(reset_doc, "safe_reset_protocol_version", 0)
        ),
        "request_origin": cstr(
            getattr(reset_doc, "request_origin", None)
        ).strip(),
        "request_id": cstr(getattr(reset_doc, "request_id", None)).strip(),
        "reset_id": cstr(getattr(reset_doc, "reset_id", None)).strip()
        or cstr(getattr(reset_doc, "name", None)).strip(),
        "approval_generation": cint(
            getattr(reset_doc, "approval_generation", 0)
        ),
        "previous_config_version": cint(
            getattr(reset_doc, "previous_config_version", 0)
        ),
        "new_config_version": cint(getattr(reset_doc, "new_config_version", 0)),
        "export_sha256": cstr(getattr(reset_doc, "export_sha256", None)).strip(),
        "export_content_sha256": cstr(
            getattr(reset_doc, "export_content_sha256", None)
        ).strip(),
        "export_byte_length": cint(
            getattr(reset_doc, "export_byte_length", 0)
        ),
        **_migration_recovery_evidence_from_doc(reset_doc),
    }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_expired(value: Any) -> bool:
    if not value:
        return True
    return get_datetime(value) <= now_datetime()


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    parsed = get_datetime(value)
    if isinstance(parsed, datetime):
        return parsed.isoformat()
    return cstr(parsed)


def _iso_utc(value: Any) -> str | None:
    if value is None or value == "":
        return None
    parsed = get_datetime(value)
    if not isinstance(parsed, datetime):
        raise frappe.ValidationError("Safe reset timestamp is invalid")
    if parsed.tzinfo is None:
        timezone_name = cstr(frappe.utils.get_system_timezone()).strip()
        if not timezone_name:
            raise frappe.ValidationError("ERP system timezone is required")
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)
