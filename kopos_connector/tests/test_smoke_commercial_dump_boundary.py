# pyright: reportMissingImports=false

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


def _smoke_module():
    install_fake_frappe_modules()

    from kopos_connector import smoke

    return smoke


def test_commercial_dump_does_not_call_recipe_or_inventory_collectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _smoke_module()

    import frappe

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("commercial dump consulted optional recipe or inventory data")

    optional_fields = {
        "FB Shift": {"warehouse"},
        "FB Order": {"booth_warehouse", "stock_status", "ingredient_stock_entry"},
    }
    requested_fields: dict[str, list[str]] = {}

    def commercial_get_rows(
        doctype: str,
        *,
        fields: list[str] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        del kwargs
        selected_fields = list(fields or [])
        requested_fields[doctype] = selected_fields
        forbidden_fields = optional_fields.get(doctype, set())
        if forbidden_fields.intersection(selected_fields):
            raise AssertionError(
                f"default commercial dump selected absent optional columns for {doctype}"
            )
        return []

    for name in (
        "_collect_inventory_mutation_audit",
        "_collect_ingredient_stock_entries",
        "_collect_ingredient_bin_balances",
        "_collect_stock_entries",
    ):
        monkeypatch.setattr(smoke, name, forbidden)

    monkeypatch.setattr(smoke, "_get_rows", commercial_get_rows)
    monkeypatch.setattr(smoke, "_collect_sales_invoices", lambda device_id: [])
    monkeypatch.setattr(smoke, "_collect_promotion_snapshots", lambda orders: [])
    monkeypatch.setattr(smoke, "_collect_manual_qr_reconciliations", lambda device_id: [])
    monkeypatch.setattr(smoke, "_collect_maybank_qr_transactions", lambda device_id: [])
    monkeypatch.setattr(smoke, "_collect_return_records", lambda *args, **kwargs: [])
    monkeypatch.setattr(smoke, "_collect_projection_state", lambda *args: {})
    monkeypatch.setattr(smoke, "_collect_legacy_active_paths", lambda device_id: {})
    monkeypatch.setattr(smoke, "_build_idempotency_summary", lambda *args: {})
    monkeypatch.setattr(smoke, "_collect_void_records", lambda *args: [])
    monkeypatch.setattr(smoke, "_maybank_qr_policy", lambda: {})
    monkeypatch.setattr(smoke, "_maybank_qr_contract", lambda: {})
    monkeypatch.setattr(
        frappe,
        "get_all",
        lambda doctype, *args, **kwargs: (
            (_ for _ in ()).throw(
                AssertionError("commercial dump queried optional modifier authoring data")
            )
            if doctype == "FB Modifier Group"
            else []
        ),
    )
    monkeypatch.setattr(frappe.db, "get_value", forbidden)

    result = smoke._collect_smoke_business_state(
        "SMOKE-TAB-A001",
        ingredient_warehouse="Store - KMY",
        company="KoPOS Malaysia Sdn Bhd",
        site_timezone="Asia/Kuala_Lumpur",
    )

    assert result["inventory_evaluation"] == "excluded_not_evaluated"
    assert result["demo_recipe"] is None
    assert "modifier_groups" not in result
    assert "ingredient_stock_entries" not in result
    assert "ingredient_bin_balances" not in result
    assert "inventory_mutation_audit" not in result
    assert optional_fields["FB Shift"].isdisjoint(requested_fields["FB Shift"])
    assert optional_fields["FB Order"].isdisjoint(requested_fields["FB Order"])


@pytest.mark.inventory_regression
def test_inventory_regression_dump_explicitly_selects_optional_stock_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _smoke_module()

    import frappe

    requested_fields: dict[str, list[str]] = {}

    def capture_get_rows(
        doctype: str,
        *,
        fields: list[str] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        del kwargs
        requested_fields[doctype] = list(fields or [])
        return []

    monkeypatch.setattr(smoke, "_get_rows", capture_get_rows)
    monkeypatch.setattr(smoke, "_collect_inventory_mutation_audit", lambda **kwargs: {})
    monkeypatch.setattr(smoke, "_collect_ingredient_stock_entries", lambda orders: [])
    monkeypatch.setattr(smoke, "_collect_ingredient_bin_balances", lambda warehouse: [])
    monkeypatch.setattr(smoke, "_collect_sales_invoices", lambda device_id: [])
    monkeypatch.setattr(smoke, "_collect_promotion_snapshots", lambda orders: [])
    monkeypatch.setattr(smoke, "_collect_manual_qr_reconciliations", lambda device_id: [])
    monkeypatch.setattr(smoke, "_collect_maybank_qr_transactions", lambda device_id: [])
    monkeypatch.setattr(smoke, "_collect_return_records", lambda *args, **kwargs: [])
    monkeypatch.setattr(smoke, "_collect_projection_state", lambda *args: {})
    monkeypatch.setattr(smoke, "_collect_legacy_active_paths", lambda device_id: {})
    monkeypatch.setattr(smoke, "_build_idempotency_summary", lambda *args: {})
    monkeypatch.setattr(smoke, "_collect_void_records", lambda *args: [])
    monkeypatch.setattr(smoke, "_maybank_qr_policy", lambda: {})
    monkeypatch.setattr(smoke, "_maybank_qr_contract", lambda: {})
    monkeypatch.setattr(frappe, "get_all", lambda *args, **kwargs: [])
    monkeypatch.setattr(frappe.db, "get_value", lambda *args, **kwargs: None)

    smoke._collect_smoke_business_state(
        "SMOKE-TAB-A001",
        ingredient_warehouse="Store - KMY",
        company="KoPOS Malaysia Sdn Bhd",
        site_timezone="Asia/Kuala_Lumpur",
        include_inventory_regression=True,
    )

    assert "warehouse" in requested_fields["FB Shift"]
    assert {
        "booth_warehouse",
        "stock_status",
        "ingredient_stock_entry",
    }.issubset(requested_fields["FB Order"])


def test_commercial_modifier_dump_uses_immutable_snapshot_without_resolved_sale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _smoke_module()

    import frappe

    monkeypatch.setattr(
        frappe,
        "get_doc",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("commercial modifier dump consulted FB Resolved Sale")
        ),
    )
    snapshot = [
        {
            "modifier_group": "SIZE",
            "modifier": "LARGE",
            "price_adjustment": "2.00",
            "affects_stock": False,
            "affects_recipe": False,
        }
    ]
    order = SimpleNamespace(
        items=[
            SimpleNamespace(
                line_id="LINE-1",
                item="AMERICANO",
                item_name_snapshot="Americano",
                qty="1",
                uom="Cup",
                unit_price="10.00",
                modifier_total="2.00",
                discount_amount="0.00",
                line_total="12.00",
                recipe=None,
                recipe_version=None,
                is_recipe_managed=0,
                resolved_sale="RESOLVED-OPTIONAL-1",
                resolved_components_snapshot="[]",
                selected_modifiers=[],
                commercial_modifier_snapshot_json=json.dumps(snapshot),
                promotion_allocations_json="[]",
            )
        ]
    )

    rows = smoke._collect_fb_order_items(order)

    assert rows[0]["modifier_total"] == "2.00"
    assert rows[0]["selected_modifiers"] == snapshot


def test_commercial_return_dump_never_reads_stock_or_resolved_sale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _smoke_module()

    import frappe

    requested_fields: dict[str, list[str]] = {}

    def fake_get_rows(
        doctype: str,
        *,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        del filters, order_by
        requested_fields[doctype] = list(fields or [])
        if doctype == "FB Return Event":
            return [
                {
                    "name": "FB-RETURN-1",
                    "return_id": "REFUND-1",
                    "fb_order": "FB-ORDER-1",
                    "original_sales_invoice": "SINV-1",
                    "return_sales_invoice": "",
                    "refund_method": "cash",
                    "request_fingerprint": "a" * 64,
                    "approval_token_id": "APPROVAL-1",
                    "approved_by_manager": "manager@example.com",
                    "settlement_doctype": "",
                    "settlement_document": "",
                    "settlement_status": "Posted",
                    "settlement_amount": "12.00",
                    "settlement_tenders_json": "{}",
                    "status": "Submitted",
                    "docstatus": 1,
                }
            ]
        if doctype == "FB Return Event Line":
            return [
                {
                    "name": "RETURN-LINE-1",
                    "parent": "FB-RETURN-1",
                    "idx": 1,
                    "original_sales_invoice_item": "SINV-ITEM-1",
                    "original_fb_order_line_ref": "ORDER-LINE-1",
                    "qty_returned": "1.000000",
                    "commercial_modifier_snapshot_json": "[]",
                }
            ]
        if doctype in {"Stock Entry", "Stock Ledger Entry", "Bin"}:
            raise AssertionError(f"commercial return dump queried {doctype}")
        if doctype == "GL Entry":
            return []
        raise AssertionError(f"unexpected doctype {doctype}")

    monkeypatch.setattr(smoke, "_get_rows", fake_get_rows)
    monkeypatch.setattr(
        smoke,
        "_collect_stock_entries",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("commercial return dump collected Stock Entries")
        ),
    )
    monkeypatch.setattr(frappe.db, "get_value", lambda *args, **kwargs: None)

    records = smoke._collect_return_records([{"name": "FB-ORDER-1"}])

    assert "return_to_stock" not in requested_fields["FB Return Event"]
    assert "original_resolved_sale" not in requested_fields["FB Return Event Line"]
    assert "reversal_stock_entry" not in requested_fields["FB Return Event Line"]
    assert records[0]["lines"] == [
        {
            "name": "RETURN-LINE-1",
            "original_sales_invoice_item": "SINV-ITEM-1",
            "original_fb_order_line_ref": "ORDER-LINE-1",
            "qty_returned": "1.000000",
            "commercial_modifier_snapshot_json": "[]",
        }
    ]
    assert "return_to_stock" not in records[0]
    assert "reversal_stock_entries" not in records[0]


def test_default_smoke_base_never_calls_optional_recipe_or_inventory_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _smoke_module()

    import frappe

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("default smoke seed consulted optional recipe/inventory setup")

    monkeypatch.setattr(smoke, "_ensure_smoke_company", lambda: "KoPOS Malaysia")
    monkeypatch.setattr(smoke, "_ensure_customer", lambda company: "Customer")
    monkeypatch.setattr(smoke, "_ensure_warehouse", lambda company: "Store - KMY")
    monkeypatch.setattr(smoke, "_ensure_cost_center", lambda company: "Main - KMY")
    monkeypatch.setattr(smoke, "_ensure_cash_account", lambda company: "Cash - KMY")
    monkeypatch.setattr(smoke, "_ensure_bank_account", lambda company: "Bank - KMY")
    monkeypatch.setattr(smoke, "_ensure_expense_account", lambda company: "Expense - KMY")
    monkeypatch.setattr(smoke, "_ensure_mode_of_payment", lambda *args: None)
    monkeypatch.setattr(smoke, "_ensure_pos_profile", lambda **kwargs: "Smoke POS")
    monkeypatch.setattr(
        smoke, "_ensure_commercial_smoke_item", lambda: smoke.DEMO_DRINK_ITEM
    )
    monkeypatch.setattr(smoke, "_ensure_fb_modifier_group", forbidden)
    monkeypatch.setattr(smoke, "_ensure_item", forbidden)
    monkeypatch.setattr(smoke, "_ensure_demo_recipe", forbidden)
    monkeypatch.setattr(smoke, "set_demo_ingredient_quantities", forbidden)
    monkeypatch.setattr(frappe.db, "get_value", forbidden)
    monkeypatch.setattr(frappe.db, "commit", lambda: None)

    result = smoke._ensure_smoke_base_data()

    assert result["item_code"] == smoke.DEMO_DRINK_ITEM
    assert result["recipe"] is None
    assert result["inventory_evaluation"] == "excluded_not_evaluated"


def test_default_full_smoke_seed_does_not_post_ingredient_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _smoke_module()

    import frappe
    from kopos_connector.api import provisioning

    monkeypatch.delenv(smoke.SMOKE_INVENTORY_ACCEPTANCE_ENV, raising=False)
    monkeypatch.delenv(smoke.SMOKE_INVENTORY_EVALUATION_ENV, raising=False)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("default full smoke seed posted optional ingredient stock")

    base = {
        "company": "KoPOS Malaysia",
        "pos_profile": "Smoke POS",
        "warehouse": "Store - KMY",
        "item_code": smoke.DEMO_DRINK_ITEM,
        "recipe": None,
        "inventory_evaluation": "excluded_not_evaluated",
    }
    device = SimpleNamespace(
        name="SMOKE-DEVICE-DOC",
        device_name="Smoke Test Tablet",
        device_prefix="SMK",
    )
    monkeypatch.setattr(smoke, "setup_refund_smoke_data", lambda **kwargs: base)
    monkeypatch.setattr(smoke, "_ensure_kopos_device", lambda **kwargs: device)
    monkeypatch.setattr(
        provisioning,
        "ensure_device_api_credentials",
        lambda device_doc: {"api_key": "key", "api_secret": "secret"},
    )
    monkeypatch.setattr(
        provisioning,
        "create_pos_provisioning",
        lambda **kwargs: {"token": "token"},
    )
    monkeypatch.setattr(smoke, "set_demo_ingredient_quantities", forbidden)
    monkeypatch.setattr(smoke, "_ensure_demo_promotion", lambda profile: "PROMO")
    monkeypatch.setattr(smoke, "_ensure_promotion_snapshot", lambda profile: {})
    monkeypatch.setattr(smoke, "_get_demo_currency", lambda company: "MYR")
    time_zone_writes: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        frappe.db,
        "set_single_value",
        lambda doctype, fieldname, value: time_zone_writes.append(
            (doctype, fieldname, value)
        ),
        raising=False,
    )
    monkeypatch.setattr(frappe.db, "commit", lambda: None)
    frappe.local.site = "smoke.local"

    result = smoke.setup_full_smoke_data(erpnext_url="https://erp.example.com")

    assert result["recipe"] is None
    assert result["inventory_evaluation"] == "excluded_not_evaluated"
    assert result["time_zone"] == "Asia/Kuala_Lumpur"
    assert time_zone_writes == [
        ("System Settings", "time_zone", "Asia/Kuala_Lumpur")
    ]
    assert "stock_item_code" not in result


def test_full_smoke_inventory_mode_is_campaign_aware_and_explicit_values_win(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _smoke_module()

    monkeypatch.delenv(smoke.SMOKE_INVENTORY_ACCEPTANCE_ENV, raising=False)
    monkeypatch.delenv(smoke.SMOKE_INVENTORY_EVALUATION_ENV, raising=False)
    assert smoke._full_smoke_inventory_regression_requested(None) is False

    monkeypatch.setenv(smoke.SMOKE_INVENTORY_ACCEPTANCE_ENV, "false")
    assert smoke._full_smoke_inventory_regression_requested(None) is False

    monkeypatch.setenv(smoke.SMOKE_INVENTORY_ACCEPTANCE_ENV, "true")
    monkeypatch.setenv(
        smoke.SMOKE_INVENTORY_EVALUATION_ENV,
        "included_evaluated",
    )
    assert smoke._full_smoke_inventory_regression_requested(None) is True
    assert smoke._full_smoke_inventory_regression_requested(False) is False


def test_included_campaign_seed_checks_real_economics_before_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _smoke_module()

    import frappe
    from kopos_connector.api import provisioning

    monkeypatch.setenv(smoke.SMOKE_INVENTORY_ACCEPTANCE_ENV, "true")
    monkeypatch.setenv(
        smoke.SMOKE_INVENTORY_EVALUATION_ENV,
        "included_evaluated",
    )
    calls: list[str] = []
    base = {
        "company": "KoPOS Malaysia",
        "pos_profile": "Smoke POS",
        "warehouse": "Store - KMY",
        "item_code": smoke.DEMO_DRINK_ITEM,
        "recipe": smoke.DEMO_RECIPE_CODE,
        "inventory_evaluation": "included_evaluated",
    }
    device = SimpleNamespace(
        name="SMOKE-DEVICE-DOC",
        device_name="Smoke Test Tablet",
        device_prefix="SMK",
    )

    def setup_refund_smoke_data(**kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"include_inventory_regression": True}
        return base

    monkeypatch.setattr(smoke, "setup_refund_smoke_data", setup_refund_smoke_data)
    monkeypatch.setattr(smoke, "_ensure_kopos_device", lambda **kwargs: device)
    monkeypatch.setattr(
        provisioning,
        "ensure_device_api_credentials",
        lambda device_doc: {"api_key": "key", "api_secret": "secret"},
    )
    monkeypatch.setattr(
        provisioning,
        "create_pos_provisioning",
        lambda **kwargs: {"token": "token"},
    )
    monkeypatch.setattr(
        smoke,
        "set_demo_ingredient_quantities",
        lambda: calls.append("stock"),
    )
    monkeypatch.setattr(
        smoke,
        "_ensure_demo_promotion",
        lambda profile: calls.append("promotion") or "PROMO",
    )
    monkeypatch.setattr(
        smoke,
        "_ensure_demo_promotion_economics",
        lambda promotion, profile: calls.append("economics") or {
            "status": "Ready",
            "economics_hash": "a" * 64,
        },
    )
    monkeypatch.setattr(
        smoke,
        "_ensure_promotion_snapshot",
        lambda profile: calls.append("publish") or {},
    )
    monkeypatch.setattr(smoke, "_get_demo_currency", lambda company: "MYR")
    monkeypatch.setattr(frappe.db, "set_single_value", lambda *args: None, raising=False)
    monkeypatch.setattr(frappe.db, "commit", lambda: None)
    frappe.local.site = "smoke.local"

    result = smoke.setup_full_smoke_data(erpnext_url="https://erp.example.com")

    assert result["inventory_evaluation"] == "included_evaluated"
    assert calls == ["stock", "promotion", "economics", "publish"]


def test_default_smoke_reset_never_queries_optional_inventory_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _smoke_module()

    import frappe

    forbidden_doctypes = {
        "Bin",
        "FB Resolved Sale",
        "FB Return Event Line",
        "Serial and Batch Bundle",
        "Stock Entry",
        "Stock Entry Detail",
        "Stock Ledger Entry",
    }
    forbidden_fields = {
        "ingredient_stock_entry",
        "original_resolved_sale",
        "reversal_stock_entry",
        "stock_entry_issue",
        "stock_entry_reversal",
    }
    queried_doctypes: list[str] = []

    def fake_get_all(
        doctype: str,
        *,
        fields: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        del kwargs
        assert doctype not in forbidden_doctypes
        assert not (set(fields or []) & forbidden_fields)
        queried_doctypes.append(doctype)
        if doctype == "GL Entry" and filters and "remarks" in filters:
            # A stale inventory-only GL row must be ignored without looking up
            # its optional Stock Entry parent.
            return [
                {
                    "name": "GL-STOCK-OPTIONAL",
                    "voucher_type": "Stock Entry",
                    "voucher_no": "STE-OPTIONAL",
                    "remarks": f"Device ID: {smoke.SMOKE_DEVICE_ID}",
                }
            ]
        return []

    monkeypatch.setattr(frappe, "get_all", fake_get_all)
    monkeypatch.setattr(
        frappe.db,
        "exists",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("default reset looked up an optional inventory parent")
        ),
    )
    monkeypatch.setattr(frappe.db, "delete", lambda *args, **kwargs: None)
    monkeypatch.setattr(frappe.db, "commit", lambda: None)

    smoke._delete_smoke_business_rows(smoke.SMOKE_DEVICE_ID)

    assert "FB Order" in queried_doctypes
    assert "Sales Invoice" in queried_doctypes
