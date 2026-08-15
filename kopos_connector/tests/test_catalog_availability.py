# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

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
            {"is_available": True, "stock_warning": None},
            id="optional-force-unavailable-cannot-block-sale",
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
def test_get_item_availability_ignores_optional_inventory_override_modes(
    catalog_module, monkeypatch, item, expected
):
    def fail_stock_lookup(*args, **kwargs):
        raise AssertionError("override modes should not query stock availability")

    monkeypatch.setattr(catalog_module, "get_bin_qty_map", fail_stock_lookup)
    monkeypatch.setattr(
        catalog_module, "get_fb_pending_reserved_qty_map", fail_stock_lookup
    )

    assert catalog_module.get_item_availability(item, warehouse="WH-1") == expected


def test_get_item_availability_never_reads_or_blocks_on_stock_shortfall(
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

    assert availability == {"is_available": True, "stock_warning": None}


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
    monkeypatch.setattr(
        catalog_module,
        "get_modifier_groups",
        lambda since=None, group_ids=None: [],
    )
    monkeypatch.setattr(
        catalog_module,
        "get_modifier_options",
        lambda since=None, group_ids=None: [],
    )
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
            "stock_warning": None,
            "is_active": 1,
            "is_prep_item": 0,
            "modifier_group_ids": [],
            "recipe_id": None,
            "recipe_version": None,
        }
    ]
    assert payload["metadata"] == {
        "company": "KoPOS Cafe",
        "pos_profile": "POS-1",
        "warehouse": "WH-1",
        "currency": "MYR",
        "tax_rate": 0.06,
    }
    assert payload["timestamp"] == fixed_time.replace(
        tzinfo=ZoneInfo("Asia/Kuala_Lumpur")
    ).isoformat()
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
    monkeypatch.setattr(
        catalog_module,
        "get_modifier_groups",
        lambda since=None, group_ids=None: [],
    )
    monkeypatch.setattr(
        catalog_module,
        "get_modifier_options",
        lambda since=None, group_ids=None: [],
    )
    monkeypatch.setattr(catalog_module, "get_tax_rate_value", lambda device_id=None: 0.08)
    monkeypatch.setattr(catalog_module, "now_datetime", lambda: fixed_time)

    full = catalog_module.build_catalog_payload(device_id="DEVICE-1")
    unchanged = catalog_module.build_catalog_payload(
        since="2025-01-01T00:00:00Z",
        device_id="DEVICE-1",
        known_version=full["catalog_version"],
    )

    assert unchanged["sync_mode"] == "unchanged"
    assert unchanged["unchanged"] == 1
    assert unchanged["catalog_version"] == full["catalog_version"]
    assert unchanged["timestamp"] == fixed_time.replace(
        tzinfo=ZoneInfo("Asia/Kuala_Lumpur")
    ).isoformat()
    assert unchanged["metadata"] == full["metadata"]
    assert unchanged["inventory_overlay"]["status"] == "unavailable"


def test_build_catalog_payload_never_queries_optional_modifier_rows(
    catalog_module, monkeypatch
):
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
                "id": "LATTE",
                "name": "Latte",
                "category_id": "DRINKS",
                "price_sen": 1200,
                "modifier_group_ids": ["ADDITIONAL_ESPRESSO_SHOT"],
            }
        ],
    )
    monkeypatch.setattr(
        catalog_module,
        "get_categories",
        lambda since=None, category_ids=None: [
            {"id": "DRINKS", "name": "Drinks", "display_order": 1}
        ],
    )

    def optional_call(*_args, **_kwargs):
        raise AssertionError("cashier catalog must not query optional modifier data")

    monkeypatch.setattr(catalog_module, "get_modifier_groups", optional_call)
    monkeypatch.setattr(catalog_module, "get_modifier_options", optional_call)
    monkeypatch.setattr(catalog_module, "get_tax_rate_value", lambda **kwargs: 0)

    payload = catalog_module.build_catalog_payload(device_id="DEVICE-1")

    assert payload["items"][0]["modifier_group_ids"] == []
    assert payload["modifier_groups"] == []
    assert payload["modifier_options"] == []


def test_build_catalog_payload_keeps_plain_items_when_modifier_module_fails(
    catalog_module, monkeypatch
):
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
        lambda **_kwargs: [
            {
                "id": "LATTE",
                "name": "Latte",
                "category_id": "DRINKS",
                "price_sen": 1200,
                "modifier_group_ids": ["MILK"],
                "recipe_id": "LATTE-RECIPE",
                "recipe_version": 3,
            }
        ],
    )
    monkeypatch.setattr(
        catalog_module,
        "get_categories",
        lambda **_kwargs: [
            {"id": "DRINKS", "name": "Drinks", "display_order": 1}
        ],
    )
    monkeypatch.setattr(
        catalog_module,
        "get_modifier_groups",
        lambda **_kwargs: (_ for _ in ()).throw(
            ImportError("optional recipe module is unavailable")
        ),
    )
    monkeypatch.setattr(
        catalog_module,
        "get_modifier_options",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("options must not be queried after group failure")
        ),
    )
    monkeypatch.setattr(catalog_module, "get_tax_rate_value", lambda **_kwargs: 0)

    payload = catalog_module.build_catalog_payload(device_id="DEVICE-1")

    assert payload["items"][0]["id"] == "LATTE"
    assert payload["items"][0]["modifier_group_ids"] == []
    assert payload["items"][0]["recipe_id"] is None
    assert payload["items"][0]["recipe_version"] is None
    assert payload["modifier_groups"] == []
    assert payload["modifier_options"] == []


def test_build_catalog_payload_drops_invalid_optional_rules_but_not_bad_items(
    catalog_module, monkeypatch
):
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
    item = {
        "id": "LATTE",
        "name": "Latte",
        "category_id": "DRINKS",
        "price_sen": 1200,
        "modifier_group_ids": ["MILK"],
        "recipe_id": "LATTE-RECIPE",
        "recipe_version": 3,
    }
    monkeypatch.setattr(catalog_module, "get_items", lambda **_kwargs: [item])
    monkeypatch.setattr(
        catalog_module,
        "get_categories",
        lambda **_kwargs: [
            {"id": "DRINKS", "name": "Drinks", "display_order": 1}
        ],
    )
    monkeypatch.setattr(
        catalog_module,
        "get_modifier_groups",
        lambda **_kwargs: [
            {
                "id": "MILK",
                "name": "Milk",
                "selection_type": "multiple",
                "is_required": 1,
                "min_selections": 2,
                "max_selections": 2,
                "parent_option_id": None,
            }
        ],
    )
    monkeypatch.setattr(
        catalog_module,
        "get_modifier_options",
        lambda **_kwargs: [
            {
                "id": "OAT",
                "group_id": "MILK",
                "name": "Oat",
                "price_adjustment_sen": 200,
                "is_active": 1,
            }
        ],
    )
    monkeypatch.setattr(catalog_module, "get_tax_rate_value", lambda **_kwargs: 0)

    payload = catalog_module.build_catalog_payload(device_id="DEVICE-1")
    assert payload["items"][0]["modifier_group_ids"] == []
    assert payload["modifier_groups"] == []

    item["price_sen"] = "not-an-integer"
    with pytest.raises(
        catalog_module.frappe.ValidationError,
        match="price_sen must be an integer",
    ):
        catalog_module.build_catalog_payload(device_id="DEVICE-1")


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


def _modifier_catalog_snapshot(
    group: dict[str, object], *, active_option_count: int = 2
) -> dict[str, object]:
    group_id = str(group.get("id") or "GROUP-1")
    return {
        "categories": [{"id": "DRINKS", "name": "Drinks"}],
        "items": [
            {
                "id": "LATTE",
                "name": "Latte",
                "category_id": "DRINKS",
                "price_sen": 1200,
                "modifier_group_ids": [group_id],
            }
        ],
        "modifier_groups": [group],
        "modifier_options": [
            {
                "id": f"OPTION-{index}",
                "group_id": group_id,
                "price_adjustment_sen": 0,
                "is_active": 1,
            }
            for index in range(active_option_count)
        ],
        "metadata": {},
    }


@pytest.mark.parametrize(
    ("group", "message"),
    [
        pytest.param(
            {
                "id": "REQUIRED-ZERO",
                "name": "Required zero",
                "selection_type": "single",
                "is_required": 1,
                "min_selections": 0,
                "max_selections": 1,
            },
            "must require at least one selection",
            id="required-minimum-zero",
        ),
        pytest.param(
            {
                "id": "NEGATIVE-MIN",
                "name": "Negative minimum",
                "selection_type": "multiple",
                "is_required": 0,
                "min_selections": -1,
                "max_selections": 2,
            },
            "cannot be negative",
            id="negative-minimum",
        ),
        pytest.param(
            {
                "id": "NEGATIVE-MAX",
                "name": "Negative maximum",
                "selection_type": "multiple",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": -1,
            },
            "cannot be negative",
            id="negative-maximum",
        ),
        pytest.param(
            {
                "id": "MIN-OVER-MAX",
                "name": "Minimum over maximum",
                "selection_type": "multiple",
                "is_required": 1,
                "min_selections": 2,
                "max_selections": 1,
            },
            "cannot exceed maximum",
            id="minimum-over-maximum",
        ),
        pytest.param(
            {
                "id": "SINGLE-MAX-TWO",
                "name": "Single max two",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 2,
            },
            "cannot allow more than one selection",
            id="single-maximum-over-one",
        ),
    ],
)
def test_validate_catalog_snapshot_rejects_invalid_modifier_bounds(
    catalog_module, group, message
):
    with pytest.raises(catalog_module.frappe.ValidationError, match=message):
        catalog_module.validate_catalog_snapshot(_modifier_catalog_snapshot(group))


def test_validate_catalog_snapshot_rejects_too_few_active_modifier_options(
    catalog_module,
):
    group = {
        "id": "EXTRAS",
        "name": "Extras",
        "selection_type": "multiple",
        "is_required": 1,
        "min_selections": 2,
        "max_selections": 3,
    }

    with pytest.raises(
        catalog_module.frappe.ValidationError,
        match="fewer active options than its minimum selection",
    ):
        catalog_module.validate_catalog_snapshot(
            _modifier_catalog_snapshot(group, active_option_count=1)
        )


def test_get_items_bulk_loads_commercial_data_without_inventory_queries(
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
    def optional_call(*_args, **_kwargs):
        raise AssertionError("base item catalog must not query recipe/modifier data")

    monkeypatch.setattr(
        catalog_module, "get_item_modifier_groups_map", optional_call
    )
    monkeypatch.setattr(
        catalog_module, "get_item_recipe_snapshots_map", optional_call
    )
    monkeypatch.setattr(
        catalog_module, "get_recipe_changed_item_codes", optional_call
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
    }
    assert reservation_queries == []
    assert items[0]["price_sen"] == 1200
    assert items[0]["recipe_id"] is None
    assert items[0]["recipe_version"] is None
    assert items[0]["barcode"] == "111"
    assert items[0]["stock_warning"] is None
    assert items[1]["price_sen"] == 950
    assert items[1]["stock_warning"] is None


def test_saleable_item_query_tolerates_all_optional_custom_columns_absent(
    catalog_module, monkeypatch
):
    captured_fields: list[str] = []

    monkeypatch.setattr(
        catalog_module.frappe,
        "get_meta",
        lambda doctype: SimpleNamespace(has_field=lambda _fieldname: False),
    )

    def get_all(doctype, **kwargs):
        assert doctype == "Item"
        captured_fields.extend(kwargs["fields"])
        return []

    monkeypatch.setattr(catalog_module.frappe, "get_all", get_all)

    assert catalog_module.get_saleable_item_rows({"is_sales_item": 1}) == []
    assert "stock_uom" in captured_fields
    assert not any(field.startswith("custom_") for field in captured_fields)


def test_saleable_item_query_retries_when_cached_optional_column_is_stale(
    catalog_module, monkeypatch
):
    field_attempts: list[list[str]] = []

    monkeypatch.setattr(
        catalog_module.frappe,
        "get_meta",
        lambda doctype: SimpleNamespace(has_field=lambda _fieldname: True),
    )

    def get_all(doctype, **kwargs):
        assert doctype == "Item"
        fields = list(kwargs["fields"])
        field_attempts.append(fields)
        if "custom_kopos_is_prep_item" in fields:
            raise RuntimeError("unknown column custom_kopos_is_prep_item")
        return []

    monkeypatch.setattr(catalog_module.frappe, "get_all", get_all)

    assert catalog_module.get_saleable_item_rows({"is_sales_item": 1}) == []
    assert len(field_attempts) == 2
    assert "custom_kopos_is_prep_item" in field_attempts[0]
    assert not any(field.startswith("custom_") for field in field_attempts[1])


def test_get_items_keeps_plain_menu_when_recipe_subsystem_fails(
    catalog_module, monkeypatch
):
    item_row = {
        "id": "ITEM-1",
        "item_code": "ITEM-1",
        "name": "Americano",
        "category_id": "DRINKS",
        "price": 8,
        "disabled": 0,
        "custom_kopos_availability_mode": "auto",
        "custom_kopos_track_stock": 1,
        "custom_fb_recipe_required": 1,
        "custom_fb_default_recipe": "BROKEN-RECIPE",
        "stock_uom": "Nos",
    }
    monkeypatch.setattr(
        catalog_module,
        "get_saleable_item_rows",
        lambda **_kwargs: [item_row],
    )
    monkeypatch.setattr(
        catalog_module,
        "get_recipe_changed_item_codes",
        lambda **_kwargs: set(),
    )
    monkeypatch.setattr(
        catalog_module,
        "get_item_recipe_snapshots_map",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("recipe table is unavailable")
        ),
    )
    monkeypatch.setattr(
        catalog_module,
        "get_item_prices_map",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        catalog_module,
        "get_item_barcodes_map",
        lambda _items: {},
    )
    monkeypatch.setattr(
        catalog_module,
        "get_bin_qty_map",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("inventory must not be read")
        ),
    )
    monkeypatch.setattr(
        catalog_module,
        "get_fb_pending_reserved_qty_map",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("inventory reservations must not be read")
        ),
    )

    items = catalog_module.get_items(
        warehouse="WH-1",
        selling_price_list=None,
        pos_profile={"company": "KoPOS Cafe"},
    )

    assert items == [
        {
            "id": "ITEM-1",
            "item_code": "ITEM-1",
            "name": "Americano",
            "category_id": "DRINKS",
            "price": 8.0,
            "price_sen": 800,
            "barcode": None,
            "is_available": True,
            "stock_warning": None,
            "is_active": 1,
            "is_prep_item": 0,
            "modifier_group_ids": [],
            "recipe_id": None,
            "recipe_version": None,
        }
    ]


def test_item_modifier_endpoint_returns_plain_choice_when_recipe_fails(
    catalog_module, monkeypatch
):
    monkeypatch.setattr(
        catalog_module,
        "get_item_modifier_groups",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ModuleNotFoundError("optional recipe app is unavailable")
        ),
    )
    monkeypatch.setattr(
        catalog_module,
        "get_modifier_groups",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("optional modifier groups must not be queried")
        ),
    )
    monkeypatch.setattr(
        catalog_module,
        "get_modifier_options",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("optional modifier options must not be queried")
        ),
    )

    assert (
        catalog_module.get_item_modifiers_payload(
            "ITEM-1",
            company="KoPOS Cafe",
        )
        == []
    )


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


def test_inventory_overlay_failure_keeps_the_complete_commercial_catalog(
    catalog_module, monkeypatch
):
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
                "id": "LATTE",
                "item_code": "LATTE",
                "name": "Latte",
                "category_id": "DRINKS",
                "price": 12.0,
                "price_sen": 1200,
                "barcode": None,
                "is_available": True,
                "stock_warning": None,
                "is_active": 1,
                "is_prep_item": 0,
                "modifier_group_ids": [],
            }
        ],
    )
    monkeypatch.setattr(catalog_module, "_item_recipe_snapshots", lambda items, company: {})
    monkeypatch.setattr(catalog_module, "_item_modifier_group_ids", lambda items, company, recipe_snapshots=None: {})
    monkeypatch.setattr(catalog_module, "_load_modifier_catalog", lambda group_ids: ([], []))
    monkeypatch.setattr(
        catalog_module,
        "get_categories",
        lambda category_ids=None: [{"id": "DRINKS", "name": "Drinks", "display_order": 1, "is_active": 1}],
    )
    monkeypatch.setattr(catalog_module, "get_tax_rate_value", lambda device_id=None: 0.06)
    monkeypatch.setattr(
        catalog_module,
        "build_inventory_overlay",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("stock database unavailable")),
    )

    payload = catalog_module.build_catalog_payload(device_id="DEVICE-1")

    assert payload["sync_mode"] == "full"
    assert payload["items"][0]["id"] == "LATTE"
    assert payload["inventory_overlay"]["status"] == "unavailable"
    assert payload["inventory_overlay"]["items"] == []
    assert payload["inventory_overlay"]["reasons"] == [
        {
            "code": "inventory_overlay_unavailable",
            "label": "Stock availability is temporarily unavailable; selling continues",
            "source": "stock",
        }
    ]
