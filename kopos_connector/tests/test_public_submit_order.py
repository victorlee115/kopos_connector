from __future__ import annotations

import ast
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()


ERP_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_API_PATH = ERP_ROOT / "api" / "__init__.py"
LEGACY_ORDERS_PATH = ERP_ROOT / "api" / "orders.py"


def _function_node(module: ast.Module, name: str) -> ast.FunctionDef:
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _imported_modules(node: ast.AST) -> set[str]:
    modules: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            modules.update(alias.name for alias in child.names)
        elif isinstance(child, ast.ImportFrom):
            prefix = "." * child.level
            module = child.module or ""
            modules.add(f"{prefix}{module}")
    return modules


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_public_submit_order_routes_to_fb_order_payload_only() -> None:
    tree = ast.parse(PUBLIC_API_PATH.read_text())
    submit_order = _function_node(tree, "submit_order")
    imports = _imported_modules(submit_order)
    source = ast.get_source_segment(PUBLIC_API_PATH.read_text(), submit_order) or ""

    assert "kopos_connector.kopos.api.fb_orders" in imports
    assert ".orders" not in imports
    assert "kopos_connector.api.orders" not in imports
    assert "orders.submit_order_payload" not in source
    assert "submit_order_payload" in _called_names(submit_order)


def test_public_refund_void_and_history_avoid_legacy_orders_module() -> None:
    source = PUBLIC_API_PATH.read_text()
    tree = ast.parse(source)
    expectations = {
        "process_refund": (".fb_returns", "process_return_payload"),
        "void_order": (None, "_process_sales_invoice_void_payload"),
        "get_order_history": (None, "get_order_history_payload"),
        "get_refund_reasons": (None, "_write_response"),
    }

    for function_name, (required_import, required_call) in expectations.items():
        node = _function_node(tree, function_name)
        imports = _imported_modules(node)
        function_source = ast.get_source_segment(source, node) or ""

        assert ".orders" not in imports, function_name
        assert "kopos_connector.api.orders" not in imports, function_name
        assert "process_refund_payload" not in function_source, function_name
        assert "process_void_payload" not in function_source, function_name
        assert "orders." not in function_source, function_name
        if required_import:
            assert required_import in imports, function_name
        assert required_call in _called_names(node), function_name


def test_legacy_orders_module_is_migration_only_and_not_publicly_whitelisted() -> None:
    legacy_source = LEGACY_ORDERS_PATH.read_text()
    public_source = PUBLIC_API_PATH.read_text()

    assert "Legacy POS Invoice helpers retained for migration" in legacy_source
    assert "@frappe.whitelist" not in legacy_source
    assert "from .orders import" not in public_source
    assert "kopos_connector.api.orders" not in public_source


def test_submit_order_wrapper_executes_fb_target(monkeypatch: Any) -> None:
    api = importlib.import_module("kopos_connector.api")
    fb_orders = importlib.import_module("kopos_connector.kopos.api.fb_orders")
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        api,
        "_get_submit_payload",
        lambda kwargs: {
            "order_id": "ORDER-1",
            "idempotency_key": "idem-1",
            "device_id": "DEVICE-1",
            "order": {"display_number": "A001"},
        },
    )
    monkeypatch.setattr(
        api,
        "require_device_operational_scope",
        lambda device_id, **scope: None,
    )
    monkeypatch.setattr(
        fb_orders,
        "submit_order_payload",
        lambda payload: captured.update({"payload": payload})
        or {
            "status": "ok",
            "fb_order": "FB-ORDER-1",
            "sales_invoice": "SINV-1",
            "idempotency_key": payload["idempotency_key"],
            "partial_failure": False,
        },
    )
    monkeypatch.setattr(
        api,
        "_write_response",
        lambda payload, http_status_code=200: captured.update(
            {"response": payload, "http_status_code": http_status_code}
        ),
    )

    api.submit_order()

    assert captured["payload"]["notes"] == "KoPOS display number: A001"
    assert captured["response"]["fb_order"] == "FB-ORDER-1"
    assert captured["response"]["sales_invoice"] == "SINV-1"
    assert "pos_invoice" not in captured["response"]


def test_process_refund_wrapper_executes_fb_return_target(monkeypatch: Any) -> None:
    api = importlib.import_module("kopos_connector.api")
    fb_returns = importlib.import_module("kopos_connector.api.fb_returns")
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        api,
        "_get_submit_payload",
        lambda kwargs: {
            "idempotency_key": "return-1",
            "device_id": "DEVICE-1",
            "original_sales_invoice": "SINV-1",
            "reason_text": "Customer changed mind",
            "lines": [],
        },
    )
    monkeypatch.setattr(api, "require_device_context", lambda device_id: None)
    monkeypatch.setattr(
        api.frappe.db,
        "get_value",
        lambda doctype, name, fieldname: "FB-ORDER-1"
        if (doctype, name, fieldname) == ("Sales Invoice", "SINV-1", "custom_fb_order")
        else None,
    )
    monkeypatch.setattr(
        fb_returns,
        "process_return_payload",
        lambda payload: captured.update({"payload": payload})
        or {"status": "ok", "return_id": payload["return_id"]},
    )
    monkeypatch.setattr(
        api,
        "_write_response",
        lambda payload, http_status_code=200: captured.update(
            {"response": payload, "http_status_code": http_status_code}
        ),
    )

    api.process_refund()

    assert captured["payload"] == {
        "return_id": "return-1",
        "device_id": "DEVICE-1",
        "fb_order": "FB-ORDER-1",
        "original_sales_invoice": "SINV-1",
        "reason_code": "Other",
        "reason_text": "Customer changed mind",
        "return_to_stock": None,
        "lines": [],
    }
    assert captured["response"] == {"status": "ok", "return_id": "return-1"}


def test_void_order_wrapper_executes_sales_invoice_void_target(monkeypatch: Any) -> None:
    api = importlib.import_module("kopos_connector.api")
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        api,
        "_get_submit_payload",
        lambda kwargs: {
            "idempotency_key": "void-1",
            "device_id": "DEVICE-1",
            "sales_invoice": "SINV-1",
        },
    )
    monkeypatch.setattr(api, "require_device_context", lambda device_id: None)
    monkeypatch.setattr(
        api,
        "_process_sales_invoice_void_payload",
        lambda payload: captured.update({"payload": payload})
        or {"status": "ok", "sales_invoice": payload["sales_invoice"]},
    )
    monkeypatch.setattr(
        api,
        "_write_response",
        lambda payload, http_status_code=200: captured.update(
            {"response": payload, "http_status_code": http_status_code}
        ),
    )

    api.void_order()

    assert captured["payload"]["sales_invoice"] == "SINV-1"
    assert captured["response"] == {"status": "ok", "sales_invoice": "SINV-1"}


def test_get_order_history_wrapper_executes_sales_invoice_history_target(
    monkeypatch: Any,
) -> None:
    api = importlib.import_module("kopos_connector.api")
    captured: dict[str, Any] = {}

    monkeypatch.setattr(api, "require_device_context", lambda device_id: None)
    monkeypatch.setattr(api, "mark_device_seen", lambda device_id: None)
    monkeypatch.setattr(
        api,
        "get_order_history_payload",
        lambda **kwargs: captured.update({"kwargs": kwargs})
        or {"status": "ok", "orders": []},
    )
    monkeypatch.setattr(
        api,
        "_write_response",
        lambda payload, http_status_code=200: captured.update(
            {"response": payload, "http_status_code": http_status_code}
        ),
    )

    result = api.get_order_history(device_id="DEVICE-1", since_date="2026-05-16")

    assert captured["kwargs"]["device_id"] == "DEVICE-1"
    assert captured["kwargs"]["since_date"] == "2026-05-16"
    assert captured["response"] == {"status": "ok", "orders": []}
    assert result == {"status": "ok", "orders": []}


def test_get_refund_reasons_does_not_import_legacy_orders(monkeypatch: Any) -> None:
    api = importlib.import_module("kopos_connector.api")
    captured: dict[str, Any] = {}

    monkeypatch.setattr(api, "require_kopos_api_access", lambda: None)
    monkeypatch.setattr(
        api,
        "_write_response",
        lambda payload, http_status_code=200: captured.update(
            {"response": payload, "http_status_code": http_status_code}
        ),
    )

    api.get_refund_reasons()

    assert captured["response"]["refund_reasons"] == [
        {"code": "customer_changed_mind", "label": "Customer changed mind"},
        {"code": "wrong_order", "label": "Wrong order"},
        {"code": "quality_issue", "label": "Quality issue"},
        {"code": "item_damaged", "label": "Item damaged"},
        {"code": "service_issue", "label": "Service issue"},
        {"code": "pricing_error", "label": "Pricing error"},
        {"code": "other", "label": "Other"},
    ]
