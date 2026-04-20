from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest


class FakeLogDoc:
    def __init__(self, doctype: str, sink: list["FakeLogDoc"]):
        self.doctype = doctype
        self._sink = sink
        self.name: str | None = None
        self.order_reference: str | None = None
        self.logged_at: datetime | None = None

    def insert(self, ignore_permissions: bool = False):
        self.name = f"FB-OVERRIDE-LOG-{len(self._sink) + 1}"
        self._sink.append(self)
        return self


@pytest.fixture
def fake_frappe(monkeypatch):
    created_logs: list[FakeLogDoc] = []
    stock_by_bin: dict[tuple[str, str], float] = {}
    timestamp = datetime(2026, 4, 20, 12, 0, 0)

    frappe_module: Any = types.ModuleType("frappe")
    frappe_utils_module: Any = types.ModuleType("frappe.utils")
    frappe_model_document_module: Any = types.ModuleType("frappe.model.document")

    class Document:
        pass

    def get_bin_value(doctype: str, filters: dict[str, str], fieldname: str):
        assert doctype == "Bin"
        assert fieldname == "actual_qty"
        return stock_by_bin.get((filters["item_code"], filters["warehouse"]), 0)

    frappe_module.db = SimpleNamespace(get_value=get_bin_value)
    frappe_module.new_doc = lambda doctype: FakeLogDoc(doctype, created_logs)
    frappe_module.generate_hash = lambda length=8: "X" * length
    frappe_module.scrub = (
        lambda value: str(value).replace("_", "-").replace(" ", "-").lower()
    )
    frappe_module.ValidationError = type("ValidationError", (Exception,), {})

    frappe_utils_module.now_datetime = lambda: timestamp
    frappe_utils_module.flt = lambda value: float(value or 0)
    frappe_utils_module.cstr = lambda value: "" if value is None else str(value)
    frappe_model_document_module.Document = Document

    monkeypatch.setitem(sys.modules, "frappe", frappe_module)
    monkeypatch.setitem(sys.modules, "frappe.utils", frappe_utils_module)
    monkeypatch.setitem(
        sys.modules, "frappe.model.document", frappe_model_document_module
    )

    dependency_stubs = {
        "kopos_connector.kopos.doctype.fb_modifier_group.fb_modifier_group": {
            "filter_visible_allowed_modifier_groups": lambda *args, **kwargs: []
        },
        "kopos_connector.kopos.services.accounting.sales_invoice_service": {
            "create_sales_invoice": lambda *args, **kwargs: None
        },
        "kopos_connector.kopos.services.inventory.stock_issue_service": {
            "create_ingredient_stock_entry": lambda *args, **kwargs: None
        },
        "kopos_connector.kopos.services.projection.log_service": {
            "create_projection_log": lambda *args, **kwargs: "PROJECTION-LOG",
            "update_projection_state": lambda *args, **kwargs: None,
        },
    }

    for module_name, attributes in dependency_stubs.items():
        module = types.ModuleType(module_name)
        for attr_name, attr_value in attributes.items():
            setattr(module, attr_name, attr_value)
        monkeypatch.setitem(sys.modules, module_name, module)

    for module_name in [
        "kopos_connector.kopos.services.inventory.warning_service",
        "kopos_connector.kopos.doctype.fb_order.fb_order",
    ]:
        sys.modules.pop(module_name, None)

    return SimpleNamespace(
        created_logs=created_logs,
        stock_by_bin=stock_by_bin,
        timestamp=timestamp,
    )


def test_detect_and_log_stock_shortfall(fake_frappe):
    fake_frappe.stock_by_bin[("ITEM-1", "WH-1")] = 1.0
    warning_service = importlib.import_module(
        "kopos_connector.kopos.services.inventory.warning_service"
    )

    shortfalls = warning_service.detect_stock_shortfall(
        [
            {
                "item": "ITEM-1",
                "warehouse": "WH-1",
                "stock_qty": 1.25,
                "affects_stock": 1,
            },
            {
                "item": "ITEM-1",
                "warehouse": "WH-1",
                "stock_qty": 0.75,
                "affects_stock": 1,
            },
        ]
    )

    assert len(shortfalls) == 1
    assert shortfalls[0]["item_code"] == "ITEM-1"
    assert shortfalls[0]["warehouse"] == "WH-1"
    assert shortfalls[0]["required_qty"] == 2.0
    assert shortfalls[0]["available_qty"] == 1.0
    assert shortfalls[0]["shortfall_qty"] == 1.0

    log_names = warning_service.log_stock_shortfall(
        SimpleNamespace(name="FB-ORDER-1", order_id="ORDER-1"),
        shortfalls,
        timestamp=fake_frappe.timestamp,
    )

    assert log_names == ["FB-OVERRIDE-LOG-1"]
    assert len(fake_frappe.created_logs) == 1

    log_doc = fake_frappe.created_logs[0]
    assert log_doc.fb_order == "FB-ORDER-1"
    assert log_doc.order_reference == "ORDER-1"
    assert log_doc.item == "ITEM-1"
    assert log_doc.warehouse == "WH-1"
    assert log_doc.requested_qty == 2.0
    assert log_doc.available_qty_before == 1.0
    assert log_doc.shortfall_qty == 1.0
    assert log_doc.logged_at == fake_frappe.timestamp
    assert log_doc.approved_at == fake_frappe.timestamp


def test_before_submit_logs_shortfall_without_throwing(fake_frappe):
    fake_frappe.stock_by_bin[("ITEM-1", "WH-1")] = 0.5
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )

    order = fb_order_module.FBOrder()
    order.name = "FB-ORDER-1"
    order.order_id = "ORDER-1"
    order.booth_warehouse = "WH-1"
    captured_resolutions: list[object] = []
    line_resolutions = [
        {
            "resolved_components": [
                {"item": "ITEM-1", "stock_qty": 1.0, "affects_stock": 1}
            ]
        }
    ]

    order.build_line_resolutions = lambda: line_resolutions
    order.create_resolved_sales = lambda resolutions: captured_resolutions.append(
        resolutions
    )

    order.before_submit()

    assert captured_resolutions == [line_resolutions]
    assert len(fake_frappe.created_logs) == 1
    assert fake_frappe.created_logs[0].item == "ITEM-1"
    assert fake_frappe.created_logs[0].order_reference == "ORDER-1"


def test_before_submit_still_raises_non_stock_failures(fake_frappe):
    fb_order_module = importlib.import_module(
        "kopos_connector.kopos.doctype.fb_order.fb_order"
    )

    order = fb_order_module.FBOrder()
    order.booth_warehouse = "WH-1"
    order.build_line_resolutions = lambda: []

    def raise_non_stock_failure(_line_resolutions):
        raise RuntimeError("resolved sale projection failed")

    order.create_resolved_sales = raise_non_stock_failure

    with pytest.raises(RuntimeError, match="resolved sale projection failed"):
        order.before_submit()
