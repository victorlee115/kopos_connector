from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

install_fake_frappe_modules()

fb_orders = importlib.import_module("kopos_connector.kopos.api.fb_orders")
sales_invoice_service = importlib.import_module(
    "kopos_connector.kopos.services.accounting.sales_invoice_service"
)


class FakeOrder:
    def __init__(
        self,
        *,
        invoice_status: str,
        stock_status: str,
        sales_invoice: str | None,
    ) -> None:
        self.doctype = "FB Order"
        self.name = "FB-ORDER-1"
        self.order_id = "order-1"
        self.external_idempotency_key = "idem-1"
        self.request_fingerprint = "f" * 64
        self.status = "Draft"
        self.invoice_status = invoice_status
        self.stock_status = stock_status
        self.sales_invoice = sales_invoice
        self.ingredient_stock_entry = "STE-1"
        self.insert_count = 0
        self.submit_count = 0

    def insert(self, ignore_permissions: bool = False) -> "FakeOrder":
        self.insert_count += 1
        return self

    def submit(self) -> "FakeOrder":
        self.submit_count += 1
        self.status = "Submitted"
        return self


class FakeProjectionLog:
    def __init__(self, projection_type: str) -> None:
        self.doctype = "FB Projection Log"
        self.name = f"LOG-{projection_type.upper().replace(' ', '-')}"
        self.source_doctype = "FB Order"
        self.source_name = "FB-ORDER-1"
        self.projection_type = projection_type
        self.retry_count = 0
        self.last_attempt_at = None
        self.saved = False
        self.state = "Failed"
        self.target_name: str | None = None
        self.last_error: str | None = "previous failure"

    def save(self, ignore_permissions: bool = False) -> None:
        self.saved = True


class FakeRetryOrder:
    def __init__(self) -> None:
        self.doctype = "FB Order"
        self.name = "FB-ORDER-1"
        self.order_id = "order-1"
        self.external_idempotency_key = "idem-1"
        self.shift = "FB-SHIFT-1"
        self.invoice_status = "Failed"
        self.stock_status = "Posted"
        self.sales_invoice = None
        self.ingredient_stock_entry = None
        self.db_set_calls: list[tuple[str, str, bool]] = []
        self.reload_count = 0

    def db_set(self, fieldname: str, value: str, update_modified: bool = False) -> None:
        self.db_set_calls.append((fieldname, value, update_modified))
        setattr(self, fieldname, value)

    def reload(self) -> None:
        self.reload_count += 1

    def get_resolved_sales(self) -> list[Any]:
        return []

    def requires_stock_projection(self, resolved_sales: list[Any]) -> bool:
        return bool(resolved_sales)


class FakeRelinkOrder:
    def __init__(self) -> None:
        self.doctype = "FB Order"
        self.name = "FB-ORDER-1"
        self.external_idempotency_key = "idem-1"
        self.shift = "FB-SHIFT-1"
        self.device_id = "DEVICE-1"
        self.company = "JiJi Cafe"
        self.currency = "MYR"
        self.net_total = "12.00"
        self.tax_total = "0.00"
        self.rounding_adjustment = "0.00"
        self.grand_total = "12.00"
        self.booth_warehouse = "Main - JC"
        self.sales_invoice = None
        self.invoice_status = "Failed"
        self.items: list[Any] = [
            {
                "line_id": "LINE-1",
                "item": "LATTE",
                "qty": 1,
                "line_total": "12.00",
                "modifier_total": "0.00",
            }
        ]
        self.payments: list[Any] = [
            {
                "source_payment_id": "PAY-1",
                "payment_method": "Cash",
                "amount": "12.00",
                "tendered_amount": "12.00",
                "change_amount": "0.00",
            }
        ]
        self.db_set_calls: list[tuple[str, str, bool]] = []

    def get(self, fieldname: str) -> Any:
        return getattr(self, fieldname, None)

    def db_set(self, fieldname: str, value: str, update_modified: bool = True) -> None:
        self.db_set_calls.append((fieldname, value, update_modified))
        setattr(self, fieldname, value)


def failed_invoice_projection() -> list[dict[str, Any]]:
    return [
        {
            "projection_log": "LOG-INV-1",
            "projection_type": "Sales Invoice",
            "state": "Failed",
            "target_doctype": "Sales Invoice",
            "target_name": None,
            "idempotency_key": "FB-ORDER-1:Sales Invoice",
            "retry_count": 0,
            "last_error": "forced Sales Invoice projection failure",
            "last_attempt_at": "2026-05-16T10:00:00",
        }
    ]


def succeeded_projections() -> list[dict[str, Any]]:
    return [
        {
            "projection_log": "LOG-INV-1",
            "projection_type": "Sales Invoice",
            "state": "Succeeded",
            "target_doctype": "Sales Invoice",
            "target_name": "SINV-1",
            "idempotency_key": "FB-ORDER-1:Sales Invoice",
            "retry_count": 0,
            "last_error": None,
            "last_attempt_at": "2026-05-16T10:00:00",
        },
        {
            "projection_log": "LOG-STOCK-1",
            "projection_type": "Stock Issue",
            "state": "Succeeded",
            "target_doctype": "Stock Entry",
            "target_name": "STE-1",
            "idempotency_key": "FB-ORDER-1:Stock Issue",
            "retry_count": 0,
            "last_error": None,
            "last_attempt_at": "2026-05-16T10:00:00",
        },
    ]


def test_submit_order_payload_returns_partial_failure_for_invoice_projection_failure(
    monkeypatch,
):
    order = FakeOrder(
        invoice_status="Failed",
        stock_status="Posted",
        sales_invoice=None,
    )
    monkeypatch.setattr(
        fb_orders,
        "_normalize_submit_order_payload",
        lambda payload: {
            "external_idempotency_key": "idem-1",
            "request_fingerprint": "f" * 64,
            "shift": "FB-SHIFT-1",
            "device_id": "DEVICE-1",
            "staff_id": "staff@example.com",
        },
    )
    monkeypatch.setattr(
        fb_orders,
        "_validate_new_submit_order_state",
        lambda normalized: normalized,
    )
    monkeypatch.setattr(fb_orders, "_validate_submit_shift", lambda **kwargs: None)
    monkeypatch.setattr(fb_orders, "_get_existing_fb_order_name", lambda key: None)
    monkeypatch.setattr(fb_orders, "_build_fb_order", lambda validated: order)
    monkeypatch.setattr(
        fb_orders,
        "_get_projection_statuses",
        lambda source_doctype, source_name: failed_invoice_projection(),
    )

    result = fb_orders.submit_order_payload({"idempotency_key": "idem-1"})

    assert result["status"] == "partial_failure"
    assert result["partial_failure"] is True
    assert result["fb_order"] == "FB-ORDER-1"
    assert result["projection_status"] == "failed"
    assert result["failed_subsystem"] == "sales_invoice"
    assert result["idempotency_key"] == "idem-1"
    assert result["message"] == "Sales Invoice projection status is Failed; expected Posted"
    assert result["diagnostics"][0] == {
        "fb_order": "FB-ORDER-1",
        "projection_status": "failed",
        "failed_subsystem": "sales_invoice",
        "error_message": "Sales Invoice projection status is Failed; expected Posted",
        "idempotency_key": "idem-1",
    }
    assert any(
        diagnostic["error_message"] == "forced Sales Invoice projection failure"
        for diagnostic in result["diagnostics"]
    )
    assert order.insert_count == 1
    assert order.submit_count == 1


def test_submit_order_payload_duplicate_idempotency_key_reuses_posted_projection_once(
    monkeypatch,
):
    order = FakeOrder(
        invoice_status="Posted",
        stock_status="Posted",
        sales_invoice="SINV-1",
    )
    stored_order: dict[str, FakeOrder | None] = {"value": None}
    build_count = {"value": 0}

    def get_existing(_key: str) -> str | None:
        return stored_order["value"].name if stored_order["value"] else None

    def build_order(_validated: dict[str, Any]) -> FakeOrder:
        build_count["value"] += 1
        return order

    def insert_once(ignore_permissions: bool = False) -> FakeOrder:
        order.insert_count += 1
        stored_order["value"] = order
        return order

    monkeypatch.setattr(order, "insert", insert_once)
    monkeypatch.setattr(
        fb_orders,
        "_normalize_submit_order_payload",
        lambda payload: {
            "external_idempotency_key": "idem-1",
            "request_fingerprint": "f" * 64,
            "shift": "FB-SHIFT-1",
            "device_id": "DEVICE-1",
            "staff_id": "staff@example.com",
        },
    )
    monkeypatch.setattr(
        fb_orders,
        "_validate_new_submit_order_state",
        lambda normalized: normalized,
    )
    monkeypatch.setattr(fb_orders, "_validate_submit_shift", lambda **kwargs: None)
    monkeypatch.setattr(fb_orders, "_get_existing_fb_order_name", get_existing)
    monkeypatch.setattr(fb_orders, "_build_fb_order", build_order)
    monkeypatch.setattr(fb_orders.frappe, "get_doc", lambda doctype, name: order)
    monkeypatch.setattr(
        fb_orders,
        "_get_projection_statuses",
        lambda source_doctype, source_name: succeeded_projections(),
    )

    first = fb_orders.submit_order_payload({"idempotency_key": "idem-1"})
    second = fb_orders.submit_order_payload({"idempotency_key": "idem-1"})

    assert first["status"] == "ok"
    assert second["status"] == "duplicate"
    assert first["sales_invoice"] == "SINV-1"
    assert second["sales_invoice"] == "SINV-1"
    assert first["partial_failure"] is False
    assert second["partial_failure"] is False
    assert build_count["value"] == 1
    assert order.insert_count == 1
    assert order.submit_count == 1
    assert len({first["sales_invoice"], second["sales_invoice"]}) == 1


def test_race_duplicate_rejects_same_key_with_different_order_fingerprint(
    monkeypatch,
):
    order = FakeOrder(
        invoice_status="Posted",
        stock_status="Posted",
        sales_invoice="SINV-1",
    )
    order.request_fingerprint = "a" * 64
    lookups = {"count": 0}

    def get_existing(_key: str) -> str | None:
        lookups["count"] += 1
        return None if lookups["count"] == 1 else order.name

    monkeypatch.setattr(
        fb_orders,
        "_normalize_submit_order_payload",
        lambda payload: {
            "external_idempotency_key": "idem-1",
            "request_fingerprint": "b" * 64,
            "shift": "FB-SHIFT-1",
            "device_id": "DEVICE-1",
            "staff_id": "staff@example.com",
        },
    )
    monkeypatch.setattr(
        fb_orders,
        "_validate_new_submit_order_state",
        lambda normalized: normalized,
    )
    monkeypatch.setattr(fb_orders, "_get_existing_fb_order_name", get_existing)
    monkeypatch.setattr(fb_orders, "_validate_submit_shift", lambda **kwargs: None)
    monkeypatch.setattr(fb_orders, "_build_fb_order", lambda validated: order)
    monkeypatch.setattr(fb_orders.frappe, "get_doc", lambda *args: order)
    monkeypatch.setattr(
        order,
        "insert",
        lambda **kwargs: (_ for _ in ()).throw(
            fb_orders.frappe.DuplicateEntryError("duplicate")
        ),
    )

    with pytest.raises(
        fb_orders.frappe.ValidationError,
        match="different canonical FB Order payload",
    ):
        fb_orders.submit_order_payload({"idempotency_key": "idem-1"})


def test_retry_failed_sales_invoice_projection_runs_handler_and_updates_order(
    monkeypatch,
) -> None:
    log = FakeProjectionLog("Sales Invoice")
    order = FakeRetryOrder()
    projection_updates: list[tuple[str, str, str | None, str | None]] = []

    def get_doc(doctype: str, name: str):
        if doctype == "FB Projection Log":
            return log
        if doctype == "FB Order":
            return order
        raise AssertionError(f"unexpected get_doc({doctype}, {name})")

    def update_projection_log(
        updated_log: FakeProjectionLog,
        state: str,
        target_name: str | None,
        last_error: str | None,
    ) -> None:
        updated_log.state = state
        updated_log.target_name = target_name
        updated_log.last_error = last_error
        projection_updates.append((updated_log.name, state, target_name, last_error))

    monkeypatch.setattr(fb_orders.frappe, "get_doc", get_doc)
    monkeypatch.setattr(fb_orders, "_update_projection_log", update_projection_log)
    monkeypatch.setattr(
        fb_orders,
        "_create_sales_invoice_projection",
        lambda source_doc: "SINV-RETRY-1",
    )
    monkeypatch.setattr(
        fb_orders,
        "_derive_projection_field_status",
        lambda source_doc, projection_type: "Posted"
        if projection_type == "Sales Invoice"
        else "Pending",
    )

    result = fb_orders._retry_projection_log(log.name)

    assert result == {
        "projection_log": log.name,
        "projection_type": "Sales Invoice",
        "state": "Succeeded",
        "target_name": "SINV-RETRY-1",
    }
    assert log.retry_count == 1
    assert log.saved is True
    assert projection_updates == [(log.name, "Succeeded", "SINV-RETRY-1", None)]
    assert ("invoice_status", "Posted", False) in order.db_set_calls
    assert ("stock_status", "Pending", False) in order.db_set_calls
    assert order.reload_count == 1


def test_retry_failed_noop_stock_projection_completes_without_handler(
    monkeypatch,
) -> None:
    log = FakeProjectionLog("Stock Issue")
    order = FakeRetryOrder()
    stock_handler_called = {"value": False}

    def get_doc(doctype: str, name: str):
        if doctype == "FB Projection Log":
            return log
        if doctype == "FB Order":
            return order
        raise AssertionError(f"unexpected get_doc({doctype}, {name})")

    def fail_stock_handler(source_doc, resolved_sales: list[Any]) -> str | None:
        stock_handler_called["value"] = True
        raise AssertionError("stock handler should not run for no-op stock projection")

    monkeypatch.setattr(fb_orders.frappe, "get_doc", get_doc)
    monkeypatch.setattr(
        fb_orders,
        "_update_projection_log",
        lambda updated_log, state, target_name, last_error: setattr(
            updated_log, "state", state
        ),
    )
    monkeypatch.setattr(fb_orders, "_create_stock_issue_projection", fail_stock_handler)
    monkeypatch.setattr(
        fb_orders,
        "_derive_projection_field_status",
        lambda source_doc, projection_type: "Pending",
    )

    result = fb_orders._retry_projection_log(log.name)

    assert result["projection_type"] == "Stock Issue"
    assert result["state"] == "Succeeded"
    assert result["target_name"] is None
    assert stock_handler_called["value"] is False


def test_sales_invoice_projection_relinks_existing_invoice_by_idempotency_key(
    monkeypatch,
) -> None:
    order = FakeRelinkOrder()
    invoice = type(
        "Invoice",
        (),
        {
            "doctype": "Sales Invoice",
            "name": "SINV-EXISTING-1",
            "custom_fb_order": "FB-ORDER-1",
            "custom_fb_shift": "FB-SHIFT-1",
            "custom_fb_device_id": "DEVICE-1",
            "custom_fb_idempotency_key": "idem-1",
            "company": "JiJi Cafe",
            "currency": "MYR",
            "docstatus": 1,
            "is_return": 0,
            "is_pos": 1,
            "update_stock": 0,
            "pos_profile": "Counter 1",
            "net_total": "12.00",
            "total_taxes_and_charges": "0.00",
            "grand_total": "12.00",
            "disable_rounded_total": 1,
            "rounded_total": "0.00",
            "write_off_amount": "0.00",
            "paid_amount": "12.00",
            "change_amount": "0.00",
            "outstanding_amount": "0.00",
            "items": [
                {
                    "custom_fb_order_line_ref": "LINE-1",
                    "item_code": "LATTE",
                    "qty": 1,
                    "amount": "12.00",
                    "net_amount": "12.00",
                    "custom_kopos_modifier_total": "0.00",
                    "warehouse": "Main - JC",
                }
            ],
            "payments": [
                {
                    "mode_of_payment": "Cash",
                    "amount": "12.00",
                    "account": "Cash - JC",
                    "custom_fb_source_payment_id": "PAY-1",
                }
            ],
            "taxes": [],
        },
    )()

    monkeypatch.setattr(
        sales_invoice_service.frappe.db,
        "get_value",
        lambda doctype, filters, fieldname: "SINV-EXISTING-1"
        if doctype == "Sales Invoice"
        and filters == {"custom_fb_idempotency_key": "idem-1"}
        and fieldname == "name"
        else None,
    )
    monkeypatch.setattr(
        sales_invoice_service.frappe,
        "get_doc",
        lambda doctype, name: invoice,
    )

    result = sales_invoice_service.create_sales_invoice(order)

    assert result == "SINV-EXISTING-1"
    assert ("sales_invoice", "SINV-EXISTING-1", True) in order.db_set_calls
    assert ("invoice_status", "Posted", True) in order.db_set_calls


def test_sales_invoice_projection_rejects_idempotency_collision_for_other_device(
    monkeypatch,
) -> None:
    order = FakeRelinkOrder()
    invoice = type(
        "Invoice",
        (),
        {
            "doctype": "Sales Invoice",
            "name": "SINV-EXISTING-1",
            "custom_fb_order": "FB-ORDER-1",
            "custom_fb_shift": "FB-SHIFT-1",
            "custom_fb_device_id": "OTHER-DEVICE",
            "custom_fb_idempotency_key": "idem-1",
            "company": "JiJi Cafe",
            "currency": "MYR",
            "docstatus": 1,
            "is_return": 0,
            "is_pos": 1,
            "grand_total": "12.00",
            "paid_amount": "12.00",
            "outstanding_amount": "0.00",
        },
    )()

    monkeypatch.setattr(
        sales_invoice_service.frappe.db,
        "get_value",
        lambda doctype, filters, fieldname: "SINV-EXISTING-1"
        if doctype == "Sales Invoice"
        else None,
    )
    monkeypatch.setattr(
        sales_invoice_service.frappe,
        "get_doc",
        lambda doctype, name: invoice,
    )

    try:
        sales_invoice_service.create_sales_invoice(order)
    except sales_invoice_service.frappe.ValidationError as error:
        assert "belongs to another KoPOS device" in str(error)
    else:
        raise AssertionError("expected idempotency collision to be rejected")
