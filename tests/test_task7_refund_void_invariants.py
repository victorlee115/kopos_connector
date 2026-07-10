from __future__ import annotations

import importlib
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

install_fake_frappe_modules()


class MutableDoc(SimpleNamespace):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.db_updates: list[tuple[str, Any]] = []
        self.save_count = 0
        self.submit_count = 0
        self.insert_count = 0
        self.cancel_count = 0

    def get(self, key: str) -> Any:
        return getattr(self, key, None)

    def set(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def append(self, key: str, row: dict[str, Any]) -> MutableDoc:
        child = MutableDoc(**row)
        current = list(getattr(self, key, []) or [])
        current.append(child)
        setattr(self, key, current)
        return child

    def db_set(
        self,
        fieldname: str,
        value: Any,
        update_modified: bool = True,
    ) -> None:
        setattr(self, fieldname, value)
        self.db_updates.append((fieldname, value))

    def save(self, ignore_permissions: bool = False) -> MutableDoc:
        self.save_count += 1
        return self

    def insert(self, ignore_permissions: bool = False) -> MutableDoc:
        self.insert_count += 1
        return self

    def submit(self) -> MutableDoc:
        self.submit_count += 1
        self.docstatus = 1
        return self

    def cancel(self) -> MutableDoc:
        self.cancel_count += 1
        self.docstatus = 2
        return self

    def reload(self) -> MutableDoc:
        return self


def test_public_process_refund_rejects_cross_device_before_return_event(monkeypatch):
    api = importlib.import_module("kopos_connector.api")

    captured: dict[str, Any] = {}
    original_invoice = MutableDoc(
        doctype="Sales Invoice",
        name="SINV-1",
        docstatus=1,
        is_return=0,
        custom_fb_device_id="DEVICE-A",
        custom_fb_order="FB-ORDER-1",
    )

    class FakeDB:
        def rollback(self) -> None:
            captured["rolled_back"] = True

        def get_value(self, doctype: str, name_or_filters: Any, fieldname: str) -> Any:
            if doctype == "Sales Invoice" and fieldname == "custom_fb_order":
                return "FB-ORDER-1"
            if doctype == "FB Return Event":
                return None
            return None

        def exists(self, doctype: str, name: str) -> bool:
            return doctype in {"Sales Invoice", "FB Resolved Sale"}

    def get_doc(doctype: str, name: str) -> MutableDoc:
        if doctype == "Sales Invoice" and name == "SINV-1":
            return original_invoice
        if doctype == "FB Resolved Sale" and name == "RS-1":
            return MutableDoc(
                doctype="FB Resolved Sale",
                name="RS-1",
                fb_order="FB-ORDER-1",
                sales_invoice="SINV-1",
                qty=1,
            )
        raise AssertionError(f"unexpected get_doc({doctype!r}, {name!r})")

    monkeypatch.setattr(api.frappe, "db", FakeDB())
    monkeypatch.setattr(api.frappe, "get_all", lambda *args, **kwargs: [])
    monkeypatch.setattr(api.frappe, "get_doc", get_doc)
    monkeypatch.setattr(
        api.frappe,
        "new_doc",
        lambda doctype: pytest.fail(f"{doctype} should not be created"),
    )
    monkeypatch.setattr(
        api,
        "_get_submit_payload",
        lambda kwargs: {
            "idempotency_key": "refund-idem-1",
            "device_id": "DEVICE-B",
            "original_sales_invoice": "SINV-1",
            "lines": [{"original_resolved_sale": "RS-1", "qty_returned": 1}],
            "refund_reason": "Customer return",
            "return_to_stock": False,
        },
    )
    monkeypatch.setattr(api, "require_device_context", lambda device_id: None)
    monkeypatch.setattr(
        api,
        "_write_response",
        lambda payload, http_status_code=200: captured.update(
            {"payload": payload, "http_status_code": http_status_code}
        ),
    )

    api.process_refund()

    assert captured["http_status_code"] == 400
    assert captured["payload"]["status"] == "error"
    assert "belongs to another device" in captured["payload"]["message"]
    assert captured["rolled_back"] is True


def test_cash_refund_reduces_shift_expected_cash_and_duplicate_reuses_invoice(
    monkeypatch,
):
    service = importlib.import_module(
        "kopos_connector.kopos.services.accounting.return_invoice_service"
    )

    shift = MutableDoc(
        doctype="FB Shift",
        name="SHIFT-1",
        opening_float=100.0,
        expected_cash=112.0,
        counted_cash=100.0,
    )
    original_invoice = MutableDoc(
        doctype="Sales Invoice",
        name="SINV-1",
        docstatus=1,
        customer="Walk-in Customer",
        company="Company A",
        currency="MYR",
        is_pos=1,
        pos_profile="POS-A",
        custom_fb_order="FB-ORDER-1",
        custom_fb_shift="SHIFT-1",
        custom_fb_device_id="DEVICE-A",
        items=[
            MutableDoc(
                item_code="ITEM-1",
                item_name="Item 1",
                description="Item 1",
                qty=1,
                uom="Nos",
                conversion_factor=1,
                rate=12.0,
                warehouse="WH-A",
                custom_fb_resolved_sale="RS-1",
            )
        ],
        payments=[MutableDoc(mode_of_payment="Cash", amount=12.0)],
    )
    return_invoice = MutableDoc(
        doctype="Sales Invoice",
        name="SINV-RETURN-1",
        docstatus=0,
        items=[],
        payments=[],
        grand_total=0.0,
    )
    return_doc = MutableDoc(
        doctype="FB Return Event",
        name="FB-RETURN-1",
        original_sales_invoice="SINV-1",
        return_sales_invoice=None,
        lines=[MutableDoc(original_resolved_sale="RS-1", qty_returned=1)],
    )

    def calculate_taxes_and_totals() -> None:
        return_invoice.grand_total = sum(
            float(getattr(item, "amount", 0) or 0) for item in return_invoice.items
        )

    return_invoice.set_missing_values = lambda: None
    return_invoice.calculate_taxes_and_totals = calculate_taxes_and_totals

    def get_doc(doctype: str, name: str) -> MutableDoc:
        if doctype == "Sales Invoice" and name == "SINV-1":
            return original_invoice
        if doctype == "Sales Invoice" and name == "SINV-RETURN-1":
            return return_invoice
        if doctype == "FB Shift" and name == "SHIFT-1":
            return shift
        raise AssertionError(f"unexpected get_doc({doctype!r}, {name!r})")

    def get_all(doctype: str, **kwargs: Any) -> list[MutableDoc]:
        if doctype == "FB Order":
            return [MutableDoc(sales_invoice="SINV-1")]
        if doctype == "Sales Invoice":
            return [MutableDoc(name="SINV-RETURN-1")]
        return []

    monkeypatch.setattr(service.frappe, "get_doc", get_doc)
    monkeypatch.setattr(service.frappe, "new_doc", lambda doctype: return_invoice)
    monkeypatch.setattr(service.frappe, "get_all", get_all)
    monkeypatch.setattr(
        service.frappe,
        "get_meta",
        lambda doctype: SimpleNamespace(has_field=lambda fieldname: True),
        raising=False,
    )

    first = service.create_return_sales_invoice(return_doc)
    second = service.create_return_sales_invoice(return_doc)

    assert first == "SINV-RETURN-1"
    assert second == "SINV-RETURN-1"
    assert return_invoice.submit_count == 1
    assert return_invoice.payments[0].mode_of_payment == "Cash"
    assert return_invoice.payments[0].amount == -12.0
    assert return_doc.return_sales_invoice == "SINV-RETURN-1"
    assert shift.expected_cash == 100.0
    assert shift.cash_variance == 0.0


def test_direct_return_route_requires_device_before_return_event(monkeypatch):
    fb_returns = importlib.import_module("kopos_connector.api.fb_returns")

    monkeypatch.setattr(
        fb_returns,
        "_get_request_payload",
        lambda: {
            "return_id": "refund-idem-1",
            "original_sales_invoice": "SINV-1",
            "lines": [{"original_resolved_sale": "RS-1", "qty_returned": 1}],
        },
    )
    monkeypatch.setattr(
        fb_returns,
        "require_device_context",
        lambda device_id: (_ for _ in ()).throw(
            fb_returns.frappe.ValidationError("device_id is required")
        ),
    )
    monkeypatch.setattr(
        fb_returns.frappe,
        "new_doc",
        lambda doctype: pytest.fail(f"{doctype} should not be created"),
    )

    with pytest.raises(fb_returns.frappe.ValidationError, match="device_id is required"):
        fb_returns.process_return()


def test_return_rejects_over_refund_and_duplicate_payload_mismatch(monkeypatch):
    fb_returns = importlib.import_module("kopos_connector.api.fb_returns")

    invoice = MutableDoc(
        doctype="Sales Invoice",
        name="SINV-1",
        docstatus=1,
        is_return=0,
        custom_fb_device_id="DEVICE-A",
        custom_fb_order="FB-ORDER-1",
    )
    resolved_sale = MutableDoc(
        doctype="FB Resolved Sale",
        name="RS-1",
        fb_order="FB-ORDER-1",
        sales_invoice="SINV-1",
        qty=1,
    )
    existing_return = MutableDoc(
        doctype="FB Return Event",
        name="FB-RETURN-EXISTING",
        return_id="other-return-id",
        fb_order="FB-ORDER-1",
        original_sales_invoice="SINV-1",
        return_to_stock=0,
        status="Submitted",
        lines=[MutableDoc(original_resolved_sale="RS-1", qty_returned=1)],
    )
    same_return = MutableDoc(
        doctype="FB Return Event",
        name="FB-RETURN-SAME",
        return_id="refund-idem-1",
        fb_order="FB-ORDER-1",
        original_sales_invoice="SINV-1",
        return_sales_invoice="SINV-RETURN-1",
        return_to_stock=0,
        status="Submitted",
        lines=[MutableDoc(original_resolved_sale="RS-1", qty_returned=1)],
    )

    class FakeDB:
        def get_value(self, doctype: str, filters: Any, fieldname: str) -> Any:
            if doctype == "FB Return Event" and filters == {"return_id": "refund-idem-1"}:
                return "FB-RETURN-SAME"
            return None

        def exists(self, doctype: str, name: str) -> bool:
            return doctype in {"Sales Invoice", "FB Resolved Sale"}

    def get_doc(doctype: str, name: str) -> MutableDoc:
        docs = {
            ("Sales Invoice", "SINV-1"): invoice,
            ("FB Resolved Sale", "RS-1"): resolved_sale,
            ("FB Return Event", "FB-RETURN-EXISTING"): existing_return,
            ("FB Return Event", "FB-RETURN-SAME"): same_return,
        }
        return docs[(doctype, name)]

    monkeypatch.setattr(fb_returns.frappe, "db", FakeDB())
    monkeypatch.setattr(fb_returns.frappe, "get_doc", get_doc)
    monkeypatch.setattr(
        fb_returns.frappe,
        "get_all",
        lambda doctype, **kwargs: [
            MutableDoc(parent="FB-RETURN-EXISTING", qty_returned=1)
        ]
        if doctype == "FB Return Event Line"
        else [],
    )

    with pytest.raises(fb_returns.frappe.ValidationError, match="exceeds purchased"):
        fb_returns.process_return_payload(
            {
                "return_id": "new-return-id",
                "device_id": "DEVICE-A",
                "original_sales_invoice": "SINV-1",
                "lines": [{"original_resolved_sale": "RS-1", "qty_returned": 1}],
            }
        )

    monkeypatch.setattr(fb_returns.frappe, "get_all", lambda *args, **kwargs: [])

    with pytest.raises(fb_returns.frappe.ValidationError, match="different return lines"):
        fb_returns.process_return_payload(
            {
                "return_id": "refund-idem-1",
                "device_id": "DEVICE-A",
                "original_sales_invoice": "SINV-1",
                "lines": [{"original_resolved_sale": "RS-1", "qty_returned": 0.5}],
            }
        )


def test_void_rejects_cross_device_before_cancel(monkeypatch):
    api = importlib.import_module("kopos_connector.api")
    invoice = MutableDoc(
        doctype="Sales Invoice",
        name="SINV-1",
        docstatus=1,
        is_return=0,
        custom_fb_order="FB-ORDER-1",
        custom_fb_idempotency_key="sale-idem-1",
        custom_fb_device_id="DEVICE-B",
    )

    monkeypatch.setattr(api.frappe, "get_doc", lambda doctype, name: invoice)

    with pytest.raises(api.frappe.ValidationError, match="belongs to another device"):
        api._process_sales_invoice_void_payload(
            {
                "sales_invoice": "SINV-1",
                "device_id": "DEVICE-A",
                "idempotency_key": "void-idem-1",
                "reason": "Operator correction",
            }
        )

    assert invoice.cancel_count == 0


def test_void_updates_fb_order_stock_projection_and_shift_once(monkeypatch):
    api = importlib.import_module("kopos_connector.api")

    invoice = MutableDoc(
        doctype="Sales Invoice",
        name="SINV-1",
        docstatus=1,
        is_return=0,
        custom_fb_order="FB-ORDER-1",
        custom_fb_shift="SHIFT-1",
        custom_fb_idempotency_key="sale-idem-1",
        custom_fb_device_id="DEVICE-A",
        payments=[MutableDoc(mode_of_payment="Cash", amount=12.0)],
    )
    invoice.add_comment = lambda kind, text: None
    order = MutableDoc(
        doctype="FB Order",
        name="FB-ORDER-1",
        shift="SHIFT-1",
        status="Submitted",
        invoice_status="Posted",
        stock_status="Posted",
        sales_invoice="SINV-1",
        ingredient_stock_entry="STE-1",
    )
    stock_entry = MutableDoc(doctype="Stock Entry", name="STE-1", docstatus=1)
    shift = MutableDoc(
        doctype="FB Shift",
        name="SHIFT-1",
        opening_float=100.0,
        expected_cash=112.0,
        counted_cash=100.0,
    )
    resolved_sale = MutableDoc(doctype="FB Resolved Sale", name="RS-1", status="Submitted")
    logs = {
        "LOG-SI": MutableDoc(doctype="FB Projection Log", name="LOG-SI", state="Succeeded"),
        "LOG-ST": MutableDoc(doctype="FB Projection Log", name="LOG-ST", state="Succeeded"),
        "LOG-SH": MutableDoc(doctype="FB Projection Log", name="LOG-SH", state="Succeeded"),
    }

    def get_doc(doctype: str, name: str) -> MutableDoc:
        docs = {
            ("Sales Invoice", "SINV-1"): invoice,
            ("FB Order", "FB-ORDER-1"): order,
            ("Stock Entry", "STE-1"): stock_entry,
            ("FB Shift", "SHIFT-1"): shift,
            ("FB Resolved Sale", "RS-1"): resolved_sale,
            ("FB Projection Log", "LOG-SI"): logs["LOG-SI"],
            ("FB Projection Log", "LOG-ST"): logs["LOG-ST"],
            ("FB Projection Log", "LOG-SH"): logs["LOG-SH"],
        }
        return docs[(doctype, name)]

    def get_all(doctype: str, **kwargs: Any) -> list[MutableDoc]:
        if doctype == "FB Resolved Sale":
            return [MutableDoc(name="RS-1")]
        if doctype == "FB Projection Log":
            return [
                MutableDoc(name="LOG-SI", projection_type="Sales Invoice"),
                MutableDoc(name="LOG-ST", projection_type="Stock Issue"),
                MutableDoc(name="LOG-SH", projection_type="FB Shift"),
            ]
        if doctype == "FB Order":
            return (
                [MutableDoc(sales_invoice="SINV-1")]
                if order.status == "Submitted"
                else []
            )
        if doctype == "Sales Invoice":
            return []
        return []

    monkeypatch.setattr(api, "elevate_device_api_user", lambda: nullcontext())
    monkeypatch.setattr(api.frappe, "get_doc", get_doc)
    monkeypatch.setattr(api.frappe, "get_all", get_all)

    result = api._process_sales_invoice_void_payload(
        {
            "sales_invoice": "SINV-1",
            "device_id": "DEVICE-A",
            "idempotency_key": "void-idem-1",
            "reason": "Operator correction",
        }
    )
    duplicate = api._process_sales_invoice_void_payload(
        {
            "sales_invoice": "SINV-1",
            "device_id": "DEVICE-A",
            "idempotency_key": "void-idem-1",
            "reason": "Operator correction",
        }
    )

    assert result["status"] == "ok"
    assert duplicate["status"] == "duplicate"
    assert invoice.cancel_count == 1
    assert stock_entry.cancel_count == 1
    assert order.status == "Cancelled"
    assert order.invoice_status == "Reversed"
    assert order.stock_status == "Reversed"
    assert resolved_sale.status == "Cancelled"
    assert {log.state for log in logs.values()} == {"Reversed"}
    assert shift.expected_cash == 100.0
    assert shift.cash_variance == 0.0
