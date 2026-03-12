from __future__ import annotations

from typing import Any

import frappe

from .catalog import (
    build_catalog_payload,
    get_item_modifiers_payload,
    get_tax_rate_value,
)
from .devices import (
    mark_device_seen,
    require_device_context,
    require_kopos_api_access,
    require_system_manager,
)
from .promotions import get_promotion_snapshot_payload
from .provisioning import (
    create_device_provisioning_qr as create_device_provisioning_qr_payload,
    create_pos_provisioning as create_pos_provisioning_payload,
    get_device_config as get_device_config_payload,
    redeem_pos_provisioning as redeem_pos_provisioning_payload,
)


def _write_response(payload: dict[str, Any], http_status_code: int = 200) -> None:
    frappe.local.response.update(payload)
    frappe.local.response["http_status_code"] = http_status_code
    for key in ("_server_messages", "exc", "_debug_messages", "exception"):
        frappe.local.response.pop(key, None)


@frappe.whitelist(allow_guest=True)
def ping() -> None:
    """Simple health endpoint for KoPOS setup validation."""
    _write_response({"message": "KoPOS ERPNext API ready"})


@frappe.whitelist()
def get_catalog(since: str | None = None, device_id: str | None = None) -> None:
    """Public KoPOS endpoint for catalog sync."""
    try:
        require_device_context(device_id=device_id)
        if device_id:
            mark_device_seen(device_id=device_id)
        _write_response(build_catalog_payload(since=since, device_id=device_id))
    except Exception:
        frappe.log_error(frappe.get_traceback(), "KoPOS get_catalog failed")
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
def get_item_modifiers(item_code: str) -> None:
    """Public KoPOS endpoint returning modifiers for a single item."""
    require_kopos_api_access()
    _write_response({"modifier_groups": get_item_modifiers_payload(item_code)})


@frappe.whitelist()
def get_refund_reasons() -> None:
    """Return supported refund reason presets for KoPOS clients."""
    require_kopos_api_access()
    from .orders import get_refund_reason_choices

    _write_response({"refund_reasons": get_refund_reason_choices()})


@frappe.whitelist()
def get_promotion_snapshot(
    pos_profile: str | None = None,
    current_version: str | None = None,
    device_id: str | None = None,
) -> None:
    """Return the latest KoPOS promotion snapshot for a POS profile."""
    require_device_context(device_id=device_id)
    if device_id:
        mark_device_seen(device_id=device_id)
    _write_response(
        get_promotion_snapshot_payload(
            pos_profile=pos_profile,
            current_version=current_version,
            device_id=device_id,
        )
    )


@frappe.whitelist(methods=["POST"])
def create_device_provisioning_qr(**kwargs: Any) -> None:
    """Create a one-click KoPOS provisioning QR using dedicated per-device credentials."""
    try:
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


@frappe.whitelist(allow_guest=True)
def redeem_pos_provisioning(token: str | None = None, **kwargs: Any) -> None:
    """Redeem a one-time KoPOS provisioning link from a QR/deep link."""
    try:
        payload = _get_submit_payload(kwargs)
        token_value = token or payload.get("token")
        _write_response(
            redeem_pos_provisioning_payload(token=frappe.utils.cstr(token_value))
        )
    except frappe.ValidationError as exc:
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)


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
    """Return POS invoices that need promotion reconciliation review."""
    from .promotions import get_promotion_review_queue as queue

    require_system_manager()
    _write_response({"items": queue(limit=int(limit))})


@frappe.whitelist(methods=["POST"])
def review_promotion_reconciliation(**kwargs: Any) -> None:
    """Resolve a review-required promotion reconciliation item."""
    from .promotions import review_promotion_reconciliation as review

    try:
        require_system_manager()
        payload = _get_submit_payload(kwargs)
        _write_response(
            review(
                pos_invoice=frappe.utils.cstr(payload.get("pos_invoice")),
                decision=frappe.utils.cstr(payload.get("decision")),
                notes=frappe.utils.cstr(payload.get("notes")),
                reviewed_by=frappe.utils.cstr(payload.get("reviewed_by")),
            )
        )
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(
            frappe.get_traceback(), "KoPOS review_promotion_reconciliation failed"
        )
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while reviewing promotion reconciliation",
            },
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def submit_order(**kwargs: Any) -> None:
    """Public KoPOS endpoint for order submission with raw JSON responses."""
    from .orders import submit_order_payload

    try:
        payload = _get_submit_payload(kwargs)
        require_device_context(device_id=frappe.utils.cstr(payload.get("device_id")))
        result = submit_order_payload(payload)
        _write_response(result)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "KoPOS submit_order failed")
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while submitting order",
            },
            http_status_code=500,
        )


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
def process_refund(**kwargs: Any) -> None:
    """Public KoPOS endpoint for processing refunds via Credit Notes."""
    from .orders import process_refund_payload

    try:
        payload = _get_submit_payload(kwargs)
        require_device_context(device_id=frappe.utils.cstr(payload.get("device_id")))
        result = process_refund_payload(payload)
        _write_response(result)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "KoPOS process_refund failed")
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while processing refund",
            },
            http_status_code=500,
        )


__all__ = [
    "create_device_provisioning_qr",
    "create_pos_provisioning",
    "get_catalog",
    "get_device_config",
    "get_item_modifiers",
    "get_promotion_review_queue",
    "get_promotion_snapshot",
    "get_refund_reasons",
    "get_tax_rate",
    "ping",
    "process_refund",
    "publish_promotion_snapshot",
    "redeem_pos_provisioning",
    "review_promotion_reconciliation",
    "submit_order",
]
