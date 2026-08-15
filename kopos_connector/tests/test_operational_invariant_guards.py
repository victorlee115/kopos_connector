from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

import frappe

from kopos_connector import hooks
from kopos_connector.api import devices, fb_orders


CONNECTOR_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_ACTIVE_LEGACY_TERMS = {
    "POS Invoice",
    "POS Opening Entry",
    "POS Closing Entry",
    "pos_invoice",
    "pos_opening_entry",
    "pos_closing_entry",
}
AVAILABILITY_STOCK_DOC_EVENTS = {
    doctype: {
        "on_submit": "kopos_connector.kopos.services.inventory_autopilot.availability_events.on_stock_document_submit"
    }
    for doctype in ("Purchase Receipt", "Stock Entry", "Stock Reconciliation")
}


def test_operational_doctypes_have_one_controller_owned_lifecycle() -> None:
    assert hooks.doc_events == AVAILABILITY_STOCK_DOC_EVENTS

    content = (CONNECTOR_ROOT / "api/fb_returns.py").read_text()
    assert "def on_submit_" not in content


@pytest.mark.inventory_regression
def test_optional_operational_doctypes_have_controller_owned_lifecycle() -> None:
    for module in (
        "api/fb_waste.py",
        "api/fb_refill.py",
        "api/fb_remakes.py",
    ):
        content = (CONNECTOR_ROOT / module).read_text()
        assert "def on_submit_" not in content


def test_active_hook_install_report_and_workspace_surfaces_are_legacy_free() -> None:
    report_sources = sorted(
        path
        for path in (CONNECTOR_ROOT / "kopos" / "report").rglob("*")
        if path.suffix in {".json", ".py", ".js"}
    )
    active_sources = [
        CONNECTOR_ROOT / "hooks.py",
        CONNECTOR_ROOT / "install" / "install.py",
        *report_sources,
        *sorted((CONNECTOR_ROOT / "kopos" / "workspace").glob("*/*.json")),
    ]

    for source_path in active_sources:
        content = source_path.read_text()
        for forbidden in FORBIDDEN_ACTIVE_LEGACY_TERMS:
            assert forbidden not in content, f"{source_path}: {forbidden}"


def test_legacy_modifier_analytics_are_migration_only() -> None:
    hooks_source = (CONNECTOR_ROOT / "hooks.py").read_text()
    install_source = (CONNECTOR_ROOT / "install" / "install.py").read_text()
    modifiers_tree = ast.parse((CONNECTOR_ROOT / "api" / "modifiers.py").read_text())
    analytics_names = {
        "aggregate_modifier_stats",
        "get_modifier_sales_report",
        "aggregate_modifier_stats_range",
        "retry_failed_aggregations",
    }
    analytics_functions = {
        node.name: node
        for node in modifiers_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in analytics_names
    }

    assert analytics_functions.keys() == analytics_names
    assert all(not node.decorator_list for node in analytics_functions.values())
    assert "aggregate_modifier_stats" not in hooks_source
    assert "backfill_kopos_modifiers_to_fb" not in install_source
    legacy_report_dir = (
        CONNECTOR_ROOT / "kopos" / "report" / "modifier_sales_analytics"
    )
    assert not legacy_report_dir.exists() or not any(
        path.suffix in {".json", ".py", ".js"}
        for path in legacy_report_dir.iterdir()
    )

    patch_entries = (CONNECTOR_ROOT / "patches.txt").read_text().splitlines()
    assert "kopos_connector.patches.backfill_fb_modifiers_from_kopos" in patch_entries
    assert "kopos_connector.patches.quarantine_legacy_modifier_report" in patch_entries


def test_nested_orphan_hooks_module_is_absent() -> None:
    assert not (CONNECTOR_ROOT / "kopos" / "hooks.py").exists()
    assert not (CONNECTOR_ROOT / "install" / "install.py.bak").exists()


def test_active_smoke_setup_never_creates_legacy_pos_documents() -> None:
    smoke_source = (CONNECTOR_ROOT / "smoke.py").read_text()

    assert "def _ensure_pos_opening_entry" not in smoke_source
    assert "def inspect_refund_draft" not in smoke_source
    assert 'settings.invoice_type = "POS Invoice"' not in smoke_source
    assert 'frappe.get_doc("POS Invoice"' not in smoke_source


def test_internal_fb_order_implementation_is_not_whitelisted() -> None:
    tree = ast.parse((CONNECTOR_ROOT / "kopos/api/fb_orders.py").read_text())
    guarded_names = {"submit_order", "get_order_status", "retry_failed_projections"}
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in guarded_names
    }
    assert functions.keys() == guarded_names
    assert all(not node.decorator_list for node in functions.values())


def test_public_fb_order_status_authorizes_the_owning_device(monkeypatch) -> None:
    order = SimpleNamespace(name="FB-ORDER-1", device_id="DEVICE-A")
    authorized: list[str] = []
    monkeypatch.setattr(frappe, "get_doc", lambda doctype, name: order)
    monkeypatch.setattr(
        fb_orders,
        "require_device_context",
        lambda device_id=None, name=None: authorized.append(str(device_id)),
    )
    monkeypatch.setattr(
        fb_orders.fb_orders_impl,
        "get_order_status",
        lambda name: {"status": "ok", "fb_order": name},
    )

    result = fb_orders.get_order_status("FB-ORDER-1")

    assert result == {"status": "ok", "fb_order": "FB-ORDER-1"}
    assert authorized == ["DEVICE-A"]


def test_device_operational_scope_rejects_cross_profile_warehouse(monkeypatch) -> None:
    device = SimpleNamespace(
        name="DEVICE-A",
        device_id="DEVICE-A",
        enabled=1,
        pos_profile="PROFILE-A",
    )
    profile = SimpleNamespace(
        name="PROFILE-A",
        company="COMPANY-A",
        warehouse="WAREHOUSE-A",
        currency="MYR",
    )
    monkeypatch.setattr(
        devices,
        "require_device_context",
        lambda device_id=None, name=None: device,
    )
    monkeypatch.setattr(frappe, "get_cached_doc", lambda doctype, name: profile)

    with pytest.raises(
        frappe.ValidationError,
        match="Warehouse WAREHOUSE-B is outside KoPOS Device DEVICE-A scope",
    ):
        devices.require_device_operational_scope(
            "DEVICE-A",
            company="COMPANY-A",
            warehouse="WAREHOUSE-B",
            currency="MYR",
        )


def test_device_operational_scope_accepts_matching_profile(monkeypatch) -> None:
    device = SimpleNamespace(
        name="DEVICE-A",
        device_id="DEVICE-A",
        enabled=1,
        pos_profile="PROFILE-A",
    )
    profile = SimpleNamespace(
        name="PROFILE-A",
        company="COMPANY-A",
        warehouse="WAREHOUSE-A",
        currency="MYR",
    )
    monkeypatch.setattr(
        devices,
        "require_device_context",
        lambda device_id=None, name=None: device,
    )
    monkeypatch.setattr(frappe, "get_cached_doc", lambda doctype, name: profile)

    resolved_device, resolved_profile = devices.require_device_operational_scope(
        "DEVICE-A",
        company="COMPANY-A",
        warehouse="WAREHOUSE-A",
        currency="MYR",
    )

    assert resolved_device is device
    assert resolved_profile is profile
