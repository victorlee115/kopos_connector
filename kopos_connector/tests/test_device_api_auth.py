# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

from __future__ import annotations

import importlib.util
import os
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


@pytest.fixture
def auth_module(monkeypatch):
    install_fake_frappe_modules()

    import frappe

    devices_module = types.ModuleType("kopos_connector.api.devices")
    devices_module.KOPOS_DEVICE_API_ROLE = "KoPOS Device API"
    devices_module.get_session_roles = lambda user=None: ["KoPOS Device API"]
    monkeypatch.setitem(sys.modules, "kopos_connector.api.devices", devices_module)

    module_name = "test_device_api_auth_module"
    module_path = Path(__file__).resolve().parents[1] / "auth.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec and spec.loader

    sys.modules.pop(module_name, None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module, frappe


def test_device_api_user_can_end_its_own_session(auth_module, monkeypatch):
    module, frappe = auth_module
    monkeypatch.setattr(frappe, "session", SimpleNamespace(user="tablet@example.com"))
    monkeypatch.setattr(
        frappe,
        "local",
        SimpleNamespace(
            request=SimpleNamespace(
                path="/api/v2/method/logout", method="POST", content_length=0
            )
        ),
    )

    module.enforce_device_api_restrictions()


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/method/kopos_connector.api.claim_count_task", "POST"),
        ("/api/method/kopos_connector.api.confirm_count_reconciliation", "POST"),
    ],
)
def test_device_api_user_can_use_live_count_commands(auth_module, monkeypatch, path, method):
    module, frappe = auth_module
    monkeypatch.setattr(frappe, "session", SimpleNamespace(user="tablet@example.com"))
    monkeypatch.setattr(
        frappe,
        "local",
        SimpleNamespace(request=SimpleNamespace(path=path, method=method, content_length=0)),
    )

    module.enforce_device_api_restrictions()


def test_device_api_user_cannot_open_desk(auth_module, monkeypatch):
    module, frappe = auth_module
    monkeypatch.setattr(frappe, "session", SimpleNamespace(user="tablet@example.com"))
    monkeypatch.setattr(
        frappe,
        "local",
        SimpleNamespace(
            request=SimpleNamespace(path="/desk", method="GET", content_length=0)
        ),
    )

    with pytest.raises(frappe.ValidationError, match="approved KoPOS device endpoints"):
        module.enforce_device_api_restrictions()


def test_device_api_user_cannot_get_the_logout_endpoint(auth_module, monkeypatch):
    module, frappe = auth_module
    monkeypatch.setattr(frappe, "session", SimpleNamespace(user="tablet@example.com"))
    monkeypatch.setattr(
        frappe,
        "local",
        SimpleNamespace(
            request=SimpleNamespace(
                path="/api/v2/method/logout", method="GET", content_length=0
            )
        ),
    )

    with pytest.raises(frappe.ValidationError, match="approved KoPOS device endpoints"):
        module.enforce_device_api_restrictions()


TABLET_ERP_CLIENT = Path(
    os.environ.get("KOPOS_TYPESCRIPT_ROOT", Path(__file__).resolve().parents[3] / "JiJiPOS" / "kopos" / "src")
) / "services" / "sync" / "erpnext-client.ts"


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/method/kopos_connector.api.get_order_refunds", "GET"),
        ("/api/method/kopos_connector.api.process_remake", "POST"),
        ("/api/method/kopos_connector.api.get_stock_update_status", "GET"),
        ("/api/method/kopos_connector.api.promotions.report_device_promotion_state", "POST"),
    ],
)
def test_device_api_user_can_reach_the_refund_remake_stock_and_promotion_calls(auth_module, monkeypatch, path, method):
    module, frappe = auth_module
    monkeypatch.setattr(frappe, "session", SimpleNamespace(user="tablet@example.com"))
    for request_method, allowed in ((method, True), ("POST" if method == "GET" else "GET", False)):
        monkeypatch.setattr(
            frappe,
            "local",
            SimpleNamespace(request=SimpleNamespace(path=path, method=request_method, content_length=0)),
        )
        if allowed:
            module.enforce_device_api_restrictions()
        else:
            with pytest.raises(frappe.ValidationError, match="approved KoPOS device endpoints"):
                module.enforce_device_api_restrictions()


def test_every_erp_call_in_the_tablet_client_is_allowlisted_with_its_method(auth_module):
    module, _frappe = auth_module
    if not TABLET_ERP_CLIENT.exists():
        pytest.skip("The tablet checkout is not present; the cross-repository gate supplies KOPOS_TYPESCRIPT_ROOT")
    source = TABLET_ERP_CLIENT.read_text()
    calls: dict[str, set[str]] = {}
    for match in re.finditer(r"ERP_API_PREFIX\}\.([a-z_.]+)", source):
        method = re.search(r'method:\s*"([A-Z]+)"', source[match.end():match.end() + 400])
        assert method, f"{match.group(1)} has no explicit HTTP method"
        calls.setdefault(f"/api/method/kopos_connector.api.{match.group(1)}", set()).add(method.group(1))
    assert len(calls) >= 25
    refused = {
        path: sorted(methods)
        for path, methods in calls.items()
        if path not in module.ALLOWED_DEVICE_API_PATHS or not methods <= module.DEVICE_API_HTTP_METHODS[path]
    }
    assert refused == {}


def test_gate_runs_after_frappe_authenticates_tablet_api_keys():
    # Frappe runs before_request hooks before validate_auth() reads API keys, so a
    # gate registered there saw every tablet key request as Guest and let it through.
    import ast

    hooks = ast.parse((Path(__file__).resolve().parents[1] / "hooks.py").read_text())
    assigned = {
        target.id: ast.literal_eval(node.value)
        for node in hooks.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in {"auth_hooks", "before_request"}
    }
    assert assigned.get("auth_hooks") == ["kopos_connector.auth.enforce_device_api_restrictions"]
    assert "kopos_connector.auth.enforce_device_api_restrictions" not in assigned.get("before_request", [])
