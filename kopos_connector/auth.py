from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, cstr

from kopos_connector.api.devices import KOPOS_DEVICE_API_ROLE, get_session_roles


ALLOWED_DEVICE_API_PATHS = frozenset(
    {
        "/api/method/kopos_connector.api.ping",
        "/api/method/kopos_connector.api.get_catalog",
        "/api/method/kopos_connector.api.get_tax_rate",
        "/api/method/kopos_connector.api.get_item_modifiers",
        "/api/method/kopos_connector.api.get_refund_reasons",
        "/api/method/kopos_connector.api.get_promotion_snapshot",
        "/api/method/kopos_connector.api.get_device_config",
        "/api/method/kopos_connector.api.request_device_safe_reset",
        (
            "/api/method/kopos_connector.api."
            "abandon_unregistered_device_safe_reset_request"
        ),
        "/api/method/kopos_connector.api.resolve_device_safe_reset_request",
        "/api/method/kopos_connector.api.cancel_device_safe_reset",
        "/api/method/kopos_connector.api.complete_device_safe_reset",
        "/api/method/kopos_connector.api.prepare_automatic_qr_sale",
        "/api/method/kopos_connector.api.cancel_prepared_automatic_qr_sale",
        (
            "/api/method/kopos_connector.api."
            "confirm_prepared_automatic_qr_static_payment"
        ),
        "/api/method/kopos_connector.api.submit_order",
        "/api/method/kopos_connector.api.open_shift",
        "/api/method/kopos_connector.api.close_shift",
        "/api/method/kopos_connector.api.get_device_open_shift",
        "/api/method/kopos_connector.api.get_order_history",
        "/api/method/kopos_connector.api.void_order",
        "/api/method/kopos_connector.api.process_refund",
        "/api/method/kopos_connector.api.request_shift_manager_approval",
        "/api/method/kopos_connector.api.generate_maybank_qr",
        "/api/method/kopos_connector.api.get_maybank_qr_readiness",
        "/api/method/kopos_connector.api.get_payment_readiness",
        "/api/method/kopos_connector.api.get_qr_setup_preview",
        "/api/method/kopos_connector.api.apply_qr_configuration",
        "/api/method/kopos_connector.api.check_maybank_payment",
        "/api/method/kopos_connector.api.upload_manual_qr_receipt",
        "/api/method/kopos_connector.api.fetch_manual_qr_reconciliation_status",
        "/api/method/kopos_connector.api.fb_orders.get_order_status",
        "/api/method/kopos_connector.api.fb_orders.retry_failed_projections",
        "/api/method/kopos_connector.api.get_edge_snapshot",
        "/api/method/kopos_connector.api.get_count_task",
        "/api/method/kopos_connector.api.get_inventory_tasks",
        "/api/method/kopos_connector.api.create_availability_hold",
        "/api/method/kopos_connector.api.release_availability_hold",
        "/api/method/kopos_connector.api.report_device_inventory_state",
        "/api/method/kopos_connector.api.submit_count_observation",
        "/api/method/kopos_connector.api.accept_preparation_task",
        "/api/method/kopos_connector.api.start_preparation_task",
        "/api/method/kopos_connector.api.complete_preparation_task",
        "/api/method/kopos_connector.api.submit_purchase_receipt",
        "/api/method/kopos_connector.api.submit_transfer_dispatch",
        "/api/method/kopos_connector.api.submit_transfer_receipt",
    }
)

DEVICE_API_HTTP_METHODS = {
    path: frozenset({"GET"})
    for path in ALLOWED_DEVICE_API_PATHS
}
DEVICE_API_HTTP_METHODS.update(
    {
        "/api/method/kopos_connector.api.prepare_automatic_qr_sale": frozenset(
            {"POST"}
        ),
        "/api/method/kopos_connector.api.cancel_prepared_automatic_qr_sale": frozenset(
            {"POST"}
        ),
        (
            "/api/method/kopos_connector.api."
            "confirm_prepared_automatic_qr_static_payment"
        ): frozenset({"POST"}),
        "/api/method/kopos_connector.api.submit_order": frozenset({"POST"}),
        "/api/method/kopos_connector.api.apply_qr_configuration": frozenset({"POST"}),
        "/api/method/kopos_connector.api.request_device_safe_reset": frozenset(
            {"POST"}
        ),
        (
            "/api/method/kopos_connector.api."
            "abandon_unregistered_device_safe_reset_request"
        ): frozenset({"POST"}),
        "/api/method/kopos_connector.api.resolve_device_safe_reset_request": frozenset(
            {"POST"}
        ),
        "/api/method/kopos_connector.api.cancel_device_safe_reset": frozenset(
            {"POST"}
        ),
        "/api/method/kopos_connector.api.complete_device_safe_reset": frozenset(
            {"POST"}
        ),
        "/api/method/kopos_connector.api.open_shift": frozenset({"POST"}),
        "/api/method/kopos_connector.api.close_shift": frozenset({"POST"}),
        "/api/method/kopos_connector.api.get_order_history": frozenset({"POST"}),
        "/api/method/kopos_connector.api.void_order": frozenset({"POST"}),
        "/api/method/kopos_connector.api.process_refund": frozenset({"POST"}),
        "/api/method/kopos_connector.api.request_shift_manager_approval": frozenset(
            {"POST"}
        ),
        "/api/method/kopos_connector.api.generate_maybank_qr": frozenset({"POST"}),
        "/api/method/kopos_connector.api.upload_manual_qr_receipt": frozenset(
            {"POST"}
        ),
        "/api/method/kopos_connector.api.fetch_manual_qr_reconciliation_status": frozenset(
            {"POST"}
        ),
        "/api/method/kopos_connector.api.fb_orders.retry_failed_projections": frozenset(
            {"POST"}
        ),
        "/api/method/kopos_connector.api.create_availability_hold": frozenset({"POST"}),
        "/api/method/kopos_connector.api.release_availability_hold": frozenset({"POST"}),
        "/api/method/kopos_connector.api.report_device_inventory_state": frozenset({"POST"}),
        "/api/method/kopos_connector.api.submit_count_observation": frozenset({"POST"}),
        "/api/method/kopos_connector.api.accept_preparation_task": frozenset({"POST"}),
        "/api/method/kopos_connector.api.start_preparation_task": frozenset({"POST"}),
        "/api/method/kopos_connector.api.complete_preparation_task": frozenset({"POST"}),
        "/api/method/kopos_connector.api.submit_purchase_receipt": frozenset({"POST"}),
        "/api/method/kopos_connector.api.submit_transfer_dispatch": frozenset({"POST"}),
        "/api/method/kopos_connector.api.submit_transfer_receipt": frozenset({"POST"}),
    }
)

DEFAULT_DEVICE_API_MAX_BODY_BYTES = 256 * 1024
DEVICE_API_MAX_BODY_BYTES = {
    "/api/method/kopos_connector.api.prepare_automatic_qr_sale": 512 * 1024,
    "/api/method/kopos_connector.api.submit_order": 512 * 1024,
    "/api/method/kopos_connector.api.upload_manual_qr_receipt": 6 * 1024 * 1024,
}


def enforce_device_api_restrictions() -> None:
    user = cstr(getattr(frappe.session, "user", None)).strip()
    if not user or user == "Guest":
        return

    roles = get_session_roles(user=user)
    if KOPOS_DEVICE_API_ROLE not in roles:
        return

    request = getattr(frappe.local, "request", None)
    path = cstr(getattr(request, "path", None)).strip().rstrip("/")
    method = cstr(getattr(request, "method", None)).strip().upper()
    if path in ALLOWED_DEVICE_API_PATHS and method in DEVICE_API_HTTP_METHODS[path]:
        content_length = cint(getattr(request, "content_length", 0))
        max_body_bytes = DEVICE_API_MAX_BODY_BYTES.get(
            path, DEFAULT_DEVICE_API_MAX_BODY_BYTES
        )
        if method == "POST" and content_length > max_body_bytes:
            frappe.throw(
                _("KoPOS device request body exceeds the endpoint limit"),
                frappe.ValidationError,
            )
        return

    frappe.throw(
        _("KoPOS device API users may only access approved KoPOS device endpoints"),
        frappe.ValidationError,
    )
