# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import frappe
from frappe import _

from kopos_connector.utils.diagnostics import log_sanitized_error

from .catalog import (
    build_catalog_payload,
    get_item_modifiers_payload,
    get_tax_rate_value,
)
from .devices import (
    KOPOS_DEVICE_API_ROLE,
    elevate_device_api_user,
    get_authenticated_device_doc,
    get_session_roles,
    lock_device_for_operational_mutation,
    mark_device_seen,
    require_device_context,
    require_device_operational_scope,
    require_kopos_api_access,
    require_system_manager,
)
from .device_safe_reset import (
    abandon_unregistered_device_safe_reset_request as abandon_unregistered_device_safe_reset_request_payload,
    authorize_device_safe_reset as authorize_device_safe_reset_payload,
    cancel_device_safe_reset as cancel_device_safe_reset_payload,
    cancel_device_safe_reset_as_system_manager as cancel_safe_reset_as_manager_payload,
    classify_device_safe_reset_request_registration as classify_safe_reset_request_registration_payload,
    complete_device_safe_reset as complete_device_safe_reset_payload,
    register_device_credential_recovery as register_device_credential_recovery_payload,
    request_device_safe_reset as request_device_safe_reset_payload,
    resolve_device_safe_reset_request as resolve_device_safe_reset_request_payload,
)
from .order_history import get_order_history_payload
from .promotions import get_promotion_snapshot_payload
from .provisioning import (
    create_device_provisioning_qr as create_device_provisioning_qr_payload,
    create_pos_provisioning as create_pos_provisioning_payload,
    get_device_config as get_device_config_payload,
    redeem_pos_provisioning as redeem_pos_provisioning_payload,
)


_REFUND_REASON_OPTIONS = {
    "customer_changed_mind": "Customer changed mind",
    "wrong_order": "Wrong order",
    "quality_issue": "Quality issue",
    "item_damaged": "Item damaged",
    "service_issue": "Service issue",
    "pricing_error": "Pricing error",
    "other": "Other",
}

SAFE_RESET_REQUEST_REJECTED_NO_COMMIT = (
    "SAFE_RESET_REQUEST_REJECTED_NO_COMMIT"
)
SAFE_RESET_REQUEST_LOOKUP_REQUIRED = "SAFE_RESET_REQUEST_LOOKUP_REQUIRED"


def _write_response(payload: dict[str, Any], http_status_code: int = 200) -> None:
    frappe.local.response.update(payload)
    frappe.local.response["http_status_code"] = http_status_code
    for key in ("_server_messages", "exc", "_debug_messages", "exception"):
        frappe.local.response.pop(key, None)


def _set_sensitive_response_headers() -> None:
    """Prevent QR tokens and redeemed credentials from entering browser/proxy caches."""
    headers = frappe.local.response.setdefault("headers", {})
    if not isinstance(headers, dict):
        headers = {}
        frappe.local.response["headers"] = headers
    headers.update(
        {
            "Cache-Control": "private, no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


def _validation_error_payload(exc: frappe.ValidationError) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "message": str(exc)}
    error_code = frappe.utils.cstr(getattr(exc, "error_code", None)).strip()
    if error_code:
        payload["error_code"] = error_code
    return payload


def _utc_server_time() -> str:
    """Return an authenticated clock anchor without changing snapshot identity."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _safe_reset_request_rejection_payload(
    request_payload: Mapping[str, Any],
    exc: frappe.ValidationError,
    *,
    checked_at: str,
) -> dict[str, Any]:
    """Bind an authoritative post-rollback rejection to its request identity."""
    return {
        "status": "rejected",
        "error_code": SAFE_RESET_REQUEST_REJECTED_NO_COMMIT,
        "message": str(exc),
        "request_attempt_committed": False,
        "request_registration_status": "not_found",
        "local_release_authorized": False,
        "recovery_action": "abandon_unregistered_device_safe_reset_request",
        "safe_reset_protocol_version": request_payload.get(
            "safe_reset_protocol_version"
        ),
        "request_id": request_payload.get("request_id"),
        "device_id": request_payload.get("device_id"),
        "previous_config_version": request_payload.get(
            "previous_config_version"
        ),
        "reset_proof_sha256": request_payload.get("reset_proof_sha256"),
        "export_sha256": request_payload.get("export_sha256"),
        "export_content_sha256": request_payload.get(
            "export_content_sha256"
        ),
        "export_byte_length": request_payload.get("export_byte_length"),
        "checked_at": checked_at,
    }


def _safe_reset_request_lookup_required_payload(
    request_payload: Mapping[str, Any],
    exc: frappe.ValidationError,
    *,
    lookup_reason: str,
) -> dict[str, Any]:
    """Return a bound but deliberately non-releasable rejection response."""
    return {
        "status": "lookup_required",
        "error_code": SAFE_RESET_REQUEST_LOOKUP_REQUIRED,
        "message": str(exc),
        "request_registration_status": "lookup_required",
        "lookup_reason": lookup_reason,
        "local_release_authorized": False,
        "recovery_action": "abandon_unregistered_device_safe_reset_request",
        "safe_reset_protocol_version": request_payload.get(
            "safe_reset_protocol_version"
        ),
        "request_id": request_payload.get("request_id"),
        "device_id": request_payload.get("device_id"),
        "previous_config_version": request_payload.get(
            "previous_config_version"
        ),
        "reset_proof_sha256": request_payload.get("reset_proof_sha256"),
        "export_sha256": request_payload.get("export_sha256"),
        "export_content_sha256": request_payload.get(
            "export_content_sha256"
        ),
        "export_byte_length": request_payload.get("export_byte_length"),
        "checked_at": _utc_server_time(),
    }


@frappe.whitelist(allow_guest=True)
def ping() -> None:
    """Simple health endpoint for KoPOS setup validation."""
    _write_response({"message": "KoPOS ERPNext API ready"})


@frappe.whitelist()
def get_catalog(
    since: str | None = None,
    device_id: str | None = None,
    known_version: str | None = None,
) -> None:
    """Public KoPOS endpoint for catalog sync."""
    try:
        require_device_context(device_id=device_id)
        if device_id:
            mark_device_seen(device_id=device_id)
        with elevate_device_api_user():
            _write_response(
                build_catalog_payload(
                    since=since,
                    device_id=device_id,
                    known_version=known_version,
                )
            )
    except Exception as error:
        log_sanitized_error("KoPOS get_catalog failed", error)
        raise


@frappe.whitelist()
def get_tax_rate(pos_profile: str | None = None, device_id: str | None = None) -> None:
    """Public KoPOS endpoint returning a raw tax_rate payload."""
    require_device_context(device_id=device_id)
    _write_response(
        {
            "tax_rate": get_tax_rate_value(
                pos_profile_name=pos_profile, device_id=device_id
            )
        }
    )


@frappe.whitelist()
def get_item_modifiers(item_code: str, device_id: str | None = None) -> None:
    """Return item modifiers within the authenticated device's company scope."""
    if device_id:
        device_doc = require_device_context(device_id=device_id)
    else:
        authenticated_device = get_authenticated_device_doc()
        device_doc = require_device_context(name=authenticated_device.name)

    with elevate_device_api_user():
        pos_profile_name = frappe.utils.cstr(
            getattr(device_doc, "pos_profile", None)
        ).strip()
        if not pos_profile_name:
            frappe.throw(
                _("KoPOS Device has no POS Profile configured"),
                frappe.ValidationError,
            )
        pos_profile = frappe.get_cached_doc("POS Profile", pos_profile_name)
        company = frappe.utils.cstr(getattr(pos_profile, "company", None)).strip()
        if not company:
            frappe.throw(
                _("KoPOS Device POS Profile has no company configured"),
                frappe.ValidationError,
            )
        _write_response(
            {
                "modifier_groups": get_item_modifiers_payload(
                    item_code,
                    company=company,
                )
            }
        )


@frappe.whitelist()
def get_refund_reasons() -> None:
    """Return supported refund reason presets for KoPOS clients."""
    require_kopos_api_access()
    _write_response(
        {
            "refund_reasons": [
                {"code": code, "label": label}
                for code, label in _REFUND_REASON_OPTIONS.items()
            ]
        }
    )


@frappe.whitelist()
def get_promotion_snapshot(
    pos_profile: str | None = None,
    current_version: str | None = None,
    device_id: str | None = None,
) -> None:
    """Return the latest KoPOS promotion snapshot for a POS profile."""
    try:
        require_device_context(device_id=device_id)
        if device_id:
            mark_device_seen(device_id=device_id)
        payload = get_promotion_snapshot_payload(
            pos_profile=pos_profile,
            current_version=current_version,
            device_id=device_id,
        )
        if payload is None:
            _write_response(
                {
                    "status": "unavailable",
                    "reason": "no_published_snapshot",
                    "message": "No promotion snapshot has been published for this POS profile",
                    "server_time": _utc_server_time(),
                }
            )
        else:
            response_payload = dict(payload)
            response_payload["server_time"] = _utc_server_time()
            _write_response(response_payload)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS get_promotion_snapshot failed", error)
        _write_response(
            {"status": "error", "message": "Failed to fetch promotion snapshot"},
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def create_device_provisioning_qr(**kwargs: Any) -> None:
    """Create a one-click KoPOS provisioning QR using dedicated per-device credentials."""
    try:
        _set_sensitive_response_headers()
        payload = _get_submit_payload(kwargs)
        _write_response(
            create_device_provisioning_qr_payload(
                device=frappe.utils.cstr(payload.get("device")),
                erpnext_url=frappe.utils.cstr(payload.get("erpnext_url")),
                expires_in_seconds=payload.get("expires_in_seconds"),
                rotate_credentials=payload.get("rotate_credentials") or False,
            )
        )
    except frappe.ValidationError as exc:
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)


@frappe.whitelist(methods=["POST"])
def create_pos_provisioning(**kwargs: Any) -> None:
    """Create a short-lived KoPOS provisioning link for QR-based setup."""
    try:
        _set_sensitive_response_headers()
        payload = _get_submit_payload(kwargs)
        _write_response(
            create_pos_provisioning_payload(
                device=frappe.utils.cstr(payload.get("device")),
                pos_profile=frappe.utils.cstr(payload.get("pos_profile")),
                erpnext_url=frappe.utils.cstr(payload.get("erpnext_url")),
                api_key=frappe.utils.cstr(payload.get("api_key")),
                api_secret=frappe.utils.cstr(payload.get("api_secret")),
                warehouse=frappe.utils.cstr(payload.get("warehouse")),
                company=frappe.utils.cstr(payload.get("company")),
                currency=frappe.utils.cstr(payload.get("currency")),
                device_name=frappe.utils.cstr(payload.get("device_name")),
                device_prefix=frappe.utils.cstr(payload.get("device_prefix")),
                expires_in_seconds=payload.get("expires_in_seconds"),
            )
        )
    except frappe.ValidationError as exc:
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def redeem_pos_provisioning(token: str | None = None, **kwargs: Any) -> None:
    """Redeem a one-time KoPOS provisioning link from a QR/deep link."""
    try:
        _set_sensitive_response_headers()
        payload = _get_submit_payload(kwargs)
        token_value = token or payload.get("token")
        _write_response(
            redeem_pos_provisioning_payload(
                token=frappe.utils.cstr(token_value),
                safe_reset_protocol_version=payload.get(
                    "safe_reset_protocol_version"
                ),
                reset_id=frappe.utils.cstr(payload.get("reset_id")),
                request_id=frappe.utils.cstr(payload.get("request_id")),
                approval_challenge_id=frappe.utils.cstr(
                    payload.get("approval_challenge_id")
                ),
                approval_generation=payload.get("approval_generation"),
                reset_proof_nonce=frappe.utils.cstr(
                    payload.get("reset_proof_nonce")
                ),
                redemption_idempotency_key=frappe.utils.cstr(
                    payload.get("redemption_idempotency_key")
                ),
                export_sha256=frappe.utils.cstr(payload.get("export_sha256")),
                export_content_sha256=frappe.utils.cstr(
                    payload.get("export_content_sha256")
                ),
                export_byte_length=payload.get("export_byte_length"),
            )
        )
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS provisioning redemption failed", error)
        _write_response(
            {"status": "error", "message": "Failed to redeem device provisioning"},
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def request_device_safe_reset(**kwargs: Any) -> None:
    """Register immutable safe-reset evidence using the current device credential."""
    payload: Mapping[str, Any] = kwargs
    try:
        payload = _get_submit_payload(kwargs)
        _write_response(
            request_device_safe_reset_payload(
                safe_reset_protocol_version=payload.get(
                    "safe_reset_protocol_version"
                ),
                request_id=frappe.utils.cstr(payload.get("request_id")),
                device_id=frappe.utils.cstr(payload.get("device_id")),
                reason=frappe.utils.cstr(payload.get("reason")),
                export_sha256=frappe.utils.cstr(payload.get("export_sha256")),
                export_content_sha256=frappe.utils.cstr(
                    payload.get("export_content_sha256")
                ),
                export_byte_length=payload.get("export_byte_length"),
                exported_at=payload.get("exported_at")
                or payload.get("export_generated_at"),
                drained_row_count=payload.get("drained_row_count"),
                queue_evidence=payload.get("queue_evidence"),
                migration_recovery_point_count=payload.get(
                    "migration_recovery_point_count"
                ),
                migration_recovery_valid_point_count=payload.get(
                    "migration_recovery_valid_point_count"
                ),
                migration_recovery_invalid_point_count=payload.get(
                    "migration_recovery_invalid_point_count"
                ),
                migration_recovery_captured_pending_total=payload.get(
                    "migration_recovery_captured_pending_total"
                ),
                migration_recovery_review_required=payload.get(
                    "migration_recovery_review_required"
                ),
                previous_config_version=payload.get("previous_config_version"),
                reset_proof_sha256=frappe.utils.cstr(
                    payload.get("reset_proof_sha256")
                ),
                erp_base_url=frappe.utils.cstr(
                    payload.get("erp_base_url") or payload.get("erp_origin")
                ),
                company=frappe.utils.cstr(payload.get("company")),
                currency=frappe.utils.cstr(payload.get("currency")),
                pos_profile=frappe.utils.cstr(payload.get("pos_profile")),
                warehouse=frappe.utils.cstr(payload.get("warehouse")),
            )
        )
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        try:
            registration_check = (
                classify_safe_reset_request_registration_payload(
                    safe_reset_protocol_version=payload.get(
                        "safe_reset_protocol_version"
                    ),
                    device_id=frappe.utils.cstr(payload.get("device_id")),
                    request_id=frappe.utils.cstr(payload.get("request_id")),
                    reset_proof_sha256=frappe.utils.cstr(
                        payload.get("reset_proof_sha256")
                    ),
                    export_sha256=frappe.utils.cstr(
                        payload.get("export_sha256")
                    ),
                    export_content_sha256=frappe.utils.cstr(
                        payload.get("export_content_sha256")
                    ),
                    export_byte_length=payload.get("export_byte_length"),
                    previous_config_version=payload.get(
                        "previous_config_version"
                    ),
                )
            )
        except frappe.ValidationError:
            response_payload = _safe_reset_request_lookup_required_payload(
                payload,
                exc,
                lookup_reason="verification_failed",
            )
        except Exception as resolution_error:
            log_sanitized_error(
                "KoPOS safe reset rejection resolution failed",
                resolution_error,
            )
            response_payload = _safe_reset_request_lookup_required_payload(
                payload,
                exc,
                lookup_reason="verification_unavailable",
            )
        else:
            registration_status = registration_check.get(
                "request_registration_status"
            )
            checked_at = registration_check.get("checked_at")
            if registration_status == "not_found" and checked_at:
                response_payload = _safe_reset_request_rejection_payload(
                    payload,
                    exc,
                    checked_at=checked_at,
                )
            else:
                response_payload = _safe_reset_request_lookup_required_payload(
                    payload,
                    exc,
                    lookup_reason=registration_status or "verification_unavailable",
                )
        _write_response(response_payload, http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS safe reset request failed", error)
        _write_response(
            {"status": "error", "message": "Failed to register safe reset request"},
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def abandon_unregistered_device_safe_reset_request(**kwargs: Any) -> None:
    """Durably fence an unacknowledged safe-reset request before local release."""
    try:
        _set_sensitive_response_headers()
        payload = _get_submit_payload(kwargs)
        _write_response(
            abandon_unregistered_device_safe_reset_request_payload(
                safe_reset_protocol_version=payload.get(
                    "safe_reset_protocol_version"
                ),
                device_id=frappe.utils.cstr(payload.get("device_id")),
                request_id=frappe.utils.cstr(payload.get("request_id")),
                reason=frappe.utils.cstr(payload.get("reason")),
                export_sha256=frappe.utils.cstr(payload.get("export_sha256")),
                export_content_sha256=frappe.utils.cstr(
                    payload.get("export_content_sha256")
                ),
                export_byte_length=payload.get("export_byte_length"),
                exported_at=payload.get("exported_at")
                or payload.get("export_generated_at"),
                drained_row_count=payload.get("drained_row_count"),
                queue_evidence=payload.get("queue_evidence"),
                migration_recovery_point_count=payload.get(
                    "migration_recovery_point_count"
                ),
                migration_recovery_valid_point_count=payload.get(
                    "migration_recovery_valid_point_count"
                ),
                migration_recovery_invalid_point_count=payload.get(
                    "migration_recovery_invalid_point_count"
                ),
                migration_recovery_captured_pending_total=payload.get(
                    "migration_recovery_captured_pending_total"
                ),
                migration_recovery_review_required=payload.get(
                    "migration_recovery_review_required"
                ),
                previous_config_version=payload.get("previous_config_version"),
                reset_proof_sha256=frappe.utils.cstr(
                    payload.get("reset_proof_sha256")
                ),
                erp_base_url=frappe.utils.cstr(
                    payload.get("erp_base_url") or payload.get("erp_origin")
                ),
                company=frappe.utils.cstr(payload.get("company")),
                currency=frappe.utils.cstr(payload.get("currency")),
                pos_profile=frappe.utils.cstr(payload.get("pos_profile")),
                warehouse=frappe.utils.cstr(payload.get("warehouse")),
                cancellation_idempotency_key=frappe.utils.cstr(
                    payload.get("cancellation_idempotency_key")
                ),
            )
        )
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response(_validation_error_payload(exc), http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS safe reset abandonment failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Failed to abandon unacknowledged safe reset request",
            },
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def resolve_device_safe_reset_request(**kwargs: Any) -> None:
    """Resolve whether an exact safe-reset request was registered by ERP."""
    try:
        payload = _get_submit_payload(kwargs)
        _write_response(
            resolve_device_safe_reset_request_payload(
                safe_reset_protocol_version=payload.get(
                    "safe_reset_protocol_version"
                ),
                device_id=frappe.utils.cstr(payload.get("device_id")),
                request_id=frappe.utils.cstr(payload.get("request_id")),
                reset_proof_sha256=frappe.utils.cstr(
                    payload.get("reset_proof_sha256")
                ),
                export_sha256=frappe.utils.cstr(
                    payload.get("export_sha256")
                ),
                export_content_sha256=frappe.utils.cstr(
                    payload.get("export_content_sha256")
                ),
                export_byte_length=payload.get("export_byte_length"),
                previous_config_version=payload.get(
                    "previous_config_version"
                ),
            )
        )
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response(_validation_error_payload(exc), http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS safe reset request resolution failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Failed to resolve safe reset request",
            },
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def register_device_credential_recovery(**kwargs: Any) -> None:
    """System Manager registration for a tablet whose old credential is unavailable."""
    try:
        payload = _get_submit_payload(kwargs)
        _write_response(
            register_device_credential_recovery_payload(
                safe_reset_protocol_version=payload.get(
                    "safe_reset_protocol_version"
                ),
                confirmation=frappe.utils.cstr(payload.get("confirmation")),
                request_id=frappe.utils.cstr(payload.get("request_id")),
                device_id=frappe.utils.cstr(payload.get("device_id")),
                reason=frappe.utils.cstr(payload.get("reason")),
                export_sha256=frappe.utils.cstr(payload.get("export_sha256")),
                export_content_sha256=frappe.utils.cstr(
                    payload.get("export_content_sha256")
                ),
                export_byte_length=payload.get("export_byte_length"),
                exported_at=payload.get("exported_at")
                or payload.get("export_generated_at"),
                drained_row_count=payload.get("drained_row_count"),
                queue_evidence=payload.get("queue_evidence"),
                migration_recovery_point_count=payload.get(
                    "migration_recovery_point_count"
                ),
                migration_recovery_valid_point_count=payload.get(
                    "migration_recovery_valid_point_count"
                ),
                migration_recovery_invalid_point_count=payload.get(
                    "migration_recovery_invalid_point_count"
                ),
                migration_recovery_captured_pending_total=payload.get(
                    "migration_recovery_captured_pending_total"
                ),
                migration_recovery_review_required=payload.get(
                    "migration_recovery_review_required"
                ),
                previous_config_version=payload.get("previous_config_version"),
                reset_proof_sha256=frappe.utils.cstr(
                    payload.get("reset_proof_sha256")
                ),
                erp_base_url=frappe.utils.cstr(
                    payload.get("erp_base_url") or payload.get("erp_origin")
                ),
                company=frappe.utils.cstr(payload.get("company")),
                currency=frappe.utils.cstr(payload.get("currency")),
                pos_profile=frappe.utils.cstr(payload.get("pos_profile")),
                warehouse=frappe.utils.cstr(payload.get("warehouse")),
                allow_stale_export=payload.get("allow_stale_export") or False,
                stale_export_override_reason=frappe.utils.cstr(
                    payload.get("stale_export_override_reason")
                ),
            )
        )
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response(_validation_error_payload(exc), http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS credential recovery registration failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Failed to register device credential recovery",
            },
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def authorize_device_safe_reset(**kwargs: Any) -> None:
    """Authorize or reissue a proof-bound safe-reset QR as a System Manager."""
    try:
        _set_sensitive_response_headers()
        payload = _get_submit_payload(kwargs)
        _write_response(
            authorize_device_safe_reset_payload(
                reset_id=frappe.utils.cstr(payload.get("reset_id")),
                expires_in_seconds=payload.get("expires_in_seconds"),
                erpnext_url=frappe.utils.cstr(payload.get("erpnext_url")),
                migration_recovery_confirmation=frappe.utils.cstr(
                    payload.get("migration_recovery_confirmation")
                ),
                migration_recovery_acknowledgement_reason=frappe.utils.cstr(
                    payload.get("migration_recovery_acknowledgement_reason")
                ),
            )
        )
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response(_validation_error_payload(exc), http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS safe reset authorization route failed", error)
        _write_response(
            {"status": "error", "message": "Failed to authorize safe reset"},
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def cancel_device_safe_reset(**kwargs: Any) -> None:
    """Cancel a requested or authorized safe reset before credential rotation."""
    try:
        _set_sensitive_response_headers()
        payload = _get_submit_payload(kwargs)
        _write_response(
            cancel_device_safe_reset_payload(
                safe_reset_protocol_version=payload.get(
                    "safe_reset_protocol_version"
                ),
                confirmation=frappe.utils.cstr(payload.get("confirmation")),
                request_id=frappe.utils.cstr(payload.get("request_id")),
                reset_id=frappe.utils.cstr(payload.get("reset_id")),
                device_id=frappe.utils.cstr(payload.get("device_id")),
                reason=frappe.utils.cstr(payload.get("reason")),
                idempotency_key=frappe.utils.cstr(
                    payload.get("idempotency_key")
                ),
                previous_config_version=payload.get("previous_config_version"),
                reset_proof_sha256=frappe.utils.cstr(
                    payload.get("reset_proof_sha256")
                ),
            )
        )
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response(_validation_error_payload(exc), http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS safe reset cancellation failed", error)
        _write_response(
            {"status": "error", "message": "Failed to cancel safe reset"},
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def cancel_device_safe_reset_as_system_manager(**kwargs: Any) -> None:
    """System Manager abandonment for a requested/authorized reset without rotation."""
    try:
        _set_sensitive_response_headers()
        payload = _get_submit_payload(kwargs)
        _write_response(
            cancel_safe_reset_as_manager_payload(
                safe_reset_protocol_version=payload.get(
                    "safe_reset_protocol_version"
                ),
                confirmation=frappe.utils.cstr(payload.get("confirmation")),
                reset_id=frappe.utils.cstr(payload.get("reset_id")),
                reason=frappe.utils.cstr(payload.get("reason")),
                idempotency_key=frappe.utils.cstr(
                    payload.get("idempotency_key")
                ),
            )
        )
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response(_validation_error_payload(exc), http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error(
            "KoPOS System Manager safe reset cancellation failed",
            error,
        )
        _write_response(
            {"status": "error", "message": "Failed to cancel safe reset"},
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def complete_device_safe_reset(**kwargs: Any) -> None:
    """Complete safe reset using the newly rotated device credential."""
    try:
        payload = _get_submit_payload(kwargs)
        _write_response(
            complete_device_safe_reset_payload(
                safe_reset_protocol_version=payload.get(
                    "safe_reset_protocol_version"
                ),
                device_id=frappe.utils.cstr(payload.get("device_id")),
                reset_id=frappe.utils.cstr(payload.get("reset_id")),
                new_config_version=payload.get("new_config_version"),
                export_sha256=frappe.utils.cstr(payload.get("export_sha256")),
                export_content_sha256=frappe.utils.cstr(
                    payload.get("export_content_sha256")
                ),
                export_byte_length=payload.get("export_byte_length"),
                completion_idempotency_key=frappe.utils.cstr(
                    payload.get("completion_idempotency_key")
                ),
            )
        )
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response(_validation_error_payload(exc), http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS safe reset completion failed", error)
        _write_response(
            {"status": "error", "message": "Failed to complete safe reset"},
            http_status_code=500,
        )


@frappe.whitelist()
def get_device_config(device_id: str | None = None, **kwargs: Any) -> None:
    """Return ERP-managed config for a provisioned KoPOS device."""
    try:
        payload = _get_submit_payload(kwargs)
        resolved_device_id = device_id or payload.get("device_id")
        require_device_context(device_id=frappe.utils.cstr(resolved_device_id))
        _write_response(
            get_device_config_payload(device_id=frappe.utils.cstr(resolved_device_id))
        )
    except frappe.ValidationError as exc:
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)


@frappe.whitelist(methods=["POST"])
def publish_promotion_snapshot(
    pos_profile: str | None = None, device_id: str | None = None
) -> None:
    """Publish an immutable KoPOS promotion snapshot for a POS profile."""
    from .promotions import publish_promotion_snapshot as publish

    require_system_manager()
    _write_response(publish(pos_profile=pos_profile, device_id=device_id))


@frappe.whitelist()
def get_promotion_review_queue(limit: int = 20) -> None:
    require_system_manager()
    _write_response({"items": []})


@frappe.whitelist(methods=["POST"])
def review_promotion_reconciliation(**kwargs: Any) -> None:
    try:
        require_system_manager()
        _get_submit_payload(kwargs)
        _write_response({"status": "unavailable", "message": "Promotion reconciliation review is not enabled for Sales Invoice flow"})
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS review_promotion_reconciliation failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while reviewing promotion reconciliation",
            },
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def prepare_automatic_qr_sale(**kwargs: Any) -> None:
    """Persist the immutable FB Order snapshot before Maybank QR generation."""
    from kopos_connector.kopos.api.fb_orders import (
        prepare_automatic_qr_sale_payload,
    )

    try:
        payload = _get_submit_payload(kwargs)
        lock_device_for_operational_mutation(
            device_id=frappe.utils.cstr(payload.get("device_id"))
        )
        require_device_operational_scope(
            frappe.utils.cstr(payload.get("device_id")),
            company=frappe.utils.cstr(payload.get("company")),
            warehouse=frappe.utils.cstr(
                payload.get("booth_warehouse") or payload.get("warehouse")
            ),
            currency=frappe.utils.cstr(payload.get("currency")),
        )
        result = prepare_automatic_qr_sale_payload(
            _to_public_fb_submit_payload(payload)
        )
        _write_response(result)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS prepare_automatic_qr_sale failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while preparing Automatic QR sale",
            },
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def submit_order(**kwargs: Any) -> None:
    """Public KoPOS endpoint for FB Order submission with raw JSON responses."""
    from kopos_connector.kopos.api.fb_orders import submit_order_payload

    try:
        payload = _get_submit_payload(kwargs)
        lock_device_for_operational_mutation(
            device_id=frappe.utils.cstr(payload.get("device_id"))
        )
        require_device_operational_scope(
            frappe.utils.cstr(payload.get("device_id")),
            company=frappe.utils.cstr(payload.get("company")),
            warehouse=frappe.utils.cstr(
                payload.get("booth_warehouse") or payload.get("warehouse")
            ),
            currency=frappe.utils.cstr(payload.get("currency")),
        )
        fb_payload = _to_public_fb_submit_payload(payload)
        result = submit_order_payload(fb_payload)
        _write_response(_to_public_fb_submit_response(fb_payload, result))
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS submit_order failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while submitting order",
            },
            http_status_code=500,
        )


def _to_public_fb_submit_response(
    payload: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    sales_invoice = frappe.utils.cstr(result.get("sales_invoice")) or None
    order_id = frappe.utils.cstr(result.get("order_id") or payload.get("order_id"))
    return {
        "status": result.get("status"),
        "fb_order": result.get("fb_order"),
        "order_id": order_id or result.get("fb_order"),
        "idempotency_key": result.get("idempotency_key") or payload.get("idempotency_key"),
        "sales_invoice": sales_invoice,
        "ingredient_stock_entry": result.get("ingredient_stock_entry"),
        "order_status": result.get("order_status"),
        "invoice_status": result.get("invoice_status"),
        "stock_status": result.get("stock_status"),
        "partial_failure": result.get("partial_failure") or False,
        "projection_status": result.get("projection_status"),
        "failed_subsystem": result.get("failed_subsystem"),
        "diagnostics": _sanitize_public_projection_diagnostics(
            result.get("diagnostics")
        ),
        "projections": _sanitize_public_projection_rows(result.get("projections")),
        "message": _sanitize_public_projection_message(result),
    }


def _sanitize_public_projection_message(result: Mapping[str, Any]) -> str | None:
    if not result.get("partial_failure") and not result.get("failed_subsystem"):
        return frappe.utils.cstr(result.get("message")) or None
    failed_subsystem = frappe.utils.cstr(result.get("failed_subsystem")).strip()
    if failed_subsystem:
        return f"{failed_subsystem} projection failed"
    return "Projection failed"


def _sanitize_public_projection_diagnostics(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    diagnostics = []
    for row in value:
        if not isinstance(row, Mapping):
            continue
        failed_subsystem = frappe.utils.cstr(row.get("failed_subsystem")).strip()
        diagnostics.append(
            {
                "fb_order": row.get("fb_order"),
                "projection_status": row.get("projection_status"),
                "failed_subsystem": failed_subsystem or None,
                "error_message": f"{failed_subsystem} projection failed"
                if failed_subsystem
                else "Projection failed",
                "idempotency_key": row.get("idempotency_key"),
            }
        )
    return diagnostics


def _sanitize_public_projection_rows(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    rows = []
    for row in value:
        if not isinstance(row, Mapping):
            continue
        state = frappe.utils.cstr(row.get("state")).strip()
        projection_type = frappe.utils.cstr(row.get("projection_type")).strip()
        rows.append(
            {
                "projection_log": row.get("projection_log"),
                "projection_type": projection_type or None,
                "state": state or None,
                "target_doctype": row.get("target_doctype"),
                "target_name": row.get("target_name"),
                "idempotency_key": row.get("idempotency_key"),
                "retry_count": row.get("retry_count"),
                "last_error": f"{projection_type} projection failed"
                if state == "Failed" and projection_type
                else None,
                "last_attempt_at": row.get("last_attempt_at"),
            }
        )
    return rows


def _to_public_fb_submit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    fb_payload = dict(payload)
    order_payload = _string_keyed_dict(payload.get("order"))
    display_number = frappe.utils.cstr(order_payload.get("display_number"))
    if display_number and not frappe.utils.cstr(fb_payload.get("notes")):
        fb_payload["notes"] = f"KoPOS display number: {display_number}"
    return fb_payload


def _string_keyed_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): mapped_value for key, mapped_value in value.items()}


def _get_submit_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
    request_json = None
    if getattr(frappe, "request", None):
        request_json = frappe.request.get_json(silent=True)

    if isinstance(request_json, dict):
        return request_json

    if kwargs:
        payload = dict(kwargs)
        order = payload.get("order")
        if isinstance(order, str):
            payload["order"] = frappe.parse_json(order)
        return payload

    form_dict = dict(frappe.form_dict or {})
    form_dict.pop("cmd", None)
    if isinstance(form_dict.get("order"), str):
        form_dict["order"] = frappe.parse_json(form_dict["order"])
        return form_dict

    return form_dict


@frappe.whitelist(methods=["POST"])
def open_shift(**kwargs: Any) -> None:
    """Public KoPOS endpoint for opening an FB Shift."""
    from .shifts import open_shift_payload

    try:
        payload = _get_submit_payload(kwargs)
        lock_device_for_operational_mutation(
            device_id=frappe.utils.cstr(payload.get("device_id"))
        )
        result = open_shift_payload(payload)
        _write_response(result)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS open_shift failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while opening shift",
            },
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def close_shift(**kwargs: Any) -> None:
    """Public KoPOS endpoint for closing an FB Shift."""
    from .shifts import close_shift_payload

    try:
        payload = _get_submit_payload(kwargs)
        lock_device_for_operational_mutation(
            device_id=frappe.utils.cstr(payload.get("device_id"))
        )
        result = close_shift_payload(payload)
        _write_response(result)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS close_shift failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while closing shift",
            },
            http_status_code=500,
        )


@frappe.whitelist()
def get_device_open_shift(device_id: str | None = None) -> None:
    """Public KoPOS endpoint to get the current open shift for a device.

    This allows KoPOS to discover and adopt an existing open shift that was
    created from another device or from ERPNext directly.
    """
    from .shifts import get_device_open_shift_payload

    try:
        resolved_device_id = frappe.utils.cstr(device_id)
        if not resolved_device_id:
            _write_response(
                {"status": "error", "message": "device_id is required"},
                http_status_code=400,
            )
            return

        require_device_context(device_id=resolved_device_id)
        mark_device_seen(device_id=resolved_device_id)

        result = get_device_open_shift_payload(device_id=resolved_device_id)
        if result:
            _write_response({"status": "ok", "shift": result})
        else:
            _write_response({"status": "ok", "shift": None})
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS get_device_open_shift failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while fetching open shift",
            },
            http_status_code=500,
        )


@frappe.whitelist()
def get_order_history(
    device_id: str | None = None,
    since_date: str | None = None,
    cursor: str | int | None = None,
    limit: str | int | None = None,
) -> dict[str, Any]:
    """Public KoPOS endpoint for current-shift Sales Invoice history."""
    try:
        resolved_device_id = frappe.utils.cstr(device_id).strip()
        if resolved_device_id:
            require_device_context(device_id=resolved_device_id)
            mark_device_seen(device_id=resolved_device_id)
        else:
            device_doc = get_authenticated_device_doc()
            resolved_device_id = frappe.utils.cstr(
                getattr(device_doc, "device_id", None)
            ).strip()
            if resolved_device_id:
                mark_device_seen(device_id=resolved_device_id)

        result = get_order_history_payload(
            device_id=resolved_device_id,
            since_date=since_date,
            cursor=cursor,
            limit=limit,
        )
        _write_response(result)
        return result
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
        return {"status": "error", "message": str(exc)}
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS get_order_history failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while fetching order history",
            },
            http_status_code=500,
        )
        return {
            "status": "error",
            "message": "Unexpected server error while fetching order history",
        }


@frappe.whitelist(methods=["POST"])
def void_order(**kwargs: Any) -> None:
    """Public KoPOS endpoint for voiding a submitted Sales Invoice."""
    try:
        payload = _get_submit_payload(kwargs)
        lock_device_for_operational_mutation(
            device_id=frappe.utils.cstr(payload.get("device_id"))
        )
        result = _process_sales_invoice_void_payload(payload)
        _write_response(result)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response(_validation_error_payload(exc), http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS void_order failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while voiding order",
            },
            http_status_code=500,
        )


def _process_sales_invoice_void_payload(payload: dict[str, Any]) -> dict[str, Any]:
    from kopos_connector.utils.manager_approval import (
        build_sales_invoice_approval_scope,
        canonical_context_hash,
        load_consumed_manager_approval_proof,
        verify_manager_approval_token,
    )

    sales_invoice = frappe.utils.cstr(payload.get("sales_invoice")).strip()
    device_id = frappe.utils.cstr(payload.get("device_id")).strip()
    idempotency_key = frappe.utils.cstr(payload.get("idempotency_key")).strip()
    reason = frappe.utils.cstr(payload.get("reason")).strip()
    manager_approval_token = frappe.utils.cstr(
        payload.get("manager_approval_token")
    ).strip()
    if not sales_invoice:
        frappe.throw(_("sales_invoice is required"), frappe.ValidationError)
    if not device_id:
        frappe.throw(_("device_id is required"), frappe.ValidationError)
    if not idempotency_key:
        frappe.throw(_("idempotency_key is required"), frappe.ValidationError)
    locked_cash_shift = _lock_sales_invoice_cash_shift(sales_invoice)
    locked_rows = frappe.db.sql(
        """
        SELECT
            name, docstatus, is_return, custom_fb_order, custom_fb_shift,
            custom_fb_device_id, grand_total, custom_fb_void_idempotency_key,
            custom_fb_void_request_fingerprint, custom_fb_void_manager,
            custom_fb_void_approval_token_id
        FROM `tabSales Invoice`
        WHERE name = %s
        FOR UPDATE
        """,
        (sales_invoice,),
        as_dict=True,
    )
    invoice = frappe.get_doc("Sales Invoice", sales_invoice)
    if locked_rows:
        for fieldname in (
            "docstatus",
            "is_return",
            "custom_fb_order",
            "custom_fb_shift",
            "custom_fb_device_id",
            "grand_total",
            "custom_fb_void_idempotency_key",
            "custom_fb_void_request_fingerprint",
            "custom_fb_void_manager",
            "custom_fb_void_approval_token_id",
        ):
            locked_value = _row_value(locked_rows[0], fieldname)
            if locked_value is not None:
                setattr(invoice, fieldname, locked_value)
    invoice_cash_shift = frappe.utils.cstr(
        getattr(invoice, "custom_fb_shift", None)
    ).strip()
    if not locked_cash_shift or invoice_cash_shift != locked_cash_shift:
        frappe.throw(
            _(
                "Sales Invoice {0} has no stable FB Shift cash scope; retry after repairing its shift link"
            ).format(sales_invoice),
            frappe.ValidationError,
        )
    if getattr(invoice, "is_return", 0):
        frappe.throw(_("Cannot void a return Sales Invoice"), frappe.ValidationError)
    if not _is_fb_sales_invoice(invoice):
        frappe.throw(
            _("Sales Invoice {0} was not created via KoPOS").format(sales_invoice),
            frappe.ValidationError,
        )
    invoice_device_id = frappe.utils.cstr(getattr(invoice, "custom_fb_device_id", ""))
    if not invoice_device_id:
        frappe.throw(_("Sales Invoice {0} has no device ownership context").format(sales_invoice), frappe.ValidationError)
    if invoice_device_id != device_id:
        frappe.throw(_("Sales Invoice {0} belongs to another device").format(sales_invoice), frappe.ValidationError)
    approval_context = {"reason": reason}
    scope = build_sales_invoice_approval_scope(invoice, context=approval_context)
    if scope["device_id"] != device_id:
        frappe.throw(
            _("Sales Invoice {0} belongs to another device").format(sales_invoice),
            frappe.ValidationError,
        )
    request_fingerprint = canonical_context_hash(
        {
            "idempotency_key": idempotency_key,
            "device_id": scope["device_id"],
            "staff_id": scope["staff_id"],
            "shift_id": scope["shift_id"],
            "resource_id": scope["resource_id"],
            "amount_sen": scope["amount_sen"],
            "context": approval_context,
        }
    )
    if invoice.docstatus == 2:
        _validate_completed_void_retry(
            invoice,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        _apply_fb_void_side_effects(invoice)
        proof = load_consumed_manager_approval_proof(
            approval_token_id=frappe.utils.cstr(
                getattr(invoice, "custom_fb_void_approval_token_id", None)
            ).strip(),
            approval_manager_id=frappe.utils.cstr(
                getattr(invoice, "custom_fb_void_manager", None)
            ).strip(),
            action="void_order",
            idempotency_key=idempotency_key,
            resource_id=sales_invoice,
        )
        return {
            "status": "duplicate",
            "sales_invoice": sales_invoice,
            "idempotency_key": idempotency_key,
            "order_status": "Cancelled",
            "invoice_status": "Cancelled",
            **proof,
        }
    if invoice.docstatus != 1:
        frappe.throw(_("Sales Invoice {0} is not submitted").format(sales_invoice), frappe.ValidationError)
    approval = verify_manager_approval_token(
        manager_approval_token,
        device_id=scope["device_id"],
        staff_id=scope["staff_id"],
        action="void_order",
        shift_id=scope["shift_id"],
        resource_id=scope["resource_id"],
        amount_sen=scope["amount_sen"],
        context_hash=scope["context_hash"],
        idempotency_key=idempotency_key,
    )
    with elevate_device_api_user():
        void_updates = {
            "custom_fb_void_idempotency_key": idempotency_key,
            "custom_fb_void_request_fingerprint": request_fingerprint,
            "custom_fb_void_manager": approval["manager_id"],
            "custom_fb_void_approval_token_id": approval["token_id"],
        }
        frappe.db.set_value(
            "Sales Invoice",
            sales_invoice,
            void_updates,
            update_modified=False,
        )
        for fieldname, value in void_updates.items():
            setattr(invoice, fieldname, value)
        if reason:
            invoice.add_comment("Comment", f"KoPOS void reason: {reason}")
        flags = getattr(invoice, "flags", None)
        if flags is not None:
            flags.ignore_links = True
        invoice.cancel()
        _apply_fb_void_side_effects(invoice)
    proof = load_consumed_manager_approval_proof(
        approval_token_id=approval["token_id"],
        approval_manager_id=approval["manager_id"],
        action="void_order",
        idempotency_key=idempotency_key,
        resource_id=sales_invoice,
    )
    return {
        "status": "ok",
        "sales_invoice": sales_invoice,
        "idempotency_key": idempotency_key,
        "order_status": "Cancelled",
        "invoice_status": "Cancelled",
        **proof,
    }


def _validate_completed_void_retry(
    invoice: Any, *, idempotency_key: str, request_fingerprint: str
) -> None:
    existing_key = frappe.utils.cstr(
        getattr(invoice, "custom_fb_void_idempotency_key", None)
    ).strip()
    existing_fingerprint = frappe.utils.cstr(
        getattr(invoice, "custom_fb_void_request_fingerprint", None)
    ).strip()
    if not existing_key or not existing_fingerprint:
        frappe.throw(
            _("Cancelled Sales Invoice has no verifiable KoPOS void idempotency proof"),
            frappe.ValidationError,
        )
    if existing_key != idempotency_key:
        frappe.throw(
            _("Sales Invoice was already voided with another idempotency_key"),
            frappe.ValidationError,
        )
    if existing_fingerprint != request_fingerprint:
        frappe.throw(
            _("idempotency_key was already used with a different void payload"),
            frappe.ValidationError,
        )


def _is_fb_sales_invoice(invoice: Any) -> bool:
    return bool(
        frappe.utils.cstr(getattr(invoice, "custom_fb_order", "")).strip()
        or frappe.utils.cstr(getattr(invoice, "custom_fb_idempotency_key", "")).strip()
    )


def _lock_sales_invoice_cash_shift(sales_invoice: str) -> str:
    """Lock the shift before the invoice to keep cash mutations deadlock-safe."""
    from kopos_connector.kopos.services.accounting.return_invoice_service import (
        lock_fb_shift_cash_scope,
    )

    shift_name = frappe.utils.cstr(
        frappe.db.get_value("Sales Invoice", sales_invoice, "custom_fb_shift")
    ).strip()
    if not shift_name:
        return ""
    lock_fb_shift_cash_scope(shift_name)
    return shift_name


def _apply_fb_void_side_effects(invoice: Any) -> None:
    from kopos_connector.kopos.services.accounting.return_invoice_service import (
        refresh_fb_shift_cash,
    )

    fb_order_name = frappe.utils.cstr(getattr(invoice, "custom_fb_order", "")).strip()
    shift_name = frappe.utils.cstr(getattr(invoice, "custom_fb_shift", "")).strip()
    order_doc = None
    if fb_order_name:
        order_doc = frappe.get_doc("FB Order", fb_order_name)
        shift_name = frappe.utils.cstr(getattr(order_doc, "shift", shift_name)).strip() or shift_name
        _cancel_fb_order_stock_entry(order_doc)
        _set_doc_field(order_doc, "status", "Cancelled")
        _set_doc_field(order_doc, "invoice_status", "Reversed")
        _set_doc_field(order_doc, "stock_status", "Reversed")
        _mark_fb_resolved_sales_cancelled(fb_order_name)
        _mark_fb_order_projections_reversed(order_doc)
    if shift_name:
        refresh_fb_shift_cash(shift_name)


def _cancel_fb_order_stock_entry(order_doc: Any) -> None:
    stock_entry_name = frappe.utils.cstr(
        getattr(order_doc, "ingredient_stock_entry", "")
    ).strip()
    if not stock_entry_name:
        if frappe.utils.cstr(getattr(order_doc, "stock_status", "")) == "Posted":
            frappe.throw(
                _("FB Order {0} has posted stock status but no Stock Entry").format(
                    order_doc.name
                ),
                frappe.ValidationError,
            )
        return
    stock_entry = frappe.get_doc("Stock Entry", stock_entry_name)
    if getattr(stock_entry, "docstatus", 0) == 1:
        flags = getattr(stock_entry, "flags", None)
        if flags is not None:
            flags.ignore_links = True
        stock_entry.cancel()
    elif getattr(stock_entry, "docstatus", 0) != 2:
        frappe.throw(
            _("Stock Entry {0} is not submitted or cancelled").format(stock_entry_name),
            frappe.ValidationError,
        )


def _mark_fb_resolved_sales_cancelled(fb_order_name: str) -> None:
    rows = frappe.get_all(
        "FB Resolved Sale",
        filters={"fb_order": fb_order_name},
        fields=["name"],
    )
    for row in rows or []:
        resolved_sale_name = frappe.utils.cstr(_row_value(row, "name")).strip()
        if not resolved_sale_name:
            continue
        resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sale_name)
        _set_doc_field(resolved_sale, "status", "Cancelled")


def _mark_fb_order_projections_reversed(order_doc: Any) -> None:
    rows = frappe.get_all(
        "FB Projection Log",
        filters={"source_doctype": "FB Order", "source_name": order_doc.name},
        fields=["name", "projection_type"],
    )
    for row in rows or []:
        log_name = frappe.utils.cstr(_row_value(row, "name")).strip()
        if not log_name:
            continue
        projection_type = frappe.utils.cstr(_row_value(row, "projection_type")).strip()
        log_doc = frappe.get_doc("FB Projection Log", log_name)
        _set_doc_field(log_doc, "state", "Reversed")
        if projection_type == "Sales Invoice":
            _set_doc_field(log_doc, "target_doctype", "Sales Invoice")
            _set_doc_field(log_doc, "target_name", getattr(order_doc, "sales_invoice", None))
        elif projection_type == "Stock Issue":
            _set_doc_field(log_doc, "target_doctype", "Stock Entry")
            _set_doc_field(log_doc, "target_name", getattr(order_doc, "ingredient_stock_entry", None))
        elif projection_type == "FB Shift":
            _set_doc_field(log_doc, "target_doctype", "FB Shift")
            _set_doc_field(log_doc, "target_name", getattr(order_doc, "shift", None))


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def _set_doc_field(doc: Any, fieldname: str, value: Any) -> None:
    db_set = getattr(doc, "db_set", None)
    if callable(db_set):
        db_set(fieldname, value, update_modified=False)
        return
    setattr(doc, fieldname, value)
    save = getattr(doc, "save", None)
    if callable(save):
        save(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
def process_refund(**kwargs: Any) -> None:
    """Public KoPOS endpoint for processing FB returns via Sales Invoice returns."""
    from .fb_returns import process_return_payload

    try:
        payload = _get_submit_payload(kwargs)
        lock_device_for_operational_mutation(
            device_id=frappe.utils.cstr(payload.get("device_id"))
        )
        result = process_return_payload(
            _to_public_fb_return_payload(payload),
            require_manager_approval=True,
        )
        _write_response(result)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response(_validation_error_payload(exc), http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS process_refund failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while processing refund",
            },
            http_status_code=500,
        )


def _to_public_fb_return_payload(payload: dict[str, Any]) -> dict[str, Any]:
    original_sales_invoice = frappe.utils.cstr(
        payload.get("original_sales_invoice") or payload.get("original_invoice")
    )
    fb_order = frappe.utils.cstr(payload.get("fb_order"))
    if original_sales_invoice and not fb_order:
        fb_order = frappe.utils.cstr(
            frappe.db.get_value("Sales Invoice", original_sales_invoice, "custom_fb_order")
        )
    return {
        "return_id": payload.get("return_id") or payload.get("idempotency_key"),
        "device_id": payload.get("device_id"),
        "fb_order": fb_order or None,
        "original_sales_invoice": original_sales_invoice or None,
        "reason_code": payload.get("reason_code") or "Other",
        "reason_text": payload.get("reason_text") or payload.get("refund_reason"),
        "refund_method": payload.get("refund_method"),
        "return_to_stock": payload.get("return_to_stock"),
        "lines": payload.get("lines"),
        "manager_approval_token": payload.get("manager_approval_token"),
    }


def _resolve_manager_approval_scope(payload: dict[str, Any]) -> dict[str, Any]:
    from kopos_connector.utils.manager_approval import (
        build_sales_invoice_approval_scope,
        canonical_context_hash,
        parse_integer_sen,
        validate_requested_scope,
    )

    action = frappe.utils.cstr(payload.get("action")).strip()
    if action == "void_order":
        invoice_name = frappe.utils.cstr(
            payload.get("sales_invoice") or payload.get("resource_id")
        ).strip()
        if not invoice_name:
            frappe.throw(_("sales_invoice is required"), frappe.ValidationError)
        invoice = frappe.get_doc("Sales Invoice", invoice_name)
        if frappe.utils.cint(getattr(invoice, "docstatus", 0)) != 1:
            frappe.throw(
                _("Sales Invoice {0} is not submitted").format(invoice_name),
                frappe.ValidationError,
            )
        scope = build_sales_invoice_approval_scope(
            invoice,
            context={"reason": frappe.utils.cstr(payload.get("reason")).strip()},
        )
        validate_requested_scope(payload, scope)
    elif action == "refund_order":
        from kopos_connector.api.fb_returns import build_refund_approval_scope

        scope = build_refund_approval_scope(_to_public_fb_return_payload(payload))
        validate_requested_scope(payload, scope)
    else:
        device_id = frappe.utils.cstr(payload.get("device_id")).strip()
        staff_id = frappe.utils.cstr(payload.get("staff_id")).strip()
        shift_id = frappe.utils.cstr(payload.get("shift_id")).strip()
        if not device_id:
            frappe.throw(_("device_id is required"), frappe.ValidationError)
        if not staff_id:
            frappe.throw(_("staff_id is required"), frappe.ValidationError)
        if not shift_id:
            frappe.throw(_("shift_id is required"), frappe.ValidationError)
        raw_amount = payload.get("amount_sen")
        if raw_amount is None:
            raw_amount = (
                payload.get("opening_float_sen")
                if action == "open_shift"
                else payload.get("counted_cash_sen", 0)
            )
        context_value = payload.get("context")
        context = (
            dict(context_value)
            if isinstance(context_value, Mapping)
            else {"reason": frappe.utils.cstr(payload.get("reason")).strip()}
        )
        scope = {
            "device_id": device_id,
            "staff_id": staff_id,
            "shift_id": shift_id,
            "resource_id": frappe.utils.cstr(
                payload.get("resource_id") or shift_id
            ).strip(),
            "amount_sen": parse_integer_sen(raw_amount or 0),
            "context_hash": canonical_context_hash(context),
        }
    scope["action"] = action
    return scope


@frappe.whitelist(methods=["POST"])
def request_shift_manager_approval(**kwargs: Any) -> None:
    """
    Request a server-scoped manager approval token for a privileged POS action.

    Device API sessions must supply the exact assigned manager and raw PIN.
    Only an explicit System Manager session may use admin_approval. Void and
    refund identity, shift, resource, amount, and context are derived from ERP.

    Required parameters:
        - device_id: The KoPOS device ID
        - action: The privileged action to authorize
        - manager_id / manager_pin: Device-assigned manager credentials

    Optional parameters:
        - sales_invoice and action-specific context for void/refund
        - shift_id / staff_id / amount_sen / context for shift operations
        - admin_approval: Explicit System Manager-only approval path
        - ttl_seconds: Token validity duration (default: 300 seconds / 5 minutes)

    Returns:
        - token: The approval token string
        - token_id: Unique token identifier
        - issued_at / expires_at: Token validity timestamps
        - action, device_id, staff_id, shift_id, resource_id, amount_sen,
          context_hash: Exact server-derived scope bound into the token
    """
    from kopos_connector.utils.manager_approval import (
        authorize_manager_for_device,
        generate_manager_approval_token,
    )

    try:
        payload = _get_submit_payload(kwargs)
        requested_device_id = frappe.utils.cstr(payload.get("device_id")).strip()
        device_doc = lock_device_for_operational_mutation(
            device_id=requested_device_id
        )
        scope = _resolve_manager_approval_scope(payload)
        if frappe.utils.cstr(getattr(device_doc, "device_id", None)).strip() != scope[
            "device_id"
        ]:
            frappe.throw(
                _("Manager approval scope belongs to another KoPOS Device"),
                frappe.ValidationError,
            )
        admin_approval = bool(frappe.utils.cint(payload.get("admin_approval")))
        manager_id = authorize_manager_for_device(
            device_doc,
            manager_id=frappe.utils.cstr(payload.get("manager_id")).strip() or None,
            manager_pin=frappe.utils.cstr(
                payload.get("manager_pin") or payload.get("pin")
            ).strip()
            or None,
            admin_approval=admin_approval,
            action=scope["action"],
        )

        result = generate_manager_approval_token(
            device_id=scope["device_id"],
            staff_id=scope["staff_id"],
            action=scope["action"],
            manager_id=manager_id,
            shift_id=scope["shift_id"],
            resource_id=scope["resource_id"],
            amount_sen=scope["amount_sen"],
            context_hash=scope["context_hash"],
            ttl_seconds=payload.get("ttl_seconds"),
            authorization_mode=(
                "system_manager" if admin_approval else "device_manager"
            ),
        )

        _write_response(
            {
                "status": "ok",
                **result,
            }
        )
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS request_shift_manager_approval failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while requesting manager approval",
            },
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def generate_maybank_qr(**kwargs: Any) -> None:
    """Generate a Maybank DuitNow QR code for POS payment."""
    from .maybank_qr import generate_maybank_qr_payload

    try:
        payload = _get_submit_payload(kwargs)
        device_doc, _profile_doc = require_device_operational_scope(
            device_id=frappe.utils.cstr(payload.get("device_id")),
            currency="MYR",
        )
        authority = _capture_maybank_device_authority(device_doc)
        payload["currency"] = "MYR"
        result = generate_maybank_qr_payload(payload)
        # Provider evidence is committed before acquiring the device fence. This
        # keeps the irreversible network call outside the broad device lock while
        # still denying a response if credentials/config changed in flight.
        frappe.db.commit()
        _revalidate_maybank_device_authority(authority)
        _write_response(result)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS generate_maybank_qr failed", error)
        _write_response(
            {"status": "error", "message": "Failed to generate QR code"},
            http_status_code=500,
        )


@frappe.whitelist()
def check_maybank_payment(
    transaction_refno: str | None = None, device_id: str | None = None
) -> None:
    """Check payment status of a Maybank QR transaction."""
    from .maybank_qr import check_maybank_payment_payload

    try:
        require_kopos_api_access()
        resolved_device_id = frappe.utils.cstr(device_id).strip() or None
        if resolved_device_id:
            device, _profile_doc = require_device_operational_scope(
                resolved_device_id,
                currency="MYR",
            )
            resolved_device_id = frappe.utils.cstr(getattr(device, "device_id", ""))
        else:
            device = get_authenticated_device_doc()
            resolved_device_id = frappe.utils.cstr(getattr(device, "device_id", ""))
            device, _profile_doc = require_device_operational_scope(
                resolved_device_id,
                currency="MYR",
            )
        authority = _capture_maybank_device_authority(device)
        result = check_maybank_payment_payload(
            transaction_refno=frappe.utils.cstr(transaction_refno),
            device_id=resolved_device_id,
        )
        frappe.db.commit()
        _revalidate_maybank_device_authority(authority)
        # This endpoint remains GET-compatible for deployed tablets. Tell Frappe to
        # commit the validated on-demand poll writes at request completion.
        frappe.flags.commit = True
        _write_response(result)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS check_maybank_payment failed", error)
        _write_response(
            {"status": "error", "message": "Failed to check payment status"},
            http_status_code=500,
        )


def _capture_maybank_device_authority(device_doc: Any) -> dict[str, Any]:
    return {
        "name": frappe.utils.cstr(getattr(device_doc, "name", None)).strip(),
        "device_id": frappe.utils.cstr(
            getattr(device_doc, "device_id", None)
        ).strip(),
        "config_version": frappe.utils.cint(
            getattr(device_doc, "config_version", 0)
        ),
    }


def _revalidate_maybank_device_authority(authority: Mapping[str, Any]) -> None:
    locked_device = lock_device_for_operational_mutation(
        device_id=frappe.utils.cstr(authority.get("device_id"))
    )
    if (
        frappe.utils.cstr(getattr(locked_device, "name", None)).strip()
        != frappe.utils.cstr(authority.get("name")).strip()
        or frappe.utils.cint(getattr(locked_device, "config_version", 0))
        != frappe.utils.cint(authority.get("config_version"))
    ):
        frappe.throw(
            _(
                "KoPOS Device authority changed while the Maybank request was in flight; authenticate again"
            ),
            frappe.ValidationError,
        )


@frappe.whitelist(methods=["POST"])
def resolve_maybank_qr_generation(**kwargs: Any) -> None:
    """Resolve an ambiguous provider generation using audited System Manager evidence."""
    from .maybank_qr import resolve_maybank_qr_generation_payload

    try:
        require_system_manager()
        if KOPOS_DEVICE_API_ROLE in get_session_roles():
            frappe.throw(
                _(
                    "Maybank QR generation resolution requires a non-device System Manager session"
                ),
                frappe.ValidationError,
            )
        payload = _get_submit_payload(kwargs)
        _write_response(resolve_maybank_qr_generation_payload(payload))
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response(_validation_error_payload(exc), http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS Maybank QR generation resolution failed", error)
        _write_response(
            {
                "status": "error",
                "message": "Failed to resolve ambiguous Maybank QR generation",
            },
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def resolve_duplicate_automatic_qr_refund(**kwargs: Any) -> None:
    """Record an exact provider refund for an accounted duplicate QR payment."""
    from .duplicate_qr_payment import (
        resolve_duplicate_automatic_qr_refund_payload,
    )

    try:
        require_system_manager()
        if KOPOS_DEVICE_API_ROLE in get_session_roles():
            frappe.throw(
                _(
                    "Duplicate Automatic QR refund resolution requires a non-device System Manager session"
                ),
                frappe.ValidationError,
            )
        payload = _get_submit_payload(kwargs)
        _write_response(resolve_duplicate_automatic_qr_refund_payload(payload))
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response(_validation_error_payload(exc), http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error(
            "KoPOS duplicate Automatic QR refund resolution failed",
            error,
        )
        _write_response(
            {
                "status": "error",
                "message": "Failed to resolve duplicate Automatic QR refund",
            },
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def upload_manual_qr_receipt(**kwargs: Any) -> None:
    """Attach a validated private receipt JPEG to a Maybank QR transaction."""
    from .manual_qr_receipt import upload_manual_qr_receipt as upload_payload

    try:
        _write_response(upload_payload(**kwargs))
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception as error:
        frappe.db.rollback()
        log_sanitized_error("KoPOS upload_manual_qr_receipt failed", error)
        _write_response(
            {"status": "error", "message": "Failed to upload manual QR receipt"},
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def fetch_manual_qr_reconciliation_status(**kwargs: Any) -> None:
    """Return manual Maybank QR reconciliation statuses for submitted payments."""
    from .manual_qr_receipt import (
        fetch_manual_qr_reconciliation_status as fetch_status_payload,
    )

    try:
        _write_response(fetch_status_payload(**kwargs))
    except frappe.ValidationError as exc:
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception as error:
        log_sanitized_error(
            "KoPOS fetch_manual_qr_reconciliation_status failed", error
        )
        _write_response(
            {
                "status": "error",
                "message": "Failed to fetch manual QR reconciliation status",
            },
            http_status_code=500,
        )


__all__ = [
    "abandon_unregistered_device_safe_reset_request",
    "authorize_device_safe_reset",
    "cancel_device_safe_reset",
    "cancel_device_safe_reset_as_system_manager",
    "check_maybank_payment",
    "close_shift",
    "complete_device_safe_reset",
    "create_device_provisioning_qr",
    "create_pos_provisioning",
    "fetch_manual_qr_reconciliation_status",
    "generate_maybank_qr",
    "get_catalog",
    "get_device_config",
    "get_item_modifiers",
    "get_order_history",
    "get_promotion_review_queue",
    "get_promotion_snapshot",
    "get_refund_reasons",
    "get_tax_rate",
    "open_shift",
    "ping",
    "prepare_automatic_qr_sale",
    "process_refund",
    "publish_promotion_snapshot",
    "redeem_pos_provisioning",
    "register_device_credential_recovery",
    "request_device_safe_reset",
    "resolve_device_safe_reset_request",
    "request_shift_manager_approval",
    "resolve_duplicate_automatic_qr_refund",
    "resolve_maybank_qr_generation",
    "review_promotion_reconciliation",
    "submit_order",
    "upload_manual_qr_receipt",
    "void_order",
]
