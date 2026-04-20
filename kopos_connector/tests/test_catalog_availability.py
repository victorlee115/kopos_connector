# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


@pytest.fixture
def catalog_module(monkeypatch):
    install_fake_frappe_modules()

    import frappe

    monkeypatch.setattr(
        frappe,
        "db",
        SimpleNamespace(get_value=lambda *args, **kwargs: None, commit=lambda: None),
        raising=False,
    )
    monkeypatch.setattr(
        frappe,
        "logger",
        lambda *args, **kwargs: SimpleNamespace(info=lambda *a, **k: None),
        raising=False,
    )
    monkeypatch.setattr(
        frappe,
        "defaults",
        SimpleNamespace(get_user_default=lambda *args, **kwargs: None),
        raising=False,
    )
    monkeypatch.setattr(
        frappe,
        "session",
        SimpleNamespace(user="Administrator"),
        raising=False,
    )

    devices_module = types.ModuleType("kopos_connector.api.devices")
    devices_module.KOPOS_DEVICE_API_ROLE = "KoPOS Device API"
    devices_module.get_device_doc = lambda device_id=None: SimpleNamespace(
        pos_profile=None
    )
    devices_module.get_session_roles = lambda: ["System Manager"]
    monkeypatch.setitem(sys.modules, "kopos_connector.api.devices", devices_module)

    module_name = "test_catalog_availability_catalog"
    module_path = Path(__file__).resolve().parents[1] / "api" / "catalog.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec and spec.loader

    sys.modules.pop(module_name, None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        pytest.param(
            {
                "item_code": "DISABLED-ITEM",
                "disabled": 1,
                "custom_kopos_availability_mode": "force_available",
                "custom_kopos_track_stock": 1,
                "custom_kopos_min_qty": 1,
            },
            {"is_available": False, "stock_warning": None},
            id="disabled-item-hard-blocks-without-warning",
        ),
        pytest.param(
            {
                "item_code": "MANUAL-OFF",
                "disabled": 0,
                "custom_kopos_availability_mode": "force_unavailable",
                "custom_kopos_track_stock": 1,
                "custom_kopos_min_qty": 1,
            },
            {"is_available": False, "stock_warning": None},
            id="force-unavailable-hard-blocks-without-warning",
        ),
        pytest.param(
            {
                "item_code": "MANUAL-ON",
                "disabled": 0,
                "custom_kopos_availability_mode": "force_available",
                "custom_kopos_track_stock": 1,
                "custom_kopos_min_qty": 1,
            },
            {"is_available": True, "stock_warning": None},
            id="force-available-clears-warning",
        ),
    ],
)
def test_get_item_availability_respects_override_modes(
    catalog_module, monkeypatch, item, expected
):
    def fail_stock_lookup(*args, **kwargs):
        raise AssertionError("override modes should not query stock availability")

    monkeypatch.setattr(catalog_module.frappe.db, "get_value", fail_stock_lookup)
    monkeypatch.setattr(catalog_module, "get_pos_reserved_qty", fail_stock_lookup)

    assert catalog_module.get_item_availability(item, warehouse="WH-1") == expected


def test_get_item_availability_auto_stock_short_sets_advisory_warning(
    catalog_module, monkeypatch
):
    monkeypatch.setattr(
        catalog_module.frappe.db, "get_value", lambda *args, **kwargs: 1.0
    )
    monkeypatch.setattr(
        catalog_module, "get_pos_reserved_qty", lambda *args, **kwargs: 0.5
    )

    availability = catalog_module.get_item_availability(
        {
            "item_code": "AUTO-SHORT",
            "disabled": 0,
            "custom_kopos_availability_mode": "auto",
            "custom_kopos_track_stock": 1,
            "custom_kopos_min_qty": 1,
        },
        warehouse="WH-1",
    )

    assert availability == {"is_available": True, "stock_warning": "erp_stock_short"}


def test_get_item_availability_auto_stock_sufficient_clears_warning(
    catalog_module, monkeypatch
):
    monkeypatch.setattr(
        catalog_module.frappe.db, "get_value", lambda *args, **kwargs: 2.0
    )
    monkeypatch.setattr(
        catalog_module, "get_pos_reserved_qty", lambda *args, **kwargs: 0.25
    )

    availability = catalog_module.get_item_availability(
        {
            "item_code": "AUTO-OK",
            "disabled": 0,
            "custom_kopos_availability_mode": "auto",
            "custom_kopos_track_stock": 1,
            "custom_kopos_min_qty": 1,
        },
        warehouse="WH-1",
    )

    assert availability == {"is_available": True, "stock_warning": None}


def test_build_catalog_payload_includes_stock_warning_in_items(
    catalog_module, monkeypatch
):
    fixed_time = datetime(2026, 4, 21, 9, 30, 0)

    monkeypatch.setattr(
        catalog_module,
        "resolve_catalog_pos_profile",
        lambda device_id=None: {
            "name": "POS-1",
            "company": "KoPOS Cafe",
            "warehouse": "WH-1",
            "selling_price_list": "Standard Selling",
            "currency": "MYR",
        },
    )
    monkeypatch.setattr(
        catalog_module,
        "get_items",
        lambda **kwargs: [
            {
                "id": "ITEM-1",
                "item_code": "ITEM-1",
                "name": "Low Stock Latte",
                "category_id": "DRINKS",
                "price": 12.0,
                "barcode": None,
                "is_available": True,
                "stock_warning": "erp_stock_short",
                "is_active": 1,
                "is_prep_item": 0,
                "modifier_group_ids": [],
            }
        ],
    )
    monkeypatch.setattr(
        catalog_module,
        "get_categories",
        lambda since=None, category_ids=None: [
            {
                "id": "DRINKS",
                "name": "Drinks",
                "display_order": 1,
                "is_active": 1,
            }
        ],
    )
    monkeypatch.setattr(catalog_module, "get_modifier_groups", lambda since=None: [])
    monkeypatch.setattr(catalog_module, "get_modifier_options", lambda since=None: [])
    monkeypatch.setattr(
        catalog_module, "get_tax_rate_value", lambda device_id=None: 0.06
    )
    monkeypatch.setattr(catalog_module, "now_datetime", lambda: fixed_time)

    payload = catalog_module.build_catalog_payload(device_id="DEVICE-1")

    assert payload["items"] == [
        {
            "id": "ITEM-1",
            "item_code": "ITEM-1",
            "name": "Low Stock Latte",
            "category_id": "DRINKS",
            "price": 12.0,
            "barcode": None,
            "is_available": True,
            "stock_warning": "erp_stock_short",
            "is_active": 1,
            "is_prep_item": 0,
            "modifier_group_ids": [],
        }
    ]
    assert payload["metadata"] == {
        "company": "KoPOS Cafe",
        "pos_profile": "POS-1",
        "warehouse": "WH-1",
        "currency": "MYR",
        "tax_rate": 0.06,
    }
    assert payload["timestamp"] == fixed_time.isoformat()
