from __future__ import annotations

import importlib
import sys
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

install_fake_frappe_modules()

sales_invoice_service = importlib.import_module(
    "kopos_connector.kopos.services.accounting.sales_invoice_service"
)
order_history = importlib.import_module("kopos_connector.api.order_history")


def make_doc(**kwargs: Any) -> SimpleNamespace:
    doc = SimpleNamespace(**kwargs)
    setattr(doc, "get", lambda key, default=None: getattr(doc, key, default))
    return doc


class MutableDoc(SimpleNamespace):
    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.insert_calls: list[bool] = []
        self.submit_calls = 0
        self.db_set_calls: list[tuple[str, Any, bool]] = []

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def set(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def append(self, key: str, value: dict[str, Any]) -> SimpleNamespace:
        rows = getattr(self, key, None)
        if rows is None:
            rows = []
            setattr(self, key, rows)
        if self.doctype == "Sales Invoice" and key == "items":
            value = {**value, "net_amount": value["amount"]}
        row = MutableDoc(doctype=f"{self.doctype} {key.title()}", **value)
        rows.append(row)
        return row

    def insert(self, ignore_permissions: bool = False) -> "MutableDoc":
        self.insert_calls.append(ignore_permissions)
        return self

    def submit(self) -> "MutableDoc":
        self.submit_calls += 1
        self.docstatus = 1
        return self

    def db_set(
        self,
        fieldname: str,
        value: Any,
        update_modified: bool = True,
    ) -> None:
        self.db_set_calls.append((fieldname, value, update_modified))
        setattr(self, fieldname, value)


def make_fb_order(*, device_id: str = "DEVICE-1") -> MutableDoc:
    return MutableDoc(
        doctype="FB Order",
        name="FB-ORDER-1",
        device_id=device_id,
        shift="FB-SHIFT-1",
        customer="Walk-in Customer",
        company="KoPOS Cafe",
        currency="MYR",
        status="Submitted",
        invoice_status="Pending",
        sales_invoice=None,
        external_idempotency_key="idem-001",
        booth_warehouse="Main Warehouse - KC",
        tax_total=0,
        rounding_adjustment=0,
        net_total=12,
        grand_total=12,
        items=[
            make_doc(
                item="LATTE",
                line_id="LINE-1",
                item_name_snapshot="Latte",
                qty=1,
                unit_price=12,
                line_total=12,
                selected_modifiers=[],
            )
        ],
        payments=[
            make_doc(
                source_payment_id="PAY-1",
                payment_method="Cash",
                amount=12,
                tendered_amount=12,
                change_amount=0,
                settlement_status="verified",
            )
        ],
    )


def install_sales_invoice_fakes(monkeypatch: pytest.MonkeyPatch) -> MutableDoc:
    invoice = MutableDoc(
        doctype="Sales Invoice",
        name="SINV-GENERATED-1",
        items=[],
        taxes=[],
        payments=[],
        docstatus=0,
        is_return=0,
        update_stock=0,
        net_total=12,
        grand_total=12,
        total_taxes_and_charges=0,
        rounded_total=0,
        disable_rounded_total=0,
        write_off_amount=0,
        paid_amount=0,
        change_amount=0,
        outstanding_amount=0,
    )

    monkeypatch.setattr(
        sales_invoice_service,
        "get_device_doc",
        lambda device_id=None, name=None: make_doc(
            name="KoPOS Device 1",
            device_id=device_id,
            pos_profile="Counter 1",
        ),
    )
    monkeypatch.setattr(
        sales_invoice_service,
        "privileged_device_api_operation",
        lambda reason: nullcontext(),
    )
    monkeypatch.setattr(
        sales_invoice_service.frappe,
        "new_doc",
        lambda doctype: invoice,
    )
    monkeypatch.setattr(
        sales_invoice_service.frappe,
        "get_cached_doc",
        lambda doctype, name: make_doc(
            doctype=doctype,
            name=name,
            company="KoPOS Cafe",
        ),
    )
    monkeypatch.setattr(
        sales_invoice_service.frappe,
        "get_doc",
        lambda doctype, name: make_doc(
            doctype="Item",
            name=name,
            item_name=name,
            description=name,
            stock_uom="Nos",
        )
        if doctype == "Item"
        else make_doc(doctype=doctype, name=name),
    )
    monkeypatch.setattr(
        sales_invoice_service.frappe,
        "get_meta",
        lambda doctype: SimpleNamespace(has_field=lambda fieldname: True),
        raising=False,
    )
    monkeypatch.setattr(
        sales_invoice_service,
        "_resolve_mode_of_payment_context",
        lambda mode_of_payment, company: {
            "account": "Cash - KC",
            "type": "Cash",
        },
    )
    return invoice


def test_create_sales_invoice_sets_device_pos_profile(monkeypatch: pytest.MonkeyPatch):
    invoice = install_sales_invoice_fakes(monkeypatch)
    order_doc = make_fb_order()

    result = sales_invoice_service.create_sales_invoice(order_doc)

    assert result == "SINV-GENERATED-1"
    assert invoice.pos_profile == "Counter 1"
    assert invoice.custom_fb_order == "FB-ORDER-1"
    assert invoice.custom_fb_shift == "FB-SHIFT-1"
    assert invoice.custom_fb_device_id == "DEVICE-1"
    assert invoice.insert_calls == [True]
    assert invoice.submit_calls == 1
    assert ("sales_invoice", "SINV-GENERATED-1", True) in order_doc.db_set_calls
    assert ("invoice_status", "Posted", True) in order_doc.db_set_calls


def test_missing_pos_profile_fails_before_invoice_creation(
    monkeypatch: pytest.MonkeyPatch,
):
    order_doc = make_fb_order()
    created_invoice = {"value": False}

    monkeypatch.setattr(
        sales_invoice_service,
        "get_device_doc",
        lambda device_id=None, name=None: make_doc(
            name="KoPOS Device 1",
            device_id=device_id,
            pos_profile="",
        ),
    )

    def fail_new_doc(doctype: str) -> None:
        created_invoice["value"] = True
        raise AssertionError("Sales Invoice must not be created without POS Profile")

    monkeypatch.setattr(sales_invoice_service.frappe, "new_doc", fail_new_doc)

    with pytest.raises(
        sales_invoice_service.frappe.ValidationError,
        match="KoPOS Device DEVICE-1 has no POS Profile configured",
    ):
        sales_invoice_service.create_sales_invoice(order_doc)

    assert created_invoice["value"] is False
    assert order_doc.db_set_calls == []


def test_pos_profile_company_mismatch_fails_before_invoice_creation(
    monkeypatch: pytest.MonkeyPatch,
):
    order_doc = make_fb_order()
    created_invoice = {"value": False}

    monkeypatch.setattr(
        sales_invoice_service,
        "get_device_doc",
        lambda device_id=None, name=None: make_doc(
            name="KoPOS Device 1",
            device_id=device_id,
            pos_profile="Counter 1",
        ),
    )
    monkeypatch.setattr(
        sales_invoice_service.frappe,
        "get_cached_doc",
        lambda doctype, name: make_doc(
            doctype=doctype,
            name=name,
            company="Other Company",
        ),
    )

    def fail_new_doc(doctype: str) -> None:
        created_invoice["value"] = True
        raise AssertionError("Sales Invoice must not be created for company mismatch")

    monkeypatch.setattr(sales_invoice_service.frappe, "new_doc", fail_new_doc)

    with pytest.raises(
        sales_invoice_service.frappe.ValidationError,
        match="FB Order company KoPOS Cafe does not match POS Profile Counter 1 company Other Company",
    ):
        sales_invoice_service.create_sales_invoice(order_doc)

    assert created_invoice["value"] is False
    assert order_doc.db_set_calls == []


def test_order_history_reads_generated_sales_invoice_for_device_profile_shift(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_invoice_filters: list[dict[str, Any]] = []
    invoices = [
        {
            "name": "SINV-BEFORE-SHIFT",
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "Counter 1",
            "posting_date": "2026-05-16",
            "posting_time": "09:00:00",
            "creation": datetime(2026, 5, 16, 9, 0),
            "custom_fb_order": "FB-ORDER-BEFORE",
            "custom_fb_shift": "FB-SHIFT-1",
            "custom_fb_device_id": "DEVICE-1",
            "custom_fb_idempotency_key": "idem-before",
            "grand_total": 9,
            "paid_amount": 9,
        },
        {
            "name": "SINV-GENERATED-1",
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "Counter 1",
            "posting_date": "2026-05-16",
            "posting_time": "10:05:00",
            "creation": datetime(2026, 5, 16, 10, 5),
            "modified": datetime(2026, 5, 16, 10, 6),
            "custom_fb_order": "FB-ORDER-1",
            "custom_fb_shift": "FB-SHIFT-1",
            "custom_fb_device_id": "DEVICE-1",
            "custom_fb_idempotency_key": "idem-001",
            "grand_total": 12,
            "paid_amount": 12,
        },
        {
            "name": "SINV-OTHER-DEVICE",
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "Counter 1",
            "posting_date": "2026-05-16",
            "posting_time": "10:10:00",
            "creation": datetime(2026, 5, 16, 10, 10),
            "custom_fb_device_id": "DEVICE-2",
            "grand_total": 15,
            "paid_amount": 15,
        },
    ]

    def fake_get_all(doctype: str, **kwargs: Any) -> list[dict[str, Any]]:
        filters = kwargs.get("filters") or {}
        if doctype == "FB Shift":
            return [
                {
                    "name": "FB-SHIFT-1",
                    "opened_at": datetime(2026, 5, 16, 10, 0),
                    "creation": datetime(2026, 5, 16, 10, 0),
                }
            ]
        if doctype == "Sales Invoice" and filters.get("is_return") == 0:
            captured_invoice_filters.append(filters)
            return [row for row in invoices if matches_filters(row, filters)]
        if doctype == "Sales Invoice" and filters.get("is_return") == 1:
            return []
        if doctype == "Sales Invoice Item":
            return [
                {
                    "parent": "SINV-GENERATED-1",
                    "idx": 1,
                    "item_code": "LATTE",
                    "item_name": "Latte",
                    "qty": 1,
                    "rate": 12,
                    "amount": 12,
                    "net_amount": 12,
                }
            ]
        if doctype == "Sales Invoice Payment":
            return [
                {
                    "parent": "SINV-GENERATED-1",
                    "idx": 1,
                    "mode_of_payment": "Cash",
                    "amount": 12,
                    "default": 1,
                }
            ]
        if doctype in {"FB Order Line", "FB Selected Modifier"}:
            return []
        return []

    monkeypatch.setattr(
        order_history,
        "get_device_doc",
        lambda device_id=None: make_doc(
            name="KoPOS Device 1",
            device_id=device_id,
            enabled=1,
            pos_profile="Counter 1",
        ),
    )
    monkeypatch.setattr(
        order_history.frappe,
        "get_cached_doc",
        lambda doctype, name: make_doc(name=name, company="KoPOS Cafe"),
    )
    monkeypatch.setattr(order_history.frappe, "get_all", fake_get_all)

    result = order_history.get_order_history_payload(
        device_id="DEVICE-1",
        since_date="2026-05-01",
    )

    assert captured_invoice_filters == [
        {
            "docstatus": 1,
            "is_return": 0,
            "company": "KoPOS Cafe",
            "pos_profile": "Counter 1",
            "posting_date": [">=", "2026-05-16"],
            "custom_fb_device_id": "DEVICE-1",
        }
    ]
    assert result["since_datetime"] == "2026-05-16T10:00:00"
    assert [order["name"] for order in result["orders"]] == ["SINV-GENERATED-1"]
    assert result["orders"][0]["device_id"] == "DEVICE-1"
    assert result["orders"][0]["pos_profile"] == "Counter 1"
    assert result["orders"][0]["items"][0]["item_code"] == "LATTE"
    assert result["orders"][0]["payments"][0]["mode_of_payment"] == "Cash"


def matches_filters(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    for fieldname, expected in filters.items():
        actual = row.get(fieldname)
        if isinstance(expected, list):
            operator, expected_value = expected
            if operator == ">=":
                if str(actual) < str(expected_value):
                    return False
            elif operator == "in":
                if actual not in expected_value:
                    return False
            else:
                raise AssertionError(f"Unhandled filter operator: {operator}")
        elif actual != expected:
            return False
    return True
