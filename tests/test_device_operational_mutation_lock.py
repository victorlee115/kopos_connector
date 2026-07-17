from __future__ import annotations

import ast
from contextlib import ExitStack
import importlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()
devices = importlib.import_module("kopos_connector.api.devices")

CONNECTOR_ROOT = Path(__file__).resolve().parents[1] / "kopos_connector"


def _device_lock_sql(
    query_log: list[str],
    *,
    reset_status: str | None = "",
    current_api_key: str = "current-key",
):
    def execute(query: str, params: tuple[str, ...], **_kwargs: Any):
        normalized = " ".join(query.split())
        query_log.append(normalized)
        if "FROM `tabKoPOS Device`" in normalized:
            if params != ("DEVICE-A",):
                raise AssertionError(f"unexpected device lock params: {params}")
            return [
                {
                    "name": "DEVICE-A",
                    "device_id": "DEVICE-A",
                    "api_user": "device-a@kopos.local",
                    "enabled": 1,
                    "config_version": 8,
                }
            ]
        if "FROM `tabKoPOS Device Safe Reset`" in normalized:
            return (
                [{"name": "KSR-1", "status": reset_status}]
                if reset_status != ""
                else []
            )
        if "FROM `tabUser`" in normalized:
            return [
                {
                    "name": "device-a@kopos.local",
                    "api_key": current_api_key,
                    "enabled": 1,
                }
            ]
        raise AssertionError(f"unexpected SQL: {normalized}")

    return execute


def _function_call_lines(path: Path, function_name: str) -> dict[str, list[int]]:
    tree = ast.parse(path.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )
    calls: dict[str, list[int]] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        called = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        calls.setdefault(called, []).append(node.lineno)
    return calls


class TestDeviceOperationalMutationLock(unittest.TestCase):
    def _patch_device_context(
        self,
        *,
        presented_api_key: str = "current-key",
    ):
        device = SimpleNamespace(
            name="DEVICE-A",
            device_id="DEVICE-A",
            api_user="device-a@kopos.local",
            enabled=1,
            config_version=8,
        )
        request = SimpleNamespace(
            headers={
                "Authorization": f"token {presented_api_key}:presented-api-secret"
            }
        )
        return (
            device,
            patch.object(
                devices.frappe,
                "session",
                SimpleNamespace(user="device-a@kopos.local"),
            ),
            patch.object(devices.frappe, "request", request, create=True),
            patch.object(devices.frappe.local, "request", request, create=True),
            patch.object(
                devices,
                "get_session_roles",
                return_value={devices.KOPOS_DEVICE_API_ROLE},
            ),
            patch.object(devices, "ensure_unique_device_api_user"),
            patch.object(devices.frappe, "get_doc", return_value=device),
        )

    def test_revalidates_presented_token_after_device_and_reset_locks(self) -> None:
        query_log: list[str] = []
        device, *context_patches = self._patch_device_context()
        with ExitStack() as stack:
            for context_patch in context_patches:
                stack.enter_context(context_patch)
            stack.enter_context(
                patch.object(
                devices.frappe.db,
                "sql",
                side_effect=_device_lock_sql(query_log),
                )
            )
            resolved = devices.lock_device_for_operational_mutation("DEVICE-A")

        self.assertIs(resolved, device)
        self.assertEqual(len(query_log), 3)
        self.assertIn("tabKoPOS Device`", query_log[0])
        self.assertIn("tabKoPOS Device Safe Reset`", query_log[1])
        self.assertIn("tabUser`", query_log[2])
        self.assertTrue(all("FOR UPDATE" in query for query in query_log))

    def test_rejects_request_authenticated_before_credential_rotation(self) -> None:
        query_log: list[str] = []
        _, *context_patches = self._patch_device_context(
            presented_api_key="revoked-key"
        )
        with ExitStack() as stack:
            for context_patch in context_patches:
                stack.enter_context(context_patch)
            stack.enter_context(
                patch.object(
                    devices.frappe.db,
                    "sql",
                    side_effect=_device_lock_sql(
                        query_log,
                        current_api_key="rotated-key",
                    ),
                )
            )
            stack.enter_context(
                self.assertRaisesRegex(
                devices.frappe.ValidationError,
                "credentials changed",
                )
            )
            devices.lock_device_for_operational_mutation("DEVICE-A")

        self.assertIn("tabKoPOS Device`", query_log[0])
        self.assertIn("tabUser`", query_log[-1])

    def test_rejects_active_unknown_and_null_reset_before_other_locks(self) -> None:
        for reset_status in ("authorized", None, "future_lifecycle_state"):
            query_log: list[str] = []
            _, *context_patches = self._patch_device_context()
            with self.subTest(reset_status=reset_status), ExitStack() as stack:
                for context_patch in context_patches:
                    stack.enter_context(context_patch)
                stack.enter_context(
                    patch.object(
                        devices.frappe.db,
                        "sql",
                        side_effect=_device_lock_sql(
                            query_log,
                            reset_status=reset_status,
                        ),
                    )
                )
                stack.enter_context(
                    self.assertRaisesRegex(
                        devices.frappe.ValidationError,
                        "active or unresolved safe reset",
                    )
                )
                devices.lock_device_for_operational_mutation("DEVICE-A")

            self.assertEqual(len(query_log), 2)
            self.assertIn("tabKoPOS Device`", query_log[0])
            self.assertIn("tabKoPOS Device Safe Reset`", query_log[1])
            self.assertIn("status IS NULL", query_log[1])
            self.assertIn(
                "status NOT IN ('completed', 'cancelled', 'expired')",
                query_log[1],
            )

    def test_seen_telemetry_cannot_write_when_mutation_lock_rejects(self) -> None:
        with (
            patch.object(
                devices,
                "lock_device_for_operational_mutation",
                side_effect=devices.frappe.ValidationError("stale credential"),
            ) as lock_device,
            patch.object(devices.frappe.db, "set_value") as set_value,
            self.assertRaisesRegex(
                devices.frappe.ValidationError,
                "stale credential",
            ),
        ):
            devices.mark_device_seen(device_id="DEVICE-A")

        lock_device.assert_called_once_with(device_id="DEVICE-A", name=None)
        set_value.assert_not_called()

    def test_all_device_mutation_routes_lock_before_business_mutation(self) -> None:
        guarded_routes = {
            "api/__init__.py": {
                "prepare_automatic_qr_sale": "prepare_automatic_qr_sale_payload",
                "cancel_prepared_automatic_qr_sale": "cancel_prepared_automatic_qr_sale_payload",
                "submit_order": "submit_order_payload",
                "open_shift": "open_shift_payload",
                "close_shift": "close_shift_payload",
                "void_order": "_process_sales_invoice_void_payload",
                "process_refund": "process_return_payload",
                "request_shift_manager_approval": "_resolve_manager_approval_scope",
            },
            "api/fb_orders.py": {
                "submit_order": "submit_order_payload",
                "retry_failed_projections": "retry_failed_projections",
            },
            "api/fb_returns.py": {"process_return": "process_return_payload"},
            "api/fb_refill.py": {"process_refill": "_build_refill_request"},
            "api/fb_waste.py": {"process_waste": "_build_waste_event"},
            "api/fb_remakes.py": {"process_remake": "_build_remake_event"},
        }

        for relative_path, routes in guarded_routes.items():
            path = CONNECTOR_ROOT / relative_path
            for function_name, business_call in routes.items():
                with self.subTest(path=relative_path, function=function_name):
                    calls = _function_call_lines(path, function_name)
                    guard_lines = calls.get(
                        "lock_device_for_operational_mutation", []
                    )
                    business_lines = calls.get(business_call, [])
                    self.assertTrue(guard_lines)
                    self.assertTrue(business_lines)
                    self.assertLess(max(guard_lines), min(business_lines))

        manual_path = CONNECTOR_ROOT / "api/manual_qr_receipt.py"
        resolver_calls = _function_call_lines(
            manual_path,
            "_resolve_authorized_device",
        )
        upload_calls = _function_call_lines(
            manual_path,
            "upload_manual_qr_receipt",
        )
        revalidation_calls = _function_call_lines(
            manual_path,
            "_revalidate_receipt_device_authority",
        )
        self.assertTrue(
            resolver_calls.get("require_device_context")
        )
        self.assertTrue(
            revalidation_calls.get("lock_device_for_operational_mutation")
        )
        self.assertLess(
            max(upload_calls["_resolve_authorized_device"]),
            min(upload_calls["_read_and_validate_jpeg"]),
        )
        self.assertLess(
            max(upload_calls["_read_and_validate_jpeg"]),
            min(upload_calls["_revalidate_receipt_device_authority"]),
        )
        self.assertLess(
            max(upload_calls["_revalidate_receipt_device_authority"]),
            min(upload_calls["_load_and_validate_transaction"]),
        )

        telemetry_calls = _function_call_lines(
            CONNECTOR_ROOT / "api/devices.py",
            "mark_device_seen",
        )
        self.assertLess(
            max(telemetry_calls["lock_device_for_operational_mutation"]),
            min(telemetry_calls["set_value"]),
        )

    def test_maybank_routes_call_provider_outside_broad_device_lock(self) -> None:
        api_path = CONNECTOR_ROOT / "api/__init__.py"
        for function_name, business_call in {
            "generate_maybank_qr": "generate_maybank_qr_payload",
            "check_maybank_payment": "check_maybank_payment_payload",
        }.items():
            with self.subTest(function=function_name):
                calls = _function_call_lines(api_path, function_name)
                preflight = calls["require_device_operational_scope"]
                business = calls[business_call]
                fence = calls["_revalidate_maybank_device_authority"]
                self.assertLess(max(preflight), min(business))
                self.assertLess(max(business), min(fence))

        revalidation_calls = _function_call_lines(
            api_path,
            "_revalidate_maybank_device_authority",
        )
        self.assertTrue(
            revalidation_calls.get("lock_device_for_operational_mutation")
        )


if __name__ == "__main__":
    unittest.main()
