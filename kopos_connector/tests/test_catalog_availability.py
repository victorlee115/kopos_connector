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

    monkeypatch.setattr(catalog_module, "get_bin_qty_map", fail_stock_lookup)
    monkeypatch.setattr(
        catalog_module, "get_fb_pending_reserved_qty_map", fail_stock_lookup
    )

    assert catalog_module.get_item_availability(item, warehouse="WH-1") == expected


def test_get_item_availability_auto_stock_short_sets_advisory_warning(
    catalog_module, monkeypatch
):
    availability = catalog_module.get_item_availability(
        {
            "item_code": "AUTO-SHORT",
            "disabled": 0,
            "custom_kopos_availability_mode": "auto",
            "custom_kopos_track_stock": 1,
            "custom_kopos_min_qty": 1,
        },
        warehouse="WH-1",
        bin_qty_by_item={"AUTO-SHORT": 1.0},
        reserved_qty_by_item={"AUTO-SHORT": 0.5},
    )

    assert availability == {"is_available": True, "stock_warning": "erp_stock_short"}


def test_get_item_availability_auto_stock_sufficient_clears_warning(
    catalog_module, monkeypatch
):
    availability = catalog_module.get_item_availability(
        {
            "item_code": "AUTO-OK",
            "disabled": 0,
            "custom_kopos_availability_mode": "auto",
            "custom_kopos_track_stock": 1,
            "custom_kopos_min_qty": 1,
        },
        warehouse="WH-1",
        bin_qty_by_item={"AUTO-OK": 2.0},
        reserved_qty_by_item={"AUTO-OK": 0.25},
    )

    assert availability == {"is_available": True, "stock_warning": None}


def test_device_catalog_and_tax_fail_closed_without_pos_profile(catalog_module):
    with pytest.raises(
        catalog_module.frappe.ValidationError,
        match="has no POS Profile configured",
    ):
        catalog_module.resolve_catalog_pos_profile(device_id="DEVICE-NO-PROFILE")

    with pytest.raises(
        catalog_module.frappe.ValidationError,
        match="has no POS Profile configured",
    ):
        catalog_module.get_tax_rate_value(device_id="DEVICE-NO-PROFILE")


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
                "price_sen": 1200,
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
            "price_sen": 1200,
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
    assert payload["sync_mode"] == "full"
    assert payload["unchanged"] == 0
    assert payload["catalog_version"].startswith("sha256:")


def test_build_catalog_payload_returns_small_unchanged_response(
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
                "name": "Latte",
                "category_id": "DRINKS",
                "price": 12,
                "price_sen": 1200,
                "barcode": "955000000001",
                "is_available": True,
                "stock_warning": None,
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
            {"id": "DRINKS", "name": "Drinks", "display_order": 1, "is_active": 1}
        ],
    )
    monkeypatch.setattr(catalog_module, "get_modifier_groups", lambda since=None: [])
    monkeypatch.setattr(catalog_module, "get_modifier_options", lambda since=None: [])
    monkeypatch.setattr(catalog_module, "get_tax_rate_value", lambda device_id=None: 0.08)
    monkeypatch.setattr(catalog_module, "now_datetime", lambda: fixed_time)

    full = catalog_module.build_catalog_payload(device_id="DEVICE-1")
    unchanged = catalog_module.build_catalog_payload(
        since="2025-01-01T00:00:00Z",
        device_id="DEVICE-1",
        known_version=full["catalog_version"],
    )

    assert unchanged == {
        "sync_mode": "unchanged",
        "unchanged": 1,
        "catalog_version": full["catalog_version"],
        "timestamp": fixed_time.isoformat(),
        "metadata": full["metadata"],
    }


def test_validate_catalog_snapshot_rejects_empty_or_partial_publication(
    catalog_module,
):
    with pytest.raises(catalog_module.frappe.ValidationError, match="at least one category"):
        catalog_module.validate_catalog_snapshot(
            {
                "categories": [],
                "items": [],
                "modifier_groups": [],
                "modifier_options": [],
                "metadata": {},
            }
        )


def test_validate_catalog_snapshot_rejects_orphan_modifier_references(
    catalog_module,
):
    with pytest.raises(catalog_module.frappe.ValidationError, match="unknown group"):
        catalog_module.validate_catalog_snapshot(
            {
                "categories": [
                    {"id": "DRINKS", "name": "Drinks", "display_order": 1}
                ],
                "items": [
                    {
                        "id": "ITEM-1",
                        "name": "Latte",
                        "category_id": "DRINKS",
                        "price_sen": 1200,
                        "modifier_group_ids": [],
                    }
                ],
                "modifier_groups": [],
                "modifier_options": [
                    {
                        "id": "OPT-1",
                        "group_id": "MISSING",
                        "price_adjustment_sen": 0,
                    }
                ],
                "metadata": {},
            }
        )


def test_get_items_bulk_loads_prices_barcodes_bins_and_fb_reservations(
    catalog_module, monkeypatch
):
    query_counts: dict[str, int] = {}

    def get_all(doctype, **kwargs):
        query_counts[doctype] = query_counts.get(doctype, 0) + 1
        if doctype == "Item":
            return [
                {
                    "id": "ITEM-1",
                    "item_code": "ITEM-1",
                    "name": "Latte",
                    "category_id": "DRINKS",
                    "price": 10,
                    "disabled": 0,
                    "custom_kopos_availability_mode": "auto",
                    "custom_kopos_track_stock": 1,
                    "custom_kopos_min_qty": 1,
                },
                {
                    "id": "ITEM-2",
                    "item_code": "ITEM-2",
                    "name": "Tea",
                    "category_id": "DRINKS",
                    "price": 8,
                    "disabled": 0,
                    "custom_kopos_availability_mode": "auto",
                    "custom_kopos_track_stock": 1,
                    "custom_kopos_min_qty": 1,
                },
            ]
        if doctype == "Item Price":
            return [
                {"item_code": "ITEM-1", "price_list_rate": 12},
                {"item_code": "ITEM-2", "price_list_rate": 9.5},
            ]
        if doctype == "Item Barcode":
            return [
                {"parent": "ITEM-1", "barcode": "111", "idx": 1},
                {"parent": "ITEM-2", "barcode": "222", "idx": 1},
            ]
        if doctype == "Bin":
            return [
                {"item_code": "ITEM-1", "actual_qty": 5},
                {"item_code": "ITEM-2", "actual_qty": 1},
            ]
        raise AssertionError(f"unexpected bulk query for {doctype}")

    reservation_queries = []

    def sql(query, values=(), as_dict=False):
        reservation_queries.append((query, values, as_dict))
        return [
            {"item_code": "ITEM-1", "reserved_qty": 1},
            {"item_code": "ITEM-2", "reserved_qty": 1},
        ]

    monkeypatch.setattr(catalog_module.frappe, "get_all", get_all)
    monkeypatch.setattr(catalog_module.frappe.db, "sql", sql, raising=False)
    monkeypatch.setattr(
        catalog_module,
        "get_item_modifier_groups_map",
        lambda rows, company=None, recipe_snapshots_by_item=None: {},
    )
    monkeypatch.setattr(
        catalog_module,
        "get_item_recipe_snapshots_map",
        lambda rows, company=None: {
            "ITEM-1": {"recipe_id": "RECIPE-ITEM-1", "recipe_version": 1},
            "ITEM-2": {"recipe_id": "RECIPE-ITEM-2", "recipe_version": 1},
        },
    )

    items = catalog_module.get_items(
        warehouse="WH-1",
        selling_price_list="Standard Selling",
        pos_profile={"company": "KoPOS Cafe"},
    )

    assert query_counts == {
        "Item": 1,
        "Item Price": 1,
        "Item Barcode": 1,
        "Bin": 1,
    }
    assert len(reservation_queries) == 1
    assert "tabFB Order" in reservation_queries[0][0]
    assert "POS Invoice" not in reservation_queries[0][0]
    assert reservation_queries[0][1] == ("WH-1", "ITEM-1", "ITEM-2")
    assert items[0]["price_sen"] == 1200
    assert items[0]["recipe_id"] == "RECIPE-ITEM-1"
    assert items[0]["recipe_version"] == 1
    assert items[0]["barcode"] == "111"
    assert items[0]["stock_warning"] is None
    assert items[1]["price_sen"] == 950
    assert items[1]["stock_warning"] == "erp_stock_short"


def test_item_price_selection_rejects_future_expired_and_wrong_uom_rows(
    catalog_module, monkeypatch
):
    monkeypatch.setattr(
        catalog_module,
        "now_datetime",
        lambda: datetime(2026, 7, 12, 12, 0, 0),
    )
    monkeypatch.setattr(
        catalog_module.frappe,
        "get_all",
        lambda *args, **kwargs: [
            {
                "name": "FUTURE",
                "item_code": "LATTE",
                "price_list_rate": 99,
                "uom": "Nos",
                "valid_from": "2099-01-01",
                "valid_upto": None,
                "modified": "2099-01-01",
            },
            {
                "name": "EXPIRED",
                "item_code": "LATTE",
                "price_list_rate": 88,
                "uom": "Nos",
                "valid_from": "2025-01-01",
                "valid_upto": "2026-07-11",
                "modified": "2026-07-11",
            },
            {
                "name": "WRONG-UOM",
                "item_code": "LATTE",
                "price_list_rate": 77,
                "uom": "Box",
                "valid_from": "2026-07-01",
                "valid_upto": None,
                "modified": "2026-07-12",
            },
            {
                "name": "GENERIC",
                "item_code": "LATTE",
                "price_list_rate": 13,
                "uom": None,
                "valid_from": "2026-07-10",
                "valid_upto": None,
                "modified": "2026-07-12",
            },
            {
                "name": "EXACT-UOM",
                "item_code": "LATTE",
                "price_list_rate": 12,
                "uom": "Nos",
                "valid_from": "2026-07-01",
                "valid_upto": None,
                "modified": "2026-07-01",
            },
        ],
    )

    result = catalog_module.get_item_prices_map(
        ["LATTE"],
        "Standard Selling",
        item_uoms={"LATTE": "Nos"},
    )

    assert result == {"LATTE": 12.0}


@pytest.mark.parametrize(
    ("amount", "expected_sen"),
    [("12.345", 1235), ("12.344", 1234), ("-0.005", -1)],
)
def test_money_to_sen_uses_explicit_half_up_rounding(
    catalog_module, amount, expected_sen
):
    assert catalog_module.money_to_sen(amount) == expected_sen
