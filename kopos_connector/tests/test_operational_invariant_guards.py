from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

import frappe

from kopos_connector.api import devices, fb_orders


CONNECTOR_ROOT = Path(__file__).resolve().parents[1]


def test_operational_doctypes_have_one_controller_owned_lifecycle() -> None:
    hooks_tree = ast.parse((CONNECTOR_ROOT / "hooks.py").read_text())
    assignments = {
        target.id
        for node in hooks_tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "doc_events" not in assignments

    for module in (
        "api/fb_waste.py",
        "api/fb_refill.py",
        "api/fb_remakes.py",
        "api/fb_returns.py",
    ):
        content = (CONNECTOR_ROOT / module).read_text()
        assert "def on_submit_" not in content


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
