# pyright: reportMissingImports=false

from __future__ import annotations

import frappe
from frappe.utils import cstr

from kopos_connector.api.devices import (
    lock_device_for_operational_mutation,
    require_device_context,
    require_device_operational_scope,
)
from kopos_connector.kopos.api import fb_orders as fb_orders_impl


@frappe.whitelist(methods=["POST"])
def submit_order():
    payload = fb_orders_impl._get_request_payload()
    lock_device_for_operational_mutation(device_id=cstr(payload.get("device_id")))
    require_device_operational_scope(
        cstr(payload.get("device_id")),
        company=cstr(payload.get("company")),
        warehouse=cstr(payload.get("booth_warehouse") or payload.get("warehouse")),
        currency=cstr(payload.get("currency")),
    )
    return fb_orders_impl.submit_order_payload(payload)


@frappe.whitelist()
def get_order_status(fb_order_name: str):
    order_doc = frappe.get_doc("FB Order", fb_order_name)
    require_device_context(device_id=cstr(getattr(order_doc, "device_id", None)))
    return fb_orders_impl.get_order_status(fb_order_name)


@frappe.whitelist(methods=["POST"])
def retry_failed_projections(fb_order_name: str):
    order_doc = frappe.get_doc("FB Order", fb_order_name)
    lock_device_for_operational_mutation(
        device_id=cstr(getattr(order_doc, "device_id", None))
    )
    return fb_orders_impl.retry_failed_projections(fb_order_name)
