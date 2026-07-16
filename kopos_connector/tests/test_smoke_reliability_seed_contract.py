# pyright: reportMissingImports=false

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


def test_smoke_asserts_sale_datetime_on_invoice_and_stock_projection() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    state = {
        "fb_shifts": [],
        "fb_orders": [
            {
                "name": "FB-ORDER-1",
                "status": "Submitted",
                "stock_status": "Posted",
                "sale_datetime": datetime(2026, 7, 12, 0, 30, 45),
                "sales_invoice": "SINV-1",
                "ingredient_stock_entry": "STE-1",
            }
        ],
        "sales_invoices": [
            {
                "name": "SINV-1",
                "docstatus": 1,
                "is_return": False,
                "posting_date": "2026-07-12",
                "posting_time": "00:30:45",
                "items": [{}],
                "payments": [{}],
            }
        ],
        "ingredient_stock_entries": [
            {
                "name": "STE-1",
                "docstatus": 1,
                "posting_date": "2026-07-12",
                "posting_time": timedelta(minutes=30, seconds=45),
            }
        ],
        "return_records": [],
        "void_records": [],
        "projection_statuses": {"failed": []},
        "legacy_active_paths": {},
        "idempotency": {},
        "order_history": {},
    }

    result = smoke.build_smoke_business_assertions(state)

    assert result["assertions"]["fb_order_sale_datetime_persisted"] is True
    assert result["assertions"]["sales_invoice_sale_datetime_preserved"] is True
    assert (
        result["assertions"]["ingredient_stock_entry_sale_datetime_preserved"]
        is True
    )


def test_smoke_business_gate_requires_myr_device_invoice_payment_and_gl() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    state = {
        "device": {
            "pos_profile_currency": "MYR",
            "pos_profile_company": smoke.SMOKE_COMPANY_NAME,
        },
        "data": {
            "fb_shifts": [],
            "fb_orders": [
                {
                    "name": "FB-ORDER-MYR",
                    "status": "Submitted",
                    "company": smoke.SMOKE_COMPANY_NAME,
                    "currency": "MYR",
                }
            ],
            "sales_invoices": [
                {
                    "name": "SINV-MYR",
                    "docstatus": 1,
                    "is_return": False,
                    "company": smoke.SMOKE_COMPANY_NAME,
                    "currency": "MYR",
                    "items": [{}],
                    "payments": [
                        {
                            "account": "Cash - KMY",
                            "account_currency": "MYR",
                        }
                    ],
                    "gl_entries": [
                        {
                            "account": "Cash - KMY",
                            "account_currency": "MYR",
                        }
                    ],
                }
            ],
            "ingredient_stock_entries": [],
            "return_records": [],
            "void_records": [],
            "projection_statuses": {"failed": []},
            "legacy_active_paths": {},
            "idempotency": {},
            "order_history": {},
        },
    }

    result = smoke.build_smoke_business_assertions(state)

    for assertion in (
        "provisioned_device_currency_is_myr",
        "fb_order_currency_is_myr",
        "sales_invoice_currency_is_myr",
        "sales_invoice_payment_currency_is_myr",
        "sales_invoice_gl_currency_is_myr",
    ):
        assert result["assertions"][assertion] is True


def test_smoke_company_contract_is_dedicated_malaysia_myr() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    assert smoke.SMOKE_COMPANY_NAME == "KoPOS Malaysia Sdn Bhd"
    assert smoke.SMOKE_COMPANY_ABBR == "KMY"
    assert smoke.SMOKE_COMPANY_COUNTRY == "Malaysia"
    assert smoke.SMOKE_COMPANY_CURRENCY == "MYR"


def test_existing_smoke_device_refresh_disables_training_mode(monkeypatch) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke
    from kopos_connector.utils import pin

    class ExistingDevice:
        def __init__(self) -> None:
            self.allow_training_mode = 1
            self.device_users: list[dict[str, Any]] = []
            self.printers: list[dict[str, Any]] = []
            self.saved = False

        def append(self, fieldname: str, row: dict[str, Any]) -> None:
            getattr(self, fieldname).append(row)

        def save(self, *, ignore_permissions: bool) -> None:
            assert ignore_permissions is True
            self.saved = True

    device = ExistingDevice()
    monkeypatch.setattr(smoke, "_ensure_frappe_user", lambda *args: None)
    monkeypatch.setattr(smoke, "_build_smoke_device_printers", lambda: [])
    monkeypatch.setattr(pin, "hash_pin", lambda value: f"hash:{value}")
    monkeypatch.setattr(
        frappe.db,
        "exists",
        lambda doctype, filters: "KOPOS-DEVICE-001",
    )
    monkeypatch.setattr(frappe, "get_doc", lambda *args: device)

    result = smoke._ensure_kopos_device(
        smoke.SMOKE_DEVICE_ID,
        "KoPOS Smoke Profile",
        smoke.SMOKE_COMPANY_NAME,
    )

    assert result is device
    assert device.allow_training_mode == smoke.SMOKE_ALLOW_TRAINING_MODE == 0
    assert device.saved is True


def test_new_smoke_device_is_created_with_training_mode_disabled(monkeypatch) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke
    from kopos_connector.utils import pin

    captured: dict[str, Any] = {}

    class NewDevice:
        def __init__(self) -> None:
            self.inserted = False

        def insert(self, *, ignore_permissions: bool) -> None:
            assert ignore_permissions is True
            self.inserted = True

    device = NewDevice()

    def fake_get_doc(values: dict[str, Any]) -> NewDevice:
        captured.update(values)
        return device

    monkeypatch.setattr(smoke, "_ensure_frappe_user", lambda *args: None)
    monkeypatch.setattr(smoke, "_build_smoke_device_printers", lambda: [])
    monkeypatch.setattr(pin, "hash_pin", lambda value: f"hash:{value}")
    monkeypatch.setattr(frappe.db, "exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(frappe, "get_doc", fake_get_doc)

    result = smoke._ensure_kopos_device(
        smoke.SMOKE_DEVICE_ID,
        "KoPOS Smoke Profile",
        smoke.SMOKE_COMPANY_NAME,
    )

    assert result is device
    assert captured["allow_training_mode"] == smoke.SMOKE_ALLOW_TRAINING_MODE == 0
    assert device.inserted is True


def test_smoke_dump_exposes_disabled_training_policy(monkeypatch) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    device = SimpleNamespace(
        api_user="",
        enabled=1,
        allow_training_mode="0",
        pos_profile="KoPOS Smoke Profile",
        config_version=9,
    )
    monkeypatch.setattr(
        frappe.db,
        "get_value",
        lambda *args, **kwargs: "KOPOS-DEVICE-001",
    )
    monkeypatch.setattr(frappe, "get_doc", lambda *args, **kwargs: device)
    monkeypatch.setattr(
        frappe,
        "local",
        SimpleNamespace(site="test-site.local"),
    )
    monkeypatch.setattr(
        smoke,
        "_collect_device_profile_evidence",
        lambda doc: {"pos_profile_warehouse": "KoPOS Store - KMY"},
    )
    monkeypatch.setattr(
        smoke,
        "_collect_smoke_business_state",
        lambda *args, **kwargs: {},
    )

    result = smoke.dump_smoke_state()

    assert result["device"]["allow_training_mode"] is False


def test_smoke_rejects_projection_posted_on_sync_date_instead_of_sale_date() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    assert smoke._projection_posts_at_sale_datetime(
        {"sale_datetime": datetime(2026, 7, 11, 23, 55, 0)},
        {
            "posting_date": "2026-07-12",
            "posting_time": "09:00:00",
        },
    ) is False


def test_smoke_rejects_closed_shift_timestamp_before_open_timestamp() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    assert smoke._shift_timestamps_in_order(
        {
            "opened_at": "2026-07-11 10:00:00",
            "closed_at": "2026-07-11 17:00:00",
        }
    ) is True
    assert smoke._shift_timestamps_in_order(
        {
            "opened_at": "2026-07-11 10:00:00",
            "closed_at": "2026-07-11 09:59:59",
        }
    ) is False


def test_smoke_business_gate_proves_modifier_audit_and_totals() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    history_key = "history-smoke-order-1"
    state = {
        "fb_shifts": [],
        "fb_orders": [
            {
                "name": "FB-ORDER-MODIFIER",
                "status": "Submitted",
                "external_idempotency_key": history_key,
                "sales_invoice": "SINV-MODIFIER",
                "grand_total": "14.00",
                "items": [
                    {
                        "line_id": "LINE-MODIFIER",
                        "modifier_total": "2.00",
                        "line_total": "14.00",
                        "resolved_sale": "FB-RESOLVED-MODIFIER",
                        "selected_modifiers": [
                            {
                                "modifier": "SMOKE-FB-SIZE-LARGE",
                                "price_adjustment": "2.00",
                            }
                        ],
                    }
                ],
            }
        ],
        "sales_invoices": [
            {
                "name": "SINV-MODIFIER",
                "docstatus": 1,
                "is_return": False,
                "grand_total": "14.00",
                "items": [
                    {
                        "order_line_ref": "LINE-MODIFIER",
                        "amount": "14.00",
                        "modifier_total": "2.00",
                        "has_modifiers": True,
                        "modifiers_json": (
                            '{"modifiers":[{"id":"SMOKE-FB-SIZE-LARGE",'
                            '"price_adjustment_sen":200}]}'
                        ),
                    }
                ],
                "payments": [{}],
            }
        ],
        "ingredient_stock_entries": [],
        "return_records": [],
        "void_records": [],
        "projection_statuses": {"failed": []},
        "legacy_active_paths": {},
        "idempotency": {
            "duplicate_sales_invoice_keys": [],
            "sales_invoice_counts_by_idempotency_key": {history_key: 1},
        },
        "order_history": {"invoice_count": 1},
    }

    result = smoke.build_smoke_business_assertions(
        state,
        expected_idempotency_keys=[history_key],
    )

    assert result["assertions"]["modifier_resolved_sale_audit_proven"] is True
    assert result["assertions"]["modifier_sales_invoice_audit_proven"] is True
    assert result["assertions"]["modifier_order_and_invoice_totals_proven"] is True


def test_reliability_drink_item_code_matches_t16_submit_payload() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    assert smoke.DEMO_DRINK_ITEM == "SMOKE-STRAWBERRY-001"
    assert smoke.DEMO_DRINK_BARCODE == "SMOKE-STRAWBERRY-001"
    assert smoke.DEMO_DRINK_NAME == "Strawberry Matcha Latte"
    assert smoke.LEGACY_DEMO_DRINK_ITEM == "STRAWBERRY-MATCHA-LATTE"


def test_existing_recipe_is_repointed_to_reliability_item_code() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    class Recipe:
        recipe_code = smoke.DEMO_RECIPE_CODE
        recipe_name = smoke.DEMO_DRINK_NAME
        sellable_item = "STRAWBERRY-MATCHA-LATTE"
        recipe_type = "Finished Drink"
        status = "Active"
        version_no = 1
        company = "KoPOS Cafe"
        yield_qty = 1
        yield_uom = "Nos"
        default_serving_qty = 1
        default_serving_uom = "Nos"

    recipe = Recipe()

    changed = smoke._ensure_demo_recipe_fields(recipe, "KoPOS Cafe")

    assert changed is True
    assert recipe.sellable_item == "SMOKE-STRAWBERRY-001"


def test_smoke_recipe_uses_company_specific_code_instead_of_mutating_published_recipe(
    monkeypatch,
) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    monkeypatch.setattr(
        frappe.db,
        "exists",
        lambda doctype, name: smoke.DEMO_RECIPE_CODE
        if doctype == "FB Recipe" and name == smoke.DEMO_RECIPE_CODE
        else False,
    )

    def fake_get_value(doctype: str, name: str, fieldname: str) -> str | None:
        if (doctype, name, fieldname) == (
            "FB Recipe",
            smoke.DEMO_RECIPE_CODE,
            "company",
        ):
            return "Wind Power LLC"
        if (doctype, name, fieldname) == (
            "Company",
            smoke.SMOKE_COMPANY_NAME,
            "abbr",
        ):
            return smoke.SMOKE_COMPANY_ABBR
        return None

    monkeypatch.setattr(frappe.db, "get_value", fake_get_value)

    assert smoke._demo_recipe_code_for_company(smoke.SMOKE_COMPANY_NAME) == (
        f"{smoke.DEMO_RECIPE_CODE}-{smoke.SMOKE_COMPANY_ABBR}"
    )


def test_smoke_recipe_keeps_base_code_for_same_company(monkeypatch) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    monkeypatch.setattr(
        frappe.db,
        "exists",
        lambda doctype, name: smoke.DEMO_RECIPE_CODE
        if doctype == "FB Recipe" and name == smoke.DEMO_RECIPE_CODE
        else False,
    )
    monkeypatch.setattr(
        frappe.db,
        "get_value",
        lambda doctype, name, fieldname: smoke.SMOKE_COMPANY_NAME
        if (doctype, name, fieldname)
        == ("FB Recipe", smoke.DEMO_RECIPE_CODE, "company")
        else None,
    )

    assert (
        smoke._demo_recipe_code_for_company(smoke.SMOKE_COMPANY_NAME)
        == smoke.DEMO_RECIPE_CODE
    )


def test_smoke_recipe_components_pin_stock_units_before_frappe_defaults() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    components = smoke._build_demo_recipe_components()

    assert len(components) == 4
    assert all(component["stock_qty"] == component["qty"] for component in components)
    assert all(component["stock_uom"] == component["uom"] for component in components)
    assert components[0]["stock_uom"] == "Gram"
    assert components[1]["stock_uom"] == "Millilitre"


def test_existing_smoke_recipe_repairs_component_stock_quantities_and_units() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    class Recipe:
        def __init__(self) -> None:
            self.components = [
                SimpleNamespace(
                    item=smoke.DEMO_MATCHA_ITEM,
                    component_type="Ingredient",
                    qty=smoke.DEMO_MATCHA_QTY_PER_ORDER,
                    uom="Gram",
                    stock_qty=1,
                    stock_uom="Nos",
                    affects_stock=1,
                    affects_cogs=1,
                )
            ]

        def get(self, fieldname: str) -> list[Any] | None:
            if fieldname == "components":
                return self.components
            return None

        def set(self, fieldname: str, value: list[Any]) -> None:
            setattr(self, fieldname, value)

        def append(self, fieldname: str, value: dict[str, Any]) -> None:
            getattr(self, fieldname).append(SimpleNamespace(**value))

    recipe = Recipe()

    changed = smoke._ensure_demo_recipe_components(recipe)

    assert changed is True
    assert len(recipe.components) == 4
    assert recipe.components[0].stock_qty == smoke.DEMO_MATCHA_QTY_PER_ORDER
    assert recipe.components[0].stock_uom == "Gram"
    assert recipe.components[1].stock_qty == smoke.DEMO_STRAWBERRY_QTY_PER_ORDER
    assert recipe.components[1].stock_uom == "Millilitre"


def test_existing_smoke_recipe_components_are_idempotent_when_correct() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    components = [SimpleNamespace(**row) for row in smoke._build_demo_recipe_components()]
    recipe = SimpleNamespace(get=lambda fieldname: components)

    assert smoke._ensure_demo_recipe_components(recipe) is False


def test_smoke_seed_defines_manual_selectable_erp_promotion() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    values = smoke._build_demo_promotion_values("KoPOS Smoke Profile")

    assert values["promotion_name"] == "SMOKE-MANUAL-10-PCT"
    assert values["display_label"] == "Smoke 10% Off"
    assert values["activation_mode"] == "manual_selectable"
    assert values["offline_allowed"] == 1
    assert values["discount_type"] == "percentage"
    assert values["discount_value"] == 10
    assert values["eligible_items"] == [{"item_code": "SMOKE-STRAWBERRY-001"}]
    assert values["eligible_pos_profiles"] == [
        {"pos_profile": "KoPOS Smoke Profile"}
    ]


def test_smoke_dump_exposes_release_safe_authoritative_promotion_snapshot(
    monkeypatch,
) -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    captured: dict[str, Any] = {}
    snapshot_payload = {
        "snapshot_version": "KOPOS-PROMO-SMOKE",
        "snapshot_hash": "a" * 64,
        "pos_profile": "KoPOS Smoke Profile",
        "promotions": [
            {
                "promotion_id": "SMOKE-MANUAL-10-PCT",
                "promotion_name": "SMOKE-MANUAL-10-PCT",
                "promotion_type": "item_discount",
                "activation_mode": "manual_selectable",
                "discount_type": "percentage",
                "discount_value": 10,
                "eligible_items": ["SMOKE-STRAWBERRY-001"],
                "selected_pos_profiles": ["KoPOS Smoke Profile"],
                "internal_rule_state": "must-not-leak",
            }
        ],
        "internal_snapshot_state": "must-not-leak",
    }

    def fake_get_rows(
        doctype: str,
        *,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        captured.update(
            {
                "doctype": doctype,
                "filters": filters,
                "fields": fields,
                "order_by": order_by,
            }
        )
        return [
            {
                "name": "KOPOS-PROMO-SMOKE",
                "status": "Published",
                "pos_profile": "KoPOS Smoke Profile",
                "published_at": "2026-07-12 08:59:00",
                "effective_from": "2026-07-12 08:59:00",
                "snapshot_hash": "a" * 64,
                "snapshot_version": "KOPOS-PROMO-SMOKE",
                "promotion_count": 1,
                "snapshot_payload": json.dumps(snapshot_payload),
            }
        ]

    monkeypatch.setattr(smoke, "_get_rows", fake_get_rows)

    result = smoke._collect_promotion_snapshots(
        [{"promotion_snapshot_version": "KOPOS-PROMO-SMOKE"}]
    )

    assert captured["doctype"] == "KoPOS Promotion Snapshot"
    assert captured["filters"] == {
        "snapshot_version": ["in", ["KOPOS-PROMO-SMOKE"]]
    }
    assert result == [
        {
            "name": "KOPOS-PROMO-SMOKE",
            "status": "Published",
            "pos_profile": "KoPOS Smoke Profile",
            "published_at": "2026-07-12 08:59:00",
            "effective_from": "2026-07-12 08:59:00",
            "snapshot_hash": "a" * 64,
            "snapshot_version": "KOPOS-PROMO-SMOKE",
            "promotion_count": 1,
            "source": "published_snapshot",
            "promotions": [
                {
                    "promotion_id": "SMOKE-MANUAL-10-PCT",
                    "promotion_name": "SMOKE-MANUAL-10-PCT",
                    "promotion_type": "item_discount",
                    "activation_mode": "manual_selectable",
                    "discount_type": "percentage",
                    "discount_value": 10,
                    "eligible_items": ["SMOKE-STRAWBERRY-001"],
                    "selected_pos_profiles": ["KoPOS Smoke Profile"],
                }
            ],
        }
    ]


def test_reliability_item_code_does_not_require_duplicate_barcode_row() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    class Item:
        item_code = "SMOKE-STRAWBERRY-001"

        def get(self, fieldname: str):
            if fieldname == "barcodes":
                return []
            return None

    assert smoke._ensure_demo_drink_barcode(Item()) is False


def test_smoke_device_printers_default_to_mock_printer_endpoint() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    printers = smoke._build_smoke_device_printers()

    assert printers == [
        {
            "role": "receipt",
            "enabled": 1,
            "protocol": "escpos_tcp",
            "host": "127.0.0.1",
            "port": 19100,
            "copies": 1,
        },
        {
            "role": "sticker",
            "enabled": 1,
            "protocol": "tspl_tcp",
            "host": "127.0.0.1",
            "port": 19100,
            "label_width_mm": 35,
            "label_height_mm": 30,
            "copies": 1,
        },
    ]


def test_smoke_device_printers_allow_mock_endpoint_override(monkeypatch) -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    monkeypatch.setenv(smoke.SMOKE_MOCK_PRINTER_HOST_ENV, "10.0.2.2")
    monkeypatch.setenv(smoke.SMOKE_MOCK_PRINTER_PORT_ENV, "19101")

    printers = smoke._build_smoke_device_printers()

    assert [printer["host"] for printer in printers] == ["10.0.2.2", "10.0.2.2"]
    assert [printer["port"] for printer in printers] == [19101, 19101]
    assert {printer["role"] for printer in printers} == {"receipt", "sticker"}


def test_smoke_reset_skips_removed_legacy_device_fields(monkeypatch) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    cleanup_calls: list[str] = []
    credential_reset_calls: list[str] = []

    monkeypatch.setattr(
        frappe.db,
        "get_value",
        lambda doctype, filters, fieldname: "KOPOS-DEVICE-001",
    )
    monkeypatch.setattr(
        frappe,
        "get_meta",
        lambda doctype: SimpleNamespace(has_field=lambda fieldname: True),
    )
    monkeypatch.setattr(frappe.db, "has_column", lambda doctype, fieldname: False)
    monkeypatch.setattr(
        frappe,
        "get_all",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("removed legacy device field must not be queried")
        ),
    )
    monkeypatch.setattr(
        smoke,
        "_delete_smoke_business_rows",
        lambda device_id: cleanup_calls.append(device_id),
    )
    monkeypatch.setattr(
        smoke,
        "_reset_smoke_device_api_credentials",
        lambda device_name: credential_reset_calls.append(device_name),
    )
    monkeypatch.setattr(
        smoke,
        "setup_full_smoke_data",
        lambda erpnext_url=None: {"erpnext_url": erpnext_url, "status": "reseeded"},
    )
    monkeypatch.setattr(frappe.db, "commit", lambda: None)

    result = smoke.reset_smoke_data(erpnext_url="https://erp.example.com")

    assert cleanup_calls == [smoke.SMOKE_DEVICE_ID]
    assert credential_reset_calls == ["KOPOS-DEVICE-001"]
    assert result == {
        "erpnext_url": "https://erp.example.com",
        "status": "reseeded",
    }


def test_smoke_reset_revokes_only_the_fixed_smoke_device_credentials(
    monkeypatch,
) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    state: dict[str, Any] = {
        "api_key": "present",
        "api_secret_present": True,
    }
    mutations: list[tuple[str, Any]] = []

    def fake_get_value(doctype, name, fieldname):
        if doctype == "KoPOS Device":
            return smoke.SMOKE_DEVICE_API_USER
        if doctype == "User" and fieldname == "api_key":
            return state["api_key"]
        return None

    def fake_set_value(doctype, name, fieldname, value, **kwargs):
        mutations.append((fieldname, value))
        state["api_key"] = value

    def fake_exists(doctype, filters):
        if doctype == "User":
            return True
        if doctype == "__Auth":
            return state["api_secret_present"]
        return False

    def fake_delete(doctype, filters):
        mutations.append((doctype, dict(filters)))
        state["api_secret_present"] = False

    monkeypatch.setattr(frappe.db, "get_value", fake_get_value)
    monkeypatch.setattr(frappe.db, "set_value", fake_set_value)
    monkeypatch.setattr(frappe.db, "exists", fake_exists)
    monkeypatch.setattr(frappe.db, "delete", fake_delete)
    monkeypatch.setattr(frappe, "get_all", lambda *args, **kwargs: [])

    smoke._reset_smoke_device_api_credentials("KOPOS-DEVICE-001")

    assert state == {"api_key": None, "api_secret_present": False}
    assert mutations[0] == ("api_key", None)
    assert mutations[1][0] == "__Auth"


def test_smoke_reset_rejects_non_smoke_device_api_user(monkeypatch) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    monkeypatch.setattr(
        frappe.db,
        "get_value",
        lambda doctype, name, fieldname: "merchant-terminal@example.com",
    )
    monkeypatch.setattr(
        frappe.db,
        "set_value",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("merchant credentials must not be changed")
        ),
    )

    with pytest.raises(RuntimeError, match="non-smoke device user"):
        smoke._reset_smoke_device_api_credentials("KOPOS-DEVICE-001")


def test_legacy_path_evidence_treats_removed_device_fields_as_inactive(
    monkeypatch,
) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    monkeypatch.setattr(
        frappe,
        "get_meta",
        lambda doctype: SimpleNamespace(has_field=lambda fieldname: False),
    )
    monkeypatch.setattr(
        smoke,
        "_get_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("removed legacy device field must not be queried")
        ),
    )

    result = smoke._collect_legacy_active_paths(smoke.SMOKE_DEVICE_ID)

    assert set(result) == {
        "pos_invoice",
        "pos_opening_entry",
        "pos_closing_entry",
    }
    for evidence in result.values():
        assert evidence["device_field_present"] is False
        assert evidence["count"] == 0
        assert evidence["records"] == []


def test_legacy_path_evidence_still_reports_active_rows_when_field_exists(
    monkeypatch,
) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    monkeypatch.setattr(
        frappe,
        "get_meta",
        lambda doctype: SimpleNamespace(has_field=lambda fieldname: True),
    )
    monkeypatch.setattr(frappe.db, "has_column", lambda doctype, fieldname: True)

    def fake_get_rows(
        doctype: str,
        *,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        if doctype == "POS Invoice":
            return [{"name": "POS-INV-LEGACY", "docstatus": 1, "status": "Paid"}]
        return []

    monkeypatch.setattr(smoke, "_get_rows", fake_get_rows)

    result = smoke._collect_legacy_active_paths(smoke.SMOKE_DEVICE_ID)

    assert result["pos_invoice"] == {
        "doctype": "POS Invoice",
        "device_field_present": True,
        "count": 1,
        "records": [
            {"name": "POS-INV-LEGACY", "docstatus": 1, "status": "Paid"}
        ],
    }
    assert result["pos_opening_entry"]["count"] == 0
    assert result["pos_closing_entry"]["count"] == 0


def test_legacy_reliability_item_is_retired_from_catalog(monkeypatch) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    class LegacyItem:
        disabled = 0
        is_sales_item = 1
        custom_kopos_availability_mode = "auto"
        custom_fb_recipe_required = 1
        custom_fb_default_recipe = smoke.DEMO_RECIPE_CODE
        custom_fb_track_theoretical_stock = 1

        def save(self, ignore_permissions: bool = False) -> None:
            self.saved = ignore_permissions

    legacy_item = LegacyItem()
    monkeypatch.setattr(frappe.db, "exists", lambda doctype, name: name == smoke.LEGACY_DEMO_DRINK_ITEM)
    monkeypatch.setattr(frappe, "get_doc", lambda doctype, name: legacy_item)

    assert smoke._retire_legacy_demo_drink_item() is True
    assert legacy_item.disabled == 1
    assert legacy_item.is_sales_item == 0
    assert legacy_item.custom_kopos_availability_mode == "force_unavailable"
    assert legacy_item.custom_fb_recipe_required == 0
    assert legacy_item.custom_fb_default_recipe is None
    assert legacy_item.custom_fb_track_theoretical_stock == 0
    assert legacy_item.saved is True


def test_smoke_reset_cleanup_deletes_stale_smoke_device_business_state(monkeypatch) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    tables: dict[str, list[dict[str, Any]]] = {
        "FB Order": [
            {
                "name": "FB-ORDER-STALE",
                "device_id": smoke.SMOKE_DEVICE_ID,
                "order_id": "legacy-order-001",
                "external_idempotency_key": "legacy-idem-001",
                "ingredient_stock_entry": "STE-STALE",
            },
            {
                "name": "FB-ORDER-LIVE",
                "device_id": "LIVE-TAB-001",
                "order_id": "merchant-order-001",
                "external_idempotency_key": "merchant-idem-001",
                "ingredient_stock_entry": "STE-LIVE",
            },
        ],
        "FB Shift": [
            {
                "name": "FB-SHIFT-2026-05-18-00201",
                "device_id": smoke.SMOKE_DEVICE_ID,
                "shift_code": "legacy-open-shift",
                "staff_id": "cashier@example.com",
            },
            {
                "name": "FB-SHIFT-LIVE",
                "device_id": "LIVE-TAB-001",
                "shift_code": "merchant-shift",
                "staff_id": "cashier@merchant.local",
            },
        ],
        "Maybank QR Transaction": [
            {
                "name": "MBQR-SMOKE-STALE",
                "device_id": smoke.SMOKE_DEVICE_ID,
                "transaction_refno": "SMOKE-MOCK-MBQR-STALE",
                "idempotency_key": "SMOKE-MBQR-STALE",
            },
            {
                "name": "MBQR-LIVE",
                "device_id": "LIVE-TAB-001",
                "transaction_refno": "LIVE-MBQR-001",
                "idempotency_key": "merchant-mbqr-idem-001",
            },
        ],
        "FB Return Event": [
            {
                "name": "RET-STALE",
                "fb_order": "FB-ORDER-STALE",
                "return_id": "RETURN-STALE",
                "original_sales_invoice": "SI-STALE",
                "return_sales_invoice": "SI-RETURN-STALE",
                "settlement_doctype": "Journal Entry",
                "settlement_document": "JV-REFUND-STALE",
            }
        ],
        "FB Return Event Line": [
            {
                "name": "RETURN-LINE-STALE",
                "parent": "RET-STALE",
                "reversal_stock_entry": "STE-RETURN-STALE",
            }
        ],
        "Sales Invoice": [
            {
                "name": "SI-STALE",
                "custom_fb_device_id": smoke.SMOKE_DEVICE_ID,
                "custom_fb_order": "FB-ORDER-STALE",
                "custom_fb_idempotency_key": "legacy-idem-001",
            },
            {
                "name": "SI-RETURN-STALE",
                "custom_fb_device_id": smoke.SMOKE_DEVICE_ID,
                "custom_fb_order": "FB-ORDER-STALE",
                "custom_fb_idempotency_key": "smoke-refund-old",
            },
            {
                "name": "SI-LIVE",
                "custom_fb_device_id": "LIVE-TAB-001",
                "custom_fb_order": "FB-ORDER-LIVE",
                "custom_fb_idempotency_key": "merchant-idem-001",
            },
        ],
        "Journal Entry": [{"name": "JV-REFUND-STALE"}],
        "Stock Entry": [
            {"name": "STE-STALE"},
            {"name": "STE-RETURN-STALE"},
            {"name": "STE-LIVE"},
        ],
        "FB Resolved Sale": [
            {
                "name": "RS-STALE",
                "fb_order": "FB-ORDER-STALE",
                "stock_entry_issue": "STE-STALE",
                "stock_entry_reversal": "STE-RETURN-STALE",
            },
            {"name": "RS-LIVE", "fb_order": "FB-ORDER-LIVE"},
        ],
        "Serial and Batch Bundle": [
            {
                "name": "SABB-STALE",
                "voucher_type": "Stock Entry",
                "voucher_no": "STE-STALE",
            },
            {
                "name": "SABB-LIVE",
                "voucher_type": "Stock Entry",
                "voucher_no": "STE-LIVE",
            },
        ],
        "GL Entry": [
            {
                "name": "GL-SI-OLD",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SI-STALE",
            },
            {
                "name": "GL-SI-REUSED-NAME",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SI-STALE",
            },
            {
                "name": "GL-JV-STALE",
                "voucher_type": "Journal Entry",
                "voucher_no": "JV-REFUND-STALE",
            },
            {
                "name": "GL-STOCK-STALE",
                "voucher_type": "Stock Entry",
                "voucher_no": "STE-STALE",
            },
            {
                "name": "GL-SI-LIVE",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SI-LIVE",
            },
        ],
        "Payment Ledger Entry": [
            {
                "name": "PLE-SI-STALE",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SI-STALE",
                "against_voucher_type": "Sales Invoice",
                "against_voucher_no": "SI-STALE",
            },
            {
                "name": "PLE-JV-STALE",
                "voucher_type": "Journal Entry",
                "voucher_no": "JV-REFUND-STALE",
                "against_voucher_type": "Sales Invoice",
                "against_voucher_no": "SI-RETURN-STALE",
            },
            {
                "name": "PLE-SI-LIVE",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SI-LIVE",
                "against_voucher_type": "Sales Invoice",
                "against_voucher_no": "SI-LIVE",
            },
        ],
        "Stock Ledger Entry": [
            {
                "name": "SLE-STALE",
                "voucher_type": "Stock Entry",
                "voucher_no": "STE-STALE",
            },
            {
                "name": "SLE-RETURN-STALE",
                "voucher_type": "Stock Entry",
                "voucher_no": "STE-RETURN-STALE",
            },
            {
                "name": "SLE-LIVE",
                "voucher_type": "Stock Entry",
                "voucher_no": "STE-LIVE",
            },
        ],
        "FB Projection Log": [
            {
                "name": "PROJ-FAILED-ORDER",
                "source_doctype": "FB Order",
                "source_name": "FB-ORDER-STALE",
                "idempotency_key": "legacy-idem-001",
                "projection_id": "GEN-ORDER-001",
            },
            {
                "name": "PROJ-FAILED-SHIFT",
                "source_doctype": "FB Shift",
                "source_name": "FB-SHIFT-2026-05-18-00201",
                "idempotency_key": "legacy-shift-idem-001",
                "projection_id": "GEN-SHIFT-001",
            },
            {
                "name": "PROJ-SMOKE-IDEM-MISSING-SOURCE",
                "source_doctype": "FB Order",
                "source_name": "FB-ORDER-ALREADY-DELETED",
                "idempotency_key": "smoke-tab-a001:shift-1:old",
                "projection_id": "GEN-MISSING-001",
            },
            {
                "name": "PROJ-TASK15",
                "source_doctype": "FB Order",
                "source_name": "FB-ORDER-ALREADY-DELETED",
                "idempotency_key": "task-15-forced-failed-projection",
                "projection_id": "TASK-15-FAILED-001",
            },
            {
                "name": "PROJ-LIVE",
                "source_doctype": "FB Shift",
                "source_name": "FB-SHIFT-LIVE",
                "idempotency_key": "merchant-idem-001",
                "projection_id": "GEN-LIVE-001",
            },
        ],
    }
    existing_names = {
        (doctype, str(row["name"]))
        for doctype, rows in tables.items()
        for row in rows
        if row.get("name")
    }
    deleted: list[tuple[str, str]] = []
    cancelled_stock_entries: list[str] = []

    def row_matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
        for fieldname, expected in (filters or {}).items():
            actual = row.get(fieldname)
            if isinstance(expected, list) and expected[:1] == ["in"]:
                if actual not in expected[1]:
                    return False
            elif isinstance(expected, list) and expected[:1] == ["like"]:
                prefix = str(expected[1]).removesuffix("%")
                if not str(actual or "").startswith(prefix):
                    return False
            elif actual != expected:
                return False
        return True

    def fake_get_all(
        doctype: str,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in tables.get(doctype, []):
            if filters and not row_matches(row, filters):
                continue
            if fields:
                result.append({fieldname: row.get(fieldname) for fieldname in fields})
            else:
                result.append(dict(row))
        return result

    def fake_exists(doctype: str, name: str) -> bool:
        return (doctype, name) in existing_names

    def fake_delete_doc(
        doctype: str,
        name: str,
        force: bool = False,
        ignore_permissions: bool = False,
    ) -> None:
        deleted.append((doctype, name))
        existing_names.discard((doctype, name))

    def fake_get_doc(doctype: str, name: str) -> SimpleNamespace:
        assert doctype == "Stock Entry"
        stock_entry = SimpleNamespace(name=name, docstatus=1)

        def cancel() -> None:
            stock_entry.docstatus = 2
            cancelled_stock_entries.append(name)

        stock_entry.cancel = cancel
        return stock_entry

    def fake_db_delete(doctype: str, filters: dict[str, Any]) -> None:
        retained = []
        for row in tables.get(doctype, []):
            if row_matches(row, filters):
                existing_names.discard((doctype, str(row.get("name"))))
            else:
                retained.append(row)
        tables[doctype] = retained

    monkeypatch.setattr(frappe, "get_all", fake_get_all)
    monkeypatch.setattr(frappe, "get_doc", fake_get_doc)
    monkeypatch.setattr(frappe, "delete_doc", fake_delete_doc, raising=False)
    monkeypatch.setattr(frappe.db, "exists", fake_exists)
    monkeypatch.setattr(frappe.db, "get_value", lambda *args, **kwargs: 1)
    monkeypatch.setattr(frappe.db, "set_value", lambda *args, **kwargs: None)
    monkeypatch.setattr(frappe.db, "delete", fake_db_delete)

    smoke._delete_smoke_business_rows(smoke.SMOKE_DEVICE_ID)

    assert ("FB Shift", "FB-SHIFT-2026-05-18-00201") in deleted
    assert ("FB Order", "FB-ORDER-STALE") in deleted
    assert ("Sales Invoice", "SI-STALE") in deleted
    assert ("Sales Invoice", "SI-RETURN-STALE") in deleted
    assert ("Journal Entry", "JV-REFUND-STALE") in deleted
    assert ("Stock Entry", "STE-RETURN-STALE") in deleted
    assert ("Serial and Batch Bundle", "SABB-STALE") in deleted
    assert ("Maybank QR Transaction", "MBQR-SMOKE-STALE") in deleted
    assert cancelled_stock_entries == ["STE-STALE", "STE-RETURN-STALE"]
    assert ("FB Projection Log", "PROJ-FAILED-ORDER") in deleted
    assert ("FB Projection Log", "PROJ-FAILED-SHIFT") in deleted
    assert ("FB Projection Log", "PROJ-SMOKE-IDEM-MISSING-SOURCE") in deleted
    assert ("FB Projection Log", "PROJ-TASK15") in deleted
    assert ("FB Shift", "FB-SHIFT-LIVE") not in deleted
    assert ("FB Order", "FB-ORDER-LIVE") not in deleted
    assert ("Sales Invoice", "SI-LIVE") not in deleted
    assert ("Maybank QR Transaction", "MBQR-LIVE") not in deleted
    assert ("FB Projection Log", "PROJ-LIVE") not in deleted
    assert [row["name"] for row in tables["GL Entry"]] == ["GL-SI-LIVE"]
    assert [row["name"] for row in tables["Payment Ledger Entry"]] == [
        "PLE-SI-LIVE"
    ]
    assert [row["name"] for row in tables["Stock Ledger Entry"]] == ["SLE-LIVE"]


def test_smoke_reset_fails_loud_if_voucher_gl_rows_survive(monkeypatch) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    monkeypatch.setattr(frappe.db, "delete", lambda doctype, filters: None)

    def fake_get_all(
        doctype: str,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        if doctype == "GL Entry":
            return [{"name": "GL-STALE-REUSED-VOUCHER"}]
        return []

    monkeypatch.setattr(frappe, "get_all", fake_get_all)

    with pytest.raises(
        RuntimeError,
        match="Smoke reset left GL Entry rows for proven smoke vouchers",
    ):
        smoke._delete_smoke_ledger_artifacts(
            sales_invoices=["SI-REUSED-NAME"],
            settlement_journal_entries=[],
            stock_entries=[],
        )


def test_smoke_reset_purges_proven_orphan_ledgers_before_names_are_reused(
    monkeypatch,
) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    smoke_remarks = (
        "FB Order: FB-ORDER-2026-07-10-00028\n"
        "Shift: FB-SHIFT-2026-07-10-00027\n"
        f"Device ID: {smoke.SMOKE_DEVICE_ID}"
    )
    merchant_remarks = (
        "FB Order: FB-ORDER-MERCHANT\n"
        "Shift: FB-SHIFT-MERCHANT\n"
        "Device ID: MERCHANT-TAB-001"
    )
    tables: dict[str, list[dict[str, Any]]] = {
        "GL Entry": [
            {
                "name": "GL-SMOKE-SI-DEBIT",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SI-ORPHAN-SMOKE",
                "remarks": smoke_remarks,
            },
            {
                "name": "GL-SMOKE-SI-CREDIT",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SI-ORPHAN-SMOKE",
                "remarks": smoke_remarks,
            },
            {
                "name": "GL-SMOKE-STOCK",
                "voucher_type": "Stock Entry",
                "voucher_no": "STE-ORPHAN-SMOKE",
                "remarks": smoke_remarks,
            },
            {
                "name": "GL-OLD-SMOKE-REUSED",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SI-REUSED-BY-MERCHANT",
                "remarks": smoke_remarks,
            },
            {
                "name": "GL-CURRENT-MERCHANT-REUSED",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SI-REUSED-BY-MERCHANT",
                "remarks": merchant_remarks,
            },
            {
                "name": "GL-LIVE",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SI-LIVE",
                "remarks": merchant_remarks,
            },
        ],
        "Payment Ledger Entry": [
            {
                "name": "PLE-SMOKE-DEBIT",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SI-ORPHAN-SMOKE",
                "against_voucher_type": "Sales Invoice",
                "against_voucher_no": "SI-ORPHAN-SMOKE",
            },
            {
                "name": "PLE-REUSED-MERCHANT",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SI-REUSED-BY-MERCHANT",
                "against_voucher_type": "Sales Invoice",
                "against_voucher_no": "SI-REUSED-BY-MERCHANT",
            },
            {
                "name": "PLE-LIVE",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SI-LIVE",
                "against_voucher_type": "Sales Invoice",
                "against_voucher_no": "SI-LIVE",
            },
        ],
        "Stock Ledger Entry": [
            {
                "name": "SLE-SMOKE",
                "voucher_type": "Stock Entry",
                "voucher_no": "STE-ORPHAN-SMOKE",
            },
            {
                "name": "SLE-LIVE",
                "voucher_type": "Stock Entry",
                "voucher_no": "STE-LIVE",
            },
        ],
    }
    existing_vouchers = {
        ("Sales Invoice", "SI-REUSED-BY-MERCHANT"),
        ("Sales Invoice", "SI-LIVE"),
        ("Stock Entry", "STE-LIVE"),
    }

    def row_matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
        for fieldname, expected in filters.items():
            actual = row.get(fieldname)
            if isinstance(expected, list) and expected[:1] == ["in"]:
                if actual not in expected[1]:
                    return False
            elif isinstance(expected, list) and expected[:1] == ["like"]:
                needle = str(expected[1]).strip("%")
                if needle not in str(actual or ""):
                    return False
            elif actual != expected:
                return False
        return True

    def fake_get_all(
        doctype: str,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in tables.get(doctype, [])
            if not filters or row_matches(row, filters)
        ]
        if not fields:
            return [dict(row) for row in rows]
        return [
            {fieldname: row.get(fieldname) for fieldname in fields}
            for row in rows
        ]

    def fake_db_delete(doctype: str, filters: dict[str, Any]) -> None:
        tables[doctype] = [
            row
            for row in tables.get(doctype, [])
            if not row_matches(row, filters)
        ]

    monkeypatch.setattr(frappe, "get_all", fake_get_all)
    monkeypatch.setattr(
        frappe.db,
        "exists",
        lambda doctype, name: (doctype, name) in existing_vouchers,
    )
    monkeypatch.setattr(
        frappe.db,
        "get_value",
        lambda doctype, name, fieldname: (
            "MERCHANT-TAB-001"
            if (doctype, name, fieldname)
            == (
                "Sales Invoice",
                "SI-REUSED-BY-MERCHANT",
                "custom_fb_device_id",
            )
            else None
        ),
    )
    monkeypatch.setattr(frappe.db, "delete", fake_db_delete)

    smoke._delete_orphan_smoke_ledger_artifacts(smoke.SMOKE_DEVICE_ID)

    assert [row["name"] for row in tables["GL Entry"]] == [
        "GL-CURRENT-MERCHANT-REUSED",
        "GL-LIVE",
    ]
    assert [row["name"] for row in tables["Payment Ledger Entry"]] == [
        "PLE-REUSED-MERCHANT",
        "PLE-LIVE",
    ]
    assert [row["name"] for row in tables["Stock Ledger Entry"]] == ["SLE-LIVE"]


def test_smoke_stock_cancel_ignores_links_for_proven_smoke_voucher(
    monkeypatch,
) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    stock_entry = SimpleNamespace(
        name="STE-SMOKE-LINKED",
        docstatus=1,
        flags=SimpleNamespace(ignore_links=False),
    )

    def cancel() -> None:
        if not stock_entry.flags.ignore_links:
            raise RuntimeError("linked FB Order blocks cancellation")
        stock_entry.docstatus = 2

    stock_entry.cancel = cancel
    monkeypatch.setattr(frappe.db, "exists", lambda doctype, name: True)
    monkeypatch.setattr(frappe.db, "get_value", lambda *args, **kwargs: 1)
    monkeypatch.setattr(frappe, "get_doc", lambda doctype, name: stock_entry)

    smoke._cancel_submitted_smoke_stock_entries([stock_entry.name])

    assert stock_entry.flags.ignore_links is True
    assert stock_entry.docstatus == 2


def test_smoke_stock_cancel_still_fails_loudly_on_real_cancel_error(
    monkeypatch,
) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    stock_entry = SimpleNamespace(
        name="STE-SMOKE-BROKEN",
        docstatus=1,
        flags=SimpleNamespace(ignore_links=False),
        cancel=lambda: (_ for _ in ()).throw(RuntimeError("ledger failure")),
    )
    monkeypatch.setattr(frappe.db, "exists", lambda doctype, name: True)
    monkeypatch.setattr(frappe.db, "get_value", lambda *args, **kwargs: 1)
    monkeypatch.setattr(frappe, "get_doc", lambda doctype, name: stock_entry)

    with pytest.raises(
        RuntimeError,
        match="Failed to cancel smoke-owned Stock Entry STE-SMOKE-BROKEN",
    ):
        smoke._cancel_submitted_smoke_stock_entries([stock_entry.name])

    assert stock_entry.flags.ignore_links is True
