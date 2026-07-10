# pyright: reportMissingImports=false

from __future__ import annotations

from typing import Any

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


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
        "FB Return Event": [
            {
                "name": "RET-STALE",
                "fb_order": "FB-ORDER-STALE",
                "return_id": "RETURN-STALE",
                "original_sales_invoice": "SI-STALE",
                "return_sales_invoice": "SI-RETURN-STALE",
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
        "Stock Entry": [{"name": "STE-STALE"}, {"name": "STE-LIVE"}],
        "FB Resolved Sale": [
            {"name": "RS-STALE", "fb_order": "FB-ORDER-STALE"},
            {"name": "RS-LIVE", "fb_order": "FB-ORDER-LIVE"},
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

    monkeypatch.setattr(frappe, "get_all", fake_get_all)
    monkeypatch.setattr(frappe, "delete_doc", fake_delete_doc, raising=False)
    monkeypatch.setattr(frappe.db, "exists", fake_exists)
    monkeypatch.setattr(frappe.db, "get_value", lambda *args, **kwargs: 1)
    monkeypatch.setattr(frappe.db, "set_value", lambda *args, **kwargs: None)

    smoke._delete_smoke_business_rows(smoke.SMOKE_DEVICE_ID)

    assert ("FB Shift", "FB-SHIFT-2026-05-18-00201") in deleted
    assert ("FB Order", "FB-ORDER-STALE") in deleted
    assert ("Sales Invoice", "SI-STALE") in deleted
    assert ("Sales Invoice", "SI-RETURN-STALE") in deleted
    assert ("FB Projection Log", "PROJ-FAILED-ORDER") in deleted
    assert ("FB Projection Log", "PROJ-FAILED-SHIFT") in deleted
    assert ("FB Projection Log", "PROJ-SMOKE-IDEM-MISSING-SOURCE") in deleted
    assert ("FB Projection Log", "PROJ-TASK15") in deleted
    assert ("FB Shift", "FB-SHIFT-LIVE") not in deleted
    assert ("FB Order", "FB-ORDER-LIVE") not in deleted
    assert ("Sales Invoice", "SI-LIVE") not in deleted
    assert ("FB Projection Log", "PROJ-LIVE") not in deleted
