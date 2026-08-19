# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

from __future__ import annotations

import importlib.util
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
