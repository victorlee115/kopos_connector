from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
import sys
import types
import unittest


class TestInventoryShortfallExceptionContract(unittest.TestCase):
    def test_warning_service_has_no_active_legacy_override_writer(self) -> None:
        source_path = (
            Path(__file__).parents[1]
            / "kopos"
            / "services"
            / "inventory"
            / "warning_service.py"
        )
        source = source_path.read_text(encoding="utf-8")
        self.assertNotIn("FB Stock Override Log", source)
        self.assertIn("record_stock_shortfall_exceptions", source)

        tree = ast.parse(source)
        forbidden_calls: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in {"new_doc", "insert", "submit"}:
                forbidden_calls.append(node.func.attr)
        self.assertEqual(forbidden_calls, [])

    def test_shortfall_diagnostic_uses_finite_decimal_and_skips_bad_input(self) -> None:
        fake_frappe = types.ModuleType("frappe")
        fake_frappe.db = types.SimpleNamespace(
            get_value=lambda _doctype, _filters, _fieldname: "1.00",
        )
        fake_frappe.log_error = lambda *args, **kwargs: None
        previous = sys.modules.get("frappe")
        sys.modules["frappe"] = fake_frappe
        try:
            sys.modules.pop(
                "kopos_connector.kopos.services.inventory.warning_service", None
            )
            from kopos_connector.kopos.services.inventory import warning_service

            shortfalls = warning_service.detect_stock_shortfall(
                [
                    {
                        "item": "ITEM-1",
                        "warehouse": "WH-1",
                        "stock_qty": "1.25",
                        "affects_stock": 1,
                    },
                    {
                        "item": "ITEM-1",
                        "warehouse": "WH-1",
                        "stock_qty": "NaN",
                        "affects_stock": 1,
                    },
                ]
            )
            self.assertEqual(len(shortfalls), 1)
            self.assertIsInstance(shortfalls[0]["required_qty"], Decimal)
            self.assertEqual(shortfalls[0]["required_qty"], Decimal("1.25"))
            self.assertEqual(shortfalls[0]["available_qty"], Decimal("1.00"))
        finally:
            sys.modules.pop(
                "kopos_connector.kopos.services.inventory.warning_service", None
            )
            if previous is not None:
                sys.modules["frappe"] = previous
            else:
                sys.modules.pop("frappe", None)


if __name__ == "__main__":
    unittest.main()
