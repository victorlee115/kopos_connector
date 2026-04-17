from __future__ import annotations

import frappe

from kopos_connector.kopos.api import fb_orders as fb_orders_impl


@frappe.whitelist()
def submit_order():
    return fb_orders_impl.submit_order()


@frappe.whitelist()
def get_order_status(fb_order_name: str):
    return fb_orders_impl.get_order_status(fb_order_name)


@frappe.whitelist()
def retry_failed_projections(fb_order_name: str):
    return fb_orders_impl.retry_failed_projections(fb_order_name)
