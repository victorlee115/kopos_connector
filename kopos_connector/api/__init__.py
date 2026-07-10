# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _

from .catalog import (
    build_catalog_payload,
    get_item_modifiers_payload,
    get_tax_rate_value,
)
from .devices import (
    elevate_device_api_user,
    get_authenticated_device_doc,
    mark_device_seen,
    require_device_context,
    require_device_operational_scope,
    require_kopos_api_access,
    require_system_manager,
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
        with elevate_device_api_user():
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
    with elevate_device_api_user():
        _write_response({"modifier_groups": get_item_modifiers_payload(item_code)})


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
                }
            )
        else:
            _write_response(payload)
    except frappe.ValidationError as exc:
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "KoPOS get_promotion_snapshot failed")
        _write_response(
            {"status": "error", "message": "Failed to fetch promotion snapshot"},
            http_status_code=500,
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
    """Public KoPOS endpoint for FB Order submission with raw JSON responses."""
    from kopos_connector.kopos.api.fb_orders import submit_order_payload

    try:
        payload = _get_submit_payload(kwargs)
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
        require_device_context(device_id=frappe.utils.cstr(payload.get("device_id")))
        result = open_shift_payload(payload)
        _write_response(result)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "KoPOS open_shift failed")
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
        require_device_context(device_id=frappe.utils.cstr(payload.get("device_id")))
        result = close_shift_payload(payload)
        _write_response(result)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "KoPOS close_shift failed")
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
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "KoPOS get_device_open_shift failed")
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
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
        return {"status": "error", "message": str(exc)}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "KoPOS get_order_history failed")
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
        require_device_context(device_id=frappe.utils.cstr(payload.get("device_id")))
        result = _process_sales_invoice_void_payload(payload)
        _write_response(result)
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "KoPOS void_order failed")
        _write_response(
            {
                "status": "error",
                "message": "Unexpected server error while voiding order",
            },
            http_status_code=500,
        )


def _process_sales_invoice_void_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sales_invoice = frappe.utils.cstr(payload.get("sales_invoice")).strip()
    device_id = frappe.utils.cstr(payload.get("device_id")).strip()
    idempotency_key = frappe.utils.cstr(payload.get("idempotency_key")).strip()
    reason = frappe.utils.cstr(payload.get("reason")).strip()
    if not sales_invoice:
        frappe.throw(_("sales_invoice is required"), frappe.ValidationError)
    if not device_id:
        frappe.throw(_("device_id is required"), frappe.ValidationError)
    if not idempotency_key:
        frappe.throw(_("idempotency_key is required"), frappe.ValidationError)
    invoice = frappe.get_doc("Sales Invoice", sales_invoice)
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
    if invoice.docstatus == 2:
        _apply_fb_void_side_effects(invoice)
        return {
            "status": "duplicate",
            "sales_invoice": sales_invoice,
            "idempotency_key": idempotency_key,
            "order_status": "Cancelled",
            "invoice_status": "Cancelled",
        }
    if invoice.docstatus != 1:
        frappe.throw(_("Sales Invoice {0} is not submitted").format(sales_invoice), frappe.ValidationError)
    with elevate_device_api_user():
        if reason:
            invoice.add_comment("Comment", f"KoPOS void reason: {reason}")
        flags = getattr(invoice, "flags", None)
        if flags is not None:
            flags.ignore_links = True
        invoice.cancel()
        _apply_fb_void_side_effects(invoice)
    return {
        "status": "ok",
        "sales_invoice": sales_invoice,
        "idempotency_key": idempotency_key,
        "order_status": "Cancelled",
        "invoice_status": "Cancelled",
    }


def _is_fb_sales_invoice(invoice: Any) -> bool:
    return bool(
        frappe.utils.cstr(getattr(invoice, "custom_fb_order", "")).strip()
        or frappe.utils.cstr(getattr(invoice, "custom_fb_idempotency_key", "")).strip()
    )


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
        require_device_context(device_id=frappe.utils.cstr(payload.get("device_id")))
        result = process_return_payload(_to_public_fb_return_payload(payload))
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
        "return_to_stock": payload.get("return_to_stock"),
        "lines": payload.get("lines"),
    }


@frappe.whitelist(methods=["POST"])
def request_shift_manager_approval(**kwargs: Any) -> None:
    """
    Request a manager approval token for shift operations.

    This endpoint requires manager credentials or a server-verified manager session.
    It returns a short-lived signed token that can be used to authorize
    privileged shift actions (open_shift, close_shift, reopen_shift).

    Required parameters:
        - device_id: The KoPOS device ID
        - staff_id: The staff user ID performing the action
        - action: The action to authorize (open_shift, close_shift, reopen_shift)

    Optional parameters:
        - shift_id: The shift ID (required for close_shift and reopen_shift)
        - ttl_seconds: Token validity duration (default: 300 seconds / 5 minutes)

    Returns:
        - token: The approval token string
        - token_id: Unique token identifier
        - issued_at: Unix timestamp when token was issued
        - expires_at: Unix timestamp when token expires
    """
    from kopos_connector.utils.manager_approval import (
        generate_manager_approval_token,
    )

    try:
        payload = _get_submit_payload(kwargs)

        # Require authenticated session (not guest)
        session_user = frappe.utils.cstr(getattr(frappe.session, "user", None)).strip()
        if not session_user or session_user == "Guest":
            frappe.throw(
                _("Authentication required for manager approval"),
                frappe.ValidationError,
            )

        # Verify the requesting user has manager privileges
        # This can be via System Manager role or can_manager_override on device
        device_id = frappe.utils.cstr(payload.get("device_id"))
        if device_id:
            device_doc = require_device_context(device_id=device_id)

            # Check if user is System Manager
            roles = (
                frappe.get_roles(session_user) if hasattr(frappe, "get_roles") else []
            )
            is_system_manager = "System Manager" in roles

            # Check if user has can_manager_override on this device
            is_device_manager = False
            device_users = getattr(device_doc, "device_users", None)
            for user_row in device_users or []:
                if frappe.utils.cstr(getattr(user_row, "user", "")) == session_user:
                    if frappe.utils.cint(getattr(user_row, "can_manager_override", 0)):
                        is_device_manager = True
                        break

            if not is_system_manager and not is_device_manager:
                frappe.throw(
                    _("User {0} is not authorized to approve shift operations").format(
                        session_user
                    ),
                    frappe.ValidationError,
                )
        else:
            # Without device_id, require System Manager role
            require_system_manager()

        result = generate_manager_approval_token(
            device_id=device_id,
            staff_id=frappe.utils.cstr(payload.get("staff_id")),
            action=frappe.utils.cstr(payload.get("action")),
            manager_id=session_user,
            shift_id=frappe.utils.cstr(payload.get("shift_id")) or None,
            ttl_seconds=payload.get("ttl_seconds"),
        )

        _write_response(
            {
                "status": "ok",
                **result,
            }
        )
    except frappe.ValidationError as exc:
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), "KoPOS request_shift_manager_approval failed"
        )
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
        require_device_context(device_id=frappe.utils.cstr(payload.get("device_id")))
        _write_response(generate_maybank_qr_payload(payload))
    except frappe.ValidationError as exc:
        frappe.db.rollback()
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "KoPOS generate_maybank_qr failed")
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
            device = require_device_context(device_id=resolved_device_id)
            resolved_device_id = frappe.utils.cstr(getattr(device, "device_id", ""))
        else:
            device = get_authenticated_device_doc()
            resolved_device_id = frappe.utils.cstr(getattr(device, "device_id", ""))
        _write_response(
            check_maybank_payment_payload(
                transaction_refno=frappe.utils.cstr(transaction_refno),
                device_id=resolved_device_id,
            )
        )
    except frappe.ValidationError as exc:
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "KoPOS check_maybank_payment failed")
        _write_response(
            {"status": "error", "message": "Failed to check payment status"},
            http_status_code=500,
        )


@frappe.whitelist(methods=["POST"])
def upload_manual_qr_receipt(**kwargs: Any) -> None:
    """Attach a validated private receipt JPEG to a Maybank QR transaction."""
    from .manual_qr_receipt import upload_manual_qr_receipt as upload_payload

    try:
        _write_response(upload_payload(**kwargs))
    except frappe.ValidationError as exc:
        _write_response({"status": "error", "message": str(exc)}, http_status_code=400)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "KoPOS upload_manual_qr_receipt failed")
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
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), "KoPOS fetch_manual_qr_reconciliation_status failed"
        )
        _write_response(
            {
                "status": "error",
                "message": "Failed to fetch manual QR reconciliation status",
            },
            http_status_code=500,
        )


__all__ = [
    "check_maybank_payment",
    "close_shift",
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
    "process_refund",
    "publish_promotion_snapshot",
    "redeem_pos_provisioning",
    "request_shift_manager_approval",
    "review_promotion_reconciliation",
    "submit_order",
    "upload_manual_qr_receipt",
    "void_order",
]
